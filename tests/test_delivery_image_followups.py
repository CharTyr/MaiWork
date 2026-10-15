"""任务交付 image 形态的收尾（2026-10-15）：发件箱其余几处也认得「群图片」。

- 图片交付行算「成品行」（重发、待交付判断都要看它）；资讯卡片之类的非任务图片不算；
- 图片发不出去 → 按载荷里写好的回落描述改发群文件；没有回落描述的图片（资讯卡片）不乱补「网页里有」；
- 图片发出后给 MaiBot 记一句「刚发了图」（只第一张），有人问起能接上；
- 管理员「重发」对 image 任务能重建（以前报「缺少可恢复的交付方式」）；兜底说明说「图发在群里了」。
"""

from __future__ import annotations

import json

import pytest

from CharTyr_MaiWork.maiwork.outbox import Delivery, _is_artifact_outbox_row, _note_fallback

from test_outbox import GID, NOON, _make, _rows  # noqa: F401

pytestmark = pytest.mark.asyncio

PNG = b"\x89PNG\r\n\x1a\n" + b"payload"


def _seed(store, tid="T-1", *, kind="image", artifacts=None):
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tasks (id, group_id, workspace, title, status, delivery_kind, created, updated)"
            " VALUES (?, ?, 'ws', '两只猫', 'completed', ?, 0, 0)",
            (tid, GID, kind),
        )
        if artifacts is not None:
            conn.execute(
                "INSERT INTO attempts (task_id, n, req_version, status, artifacts, started)"
                " VALUES (?, 1, 1, 'passed', ?, 0)",
                (tid, json.dumps(artifacts)),
            )


def _imgs(tmp_path, tid="T-1", n=1):
    d = tmp_path / "ws" / "artifacts" / tid / "images"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / f"p{i}.png").write_bytes(PNG + bytes([i]))
    return d


class _Env:
    def __init__(self, root):
        self.root = root

    def workspace(self, name):
        return self.root / name

    def resolve(self, name, rel):
        return (self.root / name / rel).resolve()


async def test_image_delivery_rows_count_as_artifacts():
    assert _is_artifact_outbox_row("image", "task:T-1:deliver")
    assert _is_artifact_outbox_row("image", "task:T-1:deliver:img2")
    # 资讯卡片 / 其它图片不是任务成品
    assert not _is_artifact_outbox_row("image", "news_card:5")
    assert not _is_artifact_outbox_row("image", "task:T-1:deliver:note")


async def test_image_note_fallback_wording():
    assert _note_fallback("两只猫", "image") == "两只猫做好了"


async def test_failed_image_falls_back_to_group_file(tmp_path):
    store, _s, host, _p, _m, ob = _make(tmp_path)
    _seed(store)
    d = _imgs(tmp_path)
    await Delivery(store, ob).deliver_task("T-1", kind="image", path=d, name="两只猫", note="画好了")
    host.image_errors = [RuntimeError("send.hybrid 发送失败：被风控")] * 3
    await ob.flush(NOON)
    await ob.flush(NOON + 600)
    rows = _rows(store)
    img = [r for r in rows if r["kind"] == "image"][0]
    assert img["status"] == "failed"
    files = [r for r in rows if r["kind"] == "file"]
    assert len(files) == 1, [dict(r) for r in rows]
    assert json.loads(files[0]["payload"])["path"].endswith("p0.png")
    assert not any(str(r["key"]).endswith(":webonly") for r in rows)


async def test_failed_non_task_image_gets_no_webonly_note(tmp_path):
    """没有回落描述的图片（比如资讯卡片，那边自己兜文字）：不补「成品在网页里」。"""
    store, _s, host, _p, _m, ob = _make(tmp_path)
    f = tmp_path / "ws" / "card.png"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(PNG)
    ob.enqueue("news_card:5", GID, "image", {"path": str(f), "push_kind": "news_card"})
    host.image_errors = [RuntimeError("x")] * 3
    await ob.flush(NOON)
    await ob.flush(NOON + 600)
    assert not any(str(r["key"]).endswith(":webonly") for r in _rows(store))


async def test_sent_image_leaves_mention_for_maibot(tmp_path):
    store, _s, host, _p, mentions, ob = _make(tmp_path)
    _seed(store)
    d = _imgs(tmp_path, n=2)
    await Delivery(store, ob).deliver_task("T-1", kind="image", path=d, name="两只猫", note="画好了，两只猫")
    await ob.flush(NOON)
    keys = [str(r["key"]) for r in store.read().execute("SELECT key FROM mentions").fetchall()]
    assert len([k for k in keys if k.startswith("deliver:")]) == 1, keys


async def test_admin_resend_rebuilds_image_delivery(tmp_path):
    store, _s, host, _p, _m, ob = _make(tmp_path)
    _seed(store, artifacts=["artifacts/T-1/images"])
    _imgs(tmp_path, n=2)
    delivery = Delivery(store, ob)
    assert await delivery.reenqueue_missing("T-1", _Env(tmp_path)) is True
    rows = [r for r in _rows(store) if r["kind"] == "image"]
    assert [r["key"] for r in rows] == ["task:T-1:deliver", "task:T-1:deliver:img2"]
    assert json.loads(rows[0]["payload"])["text"] == "两只猫做好了"
    # 已有成品行 → 不重复建
    assert await delivery.reenqueue_missing("T-1", _Env(tmp_path)) is False
