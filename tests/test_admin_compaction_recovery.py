"""Recovery and coverage invariants use SQLite and scripted models only."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import compaction
from CharTyr_MaiWork.maiwork.admin_chat import AdminChat, TOOL_CONTENT_MAX
from test_admin_chat import G1, _Models, _Svc, _call, _res


def test_summary_survives_more_than_thirty_new_rows(tmp_path: Path) -> None:
    svc = _Svc(tmp_path)
    chat = AdminChat(svc)
    cid = int(chat.create(group_id=G1)["id"])
    old = chat._add_msg(cid, "user", "old material")
    chat._add_msg(cid, "assistant", "saved summary", meta={"kind": "summary", "msg_to": old})
    for i in range(45):
        chat._add_msg(cid, "assistant", f"new {i}")
    assert chat._latest_summary_row(cid) is not None
    history = chat._history(cid)
    assert history[0]["content"] == "saved summary"
    assert not any(m.get("content") == "old material" for m in history)


@pytest.mark.asyncio
async def test_manual_summary_keeps_latest_typed_requirement(tmp_path: Path) -> None:
    svc = _Svc(tmp_path, models=_Models(script=[_res("model forgot the request")]))
    chat = AdminChat(svc)
    cid = int(chat.create(group_id=G1)["id"])
    chat._add_msg(cid, "user", "Keep all filenames ASCII and use three columns")
    chat._add_msg(cid, "assistant", "working")
    await chat.compact(cid)
    history = chat._history(cid)
    assert any("Keep all filenames ASCII and use three columns" in str(m.get("content")) for m in history)


@pytest.mark.asyncio
async def test_tool_storage_preserves_full_masked_body(tmp_path: Path) -> None:
    from CharTyr_MaiWork.maiwork.tools import Tool, ToolResult

    svc = _Svc(tmp_path)
    body = "A" * TOOL_CONTENT_MAX + "MIDDLE_RECOVERABLE" + "B" * 8000

    async def handler(ctx, args):
        return ToolResult(ok=True, output=body)

    svc.tools.register(Tool(name="large_read", description="read", parameters={"type": "object"}, roles=frozenset({"admin"}), handler=handler))
    chat = AdminChat(svc)
    cid = int(chat.create(group_id=G1)["id"])
    call = _call("large_read", {})
    mid = chat._add_msg(cid, "assistant", tool_calls=[call])
    await chat._run_call(cid, call, {}, G1, mid)
    row = svc.store.read().execute("SELECT * FROM admin_chat_msgs WHERE chat_id=? AND role='tool'", (cid,)).fetchone()
    assert row["content"] == body
    projected = chat._history(cid)[-1]["content"]
    assert len(projected) < len(body)
    assert "read_admin_history" in projected
    recovered = chat.read_history(cid, after=mid, limit=1, offset=TOOL_CONTENT_MAX, chars=100)
    assert "MIDDLE_RECOVERABLE" in recovered["messages"][0]["content"]


@pytest.mark.asyncio
async def test_auto_coverage_includes_deferred_note_source_rows(tmp_path: Path, monkeypatch) -> None:
    svc = _Svc(tmp_path, models=_Models(script=[_res("summary")]))
    chat = AdminChat(svc)
    cid = int(chat.create(group_id=G1)["id"])
    call = _call("stub_read", {})
    chat._add_msg(cid, "assistant", tool_calls=[call])
    chat._add_msg(cid, "system_note", "note between call and reply")
    covered_to = chat._add_msg(cid, "tool", "result", tool_call_id="c1", name="stub_read")
    keep_id = chat._add_msg(cid, "user", "latest requirement")
    raw = chat._history(cid)
    monkeypatch.setattr(compaction, "compact_threshold", lambda *args: 1)
    monkeypatch.setattr(compaction, "pick_cut_point", lambda messages, **kwargs: (messages[3:], messages[:3]))
    await chat._auto_compact_for_turn(cid, [{"role": "system", "content": "rules"}] + raw)
    row, meta = chat._latest_summary_row(cid)
    assert meta["msg_to"] == covered_to
    assert meta["msg_to"] < keep_id
    assert not any(m.get("role") == "tool" for m in chat._history(cid))


@pytest.mark.asyncio
async def test_synthetic_missing_reply_does_not_cover_next_user(tmp_path: Path, monkeypatch) -> None:
    svc = _Svc(tmp_path, models=_Models(script=[_res("summary")]))
    chat = AdminChat(svc)
    cid = int(chat.create(group_id=G1)["id"])
    chat._add_msg(cid, "assistant", tool_calls=[_call("stub_read", {})])
    covered_to = chat._add_msg(cid, "system_note", "deferred")
    latest = chat._add_msg(cid, "user", "do not lose me")
    raw = chat._history(cid)
    monkeypatch.setattr(compaction, "compact_threshold", lambda *args: 1)
    monkeypatch.setattr(compaction, "pick_cut_point", lambda messages, **kwargs: (messages[3:], messages[:3]))
    await chat._auto_compact_for_turn(cid, raw)
    _, meta = chat._latest_summary_row(cid)
    assert meta["msg_to"] == covered_to
    assert meta["msg_to"] < latest
    assert any(m.get("content") == "do not lose me" for m in chat._history(cid))
