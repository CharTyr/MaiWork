"""个人向找料的用量（线上巡检 2026-10-02 / 10-03 复查）。

实测：个人向「找资料」子 agent 一天约 322 万 token（约占实际花费四成）。九次运行里
便宜的 9~11 次搜索、8~9 次打开就交回（9~19 万 token）；贵的搜了 32~44 次、打开 10~18 次
（45~78 万 token）——每多一步都把整段对话重发一遍，越往后越贵。而且这些调用没有标记，
网页和账本上分不出是谁的。

修法：
- 每次找料带标记 task_id = personal-collect:<群号>:<人 id 前 8 位>:<毫秒时间戳>，用量能归账；
- 代码硬上限：这次运行最多搜 PERSONAL_SEARCH_CAP 次、打开 PERSONAL_PAGE_CAP 页，
  用完工具直接回拒绝话（让它用已有的交回），跑完关账不残留；
- 别的任务（普通派活、feeds-collect:）照旧不限。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from CharTyr_MaiWork.maiwork import verify_budget
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.personal import PERSONAL_PAGE_CAP, PERSONAL_SEARCH_CAP
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import ToolContext, Tools
from CharTyr_MaiWork.maiwork.tools_builtin import register_builtin

from fakes import FakeModelsQueue, FakeProfiles
from test_feeds import _ok_report
from test_personal import GID, UID, _WORKER_ITEMS, _make_personal, _personal_scores_json, _run, _time_patch


def test_caps_are_like_the_cheap_runs() -> None:
    assert 8 <= PERSONAL_SEARCH_CAP <= 12
    assert 6 <= PERSONAL_PAGE_CAP <= 9


class _BudgetSpyWorkers:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.status_during: Any = None

    async def run(self, brief: str, **kwargs: Any) -> Any:
        self.calls.append({"brief": brief, **kwargs})
        self.status_during = verify_budget._status(str(kwargs.get("task_id") or ""))
        return _ok_report(_WORKER_ITEMS)


def test_personal_collect_is_tagged_and_budgeted(tmp_path) -> None:
    workers = _BudgetSpyWorkers()
    models = FakeModelsQueue(ready=True, replies=[
        json.dumps({"focus": [{"query": "FPGA", "why": "在做"}], "idea": None}, ensure_ascii=False),
        _personal_scores_json(),
        json.dumps({"posts": []}, ensure_ascii=False),
    ])
    _store, _s, personal, *_ = _make_personal(tmp_path, models=models, workers=workers)
    with _time_patch():
        _run(personal.prepare_personal(GID, UID))
    assert workers.calls, "要派找料子 agent"
    tid = str(workers.calls[0].get("task_id") or "")
    assert tid.startswith(f"personal-collect:{GID}:{UID[:8]}:"), tid
    assert workers.status_during is not None, "跑的时候账本要开着"
    assert workers.status_during["cap"] == PERSONAL_PAGE_CAP
    assert workers.status_during["search_cap"] == PERSONAL_SEARCH_CAP
    assert verify_budget._status(tid) is None, "跑完要关账"
    # 提示词里告诉它上限（别一上来就撞墙）
    brief = workers.calls[0]["brief"]
    assert str(PERSONAL_SEARCH_CAP) in brief and str(PERSONAL_PAGE_CAP) in brief


def test_budget_closed_even_if_worker_raises(tmp_path) -> None:
    class _Boom:
        tid = ""

        async def run(self, brief: str, **kwargs: Any) -> Any:
            _Boom.tid = str(kwargs.get("task_id") or "")
            raise ValueError("炸了")

    models = FakeModelsQueue(ready=True, replies=[
        json.dumps({"focus": [{"query": "FPGA", "why": "在做"}], "idea": None}, ensure_ascii=False),
    ])
    _store, _s, personal, *_ = _make_personal(tmp_path, models=models, workers=_Boom())
    with _time_patch():
        assert _run(personal.prepare_personal(GID, UID)) == 0
    assert _Boom.tid and verify_budget._status(_Boom.tid) is None


def test_search_budget_ledger() -> None:
    tid = "personal-collect:g:u:1"
    verify_budget.open_run(tid, cap_page_calls=5, cap_search_calls=2)
    try:
        assert verify_budget.consume_search(tid)[0] is True
        assert verify_budget.consume_search(tid)[0] is True
        ok, note = verify_budget.consume_search(tid)
        assert ok is False and "搜索" in note and "交回" in note
        # 打开页数另算
        assert verify_budget.consume(tid)[0] is True
    finally:
        verify_budget.close_run(tid)
    # 没开账本 / 没设搜索上限的照旧不限
    for _ in range(30):
        assert verify_budget.consume_search("T-9")[0] is True
    verify_budget.open_run("feeds-verify:a:b:0", cap_page_calls=1)
    try:
        assert all(verify_budget.consume_search("feeds-verify:a:b:0")[0] for _ in range(10))
    finally:
        verify_budget.close_run("feeds-verify:a:b:0")


class _FakeSearch:
    def __init__(self) -> None:
        self.n = 0

    async def search(self, query: str, **kw: Any) -> list[dict]:
        self.n += 1
        return [{"title": "t", "url": "https://example.com/a", "snippet": "s", "published": None}]

    def ready(self) -> bool:
        return True


def _tools(store: Store, search: Any) -> Tools:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text="<html><head><title>t</title></head><body>p</body></html>")

    settings, _ = load_settings({})
    tools = Tools(store)
    register_builtin(
        tools, search=search, profiles=FakeProfiles(),
        http_transport=httpx.MockTransport(handler),
        get_settings=lambda: settings, resolver=lambda h: ["93.184.216.34"],
    )
    return tools


@pytest.mark.asyncio
async def test_tools_enforce_personal_budget(tmp_path) -> None:
    store = Store(tmp_path / "t.db")
    store.migrate()
    search = _FakeSearch()
    tools = _tools(store, search)
    tid = "personal-collect:111:10001:1"
    verify_budget.open_run(tid, cap_page_calls=1, cap_search_calls=1)
    ctx = ToolContext(group_id=GID, task_id=tid, actor="子 agent #1", role="worker")
    try:
        assert (await tools.call("web_search", {"query": "a"}, ctx)).ok
        r = await tools.call("web_search", {"query": "b"}, ctx)
        assert r.ok is False and "交回" in (r.error or "")
        assert search.n == 1, "被拦的那次不该真去搜"
        assert (await tools.call("fetch_page", {"url": "http://example.com/1"}, ctx)).ok
        r2 = await tools.call("fetch_page", {"url": "http://example.com/2"}, ctx)
        assert r2.ok is False
    finally:
        verify_budget.close_run(tid)
    # 普通任务不受影响
    ctx2 = ToolContext(group_id=GID, task_id="T-9", actor="子 agent #1", role="worker")
    for i in range(4):
        assert (await tools.call("web_search", {"query": f"q{i}"}, ctx2)).ok
    store.close()
