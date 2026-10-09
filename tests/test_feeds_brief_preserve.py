"""卡片短摘要（brief）不再被硬截（线上巡检 2026-10-08，卡片 #76 / 条目 #1301）。

实测：模型原文 176 字，`feeds.py` 打分落库转换处 `[:140]` 硬截，卡片显示「…目前只是 al」
（alpha 截成 al）；被截掉的尾句正是「；套件内 AI 工具有生成额度订阅，规避 Adobe 专利仍未决」
——免费 / 付费限定，恰好是这批修复要保住的信息。该截断发生在打分转换处，早于展示核验，
所以核验看到的是截断后的文本；本轮实际没有拦下。conditions 也在核验输入中，
不能据此断言原则上无法发现问题。下面是假模型回归文案，只对齐长度和尾句位置，不是原新闻事实。

修法（不在打分转换阶段静默切掉条件）：
- 正常长度的 brief（含实测的 176 字）原样保留，不问断句、不切词；
- 只有超长异常（> _BRIEF_KEEP_MAX）才 fail-safe：丢掉 brief，让卡片回落已核验的 summary，
  不截在词中、不把收费条件丢一半；
- 展示核验（post_check）看到的是保留下来的完整 brief。
"""

from __future__ import annotations

import json

from CharTyr_MaiWork.maiwork import news_card
from CharTyr_MaiWork.maiwork.feeds import _BRIEF_KEEP_MAX

from fakes import FakeModelsQueue
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

# 线上 #1301 的真实形状：176 字，尾巴是收费 / 专利限定（脱敏后保留句式与长度）
_BRIEF_TAIL = "；套件内 AI 工具有生成额度订阅，规避 Adobe 专利仍未决。"
_BRIEF_176 = (
    "Adobe 发布七款创意套件新版本，核心绘图应用 Opus 5.5 用 Rust 重写，ArtCraft 走 clean-room 路线，"
    "一个月内做到功能对齐，支持 macOS、Windows 和 Linux，旧版仍会维护，"
    "新版本目前仍是 alpha，免费下载试用，官方在博客里做了说明"
    + _BRIEF_TAIL
)
_BRIEF_OVERLONG = "甲" * (_BRIEF_KEEP_MAX + 20)
# 250 字：超过旧的 post_check 提示词 200 字上限、但在安全上限内 —— 核验也必须看全
_BRIEF_250 = "丙" * (250 - len(_BRIEF_TAIL)) + _BRIEF_TAIL
_PUB = 1_790_000_000.0 - 86400

_NAMES = [
    "量子芯片新架构发布", "开源掌机固件更新", "本地大模型推理提速", "无线电爱好者大会",
    "云成本账单拆解", "新款机械键盘评测", "独立游戏节获奖名单", "卫星互联网新进展",
    "浏览器引擎重大更新", "天文摄影入门指南", "复古计算设备修复", "自动驾驶新规解读",
]


def _search_items(n: int, *, brief: str = "") -> list[dict]:
    return [
        {"title": _NAMES[i % len(_NAMES)], "url": f"https://site{i}.example.com/a",
         "summary": f"摘要{i}：两三句话讲清楚这件事。", "kind": "news",
         "published": 1_790_000_000.0 - 3600, "fetched": True, "quote": "原文依据", "paywall": False}
        for i in range(n)
    ]


def _score_one(reply_brief: str) -> FakeModelsQueue:
    return FakeModelsQueue(ready=True, replies=[_scores_json(_score(0, brief=reply_brief))])


def _prompt(models: FakeModelsQueue, purpose: str) -> str:
    for _role, messages, kwargs in models.calls:
        if str(kwargs.get("purpose") or "") == purpose:
            return "\n".join(str(m.get("content") or "") for m in messages)
    raise AssertionError(f"没调 {purpose}")


# ---------------------------------------------------------------- 打分转换：不静默切


def test_audit_brief_is_exactly_176_chars() -> None:
    """先钉住样例形状：176 字、尾句是限定条件（否则用例测不到线上那条）。"""
    assert len(_BRIEF_176) == 176
    assert _BRIEF_176.endswith(_BRIEF_TAIL)
    assert _BRIEF_176[:140] != _BRIEF_176, "140 截断确实会切掉尾巴（线上就是这么丢的）"


def test_score_keeps_176_char_brief_verbatim(tmp_path) -> None:
    """#1301：176 字的 brief 转换后必须一字不少，尾巴的收费限定还在。"""
    models = _score_one(_BRIEF_176)
    _store, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    cand = _search_items(1)[0]
    with _TimePatch():
        _run(feeds._score(GID, settings, [cand]))
    assert cand["brief"] == _BRIEF_176
    assert len(cand["brief"]) == 176
    assert cand["brief"].endswith(_BRIEF_TAIL), "免费 / 付费限定不许丢"
    assert not cand["brief"].endswith("al"), "不许截在词中间"


def test_score_keeps_brief_at_limit(tmp_path) -> None:
    """边界：正好 _BRIEF_KEEP_MAX 字照留。"""
    brief = "乙" * _BRIEF_KEEP_MAX
    models = _score_one(brief)
    _store, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    cand = _search_items(1)[0]
    with _TimePatch():
        _run(feeds._score(GID, settings, [cand]))
    assert cand["brief"] == brief


def test_overlong_brief_falls_back_to_verified_summary(tmp_path) -> None:
    """超长异常：fail-safe 丢掉 brief，卡片回落已核验的 summary；不是截一半。"""
    models = _score_one(_BRIEF_OVERLONG)
    _store, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    cand = _search_items(1)[0]
    with _TimePatch():
        _run(feeds._score(GID, settings, [cand]))
    assert cand["brief"] == "", "超长不硬截、也不用半截文本，整条回落到已核验摘要"
    text = news_card._card_text({"brief": cand["brief"], "summary": cand["summary"]})
    assert text == cand["summary"]
    assert "甲" not in text


def test_post_check_sees_brief_over_200_chars_in_full(tmp_path) -> None:
    """安全上限内的长 brief（250 字）核验也要看全：不许默默只核前 200 字再全量发出去。"""
    assert len(_BRIEF_250) == 250
    it = {
        "title": "某套件新版本发布", "url": "https://suite.example.com/v", "site": "suite.example.com",
        "summary": "某套件发布新版本，限时免费但只有指定型号不计费。", "quote": "免费仅限指定型号，限时。",
        "conditions": ["限时免费", "只有指定型号不计费"],
        "kind": "news", "published_ts": _PUB, "brief": _BRIEF_250,
        "post": {"body": "某套件发新版本了。", "reason": "", "refs": [], "audience": [], "keywords": []},
    }
    models = FakeModelsQueue(ready=True, replies=['{"unsupported": []}'])
    _store, _settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    with _TimePatch():
        _run(feeds._check_display(GID, [it]))
    prompt = _prompt(models, "feeds.post_check")
    assert it["brief"] in prompt, "250 字的 brief 必须整条进核验提示词"
    assert _BRIEF_TAIL in prompt, "尾巴上的限定条件核验必须看到"
    assert it["brief"] == _BRIEF_250, "核验过关就原样保留"


# ---------------------------------------------------------------- 全流程：核验看到的 + 入库 / 卡片取值


def _round_with_brief(tmp_path, brief: str) -> tuple:
    """真实 prepare_news 走一轮（1 条候选）：[定关注点, 挑, 打分, 写帖, 自检]。"""
    models = FakeModelsQueue(ready=True, replies=[
        _FOCUS_JSON,
        _scores_json(_score(0, brief=brief)),
        _posts_json(_post(0, _NAMES[0])),
        '{"unsupported": []}',
    ])
    from test_feeds_freshness import FakeWorkers

    store, settings, feeds, *_ = _make_feeds(
        tmp_path, models=models, workers=FakeWorkers(_ok_report({"items": _search_items(1)})))
    with _TimePatch():
        kept = _run(feeds.prepare_news(GID))
    return store, models, kept


def test_prepare_news_keeps_full_brief_end_to_end(tmp_path) -> None:
    """#1301 全链路：转换不截 → 自检看到完整条件 → 入库和卡片都是完整 176 字。"""
    store, models, kept = _round_with_brief(tmp_path, _BRIEF_176)
    assert kept == 1
    row = store.read().execute(
        "SELECT title, brief, summary FROM news_items WHERE group_id=? AND rejected=0", (GID,)
    ).fetchone()
    assert row is not None
    assert row["brief"] == _BRIEF_176, "入库阶段不许再切一刀"
    assert _BRIEF_TAIL in row["brief"]
    # 展示核验看到的是完整 brief（含尾句限定），不是 140 字版本
    prompt = _prompt(models, "feeds.post_check")
    assert _BRIEF_176 in prompt, "实际要展示的 brief 必须全量进入核验"
    assert _BRIEF_TAIL in prompt
    # 卡片取值：card_push 原样把 brief 交给 news_card；news_card 对 brief 不再截
    # （只有 brief 为空时才从 summary 按句截），176 字整条进卡片文字
    card_text = news_card._card_text({"brief": row["brief"], "summary": row["summary"]})
    assert card_text == _BRIEF_176
    assert _BRIEF_TAIL in card_text


def test_prepare_news_overlong_brief_stored_as_empty(tmp_path) -> None:
    """超长异常的兜底也要在全链路成立：入库 brief 为空，卡片用已核验摘要。"""
    store, _models, kept = _round_with_brief(tmp_path, _BRIEF_OVERLONG)
    assert kept == 1
    row = store.read().execute(
        "SELECT brief, summary FROM news_items WHERE group_id=? AND rejected=0", (GID,)
    ).fetchone()
    assert row["brief"] == ""
    text = news_card._card_text({"brief": row["brief"], "summary": row["summary"]})
    assert text.startswith(str(row["summary"])[:10])
    assert "甲" not in text
