"""资讯相关度放宽 + guide 收紧 + 探索感（2026-11，docs/02-设计.md §4.1）。

线上 7 天实测：三个群合计约 58 条被「相关度 <3」刷掉，留下约 55 条——很多明显能聊的被误杀。
原因有四：画像只给前 20 条（第 22/24 条的对不上编号被封顶 2）；提示词只说「必须对着某条画像」
没有「拓展」档；封顶规则写死哪怕 why 说清了关联；漏打分被当成「相关度 0」。同步还有：
guide（好文）入选太松（300+/1400+ 天的工具页、新闻评测、GitHub 仓库页都混进来了），
explore 方向找回 7 条全被相关度刷掉、diverse 实际只是同话题另一条新闻、政府通讯稿混入。
对应修法（A–H，见 feeds.py 顶部 docstring 第 6 条）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from CharTyr_MaiWork.maiwork.feeds import Feeds
from CharTyr_MaiWork.maiwork.models import ModelError

from fakes import FakeModelsQueue, focus_reply
from test_feeds_freshness import (
    GID,
    NOW,
    _TimePatch,
    _cand,
    _make_feeds,
    _post,
    _posts_json,
    _rejected_rows,
    _run,
    _score,
    _scores_json,
    FakeWorkers,
    _FOCUS_JSON,
    _ok_report,
)


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------


def _entry(i: int, *, category: str = "interest", text: str | None = None,
           evidence_count: int = 1, confidence: float = 0.5, locked: bool = False,
           last_ts: float = NOW - 86400) -> dict:
    return {
        "id": i + 1,
        "group_id": GID,
        "category": category,
        "text": text or f"画像条目{i:02d}",
        "evidence_count": evidence_count,
        "confidence": confidence,
        "first_ts": NOW - 30 * 86400,
        "last_ts": last_ts,
        "locked": locked,
        "deleted": False,
        "source": "auto",
    }


def _score_prompt_line(models: FakeModelsQueue, call_idx: int = 0) -> str:
    return str(models.calls[call_idx][1][0]["content"])


# ----------------------------------------------------------------------
# A 画像给全：打分时给模型全部画像（前 60 条），按重要性排序。
# ----------------------------------------------------------------------


class TestProfileEntriesExpanded:
    def test_score_prompt_includes_entries_beyond_20(self, tmp_path) -> None:
        """群有 55 条画像时，第 21/24 条也能进打分提示词（之前只给前 20 条）。"""
        entries = [_entry(i) for i in range(55)]
        # 把「群里在做一个 agent harness」放到第 22 条（索引 21）
        entries[21]["text"] = "群里在做一个 agent harness"
        entries[23]["text"] = "《巫师3》重制明天上：MC 95"
        models = FakeModelsQueue(ready=True, replies=[_scores_json(_score(0))])
        _store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        profiles = feeds._profiles
        profiles.entries_map[GID] = entries
        cands = [_cand(0)]
        with _TimePatch():
            _run(feeds._score(GID, settings, cands))
        prompt = _score_prompt_line(models)
        assert "[21] 群里在做一个 agent harness" in prompt
        assert "[23] 《巫师3》重制明天上：MC 95" in prompt

    def test_entry_over_60_capped_sorted_by_importance(self, tmp_path) -> None:
        """画像超过 60 条时给前 60 条，按重要性排序（locked 优先、新画像在前）。"""
        entries = []
        for i in range(80):
            entries.append(_entry(i, evidence_count=1, last_ts=NOW - (i + 1) * 3600))
        # 一个 locked 且新的放最前
        entries[70]["locked"] = True
        entries[70]["last_ts"] = NOW - 60
        entries[70]["text"] = "锁定的重要画像"
        models = FakeModelsQueue(ready=True, replies=[_scores_json(_score(0))])
        _store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        feeds._profiles.entries_map[GID] = entries
        cands = [_cand(0)]
        with _TimePatch():
            _run(feeds._score(GID, settings, cands))
        prompt = _score_prompt_line(models)
        # 编号 0 应该是 locked 且新的那条
        assert "[0] 锁定的重要画像" in prompt
        # 最多给 60 条
        assert "[59]" in prompt
        assert "[60]" not in prompt


# ----------------------------------------------------------------------
# C 封顶放宽：profile 编号对得上，或者给了非空 bridge（≥6 个字），就不封顶；
#   两者都没有才封顶 2。
# ----------------------------------------------------------------------


class TestRelevanceCap:
    def test_profile_match_not_capped(self, tmp_path) -> None:
        """profile 编号对得上 → relevance 不被封顶（照旧规则）。"""
        models = FakeModelsQueue(ready=True, replies=[
            _scores_json(_score(0, relevance=5, profile=0)),
        ])
        _store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        cands = [_cand(0)]
        with _TimePatch():
            _run(feeds._score(GID, settings, cands))
        assert cands[0]["scores"]["relevance"] == 5.0

    def test_bridge_not_capped(self, tmp_path) -> None:
        """profile 给不出编号、但 bridge ≥6 个字 → relevance 不封顶。"""
        models = FakeModelsQueue(ready=True, replies=[
            _scores_json(_score(0, relevance=4, profile=None,
                              bridge="和群在用的 agent 框架是同一医院的竞品")),
        ])
        _store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        cands = [_cand(0)]
        with _TimePatch():
            _run(feeds._score(GID, settings, cands))
        assert cands[0]["scores"]["relevance"] == 4.0
        assert cands[0]["bridge"] != ""

    def test_no_profile_no_bridge_capped_at_2(self, tmp_path) -> None:
        """profile 指不出编号、bridge 也空 → relevance 封顶 2（现有规则保留）。"""
        models = FakeModelsQueue(ready=True, replies=[
            _scores_json(_score(0, relevance=5, profile=None)),
        ])
        _store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        cands = [_cand(0)]
        with _TimePatch():
            _run(feeds._score(GID, settings, cands))
        assert cands[0]["scores"]["relevance"] == 2.0

    def test_bridge_too_short_capped(self, tmp_path) -> None:
        """bridge 太短（<6 个字）不算数 → 还是封顶 2。"""
        models = FakeModelsQueue(ready=True, replies=[
            _scores_json(_score(0, relevance=5, profile=None, bridge="同游戏")),
        ])
        _store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        cands = [_cand(0)]
        with _TimePatch():
            _run(feeds._score(GID, settings, cands))
        assert cands[0]["scores"]["relevance"] == 2.0

    def test_profile_idx_25_beyond_20_entries(self, tmp_path) -> None:
        """模型指到第 25 条画像（索引 24，前 20 条之外）→ 编号有效，不封顶。"""
        entries = [_entry(i) for i in range(55)]
        entries[24]["text"] = "《巫师3》重制明天上：MC 95"
        models = FakeModelsQueue(ready=True, replies=[
            _scores_json(_score(0, relevance=4, profile=24)),
        ])
        _store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        feeds._profiles.entries_map[GID] = entries
        cands = [_cand(0)]
        with _TimePatch():
            _run(feeds._score(GID, settings, cands))
        assert cands[0]["scores"]["relevance"] == 4.0
        assert cands[0]["profile_ref"] == "《巫师3》重制明天上：MC 95"


# ----------------------------------------------------------------------
# B 分档写进提示词：relevance 1–5 的定义 + bridge 字段说明。
# ----------------------------------------------------------------------


class TestRelevanceGuidelinesInPrompt:
    def test_score_prompt_explains_relevance_scale(self, tmp_path) -> None:
        """打分提示词里写明 relevance 1-5 的含义（5=直接命中，3=能拓展，1=无关）。"""
        models = FakeModelsQueue(ready=True, replies=[_scores_json(_score(0))])
        _store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        cands = [_cand(0)]
        with _TimePatch():
            _run(feeds._score(GID, settings, cands))
        prompt = _score_prompt_line(models)
        # 分档文字来自资讯标准 skill（scoring.md「相关度 relevance」）
        assert "5：直接命中" in prompt
        assert "3：能拓展" in prompt
        assert "bridge" in prompt

    def test_score_prompt_mentions_bridge_field(self, tmp_path) -> None:
        """提示词里说明：3 分及以上但 profile 给不出编号时用 bridge 写清桥。"""
        models = FakeModelsQueue(ready=True, replies=[_scores_json(_score(0))])
        _store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        cands = [_cand(0)]
        with _TimePatch():
            _run(feeds._score(GID, settings, cands))
        prompt = _score_prompt_line(models)
        assert "bridge" in prompt
        assert "一句话" in prompt or "桥" in prompt


# ----------------------------------------------------------------------
# D 拓展名额：relevance 恰为 2、但 chat/info 都 ≥4、非 sensitive，每轮最多 1 条
#   可以作为 angle="explore" 过第二道。第三道规则不变。
# ----------------------------------------------------------------------


class TestExploreQuota:
    def test_rel2_chat4_info4_passes_as_explore(self, tmp_path) -> None:
        """relevance=2 但 chat=info=4 → 给个 explore 名额过第二道。"""
        models = FakeModelsQueue(ready=True, replies=[
            _FOCUS_JSON,
            _scores_json(_score(0, topic="拓展话题", relevance=2, chat=4, info=4,
                              timeliness=4, profile=None,
                              bridge="同厂商的另一条产品线，群里会关心")),
            _posts_json(_post(0, "量子芯片全新架构发布")),
        ])
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path, models=models,
            workers=FakeWorkers(_ok_report({"items": [_cand(0)]})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 1
        assert _rejected_rows(_store) == []
        rows = _store.read().execute(
            "SELECT angle FROM news_items WHERE rejected=0"
        ).fetchall()
        assert rows[0]["angle"] == "explore"

    def test_rel2_chat3_rejected(self, tmp_path) -> None:
        """relevance=2 但 chat 只有 3 → 走相关度拒绝（不满足拓展名额）。"""
        models = FakeModelsQueue(ready=True, replies=[
            _FOCUS_JSON,
            _scores_json(_score(0, topic="边缘话题", relevance=2, chat=3, info=4,
                              timeliness=4, profile=None,
                              bridge="有个桥但 chat 不够")),
            _posts_json(),
        ])
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path, models=models,
            workers=FakeWorkers(_ok_report({"items": [_cand(0)]})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 0
        rows = _rejected_rows(_store)
        assert len(rows) == 1
        assert rows[0]["reject_gate"] == "web"
        assert "相关度" in rows[0]["reject_reason"]

    def test_at_most_one_explore_per_round(self, tmp_path) -> None:
        """两条都满足 relevance=2 + chat/info≥4：只留 1 条 explore，另一条照常拒。"""
        items = [
            _cand(0, url="https://a.com/x", title="量子芯片全新架构发布"),
            _cand(1, url="https://b.com/y", title="编辑部圆桌会谈纪要"),
        ]
        models = FakeModelsQueue(ready=True, replies=[
            _FOCUS_JSON,
            _scores_json(
                _score(0, topic="甲", relevance=2, chat=4, info=4, timeliness=4,
                       profile=None, bridge="和群在用的工具同一厂商的新品"),
                _score(1, topic="乙", relevance=2, chat=4, info=4, timeliness=4,
                       profile=None, bridge="和群在聊的领域同类型的新鲜事"),
            ),
            _posts_json(_post(0, "量子芯片全新架构发布")),
            _posts_json(_post(0, "编辑部圆桌会谈纪要")),
        ])
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path, models=models,
            workers=FakeWorkers(_ok_report({"items": items})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 1
        rows = _rejected_rows(_store)
        assert len(rows) == 1
        # 留的是 avg 高的（这里两条 avg 一样，按实现稳定排序）；被拒的理由是相关度
        assert rows[0]["reject_gate"] == "web"

    def test_rel3_still_passes_normally(self, tmp_path) -> None:
        """relevance≥3 照旧正常过（不受拓展名额影响）。"""
        models = FakeModelsQueue(ready=True, replies=[
            _FOCUS_JSON,
            _scores_json(_score(0, topic="甲", relevance=3, profile=0)),
            _posts_json(_post(0, "量子芯片全新架构发布")),
        ])
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path, models=models,
            workers=FakeWorkers(_ok_report({"items": [_cand(0)]})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 1

    def test_explore_quota_not_for_sensitive(self, tmp_path) -> None:
        """sensitive 的条目即使是 relevance=2 也不走拓展名额。"""
        models = FakeModelsQueue(ready=True, replies=[
            _FOCUS_JSON,
            _scores_json(_score(0, topic="敏感", relevance=2, chat=4, info=4,
                              timeliness=4, sensitive=True, profile=None,
                              bridge="敏感内容不放进群")),
            _posts_json(),
        ])
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path, models=models,
            workers=FakeWorkers(_ok_report({"items": [_cand(0)]})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 0

    def test_explore_quota_not_for_guide(self, tmp_path) -> None:
        """guide 的 relevance=2 不走拓展名额（G 项：guide 有自己的门槛）。"""
        models = FakeModelsQueue(ready=True, replies=[
            _FOCUS_JSON,
            _scores_json(_score(0, topic="工具", relevance=2, chat=4, info=4,
                              timeliness=4, profile=None,
                              bridge="好文拓展不适用")),
            _posts_json(),
        ])
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path, models=models,
            workers=FakeWorkers(_ok_report({"items": [
                _cand(0, kind="guide", url="https://tut.com/x", title="经典教程",
                      published=NOW - 60 * 86400),
            ]})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 0


# ----------------------------------------------------------------------
# E 漏打分：模型漏给的候选先单独补打一次，补不上才淘汰。
#   淘汰理由写「打分漏了这条（模型没给分）」，不走相关度理由。
# ----------------------------------------------------------------------


class TestMissingScoreRescue:
    def test_missing_candidate_retried_once(self, tmp_path) -> None:
        """模型漏给一条 → 单独补打一次（只对漏的那条），成功就收。"""
        models = FakeModelsQueue(ready=True, replies=[
            _scores_json(_score(0)),               # 第一波只回了 0
            _scores_json(_score(1, relevance=4)),  # 补打回了 1
        ])
        _store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        cands = [_cand(0), _cand(1, url="https://b.com/y", title="本地部署实战全记录")]
        with _TimePatch():
            _run(feeds._score(GID, settings, cands))
        assert len(models.calls) == 2
        assert "reject" not in cands[1]
        assert cands[1]["scores"]["avg"] > 0

    def test_rescue_prompt_only_contains_missing(self, tmp_path) -> None:
        """补打提示词里只有漏的那条，不重复已评上的。"""
        models = FakeModelsQueue(ready=True, replies=[
            _scores_json(_score(0)),
            _scores_json(_score(1, relevance=4)),
        ])
        _store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        cands = [_cand(0), _cand(1, url="https://b.com/y", title="本地部署实战全记录")]
        with _TimePatch():
            _run(feeds._score(GID, settings, cands))
        retry_prompt = _score_prompt_line(models, call_idx=1)
        assert "[1]（资讯）本地部署实战全记录" in retry_prompt
        assert "[0]（资讯）量子芯片全新架构发布" not in retry_prompt

    def test_still_missing_after_rescue_rejected_as_missing(self, tmp_path) -> None:
        """补打还漏 → 淘汰理由「打分漏了这条（模型没给分）」，不写相关度 0。"""
        models = FakeModelsQueue(ready=True, replies=[
            _scores_json(_score(0)),
            '{"scores": []}',  # 补打也没给
        ])
        _store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        cands = [_cand(0), _cand(1, url="https://b.com/y", title="本地部署实战全记录")]
        with _TimePatch():
            _run(feeds._score(GID, settings, cands))
        assert cands[1]["reject"][0] == "score"
        assert "打分漏了这条" in cands[1]["reject"][1]
        assert "相关度" not in cands[1]["reject"][1]

    def test_rescue_only_once_no_third_call(self, tmp_path) -> None:
        """补打只调一次（不多调）——哪怕还漏。"""
        models = FakeModelsQueue(ready=True, replies=[
            _scores_json(_score(0)),
            '{"scores": []}',
        ])
        _store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        cands = [_cand(0), _cand(1, url="https://b.com/y", title="本地部署实战全记录")]
        with _TimePatch():
            _run(feeds._score(GID, settings, cands))
        assert len(models.calls) == 2  # 第一次 + 补打一次


# ----------------------------------------------------------------------
# F offtopic 反例进打分提示：最近被标「和群无关」的几条标题给打分模型当反例。
# ----------------------------------------------------------------------


class TestOfftopicNegativeExamples:
    def test_score_prompt_includes_offtopic_examples(self, tmp_path) -> None:
        """打分提示词里带最近被标「和群无关」的资讯标题作反例（避免放宽后变吵）。"""
        from CharTyr_MaiWork.maiwork import news_rating

        _store, settings, feeds, models, *_r = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _scores_json(_score(0)),
            ]),
        )
        # 塞一条已发布的资讯 + 一条「和本群无关」评价
        with _TimePatch():
            with _store.tx() as conn:
                cur = conn.execute(
                    "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                    " VALUES (?, ?, 1, 1, 0, '', ?)",
                    (GID, NOW - 86400, NOW - 86400),
                )
                conn.execute(
                    "INSERT INTO news_items (batch_id, group_id, title, url_key, summary,"
                    " score, created, rejected) VALUES (?, ?, ?, ?, '摘要', 4.0, ?, 0)",
                    (int(cur.lastrowid or 0), GID, "某省召开基层治理现场会", "gov.cn/x", NOW - 86400),
                )
            item_id = _store.read().execute(
                "SELECT id FROM news_items WHERE title=?", ("某省召开基层治理现场会",)
            ).fetchone()["id"]
            news_rating.rate(
                _store, GID, item_id,
                client="testclient1", reasons=["offtopic"], note="", now=NOW - 3600,
            )
            cands = [_cand(0)]
            _run(feeds._score(GID, settings, cands))
        prompt = _score_prompt_line(models)
        assert "反例" in prompt or "无关" in prompt or "不要" in prompt
        assert "某省召开基层治理现场会" in prompt

    def test_no_offtopic_ratings_no_extra_lines(self, tmp_path) -> None:
        """没有 offtopic 评价时：提示词不多带这段。"""
        models = FakeModelsQueue(ready=True, replies=[_scores_json(_score(0))])
        _store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        with _TimePatch():
            cands = [_cand(0)]
            _run(feeds._score(GID, settings, cands))
        prompt = _score_prompt_line(models)
        assert "反例" not in prompt


