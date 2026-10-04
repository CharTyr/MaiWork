"""群控归一：旧覆盖**已物化进 config.toml**、但紧接着的**有效重读失败**（真 App + 真 HTTP + 真 Store）。

背景（0.8.0 收尾复审后剩下的同类第二个空洞）：

`_start_stack` 里「旧配置覆盖层物化失败」（写文件就抛）时确实会跳过
`migrate_group_controls`（`legacy_ok=False`）——`test_group_seed_pending_runtime.py` 锁住了那条。
但还有一条**更隐蔽**的路：物化**写文件成功**（`migrate_db_config_to_file` 返回非空、DB 旧键
已按「写完才清」清掉），紧跟的 `_reload_settings_from_file()` 却失败（`None` / 读文件
`OSError`）。此时 `eff_read_ok=False`，但旧代码的判断只看 `legacy_ok`：

    if not legacy_ok: 跳过
    else:             migrate_group_controls(store, settings, …)   # settings 还是 bare 文件值

于是 `migrate_group_controls` 拿**没有旧覆盖的 bare 文件值**当种子父播种每群 canonical，
而 `_seed_ready_settings()` 在迁移里**主动把惰性种子门打开**——`GroupApprovals.get` /
`group_push.get_config` 照种。这份 canonical 一旦落库就永远盖住旧覆盖的真实约束
（要批 / cap=1 / topics=false 被换成不批 / cap=12 / topics=true）；「canonical 已有记录就照听」
还会让不批的 canonical 在没有管理员批准的情况下放行——**静默放宽安全约束**。

本文件锁死的契约（真 `MaiWorkApp` + 真 Store + 真 config.toml + 真控制台 HTTP 客户端；
失败**只在真实文件写成功之后**注入一次 / 本轮若干次 `Path.read_text` 的 `OSError`）：

1. 物化写文件成功、有效重读失败 → 这一轮**绝不**调 `migrate_group_controls`（调用 0 次），
   绝不拿 bare 值播每群种子、绝不删退役旧源；
2. `group_controls_seed_ready` 门保持关着：群页 GET、派活、后台循环读「缺失记录」
   一律安全默认、零落库；
3. 保源：已成功物化的 config.toml 就是那份「旧覆盖后的有效值」，一字不删；DB 里那份
   `kv["config.override"]` 因为物化**真成功过**已经被清（不假装能还原）；
4. after 最后一次重读（`moved` 后的那次）也失败时，门同样不开、live settings 仍是 bare，
   但**照样不播种**；
5. 下一次启动（重读正常）→ 从文件里的旧覆盖有效值种下正确的 canonical
   （要批 / cap=1 / topics=false），既有每群记录一字不改。

注入口径说明：`config_file.write_fields` 包一层——真写成功**且**写后的文件里已经带上旧覆盖的
管理员（`qq:10001`）时才「武装」读失败。之后只对 `plug/config.toml` 这一个路径的前 N 次
`Path.read_text` 抛 `OSError`（模拟磁盘 / 权限式的读失败），别的一律走真实现。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiohttp
import pytest
import tomlkit
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork import clock, config_file, group_push
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.group_approval import KV_PREFIX as APPROVAL_KV
from CharTyr_MaiWork.maiwork.migrations import KV_CONFIG_OVERRIDE
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"
G2 = "123456789"
ADMIN_PW = "总管理员密码-reload-fail-1234"

# bare 文件值：故意「放开」（复审里那份：required=false / cap=12 / topics=true）
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
    plug_dir.mkdir(parents=True, exist_ok=True)
    doc = tomlkit.document()
    for section, values in raw.items():
        if isinstance(values, dict):
            table = tomlkit.table()
            for k, v in values.items():
                table[k] = v
            doc[section] = table
    (plug_dir / "config.toml").write_text(tomlkit.dumps(doc), encoding="utf-8")


def _raw_from_file(plug_dir: Path) -> dict:
    """像线上重启那样：宿主交出来的 raw 就是 config.toml 当前内容（不是内存里那份旧的）。"""
    parsed = tomlkit.parse((plug_dir / "config.toml").read_text(encoding="utf-8"))
    return parsed.unwrap()


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


def _patch_write_arms_read_failure(
    monkeypatch: pytest.MonkeyPatch, plug_dir: Path, *, fail_times: int
) -> dict:
    """只注入「真实文件写成功之后的 config.toml 读失败」；别的一律走真实现。

    返回状态（armed / left / failed），测试用它钉死注入真的生效过。
    """
    real_write = config_file.write_fields
    real_read = Path.read_text
    cfg = (plug_dir / "config.toml").resolve()
    state = {"armed": False, "left": 0, "failed": 0}

    def write_then_arm(plugin_dir: Any, data_dir: Any, flat: dict) -> str:
        text = real_write(plugin_dir, data_dir, flat)
        # 真写成功、而且写后的文件已经带上旧覆盖的有效值（网页那份管理员）→ 从这里开始让读挂掉
        if not state["armed"] and "10001" in text:
            state["armed"] = True
            state["left"] = fail_times
        return text

    def flaky_read(self: Path, *a: Any, **kw: Any) -> str:
        try:
            same = Path(self).resolve() == cfg
        except Exception:
            same = False
        if same and state["armed"] and state["left"] > 0:
            state["left"] -= 1
            state["failed"] += 1
            raise OSError(5, "Input/output error（测试模拟旧覆盖物化后 config.toml 读不了）")
        return real_read(self, *a, **kw)

    monkeypatch.setattr(config_file, "write_fields", write_then_arm)
    monkeypatch.setattr(Path, "read_text", flaky_read)
    return state


def _spy_controls(monkeypatch: pytest.MonkeyPatch) -> dict:
    """记 `migrate_group_controls` 调用次数（照调真实现，好让红测能真看见坏 canonical）。"""
    from CharTyr_MaiWork.maiwork import migrations as mig

    real = mig.migrate_group_controls
    calls = {"n": 0}

    def spy(*a: Any, **kw: Any) -> dict:
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(mig, "migrate_group_controls", spy)
    return calls


class Env:
    def __init__(
        self, app: MaiWorkApp, client: TestClient, plug: Path, data: Path, controls: dict
    ) -> None:
        self.app = app
        self.client = client
        self.plug = plug
        self.data = data
        self.controls = controls

    def text(self) -> str:
        return (self.plug / "config.toml").read_text(encoding="utf-8")

    def kv(self, key: str) -> Any:
        return self.app.store.kv_get(key)


async def _start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_times: int,
    raw_from_file: bool = False,
) -> tuple[Env, dict]:
    data_dir = tmp_path / "data"
    plug = tmp_path / "plug"
    port = _free_port()
    if raw_from_file:
        # 重启：raw = 文件当前内容（线上宿主的行为），只把监听端口 / 数据目录钉回测试的
        raw = _raw_from_file(plug)
        raw["console"] = {**(raw.get("console") or {}), "listen": f"127.0.0.1:{port}"}
        raw["storage"] = {**(raw.get("storage") or {}), "data_dir": str(data_dir)}
    else:
        raw = _raw_config(data_dir, port)
        _write_plugin_dir(plug, raw)
        _precreate_override(data_dir)
    state = {"armed": False, "left": 0, "failed": 0}
    if fail_times:
        state = _patch_write_arms_read_failure(monkeypatch, plug, fail_times=fail_times)
    controls = _spy_controls(monkeypatch)
    app = MaiWorkApp(FakeCtx({"config.get": "987654321"}), raw, plugin_dir=plug)
    app.profiles_cls = FakeProfiles
    await app.start()
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    return Env(app, client, plug, data_dir, controls), state


async def _close(env: Env) -> None:
    await env.client.close()
    await env.app.stop()


# ======================================================================
# 1. 有效重读失败 → 门关着、零播种、零删源（安全默认照旧）
# ======================================================================


class TestEffectiveReloadFailureDoesNotSeed:
    @pytest.mark.asyncio
    async def test_single_failed_reread_does_not_seed_or_clean_sources(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """物化写文件成功、紧接着第一次有效重读 OSError → 本轮不调迁移、不播种。"""
        env, state = await _start(tmp_path, monkeypatch, fail_times=1)
        app = env.app
        try:
            # 注入真的生效：文件写成功之后有一次 config.toml 读被注入失败
            assert state["armed"] is True, "读失败注入没武装（物化没写成功？）"
            assert state["failed"] == 1, state
            # 物化真写成功过、DB 旧源按「写完才清」清掉了（不假装能还原）
            assert env.kv(KV_CONFIG_OVERRIDE) is None

            # --- 0) 本轮绝不拿 bare 值调群控归一 ---
            assert env.controls["n"] == 0, "有效重读失败还调了 migrate_group_controls"
            assert app.get_settings().group_controls_seed_ready is False, "门不该开"

            # --- 1) 保源：文件里就是旧覆盖后的有效值（不是 bare 的放开值）---
            text = env.text()
            assert "10001" in text and "70007" not in text, text
            assert "required = true" in text, text
            assert "push_per_day = 1" in text, text
            assert "22:00-07:30" in text, text
            # 旧全局键一个没删（退役来源保留下次再迁）
            for kept in ("push_per_day", "quiet_hours", "admins", "exempt_users", "per_day", "speaker"):
                assert kept in text, kept

            # --- 2) 群页 GET 批准名单：安全默认，零落库 ---
            assert (await env.client.post("/api/login", json={"password": ADMIN_PW})).status == 200
            r = await env.client.get(f"/api/groups/{G1}/approval")
            assert r.status == 200, await r.text()
            body = await r.json()
            assert body["required"] is True, body       # 绝不放宽成 bare 的 false
            assert body["approvers"] == [] and body["exempt_users"] == []
            assert body["exempt_group"] is False
            assert env.kv(APPROVAL_KV + G1) is None, "读失败那轮绝不能被 seed 出 canonical"

            # --- 3) 群页 GET 往群里发：保守默认 ---
            r = await env.client.get(f"/api/groups/{G1}/push")
            assert r.status == 200, await r.text()
            cfg = (await r.json())["config"]
            assert cfg["topics_enabled"] is False
            assert cfg["news_card_enabled"] is False and cfg["idea_mention_enabled"] is False
            assert cfg["daily_max"] <= group_push.DEFAULT_DAILY_MAX, cfg   # 不放成 12
            assert env.kv(group_push.KV_PREFIX + G1) is None

            # --- 4) 收到派活：不判免批、不自动开工 ---
            app.approvals.set_review_hook(None)
            res = app.approvals.create(
                G1, kind="task", title="帮我看下这个", quote="群友派活",
                via="群友 @", requester_id="70007", requester_name="小明",
            )
            assert res["status"] == "pending" and res["auto"] is None
            assert app.approvals.is_admin("70007", group_id=G1) is False  # bare 全局管理员没混进本群
            assert env.kv(APPROVAL_KV + G1) is None

            # --- 5) 后台循环真读：开关关着、推送闸不放行、一轮不发东西 ---
            assert app._topics_effective_on(G1) is False
            ok, why = app.pushes.can_push(G1, "topic", _noon())
            assert ok is False and why == "开关已关", (ok, why)
            before = app.store.read().execute("SELECT COUNT(*) AS c FROM outbox").fetchone()["c"]
            await app.run_loop_once()
            after = app.store.read().execute("SELECT COUNT(*) AS c FROM outbox").fetchone()["c"]
            assert after == before == 0
            assert env.kv(group_push.KV_PREFIX + G1) is None
            assert env.kv(APPROVAL_KV + G1) is None

            # 本轮 live settings 已经因为「最后一次重读」成功而变成有效值，门照样不开——保守：
            # 只要这轮有过一次有效重读失败，整轮不播种，交给下一次启动。
            assert app.get_settings().approval.required is WEB_REQUIRED
            assert app.get_settings().group_controls_seed_ready is False
        finally:
            await _close(env)

    @pytest.mark.asyncio
    async def test_all_rereads_failed_in_round_gate_stays_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """本轮两次重读（物化后 + moved 后）都失败 → 门不开、live 仍是 bare，但零播种。"""
        env, state = await _start(tmp_path, monkeypatch, fail_times=2)
        app = env.app
        try:
            assert state["armed"] is True and state["failed"] == 2, state
            assert env.controls["n"] == 0, "有效重读失败还调了 migrate_group_controls"
            # 两次都没读成 → live settings 还是 bare 文件值（危险是真的）
            assert app.get_settings().approval.required is FILE_REQUIRED
            assert app.get_settings().delivery.push_per_day == FILE_PUSH_PER_DAY
            assert app.get_settings().group_controls_seed_ready is False

            # 但缺失记录一律安全默认、零落库
            await env.client.post("/api/login", json={"password": ADMIN_PW})
            body = await (await env.client.get(f"/api/groups/{G1}/approval")).json()
            assert body["required"] is True and body["approvers"] == []
            cfg = (await (await env.client.get(f"/api/groups/{G1}/push")).json())["config"]
            assert cfg["topics_enabled"] is False and cfg["daily_max"] <= group_push.DEFAULT_DAILY_MAX
            assert env.kv(APPROVAL_KV + G1) is None
            assert env.kv(group_push.KV_PREFIX + G1) is None
            assert app._topics_effective_on(G1) is False

            # 源留着（文件里的旧覆盖有效值）；DB 那份旧覆盖已因物化成功而清掉
            text = env.text()
            assert "10001" in text and "required = true" in text and "push_per_day = 1" in text
            assert env.kv(KV_CONFIG_OVERRIDE) is None
        finally:
            await _close(env)


# ======================================================================
# 2. 下一次启动（重读正常）：按文件里的旧覆盖有效值种 canonical；既有记录不动
# ======================================================================


class TestNextStartSeedsFromMaterializedFile:
    @pytest.mark.asyncio
    async def test_next_start_restores_effective_constraints(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env, _state = await _start(tmp_path, monkeypatch, fail_times=1)
        try:
            # 读失败那轮先塞一份「已经正确」的每群记录：修好后绝不能被覆盖
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
            assert env.controls["n"] == 0
        finally:
            await _close(env)

        # 下一次启动：读正常（宿主交出来的 raw 就是文件当前内容，像线上重启）
        env2, state2 = await _start(
            tmp_path, monkeypatch, fail_times=0, raw_from_file=True
        )
        app2 = env2.app
        try:
            assert state2["failed"] == 0
            assert env2.controls["n"] == 1, "下一次启动该真跑一次群控归一"
            assert app2.get_settings().group_controls_seed_ready is True, "重读正常、迁移成功后门要开"

            # 每群那份吃的是「旧覆盖后的有效值」，不是 bare 的放开值
            approval = env2.kv(APPROVAL_KV + G1)
            assert isinstance(approval, dict), approval
            assert approval["required"] is WEB_REQUIRED            # true，不是 false
            assert approval["approvers"] == list(WEB_ADMINS)
            assert approval["exempt_users"] == [] and approval["exempt_group"] is False
            push = env2.kv(group_push.KV_PREFIX + G1)
            assert isinstance(push, dict), push
            assert push["topics_enabled"] is WEB_TOPICS_ENABLED   # false，不是 true
            assert push["daily_max"] == WEB_PUSH_PER_DAY          # 1，不是 12（旧额度约束回来了）
            assert push["quiet_hours"] == WEB_QUIET

            # 运行口按 canonical 走
            assert app2._topics_effective_on(G1) is False
            ok, why = app2.pushes.can_push(G1, "topic", _noon())
            assert ok is False and why == "开关已关", (ok, why)

            # 既有正确记录一字不改
            assert env2.kv(APPROVAL_KV + G2) == g2_approval_before
            assert env2.kv(group_push.KV_PREFIX + G2) == g2_push_before

            # 这次真种好了才清源：登记的旧全局键从文件里删掉
            text = env2.text()
            for gone in ("push_per_day", "quiet_hours", "admins", "exempt_users", "per_day", "speaker"):
                assert gone not in text, gone
        finally:
            await _close(env2)
