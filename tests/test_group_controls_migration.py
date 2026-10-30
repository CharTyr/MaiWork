"""0.8.0 群控归一：一次性启动迁移 + 旧入口收口（docs/18）。

背景（parent 拆下来的这一份「迁移与收口」）：
「谁能批本群的活 / 免批」「冷场开话题 / 往群里发多少、几点不打扰」以前是**全局**一份
（config.toml 的 `[approval] required/admins/exempt_groups/exempt_users`、
`[topics] enabled/per_day/speaker`、`[delivery] push_per_day/quiet_hours`），
现在每个服务群自己一份（kv["group_approval.<群号>"] / kv["group_push.<群号>"]）。
本文件锁死这几条契约：

1. `migrate_group_controls(store, settings, plugin_dir, data_dir)`：
   - 只对**服务群**做事；先用**未清版**的 settings 把每群那份种好（group_approval；
     group_push 接口可用时一并种），**确认真的落库**以后才从 config.toml 删旧键；
   - 失败 / config.toml 读不了 → 旧键一个不删（源保留，下次启动再迁）；
   - 幂等：第二遍不删键、不重写文件、不新增备份；
   - 不覆盖**已经存在**的每群配置；
   - 非服务群零读取零写入；
   - 日志不泄密钥。
2. 旧入口收口：`[approval] required/admins/exempt_groups/exempt_users`、
   `[topics] enabled/per_day/speaker`、`[delivery] push_per_day/quiet_hours`
   不再能从「全部配置」网页或 set_rules 工具改，旧 API 明确 400 说「到群页管理」，
   而不是还写进文件、runtime 却不听。

测试用**真 Store + 真 config.toml 文件 + 真 Settings**，核心不 mock（失败路径临时把 kv 写坏）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from CharTyr_MaiWork.maiwork import rules
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.group_approval import KV_PREFIX as APPROVAL_KV
from CharTyr_MaiWork.maiwork.group_push import KV_PREFIX as PUSH_KV
from CharTyr_MaiWork.maiwork.migrations import (
    GROUP_APPROVAL_SEED_KEYS,
    GROUP_PUSH_SEED_KEYS,
    migrate_group_controls,
)
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"
G2 = "123456789"
G3 = "555444333"

SECRET = "sk-live-secret-9527-不许外泄"

BASE_BODY = """\
[plugin]
enabled = true

[groups]
serve = [{group = "qq:%(g1)s"}, {group = "qq:%(g2)s"}]

[storage]
data_dir = "%(data)s"

[jev]
api_key = "%(secret)s"

[approval]
required = true
admins = ["qq:10001"]
exempt_groups = ["qq:%(g1)s"]
exempt_users = ["qq:20002", "telegram:999"]
remind = true
auto_review = true
auto_review_daily = 5

[topics]
enabled = true
speaker = "maiwork"
per_day = 2
min_gap_hours = 3
candidate_ttl_hours = 12

[delivery]
push_per_day = 3
quiet_hours = "23:00-08:00"
"""


def _raw(serve=(G1, G2), *, data_dir: Path) -> dict:
    """未清版 Settings 的原始 dict（旧字段还在）。"""
    return {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{g}", "workspace": f"ws-{g}"} for g in serve]},
        "storage": {"data_dir": str(data_dir)},
        "approval": {
            "required": True,
            "admins": ["qq:10001"],
            "exempt_groups": [f"qq:{G1}"],
            "exempt_users": ["qq:20002", "telegram:999"],
            "remind": True,
            "auto_review": True,
            "auto_review_daily": 5,
        },
        "topics": {"enabled": True, "speaker": "maiwork", "per_day": 2, "min_gap_hours": 3},
        "delivery": {"push_per_day": 3, "quiet_hours": "23:00-08:00"},
    }


class Env:
    def __init__(self, store: Store, settings: Any, plug: Path, data: Path) -> None:
        self.store = store
        self.settings = settings
        self.plug = plug
        self.data = data

    def config_text(self) -> str:
        return (self.plug / "config.toml").read_text(encoding="utf-8")

    def backups(self) -> list[Path]:
        bdir = self.data / "config-backups"
        return sorted(bdir.glob("config.toml-*")) if bdir.exists() else []


@pytest.fixture
def env(tmp_path: Path) -> Env:
    data = tmp_path / "data"
    plug = tmp_path / "plug"
    plug.mkdir(parents=True, exist_ok=True)
    body = BASE_BODY % {"g1": G1, "g2": G2, "data": data, "secret": SECRET}
    (plug / "config.toml").write_text(body, encoding="utf-8")
    store = Store(data / "maiwork.db")
    store.migrate()
    settings, problems = load_settings(_raw(data_dir=data))
    assert not problems, problems
    return Env(store, settings, plug, data)


def _run(env: Env, **kw: Any) -> dict:
    return migrate_group_controls(env.store, env.settings, env.plug, env.data, **kw)


def _seed_legacy_admins(env: Env, gid: str, accounts: list[str]) -> None:
    with env.store.tx() as conn:
        env.store.kv_set(conn, f"group_admins.{gid}", accounts)


# ======================================================================
# 1. 种每群批准名单 + 清源
# ======================================================================


class TestApprovalSeed:
    def test_served_groups_seeded_then_source_cleaned(self, env: Env) -> None:
        _seed_legacy_admins(env, G1, ["qq:30003"])
        result = _run(env)
        # 两个服务群都种下了：全局批准人 ∪ 旧的按群名单（去重保序）
        g1 = env.store.kv_get(APPROVAL_KV + G1)
        g2 = env.store.kv_get(APPROVAL_KV + G2)
        assert g1 == {
            "approvers": ["qq:10001", "qq:30003"],
            "exempt_users": ["qq:20002"],  # telegram:999 不属于本群平台，丢掉
            "exempt_group": True,          # 全局 exempt_groups 含本群
            "required": True,
        }
        assert g2["approvers"] == ["qq:10001"]
        assert g2["exempt_group"] is False
        # 旧的按群名单键清掉了（同一事务）
        assert env.store.kv_get(f"group_admins.{G1}") is None
        assert sorted(result["seeded_approval"]) == sorted([G1, G2])
        # 源：四个全局键从 config.toml 删掉
        text = env.config_text()
        assert "admins" not in text
        assert "exempt_groups" not in text
        assert "exempt_users" not in text
        assert "required" not in text
        # 「自动审核 / 待批提醒」还留着（没归每群）
        assert "remind = true" in text
        assert "auto_review = true" in text
        assert set(result["deleted"]) >= set(GROUP_APPROVAL_SEED_KEYS)

    def test_existing_pergroup_not_overwritten(self, env: Env) -> None:
        with env.store.tx() as conn:
            env.store.kv_set(conn, APPROVAL_KV + G1, {
                "approvers": ["qq:99999"], "exempt_users": [], "exempt_group": False, "required": False,
            })
        result = _run(env)
        # 已有那份一字不动，也不当「这次种的」
        assert env.store.kv_get(APPROVAL_KV + G1) == {
            "approvers": ["qq:99999"], "exempt_users": [], "exempt_group": False, "required": False,
        }
        assert G1 not in result["seeded_approval"]
        assert G2 in result["seeded_approval"]
        # 种好了照样清源（旧键已不是运行来源）
        assert "admins" not in env.config_text()

    def test_failure_keeps_source(self, env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
        """写入事务失败 → 每群记录没落库 → 旧全局键一个不删。"""
        before = env.config_text()

        def _boom(*a: Any, **k: Any) -> None:
            raise RuntimeError("磁盘坏了")

        monkeypatch.setattr(env.store, "kv_set", _boom)
        result = _run(env)
        assert result["problem"], "失败了要有说明"
        assert result["deleted"] == []
        assert env.config_text() == before
        assert "admins" in env.config_text()
        assert env.store.kv_get(APPROVAL_KV + G1) is None

    def test_idempotent_no_duplicate_backup(self, env: Env) -> None:
        first = _run(env)
        assert first["deleted"]
        backups_after_first = len(env.backups())
        assert backups_after_first >= 1
        before = env.config_text()
        second = _run(env)
        assert second["deleted"] == []
        assert second["problem"] == ""
        assert env.config_text() == before
        assert len(env.backups()) == backups_after_first  # 没出活就不该再备份

    def test_non_served_group_not_touched(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        plug = tmp_path / "plug"
        plug.mkdir(parents=True, exist_ok=True)
        (plug / "config.toml").write_text('[approval]\nadmins = ["qq:10001"]\n', encoding="utf-8")
        store = Store(data / "m.db")
        store.migrate()
        settings, _ = load_settings(_raw(serve=(G1,), data_dir=data))
        with store.tx() as conn:
            store.kv_set(conn, f"group_admins.{G3}", ["qq:77777"])
        migrate_group_controls(store, settings, plug, data)
        # 非服务群：零读取零写入（旧的按群名单也原样留着，等它被加到服务列表再迁）
        assert store.kv_get(APPROVAL_KV + G3) is None
        assert store.kv_get(f"group_admins.{G3}") == ["qq:77777"]

    def test_broken_config_keeps_source(self, env: Env) -> None:
        (env.plug / "config.toml").write_text("[approval\n坏了", encoding="utf-8")
        result = _run(env)
        # 每群还是种下了（seed 不需要文件），但源读不了就一个不删
        assert env.store.kv_get(APPROVAL_KV + G1) is not None
        assert result["deleted"] == []
        assert result["problem"]

    def test_no_secret_in_logs(self, env: Env, caplog: pytest.CaptureFixture[str]) -> None:
        with caplog.at_level(logging.DEBUG):
            result = _run(env)
        assert SECRET not in caplog.text
        assert SECRET not in str(result)
        assert SECRET in env.config_text()  # 密钥那行本来就没被这次迁移动（只删登记过的旧键）


# ======================================================================
# 2. group_push 接口可用时：同一迁移种每群推送设置，再清这几键
# ======================================================================


class TestPushSeed:
    def test_push_seeded_then_source_cleaned(self, env: Env) -> None:
        result = _run(env)
        assert result["push_available"] is True
        cfg = env.store.kv_get(PUSH_KV + G1)
        assert isinstance(cfg, dict)
        # 现有数字沿用，不提高频次
        assert cfg["topics_enabled"] is True
        assert cfg["daily_max"] == 3
        assert cfg["quiet_hours"] == "23:00-08:00"
        assert sorted(result["seeded_push"]) == sorted([G1, G2])
        text = env.config_text()
        assert "per_day = 2" not in text.split("[delivery]")[0]  # topics.per_day 没了
        assert "speaker" not in text
        assert "enabled = true" in text  # 这句是 plugin.enabled，还在
        assert "push_per_day" not in text
        assert "quiet_hours" not in text
        # 全局技术项还留着（不是每群一份）
        assert "min_gap_hours = 3" in text
        assert "candidate_ttl_hours = 12" in text
        assert set(result["deleted"]) >= set(GROUP_PUSH_SEED_KEYS)

    def test_push_unavailable_keeps_push_source(self, env: Env) -> None:
        class _Broken:
            KV_PREFIX = "group_push."

            @staticmethod
            def get_config(store: Any, gid: Any, settings: Any = None) -> dict:
                raise RuntimeError("接口还没长好")

        result = _run(env, push_module=_Broken)
        assert result["push_available"] is False
        text = env.config_text()
        # 推送那几键一个没动
        assert "push_per_day" in text and "quiet_hours" in text
        assert "per_day = 2" in text and 'speaker = "maiwork"' in text
        # 批准那份照样迁完（互不拖累）
        assert env.store.kv_get(APPROVAL_KV + G1) is not None
        assert "admins" not in text.split("[topics]")[0]
        assert set(result["deleted"]) >= set(GROUP_APPROVAL_SEED_KEYS)
        assert set(result["deleted"]).isdisjoint(GROUP_PUSH_SEED_KEYS)


# ======================================================================
# 3. 旧入口收口：网页 / 工具都不能再改这些键
# ======================================================================


class TestOldEntriesClosed:
    def test_view_hides_group_managed_keys(self, env: Env) -> None:
        view = rules.config_view(env.settings, env.store)
        keys = {f["key"] for sec in view["sections"] for f in sec["fields"]}
        for key in (*GROUP_APPROVAL_SEED_KEYS, *GROUP_PUSH_SEED_KEYS):
            assert key not in keys, key
        # 还归全局的照样在
        assert "approval.remind" in keys
        assert "approval.auto_review" in keys
        assert "topics.min_gap_hours" in keys

    @pytest.mark.parametrize("key", [*GROUP_APPROVAL_SEED_KEYS, *GROUP_PUSH_SEED_KEYS])
    def test_put_old_key_400_names_group_page(self, env: Env, key: str) -> None:
        before = env.config_text()
        with pytest.raises(ValueError) as ei:
            rules.save_config_patch(env.store, {key: [1] if key.endswith("s") else 1},
                                    base=env.settings, plugin_dir=env.plug)
        msg = str(ei.value)
        assert "群" in msg, msg
        assert env.config_text() == before  # 一个字节没动

    @pytest.mark.parametrize("key", [*GROUP_APPROVAL_SEED_KEYS, *GROUP_PUSH_SEED_KEYS])
    def test_reset_old_key_400(self, env: Env, key: str) -> None:
        with pytest.raises(ValueError):
            rules.reset_config_field(env.store, key, base=env.settings, plugin_dir=env.plug)

    def test_schema_keeps_specs_for_seed_not_editable(self) -> None:
        """规格还留着（schema 覆盖面 + 种子的校验口径），但都被标成「归群管」。"""
        by_key = {f["key"]: f for f in rules.CONFIG_SCHEMA}
        for key in (*GROUP_APPROVAL_SEED_KEYS, *GROUP_PUSH_SEED_KEYS):
            spec = by_key[key]
            assert spec.get("group_managed"), key
            assert rules.is_group_managed(key) is True
        assert rules.is_group_managed("topics.min_gap_hours") is False

    def test_dead_config_keys_do_not_contain_seed_keys(self) -> None:
        """deadclean 是启动第一步：这些种子键不能进 _DEAD_CONFIG_KEYS（会丢旧值）。"""
        from CharTyr_MaiWork.maiwork import migrations

        assert set(migrations._DEAD_CONFIG_KEYS).isdisjoint(
            set(GROUP_APPROVAL_SEED_KEYS) | set(GROUP_PUSH_SEED_KEYS)
        )

    def test_seed_key_lists_agree_across_modules(self) -> None:
        """三处登记同一批键（config 的 seed-only 清单 / rules 的归群管表 / 迁移的两组），
        以后加一个忘记另一处，这里就红。"""
        from CharTyr_MaiWork.maiwork import config as config_mod

        seed = set(GROUP_APPROVAL_SEED_KEYS) | set(GROUP_PUSH_SEED_KEYS)
        assert seed == set(config_mod.SEED_ONLY_GLOBAL_KEYS)
        assert seed == set(rules.GROUP_MANAGED_KEYS)
        assert set(GROUP_APPROVAL_SEED_KEYS).isdisjoint(GROUP_PUSH_SEED_KEYS)


def test_missing_config_file_is_not_a_problem_after_seeding(env: Env) -> None:
    """插件目录里没有 config.toml：没有旧键可删，种好每群记录就算做完（不让种子门永远关着）。"""
    (env.plug / "config.toml").unlink()
    out = _run(env)
    assert out["problem"] == ""
    assert out["deleted"] == []
    assert isinstance(env.store.kv_get(APPROVAL_KV + G1), dict)
    assert isinstance(env.store.kv_get(PUSH_KV + G1), dict)
    assert not (env.plug / "config.toml").exists()
