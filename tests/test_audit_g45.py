"""G4/G5 回归测试。

G4：planner 备忘钩子不能只看内存信号认服务群——信号是历史残留，热更新删群后
可能还没清；Mentions.render 用 groups 表 session_id 查到群就渲染，也不查
is_served。修复：两处都用 settings.is_served 复核；update_config 时清掉
不再服务的群的信号。

G5：收消息钩子等 Jev 的实际时间 = min(settings.jev.timeout_ms, 1200) 毫秒；
配置解析时 jev.timeout_ms 夹到 [200, 1200]，超出记问题。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork import clock
from CharTyr_MaiWork.app import MaiWorkApp
from CharTyr_MaiWork.config import load_settings
from CharTyr_MaiWork.delivery import Mentions
from CharTyr_MaiWork.intake import Intake, Signals
from CharTyr_MaiWork.store import Store

G1 = "900000001"
G2 = "123456789"


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "t.db")
    store.migrate()
    return store


class _FakeJev:
    def __init__(self) -> None:
        self.seen_timeout_ms: list[int] = []

    def available(self) -> bool:
        return True

    async def ask(self, state, questions, *, purpose, group_id, timeout_ms=None):
        self.seen_timeout_ms.append(int(timeout_ms or 0))
        return {"kind": ("none", 0.9, 0.9)}


def _at_message(gid: str) -> dict:
    return {
        "message": {
            "message_id": "m-1",
            "session_id": "sess-g2",
            "timestamp": clock.now(),
            "is_at": True,
            "processed_plain_text": "在吗",
            "message_info": {
                "group_info": {"group_id": gid},
                "user_info": {"user_id": "10001", "user_nickname": "阿明"},
            },
        }
    }


class TestG5JevTimeoutClamp:
    def test_config_clamps_jev_timeout(self) -> None:
        s, problems = load_settings({"jev": {"timeout_ms": 5000}})
        assert s.jev.timeout_ms == 1200
        assert any("timeout_ms" in p for p in problems)

    def test_config_clamps_jev_timeout_low(self) -> None:
        s, problems = load_settings({"jev": {"timeout_ms": 10}})
        assert s.jev.timeout_ms == 200
        assert any("timeout_ms" in p for p in problems)

    def test_config_keeps_reasonable_timeout(self) -> None:
        s, problems = load_settings({"jev": {"timeout_ms": 800}})
        assert s.jev.timeout_ms == 800
        assert not any("timeout_ms" in p for p in problems)

    @pytest.mark.asyncio
    async def test_intake_wait_uses_clamped_value(self, tmp_path: Path) -> None:
        """钩子里等 Jev 传下去的 timeout_ms ≤ 1200（即使配置还没夹过）。"""
        store = _store(tmp_path)
        settings, _ = load_settings({
            "groups": {"serve": [{"group": f"qq:{G1}"}]},
            "jev": {"timeout_ms": 1200},
        })
        jev = _FakeJev()
        intake = Intake(
            lambda: settings, Signals(), jev=jev, store=store,
            spawn=lambda c: None,
        )
        out = await intake.handle(_at_message(G1))
        assert out == {"action": "continue"}
        assert jev.seen_timeout_ms and all(t <= 1200 for t in jev.seen_timeout_ms)


class TestG4ServedGroupChecks:
    def test_mentions_render_non_served_group_returns_none(self, tmp_path: Path) -> None:
        """groups 表里还留着旧映射（删群前收的），但配置已不服务 → render None。"""
        store = _store(tmp_path)
        # 配置只服务 G1；groups 表里 G2 有 session 映射和备忘
        settings, _ = load_settings({"groups": {"serve": [{"group": f"qq:{G1}"}]}})
        mentions = Mentions(store, lambda: settings)
        now = clock.now()
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO groups (group_id, session_id) VALUES (?, ?)", (G2, "sess-g2")
            )
            conn.execute(
                "INSERT INTO mentions (group_id, key, text, expires_ts, turns_left, created)"
                " VALUES (?, 'k', '残留备忘', ?, 5, ?)",
                (G2, now + 3600, now),
            )
        assert mentions.render("sess-g2") is None

    def test_mentions_render_served_group_works(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        settings, _ = load_settings({"groups": {"serve": [{"group": f"qq:{G1}"}]}})
        mentions = Mentions(store, lambda: settings)
        now = clock.now()
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO groups (group_id, session_id) VALUES (?, ?)", (G1, "sess-g1")
            )
            conn.execute(
                "INSERT INTO mentions (group_id, key, text, expires_ts, turns_left, created)"
                " VALUES (?, 'k', '正常备忘', ?, 5, ?)",
                (G1, now + 3600, now),
            )
        text = mentions.render("sess-g1")
        assert text is not None and "正常备忘" in text

    @pytest.mark.asyncio
    async def test_planner_hook_rechecks_is_served(self, tmp_path: Path) -> None:
        """内存信号里有 G2（删群前收的）→ 钩子里还要再查 is_served，不认就 continue。"""
        raw = {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{G1}"}]},
            "console": {"listen": f"127.0.0.1:{_port()}", "password": "pw"},
            "storage": {"data_dir": str(tmp_path / "data")},
        }
        app = MaiWorkApp(FakeCtx({}), raw, plugin_dir=Path(__file__).resolve().parents[1])
        app.profiles_cls = FakeProfiles
        await app.start()
        try:
            # 手动往里塞一个 G2 的信号（等价于删群前收到的残留）
            app.signals.mark(G2, "sess-g2", clock.now())
            called = []
            if app.mentions is not None:
                orig_inject = app.mentions.inject
                app.mentions.inject = lambda kw: called.append(kw) or None  # type: ignore[assignment]
                out = app.on_planner_before_request({"session_id": "sess-g2", "items": []})
                assert out == {"action": "continue"}
                assert called == []  # 根本没走到 inject
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_update_config_drops_unserved_signals(self, tmp_path: Path) -> None:
        """热更新删群后，G2 的内存信号被清掉（planner 钩子凭它认群）。"""
        raw = {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{G1}"}, {"group": f"qq:{G2}"}]},
            "console": {"listen": f"127.0.0.1:{_port()}", "password": "pw"},
            "storage": {"data_dir": str(tmp_path / "data")},
        }
        app = MaiWorkApp(FakeCtx({}), raw, plugin_dir=Path(__file__).resolve().parents[1])
        app.profiles_cls = FakeProfiles
        await app.start()
        try:
            app.signals.mark(G1, "sess-g1", clock.now())
            app.signals.mark(G2, "sess-g2", clock.now())
            raw2 = dict(raw)
            raw2["groups"] = {"serve": [{"group": f"qq:{G1}"}]}
            await app.update_config(raw2)
            signals = getattr(app.signals, "_map", {})
            assert G2 not in signals
            assert G1 in signals
        finally:
            await app.stop()


def _free_port() -> int:
    """挑一个本机空闲端口：测试别依赖 18650 空着（用户可能正开着 SSH 隧道看网页）。"""
    import socket as _s
    with _s.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


_PORT: list[int] = []


def _port() -> int:
    """整个测试文件只挑一次：同一测试里前后两份配置端口要一样，不然会被当成「改了网页地址」重启。"""
    if not _PORT:
        _PORT.append(_free_port())
    return _PORT[0]
