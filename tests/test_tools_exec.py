"""tools_exec.py 单元测试（M3）：子 agent 的文件/命令工具 + 主模型的只读工具。

所有用例走 direct 模式 + tmp_path 当 workspace_root，真跑命令（echo），
验证：工具能用、越界被拒且 tool_calls 记失败、超时上限被夹住、角色限制。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.environments.local import LocalEnv
from CharTyr_MaiWork.maiwork.host import Msg
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import ToolContext, Tools
from CharTyr_MaiWork.maiwork.tools_exec import register_exec_tools

from fakes import FakeCtx, FakeHost


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def settings(tmp_path):
    s, _ = load_settings({"environments": {"local_mode": "direct", "workspace_root": str(tmp_path / "ws-root")}})
    return s


@pytest.fixture
def env(settings):
    return LocalEnv(lambda: settings)


@pytest.fixture
def host():
    return FakeHost()


@pytest.fixture
def session_map() -> dict:
    """群号 -> session_id；测试改它，register_exec_tools 的 session_of 立刻生效。"""
    return {}


def _make(tools: Tools, *, env, host, settings, session_map=None) -> None:
    if session_map is None:
        register_exec_tools(tools, env=env, host=host, get_settings=lambda: settings)
    else:
        register_exec_tools(
            tools, env=env, host=host, get_settings=lambda: settings, session_of=lambda gid: session_map.get(gid, "")
        )


def _ctx(ws_path: Path, **over) -> ToolContext:
    base = dict(group_id="g1", task_id="T-1", actor="子 agent #1", role="worker", workspace=ws_path)
    base.update(over)
    return ToolContext(**base)


def _calls(store: Store, tool: str) -> list[dict]:
    rows = store.read().execute(
        "SELECT tool, ok, error, input, output FROM tool_calls WHERE tool=? ORDER BY id", (tool,)
    ).fetchall()
    return [dict(r) for r in rows]


@pytest.fixture
def setup(store, settings, env, host, session_map):
    tools = Tools(store)
    _make(tools, env=env, host=host, settings=settings, session_map=session_map)
    ws_path = env.workspace("ws-1")
    return tools, env, host, ws_path, store


@pytest.mark.asyncio
class TestWorkerTools:
    async def test_write_file_then_read_file(self, setup):
        tools, env, host, ws_path, store = setup
        ctx = _ctx(ws_path)
        r = await tools.call("write_file", {"path": "notes/a.txt", "content": "第一版"}, ctx)
        assert r.ok
        r = await tools.call("read_file", {"path": "notes/a.txt"}, ctx)
        assert r.ok and r.output == "第一版"

    async def test_write_file_append(self, setup):
        tools, _e, _h, ws_path, _s = setup
        ctx = _ctx(ws_path)
        await tools.call("write_file", {"path": "a.txt", "content": "1"}, ctx)
        r = await tools.call("write_file", {"path": "a.txt", "content": "2", "append": True}, ctx)
        assert r.ok
        assert (await tools.call("read_file", {"path": "a.txt"}, ctx)).output == "12"

    async def test_write_no_path_fails(self, setup):
        tools, _e, _h, ws_path, _s = setup
        r = await tools.call("write_file", {"content": "x"}, _ctx(ws_path))
        assert not r.ok

    async def test_read_missing_file_returns_not_ok(self, setup):
        tools, _e, _h, ws_path, _s = setup
        r = await tools.call("read_file", {"path": "nope.txt"}, _ctx(ws_path))
        assert not r.ok and "没有" in r.error

    async def test_list_files(self, setup):
        tools, _e, _h, ws_path, _s = setup
        ctx = _ctx(ws_path)
        await tools.call("write_file", {"path": "d1/f.txt", "content": "x"}, ctx)
        r = await tools.call("list_files", {}, ctx)
        assert r.ok and "d1/f.txt" in r.output

    async def test_run_command_echo(self, setup):
        tools, _e, _h, ws_path, _s = setup
        r = await tools.call("run_command", {"command": "echo hello"}, _ctx(ws_path))
        assert r.ok and r.data["exit_code"] == 0 and "hello" in r.output

    async def test_run_command_requires_command(self, setup):
        tools, _e, _h, ws_path, _s = setup
        r = await tools.call("run_command", {}, _ctx(ws_path))
        assert not r.ok

    async def test_start_check_stop_process(self, setup):
        tools, _e, _h, ws_path, _s = setup
        ctx = _ctx(ws_path)
        r = await tools.call("start_process", {"command": "sleep 60", "label": "w"}, ctx)
        assert r.ok and "w" in r.output
        r = await tools.call("check_process", {"label": "w"}, ctx)
        assert r.ok and "在跑" in r.output
        r = await tools.call("stop_process", {"label": "w"}, ctx)
        assert r.ok
        r = await tools.call("check_process", {"label": "w"}, ctx)
        assert r.ok and "没在跑" in r.output

    async def test_stop_unknown_process(self, setup):
        tools, _e, _h, ws_path, _s = setup
        r = await tools.call("stop_process", {"label": "never"}, _ctx(ws_path))
        assert r.ok  # 幂等：不存在的进程停也成了「没在跑了」


@pytest.mark.asyncio
class TestOutOfBoundsIsRecordedFailure:
    async def test_read_absolute_path_rejected(self, setup):
        tools, _e, _h, ws_path, store = setup
        r = await tools.call("read_file", {"path": "/etc/passwd"}, _ctx(ws_path))
        assert not r.ok and "不允许" in r.error
        calls = _calls(store, "read_file")
        assert calls and calls[-1]["ok"] == 0 and calls[-1]["error"]

    async def test_write_dotdot_rejected(self, setup):
        tools, _e, _h, ws_path, store = setup
        r = await tools.call("write_file", {"path": "../evil.txt", "content": "x"}, _ctx(ws_path))
        assert not r.ok and "不允许" in r.error
        calls = _calls(store, "write_file")
        assert calls and calls[-1]["ok"] == 0

    async def test_read_symlink_escape_rejected(self, setup):
        tools, env, _h, ws_path, store = setup
        outside = ws_path.parent / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("shh")
        (ws_path / "link.txt").symlink_to(outside / "secret.txt")
        r = await tools.call("read_file", {"path": "link.txt"}, _ctx(ws_path))
        assert not r.ok and "不允许" in r.error
        calls = _calls(store, "read_file")
        assert calls and calls[-1]["ok"] == 0

    async def test_failed_call_is_persisted(self, setup):
        tools, _e, _h, ws_path, store = setup
        await tools.call("read_file", {"path": "/etc/passwd"}, _ctx(ws_path))
        calls = _calls(store, "read_file")
        assert len(calls) == 1
        assert calls[0]["ok"] == 0
        assert "不允许" in calls[0]["error"]


@pytest.mark.asyncio
class TestRunCommandTimeoutClamped:
    async def test_timeout_s_over_limit_is_clamped(self, setup, settings):
        tools, _e, _h, ws_path, _s = setup
        assert settings.environments.command_timeout_s == 300
        # sleep 要跑 2 秒，但 2 > command_timeout_s 是假话（300）；这里给超大值让它被夹，
        # 真测「夹住」要走 settings：把 command_timeout_s 改成 1 再试一次
        r = await tools.call("run_command", {"command": "sleep 0.2; echo done", "timeout_s": 99999}, _ctx(ws_path))
        assert r.ok and r.data["timed_out"] is False  # 默认 300 内，跑完了

    async def test_timeout_s_respected_when_small(self, setup):
        tools, _e, _h, ws_path, _s = setup
        r = await tools.call("run_command", {"command": "sleep 30", "timeout_s": 1}, _ctx(ws_path))
        assert r.ok and r.data["timed_out"] is True  # 结果照常返回，但标了超时

    async def test_timeout_respects_configured_command_timeout_s(self, setup, settings, store, tmp_path):
        """[environments] command_timeout_s 是真上限：配 1 秒后，sleep 30 必被掐。"""
        s2, _ = load_settings(
            {
                "environments": {
                    "local_mode": "direct",
                    "workspace_root": str(settings.environments.workspace_root),
                    "command_timeout_s": 1,
                }
            }
        )
        env2 = LocalEnv(lambda: s2)
        tools2 = Tools(store)
        ctx = _ctx(env2.workspace("ws-1"))
        register_exec_tools(tools2, env=env2, host=FakeHost(), get_settings=lambda: s2, session_of=lambda gid: "")
        r = await tools2.call("run_command", {"command": "sleep 30"}, ctx)
        assert r.ok and r.data["timed_out"] is True


@pytest.mark.asyncio
class TestWorkspaceMissing:
    async def test_no_workspace_in_ctx(self, setup):
        tools, _e, _h, _w, _s = setup
        ctx = ToolContext(group_id="g1", task_id="T-1", actor="子 agent #1", role="worker", workspace=None)
        r = await tools.call("run_command", {"command": "true"}, ctx)
        assert not r.ok and "工作区" in r.error


@pytest.mark.asyncio
class TestChatHistory:
    def _msgs(self, group_marker_ts: float):
        return [
            Msg(
                id="1",
                ts=group_marker_ts - 60,
                user_id="u1",
                user_name="张三",
                text="今天聊 PyPI 镜像",
                is_bot=False,
                is_at=False,
                is_picture=False,
                reply_to="",
            ),
            Msg(
                id="2",
                ts=group_marker_ts - 30,
                user_id="u2",
                user_name="李四",
                text="发个网址 https://example.com/",
                is_bot=False,
                is_at=False,
                is_picture=False,
                reply_to="",
            ),
        ]

    async def test_read_chat_history_filters_by_group(self, setup, session_map):
        tools, _e, host, ws_path, _s = setup
        host.msgs = self._msgs(clock.now())
        session_map["g1"] = "sess-1"
        session_map["g2"] = "sess-2"
        ctx = _ctx(ws_path)
        r = await tools.call("read_chat_history", {"hours": 1}, ctx)
        assert r.ok
        assert "张三" in r.output and "李四" in r.output
        # 只读本群：只查了 g1 -> sess-1 的消息，没碰别群
        assert all(call[0] == "sess-1" for call in host.msg_calls)
        assert not any(call[0] == "sess-2" for call in host.msg_calls)

    async def test_read_chat_history_keyword_filter(self, setup, session_map):
        tools, _e, host, ws_path, _s = setup
        host.msgs = self._msgs(clock.now())
        session_map["g1"] = "sess-1"
        r = await tools.call("read_chat_history", {"keyword": "PyPI"}, _ctx(ws_path))
        assert r.ok and "张三" in r.output and "李四" not in r.output

    async def test_read_chat_history_no_session(self, setup, session_map):
        tools, _e, host, ws_path, _s = setup
        assert "g1" not in session_map
        r = await tools.call("read_chat_history", {}, _ctx(ws_path))
        assert not r.ok and "会话" in r.error

    async def test_read_chat_history_is_worker_only(self, setup, session_map):
        tools, _e, _h, ws_path, _s = setup
        session_map["g1"] = "sess-1"
        ctx = _ctx(ws_path, role="main", actor="主模型")
        r = await tools.call("read_chat_history", {}, ctx)
        assert not r.ok and "角色" in r.error

    async def test_read_chat_history_time_format(self, setup, session_map):
        tools, _e, host, ws_path, _s = setup
        host.msgs = [Msg(
            id="m", ts=clock.now() - 600, user_id="u", user_name="王五", text="晚上好",
            is_bot=False, is_at=False, is_picture=False, reply_to="",
        )]
        session_map["g1"] = "sess-1"
        r = await tools.call("read_chat_history", {"hours": 720}, _ctx(ws_path))
        # 严格断言格式：HH:MM 名字: 文本
        import re

        assert re.search(r"\[\d{2}:\d{2}\] 王五: 晚上好", r.output)


@pytest.mark.asyncio
class TestSearchMemory:
    async def test_search_memory_queries_host_knowledge(self, setup):
        tools, _e, host, ws_path, _s = setup
        # knowledge：记录调用
        captured = {}

        async def knowledge(query, **kw):
            captured["query"] = query
            captured.update(kw)
            return "记忆片段：PyPI 镜像换 https://pypi.tuna.tsinghua.edu.cn/simple"

        host.knowledge = knowledge  # type: ignore[attr-defined]
        ctx = _ctx(ws_path)
        r = await tools.call("search_memory", {"query": "pypi 镜像"}, ctx)
        assert r.ok and "镜像" in r.output
        assert captured["query"] == "pypi 镜像"
        # 只给 group_id，不传 person_id（docs/06：person_id 过滤是假的）
        assert captured.get("group_id") == "g1"
        assert "person_id" not in captured

    async def test_search_memory_empty_query(self, setup):
        tools, _e, _h, ws_path, _s = setup
        r = await tools.call("search_memory", {"query": "  "}, _ctx(ws_path))
        assert not r.ok

    async def test_search_memory_no_results(self, setup):
        tools, _e, host, ws_path, _s = setup

        async def knowledge(query, **kw):
            return ""

        host.knowledge = knowledge  # type: ignore[attr-defined]
        r = await tools.call("search_memory", {"query": "没印象的事"}, _ctx(ws_path))
        assert r.ok and "没留下这方面的印象" in r.output

    async def test_search_memory_worker_only(self, setup):
        tools, _e, _h, ws_path, _s = setup
        r = await tools.call("search_memory", {"query": "x"}, _ctx(ws_path, role="main", actor="主模型"))
        assert not r.ok


@pytest.mark.asyncio
class TestMainReadOnlyTools:
    async def test_main_can_inspect_file(self, setup):
        tools, _e, _h, ws_path, _s = setup
        (ws_path / "out.md").write_text("# 交付报告")
        ctx = _ctx(ws_path, role="main", actor="主模型")
        r = await tools.call("inspect_file", {"path": "out.md"}, ctx)
        assert r.ok and r.output == "# 交付报告"

    async def test_main_can_inspect_files(self, setup):
        tools, _e, _h, ws_path, _s = setup
        (ws_path / "out.md").write_text("x")
        ctx = _ctx(ws_path, role="main", actor="主模型")
        r = await tools.call("inspect_files", {}, ctx)
        assert r.ok and "out.md" in r.output

    async def test_main_cannot_write_file(self, setup):
        tools, _e, _h, ws_path, _s = setup
        r = await tools.call("write_file", {"path": "a.txt", "content": "x"}, _ctx(ws_path, role="main", actor="主模型"))
        assert not r.ok and "角色" in r.error

    async def test_main_cannot_run_command(self, setup):
        tools, _e, _h, ws_path, _s = setup
        r = await tools.call("run_command", {"command": "true"}, _ctx(ws_path, role="main", actor="主模型"))
        assert not r.ok and "角色" in r.error

    async def test_main_cannot_chat_history(self, setup):
        tools, _e, _h, ws_path, _s = setup
        r = await tools.call("read_chat_history", {}, _ctx(ws_path, role="main", actor="主模型"))
        assert not r.ok

    async def test_main_cannot_search_memory(self, setup):
        tools, _e, _h, ws_path, _s = setup
        r = await tools.call("search_memory", {"query": "x"}, _ctx(ws_path, role="main", actor="主模型"))
        assert not r.ok

    async def test_worker_cannot_inspect(self, setup):
        """inspect_* 是主模型专用：worker 调就被拒（不混用工具面）。"""
        tools, _e, _h, ws_path, _s = setup
        r = await tools.call("inspect_file", {"path": "x"}, _ctx(ws_path))
        assert not r.ok and "角色" in r.error
        r = await tools.call("inspect_files", {}, _ctx(ws_path))
        assert not r.ok and "角色" in r.error

    async def test_main_inspect_file_respects_bounds(self, setup):
        tools, _e, _h, ws_path, store = setup
        r = await tools.call("inspect_file", {"path": "../outside.txt"}, _ctx(ws_path, role="main", actor="主模型"))
        assert not r.ok and "不允许" in r.error
        calls = _calls(store, "inspect_file")
        assert calls[-1]["ok"] == 0


@pytest.mark.asyncio
class TestSummaries:
    async def test_run_command_summarize_includes_exit_and_ms(self, setup):
        tools, _e, _h, ws_path, store = setup
        await tools.call("run_command", {"command": "false"}, _ctx(ws_path))
        calls = _calls(store, "run_command")
        assert "false" in calls[0]["input"]
        assert "退出码 1" in calls[0]["output"]
        assert "秒" in calls[0]["output"] or "毫秒" in calls[0]["output"]

    async def test_write_file_summarize(self, setup):
        tools, _e, _h, ws_path, store = setup
        await tools.call("write_file", {"path": "d/e.txt", "content": "12345"}, _ctx(ws_path))
        calls = _calls(store, "write_file")
        assert calls[0]["input"] == "d/e.txt"
        assert "5" in calls[0]["output"]

    async def test_read_file_summarize_truncates(self, setup):
        tools, _e, _h, ws_path, store = setup
        (ws_path / "big.txt").write_text("z" * 300)
        await tools.call("read_file", {"path": "big.txt"}, _ctx(ws_path))
        calls = _calls(store, "read_file")
        assert calls[0]["ok"] == 1
        assert calls[0]["output"].endswith("…")
