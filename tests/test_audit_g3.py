"""G3 回归测试：群友视图不泄露内部信息。

- tasks.detail_view(admin=False)：去掉 tokens、workspace、source、request_id、requester_id；
  管理员版照常给。
- console server 任务详情群友分支：再兜一层（即使别处给了也剥掉）。
- views.group_view(admin=False)：去掉 workspace；pulse.sleep 保留（前端要画睡觉时段）。
"""

from __future__ import annotations

from pathlib import Path

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks
from CharTyr_MaiWork.maiwork.console import views

GID = "900000001"
MEMBER_HIDDEN_TASK_KEYS = ("tokens", "workspace", "source", "request_id", "requester_id")


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "t.db")
    store.migrate()
    return store


def _seed_task(store: Store) -> str:
    now = clock.now()
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tasks (id, group_id, workspace, source, request_id, requester_id,"
            " requester_name, title, status, tokens, created, updated)"
            " VALUES ('T-1', ?, 'secret-ws', 'request', 'R-9', '123456',"
            " '阿明', '做个东西', 'running', 4242, ?, ?)",
            (GID, now, now),
        )
    return "T-1"


class TestDetailViewHidesInternalsFromMembers:
    def test_member_view_drops_sensitive_fields(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        settings, _ = load_settings({"groups": {"serve": [{"group": f"qq:{GID}"}]}})
        tasks = Tasks(store, lambda: settings)
        tid = _seed_task(store)
        detail = tasks.detail_view(tid, admin=False)
        for key in MEMBER_HIDDEN_TASK_KEYS:
            assert key not in detail, f"群友看得见 {key} 了"
        # 常规字段还在（前端要用）
        for key in ("id", "title", "status", "req", "steps"):
            assert key in detail, f"该有的 {key} 没了"

    def test_admin_view_keeps_all_fields(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        settings, _ = load_settings({"groups": {"serve": [{"group": f"qq:{GID}"}]}})
        tasks = Tasks(store, lambda: settings)
        tid = _seed_task(store)
        detail = tasks.detail_view(tid, admin=True)
        assert detail["tokens"] == 4242
        assert detail["workspace"] == "secret-ws"
        assert detail["source"] == "request"
        assert detail["request_id"] == "R-9"
        # requester_id 管理也不过（管理员能看到），只确认字段没被误删


class _Svc:
    """group_view 需要的最小服务对象。"""

    def __init__(self, tmp_path: Path) -> None:
        self.store = Store(tmp_path / "v.db")
        self.store.migrate()
        self._settings, _ = load_settings(
            {"groups": {"serve": [{"group": f"qq:{GID}", "workspace": "ws-internal"}]}}
        )
        self.signals = None
        self.profiles = None
        self.tasks = None
        self.approvals = None
        self.goals = None
        self.delivery = None
        self.feeds = None
        self.topics = None
        self.scheduler = None
        self.models = None
        self.host = None

    def get_settings(self):
        return self._settings

    class _Signals:
        def last_ts(self, gid):
            return 0.0


class TestGroupViewHidesWorkspaceFromMembers:
    def _svc(self, tmp_path: Path) -> _Svc:
        svc = _Svc.__new__(_Svc)
        svc.store = Store(tmp_path / "v.db")
        svc.store.migrate()
        svc._settings, _ = load_settings(
            {"groups": {"serve": [{"group": f"qq:{GID}", "workspace": "ws-internal"}]}})
        class _Sig:
            def last_ts(self, gid):
                return 0.0
        svc.signals = _Sig()
        svc.profiles = None
        svc.tasks = None
        svc.approvals = None
        svc.goals = None
        svc.delivery = None
        svc.feeds = None
        svc.topics = None
        svc.scheduler = None
        svc.models = None
        svc.host = None
        return svc

    def test_member_group_view_has_no_workspace_but_keeps_sleep(self, tmp_path: Path) -> None:
        svc = self._svc(tmp_path)
        out = views.group_view(svc, GID, admin=False)
        assert "workspace" not in out
        assert "ws-internal" not in str(out)
        # sleep 保留（前端要画睡觉时段）
        assert out["pulse"]["sleep"]

    def test_admin_group_view_keeps_workspace(self, tmp_path: Path) -> None:
        svc = self._svc(tmp_path)
        out = views.group_view(svc, GID, admin=True)
        assert out["workspace"] == "ws-internal"
