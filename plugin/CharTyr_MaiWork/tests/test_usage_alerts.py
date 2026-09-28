"""usage_alerts 单元测试（docs/02 §7.2）：超阈值只记一条提醒给网页看。

要求：
- alert_daily_tokens=0 / alert_task_tokens=0 → 永远不提醒；
- 超日阈值出一条第 daily，同一天再跑不重复；
- 单任务超阈值出第 task 一条，再跑不重复；
- 提醒只落在 kv（settings 视图的 usage.alerts 读它），
  绝不往发件箱塞东西（不往群里发），也不暂停任务。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork import clock, usage_alerts
from CharTyr_MaiWork.app import MaiWorkApp
from CharTyr_MaiWork.config import Settings, load_settings
from CharTyr_MaiWork.console.views import settings_view
from CharTyr_MaiWork.store import Store

G1 = "900000001"
G2 = "123456789"
# 2026-01-02 12:00 北京时间（避开睡觉时段，循环里的排程不会因此额外动作）
TODAY_TS = datetime(2026, 1, 2, 12, 0, tzinfo=clock.BJ).timestamp()
TODAY = "2026-01-02"


# ----------------------------------------------------------------------
# 造数据的小工具
# ----------------------------------------------------------------------


def _settings(*, daily: int = 0, task: int = 0) -> Settings:
    settings, problems = load_settings(
        {
            "groups": {"serve": [{"group": f"qq:{G1}"}]},
            "usage": {"alert_daily_tokens": daily, "alert_task_tokens": task},
        }
    )
    assert not problems, problems
    assert settings.usage.alert_daily_tokens == daily
    assert settings.usage.alert_task_tokens == task
    return settings


def _getter(settings: Settings):
    return lambda: settings


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "t.db")
    store.migrate()
    return store


def _usage(
    store: Store,
    group_id: str,
    tokens: int,
    *,
    role: str = "main",
    day: str = TODAY,
    ts: float = TODAY_TS,
) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO usage (ts, day, role, model, purpose, group_id, task_id,"
            " prompt_tokens, completion_tokens, ok, ms, error)"
            " VALUES (?, ?, ?, 'm', '', ?, '', ?, 0, 1, 0, '')",
            (ts, day, role, group_id, tokens),
        )


def _task(store: Store, task_id: str, *, title: str = "修键盘", tokens: int = 0, group_id: str = G1) -> str:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tasks (id, group_id, workspace, title, status, tokens, created, updated)"
            " VALUES (?, ?, 'ws', ?, 'completed', ?, ?, ?)",
            (task_id, group_id, title, tokens, TODAY_TS, TODAY_TS),
        )
    return task_id


def _outbox_rows(app: MaiWorkApp) -> list[tuple]:
    rows = app.store.read().execute("SELECT id, kind, group_id, payload FROM outbox ORDER BY id").fetchall()
    return [tuple(r) for r in rows]


# ----------------------------------------------------------------------
# check()
# ----------------------------------------------------------------------


class TestCheckDaily:
    def test_threshold_zero_never_alerts(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _usage(store, G1, 99_000_000)
        settings = _settings(daily=0)
        assert usage_alerts.check(store, _getter(settings), TODAY_TS) == []
        assert store.kv_get(f"usage.alerted.{G1}.{TODAY}") is None

    def test_not_over_threshold_no_alert(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _usage(store, G1, 20_000)
        settings = _settings(daily=20_000)  # 正好等于阈值不算「超」
        assert usage_alerts.check(store, _getter(settings), TODAY_TS) == []

    def test_main_plus_worker_over_threshold_alerts_once(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _usage(store, G1, 20_000, role="main")
        _usage(store, G1, 10_000, role="worker")
        settings = _settings(daily=20_000)
        out = usage_alerts.check(store, _getter(settings), TODAY_TS)
        assert len(out) == 1
        alert = out[0]
        assert alert["group_id"] == G1
        assert alert["kind"] == "daily"
        assert "3 万" in alert["text"]
        assert "2 万" in alert["text"]
        # 同一天再跑：不重复
        assert usage_alerts.check(store, _getter(settings), TODAY_TS) == []

    def test_yesterday_usage_not_counted(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _usage(store, G1, 99_000, day="2026-01-01", ts=TODAY_TS - 86400)
        settings = _settings(daily=10_000)
        assert usage_alerts.check(store, _getter(settings), TODAY_TS) == []

    def test_unserved_group_ignored(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _usage(store, G2, 99_000)
        settings = _settings(daily=10_000)
        assert usage_alerts.check(store, _getter(settings), TODAY_TS) == []


class TestCheckTask:
    def test_task_over_threshold_alerts_once(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _task(store, "T-7", title="修键盘", tokens=50_000)
        settings = _settings(task=30_000)
        out = usage_alerts.check(store, _getter(settings), TODAY_TS)
        assert len(out) == 1
        alert = out[0]
        assert alert["group_id"] == G1
        assert alert["kind"] == "task"
        assert "T-7" in alert["text"]
        assert "修键盘" in alert["text"]
        assert "5 万" in alert["text"]
        assert usage_alerts.check(store, _getter(settings), TODAY_TS) == []

    def test_task_not_over_threshold_no_alert(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _task(store, "T-8", tokens=30_000)
        settings = _settings(task=30_000)
        assert usage_alerts.check(store, _getter(settings), TODAY_TS) == []

    def test_task_in_unserved_group_ignored(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _task(store, "T-9", tokens=99_000, group_id=G2)
        settings = _settings(task=10_000)
        assert usage_alerts.check(store, _getter(settings), TODAY_TS) == []


class TestTodayAlerts:
    """网页读的就是这个：只给今天产生的提醒。"""

    def test_lists_today_only(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _usage(store, G1, 50_000)
        settings = _settings(daily=10_000)
        usage_alerts.check(store, _getter(settings), TODAY_TS)
        alerts = usage_alerts.today_alerts(store, TODAY_TS)
        assert len(alerts) == 1
        assert set(alerts[0]) == {"group_id", "kind", "text", "ts"}
        assert alerts[0]["group_id"] == G1
        assert alerts[0]["kind"] == "daily"
        assert alerts[0]["ts"] == TODAY_TS
        # 到了第二天，昨天的提醒不再列出来
        assert usage_alerts.today_alerts(store, TODAY_TS + 86400) == []


# ----------------------------------------------------------------------
# app 接线 + 网页：只标出，不往群里发
# ----------------------------------------------------------------------


def _raw(data_dir: Path) -> dict:
    return {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}"}]},
        "console": {"listen": f"127.0.0.1:{_port()}", "password": "pw-用量提醒"},
        "storage": {"data_dir": str(data_dir)},
        "usage": {"alert_daily_tokens": 10_000, "alert_task_tokens": 20_000},
    }


def _app(tmp_path: Path) -> MaiWorkApp:
    app = MaiWorkApp(
        FakeCtx({}),
        _raw(tmp_path / "data"),
        plugin_dir=Path(__file__).resolve().parents[1],
    )
    app.profiles_cls = FakeProfiles
    return app


class TestWebOnly:
    @pytest.mark.asyncio
    async def test_loop_records_alert_for_web_view(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(clock, "now", lambda: TODAY_TS)
        app = _app(tmp_path)
        await app.start()
        try:
            _usage(app.store, G1, 30_000)
            await app.run_loop_once()
            alerts = settings_view(app)["usage"]["alerts"]
            assert len(alerts) == 1
            assert set(alerts[0]) == {"group_id", "kind", "text", "ts"}
            assert alerts[0]["group_id"] == G1
            assert alerts[0]["kind"] == "daily"
            assert "3 万" in alerts[0]["text"]
            # 再跑一轮不重复
            await app.run_loop_once()
            assert len(settings_view(app)["usage"]["alerts"]) == 1
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_alert_round_never_enqueues_to_outbox(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(clock, "now", lambda: TODAY_TS)
        app = _app(tmp_path)
        await app.start()
        try:
            _usage(app.store, G1, 30_000)
            _task(app.store, "T-3", title="修键盘", tokens=90_000)
            before = _outbox_rows(app)
            app._usage_alert_round(TODAY_TS)
            after = _outbox_rows(app)
            assert after == before, "用量提醒只该在网页标出，不该往发件箱塞任何东西"
            # 提醒确实产生了（kv 里两条：daily + task）
            kinds = {a["kind"] for a in usage_alerts.today_alerts(app.store, TODAY_TS)}
            assert kinds == {"daily", "task"}
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
