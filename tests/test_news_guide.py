"""「文章」（kind=guide，好文）收紧（2026-11，docs/02-设计.md §4.1）。

线上 10 天实测留下的 27 条 guide 里混了：发布 349/475/1426 天的（1426 天那条是
Last Epoch 构建模拟器工具页还被 timeliness 打了 4）、新闻类的（Switch2 Pro 手柄 IGN 8 分
评测报道、巫师3 重制版上线、Steam 新品节公告）、非文章页（GitHub 仓库首页、官方文档
入门页、工具页）。修法：
1. 硬门槛（第一道，代码判）：guide 必须有发布时间且在 60 天内（GUIDE_MAX_AGE_DAYS；原 180，2026-10-05 用户改 60）；
   没有发布时间一律不收「文章没有发布时间，宁缺毋滥不收」，太旧「文章太旧：N 天前发的」。
2. 打分提示给 guide 加判断字段 not_article + not_article_reason：文章只收教程/指南/攻略/
   经验复盘/深度分析/横向对比实测；新闻、商店页/产品页/工具页/构建器/代码仓库首页/
   官方文档入口页/百科资料页、纯观点短评一律 not_article=true → 第一道淘汰
   「不是文章（新闻/工具页…）」。
3. 门槛提高：guide 过第二道要 relevance≥4 且 info≥4 且 avg≥3.8；拓展名额和 B 项
   relevance=3 的放宽都不适用于 guide；每轮 guide 最多 1 条（原 2，2026-10-05 用户改）。
4. 子 agent brief 同步写明这些标准。
"""

from __future__ import annotations

from CharTyr_MaiWork.maiwork.feeds import Feeds

from fakes import FakeModelsQueue
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


def _guide(idx: int, *, published=NOW - 30 * 86400, title: str = "经典老教程", **kw) -> dict:
    return _cand(idx, kind="guide", title=title, published=published,
                 url=kw.pop("url", f"https://tut.com/g{idx}"), **kw)


# ----------------------------------------------------------------------
# 1. 发布时间硬门槛
# ----------------------------------------------------------------------


class TestGuideAgeCap:
    def test_guide_no_published_rejected(self, tmp_path) -> None:
        """guide 没有发布时间 → 硬拒「文章没有发布时间，宁缺毋滥不收」。"""
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[_FOCUS_JSON]),
            workers=FakeWorkers(_ok_report({"items": [
                _guide(0, published=""),
            ]})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 0
        rows = _rejected_rows(_store)
        assert len(rows) == 1
        assert rows[0]["reject_gate"] == "hard"
        assert "没有发布时间" in rows[0]["reject_reason"]

    def test_guide_61_days_rejected(self, tmp_path) -> None:
        """guide 发布 61 天 → 拒「太旧」（上限 60 天）。

        两阶段恒生效后这道在预筛：连打分都不进、不落 news_items，
        理由进漏斗「预筛刷掉的」。
        暗线变化：老 gate 的「文章太旧：N 天前发的」文案不再能拦到这类（题目到不了 gate）。
        """
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[_FOCUS_JSON]),
            workers=FakeWorkers(_ok_report({"items": [
                _guide(0, published=NOW - 61 * 86400),
            ]})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 0
        assert _rejected_rows(_store) == []
        batch = _store.read().execute(
            "SELECT * FROM news_batches ORDER BY id DESC LIMIT 1").fetchone()
        stats = _store.kv_get(f"feeds.batch_stats.{batch['id']}") or {}
        rejects = (stats.get("funnel") or {}).get("rejects") or {}
        assert any("太旧" in k for k in rejects), f"预筛刷掉的应记太旧：{rejects}"
        # 根本没走到打分
        assert not any(str(c[2].get("purpose") or "") == "feeds.score" for c in models.calls)

    def test_guide_59_days_passes_age_gate(self, tmp_path) -> None:
        """guide 发布 59 天 → 过年龄硬门槛（其他门槛照常打）。"""
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="教程", relevance=4, info=4)),
                _posts_json(_post(0, "经典老教程")),
            ]),
            workers=FakeWorkers(_ok_report({"items": [
                _guide(0, published=NOW - 59 * 86400),
            ]})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 1
        assert _rejected_rows(_store) == []

    def test_guide_age_uses_guide_not_news_line(self, tmp_path) -> None:
        """guide 远超上限照样在预筛挡下（口径「太旧：超过 60 天」，guide/news 共用一条文案）。"""
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[_FOCUS_JSON]),
            workers=FakeWorkers(_ok_report({"items": [
                _guide(0, published=NOW - 400 * 86400),
            ]})),
        )
        with _TimePatch():
            _run(feeds.prepare_news(GID))
        assert _rejected_rows(_store) == []
        batch = _store.read().execute(
            "SELECT * FROM news_batches ORDER BY id DESC LIMIT 1").fetchone()
        stats = _store.kv_get(f"feeds.batch_stats.{batch['id']}") or {}
        rejects = (stats.get("funnel") or {}).get("rejects") or {}
        assert any("太旧" in k for k in rejects), f"预筛刷掉的应记太旧：{rejects}"


# ----------------------------------------------------------------------
# 2. not_article 判断
# ----------------------------------------------------------------------


class TestNotArticle:
    def test_news_like_guide_rejected_as_not_article(self, tmp_path) -> None:
        """guide 被判 not_article=true（是新闻不是文章）→ 硬拒。"""
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="游戏", not_article=True,
                                  not_article_reason="是新品发布报道，不是文章")),
                _posts_json(),
            ]),
            workers=FakeWorkers(_ok_report({"items": [
                # 年龄在门槛内，单看 not_article 这一条（469 天那条线上实例会先被年龄挡）
                _guide(0, title="Switch2 Pro 手柄 IGN 8 分", published=NOW - 30 * 86400),
            ]})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 0
        rows = _rejected_rows(_store)
        assert len(rows) == 1
        assert rows[0]["reject_gate"] == "hard"
        assert "不是文章" in rows[0]["reject_reason"]

    def test_github_repo_homepage_rejected(self, tmp_path) -> None:
        """GitHub 仓库首页 → not_article（工具页）。"""
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="工具", not_article=True,
                                  not_article_reason="GitHub 仓库首页，不是文章")),
                _posts_json(),
            ]),
            workers=FakeWorkers(_ok_report({"items": [
                _guide(0, title="unjs/untracing", published=NOW - 40 * 86400),
            ]})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 0
        rows = _rejected_rows(_store)
        assert "不是文章" in rows[0]["reject_reason"]

    def test_real_guide_passes_not_article(self, tmp_path) -> None:
        """真正的教程/攻略：not_article=false → 不误伤。"""
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="攻略", relevance=4, info=4,
                                  not_article=False)),
                _posts_json(_post(0, "深度对比：五款桌面笔记软件")),
            ]),
            workers=FakeWorkers(_ok_report({"items": [
                _guide(0, title="深度对比：五款桌面笔记软件"),
            ]})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 1


# ----------------------------------------------------------------------
# 3. 第二道门槛提高 + 每轮上限
# ----------------------------------------------------------------------


class TestGuideSecondGate:
    def test_guide_relevance_3_rejected(self, tmp_path) -> None:
        """guide 的 relevance=3 不够（要 ≥4）→ 拒。"""
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="教程", relevance=3, info=4)),
                _posts_json(),
            ]),
            workers=FakeWorkers(_ok_report({"items": [_guide(0)]})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 0
        rows = _rejected_rows(_store)
        assert rows[0]["reject_gate"] == "web"
        assert "相关度" in rows[0]["reject_reason"]

    def test_news_relevance_3_passes(self, tmp_path) -> None:
        """news 的 relevance=3 可过（对照组：news 第二道仍是 ≥3）。"""
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="新闻", relevance=3)),
                _posts_json(_post(0, "量子芯片全新架构发布")),
            ]),
            workers=FakeWorkers(_ok_report({"items": [_cand(0)]})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 1

    def test_guide_avg_3_5_rejected(self, tmp_path) -> None:
        """guide 的 avg 要 ≥3.8（3.5 不够）→ 拒。"""
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                # relevance=4、info=4 都够，但 (4+3+4+3+3)/5=3.4 < 3.8 → 按平均分拒
                _scores_json(_score(0, topic="教程", relevance=4, chat=3, info=4,
                                  source=3, timeliness=3)),
                _posts_json(),
            ]),
            workers=FakeWorkers(_ok_report({"items": [_guide(0)]})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 0
        rows = _rejected_rows(_store)
        assert rows[0]["reject_gate"] == "web"
        assert "平均" in rows[0]["reject_reason"] or "avg" in rows[0]["reject_reason"].lower()

    def test_guide_avg_3_8_passes(self, tmp_path) -> None:
        """guide avg=3.8 恰好够（边界值）。"""
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                # 4+4+4+4+3=19/5=3.8
                _scores_json(_score(0, topic="教程", relevance=4, chat=4, info=4,
                                  source=4, timeliness=3)),
                _posts_json(_post(0, "经典老教程")),
            ]),
            workers=FakeWorkers(_ok_report({"items": [_guide(0)]})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 1

    def test_guide_max_1_per_round(self, tmp_path) -> None:
        """每轮 guide 最多 1 条（第 2、3 条被拒，理由说明上限；2026-10-05 由 2 改 1）。"""
        items = [_guide(i, title=f"老教程{i}", url=f"https://tut.com/g{i}") for i in range(3)]
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(
                    _score(0, topic="教甲", relevance=4, info=4, chat=4),
                    _score(1, topic="教乙", relevance=4, info=4, chat=4),
                    _score(2, topic="教丙", relevance=4, info=4, chat=4),
                ),
                _posts_json(_post(0, "老教程0"), _post(1, "老教程1"), _post(2, "老教程2")),
            ]),
            workers=FakeWorkers(_ok_report({"items": items})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 1
        rows = _rejected_rows(_store)
        assert len(rows) == 2
        for row in rows:
            assert row["reject_gate"] == "web"
            assert "文章这轮已经留了 1 篇" in row["reject_reason"]


# ----------------------------------------------------------------------
# 4. brief 同步写明标准
# ----------------------------------------------------------------------


class TestGuideBrief:
    def test_collect_brief_mentions_guide_criteria(self, tmp_path) -> None:
        """子 agent brief 里写明 guide 的收录标准（教程/指南/深度分析才收，60 天内）。"""
        _store, settings, feeds, models, workers, topics, _p = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="新话题")),
                _posts_json(_post(0, "量子芯片全新架构发布")),
            ]),
            workers=FakeWorkers(_ok_report({"items": [_cand(0)]})),
        )
        with _TimePatch():
            _run(feeds.prepare_news(GID))
        brief = str(workers.calls[0]["brief"])
        assert "60" in brief or "天" in brief
        assert "教程" in brief or "指南" in brief or "文章" in brief


