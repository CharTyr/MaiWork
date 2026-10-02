"""线上巡检 2026-10-02：最后改写（写帖子）给正文添错，自检没拦住。

核实过的四类错：
- 相对日期：原文「9 月 29 日实装」，10 月 2 日的正文写「今天已经实装」；
- 来源拔高：IGN 经 Steam 新闻页转载，正文说成「Mojang 官方公告」；
- 漏掉限定：原文是没有真实模型的教学模拟，正文把「不需要 API key」安到正式产品上；
- 意思写反：原文「命中 fetch/require 就禁止运行」，正文写成「命中才放行」。
另外，标题只在括号里带几个汉字的外文长标题被当成「已是中文」，没翻译就进了卡片。

修法：写帖子和自检都带上今天日期、每条发布日期和来源网站，并点名这几类错；
自检本身出错时正文回落摘要（宁可朴素，不放没核对过的改写）；中文标题按汉字占比判。
"""

from __future__ import annotations

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.feeds import _looks_chinese
from CharTyr_MaiWork.maiwork.models import ModelError

from fakes import FakeModelsQueue
from test_feeds_freshness import GID, NOW, _TimePatch, _make_feeds, _run

_PUB = NOW - 3 * 86400


def _item(i: int = 0, **kw) -> dict:
    it = {
        "title": f"补丁热修{i}", "url": f"https://news.example.com/p{i}", "site": "news.example.com",
        "summary": "9 月 29 日实装的热修补丁。", "quote": "今日 9 月 29 日实装。",
        "kind": "news", "published_ts": _PUB, "published_raw": _PUB,
        "post": {"body": "今天已经实装了，赶紧去玩。", "reason": "", "refs": [], "audience": [], "keywords": []},
    }
    it.update(kw)
    return it


def _prompt(models: FakeModelsQueue, purpose: str) -> str:
    for _role, msgs, kw in models.calls:
        if kw.get("purpose") == purpose:
            return "\n".join(str(m.get("content") or "") for m in msgs)
    raise AssertionError(f"没调 {purpose}")


def test_check_prompt_has_dates_site_and_named_error_kinds(tmp_path) -> None:
    models = FakeModelsQueue(ready=True, replies=['{"unsupported": []}'])
    _store, _settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    with _TimePatch():
        _run(feeds._check_posts(GID, [_item()]))
    p = _prompt(models, "feeds.post_check")
    assert clock.bj(NOW).strftime("%Y-%m-%d") in p, "自检要知道今天是哪天"
    assert clock.bj(_PUB).strftime("%Y-%m-%d") in p, "自检要知道原文发布日期"
    assert "news.example.com" in p, "自检要知道来源网站"
    for kind in ("相对日期", "官方", "限定", "反"):
        assert kind in p, f"自检要点名「{kind}」这类错"


def test_check_flags_relative_date_falls_back_to_summary(tmp_path) -> None:
    models = FakeModelsQueue(ready=True, replies=['{"unsupported": [{"i": 0, "phrases": ["今天已经实装"]}]}'])
    _store, _settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    it = _item()
    with _TimePatch():
        _run(feeds._check_posts(GID, [it]))
    assert it["post"]["body"] == it["summary"]


def test_check_error_falls_back_to_summary(tmp_path) -> None:
    """自检本身失败（模型报错 / JSON 坏）：没核对过的改写不放出去，正文回落摘要。"""
    for reply in (ModelError("超时"), "这不是 JSON"):
        models = FakeModelsQueue(ready=True, replies=[reply])
        _store, _settings, feeds, *_ = _make_feeds(tmp_path / str(id(reply)), models=models)
        it = _item()
        with _TimePatch():
            _run(feeds._check_posts(GID, [it]))
        assert it["post"]["body"] == it["summary"]


def test_writer_prompt_has_today_published_date_and_rules(tmp_path) -> None:
    models = FakeModelsQueue(ready=True, replies=[])
    _store, _settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    it = _item()
    it.pop("post")
    packs = [{"item": it, "site": "news.example.com", "quotes": []}]
    with _TimePatch():
        p = "\n".join(_run(feeds._posts_prompt_lines(GID, packs)))
    assert clock.bj(NOW).strftime("%Y-%m-%d") in p
    assert clock.bj(_PUB).strftime("%Y-%m-%d") in p
    for kind in ("今天", "官方", "限定", "反"):
        assert kind in p


def test_mostly_foreign_title_is_not_chinese() -> None:
    assert not _looks_chinese(
        "INVESTIGATION: Poor Performance & Power Draw Issues Impacting AMD Radeon 6000 - 9000 Series GPUs"
        "（Warhammer 40,000: Darktide 官方论坛调查帖）")
    assert not _looks_chinese(
        "PREVIEW - Upcoming Balance Changes - Warhammer 40,000: Darktide（Fatshark 官方论坛公告）")
    # 中文为主、夹几个英文专有名词的照旧算中文
    for t in ("ChatGPT 推出工作区智能体 workspace agents", "Into the Void——暗潮灵能者配装",
              "OM System OM-3 三周长测（英文版） — Patrice Michellon", "新款开源 FPGA 开发板发布",
              "[Plugin] MaiWork - MaiBot的常驻Agent（官方插件仓库验证通过）"):
        assert _looks_chinese(t), t
