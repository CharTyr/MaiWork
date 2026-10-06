"""0.8.0「往群里发」归一：每群一份设置（group_push）+ 三种自发消息真正共用发件箱。

这一份测的是**跨模块的契约**，不是某一个模块的内部实现：

1. `group_push` 是唯一的配置来源（kv `group_push.<群号>`）：首次 seed 现有设置、惰性迁移
   旧 `cardpush.<群号>` 后把旧源删掉、写时原子校验、非服务群零读取 / 拒绝写、按群隔离。
2. 三种自发消息（冷场开场白 `topic` / 资讯卡片 `news_card` / 构想提一嘴 `idea_mention`）
   共用一个每日总上限和一份睡觉时段：2 个额度跨三种最多发 2 条。
3. 真正的发送只有发件箱一条路：生产者只 enqueue（不标 opener / 不标 sent），发件箱在
   发送前再查一遍服务群 / 开关 / 睡觉时段 / 额度，成功后用结果 hook 回写各自的表。
4. 失败与超时：非超时的安全失败最多自动重试一次（attempts 落库、时间落库）；
   超时 / 重启中断标 uncertain，绝不重发；失败不占额度、不算 sent，uncertain 保留额度，
   网页视图里也不写成「已发」。
5. 图片走发件箱：严格路径闸（符号链接 / 越界 / 大小 / 类型），文本支持 at_user / at_name 透传。

全部用真 Store + 假 Host，不联网、不起浏览器。没配 Outbox 的生产者必须明确拒绝发送
（测试里一律显式构造真 Outbox，不用假成功）。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import card_push, group_push
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
from CharTyr_MaiWork.maiwork.host import HostError
from CharTyr_MaiWork.maiwork.outbox import Outbox
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.topics import Topics

pytestmark = pytest.mark.asyncio

BJ = timezone(timedelta(hours=8))
GID = "900000001"
OTHER = "555000"
UID = "31415926"

# 一张「够真」的 PNG：发件箱按魔数判类型（不需要真能解码）
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _ts(hour: int, minute: int = 0, *, day: int = 15) -> float:
    """北京时间 2026-10-{day} 某时刻。"""
    return datetime(2026, 10, day, hour, minute, tzinfo=BJ).timestamp()


NOON = _ts(12)
NEXT_DAY = _ts(9, 0, day=16)


class PushHost:
    """假宿主发送口：只记录，不联网；可按队列注入错误。"""

    def __init__(self) -> None:
        self.texts: list[dict] = []
        self.images: list[dict] = []
        self.uploads: list[dict] = []
        self.text_errors: list[BaseException] = []
        self.image_errors: list[BaseException] = []
        self.upload_errors: list[BaseException] = []
        self._seq = 0

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq}"

    async def send_text(self, session_id, text, *, reply_to="", at_user="", at_name=""):
        self.texts.append({
            "session_id": session_id, "text": text, "reply_to": reply_to,
            "at_user": at_user, "at_name": at_name,
        })
        if self.text_errors:
            raise self.text_errors.pop(0)
        return type("R", (), {"sent": True, "message_id": self._next_id("m")})()

    async def send_image(self, session_id, png, *, text=""):
        self.images.append({"session_id": session_id, "png": png, "text": text})
        if self.image_errors:
            raise self.image_errors.pop(0)
        return type("R", (), {"sent": True, "message_id": self._next_id("img")})()

    async def messages(self, session_id, start, end, limit, *, limit_mode="latest"):
        return []

    async def upload_group_file(self, group_id, path, name):
        self.uploads.append({"group_id": group_id, "path": path, "name": name})
        if self.upload_errors:
            raise self.upload_errors.pop(0)
        return "file-1"


class World:
    def __init__(self, store, settings, host, pushes, mentions, ob, root: Path) -> None:
        self.store = store
        self.settings = settings
        self.host = host
        self.pushes = pushes
        self.mentions = mentions
        self.ob = ob
        self.root = root


def _make(tmp_path, *, cfg: dict | None = None, groups=(GID,), sub: str = "w") -> World:
    root = tmp_path / sub
    root.mkdir(parents=True, exist_ok=True)
    store = Store(root / "t.db")
    store.migrate()
    merged = {
        "environments": {"workspace_root": str(root)},
        "groups": {"serve": [{"group": f"qq:{g}"} for g in groups]},
    }
    if cfg:
        merged.update(cfg)
    settings, _ = load_settings(merged)
    host = PushHost()
    pushes = Pushes(store, lambda: settings)
    mentions = Mentions(store, lambda: settings)
    ob = Outbox(store, host, pushes, mentions, lambda: settings)
    with store.tx() as conn:
        for g in set(groups) | {OTHER}:
            conn.execute(
                "INSERT INTO groups (group_id, session_id, name, token) VALUES (?, ?, '测试群', ?)",
                (g, f"sess-{g}", f"tok{g}"),
            )
    return World(store, settings, host, pushes, mentions, ob, root)


def _outbox_rows(store: Store) -> list:
    return store.read().execute(
        "SELECT id, key, group_id, kind, payload, status, attempts, not_before, error, result"
        " FROM outbox ORDER BY id"
    ).fetchall()


def _push_rows(store: Store) -> list:
    return store.read().execute("SELECT group_id, day, kind, text FROM pushes ORDER BY id").fetchall()


# ---------------------------------------------------------------------------
# 1. group_push：一份真源
# ---------------------------------------------------------------------------


async def test_seed_from_settings_once_and_kv_is_the_source(tmp_path):
    """首次读：把现有设置（开关 / 总上限 / 睡觉时段）seed 进 group_push.<群号>。"""
    world = _make(tmp_path, cfg={
        "topics": {"enabled": False},
        "delivery": {"push_per_day": 2, "quiet_hours": "22:00-07:30"},
    })
    cfg = group_push.get_config(world.store, GID, world.settings)
    assert cfg["topics_enabled"] is False
    assert cfg["daily_max"] == 2                     # 沿用原 push_per_day，不加大默认
    assert cfg["quiet_hours"] == "22:00-07:30"
    assert cfg["news_card_enabled"] is False         # 旧卡片开关默认关
    assert cfg["news_card_count"] == 3
    assert cfg["idea_mention_enabled"] is False
    # 落库的键就是父会话要用的那个常量
    raw = world.store.kv_get(f"{group_push.KV_PREFIX}{GID}")
    assert raw["daily_max"] == 2 and raw["quiet_hours"] == "22:00-07:30"
    # 已经 seed 过的群以 kv 为准（一份真源）
    with world.store.tx() as conn:
        world.store.kv_set(conn, f"{group_push.KV_PREFIX}{GID}", {**raw, "daily_max": 9})
    assert group_push.get_config(world.store, GID, world.settings)["daily_max"] == 9


async def test_retired_kind_daily_caps_are_gone(tmp_path):
    """旧的每类每日上限退役：配置里只有 daily_max 一个总数。"""
    world = _make(tmp_path)
    cfg = group_push.get_config(world.store, GID, world.settings)
    assert "news_card_daily_max" not in cfg and "idea_mention_daily_max" not in cfg
    assert cfg["daily_max"] == int(world.settings.delivery.push_per_day) == 3


async def test_lazy_migrate_legacy_cardpush_then_delete_source(tmp_path):
    """旧 kv cardpush.<群号> 惰性迁移：开关 / 条数 / since 带过来，旧键删掉，重复读幂等。"""
    world = _make(tmp_path)
    since = NOON - 3600
    with world.store.tx() as conn:
        world.store.kv_set(conn, f"{card_push.LEGACY_KV_PREFIX}{GID}", {
            "news_card_enabled": True,
            "news_card_count": 2,
            "news_card_daily_max": 9,       # 退役字段：不迁
            "idea_mention_enabled": True,
            "news_card_since": since,
        })
    cfg = group_push.get_config(world.store, GID, world.settings)
    assert cfg["news_card_enabled"] is True
    assert cfg["news_card_count"] == 2
    assert cfg["idea_mention_enabled"] is True
    assert float(cfg["news_card_since"]) == since
    assert cfg["daily_max"] == 3            # 上限只认 delivery.push_per_day
    assert world.store.kv_get(f"cardpush.{GID}", None) is None   # 旧源删掉，不再两份
    before = world.store.kv_get(f"{group_push.KV_PREFIX}{GID}")
    again = group_push.get_config(world.store, GID, world.settings)
    assert again == cfg and world.store.kv_get(f"{group_push.KV_PREFIX}{GID}") == before


async def test_set_config_validates_atomically(tmp_path):
    """字段 / 范围不对 → ValueError，而且一个字段都不写。"""
    world = _make(tmp_path)
    body = dict(group_push.get_config(world.store, GID, world.settings))
    for bad in (
        {"daily_max": 99}, {"daily_max": True}, {"daily_max": 2.5},
        {"news_card_count": 4}, {"topics_enabled": "yes"}, {"quiet_hours": "25:00-08:00"},
        {"nope": 1}, {},
    ):
        with pytest.raises(ValueError):
            group_push.set_config(world.store, GID, bad, world.settings)
    assert group_push.get_config(world.store, GID, world.settings) == body  # 原样没动


async def test_set_config_replaces_and_records_switch_since(tmp_path):
    world = _make(tmp_path)
    cfg = group_push.set_config(
        world.store, GID,
        {"news_card_enabled": True, "news_card_count": 1, "daily_max": 5,
         "quiet_hours": "00:00-07:00", "topics_enabled": False},
        world.settings, now=NOON,
    )
    assert cfg["news_card_enabled"] is True and cfg["news_card_count"] == 1
    assert cfg["daily_max"] == 5 and cfg["quiet_hours"] == "00:00-07:00"
    assert float(cfg["news_card_since"]) == NOON      # 开关打开的时刻落库
    assert group_push.get_config(world.store, GID, world.settings) == cfg
    # 再关再开：since 取最近一次打开
    group_push.set_config(world.store, GID, {"news_card_enabled": False}, world.settings, now=NOON + 10)
    again = group_push.set_config(world.store, GID, {"news_card_enabled": True}, world.settings, now=NOON + 20)
    assert float(again["news_card_since"]) == NOON + 20


async def test_unserved_group_zero_read_and_set_refused(tmp_path):
    """非服务群：读不落库（零读取），写直接拒绝。"""
    world = _make(tmp_path, groups=(GID,))
    cfg = group_push.get_config(world.store, OTHER, world.settings)
    assert cfg["daily_max"] == 3 and cfg["news_card_enabled"] is False
    for key in (f"{group_push.KV_PREFIX}{OTHER}", f"cardpush.{OTHER}"):
        assert world.store.kv_get(key, None) is None
    with pytest.raises(ValueError):
        group_push.set_config(world.store, OTHER, {"daily_max": 1}, world.settings)


async def test_config_is_per_group(tmp_path):
    world = _make(tmp_path, groups=(GID, OTHER))
    group_push.set_config(world.store, GID, {"daily_max": 5, "news_card_enabled": True}, world.settings)
    other = group_push.get_config(world.store, OTHER, world.settings)
    assert other["daily_max"] == 3 and other["news_card_enabled"] is False


async def test_kind_enabled_maps_three_kinds_only(tmp_path):
    world = _make(tmp_path)
    cfg = group_push.get_config(world.store, GID, world.settings)
    assert group_push.kind_enabled(cfg, "topic") is True
    assert group_push.kind_enabled(cfg, "news_card") is False
    assert group_push.kind_enabled(cfg, "idea_mention") is False
    # 别的推送（交付 / 状态 / 故障 / 指令）不受这三个开关管
    for kind in ("delivery", "status", "reminder", "error", "command", "admin", "awaited_delivery", ""):
        assert group_push.kind_enabled(cfg, kind) is True


# ---------------------------------------------------------------------------
# 2. 一个总上限 + 一份睡觉时段，跨三种 kind
# ---------------------------------------------------------------------------


async def test_two_quota_across_three_kinds_at_most_two(tmp_path):
    """2 个额度跨三种自制消息最多发 2 条，第三条推到明天（真 Store + 真 Outbox）。"""
    world = _make(tmp_path)
    group_push.set_config(world.store, GID, {
        "daily_max": 2, "news_card_enabled": True, "idea_mention_enabled": True,
    }, world.settings)
    for kind in ("topic", "news_card", "idea_mention"):
        world.ob.enqueue(f"k:{kind}", GID, "text", {"text": f"{kind} 一条", "push_kind": kind})
    await world.ob.flush(NOON)
    rows = {str(r["key"]): r for r in _outbox_rows(world.store)}
    assert rows["k:topic"]["status"] == "sent"
    assert rows["k:news_card"]["status"] == "sent"
    third = rows["k:idea_mention"]
    assert third["status"] == "pending" and float(third["not_before"]) >= _ts(0, day=16)
    assert len(world.host.texts) == 2
    assert world.pushes.count_used(GID, NOON) == 2
    assert [r["kind"] for r in _push_rows(world.store)] == ["topic", "news_card"]
    # 第二天醒来接着发第三条
    await world.ob.flush(NEXT_DAY)
    assert {str(r["key"]): r["status"] for r in _outbox_rows(world.store)}["k:idea_mention"] == "sent"


async def test_quiet_hours_from_group_push(tmp_path):
    world = _make(tmp_path)
    group_push.set_config(world.store, GID, {"quiet_hours": "20:00-08:00"}, world.settings, now=NOON)
    world.ob.enqueue("k:topic", GID, "text", {"text": "开场白", "push_kind": "topic"})
    await world.ob.flush(_ts(21, 0))              # 群自己的睡觉时段里
    row = _outbox_rows(world.store)[0]
    assert row["status"] == "pending" and "睡觉" in row["error"]
    assert world.host.texts == []


async def test_exempt_kinds_unchanged(tmp_path):
    """原豁免清单不动：error / command / admin / awaited_delivery 照旧；三种自制消息不豁免。"""
    world = _make(tmp_path)
    group_push.set_config(world.store, GID, {"daily_max": 1, "quiet_hours": "00:00-00:00"},
                          world.settings, now=NOON - 10)
    for kind in ("error", "command", "admin", "awaited_delivery"):
        ok, why = world.pushes.can_push(GID, kind, NOON)
        assert (ok, why) == (True, ""), kind
    # 卡片 / 提一嘴的开关默认关 → 先报「开关已关」（这一层比额度先查）
    for kind in ("news_card", "idea_mention"):
        assert world.pushes.can_push(GID, kind, NOON) == (False, "开关已关")
    group_push.set_config(world.store, GID, {"news_card_enabled": True, "idea_mention_enabled": True},
                          world.settings, now=NOON - 5)
    world.pushes.record(GID, "topic", "用掉唯一一个额度", NOON)
    for kind in ("topic", "news_card", "idea_mention", "delivery", "status"):
        ok, why = world.pushes.can_push(GID, kind, NOON)
        assert ok is False and why == "今天推够了", kind


async def test_followup_note_does_not_double_count_quota(tmp_path):
    """同一件交付的自动说明（payload.follow_up_of）不再吃第二份额度。"""
    world = _make(tmp_path)
    group_push.set_config(world.store, GID, {"daily_max": 1}, world.settings)
    f = world.root / "报告.html"
    f.write_text("<html>ok</html>", encoding="utf-8")
    world.ob.enqueue("f1", GID, "file", {
        "path": str(f), "name": "报告.html", "note": "成品在群文件里", "push_kind": "delivery",
    })
    await world.ob.flush(NOON)
    rows = {str(r["key"]): r for r in _outbox_rows(world.store)}
    assert rows["f1"]["status"] == "sent"
    assert rows["f1:note"]["status"] == "sent"          # 说明不被额度拦住、也不留到明天
    assert len(_push_rows(world.store)) == 1            # 只记一笔
    assert world.pushes.count_used(GID, NOON) == 1
    assert json.loads(rows["f1:note"]["payload"])["follow_up_of"] == int(rows["f1"]["id"])


# ---------------------------------------------------------------------------
# 3. 发送前再查一遍：服务群 / 开关 / 睡觉时段 / 额度
# ---------------------------------------------------------------------------


async def test_switch_off_before_send_drops_stale_push(tmp_path):
    """待发期间开关关掉 → 直接作废，不发陈旧的开场白 / 卡片。"""
    world = _make(tmp_path)
    group_push.set_config(world.store, GID, {"news_card_enabled": True}, world.settings, now=NOON)
    world.ob.enqueue("news_card:1", GID, "image",
                     {"path": str(world.root / "x.png"), "text": "", "push_kind": "news_card"})
    group_push.set_config(world.store, GID, {"news_card_enabled": False}, world.settings, now=NOON + 1)
    await world.ob.flush(NOON + 2)
    row = _outbox_rows(world.store)[0]
    assert row["status"] == "dropped" and "开关" in row["error"]
    assert world.host.images == [] and world.host.texts == []


async def test_unserved_group_pending_row_never_sent(tmp_path):
    world = _make(tmp_path)
    world.ob.enqueue("k:topic", GID, "text", {"text": "开场白", "push_kind": "topic"})
    await world.ob.flush(NOON, allowed_groups={OTHER})
    assert _outbox_rows(world.store)[0]["status"] == "pending"
    assert world.host.texts == []


# ---------------------------------------------------------------------------
# 4. 失败 / 超时 / 重启
# ---------------------------------------------------------------------------


async def test_safe_failure_retries_once_then_failed_and_not_counted(tmp_path):
    """非超时的安全失败：自动重试一次（attempts / 时间落库）；两次都失败 → failed，不占额度。"""
    world = _make(tmp_path)
    world.host.text_errors = [HostError("send.hybrid 发送失败"), HostError("send.hybrid 发送失败")]
    world.ob.enqueue("k:topic", GID, "text", {"text": "开场白", "push_kind": "topic"})
    await world.ob.flush(NOON)
    row = _outbox_rows(world.store)[0]
    assert row["status"] == "pending"              # 排了一次重试：仍在待发
    assert int(row["attempts"]) == 1
    assert float(row["not_before"]) == NOON + 300
    assert "重试" in row["error"]
    assert _push_rows(world.store) == []           # 失败不算 sent、不占额度
    await world.ob.flush(NOON + 400)
    row = _outbox_rows(world.store)[0]
    assert row["status"] == "failed" and int(row["attempts"]) == 2
    assert len(world.host.texts) == 2              # 首发 + 重试一次，就两次
    await world.ob.flush(NOON + 900)
    assert len(world.host.texts) == 2              # 再不自动发
    assert _push_rows(world.store) == []


async def test_file_upload_failure_still_not_retried(tmp_path):
    """群文件上传不幂等：失败照旧不自动重试。"""
    world = _make(tmp_path)
    f = world.root / "a.zip"
    f.write_bytes(b"zip")
    world.host.upload_errors = [HostError("上传失败")]
    world.ob.enqueue("f1", GID, "file", {"path": str(f), "name": "a.zip", "push_kind": "delivery"})
    await world.ob.flush(NOON)
    row = _outbox_rows(world.store)[0]
    assert row["status"] == "failed" and int(row["attempts"]) == 1
    assert len(world.host.uploads) == 1


async def test_timeout_keeps_quota_reservation_and_view_never_says_sent(tmp_path):
    """超时 → uncertain：不重发、保留额度（防后面刷群）、网页视图不写「已发」。"""
    world = _make(tmp_path)
    group_push.set_config(world.store, GID, {
        "daily_max": 1, "news_card_enabled": True,
    }, world.settings)
    world.host.text_errors = [asyncio.TimeoutError()]
    world.ob.enqueue("k:topic", GID, "text", {"text": "开场白", "push_kind": "topic"})
    world.ob.enqueue("k:news_card", GID, "text", {"text": "卡片", "push_kind": "news_card"})
    await world.ob.flush(NOON)
    rows = {str(r["key"]): r for r in _outbox_rows(world.store)}
    assert rows["k:topic"]["status"] == "uncertain"
    assert rows["k:news_card"]["status"] == "pending"      # 额度被不确定的那条占着
    assert "上限" in rows["k:news_card"]["error"] or "推够" in rows["k:news_card"]["error"]
    await world.ob.flush(NOON + 600)
    assert len(world.host.texts) == 1                      # 不重发
    assert _push_rows(world.store) == []                   # 没当成 sent 记账
    view = group_push.view(world.store, GID, world.settings, now=NOON + 700)
    assert view["sent_today"] == 0
    assert view["quota_used"] == 1                         # 安全保留
    assert view["daily_max"] == 1
    first = [r for r in view["recent"] if r["key"] == "k:topic"][0]
    assert first["state"] == "不确定" and first["uncertain"] is True


async def test_recover_marks_sending_uncertain_and_never_resends(tmp_path):
    world = _make(tmp_path)
    oid = world.ob.enqueue("k:topic", GID, "text", {"text": "开场白", "push_kind": "topic"})
    with world.store.tx() as conn:
        conn.execute("UPDATE outbox SET status='sending' WHERE id=?", (oid,))
    assert world.ob.recover() == 1
    await world.ob.flush(NOON)
    assert _outbox_rows(world.store)[0]["status"] == "uncertain"
    assert world.host.texts == []


# ---------------------------------------------------------------------------
# 5. image 严格路径闸 + 文本 at 透传
# ---------------------------------------------------------------------------


async def test_image_goes_through_outbox_with_strict_path_check(tmp_path):
    world = _make(tmp_path)
    group_push.set_config(world.store, GID, {"news_card_enabled": True}, world.settings)
    png = world.root / "card.png"
    png.write_bytes(PNG)
    world.ob.enqueue("img1", GID, "image",
                     {"path": str(png), "text": "看全部资讯：https://mw.example", "push_kind": "news_card"})
    await world.ob.flush(NOON)
    row = _outbox_rows(world.store)[0]
    assert row["status"] == "sent"
    assert world.host.images[0]["png"] == PNG
    assert world.host.images[0]["text"].startswith("看全部资讯")
    assert json.loads(row["result"])["message_id"]


@pytest.mark.parametrize("case,needle", [
    ("symlink", "符号链接"),
    ("outside", "工作区"),
    ("huge", "大小"),
    ("notpng", "PNG"),
    ("missing", "普通文件"),
])
async def test_image_path_rejects(tmp_path, case, needle):
    world = _make(tmp_path)
    group_push.set_config(world.store, GID, {"news_card_enabled": True}, world.settings)
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    if case == "symlink":
        path = world.root / "link.png"
        path.symlink_to(outside)
    elif case == "outside":
        path = outside
    elif case == "huge":
        path = world.root / "big.png"
        path.write_bytes(PNG + b"\x00" * (9 * 1024 * 1024))
    elif case == "notpng":
        path = world.root / "not.png"
        path.write_bytes(b"GIF89a not a png")
    else:
        path = world.root / "nope.png"
    world.ob.enqueue("img1", GID, "image", {"path": str(path), "text": "", "push_kind": "news_card"})
    await world.ob.flush(NOON)
    row = _outbox_rows(world.store)[0]
    assert row["status"] == "failed", row["error"]        # 载荷本身不合法：不浪费一次自动重试
    assert needle in row["error"]
    assert world.host.images == []


async def test_text_at_user_and_at_name_passthrough(tmp_path):
    """at_user / at_name 真实透传（Telegram 退正文是 host 自己的事）。"""
    world = _make(tmp_path)
    group_push.set_config(world.store, GID, {"idea_mention_enabled": True}, world.settings)
    world.ob.enqueue("k:idea", GID, "text",
                     {"text": "话说你之前想弄的那个战报表怎么样了？", "push_kind": "idea_mention",
                      "at_user": UID, "at_name": "阿柒"})
    await world.ob.flush(NOON)
    assert world.host.texts[0]["at_user"] == UID
    assert world.host.texts[0]["at_name"] == "阿柒"
    assert _outbox_rows(world.store)[0]["status"] == "sent"


# ---------------------------------------------------------------------------
# 6. 结果 hook：只 enqueue 不算 sent，真发成功才回写
# ---------------------------------------------------------------------------


async def test_result_hook_fires_on_every_outcome(tmp_path):
    seen: list[dict] = []
    world = _make(tmp_path)
    group_push.set_config(world.store, GID, {
        "news_card_enabled": True, "idea_mention_enabled": True,
    }, world.settings)
    world.ob.add_result_hook(seen.append)
    world.ob.enqueue("k:ok", GID, "text", {"text": "一条", "push_kind": "topic"})
    await world.ob.flush(NOON)
    by_key = {info["key"]: info for info in seen}
    assert by_key["k:ok"]["outcome"] == "sent"
    assert by_key["k:ok"]["push_kind"] == "topic"
    assert by_key["k:ok"]["result"]["message_id"]
    world.host.text_errors = [asyncio.TimeoutError()]
    world.ob.enqueue("k:slow", GID, "text", {"text": "两条", "push_kind": "news_card"})
    await world.ob.flush(NOON + 1)
    assert [i for i in seen if i["key"] == "k:slow"][-1]["outcome"] == "uncertain"
    # 非超时失败 → failed 也照报（生产者据此把本地行标失败）
    world.host.text_errors = [HostError("boom"), HostError("boom")]
    world.ob.enqueue("k:bad", GID, "text", {"text": "三条", "push_kind": "topic"})
    await world.ob.flush(NOON + 10)
    await world.ob.flush(NOON + 400)
    assert [i for i in seen if i["key"] == "k:bad"][-1]["outcome"] == "failed"


# ---------------------------------------------------------------------------
# 7. 三种生产者：只入队 → 发件箱发 → hook 回写
# ---------------------------------------------------------------------------


class Jev:
    def available(self) -> bool:
        return True

    async def ask(self, state, questions, *, purpose, group_id, timeout_ms=None):
        out = {}
        for k, q in questions.items():
            if not isinstance(q, dict):
                continue
            if q["type"] == "noul":
                out[k] = 0.9
            elif q["type"] == "choice":
                out[k] = ("fine", 0.9, 0.9)
        return out


class Profiles:
    def usual_gap(self, gid, now):
        return 300.0

    def entries(self, gid):
        return []


class Signals:
    def __init__(self, ts: float) -> None:
        self._ts = ts

    def last_ts(self, gid):
        return self._ts

    def session_id(self, gid):
        return f"sess-{gid}"


class Models:
    def __init__(self, opener: str = "话说最近那个新板子你们看了没？", *, guard=None) -> None:
        self._opener = opener
        # 个人提一嘴的发送前复核默认：没结果、没拒绝、也没有明确需要（= 不通过）
        self._guard = guard or {"resolved": False, "declined": False, "need": False, "evidence": []}

    def settings(self):
        return type("S", (), {"ready": lambda self: True})()

    async def chat(self, agent=None, messages=None, **kw):
        if str(kw.get("purpose") or "") == "card_push.idea_guard":
            return type("C", (), {"text": json.dumps(self._guard, ensure_ascii=False)})()
        return type("C", (), {"text": self._opener})()


def _make_topics(world: World, *, ts: float = NOON, outbox=True, opener=None) -> Topics:
    with world.store.tx() as conn:
        conn.execute("UPDATE groups SET last_msg_ts=? WHERE group_id=?", (ts - 3600, GID))
    return Topics(
        world.store, world.host, Models(opener) if opener else Models(), Jev(), Profiles(),
        world.mentions, world.pushes, lambda: world.settings, Signals(ts - 3600),
        outbox=world.ob if outbox else None,
    )


def _seed_candidate(world: World, *, now: float) -> int:
    with world.store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO topic_candidates (group_id, kind, ref_id, title, brief, link, expires_ts, used_ts, created)"
            " VALUES (?, 'news', 7, '新开源 FPGA 开发板', '配置不错', 'http://x', ?, NULL, ?)",
            (GID, now + 6 * 3600, now - 60),
        )
        return int(cur.lastrowid)


async def test_topics_enqueues_then_outbox_writes_back(tmp_path):
    """开场白只入队（不标 opener / 不用候选），发出去之后才回写。"""
    world = _make(tmp_path)
    topics = _make_topics(world)
    _seed_candidate(world, now=NOON)
    out = await topics.check(GID, NOON)
    assert out.startswith("queued:topic_id=")
    log = world.store.read().execute("SELECT id, opener, message_id FROM topic_log").fetchone()
    assert log["opener"] == "" and log["message_id"] == ""
    assert world.host.texts == []
    rows = _outbox_rows(world.store)
    assert len(rows) == 1 and str(rows[0]["key"]) == f"topic:{int(log['id'])}"
    assert rows[0]["status"] == "pending"
    await world.ob.flush(NOON)
    log = world.store.read().execute("SELECT opener, message_id, followup_due_ts FROM topic_log").fetchone()
    assert log["opener"] == "话说最近那个新板子你们看了没？"
    assert log["message_id"]
    assert float(log["followup_due_ts"]) > NOON
    assert world.store.read().execute(
        "SELECT used_ts FROM topic_candidates WHERE group_id=?", (GID,)
    ).fetchone()["used_ts"] is not None
    memo = world.store.read().execute("SELECT key FROM mentions").fetchall()
    assert any(str(r["key"]).startswith("topic:") for r in memo)
    assert [r["kind"] for r in _push_rows(world.store)] == ["topic"]


async def test_topics_without_outbox_refuses_to_send(tmp_path):
    """没接发件箱：明确拒绝（skip:no_outbox），绝不直发第二路。"""
    world = _make(tmp_path)
    topics = _make_topics(world, outbox=False)
    _seed_candidate(world, now=NOON)
    out = await topics.check(GID, NOON)
    assert out == "skip:no_outbox"
    assert world.host.texts == [] and _outbox_rows(world.store) == []


async def test_topics_no_longer_triggers_maibot(tmp_path):
    """speaker=maibot 退役：一律走发件箱的 send_text，不再请 MaiBot 主动开口。"""
    world = _make(tmp_path, cfg={"topics": {"speaker": "maibot"}})
    topics = _make_topics(world)
    _seed_candidate(world, now=NOON)
    assert (await topics.check(GID, NOON)).startswith("queued:")
    await world.ob.flush(NOON)
    assert len(world.host.texts) == 1
    assert not hasattr(world.host, "proactive_trigger_calls")


async def test_topics_uncertain_does_not_mark_opener_sent(tmp_path):
    world = _make(tmp_path)
    topics = _make_topics(world)
    _seed_candidate(world, now=NOON)
    world.host.text_errors = [asyncio.TimeoutError()]
    assert (await topics.check(GID, NOON)).startswith("queued:")
    await world.ob.flush(NOON)
    log = world.store.read().execute("SELECT opener, result FROM topic_log").fetchone()
    assert log["opener"] == ""                       # 没当成已发
    assert "uncertain" in str(log["result"]) or "不确定" in str(log["result"])
    # 候选按「可能已经说了」保留，不再重复挑它
    assert world.store.read().execute(
        "SELECT used_ts FROM topic_candidates WHERE group_id=?", (GID,)
    ).fetchone()["used_ts"] is not None
    assert _push_rows(world.store) == []


async def test_topic_privacy_gate_rejects_before_enqueue(tmp_path):
    """开场白含关注成员的私下注记 → 整条作废，连发件箱都不进（更不会发出去）。"""
    world = _make(tmp_path)
    note = "他偷偷在学钢琴所以晚上常不在线"
    with world.store.tx() as conn:
        conn.execute(
            "INSERT INTO focus_members (group_id, user_id, name, reasons, note, pinned, removed, updated)"
            " VALUES (?, '10001', '阿帆', '[]', ?, 1, 0, ?)",
            (GID, note, NOON),
        )
    topics = _make_topics(world, opener=f"话说 {note}，大家最近都在忙啥？")
    _seed_candidate(world, now=NOON)
    out = await topics.check(GID, NOON)
    assert out == "rejected:privacy", out
    assert _outbox_rows(world.store) == [] and world.host.texts == []
    log = world.store.read().execute("SELECT opener, result FROM topic_log").fetchone()
    assert log["opener"] == "" and "关注成员" in str(log["result"])


async def test_topic_in_flight_blocks_second_opener(tmp_path):
    """已经有一条待发开场白在发件箱里 → 不再入队第二条。"""
    world = _make(tmp_path)
    topics = _make_topics(world)
    _seed_candidate(world, now=NOON)
    world.ob.enqueue("topic:999", GID, "text", {"text": "上一条还在排队", "push_kind": "topic"})
    out = await topics.check(GID, NOON)
    assert out == "skip:opener_in_flight"
    assert len(_outbox_rows(world.store)) == 1


class Renderer:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, data):
        self.calls += 1
        return PNG


def _enable_card(world: World, **patch):
    body = {"news_card_enabled": True}
    body.update(patch)
    return group_push.set_config(world.store, GID, body, world.settings, now=NOON - 3600)


def _batch(world: World, *, created=NOON, n=3) -> tuple[int, list[int]]:
    with world.store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
            " VALUES (?, ?, 10, ?, 0, '', ?)",
            (GID, created, n, created),
        )
        bid = int(cur.lastrowid)
        ids = []
        for i in range(n):
            cur = conn.execute(
                "INSERT INTO news_items (batch_id, group_id, title, summary, why, sources, score, created,"
                " kind, image_url, keywords, target_user_id, rejected)"
                " VALUES (?, ?, ?, '摘要', '值得看', '[]', ?, ?, 'news', '', '[\"k\"]', '', 0)",
                (bid, GID, f"标题{bid}-{i}", 4.0 - i, created),
            )
            ids.append(int(cur.lastrowid))
    return bid, ids


async def test_card_push_enqueues_image_then_hook_settles(tmp_path):
    world = _make(tmp_path, cfg={"console": {"public_url": "https://mw.example"}})
    renderer = Renderer()
    cp = card_push.CardPush(world.store, world.host, world.pushes, world.mentions,
                            lambda: world.settings, renderer=renderer, outbox=world.ob)
    _enable_card(world, news_card_count=2)
    bid, ids = _batch(world)
    assert cp.scan(GID, NOON + 60) == 1
    await cp.flush(GID, NOON + 60)
    card = world.store.read().execute("SELECT * FROM news_cards").fetchone()
    assert card["status"] == "queued" and card["sent_ts"] is None      # 只入队，没发
    row = _outbox_rows(world.store)[0]
    assert row["kind"] == "image" and str(row["key"]) == f"news_card:{int(card['id'])}"
    payload = json.loads(row["payload"])
    assert payload["push_kind"] == "news_card" and Path(payload["path"]).is_file()
    assert "https://mw.example/#/tok900000001/news" in payload["text"]
    await world.ob.flush(NOON + 61)
    assert world.host.images and world.host.images[0]["png"] == PNG
    card = world.store.read().execute("SELECT * FROM news_cards").fetchone()
    assert card["status"] == "sent" and card["sent_ts"] is not None and card["mode"] == "image"
    assert [r["kind"] for r in _push_rows(world.store)] == ["news_card"]
    memo = world.store.read().execute("SELECT text FROM mentions WHERE group_id=?", (GID,)).fetchall()
    assert any("资讯卡片" in r["text"] for r in memo)
    assert json.loads(card["item_ids"]) == ids[:2]


async def test_card_image_render_cached_once_across_retry(tmp_path):
    """画一次图落盘缓存：发送失败重试用的是同一个文件，不重画。"""
    world = _make(tmp_path)
    renderer = Renderer()
    cp = card_push.CardPush(world.store, world.host, world.pushes, world.mentions,
                            lambda: world.settings, renderer=renderer, outbox=world.ob)
    _enable_card(world)
    _batch(world)
    cp.scan(GID, NOON + 10)
    await cp.flush(GID, NOON + 10)
    path = json.loads(_outbox_rows(world.store)[0]["payload"])["path"]
    world.host.image_errors = [HostError("send.hybrid 发送失败")]
    await world.ob.flush(NOON + 20)
    assert _outbox_rows(world.store)[0]["status"] == "pending"      # 排了重试
    await world.ob.flush(NOON + 400)
    assert world.host.images and renderer.calls == 1
    assert json.loads(_outbox_rows(world.store)[0]["payload"])["path"] == path


async def test_card_switch_off_before_flush_drops_card(tmp_path):
    """扫描后在发送前关掉开关 → 卡片作废、不入发件箱。"""
    world = _make(tmp_path)
    renderer = Renderer()
    cp = card_push.CardPush(world.store, world.host, world.pushes, world.mentions,
                            lambda: world.settings, renderer=renderer, outbox=world.ob)
    _enable_card(world)
    _batch(world)
    cp.scan(GID, NOON + 10)
    group_push.set_config(world.store, GID, {"news_card_enabled": False}, world.settings, now=NOON + 20)
    await cp.flush(GID, NOON + 30)
    card = world.store.read().execute("SELECT status, error FROM news_cards").fetchone()
    assert card["status"] == "dropped" and card["error"]
    assert _outbox_rows(world.store) == [] and world.host.images == [] and renderer.calls == 0


async def test_card_without_outbox_refuses_to_send(tmp_path):
    world = _make(tmp_path)
    cp = card_push.CardPush(world.store, world.host, world.pushes, world.mentions,
                            lambda: world.settings, renderer=Renderer(), outbox=None)
    _enable_card(world)
    _batch(world)
    cp.scan(GID, NOON + 10)
    await cp.flush(GID, NOON + 10)
    card = world.store.read().execute("SELECT status FROM news_cards").fetchone()
    assert card["status"] == "pending"          # 没接线就不发，也不假装发过
    assert _outbox_rows(world.store) == [] and world.host.images == []


async def test_idea_mention_enqueues_with_at_and_settles(tmp_path):
    """个人向构想：入队文本带 at_user / at_name，发成功后回写 idea_mentions + 备忘。"""
    world = _make(tmp_path, cfg={"console": {"public_url": "https://mw.example"}})
    with world.store.tx() as conn:
        conn.execute(
            "INSERT INTO ideas (group_id, title, body, basis, state, created, updated, target_user_id, origin)"
            " VALUES (?, '我可以帮群里做个番剧追更表', '每周自动汇总更新', '画像里说他最近在考研',"
            " 'new', ?, ?, ?, '')",
            (GID, NOON - 60, NOON - 60, UID),
        )
        conn.execute(
            "INSERT INTO members (group_id, user_id, name, ts) VALUES (?, ?, '阿柒', ?)",
            (GID, UID, NOON),
        )
        # 个人提一嘴只提「当前关注成员」：先把他放进关注名单
        conn.execute(
            "INSERT INTO focus_members (group_id, user_id, name, reasons, note, pinned, removed,"
            " updated) VALUES (?, ?, '阿柒', '[]', '', 1, 0, ?)",
            (GID, UID, NOON),
        )
        # 发送前复核要求「他本人明确要过这件事」的原文依据
        conn.execute(
            "INSERT INTO chat_log (text, group_id, message_id, ts, user_id, user_name)"
            " VALUES (?, ?, 'need-1', ?, ?, '阿柒')",
            ("那个追更表能帮我做吗", GID, NOON - 30, UID),
        )
    im = card_push.IdeaMention(
        world.store, world.host,
        Models(guard={"resolved": False, "declined": False, "need": True,
                      "evidence": ["need-1"]}),
        world.pushes, world.mentions, lambda: world.settings, outbox=world.ob,
    )
    group_push.set_config(world.store, GID, {"idea_mention_enabled": True}, world.settings, now=NOON - 120)
    assert im.scan(GID, NOON) == 1
    await im.flush(GID, NOON)
    mention = world.store.read().execute("SELECT * FROM idea_mentions").fetchone()
    assert mention["status"] == "queued" and mention["sent_ts"] is None
    payload = json.loads(_outbox_rows(world.store)[0]["payload"])
    assert payload["at_user"] == UID and payload["at_name"] == "阿柒"
    assert UID not in payload["text"]                       # QQ 号绝不进正文
    assert "https://mw.example/#/tok900000001/ideas/I-" in payload["text"]
    await world.ob.flush(NOON)
    assert world.host.texts[0]["at_user"] == UID and world.host.texts[0]["at_name"] == "阿柒"
    mention = world.store.read().execute("SELECT * FROM idea_mentions").fetchone()
    assert mention["status"] == "sent" and mention["message_id"]
    assert [r["kind"] for r in _push_rows(world.store)] == ["idea_mention"]


async def test_idea_mention_uncertain_keeps_quota_and_not_sent(tmp_path):
    world = _make(tmp_path)
    with world.store.tx() as conn:
        conn.execute(
            "INSERT INTO ideas (group_id, title, body, basis, state, created, updated, target_user_id, origin)"
            " VALUES (?, '我可以帮群里做个番剧追更表', '每周自动汇总更新', '', 'new', ?, ?, '', '')",
            (GID, NOON - 60, NOON - 60),
        )
    im = card_push.IdeaMention(world.store, world.host, Models(), world.pushes, world.mentions,
                               lambda: world.settings, outbox=world.ob)
    group_push.set_config(world.store, GID, {"idea_mention_enabled": True, "daily_max": 1},
                          world.settings, now=NOON - 120)
    im.scan(GID, NOON)
    world.host.text_errors = [asyncio.TimeoutError()]
    await im.flush(GID, NOON)
    await world.ob.flush(NOON)
    row = world.store.read().execute("SELECT status, error FROM idea_mentions").fetchone()
    assert row["status"] == "uncertain" and "不重发" in row["error"]
    assert world.pushes.count_used(GID, NOON) == 1


async def test_producers_recover_follow_outbox_state(tmp_path):
    """重启：本地行跟着发件箱走——没发出去的回到 pending，发出去的补记 sent。"""
    world = _make(tmp_path)
    with world.store.tx() as conn:
        conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
            " VALUES (?, ?, 1, 1, 0, '', ?)", (GID, NOON, NOON),
        )
        cur = conn.execute(
            "INSERT INTO news_cards (group_id, batch_id, status, item_ids, created, due_ts)"
            " VALUES (?, 1, 'queued', '[]', ?, ?)", (GID, NOON, NOON),
        )
        card_id = int(cur.lastrowid)
    cp = card_push.CardPush(world.store, world.host, world.pushes, world.mentions,
                            lambda: world.settings, renderer=Renderer(), outbox=world.ob)
    # 发件箱里没有对应行（入队前崩了）→ 回到待发，可以重建
    assert cp.recover() == 1
    assert world.store.read().execute(
        "SELECT status FROM news_cards WHERE id=?", (card_id,)
    ).fetchone()["status"] == "pending"
    # 真的发出去过（发件箱 sent）→ 补记 sent，不重发
    world.ob.enqueue(f"news_card:{card_id}", GID, "image",
                     {"path": str(world.root / "x.png"), "text": "", "push_kind": "news_card"})
    with world.store.tx() as conn:
        conn.execute("UPDATE news_cards SET status='queued' WHERE id=?", (card_id,))
        conn.execute("UPDATE outbox SET status='sent', result=? WHERE key=?",
                     ('{"message_id": "m9"}', f"news_card:{card_id}"))
    cp.recover()
    row = world.store.read().execute("SELECT status, message_id FROM news_cards WHERE id=?", (card_id,)).fetchone()
    assert row["status"] == "sent" and row["message_id"] == "m9"


# ---------------------------------------------------------------------------
# 8. 视图：今日发出多少 / 最近几条（父会话接 API 用）
# ---------------------------------------------------------------------------


async def test_view_shape_and_recent(tmp_path):
    world = _make(tmp_path)
    group_push.set_config(world.store, GID, {"news_card_enabled": True}, world.settings)
    world.ob.enqueue("k:topic", GID, "text", {"text": "开场白一句", "push_kind": "topic"})
    await world.ob.flush(NOON)
    view = group_push.view(world.store, GID, world.settings, now=NOON + 10)
    assert set(view) >= {"config", "daily_max", "sent_today", "quota_used", "recent"}
    assert view["config"]["news_card_enabled"] is True
    assert view["sent_today"] == 1 and view["quota_used"] == 1
    top = view["recent"][0]
    assert top["push_kind"] == "topic" and top["state"] == "已发"
    assert "开场白一句" in top["text"] and top["uncertain"] is False
    # 非服务群：零读取也能给出结构
    other = group_push.view(world.store, OTHER, world.settings, now=NOON)
    assert other["sent_today"] == 0 and other["recent"] == []
    assert world.store.kv_get(f"{group_push.KV_PREFIX}{OTHER}", None) is None


# ---------------------------------------------------------------------------
# 9. 兼容壳：老调用（网页 PUT / GET）继续可用，但数据只有一份
# ---------------------------------------------------------------------------


async def test_legacy_module_helpers_read_and_write_same_source(tmp_path):
    world = _make(tmp_path)
    cfg = card_push.set_config(world.store, GID, {"news_card_enabled": True, "news_card_count": 2}, now=NOON)
    assert cfg["news_card_enabled"] is True and cfg["news_card_count"] == 2
    assert card_push.get_config(world.store, GID)["news_card_enabled"] is True
    assert group_push.get_config(world.store, GID, world.settings)["news_card_count"] == 2
    # 退役字段：所有调用口都拒（含 settings=None 的老壳），不静默假保存
    with pytest.raises(ValueError):
        card_push.set_config(world.store, GID, {"news_card_daily_max": 9}, now=NOON)
    assert group_push.get_config(world.store, GID, world.settings)["daily_max"] == 3
    with pytest.raises(ValueError):
        card_push.set_config(world.store, GID, {"news_card_count": 9}, now=NOON)
