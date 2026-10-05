"""资讯「重复 / 话题饱和 / 新鲜度 / 写帖子鲁棒性」质量修复测试（2026-11，docs/02-设计.md §4.1）。

线上问题（用户实测两天）：同一件事换家媒体反复发、两个月前的商店页当新闻发、
话题标签漂移导致每轮话题上限形同虚设、写帖子模型只回一条裸对象时其他条目回落成烂关键词。
对应修法：
- A 去重：打分看「最近 14 天已发布的（rejected=0，最多 40 条，带话题+摘要）」，
  模型可回 dup_of=R编号（和最近发过的同一件事，含跨站跨语言）/ dup_in_batch=靠前的编号
  （同一轮里的重复，留分高的）；老的 same_as_recent=true 照拒；
- B 话题饱和：topic_coverage 统计最近已发布话题（规范化）；同一话题最近 72 小时
  已发 3 条以上的，这轮新的拒掉（信息量/新鲜度都 4.5 以上的重大新进展每话题每轮最多放行 1 条）；
  定关注点 / 子 agent brief 都带「这些已饱和，别找」；定关注点少于 3 个会重试一次；
- C 资讯新鲜度：发布时间超过 7 天的资讯代码侧硬拒（没发布时间的不拦）；
  打分后新鲜度 <3 的资讯上网页这道拒掉；商店页/比价页/资料页提示模型判「不是新闻」；
- D 写帖子：模型回裸对象 / 裸列表也认；漏写的条目补一次重试调用，再漏的才回落；
  回落的 keywords 是「话题 + 标题里有意义的词」（滤掉虚词、单字符、纯数字）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.feeds import Feeds
from CharTyr_MaiWork.maiwork.store import Store

from fakes import (
    PICK_FALLBACK_REPLY,
    FakeModelsQueue,
    FakeProfiles,
    ensure_pick_fallback,
    focus_reply,
    patch_two_phase_feeds,
    two_phase_workers_run,
)

NOW = 1_790_000_000.0
GID = "111"


def _run(coro):
    return asyncio.run(coro)


def _settings(cfg: dict | None = None) -> Any:
    settings, _ = load_settings(cfg or {})
    return settings


def _seed_group(store: Store, gid: str = GID) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, profile_ready_ts) VALUES (?, ?)",
            (gid, 1_700_000_000.0),
        )


class _TimePatch:
    """feeds.clock.now 固定成 NOW（本文件的数据都围着 NOW 造）。"""

    def __enter__(self):
        import CharTyr_MaiWork.maiwork.feeds as feeds_mod

        self._mod = feeds_mod
        self._orig = feeds_mod.clock.now
        feeds_mod.clock.now = lambda: NOW
        return self

    def __exit__(self, *exc):
        self._mod.clock.now = self._orig


class FakeWorkers:
    """假的 workers.run：预置一份 WorkerReport 或 Exception，并记录 brief。

    两阶段恒生效：feeds-discover 把预置 items 种进撒网登记簿、feeds-verify 按 brief 链接交回。
    """

    def __init__(self, report: Any = None) -> None:
        self.report = report
        self.calls: List[Dict[str, Any]] = []

    async def run(self, brief: str, **kwargs: Any) -> Any:
        self.calls.append({"brief": brief, **kwargs})
        return await two_phase_workers_run(self.report, brief, kwargs)


class FakeTopics:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def add_candidate(self, group_id: str, **kw: Any) -> None:
        self.calls.append({"group_id": group_id, **kw})


def _ok_report(data: dict) -> Any:
    from CharTyr_MaiWork.maiwork.workers import WorkerReport

    return WorkerReport(ok=True, summary="找好了", data=data, evidence=[], steps=3)


_QUOTE = "原文里确实写着这件事，摘要能在正文找到依据。"
# 定关注点要求 3–5 个（少了会触发追问重试，把本来给打分的回复吃掉），统一用 fakes.focus_reply 造
_FOCUS_JSON = focus_reply("FPGA 新动态", "本地大模型新玩法", "开源掌机社区风向")


def _cand(idx: int, *, kind: str = "news", url: str | None = None, title: str | None = None,
          published: Any = NOW - 3600, summary: str | None = None) -> dict:
    base_titles = [
        "量子芯片全新架构发布", "本地部署实战全记录", "编辑部圆桌会谈纪要", "开源周报精选集",
        "硬件入门避坑清单", "某语言运行时细节解析", "无线电通联小知识", "云端成本控制心得",
    ]
    return {
        "title": title or base_titles[idx % len(base_titles)],
        "url": url or f"https://example.com/post-{idx}",
        "summary": summary or f"摘要{idx}：两三句话讲清楚这件事。",
        "kind": kind,
        "published": published,
        "fetched": True,
        "quote": _QUOTE,
        "paywall": False,
    }


def _score(idx: int, *, topic: str | None = None,
           info: float = 4, source: float = 4, relevance: float = 4,
           timeliness: float = 4, chat: float = 4, profile: Any = 0,
           junk: bool = False, junk_reason: str = "",
           same_as_recent: bool = False, **extra: Any) -> dict:
    out = {
        "i": idx,
        "info": info,
        "source": source,
        "relevance": relevance,
        "timeliness": timeliness,
        "chat": chat,
        "profile": profile,
        "topic": topic if topic is not None else f"话题{idx}",
        "sensitive": False,
        "grounded": True,
        "junk": junk,
        "junk_reason": junk_reason,
        "same_as_recent": same_as_recent,
        "why": "和画像对得上",
        "icon": "robot",
    }
    out.update(extra)
    return out


def _scores_json(*scores: dict) -> str:
    return json.dumps({"scores": list(scores)}, ensure_ascii=False)


def _posts_json(*posts: dict) -> str:
    return json.dumps({"posts": list(posts)}, ensure_ascii=False)


def _post(idx: int, title: str, body: str | None = None) -> dict:
    return {
        "i": idx,
        "title": title,
        "body": body if body is not None else f"{title}的正文，按口吻写的。",
        "reason": "群里正聊着这个方向",
        "refs": [],
        "audience": [],
        "keywords": ["关键词甲", "关键词乙"],
    }


def _insert_published(store: Store, gid: str, *, title: str, topic: str = "",
                      url_key: str = "", created: float = NOW - 86400,
                      rejected: int = 0, summary: str = "摘要") -> None:
    """往库里塞一条已出过的资讯（默认已通过 rejected=0）。"""
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
            " VALUES (?, ?, 1, 1, 0, '', ?)",
            (gid, created, created),
        )
        conn.execute(
            "INSERT INTO news_items (batch_id, group_id, title, url_key, summary, topic,"
            " score, created, rejected) VALUES (?, ?, ?, ?, ?, ?, 4.0, ?, ?)",
            (int(cur.lastrowid or 0), gid, title, url_key, summary, topic, created, rejected),
        )


def _make_feeds(tmp_path, *, models: FakeModelsQueue | None = None,
                workers: FakeWorkers | None = None) -> tuple:
    store = Store(tmp_path / "t.db")
    store.migrate()
    _seed_group(store, GID)
    settings = _settings()
    if models is None:
        models = FakeModelsQueue(ready=True)
    ensure_pick_fallback(models)
    if workers is None:
        workers = FakeWorkers(_ok_report({"items": []}))
    topics = FakeTopics()
    profiles = FakeProfiles()
    profiles.entries_map[GID] = [{"category": "ongoing", "text": "在做开源硬件项目"}]
    feeds = Feeds(store, models, workers, profiles, topics, lambda: settings)
    if workers is not None:
        # 2026-10-01 撒网改代码按计划搜：FakeWorkers 预置的 items 挪给假搜索出；
        # 懒取 report——有用例在建好 _feeds 之后才换 report（出错→撒网全挂）
        def _live_items():
            if isinstance(getattr(workers, "report", None), BaseException):
                raise RuntimeError("预置：撒网全挂")
            r = getattr(workers, "report", None)
            if r is not None and not getattr(r, "ok", True):
                raise RuntimeError("预置：子 agent 没跑成")
            data = getattr(r, "data", None)
            if isinstance(data, dict):
                items = list(data.get("items") or [])
                sf = data.get("seed_focus")  # 老用例：假装都是某方向（diverse 4）搜出来的
                if isinstance(sf, int) and sf >= 1:
                    return {sf: items}
                return items
            return []

        patch_two_phase_feeds(feeds, models, _live_items)
    return store, settings, feeds, models, workers, topics, profiles


def _score_prompt(models: FakeModelsQueue) -> str:
    """打分那次的提示词（两阶段后队列里隔着 feeds.pick，不能按下标 1 拿）。"""
    for _role, messages, kwargs in models.calls:
        if str(kwargs.get("purpose") or "") == "feeds.score":
            return str(messages[0]["content"])
    raise AssertionError("打分模型没被调用")


def _rejected_rows(store: Store) -> list:
    return store.read().execute(
        "SELECT * FROM news_items WHERE rejected=1 ORDER BY id").fetchall()


# ----------------------------------------------------------------------
# A 去重：打分参考的「最近发过的」只算已发布（rejected=0），带话题 + 摘要，
#    模型可指 dup_of（和最近发过的同一件事）/ dup_in_batch（同一轮里重复）。
# ----------------------------------------------------------------------


class TestRecentPublishedForDedup:
    def test_score_prompt_lists_published_not_rejected_and_numbered(self, tmp_path) -> None:
        """打分提示词里的「最近发过的」：只列已发布条目（被拒的不出现），
        按 R1/R2 编号、带话题和摘要开头；候选块也在提示词里。"""
        store, settings, feeds, models, workers, topics, _ = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="硬件")),
                _posts_json(_post(0, "新开源 FPGA 开发板发布")),
            ]),
            workers=FakeWorkers(_ok_report({"items": [
                _cand(0, url="https://example.com/board", title="新开源 FPGA 开发板发布"),
            ]})),
        )
        with _TimePatch():
            _insert_published(store, GID, title="新掌机官宣下周发售", topic="掌机",
                              url_key="ok.com/1", summary="下周发售，参数全公开。")
            _insert_published(store, GID, title="被筛掉的旧闻不该出现", topic="旧话题",
                              url_key="no.cn/2", rejected=1)
            assert _run(feeds.prepare_news(GID)) == 1
        prompt = _score_prompt(models)
        assert "R1" in prompt
        assert "新掌机官宣下周发售" in prompt
        assert "掌机" in prompt
        assert "下周发售" in prompt          # 摘要开头也要带上
        assert "被筛掉的旧闻不该出现" not in prompt

    def test_dup_of_valid_pointer_hard_rejects(self, tmp_path) -> None:
        """模型指 dup_of=R1 → 硬拒，理由带那条已发布的标题（前 30 字）。"""
        published_title = "新掌机官宣下周发售，定价公布"
        store, settings, feeds, models, workers, topics, _ = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="硬件", dup_of="R1")),
                _posts_json(),
            ]),
            workers=FakeWorkers(_ok_report({"items": [
                _cand(0, url="https://other-outlet.com/switch-launch",
                      title="外媒报道：新掌机公布发售日期和价格，和之前官宣的一致"),
            ]})),
        )
        with _TimePatch():
            _insert_published(store, GID, title=published_title, topic="掌机", url_key="ok.com/9")
            assert _run(feeds.prepare_news(GID)) == 0
        rows = _rejected_rows(store)
        assert len(rows) == 1
        assert rows[0]["reject_gate"] == "hard"
        assert "同一件事" in rows[0]["reject_reason"]
        assert published_title[:30] in rows[0]["reject_reason"]
        # 被 dup 拒的条目在打分这道就出局，不该走到写帖子
        assert len(models.calls) == 3  # 两阶段后 +「挑」：focus + pick + score

    def test_dup_of_backward_compat_same_as_recent_still_rejects(self, tmp_path) -> None:
        """老字段 same_as_recent=true 照拒（向后兼容）。"""
        store, settings, feeds, models, workers, topics, _ = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="硬件", same_as_recent=True)),
                _posts_json(),
            ]),
            workers=FakeWorkers(_ok_report({"items": [
                _cand(0, url="https://example.com/dup", title="和最近出过的一件事"),
            ]})),
        )
        with _TimePatch():
            assert _run(feeds.prepare_news(GID)) == 0
        rows = _rejected_rows(store)
        assert len(rows) == 1
        assert rows[0]["reject_gate"] == "hard"
        assert "同一件事" in rows[0]["reject_reason"]

    def test_dup_in_batch_keeps_higher_avg_rejects_lower(self, tmp_path) -> None:
        """dup_in_batch 指向前面的编号：两条留分高的，另一条含理由「留分高的」。"""
        items = [
            _cand(0, url="https://a.com/splatoon-raiders", title="涂击队新作确认不含追加内容"),
            _cand(1, url="https://b.com/splatoon-raiders-2", title="另一个媒体：涂击队新作不会有 DLC"),
            _cand(2, url="https://c.com/other", title="编辑部圆桌会谈纪要"),
        ]
        store, settings, feeds, models, workers, topics, _ = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(
                    _score(0, topic="涂击队", info=3, source=3, relevance=4, timeliness=4, chat=4),
                    _score(1, topic="涂击队", info=5, source=5, relevance=5, timeliness=5, chat=5, dup_in_batch=0),
                    _score(2, topic="圆桌", info=4, source=4, relevance=4, timeliness=4, chat=4),
                ),
                _posts_json(_post(0, "另一个媒体：涂击队新作不会有 DLC"),
                            _post(1, "编辑部圆桌会谈纪要")),
            ]),
            workers=FakeWorkers(_ok_report({"items": items})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 2
        rows = _rejected_rows(store)
        assert len(rows) == 1
        assert rows[0]["title"] == "涂击队新作确认不含追加内容"  # 分低的被拒
        assert "留分高的" in rows[0]["reject_reason"]
        assert "另一个媒体：涂击队新作不会有 DLC"[:30] in rows[0]["reject_reason"]
        # 留的两条都写上了帖子：第二通模型调用回帖时按「留下的顺序」重新编号
        kept = store.read().execute(
            "SELECT title, body FROM news_items WHERE rejected=0 ORDER BY id").fetchall()
        assert {r["title"] for r in kept} == {"另一个媒体：涂击队新作不会有 DLC", "编辑部圆桌会谈纪要"}
        assert all(r["body"] for r in kept)

    def test_dup_in_batch_invalid_pointer_ignored(self, tmp_path) -> None:
        """dup_in_batch 指向不存在的编号 / 自己 / 后面的编号 → 忽略，不拒。"""
        items = [
            _cand(0, url="https://a.com/x", title="量子芯片全新架构发布"),
            _cand(1, url="https://b.com/y", title="本地部署实战全记录"),
        ]
        store, settings, feeds, models, workers, topics, _ = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(
                    _score(0, topic="甲", dup_in_batch=0),          # 指自己 → 无效
                    _score(1, topic="乙", dup_in_batch="R2"),       # 指 R 编号 → 无效
                ),
                _posts_json(_post(0, "量子芯片全新架构发布", body="帖子甲"),
                            _post(1, "本地部署实战全记录", body="帖子乙")),
                _posts_json(),  # 兜底：万一实现里多了一次重试也不炸（队列空时不该发生）
            ]),
            workers=FakeWorkers(_ok_report({"items": items})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 2
        assert _rejected_rows(store) == []

    def test_ideas_dedup_uses_published_titles_only(self, tmp_path) -> None:
        """构想去重参考的「最近资讯」也只列已发布的（被拒的不给模型看）。"""
        store, settings, feeds, models, *_r = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[json.dumps(
                {"idea": {"title": "我可以整理对比表", "body": "整一下", "basis": "群里在聊",
                          "step": "列条目", "effort": "一小时", "icon": "chart",
                          "chat_worthy": False}},
                ensure_ascii=False,
            )]),
        )
        with _TimePatch():
            _insert_published(store, GID, title="已发布的资讯标题", url_key="ok.cn/1")
            _insert_published(store, GID, title="被拒的资讯标题不该给构想看", url_key="no.cn/2", rejected=1)
            _run(feeds.make_idea(GID))
        prompt = str(models.calls[0][1][-1]["content"])
        assert "已发布的资讯标题" in prompt
        assert "被拒的资讯标题不该给构想看" not in prompt


# ----------------------------------------------------------------------
# B 话题饱和：topic_coverage / 72 小时滚动上限 / 定关注点和 brief 都带饱和提示
# ----------------------------------------------------------------------


class TestTopicCoverage:
    def test_counts_only_published_normalized_sorted(self, tmp_path) -> None:
        """topic_coverage：只数已发布的；同一群；规范化（去空格、拉丁小写）合并计数，
        多的排前面；窗口外的不算。"""
        store, settings, feeds, *_r = _make_feeds(tmp_path)
        with _TimePatch():
            _insert_published(store, GID, title="t1", topic=" 涂击队资讯 ", url_key="a.cn/1")
            _insert_published(store, GID, title="t2", topic="涂击队资讯", url_key="a.cn/2")
            _insert_published(store, GID, title="t3", topic="Splatoon", url_key="a.cn/3")
            _insert_published(store, GID, title="t4", topic=" splatoon ", url_key="a.cn/4")
            _insert_published(store, GID, title="t5", topic="SPLATOON", url_key="a.cn/5")
            _insert_published(store, GID, title="t6", topic="被拒话题", url_key="a.cn/6", rejected=1)
            _insert_published(store, GID, title="t7", topic="老话题",
                              url_key="a.cn/7", created=NOW - 20 * 86400)
            _insert_published(store, "999", title="t8", topic="涂击队资讯", url_key="a.cn/8")
            got = feeds.topic_coverage(GID, 14)
        counts = dict(got)
        assert counts["涂击队资讯"] == 2
        assert counts["splatoon"] == 3
        assert "被拒话题" not in counts
        assert "老话题" not in counts
        # 排序：多的在前（splatoon 3 > 涂击队资讯 2）
        assert got[0][0] == "splatoon"
        # 规范化后迭代给的也应该是规范形
        assert all(label == label.strip() for label, _ in got)

    def test_score_prompt_lists_recent_topics_and_asks_reusing(self, tmp_path) -> None:
        """打分提示词列「最近 14 天用过的话题标签」，并要求属于这些话题的沿用原标签。"""
        store, settings, feeds, models, workers, topics, _ = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="硬件")),
                _posts_json(_post(0, "新开源 FPGA 开发板发布")),
            ]),
            workers=FakeWorkers(_ok_report({"items": [
                _cand(0, url="https://example.com/board", title="新开源 FPGA 开发板发布"),
            ]})),
        )
        with _TimePatch():
            _insert_published(store, GID, title="旧发一条", topic="涂击队资讯", url_key="a.cn/1")
            assert _run(feeds.prepare_news(GID)) == 1
        prompt = _score_prompt(models)
        assert "涂击队资讯" in prompt
        assert "沿用" in prompt or "原样" in prompt or "同一个标签" in prompt


class TestTopicRollingCap:
    def _seed_splatoon_x3(self, store: Store) -> None:
        """最近 72 小时里「涂击队资讯」已发 3 条（达上限）。"""
        for i in range(3):
            _insert_published(
                store, GID, title=f"涂击队旧闻{i}", topic="涂击队资讯",
                url_key=f"old.cn/{i}", created=NOW - (i + 1) * 86400,
            )

    def test_topic_saturated_rejects_new_even_with_variant_label(self, tmp_path) -> None:
        """同一话题 72 小时已发 3 条：新一轮同话题的条目被拒（标签差一点也算同一话题，
        如 涂击队动态 vs 涂击队资讯）；理由说明「最近三天已经发了 3 条」。"""
        items = [
            _cand(0, url="https://new.cn/splatoon", title="涂击队新一轮平衡补丁说明"),
            _cand(1, url="https://new.cn/other", title="编辑部圆桌会谈纪要"),
        ]
        store, settings, feeds, models, workers, topics, _ = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(
                    _score(0, topic="涂击队动态"),   # 和已发的「涂击队资讯」是同一话题
                    _score(1, topic="圆桌会谈"),
                ),
                _posts_json(_post(0, "编辑部圆桌会谈纪要")),
            ]),
            workers=FakeWorkers(_ok_report({"items": items})),
        )
        with _TimePatch():
            self._seed_splatoon_x3(store)
            got = _run(feeds.prepare_news(GID))
        assert got == 1
        rows = _rejected_rows(store)
        assert len(rows) == 1
        assert rows[0]["title"] == "涂击队新一轮平衡补丁说明"
        assert rows[0]["reject_gate"] == "web"
        assert "3" in rows[0]["reject_reason"]
        assert "三天" in rows[0]["reject_reason"]
        assert "涂击队资讯" in rows[0]["reject_reason"]

    def test_topic_cap_exception_for_big_news_one_per_topic_per_batch(self, tmp_path) -> None:
        """重大新进展（信息量和新�"鲜度都 4.5 以上）可以破格，但同一话题每轮最多放行 1 条。"""
        items = [
            _cand(0, url="https://new.cn/big1", title="涂击队服务器遭入侵官方公告"),
            _cand(1, url="https://new.cn/big2", title="涂击队总监辞职官宣"),
        ]
        store, settings, feeds, models, workers, topics, _ = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(
                    _score(0, topic="涂击队资讯", info=5, source=5, relevance=5,
                           timeliness=5, chat=5),
                    _score(1, topic="涂击队资讯", info=5, source=4.6, relevance=4.6,
                           timeliness=4.6, chat=4.6),
                ),
                _posts_json(_post(0, "涂击队服务器遭入侵官方公告", body="大新闻一号"),
                            _post(1, "涂击队总监辞职官宣", body="大新闻二号")),
            ]),
            workers=FakeWorkers(_ok_report({"items": items})),
        )
        with _TimePatch():
            self._seed_splatoon_x3(store)
            got = _run(feeds.prepare_news(GID))
        # 两条都是「大新闻」，但每话题每轮最多放行 1 条 → 留 1 拒 1
        assert got == 1
        kept = store.read().execute(
            "SELECT title FROM news_items WHERE rejected=0 AND batch_id=(SELECT MAX(id) FROM news_batches)"
        ).fetchall()
        assert {r["title"] for r in kept} == {"涂击队服务器遭入侵官方公告"}
        rows = [r for r in _rejected_rows(store) if r["title"] == "涂击队总监辞职官宣"]
        assert len(rows) == 1
        assert "三天" in rows[0]["reject_reason"]

    def test_topic_below_cap_passes(self, tmp_path) -> None:
        """同一话题 72 小时内只发过 2 条（没到 3）：正常放行。"""
        store, settings, feeds, models, workers, topics, _ = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="涂击队资讯")),
                _posts_json(_post(0, "涂击队新赛季预告")),
            ]),
            workers=FakeWorkers(_ok_report({"items": [
                _cand(0, url="https://new.cn/splat", title="涂击队新赛季预告"),
            ]})),
        )
        with _TimePatch():
            for i in range(2):
                _insert_published(
                    store, GID, title=f"涂击队旧闻{i}", topic="涂击队资讯",
                    url_key=f"old.cn/{i}", created=NOW - (i + 1) * 86400,
                )
            assert _run(feeds.prepare_news(GID)) == 1
        assert _rejected_rows(store) == []

    def test_per_batch_caps_still_apply(self, tmp_path) -> None:
        """每轮话题上限（同话题最多 2 条）仍然生效。"""
        items = [
            _cand(0, url="https://x.cn/a", title="量子芯片全新架构发布"),
            _cand(1, url="https://x.cn/b", title="量子芯片另一则新闻"),
            _cand(2, url="https://x.cn/c", title="量子芯片第三条新闻"),
        ]
        store, settings, feeds, models, workers, topics, _ = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(
                    _score(0, topic="量子芯片"),
                    _score(1, topic="量子芯片", chat=3),  # 分低一点
                    _score(2, topic="量子芯片"),
                ),
                _posts_json(_post(0, "量子芯片全新架构发布"), _post(1, "量子芯片第三条新闻")),
            ]),
            workers=FakeWorkers(_ok_report({"items": items})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 2
        rows = _rejected_rows(store)
        assert len(rows) == 1
        assert "同一个话题" in rows[0]["reject_reason"]


class TestSaturatedHints:
    def test_focus_prompt_lists_saturated_topics(self, tmp_path) -> None:
        """定关注点的提示词：最近 7 天发得最多的话题（≥2 条）列为「已饱和，别找」。"""
        store, settings, feeds, models, *_r = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[_FOCUS_JSON]),
        )
        with _TimePatch():
            for i in range(2):
                _insert_published(
                    store, GID, title=f"涂击队旧闻{i}", topic="涂击队资讯",
                    url_key=f"old.cn/{i}", created=NOW - (i + 1) * 86400,
                )
            _insert_published(store, GID, title="单机旧闻", topic="单机游戏",
                              url_key="old.cn/9", created=NOW - 86400)
            _run(feeds._plan_focus(GID, settings))
        prompt = str(models.calls[0][1][-1]["content"])
        assert "饱和" in prompt
        assert "涂击队资讯" in prompt
        assert "单机游戏" not in prompt  # 只发过 1 条，不算饱和

    def test_collect_brief_lists_saturated_topics(self, tmp_path) -> None:
        """定关注点提示词：带「这些话题最近已经发得很多了，不要再找」一行
        （2026-10-01 起撒网改代码按计划搜，饱和话题从那一段挪这里）。"""
        store, settings, feeds, models, workers, topics, _ = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="新话题")),
                _posts_json(_post(0, "新开源 FPGA 开发板发布")),
            ]),
            workers=FakeWorkers(_ok_report({"items": [
                _cand(0, url="https://example.com/board", title="新开源 FPGA 开发板发布"),
            ]})),
        )
        with _TimePatch():
            for i in range(2):
                _insert_published(
                    store, GID, title=f"涂击队旧闻{i}", topic="涂击队资讯",
                    url_key=f"old.cn/{i}", created=NOW - (i + 1) * 86400,
                )
            assert _run(feeds.prepare_news(GID)) == 1
        prompt = models.calls[0][1][0]["content"]
        assert "涂击队资讯" in prompt
        assert "发得很多" in prompt or "不要再找" in prompt or "别再找" in prompt


class TestFocusRetry:
    def test_focus_fewer_than_3_retries_once_and_merges(self, tmp_path) -> None:
        """模型回的焦点不到 3 个：带一句追问重试一次；两次结果按 query 去重合并，
        历史只记一次（合并后那份）。"""
        first = json.dumps({"focus": [{"query": "FPGA 新动态", "why": "群里在做硬件"}]},
                           ensure_ascii=False)
        retry = json.dumps({"focus": [
            {"query": "FPGA 新动态", "why": "重试里也带它（去重）"},
            {"query": "本地大模型新玩法", "why": "长期兴趣"},
            {"query": "开源掌机社区风向", "why": "拓展方向"},
        ]}, ensure_ascii=False)
        models = FakeModelsQueue(ready=True, replies=[first, retry])
        store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        with _TimePatch():
            out = _run(feeds._plan_focus(GID, settings))
        assert len(models.calls) == 2
        assert [f["query"] for f in out] == ["FPGA 新动态", "本地大模型新玩法", "开源掌机社区风向"]
        # 历史只记一次（合并后那份 3 个）
        hist = feeds.focus_history(GID)
        assert [h["query"] for h in hist] == ["FPGA 新动态", "本地大模型新玩法", "开源掌机社区风向"]

    def test_focus_retry_still_fewer_keeps_merged_without_error(self, tmp_path) -> None:
        """重试也没凑够 3 个：不报错、不再重试，用手头合并后的继续跑，历史记合并后的。"""
        first = json.dumps({"focus": [{"query": "FPGA 新动态", "why": "x"}]}, ensure_ascii=False)
        retry = json.dumps({"focus": [{"query": "另一个方向", "why": "y"}]}, ensure_ascii=False)
        models = FakeModelsQueue(ready=True, replies=[first, retry])
        store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        with _TimePatch():
            out = _run(feeds._plan_focus(GID, settings))
        assert len(models.calls) == 2
        assert [f["query"] for f in out] == ["FPGA 新动态", "另一个方向"]
        hist = feeds.focus_history(GID)
        assert [h["query"] for h in hist] == ["FPGA 新动态", "另一个方向"]


# ----------------------------------------------------------------------
# C 资讯新鲜度：超过 7 天的代码侧硬拒；打分后新鲜度 <3 的上网页这道拒；
#   商店页/比价页/资料页提示模型判「不是新闻」。
# ----------------------------------------------------------------------


class TestNewsFreshness:
    def test_old_news_hard_rejected_over_7_days(self, tmp_path) -> None:
        """发布时间在 7 天前的资讯：代码侧硬拒「旧闻：N 天前发的」，不走到打分。"""
        items = [
            _cand(0, url="https://old.cn/game", title="一个月前发售游戏的商店页",
                  published=NOW - 30 * 86400),
        ]
        store, settings, feeds, models, workers, topics, _ = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[_FOCUS_JSON]),
            workers=FakeWorkers(_ok_report({"items": items})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 0
        rows = _rejected_rows(store)
        assert len(rows) == 1
        assert rows[0]["reject_gate"] == "hard"
        assert "旧闻" in rows[0]["reject_reason"]
        assert "30" in rows[0]["reject_reason"]
        assert len(models.calls) == 2  # 两阶段后只调了定关注点 +「挑」，没调打分

    def test_news_within_7_days_passes(self, tmp_path) -> None:
        """6 天前发的资讯：新鲜度这关不拦（其他门槛照常过）。"""
        items = [_cand(0, url="https://new.cn/x", title="六天前的资讯还新鲜",
                       published=NOW - 6 * 86400)]
        store, settings, feeds, models, workers, topics, _ = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="硬件")),
                _posts_json(_post(0, "六天前的资讯还新鲜")),
            ]),
            workers=FakeWorkers(_ok_report({"items": items})),
        )
        with _TimePatch():
            assert _run(feeds.prepare_news(GID)) == 1
        assert _rejected_rows(store) == []

    def test_news_unknown_published_not_rejected(self, tmp_path) -> None:
        """拿不到发布时间的资讯：代码侧不因新鲜度硬拒（交给模型的新鲜度分去管）。"""
        items = [_cand(0, url="https://new.cn/y", title="没写时间的资讯", published="")]
        store, settings, feeds, models, workers, topics, _ = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="硬件")),
                _posts_json(_post(0, "没写时间的资讯")),
            ]),
            workers=FakeWorkers(_ok_report({"items": items})),
        )
        with _TimePatch():
            assert _run(feeds.prepare_news(GID)) == 1
        assert _rejected_rows(store) == []

    def test_old_guide_not_hard_rejected(self, tmp_path) -> None:
        """好文（kind=guide）不受资讯的 7 天硬规则限制——60 天内的文章只要现在还适用照样收
        （文章另有 60 天硬线，2026-10-05 由 180 改 60，见 test_news_guide.py）。"""
        items = [_cand(0, kind="guide", url="https://tut.cn/z", title="经典老教程",
                       published=NOW - 40 * 86400)]
        store, settings, feeds, models, workers, topics, _ = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="教程")),
                _posts_json(_post(0, "经典老教程")),
            ]),
            workers=FakeWorkers(_ok_report({"items": items})),
        )
        with _TimePatch():
            assert _run(feeds.prepare_news(GID)) == 1
        assert _rejected_rows(store) == []

    def test_low_timeliness_news_web_rejected(self, tmp_path) -> None:
        """模型给的新鲜度 <3 的资讯：上网页这道拒「不够新（新鲜度 x.x）」；
        好文即使新鲜度低也不按这条拒。"""
        items = [
            _cand(0, url="https://a.cn/n1", title="量子芯片全新架构发布"),
            _cand(1, kind="guide", url="https://b.cn/g1", title="本地部署实战全记录",
                  published=NOW - 40 * 86400),
        ]
        store, settings, feeds, models, workers, topics, _ = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(
                    _score(0, topic="甲", timeliness=2.0),   # 资讯不新鲜 → 拒
                    # 好文 timeliness 低不关「不够新」的事（info=5 让平均够文章的 3.8 线）
                    _score(1, topic="乙", timeliness=2.0, info=5),
                ),
                _posts_json(_post(0, "本地部署实战全记录")),
            ]),
            workers=FakeWorkers(_ok_report({"items": items})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 1
        rows = _rejected_rows(store)
        assert len(rows) == 1
        assert rows[0]["title"] == "量子芯片全新架构发布"
        assert rows[0]["reject_gate"] == "web"
        assert "不够新" in rows[0]["reject_reason"]
        assert "2.0" in rows[0]["reject_reason"]
        # 好文没被「不够新」拒：确实过线入库了
        kept = store.read().execute(
            "SELECT title, kind FROM news_items WHERE rejected=0").fetchall()
        assert [(r["title"], r["kind"]) for r in kept] == [("本地部署实战全记录", "guide")]

    def test_store_page_prompt_hints(self, tmp_path) -> None:
        """打分提示词：明说商店页/比价页/资料页/已发售基本资料不是资讯 → junk；
        超过 7 天的资讯新鲜度打到 2 以下；brief 规则 1/3 写明资讯要 7 天内、这���页面不算资讯。"""
        store, settings, feeds, models, workers, topics, _ = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="硬件", junk=True,
                                    junk_reason="不是新闻（商店页/资料页）")),
            ]),
            workers=FakeWorkers(_ok_report({"items": [
                _cand(0, url="https://store.cn/game", title="商店页：某游戏基本信息一览",
                      published=NOW - 3600),
            ]})),
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 0
        prompt = _score_prompt(models)
        assert "商店页" in prompt
        assert "不是新闻" in prompt
        # 核验子 agent 的 brief（两阶段恒生效后「看那类页面算不算资讯」在这里）：「最近 7 天」+「商店页不算资讯」
        briefs = "\n".join(
            str(c["brief"]) for c in workers.calls
            if str(c.get("task_id") or "").startswith("feeds-verify:")
        )
        assert "7 天" in briefs
        assert "商店页" in briefs
        rows = _rejected_rows(store)
        assert len(rows) == 1
        assert rows[0]["reject_gate"] == "hard"
        assert "不是新闻" in rows[0]["reject_reason"]
        # 被拒在打分这道，没走到写帖子（两阶段后 +「挑」= 3 次调用）
        assert len(models.calls) == 3


# ----------------------------------------------------------------------
# D 写帖子鲁棒性：裸对象 / 裸列表也认；漏写的补一次重试；回落 keywords 滤虚词
# ----------------------------------------------------------------------


class TestWritePostsRobust:
    def _two_item_setup(self, tmp_path, post_replies: list) -> tuple:
        """两条候选都过线，写帖子的模型回复用 post_replies（先进先出）。"""
        items = [
            _cand(0, url="https://a.cn/1", title="量子芯片全新架构发布"),
            _cand(1, url="https://b.cn/2", title="本地部署实战全记录"),
        ]
        models = FakeModelsQueue(ready=True, replies=[
            _FOCUS_JSON,
            _scores_json(_score(0, topic="甲"), _score(1, topic="乙")),
            *post_replies,
        ])
        store, settings, feeds, models, workers, topics, _ = _make_feeds(
            tmp_path,
            models=models,
            workers=FakeWorkers(_ok_report({"items": items})),
        )
        return store, settings, feeds, models, workers, topics

    def test_bare_single_post_object_accepted(self, tmp_path) -> None:
        """模型只回一个裸帖子对象（没有 {"posts":[...]} 外套）：也算这一条的帖子。"""
        bare = json.dumps(
            {"i": 1, "title": "本地部署实战全记录", "body": "裸对象正文",
             "reason": "裸对象原因", "refs": [], "audience": [], "keywords": ["裸词"]},
            ensure_ascii=False,
        )
        store, settings, feeds, models, workers, topics = self._two_item_setup(
            tmp_path, [bare, _posts_json(_post(0, "量子芯片全新架构发布", body="重试补写的正文"))],
        )
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 2
        rows = store.read().execute(
            "SELECT title, body FROM news_items WHERE rejected=0 ORDER BY id").fetchall()
        by_title = {r["title"]: r["body"] for r in rows}
        assert by_title["本地部署实战全记录"] == "裸对象正文"
        # 漏掉的另一条走了补写重试（第二轮只写它一条）
        assert by_title["量子芯片全新架构发布"] == "重试补写的正文"
        assert len(models.calls) == 6  # 两阶段后 +「挑」：focus + pick + score + post + 补写 + 对原文自检
        retry_prompt = str(models.calls[4][1][-1]["content"])
        assert "量子芯片全新架构发布" in retry_prompt
        assert "本地部署实战全记录" not in retry_prompt  # 只补漏写的

    def test_bare_list_accepted(self, tmp_path) -> None:
        """模型回一个裸列表（[{"i":0,...},{"i":1,...}]）：直接当 posts 列表用。"""
        bare_list = json.dumps(
            [_post(0, "量子芯片全新架构发布", body="正文零"),
             _post(1, "本地部署实战全记录", body="正文一")],
            ensure_ascii=False,
        )
        store, settings, feeds, models, workers, topics = self._two_item_setup(
            tmp_path, [bare_list],
        )
        with _TimePatch():
            assert _run(feeds.prepare_news(GID)) == 2
        rows = store.read().execute(
            "SELECT title, body FROM news_items WHERE rejected=0 ORDER BY id").fetchall()
        assert [r["body"] for r in rows] == ["正文零", "正文一"]
        assert len(models.calls) == 5  # 都写上了，不用补（+「挑」+ 对原文自检）

    def test_retry_only_missing_and_fallback_for_still_missing(self, tmp_path) -> None:
        """第一回漏写，重试又只补了一条；还漏的那条才走 _post_fallback（body=summary）。"""
        first = _posts_json(_post(0, "量子芯片全新架构发布", body="第一回写上的"))
        retry = _posts_json()  # 重试一条都不给
        store, settings, feeds, models, workers, topics = self._two_item_setup(
            tmp_path, [first, retry],
        )
        with _TimePatch():
            assert _run(feeds.prepare_news(GID)) == 2
        assert len(models.calls) == 6  # 含「挑」和对原文自检（两阶段后 +1）
        rows = store.read().execute(
            "SELECT title, body FROM news_items WHERE rejected=0 ORDER BY id").fetchall()
        by_title = {r["title"]: r["body"] for r in rows}
        assert by_title["量子芯片全新架构发布"] == "第一回写上的"
        # 还漏的回落：body=summary
        assert by_title["本地部署实战全记录"] == "摘要1：两三句话讲清楚这件事。"


class TestPostFallbackKeywords:
    def test_fallback_keywords_filter_stopwords_and_digits(self, tmp_path) -> None:
        """回落 keywords：话题优先，再补标题里 2 个字以上、非纯数字、非英文虚词的词。"""
        store, settings, feeds, *_r = _make_feeds(tmp_path)
        item = {
            "title": "Why Your Nintendo Switch 2 Costs 60 Dollars After 2026",
            "topic": "主机涨价",
            "summary": "摘要正文",
            "why": "原因",
        }
        feeds._post_fallback(item)
        kw = item["post"]["keywords"]
        assert kw[0] == "主机涨价"                      # 话题在最前
        assert "Why" not in kw and "Your" not in kw     # 英文虚词滤掉
        assert "After" not in kw
        assert "2" not in kw                            # 单字符
        assert "60" not in kw and "2026" not in kw      # 纯数字
        assert "Nintendo" in kw and "Switch" in kw and "Costs" in kw
        assert len(kw) <= 10
