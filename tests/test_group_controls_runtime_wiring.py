"""0.8.0 群控后端最终跨模块接线（真 app 实例 + 真 config.toml + 真发件箱）。

锁死这几条（parent 拆给「后端接线」这一份）：

1. `app._start_stack` 在 deadclean **之前**调 `migrations.migrate_group_controls`；
   迁移真清了 config.toml 旧键（`deleted` 非空）→ `moved=True` → 本进程按新文件重读配置
   （`_raw_config` 里旧键也没了）。种每群那份用的是**旧文件里的原值**，不是代码默认：
   已服务群原来 `[topics] enabled=false` / `quiet_hours="22:00-07:30"` / `push_per_day=5`
   必须原样进 `kv["group_push.<群号>"]`。
2. 发件箱造好后，Topics / CardPush / IdeaMention 统一走各自的 `attach_outbox`（公开方法），
   hook 在 attach 内部登记，**只登记一次**；只有真发出去了才写 opener / 候选 used / pushes 账本。
3. `GET|PUT /api/groups/{gid}/push`：接 group_push 的 get / set / view；读 + 写都只给
   本群群管理员（总管理员随便），群友 403 / 匿名 401 / 非服务群 404；PUT 走同源守卫；
   退役字段明确 400（说清去群页），不静默假保存；返回
   `{config, daily_max, sent_today, quota_used, recent}`；整份响应不泄密钥 / 绝对路径。
   旧的 `/card-push` 别名留着，但和 `/push` 是**同一份数据**（不是第二处存储）。
4. 每群开关 / 睡觉时段只有一个来源（`group_push.<群号>`）：`app._topics_effective_on(gid)`
   不再被 `[topics] enabled`（全局种子）挡住；`scheduler._in_quiet`、`views.group_view` 的
   `pulse.sleep` 都按**当前群**那份算（不读别的群）。
5. 每个服务群巡检里挂 `Feeds.shelve_ignored_ideas(gid, now)`：7 天没人理的构想变「已收起」，
   **不发消息**；个人向 / 已开工 / 待批准的不动。

测试用真 `MaiWorkApp` + 真 Store + 真 config.toml + 真控制台客户端，不手工补私有线。
"""

from __future__ import annotations

import dataclasses
import inspect
import json
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork import clock, group_push
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.group_approval import KV_PREFIX as APPROVAL_KV

G1 = "900000001"
G2 = "123456789"
ADMIN_PW = "总管理员密码-群控接线-1234"
G1_PW = "群一管理员密码-pushwiring"
G2_PW = "群二管理员密码-pushwiring"
SECRET = "sk-push-leak-9527-不许外泄"
ABS_PATH = "/Users/nobody/private/workspaces/secret-dir"


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _raw_config(data_dir: Path) -> dict:
    """未清版旧配置：全局三套（批准 / 开话题 / 推送）都写着非默认值。"""
    return {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}, {"group": f"qq:{G2}"}]},
        "console": {"listen": f"127.0.0.1:{_free_port()}", "password": ADMIN_PW, "public_url": ""},
        "storage": {"data_dir": str(data_dir)},
        "models": {"base_url": "https://ep.test/v1", "api_key": SECRET, "main": "m", "worker": "w"},
        "approval": {
            "required": False,
            "admins": ["qq:10001"],
            "exempt_users": ["qq:20002"],
            "exempt_groups": [f"qq:{G1}"],
            "remind": True,
            "auto_review": True,
        },
        "topics": {"enabled": False, "speaker": "maiwork", "per_day": 2, "min_gap_hours": 3},
        "delivery": {"push_per_day": 5, "quiet_hours": "22:00-07:30"},
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


def _config_text(plug_dir: Path) -> str:
    return (plug_dir / "config.toml").read_text(encoding="utf-8")


def _noon() -> float:
    """今天北京时间 12:00：睡觉 / quiet 判定用的确定时刻（跟墙钟几点无关）。"""
    return clock.bj(clock.now()).replace(hour=12, minute=0, second=0, microsecond=0).timestamp()


class Env:
    def __init__(self, app: MaiWorkApp, client: TestClient, plug_dir: Path, data_dir: Path) -> None:
        self.app = app
        self.client = client
        self.plug_dir = plug_dir
        self.data_dir = data_dir

    async def login(self, password: str = ADMIN_PW):
        return await self.client.post("/api/login", json={"password": password})

    def set_group_password(self, gid: str, pw: str) -> None:
        self.app.group_admins.set_password(gid, pw)

    def group_headers(self) -> dict[str, str]:
        return {"X-MW-Group": self.app.token_of(G1)}

    def config_text(self) -> str:
        return _config_text(self.plug_dir)


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    data_dir = tmp_path / "data"
    plug_dir = tmp_path / "plug"
    raw = _raw_config(data_dir)
    _write_plugin_dir(plug_dir, raw)
    app = MaiWorkApp(FakeCtx({"config.get": "987654321"}), raw, plugin_dir=plug_dir)
    app.profiles_cls = FakeProfiles
    await app.start()
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    e = Env(app, client, plug_dir, data_dir)
    try:
        yield e
    finally:
        await client.close()
        await app.stop()


# ======================================================================
# 1. 启动迁移：先于 deadclean；清了源就重读配置；旧值原样进每群那份
# ======================================================================


class TestStartupMigrationWiring:
    @pytest.mark.asyncio
    async def test_group_controls_migration_runs_before_deadclean(self, tmp_path: Path, monkeypatch) -> None:
        """真的是「deadclean 之前」调，不是先后颠倒（顺序用调用记录钉死）。"""
        from CharTyr_MaiWork.maiwork import migrations as mig

        order: list[str] = []
        real_controls = mig.migrate_group_controls
        real_dead = mig.migrate_dead_config_keys

        def spy_controls(*a, **kw):
            order.append("group_controls")
            return real_controls(*a, **kw)

        def spy_dead(*a, **kw):
            order.append("deadclean")
            return real_dead(*a, **kw)

        monkeypatch.setattr(mig, "migrate_group_controls", spy_controls)
        monkeypatch.setattr(mig, "migrate_dead_config_keys", spy_dead)

        data_dir = tmp_path / "data"
        plug_dir = tmp_path / "plug"
        raw = _raw_config(data_dir)
        _write_plugin_dir(plug_dir, raw)
        app = MaiWorkApp(FakeCtx({}), raw, plugin_dir=plug_dir)
        app.profiles_cls = FakeProfiles
        await app.start()
        try:
            assert "group_controls" in order and "deadclean" in order
            assert order.index("group_controls") < order.index("deadclean")
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_old_values_seeded_into_per_group_source_then_file_cleaned(self, env: Env) -> None:
        """旧文件里的原值进每群那份；源被清掉；本进程按新文件重读。"""
        cfg = env.app.store.kv_get(f"{group_push.KV_PREFIX}{G1}")
        assert isinstance(cfg, dict)
        assert cfg["topics_enabled"] is False          # [topics] enabled=false 原样保
        assert cfg["daily_max"] == 5                   # [delivery] push_per_day=5 原样保
        assert cfg["quiet_hours"] == "22:00-07:30"     # 不回落代码默认 23:00-08:00
        approval = env.app.store.kv_get(APPROVAL_KV + G1)
        assert isinstance(approval, dict)
        assert approval["approvers"] == ["qq:10001"]   # 全局种子父
        assert approval["required"] is False
        assert approval["exempt_group"] is True
        # 源：登记的旧全局键从 config.toml 删掉，没登记的还留着
        text = env.config_text()
        for gone in ("push_per_day", "quiet_hours", "per_day", "speaker", "admins", "exempt_users"):
            assert gone not in text, gone
        assert "remind = true" in text and "min_gap_hours = 3" in text
        # deleted 非空 → moved=True → 本进程按新文件重读（旧键在 _raw_config 里也没了）
        raw = env.app._raw_config
        assert not (raw.get("delivery") or {})
        topics_raw = raw.get("topics") or {}
        assert "enabled" not in topics_raw and "per_day" not in topics_raw
        assert "speaker" not in topics_raw
        assert topics_raw.get("min_gap_hours") == 3   # 仍归全局的技术项留着
        approval_raw = raw.get("approval") or {}
        assert "admins" not in approval_raw and "required" not in approval_raw
        assert approval_raw.get("remind") is True

    @pytest.mark.asyncio
    async def test_seed_failure_keeps_file_and_source(self, tmp_path: Path, monkeypatch) -> None:
        """种每群那份失败（写库炸）→ 旧键一个不删、文件一字不动（seed 先成功才清源）。"""
        from CharTyr_MaiWork.maiwork import migrations as mig

        data_dir = tmp_path / "data"
        plug_dir = tmp_path / "plug"
        raw = _raw_config(data_dir)
        _write_plugin_dir(plug_dir, raw)
        real = mig.migrate_group_controls

        def failing(*a, **kw):
            store = a[0]
            original = store.kv_set

            def boom(*aa, **kk):
                raise RuntimeError("磁盘坏了")

            store.kv_set = boom
            try:
                return real(*a, **kw)
            finally:
                store.kv_set = original

        monkeypatch.setattr(mig, "migrate_group_controls", failing)
        app = MaiWorkApp(FakeCtx({}), raw, plugin_dir=plug_dir)
        app.profiles_cls = FakeProfiles
        await app.start()   # 迁移炸了也不该拖垮启动
        try:
            text = _config_text(plug_dir)
            # 旧全局键一个不删（种每群那份没成功，源必须留着下次再迁）
            for kept in ("admins", "exempt_users", "per_day", "speaker", "push_per_day", "quiet_hours"):
                assert kept in text, kept
            assert app.store.kv_get(f"{group_push.KV_PREFIX}{G1}") is None
            assert app.store.kv_get(APPROVAL_KV + G1) is None
        finally:
            await app.stop()


# ======================================================================
# 2. 三种自制消息统一接发件箱：hook 只登记一次；只有真 sent 才回写
# ======================================================================


def _hook_count(outbox: object, owner: object, name: str, *, attr: str = "_result_hooks") -> int:
    hooks = getattr(outbox, attr, [])
    return sum(
        1
        for h in hooks
        if getattr(h, "__self__", None) is owner and getattr(h, "__name__", "") == name
    )


class TestOutboxWiring:
    @pytest.mark.asyncio
    async def test_all_three_attached_with_single_hook(self, env: Env) -> None:
        app = env.app
        assert app.outbox is not None
        assert app.topics._outbox is app.outbox
        assert app.card_push._outbox is app.outbox
        assert app.idea_mention._outbox is app.outbox
        for owner, name in (
            (app.topics, "on_result"),
            (app.card_push, "on_result"),
            (app.idea_mention, "on_result"),
        ):
            assert _hook_count(app.outbox, owner, name) == 1, (type(owner).__name__, name)
        # 个人提一嘴的复核者是单独登记的（set_personal_guard）：登记一次，且不是普通 preflight
        guard = getattr(app.outbox, "_personal_guard", None)
        assert guard is not None and getattr(guard, "__self__", None) is app.idea_mention
        assert getattr(guard, "__name__", "") == "on_before_send"
        assert _hook_count(app.outbox, app.idea_mention, "on_before_send",
                           attr="_preflight_hooks") == 0
        assert _hook_count(app.outbox, app.card_push, "on_before_send",
                           attr="_preflight_hooks") == 0
        # 开场白还是走普通 preflight（不能冒充个人提一嘴的复核者）
        assert _hook_count(app.outbox, app.topics, "on_before_send",
                           attr="_preflight_hooks") == 1

    @pytest.mark.asyncio
    async def test_pending_opener_written_only_after_real_sent(self, env: Env) -> None:
        """入队时只标 pending（opener 空）；真发出去了才写 opener / 候选 / pushes 账本。"""
        app = env.app
        noon = _noon()
        assert not app.pushes.in_quiet(noon, G1), "这个时刻不该在睡觉时段"
        group_push.set_config(app.store, G1, {"topics_enabled": True}, app.get_settings(), now=noon)
        with app.store.tx() as conn:
            conn.execute("UPDATE groups SET session_id='sess-g1' WHERE group_id=?", (G1,))
        cand = app.topics.add_candidate_at(
            G1, kind="news", ref_id=0, title="一条资讯", brief="看点", ttl_h=12, now=noon
        )
        with app.store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO topic_log (group_id, ts, candidate_id) VALUES (?, ?, ?)",
                (G1, noon, cand),
            )
            topic_id = int(cur.lastrowid or 0)

        # 手工入队也带上「冷场快照」证据（0 = 群里还没有更晚的消息）：发件箱的
        # 新鲜度 preflight 拿不到证据会失败关闭作废。
        app.outbox.enqueue(f"topic:{topic_id}", G1, "text",
                           {"text": "开场白第一句", "push_kind": "topic", "cold_since_ts": 0.0})
        row = app.store.read().execute("SELECT opener FROM topic_log WHERE id=?", (topic_id,)).fetchone()
        assert not row["opener"]
        assert app.store.read().execute(
            "SELECT COUNT(*) AS c FROM pushes WHERE group_id=?", (G1,)
        ).fetchone()["c"] == 0

        async def _send_ok(session_id, text, **kw):
            class _R:
                message_id = "mid-1"

            return _R()

        env.app.host.send_text = _send_ok          # 宿主发送打桩（外部依赖，不是内部接线）
        await app.outbox.flush(noon)

        fresh = app.store.read().execute(
            "SELECT opener, candidate_id FROM topic_log WHERE id=?", (topic_id,)
        ).fetchone()
        assert fresh["opener"] == "开场白第一句"
        used = app.store.read().execute(
            "SELECT used_ts FROM topic_candidates WHERE id=?", (cand,)
        ).fetchone()
        assert used["used_ts"] is not None
        assert app.store.read().execute(
            "SELECT COUNT(*) AS c FROM pushes WHERE group_id=?", (G1,)
        ).fetchone()["c"] == 1

    @pytest.mark.asyncio
    async def test_send_not_done_writes_nothing(self, env: Env) -> None:
        """开关关着 → 发件箱作废（dropped）→ 不写 opener、候选不标 used、账本零。"""
        app = env.app
        noon = _noon()
        group_push.set_config(app.store, G1, {"topics_enabled": False}, app.get_settings(), now=noon)
        cand = app.topics.add_candidate_at(
            G1, kind="news", ref_id=0, title="没人看", brief="", ttl_h=12, now=noon
        )
        with app.store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO topic_log (group_id, ts, candidate_id) VALUES (?, ?, ?)",
                (G1, noon, cand),
            )
            topic_id = int(cur.lastrowid or 0)
        app.outbox.enqueue(f"topic:{topic_id}", G1, "text",
                           {"text": "不该发出去", "push_kind": "topic", "cold_since_ts": 0.0})
        await app.outbox.flush(noon)
        row = app.store.read().execute("SELECT opener FROM topic_log WHERE id=?", (topic_id,)).fetchone()
        assert not row["opener"]
        used = app.store.read().execute(
            "SELECT used_ts FROM topic_candidates WHERE id=?", (cand,)
        ).fetchone()
        assert used["used_ts"] is None
        assert app.store.read().execute(
            "SELECT COUNT(*) AS c FROM pushes WHERE group_id=?", (G1,)
        ).fetchone()["c"] == 0


# ======================================================================
# 3. /api/groups/{gid}/push：鉴权 / 同源 / 退役字段 / 返回形状 / 不泄密
# ======================================================================


class TestPushApi:
    @pytest.mark.asyncio
    async def test_anonymous_401_member_403_other_group_403(self, env: Env) -> None:
        r = await env.client.get(f"/api/groups/{G1}/push")
        assert r.status == 401, r.status
        r = await env.client.get(f"/api/groups/{G1}/push", headers=env.group_headers())
        assert r.status == 403, r.status
        env.set_group_password(G1, G1_PW)
        env.set_group_password(G2, G2_PW)
        await env.login(G2_PW)
        r = await env.client.get(f"/api/groups/{G1}/push")
        assert r.status == 403, r.status

    @pytest.mark.asyncio
    async def test_own_group_admin_read_and_write(self, env: Env) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        r = await env.client.get(f"/api/groups/{G1}/push")
        assert r.status == 200, await r.text()
        body = await r.json()
        assert set(body) == {"config", "daily_max", "sent_today", "quota_used", "recent"}
        assert body["config"]["daily_max"] == 5
        assert body["config"]["topics_enabled"] is False
        r2 = await env.client.put(f"/api/groups/{G1}/push", json={"daily_max": 4, "topics_enabled": True})
        assert r2.status == 200, await r2.text()
        out = await r2.json()
        assert out["config"]["daily_max"] == 4 and out["config"]["topics_enabled"] is True
        # 只认自己群：别的群写 403
        r3 = await env.client.put(f"/api/groups/{G2}/push", json={"daily_max": 1})
        assert r3.status == 403, r3.status

    @pytest.mark.asyncio
    async def test_admin_and_cross_origin_and_nonserved(self, env: Env) -> None:
        await env.login(ADMIN_PW)
        r = await env.client.put(
            f"/api/groups/{G1}/push", json={"daily_max": 3}, headers={"Origin": "https://evil.example"}
        )
        assert r.status == 403, r.status            # 同源守卫
        r = await env.client.put("/api/groups/999999/push", json={"daily_max": 3})
        assert r.status == 404, r.status            # 非服务群
        r = await env.client.get("/api/groups/999999/push")
        assert r.status == 404, r.status
        r = await env.client.put(f"/api/groups/{G1}/push", json={"daily_max": 3})
        assert r.status == 200, await r.text()      # 总管理员随便改

    @pytest.mark.asyncio
    async def test_retired_field_400_says_group_page_and_changes_nothing(self, env: Env) -> None:
        await env.login(ADMIN_PW)
        before = await (await env.client.get(f"/api/groups/{G1}/push")).json()
        r = await env.client.put(f"/api/groups/{G1}/push", json={"push_per_day": 9})
        assert r.status == 400, r.status
        body = await r.json()
        assert "群" in body["error"] and "退役" in body["error"]
        r2 = await env.client.put(f"/api/groups/{G1}/push", json={"news_card_daily_max": 2})
        assert r2.status == 400, r2.status          # 退役字段明确拒，不静默假保存
        after = await (await env.client.get(f"/api/groups/{G1}/push")).json()
        assert after["config"] == before["config"]
        r3 = await env.client.put(f"/api/groups/{G1}/push", json={"nonsense": 1})
        assert r3.status == 400, r3.status          # 不认识的字段也拒

    @pytest.mark.asyncio
    async def test_push_and_card_push_share_one_source(self, env: Env) -> None:
        await env.login(ADMIN_PW)
        r = await env.client.put(f"/api/groups/{G1}/push", json={"daily_max": 6, "news_card_count": 2})
        assert r.status == 200, await r.text()
        alias = await (await env.client.get(f"/api/groups/{G1}/card-push")).json()
        assert alias["config"]["daily_max"] == 6
        assert alias["config"]["news_card_count"] == 2
        # 反向：旧别名写进去的，新接口读得到（同一份 kv，没有第二处）
        r2 = await env.client.put(f"/api/groups/{G1}/card-push", json={"news_card_count": 3})
        assert r2.status == 200, await r2.text()
        fresh = await (await env.client.get(f"/api/groups/{G1}/push")).json()
        assert fresh["config"]["news_card_count"] == 3

    @pytest.mark.asyncio
    async def test_response_masks_secrets_and_paths(self, env: Env) -> None:
        app = env.app
        noon = _noon()
        url = "https://news.example.com/a/b?c=1"
        with app.store.tx() as conn:
            app.store.secret_set(conn, "push-leak", SECRET)
            conn.execute(
                "INSERT INTO outbox (key, group_id, kind, payload, status, attempts, result, error,"
                " created, updated) VALUES ('topic:999', ?, 'text', ?, 'failed', 1, '{}', ?, ?, ?)",
                (G1, json.dumps({"text": f"发失败了 {SECRET}", "push_kind": "topic"}),
                 f"发送失败：{SECRET} 路径 {ABS_PATH} 链接 {url}", noon, noon),
            )
        await env.login(ADMIN_PW)
        r = await env.client.get(f"/api/groups/{G1}/push")
        assert r.status == 200, await r.text()
        text = await r.text()
        assert SECRET not in text
        assert ABS_PATH not in text
        body = await r.json()
        assert "路径已略" in body["recent"][0]["error"]
        assert SECRET not in body["recent"][0]["error"]
        assert SECRET not in body["recent"][0]["text"]
        assert url in body["recent"][0]["error"]     # 网址不许被路径遮罩误伤


# ======================================================================
# 4. 每群开关 / 睡觉时段：只有一个来源
# ======================================================================


class TestPerGroupSwitchAndQuiet:
    @pytest.mark.asyncio
    async def test_topics_effective_on_takes_gid_and_ignores_global_seed(self, env: Env) -> None:
        app = env.app
        params = inspect.signature(app._topics_effective_on).parameters
        assert "gid" in params, "参数 gid 必要"
        # 全局种子说关（旧文件里的原值；迁移后 config.toml 里已经没有了，这里直接改内存设置）
        off = dataclasses.replace(
            app._settings, topics=dataclasses.replace(app._settings.topics, enabled=False)
        )
        app._settings = off
        group_push.set_config(app.store, G1, {"topics_enabled": True}, app.get_settings(), now=_noon())
        assert app._topics_effective_on(G1) is True
        group_push.set_config(app.store, G1, {"topics_enabled": False}, app.get_settings(), now=_noon())
        assert app._topics_effective_on(G1) is False

    @pytest.mark.asyncio
    async def test_scheduler_quiet_uses_current_group_only(self, env: Env) -> None:
        app = env.app
        noon = _noon()
        # G1 白天睡觉，G2 不睡；全局那份是默认 23:00-08:00（此刻不算睡觉）
        group_push.set_config(app.store, G1, {"quiet_hours": "10:00-14:00"}, app.get_settings(), now=noon)
        group_push.set_config(app.store, G2, {"quiet_hours": "23:00-08:00"}, app.get_settings(), now=noon)
        assert app.scheduler._in_quiet(G1, noon) is True
        assert app.scheduler._in_quiet(G2, noon) is False   # 不读外群
        assert app.pushes.in_quiet(noon, G1) is True
        assert await app.topics.check(G1, noon) == "skip:quiet_hours"

    @pytest.mark.asyncio
    async def test_group_view_sleep_is_current_group_quiet(self, env: Env) -> None:
        from CharTyr_MaiWork.maiwork.console import views

        app = env.app
        noon = _noon()
        group_push.set_config(app.store, G1, {"quiet_hours": "10:00-14:00"}, app.get_settings(), now=noon)
        group_push.set_config(app.store, G2, {"quiet_hours": "21:00-06:00"}, app.get_settings(), now=noon)
        v1 = views.group_view(app, G1, admin=True)
        v2 = views.group_view(app, G2, admin=True)
        assert v1["pulse"]["sleep"] == "10:00-14:00"
        assert v2["pulse"]["sleep"] == "21:00-06:00"


class TestSettingsViewSeedOnly:
    @pytest.mark.asyncio
    async def test_settings_rules_marked_seed_only_with_group_entry(self, env: Env) -> None:
        """设置页的 rules 旧 blob 是死展示：明确标成「迁移种子 + 去群页改」，不再像全局可改项。"""
        await env.login(ADMIN_PW)
        r = await env.client.get("/api/settings")
        assert r.status == 200, await r.text()
        rules = (await r.json())["rules"]
        assert rules["group_managed"] is True and rules["seed_only"] is True
        assert "每个群" in rules["note"]
        assert "往群里发" in rules["group_page"]
        # 老键还在（兼容老前端 / 老测试），但都被标成种子，不是第二运行来源
        assert "quiet_hours" in rules and "topics_min_gap_hours" in rules


# ======================================================================
# 5. 每服务群巡检挂构想自动收起（只变网页，不发消息）
# ======================================================================


class TestShelveIgnoredIdeasRound:
    def _add_idea(self, store, gid: str, *, created: float, state: str = "new",
                  target: str = "", task_id: str = "") -> int:
        with store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO ideas (group_id, icon, title, body, state, target_user_id, task_id,"
                " created, updated) VALUES (?, 'bulb', '没人理的构想', '正文', ?, ?, ?, ?, ?)",
                (gid, state, target, task_id or None, created, created),
            )
            return int(cur.lastrowid or 0)

    @pytest.mark.asyncio
    async def test_round_shelves_only_ignored_group_ideas_without_sending(self, env: Env) -> None:
        app = env.app
        now = clock.now()
        old = now - 8 * 86400.0
        ignored_g1 = self._add_idea(app.store, G1, created=old)
        ignored_g2 = self._add_idea(app.store, G2, created=old)
        personal = self._add_idea(app.store, G1, created=old, target="qq:20002")
        started = self._add_idea(app.store, G1, created=old, state="started", task_id="T-9")
        waiting = self._add_idea(app.store, G1, created=old, state="pending")
        with app.store.tx() as conn:
            conn.execute(
                "INSERT INTO requests (id, group_id, kind, title, quote, via, status, idea_id,"
                " created, updated) VALUES ('R-77', ?, 'task', 't', 'q', '来自构想', 'pending', ?, ?, ?)",
                (G1, waiting, old, old),
            )
        outbox_before = app.store.read().execute("SELECT COUNT(*) AS c FROM outbox").fetchone()["c"]

        await app.run_loop_once()

        def _state(iid: int) -> str:
            row = app.store.read().execute("SELECT state FROM ideas WHERE id=?", (iid,)).fetchone()
            return str(row["state"])

        assert _state(ignored_g1) == "dismissed"
        assert _state(ignored_g2) == "dismissed"      # 每个服务群都巡检
        assert _state(personal) == "new"              # 个人向不动
        assert _state(started) == "started"           # 已开工不动
        assert _state(waiting) == "pending"           # 待批准不动
        outbox_after = app.store.read().execute("SELECT COUNT(*) AS c FROM outbox").fetchone()["c"]
        assert outbox_after == outbox_before, "自动收起不发消息"
