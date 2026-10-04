"""outbox.py 单元测试：发件箱（pending→sending→sent/uncertain/failed）、睡觉时段推迟、
群文件上传不盲目重试、交付回落、report_error 去重与遮密钥。

全部用假 host（内存记录），不碰真宿主；here.now 也全用假对象。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import clock, group_push
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
from CharTyr_MaiWork.maiwork.host import HostError
from CharTyr_MaiWork.maiwork.outbox import Delivery, Outbox, report_error
from CharTyr_MaiWork.maiwork.store import Store

pytestmark = pytest.mark.asyncio

BJ = timezone(timedelta(hours=8))
GID = "900000001"


def _ts(hour: int, minute: int = 0, *, day: int = 15) -> float:
    """北京时间 2026-10-{day} 某时刻的 epoch。"""
    return datetime(2026, 10, day, hour, minute, tzinfo=BJ).timestamp()


NOON = _ts(12)          # 非睡觉时段
SLEEP = _ts(23, 30)     # 睡觉时段（默认 23:00-08:00）
MORNING = _ts(8, 30, day=16)  # 时段结束后（推迟到次日 08:00）


class OutboxHost:
    """假的宿主发送口：记录 send_text / send_image / upload_group_file，可预置错误队列。"""

    def __init__(self) -> None:
        self.texts: list[dict] = []
        self.images: list[dict] = []
        self.uploads: list[dict] = []
        self.text_errors: list[Exception] = []
        self.image_errors: list[Exception] = []
        self.upload_errors: list[Exception] = []
        self.msg_seq = 0

    async def send_text(self, session_id: str, text: str, *, reply_to: str = "",
                        at_user: str = "", at_name: str = "") -> object:
        self.texts.append({"session_id": session_id, "text": text, "reply_to": reply_to,
                           "at_user": at_user, "at_name": at_name})
        if self.text_errors:
            raise self.text_errors.pop(0)
        self.msg_seq += 1
        return type("SendResult", (), {"sent": True, "message_id": f"m{self.msg_seq}"})()

    async def send_image(self, session_id: str, png: bytes, *, text: str = "") -> object:
        self.images.append({"session_id": session_id, "png": png, "text": text})
        if self.image_errors:
            raise self.image_errors.pop(0)
        self.msg_seq += 1
        return type("SendResult", (), {"sent": True, "message_id": f"i{self.msg_seq}"})()

    async def upload_group_file(self, group_id: str, path: str, name: str) -> str:
        self.uploads.append({"group_id": group_id, "path": path, "name": name})
        if self.upload_errors:
            raise self.upload_errors.pop(0)
        return "file-id-123"


class FakeHereNow:
    """假的 HereNow：成功队列 / 失败队列。"""

    def __init__(self) -> None:
        self.calls: list[Path] = []
        self.results: list[object] = []

    async def publish(self, directory: Path) -> dict:
        self.calls.append(Path(directory))
        if self.results:
            r = self.results.pop(0)
            if isinstance(r, BaseException):
                raise r
            return r
        return {
            "url": "https://page.here.now/",
            "slug": "s1",
            "expires_ts": NOON + 24 * 3600,
            "claim_url": "https://here.now/c/x",
            "claim_token": "tok",
        }


def _make(tmp_path, *, cfg: dict | None = None, herenow=None):
    store = Store(tmp_path / "t.db")
    store.migrate()
    merged = {
        # 工作区根目录 = tmp_path；_seed_task 里工作区名是 "ws" → 工作区就是 tmp_path/ws
        # （交付路径闸要按 <工作区>/artifacts/<task_id>/ 比较，所以这两个得对得上）
        "environments": {"workspace_root": str(tmp_path)},
        "groups": {"serve": [{"group": f"qq:{GID}"}]},  # flush 只发服务群（S1），测试群必须配上
    }
    if cfg:
        merged.update(cfg)
    settings, _ = load_settings(merged)
    host = OutboxHost()
    pushes = Pushes(store, lambda: settings)
    mentions = Mentions(store, lambda: settings)
    ob = Outbox(store, host, pushes, mentions, lambda: settings, herenow=herenow)
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, session_id) VALUES (?, ?)",
            (GID, "sess-1"),
        )
    return store, settings, host, pushes, mentions, ob


def _seed_task(store: Store, task_id: str = "T-1") -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tasks (id, group_id, workspace, title, status, created, updated)"
            " VALUES (?, ?, 'ws', '测试任务', 'completed', 0, 0)",
            (task_id, GID),
        )


def _artifact_file(
    tmp_path, name: str = "报告.html", *, task_id: str = "T-1", text: str = "<html>ok</html>"
) -> Path:
    """任务工作区里 artifacts/<task_id>/ 下的成品文件。

    交付路径闸（审核整改 6）要求交付的 path 在这个目录里；工作区 = tmp_path/ws。
    """
    d = tmp_path / "ws" / "artifacts" / task_id
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text(text, encoding="utf-8")
    return f


def _rows(store: Store):
    return store.read().execute(
        "SELECT id, key, kind, status, payload, error, result, attempts, not_before, task_id"
        " FROM outbox ORDER BY id"
    ).fetchall()


# ----------------------------------------------------------------------
# enqueue / flush 基本功
# ----------------------------------------------------------------------


async def test_enqueue_same_key_returns_old_id(tmp_path):
    store, *_rest, ob = _make(tmp_path)
    i1 = ob.enqueue("k1", GID, "text", {"text": "hi", "push_kind": "delivery"})
    i2 = ob.enqueue("k1", GID, "text", {"text": "hi again", "push_kind": "delivery"})
    assert i1 == i2
    rows = _rows(store)
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"])["text"] == "hi"  # 没覆盖


async def test_flush_sends_text_and_records_push(tmp_path):
    store, _, host, pushes, mentions, ob = _make(tmp_path)
    oid = ob.enqueue("k1", GID, "text", {"text": "做好了", "push_kind": "delivery", "reply_to": "777"})
    await ob.flush(NOON)
    row = _rows(store)[0]
    assert row["status"] == "sent"
    assert json.loads(row["result"])["message_id"]
    # 发给 groups 表里的 session_id，带引用
    assert host.texts[0]["session_id"] == "sess-1"
    assert host.texts[0]["reply_to"] == "777"
    # 推送记了一笔（按 record 的 now 记账，直接查表）
    assert store.read().execute(
        "SELECT COUNT(*) c FROM pushes WHERE group_id=? AND kind='delivery'", (GID,)
    ).fetchone()["c"] == 1
    # 交付备忘进可提起清单
    mrows = store.read().execute("SELECT key, text FROM mentions").fetchall()
    assert any(r["key"] == f"deliver:{oid}" for r in mrows)
    assert "做好了" in mrows[0]["text"] or any("做好了" in r["text"] for r in mrows)


async def test_flush_sleeping_hours_postpones(tmp_path):
    """睡觉时段：受限推送（topic/status/reminder）推迟到时段结束后，状态仍 pending，
    原因写进 error 字段。

    普通交付同样受睡觉时段约束；只有明确标记为当时有人等待的交付可以即时发。
    """
    store, _, host, _, _, ob = _make(tmp_path)
    ob.enqueue("k1", GID, "text", {"text": "随便聊聊这个", "push_kind": "topic"})
    await ob.flush(SLEEP)
    row = _rows(store)[0]
    assert row["status"] == "pending"
    assert "睡觉" in row["error"]
    assert row["not_before"] >= _ts(8, day=16)  # 推迟到早上 8 点之后
    assert not host.texts  # 没发
    # 早上再 flush 就发出去了
    await ob.flush(MORNING)
    assert _rows(store)[0]["status"] == "sent"
    assert len(host.texts) == 1


async def test_flush_sleeping_hours_delivery_postponed(tmp_path):
    """普通交付在睡觉时段也要推迟，明确当场有人等的除外。"""
    store, _, host, _, _, ob = _make(tmp_path)
    ob.enqueue("k1", GID, "text", {"text": "成品来了", "push_kind": "delivery"})
    ob.enqueue("k2", GID, "text", {"text": "当场交付", "push_kind": "awaited_delivery"})
    await ob.flush(SLEEP)
    assert _rows(store)[0]["status"] == "pending"
    assert _rows(store)[1]["status"] == "sent"
    assert [t["text"] for t in host.texts] == ["当场交付"]


async def test_flush_sleeping_hours_error_and_command_not_postponed(tmp_path):
    """push_kind=error / command 不受睡觉时段限制。"""
    store, _, host, pushes, _, ob = _make(tmp_path)
    ob.enqueue("e1", GID, "text", {"text": "出故障了", "push_kind": "error"})
    ob.enqueue("c1", GID, "text", {"text": "/mw 状态", "push_kind": "command"})
    await ob.flush(SLEEP)
    rows = _rows(store)
    assert [r["status"] for r in rows] == ["sent", "sent"]
    assert len(host.texts) == 2


async def test_flush_daily_limit_postpones_to_tomorrow(tmp_path):
    """当天配额用完 → 受限推送推到明天 00:00 之后。

    普通交付和 status 一样计入主动推送日限额。
    """
    store, _, host, pushes, _, ob = _make(tmp_path, cfg={"delivery": {"push_per_day": 1}})
    ob.enqueue("k1", GID, "text", {"text": "第一条", "push_kind": "status"})
    await ob.flush(NOON)
    assert _rows(store)[0]["status"] == "sent"
    ob.enqueue("k2", GID, "text", {"text": "第二条", "push_kind": "status"})
    await ob.flush(NOON)
    row = _rows(store)[1]
    assert row["status"] == "pending"
    assert row["not_before"] >= _ts(0, day=16)
    assert "上限" in row["error"] or "推够" in row["error"]


async def test_flush_daily_limit_delivery_counts_as_push(tmp_path):
    """每日上限 1：交付先发，status 推迟到明天。"""
    store, _, host, pushes, _, ob = _make(tmp_path, cfg={"delivery": {"push_per_day": 1}})
    ob.enqueue("d1", GID, "text", {"text": "成品", "push_kind": "delivery"})
    ob.enqueue("s1", GID, "text", {"text": "状态", "push_kind": "status"})
    await ob.flush(NOON)
    rows = _rows(store)
    assert rows[0]["status"] == "sent"
    assert rows[1]["status"] == "pending"  # delivery 已占用当天额度


async def test_flush_timeout_goes_uncertain_and_not_resent(tmp_path):
    """超时 → uncertain，不重试；再 flush 也不碰。"""
    store, _, host, _, _, ob = _make(tmp_path)
    host.text_errors.append(asyncio.TimeoutError())
    ob.enqueue("k1", GID, "text", {"text": "你好", "push_kind": "delivery"})
    await ob.flush(NOON)
    row = _rows(store)[0]
    assert row["status"] == "uncertain"
    sent_before = len(host.texts)
    await ob.flush(NOON + 60)
    assert len(host.texts) == sent_before  # 没重发


async def test_flush_hosterror_timeout_text_goes_uncertain(tmp_path):
    """HostError 消息里带「超时」也算超时类。"""
    store, _, host, _, _, ob = _make(tmp_path)
    host.text_errors.append(HostError("调用宿主能力超时: send.hybrid"))
    ob.enqueue("k1", GID, "text", {"text": "你好", "push_kind": "delivery"})
    await ob.flush(NOON)
    assert _rows(store)[0]["status"] == "uncertain"


async def test_flush_other_error_retries_once_then_failed_and_redacts_secrets(tmp_path):
    """非超时的安全失败：自动重试一次；两次都失败 → failed，error 去密钥截断。"""
    store, _, host, _, _, ob = _make(tmp_path)
    host.text_errors.append(RuntimeError("无效密钥 sk-abcdefghijklmnop Bearer toptoken123"))
    host.text_errors.append(RuntimeError("无效密钥 sk-abcdefghijklmnop Bearer toptoken123"))
    ob.enqueue("k1", GID, "text", {"text": "你好", "push_kind": "delivery"})
    await ob.flush(NOON)
    row = _rows(store)[0]
    assert row["status"] == "pending"            # 排了一次重试
    assert int(row["attempts"]) == 1
    assert float(row["not_before"]) == NOON + 300
    assert "重试" in row["error"]
    assert "sk-abcdefghijklmnop" not in row["error"]
    assert "toptoken123" not in row["error"]
    await ob.flush(NOON + 400)
    row = _rows(store)[0]
    assert row["status"] == "failed"
    assert int(row["attempts"]) == 2             # 首发 + 重试一次，就两次，不再自动发
    assert "sk-abcdefghijklmnop" not in row["error"]
    assert "toptoken123" not in row["error"]
    assert len(row["error"]) <= 320
    assert len(host.texts) == 2
    # 失败不算发出去：不占额度、没留痕
    assert store.read().execute("SELECT COUNT(*) c FROM pushes").fetchone()["c"] == 0


async def test_retry_failed_back_to_pending(tmp_path):
    """手动重发：自动重试用完判 failed 之后，管理员还能手动再来一次。"""
    store, _, host, _, _, ob = _make(tmp_path)
    host.text_errors.append(RuntimeError("boom"))
    host.text_errors.append(RuntimeError("boom"))
    oid = ob.enqueue("k1", GID, "text", {"text": "你好", "push_kind": "delivery"})
    await ob.flush(NOON)
    assert _rows(store)[0]["status"] == "pending"     # 自动重试一次
    await ob.flush(NOON + 400)
    assert _rows(store)[0]["status"] == "failed"
    ob.retry(oid)
    row = _rows(store)[0]
    assert row["status"] == "pending"
    assert row["attempts"] == 3                        # 两次自动 + 这次手动
    await ob.flush(NOON + 500)
    assert _rows(store)[0]["status"] == "sent"
    assert store.read().execute("SELECT COUNT(*) c FROM pushes").fetchone()["c"] == 1


async def test_retry_only_failed_or_uncertain(tmp_path):
    store, *_r, ob = _make(tmp_path)
    oid = ob.enqueue("k1", GID, "text", {"text": "hi", "push_kind": "delivery"})
    with pytest.raises(ValueError):
        ob.retry(oid)  # pending 不能 retry


async def test_recover_marks_sending_uncertain(tmp_path):
    """插件重启：sending 状态（上次中断）→ uncertain，不重放。"""
    store, _, host, _, _, ob = _make(tmp_path)
    oid = ob.enqueue("k1", GID, "text", {"text": "hi", "push_kind": "delivery"})
    with store.tx() as conn:
        conn.execute("UPDATE outbox SET status='sending' WHERE id=?", (oid,))
    ob.recover()
    assert _rows(store)[0]["status"] == "uncertain"
    await ob.flush(NOON)
    assert not host.texts  # 不重放


async def test_not_before_in_future_not_sent(tmp_path):
    store, _, host, _, _, ob = _make(tmp_path)
    ob.enqueue("k1", GID, "text", {"text": "hi", "push_kind": "delivery"}, not_before=NOON + 3600)
    await ob.flush(NOON)
    assert _rows(store)[0]["status"] == "pending"
    assert not host.texts


# ----------------------------------------------------------------------
# 群文件：上传成功补说明；失败不重试
# ----------------------------------------------------------------------


async def test_file_upload_success_then_note(tmp_path):
    store, _, host, pushes, _, ob = _make(tmp_path)
    (tmp_path / "ws").mkdir(exist_ok=True)
    f = tmp_path / "ws" / "报告.html"  # S3 起上传文件必须在 workspace_root 下
    f.write_text("<html>", encoding="utf-8")
    oid = ob.enqueue(
        "f1", GID, "file",
        {"path": str(f), "name": "报告.html", "note": "成品报告做好了，在群文件里", "push_kind": "delivery"},
    )
    await ob.flush(NOON)
    rows = _rows(store)
    assert len(rows) == 2  # 原条目 + 说明
    file_row = rows[0]
    assert file_row["status"] == "sent"
    assert json.loads(file_row["result"])["file_id"] == "file-id-123"
    note_row = rows[1]
    assert note_row["kind"] == "text"
    assert note_row["key"] == "f1:note"
    assert json.loads(note_row["payload"])["text"] == "成品报告做好了，在群文件里"
    # 说明这条也发出去了（同一次 flush）
    assert note_row["status"] == "sent"
    assert host.uploads[0]["name"] == "报告.html"


async def test_file_upload_failure_not_retried(tmp_path):
    """群文件上传不幂等：失败 → failed，绝不自动重试。"""
    store, _, host, _, _, ob = _make(tmp_path)
    f = tmp_path / "a.zip"
    f.write_bytes(b"zip")
    host.upload_errors.append(HostError("上传失败: status=error"))
    ob.enqueue("f1", GID, "file", {"path": str(f), "name": "a.zip", "note": "说明", "push_kind": "delivery"})
    await ob.flush(NOON)
    assert _rows(store)[0]["status"] == "failed"
    uploads_before = len(host.uploads)
    await ob.flush(NOON + 60)
    assert len(host.uploads) == uploads_before  # 没重试


async def test_file_upload_timeout_goes_uncertain(tmp_path):
    """上传超时（可能传上去也可能没有）→ uncertain，不重试。"""
    store, _, host, _, _, ob = _make(tmp_path)
    (tmp_path / "ws").mkdir(exist_ok=True)
    f = tmp_path / "ws" / "a.zip"  # S3 起上传文件必须在 workspace_root 下
    f.write_bytes(b"zip")
    host.upload_errors.append(HostError("调用宿主能力超时: api.call"))
    ob.enqueue("f1", GID, "file", {"path": str(f), "name": "a.zip", "note": "说明", "push_kind": "delivery"})
    await ob.flush(NOON)
    assert _rows(store)[0]["status"] == "uncertain"


# ----------------------------------------------------------------------
# here.now 交付 + 回落
# ----------------------------------------------------------------------


async def test_herenow_success_sends_link_note(tmp_path):
    hn = FakeHereNow()
    store, _, host, _, _, ob = _make(tmp_path, herenow=hn)
    d = tmp_path / "site"
    d.mkdir()
    ob.enqueue(
        "h1", GID, "herenow",
        {"dir": str(d), "note": "做好了，打开看看", "title": "周报", "push_kind": "delivery"},
    )
    await ob.flush(NOON)
    rows = _rows(store)
    assert len(rows) == 2
    assert rows[0]["status"] == "sent"
    assert json.loads(rows[0]["result"])["url"] == "https://page.here.now/"
    note_row = rows[1]
    assert note_row["key"] == "h1:note"
    text = json.loads(note_row["payload"])["text"]
    assert "做好了，打开看看" in text and "https://page.here.now/" in text
    assert note_row["status"] == "sent"


async def test_view_delivery_falls_back_to_group_file(tmp_path):
    """view 首选 herenow；失败自动回落群文件（html 本身）。"""
    from CharTyr_MaiWork.maiwork.herenow import HereNowError

    hn = FakeHereNow()
    hn.results.append(HereNowError("发布失败：模拟"))
    store, _, host, _, _, ob = _make(tmp_path, herenow=hn)
    f = _artifact_file(tmp_path, "报告.html", text="<html>")  # 交付路径必须在 artifacts/T-1/ 里
    delivery = Delivery(store, ob)
    _seed_task(store)
    await delivery.deliver_task("T-1", kind="view", path=f, name="报告.html", note="报告做好了")
    await ob.flush(NOON)
    rows = _rows(store)
    # 首选 herenow failed
    assert rows[0]["kind"] == "herenow" and rows[0]["status"] == "failed"
    # 自动回落群文件
    fallback = [r for r in rows if r["key"].endswith(":fallback")]
    assert len(fallback) == 1
    assert fallback[0]["kind"] == "file"
    # 同一次 flush 里继续发回落
    assert fallback[0]["status"] == "sent"
    assert len(host.uploads) == 1


async def test_file_delivery_falls_back_to_herenow(tmp_path):
    """file 首选群文件；失败回落 here.now 附件页。"""
    hn = FakeHereNow()
    store, _, host, _, _, ob = _make(tmp_path, herenow=hn)
    f = _artifact_file(tmp_path, "数据.csv", text="a,b\n1,2")
    host.upload_errors.append(HostError("上传失败"))
    delivery = Delivery(store, ob)
    _seed_task(store)
    await delivery.deliver_task("T-1", kind="file", path=f, name="数据.csv", note="数据做好了")
    await ob.flush(NOON)
    rows = _rows(store)
    assert rows[0]["kind"] == "file" and rows[0]["status"] == "failed"
    fallback = [r for r in rows if r["key"].endswith(":fallback")]
    assert len(fallback) == 1 and fallback[0]["kind"] == "herenow"
    assert fallback[0]["status"] == "sent"
    assert len(hn.calls) == 1
    # 附件页目录里应该有 index.html
    pubdir = hn.calls[0]
    assert (pubdir / "index.html").exists()


async def test_both_channels_failed_sends_final_notice(tmp_path):
    """两条路都失败 → 发兜底说明「成品在 MaiWork 网页里」。"""
    from CharTyr_MaiWork.maiwork.herenow import HereNowError

    hn = FakeHereNow()
    hn.results.append(HereNowError("发布失败：模拟"))
    hn.results.append(HereNowError("发布失败：还是失败"))
    store, _, host, _, _, ob = _make(tmp_path, herenow=hn)
    f = _artifact_file(tmp_path, "报告.html", text="<html>")
    host.upload_errors.append(HostError("上传失败"))
    delivery = Delivery(store, ob)
    _seed_task(store)
    await delivery.deliver_task("T-1", kind="view", path=f, name="报告.html", note="报告做好了")
    await ob.flush(NOON)
    rows = _rows(store)
    assert rows[0]["status"] == "failed"
    fallback = [r for r in rows if r["key"].endswith(":fallback")]
    assert fallback and fallback[0]["status"] == "failed"
    # 兜底说明
    webonly = [r for r in rows if r["key"].endswith(":webonly")]
    assert len(webonly) == 1
    assert webonly[0]["kind"] == "text"
    payload = json.loads(webonly[0]["payload"])
    assert "发群文件和网页都没成功" in payload["text"]
    assert "MaiWork 网页" in payload["text"]
    assert webonly[0]["status"] == "sent"


async def test_sent_file_recovers_missing_note_without_reupload(tmp_path, monkeypatch):
    """上传成功但后续入队崩溃：下一轮补说明，不重复上传文件。"""
    store, _, host, _, _, ob = _make(tmp_path)
    _seed_task(store)
    f = _artifact_file(tmp_path, "结果.txt", text="结果")
    delivery = Delivery(store, ob)
    await delivery.deliver_task("T-1", kind="file", path=f, name="结果.txt", note="结果已交付")
    original = ob._after_sent

    def _boom(*args, **kwargs):
        raise RuntimeError("刚上传成功就故障")

    monkeypatch.setattr(ob, "_after_sent", _boom)
    await ob.flush(NOON)
    assert _rows(store)[0]["status"] == "sent"
    assert not [r for r in _rows(store) if r["key"].endswith(":note")]
    monkeypatch.setattr(ob, "_after_sent", original)
    await ob.flush(NOON)
    assert len(host.uploads) == 1
    notes = [r for r in _rows(store) if r["key"].endswith(":note")]
    assert len(notes) == 1
    assert notes[0]["status"] in ("pending", "sent")


async def test_sent_file_recovers_memo_after_note_queued(tmp_path, monkeypatch):
    """说明已入队但写备忘失败：下一轮补备忘，不重复入队说明或上传。"""
    store, _, host, _, mentions, ob = _make(tmp_path)
    _seed_task(store)
    f = _artifact_file(tmp_path, "结果.txt", text="结果")
    delivery = Delivery(store, ob)
    await delivery.deliver_task("T-1", kind="file", path=f, name="结果.txt", note="结果已交付")
    original = mentions.add

    def _boom(*args, **kwargs):
        raise RuntimeError("备忘落库故障")

    monkeypatch.setattr(mentions, "add", _boom)
    await ob.flush(NOON)
    assert len([r for r in _rows(store) if r["key"].endswith(":note")]) == 1
    assert not store.read().execute("SELECT 1 FROM mentions").fetchone()
    monkeypatch.setattr(mentions, "add", original)
    await ob.flush(NOON)
    assert len(host.uploads) == 1
    assert len([r for r in _rows(store) if r["key"].endswith(":note")]) == 1
    assert store.read().execute("SELECT 1 FROM mentions WHERE key='deliver:1'").fetchone()


async def test_uncertain_does_not_trigger_fallback(tmp_path):
    """超时不确定 → 不回落（可能其实传上去了）。"""
    from CharTyr_MaiWork.maiwork.herenow import HereNowError

    hn = FakeHereNow()
    store, _, host, _, _, ob = _make(tmp_path, herenow=hn)
    f = _artifact_file(tmp_path, "数据.csv", text="a")  # 交付路径必须在 artifacts/T-1/ 里
    host.upload_errors.append(HostError("调用宿主能力超时: api.call"))
    delivery = Delivery(store, ob)
    _seed_task(store)
    await delivery.deliver_task("T-1", kind="file", path=f, name="数据.csv", note="数据")
    await ob.flush(NOON)
    rows = _rows(store)
    assert rows[0]["status"] == "uncertain"
    assert not [r for r in rows if r["key"].endswith(":fallback")]


# ----------------------------------------------------------------------
# delivery_records / undelivered
# ----------------------------------------------------------------------


async def test_delivery_records_and_undelivered(tmp_path):
    hn = FakeHereNow()
    store, _, host, _, _, ob = _make(tmp_path, herenow=hn)
    d = tmp_path / "site"
    d.mkdir()
    delivery = Delivery(store, ob)
    _seed_task(store)
    ob.enqueue(
        "h1", GID, "herenow",
        {"dir": str(d), "note": "看看", "title": "周报", "push_kind": "delivery"},
        task_id="T-1",
    )
    # 还没发：没有 sent，但也没用 failed/uncertain → undelivered False
    assert delivery.undelivered("T-1") is False
    await ob.flush(NOON)
    records = delivery.delivery_records("T-1")
    kinds = {r["kind"] for r in records}
    assert "here.now" in kinds
    main = [r for r in records if r["kind"] == "here.now"][0]
    assert main["url"] == "https://page.here.now/"
    assert main["state"] == "已发"
    assert delivery.undelivered("T-1") is False


async def test_undelivered_true_when_all_failed(tmp_path):
    from CharTyr_MaiWork.maiwork.herenow import HereNowError

    hn = FakeHereNow()
    hn.results.append(HereNowError("失败1"))
    hn.results.append(HereNowError("失败2"))
    store, _, host, _, _, ob = _make(tmp_path, herenow=hn)
    f = _artifact_file(tmp_path, "报告.html", text="<html>")
    host.upload_errors.append(HostError("上传失败"))
    delivery = Delivery(store, ob)
    _seed_task(store)
    await delivery.deliver_task("T-1", kind="view", path=f, name="报告.html", note="看看")
    await ob.flush(NOON)
    assert delivery.undelivered("T-1") is True
    records = delivery.delivery_records("T-1")
    states = {r["state"] for r in records}
    assert "失败" in states
    # 兜底说明也算一条记录（中文 kind）
    assert any(r["kind"] == "网页副本" for r in records)


async def test_completed_without_any_delivery_record_is_marked_undelivered(tmp_path):
    store, *_r, ob = _make(tmp_path)
    delivery = Delivery(store, ob)
    _seed_task(store)
    assert delivery.undelivered("T-1") is True


async def test_completed_text_delivery_failure_is_marked_undelivered(tmp_path):
    store, *_r, ob = _make(tmp_path)
    delivery = Delivery(store, ob)
    _seed_task(store)
    oid = ob.enqueue(
        "task:T-1:deliver:text", GID, "text",
        {"text": "成品文字", "push_kind": "delivery"}, task_id="T-1",
    )
    assert delivery.undelivered("T-1") is False  # 正在排队，暂不报错
    ob._set(oid, status="failed", error="发送失败")
    assert delivery.undelivered("T-1") is True


async def test_delivery_records_empty_for_unknown_task(tmp_path):
    store, *_r, ob = _make(tmp_path)
    delivery = Delivery(store, ob)
    assert delivery.delivery_records("T-999") == []
    assert delivery.undelivered("T-999") is False


async def test_deliver_task_single_html_file_published_as_index(tmp_path):
    """view 单个 html 文件：临时放进目录当 index.html 发布。"""
    hn = FakeHereNow()
    store, _, host, _, _, ob = _make(tmp_path, herenow=hn)
    f = _artifact_file(tmp_path, "漂亮报告.html", text="<html>hi</html>")
    delivery = Delivery(store, ob)
    _seed_task(store)
    await delivery.deliver_task("T-1", kind="view", path=f, name="漂亮报告.html", note="看看")
    await ob.flush(NOON)
    assert len(hn.calls) == 1
    pubdir = hn.calls[0]
    assert (pubdir / "index.html").read_text(encoding="utf-8") == "<html>hi</html>"


# ----------------------------------------------------------------------
# report_error
# ----------------------------------------------------------------------


async def test_report_error_enqueues_and_dedups_10min(tmp_path):
    store, _, host, _, _, ob = _make(tmp_path)
    ok1 = report_error(store, ob, GID, "模型端点 500：服务器错误", NOON)
    assert ok1 is True
    # 同群同指纹 10 分钟内 → False
    ok2 = report_error(store, ob, GID, "模型端点 500：服务器错误", NOON + 300)
    assert ok2 is False
    # 过了 10 分钟 → True
    ok3 = report_error(store, ob, GID, "模型端点 500：服务器错误", NOON + 601)
    assert ok3 is True
    rows = store.read().execute("SELECT * FROM error_reports").fetchall()
    assert len(rows) == 1  # 同指纹覆盖
    await ob.flush(NOON)
    texts = [json.loads(r["payload"])["text"] for r in _rows(store)]
    assert "模型端点 500" in texts[0]


async def test_report_error_fingerprint_ignores_numbers(tmp_path):
    """指纹去掉数字：超时 12 秒和超时 37 秒算同一错误。"""
    store, _, host, _, _, ob = _make(tmp_path)
    ok1 = report_error(store, ob, GID, "调用超时 12 秒", NOON)
    ok2 = report_error(store, ob, GID, "调用超时 37 秒", NOON + 60)
    assert ok1 is True and ok2 is False


async def test_report_error_redacts_secrets(tmp_path):
    store, _, host, _, _, ob = _make(tmp_path)
    report_error(store, ob, GID, "401: invalid key sk-abcdef123456789 api_key=SECRET999 Bearer tok_abc", NOON)
    await ob.flush(NOON)
    payload = json.loads(_rows(store)[0]["payload"])
    assert "sk-abcdef123456789" not in payload["text"]
    assert "SECRET999" not in payload["text"]
    assert "tok_abc" not in payload["text"]
    # 发出去（push_kind=error 不受睡觉限制，半夜也发）
    assert host.texts[0]["text"] == payload["text"]
    # 存的报错记录里也不带密钥
    er = store.read().execute("SELECT * FROM error_reports").fetchall()
    assert er  # 至少一条


async def test_report_error_different_group_not_deduped(tmp_path):
    """不同群同一个错误各报各的。"""
    store, _, host, _, _, ob = _make(tmp_path)
    with store.tx() as conn:
        conn.execute("INSERT INTO groups (group_id, session_id) VALUES ('999', 'sess-9')")
    ok1 = report_error(store, ob, GID, "端点挂了", NOON)
    ok2 = report_error(store, ob, "999", "端点挂了", NOON)
    assert ok1 is True and ok2 is True


async def test_herenow_kind_without_herenow_goes_failed(tmp_path):
    """没配 HereNow 时接到 herenow 条目 → failed（中文原因），不崩。"""
    store, _, host, _, _, ob = _make(tmp_path, herenow=None)
    d = tmp_path / "site"
    d.mkdir()
    ob.enqueue("h1", GID, "herenow", {"dir": str(d), "note": "n", "push_kind": "delivery"})
    await ob.flush(NOON)
    assert _rows(store)[0]["status"] == "failed"


# ---------------------------------------------------------------------------
# 交付路径第二道闸：path 必须在 <工作区>/artifacts/<task_id>/ 里（审核整改 6）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [".", "tasks/x.txt", "artifacts/T-其他/index.html"],
)
async def test_deliver_task_rejects_path_outside_artifact_dir(tmp_path, rel: str):
    """".", "tasks/..."、"artifacts/别的任务/..." → ValueError，一个条目都不入队。"""
    store, _, _, _, _, ob = _make(tmp_path)
    _seed_task(store)
    delivery = Delivery(store, ob)
    if rel == ".":
        target = tmp_path / "ws"
    else:
        target = tmp_path / "ws" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_dir():
        target.mkdir(parents=True, exist_ok=True)
    else:
        target.write_text("<html>别人的</html>", encoding="utf-8")
    with pytest.raises(ValueError) as ei:
        await delivery.deliver_task("T-1", kind="view", path=target, name="x", note="x")
    assert "artifacts" in str(ei.value) or "成品目录" in str(ei.value)
    assert _rows(store) == []          # 没入队
    assert store.read().execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"] == 0


async def test_deliver_task_rejects_symlink_pointing_outside(tmp_path):
    """artifacts/<本任务>/link.html 指向工作区外 → 解析后不在成品目录里 → ValueError。"""
    store, _, _, _, _, ob = _make(tmp_path)
    _seed_task(store)
    delivery = Delivery(store, ob)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("机密", encoding="utf-8")
    d = tmp_path / "ws" / "artifacts" / "T-1"
    d.mkdir(parents=True, exist_ok=True)
    link = d / "link.html"
    link.symlink_to(outside)
    with pytest.raises(ValueError):
        await delivery.deliver_task("T-1", kind="file", path=link, name="link.html", note="x")
    assert _rows(store) == []


async def test_deliver_task_accepts_file_and_dir_inside_artifact_dir(tmp_path):
    """artifacts/<本任务>/index.html 和整个目录 artifacts/<本任务> → 放行。"""
    store, _, _, _, _, ob = _make(tmp_path)
    _seed_task(store, "T-1")
    _seed_task(store, "T-2")
    delivery = Delivery(store, ob)
    f = _artifact_file(tmp_path, "index.html", task_id="T-1")
    oid1 = await delivery.deliver_task("T-1", kind="view", path=f, name="页面", note="看")
    d = tmp_path / "ws" / "artifacts" / "T-2"
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text("<html>目录交付</html>", encoding="utf-8")
    oid2 = await delivery.deliver_task("T-2", kind="view", path=d, name="目录", note="看")
    rows = _rows(store)
    assert len(rows) == 2
    assert rows[0]["id"] == oid1 and rows[1]["id"] == oid2


# ----------------------------------------------------------------------
# 0.8.0 归一：at 透传、image 载荷闸、结果 hook、结果不明的额度保留
# ----------------------------------------------------------------------


async def test_text_passes_at_user_and_at_name(tmp_path):
    """text 载荷里的 at_user / at_name 真实透传给 host（Telegram 退正文是 host 的事）。"""
    store, settings, host, *_r, ob = _make(tmp_path)
    group_push.set_config(store, GID, {"idea_mention_enabled": True}, settings, now=NOON - 10)
    ob.enqueue("k1", GID, "text", {"text": "话说你之前那个怎么样了？", "push_kind": "idea_mention",
                                   "at_user": "31415926", "at_name": "阿柒"})
    ob.enqueue("k2", GID, "text", {"text": "普通一条", "push_kind": "delivery"})
    await ob.flush(NOON)
    assert host.texts[0]["at_user"] == "31415926" and host.texts[0]["at_name"] == "阿柒"
    assert host.texts[1]["at_user"] == "" and host.texts[1]["at_name"] == ""


async def test_image_kind_sends_png_and_rejects_bad_payload_without_retry(tmp_path):
    """image：真 PNG 才发；载荷本身不合法（符号链接 / 越界 / 不是 PNG）直接 failed，不浪费重试。"""
    store, settings, host, *_r, ob = _make(tmp_path)
    group_push.set_config(store, GID, {"news_card_enabled": True}, settings, now=NOON - 10)
    png = tmp_path / "card.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"data")
    ob.enqueue("img1", GID, "image", {"path": str(png), "text": "看全部资讯", "push_kind": "news_card"})
    await ob.flush(NOON)
    row = _rows(store)[0]
    assert row["status"] == "sent"
    assert host.images[0]["png"] == b"\x89PNG\r\n\x1a\ndata"
    assert host.images[0]["text"] == "看全部资讯"
    # 不是 PNG：判失败，而且 attempts 只有 1（没有自动重试）
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"GIF89a")
    ob.enqueue("img2", GID, "image", {"path": str(bad), "text": "", "push_kind": "news_card"})
    await ob.flush(NOON + 1)
    row = [r for r in _rows(store) if r["key"] == "img2"][0]
    assert row["status"] == "failed" and int(row["attempts"]) == 1
    assert "PNG" in row["error"] and len(host.images) == 1


async def test_uncertain_reserves_quota_but_is_not_recorded_as_sent(tmp_path):
    """超时 → uncertain：不写 pushes（不算已发），但额度按「可能已发出」保留。"""
    store, _, host, pushes, _, ob = _make(tmp_path, cfg={"delivery": {"push_per_day": 1}})
    host.text_errors.append(asyncio.TimeoutError())
    ob.enqueue("k1", GID, "text", {"text": "开场白", "push_kind": "topic"})
    await ob.flush(NOON)
    assert _rows(store)[0]["status"] == "uncertain"
    assert store.read().execute("SELECT COUNT(*) c FROM pushes").fetchone()["c"] == 0
    assert pushes.count_used(GID, NOON) == 1
    assert pushes.can_push(GID, "topic", NOON + 1) == (False, "今天推够了")
    await ob.flush(NOON + 600)
    assert len(host.texts) == 1          # 不重发


async def test_result_hook_gets_outcome_and_ts(tmp_path):
    """结果 hook：发出去才 sent（带 message_id），并带上这一轮 flush 的时间。"""
    seen: list[dict] = []
    store, _, host, *_r, ob = _make(tmp_path)
    ob.add_result_hook(seen.append)
    ob.enqueue("k1", GID, "text", {"text": "好了", "push_kind": "delivery"})
    await ob.flush(NOON)
    assert seen and seen[0]["outcome"] == "sent"
    assert seen[0]["key"] == "k1" and seen[0]["result"]["message_id"]
    assert float(seen[0]["ts"]) == NOON
