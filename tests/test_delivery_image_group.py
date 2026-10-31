"""交付图组的定位与规范化（2026-10-15 发版前修复；491d 线上真复现）。

三件事，都是「图交付」这一类：

1. **图组定位口径**（`Delivery._image_group`）认三种形态、不递归：
   ① 明确给的目录（`artifacts/<任务>/images/` 或别的子目录）→ 取这一层的图；
   ② 单张文件、父目录名是 `images`（**线上验收记的就是这种**）→ 用那个目录；
   ③ 任务成品根 `artifacts/<任务>`（图在根下的 `images/`）→ 取 `images/` 这一层。
   每张都过容器闸（resolve 后必须在本任务成品目录里）、跳过符号链接、按文件名排序、最多 4 张。
   线上症状：coordinator 允许把成品根交给交付层，而旧 `deliver_task` 只 `iterdir` 根一层 →
   目录里没有图 → ValueError 被 coordinator 吞掉 → 任务 completed 却**零入队**。

2. **整组恢复**：`reenqueue_missing`（管理员重发）以前只拿 `attempts.artifacts[0]` 那**一张**，
   多图任务热重载后只能补回第一张；`_repair_sent_followups`（发出后补说明）以前完全不认 image。
   两处都走同一套图组口径。

3. **WEBP/GIF → PNG**：交付入口按内容认格式（不信后缀），WEBP/GIF 用 Pillow（可选依赖）
   真转成 PNG 再入队（输出在受控位置、幂等、不删原成品）；转不了就按现有群文件回落，
   回落文案必须说真话（不许再宣称「图发在群里了」）。

先写先红：`_image_group` / `_image_for_send` / 任务根形态 / 整组恢复 / 补漏行在实现前都不存在。
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.outbox import Delivery, _note_fallback

from test_outbox import GID, NOON, _make, _rows  # noqa: F401

pytestmark = pytest.mark.asyncio

PNG = b"\x89PNG\r\n\x1a\n" + b"payload"
TITLE = "两只猫"


# ----------------------------------------------------------------------
# 小工具（和 test_delivery_image_followups 同口径，本文件自带一份）
# ----------------------------------------------------------------------


def _seed(store, tid: str = "T-1", *, kind: str = "image", artifacts=None) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tasks (id, group_id, workspace, title, status, delivery_kind, created, updated)"
            " VALUES (?, ?, 'ws', ?, 'completed', ?, 0, 0)",
            (tid, GID, TITLE, kind),
        )
        if artifacts is not None:
            conn.execute(
                "INSERT INTO attempts (task_id, n, req_version, status, artifacts, started)"
                " VALUES (?, 1, 1, 'passed', ?, 0)",
                (tid, json.dumps(artifacts)),
            )


def _imgs(tmp_path, tid: str = "T-1", n: int = 1) -> Path:
    """真 PNG 字节的成品图目录：<工作区>/artifacts/<任务>/images/p0..pN-1.png。"""
    d = tmp_path / "ws" / "artifacts" / tid / "images"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / f"p{i}.png").write_bytes(PNG + bytes([i]))
    return d


class _Env:
    """reenqueue_missing 用的最小执行环境（工作区根 = tmp_path，和 test_outbox 一致）。"""

    def __init__(self, root):
        self.root = root

    def workspace(self, name):
        return self.root / name

    def resolve(self, name, rel):
        return (self.root / name / rel).resolve()


def _image_rows(store):
    return [r for r in _rows(store) if r["kind"] == "image"]


def _names(rows):
    return [Path(json.loads(r["payload"])["path"]).name for r in rows]


def _webp_bytes(size=(20, 12)) -> bytes:
    """真 WEBP 字节（Pillow 生成，交给交付层转 PNG）。"""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 40)).save(buf, "WEBP")
    return buf.getvalue()


def _gif_bytes(size=(20, 12)) -> bytes:
    """真 GIF 字节。"""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("P", size, 3).save(buf, "GIF")
    return buf.getvalue()


def _big_gif_bytes() -> bytes:
    """像素数刚好超过转换上限（4096²）的真 GIF：4097×4096，纯色压完只有十几 KB。"""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("P", (4097, 4096), 3).save(buf, "GIF")
    return buf.getvalue()


# ----------------------------------------------------------------------
# 1. 图组定位：三种形态 + 上限 + 符号链接
# ----------------------------------------------------------------------


async def test_image_group_locates_three_shapes(tmp_path):
    """明确目录 / 单张（父是 images）/ 任务成品根，三种形态回同一组图。"""
    store, _s, _host, _p, _m, ob = _make(tmp_path)
    _seed(store)
    d = _imgs(tmp_path, n=3)
    delivery = Delivery(store, ob)
    want = ["p0.png", "p1.png", "p2.png"]

    assert [p.name for p in delivery._image_group("T-1", GID, d)] == want          # ① 明确目录
    assert [p.name for p in delivery._image_group("T-1", GID, d / "p1.png")] == want  # ② 单张
    assert [p.name for p in delivery._image_group("T-1", GID, d.parent)] == want  # ③ 成品根
    # 空目录 → 空（调用方按「没有可发的图片」报中文错）
    empty = d.parent / "empty"
    empty.mkdir()
    assert delivery._image_group("T-1", GID, empty) == []
    # 别的任务的目录 → 容器闸拦住（不递归、不越界）
    with pytest.raises(ValueError):
        delivery._image_group("T-1", GID, tmp_path / "ws" / "artifacts" / "T-2")


async def test_image_group_from_task_root_enqueues_whole_group(tmp_path):
    """491d 真复现：成品根交给交付层 → 必须 ≥1 个出站项，且是正确的一整组。"""
    store, _s, host, _p, _m, ob = _make(tmp_path)
    _seed(store)
    d = _imgs(tmp_path, n=3)
    first = await Delivery(store, ob).deliver_task(
        "T-1", kind="image", path=d.parent, name="两只猫", note="画好了"
    )
    assert first >= 1
    rows = _image_rows(store)
    assert [r["key"] for r in rows] == ["task:T-1:deliver", "task:T-1:deliver:img2",
                                       "task:T-1:deliver:img3"]
    assert _names(rows) == ["p0.png", "p1.png", "p2.png"]
    assert [json.loads(r["payload"])["text"] for r in rows] == ["画好了", "", ""]
    await ob.flush(NOON)
    assert len(host.images) == 3


async def test_image_group_caps_at_four_and_skips_symlinks(tmp_path):
    """5 张真图只发最前 4 张；软链不算图（绝不跟着链把工作区外的文件发进群）。"""
    store, _s, host, _p, _m, ob = _make(tmp_path)
    _seed(store)
    d = _imgs(tmp_path, n=5)
    outside = d.parent / "outside.png"
    outside.write_bytes(PNG + b"outside")
    (d / "p9.png").symlink_to(outside)   # 名字排序在最后，且必须被跳过
    await Delivery(store, ob).deliver_task("T-1", kind="image", path=d, name="两只猫", note="")
    rows = _image_rows(store)
    assert _names(rows) == ["p0.png", "p1.png", "p2.png", "p3.png"]
    await ob.flush(NOON)
    assert len(host.images) == 4
    assert all(b"outside" not in img["png"] for img in host.images)


# ----------------------------------------------------------------------
# 2. 整组恢复（重发 / 补漏行）
# ----------------------------------------------------------------------


async def test_reenqueue_missing_restores_whole_group_from_single_artifact(tmp_path):
    """线上真形状：验收记的是 images/ 里的某一张 → 重发要恢复整组，不是只发那一张。"""
    store, _s, host, _p, _m, ob = _make(tmp_path)
    _seed(store, artifacts=["artifacts/T-1/images/p0.png"])
    _imgs(tmp_path, n=3)
    delivery = Delivery(store, ob)
    assert await delivery.reenqueue_missing("T-1", _Env(tmp_path)) is True
    rows = _image_rows(store)
    assert [r["key"] for r in rows] == ["task:T-1:deliver", "task:T-1:deliver:img2",
                                       "task:T-1:deliver:img3"]
    assert _names(rows) == ["p0.png", "p1.png", "p2.png"]
    await ob.flush(NOON)
    assert len(host.images) == 3
    # 已有成品行 → 不重复建（幂等）
    assert await delivery.reenqueue_missing("T-1", _Env(tmp_path)) is False


async def test_reenqueue_missing_keeps_the_four_image_cap(tmp_path):
    store, _s, _host, _p, _m, ob = _make(tmp_path)
    _seed(store, artifacts=["artifacts/T-1/images/p0.png"])
    _imgs(tmp_path, n=6)
    assert await Delivery(store, ob).reenqueue_missing("T-1", _Env(tmp_path)) is True
    assert _names(_image_rows(store)) == ["p0.png", "p1.png", "p2.png", "p3.png"]


async def test_repair_sent_followups_completes_missing_image_rows(tmp_path):
    """热重载只入队并发出第一张：补说明这一轮要把漏掉的 imgN 补齐（不重发已发的）。"""
    store, _s, host, _p, _m, ob = _make(tmp_path)
    _seed(store)
    d = _imgs(tmp_path, n=3)
    ob.enqueue(
        "task:T-1:deliver",
        GID,
        "image",
        {
            "path": str(d / "p0.png"),
            "text": "画好了",
            "push_kind": "delivery",
            "fallback": {"kind": "file", "path": str(d / "p0.png"), "name": "p0.png", "note": ""},
        },
        task_id="T-1",
    )
    with store.tx() as conn:
        conn.execute("UPDATE outbox SET status='sent', updated=? WHERE key='task:T-1:deliver'",
                     (NOON,))
    await ob.flush(NOON)
    rows = _image_rows(store)
    assert [r["key"] for r in rows] == ["task:T-1:deliver", "task:T-1:deliver:img2",
                                       "task:T-1:deliver:img3"]
    assert _names(rows) == ["p0.png", "p1.png", "p2.png"]
    assert [r["status"] for r in rows] == ["sent", "sent", "sent"]
    assert len(host.images) == 2  # 只补发漏掉的两张，第一张不重发
    await ob.flush(NOON + 600)
    assert len(host.images) == 2 and len(_image_rows(store)) == 3  # 再跑不重复


# ----------------------------------------------------------------------
# 3. WEBP / GIF → PNG（真字节一路走到发送口）
# ----------------------------------------------------------------------


async def test_webp_and_gif_are_sent_as_real_png(tmp_path):
    """真 webp / gif 走完整 flush：假宿主收到的是真 PNG（魔数 + Pillow 解码 + 尺寸）。"""
    pytest.importorskip("PIL")
    from PIL import Image

    store, _s, host, _p, _m, ob = _make(tmp_path)
    _seed(store)
    d = tmp_path / "ws" / "artifacts" / "T-1" / "images"
    d.mkdir(parents=True)
    (d / "a.webp").write_bytes(_webp_bytes())
    (d / "b.gif").write_bytes(_gif_bytes())
    await Delivery(store, ob).deliver_task("T-1", kind="image", path=d, name="封面", note="画好了")
    rows = _image_rows(store)
    assert len(rows) == 2
    first = json.loads(rows[0]["payload"])
    if os.open not in os.supports_dir_fd:
        assert [Path(json.loads(r["payload"])["path"]) for r in rows] == [
            (d / "a.webp").resolve(), (d / "b.gif").resolve(),
        ]
        await ob.flush(NOON)
        assert host.images == [] and len(host.uploads) == 2
        assert all(r["status"] == "sent" for r in _rows(store) if r["kind"] == "file")
        return
    assert Path(first["path"]).name.endswith(".webp.png")
    assert Path(first["path"]).parent.name == ".png"
    # 回落仍指向**原成品**（群文件不挑格式），原文件也一个字没动
    assert first["fallback"]["path"].endswith("a.webp")
    assert (d / "a.webp").exists() and (d / "b.gif").exists()
    await ob.flush(NOON)
    assert len(host.images) == 2
    for sent in host.images:
        raw = sent["png"]
        assert raw.startswith(b"\x89PNG\r\n\x1a\n")
        with Image.open(io.BytesIO(raw)) as im:
            assert im.format == "PNG" and im.size == (20, 12)


async def test_converted_png_is_reused_on_repeat_delivery(tmp_path):
    """幂等：同一张 webp 重复交付复用同一个 .png（不重转、不新增文件）。"""
    pytest.importorskip("PIL")

    store, _s, _host, _p, _m, ob = _make(tmp_path)
    _seed(store)
    d = tmp_path / "ws" / "artifacts" / "T-1" / "images"
    d.mkdir(parents=True)
    (d / "a.webp").write_bytes(_webp_bytes())
    delivery = Delivery(store, ob)
    await delivery.deliver_task("T-1", kind="image", path=d, name="封面", note="")
    made = sorted((d / ".png").iterdir())
    if os.open not in os.supports_dir_fd:
        assert made == []
        assert Path(json.loads(_image_rows(store)[0]["payload"])["path"]) == (d / "a.webp").resolve()
        await delivery.deliver_task("T-1", kind="image", path=d, name="封面", note="")
        assert len(_image_rows(store)) == 1
        assert list((d / ".png").iterdir()) == []
        return
    assert [p.name for p in made] == ["a.webp.png"]
    before = [p.stat().st_mtime_ns for p in made]
    await delivery.deliver_task("T-1", kind="image", path=d, name="封面", note="")
    after = sorted((d / ".png").iterdir())
    assert [p.name for p in after] == ["a.webp.png"]
    assert [p.stat().st_mtime_ns for p in after] == before  # 没重写


async def test_png_and_jpeg_are_not_touched(tmp_path):
    """本来就是 PNG/JPEG 的成品：原样入队，不生成 .png 缓存。"""
    store, _s, _host, _p, _m, ob = _make(tmp_path)
    _seed(store)
    d = _imgs(tmp_path, n=2)
    (d / "c.jpg").write_bytes(b"\xff\xd8\xff" + b"jpegpayload")
    await Delivery(store, ob).deliver_task("T-1", kind="image", path=d, name="封面", note="")
    rows = _image_rows(store)
    assert _names(rows) == ["c.jpg", "p0.png", "p1.png"]
    assert [json.loads(r["payload"])["path"] for r in rows] == [
        str((d / "c.jpg").resolve()), str((d / "p0.png").resolve()), str((d / "p1.png").resolve())
    ]
    assert not (d / ".png").exists()


async def test_missing_directory_handles_keeps_safe_file_fallback(tmp_path, monkeypatch):
    """PNG 缓存不能安全锚定时发原件；不要求 Windows 放开目录句柄写入。"""
    from CharTyr_MaiWork.maiwork import outbox as outbox_mod

    store, _s, host, _p, _m, ob = _make(tmp_path)
    _seed(store)
    d = tmp_path / "ws" / "artifacts" / "T-1" / "images"
    d.mkdir(parents=True)
    original = _webp_bytes()
    source = d / "a.webp"
    source.write_bytes(original)
    cache = (d / ".png").resolve()
    real_open = outbox_mod.os.open

    def no_directory_handle(path, flags, *args, **kwargs):
        if Path(path) == cache:
            raise NotImplementedError("directory handles unavailable")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(outbox_mod.os, "open", no_directory_handle)
    await Delivery(store, ob).deliver_task("T-1", kind="image", path=d, name="封面", note="画好了")
    payload = json.loads(_image_rows(store)[0]["payload"])
    assert Path(payload["path"]) == source.resolve()
    assert source.read_bytes() == original
    assert list(cache.iterdir()) == []
    await ob.flush(NOON)
    assert host.images == [] and len(host.uploads) == 1
    files = [r for r in _rows(store) if r["kind"] == "file"]
    assert len(files) == 1 and files[0]["status"] == "sent"
    assert json.loads(files[0]["payload"])["note"] == _note_fallback(TITLE, "file")


async def test_pillow_missing_falls_back_to_group_file_with_honest_note(tmp_path, monkeypatch):
    """没装 Pillow（可选依赖）→ 不转、按老路失败 → 群文件回落，文案说真话。

    回落说明必须是群文件口径（「…文件在链接里」），绝不能再说「图发在群里了」。
    """
    pytest.importorskip("PIL")
    webp = _webp_bytes()  # 先把真字节拿到手，再把 PIL 藏起来
    store, _s, host, _p, _m, ob = _make(tmp_path)
    _seed(store)
    d = tmp_path / "ws" / "artifacts" / "T-1" / "images"
    d.mkdir(parents=True)
    (d / "a.webp").write_bytes(webp)
    monkeypatch.setitem(sys.modules, "PIL", None)  # `from PIL import ...` → ImportError
    await Delivery(store, ob).deliver_task(
        "T-1", kind="image", path=d, name="封面", note="两只猫弄好了，图发在群里了"
    )
    assert not (d / ".png").exists()  # 没转
    await ob.flush(NOON)
    rows = _rows(store)
    img = [r for r in rows if r["kind"] == "image"][0]
    assert img["status"] == "failed"
    files = [r for r in rows if r["kind"] == "file"]
    assert len(files) == 1, [dict(r) for r in rows]
    fb = json.loads(files[0]["payload"])
    assert fb["path"].endswith("a.webp")           # 回落发的是原成品
    assert fb["note"] == _note_fallback(TITLE, "file")
    assert len(host.uploads) == 1
    # 群里真正看到的话（回落文件那条 + 它的说明）不能宣称「图发在群里了」
    seen = " ".join(t["text"] for t in host.texts)
    assert "图发在群里了" not in seen
    assert "文件在链接里" in seen
    assert host.images == []


# ----------------------------------------------------------------------
# 4. 转换的安全边界（2026-10-15 收口）：软链落点 + 读入 / 像素预算
# ----------------------------------------------------------------------


async def test_symlinked_png_cache_dir_is_refused(tmp_path):
    """`.png/` 被预置成指向成品目录外的软链 → 不转、不往外面写，原件走群文件回落。"""
    pytest.importorskip("PIL")

    store, _s, host, _p, _m, ob = _make(tmp_path)
    _seed(store)
    d = tmp_path / "ws" / "artifacts" / "T-1" / "images"
    d.mkdir(parents=True)
    (d / "a.webp").write_bytes(_webp_bytes())
    outside = tmp_path / "outside"           # 成品目录外（仍在工作区里，旧代码会写进去）
    outside.mkdir()
    (d / ".png").symlink_to(outside, target_is_directory=True)
    await Delivery(store, ob).deliver_task("T-1", kind="image", path=d, name="封面", note="画好了")
    payload = json.loads(_image_rows(store)[0]["payload"])
    assert payload["path"].endswith("a.webp")      # 没转，按原件入队
    assert list(outside.iterdir()) == []           # 外面一个文件都没落
    assert (d / "a.webp").exists()                 # 原成品保留
    await ob.flush(NOON)
    assert host.images == [] and len(host.uploads) == 1   # 群文件回落


async def test_symlinked_existing_output_is_refused_and_target_untouched(tmp_path):
    """既有输出是软链（指到外部受保护文件）→ 不读它、不改它、也不把它换掉。"""
    pytest.importorskip("PIL")

    store, _s, host, _p, _m, ob = _make(tmp_path)
    _seed(store)
    d = tmp_path / "ws" / "artifacts" / "T-1" / "images"
    d.mkdir(parents=True)
    (d / "a.webp").write_bytes(_webp_bytes())
    cache = d / ".png"
    cache.mkdir()
    protected = tmp_path / "protected.png"
    protected.write_bytes(b"PROTECTED-DO-NOT-TOUCH")
    (cache / "a.webp.png").symlink_to(protected)
    await Delivery(store, ob).deliver_task("T-1", kind="image", path=d, name="封面", note="画好了")
    payload = json.loads(_image_rows(store)[0]["payload"])
    assert payload["path"].endswith("a.webp")               # 没跟着软链读、也没转
    assert protected.read_bytes() == b"PROTECTED-DO-NOT-TOUCH"   # 外部文件没被改
    assert (cache / "a.webp.png").is_symlink()              # 软链还在（没被 replace 掉）
    assert (d / "a.webp").exists()
    await ob.flush(NOON)
    assert host.images == [] and len(host.uploads) == 1


async def test_oversized_input_is_not_converted(tmp_path, monkeypatch):
    """超过 8MiB 的成品：整份不读、不调转换，按原件走（发件箱失败 → 群文件）。"""
    from CharTyr_MaiWork.maiwork import outbox as outbox_mod

    calls: list[int] = []
    real = outbox_mod._to_png_bytes
    monkeypatch.setattr(outbox_mod, "_to_png_bytes",
                        lambda data: (calls.append(len(data)), real(data))[1])
    store, _s, host, _p, _m, ob = _make(tmp_path)
    _seed(store)
    d = tmp_path / "ws" / "artifacts" / "T-1" / "images"
    d.mkdir(parents=True)
    big = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * (8 * 1024 * 1024)
    (d / "a.webp").write_bytes(big)
    assert big.__len__() > 8 * 1024 * 1024
    await Delivery(store, ob).deliver_task("T-1", kind="image", path=d, name="封面", note="画好了")
    assert calls == []                                        # 转换一次都没被调用
    assert json.loads(_image_rows(store)[0]["payload"])["path"].endswith("a.webp")
    assert not (d / ".png").exists()
    await ob.flush(NOON)
    assert host.images == [] and len(host.uploads) == 1


async def test_over_pixel_image_is_not_loaded(tmp_path, monkeypatch):
    """像素数超过上限的真 GIF：不 `load()`（防解压炸弹），按原件走群文件。"""
    pytest.importorskip("PIL")
    from PIL import ImageFile

    gif = _big_gif_bytes()          # 先把真字节拿到手，再装探针
    loads: list[str] = []
    real_load = ImageFile.ImageFile.load

    def spy(self, *a, **kw):
        loads.append(str(getattr(self, "format", "")))
        return real_load(self, *a, **kw)

    monkeypatch.setattr(ImageFile.ImageFile, "load", spy)
    store, _s, host, _p, _m, ob = _make(tmp_path)
    _seed(store)
    d = tmp_path / "ws" / "artifacts" / "T-1" / "images"
    d.mkdir(parents=True)
    (d / "a.gif").write_bytes(gif)
    await Delivery(store, ob).deliver_task("T-1", kind="image", path=d, name="封面", note="画好了")
    assert loads == []                                        # 超像素：一次都没 load
    assert json.loads(_image_rows(store)[0]["payload"])["path"].endswith("a.gif")
    assert not (d / ".png").exists()
    await ob.flush(NOON)
    assert host.images == [] and len(host.uploads) == 1
