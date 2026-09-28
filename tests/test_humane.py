"""「有人味」资讯测试（docs/02-设计.md §4.1「写法：要有人味」，2026-09-27 与用户定）。

覆盖：
- 子 agent 交回的候选可带 image_url（fetch_page 给的 og:image）；
- 写帖子（feeds._write_posts）：打分之后、入库前，对过第二道门槛的每条再调一次主模型
  json_mode，输入带群原话（search_chat 的 6 条）、群画像、MaiBot 人设（host.config）、
  资讯偏好（kv["feeds.pref.<群号>"]）；输出 body/reason/refs/audience/keywords；
  代码侧 refs 换真实 {ts, who, text(≤80), message_id}、audience 只留原话里的名字、
  body 链接只留 http(s) 且最多 4 个；失败回落 body=summary、reason=why，不丢条目；
  好文同样处理；入库 body/reason/refs/audience/keywords/image_url；隐私闸过
  reason/body/audience（按片段规则）；
- 定关注点（_plan_focus）：带资讯偏好；可额外产出 0–1 个「不同角度」关注点，
  产出的条目 angle='diverse'；去同质化每轮最多留 2 条 diverse；
- chat-vote：admin_chat_vote / 接口计数；chat_votes≥2 的资讯没过「值得聊≥4」也进
  开话题候选池（48 小时内、非争议照旧），候选过期时间延长到 24 小时；
- 构想：feasibility {level, note} + keywords 入库，ideas_view 带上；
- 视图：news/guides item 带 body/reason/refs/audience/image_url/keywords/verify/chat_votes/angle，
  群友版去掉 keywords（verify 解析失败 → null）；GroupView 带 feeds_pref；
- 资讯偏好接口：GET/PUT /api/groups/{gid}/feeds-pref，群友 403、管理员可读写。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.chatlog import record_messages
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.feeds import Feeds
from CharTyr_MaiWork.maiwork.store import Store

from fakes import FakeCtx, FakeModelsQueue, FakeProfiles, focus_reply

BJ = timezone(timedelta(hours=8))
NOW = 1_790_000_000.0
GID = "111"
G1 = "900000001"
PASSWORD = "测试密码-非常显眼-不要出现在日志里"
SECRET = "sk-test-十分显眼的密钥AaBbCc123"


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
    def __enter__(self):
        import CharTyr_MaiWork.maiwork.feeds as feeds_mod

        self._mod = feeds_mod
        self._orig = feeds_mod.clock.now
        feeds_mod.clock.now = lambda: NOW
        return self

    def __exit__(self, *exc):
        self._mod.clock.now = self._orig


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


class FakeHostCfg:
    """假的 host：config(key) 预置 MaiBot 人设。"""

    def __init__(self, values: Dict[str, Any] | None = None) -> None:
        self.values = dict(values or {})
        self.keys: List[str] = []

    async def config(self, key: str, default: Any = None) -> Any:
        self.keys.append(key)
        return self.values.get(key, default)


def _ok_report(data: dict) -> Any:
    from CharTyr_MaiWork.maiwork.workers import WorkerReport

    return WorkerReport(ok=True, summary="找好了", data=data, evidence=[], steps=3)


_QUOTE = "原文里确实写着这件事，摘要能在正文找到依据。"

# 定关注点现在要求 3–5 个（少于 3 个会触发一次追问重试），统一用 fakes.focus_reply 造
_FOCUS_JSON = focus_reply("FPGA 新动态", "本地大模型新玩法", "开源掌机社区风向")

_FOCUS_DIVERSE_JSON = json.dumps(
    {
        "focus": [
            {"query": "FPGA 新动态", "why": "群里在做硬件", "source": "recent"},
            {"query": "本地大模型新玩法", "why": "长期兴趣", "source": "long"},
            {"query": "开源掌机社区风向", "why": "拓展方向", "source": "explore"},
        ],
        "diverse": {"query": "FPGA 厂商宣传水分", "why": "反方观点：新板子参数存疑"},
    },
    ensure_ascii=False,
)

_WORKER_ITEMS = {
    "items": [
        {"title": "新开源 FPGA 开发板发布", "url": "https://example.com/board",
         "summary": "一块新板子。配置不错。社区关注高。", "kind": "news",
         "published": NOW - 86400, "fetched": True, "quote": _QUOTE, "paywall": False,
         "image_url": "https://cdn.example.com/board.jpg"},
    ]
}

_SCORES_JSON = json.dumps(
    {
        "scores": [
            {"i": 0, "title": "新开源 FPGA 开发板发布", "info": 5, "source": 4, "relevance": 5,
             "timeliness": 4, "chat": 4, "profile": 0, "topic": "FPGA", "sensitive": False,
             "grounded": True, "junk": False, "junk_reason": "", "same_as_recent": False,
             "why": "群里的硬件项目正好用得上", "icon": "tools"},
        ]
    },
    ensure_ascii=False,
)


def _post_json(body: str = "开法很轻：先把板子插上就能跑。详见[官方帖](https://example.com/board)。",
               reason: str = "昨天阿一还在群里聊重装系统的事，这块板子正好能接上",
               refs=None, audience=None, keywords=None) -> str:
    return json.dumps(
        {
            "posts": [
                {
                    "i": 0,
                    "title": "新开源 FPGA 开发板发布",
                    "body": body,
                    "reason": reason,
                    "refs": refs if refs is not None else [1],
                    "audience": audience if audience is not None else ["阿一"],
                    "keywords": keywords if keywords is not None else ["FPGA", "开源硬件", "开发板", "board", "荔枝派"],
                }
            ]
        },
        ensure_ascii=False,
    )


def _make_feeds(
    tmp_path,
    *,
    models=None,
    workers=None,
    search=None,
    topics=None,
    host=None,
    cfg: dict | None = None,
) -> tuple:
    store = Store(tmp_path / "t.db")
    store.migrate()
    _seed_group(store, GID)
    settings = _settings(cfg)
    if models is None:
        models = FakeModelsQueue(ready=True)
    if workers is None:
        workers = FakeWorkers(_ok_report(_WORKER_ITEMS))
    if topics is None:
        topics = FakeTopics()
    profiles = FakeProfiles()
    profiles.entries_map[GID] = [{"category": "ongoing", "text": "在做开源硬件项目"}]
    feeds = Feeds(store, models, workers, profiles, topics, lambda: settings,
                  search=search, host=host)
    return store, settings, feeds, models, workers, topics, profiles


def _seed_chat(store: Store) -> None:
    """群里最近说过的话（写帖子要引用的原话）。"""

    class M:
        def __init__(self, mid, ts, text, name):
            self.id = mid
            self.ts = ts
            self.text = text
            self.user_id = f"u-{name}"
            self.user_name = name
            self.is_bot = False

    record_messages(store, GID, [
        M("c1", NOW - 86400, "新出的 FPGA 板子要不要上，纠结", "阿一"),
        M("c2", NOW - 80000, "重装系统之前记得先备份 home 目录", "老王"),
    ], now=NOW)


# ----------------------------------------------------------------------
# 写帖子：输入素材、refs 映射、audience 过滤、链接清洗、回落
# ----------------------------------------------------------------------


class TestWritePosts:
    def test_happy_path_full_flow(self, tmp_path) -> None:
        """写帖子成功后：body/reason/refs/audience/keywords/image_url 入库并出视图；
        prompt 里带群原话、人设、偏好。"""
        host = FakeHostCfg({
            "bot.nickname": "小麦",
            "personality.personality": "热心肠",
            "personality.reply_style": "随口一聊",
        })
        models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, _SCORES_JSON, _post_json()])
        store, settings, feeds, models, workers, topics, profiles = _make_feeds(
            tmp_path, models=models, host=host,
        )
        _seed_chat(store)
        with store.tx() as conn:
            store.kv_set(conn, f"feeds.pref.{GID}", "多找硬件折腾的，少来软文")
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 1

        # 写帖子是第三次主模型调用；prompt 里带原话、人设、偏好
        assert len(models.calls) == 3
        prompt = models.calls[2][1][-1]["content"]
        assert "新出的 FPGA 板子要不要上" in prompt  # 群原话（chat_log 搜出来的）
        assert "阿一" in prompt
        assert "小麦" in prompt and "热心肠" in prompt  # MaiBot 人设
        assert "多找硬件折腾的，少来软文" in prompt   # 资讯偏好

        row = store.read().execute("SELECT * FROM news_items WHERE rejected=0").fetchone()
        assert row is not None
        assert "开法很轻" in row["body"]
        assert "阿一" in row["reason"]
        refs = json.loads(row["refs"])
        assert len(refs) == 1
        assert set(refs[0].keys()) == {"ts", "who", "text", "message_id"}
        assert refs[0]["who"] == "阿一"
        assert refs[0]["message_id"] == "c1"
        assert len(refs[0]["text"]) <= 80
        assert json.loads(row["audience"]) == ["阿一"]
        kws = json.loads(row["keywords"])
        assert 5 <= len(kws) <= 10 or len(kws) >= 1
        assert "FPGA" in kws
        assert row["image_url"] == "https://cdn.example.com/board.jpg"

        # 视图也带上这些字段
        with _TimePatch():
            view = feeds.news_view(GID, admin=True)
        item = view[0]["items"][0]
        assert item["body"] == row["body"]
        assert item["reason"] == row["reason"]
        assert item["refs"] == refs
        assert item["audience"] == ["阿一"]
        assert item["image_url"] == "https://cdn.example.com/board.jpg"
        assert item["keywords"] == kws
        assert item["chat_votes"] == 0
        assert item["angle"] == ""
        assert item["verify"] is None

    def test_audience_filtered_to_ref_speakers(self, tmp_path) -> None:
        """audience 只留确实出现在引用原话里的名字；模型乱点名的被去掉。"""
        models = FakeModelsQueue(ready=True, replies=[
            _FOCUS_JSON, _SCORES_JSON,
            _post_json(refs=[1], audience=["阿一", "隔壁老张", "老王"]),
        ])
        store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        _seed_chat(store)
        with _TimePatch():
            assert _run(feeds.prepare_news(GID)) == 1
        row = store.read().execute("SELECT audience FROM news_items WHERE rejected=0").fetchone()
        # refs=[1] → 只有第 1 条原话（阿一）；老王没被引用，也不在 audience 里
        assert json.loads(row["audience"]) == ["阿一"]

    def test_body_links_only_https_max4(self, tmp_path) -> None:
        """body 里的链接只留 http(s)，最多 4 个；javascript:/ftp: 被洗掉。"""
        body = (
            "看[一](https://a.com/1)、[二](http://b.com/2)、[三](https://c.com/3)、"
            "[四](https://d.com/4)、[五](https://e.com/5)，"
            "别点[坏](javascript:alert(1))和[夹](ftp://f.com/x)。"
        )
        models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, _SCORES_JSON, _post_json(body=body)])
        store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        _seed_chat(store)
        with _TimePatch():
            assert _run(feeds.prepare_news(GID)) == 1
        row = store.read().execute("SELECT body FROM news_items WHERE rejected=0").fetchone()
        text = row["body"]
        assert "https://a.com/1" in text and "https://d.com/4" in text
        assert "https://e.com/5" not in text        # 第 5 个超上限被摘
        assert "javascript:" not in text
        assert "ftp://" not in text

    def test_shorter_ref_text_truncated_80(self, tmp_path) -> None:
        """refs 里的 text 截到 80 字。"""
        long_quote = ("FPGA 开发板的事我再啰嗦几句，" + "这是一条特别长的群聊原话，" * 20)  # 远超 80 字

        class M:
            def __init__(self):
                self.id = "c1"          # 和「阿一」同一 message_id，免得再多一条
                self.ts = NOW - 86400
                self.text = long_quote
                self.user_id = "u9"
                self.user_name = "长话哥"
                self.is_bot = False

        models = FakeModelsQueue(ready=True, replies=[
            _FOCUS_JSON, _SCORES_JSON, _post_json(refs=[1], audience=["长话哥"]),
        ])
        store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        record_messages(store, GID, [M()], now=NOW)
        with _TimePatch():
            assert _run(feeds.prepare_news(GID)) == 1
        row = store.read().execute("SELECT refs, audience FROM news_items WHERE rejected=0").fetchone()
        refs = json.loads(row["refs"])
        assert len(refs) == 1
        assert len(refs[0]["text"]) <= 80
        assert refs[0]["who"] == "长话哥"
        assert json.loads(row["audience"]) == ["长话哥"]

    def test_post_failure_falls_back(self, tmp_path) -> None:
        """写帖子模型抛错：条目不丢，body=summary、reason=why。"""
        class _Err(Exception):
            pass

        models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, _SCORES_JSON, _Err("炸了")])
        store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        _seed_chat(store)
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 1  # 不丢条目
        row = store.read().execute("SELECT * FROM news_items WHERE rejected=0").fetchone()
        assert row["summary"] in row["body"] or row["body"] == row["summary"]
        assert row["reason"] == row["why"]
        assert row["why"] == "群里的硬件项目正好用得上"
        assert json.loads(row["refs"]) == []
        assert json.loads(row["audience"]) == []
        # keywords 回落成候选本身的关键词（代码侧从标题/话题拼一个兜底也行——但不能崩）
        assert isinstance(json.loads(row["keywords"]), list)

    def test_post_bad_json_falls_back(self, tmp_path) -> None:
        """写帖子返回不是 JSON：同样回落，不丢条目。"""
        models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, _SCORES_JSON, "不是JSON"])
        store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        _seed_chat(store)
        with _TimePatch():
            assert _run(feeds.prepare_news(GID)) == 1
        row = store.read().execute("SELECT * FROM news_items WHERE rejected=0").fetchone()
        assert row["body"] == row["summary"]
        assert row["reason"] == row["why"]

    def test_scrub_rejects_note_fragment_in_post(self, tmp_path) -> None:
        """写出的 body/reason 含关注成员 note 片段 → 回落成 summary/why（不丢条目）。"""
        store, settings, feeds, models, *_r = _make_feeds(
            tmp_path, models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON, _SCORES_JSON,
                _post_json(body="这条给备考注册建筑师考试的朋友", reason="他在备考注册建筑师考试"),
            ]),
        )
        # 关注成员 note 片段
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO focus_members (group_id, user_id, name, note, pinned, removed, updated)"
                " VALUES (?, 'ufan', '阿帆', '他最近在备考注册建筑师考试，周三晚上没空', 0, 0, 0)",
                (GID,),
            )
        _seed_chat(store)
        with _TimePatch():
            assert _run(feeds.prepare_news(GID)) == 1
        row = store.read().execute("SELECT * FROM news_items WHERE rejected=0").fetchone()
        # 被闸的回落干净版：body=summary、reason=why、refs/audience 空
        assert "备考注册建筑师考试" not in row["body"]
        assert "备考注册建筑师考试" not in row["reason"]
        assert row["body"] == row["summary"]
        assert row["reason"] == row["why"]
        assert json.loads(row["audience"]) == []
        # 名字本身可以出现（2026-09-27 规则）——本测试的 why 就没带名字被闸；
        # 同规则的「名字放行」断言见 tests/test_audit_g7.py

    def test_guides_also_get_posts(self, tmp_path) -> None:
        """好文（kind=guide）同样写帖子、入 guides_view。"""
        guide_items = {
            "items": [
                {"title": "小模型本地部署教程", "url": "https://example.com/llm",
                 "summary": "介绍怎么在小机器上跑模型。步骤清楚。", "kind": "guide",
                 "published": NOW - 10 * 86400, "fetched": True, "quote": _QUOTE, "paywall": False},
            ]
        }
        guide_scores = json.dumps(
            {"scores": [
                {"i": 0, "title": "小模型本地部署教程", "info": 4, "source": 4, "relevance": 4,
                 "timeliness": 4, "chat": 3, "profile": 0, "topic": "本地模型", "sensitive": False,
                 "grounded": True, "junk": False, "junk_reason": "", "same_as_recent": False,
                 "why": "大家最近想自己跑模型", "icon": "robot"},
            ]},
            ensure_ascii=False,
        )
        guide_post = json.dumps(
            {"posts": [
                {"i": 0, "title": "小模型本地部署教程", "body": "教程版正文",
                 "reason": "教程版原因", "refs": [1], "audience": ["阿一"],
                 "keywords": ["本地模型", "教程"]},
            ]},
            ensure_ascii=False,
        )
        models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, guide_scores, guide_post])
        store, settings, feeds, models, *_r = _make_feeds(
            tmp_path, models=models, workers=FakeWorkers(_ok_report(guide_items)),
        )
        # 群原话里有和「本地部署」相关的，refs 才找得到
        class M0:
            id = "g1"
            ts = NOW - 5000
            text = "求推荐小模型本地部署教程，想在小机器上跑"
            user_id = "u-阿一"
            user_name = "阿一"
            is_bot = False

        record_messages(store, GID, [M0()], now=NOW)
        with _TimePatch():
            assert _run(feeds.prepare_news(GID)) == 1
            guides = feeds.guides_view(GID, admin=True)
        assert len(guides) == 1
        assert guides[0]["body"] == "教程版正文"
        assert guides[0]["reason"] == "教程版原因"
        assert guides[0]["refs"][0]["who"] == "阿一"

    def test_batch_call_and_no_posts_for_below_threshold(self, tmp_path) -> None:
        """没过第二道门槛的条目不写帖子（调一次给所有过线的写，漏了的回落）。"""
        two_items = {
            "items": [
                {"title": "新开源 FPGA 开发板发布", "url": "https://example.com/board",
                 "summary": "一块新板子。配置不错。社区关注高。", "kind": "news",
                 "published": NOW - 86400, "fetched": True, "quote": _QUOTE, "paywall": False},
                {"title": "吃桃子的十种方法", "url": "https://food.com/peach",
                 "summary": "生活小窍门。", "kind": "news",
                 "published": NOW - 86400, "fetched": True, "quote": _QUOTE, "paywall": False},
            ]
        }
        two_scores = json.dumps(
            {"scores": [
                {"i": 0, "title": "新开源 FPGA 开发板发布", "info": 5, "source": 4, "relevance": 5,
                 "timeliness": 4, "chat": 4, "profile": 0, "topic": "FPGA", "sensitive": False,
                 "grounded": True, "junk": False, "junk_reason": "", "same_as_recent": False,
                 "why": "群里的硬件项目正好用得上", "icon": "tools"},
                {"i": 1, "title": "吃桃子的十种方法", "info": 2, "source": 2, "relevance": 1,
                 "timeliness": 2, "chat": 1, "profile": 0, "topic": "生活", "sensitive": False,
                 "grounded": True, "junk": False, "junk_reason": "", "same_as_recent": False,
                 "why": "和群没啥关系", "icon": "newspaper"},
            ]},
            ensure_ascii=False,
        )
        models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, two_scores, _post_json()])
        store, settings, feeds, models, *_r = _make_feeds(
            tmp_path, models=models, workers=FakeWorkers(_ok_report(two_items)),
        )
        _seed_chat(store)
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 1  # 桃子没过第二道
        # 写帖子只调一次
        assert len(models.calls) == 3
        row = store.read().execute(
            "SELECT body, reason FROM news_items WHERE rejected=0").fetchone()
        assert "开法很轻" in row["body"]
        # 被筛的那条 body/reason 空
        rej = store.read().execute(
            "SELECT body, reason FROM news_items WHERE rejected=1").fetchone()
        assert rej["body"] == "" and rej["reason"] == ""


# ----------------------------------------------------------------------
# 不同角度（diverse）
# ----------------------------------------------------------------------


class TestDiverse:
    def test_focus_can_ask_diverse_angle(self, tmp_path) -> None:
        """定关注点返回带 diverse：它进搜索，产出的条目 angle='diverse'。"""
        models = FakeModelsQueue(ready=True, replies=[_FOCUS_DIVERSE_JSON, _SCORES_JSON, _post_json()])
        store, settings, feeds, models, workers, *_r = _make_feeds(tmp_path, models=models)
        _seed_chat(store)
        with _TimePatch():
            _run(feeds.prepare_news(GID))
        # 子 agent 的 brief 里两个关注点都有（含不同角度那句）
        brief = workers.calls[0]["brief"]
        assert "FPGA 新动态" in brief
        assert "FPGA 厂商宣传水分" in brief

    def test_diverse_items_get_angle_and_cap2(self, tmp_path) -> None:
        """去同质化：一轮最多留 2 条 diverse，超出的筛掉；留下的 angle='diverse'。"""
        _diverse_titles = [
            "FPGA 宣传的参数水分实测",
            "FPGA 新板子到底值不值得买",
            "FPGA 生态是不是被高估了",
            "FPGA 上手门槛真的低吗",
        ]
        items = {
            "items": [
                {"title": _diverse_titles[i], "url": f"https://d{i}.example.com/x",
                 "summary": f"反方第{i}条。FPGA 宣传里的水分。", "kind": "news",
                 "published": NOW - 3600, "fetched": True, "quote": _QUOTE, "paywall": False}
                for i in range(4)
            ]
        }
        scores = json.dumps(
            {"scores": [
                {"i": i, "title": _diverse_titles[i], "info": 4, "source": 4, "relevance": 4,
                 "timeliness": 4, "chat": 3, "profile": 0, "topic": f"反{i}", "sensitive": False,
                 "grounded": True, "junk": False, "junk_reason": "", "same_as_recent": False,
                 "why": "不同角度", "icon": "newspaper"}
                for i in range(4)
            ]},
            ensure_ascii=False,
        )
        # 模型给每条的打分里没有 angle 概念——diverse 是「定关注点」决定的方向
        # 实现：定关注点带 diverse 时，这轮里打标 angle='diverse' 的候选按序最多 2 条
        posts = json.dumps({"posts": []}, ensure_ascii=False)

        def _score_then_post(_replies):
            return FakeModelsQueue(ready=True, replies=_replies)

        models = FakeModelsQueue(ready=True, replies=[_FOCUS_DIVERSE_JSON, scores, posts])
        store, settings, feeds, models, workers, topics, profiles = _make_feeds(
            tmp_path, models=models, workers=FakeWorkers(_ok_report(items)),
        )
        _seed_chat(store)
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        # diverse 最多 2 条
        rows = store.read().execute(
            "SELECT angle, rejected, reject_reason FROM news_items").fetchall()
        diverse_kept = [r for r in rows if r["angle"] == "diverse" and not r["rejected"]]
        diverse_dropped = [r for r in rows if r["angle"] == "diverse" and r["rejected"]]
        assert len(diverse_kept) == 2
        assert len(diverse_dropped) == 2
        assert all("不同角度" in (r["reject_reason"] or "") for r in diverse_dropped)
        assert got == 2


# ----------------------------------------------------------------------
# chat-vote：计数与进池规则
# ----------------------------------------------------------------------


class TestChatVote:
    def _news_row(self, store: Store) -> Any:
        return store.read().execute("SELECT * FROM news_items WHERE rejected=0").fetchone()

    def test_admin_chat_vote_counts(self, tmp_path) -> None:
        store, settings, feeds, *_r = _make_feeds(tmp_path)
        models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, _SCORES_JSON, _post_json()])
        feeds._models = models
        with _TimePatch():
            _run(feeds.prepare_news(GID))
        row = self._news_row(store)
        iid = int(row["id"])
        assert feeds.admin_chat_vote(GID, iid) == {"chat_votes": 1}
        assert feeds.admin_chat_vote(GID, iid) == {"chat_votes": 2}
        # 视图也看到
        with _TimePatch():
            item = feeds.news_view(GID, admin=True)[0]["items"][0]
        assert item["chat_votes"] == 2
        # 别的群 / 不存在的条目
        with pytest.raises((KeyError, ValueError)):
            feeds.admin_chat_vote("999", iid)
        with pytest.raises((KeyError, ValueError)):
            feeds.admin_chat_vote(GID, 99999)

    def test_chat_votes_two_bypasses_chat4(self, tmp_path) -> None:
        """chat_votes≥2：即使「值得聊」<4 也进候选池（48h、非争议照旧），候选 ttl 24h。"""
        low_chat_scores = json.dumps(
            {"scores": [
                {"i": 0, "title": "新开源 FPGA 开发板发布", "info": 5, "source": 5, "relevance": 5,
                 "timeliness": 4, "chat": 2, "profile": 0, "topic": "FPGA", "sensitive": False,
                 "grounded": True, "junk": False, "junk_reason": "", "same_as_recent": False,
                 "why": "群里的硬件项目正好用得上", "icon": "tools"},
            ]},
            ensure_ascii=False,
        )
        models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, low_chat_scores, _post_json()])
        store, settings, feeds, models, workers, topics, profiles = _make_feeds(
            tmp_path, models=models,
        )
        _seed_chat(store)
        with _TimePatch():
            got = _run(feeds.prepare_news(GID))
        assert got == 1
        assert topics.calls == []  # chat=2 本来进不了池

        row = self._news_row(store)
        iid = int(row["id"])
        feeds.admin_chat_vote(GID, iid)
        feeds.admin_chat_vote(GID, iid)  # → 2 票

        # 再来一轮备料：同一条撞 URL 去重，不再进候选；所以直接检查它之后的进池判断
        # 简单点：再造一轮新的（URL 不同），先看低 chat 进不了；投票后就能进
        item_ids = [iid]
        # 直接调 feeds 的进池逻辑：把这条重新判一次
        item = dict(row)
        item["scores"] = json.loads(row["scores"])
        item["published_ts"] = row["published_ts"]
        item["chat_votes"] = 2
        now = NOW
        pool_min_avg = 4.0
        ok = feeds._pool_eligible_with_votes(item, pool_min_avg, now)
        assert ok is True

    def test_pool_candidate_ttl_extended_for_voted(self, tmp_path) -> None:
        """chat_votes≥2 进候选时，候选过期时间 24 小时（不是默认 12）。"""
        # 走全流程：先备一条 chat=2 的（进不了池），投 2 票后再让 feeds 补进池
        low_chat_scores = json.dumps(
            {"scores": [
                {"i": 0, "title": "新开源 FPGA 开发板发布", "info": 5, "source": 5, "relevance": 5,
                 "timeliness": 4, "chat": 2, "profile": 0, "topic": "FPGA", "sensitive": False,
                 "grounded": True, "junk": False, "junk_reason": "", "same_as_recent": False,
                 "why": "群里的硬件项目正好用得上", "icon": "tools"},
            ]},
            ensure_ascii=False,
        )
        models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, low_chat_scores, _post_json()])
        store, settings, feeds, models, workers, topics, profiles = _make_feeds(
            tmp_path, models=models,
        )
        _seed_chat(store)
        with _TimePatch():
            _run(feeds.prepare_news(GID))
            row = self._news_row(store)
            iid = int(row["id"])
            feeds.admin_chat_vote(GID, iid)
            feeds.admin_chat_vote(GID, iid)
        # 投票到 2：自动补进候选池（ttl 24h）
        assert len(topics.calls) >= 1
        voted_call = [c for c in topics.calls if int(c["ref_id"]) == iid]
        assert voted_call, topics.calls
        assert voted_call[0]["kind"] == "news"
        # ttl 传的是 24
        assert float(voted_call[0]["ttl_h"]) == pytest.approx(24.0)


# ----------------------------------------------------------------------
# 构想：feasibility / keywords
# ----------------------------------------------------------------------


class TestIdeaFeasibility:
    _IDEA_JSON = json.dumps(
        {"idea": {"title": "我可以帮大家把板子列表整理成对比表",
                  "body": "把最近聊到的几块 FPGA 板子收集起来，做个对比表",
                  "basis": "群里一直在聊板子选型", "step": "先列出三块板子",
                  "effort": "大概一晚上", "icon": "chart", "chat_worthy": True,
                  "feasibility": {"level": "ok", "note": "能做：靠联网搜公开参数就行"},
                  "keywords": ["FPGA", "对比表", "选型"]}},
        ensure_ascii=False,
    )

    def test_idea_stores_feasibility_and_keywords(self, tmp_path) -> None:
        models = FakeModelsQueue(ready=True, replies=[self._IDEA_JSON])
        store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        with _TimePatch():
            idea_id = _run(feeds.make_idea(GID))
        assert idea_id
        row = store.read().execute("SELECT * FROM ideas WHERE id=?", (idea_id,)).fetchone()
        feas = json.loads(row["feasibility"])
        assert feas["level"] == "ok"
        assert "能做" in feas["note"]
        assert json.loads(row["keywords"]) == ["FPGA", "对比表", "选型"]
        # 视图带上
        with _TimePatch():
            ideas = feeds.ideas_view(GID)
        assert ideas[0]["feasibility"]["level"] == "ok"
        assert ideas[0]["feasibility"]["note"]
        assert ideas[0]["keywords"] == ["FPGA", "对比表", "选型"]

    def test_idea_without_feasibility_defaults(self, tmp_path) -> None:
        """模型没给 feasibility：回落 {"level": "maybe", "note": ""}；keywords 空列表。"""
        plain = json.dumps(
            {"idea": {"title": "我可以整理一个 NAS 清单", "body": "整理清单",
                      "basis": "群里在折腾 NAS", "step": "列条目", "effort": "一小时",
                      "icon": "chart", "chat_worthy": False}},
            ensure_ascii=False,
        )
        models = FakeModelsQueue(ready=True, replies=[plain])
        store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        with _TimePatch():
            idea_id = _run(feeds.make_idea(GID))
        row = store.read().execute("SELECT * FROM ideas WHERE id=?", (idea_id,)).fetchone()
        feas = json.loads(row["feasibility"])
        assert feas["level"] == "maybe"
        assert json.loads(row["keywords"]) == []
        with _TimePatch():
            view = feeds.ideas_view(GID)[0]
        assert view["feasibility"]["level"] == "maybe"
        assert view["keywords"] == []

    def test_idea_bad_feasibility_level_normalized(self, tmp_path) -> None:
        bad = json.dumps(
            {"idea": {"title": "我可以写一个群机器人", "body": "写个 bot",
                      "basis": "群里想要", "step": "起项目", "effort": "一周",
                      "icon": "robot", "chat_worthy": False,
                      "feasibility": {"level": "impossible", "note": "不合法的 level"},
                      "keywords": "不是列表"}},
            ensure_ascii=False,
        )
        models = FakeModelsQueue(ready=True, replies=[bad])
        store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        with _TimePatch():
            idea_id = _run(feeds.make_idea(GID))
        row = store.read().execute("SELECT * FROM ideas WHERE id=?", (idea_id,)).fetchone()
        assert json.loads(row["feasibility"])["level"] == "maybe"
        assert json.loads(row["keywords"]) == []


# ----------------------------------------------------------------------
# 资讯偏好：kv + 接口 + GroupView
# ----------------------------------------------------------------------


class TestFeedsPref:
    def test_prefs_text_trimmed_and_300(self, tmp_path) -> None:
        store, settings, feeds, *_r = _make_feeds(tmp_path)
        feeds.set_pref(GID, "  多找硬件的  ")
        assert feeds.pref(GID) == "多找硬件的"
        long_text = "长" * 500
        feeds.set_pref(GID, long_text)
        assert len(feeds.pref(GID)) == 300
        feeds.set_pref(GID, "")
        assert feeds.pref(GID) == ""

    def test_focus_prompt_carries_pref(self, tmp_path) -> None:
        models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON])
        store, settings, feeds, models, *_r = _make_feeds(tmp_path, models=models)
        with store.tx() as conn:
            store.kv_set(conn, f"feeds.pref.{GID}", "这个群想看国产硬件")
        _run(feeds._plan_focus(GID, settings))
        prompt = models.calls[0][1][-1]["content"]
        assert "这个群想看国产硬件" in prompt


def _raw_config(data_dir: Path) -> dict:
    return {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}]},
        "console": {"listen": f"127.0.0.1:{_port()}", "password": PASSWORD, "public_url": ""},
        "models": {"base_url": "https://ep.test/v1", "api_key": SECRET, "main": "main-m", "worker": "worker-m"},
        "storage": {"data_dir": str(data_dir)},
        "approval": {"required": True, "admins": ["10001"]},
    }


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    raw = _raw_config(tmp_path / "data")
    ctx = FakeCtx({"config.get": "987654321"})
    app = MaiWorkApp(ctx, raw, plugin_dir=Path(__file__).resolve().parents[1])
    app.profiles_cls = FakeProfiles
    await app.start()
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    try:
        yield type("SimpleEnv", (), {"app": app, "client": client, "tmp_path": tmp_path})()
    finally:
        await client.close()
        await app.stop()


@pytest.mark.asyncio
async def test_feeds_pref_api_admin_rw(env) -> None:
    await env.client.post("/api/login", json={"password": PASSWORD})
    r = await env.client.get(f"/api/groups/{G1}/feeds-pref")
    assert r.status == 200
    assert (await r.json()) == {"text": ""}
    r = await env.client.put(f"/api/groups/{G1}/feeds-pref", json={"text": "多找开源硬件"})
    assert r.status == 200
    assert (await r.json())["text"] == "多找开源硬件"
    r = await env.client.get(f"/api/groups/{G1}/feeds-pref")
    assert (await r.json())["text"] == "多找开源硬件"
    # 超长截到 300
    r = await env.client.put(f"/api/groups/{G1}/feeds-pref", json={"text": "长" * 500})
    assert r.status == 200
    assert len((await r.json())["text"]) == 300


@pytest.mark.asyncio
async def test_feeds_pref_member_403_anon_401(env) -> None:
    token = env.app.token_of(G1)
    r = await env.client.put(
        f"/api/groups/{G1}/feeds-pref", json={"text": "x"},
        headers={"X-MW-Group": token},
    )
    assert r.status == 403
    r = await env.client.put(f"/api/groups/{G1}/feeds-pref", json={"text": "x"})
    assert r.status == 401


@pytest.mark.asyncio
async def test_group_view_has_feeds_pref(env) -> None:
    await env.client.post("/api/login", json={"password": PASSWORD})
    r = await env.client.put(f"/api/groups/{G1}/feeds-pref", json={"text": "想看硬件"})
    assert r.status == 200
    r = await env.client.get(f"/api/groups/{G1}")
    data = await r.json()
    assert data["feeds_pref"] == "想看硬件"


@pytest.mark.asyncio
async def test_chat_vote_api_and_views(env) -> None:
    """POST /api/news/{id}/chat-vote：群友或管理员都能点，只计本群条目，返回计数。"""
    await env.client.post("/api/login", json={"password": PASSWORD})
    # 塞一条本群资讯
    with env.app.store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
            " VALUES (?, ?, 1, 1, 0, '', ?)",
            (G1, clock.now() - 3600, clock.now() - 3600),
        )
        bid = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO news_items (batch_id, group_id, title, summary, sources, url_key,"
            " score, created, kind, rejected) VALUES (?, ?, '测试资讯', '摘要', '[]', 'a.com/x',"
            " 4.0, ?, 'news', 0)",
            (bid, G1, clock.now() - 3000),
        )
        iid = int(cur.lastrowid)
    token = env.app.token_of(G1)
    # 群友点两次（没身份也允许，只计数）
    r = await env.client.post(f"/api/news/{iid}/chat-vote", headers={"X-MW-Group": token})
    assert r.status == 200
    assert (await r.json()) == {"chat_votes": 1}
    r = await env.client.post(f"/api/news/{iid}/chat-vote", headers={"X-MW-Group": token})
    assert (await r.json()) == {"chat_votes": 2}
    # 管理员也能点
    r = await env.client.post(f"/api/news/{iid}/chat-vote")
    assert (await r.json()) == {"chat_votes": 3}
    # 匿名（没有任何身份的干净 client）401
    async with aiohttp.ClientSession() as anon:
        async with anon.post(f"http://127.0.0.1:{env.app.console.port}/api/news/{iid}/chat-vote") as r:
            assert r.status == 401
    r = await env.client.post("/api/news/999999/chat-vote", headers={"X-MW-Group": token})
    assert r.status == 404


def _free_port() -> int:
    """挑一个本机空闲端口：测试别依赖 18650 空着（用户可能正开着 SSH 隧道看网页）。"""
    import socket as _s
    with _s.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


_PORT: list[int] = []


def _port() -> int:
    """整个测试文件只挑一次：同一测试里前后两份配置端口要一样，不然会被当成「改了网页地址」重启。"""
    if not _PORT:
        _PORT.append(_free_port())
    return _PORT[0]


@pytest.mark.asyncio
async def test_news_run_now_api(env) -> None:
    """「现在就备一批」：群友 403、匿名 401；管理员成功 200 / 已在跑 409。"""
    token = env.app.token_of(G1)
    r = await env.client.post(f"/api/groups/{G1}/news/run", json={}, headers={"X-MW-Group": token})
    assert r.status == 403
    r = await env.client.post(f"/api/groups/{G1}/news/run", json={})
    assert r.status == 401
    await env.client.post("/api/login", json={"password": PASSWORD})
    calls = []
    env.app.run_news_now = lambda gid: (calls.append(gid), {"started": True, "reason": ""})[1]
    r = await env.client.post(f"/api/groups/{G1}/news/run", json={})
    assert r.status == 200 and calls == [G1]
    env.app.run_news_now = lambda gid: {"started": False, "reason": "这个群已经在备料了"}
    r = await env.client.post(f"/api/groups/{G1}/news/run", json={})
    assert r.status == 409 and "已经在备料" in (await r.json())["error"]
