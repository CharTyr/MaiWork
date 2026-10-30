"""0.12.1 排查修的几处 bug，逐条守住。

- 轮标记（run_ref）不跟着后台长活走：管理员对话一轮里起的「重新整理画像」等长活，
  不该带着那一轮的 ref（调用挂错条目、也不给自己开主模型条目）；
- 看板「主模型」栏：不聚焦群的管理员对话（group_id 空）也上板；过期 running
  受时间窗限制；列表每件的最后一步只取最新一行（且取对）；
- 资讯卡片的时段 / 日期按北京时间（服务器 TZ=UTC 也一样）；
- 思考链：Responses 的明文 reasoning_text 不当加密；encrypted_content 记 redacted；
  chat completions 的 `reasoning` 字段（OpenRouter / 新版 vLLM）也认。
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from CharTyr_MaiWork.maiwork import clock, main_run, news_card, run_ref
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.console.board import board_view, item_view
from CharTyr_MaiWork.maiwork.console.board.context import Ctx
from CharTyr_MaiWork.maiwork.console.board.trace import Trace, batch_last_step
from CharTyr_MaiWork.maiwork.models import _parse_responses_response, _reasoning_of
from CharTyr_MaiWork.maiwork.store import Store

GID = "900000001"


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "t.db")
    store.migrate()
    return store


class _Svc:
    def __init__(self, store: Store) -> None:
        self.store = store
        from CharTyr_MaiWork.maiwork.agents import Agents
        self.agents = Agents(store, self.get_settings)
        self.agents._ensure_schema()

    def get_settings(self):
        settings, _ = load_settings({"groups": {"serve": [{"group": f"qq:{GID}"}]}})
        return settings


def _main_lane(view: dict) -> dict:
    return next(lane for lane in view["lanes"] if lane["kind"] == "main")


# ----------------------------------------------------------------------
# run_ref：后台长活不继承起它那一轮的标记
# ----------------------------------------------------------------------


class TestDetach:
    @pytest.mark.asyncio
    async def test_spawned_task_inherits_without_detach(self) -> None:
        """先钉住前提：create_task 会复制 context（不 detach 就会带着走）。"""
        seen: list[str] = []

        async def job() -> None:
            seen.append(run_ref.current())

        async with run_ref.tagged("main:admin_chat::1:1"):
            await asyncio.get_running_loop().create_task(job())
        assert seen == ["main:admin_chat::1:1"]

    @pytest.mark.asyncio
    async def test_detached_clears_ref_and_keeps_parent(self) -> None:
        seen: list[str] = []

        async def job() -> str:
            seen.append(run_ref.current())
            return "ok"

        async with run_ref.tagged("main:admin_chat::1:2"):
            out = await asyncio.get_running_loop().create_task(run_ref.detached(job()))
            assert run_ref.current() == "main:admin_chat::1:2"  # 父任务不受影响
        assert out == "ok"
        assert seen == [""]

    @pytest.mark.asyncio
    async def test_detached_job_opens_its_own_main_run(self, tmp_path) -> None:
        """detach 之后，长活里的主模型调用自己开条目，不挂到管理员那一轮上。"""
        store = _store(tmp_path)

        async def profile_refresh() -> str:
            async with main_run.auto(store, agent="main", purpose="profile.refresh", group_id=GID):
                return run_ref.current()

        async with main_run.MainRuns(store).scope(group_id="", purpose="admin_chat", title="回答管理员") as turn:
            task = asyncio.get_running_loop().create_task(run_ref.detached(profile_refresh()))
            admin_ref = turn.ref
        job_ref = await task
        assert job_ref and job_ref != admin_ref
        purposes = [r["purpose"] for r in store.read().execute("SELECT purpose FROM main_runs ORDER BY id")]
        assert purposes == ["admin_chat", "profile.refresh"]
        store.close()

    @pytest.mark.asyncio
    async def test_tools_admin_spawn_bg_detaches(self) -> None:
        from CharTyr_MaiWork.maiwork import tools_admin

        seen: list[str] = []
        done = asyncio.Event()

        async def job() -> None:
            seen.append(run_ref.current())
            done.set()

        async with run_ref.tagged("main:admin_chat::1:3"):
            tools_admin._spawn_bg(job())
        await asyncio.wait_for(done.wait(), 2)
        assert seen == [""]


# ----------------------------------------------------------------------
# 看板「主模型」栏
# ----------------------------------------------------------------------


def _insert_run(store: Store, *, gid: str, status: str, started: float, finished: float = 0.0,
                purpose: str = "admin_chat", ref: str = "") -> int:
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO main_runs (group_id, purpose, title, status, started, finished, ok, error, ref)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (gid, purpose, "回答管理员", status, started, finished, 1 if status == "done" else 0, "",
             ref or f"main:{purpose}:{gid}:{int(started * 1000)}:{status}"),
        )
        return int(cur.lastrowid)


class TestBoardMainLane:
    def test_unfocused_admin_chat_is_on_board(self, tmp_path) -> None:
        store = _store(tmp_path)
        svc = _Svc(store)
        now = clock.now()
        rid = _insert_run(store, gid="", status="done", started=now - 60, finished=now - 30)
        lane = _main_lane(board_view(svc, now=now))
        hist = [x for x in lane["history"] if x["id"] == rid]
        assert hist, "不聚焦群的管理员对话应进主模型栏"
        assert hist[0]["group_name"] == ""  # 不兜成占位的「群」
        assert item_view(svc, "main", str(rid), now=now) is not None
        store.close()

    def test_unserved_group_still_hidden(self, tmp_path) -> None:
        store = _store(tmp_path)
        svc = _Svc(store)
        now = clock.now()
        rid = _insert_run(store, gid="555555555", status="done", started=now - 60, finished=now - 30)
        lane = _main_lane(board_view(svc, now=now))
        assert all(x["id"] != rid for x in lane["history"])
        assert item_view(svc, "main", str(rid), now=now) is None
        store.close()

    def test_stale_running_respects_window(self, tmp_path) -> None:
        store = _store(tmp_path)
        svc = _Svc(store)
        now = clock.now()
        old = _insert_run(store, gid=GID, status="running", started=now - 2 * 86400)
        recent = _insert_run(store, gid=GID, status="running", started=now - 7 * 3600)
        lane = _main_lane(board_view(svc, hours=24, now=now))
        ids = {x["id"] for x in lane["history"]}
        assert recent in ids          # 过期 running（7 小时前）在 24 小时窗内 → 显示中断
        assert old not in ids         # 两天前的不该出现在 24 小时窗里
        assert next(x for x in lane["history"] if x["id"] == recent)["status"] == "cancelled"
        store.close()


class TestBatchLastStep:
    def test_picks_latest_row_per_ref(self, tmp_path) -> None:
        store = _store(tmp_path)
        _Svc(store)
        ref = "news:g:1:1"
        with store.tx() as conn:
            for ts, text in ((100.0, "第一句"), (300.0, "最后一句"), (200.0, "中间")):
                conn.execute(
                    "INSERT INTO model_calls (ts, purpose, role, agent, model, group_id, task_id, attempt,"
                    " ok, status, ms, prompt_tokens, completion_tokens, error, request, response,"
                    " usage_src, cache_read, cache_write, run_ref)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, "p", "main", "news", "m", GID, "", 1, 1, 200, 1, 0, 0, "", "{}",
                     json.dumps({"text": text}, ensure_ascii=False), "reported", 0, 0, ref),
                )
            conn.execute(
                "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ms, ok, error, run_ref)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (250.0, GID, "", "news", "web_search", "{}", "搜到了", 1, 1, "", ref),
            )
        ctx = Ctx(_Svc(store), now=400.0)
        out = batch_last_step(ctx, [Trace(kind="run_ref", value=ref)])
        assert out[f"run_ref:{ref}"] == {"ts": 300.0, "text": "最后一句"}
        store.close()


# ----------------------------------------------------------------------
# 资讯卡片：北京时间
# ----------------------------------------------------------------------


@pytest.fixture
def utc_tz():
    if not hasattr(time, "tzset"):
        pytest.skip("这个平台没有 time.tzset")
    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


class TestNewsCardBeijingTime:
    # 2026-09-29 20:30 北京时间 = 12:30 UTC
    TS = 1790685000.0

    def test_slot_word_uses_beijing_time(self, utc_tz) -> None:
        assert clock.bj(self.TS).hour == 20
        assert news_card.slot_word(self.TS, "") == "晚上"

    def test_date_line_uses_beijing_date(self, utc_tz) -> None:
        # 2026-09-30 01:00 北京时间 = 09-29 17:00 UTC：卡片上应是 9 月 30 日
        ts = 1790701200.0
        assert news_card._date_line(ts, "") == "9月30日 · 周三"


# ----------------------------------------------------------------------
# 思考链解析
# ----------------------------------------------------------------------


class TestReasoningParsing:
    def test_responses_reasoning_text_is_readable(self) -> None:
        data = {
            "status": "completed",
            "output": [
                {"type": "reasoning", "summary": [],
                 "content": [{"type": "reasoning_text", "text": "先想想用户要什么"}]},
                {"type": "message", "content": [{"type": "output_text", "text": "答案"}]},
            ],
        }
        r = _parse_responses_response(data, "gpt-oss")
        assert r.reasoning == "先想想用户要什么"
        assert r.reasoning_redacted is False
        assert r.text == "答案"

    def test_responses_encrypted_content_is_redacted(self) -> None:
        data = {
            "status": "completed",
            "output": [
                {"type": "reasoning", "summary": [], "encrypted_content": "gAAAAB..."},
                {"type": "message", "content": [{"type": "output_text", "text": "答案"}]},
            ],
        }
        r = _parse_responses_response(data, "o-x")
        assert r.reasoning == ""
        assert r.reasoning_redacted is True

    def test_responses_unknown_content_shape_still_redacted(self) -> None:
        data = {
            "status": "completed",
            "output": [{"type": "reasoning", "summary": [], "content": "opaque-blob"}],
        }
        assert _parse_responses_response(data, "m").reasoning_redacted is True

    def test_chat_reasoning_alias(self) -> None:
        assert _reasoning_of({"reasoning_content": "甲"}) == "甲"
        assert _reasoning_of({"reasoning": "乙"}) == "乙"
        assert _reasoning_of({"reasoning_content": "甲", "reasoning": "乙"}) == "甲"
        assert _reasoning_of({"reasoning": {"not": "text"}}) == ""
        assert _reasoning_of(None) == ""
