"""「探索感」（2026-11，docs/02-设计.md §4.1）。

线上两周实测：测试群留下 18 条里《涂击队》占 7 条，「不出 DLC」几天内出了 3 次
（same_as_recent 没拦住），群友 👎 集中在这些；explore 方向找回 7 条全被相关度刷掉；
diverse 实际只是同话题另一条新闻；还混进了检察院表情包、「府检联动」基层动态这类
政府通讯稿。修法（H1–H6）：
- H1 新鲜感：打分加 novelty 1–5；kind=news 且 novelty≤2 第二道拒「群里已经聊过这件事」。
- H2 「跳一步」拓展：_plan_focus 的 explore 提示词改为「跳一步」并要求 bridge；每轮
  第二道给 explore 保留名额（和 D 项合并成一个「拓展名额」）。
- H3 意外度：打分加 surprise 1–5；拓展名额多条候选时按 surprise 高者优先；surprise
  也作为 news 常规排序的加分项（小权重，不改 avg 定义）。
- H4 bridge 落库（news_items.bridge 列）+ 视图 JSON 输出 bridge 和 angle。
- H5 按反馈调名额：纯函数 explore_quota(store, gid, now)。
- H6 挡政府通讯稿：域名规则 + junk 提示补一句。
- diverse 改为「对群正在聊的话题的反方/批评/另一种看法」（观点或分析，不是同话题另一条新闻）。
"""

from __future__ import annotations

import json

from CharTyr_MaiWork.maiwork.feeds import Feeds, explore_quota

from fakes import FakeModelsQueue
from test_feeds_freshness import (
    GID,
    NOW,
    _TimePatch,
    _cand,
    _insert_published,
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
# H1 新鲜感（novelty）
# ----------------------------------------------------------------------


class TestNovelty:
    def test_news_novelty_2_rejected_as_already_known(self, tmp_path) -> None:
        """news 的 novelty≤2 → 第二道拒「群里已经聊过这件事」。"""
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="涂击队", novelty=2)),
                _posts_json(),
            ]),
            workers=FakeWorkers(_ok_report({"items": [_cand(0)]})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 0
        rows = _rejected_rows(_store)
        assert len(rows) == 1
        assert rows[0]["reject_gate"] == "web"
        assert "已经聊过" in rows[0]["reject_reason"] or "已经知道" in rows[0]["reject_reason"]

    def test_news_novelty_4_passes(self, tmp_path) -> None:
        """news 的 novelty=4 → 新鲜感这关不拦。"""
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="新话题", novelty=4)),
                _posts_json(_post(0, "量子芯片全新架构发布")),
            ]),
            workers=FakeWorkers(_ok_report({"items": [_cand(0)]})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 1

    def test_guide_novelty_not_checked(self, tmp_path) -> None:
        """guide 不按 novelty 拒（新鲜感只管 news）。"""
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="教程", relevance=4, info=4, novelty=1)),
                _posts_json(_post(0, "经典老教程")),
            ]),
            workers=FakeWorkers(_ok_report({"items": [
                _cand(0, kind="guide", title="经典老教程", published=NOW - 60 * 86400),
            ]})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 1

    def test_score_prompt_asks_for_novelty(self, tmp_path) -> None:
        """打分提示词里要模型给 novelty（对照本群最近原话/画像）。"""
        models = FakeModelsQueue(ready=True, replies=[_scores_json(_score(0))])
        _store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        with _TimePatch():
            _run(feeds._score(GID, settings, [_cand(0)]))
        prompt = str(models.calls[0][1][0]["content"])
        assert "novelty" in prompt


# ----------------------------------------------------------------------
# H3 意外度（surprise）
# ----------------------------------------------------------------------


class TestSurprise:
    def test_score_prompt_asks_for_surprise(self, tmp_path) -> None:
        """打分提示词里要模型给 surprise。"""
        models = FakeModelsQueue(ready=True, replies=[_scores_json(_score(0))])
        _store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        with _TimePatch():
            _run(feeds._score(GID, settings, [_cand(0)]))
        prompt = str(models.calls[0][1][0]["content"])
        assert "surprise" in prompt

    def test_avg_unchanged_by_surprise(self, tmp_path) -> None:
        """surprise 不进 avg（avg 定义不变以免影响第三道）。"""
        models = FakeModelsQueue(ready=True, replies=[
            _scores_json(_score(0, info=4, source=4, relevance=4, timeliness=4, chat=4,
                              surprise=5)),
        ])
        _store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        cands = [_cand(0)]
        with _TimePatch():
            _run(feeds._score(GID, settings, cands))
        assert cands[0]["scores"]["avg"] == 4.0
        # surprise 要存下来（排序加分用）
        assert cands[0]["scores"].get("surprise") == 5.0


# ----------------------------------------------------------------------
# H4 bridge 落库 + 视图输出
# ----------------------------------------------------------------------


class TestBridgePersisted:
    def test_bridge_saved_to_db(self, tmp_path) -> None:
        """bridge 随 item 落库（news_items.bridge 列）。"""
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="拓展", relevance=4, profile=None,
                                  bridge="和群在用的工具是同一厂商的竞品")),
                _posts_json(_post(0, "量子芯片全新架构发布")),
            ]),
            workers=FakeWorkers(_ok_report({"items": [_cand(0)]})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 1
        row = _store.read().execute(
            "SELECT bridge FROM news_items WHERE rejected=0"
        ).fetchone()
        assert row["bridge"] == "和群在用的工具是同一厂商的竞品"

    def test_bridge_in_news_view(self, tmp_path) -> None:
        """网页 JSON 视图（news_view / guides_view）给每条带 bridge 和 angle。"""
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="拓展", relevance=4, profile=None,
                                  bridge="这个桥写得很清楚")),
                _posts_json(_post(0, "量子芯片全新架构发布")),
            ]),
            workers=FakeWorkers(_ok_report({"items": [_cand(0)]})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
            assert got == 1
            view = feeds.news_view(GID, admin=True)
        item = view[0]["items"][0]
        assert item["bridge"] == "这个桥写得很清楚"
        assert "angle" in item

    def test_bridge_migration_adds_column(self, tmp_path) -> None:
        """迁移后 news_items 有 bridge 列（老库也能追加）。"""
        from CharTyr_MaiWork.maiwork.store import Store
        store = Store(tmp_path / "t.db")
        store.migrate()
        cols = {r["name"] for r in store.read().execute("PRAGMA table_info(news_items)")}
        assert "bridge" in cols


# ----------------------------------------------------------------------
# H5 按反馈调名额（纯函数）
# ----------------------------------------------------------------------


class TestExploreQuotaFn:
    def test_quota_default_1(self, tmp_path) -> None:
        """没有反馈数据 → 名额 1。"""
        _store, settings, feeds, *_r = _make_feeds(tmp_path)
        with _TimePatch():
            assert explore_quota(_store, GID, NOW) == 1

    def test_quota_2_when_explore_liked(self, tmp_path) -> None:
        """近 14 天 explore 条目 up 多于 down 且 ≥2 → 名额 2。"""
        _store, settings, feeds, *_r = _make_feeds(tmp_path)
        with _TimePatch():
            with _store.tx() as conn:
                cur = conn.execute(
                    "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                    " VALUES (?, ?, 1, 1, 0, '', ?)",
                    (GID, NOW - 86400, NOW - 86400),
                )
                for i in range(2):
                    conn.execute(
                        "INSERT INTO news_items (batch_id, group_id, title, url_key, summary,"
                        " score, created, rejected, angle, up, down)"
                        " VALUES (?, ?, ?, ?, '摘要', 4.0, ?, 0, 'explore', 2, 0)",
                        (int(cur.lastrowid or 0), GID, f"拓展{i}", f"ex.com/{i}", NOW - 86400),
                    )
            assert explore_quota(_store, GID, NOW) == 2

    def test_quota_0_when_explore_disliked(self, tmp_path) -> None:
        """近 14 天 explore 条目 down+offtopic ≥3 且多于 up → 名额 0。"""
        _store, settings, feeds, *_r = _make_feeds(tmp_path)
        with _TimePatch():
            with _store.tx() as conn:
                cur = conn.execute(
                    "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                    " VALUES (?, ?, 1, 1, 0, '', ?)",
                    (GID, NOW - 86400, NOW - 86400),
                )
                for i in range(3):
                    conn.execute(
                        "INSERT INTO news_items (batch_id, group_id, title, url_key, summary,"
                        " score, created, rejected, angle, up, down)"
                        " VALUES (?, ?, ?, ?, '摘要', 4.0, ?, 0, 'explore', 0, 2)",
                        (int(cur.lastrowid or 0), GID, f"拓展{i}", f"ex.com/{i}", NOW - 86400),
                    )
            assert explore_quota(_store, GID, NOW) == 0

    def test_quota_min_1_per_7_days(self, tmp_path) -> None:
        """名额 0 时每 7 天至少试 1 条（防止永远关死）：最近 7 天没发过 explore → 1。"""
        _store, settings, feeds, *_r = _make_feeds(tmp_path)
        with _TimePatch():
            with _store.tx() as conn:
                cur = conn.execute(
                    "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                    " VALUES (?, ?, 1, 1, 0, '', ?)",
                    (GID, NOW - 10 * 86400, NOW - 10 * 86400),
                )
                # 10 天前的 explore 被踩（名额该 0），但最近 7 天没发过 → 保底 1
                for i in range(3):
                    conn.execute(
                        "INSERT INTO news_items (batch_id, group_id, title, url_key, summary,"
                        " score, created, rejected, angle, up, down)"
                        " VALUES (?, ?, ?, ?, '摘要', 4.0, ?, 0, 'explore', 0, 2)",
                        (int(cur.lastrowid or 0), GID, f"拓展{i}", f"ex.com/{i}", NOW - 10 * 86400),
                    )
            assert explore_quota(_store, GID, NOW) == 1


# ----------------------------------------------------------------------
# H6 挡政府通讯稿
# ----------------------------------------------------------------------


class TestGovSpamBlocked:
    def test_gov_cn_domain_auto_junk(self, tmp_path) -> None:
        """.gov.cn 域名 → 第一道拒「政府通讯稿，和群无关」（profile 指不出时）。"""
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="时政", profile=None)),
                _posts_json(),
            ]),
            workers=FakeWorkers(_ok_report({"items": [
                _cand(0, url="https://www.somecity.gov.cn/xw/123.html",
                      title="某市召开基层治理现场会"),
            ]})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 0
        rows = _rejected_rows(_store)
        assert len(rows) == 1
        assert rows[0]["reject_gate"] == "hard"
        assert "政府" in rows[0]["reject_reason"] or "通讯稿" in rows[0]["reject_reason"]

    def test_gov_passes_when_profile_matches(self, tmp_path) -> None:
        """政府域名但 profile 编号对得上且 relevance≥4 → 不拦（画像明确涉及）。"""
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="政务", relevance=4, profile=0)),
                _posts_json(_post(0, "某市召开基层治理现场会")),
            ]),
            workers=FakeWorkers(_ok_report({"items": [
                _cand(0, url="https://www.somecity.gov.cn/xw/123.html",
                      title="某市召开基层治理现场会"),
            ]})),
        )
        # 画像里加一条政务相关
        feeds._profiles.entries_map[GID] = [{
            "id": 1, "group_id": GID, "category": "ongoing",
            "text": "群里有人在基层政府做数字化", "evidence_count": 3,
            "confidence": 0.9, "first_ts": NOW - 86400, "last_ts": NOW - 3600,
            "locked": True, "deleted": False, "source": "auto",
        }]
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 1

    def test_jcy_domain_blocked(self, tmp_path) -> None:
        """检察院域名（jcy）→ 拦。"""
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="时政", profile=None)),
                _posts_json(),
            ]),
            workers=FakeWorkers(_ok_report({"items": [
                _cand(0, url="https://www.spp.jcy.gov.cn/xw/456.html",
                      title="某检察院推出表情包"),
            ]})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 0
        rows = _rejected_rows(_store)
        assert "政府" in rows[0]["reject_reason"] or "通讯稿" in rows[0]["reject_reason"]


# ----------------------------------------------------------------------
# diverse 改观点向
# ----------------------------------------------------------------------


class TestDiverseAsOpinion:
    def test_focus_prompt_asks_opinion_diverse(self, tmp_path) -> None:
        """定关注点提示词：diverse 是「对群正在聊的话题的反方/批评/另一种看法」，
        必须是观点或分析而不是同话题的另一条新闻。"""
        _store, settings, feeds, models, *_r = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[_FOCUS_JSON]),
        )
        with _TimePatch():
            _run(feeds._plan_focus(GID, settings))
        prompt = str(models.calls[0][1][-1]["content"])
        assert "反方" in prompt or "批评" in prompt or "另一种看法" in prompt
        assert "观点" in prompt or "分析" in prompt


# ----------------------------------------------------------------------
# H1 补充：Splatoon Raiders 无 DLC 为什么没被拦（排查回归测试）
# 线上同一件事 3 次没被 same_as_recent/dup_of 拦下。排查点：
# _recent_published_for_dedup 看 14 天 40 条带摘要；问题可能是打分提示里没把
# 「最近在聊」的画像条目（last_ts 较新）标出来，模型对 novelty 判不准。
# 这里加一个回归测试：打分提示里要把「最近在聊」的画像标出来。
# ----------------------------------------------------------------------


class TestRecentChatMarkedInScorePrompt:
    def test_recent_profile_entries_marked(self, tmp_path) -> None:
        """打分提示词里：last_ts 较新（近 3 天）的画像条目标成「最近在聊」。"""
        _store, settings, feeds, models, *_r = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[_scores_json(_score(0))]),
        )
        feeds._profiles.entries_map[GID] = [
            {"id": 1, "group_id": GID, "category": "interest",
             "text": "涂击队新赛季", "evidence_count": 5, "confidence": 0.9,
             "first_ts": NOW - 5 * 86400, "last_ts": NOW - 3600,  # 1 小时前
             "locked": False, "deleted": False, "source": "auto"},
            {"id": 2, "group_id": GID, "category": "interest",
             "text": "半年前的老话题", "evidence_count": 1, "confidence": 0.5,
             "first_ts": NOW - 200 * 86400, "last_ts": NOW - 180 * 86400,
             "locked": False, "deleted": False, "source": "auto"},
        ]
        with _TimePatch():
            _run(feeds._score(GID, settings, [_cand(0)]))
        prompt = str(models.calls[0][1][0]["content"])
        # 最近 3 天有新发言的画像要标出来
        assert "最近在聊" in prompt or "近期" in prompt
        # 老话题不该被标
        idx = prompt.find("半年前的老话题")
        recent_idx = prompt.find("最近在聊") if "最近在聊" in prompt else prompt.find("近期")
        assert recent_idx < idx or recent_idx == -1  # 标记出现在老话题之前




# ----------------------------------------------------------------------
# 同一轮里同一件事换站报两遍（线上 09-28 批次 9：「Splatoon Raiders 不出 DLC」两条都留了）
# ----------------------------------------------------------------------


class TestSameStoryInRound:
    def test_same_topic_same_story_keeps_one(self, tmp_path) -> None:
        items = [
            _cand(0, url="https://a.com/x",
                  title="Splatoon Raiders Officially Won't Get DLC Or Updates（制作人确认无DLC）"),
            _cand(1, url="https://b.com/y",
                  title="Splatoon Raiders won't be getting DLC or balance updates, Nintendo confirms"),
        ]
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="涂击队动态"), _score(1, topic="涂击队动态", info=5)),
                _posts_json(_post(0, items[0]["title"]), _post(1, items[1]["title"])),
            ]),
            workers=FakeWorkers(_ok_report({"items": items})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 1
        rows = _rejected_rows(_store)
        assert len(rows) == 1 and "同一件事" in rows[0]["reject_reason"]

    def test_same_topic_different_story_both_kept(self, tmp_path) -> None:
        items = [
            _cand(0, url="https://a.com/x", title="Splatoon Raiders won't get DLC or balance updates"),
            _cand(1, url="https://b.com/y", title="Splatoon Raiders sales pass one million in Japan"),
        ]
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="涂击队动态"), _score(1, topic="涂击队动态")),
                _posts_json(_post(0, items[0]["title"]), _post(1, items[1]["title"])),
            ]),
            workers=FakeWorkers(_ok_report({"items": items})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 2
