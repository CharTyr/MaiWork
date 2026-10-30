"""资讯补找（design §4）+ discovery.note_output。

- 没做法工具：不跑补找、零行为变化（模型调用数/候选/漏斗一致）；
- 有：保底之后跑一次；交回链接必须在「见过」账里，不在的丢弃；
- 出现的并入候选（provider=agent:<工具名>），按链接去重；
- 补找抛错：整批照常，funnel["supplement"]["error"] 记类型名。
"""

from __future__ import annotations

import pytest

from CharTyr_MaiWork.maiwork import discovery
from CharTyr_MaiWork.maiwork.store import Store

from mafakes_specialists import FakeAgents, FakeSpecialists, FakeWorkersQueue
from test_pipeline_specialists import _mk_feeds, _Settings, G1
from CharTyr_MaiWork.maiwork.workers import WorkerReport


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _search_ok():
    class _S:
        async def search(self, query, *, limit=8, days=None, site="", news=False):
            return [{"title": "t", "url": "https://a.com/1", "snippet": "s",
                     "published": None, "provider": "t"}]

        def broad_providers(self):
            return ["t"]

        async def social_search(self, *a, **kw):
            return [], {}

    return _S()


def _models_pick():
    class _M:
        async def chat(self, role=None, messages=None, **kwargs):
            return type("R", (), {"text": '{"picks":[{"i":0,"kind":"news","hook":"h"}]}',
                                  "tool_calls": []})()
    return _M()


class _SuppSpecialists(FakeSpecialists):
    """能补找的 fake：phase_tools 里 supplement 给非空名单；run_auto 录参。"""
    def __init__(self, agents, results=None, supp_tools=("mcp_x",)):
        super().__init__(agents, results=results)
        self._supp_tools = list(supp_tools)

    def phase_tools(self, kind, phase):
        if phase == "supplement":
            return list(self._supp_tools)
        return []

    async def run_auto(self, kind, brief, *, phase, group_id, task_id="",
                       deadline_ts=None, max_steps=0, actor="", output_schema=None):
        if phase == "supplement":
            # 模拟工具输出里出现过 good 链接（记账），然后交回 1 真 1 假
            discovery.note_output(task_id, "mcp_x",
                                  "结果 https://seen.com/good?utm_source=x 和 https://seen.com/dup")
            report = WorkerReport(ok=True, summary="补找完", data={"items": [
                {"title": "真见过", "url": "https://seen.com/good",
                 "snippet": "s", "published": "", "focus": 1},
                {"title": "编的", "url": "https://fake.com/nope",
                 "snippet": "s", "published": "", "focus": 1},
                {"title": "重了", "url": "https://a.com/1",
                 "snippet": "s", "published": "", "focus": 1},
            ]})
            report.handoff_id = ""
            self.runs.append({"kind": kind, "phase": phase, "task_id": task_id,
                              "brief": brief, "max_steps": max_steps,
                              "actor": actor, "deadline_ts": deadline_ts,
                              "tools": None, "eff_tools": []})
            return report
        return await super().run_auto(kind, brief, phase=phase, group_id=group_id,
                                      task_id=task_id, deadline_ts=deadline_ts,
                                      max_steps=max_steps, actor=actor,
                                      output_schema=output_schema)


async def _collect(store, specialists=None):
    feeds = _mk_feeds(store, models=_models_pick(), workers=FakeWorkersQueue(),
                      settings=_Settings(), specialists=specialists)
    feeds._search = _search_ok()
    stats: dict = {}
    items = await feeds._collect_two_phase(G1, [{"query": "q", "why": "因为", "angle": "", "source": ""}],
                                           _Settings(), collect_mark="feeds-collect:sp", stats_out=stats)
    return items, stats.get("funnel") or {}


@pytest.mark.asyncio
async def test_no_skill_no_supplement(store):
    sp = FakeSpecialists(FakeAgents())
    items, funnel = await _collect(store, specialists=sp)
    assert funnel["supplement"]["ran"] is False
    assert all(i.get("provider") != "agent:mcp_x" for i in items)


@pytest.mark.asyncio
async def test_supplement_filters_fabricated(store):
    sp = _SuppSpecialists(FakeAgents())
    items, funnel = await _collect(store, specialists=sp)
    supp = funnel["supplement"]
    assert supp["ran"] is True and supp["returned"] == 3
    # 编的链接丢 1、见过的留 1（进候选按 provider=agent:mcp_x 计数）
    assert supp["not_in_output"] == 1 and supp["kept"] == 1
    assert funnel["providers"].get("agent:mcp_x") == 1
    assert funnel["discovered"] == 2  # 撒网 1 + 补找 1
    # 补找进来的候选按 focus=1 记进 per_focus（保底可能再加，至少 2）
    assert funnel["per_focus"][0]["cands"] >= 2
    run = next(r for r in sp.runs if r["phase"] == "supplement")
    assert run["task_id"].startswith("feeds-supplement:")
    assert run["max_steps"] == 10 and run["actor"] == "资讯补找"
    assert "1. q" in run["brief"] and "补找" in run["brief"]


@pytest.mark.asyncio
async def test_supplement_error_batch_continues(store):
    class _Boom(_SuppSpecialists):
        async def run_auto(self, kind, brief, *, phase, group_id, task_id="",
                           deadline_ts=None, max_steps=0, actor="", output_schema=None):
            if phase == "supplement":
                raise RuntimeError("炸了")
            return await super().run_auto(kind, brief, phase=phase, group_id=group_id,
                                          task_id=task_id, deadline_ts=deadline_ts,
                                          max_steps=max_steps, actor=actor,
                                          output_schema=output_schema)

    sp = _Boom(FakeAgents())
    items, funnel = await _collect(store, specialists=sp)
    assert funnel["supplement"]["error"] == "RuntimeError"
    assert funnel["discovered"] >= 1, "撒网候选还在，整批照常走完"


class TestNoteOutput:
    def test_records_urls_only_when_run_open(self):
        tid = "feeds-supplement:t1"
        discovery.note_output(tid, "mcp_x", "https://a.com/x")
        assert discovery.take_seen(tid) == {}
        discovery.open_run(tid)
        discovery.note_output(tid, "mcp_x", "见 https://a.com/x?utm=1 和 https://b.com/y。")
        discovery.note_output(tid, "web_search", "又见 https://a.com/x")
        seen = discovery.take_seen(tid)
        assert "a.com/x" in next(iter(seen)) or len(seen) == 2
        assert seen.get(list(seen)[0]) == "mcp_x"
        discovery.close_run(tid)
        assert discovery.take_seen(tid) == {}

    def test_error_swallowed(self):
        discovery.open_run("t2")
        discovery.note_output("t2", "t", object())  # 坏输入不炸
        discovery.close_run("t2")
