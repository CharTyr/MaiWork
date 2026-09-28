"""delivery.py 单元测试：Mentions 备忘注入、Pushes 上限与睡觉时段。

Mentions 的注入走 docs/06 的 maisaka.planner.before_request 载荷
（items / item_schema_version 等），不经过模型。
"""

from __future__ import annotations

import copy

import pytest

from datetime import datetime, timedelta, timezone

from CharTyr_MaiWork import clock
from CharTyr_MaiWork.config import load_settings
from CharTyr_MaiWork.delivery import Mentions, Pushes
from CharTyr_MaiWork.store import Store

BJ = timezone(timedelta(hours=8))


def _noon_epoch() -> float:
    """北京时间今天 12:00（非睡觉时段）。"""
    from datetime import datetime as _dt
    b = _dt.now(tz=BJ)
    noon = b.replace(hour=12, minute=0, second=0, microsecond=0)
    return noon.timestamp()

GID = "111"


def _settings(cfg: dict | None = None) -> object:
    # G4 起 Mentions.render 复核 settings.is_served：群里必须有 GID 才渲染
    merged = {"groups": {"serve": [{"group": f"qq:{GID}"}]}}
    if cfg:
        merged.update(cfg)
    settings, _ = load_settings(merged)
    return settings


def _make_mentions(tmp_path, *, cfg: dict | None = None):
    store = Store(tmp_path / "t.db")
    store.migrate()
    settings = _settings(cfg)
    return store, settings, Mentions(store, lambda: settings)


def _make_pushes(tmp_path, *, cfg: dict | None = None):
    store = Store(tmp_path / "t.db")
    store.migrate()
    settings = _settings(cfg)
    return store, settings, Pushes(store, lambda: settings)


def _seed_group(store: Store) -> None:
    """造一个服务群的运行时行（Mentions.render 要靠 groups.session_id 找群）。"""
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, session_id) VALUES (?, ?)",
            (GID, "sess-1"),
        )


# ----------------------------------------------------------------------
# Mentions.add
# ----------------------------------------------------------------------


def test_mentions_add_and_overwrite(tmp_path):
    store, settings, m = _make_mentions(tmp_path)
    _seed_group(store)
    t = clock.now()
    m.add(GID, "素材一", key="k1", ttl_s=3600, turns=2)
    m.add(GID, "素材一覆盖", key="k1", ttl_s=3600, turns=3)
    m.add(GID, "素材二", key="k2", ttl_s=3600, turns=1)
    # render 返回文本
    out = m.render("sess-1")
    assert out is not None
    assert "素材一覆盖" in out
    assert "素材二" in out
    assert out.count("素材一") == 1  # 同 key 覆盖了，不出现两份
    # turns_left 递减
    rows = store.read().execute("SELECT key, turns_left FROM mentions ORDER BY key").fetchall()
    assert [(r["key"], r["turns_left"]) for r in rows] == [("k1", 2), ("k2", 0)]


def test_mentions_render_non_served_group(tmp_path):
    """非服务群（groups 表里没有这个 session_id 的群）→ None。"""
    store, settings, m = _make_mentions(tmp_path)
    m.add(GID, "素材", key="k1", ttl_s=3600, turns=1)
    # 没 seed groups → 找不到
    assert m.render("sess-999") is None


def test_mentions_render_no_expired_no_turns(tmp_path):
    """过期或 turns_left=0 的不再出现。"""
    store, settings, m = _make_mentions(tmp_path)
    _seed_group(store)
    t0 = clock.now()
    m.add(GID, "新鲜的", key="fresh", ttl_s=60, turns=1)
    # 手动塞一条过期的
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO mentions (group_id, key, text, expires_ts, turns_left, created)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (GID, "stale", "过期的", t0 - 10, 5, t0 - 100),
        )
        conn.execute(
            "INSERT INTO mentions (group_id, key, text, expires_ts, turns_left, created)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (GID, "empty", "没轮次了", t0 + 100, 0, t0),
        )
    out = m.render("sess-1")
    assert "新鲜的" in out
    assert "过期的" not in out
    assert "没轮次了" not in out


def test_mentions_render_ordered_new_first(tmp_path):
    """按创建时间新到旧拼。"""
    store, settings, m = _make_mentions(tmp_path)
    _seed_group(store)
    t0 = clock.now()
    for i, label in enumerate(["最旧", "中间", "最新"]):
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO mentions (group_id, key, text, expires_ts, turns_left, created)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (GID, f"k{i}", label, t0 + 3600, 5, t0 + i),
            )
    out = m.render("sess-1")
    assert out is not None
    lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert len(lines) == 3
    # 最新（created 最大）排在最前
    assert "最新" in lines[0]
    assert "中间" in lines[1]
    assert "最旧" in lines[2]


def test_mentions_render_max_300_chars(tmp_path):
    """总长 ≤300 字。"""
    store, settings, m = _make_mentions(tmp_path)
    _seed_group(store)
    t0 = clock.now()
    for i in range(20):
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO mentions (group_id, key, text, expires_ts, turns_left, created)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (GID, f"k{i}", "x" * 50, t0 + 3600, 5, t0 + i),
            )
    out = m.render("sess-1")
    assert out is not None
    assert len(out) <= 300


def test_mentions_render_header(tmp_path):
    """固定标题。"""
    store, settings, m = _make_mentions(tmp_path)
    _seed_group(store)
    m.add(GID, "素材", key="k1", ttl_s=3600, turns=1)
    out = m.render("sess-1")
    assert out is not None
    assert out.startswith("【MaiWork 备忘】")


# ----------------------------------------------------------------------
# Mentions.inject
# ----------------------------------------------------------------------

_BASE_KWARGS = {
    "item_schema_version": 1,
    "tool_definitions": [],
    "selected_history_count": 5,
    "built_message_count": 7,
    "selection_reason": "x",
    "session_id": "sess-1",
    "items": [
        {"item_type": "SystemMessageItem", "meta": {"k": "v"}, "parts": [{"type": "text", "text": "系统指令"}]},
        {"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": "群友"}]},
    ],
}


def test_inject_appends_to_first_system_item_last_text_part(tmp_path):
    """追加到第一个 SystemMessageItem 的最后一个 text part。"""
    store, settings, m = _make_mentions(tmp_path)
    _seed_group(store)
    m.add(GID, "可以提一句：最近那个开源项目", key="k1", ttl_s=3600, turns=1)
    out = m.inject(copy.deepcopy(_BASE_KWARGS))
    assert out is not None
    # 其他键保留
    for k in ("item_schema_version", "tool_definitions", "selected_history_count", "built_message_count", "selection_reason", "session_id"):
        assert out[k] == _BASE_KWARGS[k]
    # 追加在第一个 SystemMessageItem 的最后一个 text part 末尾
    first_sys = out["items"][0]
    assert first_sys["parts"][0]["type"] == "text"
    assert "系统指令" in first_sys["parts"][0]["text"]
    assert "【MaiWork 备忘】" in first_sys["parts"][0]["text"]
    assert "最近那个开源项目" in first_sys["parts"][0]["text"]


def test_inject_original_kwargs_unchanged(tmp_path):
    """inject 返回新 kwargs，不改原 dict。"""
    store, settings, m = _make_mentions(tmp_path)
    _seed_group(store)
    m.add(GID, "素材", key="k1", ttl_s=3600, turns=1)
    original = copy.deepcopy(_BASE_KWARGS)
    out = m.inject(_BASE_KWARGS)
    assert out is not None
    # 原 kwargs 没被改
    assert _BASE_KWARGS["items"][0] == _BASE_KWARGS["items"][0]
    assert _BASE_KWARGS["items"][0]["parts"][0]["text"] == "系统指令"


def test_inject_already_contains_header_no_duplicate(tmp_path):
    """已经含固定标题就不再追加。"""
    store, settings, m = _make_mentions(tmp_path)
    _seed_group(store)
    m.add(GID, "素材", key="k1", ttl_s=3600, turns=1)
    kwargs = copy.deepcopy(_BASE_KWARGS)
    kwargs["items"][0]["parts"][0]["text"] += "\n\n【MaiWork 备忘】已有"
    out = m.inject(kwargs)
    assert out is None


def test_inject_no_system_item_returns_none(tmp_path):
    """items 里没有 SystemMessageItem → 返回 None（不破坏请求）。"""
    store, settings, m = _make_mentions(tmp_path)
    _seed_group(store)
    m.add(GID, "素材", key="k1", ttl_s=3600, turns=1)
    kwargs = copy.deepcopy(_BASE_KWARGS)
    kwargs["items"] = [{"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": "群友"}]}]
    out = m.inject(kwargs)
    assert out is None


def test_inject_no_mentions_returns_none(tmp_path):
    """没有可用的备忘 → 返回 None。"""
    store, settings, m = _make_mentions(tmp_path)
    _seed_group(store)
    out = m.inject(copy.deepcopy(_BASE_KWARGS))
    assert out is None


def test_inject_non_dict_kwargs_returns_none(tmp_path):
    """任何异常/格式不符 → None。"""
    store, settings, m = _make_mentions(tmp_path)
    _seed_group(store)
    assert m.inject(None) is None
    assert m.inject({}) is None
    assert m.inject("str") is None


# ----------------------------------------------------------------------
# Pushes
# ----------------------------------------------------------------------


def test_pushes_up_to_limit(tmp_path):
    """除 error/command 外 push_per_day 上限。"""
    store, settings, p = _make_pushes(tmp_path, cfg={"delivery": {"push_per_day": 2}})
    t0 = _noon_epoch()
    p.record(GID, "topic", "开了个话题", t0)
    p.record(GID, "news", "推了一条资讯", t0)
    ok, why = p.can_push(GID, "idea", t0)
    assert ok is False
    assert "上限" in why or "今天推够" in why


def test_pushes_error_and_command_unrestricted(tmp_path):
    """kind="error" / "command" 永远 True。"""
    store, settings, p = _make_pushes(tmp_path, cfg={"delivery": {"push_per_day": 0, "quiet_hours": "00:00-23:59"}})
    t0 = clock.now()
    p.record(GID, "error", "故障报错", t0)
    p.record(GID, "command", "指令回复", t0)
    assert p.can_push(GID, "error", t0)[0] is True
    assert p.can_push(GID, "command", t0)[0] is True


def test_pushes_admin_unrestricted(tmp_path):
    """kind="admin"（管理员在网页对话里当场让发的）不受每日上限和睡觉时段限制。"""
    store, settings, p = _make_pushes(tmp_path, cfg={"delivery": {"push_per_day": 1, "quiet_hours": "00:00-23:59"}})
    t0 = clock.now()
    p.record(GID, "topic", "开了个话题", t0)  # 把今天的受限推送额度用掉
    ok, why = p.can_push(GID, "admin", t0)
    assert ok is True
    # 受限类型这时候确实被拦（对照组，证明上限在起作用）
    ok2, why2 = p.can_push(GID, "topic", t0)
    assert ok2 is False


def test_pushes_sleep_window(tmp_path, monkeypatch):
    """睡觉时段内（23:00-08:00 北京时间）其他推送也不行。"""
    monkeypatch.delenv("TZ", raising=False)
    # 北京时间 2026-09-27 00:00（睡觉时段内）
    sleep_ts = clock.bj(1_790_395_200).timestamp()  # 要显式构造；见下更稳的方式
    # 更稳：直接戳 clock.bj
    # 2026-09-27 00:00 UTC+8 = 2026-09-26 16:00 UTC = epoch 1789920000 ≈ 算一下
    # 用已知点：时钟北京时间 23:30 → 在 quiet_hours="23:00-08:00" 里
    from datetime import datetime, timezone, timedelta
    BJ = timezone(timedelta(hours=8))
    sleep_dt = datetime(2026, 9, 27, 23, 30, tzinfo=BJ)
    sleep_epoch = sleep_dt.timestamp()
    store, settings, p = _make_pushes(tmp_path, cfg={"delivery": {"push_per_day": 10, "quiet_hours": "23:00-08:00"}})
    ok, why = p.can_push(GID, "topic", sleep_epoch)
    assert ok is False
    assert "睡觉" in why


def test_pushes_sleep_window_cross_midnight_morning(tmp_path, monkeypatch):
    """跨午夜睡觉时段：早上 07:59 还在睡觉时段。"""
    from datetime import datetime, timezone, timedelta
    BJ = timezone(timedelta(hours=8))
    morning_dt = datetime(2026, 9, 27, 7, 59, tzinfo=BJ)
    morning_epoch = morning_dt.timestamp()
    store, settings, p = _make_pushes(tmp_path, cfg={"delivery": {"push_per_day": 10, "quiet_hours": "23:00-08:00"}})
    ok, why = p.can_push(GID, "topic", morning_epoch)
    assert ok is False
    assert "睡觉" in why


def test_pushes_outside_sleep_window_ok(tmp_path):
    """非睡觉时段、推够之前 → True。"""
    from datetime import datetime, timezone, timedelta
    BJ = timezone(timedelta(hours=8))
    day_dt = datetime(2026, 9, 27, 12, 0, tzinfo=BJ)
    day_epoch = day_dt.timestamp()
    store, settings, p = _make_pushes(tmp_path, cfg={"delivery": {"push_per_day": 10, "quiet_hours": "23:00-08:00"}})
    ok, why = p.can_push(GID, "topic", day_epoch)
    assert ok is True
    assert why == ""


def test_pushes_per_day_excludes_error_and_command(tmp_path):
    """error / command 不计入 push_per_day。"""
    store, settings, p = _make_pushes(tmp_path, cfg={"delivery": {"push_per_day": 1}})
    t0 = _noon_epoch()
    p.record(GID, "error", "故障报错1", t0)
    p.record(GID, "command", "指令回复1", t0)
    p.record(GID, "error", "故障报错2", t0)
    ok, why = p.can_push(GID, "topic", t0)
    assert ok is True  # error / command 不算


def test_pushes_count_today(tmp_path):
    store, settings, p = _make_pushes(tmp_path)
    t0 = clock.now()
    assert p.count_today(GID) == 0
    p.record(GID, "error", "x", t0)
    p.record(GID, "topic", "y", t0)
    p.record(GID, "topic", "z", t0)
    assert p.count_today(GID) == 3
    assert p.count_today(GID, kind="topic") == 2
