"""任务交付的 image 形态（2026-10-15 任务 A2）：Delivery.deliver_task 直接把图发进群。

规矩：成品是一到四张图片时（deliver_kind="image"）直接发图 + 一句交付说明；
载荷走发件箱的图片严格闸（路径在工作区、按内容判真图片、大小上限），
所以这里的测试图用真 PNG（另一个 agent 正在让那道闸放行 JPEG）。

用真 Store / 真 Outbox / 假 host（内存记录），不发网络、不碰宿主。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.outbox import Delivery

from test_outbox import (  # noqa: F401  （同目录测试模块互相导入是仓库惯例）
    GID,
    NOON,
    OutboxHost,
    _make,
    _rows,
    _seed_task,
)

pytestmark = pytest.mark.asyncio

PNG = b"\x89PNG\r\n\x1a\n" + b"payload"
PNG2 = b"\x89PNG\r\n\x1a\n" + b"second"


def _img_dir(tmp_path, task_id: str = "T-1"):
    d = tmp_path / "ws" / "artifacts" / task_id / "images"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _png(d, name: str, data: bytes = PNG):
    f = d / name
    f.write_bytes(data)
    return f


# ----------------------------------------------------------------------
# 入队形状
# ----------------------------------------------------------------------


async def test_deliver_task_image_dir_enqueues_one_row_per_image(tmp_path):
    """成品目录给 images/ 整个目录：每张图一条 image 发件行，说明只挂在第一张上。"""
    store, _settings, _host, _p, _m, ob = _make(tmp_path)
    _seed_task(store, "T-1")
    d = _img_dir(tmp_path)
    _png(d, "a.png")
    _png(d, "b.png", PNG2)
    delivery = Delivery(store, ob)

    oid = await delivery.deliver_task("T-1", kind="image", path=d, name="封面", note="画好了，两只猫")

    rows = [r for r in _rows(store) if r["kind"] == "image"]
    assert len(rows) == 2
    assert rows[0]["id"] == oid
    assert [r["key"] for r in rows] == ["task:T-1:deliver", "task:T-1:deliver:img2"]
    import json

    p0 = json.loads(rows[0]["payload"])
    p1 = json.loads(rows[1]["payload"])
    assert p0["text"] == "画好了，两只猫" and p0["push_kind"] == "delivery"
    assert p1["text"] == ""
    assert Path(p0["path"]).parts[-2:] == ("images", "a.png")
    assert Path(p1["path"]).parts[-2:] == ("images", "b.png")
    assert all(r["task_id"] == "T-1" for r in rows)


async def test_deliver_task_image_single_file(tmp_path):
    store, _settings, _host, _p, _m, ob = _make(tmp_path)
    _seed_task(store, "T-1")
    f = _png(_img_dir(tmp_path), "hero.png")
    delivery = Delivery(store, ob)

    await delivery.deliver_task("T-1", kind="image", path=f, name="hero.png", note="画好了")

    rows = [r for r in _rows(store) if r["kind"] == "image"]
    assert len(rows) == 1
    assert rows[0]["key"] == "task:T-1:deliver"


async def test_deliver_task_image_caps_at_four(tmp_path):
    """一次最多发四张（任务 A2 的口径）；多的不发，也不报错。"""
    store, _settings, _host, _p, _m, ob = _make(tmp_path)
    _seed_task(store, "T-1")
    d = _img_dir(tmp_path)
    for i in range(6):
        _png(d, f"p{i}.png", PNG + bytes([i]))
    delivery = Delivery(store, ob)

    await delivery.deliver_task("T-1", kind="image", path=d, name="图", note="好了")

    assert len([r for r in _rows(store) if r["kind"] == "image"]) == 4


async def test_deliver_task_image_skips_non_image_files(tmp_path):
    store, _settings, _host, _p, _m, ob = _make(tmp_path)
    _seed_task(store, "T-1")
    d = _img_dir(tmp_path)
    _png(d, "a.png")
    (d / "notes.txt").write_text("不是图", encoding="utf-8")
    (d / "page.html").write_text("<html>x</html>", encoding="utf-8")
    delivery = Delivery(store, ob)

    await delivery.deliver_task("T-1", kind="image", path=d, name="图", note="好了")

    rows = [r for r in _rows(store) if r["kind"] == "image"]
    assert len(rows) == 1
    assert "a.png" in rows[0]["payload"]


async def test_deliver_task_image_without_images_refuses(tmp_path):
    """目录里一张图都没有：中文报错，不入队（不假装交付了）。"""
    store, _settings, _host, _p, _m, ob = _make(tmp_path)
    _seed_task(store, "T-1")
    d = _img_dir(tmp_path)
    (d / "readme.md").write_text("没图", encoding="utf-8")
    delivery = Delivery(store, ob)

    with pytest.raises(ValueError) as ei:
        await delivery.deliver_task("T-1", kind="image", path=d, name="图", note="好了")
    assert "图" in str(ei.value)
    assert _rows(store) == []


async def test_deliver_task_image_outside_task_dir_refused(tmp_path):
    """路径闸照旧：图不在本任务成品目录里 → 拒。"""
    store, _settings, _host, _p, _m, ob = _make(tmp_path)
    _seed_task(store, "T-1")
    outside = tmp_path / "ws" / "images"
    outside.mkdir(parents=True, exist_ok=True)
    f = outside / "x.png"
    f.write_bytes(PNG)
    delivery = Delivery(store, ob)

    with pytest.raises(ValueError):
        await delivery.deliver_task("T-1", kind="image", path=f, name="x.png", note="好了")
    assert _rows(store) == []


# ----------------------------------------------------------------------
# 真发：走发件箱的图片通道
# ----------------------------------------------------------------------


async def test_deliver_task_image_really_sends_pngs_with_note(tmp_path):
    store, _settings, host, _p, _m, ob = _make(tmp_path)
    _seed_task(store, "T-1")
    d = _img_dir(tmp_path)
    _png(d, "a.png", PNG)
    _png(d, "b.png", PNG2)
    delivery = Delivery(store, ob)

    await delivery.deliver_task("T-1", kind="image", path=d, name="封面", note="画好了，两只猫")
    await ob.flush(NOON)

    assert [x["png"] for x in host.images] == [PNG, PNG2]
    assert host.images[0]["text"] == "画好了，两只猫"
    assert host.images[1]["text"] == ""
    assert all(r["status"] == "sent" for r in _rows(store) if r["kind"] == "image")


async def test_deliver_task_image_bad_content_fails_without_retry(tmp_path):
    """后缀 .png 但内容不是图 → 发件箱的严格闸判失败（载荷本身不合法，不浪费重试）。"""
    store, _settings, host, _p, _m, ob = _make(tmp_path)
    _seed_task(store, "T-1")
    d = _img_dir(tmp_path)
    _png(d, "fake.png", b"not an image at all")
    delivery = Delivery(store, ob)

    await delivery.deliver_task("T-1", kind="image", path=d, name="图", note="好了")
    await ob.flush(NOON)

    row = [r for r in _rows(store) if r["kind"] == "image"][0]
    assert row["status"] == "failed" and int(row["attempts"]) == 1
    assert host.images == []


async def test_deliver_task_image_has_group_file_fallback_declared(tmp_path):
    """载荷里带好回落描述（图片发不出去时当群文件发）——回落由 Outbox.flush 决定。

    回落的说明必须是**群文件口径**（2026-10-15）：图没发出去、改发群文件之后，
    群里看到的话不能还是「…图发在群里了」。
    """
    import json

    from CharTyr_MaiWork.maiwork.outbox import _note_fallback

    store, _settings, _host, _p, _m, ob = _make(tmp_path)
    _seed_task(store, "T-1")
    d = _img_dir(tmp_path)
    _png(d, "a.png")
    delivery = Delivery(store, ob)

    await delivery.deliver_task("T-1", kind="image", path=d, name="封面", note="好了")

    payload = json.loads([r for r in _rows(store) if r["kind"] == "image"][0]["payload"])
    assert payload["fallback"]["kind"] == "file"
    assert payload["fallback"]["path"].endswith("a.png")
    assert payload["text"] == "好了"  # 图正常发出去时还是原来的交付说明
    assert payload["fallback"]["note"] == _note_fallback("测试任务", "file")
    assert "图发在群里了" not in payload["fallback"]["note"]
