"""专用 SSH 机器上的子 agent 工具（tools_ssh.py）：machine_run / machine_put_file /
machine_read_file / machine_fetch_file。

- 机器按任务分（get_box(task_id)）：同时可能有几个任务各占一台机器；
- 路径一律是「这次工作目录下的相对路径」，由 SshEnv 校验；
- 上传只收本机工作区里的文件（绝对路径、..、符号链接逃逸都拒）；
- 拷回只落到本机工作区 artifacts/<任务ID>/ 下，单文件 ≤20MB；
- 没分到机器 → 中文失败。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from CharTyr_MaiWork.maiwork.environments.local import RunResult
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import ToolContext, Tools
from CharTyr_MaiWork.maiwork.tools_ssh import MACHINE_TOOLS, register_machine_tools


class FakeBox:
    def __init__(self, name="甲"):
        self.name = name
        self.workdir = "/home/u/maiwork/T-1"


class FakeSsh:
    def __init__(self):
        self.calls: list[tuple] = []
        self.files: dict[str, bytes] = {}

    async def run(self, box, command, *, timeout_s):
        self.calls.append(("run", box.name, command, timeout_s))
        return RunResult(exit_code=0, stdout="hi\n", stderr="", ms=5, timed_out=False, oom=False)

    async def put(self, box, local, rel):
        if rel.startswith("/") or ".." in rel:
            raise ValueError("路径不合法")
        self.calls.append(("put", box.name, Path(local).name, rel))
        self.files[rel] = Path(local).read_bytes()

    async def get(self, box, rel, local):
        if rel.startswith("/") or ".." in rel:
            raise ValueError("路径不合法")
        self.calls.append(("get", box.name, rel))
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        Path(local).write_bytes(self.files.get(rel, b"content of " + rel.encode()))


class FakeLocalEnv:
    def __init__(self, root: Path):
        self.root = root

    def resolve(self, name, rel):
        p = (self.root / name / rel)
        if ".." in Path(rel).parts:
            raise PermissionError("越界")
        return p


def _setup(tmp_path, boxes: dict[str, Any]):
    store = Store(tmp_path / "t.db")
    store.migrate()
    tools = Tools(store)
    env = FakeSsh()
    register_machine_tools(tools, get_box=lambda tid: boxes.get(tid), env=env,
                           local_env=FakeLocalEnv(tmp_path / "ws"), tmp_dir=tmp_path / "tmp")
    ws = tmp_path / "ws" / "g1"
    ws.mkdir(parents=True)
    return tools, env, ws


def _call(tools, name, args, ctx):
    return asyncio.run(tools.call(name, args, ctx))


def test_tool_names_registered_for_worker_only(tmp_path) -> None:
    tools, _env, _ws = _setup(tmp_path, {})
    names = {s["function"]["name"] for s in tools.specs("worker")}
    assert set(MACHINE_TOOLS) <= names
    assert not set(MACHINE_TOOLS) & {s["function"]["name"] for s in tools.specs("main")}


def test_no_box_fails_in_chinese(tmp_path) -> None:
    tools, _env, ws = _setup(tmp_path, {})
    ctx = ToolContext(group_id="1", task_id="T-9", actor="子 agent", role="worker", workspace=ws)
    r = _call(tools, "machine_run", {"command": "ls"}, ctx)
    assert not r.ok and "专用机器" in r.error


def test_run_uses_task_box_and_caps_timeout(tmp_path) -> None:
    tools, env, ws = _setup(tmp_path, {"T-1": FakeBox("甲"), "T-2": FakeBox("乙")})
    ctx = ToolContext(group_id="1", task_id="T-2", actor="子 agent", role="worker", workspace=ws)
    r = _call(tools, "machine_run", {"command": "echo hi", "timeout_s": 99999}, ctx)
    assert r.ok and "退出码 0" in r.output and "hi" in r.output
    assert env.calls[-1][:3] == ("run", "乙", "echo hi")
    assert env.calls[-1][3] <= 1800


def test_put_only_workspace_files(tmp_path) -> None:
    tools, env, ws = _setup(tmp_path, {"T-1": FakeBox()})
    (ws / "s.py").write_text("print(1)")
    ctx = ToolContext(group_id="1", task_id="T-1", actor="子 agent", role="worker", workspace=ws)
    r = _call(tools, "machine_put_file", {"path": "s.py", "remote_path": "scripts/s.py"}, ctx)
    assert r.ok, r.error
    assert env.files["scripts/s.py"] == b"print(1)"
    for bad in ("/etc/passwd", "../x.py"):
        r = _call(tools, "machine_put_file", {"path": bad}, ctx)
        assert not r.ok
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    (ws / "link.txt").symlink_to(outside)
    r = _call(tools, "machine_put_file", {"path": "link.txt"}, ctx)
    assert not r.ok and "越界" in r.error


def test_read_and_fetch(tmp_path) -> None:
    tools, env, ws = _setup(tmp_path, {"T-1": FakeBox()})
    env.files["out/report.md"] = b"# hello"
    ctx = ToolContext(group_id="1", task_id="T-1", actor="子 agent", role="worker", workspace=ws)
    r = _call(tools, "machine_read_file", {"path": "out/report.md"}, ctx)
    assert r.ok and "# hello" in r.output
    r = _call(tools, "machine_fetch_file", {"remote_path": "out/report.md"}, ctx)
    assert r.ok, r.error
    assert (tmp_path / "ws" / "g1" / "artifacts" / "T-1" / "report.md").read_bytes() == b"# hello"
    r = _call(tools, "machine_fetch_file", {"remote_path": "out/report.md", "local_name": "../x"}, ctx)
    assert not r.ok
    r = _call(tools, "machine_read_file", {"path": "/etc/passwd"}, ctx)
    assert not r.ok


def test_fetch_too_big_removed(tmp_path) -> None:
    tools, env, ws = _setup(tmp_path, {"T-1": FakeBox()})
    env.files["big.bin"] = b"0" * (20 * 1024 * 1024 + 1)
    ctx = ToolContext(group_id="1", task_id="T-1", actor="子 agent", role="worker", workspace=ws)
    r = _call(tools, "machine_fetch_file", {"remote_path": "big.bin"}, ctx)
    assert not r.ok and "20MB" in r.error
    assert not (tmp_path / "ws" / "g1" / "artifacts" / "T-1" / "big.bin").exists()
