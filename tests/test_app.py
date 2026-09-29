"""app.py / plugin.py 单元测试：enabled 开关、热更新、后台循环、钩子转发。"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from pathlib import Path

import pytest

from fakes import FakeCtx, FakeProfiles, hook_message

from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.plugin import MaiWorkPlugin, create_plugin

G1 = "900000001"
G2 = "123456789"



def _free_port() -> int:
    """挑一个本机空闲端口：测试别依赖 18650 空着（用户可能正开着 SSH 隧道看网页）。"""
    import socket as _s
    with _s.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


P0, P1, P2, P3 = (_free_port() for _ in range(4))

def _raw(data_dir: Path, *, enabled: bool = True, listen: str = f"127.0.0.1:{P0}") -> dict:
    return {
        "plugin": {"enabled": enabled},
        "groups": {"serve": [{"group": f"qq:{G1}"}, {"group": f"qq:{G2}"}]},
        "console": {"listen": listen, "password": "pw-测试"},
        "storage": {"data_dir": str(data_dir)},
    }


def _app(tmp_path: Path, *, enabled: bool = True, name: str = "data", ctx: FakeCtx | None = None, raw: dict | None = None) -> MaiWorkApp:
    app = MaiWorkApp(ctx or FakeCtx({}), raw if raw is not None else _raw(tmp_path / name, enabled=enabled), plugin_dir=Path(__file__).resolve().parents[1])
    app.profiles_cls = FakeProfiles  # 测试不依赖另一个人写的 profile.py
    return app


def _raw_models(data_dir: Path) -> dict:
    """基础配置 + 配好的 [models]（假端点，不会真调；门就绪用于「派工」类测试）。

    没配 models 时后台派工 / coordinator 都应保持排队（02 §12.1 的回归约束），
    所以要测「会派工」的路径必须先把 [models] 配上。
    """
    raw = _raw(data_dir)
    raw["models"] = {"base_url": "http://127.0.0.1:9/v1", "api_key": "test-key", "main": "m1", "worker": "w1"}
    return raw


def _port_open(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.2)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


# ----------------------------------------------------------------------
# enabled 开关
# ----------------------------------------------------------------------


class TestEnabledSwitch:
    @pytest.mark.asyncio
    async def test_disabled_start_does_nothing(self, tmp_path: Path) -> None:
        app = _app(tmp_path, enabled=False)
        await app.start()
        assert app.started is False
        assert app.store is None
        assert not _port_open(P0)
        assert not (tmp_path / "data" / "maiwork.db").exists()
        await app.stop()

    @pytest.mark.asyncio
    async def test_enabled_start_opens_store_and_console(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        await app.start()
        try:
            assert app.started is True
            assert app.store is not None
            assert _port_open(P0)
            assert (tmp_path / "data" / "maiwork.db").exists()
            # ensure_groups：每个服务群都有行 + 8 位 token
            for gid in (G1, G2):
                row = app.store.read().execute("SELECT token, workspace FROM groups WHERE group_id=?", (gid,)).fetchone()
                assert row is not None
                assert len(row["token"]) == 8
        finally:
            await app.stop()
        assert not _port_open(P0)

    @pytest.mark.asyncio
    async def test_on_off_on(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        await app.start()
        assert _port_open(P0)
        assert app.store is not None
        # 开着 → 关掉
        await app.update_config(_raw(tmp_path / "data", enabled=False))
        assert app.started is False
        assert not _port_open(P0)
        assert app.store is None
        # 关掉 → 再开
        await app.update_config(_raw(tmp_path / "data", enabled=True))
        assert app.started is True
        assert _port_open(P0)
        await app.stop()
        assert not _port_open(P0)

    @pytest.mark.asyncio
    async def test_listen_change_restarts_console(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        await app.start()
        assert _port_open(P0)
        # 改监听端口：旧的关、新的开
        new_raw = _raw(tmp_path / "data", listen=f"127.0.0.1:{P1}")
        await app.update_config(new_raw)
        assert not _port_open(P0)
        assert _port_open(P1)
        await app.stop()

    @pytest.mark.asyncio
    async def test_port_taken_does_not_raise(self, tmp_path: Path, caplog) -> None:
        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", P0))
        blocker.listen(1)
        try:
            app = _app(tmp_path)
            with caplog.at_level(logging.ERROR):
                await app.start()  # 不抛
            assert "端口" in caplog.text or str(P0) in caplog.text
            await app.stop()
        finally:
            blocker.close()


# ----------------------------------------------------------------------
# on_message / 钩子
# ----------------------------------------------------------------------


class TestOnMessage:
    @pytest.mark.asyncio
    async def test_unstarted_app_returns_continue(self, tmp_path: Path) -> None:
        app = _app(tmp_path, enabled=False)
        out = await app.on_message(hook_message(group_id=G1))
        assert out == {"action": "continue"}

    @pytest.mark.asyncio
    async def test_non_served_group_zero_host_calls(self, tmp_path: Path) -> None:
        ctx = FakeCtx({})
        app = _app(tmp_path, ctx=ctx)
        await app.start()
        try:
            ctx.calls.clear()
            out = await app.on_message(hook_message(group_id="999999999"))
            assert out == {"action": "continue"}
            # 启动后清掉预热调用，非服务群消息后不许再有任何宿主调用
            assert ctx.calls == []
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_served_group_marks_signal(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        await app.start()
        try:
            out = await app.on_message(hook_message(group_id=G1, ts=1_790_000_123.0))
            assert out == {"action": "continue"}
            taken = app.signals.take()
            assert G1 in taken
            assert taken[G1].session_id == "sess-1"
            assert taken[G1].last_ts == 1_790_000_123.0
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_hook_survives_app_exception(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        await app.start()
        try:
            app.intake.handle = lambda kwargs: (_ for _ in ()).throw(RuntimeError("炸"))
            assert await app.on_message({"message": {}}) == {"action": "continue"}
        finally:
            await app.stop()


# ----------------------------------------------------------------------
# 后台循环
# ----------------------------------------------------------------------


class TestLoop:
    @pytest.mark.asyncio
    async def test_loop_writes_signals_and_ticks(self, tmp_path: Path) -> None:
        profiles = FakeProfiles()
        app = _app(tmp_path)
        app.profiles_factory = lambda *a, **kw: profiles
        await app.start()
        try:
            app.signals.mark(G1, "sess-9", 1_790_000_111.0)
            await app.run_loop_once()
            # 后台循环可能自己也跑了一轮：信号断言被处理过，tick 断言覆盖了每个服务群
            assert (G1, "sess-9", 1_790_000_111.0) in profiles.remembered
            assert G1 in profiles.ticks and G2 in profiles.ticks
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_loop_tick_error_does_not_block_other_groups(self, tmp_path: Path) -> None:
        profiles = FakeProfiles()
        profiles.tick_errors.add(G1)
        app = _app(tmp_path)
        app.profiles_factory = lambda *a, **kw: profiles
        await app.start()
        try:
            await app.run_loop_once()  # 不抛
            assert G1 in profiles.ticks and G2 in profiles.ticks
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_loop_runs_in_background_with_injectable_interval(self, tmp_path: Path) -> None:
        profiles = FakeProfiles()
        app = _app(tmp_path)
        app.profiles_factory = lambda *a, **kw: profiles
        app.loop_interval = 0.05
        await app.start()
        try:
            await asyncio.sleep(0.3)
            assert len(profiles.ticks) >= 2
        finally:
            await app.stop()
        n = len(profiles.ticks)
        await asyncio.sleep(0.15)
        assert len(profiles.ticks) == n  # stop 后不再跑


# ----------------------------------------------------------------------
# plugin.py 接线
# ----------------------------------------------------------------------


def _plugin_config(**overrides) -> dict:
    """按宿主的方式生成完整默认配置，再叠测试要改的节（带 config_version）。"""
    from CharTyr_MaiWork.maiwork.config import MaiWorkConfig

    raw = MaiWorkConfig().model_dump(mode="python")
    for section, values in overrides.items():
        raw.setdefault(section, {}).update(values)
    return raw


def _plugin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MaiWorkPlugin:
    """造一个插件实例，数据目录指到临时路径。"""
    p = create_plugin()
    monkeypatch.setattr(MaiWorkPlugin, "_data_dir_override", str(tmp_path / "pdata"))
    return p


class TestPluginWiring:
    @pytest.mark.asyncio
    async def test_on_load_disabled_no_app(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        p = _plugin(monkeypatch, tmp_path)
        p.set_plugin_config(_plugin_config(plugin={"enabled": False}))
        p._set_context(FakeCtx({}))
        await p.on_load()
        assert p._app is None
        await p.on_unload()

    @pytest.mark.asyncio
    async def test_on_load_enabled_starts_app(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        p = _plugin(monkeypatch, tmp_path)
        p.set_plugin_config(
            _plugin_config(
                plugin={"enabled": True},
                groups={"serve": [{"group": f"qq:{G1}"}]},
                console={"listen": f"127.0.0.1:{P2}", "password": "x"},
            )
        )
        p._set_context(FakeCtx({}))
        await p.on_load()
        assert p._app is not None
        assert _port_open(P2)
        await p.on_unload()
        assert not _port_open(P2)

    @pytest.mark.asyncio
    async def test_no_host_ctx_does_not_start_app(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """没有宿主 ctx（极端情况：测试直接建插件）→ 不启动 app，只记日志。

        插件本体照常加载，on_unload 也不炸。以前这里塞了个动态属性替身
        （__getattr__ 返回一个什么都抛的协程）绕源码扫描，审核不接受，已删。
        """
        p = _plugin(monkeypatch, tmp_path)
        p.set_plugin_config(
            _plugin_config(
                plugin={"enabled": True},
                groups={"serve": [{"group": f"qq:{G1}"}]},
                console={"listen": f"127.0.0.1:{P2}", "password": "x"},
            )
        )
        # 故意不调 _set_context（self._ctx 保持 None）
        await p.on_load()
        assert p._app is None
        assert not _port_open(P2)  # 网页端口都没开
        # 钩子照旧安全返回
        assert await p.maiwork_intake(message={"message_info": {}}) == {"action": "continue"}
        await p.on_unload()  # 不炸

    @pytest.mark.asyncio
    async def test_config_update_without_ctx_does_not_start_app(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """热更新把插件从关到开、但还没有 ctx → 同样不启动，不炸。"""
        p = _plugin(monkeypatch, tmp_path)
        p.set_plugin_config(_plugin_config(plugin={"enabled": False}))
        await p.on_load()
        await p.on_config_update(
            "self",
            _plugin_config(
                plugin={"enabled": True},
                console={"listen": f"127.0.0.1:{P3}", "password": "x"},
                groups={"serve": [{"group": f"qq:{G1}"}]},
            ),
            "0.1.0",
        )
        assert p._app is None
        assert not _port_open(P3)

    @pytest.mark.asyncio
    async def test_on_load_exception_does_not_propagate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        p = _plugin(monkeypatch, tmp_path)
        p.set_plugin_config(_plugin_config(plugin={"enabled": True}))
        p._set_context(FakeCtx({}))

        async def boom(*a, **kw):
            raise RuntimeError("start 炸了")

        monkeypatch.setattr(MaiWorkApp, "start", boom)
        await p.on_load()  # 不抛，插件照常加载
        assert p._app is None
        await p.on_unload()

    @pytest.mark.asyncio
    async def test_config_update_off_on(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        p = _plugin(monkeypatch, tmp_path)
        p.set_plugin_config(_plugin_config(plugin={"enabled": False}))
        p._set_context(FakeCtx({}))
        await p.on_load()
        assert p._app is None
        # 从关到开
        await p.on_config_update(
            "self",
            _plugin_config(
                plugin={"enabled": True},
                console={"listen": f"127.0.0.1:{P3}", "password": "x"},
                groups={"serve": [{"group": f"qq:{G1}"}]},
            ),
            "0.1.0",
        )
        assert p._app is not None
        assert _port_open(P3)
        # 从开到关
        await p.on_config_update("self", _plugin_config(plugin={"enabled": False}), "0.1.0")
        assert not _port_open(P3)
        await p.on_unload()

    @pytest.mark.asyncio
    async def test_config_update_other_scope_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        p = _plugin(monkeypatch, tmp_path)
        p.set_plugin_config(_plugin_config(plugin={"enabled": False}))
        p._set_context(FakeCtx({}))
        await p.on_load()
        await p.on_config_update("bot", _plugin_config(plugin={"enabled": True}), "0.1.0")
        assert p._app is None
        await p.on_unload()

    @pytest.mark.asyncio
    async def test_hook_delegates_and_never_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        p = _plugin(monkeypatch, tmp_path)
        p.set_plugin_config(_plugin_config(plugin={"enabled": False}))
        p._set_context(FakeCtx({}))
        await p.on_load()
        # app 未启动：直接 continue
        out = await p.maiwork_intake(message={"message_info": {}})
        assert out == {"action": "continue"}
        # 装一个会炸的 app
        class _Boom:
            def on_message(self, kwargs):
                raise RuntimeError("炸")

        p._app = _Boom()
        out = await p.maiwork_intake(message={"message_info": {}})
        assert out == {"action": "continue"}
        await p.on_unload()

    def test_create_plugin_contract(self) -> None:
        p = create_plugin()
        assert hasattr(p, "on_load") and hasattr(p, "on_unload") and hasattr(p, "on_config_update")
        info = getattr(type(p).__dict__["maiwork_intake"], "__maibot_component_info__", None)
        assert info is not None
        assert info.hook == "chat.receive.after_process"


# ----------------------------------------------------------------------
# M2：planner 备忘钩子（maisaka.planner.before_request）
# ----------------------------------------------------------------------


class TestPlannerHook:
    def _planner_kwargs(self, session_id: str, system_text: str = "原来的人格") -> dict:
        return {
            "items": [
                {
                    "item_type": "SystemMessageItem",
                    "meta": {"name": "人格"},
                    "parts": [{"type": "text", "text": system_text}],
                }
            ],
            "item_schema_version": 1,
            "session_id": session_id,
        }

    @pytest.mark.asyncio
    async def test_not_served_group_no_read_no_call(self, tmp_path: Path) -> None:
        """非服务群：零读取零调用，返回 continue。"""
        ctx = FakeCtx({})
        app = _app(tmp_path, ctx=ctx)
        await app.start()
        try:
            ctx.calls.clear()
            out = app.on_planner_before_request(self._planner_kwargs("sess-不认识"))
            assert out == {"action": "continue"}
            assert ctx.calls == []  # 一次宿主调用都不许有
            assert app.mentions is not None
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_app_not_started_returns_continue(self, tmp_path: Path) -> None:
        app = _app(tmp_path, enabled=False)
        await app.start()
        out = app.on_planner_before_request(self._planner_kwargs("sess-x"))
        assert out == {"action": "continue"}
        await app.stop()

    @pytest.mark.asyncio
    async def test_mentions_exception_still_continue(self, tmp_path: Path) -> None:
        """inject 抛异常也一律 continue（绝不中止 planner）。"""
        app = _app(tmp_path)
        await app.start()
        try:
            app._started = True

            def _boom(kwargs):
                raise RuntimeError("炸")

            app.mentions.inject = _boom  # type: ignore[method-assign]
            # 让它到 inject 这一步：先别在内存信号里命中
            out = app.on_planner_before_request({"stuff": {}})
            assert out == {"action": "continue"}
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_served_group_injects_memo(self, tmp_path: Path) -> None:
        """服务群 + 有可提起清单 → 返回继续 + modified_kwargs；第一个 system item 尾部被加上备忘。"""
        app = _app(tmp_path)
        await app.start()
        try:
            # 往库里塞一条属于「本群」的 session_id（重启后靠它认群）
            app.signals.mark(G1, "sess-1", 1_790_000_000.0)
            # 实际写一条 mention（groups.session_id 由 run_loop_once 的 remember 写库，这里先手动补足）
            with app.store.tx() as conn:
                conn.execute("UPDATE groups SET session_id='sess-1' WHERE group_id=?", (G1,))
            app.mentions.add(G1, "大家最近都在聊铝坨坨键盘", key="news:1", ttl_s=3600.0, turns=3)
            kwargs = self._planner_kwargs("sess-1")
            out = app.on_planner_before_request(kwargs)
            assert out["action"] == "continue"
            assert "modified_kwargs" in out
            mk = out["modified_kwargs"]
            assert mk["item_schema_version"] == 1
            first_text = mk["items"][0]["parts"][-1]["text"]
            assert "原来的人格" in first_text
            assert "【MaiWork 备忘】" in first_text
            assert "铝坨坨键盘" in first_text
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_memo_survives_signal_consumption_and_restart(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        await app.start()
        try:
            app._remember_session_row(G1, "sess-memo", 1_790_000_000.0)
            app.signals.mark(G1, "sess-memo", 1_790_000_000.0)
            app.signals.take()  # 后台循环消费了易失信号
            app.mentions.add(G1, "待提起的消息", key="news:survive", ttl_s=3600, turns=3)
            out = app.on_planner_before_request(self._planner_kwargs("sess-memo"))
            assert "待提起的消息" in out["modified_kwargs"]["items"][0]["parts"][0]["text"]
        finally:
            await app.stop()
        restarted = _app(tmp_path)
        await restarted.start()
        try:
            out = restarted.on_planner_before_request(self._planner_kwargs("sess-memo"))
            assert "待提起的消息" in out["modified_kwargs"]["items"][0]["parts"][0]["text"]
        finally:
            await restarted.stop()

    @pytest.mark.asyncio
    async def test_memo_injection_uses_verified_group_not_second_lookup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """若数据库会话行歧义，钩子已确认的服务群不能被二次查库换成另一群。"""
        app = _app(tmp_path)
        await app.start()
        try:
            app._remember_session_row(G1, "sess-verified", 1_790_000_000.0)
            app.signals.take()
            app.mentions.add(G1, "只属于本群", key="memo:one", ttl_s=3600)
            app.mentions.add(G2, "属于隔壁群的隐私", key="memo:two", ttl_s=3600)
            monkeypatch.setattr(app.mentions, "_resolve_group", lambda sid: G2)
            out = app.on_planner_before_request(self._planner_kwargs("sess-verified"))
            text = out["modified_kwargs"]["items"][0]["parts"][0]["text"]
            assert "只属于本群" in text
            assert "隔壁群的隐私" not in text
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_duplicate_served_session_ids_do_not_guess_group(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        await app.start()
        try:
            with app.store.tx() as conn:
                conn.execute("UPDATE groups SET session_id='sess-duplicate' WHERE group_id IN (?, ?)", (G1, G2))
            app._refresh_served_sessions()
            app.signals.take()
            app.mentions.add(G1, "第一群内容", key="memo:one", ttl_s=3600)
            app.mentions.add(G2, "第二群内容", key="memo:two", ttl_s=3600)
            assert app.on_planner_before_request(self._planner_kwargs("sess-duplicate")) == {"action": "continue"}
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_old_session_of_removed_group_cannot_inject_memo(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        await app.start()
        try:
            with app.store.tx() as conn:
                conn.execute("INSERT INTO groups (group_id, session_id) VALUES (?, ?)",
                             ("non-served", "sess-removed"))
            app.mentions.add("non-served", "绝不能注入", key="gone:1", ttl_s=3600)
            out = app.on_planner_before_request(self._planner_kwargs("sess-removed"))
            assert out == {"action": "continue"}
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_plugin_hook_signature_registered_with_correct_metadata(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """插件层：maiwork_mentions 已按 BLOCKING/1000ms/SKIP/name 注册。"""
        from CharTyr_MaiWork.plugin import MaiWorkPlugin

        info = MaiWorkPlugin.__dict__["maiwork_mentions"].__maibot_component_info__
        assert info.hook == "maisaka.planner.before_request"
        assert info.name == "maiwork_mentions"
        assert str(info.mode).endswith("BLOCKING")
        assert int(info.timeout_ms) == 1000
        assert str(info.error_policy).endswith("SKIP")


# ----------------------------------------------------------------------
# M3：构想 → 活（「做这个」on_start / 「想要这个」on_idea_want）
# ----------------------------------------------------------------------


class TestM3IdeaHooks:
    def _insert_idea(self, app: MaiWorkApp, gid: str = G1, *, items: list | None = None) -> int:
        import json as _json

        from CharTyr_MaiWork.maiwork import clock

        with app.store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO ideas (group_id, icon, title, body, basis, step, effort, items,"
                " state, created, updated)"
                " VALUES (?, 'bulb', '做个铝价表', '把最近铝价整理成表', '群里天天聊铝', '', '', ?,"
                " 'new', ?, ?)",
                (gid, _json.dumps(items or [], ensure_ascii=False), clock.now(), clock.now()),
            )
            return int(cur.lastrowid or 0)

    def _idea_view(self, app: MaiWorkApp, idea_id: int) -> dict:
        return app.feeds._idea_one(idea_id)  # noqa: SLF001 —— 测试直接要「真实 view 形状」

    @pytest.mark.asyncio
    async def test_on_idea_started_creates_task_and_writes_back(self, tmp_path: Path) -> None:
        """「做这个」→ 落任务(queued, source=idea) + ideas.task_id 回写 + spawn run_task。"""
        from fakes import FakeCoordinator

        coord = FakeCoordinator()
        app = _app(tmp_path)
        app.coordinator_factory = lambda *a, **kw: coord
        await app.start()
        try:
            idea_id = self._insert_idea(app)
            view = self._idea_view(app, idea_id)
            app._on_idea_started(view)
            row = app.store.read().execute(
                "SELECT state, task_id FROM ideas WHERE id=?", (idea_id,)
            ).fetchone()
            tid = str(row["task_id"] or "")
            assert tid.startswith("T-")
            task = app.tasks.get(tid)
            assert task["status"] == "queued"
            assert task["source"] == "idea"
            assert task["group_id"] == G1
            for _ in range(30):
                if tid in coord.run_calls:
                    break
                await asyncio.sleep(0.02)
            assert tid in coord.run_calls
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_on_idea_want_pending_marks_idea_pending(self, tmp_path: Path) -> None:
        """要批准的部署里 want → 待批请求（via=来自构想），构想卡标 pending。"""
        app = _app(tmp_path)
        app.coordinator_factory = lambda *a, **kw: None
        await app.start()
        try:
            idea_id = self._insert_idea(app)
            view = self._idea_view(app, idea_id)
            view["state"] = "wanted"
            view["requested_by"] = "阿柒"
            app.on_idea_want(view, G1)
            row = app.store.read().execute(
                "SELECT state FROM ideas WHERE id=?", (idea_id,)
            ).fetchone()
            assert row["state"] == "pending"
            pending = app.approvals.pending_view(G1)
            assert len(pending) == 1
            assert pending[0]["via"] == "来自构想"
            assert pending[0]["title"] == "做个铝价表"
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_on_idea_want_auto_approves_and_starts(self, tmp_path: Path) -> None:
        """免批的部署里 want → 直接落地：构想卡标 started、回写 task_id、spawn run_task。"""
        from fakes import FakeCoordinator

        raw = _raw(tmp_path / "data")
        raw["approval"] = {"required": False}
        ctx = FakeCtx({})
        app = MaiWorkApp(ctx, raw, plugin_dir=Path(__file__).resolve().parents[1])
        app.profiles_cls = FakeProfiles
        coord = FakeCoordinator()
        app.coordinator_factory = lambda *a, **kw: coord
        await app.start()
        try:
            idea_id = self._insert_idea(app)
            view = self._idea_view(app, idea_id)
            view["state"] = "wanted"
            view["requested_by"] = "阿柒"
            app.on_idea_want(view, G1)
            row = app.store.read().execute(
                "SELECT state, task_id FROM ideas WHERE id=?", (idea_id,)
            ).fetchone()
            assert row["state"] == "started"
            tid = str(row["task_id"] or "")
            assert tid.startswith("T-")
            for _ in range(30):
                if tid in coord.run_calls:
                    break
                await asyncio.sleep(0.02)
            assert tid in coord.run_calls
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_on_idea_started_with_items_lands_each_item(self, tmp_path: Path) -> None:
        """「做这个」也认项目：task 项落任务、goal 项落 agent 目标，所有任务都开工。"""
        from fakes import FakeCoordinator

        coord = FakeCoordinator()
        app = _app(tmp_path)
        app.coordinator_factory = lambda *a, **kw: coord
        await app.start()
        try:
            idea_id = self._insert_idea(app, items=[
                {"kind": "task", "title": "抓铝价数据", "desc": "先抓一个月"},
                {"kind": "goal", "title": "每周更新铝价表", "desc": "每周更新一次"},
                {"kind": "task", "title": "做成一张表", "desc": ""},
            ])
            app._on_idea_started(self._idea_view(app, idea_id))
            rows = app.store.read().execute(
                "SELECT id, title, req FROM tasks WHERE group_id=? ORDER BY id", (G1,)
            ).fetchall()
            assert [r["title"] for r in rows] == ["抓铝价数据", "做成一张表"]
            assert "先抓一个月" in rows[0]["req"] and "来自构想" in rows[0]["req"]
            goals = app.goals.view(G1)["agent"]
            assert [g["title"] for g in goals] == ["每周更新铝价表"]
            tid = str(app.store.read().execute(
                "SELECT task_id FROM ideas WHERE id=?", (idea_id,)
            ).fetchone()["task_id"])
            assert tid == rows[0]["id"]
            for one in (rows[0]["id"], rows[1]["id"]):
                for _ in range(30):
                    if one in coord.run_calls:
                        break
                    await asyncio.sleep(0.02)
                assert one in coord.run_calls
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_on_idea_started_only_picked_items(self, tmp_path: Path) -> None:
        """网页「直接开工」带 item_nos 时只落勾选的项目（1 起序号），别的项目不动。"""
        from fakes import FakeCoordinator

        coord = FakeCoordinator()
        app = _app(tmp_path)
        app.coordinator_factory = lambda *a, **kw: coord
        await app.start()
        try:
            idea_id = self._insert_idea(app, items=[
                {"kind": "task", "title": "抓铝价数据", "desc": "先抓一个月"},
                {"kind": "goal", "title": "每周更新铝价表", "desc": "每周更新一次"},
                {"kind": "task", "title": "做成一张表", "desc": ""},
            ])
            view = self._idea_view(app, idea_id)
            view["item_nos"] = [1, 3]
            app._on_idea_started(view)
            rows = app.store.read().execute(
                "SELECT title FROM tasks WHERE group_id=? ORDER BY id", (G1,)
            ).fetchall()
            assert [r["title"] for r in rows] == ["抓铝价数据", "做成一张表"]
            assert app.goals.view(G1)["agent"] == []  # 第 2 项（goal）没被勾选
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_on_idea_started_bad_item_nos_lands_all(self, tmp_path: Path) -> None:
        """越界 / 非法序号一个都对不上 → 按「全部」处理（和 approvals 的口径一致）。"""
        from fakes import FakeCoordinator

        coord = FakeCoordinator()
        app = _app(tmp_path)
        app.coordinator_factory = lambda *a, **kw: coord
        await app.start()
        try:
            idea_id = self._insert_idea(app, items=[
                {"kind": "task", "title": "第一件", "desc": ""},
                {"kind": "task", "title": "第二件", "desc": ""},
            ])
            view = self._idea_view(app, idea_id)
            view["item_nos"] = [9, "x", None]
            app._on_idea_started(view)
            rows = app.store.read().execute(
                "SELECT title FROM tasks WHERE group_id=? ORDER BY id", (G1,)
            ).fetchall()
            assert [r["title"] for r in rows] == ["第一件", "第二件"]
        finally:
            await app.stop()


# ----------------------------------------------------------------------
# M3：后台循环接 outbox / approvals / goals / coordinator
# ----------------------------------------------------------------------


class TestReminderParse:
    def test_parse_ok(self) -> None:
        from CharTyr_MaiWork.maiwork.app import _parse_reminder_json

        now = 1_790_000_000.0
        out = _parse_reminder_json(
            f'hhhh {{"ok": true, "title": "吃药", "due_ts": {now + 600}, "remind_ts": {now + 500}}} zzz', now
        )
        assert out == {"title": "吃药", "due_ts": now + 600, "remind_ts": now + 500}

    def test_parse_ok_false_and_garbage(self) -> None:
        from CharTyr_MaiWork.maiwork.app import _parse_reminder_json

        now = 1_790_000_000.0
        assert _parse_reminder_json('{"ok": false}', now) is None
        assert _parse_reminder_json("没有 JSON", now) is None
        assert _parse_reminder_json('{"ok": true, "title": "", "due_ts": 1}', now) is None
        # 解析到过去的时间也不收
        assert _parse_reminder_json(f'{{"ok": true, "title": "x", "due_ts": {now - 3600}}}', now) is None

    def test_remind_ts_defaults_to_due_ts(self) -> None:
        from CharTyr_MaiWork.maiwork.app import _parse_reminder_json

        now = 1_790_000_000.0
        out = _parse_reminder_json(f'{{"ok": true, "title": "吃药", "due_ts": {now + 600}}}', now)
        assert out is not None and out["remind_ts"] == now + 600


class TestM3Loop:
    @pytest.mark.asyncio
    async def test_m3_modules_exist_after_start(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        await app.start()
        try:
            assert app.tasks is not None
            assert app.goals is not None
            assert app.approvals is not None
            assert app.outbox is not None
            assert app.delivery is not None
            assert app.commands is not None
            assert app.env is not None  # environments/local.py 在本机上能建（direct/systemd 都能构造）
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_auto_review_wired_and_reuses_spawn_run_task(self, tmp_path: Path) -> None:
        """自动审核接在 Approvals 上；开工入口和人批 / 网页批准同一个（app.spawn_run_task）。"""
        app = _app(tmp_path)
        await app.start()
        try:
            assert app.auto_review is not None
            assert app.approvals._review_hook is not None
            assert app.auto_review._run_task_starter == app.spawn_run_task
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_pending_request_auto_approved_in_background(self, tmp_path: Path) -> None:
        """落一条待批请求 → 回调 spawn 后台审核 → 主模型说能批 → 批准 + 开工（钩子不阻塞）。"""
        app = _app(tmp_path)
        await app.start()
        try:
            class _FakeModels:
                def settings(self):
                    class _S:
                        def ready(self) -> bool:
                            return True

                    return _S()

                async def chat(self, role, messages, **kwargs):
                    return type("R", (), {"text": '{"approve": true, "reason": "查资料的小活"}'})()

            app.auto_review._models = _FakeModels()
            started: list[str] = []
            app.auto_review._run_task_starter = started.append
            r = app.approvals.create(
                G1, kind="task", title="帮我查一下免费图床", quote="帮我查一下",
                via="群里 @", requester_id="10001", requester_name="阿柒",
            )
            assert r["status"] == "pending"  # create 直接返回，没等模型
            for _ in range(50):
                if started:
                    break
                await asyncio.sleep(0.01)
            row = app.store.read().execute("SELECT * FROM requests WHERE id=?", (r["id"],)).fetchone()
            assert str(row["status"]) == "approved"
            assert str(row["decided_by"]) == "MaiWork 自动审核"
            assert str(row["auto_reason"]) == "查资料的小活"
            assert started == [str(row["task_id"])]
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_coordinator_missing_still_starts(self, tmp_path: Path) -> None:
        """coordinator 没建好：app 照常启动，queued 任务不炸，只是没人开工。"""
        app = _app(tmp_path)
        app.coordinator_factory = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("没就位"))
        await app.start()
        try:
            assert app.coordinator is None
            tid = app.tasks.create(G1, title="没人做的活", req="", criteria=[], source="test", status="queued")
            await app.run_loop_once()
            assert app.tasks.get(tid)["status"] == "queued"
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_queued_task_spawned_once_until_free(self, tmp_path: Path) -> None:
        """queued 任务 spawn run_task；任务还在跑时下一轮不重复开工。"""
        from fakes import FakeCoordinator

        coord = FakeCoordinator(delay_s=0.25)
        app = _app(tmp_path, raw=_raw_models(tmp_path / "data"))
        app.coordinator_factory = lambda *a, **kw: coord
        await app.start()
        try:
            tid = app.tasks.create(G1, title="整理资料", req="", criteria=[], source="test", status="queued")
            await app.run_loop_once()
            for _ in range(30):
                if tid in coord.run_calls:
                    break
                await asyncio.sleep(0.02)
            assert tid in coord.run_calls
            # 任务还在跑（delay 0.25s），第二轮不再开工
            await app.run_loop_once()
            assert coord.run_calls.count(tid) == 1
            await asyncio.sleep(0.35)  # 跑完释放
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_outbox_flush_called_every_round(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _app(tmp_path)
        await app.start()
        try:
            calls: list[float] = []

            async def _flush(now):
                calls.append(float(now))

            monkeypatch.setattr(app.outbox, "flush", _flush)
            await app.run_loop_once()
            await app.run_loop_once()
            assert len(calls) == 2
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_approval_expire_and_reminder_enqueue(self, tmp_path: Path) -> None:
        """pending 超 7 天 → expired；超 24 小时 → 群里提醒一次（去重）。"""
        app = _app(tmp_path)
        app.coordinator_factory = lambda *a, **kw: None  # 本测试不关心任务开工
        await app.start()
        try:
            # 三件待批：G1 一件 25 小时（提醒）、G2 一件 8 天（过期）、G2 一件 7 天 1 小时（也过期）
            r1 = app.approvals.create(
                G1, kind="task", title="旧请求一", quote="", via="群里 @",
                requester_id="20002", requester_name="阿柒",
            )
            r2 = app.approvals.create(
                G2, kind="task", title="旧请求二", quote="", via="群里 @",
                requester_id="20003", requester_name="老八",
            )
            from CharTyr_MaiWork.maiwork import clock

            t = clock.now()
            with app.store.tx() as conn:
                conn.execute("UPDATE requests SET created=? WHERE id=?", (t - 25 * 3600, r1["id"]))
                conn.execute("UPDATE requests SET created=? WHERE id=?", (t - 8 * 86400, r2["id"]))
            await app.run_loop_once()
            # 8 天的过期了
            row = app.store.read().execute("SELECT status FROM requests WHERE id=?", (r2["id"],)).fetchone()
            assert row["status"] == "expired"
            # 25 小时的被提醒：outbox 里有一条 status 文本，且 requests 记了 reminded_ts
            rows = app.store.read().execute(
                "SELECT key, payload, status FROM outbox WHERE key LIKE 'approval-remind:%'"
            ).fetchall()
            assert rows, "该有一条待批提醒进群"
            assert any(str(r["key"]).startswith(f"approval-remind:{G1}:") for r in rows)
            row = app.store.read().execute("SELECT reminded_ts FROM requests WHERE id=?", (r1["id"],)).fetchone()
            assert row["reminded_ts"] is not None
            # 再跑一轮：提醒过的不再提醒、也不再入队
            n_before = len(rows)
            await app.run_loop_once()
            rows2 = app.store.read().execute(
                "SELECT key FROM outbox WHERE key LIKE 'approval-remind:%'"
            ).fetchall()
            assert len(rows2) == n_before
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_goals_due_member_reminder_and_agent_check(self, tmp_path: Path) -> None:
        from fakes import FakeCoordinator

        coord = FakeCoordinator()
        app = _app(tmp_path, raw=_raw_models(tmp_path / "data"))
        app.coordinator_factory = lambda *a, **kw: coord
        await app.start()
        try:
            from CharTyr_MaiWork.maiwork import clock

            t = clock.now()
            mid = app.goals.create_member(
                G1, who_id="20002", who_name="阿柒", title="吃药",
                due_ts=t + 3600, remind_ts=t - 10,  # 已到期
            )
            gid_goal = app.goals.create_agent(G1, title="盯着铝价", body="", criteria=[], by_text="管理员 发起")
            with app.store.tx() as conn:
                conn.execute("UPDATE goals SET next_check_ts=? WHERE id=?", (t - 5, gid_goal))
            await app.run_loop_once()
            # 成员提醒：outbox 有 push_kind=reminder 的文本
            rows = app.store.read().execute(
                "SELECT payload FROM outbox WHERE key LIKE 'goal-remind:%'"
            ).fetchall()
            assert rows
            assert "@阿柒 提醒：吃药" in rows[0]["payload"]
            # 提醒过的不再提醒
            row = app.store.read().execute("SELECT remind_ts FROM goals WHERE id=?", (mid,)).fetchone()
            assert row["remind_ts"] is None
            # agent 目标：spawn check_goal
            for _ in range(30):
                if gid_goal in coord.check_calls:
                    break
                await asyncio.sleep(0.02)
            assert gid_goal in coord.check_calls
            # next_check_ts 推进到未来
            row = app.store.read().execute("SELECT next_check_ts FROM goals WHERE id=?", (gid_goal,)).fetchone()
            assert float(row["next_check_ts"]) > t
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_waiting_input_shelved_after_24h(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        await app.start()
        try:
            tid = app.tasks.create(G1, title="等回答的活", req="", criteria=[], source="test", status="queued")
            app.tasks.transition(tid, "running")
            app.tasks.transition(tid, "waiting_input", question="预算多少？", question_ts=1.0)
            from CharTyr_MaiWork.maiwork import clock

            t = clock.now()
            with app.store.tx() as conn:
                conn.execute("UPDATE tasks SET question_ts=? WHERE id=?", (t - 25 * 3600, tid))
            await app.run_loop_once()
            assert app.tasks.get(tid)["status"] == "shelved"
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_waiting_input_remind_once_after_6h(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        await app.start()
        try:
            tid = app.tasks.create(G1, title="等回答的活", req="", criteria=[], source="test", status="queued")
            app.tasks.transition(tid, "running")
            app.tasks.transition(tid, "waiting_input", question="预算多少？", question_ts=1.0)
            from CharTyr_MaiWork.maiwork import clock

            t = clock.now()
            with app.store.tx() as conn:
                conn.execute("UPDATE tasks SET question_ts=? WHERE id=?", (t - 7 * 3600, tid))
            await app.run_loop_once()
            rows = app.store.read().execute(
                "SELECT key, payload FROM outbox WHERE key=?", (f"task-wait-remind:{tid}",)
            ).fetchall()
            assert len(rows) == 1
            assert "还在等回答" in rows[0]["payload"]
            # key 去重：第二轮不再加新的
            await app.run_loop_once()
            rows = app.store.read().execute(
                "SELECT key FROM outbox WHERE key=?", (f"task-wait-remind:{tid}",)
            ).fetchall()
            assert len(rows) == 1
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_queued_task_not_spawned_when_models_unconfigured(self, tmp_path: Path) -> None:
        """模型没配好：后台巡检不派工，任务保持 queued、不报错、不发群消息（02 §12.1）。"""
        from fakes import FakeCoordinator

        coord = FakeCoordinator()
        app = _app(tmp_path)  # _raw 没有 [models] 节 → ready() = False
        app.coordinator_factory = lambda *a, **kw: coord
        await app.start()
        try:
            assert app.models.settings().ready() is False
            tid = app.tasks.create(G1, title="等模型的活", req="", criteria=[], source="test", status="queued")
            await app.run_loop_once()
            assert coord.run_calls == []
            assert app.tasks.get(tid)["status"] == "queued"
            # 零 outbox、零 error_reports
            rows = app.store.read().execute("SELECT COUNT(*) AS c FROM outbox").fetchone()
            assert int(rows["c"]) == 0
            rows = app.store.read().execute("SELECT COUNT(*) AS c FROM error_reports").fetchone()
            assert int(rows["c"]) == 0
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_real_coordinator_run_task_keeps_queued_when_unconfigured(self, tmp_path: Path) -> None:
        """真 coordinator：没配好时 run_task 直接返回——不开 attempt、不转 failed、不报群。"""
        app = _app(tmp_path)  # 真 coordinator
        await app.start()
        try:
            assert app.models.settings().ready() is False
            tid = app.tasks.create(G1, title="等模型的活", req="", criteria=[], source="test", status="queued")
            await app.coordinator.run_task(tid)
            t = app.tasks.get(tid)
            assert t["status"] == "queued"
            assert int(t["attempts"]) == 0
            rows = app.store.read().execute("SELECT COUNT(*) AS c FROM error_reports").fetchone()
            assert int(rows["c"]) == 0
            rows = app.store.read().execute("SELECT COUNT(*) AS c FROM outbox").fetchone()
            assert int(rows["c"]) == 0
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_goal_check_skipped_when_models_unconfigured(self, tmp_path: Path) -> None:
        """agent 目标到点检查：模型没配好时不 spawn check_goal，检查点不推进。"""
        from fakes import FakeCoordinator

        coord = FakeCoordinator()
        app = _app(tmp_path)  # 没 [models]
        app.coordinator_factory = lambda *a, **kw: coord
        await app.start()
        try:
            from CharTyr_MaiWork.maiwork import clock

            t = clock.now()
            gid_goal = app.goals.create_agent(G1, title="盯着铝价", body="", criteria=[], by_text="管理员 发起")
            with app.store.tx() as conn:
                conn.execute("UPDATE goals SET next_check_ts=? WHERE id=?", (t - 5, gid_goal))
            await app.run_loop_once()
            await asyncio.sleep(0.05)
            assert coord.check_calls == []
            row = app.store.read().execute("SELECT next_check_ts FROM goals WHERE id=?", (gid_goal,)).fetchone()
            assert float(row["next_check_ts"]) <= t  # 没被推进
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_m3_error_in_one_step_does_not_block_others(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """outbox.flush 炸了也不影响 goals.due 和任务派工。"""
        from fakes import FakeCoordinator

        coord = FakeCoordinator()
        app = _app(tmp_path, raw=_raw_models(tmp_path / "data"))
        app.coordinator_factory = lambda *a, **kw: coord
        await app.start()
        try:
            async def _boom(now):
                raise RuntimeError("炸")

            monkeypatch.setattr(app.outbox, "flush", _boom)
            tid = app.tasks.create(G1, title="照样要做的活", req="", criteria=[], source="test", status="queued")
            await app.run_loop_once()  # 不抛
            for _ in range(30):
                if tid in coord.run_calls:
                    break
                await asyncio.sleep(0.02)
            assert tid in coord.run_calls
        finally:
            await app.stop()


# ----------------------------------------------------------------------
# M2：后台循环接 topics / scheduler / feeds；长活不阻塞
# ----------------------------------------------------------------------


class TestM2Loop:
    @pytest.mark.asyncio
    async def test_loop_calls_topics_check_and_follow_up(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _app(tmp_path)
        await app.start()
        try:
            called: list[tuple[str, str]] = []

            async def _check(gid, now):
                called.append(("check", gid))
                return "skip:no_candidate"

            async def _follow(gid, now):
                called.append(("follow_up", gid))

            monkeypatch.setattr(app.topics, "check", _check)
            monkeypatch.setattr(app.topics, "follow_up", _follow)
            await app.run_loop_once()
            assert ("check", G1) in called and ("check", G2) in called
            assert ("follow_up", G1) in called and ("follow_up", G2) in called
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_topics_error_does_not_block_other_groups(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _app(tmp_path)
        await app.start()
        try:
            called: list[str] = []

            async def _check(gid, now):
                called.append(gid)
                if gid == G1:
                    raise RuntimeError("炸")
                return "skip:no_candidate"

            async def _follow(gid, now):
                return None

            monkeypatch.setattr(app.topics, "check", _check)
            monkeypatch.setattr(app.topics, "follow_up", _follow)
            await app.run_loop_once()  # 不抛
            assert called == [G1, G2]  # G1 炸了 G2 照样跑
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_news_due_spawns_background_job_and_does_not_block_loop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """scheduler.due 给了 news → spawn 成后台任务；它跑得久一点，run_loop_once 这一轮不等它。"""
        from fakes import FakeFeeds, FakeScheduler

        feeds = FakeFeeds()
        scheduler = FakeScheduler()
        feeds.delay_s = 0.25  # 250ms 的「长活」
        scheduler.due_map[G1] = [["news"]]
        app = _app(tmp_path)
        app.feeds_factory = lambda *a, **kw: feeds
        app.scheduler_factory = lambda *a, **kw: scheduler
        await app.start()
        try:
            t0 = asyncio.get_running_loop().time()
            await app.run_loop_once()
            took = asyncio.get_running_loop().time() - t0
            assert took < 0.2, f"长活不该堵住巡检（实际 {took:.2f}s）"
            # 后台任务已被登记，prepare_news 最终会被调到
            for _ in range(40):
                if G1 in feeds.prepare_calls:
                    break
                await asyncio.sleep(0.02)
            assert G1 in feeds.prepare_calls
            # 长活结束后 done 一定被记（成功失败都算做过）
            for _ in range(40):
                if any(c[0] == G1 and c[1] == "news" for c in scheduler.done_calls):
                    break
                await asyncio.sleep(0.02)
            assert any(c[0] == G1 and c[1] == "news" for c in scheduler.done_calls)
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_run_news_now_spawns_without_marking_slot(self, tmp_path: Path) -> None:
        """管理员「现在就备一批」：后台跑 prepare_news，但不记 scheduler.done——
        否则会把最近的时段（比如今晚 19:00）当成做过了而跳过。"""
        from fakes import FakeFeeds, FakeScheduler

        feeds = FakeFeeds()
        feeds.delay_s = 0.1
        scheduler = FakeScheduler()
        app = _app(tmp_path)
        app.feeds_factory = lambda *a, **kw: feeds
        app.scheduler_factory = lambda *a, **kw: scheduler
        await app.start()
        try:
            app._models_ready = lambda: False  # type: ignore[method-assign]
            assert app.run_news_now(G1)["started"] is False  # 模型没配好不跑
            app._models_ready = lambda: True  # type: ignore[method-assign]
            r = app.run_news_now(G1)
            assert r["started"] is True
            # 同一群正在跑：第二次不重复开（手动 / 定时都算）
            r2 = app.run_news_now(G1)
            assert r2["started"] is False and r2["reason"]
            for _ in range(50):
                if G1 in feeds.prepare_calls and not app._running_jobs:
                    break
                await asyncio.sleep(0.02)
            assert feeds.prepare_calls == [G1]
            assert not any(c[1] == "news" for c in scheduler.done_calls)
            # 非服务群不跑
            r3 = app.run_news_now("999999")
            assert r3["started"] is False
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_long_job_does_not_overlap_same_group_same_kind(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """同一群同一种长活同时只跑一个：不重复 spawn。"""
        from fakes import FakeFeeds, FakeScheduler

        feeds = FakeFeeds()
        scheduler = FakeScheduler()
        feeds.delay_s = 0.3
        # 连续两轮都 due news；第一轮的任务还没结束，第二轮不许再开
        scheduler.due_map[G1] = [["news"], ["news"]]
        app = _app(tmp_path)
        app.feeds_factory = lambda *a, **kw: feeds
        app.scheduler_factory = lambda *a, **kw: scheduler
        await app.start()
        try:
            await app.run_loop_once()
            await asyncio.sleep(0.05)  # 第一个还没跑完
            await app.run_loop_once()
            # 只 spawn 了一次
            assert feeds.prepare_calls.count(G1) == 1
            # 只 done 了一次（第二轮根本没跑）
            done_news = [c for c in scheduler.done_calls if c[0] == G1 and c[1] == "news"]
            assert len(done_news) <= 1
            await asyncio.sleep(0.4)  # 收干净
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_idea_due_routes_to_make_idea(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from fakes import FakeFeeds, FakeScheduler

        feeds = FakeFeeds()
        scheduler = FakeScheduler()
        scheduler.due_map[G2] = [["idea"]]
        app = _app(tmp_path)
        app.feeds_factory = lambda *a, **kw: feeds
        app.scheduler_factory = lambda *a, **kw: scheduler
        await app.start()
        try:
            await app.run_loop_once()
            for _ in range(40):
                if G2 in feeds.make_idea_calls:
                    break
                await asyncio.sleep(0.02)
            assert G2 in feeds.make_idea_calls
            assert G2 not in feeds.prepare_calls
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_goal_due_routes_to_proposer(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """scheduler 报 goal → 调 GoalProposer.propose（一天一次由它自己兜），并记 scheduler.done。"""
        from fakes import FakeFeeds, FakeScheduler

        calls: list[str] = []

        class _Proposer:
            async def propose(self, gid: str):
                calls.append(str(gid))
                return None

        feeds = FakeFeeds()
        scheduler = FakeScheduler()
        scheduler.due_map[G2] = [["goal"]]
        app = _app(tmp_path)
        app.feeds_factory = lambda *a, **kw: feeds
        app.scheduler_factory = lambda *a, **kw: scheduler
        await app.start()
        app.goal_proposer = _Proposer()
        try:
            await app.run_loop_once()
            for _ in range(40):
                if G2 in calls:
                    break
                await asyncio.sleep(0.02)
            assert G2 in calls
            assert G2 not in feeds.make_idea_calls
            assert any(c[0] == G2 and c[1] == "goal" for c in scheduler.done_calls)
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_goal_due_without_proposer_is_skipped(self, tmp_path: Path) -> None:
        """GoalProposer 没就位（模块缺）→ 这轮跳过，不炸。"""
        from fakes import FakeFeeds, FakeScheduler

        feeds = FakeFeeds()
        scheduler = FakeScheduler()
        scheduler.due_map[G1] = [["goal"]]
        app = _app(tmp_path)
        app.feeds_factory = lambda *a, **kw: feeds
        app.scheduler_factory = lambda *a, **kw: scheduler
        await app.start()
        app.goal_proposer = None
        try:
            await app.run_loop_once()
            await asyncio.sleep(0.05)
            assert feeds.make_idea_calls == []
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_feeds_error_is_logged_and_done_recorded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
        """长活炸了：不让后台循环跟着炸；scheduler.done 照样记（错过不补，docs/07 §10.5）。"""
        from fakes import FakeFeeds, FakeScheduler

        feeds = FakeFeeds()
        scheduler = FakeScheduler()
        feeds.errors.add(G1)
        scheduler.due_map[G1] = [["news"]]
        app = _app(tmp_path)
        app.feeds_factory = lambda *a, **kw: feeds
        app.scheduler_factory = lambda *a, **kw: scheduler
        await app.start()
        try:
            with caplog.at_level(logging.ERROR):
                await app.run_loop_once()
                for _ in range(40):
                    if any(c[0] == G1 and c[1] == "news" for c in scheduler.done_calls):
                        break
                    await asyncio.sleep(0.02)
            assert any(c[0] == G1 and c[1] == "news" for c in scheduler.done_calls)
            assert "长活" in caplog.text or "prepare_news" in caplog.text
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_pending_long_jobs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """stop 把还在跑的长活取消掉，不许泄漏。"""
        from fakes import FakeFeeds, FakeScheduler

        feeds = FakeFeeds()
        scheduler = FakeScheduler()
        feeds.delay_s = 5.0  # 跑不完的长活
        scheduler.due_map[G1] = [["news"]]
        app = _app(tmp_path)
        app.feeds_factory = lambda *a, **kw: feeds
        app.scheduler_factory = lambda *a, **kw: scheduler
        await app.start()
        await app.run_loop_once()
        assert len(app._bg_jobs) >= 1
        await app.stop()
        assert app._bg_jobs == set()
        assert app._running_jobs == set()

    @pytest.mark.asyncio
    async def test_scheduler_none_means_no_due_calls(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """scheduler / feeds 模块没就位 → 这轮就不巡检，照常画像 tick。"""
        profiles = FakeProfiles()
        app = _app(tmp_path)
        app.profiles_factory = lambda *a, **kw: profiles
        app.feeds_factory = lambda *a, **kw: None
        app.scheduler_factory = lambda *a, **kw: None
        await app.start()
        try:
            await app.run_loop_once()  # 不抛
            assert G1 in profiles.ticks and G2 in profiles.ticks
            assert app.feeds is None and app.scheduler is None
        finally:
            await app.stop()


# ----------------------------------------------------------------------
# railway.new 一次性 VM 实测的接线（docs/07 §11.1b）
# ----------------------------------------------------------------------


def _capture_feeds_kwargs() -> tuple[dict, Any]:
    """feeds_factory 用：把 Feeds 构造参数（含 verify_runner）记下来。"""
    from fakes import FakeFeeds

    captured: dict = {}

    def _factory(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return FakeFeeds()

    return captured, _factory


class TestRailwayWiring:
    @pytest.mark.asyncio
    async def test_railway_on_wires_verify_runner_and_vm_tools(self, tmp_path: Path) -> None:
        """railway=true：建 RailwayEnv、注册 vm 工具、Feeds 拿到非 None 的 verify_runner。"""
        from CharTyr_MaiWork.maiwork.console.views import settings_view

        captured, factory = _capture_feeds_kwargs()
        app = _app(tmp_path)  # 没写 [environments] → railway 默认 true
        app.feeds_factory = factory
        await app.start()
        try:
            assert app.railway is not None
            assert callable(captured.get("verify_runner")), "railway=true 必须给 Feeds 一个 verify_runner"
            names = {s["function"]["name"] for s in app.tools.specs("worker")}
            assert {"vm_run", "vm_put_file", "vm_read_file"} <= names
            # settings 视图的 health 里有 railway 这一项
            keys = {h["key"]: h for h in settings_view(app)["health"]}
            assert "railway" in keys
            assert keys["railway"]["state"] == "ok"
        finally:
            await app.stop()
        assert app.railway is None  # stop 收干净

    @pytest.mark.asyncio
    async def test_railway_off_no_runner_and_no_vm_tools(self, tmp_path: Path) -> None:
        """railway=false：不建 RailwayEnv、不注册 vm 工具、Feeds 的 verify_runner 是 None。"""
        from CharTyr_MaiWork.maiwork.console.views import settings_view

        captured, factory = _capture_feeds_kwargs()
        raw = _raw(tmp_path / "data")
        raw["environments"] = {"railway": False}
        app = _app(tmp_path, raw=raw)
        app.feeds_factory = factory
        await app.start()
        try:
            assert app.railway is None
            assert "verify_runner" in captured and captured["verify_runner"] is None
            names = {s["function"]["name"] for s in app.tools.specs("worker")}
            assert not (names & {"vm_run", "vm_put_file", "vm_read_file"})
            keys = {h["key"]: h for h in settings_view(app)["health"]}
            assert "railway" in keys
            assert keys["railway"]["state"] == "off"
        finally:
            await app.stop()


# ----------------------------------------------------------------------
# 主循环 / 消息链里直接 await 的模型调用：必须 retries=1
# （默认 5 次重试 × 10 秒间隔会把后台循环 / 消息处理卡几分钟）
# ----------------------------------------------------------------------


class _RetryCapturingModels:
    """假 Models：chat 记 kwargs 后直接给一个可解析的提醒 JSON。"""

    class _Settings:
        def ready(self) -> bool:
            return True

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def settings(self):
        return self._Settings()

    async def close(self) -> None:
        return None

    async def chat(self, role, messages, **kwargs):
        self.calls.append({"role": role, "kwargs": kwargs})

        class _R:
            text = '{"ok": true, "title": "买菜", "due_ts": 4102444800.0}'
            tool_calls: list = []
            model = "m"
            prompt_tokens = 0
            completion_tokens = 0
            raw_message: dict = {}

        return _R()


class TestDirectAwaitChatUsesRetries1:
    @pytest.mark.asyncio
    async def test_reminder_parse_uses_retries_1(self, tmp_path: Path) -> None:
        """@ 设提醒的慢路径解析在 intake 的消息处理链上直接 await，不能等 10 秒×5。"""
        app = _app(tmp_path)
        fake = _RetryCapturingModels()
        await app.start()
        try:
            app.models = fake
            await app.on_reminder(G1, {
                "user_id": "10001",
                "user_name": "阿一",
                "text": "提醒我明天买菜",
                "message_id": "m-1",
            })
            assert fake.calls, "on_reminder 应该调了模型"
            assert all(c["kwargs"].get("retries") == 1 for c in fake.calls), fake.calls
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_topics_opener_chat_uses_retries_1(self) -> None:
        """topics.py 里所有 models.chat 调用都必须显式传 retries=1（开场白在主循环里直接 await）。

        用 AST 核查真实源码（不是 regex 猜）：开场白走 run_loop_once → _topics_round →
        topics.check → models.chat，中间没有 spawn，是主循环路径上唯一要等的模型调用。
        """
        import ast
        import inspect

        from CharTyr_MaiWork.maiwork import topics as topics_mod

        tree = ast.parse(inspect.getsource(topics_mod))
        chat_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "chat"
        ]
        assert chat_calls, "topics.py 里一个 chat 调用都没找到（结构变了？）"
        for call in (c.value for c in chat_calls):
            retries_kw = next((k for k in call.keywords if k.arg == "retries"), None)
            assert retries_kw is not None, f"第 {call.lineno} 行的 chat 没传 retries"
            assert isinstance(retries_kw.value, ast.Constant) and retries_kw.value.value == 1, (
                f"第 {call.lineno} 行的 chat retries 必须是 1"
            )


class TestAdminChatWiring:
    @pytest.mark.asyncio
    async def test_admin_chat_started_and_stopped_with_app(self, tmp_path: Path) -> None:
        app = _app(tmp_path, raw=_raw(tmp_path / "admin-chat", listen=f"127.0.0.1:{_free_port()}"))
        await app.start()
        try:
            assert app.admin_pending is not None
            assert app.admin_chat is not None
            assert app.tools.specs("admin")
            assert app.tools.specs("worker", ["send_group_message"]) == []
            c = app.admin_chat.create(G1)
            assert c["group_id"] == G1
        finally:
            await app.stop()
        assert app.admin_chat is None and app.admin_pending is None
