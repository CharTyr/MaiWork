"""群友实际看到的改写文字统一对原文核验（线上 2026-10 核实过的四个真实错误，脱敏后做成回归）。

1. 炉石补丁：官方论坛帖首楼只有一句「查看最新平衡更新」+「View Full Article」，后面是玩家回复；
   核验把玩家回复拼进 quote，写错了改动数值。→ 核验提示词：评论 / 回帖不能当事实、不能拼进 quote；
   页面只是引导时必须打开完整原文；数字 / 版本改动必须原文写明。
2. Mistral：核验摘要写明「页面未见 1M 上下文」，卡片短摘要 brief 却写「支持百万上下文」，
   自检只查帖子正文、从不查 brief。→ 自检把 brief 一起核，不过关置空（卡片回落摘要）。
3. 学生优惠：中文标题 title_zh 把「每月 69 卢比学生价」写成「免费解锁」，标题没被核验。
   → 自检也核 title_zh，不过关就删掉（保留原标题）。
4. 免费 TTS：核验保留了「fair use、必须选指定型号才不计费」，brief 写成「免费无限量调用」。
   → 核验交回 conditions（必须保留的限定条件），自检 / 写帖 / 打分提示词都列出来。
"""

from __future__ import annotations

import json
from typing import Any

from CharTyr_MaiWork.maiwork import news_card, news_recheck
from CharTyr_MaiWork.maiwork.feeds import _NEWS_OUTPUT_SCHEMA, _normalize_url, _parse_published, _source_site
from CharTyr_MaiWork.maiwork.models import ModelError
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.workers import WorkerReport

from fakes import FakeModelsQueue
from test_feeds_freshness import GID, NOW, _TimePatch, _make_feeds, _run

_PUB = NOW - 2 * 86400

_TTS_CONDITIONS = ["fair use 政策下免费", "必须选 S2.1 Pro free 型号才不计费", "限一段时间内"]


def _tts_item(**kw: Any) -> dict:
    it = {
        "title": "Free TTS API now available", "url": "https://tts.example.com/blog/free",
        "site": "tts.example.com",
        "summary": "某语音平台在 fair use 政策下开放免费调用，只有选 S2.1 Pro free 型号才不计费，限一段时间。",
        "quote": "Free under our fair use policy when you select the S2.1 Pro free model, for a limited time.",
        "conditions": list(_TTS_CONDITIONS),
        "kind": "news", "published_ts": _PUB, "published_raw": _PUB,
        "brief": "某语音平台的 TTS 接口现在免费无限量调用，开发者随便用。",
        "title_zh": "某语音平台 TTS 接口限时免费",
        "post": {"body": "某语音平台在 fair use 下开放免费调用，选 S2.1 Pro free 型号不计费。",
                 "reason": "", "refs": [], "audience": [], "keywords": []},
    }
    it.update(kw)
    return it


def _plain_item(i: int = 1, **kw: Any) -> dict:
    it = {
        "title": f"开源 FPGA 学习板{i}", "url": f"https://board.example.com/p{i}", "site": "board.example.com",
        "summary": "一块新的开源 FPGA 学习板上架，资料齐全。", "quote": "资料齐全，现已上架。",
        "kind": "news", "published_ts": _PUB, "published_raw": _PUB,
        "brief": "一块开源 FPGA 学习板上架，资料齐全。",
        "post": {"body": "新的开源 FPGA 学习板上架了，资料挺全。", "reason": "", "refs": [], "audience": [], "keywords": []},
    }
    it.update(kw)
    return it


def _prompt(models: FakeModelsQueue, purpose: str) -> str:
    for _role, msgs, kw in models.calls:
        if kw.get("purpose") == purpose:
            return "\n".join(str(m.get("content") or "") for m in msgs)
    raise AssertionError(f"没调 {purpose}")


def _check(tmp_path, reply: Any, items: list[dict]) -> FakeModelsQueue:
    models = FakeModelsQueue(ready=True, replies=[reply])
    _store, _settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    with _TimePatch():
        _run(feeds._check_display(GID, items))
    return models


# ---------------------------------------------------------------- 自检核全部展示文字


def test_check_prompt_lists_title_brief_body_and_conditions(tmp_path) -> None:
    it = _tts_item()
    models = _check(tmp_path, '{"unsupported": []}', [it])
    p = _prompt(models, "feeds.post_check")
    assert it["title_zh"] in p, "自检要核中文标题"
    assert it["brief"] in p, "自检要核卡片短摘要"
    assert it["post"]["body"] in p, "自检要核帖子正文"
    for c in _TTS_CONDITIONS:
        assert c in p, "自检要拿 conditions 当依据"
    for kw in ("免费", "限时", "同批"):
        assert kw in p, f"自检要点名「{kw}」这类错"
    assert '"field"' in p
    # 都过关：原样保留
    assert it["brief"] and it["title_zh"] and it["post"]["body"] != it["summary"]


def test_brief_with_excluded_fact_is_cleared_and_card_falls_back_to_summary(tmp_path) -> None:
    """Mistral：核验写明页面没有 1M 上下文，brief 却写了 → brief 置空，卡片用 summary。"""
    it = _plain_item(
        title="Mistral releases new model", summary="Mistral 发布新模型；页面未见 1M 上下文的说法。",
        quote="Today we release our new model.", brief="Mistral 新模型支持百万上下文，性能大涨。",
    )
    it.pop("post")  # 只有 brief 是改写（正文没写）也要进核验
    reply = json.dumps({"unsupported": [{"i": 0, "field": "brief", "phrases": ["支持百万上下文"]}]}, ensure_ascii=False)
    models = _check(tmp_path, reply, [it])
    assert "支持百万上下文" in _prompt(models, "feeds.post_check")
    assert it["brief"] == ""
    text = news_card._card_text({"brief": it["brief"], "summary": it["summary"]})
    assert "百万上下文" not in text and text.startswith("Mistral 发布新模型")


def test_brief_contradicting_conditions_is_cleared_others_kept(tmp_path) -> None:
    """免费 TTS：brief 把「fair use、指定型号」写成「免费无限量」→ 只清 brief，标题和正文过关照留。"""
    it = _tts_item()
    reply = json.dumps({"unsupported": [{"i": 0, "field": "brief", "phrases": ["免费无限量调用"]}]}, ensure_ascii=False)
    _check(tmp_path, reply, [it])
    assert it["brief"] == ""
    assert it["title_zh"] == "某语音平台 TTS 接口限时免费"
    assert it["post"]["body"] != it["summary"]


def test_title_zh_overpromising_is_removed(tmp_path) -> None:
    """学生优惠：标题写「免费解锁」，核验是学生价 → 删 title_zh，保留原标题。"""
    it = _plain_item(
        title="Student ID unlocks Gemini, Spotify and Apple Music deals",
        summary="学生凭学生证可免费用 Gemini；Apple Music 是每月 69 卢比的学生价。",
        quote="Apple Music student plan at Rs 69 per month.",
        title_zh="学生ID可免费解锁 Gemini、Spotify、Apple Music",
    )
    reply = json.dumps({"unsupported": [{"i": 0, "field": "title", "phrases": ["免费解锁"]}]}, ensure_ascii=False)
    _check(tmp_path, reply, [it])
    assert "title_zh" not in it
    assert it["title"] == "Student ID unlocks Gemini, Spotify and Apple Music deals"
    assert it["brief"] and it["post"]["body"] != it["summary"]


def test_old_format_without_field_still_means_body(tmp_path) -> None:
    it = _tts_item()
    reply = json.dumps({"unsupported": [{"i": 0, "phrases": ["某个说法"]}]}, ensure_ascii=False)
    _check(tmp_path, reply, [it])
    assert it["post"]["body"] == it["summary"]
    assert it["brief"] and it["title_zh"], "旧格式只回落正文"


def test_check_failure_falls_back_all_rewrites(tmp_path) -> None:
    for n, reply in enumerate((ModelError("超时"), "这不是 JSON")):
        it = _tts_item()
        _check(tmp_path / str(n), reply, [it])
        assert it["post"]["body"] == it["summary"]
        assert "title_zh" not in it
        assert it["brief"] == ""


def test_item_without_any_rewrite_skips_model(tmp_path) -> None:
    it = _plain_item(brief="")
    it["post"]["body"] = it["summary"]
    models = FakeModelsQueue(ready=True, replies=[])
    _store, _settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    with _TimePatch():
        _run(feeds._check_display(GID, [it]))
    assert not any(kw.get("purpose") == "feeds.post_check" for _r, _m, kw in models.calls)


# ---------------------------------------------------------------- conditions 进 item、进各提示词


def test_schema_has_conditions() -> None:
    props = _NEWS_OUTPUT_SCHEMA["properties"]["items"]["items"]["properties"]
    assert props["conditions"]["type"] == "array"
    assert news_recheck.RECHECK_SCHEMA["properties"]["items"]["items"]["properties"]["conditions"]["type"] == "array"


def test_verify_result_conditions_are_parsed_and_cleaned(tmp_path) -> None:
    _store, _settings, feeds, *_ = _make_feeds(tmp_path)
    raw = [
        {"title": "A", "url": "https://a.example.com/x", "summary": "s", "kind": "news",
         "fetched": True, "quote": "q", "paywall": False,
         "conditions": ["  只限学生 ", "", "x" * 300, "限印度地区", "限时", "需选指定型号", "第六条", 42]},
        {"title": "B", "url": "https://b.example.com/y", "summary": "s", "kind": "news",
         "fetched": True, "quote": "q", "paywall": False},
    ]
    a, b = feeds._parse_news_items(raw, results=True)
    assert a["conditions"][0] == "只限学生"
    assert len(a["conditions"]) == 5
    assert all(c and len(c) <= 80 for c in a["conditions"])
    assert b["conditions"] == []


def test_score_prompt_lists_conditions_and_brief_rules(tmp_path) -> None:
    models = FakeModelsQueue(ready=True, replies=[json.dumps({"scores": [{"i": 0, "info": 3}]})])
    _store, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    cand = _tts_item()
    for k in ("brief", "title_zh", "post"):
        cand.pop(k)
    with _TimePatch():
        _run(feeds._score(GID, settings, [cand]))
    p = _prompt(models, "feeds.score")
    for c in _TTS_CONDITIONS:
        assert c in p
    assert "同批其他条目" in p, "brief 只能用这一条自己的依据"


def test_writer_prompt_lists_conditions(tmp_path) -> None:
    models = FakeModelsQueue(ready=True, replies=[])
    _store, _settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    it = _tts_item()
    it.pop("post")
    packs = [{"item": it, "site": "tts.example.com", "quotes": []}]
    with _TimePatch():
        p = "\n".join(_run(feeds._posts_prompt_lines(GID, packs)))
    assert "必须保留的限定" in p
    for c in _TTS_CONDITIONS:
        assert c in p
    assert "扩大" in p, "title_zh 不许扩大承诺"


# ---------------------------------------------------------------- 核验提示词：来源身份 + 完整原文


def _assert_source_rules(text: str) -> None:
    assert "评论" in text and "读者" in text and "不能当事实" in text
    assert "View Full Article" in text and "完整原文" in text
    assert "旧值→新值" in text
    assert "conditions" in text


def test_verify_brief_has_source_identity_rules(tmp_path) -> None:
    _store, _settings, feeds, *_ = _make_feeds(tmp_path)
    cand = {"title": "Hearthstone balance update", "url": "https://forum.example.com/t/1", "snippet": "查看最新平衡更新"}
    with _TimePatch():
        brief = feeds._verify_brief(GID, [(cand, "news", "")])
    _assert_source_rules(brief)
    assert "镜像" in brief, "仍不许搜镜像 / 存档站"


def test_recheck_brief_has_source_identity_rules() -> None:
    cands = [{"title": "T", "url": "https://forum.example.com/t/1", "summary": "s"}]
    _assert_source_rules(news_recheck._brief(cands, [0]))


class _OneShotWorkers:
    def __init__(self, store: Store, data: dict, opened: list[str]) -> None:
        self.store, self.data, self.opened = store, data, opened
        self.briefs: list[str] = []

    async def run(self, brief: str, **kwargs: Any) -> Any:
        self.briefs.append(brief)
        tid = str(kwargs.get("task_id") or "")
        with self.store.tx() as conn:
            for url in self.opened:
                conn.execute(
                    "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ok)"
                    " VALUES (?, ?, ?, '子 agent', 'fetch_page', ?, '取到正文 500 字', 1)",
                    (NOW, GID, tid, url),
                )
        return WorkerReport(ok=True, summary="好了", data=self.data, evidence=[], steps=2)


def test_recheck_result_conditions_are_merged(tmp_path) -> None:
    store = Store(tmp_path / "t.db")
    store.migrate()
    url = "https://tts.example.com/blog/free"
    cands = [{"title": "Free TTS", "url": url, "summary": "旧摘要", "fetched": False, "quote": ""}]
    data = {"items": [{"index": 0, "verdict": "keep", "url": url, "summary": "新摘要", "quote": "原文一句",
                       "published": "", "conditions": ["fair use 政策下免费", " ", "必须选指定型号"]}]}
    workers = _OneShotWorkers(store, data, [url])
    with _TimePatch():
        kept = _run(news_recheck.recheck(
            store, workers, GID, cands, collect_mark="c-mark", recheck_mark="r-mark",
            parse_published=_parse_published, normalize_url=_normalize_url, site_of=_source_site,
        ))
    assert kept == 1
    assert cands[0]["conditions"] == ["fair use 政策下免费", "必须选指定型号"]


def test_personal_collect_brief_rules_and_conditions(tmp_path) -> None:
    from test_feeds import FakeWorkers, _ok_report
    from test_personal import GID as PGID, UID, _make_personal, _time_patch

    items = {"items": [{"title": "新开源 FPGA 学习板上架", "url": "https://example.com/board",
                        "summary": "一块新的 FPGA 学习板。", "kind": "news", "published": "",
                        "fetched": True, "quote": "原文一句", "paywall": False,
                        "conditions": ["仅限预售"]}]}
    workers = FakeWorkers(_ok_report(items))
    _store, _s, personal, *_ = _make_personal(tmp_path, workers=workers)
    with _time_patch():
        got = _run(personal._collect(PGID, [{"query": "FPGA", "why": "在做"}], UID))
    _assert_source_rules(workers.calls[-1]["brief"])
    schema = workers.calls[-1]["output_schema"]
    assert "conditions" in schema["properties"]["items"]["items"]["properties"]
    assert got and got[0]["conditions"] == ["仅限预售"]
