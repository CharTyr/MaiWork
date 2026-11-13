"""News must quote an actual article body fetched in this verification run.

2026-11 零子 agent 改版：没有核验 / 找料子 agent——主流程代码自己 fetch_page
（群向 _open_candidates、个人向 Personal._collect Phase C，都走 verify_budget 账本
+ bind_pages 绑本轮真读到的正文），打分模型只写回 summary/quote/fetched，
第一道硬拒（代码）+ 打分后硬复核（模型写回的 quote 逐字再核）照原样把关。
"""
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

URL = "https://example.com/articles/local-deploy-tool"  # 深路径，不是栏目 / 列表页
TITLE = "开发者发布本地部署工具"
BODY = "该工具现已开源，允许本地部署。免费版每天限用二十次。"
QUOTE = "免费版每天限用二十次。"

GROUP_MARK = "feeds-collect:test-body"
FOCUS = [{"query": "本地部署工具",
          "searches": [{"q": "本地部署工具", "site": "", "news": True, "kind": "news"}]}]


def _item(**changes):
    item = dict(title=TITLE, url=URL, summary=BODY, kind="news", published=clock.now(),
                fetched=True, quote=QUOTE, paywall=False)
    item.update(changes)
    return item


def _real_tools(store, settings, html: str) -> Tools:
    """真 Tools + 内置工具，HTTP 用 MockTransport 喂给定 HTML（不碰公网）。"""
    tools = Tools(store)
    register_builtin(tools, search=None, profiles=FakeProfiles(),
                     http_transport=httpx.MockTransport(lambda request: httpx.Response(
                         200, headers={"content-type": "text/html"}, text=html)),
                     get_settings=lambda: settings, resolver=lambda host: ["93.184.216.34"])
    return tools


def _candidate(item: dict) -> dict:
    """「老格式」候选：_collect_two_phase 挑中后开原文前的样子（summary/quote 留空）。"""
    from CharTyr_MaiWork.maiwork.feeds import _normalize_url, _source_site

    url = str(item.get("url") or "")
    return {
        "title": str(item.get("title") or ""),
        "url": url,
        "url_key": _normalize_url(url),
        "site": _source_site(url),
        "kind": str(item.get("kind") or "news"),
        "summary": "",
        "quote": "",
        "conditions": [],
        "image_url": "",
        "fetched": False,
        "paywall": False,
        "published_ts": item.get("published"),
        "published_raw": item.get("published"),
        "date_basis": "search",
        "_hint_snippet": str(item.get("summary") or "")[:300],
    }


def _write_back(items: list[dict], src: dict) -> None:
    """模拟打分环节写回：summary/quote/fetched/paywall 都信模型（证据仍用代码绑的）。"""
    for it in items:
        if it.get("reject"):
            continue
        it["summary"] = str(src.get("summary") or "")
        it["quote"] = str(src.get("quote") or "")
        it["fetched"] = src.get("fetched", True)  # 可能是字符串「false」，要照旧被拒
        it["paywall"] = bool(src.get("paywall"))
        it.setdefault("_flags", {})["grounded"] = True


async def _collect(pipeline, tmp_path, item, html, *, no_open=False):
    """零子 agent 流水线：代码 fetch_page → 第一道（代码）→ 打分写回 → 模型侧复核。

    no_open=True 模拟「原文没打开过」（工具不装，候选没进打开环节）。
    """
    if pipeline == "group":
        store, settings, owner, *_ = _make_feeds(tmp_path, items=[])
        if not no_open:
            owner.set_tools(_real_tools(store, settings, html))
        else:
            owner._tools = None
        items = [_candidate(item)]
        await owner._open_candidates(GID, items, task_id=GROUP_MARK)
        owner._hard_reject_code(GID, settings, items, require_quote=False)
        _write_back(items, item)
        owner._hard_reject_model(items)
        assert verify_budget._status(GROUP_MARK) is None, "账本要随这轮关掉"
    else:
        store, settings, owner, *_ = _make_personal(tmp_path, items=[item])
        if not no_open:
            owner.set_tools(_real_tools(store, settings, html))
        else:
            owner._tools = None
        items = await owner._collect(GID, FOCUS, UID)
        owner._hard_reject_code(GID, items, require_quote=False)
        _write_back(items, item)
        owner._hard_reject_model(items)
        assert not [t for t in verify_budget._RUNS if t.startswith("personal-collect:")], \
            "账本要随这轮关掉"
    survivors = [c for c in items if not c.get("reject")]
    return items, survivors


class BodyWorkers:
    """Real local fetch handler + preset reporter, for stages still run via Workers."""
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
            self.fetch_result = await self.tools.call("fetch_page", {"url": self.fetched_url}, ToolContext(
                group_id=GID, task_id=self.mark, actor="test verifier", role="worker"))
        return WorkerReport(True, "补打开", data={"items": [dict(self.item)]})


def _wire_tools(store, settings, worker):
    worker.tools = _real_tools(store, settings, worker.html)


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
        opts["no_open"] = True
    elif case == "other_page":
        # 打开候选链接时服务端交的是另一篇文章：write_back 的 quote 对不上本轮正文
        html = f"<html><title>另一篇完全无关的文档</title><article><p>{"这是另一篇文章的正文，和候选标题没有任何关系。" * 20}</p></article></html>"
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
