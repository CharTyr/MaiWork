"""群文件：路径读不到时 base64 重传一次；失败原因用大白话告诉用户。

全部用假 host，不联网。
"""

from __future__ import annotations

import base64
import json

import pytest

from CharTyr_MaiWork.maiwork import outbox as outbox_mod
from CharTyr_MaiWork.maiwork.host import HostError, upload_error_kind
from CharTyr_MaiWork.maiwork.outbox import Delivery, upload_fail_reason

from test_outbox import GID, NOON, FakeHereNow, _artifact_file, _make, _rows, _seed_task



# ---------------------------------------------------------------- 纯函数


@pytest.mark.parametrize("msg", [
    "adapter.napcat.file.upload_group_file 失败: status=failed, retcode=1200：ENOENT: open '/root/a.zip'",
    "Error: no such file or directory, stat '/x'",
    "stage source is not a regular file",
    "stage source is not a local file",
    "识别URL失败, uri= /root/x.zip",
    "文件不存在",
])
def test_kind_path_unreadable(msg):
    assert upload_error_kind(msg) == "path_unreadable"


@pytest.mark.parametrize("msg,kind", [
    ("这个平台不支持上传群文件（适配器没有群文件接口），走网页链接交付", "no_api"),
    ("调用宿主能力超时: api.call", "timeout"),
    ("对端动作执行失败", "timeout"),
    ("adapter.napcat.file.upload_group_file 没返回 file_id", "other"),
    ("连接断开", "other"),
    ("", "other"),
])
def test_kind_others(msg, kind):
    assert upload_error_kind(msg) == kind


def test_reason_texts_have_no_abs_path():
    r = upload_fail_reason("status=failed：权限不足 /srv/mw/ws/artifacts/T-1/a.zip 不让传")
    assert "/srv" not in r and "QQ 端拒绝了上传" in r
    assert "Docker" in upload_fail_reason("ENOENT")
    assert "SnowLuma" in upload_fail_reason("这个平台不支持上传群文件（适配器没有群文件接口）")
    assert "超时" in upload_fail_reason("对端动作执行失败")


# ---------------------------------------------------------------- 重传


def _file(tmp_path, data=b"zipdata"):
    (tmp_path / "ws").mkdir(exist_ok=True)
    f = tmp_path / "ws" / "a.zip"
    f.write_bytes(data)
    return f


@pytest.mark.asyncio
async def test_path_unreadable_retries_once_with_base64(tmp_path):
    store, _, host, _, _, ob = _make(tmp_path)
    f = _file(tmp_path)
    host.upload_errors.append(HostError("失败：ENOENT: no such file or directory"))
    ob.enqueue("f1", GID, "file", {"path": str(f), "name": "a.zip", "note": "说明", "push_kind": "delivery"})
    await ob.flush(NOON)
    assert len(host.uploads) == 2
    assert host.uploads[0]["path"] == str(f.resolve())
    assert host.uploads[1]["path"] == "base64://" + base64.b64encode(b"zipdata").decode()
    assert host.uploads[1]["name"] == "a.zip"
    assert _rows(store)[0]["status"] == "sent"
    ok = store.kv_get("outbox.file_last_ok")
    assert isinstance(ok, dict) and isinstance(ok["ts"], float)


@pytest.mark.asyncio
async def test_base64_retry_fails_only_once(tmp_path):
    store, _, host, _, _, ob = _make(tmp_path)
    f = _file(tmp_path)
    host.upload_errors += [HostError("文件不存在"), HostError("文件不存在")]
    ob.enqueue("f1", GID, "file", {"path": str(f), "name": "a.zip", "note": "说明", "push_kind": "delivery"})
    await ob.flush(NOON)
    assert len(host.uploads) == 2
    assert _rows(store)[0]["status"] == "failed"
    fail = store.kv_get("outbox.file_last_fail")
    assert fail["kind"] == "path_unreadable" and "Docker" in fail["reason"]
    assert set(fail) == {"ts", "reason", "kind"}


@pytest.mark.asyncio
@pytest.mark.parametrize("err", ["对端动作执行失败", "adapter 没返回 file_id", "什么怪错误"])
async def test_other_errors_never_retry(tmp_path, err):
    store, _, host, _, _, ob = _make(tmp_path)
    f = _file(tmp_path)
    host.upload_errors.append(HostError(err))
    ob.enqueue("f1", GID, "file", {"path": str(f), "name": "a.zip", "note": "说明", "push_kind": "delivery"})
    await ob.flush(NOON)
    await ob.flush(NOON + 600)
    assert len(host.uploads) == 1


@pytest.mark.asyncio
async def test_too_big_no_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox_mod, "_BASE64_MAX_BYTES", 4)
    store, _, host, _, _, ob = _make(tmp_path)
    f = _file(tmp_path)
    host.upload_errors.append(HostError("ENOENT"))
    ob.enqueue("f1", GID, "file", {"path": str(f), "name": "a.zip", "note": "说明", "push_kind": "delivery"})
    await ob.flush(NOON)
    assert len(host.uploads) == 1
    fail = store.kv_get("outbox.file_last_fail")
    assert fail["reason"] == "QQ 端和 MaiBot 不在同一台机器/容器，文件超过 20MB 没法跨机器传"
    assert fail["kind"] == "path_unreadable"


# ---------------------------------------------------------------- 回落说明 / 事件


@pytest.mark.asyncio
async def test_fallback_note_has_reason_without_path(tmp_path):
    hn = FakeHereNow()
    store, _, host, _, _, ob = _make(tmp_path, herenow=hn)
    f = _artifact_file(tmp_path, "数据.csv", text="a,b")
    host.upload_errors.append(HostError(f"QQ 拒绝：权限不足 {f}"))
    _seed_task(store)
    await Delivery(store, ob).deliver_task("T-1", kind="file", path=f, name="数据.csv", note="数据做好了")
    await ob.flush(NOON)
    rows = _rows(store)
    fb = [r for r in rows if r["key"].endswith(":fallback")][0]
    note = json.loads(fb["payload"])["note"]
    assert note.endswith("改发网页链接）")
    assert "（群文件没发出去：QQ 端拒绝了上传" in note
    assert str(tmp_path) not in note
    link = [r for r in rows if r["key"].endswith(":fallback:note")]
    assert link and str(tmp_path) not in json.loads(link[0]["payload"])["text"]
    ev = store.read().execute(
        "SELECT payload FROM events WHERE kind='task.delivery_fallback' AND entity='task' AND entity_id='T-1'"
    ).fetchall()
    assert len(ev) == 1
    p = json.loads(ev[0]["payload"])
    assert "QQ 端拒绝了上传" in p["reason"] and str(tmp_path) not in json.dumps(p, ensure_ascii=False)
    recs = Delivery(store, ob).delivery_records("T-1")
    assert any("QQ 端拒绝了上传" in (r.get("reason") or "") for r in recs)
