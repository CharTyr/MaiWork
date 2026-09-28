"""专用 SSH 机器执行环境（environments/ssh.py）。

用户在 [environments] ssh 里列自己的 VPS / VM（name / host=user@地址:端口 / note），
MaiWork 用**插件自己生成的 key**（<data_dir>/ssh/id_ed25519，公钥在网页上给用户去
authorized_keys 里加）连上去干活：
- 按列表顺序挑第一台「连得上、这会儿没活」的机器；一台机器同时只接一个任务；
- 每个任务一个远端目录 ~/maiwork/<任务ID>/，命令都在里面跑；
- 传文件 / 取文件只收这个目录下的相对路径（白名单字符，不许 .. / 绝对路径）；
- host 写法严格校验（防参数注入：不许 - 开头、不许空格等）；
- 子进程最小环境、BatchMode（不交互）、accept-new 记住主机指纹。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.environments.ssh import SshEnv, parse_host


def _settings(machines: list[dict] | None = None) -> Any:
    s, _ = load_settings({"environments": {"ssh": machines or []}})
    return s


class FakeRunner:
    """按命令特征回 (code, out, err)；记录每次 argv。"""

    def __init__(self, handler=None) -> None:
        self.calls: list[list[str]] = []
        self.handler = handler

    async def run(self, argv, *, cwd, env, timeout):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "ssh-keygen":
            key = Path(argv[argv.index("-f") + 1])
            key.write_text("PRIVATE")
            key.with_suffix(".pub").write_text("ssh-ed25519 AAAATEST maiwork\n")
            return 0, "", ""
        if self.handler is not None:
            return self.handler(argv)
        joined = " ".join(argv)
        if "mkdir -p" in joined and "pwd -P" in joined:
            return 0, "/home/u/maiwork/T-1\n", ""
        return 0, "ok\n", ""


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- host 解析


@pytest.mark.parametrize("raw,want", [
    ("root@1.2.3.4", ("root", "1.2.3.4", 22)),
    ("u_1@my-box.example.com:2222", ("u_1", "my-box.example.com", 2222)),
    ("1.2.3.4", ("", "1.2.3.4", 22)),
])
def test_parse_host_ok(raw, want) -> None:
    assert parse_host(raw) == want


@pytest.mark.parametrize("raw", [
    "", "-oProxyCommand=evil", "root@-x", "root@host;rm -rf /", "root@host name", "root@host:0",
    "root@host:70000", "a b@host", "root@host:22:33",
])
def test_parse_host_rejects(raw) -> None:
    with pytest.raises(ValueError):
        parse_host(raw)


# ---------------------------------------------------------------- key


def test_ensure_key_generates_once_and_returns_pubkey(tmp_path) -> None:
    runner = FakeRunner()
    env = SshEnv(lambda: _settings(), tmp_path, runner=runner.run)
    pub = _run(env.ensure_key())
    assert pub.startswith("ssh-ed25519 ")
    assert (tmp_path / "ssh" / "id_ed25519").exists()
    n = len(runner.calls)
    assert _run(env.ensure_key()) == pub
    assert len(runner.calls) == n  # 已有就不再生成
    assert env.public_key() == pub


# ---------------------------------------------------------------- acquire / 顺序 / 忙


def test_acquire_picks_first_reachable_in_order_and_marks_busy(tmp_path) -> None:
    def handler(argv):
        dest = next(a for a in argv if "@" in a or a in ("10.0.0.1", "10.0.0.2"))
        joined = " ".join(argv)
        if "a@10.0.0.1" in dest:
            return 255, "", "ssh: connect to host 10.0.0.1 port 22: Connection refused"
        if "mkdir -p" in joined:
            return 0, "/home/b/maiwork/T-1\n", ""
        return 0, "ok\n", ""

    runner = FakeRunner(handler)
    env = SshEnv(lambda: _settings([
        {"name": "甲", "host": "a@10.0.0.1"}, {"name": "乙", "host": "b@10.0.0.2:2200"},
    ]), tmp_path, runner=runner.run)
    box = _run(env.acquire("T-1"))
    assert box is not None and box.name == "乙" and box.kind == "ssh"
    assert box.workdir == "/home/b/maiwork/T-1"
    ssh_calls = [c for c in runner.calls if c[0] == "ssh"]
    assert all("BatchMode=yes" in c and "IdentitiesOnly=yes" in c for c in ssh_calls)
    assert any("-p" in c and "2200" in c for c in ssh_calls)
    # 同一台忙着：第二个任务只能拿别的（甲连不上）→ None，原因写清
    assert _run(env.acquire("T-2")) is None
    assert "乙" in env.last_fail()["reason"] and "甲" in env.last_fail()["reason"]
    # 放掉之后又能拿
    _run(env.release(box))
    assert _run(env.acquire("T-3")) is not None


def test_acquire_none_when_no_machines(tmp_path) -> None:
    env = SshEnv(lambda: _settings(), tmp_path, runner=FakeRunner().run)
    assert env.available() is False
    assert _run(env.acquire("T-1")) is None


def test_bad_job_id_rejected(tmp_path) -> None:
    env = SshEnv(lambda: _settings([{"name": "甲", "host": "a@10.0.0.1"}]), tmp_path, runner=FakeRunner().run)
    with pytest.raises(ValueError):
        _run(env.acquire("../x"))


def test_unsafe_remote_workdir_refused(tmp_path) -> None:
    def handler(argv):
        if "mkdir -p" in " ".join(argv):
            return 0, "/home/we ird/maiwork/T-1\n", ""
        return 0, "ok\n", ""
    env = SshEnv(lambda: _settings([{"name": "甲", "host": "a@10.0.0.1"}]), tmp_path, runner=FakeRunner(handler).run)
    assert _run(env.acquire("T-1")) is None
    assert "工作目录" in env.last_fail()["reason"]


# ---------------------------------------------------------------- run / put / get


def _box(tmp_path, runner):
    env = SshEnv(lambda: _settings([{"name": "甲", "host": "a@10.0.0.1:2222"}]), tmp_path, runner=runner.run)
    box = _run(env.acquire("T-1"))
    assert box is not None
    return env, box


def test_run_in_workdir_with_timeout_and_remote_exit_code(tmp_path) -> None:
    def handler(argv):
        joined = " ".join(argv)
        if "mkdir -p" in joined and "pwd -P" in joined:
            return 0, "/home/u/maiwork/T-1\n", ""
        if "echo hi" in joined:
            return 3, "hi\n", "warn\n"
        return 0, "ok\n", ""
    runner = FakeRunner(handler)
    env, box = _box(tmp_path, runner)
    r = _run(env.run(box, "echo hi", timeout_s=30))
    assert r.exit_code == 3 and "hi" in r.stdout and "warn" in r.stderr
    remote_cmd = runner.calls[-1][-1]
    assert remote_cmd.startswith("cd /home/u/maiwork/T-1 && ")
    assert "timeout -k 5 30" in remote_cmd
    assert "'echo hi'" in remote_cmd  # 命令整体 shlex.quote


def test_put_and_get_only_relative_safe_paths(tmp_path) -> None:
    runner = FakeRunner()
    env, box = _box(tmp_path, runner)
    local = tmp_path / "s.py"
    local.write_text("print(1)")
    _run(env.put(box, local, "scripts/s.py"))
    scp = runner.calls[-1]
    assert scp[0] == "scp" and "-P" in scp and "2222" in scp
    assert scp[-1] == "a@10.0.0.1:/home/u/maiwork/T-1/scripts/s.py"
    out = tmp_path / "got" / "r.txt"
    _run(env.get(box, "out/r.txt", out))
    assert runner.calls[-1][-2] == "a@10.0.0.1:/home/u/maiwork/T-1/out/r.txt"
    for bad in ("/etc/passwd", "../x", "a/../../b", "a b.txt", "x;rm", ""):
        with pytest.raises(ValueError):
            _run(env.put(box, local, bad))
        with pytest.raises(ValueError):
            _run(env.get(box, bad, out))


def test_minimal_env_for_subprocess(tmp_path) -> None:
    seen = {}

    async def runner(argv, *, cwd, env, timeout):
        seen.update(env)
        if argv[0] == "ssh-keygen":
            key = Path(argv[argv.index("-f") + 1]); key.write_text("K"); key.with_suffix(".pub").write_text("ssh-ed25519 X\n")
            return 0, "", ""
        return 0, "/home/u/maiwork/T-1\n", ""

    env = SshEnv(lambda: _settings([{"name": "甲", "host": "a@10.0.0.1"}]), tmp_path, runner=runner)
    _run(env.acquire("T-1"))
    assert set(seen) <= {"PATH", "LANG", "HOME"}


# ---------------------------------------------------------------- 连通检查（网页健康项用）


def test_check_all_records_status(tmp_path) -> None:
    def handler(argv):
        if "a@10.0.0.1" in argv:
            return 255, "", "Permission denied (publickey)."
        return 0, "ok\n", ""
    env = SshEnv(lambda: _settings([
        {"name": "甲", "host": "a@10.0.0.1"}, {"name": "乙", "host": "b@10.0.0.2"},
    ]), tmp_path, runner=FakeRunner(handler).run)
    _run(env.check_all())
    st = {m["name"]: m for m in env.status()}
    assert st["甲"]["ok"] is False and "公钥" in st["甲"]["error"]
    assert st["乙"]["ok"] is True


def test_box_for_tracks_task_until_release(tmp_path) -> None:
    runner = FakeRunner()
    env = SshEnv(lambda: _settings([{"name": "甲", "host": "a@10.0.0.1"}]), tmp_path, runner=runner.run)
    assert env.box_for("T-1") is None
    box = _run(env.acquire("T-1"))
    assert env.box_for("T-1") is box
    _run(env.release(box))
    assert env.box_for("T-1") is None


def test_acquire_prefers_named_machine_then_falls_back_in_order(tmp_path) -> None:
    runner = FakeRunner()
    env = SshEnv(lambda: _settings([
        {"name": "甲", "host": "a@10.0.0.1"}, {"name": "乙", "host": "b@10.0.0.2", "note": "有 GPU"},
    ]), tmp_path, runner=runner.run)
    box = _run(env.acquire("T-1", prefer="乙"))
    assert box.name == "乙"
    box2 = _run(env.acquire("T-2", prefer="乙"))  # 乙忙着 → 按顺序换甲
    assert box2.name == "甲"
    assert [m.get("note") for m in env.machines()] == ["", "有 GPU"]


def test_web_settings_reject_bad_host() -> None:
    from CharTyr_MaiWork.maiwork.rules import _check_ssh_list

    assert _check_ssh_list([{"name": "甲", "host": "u@1.2.3.4:2222", "note": ""}])[0]["host"] == "u@1.2.3.4:2222"
    with pytest.raises(ValueError):
        _check_ssh_list([{"name": "甲", "host": "-oProxyCommand=x"}])
    with pytest.raises(ValueError):
        _check_ssh_list([{"name": "甲", "host": "u@a;b"}])
