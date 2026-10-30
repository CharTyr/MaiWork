"""旧网页覆盖层与 0.8.0 群控种子的先后顺序（真 App.start，核心迁移不 mock）。

背景（0.8.0 收尾复审发现的两个真问题）：

1. `_start_stack` 先 `migrate_group_controls`（用**文件里那份** settings 种每群一份、
   清掉 9 个全局旧键）才 `migrate_rules_override_to_file` / `migrate_db_config_to_file`。
   可 0.8.0 之前「有效设置」是 `config.toml` 先被 `kv["rules.override"]` 盖、
   再被 `kv["config.override"]`（+ 网页存的 secrets / `kv["models.settings"]`）盖
   （旧代码 app._effective_settings：先 `rules.effective_settings` 再
   `rules.apply_config_override`，所以两者撞同一个键时 config.override 赢）。
   于是「文件里 cap=3、旧网页覆盖 cap=5」时，种进每群那份的却是 3——
   旧网页覆盖被清掉后，管理员在网页上设的值就永久丢了。
2. `moved = bool(moved) or bool(migrate_db_config_to_file(...))` 是短路：
   group_controls 只要清过一个键（moved=True），后面那个调用**整个不执行**——
   `config.override` / `model_api_key` / `jev_api_key` / `console_password` /
   `models.settings` 永远迁不过来，旧源一直留在库里。

还有一处顺序隐患：旧 `config.override` 里可能带 `[search]` / `feeds.blocked_domains`。
要**先**把旧覆盖物化进文件，再退役这两个来源；否则（旧顺序）`[search]`、屏蔽名单会在
来源清理后被重新写回文件，成为第二份死配置。

本文件锁死的契约（全部用真 `MaiWorkApp.start()` + 真 Store + 真 config.toml，
只给 `profiles_cls` / 扩展 transport 这类注入点，不替掉迁移函数）：

- 一次启动：先用「旧覆盖后的有效值」种每群一份（config.override > rules.override > 文件），
  再清 9 个旧全局键；旧 KV / secrets / models.settings 一个不剩；
- moved=True **不会**吞掉后面任何一次迁移（每个迁移函数都被显式调用，且次数正好一次）；
- 已经存在的每群记录绝不被 legacy 覆写；非服务群零读取零写入；
- 物化失败：不拿 bare 文件播错种子、不删任何旧源、不挡启动；
- 第二次启动幂等：不重种、不重写文件、每群那份不动。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork import migrations as mig
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.config_file import ConfigFileError
from CharTyr_MaiWork.maiwork.group_approval import KV_PREFIX as APPROVAL_KV
from CharTyr_MaiWork.maiwork.group_push import KV_PREFIX as PUSH_KV
from CharTyr_MaiWork.maiwork.migrations import (
    GROUP_APPROVAL_SEED_KEYS,
    GROUP_PUSH_SEED_KEYS,
    KV_CONFIG_OVERRIDE,
    KV_MODELS_SETTINGS,
    KV_RULES_OVERRIDE,
)
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"
G2 = "123456789"
G3 = "555444333"  # 非服务群：零读取零写入

# ----------------------------------------------------------------------
# 假密钥 / 旧值（故意不用 sk- 前缀，证明靠真值搬，不靠正则）
# ----------------------------------------------------------------------
EXA_KEY = "legacy-exa-key-9527"
MODEL_KEY = "legacy-model-key-3141"
JEV_LEGACY = "legacy-jev-key-2718"
CONSOLE_LEGACY = "旧网页总密码-seedorder"
CONSOLE_FILE = "文件里的总密码-seedorder"
SEARCH_LEGACY = "legacy-search-key-1618"      # secrets.search_api_key：只该清掉、不该写文件
LEGACY_MAIN = "legacy-main-model"
LEGACY_WORKER = "legacy-worker-model"

ADMIN_FILE = "qq:10001"
ADMIN_LEGACY = "qq:10002"
EXEMPT_FILE = "qq:20002"

BLOCKED_DOMAIN = "blocked.example"

# 文件里的原值（bare）：cap=3 / required=true / topics.enabled=false / quiet 23:00-08:00
FILE_PUSH_PER_DAY = 3
FILE_QUIET = "23:00-08:00"
# 旧 rules.override：cap=4（比文件强，比 config.override 弱）
RULES_PUSH_PER_DAY = 4
# 旧 config.override：cap=5 / required=false / topics.enabled=true / quiet 22:00-07:30
WEB_PUSH_PER_DAY = 5
WEB_QUIET = "22:00-07:30"


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _raw_config(data_dir: Path) -> dict:
    """bare 文件配置（还没被旧网页覆盖盖过）：全局三套都写着非默认 / 和覆盖不同的值。"""
    return {
        "plugin": {"enabled": True},
        "groups": {
            "serve": [
                {"group": f"qq:{G1}", "workspace": "tinker"},
                {"group": f"qq:{G2}"},
            ]
        },
        "console": {
            "listen": f"127.0.0.1:{_free_port()}",
            "password": CONSOLE_FILE,
            "public_url": "",
        },
        "storage": {"data_dir": str(data_dir)},
        # 旧 [models] 全空（线上形态）：真值在 kv["models.settings"] + secrets.model_api_key
        "models": {
            "base_url": "",
            "api_key": "",
            "main": "",
            "main_backup": "",
            "worker": "",
            "worker_backup": "",
        },
        "jev": {"api_key": ""},
        "approval": {
            "required": True,
            "admins": [ADMIN_FILE],
            "exempt_groups": [],
            "exempt_users": [EXEMPT_FILE],
            "remind": True,
            "auto_review": True,
        },
        "topics": {"enabled": False, "speaker": "maiwork", "per_day": 2, "min_gap_hours": 3},
        "delivery": {
            "push_per_day": FILE_PUSH_PER_DAY,
            "quiet_hours": FILE_QUIET,
            # 死键：退役来源，只该在「旧覆盖都物化成功」之后才删
            "mention_ttl_minutes": 30,
        },
    }


def _write_plugin_dir(plug_dir: Path, raw: dict) -> None:
    import tomlkit

    plug_dir.mkdir(parents=True, exist_ok=True)
    doc = tomlkit.document()
    for section, values in raw.items():
        if isinstance(values, dict):
            table = tomlkit.table()
            for k, v in values.items():
                table[k] = v
            doc[section] = table
        else:
            doc[section] = values
    (plug_dir / "config.toml").write_text(tomlkit.dumps(doc), encoding="utf-8")


def _file_doc(plug_dir: Path) -> Any:
    import tomlkit

    return tomlkit.parse((plug_dir / "config.toml").read_text(encoding="utf-8"))


def _file_text(plug_dir: Path) -> str:
    return (plug_dir / "config.toml").read_text(encoding="utf-8")


def _precreate_db(data_dir: Path, *, per_group: bool = True) -> None:
    """按真实升级形态预造库（user_version >= 31）：旧网页覆盖 + secrets + models.settings。

    `per_group=True` 时顺带塞一份**已经存在**的每群记录（G2 自定义）和一份旧按群名单键，
    用来证明「legacy 绝不覆写既有每群记录」「旧按群名单被并进新记录后删键」。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(data_dir / "maiwork.db")
    try:
        assert store.migrate() >= 31
        with store.tx() as conn:
            store.kv_set(
                conn,
                KV_RULES_OVERRIDE,
                {"delivery": {"push_per_day": RULES_PUSH_PER_DAY}},
            )
            store.kv_set(
                conn,
                KV_CONFIG_OVERRIDE,
                {
                    "delivery": {"push_per_day": WEB_PUSH_PER_DAY, "quiet_hours": WEB_QUIET},
                    "approval": {"required": False},
                    "topics": {"enabled": True},
                    "feeds": {"blocked_domains": [BLOCKED_DOMAIN]},
                    "search": {"provider": "exa", "api_key": EXA_KEY},
                },
            )
            store.kv_set(
                conn,
                KV_MODELS_SETTINGS,
                {
                    "base_url": "https://legacy-web.test/v1",
                    "main": LEGACY_MAIN,
                    "main_backup": "",
                    "worker": LEGACY_WORKER,
                    "worker_backup": "",
                    "retries": 3,
                    "retry_delay_s": 9,
                    "checked_at": 1_789_999_000.0,
                    "available": [LEGACY_MAIN],
                },
            )
            store.secret_set(conn, "model_api_key", MODEL_KEY)
            store.secret_set(conn, "jev_api_key", JEV_LEGACY)
            store.secret_set(conn, "console_password", CONSOLE_LEGACY)
            store.secret_set(conn, "search_api_key", SEARCH_LEGACY)
            store.secret_set(conn, "admin_password_hash", "sha256$old$hash")
            if per_group:
                store.kv_set(conn, "group_admins." + G1, [ADMIN_LEGACY])
                store.kv_set(
                    conn,
                    PUSH_KV + G2,
                    {
                        "topics_enabled": False,
                        "news_card_enabled": False,
                        "news_card_count": 1,
                        "idea_mention_enabled": False,
                        "daily_max": 1,
                        "quiet_hours": "01:00-02:00",
                        "news_card_since": 0.0,
                        "idea_mention_since": 0.0,
                    },
                )
                store.kv_set(
                    conn,
                    APPROVAL_KV + G2,
                    {
                        "approvers": ["qq:g2-custom"],
                        "exempt_users": [],
                        "exempt_group": True,
                        "required": True,
                    },
                )
    finally:
        store.close()


def _no_net_transport() -> httpx.MockTransport:
    """[search] 迁出来的 MCP 扩展：本地 mock 回 500，绝不出网（AGENTS 红线）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="no-network-in-test")

    return httpx.MockTransport(handler)


class Env:
    def __init__(self, app: MaiWorkApp, plug_dir: Path, data_dir: Path) -> None:
        self.app = app
        self.plug_dir = plug_dir
        self.data_dir = data_dir

    def push(self, gid: str) -> Any:
        return self.app.store.kv_get(PUSH_KV + gid)

    def approval(self, gid: str) -> Any:
        return self.app.store.kv_get(APPROVAL_KV + gid)

    def doc(self) -> Any:
        return _file_doc(self.plug_dir)

    def text(self) -> str:
        return _file_text(self.plug_dir)


async def _start(tmp_path: Path, *, per_group: bool = True, raw: dict | None = None) -> Env:
    data_dir = tmp_path / "data"
    plug_dir = tmp_path / "plug"
    raw = raw if raw is not None else _raw_config(data_dir)
    _write_plugin_dir(plug_dir, raw)
    _precreate_db(data_dir, per_group=per_group)
    app = MaiWorkApp(FakeCtx({"config.get": "987654321"}), raw, plugin_dir=plug_dir)
    app.profiles_cls = FakeProfiles
    app.extensions_transport = _no_net_transport()
    await app.start()
    assert app.started is True
    return Env(app, plug_dir, data_dir)


async def _restart(env: Env) -> Env:
    """第二次启动：像宿主重载那样，从文件重新给一份 raw_config。"""
    import tomlkit

    await env.app.stop()
    raw = dict(tomlkit.parse(_file_text(env.plug_dir)))
    app = MaiWorkApp(FakeCtx({"config.get": "987654321"}), raw, plugin_dir=env.plug_dir)
    app.profiles_cls = FakeProfiles
    app.extensions_transport = _no_net_transport()
    await app.start()
    assert app.started is True
    return Env(app, env.plug_dir, env.data_dir)


# ======================================================================
# 1. 一次启动：先物化旧覆盖 → 用有效值种每群一份 → 再退役旧源
# ======================================================================


class TestOneBootOrder:
    @pytest.mark.asyncio
    async def test_effective_values_seeded_then_sources_retired(self, tmp_path: Path) -> None:
        env = await _start(tmp_path)
        try:
            # --- 每群那份吃的是「旧覆盖后的有效值」，不是文件里那行 ---
            g1_push = env.push(G1)
            g2_push = env.push(G2)
            assert isinstance(g1_push, dict) and isinstance(g2_push, dict)
            # 文件 cap=3、rules cap=4、config.override cap=5 → 有效 5（config.override 赢）
            assert g1_push["daily_max"] == WEB_PUSH_PER_DAY, g1_push
            # 文件 enabled=false、config.override enabled=true → 有效 true
            assert g1_push["topics_enabled"] is True, g1_push
            # 文件 quiet=23:00-08:00、config.override quiet=22:00-07:30 → 有效后者
            assert g1_push["quiet_hours"] == WEB_QUIET, g1_push

            g1_approval = env.approval(G1)
            assert isinstance(g1_approval, dict)
            # 文件 required=true、config.override required=false → 有效 false
            assert g1_approval["required"] is False, g1_approval
            assert ADMIN_FILE in g1_approval["approvers"]
            assert ADMIN_LEGACY in g1_approval["approvers"]   # 旧按群名单也吃进来
            assert g1_approval["exempt_users"] == [EXEMPT_FILE]

            # --- 9 个旧全局键一个不剩 ---
            doc = env.doc()
            for full_key in (*GROUP_APPROVAL_SEED_KEYS, *GROUP_PUSH_SEED_KEYS):
                section, _, field = full_key.partition(".")
                sec = doc.get(section)
                assert not (isinstance(sec, dict) and field in sec), f"{full_key} 没删干净"

            # --- 旧 KV 源一个不剩 ---
            assert env.app.store.kv_get(KV_CONFIG_OVERRIDE) is None
            assert env.app.store.kv_get(KV_RULES_OVERRIDE) is None
            assert env.app.store.kv_get(KV_MODELS_SETTINGS) is None
            assert env.app.store.kv_get("group_admins." + G1) is None
            for name in ("model_api_key", "jev_api_key", "console_password", "search_api_key",
                         "admin_password_hash"):
                assert env.app.store.secret_get(name) == "", name

            # --- moved=True 没吞掉 secrets / models.settings 迁移 ---
            s = env.app.settings
            assert s.console.password == CONSOLE_LEGACY          # DB secret 盖过文件
            assert s.jev.api_key == JEV_LEGACY                   # 同上
            assert s.endpoints and s.endpoints[0].api_key == MODEL_KEY
            assert s.endpoints[0].base_url == "https://legacy-web.test/v1"
            assert {m.model for m in s.model_list} == {LEGACY_MAIN, LEGACY_WORKER}
            # 旧 [models] 整节退役，且密钥只落在 [[endpoints]] 里
            assert "[models]" not in env.text()
            assert MODEL_KEY in env.text() and SEARCH_LEGACY not in env.text()

            # --- config.override 里的 [search] / feeds.blocked_domains：
            #     先物化、再被各自迁移消费，绝不在源清理后留在 / 回到文件里 ---
            assert doc.get("search") is None
            assert SEARCH_LEGACY not in env.text()
            feeds_sec = doc.get("feeds")
            assert not (isinstance(feeds_sec, dict) and "blocked_domains" in feeds_sec)
            assert env.app.store.kv_get("feeds.blocked_domains") is None
            # 扩展 + 绑定真的建出来了（不是「没有所以当然不残留」的假绿）
            from CharTyr_MaiWork.maiwork import extensions_web, search_binding

            names = {str(e.get("name") or "") for e in extensions_web._web_raw_entries(env.app.store)}
            assert "exa" in names, names
            binding = search_binding.get_binding(env.app.store)
            assert isinstance(binding, dict) and binding.get("mcp") == "exa", binding

            # --- 死键清理在群控之后跑（旧覆盖全物化完才退役） ---
            delivery = doc.get("delivery")
            assert not (isinstance(delivery, dict) and "mention_ttl_minutes" in delivery)

            # --- moved=True → 本进程按新文件重读（旧键在 _raw_config 里也没了） ---
            raw = env.app._raw_config
            assert not (raw.get("delivery") or {})
            assert "required" not in (raw.get("approval") or {})
            assert "enabled" not in (raw.get("topics") or {})
        finally:
            await env.app.stop()

    @pytest.mark.asyncio
    async def test_existing_per_group_records_survive_and_out_of_group_untouched(self, tmp_path: Path) -> None:
        env = await _start(tmp_path)
        try:
            # G2 事先就有一份自定义记录：legacy 只能种「没有的」，绝不能覆盖它
            g2_push = env.push(G2)
            assert g2_push is not None
            assert g2_push["daily_max"] == 1, g2_push
            assert g2_push["quiet_hours"] == "01:00-02:00", g2_push
            g2_approval = env.approval(G2)
            assert g2_approval is not None
            assert g2_approval["approvers"] == ["qq:g2-custom"], g2_approval
            assert g2_approval["exempt_group"] is True, g2_approval

            # 非服务群：一个字节都不许写（含屏蔽名单）
            assert env.app.store.kv_get(PUSH_KV + G3) is None
            assert env.app.store.kv_get(APPROVAL_KV + G3) is None
            assert env.app.store.kv_get(f"feeds.blocked.{G3}") is None
            written = {
                str(r["key"])
                for r in env.app.store.read().execute("SELECT key FROM kv").fetchall()
            }
            assert not any(k.endswith("." + G3) or k == "feeds.blocked." + G3 for k in written), written
        finally:
            await env.app.stop()

    @pytest.mark.asyncio
    async def test_second_boot_is_idempotent_and_does_not_overseed(self, tmp_path: Path) -> None:
        env = await _start(tmp_path)
        try:
            before_text = env.text()
            before = {k: env.app.store.kv_get(k) for k in (
                PUSH_KV + G1, PUSH_KV + G2, APPROVAL_KV + G1, APPROVAL_KV + G2,
                "feeds.blocked." + G1,
            )}
        finally:
            await env.app.stop()

        env2 = await _restart(env)
        try:
            assert env2.text() == before_text, "第二次启动重写了 config.toml"
            after = {k: env2.app.store.kv_get(k) for k in before}
            assert after == before, "第二次启动把每群那份重新种了一遍"
            # 源早清干净了，第二次什么也不该再动
            assert env2.app.store.kv_get(KV_CONFIG_OVERRIDE) is None
            assert env2.app.store.kv_get(KV_RULES_OVERRIDE) is None
            assert env2.app.store.kv_get(KV_MODELS_SETTINGS) is None
        finally:
            await env2.app.stop()

    @pytest.mark.asyncio
    async def test_every_migration_is_called_explicitly_without_short_circuit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """每个迁移函数都被显式调用恰好一次；顺序：规则 → 配置覆盖 → 群控 → 死键。"""
        order: list[str] = []
        names = (
            "migrate_rules_override_to_file",
            "migrate_db_config_to_file",
            "migrate_group_controls",
            "migrate_dead_config_keys",
            "migrate_search_config_to_extension",
            "migrate_blocked_domains_to_groups",
            "migrate_goal_proposal_kv",
            "migrate_models_config_to_endpoints",
        )
        for name in names:
            real = getattr(mig, name)

            def spy(*a, _name=name, _real=real, **kw):
                order.append(_name)
                return _real(*a, **kw)

            monkeypatch.setattr(mig, name, spy)

        env = await _start(tmp_path)
        try:
            for name in names:
                assert order.count(name) == 1, f"{name} 调用 {order.count(name)} 次：{order}"
            assert order.index("migrate_rules_override_to_file") < order.index("migrate_db_config_to_file")
            assert order.index("migrate_db_config_to_file") < order.index("migrate_group_controls")
            assert order.index("migrate_group_controls") < order.index("migrate_dead_config_keys")
            assert order.index("migrate_dead_config_keys") < order.index("migrate_search_config_to_extension")
            assert order.index("migrate_search_config_to_extension") < order.index("migrate_blocked_domains_to_groups")
            assert order.index("migrate_blocked_domains_to_groups") < order.index("migrate_models_config_to_endpoints")
        finally:
            await env.app.stop()


# ======================================================================
# 2. 物化失败：不播错种子、不删源、不挡启动
# ======================================================================


class TestMaterializeFailure:
    @pytest.mark.asyncio
    async def test_failure_keeps_source_and_does_not_seed_from_bare_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called: list[str] = []
        controls = mig.migrate_group_controls

        def spy_controls(*a, **kw):
            called.append("group_controls")
            return controls(*a, **kw)

        def boom(*a, **kw):
            raise ConfigFileError("磁盘满了（测试模拟写文件失败）")

        monkeypatch.setattr(mig, "migrate_db_config_to_file", boom)
        monkeypatch.setattr(mig, "migrate_group_controls", spy_controls)

        env = await _start(tmp_path)
        try:
            assert env.app.started is True          # 不挡启动
            assert called == []                     # 没物化成功 → 不种每群那份
            assert env.app.store.kv_get(KV_CONFIG_OVERRIDE) is not None  # 源原样保留
            assert env.app.store.secret_get("model_api_key") == MODEL_KEY
            assert env.push(G1) is None and env.approval(G1) is None
            assert env.push(G2) is not None          # 既有记录不动
            # 文件一字未动：9 个旧键、死键都还在（不能拿 bare 文件播错种子后删源）
            doc = env.doc()
            for full_key in (*GROUP_APPROVAL_SEED_KEYS, *GROUP_PUSH_SEED_KEYS):
                section, _, field = full_key.partition(".")
                sec = doc.get(section)
                assert isinstance(sec, dict) and field in sec, f"{full_key} 被提前删了"
            assert doc["delivery"]["mention_ttl_minutes"] == 30
            # config.override（强）整份都没落进文件：cap 不是 5、required 还是 true、
            # topics.enabled 还是 false（rules.override 若已搬成功，文件是它那份 4，也算
            # 「没拿 bare 文件播错种子」——真正的有效值 5 还留在 kv 里下次再迁）
            assert doc["delivery"]["push_per_day"] != WEB_PUSH_PER_DAY
            assert doc["approval"]["required"] is True
            assert doc["topics"]["enabled"] is False
        finally:
            await env.app.stop()

    @pytest.mark.asyncio
    async def test_previous_round_materialized_file_still_seeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """上一轮已经把旧覆盖物化进文件（kv 空了）→ 本轮物化「无事可做」不算失败，照常种。"""
        env = await _start(tmp_path)
        await env.app.stop()
        # 第二轮：源已清空；把 db_config 迁移直接换成一个必然报错的假实现
        monkeypatch.setattr(
            mig, "migrate_db_config_to_file",
            lambda *a, **kw: (_ for _ in ()).throw(ConfigFileError("本轮读不到文件")),
        )
        raw = _raw_config(env.data_dir)
        raw["delivery"]["push_per_day"] = WEB_PUSH_PER_DAY   # 文件保真值
        raw["delivery"]["quiet_hours"] = WEB_QUIET
        raw["topics"]["enabled"] = True
        raw["approval"]["required"] = False
        _write_plugin_dir(env.plug_dir, raw)
        app = MaiWorkApp(FakeCtx({}), raw, plugin_dir=env.plug_dir)
        app.profiles_cls = FakeProfiles
        app.extensions_transport = _no_net_transport()
        await app.start()
        try:
            # 每组记录已在第一轮种好 → 不被覆盖；关键是「本轮没有被误判成失败而挡住」
            assert app.started is True
            assert app.store.kv_get(PUSH_KV + G1) is not None
        finally:
            await app.stop()


# ======================================================================
# 3. 优先级（旧真实读取顺序）：config.override > rules.override > 文件
# ======================================================================


class TestOverridePriorityOrder:
    def test_config_override_wins_over_rules_override_and_file(self, tmp_path: Path) -> None:
        """只调迁移（不经 app）：先物化 rules.override、再物化 config.override，
        同一个键最后落在文件里的是 config.override 的值（旧 get_settings 的真实顺序）。"""
        plug = tmp_path / "plug"
        _write_plugin_dir(plug, {
            "plugin": {"enabled": True},
            "delivery": {"push_per_day": FILE_PUSH_PER_DAY},
        })
        data = tmp_path / "data"
        store = Store(data / "m.db")
        try:
            store.migrate()
            with store.tx() as conn:
                store.kv_set(conn, KV_RULES_OVERRIDE, {"delivery": {"push_per_day": RULES_PUSH_PER_DAY}})
                store.kv_set(conn, KV_CONFIG_OVERRIDE, {"delivery": {"push_per_day": WEB_PUSH_PER_DAY}})
            assert mig.migrate_rules_override_to_file(store, plug, data) == ["delivery.push_per_day"]
            assert "push_per_day = 4" in _file_text(plug)
            assert mig.migrate_db_config_to_file(store, plug, data) == ["delivery.push_per_day"]
            text = _file_text(plug)
            assert "push_per_day = 5" in text and "push_per_day = 4" not in text, text
            assert store.kv_get(KV_RULES_OVERRIDE) is None
            assert store.kv_get(KV_CONFIG_OVERRIDE) is None
        finally:
            store.close()

    def test_file_nonempty_different_db_wins(self, tmp_path: Path) -> None:
        """migrations.py 文档第 10 行的口径：文件已有非空值且不同 → 以数据库为准。"""
        plug = tmp_path / "plug"
        _write_plugin_dir(plug, {"plugin": {"enabled": True}, "delivery": {"push_per_day": FILE_PUSH_PER_DAY}})
        data = tmp_path / "data"
        store = Store(data / "m.db")
        try:
            store.migrate()
            with store.tx() as conn:
                store.kv_set(conn, KV_CONFIG_OVERRIDE, {"delivery": {"push_per_day": WEB_PUSH_PER_DAY}})
            assert mig.migrate_db_config_to_file(store, plug, data) == ["delivery.push_per_day"]
            assert "push_per_day = 5" in _file_text(plug)
            assert store.kv_get(KV_CONFIG_OVERRIDE) is None
        finally:
            store.close()
