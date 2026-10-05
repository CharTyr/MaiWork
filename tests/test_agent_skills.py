"""每群三份（docs/17 §七 + §八）：

1) 本群做法 skill —— agent_skills / agent_skill_versions 两张表替换 agent_lessons：
   - 专岗（news/idea/goal/自定义）每群每岗恰好一份，名字固定「<kind>-本群做法」；自动换起禁（409）；
   - 通用执行（kind=task）每群 active ≤12 份，名字是一类活；满 12 不能新建（400/ValueError）；
   - 版本：改动/回退前把旧正文存一版（source admin/auto/rollback/migrate），每份只留最近 20 版；
   - CRUD + 权限闸（非服务群 ValueError、id 跨群/跨岗 KeyError）；
   - 用途计数：touch_use 让 uses+1、last_used 更新。
2) 本群规矩 —— group_rules(group_id PK body≤3000, updated, updated_by) + group_rule_versions（最近 20 版）。
3) delete_custom 连带清掉本专岗各群 skill。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"
G2 = "123456789"


class _Settings:
    def __init__(self, served=()):
        self.served = set(served)
        self.model_list = ()

    def is_served(self, gid):
        return str(gid) in self.served


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "t.db")
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
# schema / CRUD
# ----------------------------------------------------------------------


class TestSchema:
    def test_tables_exist_after_migrate(self, store):
        names = {r["name"] for r in store.read().execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "agent_skills" in names
        assert "agent_skill_versions" in names
        assert "group_rules" in names
        assert "group_rule_versions" in names
        # 第一个版本就没建过 agent_lessons（线上从没这张表）
        assert "agent_lessons" not in names

    def test_agent_lessons_dropped_in_migrate_34(self, tmp_path):
        """万一真有人在库里有 agent_lessons（不该发生），同一迁移顺手 DROP。"""
        s = Store(tmp_path / "x.db")
        s.migrate()
        s.read().execute(
            "CREATE TABLE agent_lessons (id INTEGER PRIMARY KEY, group_id TEXT, kind TEXT, text TEXT)")
        # 再跑一遍 migrate：迁移函数幂等、版本已到 → 不重复跑；直接调迁移函数验 DROP
        from CharTyr_MaiWork.maiwork import store as store_mod
        fn = store_mod._MIGRATIONS[33]  # 第 34 步（之后 docs/20 加了第 35 步 task_lanes）
        assert fn.__name__ == "_m_agent_skills_group_rules"
        fn(s.read())
        names = {r["name"] for r in s.read().execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "agent_lessons" not in names
        s.close()


class TestSpecialistSkillCrud:
    def test_add_first_produces_fixed_name(self, agents):
        sid = agents.skill_add(G1, "news", description="资讯那一套的群做法", body="先看画像再找")
        row = agents.skill_get(G1, sid)
        assert row["name"] == "news-本群做法"
        assert row["kind"] == "news"
        assert row["status"] == "active"
        assert row["locked"] is False
        assert row["uses"] == 0
        assert row["last_used"] == 0.0

    def test_add_second_specialist_raises_conflict(self, agents):
        agents.skill_add(G1, "news", description="", body="第一版")
        with pytest.raises(FileExistsError):
            agents.skill_add(G1, "news", description="", body="再来一份")

    def test_main_has_no_skill(self, agents):
        with pytest.raises(ValueError):
            agents.skill_add(G1, "main", description="", body="x")
        with pytest.raises(ValueError):
            agents.skills(G1, "main")

    def test_not_served_group_zero_read(self, agents):
        with pytest.raises(ValueError):
            agents.skill_add("999", "news", description="", body="x")
        with pytest.raises(ValueError):
            agents.skills("999", "news")

    def test_skills_list_ordering(self, agents):
        agents.skill_add(G1, "news", description="", body="A")
        agents.skill_add(G1, "idea", description="", body="B")
        out = agents.skills(G1)
        kinds = [r["kind"] for r in out]
        assert "news" in kinds and "idea" in kinds

    def test_cross_group_id_raises(self, agents):
        sid = agents.skill_add(G1, "news", description="", body="x")
        with pytest.raises(KeyError):
            agents.skill_get(G2, sid)

    def test_body_and_description_limits(self, agents):
        with pytest.raises(ValueError):
            agents.skill_add(G1, "news", description="x" * 121, body="ok")
        with pytest.raises(ValueError):
            agents.skill_add(G1, "news", description="", body="x" * 2501)
        # task kind 4000 上限
        with pytest.raises(ValueError):
            agents.skill_add(G1, "task", name="长到不行", description="", body="x" * 4001)


class TestTaskKindSkills:
    def test_task_needs_name(self, agents):
        with pytest.raises(ValueError):
            agents.skill_add(G1, "task", description="d", body="b")
        sid = agents.skill_add(G1, "task", name="整理报名表", description="d", body="b")
        assert agents.skill_get(G1, sid)["name"] == "整理报名表"

    def test_task_active_cap_12(self, agents):
        for i in range(12):
            agents.skill_add(G1, "task", name=f"活{i}", description="", body="x")
        with pytest.raises(ValueError):
            agents.skill_add(G1, "task", name="第13份", description="", body="x")

    def test_archived_not_counted_in_cap(self, agents):
        ids = [agents.skill_add(G1, "task", name=f"活{i}", description="", body="x") for i in range(12)]
        agents.skill_update(G1, ids[0], status="archived")
        # 归档一份后又能新建
        agents.skill_add(G1, "task", name="补位", description="", body="x")

    def test_unique_name_per_group_kind(self, agents):
        agents.skill_add(G1, "task", name="同名", description="", body="x")
        with pytest.raises(FileExistsError):
            agents.skill_add(G1, "task", name="同名", description="", body="y")
        # 另一群同名没事
        agents.skill_add(G2, "task", name="同名", description="", body="x")


class TestUpdateAndVersions:
    def test_update_saves_version(self, agents):
        sid = agents.skill_add(G1, "news", description="d1", body="第一版")
        agents.skill_update(G1, sid, body="第二版", description="d2")
        versions = agents.skill_versions(G1, "news", sid)
        # 2 版 = 建表时的初版（source=admin）+ 这次改动前的旧正文
        assert len(versions) == 2
        assert versions[0]["body"] == "第一版"
        assert versions[0]["source"] == "admin"
        assert agents.skill_get(G1, sid)["body"] == "第二版"

    def test_only_20_versions_kept(self, agents):
        sid = agents.skill_add(G1, "news", description="", body="v0")
        for i in range(1, 25):
            agents.skill_update(G1, sid, body=f"v{i}")
        versions = agents.skill_versions(G1, "news", sid)
        assert len(versions) == 20
        # 最近 20 版：v4..v23（v24 是当前正文，不进版本；最老 v0..v3 被裁掉）
        bodies = [v["body"] for v in versions]
        assert "v23" in bodies and "v4" in bodies
        assert "v0" not in bodies

    def test_restore_saves_current_then_restores(self, agents):
        sid = agents.skill_add(G1, "news", description="", body="第一版")
        agents.skill_update(G1, sid, body="第二版")
        vid = agents.skill_versions(G1, "news", sid)[0]["id"]
        agents.skill_restore_version(G1, "news", sid, vid)
        assert agents.skill_get(G1, sid)["body"] == "第一版"
        versions = agents.skill_versions(G1, "news", sid)
        assert any(v["source"] == "rollback" and v["body"] == "第二版" for v in versions)

    def test_locked_blocks_auto_patch_only(self, agents):
        """locked 只挡自动流程（patch_body），管理员手改照样能进。"""
        sid = agents.skill_add(G1, "news", description="", body="一二三")
        agents.skill_update(G1, sid, locked=True)
        with pytest.raises(ValueError):
            agents.skill_patch_body(G1, "news", sid, [{"old": "二", "new": "X"}], source="auto", note="n")
        agents.skill_update(G1, sid, body="一二三改")
        assert agents.skill_get(G1, sid)["body"] == "一二三改"

    def test_delete_cascades_versions(self, agents):
        sid = agents.skill_add(G1, "news", description="", body="x")
        agents.skill_update(G1, sid, body="y")
        agents.skill_delete(G1, "news", sid)
        with pytest.raises(KeyError):
            agents.skill_get(G1, sid)


class TestPatchBody:
    def test_patch_exact_once(self, agents):
        sid = agents.skill_add(G1, "news", description="", body="先做 A，再做 B，最后 C")
        agents.skill_patch_body(G1, "news", sid, [{"old": "再做 B", "new": "再做 beta"}], source="auto", note="改一句")
        assert "beta" in agents.skill_get(G1, sid)["body"]

    def test_patch_requires_single_match(self, agents):
        sid = agents.skill_add(G1, "news", description="", body="A A")
        with pytest.raises(ValueError):
            agents.skill_patch_body(G1, "news", sid, [{"old": "A", "new": "X"}], source="auto", note="")
        with pytest.raises(ValueError):
            agents.skill_patch_body(G1, "news", sid, [{"old": "不在的", "new": "X"}], source="auto", note="")

    def test_patch_saves_version_with_source_and_note(self, agents):
        sid = agents.skill_add(G1, "news", description="", body="旧")
        agents.skill_patch_body(G1, "news", sid, [{"old": "旧", "new": "新"}], source="auto", note="复盘")
        v = agents.skill_versions(G1, "news", sid)[0]
        assert v["source"] == "auto" and v["note"] == "复盘"

    def test_patch_max_4_edits(self, agents):
        sid = agents.skill_add(G1, "news", description="", body="1 2 3 4 5")
        with pytest.raises(ValueError):
            agents.skill_patch_body(
                G1, "news", sid,
                [{"old": str(i), "new": "x"} for i in range(1, 6)],
                source="auto", note="")


class TestUses:
    def test_touch_use(self, agents):
        sid = agents.skill_add(G1, "task", name="整理报名表", description="", body="x")
        agents.skill_touch_use(G1, "task", "整理报名表")
        row = agents.skill_get(G1, sid)
        assert row["uses"] == 1 and row["last_used"] > 0

    def test_touch_use_archived_noop(self, agents):
        sid = agents.skill_add(G1, "task", name="归档的", description="", body="x")
        agents.skill_update(G1, sid, status="archived")
        agents.skill_touch_use(G1, "task", "归档的")
        assert agents.skill_get(G1, sid)["uses"] == 0


class TestListByName:
    def test_skill_by_name_active_only(self, agents):
        agents.skill_add(G1, "task", name="某个名", description="", body="正文")
        assert agents.skill_by_name(G1, "task", "某个名")["body"] == "正文"
        with pytest.raises(KeyError):
            agents.skill_by_name(G1, "task", "别人的")

    def test_archived_not_visible_by_name(self, agents):
        sid = agents.skill_add(G1, "task", name="已归档", description="", body="x")
        agents.skill_update(G1, sid, status="archived")
        with pytest.raises(KeyError):
            agents.skill_by_name(G1, "task", "已归档")


class TestCustomKindCleanup:
    def test_delete_custom_cleans_skills(self, agents, store):
        p = agents.create_custom("调研小帮手")
        kind = p["kind"]
        agents.skill_add(G1, kind, description="", body="x")
        agents.skill_add(G2, kind, description="", body="y")
        agents.delete_custom(kind)
        # kind 已从名册删掉，不能再走 skills()（_kind_known 会拒）；直接数行数
        n1 = store.read().execute("SELECT COUNT(*) AS c FROM agent_skills WHERE kind=?", (kind,)).fetchone()["c"]
        assert int(n1) == 0


# ----------------------------------------------------------------------
# group_rules
# ----------------------------------------------------------------------


class TestGroupRules:
    def test_read_empty_default(self, agents):
        row = agents.group_rules_get(G1)
        assert row["body"] == ""
        assert row["updated"] == 0.0
        assert row["updated_by"] == ""

    def test_put_and_get(self, agents):
        out = agents.group_rules_set(G1, "不许发广告\n资讯要中文的", updated_by="admin")
        assert out["body"].startswith("不许发广告")
        assert out["updated"] > 0
        assert out["updated_by"] == "admin"
        again = agents.group_rules_get(G1)
        assert again == out

    def test_body_limit(self, agents):
        with pytest.raises(ValueError):
            agents.group_rules_set(G1, "x" * 3001, updated_by="admin")

    def test_versions_on_change(self, agents):
        agents.group_rules_set(G1, "第一版", updated_by="admin")
        agents.group_rules_set(G1, "第二版", updated_by="group_admin")
        vs = agents.group_rules_versions(G1)
        assert len(vs) == 1
        assert vs[0]["body"] == "第一版"
        assert vs[0]["updated_by"] == "admin"
        # 恢复第一版
        agents.group_rules_restore(G1, vs[0]["id"])
        assert agents.group_rules_get(G1)["body"] == "第一版"
        vs2 = agents.group_rules_versions(G1)
        assert len(vs2) == 2
        assert vs2[0]["body"] == "第二版"

    def test_versions_cap_20(self, agents):
        agents.group_rules_set(G1, "v0", updated_by="admin")
        for i in range(1, 25):
            agents.group_rules_set(G1, f"v{i}", updated_by="admin")
        vs = agents.group_rules_versions(G1)
        assert len(vs) == 20

    def test_same_body_no_version(self, agents):
        agents.group_rules_set(G1, "一样的", updated_by="admin")
        agents.group_rules_set(G1, "一样的", updated_by="admin")
        assert agents.group_rules_versions(G1) == []

    def test_not_served(self, agents):
        with pytest.raises(ValueError):
            agents.group_rules_get("999")
