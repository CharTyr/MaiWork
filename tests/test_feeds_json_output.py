"""Complete outer JSON fences only; no repair of malformed content."""
import json
import pytest
from CharTyr_MaiWork.maiwork import feeds as feeds_module
from fakes import FakeModelsQueue
from test_feeds_freshness import GID, _TimePatch, _make_feeds, _run, _FOCUS_JSON
from test_feeds_score_chunks import _cands, _reply_for

@pytest.mark.parametrize("wrapper", ["{}", " \n{}\n ", "```json\n{}\n```", "```\n{}\n```", " ```JSON\r\n{}\r\n``` "])
@pytest.mark.parametrize("payload", [{"scores": []}, [{"i": 0}], {"text": "inner ``` and {braces}"}])
def test_complete_outer_fence(wrapper, payload):
    assert feeds_module._parse_model_json(wrapper.format(json.dumps(payload))) == payload

@pytest.mark.parametrize("raw", ["", "answer:\n```json\n{}\n```", "```json\n{}\n```\nexplanation", "```json\n{}", "```python\n{}\n```", '```json\n{"scores": [\n```', "{} {}", "```json\n{}\n```\n```json\n{}\n```"])
def test_rejects_prose_truncation_multiple_answers(raw):
    with pytest.raises(ValueError):
        feeds_module._parse_model_json(raw)

@pytest.mark.parametrize("label", ["json", ""])
def test_fenced_score_needs_one_call(tmp_path, label):
    models = FakeModelsQueue(ready=True, replies=[f"```{label}\n{_reply_for(range(3))}\n```"])
    _, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    cands = _cands(3)
    with _TimePatch():
        _run(feeds._score(GID, settings, cands))
    assert len(models.calls) == 1
    assert all(c["scores"]["avg"] > 0 and "reject" not in c for c in cands)
    assert models.calls[0][2]["timeout"] == 240
    assert models.calls[0][2]["retries"] == 0

def test_wrong_candidate_still_rejected(tmp_path):
    reply = f"```json\n{_reply_for([99])}\n```"
    models = FakeModelsQueue(ready=True, replies=[reply, reply])
    _, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    with _TimePatch(), pytest.raises(ValueError, match="一条都没对上"):
        _run(feeds._score(GID, settings, _cands(1)))
    assert len(models.calls) == 2

def test_focus_fence_and_no_transport_retry(tmp_path):
    models = FakeModelsQueue(ready=True, replies=[f"```json\n{_FOCUS_JSON}\n```"])
    _, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    with _TimePatch():
        assert _run(feeds._plan_focus(GID, settings))
    assert models.calls[0][2]["retries"] == 0

def test_pick_fence_and_no_transport_retry(tmp_path):
    models = FakeModelsQueue(ready=True, replies=['```json\n{"picks": [{"i": 0, "kind": "news", "hook": "真实发布"}]}\n```'])
    _, _, feeds, *_ = _make_feeds(tmp_path, models=models)
    picked, ok = _run(feeds._pick(GID, [{"query": "开源"}], _cands(2)))
    assert ok and len(picked) == 1
    assert picked[0][0]["title"] == "候选标题00号"
    assert models.calls[0][2]["retries"] == 0

def test_bad_json_still_gets_one_content_retry(tmp_path):
    models = FakeModelsQueue(ready=True, replies=['```json\n{"scores": [\n```', _reply_for([0])])
    _, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
    cands = _cands(1)
    with _TimePatch():
        _run(feeds._score(GID, settings, cands))
    assert len(models.calls) == 2 and cands[0]["scores"]["avg"] > 0


def test_complete_news_pipeline_accepts_fences_without_losing_post(tmp_path):
    from test_feeds_freshness import FakeWorkers, _ok_report, _cand, _score, _scores_json, _post, _posts_json
    candidate = _cand(0)
    body = "这条新架构已经发布，群里聊过这个方向。"
    replies = [_FOCUS_JSON, _scores_json(_score(0)), _posts_json(_post(0, candidate["title"], body)), '{"unsupported": []}']
    models = FakeModelsQueue(ready=True, replies=[f"```json\n{r}\n```" for r in replies])
    store, _, feeds, *_ = _make_feeds(tmp_path, models=models, workers=FakeWorkers(_ok_report({"items": [candidate]})))
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 1
    row = store.read().execute("SELECT body,rejected FROM news_items").fetchone()
    assert row["body"] == body and row["rejected"] == 0
    purposes = [kw.get("purpose") for _, _, kw in models.calls]
    assert "feeds.post_check" in purposes


@pytest.mark.asyncio
async def test_news_override_skips_endpoint_retries_but_keeps_backup(tmp_path):
    from test_models import FakeEndpoint, _make_models, _usage_rows
    ep = FakeEndpoint({"m1": [{"kind": "timeout"}], "m-bak": [{"kind": "ok", "content": "备用回答"}]})
    store, _, models = _make_models(tmp_path, {"models": {"base_url": "https://a.test/v1", "api_key": "fake", "main": "m1", "main_backup": "m-bak", "worker": "m1", "worker_backup": "m-bak", "retries": 5}}, ep)
    try:
        result = await models.chat(agent="news", messages=[{"role": "user", "content": "x"}], retries=0, purpose="feeds.focus")
        assert result.text == "备用回答"
        assert [(r["model"], r["ok"]) for r in _usage_rows(store)] == [("m1", 0), ("m-bak", 1)]
    finally:
        await models.close()
