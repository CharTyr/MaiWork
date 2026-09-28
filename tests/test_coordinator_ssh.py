"""派活选执行环境：本机 / 专用机器（ssh）/ 一次性 VM（railway）。

- 专用机器是用户自己的 VPS / VM：要跑命令、装依赖、跑得久的活优先它；
- 回落顺序：选 ssh → ssh、railway、本机（本机不能跑命令就判失败）；
  选 railway → railway、ssh、本机；选 local 但本机受限 → ssh、railway、判失败；
- 拿到专用机器：env 字段写「专用机器 · 名字」，工具换成 machine_*，brief 说清怎么把成品拷回来；
- 结束一定释放那台机器（放给下一个任务）。
"""

from __future__ import annotations

import asyncio
import types

import pytest

from CharTyr_MaiWork.maiwork.coordinator import Coordinator


class _Dec:
    def __init__(self, ok):
        self.ok = ok
        self.reason = "" if ok else "这台机器不是 Linux"


class _SshBox:
    kind = "ssh"
    name = "甲"
    workdir = "/home/u/maiwork/t1"
    expires_ts = 0.0


class _RailBox:
    expires_ts = 0


def _coord(*, local_ok=True, ssh_box=None, rail_box=None, ssh_on=True, rail_on=True):
    c = Coordinator.__new__(Coordinator)
    c._capability = _Dec(local_ok)
    c._get_settings = lambda: types.SimpleNamespace(environments=types.SimpleNamespace(
        railway=rail_on, run_as="maiwork", memory_max="512M"))
    released: list = []

    class _Ssh:
        def available(self):
            return ssh_on

        def machines(self):
            return [{"name": "甲", "host": "u@10.0.0.1"}]

        async def acquire(self, tid):
            return ssh_box

        def last_fail(self):
            return {"reason": "甲：连不上"}

        async def release(self, box):
            released.append(("ssh", box))

    class _Rail:
        async def acquire(self, tid):
            return rail_box

        def last_fail(self):
            return {"reason": "今天的配额用完了"}

        async def release(self, box):
            released.append(("railway", box))

    c._ssh = _Ssh() if ssh_on else None
    c._railway = _Rail() if rail_on else None
    envs, notes = [], []

    class _Tasks:
        def set_env(self, tid, desc, note=""):
            envs.append(desc)
            if note:
                notes.append(note)

    c._tasks = _Tasks()
    return c, envs, notes, released


def _run(coro):
    return asyncio.run(coro)


def test_want_ssh_gets_machine() -> None:
    c, envs, notes, _ = _coord(ssh_box=_SshBox())
    on, box = _run(c._setup_exec_env("t1", {"env": "ssh"}, "1"))
    assert on is True and box.kind == "ssh"
    assert "专用机器" in envs[-1] and "甲" in envs[-1]


def test_want_ssh_falls_to_railway_then_local() -> None:
    c, envs, notes, _ = _coord(ssh_box=None, rail_box=_RailBox())
    on, box = _run(c._setup_exec_env("t1", {"env": "ssh"}, "1"))
    assert on is True and isinstance(box, _RailBox)
    assert "专用机器拿不到" in notes[-1] and "甲：连不上" in notes[-1]
    c, envs, notes, _ = _coord(ssh_box=None, rail_box=None)
    on, box = _run(c._setup_exec_env("t1", {"env": "ssh"}, "1"))
    assert on is False and box is None
    assert "本机" in envs[-1] and "专用机器拿不到" in notes[-1]


def test_want_railway_falls_to_ssh() -> None:
    c, envs, notes, _ = _coord(ssh_box=_SshBox(), rail_box=None)
    on, box = _run(c._setup_exec_env("t1", {"env": "railway"}, "1"))
    assert on is True and box.kind == "ssh"
    assert "一次性机器拿不到" in notes[-1]


def test_stopped_local_prefers_ssh_then_railway_then_fail() -> None:
    c, envs, notes, _ = _coord(local_ok=False, ssh_box=_SshBox(), rail_box=_RailBox())
    on, box = _run(c._setup_exec_env("t1", {"env": "local"}, "1"))
    assert on is True and box.kind == "ssh"
    assert "不能隔离跑命令" in notes[-1]
    c, envs, notes, _ = _coord(local_ok=False, ssh_box=None, rail_box=_RailBox())
    on, box = _run(c._setup_exec_env("t1", {"env": "local"}, "1"))
    assert on is True and isinstance(box, _RailBox)
    c, envs, notes, _ = _coord(local_ok=False, ssh_box=None, rail_box=None)
    on, box = _run(c._setup_exec_env("t1", {"env": "local"}, "1"))
    assert on is False and box is None
    assert "做不了" in notes[-1] and "受限" in envs[-1]


def test_local_ok_want_local_does_not_touch_machines() -> None:
    c, envs, notes, _ = _coord(ssh_box=_SshBox())
    on, box = _run(c._setup_exec_env("t1", {"env": "local"}, "1"))
    assert on is False and box is None and not notes


def test_tools_brief_release_for_ssh() -> None:
    tools = Coordinator._remote_job_tools(["run_command", "read_file", "web_search"], _SshBox())
    assert "run_command" not in tools and "machine_run" in tools and "machine_fetch_file" in tools
    assert "vm_run" not in tools and "web_search" in tools and "write_file" in tools
    rail = Coordinator._remote_job_tools(["run_command"], _RailBox())
    assert "vm_run" in rail and "machine_run" not in rail
    c, envs, notes, released = _coord(ssh_box=_SshBox())
    c._artifact_dir = lambda tid: f"artifacts/{tid}"
    brief = c._enrich_brief("做个东西", "t1", "file", _SshBox())
    assert "machine_fetch_file" in brief and "专用机器" in brief and "vm_" not in brief
    _run(c._release_remote(_SshBox()))
    _run(c._release_remote(_RailBox()))
    assert [k for k, _ in released] == ["ssh", "railway"]


def test_plan_env_options_mention_ssh_only_when_available() -> None:
    c, *_ = _coord(ssh_box=_SshBox(), ssh_on=True, rail_on=False)
    field, guide, allowed = c._env_options()
    assert '"local|ssh"' in field and "ssh" in allowed and "railway" not in allowed
    assert "专用机器" in guide and "优先" in guide and "railway" not in guide
    c, *_ = _coord(ssh_on=False, rail_on=True)
    c._railway_available = lambda: True
    field, guide, allowed = c._env_options()
    assert '"local|railway"' in field and "ssh" not in allowed and "专用机器" not in guide
    c, *_ = _coord(ssh_on=False, rail_on=False)
    c._railway_available = lambda: False
    field, guide, allowed = c._env_options()
    assert allowed == {"local"} and guide == ""


def test_plan_env_choice_normalized() -> None:
    c, *_ = _coord(ssh_on=True, rail_on=False)
    c._railway_available = lambda: False
    assert c._normalize_env_choice("ssh") == "ssh"
    assert c._normalize_env_choice("railway") == "ssh"   # 没开 railway，要机器就给专用机器
    assert c._normalize_env_choice("乱写") == "local"
    c, *_ = _coord(ssh_on=False, rail_on=False)
    c._railway_available = lambda: False
    assert c._normalize_env_choice("ssh") == "local"


def test_plan_prompt_lists_machines_with_notes_and_points_to_agents_md() -> None:
    c, *_ = _coord(ssh_on=True, rail_on=False)
    c._railway_available = lambda: False
    c._ssh.machines = lambda: [{"name": "甲", "host": "u@1", "note": "4 核 8G"}, {"name": "乙", "host": "u@2", "note": ""}]
    field, guide, _ = c._env_options()
    assert '"machine"' in field
    assert "甲（4 核 8G）" in guide and "乙" in guide
    assert "AGENTS" in guide or "做事规矩" in guide


def test_setup_exec_env_passes_preferred_machine() -> None:
    seen = {}
    c, envs, notes, _ = _coord(ssh_box=_SshBox())

    async def _acq(tid, prefer=""):
        seen["prefer"] = prefer
        return _SshBox()

    c._ssh.acquire = _acq
    on, box = _run(c._setup_exec_env("t1", {"env": "ssh", "machine": "乙"}, "1"))
    assert on and seen["prefer"] == "乙"


def test_agents_default_template_explains_machines() -> None:
    from CharTyr_MaiWork.maiwork.identity import _AGENTS_DEFAULT

    assert "专用机器" in _AGENTS_DEFAULT
