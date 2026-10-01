"""feeds.py 单元测试（docs/07-代码接口.md §10.4、§9.3）。

（2026-09-27 质量标准落地后按新语义更新：候选带 kind/fetched/quote/paywall；
打分带 info/source/relevance/timeliness/chat 五项 1–5 分 + profile/topic/sensitive +
grounded/junk/same_as_recent；被筛掉的也入库 rejected=1。
「质量标准」本身的逐条覆盖在 tests/test_feeds_quality.py，本文件保老场景的运行习惯：
全流程、skipped 批次、去重顺序、打分对不上兜底、视图结构、反馈和构想。）

- prepare_news：正常入选全流程；全不过门槛 → skipped 批次；搜索没配 → skipped（不抛）；
  画像没成形 → 0 且不写批次；模型没配好 → 0 且不写批次；url 规范化去重、标题去重、
  批内去重；why/icon/topic/profile_ref 来自模型打分；达标的入选进话题候选池；
  子 agent 失败 → skipped。
- make_idea：正常入库 + chat_worthy 进候选池；重复返回 None；chat_worthy=False 不进池。
- feedback：加减计数、不为负、prev 切换、id 不存在 KeyError。
- idea_action：状态机 new/wanted/dismissed/started；非法操作 ValueError(中文)。
- news_view / guides_view / ideas_view：字段和 §9.3 一致（news 只含过线的 kind=news 条目，
  每条带 kind/scores/topic/sensitive/profile_ref；rejected 一栏只给管理员）；
  pool 过期显示 expired；dismissed 3 天后不显示。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.feeds import Feeds, _normalize_url
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

BJ = timezone(timedelta(hours=8))
NOW = 1_790_000_000.0  # 测试里的「现在」
GID = "111"
GID_OTHER = "222"


def _bj_ts(y: int, m: int, d: int, hh: int, mm: int = 0) -> float:
    return datetime(y, m, d, hh, mm, tzinfo=BJ).timestamp()


def _settings(cfg: dict | None = None) -> Any:
    settings, _ = load_settings(cfg or {})
    return settings


def _seed_group(store: Store, gid: str = GID, *, ready: bool = True) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, profile_ready_ts) VALUES (?, ?)",
            (gid, 1_700_000_000.0 if ready else 0.0),
        )


class FakeSearch:
    """可用的假搜索（只用来表示「配好了」）。"""

    def __init__(self) -> None:
        self.calls: List[str] = []

    async def search(self, query: str, **kw: Any) -> list:
        self.calls.append(query)
        return []


class UnavailableSearch:
    """没配好的假搜索：search() 抛 SearchUnavailable（和 search.py 行为一致）。"""

    async def search(self, query: str, **kw: Any) -> list:
        from CharTyr_MaiWork.maiwork.search import SearchUnavailable

        raise SearchUnavailable("没配搜索服务，本次跳过联网搜索")


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


def _ok_report(data: dict) -> Any:
    from CharTyr_MaiWork.maiwork.workers import WorkerReport

    return WorkerReport(ok=True, summary="找好了", data=data, evidence=[], steps=3)


class FakeTopics:
    """只记 add_candidate 的假 Topics。"""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def add_candidate(self, group_id: str, **kw: Any) -> None:
        self.calls.append({"group_id": group_id, **kw})


# --
# 常用的模型回复素材
# --

# 定关注点要求 3–5 个（少了会触发追问重试），统一用 fakes.focus_reply 造
_FOCUS_JSON = focus_reply("FPGA 新动态", "开源项目", "本地大模型新玩法")

_QUOTE = "原文里确实写着这件事，摘要能在正文找到依据。"

_WORKER_ITEMS = {
    # 质量标准版候选：kind/fetched/quote/paywall 必填（见 docs/02 §4.1 第一道）
    "items": [
        {"title": "新开源 FPGA 开发板发布", "url": "https://Example.COM/board/?utm_source=x#frag",
         "summary": "一块新板子。配置不错。社区关注高。", "kind": "news",
         "published": NOW - 86400, "fetched": True, "quote": _QUOTE, "paywall": False},
        {"title": "小模型本地部署教程", "url": "https://news.com/llm-deploy",
         "summary": "介绍怎么在小机器上跑模型。步骤清楚。", "kind": "news",
         "published": NOW - 2 * 86400, "fetched": True, "quote": _QUOTE, "paywall": False},
        {"title": "吃桃子的十种方法", "url": "https://food.com/peach",
         "summary": "生活小窍门。", "kind": "news",
         "published": NOW - 86400, "fetched": True, "quote": _QUOTE, "paywall": False},
    ]
}

_SCORES_JSON = json.dumps(
    # 质量标准版打分：五项 1–5 + profile 编号 + topic + 三个第一道判断
    {
        "scores": [
            {"i": 0, "title": "新开源 FPGA 开发板发布", "info": 5, "source": 4, "relevance": 5,
             "timeliness": 4, "chat": 4, "profile": 0, "topic": "FPGA", "sensitive": False,
             "grounded": True, "junk": False, "junk_reason": "", "same_as_recent": False,
             "why": "群里的硬件项目正好用得上", "icon": "tools"},
            {"i": 1, "title": "小模型本地部署教程", "info": 4, "source": 5, "relevance": 4,
             "timeliness": 4, "chat": 4, "profile": 1, "topic": "本地模型", "sensitive": False,
             "grounded": True, "junk": False, "junk_reason": "", "same_as_recent": False,
             "why": "大家最近想自己跑模型", "icon": "robot"},
            {"i": 2, "title": "吃桃子的十种方法", "info": 4, "source": 4, "relevance": 1,
             "timeliness": 4, "chat": 1, "profile": 0, "topic": "生活", "sensitive": False,
             "grounded": True, "junk": False, "junk_reason": "", "same_as_recent": False,
             "why": "和群没啥关系", "icon": "newspaper"},
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
    cfg: dict | None = None,
    seed: bool = True,
    ready: bool = True,
) -> tuple:
    store = Store(tmp_path / "t.db")
    store.migrate()
    if seed:
        _seed_group(store, GID, ready=ready)
    settings = _settings(cfg)
    if models is None:
        models = FakeModelsQueue(ready=True)
    ensure_pick_fallback(models)
    if workers is None:
        workers = FakeWorkers(_ok_report(_WORKER_ITEMS))
    if topics is None:
        topics = FakeTopics()
    profiles = FakeProfiles()
    profiles.entries_map[GID] = [
        {"category": "ongoing", "text": "在做开源硬件项目"},
        {"category": "interest", "text": "本地大模型"},
    ]
    feeds = Feeds(store, models, workers, profiles, topics, lambda: settings, search=search)
    if search is None and workers is not None:
        # 2026-10-01 撒网改代码按计划搜：把 FakeWorkers 预置的 items 挪给假搜索出
        # （skip / 出错类用例会显式传 search / workers=Exception，走不到这里）；
        # 懒取 report——有用例在 _feeds(_make_feeds) 之后才换 report
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


# ----------------------------------------------------------------------
# url 规范化（纯函数）
# ----------------------------------------------------------------------


def test_normalize_url_strips_tracking_and_case() -> None:
    assert _normalize_url("HTTPS://Example.COM/A/?utm_source=x&b=2#frag") == "example.com/A?b=2"
    assert _normalize_url("https://Example.com/a/") == _normalize_url("http://example.com/a")
    assert _normalize_url("https://a.com/x?fbclid=abc&keep=1") == "a.com/x?keep=1"
    assert _normalize_url("") == ""
    assert _normalize_url("不是链接") == ""


# ----------------------------------------------------------------------
# prepare_news
# ----------------------------------------------------------------------


def test_prepare_news_not_ready_returns_0_and_records_skipped(tmp_path) -> None:
    """画像没成形：返回 0、不调模型 / 子 agent；但要留一条 skipped 批次说明原因（A11 不静默）。"""
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path, ready=False)
    got = _run(feeds.prepare_news(GID))
    assert got == 0
    assert models.calls == []
    assert workers.calls == []
    row = store.read().execute("SELECT * FROM news_batches").fetchone()
    assert row is not None, "画像没成形也要写一条 skipped 批次（A11：不静默退出）"
    assert row["skipped"] == 1
    assert "画像" in row["note"] or "熟悉" in row["note"], row["note"]


def test_prepare_news_not_ready_unknown_group(tmp_path) -> None:
    """库里没有这个群也按没成形处理：返回 0，并留一条 skipped 批次说明画像没成形。"""
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path, seed=False)
    assert _run(feeds.prepare_news("999")) == 0
    row = store.read().execute("SELECT * FROM news_batches").fetchone()
    assert row is not None and row["skipped"] == 1
    assert "画像" in row["note"] or "熟悉" in row["note"], row["note"]


def test_prepare_news_models_not_ready(tmp_path) -> None:
    """模型没配好：返回 0、不派子 agent；也留一条 skipped 批次说清是模型没配。"""
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, models=FakeModelsQueue(ready=False)
    )
    assert _run(feeds.prepare_news(GID)) == 0
    assert workers.calls == []
    row = store.read().execute("SELECT * FROM news_batches").fetchone()
    assert row is not None and row["skipped"] == 1
    assert "模型" in row["note"], row["note"]


def test_prepare_news_search_unavailable_marks_skipped(tmp_path) -> None:
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, search=UnavailableSearch()
    )
    got = _run(feeds.prepare_news(GID))
    assert got == 0
    assert workers.calls == []  # 不派子 agent
    row = store.read().execute("SELECT * FROM news_batches").fetchone()
    assert row is not None
    assert row["skipped"] == 1
    assert "搜索" in row["note"]
    assert row["kept"] == 0


def test_prepare_news_happy_path(tmp_path) -> None:
    """正常入选全流程（质量标准版）：过线的上网页+进库；达标的进话题候选池；
    不过线的 entered 库（rejected=1，gate='web'），同样能看到卡在哪一道。"""
    models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, _SCORES_JSON])
    store, settings, feeds, models, workers, topics, profiles = _make_feeds(
        tmp_path, models=models
    )
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
    assert got == 2  # 第三条 avg=(4+4+1+4+1)/5=2.8<3 且 relevance=1<3，第二道筛掉

    batch = store.read().execute("SELECT * FROM news_batches").fetchone()
    assert batch["found"] == 3
    assert batch["kept"] == 2
    assert batch["skipped"] == 0
    assert batch["slot_ts"] > NOW - 100  # 大致是「现在」
    assert batch["created"] > NOW - 100

    items = store.read().execute(
        "SELECT * FROM news_items WHERE batch_id=? ORDER BY id", (batch["id"],)
    ).fetchall()
    assert len(items) == 3  # 被筛掉的也入库
    accepted = [r for r in items if not r["rejected"]]
    rejected = [r for r in items if r["rejected"]]
    assert len(accepted) == 2 and len(rejected) == 1
    assert rejected[0]["reject_gate"] == "web"
    assert rejected[0]["reject_reason"]
    rejected_scores = json.loads(rejected[0]["scores"])
    assert set(rejected_scores.keys()) == {"info", "source", "relevance", "timeliness", "chat", "avg"}

    first = accepted[0]
    # why / icon 来自模型打分，不是子 agent 的原话
    assert first["why"] == "群里的硬件项目正好用得上"
    assert first["icon"] == "tools"
    # 质量标准的入库字段
    assert first["kind"] == "news"
    scores = json.loads(first["scores"])
    assert scores["relevance"] == 5.0 and scores["avg"] == pytest.approx(4.4)
    assert first["topic"] == "FPGA"
    assert first["profile_ref"] == "在做开源硬件项目"  # profile=0 换成了画像条目文字
    assert first["sensitive"] == 0
    # url 已规范化：小写 host、去 utm、去尾 /、去 #
    assert first["url_key"] == "example.com/board"
    src = json.loads(first["sources"])
    assert src[0]["site"] == "example.com"  # host 去 www. 小写
    assert src[0]["url"].startswith("http")
    assert first["status_kind"] == "pool"
    assert first["status_at"] is None
    assert first["expires_ts"] > NOW + 11 * 3600  # 默认候选 ttl 12 小时
    assert first["published_ts"] == pytest.approx(NOW - 86400)

    # 达标的进话题候选池（avg≥4、relevance≥4、chat≥4、48h 内、非敏感——两条都踩线达标）
    assert len(topics.calls) == 2
    kinds = {(c["group_id"], c["kind"]) for c in topics.calls}
    assert kinds == {(GID, "news")}
    assert all(len(c["brief"]) <= 120 for c in topics.calls)
    assert {c["ref_id"] for c in topics.calls} == {int(it["id"]) for it in accepted}

    # 主模型的两次调用都是 json_mode
    assert models.calls[0][2].get("json_mode") is True
    assert models.calls[1][2].get("json_mode") is True


def test_prepare_news_all_below_threshold_marks_skipped(tmp_path) -> None:
    """全不过第二道门槛：skipped 批次；被筛的照样入库（rejected=1, gate='web'）。"""
    low_scores = json.dumps(
        {"scores": [{"i": i, "info": 2, "source": 2, "relevance": 2, "timeliness": 1, "chat": 1,
                     "profile": 0, "topic": f"低{i}", "sensitive": False, "grounded": True,
                     "junk": False, "junk_reason": "", "same_as_recent": False,
                     "why": "不搭", "icon": "link"}
                    for i in range(3)]},
        ensure_ascii=False,
    )
    models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, low_scores])
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path, models=models)
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
        assert got == 0
        row = store.read().execute("SELECT * FROM news_batches").fetchone()
        assert row["skipped"] == 1
        assert row["found"] == 3
        assert row["kept"] == 0
        assert "门槛" in row["note"] or "没有" in row["note"]
        rows = store.read().execute("SELECT * FROM news_items").fetchall()
        assert len(rows) == 3  # 质量标准：被筛掉的也入库
        assert all(r["rejected"] == 1 and r["reject_gate"] == "web" for r in rows)
        assert topics.calls == []


def test_prepare_news_max_items_cap(tmp_path) -> None:
    """总数上限（去同质化之一）：全过线也只能留 max_items，avg 高者留。"""
    high_scores = json.dumps(
        {"scores": [{"i": i, "info": 5 - i * 0.1, "source": 4, "relevance": 4, "timeliness": 4,
                     "chat": 4, "profile": 0, "topic": f"高分{i}", "sensitive": False,
                     "grounded": True, "junk": False, "junk_reason": "", "same_as_recent": False,
                     "why": f"合适{i}", "icon": "robot"}
                    for i in range(3)]},
        ensure_ascii=False,
    )
    models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, high_scores])
    cfg = {"feeds": {"max_items": 2}}
    store, settings, feeds, *_ = _make_feeds(tmp_path, models=models, cfg=cfg)
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
        assert got == 2  # 3 条都过线但最多留 2 条（avg 高的留）
        rows = store.read().execute("SELECT * FROM news_items ORDER BY id").fetchall()
        accepted_scores = [r["score"] for r in rows if not r["rejected"]]
        assert accepted_scores == sorted(accepted_scores, reverse=True)
        dropped = [r for r in rows if r["rejected"]]
        assert len(dropped) == 1 and dropped[0]["reject_gate"] == "web"
        assert "超出本轮上限" in dropped[0]["reject_reason"]


def test_prepare_news_worker_fails_marks_skipped(tmp_path) -> None:
    from CharTyr_MaiWork.maiwork.workers import WorkerReport

    models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON])
    workers = FakeWorkers(WorkerReport(ok=False, summary="", data=None, evidence=[], steps=2, error="步数用完"))
    store, settings, feeds, m, w, t, p = _make_feeds(tmp_path, models=models, workers=workers)
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
        assert got == 0
        row = store.read().execute("SELECT * FROM news_batches").fetchone()
        assert row["skipped"] == 1
        assert "子" in row["note"] or "agent" in row["note"].lower()


def test_prepare_news_focus_model_bad_json_marks_skipped(tmp_path) -> None:
    models = FakeModelsQueue(ready=True, replies=["这不是 JSON"])
    store, settings, feeds, m, w, t, p = _make_feeds(tmp_path, models=models)
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
        assert got == 0
        row = store.read().execute("SELECT * FROM news_batches").fetchone()
        assert row["skipped"] == 1
        assert row["note"] != ""


def test_prepare_news_worker_returns_no_items_marks_skipped(tmp_path) -> None:
    models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON])
    workers = FakeWorkers(_ok_report({"items": []}))
    store, settings, feeds, m, w, t, p = _make_feeds(tmp_path, models=models, workers=workers)
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 0
        row = store.read().execute("SELECT * FROM news_batches").fetchone()
        assert row["skipped"] == 1
        # 两阶段恒生效：撒网 OK 但一条都没搜出 → 「撒网没搜出能用的候选」
        assert "撒网" in row["note"] or "没找到" in row["note"] or "没有" in row["note"]

    # items 不是列表也算失败
    store2 = Store(tmp_path / "t2.db")
    store2.migrate()
    _seed_group(store2, GID, ready=True)
    models2 = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON])
    workers2 = FakeWorkers(_ok_report("不是字典"))
    settings = _settings()
    feeds2 = Feeds(store2, models2, workers2, FakeProfiles(), FakeTopics(), lambda: settings, search=None)
    try:
        with _TimePatch():
            assert _run(feeds2.prepare_news(GID)) == 0
            row2 = store2.read().execute("SELECT * FROM news_batches").fetchone()
            assert row2["skipped"] == 1
    finally:
        store2.close()


# --
# 去重
# --


def _seed_news_item(store: Store, gid: str, *, title: str, url_key: str, created: float) -> int:
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
            " VALUES (?, ?, 1, 1, 0, '', ?)",
            (gid, created, created),
        )
        batch_id = int(cur.lastrowid or 0)
        cur = conn.execute(
            "INSERT INTO news_items (batch_id, group_id, icon, title, summary, why, sources,"
            " url_key, published_ts, score, status_kind, status_at, replies, expires_ts, up, down, created)"
            " VALUES (?, ?, 'robot', ?, 's', 'w', '[]', ?, NULL, 0.9, 'pool', NULL, 0, NULL, 0, 0, ?)",
            (batch_id, gid, title, url_key, created),
        )
        return int(cur.lastrowid or 0)


def test_prepare_news_dedup_url_and_title(tmp_path) -> None:
    """库里 5 天前已出的 url（规范化后相同）和近似标题都要被第一道筛掉（入库 rejected=1）。"""
    models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, _SCORES_JSON])
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path, models=models)
    with _TimePatch():
        _seed_news_item(store, GID, title="新开源 FPGA 开发板发布了！", url_key="example.com/board", created=NOW - 5 * 86400)
        _seed_news_item(store, GID, title="小模型本地部署教程完整评测", url_key="other.com/x", created=NOW - 5 * 86400)
        got = _run(feeds.prepare_news(GID))
        # 三条候选：第 0 条 URL 撞、第 1 条标题撞（difflib ≥ 0.8）、第 2 条第三道分数不过
        assert got == 0
        batch = store.read().execute(
            "SELECT * FROM news_batches ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert batch["skipped"] == 1
        # 两阶段恒生效：撞存量 URL 的候选在撒网登记簿之后的预筛就丢了（只记漏斗理由）；
        # 标题近似的过了预筛（0.85）但被第一道（0.8）拦下入库行；分不过的在打分后入库。
        rows = store.read().execute(
            "SELECT * FROM news_items WHERE batch_id=?", (batch["id"],)
        ).fetchall()
        assert len(rows) == 2, [dict(r) for r in rows]
        assert all(r["rejected"] == 1 for r in rows)
        gates = sorted(r["reject_gate"] for r in rows)
        assert gates == ["hard", "web"]
        hard = next(r for r in rows if r["reject_gate"] == "hard")
        assert "重复" in hard["reject_reason"]
        # 撞存量链接的那条连打分都没到 → 只在漏斗里
        stats = store.kv_get(f"feeds.batch_stats.{batch['id']}") or {}
        rejects = (stats.get("funnel") or {}).get("rejects") or {}
        assert any("重复" in k for k in rejects), f"漏斗应见撞链接的理由：{rejects}"
        # 撞存量链接的那条根本没进分数映射：分数字典里找不到它的标题
        assert all(
            "新开源 FPGA 开发板发布" not in str(r["scores"]) for r in rows
        )


def test_prepare_news_dedup_other_group_not_affected(tmp_path) -> None:
    """去重只看本群；别的群出过同 url 不影响。"""
    models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, _SCORES_JSON])
    store, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    _seed_group(store, GID_OTHER, ready=True)
    with _TimePatch():
        _seed_news_item(store, GID_OTHER, title="上一个群的板子", url_key="example.com/board", created=NOW - 86400)
        got = _run(feeds.prepare_news(GID))
        assert got == 2


def test_prepare_news_dedup_within_batch(tmp_path) -> None:
    """批内两条规范化后同 url 的候选只留一条（后来的那条第一道筛掉）。"""
    dup_items = {
        "items": [
            {"title": "板子发布新闻稿", "url": "https://Example.com/board/", "summary": "通稿。",
             "kind": "news", "published": "", "fetched": True, "quote": "新闻稿原文依据。", "paywall": False},
            {"title": "板子上手体验", "url": "https://example.com/board?utm_medium=a", "summary": "体验文。和新闻稿不同角度。",
             "kind": "news", "published": "", "fetched": True, "quote": "体验文原文依据。", "paywall": False},
            {"title": "另一件事", "url": "https://x.com/another", "summary": "别的事。内容不同。",
             "kind": "news", "published": "", "fetched": True, "quote": "另一件事原文依据。", "paywall": False},
        ]
    }
    scores = json.dumps(
        {"scores": [
            {"i": 0, "info": 4, "source": 4, "relevance": 4, "timeliness": 4, "chat": 4, "profile": 0,
             "topic": "板子", "sensitive": False, "grounded": True, "junk": False, "junk_reason": "",
             "same_as_recent": False, "why": "合适", "icon": "tools"},
            {"i": 1, "info": 4, "source": 4, "relevance": 4, "timeliness": 4, "chat": 4, "profile": 1,
             "topic": "另一件事", "sensitive": False, "grounded": True, "junk": False, "junk_reason": "",
             "same_as_recent": False, "why": "OK", "icon": "link"},
        ]},
        ensure_ascii=False,
    )
    models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, scores])
    workers = FakeWorkers(_ok_report(dup_items))
    store, settings, feeds, m, w, t, p = _make_feeds(tmp_path, models=models, workers=workers)
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
        assert got == 2  # 第 1 条和第 0 条撞 url，被丢；剩 0、2 入选
        rows = store.read().execute("SELECT title, rejected, reject_gate, reject_reason FROM news_items ORDER BY id").fetchall()
        accepted = [r["title"] for r in rows if not r["rejected"]]
        assert sorted(accepted) == ["另一件事", "板子发布新闻稿"]
        # 两阶段恒生效：批内撞 url 的先撞在撒网登记簿（同 url_key 只留先见的），
        # 根本不变成候选 —— 不落 news_items，漏斗的 discovered 也只有 2 条。
        assert all(not r["rejected"] for r in rows)
        batch = store.read().execute(
            "SELECT * FROM news_batches ORDER BY id DESC LIMIT 1"
        ).fetchone()
        stats = store.kv_get(f"feeds.batch_stats.{batch['id']}") or {}
        funnel = stats.get("funnel") or {}
        assert int(funnel.get("discovered") or 0) == 2, f"登记簿该去重：{funnel}"


def test_prepare_news_scoring_missing_index_dropped(tmp_path) -> None:
    """模型漏给某条的分数 → 这条五项记 0、第二道筛掉，其它照常。"""
    partial_scores = json.dumps(
        {"scores": [
            {"i": 0, "info": 5, "source": 4, "relevance": 5, "timeliness": 4, "chat": 4,
             "profile": 0, "topic": "打分", "sensitive": False, "grounded": True,
             "junk": False, "junk_reason": "", "same_as_recent": False, "why": "合适", "icon": "tools"},
        ]},
        ensure_ascii=False,
    )
    models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, partial_scores])
    store, settings, feeds, m, w, topics, _ = _make_feeds(tmp_path, models=models)
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
        assert got == 1


def test_prepare_news_bad_icon_falls_back(tmp_path) -> None:
    scores = json.dumps(
        {"scores": [
            {"i": 0, "info": 5, "source": 4, "relevance": 5, "timeliness": 4, "chat": 4,
             "profile": 0, "topic": "图标", "sensitive": False, "grounded": True,
             "junk": False, "junk_reason": "", "same_as_recent": False, "why": "合适", "icon": "不存在的图标"},
            {"i": 1, "info": 5, "source": 4, "relevance": 4, "timeliness": 4, "chat": 4,
             "profile": 1, "topic": "图标", "sensitive": False, "grounded": True,
             "junk": False, "junk_reason": "", "same_as_recent": False, "why": "合适", "icon": "robot"},
        ]},
        ensure_ascii=False,
    )
    two_items = {"items": _WORKER_ITEMS["items"][:2]}
    models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, scores])
    workers = FakeWorkers(_ok_report(two_items))
    store, settings, feeds, m, w, topics, _ = _make_feeds(tmp_path, models=models, workers=workers)
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 2
        icons = [r["icon"] for r in store.read().execute("SELECT icon FROM news_items WHERE rejected=0 ORDER BY id").fetchall()]
        assert icons[0] == "newspaper"
        assert icons[1] == "robot"


# ----------------------------------------------------------------------
# make_idea
# ----------------------------------------------------------------------

_IDEA_JSON = json.dumps(
    {
        "idea": {
            "title": "我可以帮群把每周讨论整理成一页",
            "body": "每周自动汇总",
            "basis": "群里每周都在复盘",
            "icon": "books",
            "chat_worthy": True,
            "items": [
                {"kind": "task", "title": "做一页每周复盘模板", "desc": "先出一版模板给大家改"},
                {"kind": "goal", "title": "每周自动汇总讨论", "desc": "盯着这件事，每周五汇总一次"},
            ],
        }
    },
    ensure_ascii=False,
)


def test_make_idea_happy_path(tmp_path) -> None:
    models = FakeModelsQueue(ready=True, replies=[_IDEA_JSON])
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path, models=models)
    got = _run(feeds.make_idea(GID))
    assert isinstance(got, int) and got > 0
    row = store.read().execute("SELECT * FROM ideas WHERE id=?", (got,)).fetchone()
    assert row["state"] == "new"
    assert row["title"].startswith("我可以")
    assert row["icon"] == "books"
    assert row["created"] > NOW - 100
    assert len(topics.calls) == 1
    assert topics.calls[0]["kind"] == "idea"
    assert topics.calls[0]["ref_id"] == got
    assert models.calls[0][2].get("json_mode") is True
    # 新构想：items 落库、step / effort 不再生成（留空）
    assert json.loads(row["items"]) == [
        {"kind": "task", "title": "做一页每周复盘模板", "desc": "先出一版模板给大家改"},
        {"kind": "goal", "title": "每周自动汇总讨论", "desc": "盯着这件事，每周五汇总一次"},
    ]
    assert row["step"] == "" and row["effort"] == ""
    view = feeds.ideas_view(GID)[0]
    assert [it["no"] for it in view["items"]] == [1, 2]
    assert view["items"][1]["kind"] == "goal"


def test_make_idea_prompt_asks_items_and_drops_step_effort(tmp_path) -> None:
    """提示词要「包含的项目」和克制要求；不再要第一步 / 要多久。"""
    models = FakeModelsQueue(ready=True, replies=[_IDEA_JSON])
    store, settings, feeds, models, *_ = _make_feeds(tmp_path, models=models)
    _run(feeds.make_idea(GID))
    prompt = models.calls[0][1][-1]["content"]
    assert "items" in prompt
    assert "task" in prompt and "goal" in prompt
    assert "最多 5 个" in prompt
    assert '"step"' not in prompt and '"effort"' not in prompt


def test_make_idea_items_capped_at_5_and_bad_ones_dropped(tmp_path) -> None:
    many = json.dumps(
        {"idea": {"title": "我可以做一堆小事", "body": "b", "basis": "x", "icon": "tools",
                  "chat_worthy": False,
                  "items": [
                      {"kind": "task", "title": f"事 {i}"} for i in range(1, 9)
                  ] + ["不是表", {"desc": "没标题"}, {"kind": "wat", "title": "怪类型"}]}},
        ensure_ascii=False,
    )
    models = FakeModelsQueue(ready=True, replies=[many])
    store, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    got = _run(feeds.make_idea(GID))
    row = store.read().execute("SELECT items FROM ideas WHERE id=?", (got,)).fetchone()
    items = json.loads(row["items"])
    assert len(items) == 5
    assert [it["title"] for it in items] == ["事 1", "事 2", "事 3", "事 4", "事 5"]
    # 怪类型的 kind 回落 task
    assert items[0]["kind"] == "task"


def test_make_idea_no_items_writes_empty_list(tmp_path) -> None:
    """模型没给 items（老格式）→ 存 '[]'，读取按没有项目走老逻辑。"""
    plain = json.dumps(
        {"idea": {"title": "我可以整理一份清单", "body": "b", "basis": "x",
                  "step": "列条目", "effort": "一小时", "icon": "books", "chat_worthy": False}},
        ensure_ascii=False,
    )
    models = FakeModelsQueue(ready=True, replies=[plain])
    store, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    got = _run(feeds.make_idea(GID))
    row = store.read().execute("SELECT items, step FROM ideas WHERE id=?", (got,)).fetchone()
    assert row["items"] == "[]"
    assert row["step"] == ""  # 新构想不再生成第一步
    assert feeds.ideas_view(GID)[0]["items"] == []


def test_make_idea_null_returns_none(tmp_path) -> None:
    models = FakeModelsQueue(ready=True, replies=['{"idea": null}'])
    store, settings, feeds, m, w, topics, _ = _make_feeds(tmp_path, models=models)
    assert _run(feeds.make_idea(GID)) is None
    assert topics.calls == []
    assert store.read().execute("SELECT COUNT(*) c FROM ideas").fetchone()["c"] == 0


def test_make_idea_not_chat_worthy_not_added_to_pool(tmp_path) -> None:
    idea = json.dumps(
        {"idea": {"title": "我可以做运维巡检", "body": "b", "basis": "有服务器",
                  "step": "s", "effort": "半天", "icon": "tools", "chat_worthy": False}},
        ensure_ascii=False,
    )
    models = FakeModelsQueue(ready=True, replies=[idea])
    store, settings, feeds, m, w, topics, _ = _make_feeds(tmp_path, models=models)
    got = _run(feeds.make_idea(GID))
    assert isinstance(got, int)
    assert topics.calls == []


def test_make_idea_not_ready_returns_none(tmp_path) -> None:
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path, ready=False)
    assert _run(feeds.make_idea(GID)) is None
    assert models.calls == []


def test_make_idea_models_not_ready_returns_none(tmp_path) -> None:
    store, settings, feeds, *_ = _make_feeds(tmp_path, models=FakeModelsQueue(ready=False))
    assert _run(feeds.make_idea(GID)) is None


def _seed_idea(store: Store, gid: str, *, title: str, created: float, state: str = "new", updated: float | None = None) -> int:
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO ideas (group_id, icon, title, body, basis, step, effort, state,"
            " requested_by, task_id, up, down, created, updated)"
            " VALUES (?, 'bulb', ?, 'b', 's', 'st', 'e', ?, NULL, NULL, 0, 0, ?, ?)",
            (gid, title, state, created, created if updated is None else updated),
        )
        return int(cur.lastrowid or 0)


def test_make_idea_dedup_recent_30_days(tmp_path) -> None:
    old_title = "我可以帮群把每周讨论整理成一页纸"
    new_title = "我可以帮群把每周讨论整理成一页"  # difflib 相似度 > 0.75
    models = FakeModelsQueue(ready=True, replies=[
        json.dumps({"idea": {"title": new_title, "body": "b", "basis": "x",
                             "step": "s", "effort": "e", "icon": "books", "chat_worthy": True}},
                   ensure_ascii=False)
    ])
    store, settings, feeds, m, w, topics, _ = _make_feeds(tmp_path, models=models)
    _seed_idea(store, GID, title=old_title, created=NOW - 10 * 86400)
    assert _run(feeds.make_idea(GID)) is None
    assert topics.calls == []


def test_make_idea_no_dedup_beyond_30_days(tmp_path) -> None:
    old_title = "我可以帮群把每周讨论整理成一页纸"
    new_title = "我可以帮群把每周讨论整理成一页"
    models = FakeModelsQueue(ready=True, replies=[
        json.dumps({"idea": {"title": new_title, "body": "b", "basis": "x",
                             "step": "s", "effort": "e", "icon": "books", "chat_worthy": True}},
                   ensure_ascii=False)
    ])
    store, settings, feeds, m, w, topics, _ = _make_feeds(tmp_path, models=models)
    _seed_idea(store, GID, title=old_title, created=NOW - 40 * 86400)
    got = _run(feeds.make_idea(GID))
    assert isinstance(got, int)  # 超过 30 天不算重复


def test_make_idea_model_error_returns_none(tmp_path) -> None:
    from CharTyr_MaiWork.maiwork.models import ModelError

    models = FakeModelsQueue(ready=True, replies=[ModelError("端点挂了")])
    store, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    assert _run(feeds.make_idea(GID)) is None


# ----------------------------------------------------------------------
# feedback
# ----------------------------------------------------------------------


def test_feedback_up_down_and_switch(tmp_path) -> None:
    store, settings, feeds, *_ = _make_feeds(tmp_path)
    item_id = _seed_news_item(store, GID, title="t", url_key="u/1", created=NOW)
    r1 = feeds.feedback("news", item_id, "up", None)
    assert r1 == {"up": 1, "down": 0}
    r2 = feeds.feedback("news", item_id, "up", None)
    assert r2 == {"up": 2, "down": 0}
    r3 = feeds.feedback("news", item_id, "up", "down")  # 从 down 改成 up：down 原本 0，不为负
    assert r3 == {"up": 3, "down": 0}
    feeds.feedback("news", item_id, "down", None)
    feeds.feedback("news", item_id, "down", None)
    r4 = feeds.feedback("news", item_id, None, "down")  # 取消一个 down
    assert r4 == {"up": 3, "down": 1}
    r5 = feeds.feedback("news", item_id, None, "up")  # 取消一个 up
    assert r5 == {"up": 2, "down": 1}


def test_feedback_never_negative(tmp_path) -> None:
    store, settings, feeds, *_ = _make_feeds(tmp_path)
    item_id = _seed_news_item(store, GID, title="t", url_key="u/2", created=NOW)
    r = feeds.feedback("news", item_id, None, "down")  # down 本来就是 0
    assert r == {"up": 0, "down": 0}
    r2 = feeds.feedback("news", item_id, None, "up")
    assert r2 == {"up": 0, "down": 0}


def test_feedback_ideas_also_works(tmp_path) -> None:
    store, settings, feeds, *_ = _make_feeds(tmp_path)
    idea_id = _seed_idea(store, GID, title="我可以整理每周回顾", created=NOW)
    r = feeds.feedback("ideas", idea_id, "up", None)
    assert r == {"up": 1, "down": 0}


def test_feedback_missing_id_raises_keyerror(tmp_path) -> None:
    store, settings, feeds, *_ = _make_feeds(tmp_path)
    with pytest.raises(KeyError):
        feeds.feedback("news", 9999, "up", None)
    with pytest.raises(KeyError):
        feeds.feedback("ideas", 9999, "down", None)


def test_feedback_bad_kind_raises_valueerror(tmp_path) -> None:
    store, settings, feeds, *_ = _make_feeds(tmp_path)
    with pytest.raises(ValueError):
        feeds.feedback("bogus", 1, "up", None)


# ----------------------------------------------------------------------
# idea_action
# ----------------------------------------------------------------------


def test_idea_action_want_do_dismiss_flow(tmp_path) -> None:
    store, settings, feeds, *_ = _make_feeds(tmp_path)
    idea_id = _seed_idea(store, GID, title="我可以做个小工具", created=NOW)
    v1 = feeds.idea_action(idea_id, "want", by="阿柒")
    assert v1["state"] == "wanted"
    assert v1["requested_by"] == "阿柒"
    assert v1["id"] == idea_id
    assert set(v1.keys()) >= {
        "id", "icon", "title", "body", "basis", "step", "effort",
        "state", "requested_by", "task_id", "created_ts", "feedback",
    }
    v2 = feeds.idea_action(idea_id, "do", by="管理员")
    assert v2["state"] == "started"
    assert v2["requested_by"] == "阿柒"  # 保留想要的人名


def test_idea_action_dismiss_from_new(tmp_path) -> None:
    store, settings, feeds, *_ = _make_feeds(tmp_path)
    idea_id = _seed_idea(store, GID, title="我可以做个小工具", created=NOW)
    v = feeds.idea_action(idea_id, "dismiss", by="管理员")
    assert v["state"] == "dismissed"


def test_idea_action_dismiss_from_wanted(tmp_path) -> None:
    store, settings, feeds, *_ = _make_feeds(tmp_path)
    idea_id = _seed_idea(store, GID, title="我可以做个小工具", created=NOW)
    feeds.idea_action(idea_id, "want", by="阿柒")
    v = feeds.idea_action(idea_id, "dismiss", by="管理员")
    assert v["state"] == "dismissed"


def test_idea_action_illegal_transitions(tmp_path) -> None:
    store, settings, feeds, *_ = _make_feeds(tmp_path)
    idea_id = _seed_idea(store, GID, title="我可以做个小工具", created=NOW)
    feeds.idea_action(idea_id, "dismiss", by="管理员")
    with pytest.raises(ValueError) as e:
        feeds.idea_action(idea_id, "do", by="管理员")
    assert str(e.value)  # 中文说明
    with pytest.raises(ValueError):
        feeds.idea_action(idea_id, "want", by="阿柒")


def test_idea_action_started_no_more_ops(tmp_path) -> None:
    store, settings, feeds, *_ = _make_feeds(tmp_path)
    idea_id = _seed_idea(store, GID, title="我可以做个小工具", created=NOW)
    feeds.idea_action(idea_id, "do", by="管理员")
    for op in ("want", "do", "dismiss"):
        with pytest.raises(ValueError):
            feeds.idea_action(idea_id, op, by="管理员" if op != "want" else "阿柒")


def test_idea_action_missing_idea_raises_valueerror(tmp_path) -> None:
    store, settings, feeds, *_ = _make_feeds(tmp_path)
    with pytest.raises(ValueError):
        feeds.idea_action(424242, "want", by="阿柒")


def test_idea_action_on_start_callback_called(tmp_path) -> None:
    """do 时如果注入了 on_start 回调 → 用 idea 视图调一次；回调抛异常也能无视。"""
    store, settings, feeds, *_ = _make_feeds(tmp_path)
    idea_id = _seed_idea(store, GID, title="我可以做个小工具", created=NOW)
    seen: List[dict] = []
    feeds.on_start = lambda idea: seen.append(idea)
    feeds.idea_action(idea_id, "do", by="管理员")
    assert len(seen) == 1
    assert seen[0]["id"] == idea_id

    idea_id2 = _seed_idea(store, GID, title="我可以做另一件事", created=NOW)
    def _boom(idea: dict) -> None:
        raise RuntimeError("炸了")

    feeds.on_start = _boom
    v = feeds.idea_action(idea_id2, "do", by="管理员")  # 回调抛异常不影响状态
    assert v["state"] == "started"


# ----------------------------------------------------------------------
# news_view / ideas_view / today_count
# ----------------------------------------------------------------------


def test_news_view_structure_and_order(tmp_path) -> None:
    _, _, feeds, *_ = _make_feeds(tmp_path)
    store = feeds._store  # 同一对象，建数据用
    with _TimePatch():
        # 两个批次：旧批次 1 条，新批次 2 条
        b1 = _seed_batch_and_items(
            store, GID, created=NOW - 86400,
            items=[("旧资讯", "a.com/1", 0.7)],
        )
        b2 = _seed_batch_and_items(
            store, GID, created=NOW - 100,
            items=[("高分", "a.com/2", 0.95), ("低分", "a.com/3", 0.62)],
        )
        view = feeds.news_view(GID)  # 群友版（admin=False 默认）
    assert len(view) == 2
    assert view[0]["id"] == b2  # 新的批次在前
    assert view[1]["id"] == b1
    for b in view:
        # 群友版：没有 rejected 一栏；rejected_count 人人可见（质量标准）；
        # stats=这一轮工具用量 {searches, pages, kept}，老批次没有 → None
        assert set(b.keys()) == {"id", "slot_ts", "found", "kept", "skipped", "note", "rejected_count", "items", "stats"}
        assert isinstance(b["skipped"], bool)
        assert b["rejected_count"] == 0
        assert b["stats"] is None  # 测试里是手插的批次，没有统计
    items = view[0]["items"]
    assert [i["title"] for i in items] == ["高分", "低分"]  # 按分数降序
    item = items[0]
    # 群友版 item 字段（「有人味」新增 body/reason/refs/audience/image_url/verify/angle；
    # keywords 只给管理员，群友版没有）
    assert set(item.keys()) == {
        "id", "icon", "kind", "title", "summary", "why", "sources",
        "published_ts", "scores", "topic", "sensitive", "profile_ref", "status", "feedback",
        "body", "reason", "refs", "audience", "image_url", "verify", "angle", "viz",
        "bridge",  # 2026-09-29：拓展条目「从哪条兴趣跳过来」
        "followup",  # 2026-09-30：同一件事的新进展（「后续」），不是后续为 None
    }
    assert set(item["scores"].keys()) == {"info", "source", "relevance", "timeliness", "chat", "avg"}
    assert item["kind"] == "news" and item["topic"] == "话题" and item["sensitive"] is False
    assert set(item["status"].keys()) == {"kind", "at", "replies", "expires_ts"}
    assert set(item["feedback"].keys()) == {"up", "down"}
    assert isinstance(item["sources"], list)
    assert item["status"]["kind"] == "pool"


def test_news_view_pool_expired_shows_expired(tmp_path) -> None:
    _, _, feeds, *_ = _make_feeds(tmp_path)
    store = feeds._store
    with _TimePatch():
        _seed_batch_and_items(
            store, GID, created=NOW - 3600,
            items=[("过期货", "a.com/9", 0.9)],
            expires_ts=NOW - 10,  # 已过期
        )
        view = feeds.news_view(GID)
        assert view[0]["items"][0]["status"]["kind"] == "expired"
        # status_kind 为 used 的不受过期影响
        _seed_batch_and_items(
            store, GID, created=NOW - 3000,
            items=[("用过", "a.com/10", 0.9)],
            expires_ts=NOW - 10,
            status_kind="used",
            status_at=NOW - 2000,
        )
        view2 = feeds.news_view(GID)
        kinds = {it["title"]: it["status"]["kind"] for b in view2 for it in b["items"]}
        assert kinds["过期货"] == "expired"
        assert kinds["用过"] == "used"


def test_news_view_days_window(tmp_path) -> None:
    _, _, feeds, *_ = _make_feeds(tmp_path)
    store = feeds._store
    with _TimePatch():
        _seed_batch_and_items(store, GID, created=NOW - 4 * 86400, items=[("太早", "a.com/1", 0.9)])
        _seed_batch_and_items(store, GID, created=NOW - 86400, items=[("昨天", "a.com/2", 0.9)])
        view = feeds.news_view(GID)  # 默认 3 天
    titles = [it["title"] for b in view for it in b["items"]]
    assert titles == ["昨天"]


def test_news_view_other_group_not_shown(tmp_path) -> None:
    _, _, feeds, *_ = _make_feeds(tmp_path)
    store = feeds._store
    _seed_group(store, GID_OTHER, ready=True)
    with _TimePatch():
        _seed_batch_and_items(store, GID_OTHER, created=NOW, items=[("别群", "a.com/8", 0.9)])
        assert feeds.news_view(GID) == []
        assert len(feeds.news_view(GID_OTHER)) == 1


def test_today_count(tmp_path) -> None:
    _, _, feeds, *_ = _make_feeds(tmp_path)
    store = feeds._store
    today_bj = clock.bj(NOW).replace(hour=10, minute=0, second=0, microsecond=0)
    today_ts = today_bj.timestamp()
    with _TimePatch():
        _seed_batch_and_items(store, GID, created=today_ts, items=[("今天1", "a.com/1", 0.9), ("今天2", "a.com/2", 0.8)])
        # 昨天同时刻
        _seed_batch_and_items(store, GID, created=today_ts - 86400, items=[("昨天", "a.com/3", 0.9)])
        assert feeds.today_count(GID) == 2


def test_ideas_view_structure_and_rules(tmp_path) -> None:
    _, _, feeds, *_ = _make_feeds(tmp_path)
    store = feeds._store
    with _TimePatch():
        _seed_idea(store, GID, title="我可以做 A", created=NOW - 100)
        _seed_idea(store, GID, title="我可以做 B", created=NOW - 50, state="dismissed", updated=NOW - 50)
        _seed_idea(store, GID, title="我可以做 C", created=NOW - 4 * 86400, state="dismissed", updated=NOW - 4 * 86400)  # dismiss 超 3 天
        _seed_idea(store, GID, title="我可以做 D", created=NOW - 10, state="wanted")
        view = feeds.ideas_view(GID)
    titles = [it["title"] for it in view]
    assert "我可以做 C" not in titles  # dismissed 超过 3 天不显示
    assert titles[0] == "我可以做 D"  # 新的在前
    assert titles[1] == "我可以做 B"  # dismissed 3 天内还显示
    it = view[0]
    # 「有人味」新增 feasibility / keywords
    assert set(it.keys()) == {
        "id", "icon", "title", "body", "basis", "step", "effort",
        "state", "requested_by", "task_id", "created_ts", "feedback",
        "feasibility", "keywords", "target_user_id", "items",
    }
    assert set(it["feedback"].keys()) == {"up", "down"}
    assert all(isinstance(x["created_ts"], float) for x in view)


def test_ideas_view_other_group_not_shown(tmp_path) -> None:
    _, _, feeds, *_ = _make_feeds(tmp_path)
    store = feeds._store
    _seed_group(store, GID_OTHER, ready=True)
    _seed_idea(store, GID_OTHER, title="别群构想", created=NOW)
    with _TimePatch():
        assert feeds.ideas_view(GID) == []


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------


class _TimePatch:
    """临时把 feeds.clock.now 改成一个固定值（本测试文件的数据都围着 NOW 造）。"""

    def __enter__(self):
        import CharTyr_MaiWork.maiwork.feeds as feeds_mod

        self._mod = feeds_mod
        self._orig = feeds_mod.clock.now
        feeds_mod.clock.now = lambda: NOW
        return self

    def __exit__(self, *exc):
        self._mod.clock.now = self._orig


def _seed_batch_and_items(
    store: Store,
    gid: str,
    *,
    created: float,
    items: list,
    expires_ts: float | None = None,
    status_kind: str = "pool",
    status_at: float | None = None,
) -> int:
    with _TimePatch(), store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
            " VALUES (?, ?, ?, ?, 0, '', ?)",
            (gid, created, len(items), len(items), created),
        )
        batch_id = int(cur.lastrowid or 0)
        for i, (title, url_key, score) in enumerate(items):
            # 质量标准列：kind 默认 news、scores 用 score 当五项同分造一个
            scores_json = json.dumps(
                {"info": score, "source": score, "relevance": score, "timeliness": score,
                 "chat": score, "avg": score}
            )
            conn.execute(
                "INSERT INTO news_items (batch_id, group_id, icon, title, summary, why, sources,"
                " url_key, published_ts, score, status_kind, status_at, replies, expires_ts,"
                " up, down, created, kind, scores, topic, sensitive, profile_ref, rejected)"
                " VALUES (?, ?, 'robot', ?, '摘要', '原因', ?, ?, NULL, ?, ?, ?, 0, ?, 0, 0, ?,"
                " 'news', ?, '话题', 0, '', 0)",
                (
                    batch_id, gid, title,
                    json.dumps([{"url": f"https://{url_key}", "site": "a.com", "title": title}], ensure_ascii=False),
                    url_key, score, status_kind, status_at, expires_ts, created + i, scores_json,
                ),
            )
    return batch_id


def _run(coro):
    import asyncio

    return asyncio.run(coro)
