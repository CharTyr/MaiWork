"""M1 回归测试：kind=file 且 status=uncertain 的项不自动重发。

群文件上传不幂等：超时的上传可能其实已经传上去了。约定：
- outbox.retry 对 file+uncertain 默认拒绝；force=True（管理员已去群里确认过）才允许；
- console server 的 redeliver 跳过这类项，并在返回里带 warning
  「群文件可能已发出，请先到群里确认」；
- 任务详情的交付记录里这类项带同一句提示。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
from CharTyr_MaiWork.maiwork.outbox import Delivery, Outbox
from CharTyr_MaiWork.maiwork.store import Store

GID = "900000001"


def _make(tmp_path: Path):
    store = Store(tmp_path / "t.db")
    store.migrate()
    settings, _ = load_settings(
        {
            "groups": {"serve": [{"group": f"qq:{GID}"}]},
            "environments": {"workspace_root": str(tmp_path / "ws")},
        }
    )
    ob = Outbox(store, None, Pushes(store, lambda: settings), Mentions(store, lambda: settings), lambda: settings)
    return store, settings, ob


def _seed_uncertain_file(store: Store, key: str = "task:T-1:deliver") -> int:
    now = clock.now()
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tasks (id, group_id, workspace, title, status, created, updated)"
            " VALUES ('T-1', ?, 'ws', 'T', 'completed', 0, 0)",
            (GID,),
        )
        cur = conn.execute(
            "INSERT INTO outbox (key, group_id, kind, payload, status, attempts, task_id, created, updated)"
            " VALUES (?, ?, 'file', '{\"path\": \"/x/a.zip\"}', 'uncertain', 1, 'T-1', ?, ?)",
            (key, GID, now, now),
        )
        return int(cur.lastrowid)


class TestRetryForce:
    def test_file_uncertain_retry_rejected_by_default(self, tmp_path: Path) -> None:
        store, _, ob = _make(tmp_path)
        oid = _seed_uncertain_file(store)
        with pytest.raises(ValueError, match="确认|群里"):
            ob.retry(oid)
        row = store.read().execute("SELECT status FROM outbox WHERE id=?", (oid,)).fetchone()
        assert row["status"] == "uncertain"  # 原地不动

    def test_file_uncertain_retry_force_allowed(self, tmp_path: Path) -> None:
        store, _, ob = _make(tmp_path)
        oid = _seed_uncertain_file(store)
        ob.retry(oid, force=True)
        row = store.read().execute("SELECT status FROM outbox WHERE id=?", (oid,)).fetchone()
        assert row["status"] == "pending"

    def test_text_uncertain_retry_without_force_ok(self, tmp_path: Path) -> None:
        """非 file 的 uncertain 照旧不需 force。"""
        store, _, ob = _make(tmp_path)
        now = clock.now()
        with store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO outbox (key, group_id, kind, payload, status, attempts, created, updated)"
                " VALUES ('k-t', ?, 'text', '{}', 'uncertain', 1, ?, ?)",
                (GID, now, now),
            )
            oid = int(cur.lastrowid)
        ob.retry(oid)
        row = store.read().execute("SELECT status FROM outbox WHERE id=?", (oid,)).fetchone()
        assert row["status"] == "pending"


class TestServerRedeliverSkips:
    def test_redeliver_skips_file_uncertain_with_warning(self, tmp_path: Path) -> None:
        """server._redeliver_failed：file+uncertain 跳过，返回带 warning；failed 照常 retry。"""
        store, _, ob = _make(tmp_path)
        _seed_uncertain_file(store)
        now = clock.now()
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO outbox (key, group_id, kind, payload, status, attempts, task_id, created, updated)"
                " VALUES ('task:T-1:xxx', ?, 'herenow', '{}', 'failed', 1, 'T-1', ?, ?)",
                (GID, now, now),
            )

        class _Svc:
            pass

        svc = _Svc()
        svc.outbox = ob
        svc.store = store
        from CharTyr_MaiWork.maiwork.console.server import ConsoleServer

        result = ConsoleServer._redeliver_failed(svc, "T-1")
        assert isinstance(result, dict)
        assert result["retried"] == 1  # herenow failed 的重试了
        assert result["skipped"] == 1  # file uncertain 跳过了
        assert "群里确认" in result["warning"] or "群文件可能已发出" in result["warning"]
        rows = {r["key"]: r["status"] for r in store.read().execute("SELECT key, status FROM outbox").fetchall()}
        assert rows["task:T-1:deliver"] == "uncertain"  # 没被动
        assert rows["task:T-1:xxx"] == "pending"  # failed 的被重试了


class TestDeliveryRecordWarning:
    def test_delivery_records_file_uncertain_shows_hint(self, tmp_path: Path) -> None:
        store, settings, ob = _make(tmp_path)
        d = Delivery(store, ob)
        _seed_uncertain_file(store)
        records = d.delivery_records("T-1")
        file_rec = [r for r in records if r["raw_kind"] == "file"]
        assert file_rec
        assert "群文件可能已发出" in file_rec[0]["error"]
