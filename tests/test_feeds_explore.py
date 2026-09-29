"""资讯「别打转、要拓展」测试（2026-10 与用户定）。

问题（用户实测）：
- 主模型定关注点（feeds._plan_focus）经常只给 1 个，而且总围着「最近在聊」的同一个热点
  （例：连续几轮都是「Splatoon Raiders DLC 平衡补丁」）；
- 派给子 agent 的 brief（feeds._collect）里只有这几个关注点，子 agent 不会根据群画像自己拓展
  → 资讯在几个点里打转；
- 构想（feeds.make_idea）主要看群画像 + 「最近找过的资讯」，也被资讯带偏了。

本文件覆盖：
- chatlog.recent_chat：本群最近 N 小时发言 → [{ts, who, text, message_id}]，按时间从旧到新，
  最多 limit 条（取最新 limit 条再正序），单条 text 截 _RESULT_TEXT_MAX，库错误返回 []，只查本群；
- feeds._plan_focus：提示词带「群里最近两天真实在聊的（节选）」（每行「- 名字：原话」，原话截 80）
  和「最近几轮已经找过的方向」（kv["feeds.focus_hist.<群号>"]，最多 15 个，成功后追加）；
  返回每项带 source（recent|long|explore，缺省 ""）；只给 1 个也不报错；
- feeds._collect：brief 带精简群画像段（≤12 条，优先长期兴趣/在做的事，每条截 60 字）
  和「自己拓展 1–2 个方向、标 explore: true」的要求；explore=true 且没被判 diverse 的条目
  angle='explore'；去同质化对 explore 不设上限（只有 diverse 有上限）；
- feeds.make_idea：提示词带「群里最近三天真实在聊的（节选）」；资讯段改成
  「最近找过的资讯（只作参考，别围着资讯想）」且只列 5 条。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

from CharTyr_MaiWork.maiwork.chatlog import _RESULT_TEXT_MAX, recent_chat, record_messages
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.feeds import Feeds
from CharTyr_MaiWork.maiwork.store import Store

from fakes import FakeModelsQueue, FakeProfiles, focus_reply

NOW = 1_790_000_000.0
GID = "111"
GID_B = "555666777"

_FOCUS_JSON = focus_reply("FPGA 新动态", "本地大模型新玩法", "开源掌机社区风向")


def _run(coro):
    return asyncio.run(coro)


def _settings(cfg: dict | None = None) -> Any:
    settings, _ = load_settings(cfg or {})
    return settings


class _TimePatch:
    """feeds.clock.now 固定为 NOW（chatlog 和 feeds 共用同一个 clock 模块）。"""

    def __enter__(self):
        import CharTyr_MaiWork.maiwork.feeds as feeds_mod

        self._mod = feeds_mod
        self._orig = feeds_mod.clock.now
        feeds_mod.clock.now = lambda: NOW
        return self

    def __exit__(self, *exc):
        self._mod.clock.now = self._orig


class M:
    """最小的消息对象（chatlog.record_messages 用同名字段）。"""

    def __init__(self, mid: str, ts: float, text: str, *, user: str = "u1",
                 name: str = "阿一", bot: bool = False) -> None:
        self.id = mid
        self.ts = ts
        self.text = text
        self.user_id = user
        self.user_name = name
        self.is_bot = bot


class FakeWorkers:
    def __init__(self, report: Any = None) -> None:
        self.report = report
        self.calls: List[Dict[str, Any]] = []

    async def run(self, brief: str, **kwargs: Any) -> Any:
        self.calls.append({"brief": brief, **kwargs})
        if isinstance(self.report, BaseException):
            raise self.report
        return self.report


class FakeTopics:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def add_candidate(self, group_id: str, **kw: Any) -> None:
        self.calls.append({"group_id": group_id, **kw})


def _ok_report(data: dict) -> Any:
    from CharTyr_MaiWork.maiwork.workers import WorkerReport

    return WorkerReport(ok=True, summary="找好了", data=data, evidence=[], steps=3)


def _seed_group(store: Store, gid: str = GID) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, profile_ready_ts) VALUES (?, ?)",
            (gid, 1_700_000_000.0),
        )


def _store(tmp_path: Path, *, seed: bool = True) -> Store:
    store = Store(tmp_path / "t.db")
    store.migrate()
    if seed:
        _seed_group(store)
    return store


def _feeds(
    tmp_path: Path,
    *,
    replies: list | None = None,
    entries: list[dict] | None = None,
    seed: bool = True,
) -> tuple:
    store = _store(tmp_path, seed=seed)
    settings = _settings()
    models = FakeModelsQueue(ready=True, replies=list(replies or [_FOCUS_JSON]))
    workers = FakeWorkers()
    profiles = FakeProfiles()
    profiles.entries_map[GID] = (
        entries
        if entries is not None
        else [{"category": "ongoing", "text": "在做开源硬件项目"}, {"category": "interest", "text": "本地大模型"}]
    )
    topics = FakeTopics()
    feeds = Feeds(store, models, workers, profiles, topics, lambda: settings)
    return store, settings, feeds, models, workers, profiles, topics


def _prompt(models: FakeModelsQueue, idx: int = 0) -> str:
    return str(models.calls[idx][1][-1]["content"])


def _chat(mid: str, ts: float, text: str, *, name: str = "阿一", gid: str = GID, store: Store) -> None:
    record_messages(store, gid, [M(mid, ts, text, name=name)], now=ts)


def _cand_item(idx: int, *, explore: bool = False, title: str | None = None,
               summary: str = "摘要：两句讲清楚。") -> dict:
    return {
        "title": title or f"拓展候选{idx}",
        "url": f"https://example.com/e{idx}",
        "summary": summary,
        "kind": "news",
        "published": NOW - 3600,
        "fetched": True,
        "quote": "原文依据在正文里。",
        "paywall": False,
        "explore": explore,
    }


# ----------------------------------------------------------------------
# chatlog.recent_chat
# ----------------------------------------------------------------------


class TestRecentChat:
    def test_orders_old_to_new(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _chat("m1", NOW - 300, "第一句", store=store)
        _chat("m2", NOW - 200, "第二句", store=store)
        _chat("m3", NOW - 100, "第三句", store=store)
        rows = recent_chat(store, GID, hours=48, limit=60, now=NOW)
        assert [r["text"] for r in rows] == ["第一句", "第二句", "第三句"]
        assert [r["ts"] for r in rows] == sorted(r["ts"] for r in rows)
        assert set(rows[0]) == {"ts", "who", "text", "message_id", "user_id"}

    def test_limit_takes_newest_then_ascending(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        for i in range(5):
            _chat(f"m{i}", NOW - (50 - i * 10), f"第{i}句", store=store)
        rows = recent_chat(store, GID, hours=48, limit=2, now=NOW)
        assert [r["text"] for r in rows] == ["第3句", "第4句"]

    def test_only_this_group(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _chat("a1", NOW - 100, "本群的话", store=store)
        _chat("b1", NOW - 100, "别群的话", gid=GID_B, store=store)
        rows = recent_chat(store, GID, hours=48, limit=60, now=NOW)
        assert [r["text"] for r in rows] == ["本群的话"]

    def test_time_window_excludes_old(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _chat("m1", NOW - 72 * 3600, "三天前的旧话", store=store)
        _chat("m2", NOW - 3600, "一小时前的话", store=store)
        rows = recent_chat(store, GID, hours=48, limit=60, now=NOW)
        assert [r["text"] for r in rows] == ["一小时前的话"]

    def test_text_truncated(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _chat("m1", NOW - 100, "长" * 500, store=store)
        rows = recent_chat(store, GID, hours=48, limit=60, now=NOW)
        assert len(rows[0]["text"]) == _RESULT_TEXT_MAX

    def test_db_error_returns_empty(self) -> None:
        class _Bad:
            def read(self):
                raise RuntimeError("库坏了")

        assert recent_chat(_Bad(), GID, hours=48, limit=60, now=NOW) == []


# ----------------------------------------------------------------------
# _plan_focus：最近在聊 + 已经找过的方向 + source
# ----------------------------------------------------------------------


class TestPlanFocusPrompt:
    def test_prompt_has_recent_chat_excerpt(self, tmp_path: Path) -> None:
        store, settings, feeds, models, *_r = _feeds(tmp_path)
        _chat("m1", NOW - 200, "老王的 FPGA 板子到了", name="老王", store=store)
        _chat("m2", NOW - 100, "Splatoon Raiders DLC 平衡补丁又调了", name="阿一", store=store)
        _chat("m3", NOW - 72 * 3600, "三天前的老话不该出现", name="阿二", store=store)
        with _TimePatch():
            _run(feeds._plan_focus(GID, settings))
        prompt = _prompt(models)
        assert "群里最近两天真实在聊的（节选）" in prompt
        assert "- 老王：老王的 FPGA 板子到了" in prompt
        assert "- 阿一：Splatoon Raiders DLC 平衡补丁又调了" in prompt
        assert "三天前的老话不该出现" not in prompt

    def test_recent_chat_line_truncated_to_80(self, tmp_path: Path) -> None:
        store, settings, feeds, models, *_r = _feeds(tmp_path)
        _chat("m1", NOW - 100, "甲" * 120, name="阿一", store=store)
        with _TimePatch():
            _run(feeds._plan_focus(GID, settings))
        prompt = _prompt(models)
        assert "甲" * 80 in prompt
        assert "甲" * 81 not in prompt

    def test_no_chat_no_section(self, tmp_path: Path) -> None:
        store, settings, feeds, models, *_r = _feeds(tmp_path)
        with _TimePatch():
            _run(feeds._plan_focus(GID, settings))
        assert "群里最近两天真实在聊的" not in _prompt(models)

    def test_focus_hist_written_and_capped_at_15(self, tmp_path: Path) -> None:
        store, settings, feeds, models, *_r = _feeds(tmp_path)
        with store.tx() as conn:
            store.kv_set(
                conn,
                f"feeds.focus_hist.{GID}",
                [{"query": f"老方向{i}", "ts": NOW - 1000 + i} for i in range(14)],
            )
        reply = json.dumps(
            {
                "focus": [
                    {"query": "新方向A", "why": "w", "source": "recent"},
                    {"query": "新方向B", "why": "w", "source": "long"},
                ],
                "diverse": {"query": "新方向C", "why": "w"},
            },
            ensure_ascii=False,
        )
        models.reply_queue = [reply]
        with _TimePatch():
            focus = _run(feeds._plan_focus(GID, settings))
        assert len(focus) == 3
        hist = store.kv_get(f"feeds.focus_hist.{GID}")
        assert isinstance(hist, list) and len(hist) == 15
        assert [h["query"] for h in hist][-3:] == ["新方向A", "新方向B", "新方向C"]
        assert hist[0]["query"] == "老方向2"
        assert all("ts" in h for h in hist)

    def test_prompt_lists_focus_hist_after_success(self, tmp_path: Path) -> None:
        store, settings, feeds, models, *_r = _feeds(tmp_path, replies=[])
        models.reply_queue = [
            # 第一轮一次回够 3 个（不触发追问重试），第二轮的提示词该带上轮的方向
            focus_reply("上一轮找的方向", "上一轮另一个方向", "上一轮第三个方向"),
            focus_reply("这一轮换个方向", "这一轮再换个方向", "这一轮第三个方向"),
        ]
        with _TimePatch():
            _run(feeds._plan_focus(GID, settings))
            _run(feeds._plan_focus(GID, settings))
        assert len(models.calls) == 2  # 两轮各调一次（都没重试）
        prompt2 = _prompt(models, 1)
        assert "最近几轮已经找过的方向" in prompt2
        assert "上一轮找的方向" in prompt2
        assert "别再重复这些方向" in prompt2

    def test_returns_source_field(self, tmp_path: Path) -> None:
        reply = json.dumps(
            {
                "focus": [
                    {"query": "q-recent", "why": "w", "source": "recent"},
                    {"query": "q-long", "why": "w", "source": "LONG"},
                    {"query": "q-explore", "why": "w", "source": "explore"},
                    {"query": "q-bad", "why": "w", "source": "乱填"},
                    {"query": "q-none", "why": "w"},
                ]
            },
            ensure_ascii=False,
        )
        store, settings, feeds, models, *_r = _feeds(tmp_path, replies=[reply])
        with _TimePatch():
            focus = _run(feeds._plan_focus(GID, settings))
        assert [f["source"] for f in focus] == ["recent", "long", "explore", "", ""]
        assert all("source" in f for f in focus)

    def test_single_focus_retries_once_and_uses_retry(self, tmp_path: Path) -> None:
        """第一回只给 1 个 → 触发一次追问重试；重试给 3 个就用 3 个（新语义 2026-11）。"""
        store, settings, feeds, models, *_r = _feeds(tmp_path, replies=[
            focus_reply("只有一个方向"),
            focus_reply("重试方向A", "重试方向B", "重试方向C"),
        ])
        with _TimePatch():
            focus = _run(feeds._plan_focus(GID, settings))
        assert len(models.calls) == 2  # 一次原调 + 一次追问重试
        assert [f["query"] for f in focus] == ["只有一个方向", "重试方向A", "重试方向B", "重试方向C"]

    def test_single_focus_twice_keeps_single_after_one_retry(self, tmp_path: Path) -> None:
        """两回都只给 1 个：只重试一次，就用手头这 1 个继续跑，不再重试、不报错。"""
        no_source = lambda q: json.dumps({"focus": [{"query": q, "why": "w"}]}, ensure_ascii=False)
        store, settings, feeds, models, *_r = _feeds(tmp_path, replies=[
            no_source("只有一个方向"),
            no_source("换了一个方向"),
        ])
        with _TimePatch():
            focus = _run(feeds._plan_focus(GID, settings))
        assert len(models.calls) == 2  # 只重试一次
        # 两次结果按 query 去重合并（新语义）：手头两份不同的都留着
        assert [f["query"] for f in focus] == ["只有一个方向", "换了一个方向"]
        assert focus[0]["source"] == ""
        assert focus[0]["angle"] == ""


# ----------------------------------------------------------------------
# _collect：群画像段 + explore 要求 + angle
# ----------------------------------------------------------------------


class TestCollectBrief:
    def test_brief_has_profile_section_and_explore_request(self, tmp_path: Path) -> None:
        entries = [{"category": "recent", "text": f"最近聊的{i}"} for i in range(15)]
        entries += [{"category": "interest", "text": "长期兴趣A"}, {"category": "ongoing", "text": "在做的事B"}]
        store, settings, feeds, models, workers, profiles, topics = _feeds(tmp_path, entries=entries)
        workers.report = _ok_report({"items": [_cand_item(0, explore=True)]})
        focus = [{"query": "FPGA 新动态", "why": "群里在做硬件", "angle": "", "source": "long"}]
        out = _run(feeds._collect(GID, focus, settings))
        brief = str(workers.calls[0]["brief"])
        assert "这个群大致是这样的（给你拓展方向用）" in brief
        assert "长期兴趣A" in brief
        assert "在做的事B" in brief
        assert "拓展" in brief and "跳一步" in brief  # 2026-09-29 起拓展找法写在资讯标准 skill 里
        assert "explore" in brief
        assert "最近聊的9" in brief
        assert "最近聊的10" not in brief
        assert out and out[0]["angle"] == "explore"

    def test_profile_lines_truncated_to_60(self, tmp_path: Path) -> None:
        store, settings, feeds, models, workers, profiles, topics = _feeds(
            tmp_path, entries=[{"category": "interest", "text": "长" * 90}]
        )
        workers.report = _ok_report({"items": [_cand_item(0)]})
        _run(feeds._collect(GID, [{"query": "q", "why": "w", "angle": "", "source": ""}], settings))
        brief = str(workers.calls[0]["brief"])
        assert "长" * 60 in brief
        assert "长" * 61 not in brief

    def test_explore_item_keeps_angle_explore(self, tmp_path: Path) -> None:
        store, settings, feeds, models, workers, profiles, topics = _feeds(tmp_path)
        workers.report = _ok_report({"items": [_cand_item(0, explore=True), _cand_item(1, explore=False)]})
        out = _run(feeds._collect(GID, [{"query": "q", "why": "w", "angle": "", "source": ""}], settings))
        # 没标 explore、也没判成 diverse 的条目不带 angle 键（_write_posts 等地方 .get("angle") 兜底）
        assert [it.get("angle") or "" for it in out] == ["explore", ""]

    def test_diverse_beats_explore(self, tmp_path: Path) -> None:
        store, settings, feeds, models, workers, profiles, topics = _feeds(tmp_path)
        items = [
            _cand_item(
                0,
                explore=True,
                title="FPGA 厂商宣传水分到底有多少",
                summary="说说 FPGA 厂商宣传水分。",
            )
        ]
        workers.report = _ok_report({"items": items})
        focus = [{"query": "FPGA 厂商宣传水分", "why": "反方", "angle": "diverse", "source": ""}]
        out = _run(feeds._collect(GID, focus, settings))
        assert out and out[0]["angle"] == "diverse"

    def test_explore_not_capped_by_dedup(self, tmp_path: Path) -> None:
        store, settings, feeds, models, workers, profiles, topics = _feeds(tmp_path)
        survivors = [
            {
                "title": f"拓展{i}",
                "url": f"https://e{i}.example.com/a",
                "summary": "s",
                "kind": "news",
                "scores": {"avg": 4.0},
                "topic": f"拓展话题{i}",
                "site": f"e{i}.example.com",
                "angle": "explore",
            }
            for i in range(4)
        ]
        feeds._dedup_homogeneous(survivors, 10)
        assert all("reject" not in it for it in survivors)


class TestExploreEndToEnd:
    def test_explore_item_stored_with_angle(self, tmp_path: Path) -> None:
        """子 agent 标 explore 的条目过完三道门槛，入库 news_items.angle='explore'。"""
        focus = focus_reply("本地 NAS 备份方案", "本地大模型新玩法", "开源掌机社区风向")
        scores = json.dumps(
            {
                "scores": [
                    {
                        "i": 0, "title": "拓展候选0", "info": 4, "source": 4, "relevance": 4,
                        "timeliness": 4, "chat": 4, "profile": 0, "topic": "拓展话题",
                        "sensitive": False, "grounded": True, "junk": False, "junk_reason": "",
                        "same_as_recent": False, "why": "和画像对得上", "icon": "robot",
                    }
                ]
            },
            ensure_ascii=False,
        )
        store, settings, feeds, models, workers, profiles, topics = _feeds(
            tmp_path, replies=[focus, scores]
        )
        workers.report = _ok_report({"items": [_cand_item(0, explore=True)]})
        with _TimePatch():
            kept = _run(feeds.prepare_news(GID))
        assert kept == 1
        rows = store.read().execute(
            "SELECT angle, rejected FROM news_items ORDER BY id"
        ).fetchall()
        assert [(r["angle"], int(r["rejected"])) for r in rows] == [("explore", 0)]


# ----------------------------------------------------------------------
# make_idea：最近聊天节选 + 资讯只作参考、只列 5 条
# ----------------------------------------------------------------------


class TestMakeIdeaPrompt:
    def test_prompt_has_recent_chat_and_five_news(self, tmp_path: Path) -> None:
        store, settings, feeds, models, workers, profiles, topics = _feeds(
            tmp_path, replies=['{"idea": null}']
        )
        _chat("m1", NOW - 200, "最近三天真正在聊的事：NAS 装机", name="老王", store=store)
        _chat("m2", NOW - 100 * 3600, "很久以前的话不该出现", name="阿二", store=store)
        with store.tx() as conn:
            for i in range(8):
                # created 依次变旧：最近找过的资讯按 created DESC 取，前 5 条应是 0..4
                conn.execute(
                    "INSERT INTO news_items (batch_id, group_id, title, created) VALUES (?, ?, ?, ?)",
                    (1, GID, f"资讯标题{i}", NOW - 3600 - i * 10),
                )
        with _TimePatch():
            _run(feeds.make_idea(GID))
        prompt = _prompt(models)
        assert "群里最近三天真实在聊的（节选）" in prompt
        assert "- 老王：最近三天真正在聊的事：NAS 装机" in prompt
        assert "构想主要从这里和画像里的" in prompt
        assert "很久以前的话不该出现" not in prompt
        assert "最近找过的资讯（只作参考，别围着资讯想）" in prompt
        assert "最近给这个群找过的资讯（感受一下方向）" not in prompt
        assert "资讯标题0" in prompt and "资讯标题4" in prompt
        assert "资讯标题5" not in prompt
        # 隐私规则不变：basis 依旧要求「不点名群友」（最近聊天的名字只给模型看）
        assert "不点名群友" in prompt
