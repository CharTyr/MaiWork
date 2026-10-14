"""管理员复核过期 @：必须留痕、去重、强制待批，不向群发送。"""
import pytest

import test_pending_ask_recovery as recovery
from test_pending_ask_recovery import GID, OTHER, T0, _Graph, _msg_dict

# Re-export shared pytest fixtures without unused-import lint exceptions.
now = recovery.now
store = recovery.store
from fakes import FakeHost, FakeModelsQueue


def _graph(store):
    g = _Graph(store, FakeHost([]), FakeModelsQueue(replies=[]))
    g.asks.record_pending(GID, _msg_dict("old", T0 - 49 * 3600), "jev_无答案")
    g.asks.mark_stale(GID)
    return g


def test_review_list_only_served_review_rows(store, now):
    g = _graph(store)
    g.asks.record_pending(GID, _msg_dict("new"), "r")
    assert [x["message_id"] for x in g.asks.review_view(GID)] == ["old"]
    assert g.asks.review_view(OTHER) == []


@pytest.mark.parametrize("kind", ["task", "goal"])
def test_review_creates_manual_request_once(store, now, kind):
    g = _graph(store)
    res = g.asks.review_action(GID, "old", "request", approvals=g.approvals,
                               kind=kind, by="网页管理员")
    assert res["request_id"]
    req = g.approvals.pending_view(GID)[0]
    assert req["id"] == res["request_id"]
    assert store.read().execute("SELECT kind FROM requests WHERE id=?", (res["request_id"],)).fetchone()[0] == kind
    assert g.asks.review_action(GID, "old", "request", approvals=g.approvals,
                                kind=kind, by="网页管理员") == res
    assert len(g.approvals.pending_view(GID)) == 1
    assert g.asks.get(GID, "old")["state"] == "completed"
    assert g.asks.review_view(GID) == []
    assert store.read().execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_ignore_preserves_original_and_is_idempotent(store, now):
    g = _graph(store)
    g.asks.review_action(GID, "old", "ignore", by="网页管理员")
    g.asks.review_action(GID, "old", "ignore", by="网页管理员")
    row = g.asks.get(GID, "old")
    assert row["state"] == "none" and row["handled"] == 1
    assert row["text"] and row["result_kind"] == "ignored"
    assert len(g.approvals.pending_view(GID)) == 0
    assert store.read().execute("SELECT COUNT(*) FROM events WHERE kind='ask.review'").fetchone()[0] == 1


def test_review_conflict_and_missing_do_not_mutate(store, now):
    g = _graph(store)
    with pytest.raises(KeyError):
        g.asks.review_action(OTHER, "old", "ignore", by="admin")
    with pytest.raises(ValueError):
        g.asks.review_action(GID, "old", "request", approvals=g.approvals, kind="reminder", by="admin")
    g.asks.review_action(GID, "old", "ignore", by="admin")
    with pytest.raises(ValueError):
        g.asks.review_action(GID, "old", "request", approvals=g.approvals, kind="task", by="admin")


def test_failed_create_keeps_review_visible(store, now):
    g = _graph(store)
    class Broken:
        def create(self, *args, **kwargs):
            raise RuntimeError("unavailable")
    with pytest.raises(RuntimeError):
        g.asks.review_action(GID, "old", "request", approvals=Broken(), kind="task", by="admin")
    assert len(g.asks.review_view(GID)) == 1


def test_review_close_failure_reuses_created_request(store, now, monkeypatch):
    g = _graph(store)
    event = store.event
    def fail_review_event(conn, kind, **kwargs):
        if kind == "ask.review":
            raise RuntimeError("audit unavailable")
        return event(conn, kind, **kwargs)
    monkeypatch.setattr(store, "event", fail_review_event)
    with pytest.raises(RuntimeError):
        g.asks.review_action(GID, "old", "request", approvals=g.approvals, kind="task", by="admin")
    assert g.asks.get(GID, "old")["state"] == "review"
    assert len(g.approvals.pending_view(GID)) == 1
    rid = g.approvals.pending_view(GID)[0]["id"]
    monkeypatch.setattr(store, "event", event)
    res = g.asks.review_action(GID, "old", "request", approvals=g.approvals, kind="task", by="admin")
    assert res["request_id"] == rid
    assert len(g.approvals.pending_view(GID)) == 1
    assert g.asks.get(GID, "old")["state"] == "completed"


def test_review_request_forces_manual_even_exempt_group(store, now):
    g = _graph(store)
    calls = []
    create = g.approvals.create
    def capture(*args, **kwargs):
        calls.append(kwargs)
        return create(*args, **kwargs)
    g.approvals.create = capture
    g.asks.review_action(GID, "old", "request", approvals=g.approvals, kind="task", by="admin")
    assert calls[0]["force_manual"] is True
    assert calls[0]["screen"] is False
