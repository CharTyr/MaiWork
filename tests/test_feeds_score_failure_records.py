"""整轮打分失败也要留痕（线上巡检 2026-10-08，批次 #199）。

实测：一次 HTTP 451 内容拦截（`censorship_blocked`）让同一打分块 8 条全失败；
`_score` 在「所有批都失败」时抛错，`prepare_news` 只记一条 `_skipped_batch`
（found 12 / kept 0 / skipped 1，note「模型打分失败：端点返回 451…」）就 return 0，
候选的逐条拒绝记录（哪些过了硬淘汰、为什么这轮没收录）全部没落库，事后无法审计。

修法（只做留痕 + 不拖累已成功的块）：
- 整轮失败也走正常落库路径：已经拒的保持原 reject 不动，还没评上的标
  score「打分没做完（模型超时/出错），这轮没评上」；本轮 0 收录、skipped=1、found 照实；
- 不补默认分、不放行、不发卡片，也不跑写帖 / 自检 / 话题池（零额外模型调用）；
- 451 仍按「打分失败」处理：不换通道、不拆条重发、不绕内容拦截（那属于 models.py 的边界，
  这里只保证失败原因如实留在批次 note 里）；
- 某个块成功、某个块失败时，原来的继续出资讯能力一点不变（只有「全失败」才走上面那条）。
"""

from __future__ import annotations

import json

from CharTyr_MaiWork.maiwork.models import ModelError

from fakes import PICK_FALLBACK_REPLY, FakeModelsQueue, patch_two_phase_feeds
from test_feeds_freshness import (
    GID,
    _FOCUS_JSON,
    _TimePatch,
    _make_feeds,
    _ok_report,
    _post,
    _posts_json,
    _run,
    _score,
    _scores_json,
)

_451 = '端点返回 451：{"type":"censorship_blocked","message":"blocked by content policy"}'

_NAMES = [
    "量子芯片新架构发布", "开源掌机固件更新", "本地大模型推理提速", "无线电爱好者大会",
    "云成本账单拆解", "新款机械键盘评测", "独立游戏节获奖名单", "卫星互联网新进展",
    "浏览器引擎重大更新", "天文摄影入门指南", "复古计算设备修复", "自动驾驶新规解读",
]


def _search_items(n: int) -> list[dict]:
    return [
        {"title": _NAMES[i % len(_NAMES)], "url": f"https://site{i}.example.com/a",
         "summary": f"摘要{i}：两三句话讲清楚这件事。", "kind": "news",
         "published": 1_790_000_000.0 - 3600, "fetched": True, "quote": "原文依据", "paywall": False}
        for i in range(n)
    ]


def _batches(store) -> list:
    return store.read().execute(
        "SELECT id, found, kept, skipped, note FROM news_batches WHERE group_id=? ORDER BY id", (GID,)
    ).fetchall()


def _item_rows(store) -> list:
    return store.read().execute(
        "SELECT group_id, rejected, reject_gate, reject_reason FROM news_items"
        " WHERE group_id=? ORDER BY id", (GID,)
    ).fetchall()


def _purposes(models: FakeModelsQueue) -> list[str]:
    return [str(kw.get("purpose") or "") for _role, _msgs, kw in models.calls]


def _picks_reply(n: int) -> str:
    """真的挑 n 条（回落占位只挑前 10，造不出 12 条的线上形状）。"""
    return json.dumps(
        {"picks": [{"i": k, "kind": "news", "hook": f"值得打开第 {k} 条"} for k in range(n)]},
        ensure_ascii=False,
    )


def test_all_chunks_failed_keeps_every_candidate_record(tmp_path) -> None:
    """451 让整轮打分全失败：批次照记，12 条候选逐条落库，4 条第一道理由原样保留。

    数据形状对齐线上 #199（found 12 / kept 0 / skipped 1；survivors 8 是日志推断，
    这里用假数据把 12–8=4 条先被第一道拒掉的候选造清楚）。
    """
    items = _search_items(12)
    # 先被第一道（代码侧硬淘汰）拒掉的 4 条：理由各不相同，后面一条都不许被覆盖
    items[1].update(fetched=False, quote="")                # 原文没打开过
    items[3].update(quote="")                               # 打开过但没有原文依据
    items[5].update(paywall=True)                           # 原文要登录 / 付费
    items[7]["published"] = 1_790_000_000.0 - 10 * 86400   # 旧闻（> _NEWS_MAX_AGE_DAYS=7 天）
    expected_hard = {
        items[1]["title"]: "原文没打开过",
        items[3]["title"]: "原文没打开过",
        items[5]["title"]: "原文要登录/付费",
        items[7]["title"]: "旧闻",
    }
    models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, ModelError(_451)])
    from test_feeds_freshness import FakeWorkers

    store, _settings, feeds, models, _workers, topics, _profiles = _make_feeds(
        tmp_path, models=models, workers=FakeWorkers(_ok_report({"items": items})))
    # 12 条要真走到打分：粗筛有「一个方向最多占 40%」的均衡上限，12 条挤在一个方向会被削到 10；
    # 分给两个方向（8+4）就照实留下 12 条。再把「挑」的占位回复换成真挑满 12 条。
    patch_two_phase_feeds(feeds, models, {1: items[:8], 2: items[8:]})
    assert models.reply_queue[1] == PICK_FALLBACK_REPLY
    models.reply_queue[1] = _picks_reply(12)
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))

    assert got == 0, "一条都不能放行"
    batches = _batches(store)
    assert len(batches) == 1
    b = batches[0]
    # skipped 仍是老口径的布尔「本轮没收录」，不改成条数
    assert (b["found"], b["kept"], b["skipped"]) == (12, 0, 1)
    assert "模型打分失败" in b["note"] and "451" in b["note"] and "censorship_blocked" in b["note"]

    rows = _item_rows(store)
    assert len(rows) == 12, "候选逐条留痕，不再只剩一条 skipped 批次"
    assert all(r["group_id"] == GID for r in rows), "群隔离：只写本群"
    assert all(int(r["rejected"]) == 1 for r in rows)
    score_rows = [r for r in rows if r["reject_gate"] == "score"]
    assert len(score_rows) == 8, "8 条幸存者没评上 → 标 score"
    assert all("打分没做完" in r["reject_reason"] for r in score_rows)
    assert not any("相关度" in r["reject_reason"] for r in rows), "不给未打分的候选补默认分"
    # 4 条第一道理由原样保留（按标题对，不依赖入库顺序）
    got_hard = {}
    for r in store.read().execute(
        "SELECT title, reject_reason FROM news_items WHERE group_id=? AND reject_gate='hard'", (GID,)
    ).fetchall():
        got_hard[str(r["title"])] = str(r["reject_reason"])
    assert set(got_hard) == set(expected_hard)
    for title, reason in expected_hard.items():
        assert reason in got_hard[title], f"{title} 的第一道理由被覆盖：{got_hard[title]}"

    # 失败轮也带上新增统计：有多少条卡在打分（funnel 里，老字段一个不动）
    stats = store.kv_get(f"feeds.batch_stats.{int(b['id'])}")
    assert stats is not None and stats["funnel"]["score_failed"] == 8

    # 没有写帖 / 自检 / 话题池这些多花的调用；451 也不触发换通道重试
    assert _purposes(models) == ["feeds.focus", "feeds.pick", "feeds.score"]
    assert topics.calls == []


def test_partial_chunk_failure_still_produces_news(tmp_path) -> None:
    """只有一个块失败：照旧继续出资讯（失败块留痕、成功块正常入库），能力不退化。"""
    items = _search_items(10)
    models = FakeModelsQueue(ready=True, replies=[
        _FOCUS_JSON,
        ModelError(_451),                                   # 0–7 这块失败
        _scores_json(_score(8, topic="话题八"), _score(9, topic="话题九")),
        _posts_json(_post(0, _NAMES[0]), _post(1, _NAMES[1])),
        '{"unsupported": []}',
    ])
    from test_feeds_freshness import FakeWorkers

    store, _settings, feeds, models, _workers, _topics, _profiles = _make_feeds(
        tmp_path, models=models, workers=FakeWorkers(_ok_report({"items": items})))
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))

    assert got == 2, "成功的那块照常出资讯"
    batches = _batches(store)
    assert batches and all(int(b["skipped"]) == 0 for b in batches)
    assert batches[-1]["kept"] == 2
    rows = _item_rows(store)
    failed = [r for r in rows if r["reject_gate"] == "score"]
    assert len(failed) == 8 and all("打分没做完" in r["reject_reason"] for r in failed)
    assert sum(1 for r in rows if not int(r["rejected"])) == 2
    assert "feeds.post" in _purposes(models) and "feeds.post_check" in _purposes(models)
