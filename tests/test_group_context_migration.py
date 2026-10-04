"""每群三份 ②：启动迁移（docs/17 §八.1 + A 节口味并入）。

- 口味小结 kv["feeds.taste.<gid>"]（有 text 的 3 个群）→ 各群资讯 skill（kind=news,
  name=news-本群做法）的初始正文，版本记 source=migrate；口味 kv 和 feeds.pref 删掉。
- 本群提醒 agent_memory_notes（每群每岗 notes 合一段）、资讯偏好 kv feeds.pref.<gid>、
  每群工作记忆 identity/memory/<gid>.md → 拼进本群规矩（原文一段段贴，空段跳过）；
  迁完删旧来源（memory/<gid>.md 先备份到 <data_dir>/identity/mem.bak/<gid>.md 再清空）。
- 幂等：按「row 已存在（含 migrate 来源的版本）」判定已迁，第二次启动什么都不做。
- 拒绝路径：非服务群的 key 不碰；已有更新的规矩（管理员后来手改过）**而不是 migrate 来的**
  时，跳过那一条不覆盖。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import migrations
from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.identity import Identity
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"
G2 = "123456789"


class _Settings:
    def __init__(self, data_dir, served):
        self.served = set(served)
        self.model_list = ()
        self._data_dir = Path(data_dir)

    @property
    def data_dir(self):
        return self._data_dir

    def is_served(self, gid):
        return str(gid) in self.served


@pytest.fixture
def make_env(tmp_path: Path):
    """建一环境：store + identity + agents；吐 dict 供复用"""
    def _go(served=(G1, G2), init_tables=()):
        store = Store(tmp_path / "t.db")
        store.migrate()
        settings = _Settings(tmp_path, served)
        agents = Agents(store, lambda: settings)
        identity = Identity(tmp_path, store, lambda: settings, host=None)
        return {"store": store, "settings": settings, "agents": agents,
                "identity": identity, "tmp_path": tmp_path}
    return _go


def _seed_all(store, settings):
    """塞全套旧来源：口味味 pref + notes + memory/<gid>.md"""
    with store.tx() as conn:
        store.kv_set(conn, f"feeds.taste.{G1}", {"text": "爱看开发内幕，不爱营销稿", "ts": 1.0})
        store.kv_set(conn, f"feeds.pref.{G1}", "找中文的，别太长")
        store.kv_set(conn, "feeds.taste.非服务群", {"text": "不该被碰", "ts": 0.0})
    # agent_memory_notes（按群每岗；懒建没碰过 → 先让 Agents 建表）
    env_agents = Agents(store, lambda: settings)
    env_agents._ensure_schema()
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO agent_memory_notes (group_id, kind, notes, updated) VALUES (?, ?, ?, 1.0)",
            (G1, "news", "资讯要加个「顺手一提」"),
        )
        conn.execute(
            "INSERT INTO agent_memory_notes (group_id, kind, notes, updated) VALUES (?, ?, ?, 1.0)",
            (G1, "idea", "构想别发太早"),
        )
    mem_dir = Path(settings.data_dir) / "identity" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / f"{G1}.md").write_text("- 2026-10-01 这个群爱看开发内幕（手动写）\n", encoding="utf-8")


class TestEverythingMigratesIntoRulesAndSkill:
    def test_full_migration(self, make_env):
        env = make_env()
        _seed_all(env["store"], env["settings"])
        migrated = migrations.migrate_group_context_to_rules_and_skills(
            env["store"], env["settings"], env["identity"], lambda g: str(env["settings"].data_dir),
        )
        # 规矩吃到 notes + pref + memory
        rules = env["agents"].group_rules_get(G1)
        for frag in ("资讯要加个「顺手一提」", "构想别发太早", "找中文的，别太长", "这个群爱看开发内幕"):
            assert frag in rules["body"], frag
        assert rules["updated_by"] == "migrate"
        # 资讯 skill 吃到口味
        skill = env["agents"].skill_by_name(G1, "news", env["agents"].skill_get(
                G1, env["agents"].skills(G1, "news", include_archived=True)[0]["id"])["name"])
        assert "爱看开发内幕，不爱营销稿" in skill["body"]
        # 版本记录 migrate：建这一份时就把初版正文留了一版（可溯源、可回退到迁移来的初版）
        vs = env["agents"].skill_versions(G1, "news", skill["id"])
        assert len(vs) == 1
        assert vs[0]["source"] == "migrate"
        assert vs[0]["note"] == "开迁移：口味小结进本群做法"
        assert vs[0]["body"] == "爱看开发内幕，不爱营销稿"
        # 旧来源清掉
        assert env["store"].kv_get(f"feeds.taste.{G1}") is None
        assert env["store"].kv_get(f"feeds.pref.{G1}") is None
        rows = env["store"].read().execute(
            "SELECT COUNT(*) AS c FROM agent_memory_notes WHERE group_id=?", (G1,)).fetchone()
        assert int(rows["c"]) == 0
        # memory/<gid>.md 清空+备份
        mem_file = Path(env["settings"].data_dir) / "identity" / "memory" / f"{G1}.md"
        assert mem_file.read_text(encoding="utf-8") == ""
        backup = Path(env["settings"].data_dir) / "identity" / "mem.bak" / f"{G1}.md"
        assert backup.read_text(encoding="utf-8") == "- 2026-10-01 这个群爱看开发内幕（手动写）\n"
        # 非服务群的 taste kv 不动
        assert env["store"].kv_get("feeds.taste.非服务群") is not None

    def test_idempotent(self, make_env):
        env = make_env()
        _seed_all(env["store"], env["settings"])
        migrations.migrate_group_context_to_rules_and_skills(
            env["store"], env["settings"], env["identity"], lambda g: str(env["settings"].data_dir),
        )
        body_after_first = env["agents"].group_rules_get(G1)["body"]
        # 第二批又塞一点旧东西（部分幂等：成员 B 已经迁过规矩，A 只补它没迁过的）
        with env["store"].tx() as conn:
            env["store"].kv_set(conn, f"feeds.pref.{G1}", "新补一段偏好")
        skill_before = env["agents"].skills(G1, "news", include_archived=True)[0]["body"]
        # 但资讯 skill 已有 migrate 版本 → 同一段口味不再重复拼
        with env["store"].tx() as conn:
            env["store"].kv_set(conn, f"feeds.taste.{G1}", {"text": "另一套口味", "ts": 2.0})
        migrations.migrate_group_context_to_rules_and_skills(
            env["store"], env["settings"], env["identity"], lambda g: str(env["settings"].data_dir),
        )
        rules2 = env["agents"].group_rules_get(G1)["body"]
        skill2 = env["agents"].skills(G1, "news", include_archived=True)[0]["body"]
        # 规矩：管理员后来手改过的不覆盖（migrate 来的规矩是同一个管理员身份 → 还能补新段；已经拼进规矩的旧段不重复）
        assert "新补一段偏好" in rules2
        assert rules2.count("爱看开发内幕") >= 1
        # skill：migrate 只在第一次拼，不重复
        assert "另一套口味" not in skill2

    def test_admin_edited_not_overwritten(self, make_env):
        env = make_env()
        _seed_all(env["store"], env["settings"])
        migrations.migrate_group_context_to_rules_and_skills(
            env["store"], env["settings"], env["identity"], lambda g: str(env["settings"].data_dir),
        )
        # 管理员手改成完全另一个东西（updated_by = admin，不再是 migrate）
        env["agents"].group_rules_set(G1, "只发搞笑视频", updated_by="admin")
        # 又塞旧东西进来想再迁
        with env["store"].tx() as conn:
            env["store"].kv_set(conn, f"feeds.pref.{G1}", "不该拼进来")
        migrations.migrate_group_context_to_rules_and_skills(
            env["store"], env["settings"], env["identity"], lambda g: str(env["settings"].data_dir),
        )
        assert env["agents"].group_rules_get(G1)["body"] == "只发搞笑视频"
        # 管理员手改过之后：规矩虽不再被覆盖，但「还没被谁看」的旧 kv 也留下来等下次，
        # 不擅自删（规矩不长、可能是管理员想把偏好合并了以后留给下次手写）

    def test_empty_sources_skipped(self, make_env):
        env = make_env()
        # 只有 kv pref 是空串
        with env["store"].tx() as conn:
            env["store"].kv_set(conn, f"feeds.pref.{G1}", "   ")
        migrations.migrate_group_context_to_rules_and_skills(
            env["store"], env["settings"], env["identity"], lambda g: str(env["settings"].data_dir),
        )
        assert env["agents"].group_rules_get(G1)["body"] == ""
        assert env["store"].kv_get(f"feeds.pref.{G1}") is None

    def test_rule_body_overflow_skipped_with_warning_not_crash(self, make_env):
        env = make_env()
        _seed_all(env["store"], env["settings"])
        # 已有接近满的内容（管理员后来手改成另一套、再塞一堆旧东西）应跳塞，不炸
        env["agents"].group_rules_set(G1, "长" * 2998, updated_by="admin")
        migrations.migrate_group_context_to_rules_and_skills(
            env["store"], env["settings"], env["identity"], lambda g: str(env["settings"].data_dir),
        )
        assert env["agents"].group_rules_get(G1)["body"] == "长" * 2998

    def test_unserved_group_not_touched(self, make_env):
        env = make_env(served=(G1,))  # 只有 G1 在服务
        _seed_all(env["store"], env["settings"])  # 但 G2 也塞了 G2
        with env["store"].tx() as conn:
            env["store"].kv_set(conn, f"feeds.taste.{G2}", {"text": "x", "ts": 0.0})
            env["store"].kv_set(conn, f"feeds.pref.{G2}", "y")
        migrations.migrate_group_context_to_rules_and_skills(
            env["store"], env["settings"], env["identity"], lambda g: str(env["settings"].data_dir),
        )
        assert env["store"].kv_get(f"feeds.taste.{G2}") is not None
        assert env["store"].kv_get(f"feeds.pref.{G2}") is not None
