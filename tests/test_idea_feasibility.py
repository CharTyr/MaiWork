"""idea_feasibility.py（构想可行性把关，docs/18 §八）测试。

先写测试、确认变红，再实现。覆盖：

- inventory：拿不到名单 → 基本能力（search/chat/write/watch）；按**真实**工具名推算
  （web_search/fetch_page、read_chat_history/search_memory/read_profile、write_file、
  run_command、vm_run、machine_* 前缀、mcp_ 扩展 ≤12）；群文件 / 公告 / 相册不进清单。
- CANNOT / DELIVER / prompt_section：清单、做不到、交付形式、字段格式。
- check：level 必须 ok；needs_members 必须明确 false；uses 非空且全在清单里；
  deliver 认得出且与能力对得上；缺字段一律不过。normalized 至少保留 level/note。
- record_blocked / blocked_view：kv `ideas.blocked.<群号>`、近 14 天 / ≤30 条裁剪、
  {"count": 近 days 天条数, "recent": 新的在前最多 5 条}。
- 接线：make_idea 不合格不入库且被记录、合格入库存规范化 JSON；
  personal._insert_idea 同理；views.group_view 仅管理员带 ideas_blocked。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from CharTyr_MaiWork.maiwork import clock, idea_feasibility
from CharTyr_MaiWork.maiwork.console import views
from CharTyr_MaiWork.maiwork.store import Store

from fakes import FakeModelsQueue
from test_feeds import GID, _make_feeds, _run, _TimePatch
from test_personal import (
    GID as PGID,
    UID,
    _make_personal,
    _personal_scores_json,
    _time_patch,
    _run as _prun,
)

NOW = 1_790_000_000.0

# 各能力对应的真实工具名（grep 核实过，见模块 docstring）
FULL_CATALOG: List[Any] = [
    ("web_search", "联网搜索"),
    ("fetch_page", "打开网页"),
    ("read_chat_history", "读本群聊天记录"),
    ("search_memory", "搜群记忆"),
    ("read_profile", "读群画像"),
    ("write_file", "写文件"),
    ("read_file", "读文件"),
    ("list_files", "列目录"),
    ("run_command", "跑命令"),
    ("vm_run", "一次性 VM"),
    ("machine_run", "SSH 机器"),
    ("machine_put_file", "传文件到机器"),
    ("mcp_demo_echo", "扩展工具"),
    ("submit_result", "交回结果"),
]

BASIC = ["chat", "search", "watch", "write"]

GOOD: Dict[str, Any] = {
    "level": "ok",
    "note": "联网搜公开参数，整理成一页对比表",
    "uses": ["search", "write"],
    "deliver": "doc",
    "needs_members": False,
}


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "t.db")
    store.migrate()
    return store


# ----------------------------------------------------------------------
# inventory
# ----------------------------------------------------------------------


class TestInventory:
    def test_no_catalog_gives_basic(self) -> None:
        """拿不到名单 → 只给基本能力（search/chat/write/watch）。"""
        inv = idea_feasibility.inventory(None)
        assert inv["caps"] == BASIC
        assert inv["ext"] == []
        assert inv["tools"] == []

    def test_empty_catalog_gives_basic(self) -> None:
        assert idea_feasibility.inventory([])["caps"] == BASIC
        assert idea_feasibility.inventory(())["caps"] == BASIC

    def test_reads_real_tool_names(self) -> None:
        inv = idea_feasibility.inventory(FULL_CATALOG)
        assert set(inv["caps"]) == {"chat", "search", "write", "watch", "code", "vm", "machine"}
        assert inv["caps"] == sorted(inv["caps"])
        assert inv["ext"] == ["ext:mcp_demo_echo"]
        assert "web_search" in inv["tools"]

    def test_search_needs_web_search_or_fetch_page(self) -> None:
        assert "search" in idea_feasibility.inventory([("web_search", "")])["caps"]
        assert "search" in idea_feasibility.inventory([("fetch_page", "")])["caps"]
        assert "search" not in idea_feasibility.inventory([("write_file", "")])["caps"]

    def test_chat_needs_one_of_the_three(self) -> None:
        for name in ("read_chat_history", "search_memory", "read_profile"):
            assert "chat" in idea_feasibility.inventory([(name, "")])["caps"], name
        assert "chat" not in idea_feasibility.inventory([("web_search", "")])["caps"]

    def test_write_needs_write_file(self) -> None:
        assert "write" in idea_feasibility.inventory([("write_file", "")])["caps"]
        assert "write" not in idea_feasibility.inventory([("read_file", "")])["caps"]

    def test_code_needs_run_command(self) -> None:
        assert "code" in idea_feasibility.inventory([("run_command", "")])["caps"]
        assert "code" not in idea_feasibility.inventory([("write_file", "")])["caps"]

    def test_vm_needs_vm_run(self) -> None:
        assert "vm" in idea_feasibility.inventory([("vm_run", "")])["caps"]
        assert "vm" not in idea_feasibility.inventory([("vm_put_file", "")])["caps"]

    def test_machine_prefix(self) -> None:
        inv = idea_feasibility.inventory([("machine_run", ""), ("machine_read_file", "")])
        assert "machine" in inv["caps"]

    def test_group_space_tools_not_in_catalog(self) -> None:
        """群文件 / 公告 / 相册（看本群权限）不进清单——构想不该靠它成立。"""
        inv = idea_feasibility.inventory(
            [
                ("files_list", "群文件列表"),
                ("files_manage", "管群文件"),
                ("send_notice", "发公告"),
                ("album_upload", "传相册"),
                ("group_file_upload", "传群文件"),
            ]
        )
        assert inv["caps"] == ["watch"]
        assert len(inv["caps"]) == 1

    def test_ext_capped_at_12(self) -> None:
        cat = [(f"mcp_ext{i}_tool", "") for i in range(20)]
        inv = idea_feasibility.inventory(cat)
        assert len(inv["ext"]) == 12
        assert all(x.startswith("ext:mcp_") for x in inv["ext"])

    def test_watch_always(self) -> None:
        assert "watch" in idea_feasibility.inventory([("submit_result", "")])["caps"]

    def test_accepts_dict_entries(self) -> None:
        inv = idea_feasibility.inventory([{"name": "web_search", "description": "搜"}])
        assert "search" in inv["caps"]


# ----------------------------------------------------------------------
# CANNOT / DELIVER / prompt_section
# ----------------------------------------------------------------------


class TestPromptSection:
    def test_section_lists_caps_cannot_deliver_and_fields(self) -> None:
        text = idea_feasibility.prompt_section(idea_feasibility.inventory(None))
        for cap in BASIC:
            assert cap in text, cap
        assert "做不到" in text
        assert idea_feasibility.CANNOT
        for line in idea_feasibility.CANNOT:
            assert line in text
        assert "page" in text and "doc" in text and "tool" in text and "report" in text
        for field in ("uses", "deliver", "needs_members", "level", "note"):
            assert field in text, field

    def test_section_mentions_ext(self) -> None:
        text = idea_feasibility.prompt_section(idea_feasibility.inventory([("mcp_demo_echo", "")]))
        assert "ext:mcp_demo_echo" in text

    def test_section_has_no_step_effort_fields(self) -> None:
        """构想提示词里不能再出现 step / effort 字段（2026-10 已去掉）。"""
        text = idea_feasibility.prompt_section(idea_feasibility.inventory(None))
        assert '"step"' not in text and '"effort"' not in text

    def test_deliver_table_shape(self) -> None:
        assert set(idea_feasibility.DELIVER) == {"page", "doc", "tool", "report"}
        assert idea_feasibility.DELIVER["tool"]["needs"] == ("code", "vm", "machine")
        assert idea_feasibility.DELIVER["report"]["needs"] == ("watch",)


# ----------------------------------------------------------------------
# check
# ----------------------------------------------------------------------


class TestCheck:
    def test_ok(self) -> None:
        ok, reason, norm = idea_feasibility.check(GOOD, idea_feasibility.inventory(None))
        assert ok is True and reason == ""
        assert norm["level"] == "ok"
        assert norm["note"] == GOOD["note"]
        assert norm["uses"] == ["search", "write"]
        assert norm["deliver"] == "doc"
        assert norm["needs_members"] is False

    def test_normalized_always_has_all_keys(self) -> None:
        for raw in (None, "不是表", {"level": "maybe"}, GOOD):
            _, _, norm = idea_feasibility.check(raw, idea_feasibility.inventory(None))
            assert set(norm) == {"level", "note", "uses", "deliver", "needs_members"}

    def test_level_must_be_ok(self) -> None:
        for lv in ("maybe", "need", "impossible", ""):
            raw = dict(GOOD, level=lv)
            ok, reason, _ = idea_feasibility.check(raw, idea_feasibility.inventory(None))
            assert ok is False, lv
            assert "level" in reason and reason

    def test_missing_feasibility(self) -> None:
        for raw in (None, {}, "ok", []):
            ok, reason, _ = idea_feasibility.check(raw, idea_feasibility.inventory(None))
            assert ok is False
            assert reason

    def test_needs_members_must_be_false(self) -> None:
        for bad in (True, "true", None):
            raw = dict(GOOD, needs_members=bad)
            ok, reason, _ = idea_feasibility.check(raw, idea_feasibility.inventory(None))
            assert ok is False, bad
            assert "needs_members" in reason
        # 缺字段也不放过
        raw = {k: v for k, v in GOOD.items() if k != "needs_members"}
        ok, reason, _ = idea_feasibility.check(raw, idea_feasibility.inventory(None))
        assert ok is False and "needs_members" in reason

    def test_uses_must_be_nonempty(self) -> None:
        for bad in ([], None, "search"):
            ok, reason, _ = idea_feasibility.check(
                dict(GOOD, uses=bad), idea_feasibility.inventory(None)
            )
            assert ok is False, bad
            assert "uses" in reason

    def test_uses_must_be_in_inventory(self) -> None:
        ok, reason, _ = idea_feasibility.check(
            dict(GOOD, uses=["search", "vm"]), idea_feasibility.inventory(None)
        )
        assert ok is False
        assert "vm" in reason

    def test_uses_ext_ok_when_present(self) -> None:
        inv = idea_feasibility.inventory([("mcp_demo_echo", "")])
        ok, _, norm = idea_feasibility.check(
            dict(GOOD, uses=["ext:mcp_demo_echo"], deliver="doc"), inv
        )
        assert ok is True
        assert norm["uses"] == ["ext:mcp_demo_echo"]
        # 清单里没有这个 ext → 不过
        ok2, reason2, _ = idea_feasibility.check(
            dict(GOOD, uses=["ext:mcp_other"]), idea_feasibility.inventory(None)
        )
        assert ok2 is False
        assert "ext:mcp_other" in reason2

    def test_deliver_must_be_known(self) -> None:
        for bad in ("video", "", None, "网页"):
            ok, reason, _ = idea_feasibility.check(
                dict(GOOD, deliver=bad), idea_feasibility.inventory(None)
            )
            assert ok is False, bad
            assert "deliver" in reason

    def test_deliver_tool_needs_code_vm_or_machine(self) -> None:
        ok, reason, _ = idea_feasibility.check(
            dict(GOOD, deliver="tool"), idea_feasibility.inventory(None)
        )
        assert ok is False
        assert "tool" in reason
        ok2, _, _ = idea_feasibility.check(
            dict(GOOD, deliver="tool", uses=["code", "write"]),
            idea_feasibility.inventory([("run_command", ""), ("write_file", "")]),
        )
        assert ok2 is True

    def test_deliver_report_needs_watch(self) -> None:
        ok, _, _ = idea_feasibility.check(
            dict(GOOD, deliver="report", uses=["watch"]), idea_feasibility.inventory(None)
        )
        assert ok is True

    def test_missing_deliver(self) -> None:
        raw = {k: v for k, v in GOOD.items() if k != "deliver"}
        ok, reason, _ = idea_feasibility.check(raw, idea_feasibility.inventory(None))
        assert ok is False and "deliver" in reason

    def test_bad_types_do_not_raise(self) -> None:
        for raw in (42, {"level": ["ok"]}, {"uses": {"a": 1}}, {"needs_members": []}):
            ok, reason, _ = idea_feasibility.check(raw, idea_feasibility.inventory(None))
            assert ok is False
            assert isinstance(reason, str) and reason

    def test_note_truncated(self) -> None:
        ok, _, norm = idea_feasibility.check(
            dict(GOOD, note="很长" * 200), idea_feasibility.inventory(None)
        )
        assert ok is True
        assert len(norm["note"]) <= 120


# ----------------------------------------------------------------------
# record_blocked / blocked_view
# ----------------------------------------------------------------------


class TestBlockedLog:
    def test_record_and_view(self, tmp_path, monkeypatch) -> None:
        store = _store(tmp_path)
        monkeypatch.setattr(clock, "now", lambda: NOW)
        idea_feasibility.record_blocked(store, GID, "group", "我可以帮你约人开黑", "needs_members=true")
        idea_feasibility.record_blocked(store, GID, "personal", "我可以帮你组个搭子局", "做不到：约人组队")
        view = idea_feasibility.blocked_view(store, GID)
        assert view["count"] == 2
        assert len(view["recent"]) == 2
        assert view["recent"][0]["title"] == "我可以帮你组个搭子局"  # 新的在前
        assert view["recent"][0]["kind"] == "personal"
        assert "做不到" in view["recent"][0]["reason"]
        assert view["recent"][1]["kind"] == "group"
        assert view["recent"][1]["ts"] == NOW

    def test_view_per_group_isolated(self, tmp_path, monkeypatch) -> None:
        store = _store(tmp_path)
        monkeypatch.setattr(clock, "now", lambda: NOW)
        idea_feasibility.record_blocked(store, GID, "group", "甲", "r")
        idea_feasibility.record_blocked(store, "222", "group", "乙", "r")
        assert idea_feasibility.blocked_view(store, GID)["count"] == 1
        assert idea_feasibility.blocked_view(store, "222")["recent"][0]["title"] == "乙"
        assert idea_feasibility.blocked_view(store, "333") == {"count": 0, "recent": []}

    def test_record_keeps_14_days(self, tmp_path, monkeypatch) -> None:
        store = _store(tmp_path)
        monkeypatch.setattr(clock, "now", lambda: NOW - 20 * 86400)
        idea_feasibility.record_blocked(store, GID, "group", "太老的", "r")
        monkeypatch.setattr(clock, "now", lambda: NOW - 86400)
        idea_feasibility.record_blocked(store, GID, "group", "昨天的", "r")
        raw = store.kv_get(f"ideas.blocked.{GID}", [])
        assert [e["title"] for e in raw] == ["昨天的"]

    def test_record_caps_30(self, tmp_path, monkeypatch) -> None:
        store = _store(tmp_path)
        monkeypatch.setattr(clock, "now", lambda: NOW)
        for i in range(35):
            idea_feasibility.record_blocked(store, GID, "group", f"构想{i}", "r")
        raw = store.kv_get(f"ideas.blocked.{GID}", [])
        assert len(raw) == 30
        assert raw[0]["title"] == "构想5"
        assert raw[-1]["title"] == "构想34"

    def test_view_days_window_and_recent_cap(self, tmp_path, monkeypatch) -> None:
        store = _store(tmp_path)
        monkeypatch.setattr(clock, "now", lambda: NOW - 10 * 86400)
        idea_feasibility.record_blocked(store, GID, "group", "十天前的", "r")
        monkeypatch.setattr(clock, "now", lambda: NOW - 3600)
        for i in range(8):
            idea_feasibility.record_blocked(store, GID, "group", f"今天的{i}", "r")
        v7 = idea_feasibility.blocked_view(store, GID, days=7)
        assert v7["count"] == 8
        assert len(v7["recent"]) == 5
        assert v7["recent"][0]["title"] == "今天的7"
        assert all("十天前" not in e["title"] for e in v7["recent"])
        v14 = idea_feasibility.blocked_view(store, GID, days=14)
        assert v14["count"] == 9

    def test_corrupt_kv_falls_back(self, tmp_path) -> None:
        store = _store(tmp_path)
        with store.tx() as conn:
            store.kv_set(conn, f"ideas.blocked.{GID}", "不是列表")
        assert idea_feasibility.blocked_view(store, GID) == {"count": 0, "recent": []}

    def test_record_blocked_never_raises_on_bad_store(self) -> None:
        class _Bad:
            def kv_get(self, key, default=None):
                raise RuntimeError("boom")

            def tx(self):
                raise RuntimeError("boom")

        idea_feasibility.record_blocked(_Bad(), GID, "group", "t", "r")
        assert idea_feasibility.blocked_view(_Bad(), GID) == {"count": 0, "recent": []}


# ----------------------------------------------------------------------
# Tools.catalog / Workers.tool_catalog
# ----------------------------------------------------------------------


async def _noop(ctx, args):  # pragma: no cover - 只是给 Tool 凑一个 handler
    return None


def _register_tool(tools, name: str, desc: str, roles) -> None:
    from CharTyr_MaiWork.maiwork.tools import Tool

    tools.register(Tool(name=name, description=desc, parameters={},
                        roles=frozenset(roles), handler=_noop))


class TestToolCatalog:
    def test_tools_catalog_only_that_role(self, tmp_path) -> None:
        from CharTyr_MaiWork.maiwork.tools import Tools

        tools = Tools(_store(tmp_path))
        _register_tool(tools, "web_search", "联网搜", {"main", "worker"})
        _register_tool(tools, "main_only", "只主模型", {"main"})
        cat = tools.catalog("worker")
        names = [n for n, _ in cat]
        assert "web_search" in names
        assert "main_only" not in names
        assert dict(cat)["web_search"] == "联网搜"

    def test_catalog_reflects_unregister(self, tmp_path) -> None:
        from CharTyr_MaiWork.maiwork.tools import Tools

        tools = Tools(_store(tmp_path))
        _register_tool(tools, "run_command", "跑命令", {"worker"})
        assert "run_command" in [n for n, _ in tools.catalog("worker")]
        tools.unregister("run_command")
        inv = idea_feasibility.inventory(tools.catalog("worker"))
        assert "code" not in inv["caps"]

    def test_workers_delegates(self, tmp_path) -> None:
        from CharTyr_MaiWork.maiwork.tools import Tools
        from CharTyr_MaiWork.maiwork.workers import Workers

        tools = Tools(_store(tmp_path))
        _register_tool(tools, "write_file", "写文件", {"worker"})
        workers = Workers(None, tools)
        inv = idea_feasibility.inventory(workers.tool_catalog())
        assert "write" in inv["caps"]

    def test_workers_without_catalog_returns_none(self) -> None:
        from CharTyr_MaiWork.maiwork.workers import Workers

        class _T:
            pass

        assert Workers(None, _T()).tool_catalog() is None

    def test_fake_workers_without_catalog_is_none(self, tmp_path) -> None:
        """测试替身 FakeWorkers 没有 tool_catalog → 基本能力（调用方 getattr 防御）。"""
        store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path)
        assert getattr(workers, "tool_catalog", None) is None
        assert idea_feasibility.inventory(None)["caps"] == BASIC


# ----------------------------------------------------------------------
# 接线：群向 make_idea
# ----------------------------------------------------------------------


def _idea_json(feasibility: Any = None, **over: Any) -> str:
    idea: Dict[str, Any] = {
        "title": "我可以帮群把每周讨论整理成一页",
        "body": "每周自动汇总一次",
        "basis": "群里每周都在复盘",
        "icon": "books",
        "chat_worthy": True,
        "feasibility": GOOD if feasibility is None else feasibility,
    }
    idea.update(over)
    return json.dumps({"idea": idea}, ensure_ascii=False)


class _CatalogWorkers:
    """假 workers：有 tool_catalog（转调真实 Tools），run 不走。"""

    def __init__(self, tools) -> None:
        self._tools = tools

    def tool_catalog(self):
        return self._tools.catalog("worker")

    async def run(self, brief, **kwargs):
        raise RuntimeError("这个用例不走子 agent")


class TestMakeIdeaWiring:
    def test_pass_stores_normalized_json(self, tmp_path) -> None:
        models = FakeModelsQueue(ready=True, replies=[_idea_json()])
        store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path, models=models)
        with _TimePatch():
            got = _run(feeds.make_idea(GID))
        assert isinstance(got, int) and got > 0
        row = store.read().execute("SELECT feasibility FROM ideas WHERE id=?", (got,)).fetchone()
        feas = json.loads(row["feasibility"])
        assert feas["level"] == "ok"
        assert feas["note"] == GOOD["note"]
        assert feas["uses"] == ["search", "write"]
        assert feas["deliver"] == "doc"
        assert feas["needs_members"] is False
        assert store.kv_get(f"ideas.blocked.{GID}", []) == []

    def test_blocked_returns_none_and_records(self, tmp_path) -> None:
        bad = {"level": "ok", "note": "约人开黑", "uses": ["chat"],
               "deliver": "doc", "needs_members": True}
        models = FakeModelsQueue(ready=True, replies=[_idea_json(bad, title="我可以帮你约人开黑")])
        store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path, models=models)
        with _TimePatch():
            got = _run(feeds.make_idea(GID))
        assert got is None
        assert store.read().execute("SELECT COUNT(*) c FROM ideas").fetchone()["c"] == 0
        blocked = store.kv_get(f"ideas.blocked.{GID}", [])
        assert len(blocked) == 1
        assert blocked[0]["kind"] == "group"
        assert blocked[0]["title"] == "我可以帮你约人开黑"
        assert "needs_members" in blocked[0]["reason"]
        assert topics.calls == []

    def test_missing_feasibility_blocked(self, tmp_path) -> None:
        """模型没给 feasibility → 不是「回落 maybe 入库」，而是拦下。"""
        payload = {"idea": {"title": "我可以整理一个 NAS 清单", "body": "整理清单",
                            "basis": "群里在折腾 NAS", "icon": "chart", "chat_worthy": False}}
        models = FakeModelsQueue(ready=True, replies=[json.dumps(payload, ensure_ascii=False)])
        store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        with _TimePatch():
            assert _run(feeds.make_idea(GID)) is None
        assert store.read().execute("SELECT COUNT(*) c FROM ideas").fetchone()["c"] == 0
        assert len(store.kv_get(f"ideas.blocked.{GID}", [])) == 1

    def test_unknown_capability_blocked(self, tmp_path) -> None:
        """uses 里写了清单没有的能力（FakeWorkers → 基本能力，没有 code）→ 拦下。"""
        bad = {"level": "ok", "note": "起个 VM 跑", "uses": ["code", "write"],
               "deliver": "tool", "needs_members": False}
        models = FakeModelsQueue(ready=True, replies=[_idea_json(bad)])
        store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        with _TimePatch():
            assert _run(feeds.make_idea(GID)) is None
        assert len(store.kv_get(f"ideas.blocked.{GID}", [])) == 1

    def test_prompt_carries_capability_section(self, tmp_path) -> None:
        models = FakeModelsQueue(ready=True, replies=[_idea_json()])
        store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        with _TimePatch():
            _run(feeds.make_idea(GID))
        prompt = models.calls[0][1][-1]["content"]
        assert "needs_members" in prompt
        assert "deliver" in prompt
        assert "uses" in prompt
        assert "做不到" in prompt
        for cap in ("search", "chat", "write", "watch"):
            assert cap in prompt, cap

    def test_catalog_used_when_workers_has_it(self, tmp_path) -> None:
        """Workers 有 tool_catalog（真实工具名）时，清单按它算：
        没有 run_command → code 不在清单里 → deliver=tool 被拦。"""
        from CharTyr_MaiWork.maiwork.tools import Tools

        tools = Tools(Store(tmp_path / "tools.db"))
        _register_tool(tools, "write_file", "写文件", {"worker"})
        bad = {"level": "ok", "note": "写个脚本", "uses": ["code"],
               "deliver": "tool", "needs_members": False}
        models = FakeModelsQueue(ready=True, replies=[_idea_json(bad)])
        store, settings, feeds, models, *_r = _make_feeds(
            tmp_path, models=models, workers=_CatalogWorkers(tools), search=None
        )
        with _TimePatch():
            assert _run(feeds.make_idea(GID)) is None
        blocked = store.kv_get(f"ideas.blocked.{GID}", [])
        assert blocked and "code" in blocked[0]["reason"]


# ----------------------------------------------------------------------
# 接线：个人向
# ----------------------------------------------------------------------


def _personal_idea_json(feasibility: Any, title: str = "我可以帮你把这块板的例程理一下") -> str:
    return json.dumps(
        {
            "focus": [{"query": "FPGA", "why": "在做"}],
            "idea": {"title": title, "body": "我可以帮你把例程整理成一页",
                     "step": "先列出要跑的例程", "effort": "半天",
                     "feasibility": feasibility},
        },
        ensure_ascii=False,
    )


class TestPersonalWiring:
    def _models(self, feasibility: Any, title: str = "我可以帮你把这块板的例程理一下"):
        return FakeModelsQueue(ready=True, replies=[
            _personal_idea_json(feasibility, title),
            _personal_scores_json(),
            json.dumps({"posts": []}, ensure_ascii=False),
        ])

    def test_ok_stores_normalized_json(self, tmp_path) -> None:
        models = self._models(GOOD)
        store, settings, personal, models, workers, topics, _ = _make_personal(
            tmp_path, models=models
        )
        with _time_patch():
            _prun(personal.prepare_personal(PGID, UID))
        rows = store.read().execute(
            "SELECT * FROM ideas WHERE target_user_id=?", (UID,)
        ).fetchall()
        assert len(rows) == 1
        feas = json.loads(rows[0]["feasibility"])
        assert feas["level"] == "ok"
        assert feas["uses"] == ["search", "write"]
        assert feas["deliver"] == "doc"
        assert feas["needs_members"] is False

    def test_blocked_not_stored_and_recorded(self, tmp_path) -> None:
        bad = {"level": "ok", "note": "约人开黑", "uses": ["chat"],
               "deliver": "doc", "needs_members": True}
        models = self._models(bad, title="我可以帮你约人开黑")
        store, settings, personal, models, workers, topics, _ = _make_personal(
            tmp_path, models=models
        )
        with _time_patch():
            _prun(personal.prepare_personal(PGID, UID))
        assert store.read().execute("SELECT COUNT(*) c FROM ideas").fetchone()["c"] == 0
        blocked = store.kv_get(f"ideas.blocked.{PGID}", [])
        assert len(blocked) == 1
        assert blocked[0]["kind"] == "personal"
        assert blocked[0]["title"] == "我可以帮你约人开黑"
        assert "needs_members" in blocked[0]["reason"]

    def test_blocked_title_is_title_only(self, tmp_path) -> None:
        """个人向记录的 title 只存标题，不存画像内容。"""
        bad = {"level": "maybe", "note": "可能", "uses": ["search"],
               "deliver": "doc", "needs_members": False}
        models = self._models(bad)
        store, settings, personal, models, workers, topics, _ = _make_personal(
            tmp_path, models=models
        )
        with _time_patch():
            _prun(personal.prepare_personal(PGID, UID))
        blocked = store.kv_get(f"ideas.blocked.{PGID}", [])
        assert blocked
        title = blocked[0]["title"]
        assert "FPGA 的小项目" not in title
        assert "在折腾" not in title
        assert title == "我可以帮你把这块板的例程理一下"

    def test_missing_feasibility_blocked(self, tmp_path) -> None:
        payload = json.dumps({
            "focus": [{"query": "FPGA", "why": "在做"}],
            "idea": {"title": "我可以帮你把例程理一下", "body": "整理", "step": "", "effort": ""},
        }, ensure_ascii=False)
        models = FakeModelsQueue(ready=True, replies=[
            payload, _personal_scores_json(), json.dumps({"posts": []}, ensure_ascii=False),
        ])
        store, settings, personal, models, workers, topics, _ = _make_personal(
            tmp_path, models=models
        )
        with _time_patch():
            _prun(personal.prepare_personal(PGID, UID))
        assert store.read().execute("SELECT COUNT(*) c FROM ideas").fetchone()["c"] == 0
        assert len(store.kv_get(f"ideas.blocked.{PGID}", [])) == 1

    def test_prompt_carries_capability_section(self, tmp_path) -> None:
        models = self._models(GOOD)
        store, settings, personal, models, workers, topics, _ = _make_personal(
            tmp_path, models=models
        )
        with _time_patch():
            _prun(personal.prepare_personal(PGID, UID))
        prompt = models.calls[0][1][-1]["content"]
        assert "needs_members" in prompt
        assert "deliver" in prompt
        assert "做不到" in prompt
        assert '"origin"' in prompt  # 老字段还在


# ----------------------------------------------------------------------
# 接线：views.group_view 仅管理员
# ----------------------------------------------------------------------


class _Svc:
    """group_view 需要的最小服务对象。"""

    def __init__(self, tmp_path) -> None:
        from CharTyr_MaiWork.maiwork.config import load_settings

        self.store = Store(tmp_path / "v.db")
        self.store.migrate()
        self._settings, _ = load_settings({"groups": {"serve": [{"group": f"qq:{GID}"}]}})
        self.signals = None
        self.profiles = None
        self.tasks = None
        self.approvals = None
        self.goals = None
        self.delivery = None
        self.feeds = None
        self.topics = None
        self.scheduler = None
        self.models = None
        self.host = None

    def get_settings(self):
        return self._settings


class TestGroupViewIdeasBlocked:
    def test_admin_sees_ideas_blocked(self, tmp_path) -> None:
        svc = _Svc(tmp_path)
        idea_feasibility.record_blocked(svc.store, GID, "group", "我可以帮你约人开黑", "要群友参与")
        out = views.group_view(svc, GID, admin=True)
        assert out["ideas_blocked"]["count"] == 1
        assert out["ideas_blocked"]["recent"][0]["title"] == "我可以帮你约人开黑"

    def test_member_has_no_key(self, tmp_path) -> None:
        svc = _Svc(tmp_path)
        idea_feasibility.record_blocked(svc.store, GID, "group", "我可以帮你约人开黑", "要群友参与")
        out = views.group_view(svc, GID, admin=False)
        assert "ideas_blocked" not in out
        assert "约人开黑" not in json.dumps(out, ensure_ascii=False)

    def test_admin_broken_store_falls_back(self, tmp_path) -> None:
        svc = _Svc(tmp_path)

        class _Bad:
            def kv_get(self, key, default=None):
                raise RuntimeError("boom")

        svc.store = _Bad()
        out = views.group_view(svc, GID, admin=True)
        assert out["ideas_blocked"] == {"count": 0, "recent": []}

    def test_admin_empty_is_zero(self, tmp_path) -> None:
        svc = _Svc(tmp_path)
        out = views.group_view(svc, GID, admin=True)
        assert out["ideas_blocked"] == {"count": 0, "recent": []}
