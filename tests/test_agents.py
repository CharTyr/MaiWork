"""agents.py 单元测试（契约 A 部分，/tmp/maiwork-specialists-contract.md，2026-09-30 批准）。

覆盖契约钉子：
- 四类岗位默认配置（news/idea/goal 只读调研工具；task tools=None / skills=None）。
- get_settings callable 动态：每次调用现取，不缓存 id(store) / settings。
- 带 gid 的方法先验证 is_served；非服务群零读库拒绝（含懒建表都不触发）。
- update_profile 严格：未知键拒绝、类型拒绝、长度上限；tools 不是网页可改字段。
- 记忆：notes ≤2000；remember 只验收后写、按 source_id 幂等、最多 12 条、task 不积累。
- 交接单状态机：queued→running→returned→accepted/rejected，fail→failed/cancelled；
  终态不可复活、不可跨群改、不可终态再 fail/returned/running。
- handoffs：有界 limit、跨群 handoff() → None。
- 事务一致性：review accepted+learn 与状态迁移同一事务（记忆写失败整体回滚）；
  所有写路径都走 Store.tx（BEGIN IMMEDIATE 单写入者）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"
G2 = "123456789"


class _Settings:
    """最小的 Settings 替身：served 集合动态可变（测 get_settings 每次现取）。"""

    def __init__(self, served=()):
        self.served = set(served)

    def is_served(self, gid):
        return str(gid) in self.served


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "test.db")
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def settings():
    return _Settings(served=(G1, G2))


@pytest.fixture
def agents(store, settings):
    return Agents(store, lambda: settings)


# ----------------------------------------------------------------------
# 岗位默认配置
# ----------------------------------------------------------------------


class TestProfiles:
    def test_four_kinds_in_order(self, agents):
        """跑交接单的四个岗位在 KINDS 里；profiles() 还会多带一个「main」（主模型，
        2026-10 改版），它只挂模型选择、不跑交接单。"""
        from CharTyr_MaiWork.maiwork.agents import KINDS

        profiles = agents.profiles()
        assert list(KINDS) == ["news", "idea", "goal", "task"]
        assert [p["kind"] for p in profiles] == ["main", "news", "idea", "goal", "task"]

    def test_default_shape(self, agents):
        for p in agents.profiles():
            assert set(p.keys()) == {"kind", "title", "instructions", "skills", "enabled", "tools", "fish_seed", "model", "effort", "backup"}
            assert isinstance(p["title"], str) and p["title"]
            assert isinstance(p["instructions"], str)
            assert isinstance(p["fish_seed"], str)
            assert isinstance(p["model"], str) and isinstance(p["effort"], str) and isinstance(p["backup"], str)
            assert p["enabled"] is True

    def test_default_titles_are_short_names(self, agents):
        """出厂名字：主模型 / 资讯 / 构想 / 目标（task「通用任务」不动）。"""
        titles = {p["kind"]: p["title"] for p in agents.profiles()}
        assert titles == {"main": "主模型", "news": "资讯", "idea": "构想", "goal": "目标", "task": "通用任务"}

    def test_news_default_tools_readonly(self, agents):
        tools = agents.profile("news")["tools"]
        assert tools is not None
        names = set(tools)
        assert "web_search" in names
        assert "fetch_page" in names
        assert "read_profile" in names
        assert "submit_result" in names
        for banned in ("write_file", "run_command", "mcp_send", "send_message"):
            assert banned not in names

    def test_task_tools_and_skills_none(self, agents):
        p = agents.profile("task")
        assert p["tools"] is None
        assert p["skills"] is None  # None = 当前已启用 worker 技能（不设岗名单）

    def test_specialist_skills_defaults(self, agents):
        news_skills = agents.profile("news")["skills"]
        assert "news-standard" in news_skills
        for s in ("search-exa", "search-tavily", "search-you",
                  "search-firecrawl", "search-keenable", "search-tinyfish"):
            assert s in news_skills
        for kind in ("idea", "goal"):
            sk = agents.profile(kind)["skills"]
            assert "news-standard" not in sk
            assert "search-exa" in sk

    def test_profile_unknown_kind_raises(self, agents):
        with pytest.raises(ValueError):
            agents.profile("no-such-kind")

    def test_profiles_are_copies(self, agents):
        p1 = agents.profile("news")
        p1["title"] = "改"
        p1["skills"].append("x")
        p2 = agents.profile("news")
        assert p2["title"] != "改"
        assert "x" not in p2["skills"]


class TestUpdateProfile:
    def test_update_title_instructions_enabled(self, agents):
        out = agents.update_profile("news", {"title": "资讯小队", "instructions": "先看画像", "enabled": False})
        assert out["title"] == "资讯小队"
        assert out["instructions"] == "先看画像"
        assert out["enabled"] is False
        again = agents.profile("news")
        assert again["title"] == "资讯小队"
        assert again["enabled"] is False

    def test_update_skills(self, agents):
        out = agents.update_profile("idea", {"skills": ["search-exa", "news-standard"]})
        assert out["skills"] == ["search-exa", "news-standard"]

    def test_unknown_field_rejected(self, agents):
        with pytest.raises(ValueError):
            agents.update_profile("news", {"tools": ["write_file"]})
        with pytest.raises(ValueError):
            agents.update_profile("news", {"kind": "x"})
        with pytest.raises(ValueError):
            agents.update_profile("news", {"bogus": 1})

    def test_wrong_types_rejected(self, agents):
        with pytest.raises((ValueError, TypeError)):
            agents.update_profile("news", {"title": 123})
        with pytest.raises((ValueError, TypeError)):
            agents.update_profile("news", {"enabled": "yes"})
        with pytest.raises((ValueError, TypeError)):
            agents.update_profile("news", {"enabled": 1})
        with pytest.raises((ValueError, TypeError)):
            agents.update_profile("news", {"skills": "search-exa"})
        with pytest.raises((ValueError, TypeError)):
            agents.update_profile("news", {"skills": [1, 2]})

    def test_length_limits(self, agents):
        with pytest.raises(ValueError):
            agents.update_profile("news", {"title": "x" * 41})
        with pytest.raises(ValueError):
            agents.update_profile("news", {"instructions": "x" * 3001})
        out = agents.update_profile("news", {"title": "x" * 40, "instructions": "y" * 3000})
        assert out["title"] == "x" * 40

    def test_unknown_kind_rejected(self, agents):
        with pytest.raises(ValueError):
            agents.update_profile("ghost", {"title": "x"})

    def test_disabled_task_profile_still_readable(self, agents):
        agents.update_profile("task", {"enabled": False})
        p = agents.profile("task")
        assert p["enabled"] is False


class TestOldFactoryTitleMigration:
    """默认名改名迁移：kv 里存的旧出厂名（资讯专员/构想专员/目标专员）当默认看待，
    显示新出厂名；其他任何自定义名保留。"""

    OLD = {"news": "资讯专员", "idea": "构想专员", "goal": "目标专员"}

    def test_stored_old_factory_title_shows_new_default(self, agents):
        for kind, old_title in self.OLD.items():
            agents.update_profile(kind, {"title": old_title})
        titles = {p["kind"]: p["title"] for p in agents.profiles()}
        assert titles["news"] == "资讯"
        assert titles["idea"] == "构想"
        assert titles["goal"] == "目标"
        assert titles["task"] == "通用任务"

    def test_custom_title_kept(self, agents):
        agents.update_profile("news", {"title": "资讯小队"})
        agents.update_profile("idea", {"title": "点子王"})
        assert agents.profile("news")["title"] == "资讯小队"
        assert agents.profile("idea")["title"] == "点子王"

    def test_old_title_only_matches_own_kind(self, agents):
        """旧出厂名只对本岗位算默认：news 存「构想专员」是自定义名，要保留。"""
        agents.update_profile("news", {"title": "构想专员"})
        assert agents.profile("news")["title"] == "构想专员"

    def test_other_fields_still_apply_with_old_title(self, agents):
        agents.update_profile("news", {"title": "资讯专员", "enabled": False})
        p = agents.profile("news")
        assert p["title"] == "资讯"
        assert p["enabled"] is False


class TestFishSeed:
    """fish_seed：小鱼头像种子。默认 ""；字符串、去空白、允许空、≤32、仅 [A-Za-z0-9_-]。"""

    def test_default_empty(self, agents):
        for p in agents.profiles():
            assert p["fish_seed"] == ""
        assert agents.profile("news")["fish_seed"] == ""

    def test_set_and_read_back_all_kinds(self, agents):
        for kind in ("news", "idea", "goal", "task"):
            out = agents.update_profile(kind, {"fish_seed": f"fish_{kind}-1"})
            assert out["fish_seed"] == f"fish_{kind}-1"
        for kind in ("news", "idea", "goal", "task"):
            assert agents.profile(kind)["fish_seed"] == f"fish_{kind}-1"

    def test_strip_and_empty_resets(self, agents):
        out = agents.update_profile("news", {"fish_seed": "  koi-01  "})
        assert out["fish_seed"] == "koi-01"
        back = agents.update_profile("news", {"fish_seed": ""})
        assert back["fish_seed"] == ""
        assert agents.profile("news")["fish_seed"] == ""

    def test_max_length_ok(self, agents):
        out = agents.update_profile("news", {"fish_seed": "a" * 32})
        assert out["fish_seed"] == "a" * 32

    def test_invalid_rejected(self, agents):
        with pytest.raises(ValueError):
            agents.update_profile("news", {"fish_seed": "a" * 33})
        with pytest.raises(ValueError):
            agents.update_profile("news", {"fish_seed": "a b"})
        with pytest.raises(ValueError):
            agents.update_profile("news", {"fish_seed": "鱼"})
        with pytest.raises(ValueError):
            agents.update_profile("news", {"fish_seed": 123})

    def test_invalid_rejected_for_task_too(self, agents):
        with pytest.raises(ValueError):
            agents.update_profile("task", {"fish_seed": "a b"})
        out = agents.update_profile("task", {"fish_seed": "task_fish"})
        assert out["fish_seed"] == "task_fish"

    def test_bad_stored_value_ignored(self, store, settings):
        """读路径容错：kv 里坏 fish_seed（非字符串 / 非法字符）按 "" 对待。"""
        raw = {
            "news": {"fish_seed": 123},
            "idea": {"fish_seed": "a b"},
            "goal": {"fish_seed": "x" * 40},
            "task": {"fish_seed": "ok_fish"},
        }
        with store.tx() as conn:
            store.kv_set(conn, "agents.profiles", raw)
        ag = Agents(store, lambda: settings)
        assert ag.profile("news")["fish_seed"] == ""
        assert ag.profile("idea")["fish_seed"] == ""
        assert ag.profile("goal")["fish_seed"] == ""
        assert ag.profile("task")["fish_seed"] == "ok_fish"

    def test_fish_seed_only_patch_keeps_other_fields(self, agents):
        agents.update_profile("news", {"title": "资讯小队", "enabled": False})
        out = agents.update_profile("news", {"fish_seed": "koi"})
        assert out["fish_seed"] == "koi"
        assert out["title"] == "资讯小队"
        assert out["enabled"] is False


# ----------------------------------------------------------------------
# 服务群闸
# ----------------------------------------------------------------------


class TestServedGate:
    @pytest.mark.parametrize("method,args", [
        ("memory", ("999999", "news")),
        ("set_notes", ("999999", "news", "hi")),
        ("remember", ("999999", "news", "text")),
        ("prompt", ("999999", "news")),
        ("begin", ("999999", "news", "brief")),
        ("handoffs", ("999999",)),
        ("handoff", ("999999", "whatever")),
    ])
    def test_unknown_group_rejected(self, agents, method, args):
        with pytest.raises((ValueError, PermissionError, KeyError)):
            getattr(agents, method)(*args)

    def test_non_served_no_db_write(self, store, settings, monkeypatch):
        """非服务群零读写：闸在任何 SQL（含懒建表）之前。

        用「_ensure_schema 没被调过」来证明：任何真实读写都会先触发懒建表。
        """
        ag = Agents(store, lambda: settings)  # 新实例，还没碰过库
        schema_calls: list[str] = []
        orig = Agents._ensure_schema

        def spy(self):
            schema_calls.append("ensure_schema")
            return orig(self)

        monkeypatch.setattr(Agents, "_ensure_schema", spy)
        with pytest.raises(Exception):
            ag.memory("999999", "news")
        with pytest.raises(Exception):
            ag.set_notes("999999", "news", "hi")
        with pytest.raises(Exception):
            ag.remember("999999", "news", "a")
        with pytest.raises(Exception):
            ag.begin("999999", "news", "b")
        assert schema_calls == [], "非服务群不应触发任何 DB 访问"

    def test_dynamic_settings(self, store, settings):
        """get_settings 每次现取：服务群名单热变化立刻生效。"""
        ag = Agents(store, lambda: settings)
        ag.set_notes(G1, "news", "ok")
        settings.served.discard(G1)
        with pytest.raises(Exception):
            ag.memory(G1, "news")
        settings.served.add(G1)
        assert ag.memory(G1, "news")["notes"] == "ok"


# ----------------------------------------------------------------------
# 按群记忆
# ----------------------------------------------------------------------


class TestMemory:
    def test_empty_default(self, agents):
        m = agents.memory(G1, "news")
        assert m == {"notes": "", "learned": []}

    def test_set_notes_roundtrip(self, agents):
        out = agents.set_notes(G1, "news", "这群爱看硬件资讯")
        assert out["notes"] == "这群爱看硬件资讯"
        assert agents.memory(G1, "news")["notes"] == "这群爱看硬件资讯"

    def test_notes_limit(self, agents):
        with pytest.raises(ValueError):
            agents.set_notes(G1, "news", "x" * 2001)
        out = agents.set_notes(G1, "news", "x" * 2000)
        assert len(out["notes"]) == 2000

    def test_notes_group_kind_isolated(self, agents):
        agents.set_notes(G1, "news", "g1-news")
        agents.set_notes(G1, "idea", "g1-idea")
        agents.set_notes(G2, "news", "g2-news")
        assert agents.memory(G1, "news")["notes"] == "g1-news"
        assert agents.memory(G1, "idea")["notes"] == "g1-idea"
        assert agents.memory(G2, "news")["notes"] == "g2-news"

    def test_remember_basic(self, agents):
        agents.remember(G1, "news", "上次 SSL 新闻反馈好", refs=["https://a"], source_id="s1", now=100.0)
        m = agents.memory(G1, "news")
        assert len(m["learned"]) == 1
        ent = m["learned"][0]
        assert ent["text"] == "上次 SSL 新闻反馈好"
        assert ent["refs"] == ["https://a"]
        assert ent["source_id"] == "s1"
        assert ent["updated"] == 100.0

    def test_remember_source_id_idempotent(self, agents):
        agents.remember(G1, "news", "第一版", source_id="s1", now=1.0)
        agents.remember(G1, "news", "第二版", source_id="s1", now=2.0)
        m = agents.memory(G1, "news")
        assert len(m["learned"]) == 1
        assert m["learned"][0]["text"] == "第一版"

    def test_remember_cap_12(self, agents):
        for i in range(16):
            agents.remember(G1, "news", f"条目{i}", now=float(i))
        m = agents.memory(G1, "news")
        assert len(m["learned"]) == 12
        texts = [e["text"] for e in m["learned"]]
        assert "条目0" not in texts
        assert "条目15" in texts

    def test_remember_text_limit(self, agents):
        with pytest.raises(ValueError):
            agents.remember(G1, "news", "x" * 1201)
        agents.remember(G1, "news", "x" * 1200)
        assert len(agents.memory(G1, "news")["learned"]) == 1

    def test_task_kind_no_memory(self, agents):
        """task 不积累跨任务记忆。"""
        agents.remember(G1, "task", "不该记")
        m = agents.memory(G1, "task")
        assert m["learned"] == []

    def test_memory_kinds_isolated(self, agents):
        agents.remember(G1, "news", "news-memory")
        assert agents.memory(G1, "goal")["learned"] == []
        assert agents.memory(G2, "news")["learned"] == []

    def test_set_notes_task_rejected(self, agents):
        """task 没有可编辑记忆：写 notes 也拒绝（只读交接记录）。"""
        with pytest.raises(ValueError):
            agents.set_notes(G1, "task", "不该写")


class TestPrompt:
    def test_prompt_contains_profile_and_memory(self, agents):
        agents.update_profile("news", {"title": "资讯小队", "instructions": "先看画像再搜"})
        agents.set_notes(G1, "news", "群喜欢硬件")
        agents.remember(G1, "news", "上次 AI 新闻反响好", now=1.0)
        text = agents.prompt(G1, "news")
        assert "资讯小队" in text
        # 专岗改版 3/4：instructions 不再单独注入（搬进各 kind 的 AGENTS.md 由 workers
        # 注入；这里只剩「数据」段，避免双重注入）。profile.title、工作册、既往验收都在。
        assert "先看画像再搜" not in text
        assert "群喜欢硬件" in text
        assert "上次 AI 新闻反响好" in text

    def test_prompt_says_memory_is_data(self, agents):
        agents.set_notes(G1, "news", "群喜欢硬件")
        text = agents.prompt(G1, "news")
        assert "数据" in text
        assert ("不是指令" in text) or ("不是命令" in text)

    def test_prompt_no_cross_group_or_kind(self, agents):
        agents.set_notes(G1, "news", "g1-news-notes")
        agents.set_notes(G2, "news", "g2-news-notes")
        agents.set_notes(G1, "idea", "g1-idea-notes")
        text = agents.prompt(G1, "news")
        assert "g1-news-notes" in text
        assert "g2-news-notes" not in text
        assert "g1-idea-notes" not in text

    def test_prompt_bounded(self, agents):
        agents.remember(G1, "news", "x" * 1200, now=1.0)
        agents.set_notes(G1, "news", "y" * 2000)
        text = agents.prompt(G1, "news")
        assert len(text) < 20000


# ----------------------------------------------------------------------
# 交接单
# ----------------------------------------------------------------------


class TestHandoffs:
    def test_begin_returns_id(self, agents):
        hid = agents.begin(G1, "news", "查本周硬件新闻")
        assert isinstance(hid, str) and hid
        h = agents.handoff(G1, hid)
        assert h is not None
        assert h["status"] == "queued"
        assert h["brief"] == "查本周硬件新闻"
        assert h["group_id"] == G1
        assert h["kind"] == "news"

    def test_begin_records_scope(self, agents):
        hid = agents.begin(
            G1, "news", "b",
            task_id="T-1", phase="verify", parent_id="P-1",
            tools=("web_search",), skills=("search-exa",),
            criteria=["真打开过"],
        )
        h = agents.handoff(G1, hid)
        assert h["task_id"] == "T-1"
        assert h["phase"] == "verify"
        assert h["parent_id"] == "P-1"
        assert list(h["tools"]) == ["web_search"]
        assert list(h["skills"]) == ["search-exa"]
        assert h["criteria"] == ["真打开过"]

    def test_full_lifecycle_accept(self, agents):
        hid = agents.begin(G1, "news", "b")
        agents.running(G1, hid)
        assert agents.handoff(G1, hid)["status"] == "running"
        agents.returned(G1, hid, "找到了 3 条", data={"items": [1]}, evidence=["https://a"], ok=True)
        h = agents.handoff(G1, hid)
        assert h["status"] == "returned"
        assert h["summary"] == "找到了 3 条"
        assert h["evidence"] == ["https://a"]
        agents.review(G1, hid, True, "收了", refs=["https://a"], learn=True)
        h = agents.handoff(G1, hid)
        assert h["status"] == "accepted"
        assert h["review"]

    def test_returned_failed_goes_failed(self, agents):
        hid = agents.begin(G1, "news", "b")
        agents.running(G1, hid)
        agents.returned(G1, hid, "", ok=False, error="模型超时")
        assert agents.handoff(G1, hid)["status"] == "failed"

    def test_reject_path(self, agents):
        hid = agents.begin(G1, "news", "b")
        agents.running(G1, hid)
        agents.returned(G1, hid, "结果", ok=True)
        agents.review(G1, hid, False, "质量不够", learn=True)
        assert agents.handoff(G1, hid)["status"] == "rejected"

    def test_fail_from_queued_running_returned(self, agents):
        for from_state in ("queued", "running", "returned"):
            hid = agents.begin(G1, "news", f"b-{from_state}")
            if from_state in ("running", "returned"):
                agents.running(G1, hid)
            if from_state == "returned":
                agents.returned(G1, hid, "s", ok=True)
            agents.fail(G1, hid, "出错")
            assert agents.handoff(G1, hid)["status"] == "failed"

    def test_fail_cancelled(self, agents):
        hid = agents.begin(G1, "news", "b")
        agents.running(G1, hid)
        agents.fail(G1, hid, "已取消", state="cancelled")
        assert agents.handoff(G1, hid)["status"] == "cancelled"

    def test_terminal_states_cannot_revive(self, agents):
        terminals = (
            ("accepted", lambda h: agents.review(G1, h, True, "ok")),
            ("rejected", lambda h: agents.review(G1, h, False, "no")),
            ("failed", lambda h: agents.fail(G1, h, "err")),
            ("cancelled", lambda h: agents.fail(G1, h, "err", state="cancelled")),
        )
        for terminal_state, terminal_fn in terminals:
            hid = agents.begin(G1, "news", f"t-{terminal_state}")
            agents.running(G1, hid)
            agents.returned(G1, hid, "s", ok=True)
            terminal_fn(hid)
            assert agents.handoff(G1, hid)["status"] == terminal_state
            for revive in (
                lambda: agents.running(G1, hid),
                lambda: agents.returned(G1, hid, "x", ok=True),
                lambda: agents.review(G1, hid, True, "again"),
                lambda: agents.fail(G1, hid, "again"),
            ):
                with pytest.raises(ValueError):
                    revive()
            assert agents.handoff(G1, hid)["status"] == terminal_state

    def test_invalid_transitions(self, agents):
        hid = agents.begin(G1, "news", "b")
        with pytest.raises(ValueError):
            agents.returned(G1, hid, "s", ok=True)
        with pytest.raises(ValueError):
            agents.review(G1, hid, True, "s")
        agents.running(G1, hid)
        with pytest.raises(ValueError):
            agents.review(G1, hid, True, "s")
        with pytest.raises(ValueError):
            agents.running(G1, hid)

    def test_cross_group_protection(self, agents):
        hid = agents.begin(G1, "news", "b")
        assert agents.handoff(G2, hid) is None
        with pytest.raises(Exception):
            agents.running(G2, hid)
        with pytest.raises(Exception):
            agents.returned(G2, hid, "s", ok=True)
        with pytest.raises(Exception):
            agents.review(G2, hid, True, "s")
        with pytest.raises(Exception):
            agents.fail(G2, hid, "err")
        assert agents.handoff(G1, hid)["status"] == "queued"

    def test_handoff_unknown_id(self, agents):
        assert agents.handoff(G1, "no-such-id") is None
        with pytest.raises((ValueError, KeyError)):
            agents.running(G1, "no-such-id")

    def test_summary_evidence_bounded(self, agents):
        hid = agents.begin(G1, "news", "b")
        agents.running(G1, hid)
        agents.returned(G1, hid, "x" * 50000, evidence=["https://a"] * 100)
        h = agents.handoff(G1, hid)
        assert len(h["summary"]) <= 4000
        assert len(h["evidence"]) <= 20

    # ---- 验收学习闸 ----

    def test_accept_learn_writes_memory(self, agents):
        hid = agents.begin(G1, "news", "b")
        agents.running(G1, hid)
        agents.returned(G1, hid, "找到 3 条硬件新闻", ok=True)
        agents.review(G1, hid, True, "这批质量好", refs=["https://a"], learn=True)
        m = agents.memory(G1, "news")
        assert len(m["learned"]) == 1

    def test_accept_no_learn_no_memory(self, agents):
        hid = agents.begin(G1, "news", "b")
        agents.running(G1, hid)
        agents.returned(G1, hid, "s", ok=True)
        agents.review(G1, hid, True, "accepted 但不学", learn=False)
        assert agents.handoff(G1, hid)["status"] == "accepted"  # 状态照样迁移
        assert agents.memory(G1, "news")["learned"] == []

    def test_rejected_no_memory(self, agents):
        hid = agents.begin(G1, "news", "b")
        agents.running(G1, hid)
        agents.returned(G1, hid, "s", ok=True)
        agents.review(G1, hid, False, "不够好", learn=True)
        assert agents.memory(G1, "news")["learned"] == []

    def test_failed_no_memory(self, agents):
        hid = agents.begin(G1, "news", "b")
        agents.running(G1, hid)
        agents.returned(G1, hid, "s", ok=False, error="超时")
        assert agents.memory(G1, "news")["learned"] == []

    def test_task_kind_learn_never_persists(self, agents):
        hid = agents.begin(G1, "task", "b")
        agents.running(G1, hid)
        agents.returned(G1, hid, "done", ok=True)
        agents.review(G1, hid, True, "好", learn=True)
        assert agents.memory(G1, "task")["learned"] == []

    def test_review_source_id_idempotent(self, agents):
        hid = agents.begin(G1, "news", "b")
        agents.running(G1, hid)
        agents.returned(G1, hid, "s", ok=True)
        agents.review(G1, hid, True, "好", learn=True)
        learned = agents.memory(G1, "news")["learned"]
        assert len(learned) == 1
        assert learned[0]["source_id"]

    # ---- 列表 ----

    def test_handoffs_listing(self, agents):
        ids = [agents.begin(G1, "news", f"b{i}") for i in range(3)]
        last_g1 = agents.begin(G1, "idea", "idea-b")
        agents.begin(G2, "news", "g2-b")
        items = agents.handoffs(G1)
        assert len(items) == 4
        news = agents.handoffs(G1, kind="news")
        assert len(news) == 3
        assert all(i["kind"] == "news" for i in news)
        for field in ("id", "group_id", "kind", "task_id", "phase", "parent_id",
                      "status", "brief", "criteria", "tools", "skills",
                      "summary", "evidence", "review", "created", "updated"):
            assert field in items[0], f"缺字段 {field}"
        assert items[0]["id"] == last_g1  # 最近建的在前（G2 那张不算）
        assert [i["id"] for i in news] == [ids[2], ids[1], ids[0]]  # 倒序

    def test_handoffs_limit(self, agents):
        for i in range(25):
            agents.begin(G1, "news", f"b{i}")
        items = agents.handoffs(G1, limit=20)
        assert len(items) == 20

    def test_handoffs_limit_hard_cap(self, agents):
        for i in range(60):
            agents.begin(G1, "news", f"b{i}")
        items = agents.handoffs(G1, limit=1000)
        assert len(items) <= 50

    def test_handoffs_no_raw_data_in_list(self, agents):
        hid = agents.begin(G1, "news", "b")
        agents.running(G1, hid)
        agents.returned(G1, hid, "s", data={"big": "x" * 30000}, ok=True)
        items = agents.handoffs(G1)
        for it in items:
            data = it.get("data")
            if data is not None:
                assert len(str(data)) <= 4000


# ----------------------------------------------------------------------
# 事务一致性
# ----------------------------------------------------------------------


class TestTxnConsistency:
    """验收+学习在同一事务提交；中途失败整体回滚，不留半截状态。"""

    def test_review_accept_and_learn_atomic(self, store, settings, monkeypatch):
        ag = Agents(store, lambda: settings)
        hid = ag.begin(G1, "news", "b")
        ag.running(G1, hid)
        ag.returned(G1, hid, "s", ok=True)

        # 让记忆写入在事务内炸掉：整个 review 必须回滚，状态保持 returned
        orig = Agents._remember_tx

        def boom(*a, **kw):
            raise RuntimeError("记忆写入模拟失败")

        monkeypatch.setattr(Agents, "_remember_tx", boom)
        with pytest.raises(RuntimeError):
            ag.review(G1, hid, True, "好", learn=True)
        assert ag.handoff(G1, hid)["status"] == "returned"   # 状态没被半截改掉
        assert ag.memory(G1, "news")["learned"] == []

        # 恢复后重放验收：能正常 accepted + 学习（没被那次失败锁死）
        monkeypatch.setattr(Agents, "_remember_tx", orig)
        ag.review(G1, hid, True, "好", learn=True)
        assert ag.handoff(G1, hid)["status"] == "accepted"
        assert len(ag.memory(G1, "news")["learned"]) == 1

    def test_full_pipeline_reraise_and_data(self, store, settings):
        """完整走一遍流程后，库里既有交接单又有记忆，且互不串群。"""
        ag = Agents(store, lambda: settings)
        for gid in (G1, G2):
            hid = ag.begin(gid, "news", f"brief-{gid}")
            ag.running(gid, hid)
            ag.returned(gid, hid, f"summary-{gid}", data={"k": gid}, evidence=["https://e"], ok=True)
            ag.review(gid, hid, True, f"验收-{gid}", learn=True)
        m1 = ag.memory(G1, "news")["learned"]
        m2 = ag.memory(G2, "news")["learned"]
        assert len(m1) == 1 and len(m2) == 1
        assert m1[0]["text"] == "验收-900000001"
        assert m2[0]["text"] == "验收-123456789"
        h1 = ag.handoffs(G1)
        h2 = ag.handoffs(G2)
        assert len(h1) == 1 and len(h2) == 1
        assert h1[0]["summary"] == "summary-900000001"
        assert h2[0]["summary"] == "summary-123456789"


class TestStoreLockUsed:
    """确认写路径都走 Store.tx（BEGIN IMMEDIATE 单写入者）而不是裸 connection.write。"""

    def test_write_methods_hold_store_lock(self, store, settings):
        ag = Agents(store, lambda: settings)
        entered: list[str] = []
        orig_tx = store.tx

        def spy_tx():
            entered.append("tx")
            return orig_tx()

        store.tx = spy_tx  # type: ignore[assignment]
        try:
            ag.set_notes(G1, "news", "n")
            ag.remember(G1, "news", "a")
            hid = ag.begin(G1, "news", "b")
            ag.running(G1, hid)
            ag.returned(G1, hid, "s", ok=True)
            ag.review(G1, hid, True, "ok", learn=True)
            ag.fail(G1, ag.begin(G1, "news", "b2"), "err")
            ag.update_profile("news", {"title": "改名"})
        finally:
            store.tx = orig_tx  # type: ignore[assignment]
        # 每一次写都必须进过 tx（≥8 次）
        assert len(entered) >= 8, f"写路径没都走 Store.tx：{entered}"
