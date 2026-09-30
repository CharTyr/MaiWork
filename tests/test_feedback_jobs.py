"""feedback_jobs.py：每小时一轮的「反馈 + 口味」后台活（docs/10 第七节第 4、7 步）。

- 群里接着聊：候选交主模型一次判断（JSON：{"yes": [条目编号]}），判错 / 没配模型就不记；
- 口味小结：每天蒸馏一次，近期在聊的话题取画像里「最近在聊」那几条（摘要，不读原始聊天）；
- 一小时内同群不重复跑。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes import FakeModelsQueue

from CharTyr_MaiWork.maiwork import chatlog, feedback_jobs, taste
from CharTyr_MaiWork.maiwork.store import Store

NOW = 1_790_000_000.0
GID = "111"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _run(c):
    return asyncio.run(c)


def _item(store, *, up=0):
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_items (batch_id, group_id, title, url_key, keywords, rejected, up, created)"
            " VALUES (1, ?, '《地下城2》开放世界', 'a.example/1', ?, 0, ?, ?)",
            (GID, json.dumps(["开放世界", "地下城"], ensure_ascii=False), up, NOW - 3600),
        )
        return int(cur.lastrowid)


class _Profiles:
    def entries(self, gid):
        return [{"category": "recent", "text": "最近在聊 NS2 底座发热"}, {"category": "interest", "text": "独立游戏"}]


def test_round_judges_mentions_and_refreshes_taste(store):
    iid = _item(store, up=1)
    chatlog.record_messages(store, GID, [SimpleNamespace(is_bot=False, text="地下城2 开放世界来了", id="c1", ts=NOW - 1800, user_id="2", user_name="n")], now=NOW)
    models = FakeModelsQueue(replies=[
        json.dumps({"yes": [iid]}),
        json.dumps({"summary": "爱看开发内幕", "likes": [], "dislikes": []}, ensure_ascii=False),
    ])
    out = _run(feedback_jobs.run(store, models, GID, NOW, profiles=_Profiles(), scrub=lambda g, t: t))
    assert out == {"mentions": 1, "taste": True}
    judge_prompt = models.calls[0][1][0]["content"]
    assert "地下城2 开放世界来了" in judge_prompt
    taste_prompt = models.calls[1][1][0]["content"]
    assert "NS2 底座发热" in taste_prompt and "独立游戏" not in taste_prompt
    assert taste.text(store, GID) == "爱看开发内幕"


def test_round_skips_within_hour(store):
    models = FakeModelsQueue(replies=[])
    feedback_jobs._last.clear()
    _run(feedback_jobs.run(store, models, GID, NOW, profiles=_Profiles(), scrub=None))
    assert feedback_jobs.due(GID, NOW + 600) is False
    assert feedback_jobs.due(GID, NOW + 3700) is True


def test_bad_judge_reply_records_nothing(store):
    _item(store)
    chatlog.record_messages(store, GID, [SimpleNamespace(is_bot=False, text="地下城2 开放世界来了", id="c1", ts=NOW - 1800, user_id="2", user_name="n")], now=NOW)
    models = FakeModelsQueue(replies=["不是 JSON", "不是 JSON"])
    out = _run(feedback_jobs.run(store, models, GID, NOW, profiles=_Profiles(), scrub=None))
    assert out["mentions"] == 0
