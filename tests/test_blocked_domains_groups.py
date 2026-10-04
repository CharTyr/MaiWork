"""屏蔽域名归一成按群 kv：kv["feeds.blocked.<gid>"] 是唯一来源（docs/18 第一步）。

之前是三份：[feeds] blocked_domains（config.toml）、全局 kv["feeds.blocked_domains"]
（网页维护）、自动屏蔽每群。现在：
- 迁移 = 文件名单 ∪ 全局 kv 名单 → 拷进每个服务群的 kv["feeds.blocked.<gid>"]，
  删全局 kv 键 + 删 config.toml 里的键；
- 名单读写全部按群：feeds.blocked_domains(store, gid)、
  blocked_domains_set(store, gid, names)；
- 全空时（生产现状）只清全局键和文件键，不给群写空行，保持 kv 干净。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import config_file
from CharTyr_MaiWork.maiwork import feeds as feeds_mod
from CharTyr_MaiWork.maiwork.migrations import migrate_blocked_domains_to_groups
from CharTyr_MaiWork.maiwork.store import Store

G1, G2 = "900000001", "123456789"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "data" / "m.db")
    s.migrate()
    yield s
    s.close()


def _plug(tmp_path: Path) -> Path:
    d = tmp_path / "plug"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.toml").write_text(
        "[plugin]\nenabled = true\n\n[feeds]\n# 带注释\nblocked_domains = [\"bad.com\", \"bad2.com\"]\nnews_slots = [\"08:30\"]\n",
        encoding="utf-8",
    )
    return d


class TestPerGroupStore:
    def test_read_default_empty(self, store: Store) -> None:
        assert feeds_mod.blocked_domains(store, G1) == []

    def test_set_then_read_normalizes_and_sorts(self, store: Store) -> None:
        feeds_mod.blocked_domains_set(store, G1, ["WWW.Example.COM", "B.COM", "example.com"])
        assert feeds_mod.blocked_domains(store, G1) == ["b.com", "example.com"]

    def test_per_group_isolated(self, store: Store) -> None:
        feeds_mod.blocked_domains_set(store, G1, ["a.com"])
        assert feeds_mod.blocked_domains(store, G1) == ["a.com"]
        assert feeds_mod.blocked_domains(store, G2) == []

    def test_empty_set_stays_empty(self, store: Store) -> None:
        feeds_mod.blocked_domains_set(store, G1, [])
        assert feeds_mod.blocked_domains(store, G1) == []


class TestMigration:
    def test_union_copied_to_each_group_and_sources_deleted(self, tmp_path: Path, store: Store) -> None:
        plug = _plug(tmp_path)
        with store.tx() as conn:
            store.kv_set(conn, "feeds.blocked_domains", ["bad.com", "kv-only.com"])
        migrate_blocked_domains_to_groups(store, plug, tmp_path / "data", [G1, G2])
        # 并集 = {bad.com, bad2.com, kv-only.com} 进了每个群
        for gid in (G1, G2):
            assert feeds_mod.blocked_domains(store, gid) == ["bad.com", "bad2.com", "kv-only.com"]
        # 全局 kv 键删了；config.toml 里的键也删了
        assert store.kv_get("feeds.blocked_domains") is None
        text = (plug / "config.toml").read_text(encoding="utf-8")
        assert "blocked_domains" not in text
        assert "# 带注释" in text  # 注释没丢
        assert "news_slots" in text  # 别的键没动

    def test_all_empty_cleans_up_without_group_writes(self, tmp_path: Path, store: Store) -> None:
        """生产现状：文件 [] + 全局 kv [] → 只清两处源，不往群里写空名单。"""
        d = tmp_path / "plug"
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.toml").write_text("[feeds]\nblocked_domains = []\n", encoding="utf-8")
        with store.tx() as conn:
            store.kv_set(conn, "feeds.blocked_domains", [])
        migrate_blocked_domains_to_groups(store, d, tmp_path / "data", [G1, G2])
        assert "blocked_domains" not in (d / "config.toml").read_text(encoding="utf-8")
        assert store.kv_get("feeds.blocked_domains") is None
        assert store.kv_get(f"feeds.blocked.{G1}") is None
        assert store.kv_get(f"feeds.blocked.{G2}") is None

    def test_no_file_key_no_kv_noop(self, tmp_path: Path, store: Store) -> None:
        d = tmp_path / "plug"
        d.mkdir(parents=True, exist_ok=True)
        text = "[feeds]\nnews_slots = [\"08:30\"]\n"
        (d / "config.toml").write_text(text, encoding="utf-8")
        migrate_blocked_domains_to_groups(store, d, tmp_path / "data", [G1])
        assert (d / "config.toml").read_text(encoding="utf-8") == text  # 一字节没动

    def test_idempotent(self, tmp_path: Path, store: Store) -> None:
        plug = _plug(tmp_path)
        with store.tx() as conn:
            store.kv_set(conn, "feeds.blocked_domains", ["x.com"])
        migrate_blocked_domains_to_groups(store, plug, tmp_path / "data", [G1])
        first = feeds_mod.blocked_domains(store, G1)
        migrate_blocked_domains_to_groups(store, plug, tmp_path / "data", [G1])
        assert feeds_mod.blocked_domains(store, G1) == first  # 再跑一遍不变
        # 手动改过群名单后重跑不能把群名单盖回（幂等依据是全局键没了 → 不动群名单）
        feeds_mod.blocked_domains_set(store, G1, ["manual.com"])
        migrate_blocked_domains_to_groups(store, plug, tmp_path / "data", [G1])
        assert feeds_mod.blocked_domains(store, G1) == ["manual.com"]
