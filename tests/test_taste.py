"""taste.py：本群的「口味小结」（docs/10 第七节第 7 步，2026-09-30）。

- 每天最多蒸馏一次：显式信号（有用 / 没用、资讯评价、管理员一句话偏好）+ 自动信号（回复卡片、点开原文、
  群里接着聊；表不存在就跳过）+ 近期在聊的话题（画像里「最近在聊」的摘要，不喂原始聊天记录）
  → 一段 ≤300 字的中文小结，只写群的口味，不点名任何人；
- 没有任何信号 → 不调模型、不覆盖旧的；
- 管理员手改过 → 自动蒸馏 7 天内不覆盖；管理员偏好（feeds.pref）优先级最高，原文附在小结前面；
- 过隐私闸：蒸馏结果含关注成员信息（scrub 返回 None）→ 丢弃这次结果。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from fakes import FakeModelsQueue

from CharTyr_MaiWork.maiwork import taste
from CharTyr_MaiWork.maiwork.store import Store

NOW = 1_790_000_000.0
GID = "111"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _run(coro):
    return asyncio.run(coro)


def _item(store, title, *, up=0, down=0, days_ago=1.0):
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_items (batch_id, group_id, title, url_key, rejected, up, down, created)"
            " VALUES (1, ?, ?, ?, 0, ?, ?, ?)",
            (GID, title, f"x.example/{title}", up, down, NOW - days_ago * 86400),
        )
        return int(cur.lastrowid)


def _reply(summary="群友爱看独立游戏的开发内幕和实测，不爱看营销稿。"):
    return json.dumps({"summary": summary, "likes": ["开发内幕"], "dislikes": ["营销稿"]}, ensure_ascii=False)


def test_no_signals_no_model_call(store):
    models = FakeModelsQueue(replies=[_reply()])
    out = _run(taste.refresh(store, models, GID, NOW, recent_topics=[]))
    assert out is None and models.calls == []
    assert taste.text(store, GID) == ""


def test_refresh_uses_signals_and_saves(store):
    _item(store, "《鬼武者》开发访谈", up=2)
    _item(store, "某手游充值活动", down=3)
    models = FakeModelsQueue(replies=[_reply()])
    out = _run(taste.refresh(store, models, GID, NOW, recent_topics=["最近在聊 Switch 2 底座发热"]))
    assert out and "独立游戏" in out
    prompt = models.calls[0][1][0]["content"]
    assert "鬼武者" in prompt and "充值活动" in prompt and "底座发热" in prompt
    assert "不点名" in prompt or "不要点名" in prompt
    assert taste.text(store, GID).startswith("群友爱看")
    # 当天再刷不调模型
    _run(taste.refresh(store, models, GID, NOW + 3600, recent_topics=["x"]))
    assert len(models.calls) == 1


def test_ratings_and_pref_included(store):
    iid = _item(store, "某条资讯")
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO news_ratings (group_id, item_id, client, reasons, note, created, updated) VALUES (?,?,?,?,?,?,?)",
            (GID, iid, "c1", json.dumps(["old"]), "旧闻了", NOW - 3600, NOW - 3600),
        )
        store.kv_set(conn, f"feeds.pref.{GID}", "多来点硬件评测")
    models = FakeModelsQueue(replies=[_reply()])
    _run(taste.refresh(store, models, GID, NOW, recent_topics=[]))
    prompt = models.calls[0][1][0]["content"]
    assert "太旧了" in prompt and "旧闻了" in prompt and "多来点硬件评测" in prompt


def test_prompt_text_puts_admin_pref_first(store):
    with store.tx() as conn:
        store.kv_set(conn, f"feeds.pref.{GID}", "多来点硬件评测")
    taste.set_manual(store, GID, "爱看开发内幕", NOW)
    block = taste.prompt_block(store, GID)
    assert block.index("多来点硬件评测") < block.index("爱看开发内幕")


def test_manual_edit_blocks_auto_refresh_for_7_days(store):
    _item(store, "A", up=1)
    taste.set_manual(store, GID, "管理员写的口味", NOW)
    models = FakeModelsQueue(replies=[_reply()])
    _run(taste.refresh(store, models, GID, NOW + 86400 * 2, recent_topics=[]))
    assert models.calls == [] and taste.text(store, GID) == "管理员写的口味"
    _run(taste.refresh(store, models, GID, NOW + 86400 * 8, recent_topics=[]))
    assert len(models.calls) == 1


def test_privacy_gate_discards(store):
    _item(store, "A", up=1)
    models = FakeModelsQueue(replies=[_reply("小明最爱看这个")])
    out = _run(taste.refresh(store, models, GID, NOW, recent_topics=[], scrub=lambda _g, t: None))
    assert out is None and taste.text(store, GID) == ""


def test_summary_capped_300(store):
    _item(store, "A", up=1)
    models = FakeModelsQueue(replies=[_reply("长" * 500)])
    out = _run(taste.refresh(store, models, GID, NOW, recent_topics=[]))
    assert len(out) <= 300


def test_bad_model_reply_keeps_old(store):
    _item(store, "A", up=1)
    taste.set_manual(store, GID, "旧的", NOW - 86400 * 30)
    models = FakeModelsQueue(replies=["不是 JSON"])
    out = _run(taste.refresh(store, models, GID, NOW, recent_topics=[]))
    assert out is None and taste.text(store, GID) == "旧的"


def test_view(store):
    taste.set_manual(store, GID, "口味", NOW)
    v = taste.view(store, GID)
    assert v["text"] == "口味" and v["manual"] is True and v["ts"] == NOW
