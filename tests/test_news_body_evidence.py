"""News must quote an actual article body fetched in this verification run."""
from __future__ import annotations

import httpx
import pytest

from CharTyr_MaiWork.maiwork import clock, verify_budget
from CharTyr_MaiWork.maiwork.reader import JinaReader
from CharTyr_MaiWork.maiwork.tools import ToolContext, Tools
from CharTyr_MaiWork.maiwork.tools_builtin import register_builtin
from CharTyr_MaiWork.maiwork.workers import WorkerReport
from fakes import FakeModelsQueue, FakeProfiles
from test_feeds_quality import GID, _make_feeds, _score, _scores_json
from test_personal import UID, _make_personal

URL = "https://example.com/article"
TITLE = "开发者发布本地部署工具"
BODY = "该工具现已开源，允许本地部署。免费版每天限用二十次。"
QUOTE = "免费版每天限用二十次。"


def _item(**changes):
    item = dict(title=TITLE, url=URL, summary=BODY, kind="news", published=clock.now(),
                fetched=True, quote=QUOTE, paywall=False)
    item.update(changes)
    return item


class BodyWorkers:
    """Real local fetch handler + preset verifier, without a model or public network."""
    def __init__(self, item, html, *, fetched_url=URL, fetch=True):
        self.item = item
        self.html = html
        self.fetched_url = fetched_url
        self.fetch = fetch
        self.tools = None
        self.mark = ""
        self.fetch_result = None

    async def run(self, brief, **kwargs):
        self.mark = kwargs["task_id"]
        if self.fetch:
            # Headline/navigation-only pages may legitimately fail the fetch tool;
            # the model can still claim fetched=true, and the gate must reject it.
            self.fetch_result = await self.tools.call("fetch_page", {"url": self.fetched_url}, ToolContext(
                group_id=GID, task_id=self.mark, actor="test verifier", role="worker"))
        return WorkerReport(True, "核验", data={"items": [dict(self.item)]})


def _wire_tools(store, settings, worker):
    worker.tools = Tools(store)
    register_builtin(worker.tools, search=None, profiles=FakeProfiles(),
                     http_transport=httpx.MockTransport(lambda request: httpx.Response(
                         200, headers={"content-type": "text/html"}, text=worker.html)),
                     get_settings=lambda: settings, resolver=lambda host: ["93.184.216.34"])


async def _collect(pipeline, tmp_path, item, html, **worker_opts):
    worker = BodyWorkers(item, html, **worker_opts)
    if pipeline == "group":
        store, settings, owner, *_ = _make_feeds(tmp_path, items=[], workers=worker)
        _wire_tools(store, settings, worker)
        items, _ = await owner._verify_batch(GID, [(dict(item), "news", "核验")],
                                          "feeds-verify:test:body", clock.now() + 60)
        survivors = owner._hard_reject_code(GID, settings, items)
    else:
        store, settings, owner, *_ = _make_personal(tmp_path, workers=worker)
        _wire_tools(store, settings, worker)
        items = await owner._collect(GID, [], UID)
        survivors = owner._hard_reject_code(GID, items)
    assert verify_budget._status(worker.mark) is None
    if worker.fetch and "<article>" in html:
        assert worker.fetch_result.ok, worker.fetch_result.error
    return items, survivors


@pytest.mark.asyncio
@pytest.mark.parametrize("pipeline", ["group", "personal"])
@pytest.mark.parametrize("case", ["headline", "fabricated", "navigation", "unfetched", "other_page",
                                  "wrong_type", "only_title_admission", "clipped_tail"])
async def test_unsubstantiated_evidence_rejected(tmp_path, pipeline, case):
    item = _item()
    html = f"<html><title>{TITLE}</title><article><p>{BODY}</p></article></html>"
    opts = {}
    if case == "headline":
        item["quote"] = TITLE
        html = f"<html><title>{TITLE}</title><h1>{TITLE}</h1><nav>" + "首页 社区 商店 支持 登录 " * 100 + "</nav></html>"
    elif case == "fabricated":
        item["quote"] = "该工具完全免费，不限制每天的使用次数。"
    elif case == "navigation":
        item["quote"] = "首页 社区 商店 支持 登录"
        html = f"<html><title>{TITLE}</title><nav>{item['quote']}</nav><h1>{TITLE}</h1></html>"
    elif case == "unfetched":
        opts["fetch"] = False
    elif case == "other_page":
        opts["fetched_url"] = "https://example.com/unrelated"
    elif case == "wrong_type":
        item["fetched"] = "false"
    elif case == "only_title_admission":
        item["summary"] = "仅根据标题生成摘要，未读取正文。"
    elif case == "clipped_tail":
        html = f"<html><title>{TITLE}</title><article><p>" + "这是一段其他事实的背景介绍。" * 1000 + BODY + "</p></article></html>"
    items, survivors = await _collect(pipeline, tmp_path, item, html, **opts)
    assert items, "rejected candidates must remain available for the admin audit"
    assert survivors == [], case
    assert items[0]["reject"][0] == "hard"


@pytest.mark.asyncio
@pytest.mark.parametrize("pipeline", ["group", "personal"])
async def test_concise_actual_article_remains_eligible(tmp_path, pipeline):
    html = f"<html><title>{TITLE}</title><article><p>{BODY}</p></article></html>"
    items, survivors = await _collect(pipeline, tmp_path, _item(), html)
    assert survivors == items
    assert not items[0].get("reject")


@pytest.mark.asyncio
@pytest.mark.parametrize("grounded", ["false", 1, None])
@pytest.mark.parametrize("pipeline", ["group", "personal"])
async def test_grounded_must_be_literal_true(tmp_path, pipeline, grounded):
    score = _score(0)
    if grounded is None:
        score.pop("grounded")
    else:
        score["grounded"] = grounded
    models = FakeModelsQueue(ready=True, replies=[_scores_json(score)])
    if pipeline == "group":
        _store, _settings, owner, *_ = _make_feeds(tmp_path, items=[], models=models)
        candidates = [_item()]
        await owner._score(GID, [], candidates)
        owner._hard_reject_model(candidates)
    else:
        _store, _settings, owner, *_ = _make_personal(tmp_path, models=models)
        candidates = [_item()]
        await owner._score(GID, "测试成员", {}, candidates)
        owner._hard_reject_model(candidates)
    assert candidates[0].get("reject"), "missing or coerced grounded is not approval"


class RecheckWorkers(BodyWorkers):
    """Real fetch_page under the recheck mark, then a preset keep verdict."""
    def __init__(self, verdict, html, **opts):
        super().__init__({}, html, **opts)
        self.verdict = verdict

    async def run(self, brief, **kwargs):
        self.mark = kwargs["task_id"]
        if self.fetch:
            self.fetch_result = await self.tools.call("fetch_page", {"url": self.fetched_url}, ToolContext(
                group_id=GID, task_id=self.mark, actor="test recheck", role="worker"))
        return WorkerReport(True, "补打开", data={"items": [dict(self.verdict)]})


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["actual_body", "rss_summary_quote", "not_opened"])
async def test_rss_recheck_binds_same_round_body(tmp_path, case):
    from CharTyr_MaiWork.maiwork import news_recheck
    from CharTyr_MaiWork.maiwork.feeds import _normalize_url, _parse_published, _source_site

    html = f"<html><title>{TITLE}</title><article><p>{BODY}</p></article></html>"
    verdict = {"index": 0, "verdict": "keep", "url": URL, "summary": BODY, "quote": QUOTE}
    opts = {}
    if case == "rss_summary_quote":
        verdict["quote"] = "RSS 源里写的：完全免费。"
    elif case == "not_opened":
        opts["fetch"] = False
    worker = RecheckWorkers(verdict, html, **opts)
    store, settings, owner, *_ = _make_feeds(tmp_path, items=[], workers=worker)
    _wire_tools(store, settings, worker)
    rss = _item(quote="RSS 源里写的：完全免费。", from_rss=True, url_key=_normalize_url(URL), site=_source_site(URL))
    # A model/RSS payload cannot smuggle code-owned evidence through the candidate dict.
    rss["_source_body"] = "RSS 源里写的：完全免费。"
    candidates = [rss]
    await news_recheck.recheck(store, worker, GID, candidates, collect_mark="feeds-collect:t",
                               recheck_mark="feeds-recheck:t", parse_published=_parse_published,
                               normalize_url=_normalize_url, site_of=_source_site)
    assert verify_budget._status("feeds-recheck:t") is None, "recheck run must be closed"
    survivors = owner._hard_reject_code(GID, settings, candidates)
    if case == "actual_body":
        assert survivors == candidates
        assert candidates[0]["_source_body"] == BODY
    else:
        assert survivors == []
        assert candidates[0]["reject"][0] == "hard"


@pytest.mark.asyncio
async def test_parsed_model_output_cannot_inject_evidence(tmp_path):
    store, settings, owner, *_ = _make_feeds(tmp_path, items=[])
    page = verify_budget.FetchedPage(URL, URL, TITLE, BODY)
    raw = dict(_item(), _fetched_evidence=page, _source_body=BODY)
    items = owner._parse_news_items([raw], results=True)
    assert "_fetched_evidence" not in items[0] and "_source_body" not in items[0]
    assert owner._hard_reject_code(GID, settings, items) == []


@pytest.mark.asyncio
async def test_fetch_page_without_open_run_is_not_budgeted_or_recorded(tmp_path):
    html = f"<html><title>{TITLE}</title><article><p>{BODY}</p></article></html>"
    worker = BodyWorkers(_item(), html)
    store, settings, *_ = _make_feeds(tmp_path, items=[], workers=worker)
    _wire_tools(store, settings, worker)
    for _ in range(10):  # far above any verify cap
        result = await worker.tools.call("fetch_page", {"url": URL}, ToolContext(
            group_id=GID, task_id="task:ordinary-123", actor="executor", role="worker"))
        assert result.ok, result.error
    assert verify_budget._status("task:ordinary-123") is None
    assert "task:ordinary-123" not in verify_budget._PAGES


@pytest.mark.asyncio
async def test_reader_preserves_separate_bounded_actual_body():
    from CharTyr_MaiWork.maiwork.config import load_settings
    settings, _ = load_settings({})
    long_body = BODY * 1000
    payload = {"data": {"title": TITLE, "content": long_body, "url": URL,
                        "publishedTime": "2026-10-13T12:00:00Z"}}
    reader = JinaReader(lambda: settings, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)))
    result = await reader.read(URL)
    assert result.ok
    assert result.body
    assert result.body in result.text
    assert TITLE not in result.body
    assert "发布时间" not in result.body
    assert len(result.body) < 8000


@pytest.mark.asyncio
async def test_reader_accepts_concise_body_below_sixty_chars():
    from CharTyr_MaiWork.maiwork.config import load_settings
    settings, _ = load_settings({})
    payload = {"data": {"title": TITLE, "content": BODY, "url": URL}}
    reader = JinaReader(lambda: settings, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)))
    result = await reader.read(URL)
    assert result.ok, result.reason


# ----------------------------------------------------------------------
# 2026-10-14 用户同意放宽：引文比对忽略标点 / 空格 / 全半角，文字和数字本身仍须一字不差
# ----------------------------------------------------------------------

_LOOSE_BODY = "该工具现已开源，允许本地部署。免费版每天限用 20 次（专业版不限）。"


def _bound(quote: str, body: str = _LOOSE_BODY) -> dict:
    page = verify_budget.FetchedPage(URL, URL, TITLE, body)
    item = _item(quote=quote)
    item["_fetched_evidence"] = page
    item["_source_body"] = verify_budget.article_body(body, TITLE)
    return item


@pytest.mark.parametrize("quote", [
    "免费版每天限用20次",                 # 少了空格和句末标点
    "免费版每天限用 20 次。",             # 句号代替括号
    "免费版每天限用２０次（专业版不限）",  # 全角数字
    "免费版，每天限用 20 次",             # 多了一个逗号
    "免费版每天限用 20 次(专业版不限)",    # 半角括号
    "“免费版每天限用 20 次”",             # 模型自己加了引号
])
def test_punctuation_and_width_drift_still_matches(quote: str) -> None:
    assert verify_budget.evidence_problem(_bound(quote)) == ""


@pytest.mark.parametrize("quote", [
    "免费版每天限用 30 次",      # 数字不同
    "免费版每天限用 2 次",       # 少一位数字
    "付费版每天限用 20 次",      # 字不同
    "免费版每天可用 20 次以上",  # 多出正文没有的说法
])
def test_changed_words_or_numbers_still_rejected(quote: str) -> None:
    assert verify_budget.evidence_problem(_bound(quote)) == "原文依据不在本轮读到的正文中"


def test_punctuation_only_quote_does_not_pass_min_length() -> None:
    assert verify_budget.evidence_problem(_bound("，。！（）“”")) != ""


@pytest.mark.parametrize("actual,wrong", [("20%", "20"), ("1-5", "15"), ("1/5", "15"), ("-20", "20")])
def test_numeric_meaning_punctuation_cannot_disappear(actual, wrong) -> None:
    body = f"官方说明每天允许使用{actual}次，专业版另外提供升级服务。"
    assert verify_budget.evidence_problem(_bound(f"每天允许使用{wrong}次", body)) != ""


def test_number_boundary_not_glued_by_removed_punctuation() -> None:
    # 去掉标点后「1.5」与「15」不能互相冒充（正文写 1.5 万，引文写 15 万）
    body = "官方称首批售出 1.5 万台，后续再补货，价格维持不变。"
    assert verify_budget.evidence_problem(_bound("首批售出15万台", body)) != ""
    assert verify_budget.evidence_problem(_bound("首批售出1.5万台", body)) == ""
