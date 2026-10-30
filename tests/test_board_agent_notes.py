"""看板抽屉显示「这次交接单留的便条」（docs/34 §二.4 + 第五节第 3 点）。先红后绿。"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import clock

from test_board import G1, OTHER, _handoff, _login, env  # noqa: F401  （同目录测试模块互相导入是仓库惯例）

JS = Path(__file__).resolve().parents[1] / "maiwork" / "console" / "static" / "js"
if not JS.is_dir():
    raise RuntimeError(f"找不到前端目录：{JS}")


def _note(store, gid, hid, frm, to, body, ts):
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO agent_notes (group_id, from_kind, to_kind, body, handoff_id, created)"
            " VALUES (?,?,?,?,?,?)", (gid, frm, to, body, hid, ts))


@pytest.mark.asyncio
async def test_handoff_item_carries_its_notes(env) -> None:  # noqa: F811
    await _login(env)
    now = clock.now()
    _handoff(env.store, G1, "news", status="accepted", ts=now - 60, hid="h-n1")
    _handoff(env.store, G1, "news", status="accepted", ts=now - 60, hid="h-n2")
    _note(env.store, G1, "h-n1", "news", "idea", "给构想：大家在聊本地模型", now - 50)
    _note(env.store, G1, "h-n1", "main", "*", "主模型补一句", now - 50)
    _note(env.store, G1, "h-n2", "news", "*", "别的交接单的", now - 50)
    _note(env.store, OTHER, "h-n1", "news", "*", "别的群同 id（不该出现）", now - 50)
    r = await env.client.get("/api/board/items/handoff/h-n1")
    assert r.status == 200
    item = await r.json()
    assert "notes" not in item["item"]  # 不改 item 形状（和列表同形）
    assert [(n["from_kind"], n["to_kind"], n["body"]) for n in item["notes"]] == [
        ("news", "idea", "给构想：大家在聊本地模型"), ("main", "*", "主模型补一句")]
    assert set(item["notes"][0]) == {"from_kind", "to_kind", "body", "created"}


@pytest.mark.asyncio
async def test_handoff_item_without_notes_has_empty_list(env) -> None:  # noqa: F811
    await _login(env)
    _handoff(env.store, G1, "idea", status="running", ts=clock.now() - 30, hid="h-none")
    r = await env.client.get("/api/board/items/handoff/h-none")
    item = await r.json()
    assert item["notes"] == []


def test_board_drawer_shows_notes_block() -> None:
    src = (JS / "board.js").read_text(encoding="utf-8")
    assert "留给别的岗的便条" in src
    # 正文要过 esc，不能直接塞 HTML
    assert "d.notes" in src
    assert "esc(n.body)" in src or "clip(n.body)" in src
