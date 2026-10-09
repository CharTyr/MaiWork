"""交付回落页可读化 + 交付说明 @ 发起人（线上 T-11 实测）。

背景（线上实测）：
- deliver_kind=file 的任务群文件上传失败后回落 here.now，生成的页面只有
  `<p>附件：</p><p><a href='X.docx' download>X.docx</a></p>`——用户点开什么都看不到；
  同任务 artifacts/<任务号>/ 下其实已有同名主干的 .html（手机可看版）没被用上。
- 群里那条交付说明（note + 链接，以及只发文字的交付）没有 @ 发起人。

本文件只用假 host / 假 here.now，不碰真宿主、不发任何请求。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
from CharTyr_MaiWork.maiwork.host import HostError
from CharTyr_MaiWork.maiwork.outbox import Delivery, Outbox
from CharTyr_MaiWork.maiwork.store import Store

pytestmark = pytest.mark.asyncio

BJ = timezone(timedelta(hours=8))
GID = "900000001"
NOON = datetime(2026, 10, 15, 12, tzinfo=BJ).timestamp()
REQUESTER = "31415926"
REQUESTER_NAME = "阿柒"


class OutboxHost:
    """假的宿主发送口：记录 send_text / upload_group_file，可预置错误队列。"""

    def __init__(self) -> None:
        self.texts: list[dict] = []
        self.uploads: list[dict] = []
        self.text_errors: list[Exception] = []
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
        raise AssertionError("本文件不该发图片")

    async def upload_group_file(self, group_id: str, path: str, name: str) -> str:
        self.uploads.append({"group_id": group_id, "path": path, "name": name})
        if self.upload_errors:
            raise self.upload_errors.pop(0)
        return "file-id-123"


class FakeHereNow:
    """假的 HereNow：记录每次发布的目录。"""

    def __init__(self) -> None:
        self.calls: list[Path] = []

    async def publish(self, directory: Path) -> dict:
        self.calls.append(Path(directory))
        return {
            "url": "https://page.here.now/",
            "slug": "s1",
            "expires_ts": NOON + 24 * 3600,
            "claim_url": "https://here.now/c/x",
            "claim_token": "tok",
        }


def _make(tmp_path, *, herenow=None):
    store = Store(tmp_path / "t.db")
    store.migrate()
    settings, _ = load_settings({
        "environments": {"workspace_root": str(tmp_path)},
        "groups": {"serve": [{"group": f"qq:{GID}"}]},
    })
    host = OutboxHost()
    pushes = Pushes(store, lambda: settings)
    mentions = Mentions(store, lambda: settings)
    ob = Outbox(store, host, pushes, mentions, lambda: settings, herenow=herenow)
    with store.tx() as conn:
        conn.execute("INSERT INTO groups (group_id, session_id) VALUES (?, ?)", (GID, "sess-1"))
    return store, settings, host, ob


def _seed_task(
    store: Store,
    task_id: str = "T-11",
    *,
    title: str = "测试任务",
    requester_id: str = "",
    requester_name: str = "",
    delivery_kind: str = "",
) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tasks (id, group_id, workspace, title, status, requester_id,"
            " requester_name, delivery_kind, created, updated)"
            " VALUES (?, ?, 'ws', ?, 'completed', ?, ?, ?, 0, 0)",
            (task_id, GID, title, requester_id, requester_name, delivery_kind),
        )


def _note_name(store: Store, name: str) -> None:
    """名册里给这个人一个当前显示名。"""
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO members (group_id, user_id, name, ts) VALUES (?, ?, ?, ?)",
            (GID, REQUESTER, name, NOON),
        )


def _artifact_file(tmp_path, name: str, *, task_id: str = "T-11", text: str = "内容",
                   size: int | None = None) -> Path:
    d = tmp_path / "ws" / "artifacts" / task_id
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    if size is None:
        f.write_text(text, encoding="utf-8")
    else:
        f.write_bytes(b"x" * size)
    return f


def _page_of(hn: FakeHereNow) -> str:
    assert hn.calls, "here.now 没被调用"
    return (hn.calls[0] / "index.html").read_text(encoding="utf-8")


def _rows(store: Store):
    return store.read().execute(
        "SELECT id, key, kind, status, payload, error, task_id FROM outbox ORDER BY id"
    ).fetchall()


async def _fail_file_then_flush(tmp_path, hn, path: Path, name: str, note: str = "数据做好了"):
    """走一遍真流程：file 首选失败 → 自动回落 here.now。"""
    store, settings, host, ob = _make(tmp_path, herenow=hn)
    _seed_task(store)
    host.upload_errors.append(HostError("上传失败"))
    delivery = Delivery(store, ob)
    await delivery.deliver_task("T-11", kind="file", path=path, name=name, note=note)
    await ob.flush(NOON)
    fb = [r for r in _rows(store) if r["key"].endswith(":fallback")]
    assert len(fb) == 1 and fb[0]["kind"] == "herenow" and fb[0]["status"] == "sent"
    return store, host, ob, Path(json.loads(fb[0]["payload"])["dir"])


# ----------------------------------------------------------------------
# 1a. 回落页优先用同目录同名主干 .html / 成品目录根 index.html
# ----------------------------------------------------------------------


async def test_file_fallback_uses_sibling_html_as_page_body(tmp_path):
    """X.docx 旁边有 X.html（手机可看版）→ 那份网页当页面主体，原文件一起可下载。"""
    hn = FakeHereNow()
    docx = _artifact_file(tmp_path, "ZATO防剧透入门表.docx", size=2048)
    sibling = docx.with_suffix(".html")
    sibling.write_text(
        "<!doctype html><html><head><meta charset='utf-8'><title>手机版</title></head>"
        "<body><h1>手机可看版正文</h1></body></html>",
        encoding="utf-8",
    )
    store, host, ob, pubdir = await _fail_file_then_flush(
        tmp_path, hn, docx, "ZATO防剧透入门表.docx"
    )
    page = _page_of(hn)
    assert "手机可看版正文" in page          # 主体是那份网页
    assert "ZATO防剧透入门表.docx" in page   # 原文件下载链接还在
    assert "download" in page
    assert (pubdir / "ZATO防剧透入门表.docx").exists()   # 原文件同批发布


async def test_file_fallback_uses_artifact_root_index_html(tmp_path):
    """没有同名 .html，但成品目录根下有 index.html → 用它当页面主体。"""
    hn = FakeHereNow()
    docx = _artifact_file(tmp_path, "报告.docx", size=1024)
    (docx.parent / "index.html").write_text(
        "<!doctype html><html><body><h1>成品目录的 index 页</h1></body></html>",
        encoding="utf-8",
    )
    store, host, ob, pubdir = await _fail_file_then_flush(tmp_path, hn, docx, "报告.docx")
    page = _page_of(hn)
    assert "成品目录的 index 页" in page
    assert "报告.docx" in page
    assert (pubdir / "报告.docx").exists()


async def test_file_fallback_html_already_linked_is_not_duplicated(tmp_path):
    """同名 .html 自己已经链了原文件 → 不重复塞一个下载条。"""
    hn = FakeHereNow()
    docx = _artifact_file(tmp_path, "表.docx", size=64)
    docx.with_suffix(".html").write_text(
        "<!doctype html><html><body>正文<a href='表.docx' download>下载原文件</a></body></html>",
        encoding="utf-8",
    )
    await _fail_file_then_flush(tmp_path, hn, docx, "表.docx")
    page = _page_of(hn)
    assert page.count("表.docx") == 1


async def test_file_fallback_reads_non_utf8_page(tmp_path):
    """同名网页是 GBK（老工具产出）→ 照样读对，并按 utf-8 重新写索引页。"""
    hn = FakeHereNow()
    docx = _artifact_file(tmp_path, "老表.docx", size=128)
    html = (
        "<!doctype html><html><head>"
        "<meta http-equiv='Content-Type' content='text/html; charset=gbk'>"
        "</head><body><h1>手机可看版正文</h1></body></html>"
    )
    docx.with_suffix(".html").write_bytes(html.encode("gbk"))
    await _fail_file_then_flush(tmp_path, hn, docx, "老表.docx")
    page = (hn.calls[0] / "index.html").read_bytes().decode("utf-8")   # 必须是合法 utf-8
    assert "手机可看版正文" in page
    assert 'charset="utf-8"' in page


async def test_download_bar_appended_when_page_has_no_body_tag(tmp_path):
    """同名网页是片段（没有 </body>）→ 下载条照样接在后面。"""
    hn = FakeHereNow()
    docx = _artifact_file(tmp_path, "片段.docx", size=32)
    docx.with_suffix(".html").write_text("<h1>片段页正文</h1>", encoding="utf-8")
    await _fail_file_then_flush(tmp_path, hn, docx, "片段.docx")
    page = _page_of(hn)
    assert "片段页正文" in page
    assert "片段.docx" in page and "download" in page
    assert page.rstrip().endswith("</a></div>")


# ----------------------------------------------------------------------
# 1b. 没有网页可用 → 生成手机友好页（转义、内联 CSS、不引外部资源）
# ----------------------------------------------------------------------


async def test_generated_page_is_mobile_friendly(tmp_path):
    hn = FakeHereNow()
    csv = _artifact_file(tmp_path, "数据.csv", text="a,b\n1,2")
    store, host, ob, pubdir = await _fail_file_then_flush(
        tmp_path, hn, csv, "数据.csv", note="数据表做好了，手机上点按钮下载"
    )
    page = _page_of(hn)
    assert 'charset="utf-8"' in page
    assert 'name="viewport"' in page and "width=device-width" in page
    assert "测试任务" in page                          # 任务标题
    assert "数据表做好了，手机上点按钮下载" in page      # 交付说明
    assert "数据.csv" in page and "B" in page          # 文件名 + 大小
    assert "download" in page and "<a " in page         # 醒目下载按钮
    assert "<style>" in page                            # 内联 CSS
    assert "http://" not in page and "https://" not in page   # 不引外部资源
    assert (pubdir / "数据.csv").exists()


async def test_generated_page_escapes_title_note_and_name(tmp_path):
    hn = FakeHereNow()
    evil_name = "<img src=x onerror=1>.bin"   # 文件名不能含路径分隔符，注入用别的标签测
    f = _artifact_file(tmp_path, evil_name, size=10)
    store, settings, host, ob = _make(tmp_path, herenow=hn)
    _seed_task(store, title="<script>alert(1)</script>")
    host.upload_errors.append(HostError("上传失败"))
    delivery = Delivery(store, ob)
    await delivery.deliver_task("T-11", kind="file", path=f, name=evil_name,
                                note="说明 <b>注</b>")
    await ob.flush(NOON)
    page = _page_of(hn)
    for raw in ("<script>alert(1)</script>", "<img src=x onerror=1>", "<b>注</b>"):
        assert raw not in page, f"没转义：{raw}"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "&lt;img src=x onerror=1&gt;" in page
    assert "说明 &lt;b&gt;注&lt;/b&gt;" in page


async def test_fallback_page_has_no_internal_paths(tmp_path):
    """生成页和同名网页页都不许出现工作区路径 / artifacts/、steps/、research.md。"""
    hn = FakeHereNow()
    f = _artifact_file(tmp_path, "成品.docx", size=32)
    await _fail_file_then_flush(tmp_path, hn, f, "成品.docx")
    page = _page_of(hn)
    assert str(tmp_path) not in page
    for bad in ("artifacts/", "steps/", "research.md", ".web/"):
        assert bad not in page


# ----------------------------------------------------------------------
# 2. 交付说明消息带上发起人（note + 链接 / 只发文字的交付）
# ----------------------------------------------------------------------


async def test_text_delivery_ats_requester(tmp_path):
    """deliver_kind=text：只发文字的交付要 @ 发起人。"""
    store, settings, host, ob = _make(tmp_path)
    _seed_task(store, requester_id=REQUESTER, requester_name="老快照")
    _note_name(store, REQUESTER_NAME)
    ob.enqueue(
        "task:T-11:deliver:text", GID, "text",
        {"text": "做好了：一个表格", "push_kind": "delivery"}, task_id="T-11",
    )
    await ob.flush(NOON)
    assert host.texts[0]["at_user"] == REQUESTER
    assert host.texts[0]["at_name"] == REQUESTER_NAME
    assert host.texts[0]["text"] == "做好了：一个表格"  # 正文不动（QQ 走真 at 段）


async def test_file_delivery_note_ats_requester(tmp_path):
    """群文件传成功后的那条说明消息要 @ 发起人。"""
    store, settings, host, ob = _make(tmp_path)
    _seed_task(store, requester_id=REQUESTER, requester_name="老快照")
    _note_name(store, REQUESTER_NAME)
    f = _artifact_file(tmp_path, "结果.txt")
    delivery = Delivery(store, ob)
    await delivery.deliver_task("T-11", kind="file", path=f, name="结果.txt", note="结果已交付")
    await ob.flush(NOON)
    assert len(host.uploads) == 1
    assert len(host.texts) == 1
    assert "结果已交付" in host.texts[0]["text"]
    assert host.texts[0]["at_user"] == REQUESTER and host.texts[0]["at_name"] == REQUESTER_NAME


async def test_herenow_link_note_ats_requester(tmp_path):
    """here.now 成功后的「note + 链接」那条说明也要 @ 发起人。"""
    hn = FakeHereNow()
    store, settings, host, ob = _make(tmp_path, herenow=hn)
    _seed_task(store, requester_id=REQUESTER, requester_name="老快照")
    _note_name(store, REQUESTER_NAME)
    d = tmp_path / "site"
    d.mkdir()
    ob.enqueue(
        "task:T-11:deliver", GID, "herenow",
        {"dir": str(d), "note": "做好了，打开看看", "title": "周报", "push_kind": "delivery"},
        task_id="T-11",
    )
    await ob.flush(NOON)
    note = [t for t in host.texts if "https://page.here.now/" in t["text"]]
    assert len(note) == 1
    assert note[0]["at_user"] == REQUESTER and note[0]["at_name"] == REQUESTER_NAME


async def test_awaited_delivery_keeps_at_requester(tmp_path):
    """明确领取（awaited_delivery）不占额度，但 @ 发起人照旧。"""
    store, settings, host, ob = _make(tmp_path)
    _seed_task(store, requester_id=REQUESTER)
    _note_name(store, REQUESTER_NAME)
    oid = ob.enqueue(
        "task:T-11:deliver:text", GID, "text",
        {"text": "你要的那份", "push_kind": "delivery"}, task_id="T-11",
    )
    assert ob.claim_delivery(GID, "T-11") == "queued"
    await ob.flush(NOON)
    row = [r for r in _rows(store) if int(r["id"]) == oid][0]
    assert json.loads(row["payload"])["push_kind"] == "awaited_delivery"
    assert host.texts[0]["at_user"] == REQUESTER


async def test_requester_name_uses_snapshot_when_no_roster_name(tmp_path):
    """名册里没有这个人 → 用任务里的 requester_name 老快照当 at_name。"""
    store, settings, host, ob = _make(tmp_path)
    _seed_task(store, requester_id=REQUESTER, requester_name="老张")
    ob.enqueue(
        "task:T-11:deliver:text", GID, "text",
        {"text": "做好了", "push_kind": "delivery"}, task_id="T-11",
    )
    await ob.flush(NOON)
    assert host.texts[0]["at_user"] == REQUESTER
    assert host.texts[0]["at_name"] == "老张"


async def test_no_requester_keeps_old_call_shape(tmp_path):
    """发起人缺失（管理员/网页代建）→ 不 at，保持老调用形状。"""
    store, settings, host, ob = _make(tmp_path)
    _seed_task(store)
    ob.enqueue(
        "task:T-11:deliver:text", GID, "text",
        {"text": "做好了", "push_kind": "delivery"}, task_id="T-11",
    )
    await ob.flush(NOON)
    assert host.texts[0]["at_user"] == "" and host.texts[0]["at_name"] == ""


async def test_non_delivery_text_is_not_atted(tmp_path):
    """状态提问 / 报错这类非交付文本不动：不给它们塞 at。"""
    store, settings, host, ob = _make(tmp_path)
    _seed_task(store, requester_id=REQUESTER)
    _note_name(store, REQUESTER_NAME)
    ob.enqueue(
        "ask:T-11:1", GID, "text",
        {"text": "@阿柒 缺个链接，发一下", "push_kind": "status"}, task_id="T-11",
    )
    await ob.flush(NOON)
    assert host.texts[0]["at_user"] == "" and host.texts[0]["at_name"] == ""
    assert host.texts[0]["text"] == "@阿柒 缺个链接，发一下"


async def test_payload_at_fields_are_not_overwritten(tmp_path):
    """载荷自己带了 at（比如构想提一嘴）→ 原样透传，不被任务发起人覆盖。"""
    store, settings, host, ob = _make(tmp_path)
    _seed_task(store, requester_id=REQUESTER)
    _note_name(store, REQUESTER_NAME)
    ob.enqueue(
        "task:T-11:deliver:text", GID, "text",
        {"text": "给你留的", "push_kind": "delivery", "at_user": "999",
         "at_name": "别人"},
        task_id="T-11",
    )
    await ob.flush(NOON)
    assert host.texts[0]["at_user"] == "999" and host.texts[0]["at_name"] == "别人"


async def test_requester_of_other_group_is_not_used(tmp_path):
    """任务不属于这个群 → 不拿它的发起人（只 @ 本群的本人）。"""
    store, settings, host, ob = _make(tmp_path)
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tasks (id, group_id, workspace, title, status, requester_id,"
            " requester_name, created, updated)"
            " VALUES ('T-11', '999', 'ws', '别的群的任务', 'completed', ?, ?, 0, 0)",
            (REQUESTER, REQUESTER_NAME),
        )
        conn.execute("INSERT INTO groups (group_id, session_id) VALUES ('999', 'sess-9')")
    ob.enqueue(
        "task:T-11:deliver:text", GID, "text",
        {"text": "做好了", "push_kind": "delivery"}, task_id="T-11",
    )
    await ob.flush(NOON)
    assert host.texts[0]["at_user"] == ""


# ----------------------------------------------------------------------
# 管理员重发的交付说明：措辞和 coordinator 的交付兜底同一套
# ----------------------------------------------------------------------


def _note_fallback():
    """延迟导入：先写测试跑红时函数还不存在 → 算失败（不是 skip、不是假绿）。"""
    from CharTyr_MaiWork.maiwork import outbox

    return outbox._note_fallback


async def test_reissue_note_wording_matches_delivery_fallback():
    """兜底措辞：`<标题>弄好了，点开就能看`（file / text 各有说法），内部词刮掉、≤60 字。"""
    note = _note_fallback()
    assert note("月度账单", "file") == "月度账单弄好了，文件在链接里"
    assert note("韩国银行事件", "text") == "韩国银行事件弄好了，就这几句"
    assert note("月度账单", "view") == "月度账单弄好了，点开就能看"
    assert note("", "view") == "弄好了，点开就能看"
    dirty = note("T-11 的成品已经放好了", "file")
    assert "T-11" not in dirty and dirty.endswith("弄好了，文件在链接里")
    assert len(note("标题" * 40, "view")) <= 60


async def test_reenqueue_missing_uses_new_note_wording(tmp_path):
    """管理员重发（reenqueue_missing）的说明走同一套兜底措辞，并 @ 发起人。"""
    store, settings, host, ob = _make(tmp_path)
    _seed_task(store, title="月度账单", requester_id=REQUESTER, delivery_kind="text")
    _note_name(store, REQUESTER_NAME)
    delivery = Delivery(store, ob)
    assert await delivery.reenqueue_missing("T-11", None) is True
    await ob.flush(NOON)
    assert host.texts[0]["text"] == "月度账单弄好了，就这几句"
    assert host.texts[0]["at_user"] == REQUESTER
