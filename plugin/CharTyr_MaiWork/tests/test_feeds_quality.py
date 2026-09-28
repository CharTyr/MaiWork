"""资讯「质量标准」测试（docs/02-设计.md §4.1、docs/07 §9.3/§10.4）。

三道门槛：
- 第一道（硬性淘汰）：没打开过原文（fetched 不真 / quote 空）、付费/登录（paywall）、
  URL/标题重复、同一件事（模型 same_as_recent）、屏蔽来源（配置 + 网页 + 「没用」自动）、
  不扎实（模型 grounded=false）、垃圾（模型 junk=true）。
- 第二道（上网页）：五项 1–5 分（info/source/relevance/timeliness/chat），
  avg >= [feeds] web_min_avg（默认 3）且 relevance >= 3；profile 编号对不上 → relevance 封顶 2；
  去同质化：同 topic <= 2、同域名 <= 3、sensitive <= 1、总数 <= max_items（默认 10），按 avg 高者留。
- 第三道（进话题候选池）：kind=news、avg >= pool_min_avg（默认 4）、relevance >= 4、chat >= 4、
  published 48 小时内、非 sensitive；好文（kind=guide）一律不进。

被筛掉的也入库（rejected=1 + reject_gate/reject_reason），news_view 的管理员版能看到；
群友视图没有 rejected 一栏。好文走单独的 guides_view（30 天、最多 20 条）。

管理员接口：POST /api/feeds/domains（屏蔽/解除），GET /api/settings 带 feeds 段。
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

from CharTyr_MaiWork import clock
from CharTyr_MaiWork.app import MaiWorkApp
from CharTyr_MaiWork.config import CONFIG_VERSION, load_settings
from CharTyr_MaiWork.feeds import Feeds, _normalize_domain
from CharTyr_MaiWork.store import Store

from fakes import FakeCtx, FakeModelsQueue, FakeProfiles

BJ = timezone(timedelta(hours=8))
NOW = 1_790_000_000.0
GID = "111"
G1 = "900000001"  # 接口测试用的群号（test_console 同款）
SECRET = "sk-test-十分显眼的密钥AaBbCc123"
PASSWORD = "测试密码-非常显眼-不要出现在日志里"


def _run(coro):
    return asyncio.run(coro)


def _settings(cfg: dict | None = None) -> Any:
    settings, _ = load_settings(cfg or {})
    return settings


def _seed_group(store: Store, gid: str = GID, *, ready: bool = True) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, profile_ready_ts) VALUES (?, ?)",
            (gid, 1_700_000_000.0 if ready else 0.0),
        )


class _TimePatch:
    """feeds.clock.now 固定为 NOW（本文件的数据都围着 NOW 造）。"""

    def __enter__(self):
        import CharTyr_MaiWork.feeds as feeds_mod

        self._mod = feeds_mod
        self._orig = feeds_mod.clock.now
        feeds_mod.clock.now = lambda: NOW
        return self

    def __exit__(self, *exc):
        self._mod.clock.now = self._orig


class FakeWorkers:
    """假的 workers.run：预置一份 WorkerReport 或 Exception，并记录 brief。"""

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
    from CharTyr_MaiWork.workers import WorkerReport

    return WorkerReport(ok=True, summary="找好了", data=data, evidence=[], steps=3)


_FOCUS_JSON = json.dumps(
    {"focus": [{"query": "FPGA 新动态", "why": "群里在做硬件"}]},
    ensure_ascii=False,
)


def _cand(
    idx: int,
    *,
    kind: str = "news",
    url: str | None = None,
    title: str | None = None,
    fetched: bool = True,
    quote: str = "原文里确实写着这件事，摘要能在正文找到依据。",
    paywall: bool = False,
    published: Any = NOW - 3600,
) -> dict:
    """造一条子 agent 交回的候选。"""
    # 默认标题必须两两不相似（difflib < 0.8）：带上次序和一批完全不搭界的词
    base_titles = [
        "量子芯片全新架构发布", "本地部署实战全记录", "编辑部圆桌会谈纪要", "开源周报精选集",
        "硬件入门避坑清单", "某语言运行时细节解析", "无线电通联小知识", "云端成本控制心得",
        "独立游戏开发杂记", "数据库索引漫谈", "相机传感器科普", "家用路由折腾史",
    ]
    return {
        "title": title or base_titles[idx % len(base_titles)],
        "url": url or f"https://example.com/post-{idx}",
        "summary": f"摘要{idx}：两三句话讲清楚这件事。",
        "kind": kind,
        "published": published,
        "fetched": fetched,
        "quote": quote,
        "paywall": paywall,
    }


def _score(
    idx: int,
    *,
    info: float = 4,
    source: float = 4,
    relevance: float = 4,
    timeliness: float = 4,
    chat: float = 4,
    profile: Any = 1,
    topic: str | None = None,
    sensitive: bool = False,
    grounded: bool = True,
    junk: bool = False,
    junk_reason: str = "",
    same_as_recent: bool = False,
    why: str = "和画像对得上",
    icon: str = "robot",
) -> dict:
    return {
        "i": idx,
        "info": info,
        "source": source,
        "relevance": relevance,
        "timeliness": timeliness,
        "chat": chat,
        "profile": profile,
        "topic": topic if topic is not None else f"话题{idx}",
        "sensitive": sensitive,
        "grounded": grounded,
        "junk": junk,
        "junk_reason": junk_reason,
        "same_as_recent": same_as_recent,
        "why": why,
        "icon": icon,
    }


def _scores_json(*scores: dict) -> str:
    return json.dumps({"scores": list(scores)}, ensure_ascii=False)


def _make_feeds(
    tmp_path,
    *,
    items: list[dict] | None = None,
    scores: str | list[str] | None = None,
    cfg: dict | None = None,
    seed: bool = True,
    ready: bool = True,
    workers: FakeWorkers | None = None,
    models: FakeModelsQueue | None = None,
    topics: FakeTopics | None = None,
    entries: list[dict] | None = None,
) -> tuple:
    """默认一条候选、一条全 4 分的打分，顺顺过完三道。"""
    store = Store(tmp_path / "t.db")
    store.migrate()
    if seed:
        _seed_group(store, GID, ready=ready)
    settings = _settings(cfg)
    if models is None:
        if scores is None:
            scores = _scores_json(_score(0, topic="默认话题"))
        reply_list = [scores] if isinstance(scores, str) else list(scores)
        models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, *reply_list])
    if workers is None:
        if items is None:
            items = [_cand(0, url="https://example.com/board", title="新开源 FPGA 开发板发布")]
        workers = FakeWorkers(_ok_report({"items": items}))
    if topics is None:
        topics = FakeTopics()
    profiles = FakeProfiles()
    profiles.entries_map[GID] = (
        entries
        if entries is not None
        else [
            {"category": "ongoing", "text": "在做开源硬件项目"},
            {"category": "interest", "text": "本地大模型"},
        ]
    )
    feeds = Feeds(store, models, workers, profiles, topics, lambda: settings)
    return store, settings, feeds, models, workers, topics, profiles


def _rows(store: Store, sql: str = "SELECT * FROM news_items ORDER BY id") -> list:
    return store.read().execute(sql).fetchall()


def _accepted(rows: list) -> list:
    return [r for r in rows if not r["rejected"]]


def _rejected(rows: list) -> list:
    return [r for r in rows if r["rejected"]]


def _rej_reason(rows: list, url_part: str) -> str | None:
    for r in rows:
        if url_part in str(r["url_key"]):
            return r["reject_reason"]
    return None


def _brief(workers: FakeWorkers) -> str:
    assert workers.calls, "子 agent 没被调用"
    return str(workers.calls[0]["brief"])


def _score_prompt(models: FakeModelsQueue) -> str:
    assert len(models.calls) >= 2, "打分模型没被调用"
    return str(models.calls[1][1][0]["content"])


# ----------------------------------------------------------------------
# 前置 / 子 agent brief（第一道在代码前的那半）
# ----------------------------------------------------------------------


def test_prepare_news_not_ready_returns_0(tmp_path) -> None:
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path, ready=False)
    assert _run(feeds.prepare_news(GID)) == 0
    assert models.calls == []
    assert _rows(store) == []


def test_brief_asks_news_and_guides_with_fetch_and_paywall(tmp_path) -> None:
    """brief 要同时让子 agent 找资讯和好文，真打开过 + 引用原文，付费的标出来。"""
    items = [
        _cand(0, title="全新的 FPGA TEST 板卡"),
        _cand(1, kind="guide", url="https://tut.com/aaa-bbb-guide", title="从零开始的部署手册", published=""),
    ]
    scores = _scores_json(*[_score(i, topic=f"话题{i}") for i in range(2)])
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, items=items, scores=scores
    )
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 2
    brief = _brief(workers)
    assert "好文" in brief
    assert "资讯" in brief
    assert "fetch_page" in brief
    assert "fetched" in brief
    assert "quote" in brief
    assert "paywall" in brief


def test_guides_off_brief_keeps_news_only(tmp_path) -> None:
    """[feeds] guides = false：brief 不再找好文，只找资讯。"""
    cfg = {"feeds": {"guides": False}}
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path, cfg=cfg)
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 1
    assert "好文" not in _brief(workers)


# ----------------------------------------------------------------------
# 第一道：硬性淘汰（每条淘汰的入库 rejected=1，gate='hard'）
# ----------------------------------------------------------------------


def test_not_fetched_candidates_rejected(tmp_path) -> None:
    """fetched 不真或 quote 空 → 第一道淘汰，理由「原文没打开过/打不开」。"""
    items = [
        _cand(0, title="没打开", url="https://a.com/1", fetched=False, quote="有点依据"),
        _cand(1, title="空引用", url="https://b.com/2", fetched=True, quote="   "),
        _cand(2, title="打不开", url="https://c.com/3", fetched=False, quote=""),
        _cand(3, title="正常", url="https://d.com/4"),
    ]
    scores = _scores_json(_score(0, topic="唯一话题"))
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, items=items, scores=scores
    )
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 1
    rows = _rows(store)
    assert len(rows) == 4  # 被筛掉的也入库
    assert len(_accepted(rows)) == 1
    hard = _rejected(rows)
    assert {r["reject_gate"] for r in hard} == {"hard"}
    for r in hard:
        assert "原文没打开过" in r["reject_reason"]
    # 打分只招呼到幸存的那 1 条；「有人味」写帖子是第三次模型调用（过第二道门槛的才写）
    assert len(models.calls) == 3
    assert models.calls[2][2].get("purpose") == "feeds.post"
    assert "摘要3" in _score_prompt(models)


def test_paywall_rejected(tmp_path) -> None:
    items = [_cand(0, paywall=True, title="付费墙", url="https://pay.com/x")]
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, items=items, scores="{}"
    )
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 0
    rows = _rows(store)
    assert len(rows) == 1
    assert rows[0]["rejected"] == 1
    assert rows[0]["reject_gate"] == "hard"
    assert "付费" in rows[0]["reject_reason"] or "登录" in rows[0]["reject_reason"]
    assert len(models.calls) == 1  # 只调了定关注点，没调打分


def test_dup_url_rejected_before_scoring(tmp_path) -> None:
    """规范化后撞最近已出的 URL → 第一道淘汰，理由含「重复」。"""
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path,
        items=[_cand(0, url="https://Example.com/board/?utm_source=x", title="板子再度发布")],
        scores=_scores_json(_score(0)),
    )
    with _TimePatch():
        with store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                " VALUES (?, ?, 1, 1, 0, '', ?)",
                (GID, NOW - 86400, NOW - 86400),
            )
            conn.execute(
                "INSERT INTO news_items (batch_id, group_id, title, url_key, score, created)"
                " VALUES (?, ?, '旧闻', 'example.com/board', 1.0, ?)",
                (int(cur.lastrowid or 0), GID, NOW - 86400),
            )
        assert _run(feeds.prepare_news(GID)) == 0
    rows = _rows(store, "SELECT * FROM news_items WHERE title='板子再度发布'")
    assert len(rows) == 1
    assert rows[0]["rejected"] == 1
    assert rows[0]["reject_gate"] == "hard"
    assert "重复" in rows[0]["reject_reason"]
    assert len(models.calls) == 1  # 没有打分调用


def test_dup_title_rejected(tmp_path) -> None:
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path,
        items=[_cand(0, title="小模型本地部署教程", url="https://y.com/1")],
        scores=_scores_json(_score(0)),
    )
    with _TimePatch():
        with store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                " VALUES (?, ?, 1, 1, 0, '', ?)",
                (GID, NOW - 5 * 86400, NOW - 5 * 86400),
            )
            conn.execute(
                "INSERT INTO news_items (batch_id, group_id, title, url_key, score, created)"
                " VALUES (?, ?, '小模型本地部署教程完整版', 'y.com/2', 1.0, ?)",
                (int(cur.lastrowid or 0), GID, NOW - 5 * 86400),
            )
        assert _run(feeds.prepare_news(GID)) == 0  # difflib >= 0.8
    row = _rows(store, "SELECT * FROM news_items WHERE url_key='y.com/1'")[0]
    assert row["rejected"] == 1 and row["reject_gate"] == "hard"
    assert "重复" in row["reject_reason"]


def test_blocked_domain_config_rejected(tmp_path) -> None:
    """[feeds] blocked_domains 命中（含子域 / www.）→ 第一道淘汰。"""
    cfg = {"feeds": {"blocked_domains": ["Spam.com"]}}
    items = [
        _cand(0, url="https://www.spam.com/x", title="配置屏蔽A"),
        _cand(1, url="https://m.spam.com/y", title="配置屏蔽B（子域）"),
        _cand(2, url="https://good.com/z", title="好的"),
    ]
    scores = _scores_json(_score(0, topic="唯一话题"))
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, items=items, scores=scores, cfg=cfg
    )
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 1
    rows = _rows(store)
    assert len(_accepted(rows)) == 1
    assert "屏蔽" in _rej_reason(rows, "www.spam.com")
    assert "屏蔽" in _rej_reason(rows, "m.spam.com")


def test_blocked_domain_web_rejected(tmp_path) -> None:
    """kv["feeds.blocked_domains"]（管理员在网页上维护的）也算数。"""
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path,
        items=[_cand(0, url="https://bad-web.com/a", title="网页屏蔽")],
        scores="{}",
    )
    with store.tx() as conn:
        store.kv_set(conn, "feeds.blocked_domains", ["bad-web.com"])
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 0
    row = _rows(store)[0]
    assert row["rejected"] == 1 and "屏蔽" in row["reject_reason"]


def test_auto_blocked_domain_rejected(tmp_path) -> None:
    """某域名被标「没用」净值（down-up）累计 >= 3 → 自动屏蔽。"""
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path,
        items=[_cand(0, url="https://noisy.com/a", title="自动屏蔽")],
        scores="{}",
    )
    with store.tx() as conn:
        for n in range(3):
            cur = conn.execute(
                "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                " VALUES (?, ?, 1, 1, 0, '', ?)",
                (GID, NOW - (n + 1) * 86400, NOW - (n + 1) * 86400),
            )
            conn.execute(
                "INSERT INTO news_items (batch_id, group_id, title, summary, sources, url_key,"
                " score, up, down, created)"
                " VALUES (?, ?, ?, '', ?, ?, 1.0, 0, 1, ?)",
                (
                    int(cur.lastrowid or 0), GID, f"历史{n}",
                    json.dumps([{"url": f"https://noisy.com/h{n}", "site": "noisy.com", "title": "t"}]),
                    f"noisy.com/h{n}", NOW - (n + 1) * 86400,
                ),
            )
    assert feeds._auto_blocked_domains(GID) == ["noisy.com"]
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 0
    row = _rows(store, "SELECT * FROM news_items WHERE title='自动屏蔽'")[0]
    assert row["rejected"] == 1
    assert "没用太多次" in row["reject_reason"]


def test_grounded_false_rejected(tmp_path) -> None:
    """模型判 grounded=false（摘要在原文找不到依据）→ 淘汰。"""
    items = [_cand(0, title="不扎实", url="https://e.com/1")]
    scores = _scores_json(_score(0, grounded=False))
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, items=items, scores=scores
    )
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 0
    row = _rows(store)[0]
    assert row["rejected"] == 1 and row["reject_gate"] == "hard"
    assert "依据" in row["reject_reason"]


def test_junk_rejected_with_reason(tmp_path) -> None:
    items = [_cand(0, title="震惊体", url="https://f.com/1")]
    scores = _scores_json(_score(0, junk=True, junk_reason="标题党，正文和标题对不上"))
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, items=items, scores=scores
    )
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 0
    row = _rows(store)[0]
    assert row["rejected"] == 1 and row["reject_gate"] == "hard"
    assert "垃圾" in row["reject_reason"] and "标题党" in row["reject_reason"]


def test_same_as_recent_only_when_scored(tmp_path) -> None:
    """同一件事（模型 same_as_recent=true，参考最近 14 天已出标题）→ 淘汰。"""
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path,
        items=[_cand(0, title="换个说法的同一件事", url="https://g.com/1")],
        scores=_scores_json(_score(0, same_as_recent=True)),
    )
    with _TimePatch():
        with store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                " VALUES (?, ?, 1, 1, 0, '', ?)",
                (GID, NOW - 2 * 86400, NOW - 2 * 86400),
            )
            conn.execute(
                "INSERT INTO news_items (batch_id, group_id, title, url_key, score, created)"
                " VALUES (?, ?, '某芯片发布', 'g.com/old', 1.0, ?)",
                (int(cur.lastrowid or 0), GID, NOW - 2 * 86400),
            )
        assert _run(feeds.prepare_news(GID)) == 0
    prompt = _score_prompt(models)
    assert "某芯片发布" in prompt  # 最近已出标题出现在打分参考里
    row = _rows(store, "SELECT * FROM news_items WHERE title='换个说法的同一件事'")[0]
    assert row["rejected"] == 1 and row["reject_gate"] == "hard"
    assert "重复" in row["reject_reason"] or "同一件事" in row["reject_reason"]


# ----------------------------------------------------------------------
# 第二道：打分门槛 / profile_ref / 去同质化
# ----------------------------------------------------------------------


def test_avg_below_web_min_rejected_gate_web(tmp_path) -> None:
    items = [_cand(0, title="分数不够", url="https://d.com/1")]
    scores = _scores_json(_score(0, info=2, source=3, relevance=3, timeliness=3, chat=2))  # avg 2.6
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, items=items, scores=scores
    )
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 0
    row = _rows(store)[0]
    assert row["rejected"] == 1 and row["reject_gate"] == "web"
    assert "3.0" in row["reject_reason"] or "平均" in row["reject_reason"]
    assert json.loads(row["scores"])["relevance"] == 3.0


def test_relevance_below_3_rejected_even_high_avg(tmp_path) -> None:
    items = [_cand(0, title="相关度低", url="https://d.com/2")]
    scores = _scores_json(_score(0, info=5, source=5, relevance=2, timeliness=5, chat=5))  # avg 4.4
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, items=items, scores=scores
    )
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 0
    row = _rows(store)[0]
    assert row["rejected"] == 1 and row["reject_gate"] == "web"
    assert "相关度" in row["reject_reason"]


def test_profile_ref_missing_caps_relevance_to_2(tmp_path) -> None:
    """profile 编号对不上画像条目 → relevance 封顶 2 → 过不了第二道（relevance>=3）。"""
    items = [_cand(0, title="对不上画像", url="https://d.com/3")]
    scores = _scores_json(_score(0, relevance=5, profile=97))  # 画像只有 2 条目（编号 0..1）
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, items=items, scores=scores
    )
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 0
    row = _rows(store)[0]
    data = json.loads(row["scores"])
    assert data["relevance"] == 2.0  # 封顶
    assert row["profile_ref"] == ""  # 没找到对应条目文字
    assert row["rejected"] == 1


def test_profile_ref_text_stored_when_valid(tmp_path) -> None:
    items = [_cand(0, title="对得上画像", url="https://d.com/4")]
    scores = _scores_json(_score(0, profile=1))  # 第 1 条画像（「本地大模型」）
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, items=items, scores=scores
    )
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 1
    row = _rows(store)[0]
    assert row["profile_ref"] == "本地大模型"
    data = json.loads(row["scores"])
    assert data["relevance"] == 4.0 and data["avg"] == pytest.approx(4.0)


def test_dedup_topic_cap_keeps_high_avg(tmp_path) -> None:
    """同一话题最多 2 条：三条同话题、avg 不同的，淘汰最低分那条（gate=web，同质化）。"""
    items = [_cand(i, title=f"同话题{i}", url=f"https://t{i}.com/x") for i in range(3)]
    scores = _scores_json(
        _score(0, info=4, topic="同一话题"),
        _score(1, info=5, topic="同一话题"),   # avg 4.4
        _score(2, info=3, topic="同一话题"),   # avg 3.4
    )
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, items=items, scores=scores
    )
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 2
    rows = _rows(store)
    assert len(rows) == 3
    dropped = _rejected(rows)
    assert len(dropped) == 1
    assert dropped[0]["title"] == "同话题2"
    assert dropped[0]["reject_gate"] == "web"
    assert "话题" in dropped[0]["reject_reason"]
    assert {r["title"] for r in _accepted(rows)} == {"同话题0", "同话题1"}


def test_dedup_domain_cap(tmp_path) -> None:
    """同一域名最多 3 条：5 条同域名留 avg 前 3，其余按「超出本轮上限」淘汰。"""
    items = [_cand(i, title=f"同域名{i}", url=f"https://site.com/p{i}") for i in range(5)]
    scores = _scores_json(
        _score(0, info=3, topic="dt0"),
        _score(1, info=4, topic="dt1"),
        _score(2, info=5, topic="dt2"),
        _score(3, info=4.4, topic="dt3"),
        _score(4, info=3.4, topic="dt4"),
    )
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, items=items, scores=scores
    )
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 3
    rows = _rows(store)
    dropped = _rejected(rows)
    assert {r["title"] for r in dropped} == {"同域名0", "同域名4"}
    assert all("同个来源" in r["reject_reason"] for r in dropped)


def test_dedup_sensitive_cap_one(tmp_path) -> None:
    """敏感话题每轮最多 1 条；多的按 avg 低者淘汰。"""
    items = [
        _cand(0, title="敏感A", url="https://s.com/a"),
        _cand(1, title="敏感B", url="https://s.com/b"),
        _cand(2, title="正常C", url="https://s.com/c"),
    ]
    scores = _scores_json(
        _score(0, info=3, topic="敏感话题1", sensitive=True),     # avg 3.4
        _score(1, info=5, topic="敏感话题2", sensitive=True),     # avg 4.4
        _score(2, topic="普通话题"),
    )
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, items=items, scores=scores
    )
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 2
    rows = _rows(store)
    dropped = _rejected(rows)
    assert len(dropped) == 1
    assert dropped[0]["title"] == "敏感A"
    assert "争议" in dropped[0]["reject_reason"] or "敏感" in dropped[0]["reject_reason"]
    assert dropped[0]["sensitive"] == 1


def test_dedup_max_items_cap(tmp_path) -> None:
    """总数最多 max_items：4 条全过线，配置 max_items=2 → 淘汰 avg 低的两条。"""
    cfg = {"feeds": {"max_items": 2}}
    items = [_cand(i, title=f"总上限{i}", url=f"https://m{i}.com/x") for i in range(4)]
    scores = _scores_json(
        _score(0, info=3, topic="mt0"),
        _score(1, info=4, topic="mt1"),
        _score(2, info=5, topic="mt2"),
        _score(3, info=3, topic="mt3"),
    )
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, items=items, scores=scores, cfg=cfg
    )
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 2
    rows = _rows(store)
    dropped = _rejected(rows)
    assert {r["title"] for r in dropped} == {"总上限0", "总上限3"}
    assert all("超出本轮上限" in r["reject_reason"] for r in dropped)


def test_happy_path_pool_gate_and_scores_stored(tmp_path) -> None:
    """正常通过后：rejected=0、scores JSON 带六项、topic/profile_ref/kind 落库。"""
    items = [
        _cand(0, url="https://example.com/board", title="新板子发布", published=NOW - 86400),
        _cand(1, url="https://tut.com/llm", title="本地部署好文", kind="guide", published=""),
    ]
    scores = _scores_json(
        _score(0, info=5, source=5, relevance=5, timeliness=5, chat=5, topic="FPGA", profile=0),
        _score(1, info=4, topic="部署", profile=1),
    )
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, items=items, scores=scores
    )
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
    assert got == 2
    rows = _rows(store)
    assert len(rows) == 2
    by_title = {r["title"]: r for r in rows}
    news_row = by_title["新板子发布"]
    assert news_row["rejected"] == 0
    assert news_row["kind"] == "news"
    assert news_row["topic"] == "FPGA"
    assert news_row["profile_ref"] == "在做开源硬件项目"
    assert news_row["sensitive"] == 0
    sc = json.loads(news_row["scores"])
    assert sc["info"] == 5.0 and sc["avg"] == pytest.approx(5.0)
    assert set(sc.keys()) == {"info", "source", "relevance", "timeliness", "chat", "avg"}
    guide_row = by_title["本地部署好文"]
    assert guide_row["kind"] == "guide"
    assert guide_row["rejected"] == 0


# ----------------------------------------------------------------------
# 第三道：进话题候选池
# ----------------------------------------------------------------------


def test_pool_gate_requires_high_scores_and_fresh_news(tmp_path) -> None:
    items = [
        _cand(0, title="高分新资讯", url="https://pa.com/1", published=NOW - 10 * 3600),
        _cand(1, title="分数中等", url="https://pb.com/2", published=NOW - 10 * 3600),
        _cand(2, title="相关低", url="https://pc.com/3", published=NOW - 10 * 3600),
        _cand(3, title="不值得聊", url="https://pd.com/4", published=NOW - 10 * 3600),
        _cand(4, title="两天前的", url="https://pe.com/5", published=NOW - 60 * 3600),
        _cand(5, title="没时间", url="https://pf.com/6", published=""),
        _cand(6, title="敏感高分", url="https://pg.com/7", published=NOW - 3600),
    ]
    scores = _scores_json(
        _score(0, info=5, topic="p0"),                                          # avg 4.4，全过 → 进池
        _score(1, info=4, source=4, relevance=4, timeliness=4, chat=4, topic="p1"),   # avg 4.0 → 进池
        _score(2, info=5, source=5, relevance=3, timeliness=5, chat=5, topic="p2"),   # relevance 3 → 不进
        _score(3, info=5, source=5, relevance=5, timeliness=5, chat=2, topic="p3"),   # chat 2 → 不进
        _score(4, info=5, topic="p4"),                                          # 超过 48 小时 → 不进
        _score(5, info=5, topic="p5"),                                          # 没 published → 不进
        _score(6, info=5, topic="p6", sensitive=True),                          # 敏感 → 不进
    )
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, items=items, scores=scores
    )
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
    assert got == len(items)
    pool_titles = {c["title"] for c in topics.calls if c["kind"] == "news"}
    assert pool_titles == {"高分新资讯", "分数中等"}
    assert all(c["group_id"] == GID for c in topics.calls)


def test_sensitive_still_on_web_but_not_pool_and_view(tmp_path) -> None:
    """敏感资讯能上网页（新闻栏里看得到），但不进候选池。"""
    items = [_cand(0, title="敏感但分高", url="https://sens.com/x")]
    scores = _scores_json(_score(0, info=5, topic="争议话题", sensitive=True))
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, items=items, scores=scores
    )
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 1
        news = feeds.news_view(GID, admin=True)
    assert news[0]["items"][0]["title"] == "敏感但分高"  # 上网页了
    assert news[0]["items"][0]["sensitive"] is True
    assert topics.calls == []  # 但不进池


def test_guide_never_enters_pool(tmp_path) -> None:
    """好文（kind=guide）哪怕全 5 分也不进话题候选池。"""
    items = [_cand(0, kind="guide", title="神级教程", url="https://g.com/tut", published=NOW - 3600)]
    scores = _scores_json(_score(0, info=5, source=5, relevance=5, timeliness=5, chat=5, topic="教程"))
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, items=items, scores=scores
    )
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 1
    assert topics.calls == []


# ----------------------------------------------------------------------
# 视图：news（只过线 news）、guides、rejected 只管理员
# ----------------------------------------------------------------------


def _seed_item(
    store: Store,
    batch_id: int,
    gid: str,
    *,
    title: str,
    kind: str = "news",
    avg: float = 4.0,
    topic: str = "话题",
    sensitive: int = 0,
    profile_ref: str = "",
    rejected: int = 0,
    reject_gate: str | None = None,
    reject_reason: str | None = None,
    url: str = "https://x.com/a",
    created: float,
) -> int:
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_items (batch_id, group_id, icon, title, summary, why, sources,"
            " url_key, published_ts, score, status_kind, replies, expires_ts, up, down, created,"
            " kind, scores, topic, sensitive, profile_ref, rejected, reject_gate, reject_reason)"
            " VALUES (?, ?, 'robot', ?, '摘要', '原因', ?, ?, NULL, ?, 'pool', 0, ?, 0, 0, ?,"
            " ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                batch_id, gid, title,
                json.dumps([{"url": url, "site": "x.com", "title": title}], ensure_ascii=False),
                url, avg, created + 12 * 3600, created,
                kind,
                json.dumps(
                    {"info": avg, "source": avg, "relevance": avg, "timeliness": avg, "chat": avg, "avg": avg}
                ),
                topic, sensitive, profile_ref, rejected, reject_gate, reject_reason,
            ),
        )
        return int(cur.lastrowid or 0)


def _seed_batch(store: Store, gid: str, *, created: float) -> int:
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
            " VALUES (?, ?, 3, 2, 0, '', ?)",
            (gid, created, created),
        )
        return int(cur.lastrowid or 0)


def test_news_view_only_accepted_news_with_new_fields(tmp_path) -> None:
    _, _, feeds, *_ = _make_feeds(tmp_path)
    store = feeds._store
    with _TimePatch():
        bid = _seed_batch(store, GID, created=NOW - 100)
        _seed_item(store, bid, GID, title="通过的资讯", kind="news", created=NOW - 100)
        _seed_item(store, bid, GID, title="通过的好文", kind="guide", created=NOW - 100)
        _seed_item(store, bid, GID, title="被筛掉的", kind="news", rejected=1,
                   reject_gate="hard", reject_reason="垃圾：标题党", avg=2.0, created=NOW - 100)
        member_view = feeds.news_view(GID, admin=False)
        admin_view = feeds.news_view(GID, admin=True)
    # 群友版：news 只有通过的资讯（好文在 guides、被筛的不出现），没有 rejected 字段
    assert len(member_view) == 1
    batch = member_view[0]
    assert [i["title"] for i in batch["items"]] == ["通过的资讯"]
    item = batch["items"][0]
    assert item["kind"] == "news"
    assert set(item["scores"].keys()) == {"info", "source", "relevance", "timeliness", "chat", "avg"}
    assert item["topic"] == "话题"
    assert item["sensitive"] is False
    assert "profile_ref" in item
    assert "rejected" not in batch
    assert batch["rejected_count"] == 1
    # 管理员版：多 rejected 栏
    admin_batch = admin_view[0]
    assert admin_batch["rejected_count"] == 1
    rejected = admin_batch["rejected"]
    assert len(rejected) == 1
    r0 = rejected[0]
    assert set(r0.keys()) == {"id", "title", "url", "site", "gate", "reason", "avg"}
    assert r0["title"] == "被筛掉的"
    assert r0["gate"] == "hard"
    assert r0["reason"] == "垃圾：标题党"
    assert r0["url"] == "https://x.com/a" and r0["site"] == "x.com"


def test_guides_view_separate_max20_last30days(tmp_path) -> None:
    _, _, feeds, *_ = _make_feeds(tmp_path)
    store = feeds._store
    with _TimePatch():
        bid = _seed_batch(store, GID, created=NOW - 1000)
        _seed_item(store, bid, GID, title="新好文", kind="guide", created=NOW - 1000)
        _seed_item(store, bid, GID, title="旧好文（超过30天）", kind="guide", created=NOW - 31 * 86400)
        _seed_item(store, bid, GID, title="被筛的好文", kind="guide", rejected=1,
                   reject_gate="web", reject_reason="平均 2.0 < 3.0", created=NOW - 1000)
        _seed_item(store, bid, GID, title="通过的资讯", kind="news", created=NOW - 1000)
        guides = feeds.guides_view(GID)
    assert [g["title"] for g in guides] == ["新好文"]
    g0 = guides[0]
    assert g0["kind"] == "guide"
    assert set(g0["scores"].keys()) == {"info", "source", "relevance", "timeliness", "chat", "avg"}
    assert g0["topic"] == "话题"
    # 超 20 条封顶（新到旧）
    bid2 = _seed_batch(store, GID, created=NOW)
    for i in range(25):
        _seed_item(store, bid2, GID, title=f"第{i}篇", kind="guide",
                   url=f"https://g.com/{i}", created=NOW - i * 60)
    with _TimePatch():
        assert len(feeds.guides_view(GID)) == 20


# ----------------------------------------------------------------------
# 批次 / 失败路径
# ----------------------------------------------------------------------


def test_all_hard_rejected_still_counts_as_skipped_batch(tmp_path) -> None:
    """全被第一道筛掉也记批次（skipped=1，found>0，kept=0）。"""
    items = [_cand(0, fetched=False, title="全灭", url="https://z.com/x")]
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, items=items, scores="{}"
    )
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 0
    batch = store.read().execute("SELECT * FROM news_batches ORDER BY id DESC LIMIT 1").fetchone()
    assert batch["skipped"] == 1
    assert batch["found"] == 1
    assert batch["kept"] == 0
    assert "值得" in batch["note"] or "没有" in batch["note"]
    assert len(_rows(store)) == 1  # 被筛的照样入库


# ----------------------------------------------------------------------
# 域名规范化（feeds + console 共用的 _normalize_domain）
# ----------------------------------------------------------------------


def test_normalize_domain_canonical() -> None:
    assert _normalize_domain("Spam.com") == "spam.com"
    assert _normalize_domain("www.spam.com") == "spam.com"
    assert _normalize_domain("  WwW.EXAMPLE.COM  ") == "example.com"
    assert _normalize_domain("sub.example.com") == "sub.example.com"
    # 非法字符 / 明显不是域名 → 空串
    assert _normalize_domain("not a domain") == ""
    assert _normalize_domain("exa mple.com") == ""
    assert _normalize_domain("") == ""
    assert _normalize_domain("https://x.com/path") == ""


def test_auto_blocked_domains_uses_net_down_and_recent_window(tmp_path) -> None:
    """净值 = down - up；只算近 90 天；历史老账不算。"""
    store, settings, feeds, *_ = _make_feeds(tmp_path)
    bid = _seed_batch(store, GID, created=NOW - 5 * 86400)
    with store.tx() as conn:
        for n, up, down in ((0, 0, 2), (1, 0, 2)):
            conn.execute(
                "INSERT INTO news_items (batch_id, group_id, title, summary, sources, url_key,"
                " score, up, down, created) VALUES (?, ?, ?, '', ?, ?, 1.0, ?, ?, ?)",
                (
                    bid, GID, f"h{n}",
                    json.dumps([{"url": f"https://noisy.cOM/h{n}", "site": "noisy.com", "title": "t"}]),
                    f"noisy.com/h{n}", up, down, NOW - 5 * 86400,
                ),
            )
    assert feeds._auto_blocked_domains(GID) == ["noisy.com"]
    # 90 天外的不算
    with store.tx() as conn:
        conn.execute("UPDATE news_items SET created=? WHERE group_id=?", (NOW - 100 * 86400, GID))
    assert feeds._auto_blocked_domains(GID) == []


# ----------------------------------------------------------------------
# 管理员接口：/api/feeds/domains、/api/settings.feeds
# ----------------------------------------------------------------------


def _raw_config(data_dir: Path, **over) -> dict:
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}]},
        "console": {"listen": f"127.0.0.1:{_port()}", "password": PASSWORD, "public_url": ""},
        "models": {"base_url": "https://ep.test/v1", "api_key": SECRET, "main": "main-m", "worker": "worker-m"},
        "storage": {"data_dir": str(data_dir)},
        "approval": {"required": True, "admins": ["10001"]},
    }
    for section, values in over.items():
        raw.setdefault(section, {}).update(values)
    return raw


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    raw = _raw_config(tmp_path / "data", feeds={"blocked_domains": ["cfg-bad.com"]})
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


def _seed_feedback_rows(app: MaiWorkApp) -> None:
    """给 G1 塞几条最近的 news_items：noisy.com 净值 down-up = 3 → 进 auto_blocked。"""
    now = clock.now()
    with app.store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
            " VALUES (?, ?, 3, 3, 0, '', ?)",
            (G1, now - 86400, now - 86400),
        )
        bid = int(cur.lastrowid or 0)
        for n in range(3):
            conn.execute(
                "INSERT INTO news_items (batch_id, group_id, title, summary, sources, url_key,"
                " score, up, down, created, kind, scores, topic, sensitive, profile_ref, rejected)"
                " VALUES (?, ?, ?, '', ?, ?, 1.0, 0, 1, ?, 'news', '{}', '', 0, '', 0)",
                (
                    bid, G1, f"反馈{n}",
                    json.dumps([{"url": f"https://noisy.com/x{n}", "site": "noisy.com", "title": "t"}]),
                    f"noisy.com/x{n}", now - 5 * 86400,
                ),
            )


@pytest.mark.asyncio
async def test_settings_feeds_shape(env) -> None:
    await env.client.post("/api/login", json={"password": PASSWORD})
    _seed_feedback_rows(env.app)
    r = await env.client.get("/api/settings")
    assert r.status == 200
    data = await r.json()
    feeds = data["feeds"]
    # 2026-09 追加：feeds 段加 rss（{群号: [{id,url,title,enabled,added_ts,last_ok_ts,last_error}]}）
    assert set(feeds.keys()) == {"blocked_domains", "auto_blocked", "rss"}
    assert feeds["rss"] == {G1: []}
    assert feeds["blocked_domains"] == ["cfg-bad.com"]
    assert feeds["auto_blocked"] == ["noisy.com"]


@pytest.mark.asyncio
async def test_feeds_domains_admin_block_unblock(env) -> None:
    await env.client.post("/api/login", json={"password": PASSWORD})
    # 屏蔽（大小写 / www 会被规范化）
    r = await env.client.post("/api/feeds/domains", json={"domain": "WWW.Bad-Example.COM", "blocked": True})
    assert r.status == 200
    data = await r.json()
    assert data["blocked_domains"] == ["bad-example.com", "cfg-bad.com"]
    # 再来一个
    r = await env.client.post("/api/feeds/domains", json={"domain": "other.org", "blocked": True})
    assert (await r.json())["blocked_domains"] == ["bad-example.com", "cfg-bad.com", "other.org"]
    # 解除（配置里的也能从合并列表里拿掉：kv 覆盖整份列表）
    r = await env.client.post("/api/feeds/domains", json={"domain": "cfg-bad.com", "blocked": False})
    data = await r.json()
    assert data["blocked_domains"] == ["bad-example.com", "other.org"]
    # settings 里也同步
    r = await env.client.get("/api/settings")
    feeds = (await r.json())["feeds"]
    assert feeds["blocked_domains"] == ["bad-example.com", "other.org"]


@pytest.mark.asyncio
async def test_feeds_domains_invalid_domain_400(env) -> None:
    await env.client.post("/api/login", json={"password": PASSWORD})
    for bad in ("", "not a domain", "exa mple.com", "a..com", "-bad-.com", "x_com"):
        r = await env.client.post("/api/feeds/domains", json={"domain": bad, "blocked": True})
        assert r.status == 400, (bad, r.status)
        body = await r.json()
        assert body["error"]


@pytest.mark.asyncio
async def test_feeds_domains_member_403_anonymous_401(env) -> None:
    token = env.app.token_of(G1)
    # 群友 → 403
    r = await env.client.post(
        "/api/feeds/domains", json={"domain": "evil.com", "blocked": True},
        headers={"X-MW-Group": token},
    )
    assert r.status == 403
    # 匿名 → 401
    r = await env.client.post("/api/feeds/domains", json={"domain": "evil.com", "blocked": True})
    assert r.status == 401


# ----------------------------------------------------------------------
# config 0.3.4：默认值 & min_score 提示
# ----------------------------------------------------------------------


def test_config_defaults_quality_settings() -> None:
    settings, problems = load_settings({})
    assert settings.feeds.max_items == 10
    assert tuple(settings.feeds.blocked_domains) == ()
    assert settings.feeds.guides is True
    assert settings.feeds.web_min_avg == pytest.approx(3.0)
    assert settings.feeds.pool_min_avg == pytest.approx(4.0)
    assert tuple(int(x) for x in CONFIG_VERSION.split(".")) >= (0, 3, 9)  # 0.3.5 起含 railway 实测配置；0.3.8 起含关注成员个人向产出；0.3.9 起含模型重试设置
    # min_score 还在（兼容）；显式写了 → 问题清单提示不再用它
    assert hasattr(settings.feeds, "min_score")
    _, problems2 = load_settings({"feeds": {"min_score": 0.8}})
    assert any("min_score" in p and "不" in p for p in problems2)


def test_config_blocked_domains_normalized() -> None:
    settings, _problems = load_settings({"feeds": {"blocked_domains": ["Spam.com", "WWW.evil.org"]}})
    assert set(settings.feeds.blocked_domains) == {"spam.com", "evil.org"}


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
