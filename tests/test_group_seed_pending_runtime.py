"""群控归一「种子未就绪」时的运行时读取（真 App.start + 真 HTTP + 真 Store）。

背景（0.8.0 收尾复审后的运行时空洞）：

`_start_stack` 里「旧网页覆盖层物化失败」时会正确地**跳过** `migrate_group_controls`
（不拿 bare 文件去播每群种子）。但 App 照样正常启动，`get_settings()` 返回的是**没有
被旧覆盖盖过的 bare 文件值**（或者只物化了一半的 file 值）。此时只要有人：

- 打开群页（`GET /api/groups/<gid>/approval` / `.../push`）；
- 群友派活（`Approvals.create` → 判免批）；
- 后台循环读每群开关（`app._topics_effective_on` / `pushes.can_push`）；

`GroupApprovals.get` / `group_push.get_config` 的惰性首次读就会「键不存在 → 从当前
settings 种一份 canonical」。这份 canonical 一旦落库就永久盖住后来修好的物化结果
（第二次启动看到记录已存在，不再种）——旧的 `required=true / cap=1 / topics_enabled=false`
约束**永久丢失**，方向还是放开的（bare 文件是 `required=false / cap=12 / topics=true`）。

本文件锁死的契约：

1. 旧覆盖物化失败 → `group_controls_seed_ready` 门是关的；群页 GET、派活、后台循环
   读缺失记录时**返回安全默认、绝不 seed**（approval：要批 / 无批准人 / 无免批；
   push：主动开关全关、额度收敛有限、睡觉时段照配置）。
2. 既有**合法的** canonical 记录照旧可读、被尊重（门只挡「缺失就种」，不挡已有记录）。
3. 源（config.toml 旧全局键 + `kv["config.override"]`）一个不删、原样留着下次再迁。
4. 修好写文件后再启动：按**旧覆盖后的有效值**种下 canonical（required=true / cap=1 /
   topics_enabled=false），且原先已正确存在的每群记录**一字不改**。
5. 门只在「物化成功 + 有效重读成功 + 群控归一没报问题」后打开；普通 `update_config`
   热应用不许把它重新打开（只在真迁移成功后才开）。

用真 `MaiWorkApp` + 真 Store + 真 config.toml + 真控制台客户端；失败只注入
`config_file.write_fields`（模拟磁盘写失败），不替掉任何迁移函数。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork import clock, config_file, group_push
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.config_file import ConfigFileError
from CharTyr_MaiWork.maiwork.group_approval import KV_PREFIX as APPROVAL_KV
from CharTyr_MaiWork.maiwork.migrations import KV_CONFIG_OVERRIDE
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"
G2 = "123456789"
ADMIN_PW = "总管理员密码-seedpending-1234"

# bare 文件值：故意「放开」（就是复审里那份：required=false / cap=12 / topics=true）
FILE_REQUIRED = False
FILE_PUSH_PER_DAY = 12
FILE_TOPICS_ENABLED = True
FILE_ADMINS = ["qq:70007"]

# 旧的网页覆盖（kv["config.override"]）：真正有效的旧约束（required=true / cap=1 / topics=false）
WEB_REQUIRED = True
WEB_PUSH_PER_DAY = 1
WEB_TOPICS_ENABLED = False
WEB_ADMINS = ["qq:10001"]
WEB_QUIET = "22:00-07:30"


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _raw_config(data_dir: Path, port: int) -> dict:
    """bare 文件：全局三套都写着「放开」的值（旧覆盖还没物化进来）。"""
    return {
        "plugin": {"enabled": True},
        "groups": {
            "serve": [
                {"group": f"qq:{G1}", "workspace": "tinker"},
                {"group": f"qq:{G2}"},
            ]
        },
        "console": {"listen": f"127.0.0.1:{port}", "password": ADMIN_PW, "public_url": ""},
        "storage": {"data_dir": str(data_dir)},
        "models": {"base_url": "https://ep.test/v1", "api_key": "sk-x", "main": "m", "worker": "w"},
        "approval": {
            "required": FILE_REQUIRED,
            "admins": list(FILE_ADMINS),
            "exempt_users": list(FILE_ADMINS),
            "exempt_groups": [f"qq:{G1}"],
            "remind": True,
            "auto_review": True,
        },
        "topics": {"enabled": FILE_TOPICS_ENABLED, "speaker": "maiwork", "per_day": 2, "min_gap_hours": 3},
        "delivery": {"push_per_day": FILE_PUSH_PER_DAY, "quiet_hours": "23:00-08:00"},
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
    (plug_dir / "config.toml").write_text(tomlkit.dumps(doc), encoding="utf-8")


def _precreate_override(data_dir: Path) -> None:
    """预造真库：一份还没物化的旧网页覆盖层（强覆盖）。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(data_dir / "maiwork.db")
    try:
        store.migrate()
        with store.tx() as conn:
            store.kv_set(conn, KV_CONFIG_OVERRIDE, {
                "approval": {"required": WEB_REQUIRED, "admins": list(WEB_ADMINS),
                             "exempt_users": [], "exempt_groups": []},
                "topics": {"enabled": WEB_TOPICS_ENABLED},
                "delivery": {"push_per_day": WEB_PUSH_PER_DAY, "quiet_hours": WEB_QUIET},
            })
    finally:
        store.close()


def _noon() -> float:
    return clock.bj(clock.now()).replace(hour=12, minute=0, second=0, microsecond=0).timestamp()


def _patch_flaky_write(monkeypatch: pytest.MonkeyPatch, *, fail_first: bool) -> None:
    """只注入 config_file.write_fields：第一次抛（模拟物化写失败），之后照真实现。"""
    real = config_file.write_fields
    if not fail_first:
        monkeypatch.setattr(config_file, "write_fields", real)
        return
    state = {"n": 0}

    def flaky(plugin_dir: Any, data_dir: Any, flat: dict) -> str:
        state["n"] += 1
        if state["n"] == 1:
            raise ConfigFileError("磁盘满了（测试模拟旧覆盖物化写文件失败）")
        return real(plugin_dir, data_dir, flat)

    monkeypatch.setattr(config_file, "write_fields", flaky)


class Env:
    def __init__(self, app: MaiWorkApp, client: TestClient, plug: Path, data: Path) -> None:
        self.app = app
        self.client = client
        self.plug = plug
        self.data = data

    def text(self) -> str:
        return (self.plug / "config.toml").read_text(encoding="utf-8")

    def kv(self, key: str) -> Any:
        return self.app.store.kv_get(key)


async def _start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, fail_first_write: bool
) -> Env:
    data_dir = tmp_path / "data"
    plug = tmp_path / "plug"
    port = _free_port()
    raw = _raw_config(data_dir, port)
    _write_plugin_dir(plug, raw)
    _precreate_override(data_dir)
    _patch_flaky_write(monkeypatch, fail_first=fail_first_write)
    app = MaiWorkApp(FakeCtx({"config.get": "987654321"}), raw, plugin_dir=plug)
    app.profiles_cls = FakeProfiles
    await app.start()
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    return Env(app, client, plug, data_dir)


async def _close(env: Env) -> None:
    await env.client.close()
    await env.app.stop()


# ======================================================================
# 1. 物化失败 → 门关着：群页 / 派活 / 循环读都不 seed、不放宽、不发
# ======================================================================


class TestPendingReadsDoNotSeed:
    @pytest.mark.asyncio
    async def test_pending_reads_return_safe_defaults_and_keep_sources(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await _start(tmp_path, monkeypatch, fail_first_write=True)
        app = env.app
        try:
            # live settings 就是那份放开的 bare 文件（危险是真的）
            assert app.get_settings().approval.required is FILE_REQUIRED
            assert app.get_settings().delivery.push_per_day == FILE_PUSH_PER_DAY
            assert app.get_settings().topics.enabled is FILE_TOPICS_ENABLED

            # --- 1) 群页 GET 批准名单：安全默认，零落库 ---
            assert (await env.client.post("/api/login", json={"password": ADMIN_PW})).status == 200
            r = await env.client.get(f"/api/groups/{G1}/approval")
            assert r.status == 200, await r.text()
            body = await r.json()
            assert body["required"] is True, body        # 绝不放宽成 bare 的 false
            assert body["approvers"] == [] and body["exempt_users"] == []
            assert body["exempt_group"] is False
            assert env.kv(APPROVAL_KV + G1) is None, "缺失记录绝不能在待迁期被 seed"

            # --- 2) 群页 GET 往群里发：保守默认（开关全关、额度收敛有限）---
            r = await env.client.get(f"/api/groups/{G1}/push")
            assert r.status == 200, await r.text()
            cfg = (await r.json())["config"]
            assert cfg["topics_enabled"] is False
            assert cfg["news_card_enabled"] is False and cfg["idea_mention_enabled"] is False
            assert cfg["daily_max"] <= group_push.DEFAULT_DAILY_MAX, cfg   # 不放成 12
            assert env.kv(group_push.KV_PREFIX + G1) is None

            # --- 3) 收到派活：不判免批、不自动开工、不建 canonical ---
            app.approvals.set_review_hook(None)   # 关掉自动审核后台任务，只看免批判定
            res = app.approvals.create(
                G1, kind="task", title="帮我看下这个", quote="群友派活",
                via="群友 @", requester_id="70007", requester_name="小明",
            )
            assert res["status"] == "pending" and res["auto"] is None
            assert app.approvals.is_admin("70007", group_id=G1) is False  # bare 全局管理员没混进本群
            assert env.kv(APPROVAL_KV + G1) is None

            # --- 4) 后台循环真读：每群开关关着、推送闸不放行、一轮循环不发东西 ---
            noon = _noon()
            assert app._topics_effective_on(G1) is False
            ok, why = app.pushes.can_push(G1, "topic", noon)
            assert ok is False and why == "开关已关", (ok, why)
            before = app.store.read().execute("SELECT COUNT(*) AS c FROM outbox").fetchone()["c"]
            await app.run_loop_once()
            after = app.store.read().execute("SELECT COUNT(*) AS c FROM outbox").fetchone()["c"]
            assert after == before == 0
            assert env.kv(group_push.KV_PREFIX + G1) is None
            assert env.kv(APPROVAL_KV + G1) is None

            # --- 5) 保源：旧全局键 + kv 覆盖层一个不删；门仍是关的 ---
            assert app.get_settings().group_controls_seed_ready is False
            text = env.text()
            for kept in ("push_per_day", "quiet_hours", "admins", "exempt_users", "per_day", "speaker"):
                assert kept in text, kept
            assert app.store.kv_get(KV_CONFIG_OVERRIDE) is not None
        finally:
            await _close(env)

    @pytest.mark.asyncio
    async def test_existing_canonical_is_read_and_respected_while_pending(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """门关着也不挡「已有合法 canonical」：读它、听它，但不写它。"""
        env = await _start(tmp_path, monkeypatch, fail_first_write=True)
        app = env.app
        try:
            with app.store.tx() as conn:
                app.store.kv_set(conn, APPROVAL_KV + G1, {
                    "approvers": ["qq:9"], "exempt_users": [], "exempt_group": False, "required": False,
                })
                app.store.kv_set(conn, group_push.KV_PREFIX + G1, {
                    "topics_enabled": True, "news_card_enabled": False, "news_card_count": 3,
                    "idea_mention_enabled": False, "daily_max": 5, "quiet_hours": "23:00-08:00",
                    "news_card_since": 0.0, "idea_mention_since": 0.0,
                })
            await env.client.post("/api/login", json={"password": ADMIN_PW})
            body = await (await env.client.get(f"/api/groups/{G1}/approval")).json()
            assert body["required"] is False and body["approvers"] == ["qq:9"]
            cfg = (await (await env.client.get(f"/api/groups/{G1}/push")).json())["config"]
            assert cfg["topics_enabled"] is True and cfg["daily_max"] == 5
            assert app._topics_effective_on(G1) is True
            ok, why = app.pushes.can_push(G1, "topic", _noon())
            assert ok is True and why == "", (ok, why)
            # 一字未改动
            assert env.kv(APPROVAL_KV + G1)["approvers"] == ["qq:9"]
            assert env.kv(group_push.KV_PREFIX + G1)["daily_max"] == 5
        finally:
            await _close(env)

    @pytest.mark.asyncio
    async def test_partial_push_save_rejected_while_pending_full_approval_save_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """待迁期：局部改「往群里发」明确拒（中文说清），不会混进未知默认；
        但「谁能批」的**全量**保存是人的明确意图，照常保留。"""
        env = await _start(tmp_path, monkeypatch, fail_first_write=True)
        try:
            await env.client.post("/api/login", json={"password": ADMIN_PW})
            r = await env.client.put(f"/api/groups/{G1}/push", json={"daily_max": 5})
            assert r.status == 400, await r.text()
            err = (await r.json())["error"]
            assert "迁移" in err or "全局" in err, err
            assert env.kv(group_push.KV_PREFIX + G1) is None, "拒绝时一个字段都不许落库"

            r2 = await env.client.put(f"/api/groups/{G1}/approval", json={
                "approvers": ["qq:10001"], "exempt_users": [],
                "exempt_group": False, "required": True,
            })
            assert r2.status == 200, await r2.text()
            assert env.kv(APPROVAL_KV + G1)["approvers"] == ["qq:10001"]
        finally:
            await _close(env)


# ======================================================================
# 2. 修好写文件后再启动：按旧覆盖有效值种 canonical；既有记录不动
# ======================================================================


class TestFixedRestartSeedsCanonical:
    @pytest.mark.asyncio
    async def test_fixed_restart_uses_old_effective_and_keeps_existing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await _start(tmp_path, monkeypatch, fail_first_write=True)
        try:
            # 待迁期先塞一份「已经正确」的每群记录：修好后绝不能被覆盖
            with env.app.store.tx() as conn:
                env.app.store.kv_set(conn, APPROVAL_KV + G2, {
                    "approvers": ["qq:g2-custom"], "exempt_users": [],
                    "exempt_group": True, "required": True,
                })
                env.app.store.kv_set(conn, group_push.KV_PREFIX + G2, {
                    "topics_enabled": False, "news_card_enabled": False, "news_card_count": 1,
                    "idea_mention_enabled": False, "daily_max": 1, "quiet_hours": "01:00-02:00",
                    "news_card_since": 0.0, "idea_mention_since": 0.0,
                })
            g2_approval_before = env.kv(APPROVAL_KV + G2)
            g2_push_before = env.kv(group_push.KV_PREFIX + G2)
        finally:
            await _close(env)

        # 修好：写文件不再失败，重新启动（同一目录，旧覆盖还在 kv 里）
        env2 = await _start(tmp_path, monkeypatch, fail_first_write=False)
        app2 = env2.app
        try:
            assert app2.get_settings().group_controls_seed_ready is True, "修好后门要开"
            # 旧覆盖物化成功、源清掉
            assert env2.kv(KV_CONFIG_OVERRIDE) is None

            # 每群那份吃的是「旧覆盖后的有效值」，不是 file 里放开的 bare 值
            approval = env2.kv(APPROVAL_KV + G1)
            assert isinstance(approval, dict)
            assert approval["required"] is WEB_REQUIRED            # true，不是 false
            assert approval["approvers"] == list(WEB_ADMINS)
            assert approval["exempt_users"] == [] and approval["exempt_group"] is False
            push = env2.kv(group_push.KV_PREFIX + G1)
            assert isinstance(push, dict)
            assert push["topics_enabled"] is WEB_TOPICS_ENABLED   # false，不是 true
            assert push["daily_max"] == WEB_PUSH_PER_DAY          # 1，不是 12
            assert push["quiet_hours"] == WEB_QUIET

            # 运行口按 canonical 走
            assert app2._topics_effective_on(G1) is False
            ok, why = app2.pushes.can_push(G1, "topic", _noon())
            assert ok is False and why == "开关已关"

            # 既有正确记录一字不改
            assert env2.kv(APPROVAL_KV + G2) == g2_approval_before
            assert env2.kv(group_push.KV_PREFIX + G2) == g2_push_before
        finally:
            await _close(env2)


# ======================================================================
# 3. 门不许被普通配置热更新重新打开
# ======================================================================


class TestGateNotReopenedByHotApply:
    @pytest.mark.asyncio
    async def test_update_config_does_not_reopen_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await _start(tmp_path, monkeypatch, fail_first_write=True)
        app = env.app
        try:
            assert app.get_settings().group_controls_seed_ready is False
            raw = dict(app._raw_config)
            raw["approval"] = {**(raw.get("approval") or {}), "remind": False}  # 真改一个字段
            await app.update_config(raw)
            assert app.get_settings().group_controls_seed_ready is False, "热应用把门重新开了"
            # 门还关着 → 缺失记录仍然不种
            assert app.get_settings().approval.required is FILE_REQUIRED
            r = await env.client.post("/api/login", json={"password": ADMIN_PW})
            assert r.status == 200
            body = await (await env.client.get(f"/api/groups/{G1}/approval")).json()
            assert body["required"] is True
            assert env.kv(APPROVAL_KV + G1) is None
        finally:
            await _close(env)

    @pytest.mark.asyncio
    async def test_get_settings_is_stable_across_repeated_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """重复 get_settings 必须返回同一个对象（派生快照有缓存）。

        每次 dataclasses.replace 都会让 Models.settings() 的「按 Settings 对象钉住的缓存」
        失效、每读一次重算一次（客户端跟着重造）。这里强制走「工作区根不同 → 派生一次」
        的分支，钉住缓存合同。
        """
        env = await _start(tmp_path, monkeypatch, fail_first_write=True)
        app = env.app
        try:
            app._ws_root = env.data / "workspaces"       # 和配置里的根不同 → 会走 replace 分支
            first = app.get_settings()
            second = app.get_settings()
            assert first is second, "重复 get_settings 每次新 replace → Models 缓存会抖"
            assert first.group_controls_seed_ready is app._settings.group_controls_seed_ready
        finally:
            await _close(env)
