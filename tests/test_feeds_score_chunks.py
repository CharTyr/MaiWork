"""资讯打分分批（2026-09-28 线上回放实测后加，docs/02-设计.md §4.1）。

线上回放：15 条搜索候选 + 6 条 RSS 一次性交给主模型打分，提示词变长后每次都超过
120 秒超时，6 次重试白等 789 秒，最后每条五项都记 0，全部被当成「相关度 0」拒掉
——等于一次静默的全军覆没。修法：
- 每次最多给模型 _SCORE_CHUNK=8 条，编号用全批统一的编号；后面几批带上「前面已评过的」
  标题清单，dup_in_batch 照样能指到前面批次的候选；
- 打分调用超时放宽（timeout=240），每个候选模型只试一次（retries=0；2026-10-04 起），别一轮卡十几分钟；
- 某一批失败（模型报错 / JSON 坏 / 一条都没对上）：前面批次的分照留，这批候选
  明确拒掉并写清「打分没做完」，不再冒充「相关度 0」；所有批次都失败才整轮跳过。
"""

from __future__ import annotations

import pytest

from CharTyr_MaiWork.maiwork.feeds import _SCORE_CHUNK
from CharTyr_MaiWork.maiwork.models import ModelError

from fakes import FakeModelsQueue
from test_feeds_freshness import GID, _TimePatch, _make_feeds, _run, _score, _scores_json


def _cands(n: int) -> list[dict]:
    return [
        {
            "title": f"候选标题{i:02d}号", "url": f"https://ex{i}.com/p", "summary": f"摘要{i}",
            "kind": "news", "quote": "原文依据", "fetched": True, "paywall": False,
        }
        for i in range(n)
    ]


def _reply_for(idxs, extra: dict | None = None) -> str:
    extra = extra or {}
    return _scores_json(*[_score(i, **(extra.get(i, {}))) for i in idxs])


def test_chunk_size_is_eight() -> None:
    assert _SCORE_CHUNK == 8


def test_twenty_candidates_three_calls_global_indices_and_earlier_reference(tmp_path) -> None:
    models = FakeModelsQueue(ready=True, replies=[
        _reply_for(range(0, 8)), _reply_for(range(8, 16)), _reply_for(range(16, 20)),
    ])
    _store, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    cands = _cands(20)
    with _TimePatch():
        _run(feeds._score(GID, settings, cands))
    assert len(models.calls) == 3
    p1, p2, p3 = (str(c[1][0]["content"]) for c in models.calls)
    # 每批只列本批候选，编号是全批统一的
    assert "[0]（资讯）候选标题00号" in p1 and "[7]（资讯）候选标题07号" in p1
    assert "候选标题08号" not in p1
    assert "[8]（资讯）候选标题08号" in p2 and "[15]（资讯）候选标题15号" in p2
    assert "[16]（资讯）候选标题16号" in p3
    # 后面几批带「前面已评过的」标题清单
    assert "前面已经评过的" not in p1
    assert "前面已经评过的" in p2 and "[0] 候选标题00号" in p2
    assert "[15] 候选标题15号" in p3
    # 超时放宽、重试收紧
    for _role, _msgs, kw in models.calls:
        assert kw.get("timeout") == 240
        assert kw.get("retries") == 0
    assert all(c["scores"]["avg"] > 0 for c in cands)
    assert not any("reject" in c for c in cands)


def test_dup_in_batch_across_chunks(tmp_path) -> None:
    models = FakeModelsQueue(ready=True, replies=[
        _reply_for(range(0, 8)),
        _reply_for(range(8, 10), {9: {"dup_in_batch": 1}}),
    ])
    _store, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    cands = _cands(10)
    with _TimePatch():
        _run(feeds._score(GID, settings, cands))
    assert cands[9]["_dup_target"] == ("batch", cands[1])


def test_failed_chunk_rejected_with_clear_reason_others_keep_scores(tmp_path) -> None:
    models = FakeModelsQueue(ready=True, replies=[
        _reply_for(range(0, 8)), ModelError("网络错误（ReadTimeout）"), _reply_for(range(16, 18)),
    ])
    _store, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    cands = _cands(18)
    with _TimePatch():
        _run(feeds._score(GID, settings, cands))
    for c in cands[:8] + cands[16:]:
        assert "reject" not in c and c["scores"]["avg"] > 0
    for c in cands[8:16]:
        assert c["reject"][0] == "score"
        assert "打分没做完" in c["reject"][1]


def test_chunk_with_no_matching_scores_counts_as_failed(tmp_path) -> None:
    models = FakeModelsQueue(ready=True, replies=[_reply_for(range(0, 8)), '{"scores": []}'])
    _store, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    cands = _cands(10)
    with _TimePatch():
        _run(feeds._score(GID, settings, cands))
    assert "reject" not in cands[0]
    assert cands[8]["reject"][0] == "score" and cands[9]["reject"][0] == "score"


def test_bad_json_once_is_asked_again_not_whole_batch_lost(tmp_path) -> None:
    """线上巡检 2026-10-02：打分回了解析不了的内容，整批已核验候选全丢。
    JSON 坏（模型有回话但不是合法 JSON）再问一次；第二次好了照常打分。"""
    models = FakeModelsQueue(ready=True, replies=["这不是 JSON", _reply_for(range(0, 5))])
    _store, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    cands = _cands(5)
    with _TimePatch():
        _run(feeds._score(GID, settings, cands))
    assert len(models.calls) == 2
    assert all("reject" not in c and c["scores"]["avg"] > 0 for c in cands)


def test_model_error_is_not_asked_again(tmp_path) -> None:
    """模型层已经重试 / 换备用过才抛 ModelError：打分这层不再加问，免得一轮卡太久。"""
    models = FakeModelsQueue(ready=True, replies=[ModelError("超时"), _reply_for(range(0, 5))])
    _store, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    with _TimePatch(), pytest.raises(ModelError):
        _run(feeds._score(GID, settings, _cands(5)))
    assert len(models.calls) == 1


def test_all_chunks_failed_raises(tmp_path) -> None:
    models = FakeModelsQueue(ready=True, replies=[ModelError("超时"), "不是 JSON"])
    _store, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    with _TimePatch(), pytest.raises((ModelError, ValueError)):
        _run(feeds._score(GID, settings, _cands(10)))


def test_failed_chunk_items_not_rejected_again_as_relevance_zero(tmp_path) -> None:
    """prepare_news 全流程：一批失败时，失败那批入库的拒绝理由是「打分没做完」而不是相关度 0。"""
    from test_feeds_freshness import FakeWorkers, _ok_report, _FOCUS_JSON, _posts_json, _post, _rejected_rows

    names = ["量子芯片新架构发布", "开源掌机固件更新", "本地大模型推理提速", "无线电爱好者大会",
             "云成本账单拆解", "新款机械键盘评测", "独立游戏节获奖名单", "卫星互联网新进展",
             "浏览器引擎重大更新", "天文摄影入门指南"]
    items = [
        {"title": names[i], "url": f"https://site{i}.com/a", "summary": f"摘要{i}", "kind": "news",
         "published": 1_790_000_000.0 - 3600, "fetched": True, "quote": "原文依据", "paywall": False}
        for i in range(10)
    ]
    models = FakeModelsQueue(ready=True, replies=[
        _FOCUS_JSON,
        _scores_json(*[_score(i, topic=f"话题{i}") for i in range(8)]),
        ModelError("网络错误（ReadTimeout）"),
        _posts_json(*[_post(i, names[i]) for i in range(8)]),
    ])
    store, settings, feeds, *_ = _make_feeds(
        tmp_path, models=models, workers=FakeWorkers(_ok_report({"items": items})))
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
    assert got > 0
    rows = _rejected_rows(store)
    reasons = [r["reject_reason"] for r in rows]
    late = [r for r in reasons if "打分没做完" in r]
    assert len(late) == 2
    assert not any("相关度 0.0" in r for r in reasons)


# ----------------------------------------------------------------------
# 线上回放第二次（2026-09-28 20:10）：step-5-preview 常常只回一个裸对象
# {"i":0,...}（不是 {"scores":[...]}），每批都算「一条都没对上」，整轮跳过。
# 修法：裸对象 / 裸列表也认；这批没评上的只针对漏的再追问一次，还漏的才判「打分没做完」。
# ----------------------------------------------------------------------

import json as _json


def test_bare_object_accepted_and_missing_asked_again(tmp_path) -> None:
    models = FakeModelsQueue(ready=True, replies=[
        _json.dumps(_score(0), ensure_ascii=False),  # 只回一个裸对象
        _reply_for([1, 2]),                            # 追问漏的 1、2
    ])
    _store, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    cands = _cands(3)
    with _TimePatch():
        _run(feeds._score(GID, settings, cands))
    assert len(models.calls) == 2
    retry_prompt = str(models.calls[1][1][0]["content"])
    assert "[1]（资讯）候选标题01号" in retry_prompt and "[2]（资讯）候选标题02号" in retry_prompt
    assert "[0]（资讯）候选标题00号" not in retry_prompt
    assert all("reject" not in c and c["scores"]["avg"] > 0 for c in cands)


def test_bare_list_accepted(tmp_path) -> None:
    models = FakeModelsQueue(ready=True, replies=[
        _json.dumps([_score(0), _score(1)], ensure_ascii=False),
    ])
    _store, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    cands = _cands(2)
    with _TimePatch():
        _run(feeds._score(GID, settings, cands))
    assert len(models.calls) == 1
    assert all("reject" not in c for c in cands)


def test_still_missing_after_retry_rejected_as_unfinished(tmp_path) -> None:
    models = FakeModelsQueue(ready=True, replies=[
        _json.dumps(_score(0), ensure_ascii=False),
        _json.dumps(_score(1), ensure_ascii=False),
    ])
    _store, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    cands = _cands(3)
    with _TimePatch():
        _run(feeds._score(GID, settings, cands))
    assert len(models.calls) == 2
    assert "reject" not in cands[0] and "reject" not in cands[1]
    # 模型回了但漏了这条（不是出错）→ 2026-09-29 起理由写「打分漏了这条」，别和超时混在一起
    assert cands[2]["reject"][0] == "score" and "打分漏了这条" in cands[2]["reject"][1]


def test_prompt_demands_every_item(tmp_path) -> None:
    models = FakeModelsQueue(ready=True, replies=[_reply_for(range(3))])
    _store, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    with _TimePatch():
        _run(feeds._score(GID, settings, _cands(3)))
    prompt = str(models.calls[0][1][0]["content"])
    assert "每一条都要打分" in prompt and "共 3 条" in prompt


def test_list_under_any_key_accepted(tmp_path) -> None:
    """线上第三次回放：模型回 {"": [ {...}, … ]}（键名是空串）。任何键下的「对象列表」都认。"""
    models = FakeModelsQueue(ready=True, replies=[
        _json.dumps({"": [_score(0), _score(1), _score(2)]}, ensure_ascii=False),
    ])
    _store, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    cands = _cands(3)
    with _TimePatch():
        _run(feeds._score(GID, settings, cands))
    assert len(models.calls) == 1
    assert all("reject" not in c for c in cands)


# ----------------------------------------------------------------------
# 2026-10-01 提速：几批并发跑（asyncio.gather）。线上一轮 21 条照老顺序 ~116s，
# 并发后 ≈ 最长那批 + 一点合并开销。有人看了标题要的硬保证：
# 1) 几批在时间上真的重叠（等活时间复用，不是先后排队）；
# 2) 和老串行一样的结果：同批编号、reason、by_index/by_title 合并顺序照批号先到先得。
# ----------------------------------------------------------------------


def test_chunks_run_concurrently(tmp_path) -> None:
    """一堆 20 条 → 3 批；给每次 chat 强制睡 0.05s，并发峰值 >1（证明几批真的重叠跑，不是先后排队）。"""
    import asyncio as _a

    state = {"cur": 0, "peak": 0}
    replies = [_reply_for(range(0, 8)), _reply_for(range(8, 16)), _reply_for(range(16, 20))]
    models = FakeModelsQueue(ready=True, replies=replies)
    _store, settings, feeds, *_ = _make_feeds(tmp_path, models=models)

    # 在 FakeModelsQueue.chat 外包一层带 sleep 的版本，并发峰值记在它身上
    orig_chat = FakeModelsQueue.chat

    async def slow_wrapped(self, role=None, messages=None, **kwargs):
        state["cur"] += 1
        state["peak"] = max(state["peak"], state["cur"])
        try:
            await _a.sleep(0.05)
            return await orig_chat(self, role, messages, **kwargs)
        finally:
            state["cur"] -= 1

    FakeModelsQueue.chat = slow_wrapped
    try:
        cands = _cands(20)
        with _TimePatch():
            _run(feeds._score(GID, settings, cands))
    finally:
        FakeModelsQueue.chat = orig_chat

    assert state["peak"] == 3, f"3 批隔开来跑的话 peak 应该是 1；真并发就该是 3（实际是 {state['peak']}）"
    assert all(c["scores"]["avg"] > 0 for c in cands)


def test_concurrent_matches_sequential_results(tmp_path) -> None:
    """并发版的产出和串行老口径一模一样：编号对上、失败那批照拒、零分的理由照旧。"""
    # 三批：第 2 批模型炸、第 1/3 批照回 —— 串行/并发都得是同一批被拒、别的保留
    models_par = FakeModelsQueue(ready=True, replies=[
        _reply_for(range(0, 8)), ModelError("网络错误"), _reply_for(range(16, 18)),
    ])
    _store, settings_par, feeds_par, *_ = _make_feeds(tmp_path / "par", models=models_par)
    c_par = _cands(18)
    with _TimePatch():
        _run(feeds_par._score(GID, settings_par, c_par))

    def fingerprint(cands):
        out = []
        for c in cands:
            out.append((
                tuple(round(float(v), 6) for v in c["scores"].values()),
                c.get("reject", None),
                c.get("topic", ""), c.get("icon", ""), c.get("profile_ref", ""),
            ))
        return out

    # 期望：0..7 有分、8..15 score-fail（打分没做完）、16/17 有分 —— 和串行老口径逐项一致
    expected = []
    for i in range(18):
        c = {
            "scores": {"info": 0.0, "source": 0.0, "relevance": 0.0, "timeliness": 0.0,
                        "chat": 0.0, "avg": 0.0},
        }
        if 8 <= i < 16:
            expected.append(((0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                              ("score", "打分没做完（模型超时/出错），这轮没评上"), "", "newspaper", ""))
        else:
            row = c_par[i]
            expected.append((tuple(round(float(v), 6) for v in row["scores"].values()), None,
                             row["topic"], row["icon"], row["profile_ref"]))
    assert fingerprint(c_par) == expected
    # 后段那条真的拿过第 16/17 批对应的分（avg > 0），失败那批记的 reject 是统一那句
    assert c_par[16]["scores"]["avg"] > 0 and c_par[17]["scores"]["avg"] > 0
