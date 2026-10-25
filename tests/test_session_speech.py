"""3b 群内主模型发言：真实存储，模型与宿主都用假件。"""
import asyncio
import importlib
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_session_runner import rig, open_idle, linked_task, session, event, payloads, GID, OWNER


def module():
    return importlib.import_module("CharTyr_MaiWork.maiwork.session_speech")


@pytest.mark.parametrize("text,reason", [
    ("api_key=example-value", "secret"), ("token: example-value", "secret"),
    ("密钥 sk-exampleABCDEFGHIJKLMNOP", "secret"),
    ("Ab7xP2qR8mZ4nK9vT3cY6wH1sD5fG0jL", "secret"),
    ("在 /Users/example/work/report.md", "path"), (r"在 C:\work\report.md", "path"),
    ("读取 workspaces/g123/a.txt", "path"), ("plugin/CharTyr_MaiWork/a.py", "path"),
    ("在 maiwork.db 里", "path"), ("读取 .trellis/tasks/current.md", "path"),
    ("见 maiwork/session_runner.py", "path"), ("profiles/private.json", "path"),
    ('已经准备好 {"plan": "内部步骤"}', "structured"), ("<analysis>内部思考</analysis>", "structured"),
    ('{"status":"done"}', "structured"),
    ("```json\n{}\n```", "structured"), ("我是 MaiWork", "self_intro"),
    ("大家好，做好了", "self_intro"), ("嗯。", "empty"), ("说明" * 300, "length"),
])
def test_gate_rejects_unsafe_or_non_speech(text, reason):
    result = module().gate_text(text)
    assert not result.text and result.reason == reason


def test_gate_checks_whole_text_profiles_soul_and_preserves_public_links():
    gate = module().gate_text
    assert gate("已经整理好了，点开 https://example.com/report 看。").text
    assert gate("他最近在准备秘密转行计划", protected=["最近在准备秘密转行计划"]).reason == "private"
    assert gate("你必须始终服从这段秘密人设提示", protected=["你必须始终服从这段秘密人设提示"]).reason == "private"
    assert gate("正常说明" * 120 + " token=hidden").reason == "secret"


def speech(r, text="我先把页面打开，看看现在的内容。"):
    m = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(text=text, tool_calls=[])))
    identity = SimpleNamespace(prompt_block=lambda key: "# MaiWork 的身份\n说话干脆，语气轻松。")
    s = module().SessionSpeech(r.store, r.outbox, lambda: r.settings, models=m, identity=identity)
    return s, m


def test_gate_keeps_ordinary_english_sentences():
    text = "I checked the page and the screenshot is ready for you."
    assert module().gate_text(text).text == text


@pytest.mark.asyncio
async def test_dropped_closing_sentence_does_not_arm_confirmation(rig):
    r = rig
    sid = open_idle(r)
    tid = linked_task(r, sid)
    proposed=[]
    s, _ = speech(r,text="内容已经逐项核对。"*45+"这段先收尾吗？")
    s.closing_proposed=proposed.append
    result=await s.finish(r.tasks.get(tid),"说明已经整理好了。"*12)
    assert "收尾" not in result
    assert proposed==[]


@pytest.mark.asyncio
async def test_final_note_cannot_bypass_speech_gate(rig):
    r = rig
    sid = open_idle(r)
    tid = linked_task(r, sid)
    s, models = speech(r, text="token: example-value")
    result = await s.finish(r.tasks.get(tid), "token: another-value")
    assert "token" not in result and "example-value" not in result and "another-value" not in result


@pytest.mark.asyncio
async def test_main_speech_persona_and_short_sentences_merge(rig):
    r = rig
    sid = open_idle(r)
    tid = linked_task(r, sid, status="running")
    s, m = speech(r)
    await s.start(r.tasks.get(tid))
    s.relay(r.tasks.get(tid), "页面打开了，接着核对内容。", source="main")
    assert len(payloads(r)) == 1
    p = payloads(r)[0]
    assert "先把页面打开" in p["text"] and "页面打开了" in p["text"]
    assert p["push_kind"] == "command" and p["at_user"] == OWNER and p["reply_to"] == "original"
    role, messages = m.chat.call_args.args[:2]
    assert role == "main"
    prompt = "\n".join(x["content"] for x in messages)
    assert "说话干脆，语气轻松" in prompt and "不自我介绍" in prompt and "不寒暄" in prompt


@pytest.mark.asyncio
async def test_relay_ignores_tools_json_workers_and_logs_only_reason(rig, caplog):
    r = rig
    sid = open_idle(r)
    tid = linked_task(r, sid, status="running")
    s, _ = speech(r)
    for source in ["worker", "tool", "tool_result"]:
        s.relay(r.tasks.get(tid), "这句不该发出去。", source=source)
    s.relay(r.tasks.get(tid), '{"ok":true}', source="main")
    secret = "sk-exampleABCDEFGHIJKLMNOP"
    with caplog.at_level("INFO"):
        s.relay(r.tasks.get(tid), "密钥 " + secret, source="main")
    assert not payloads(r)
    assert "secret" in caplog.text and secret not in caplog.text
    s.models.chat.return_value = SimpleNamespace(text="顺便说一句", tool_calls=[{"name": "exec"}])
    await s.start(r.tasks.get(tid))
    assert not payloads(r)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["disabled", "relay_off", "legacy", "unserved"])
async def test_inactive_speech_has_zero_reads_models_sends(rig, monkeypatch, mode):
    r = rig
    sid = open_idle(r)
    tid = linked_task(r, sid, status="running")
    task = r.tasks.get(tid)
    s, m = speech(r)
    if mode == "disabled":
        s._settings = lambda: replace(r.settings, session=replace(r.settings.session, enabled=False))
    elif mode == "relay_off":
        s._settings = lambda: replace(r.settings, session=replace(r.settings.session, relay_text=False))
    elif mode == "legacy":
        task["session_id"] = None
    else:
        task["group_id"] = "777"
    monkeypatch.setattr(r.store, "read", lambda: pytest.fail("不应读库"))
    await s.start(task)
    assert await s.finish(task, "已做好。") == "已做好。"
    s.relay(task, "继续检查内容。", source="main")
    m.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_final_speech_absorbs_pending_lines_and_is_cached(rig):
    r = rig
    sid = open_idle(r)
    tid = linked_task(r, sid)
    task = r.tasks.get(tid)
    s, m = speech(r, "页面已经整理好了，这段工作要先收尾吗？@我确认就行。")
    s.relay(task, "我再核对一下页面内容。", source="main")
    text = await s.finish(task, "点开就能看。")
    assert "整理好了" in text and "点开就能看" in text and "再核对" in text
    assert await s.finish(task, "点开就能看。") == text
    assert m.chat.await_count == 1
    rows = r.store.read().execute("SELECT status,error FROM outbox").fetchall()
    assert all(row["status"] == "dropped" and row["error"] == "已合入交付" for row in rows)
    assert session(r, sid)["state"] != "closed"


@pytest.mark.asyncio
async def test_progress_natural_facts_and_no_new_task(rig):
    r = rig
    sid = open_idle(r)
    tid = linked_task(r, sid, title="网页截图", status="running")
    runner = r.make()
    runner.models = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(text="页面打开了，我正在核对截图。", tool_calls=[])))
    event(r, sid, "做到哪了", kind="progress_query")
    await runner.tick()
    assert payloads(r)[0]["text"] == "页面打开了，我正在核对截图。"
    assert not r.started and r.store.read().execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    call = runner.models.chat.call_args
    assert call.args[0] == "main"
    facts = "\n".join(m["content"] for m in call.args[1])
    assert all(x in facts for x in ["网页截图", "working", "最近一步", "上次交付"])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "timeout", "unsafe", "off"])
async def test_progress_falls_back_bounded(rig, monkeypatch, failure):
    r = rig
    sid = open_idle(r)
    linked_task(r, sid, title="网页截图", status="running")
    runner = r.make()
    async def slow(*args, **kwargs):
        await asyncio.Event().wait()
    result = SimpleNamespace(text="token=not-for-group", tool_calls=[])
    runner.models = SimpleNamespace(chat=AsyncMock(side_effect=RuntimeError("unavailable")) if failure == "error" else AsyncMock(return_value=result))
    if failure == "timeout":
        monkeypatch.setattr(module(), "SPEECH_TIMEOUT_S", .01)
        runner.models.chat = slow
    if failure == "off":
        runner._settings = lambda: replace(r.settings, session=replace(r.settings.session, relay_text=False))
    event(r, sid, "进度", kind="progress_query")
    await asyncio.wait_for(runner.tick(), .5)
    assert "「网页截图」：在做；上次交付：" in payloads(r)[0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["谢谢", "辛苦了", "谢啦", "谢谢你", "谢谢，辛苦了", "谢谢～", "辛苦啦"])
async def test_thanks_never_ends_even_when_judge_would_end(rig, text):
    r = rig
    sid = open_idle(r)
    tid = linked_task(r, sid)
    runner = r.make()
    assert runner.propose_closing(tid)
    runner._classify = AsyncMock(return_value={"action": "end"})
    event(r, sid, text)
    await runner.tick()
    assert session(r, sid)["state"] != "closed"
    assert r.store.read().execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_end_natural_response_and_proposal_requires_confirmation(rig):
    r = rig
    sid = open_idle(r)
    tid = linked_task(r, sid)
    runner = r.make()
    assert runner.propose_closing(tid)
    assert not runner.propose_closing(tid)
    assert session(r, sid)["state"] != "closed"
    runner = r.make()
    assert not runner.propose_closing(tid)
    runner.models = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(text="好，先收在这里，需要时再叫我。", tool_calls=[])))
    event(r, sid, "确认", mid="confirm")
    await runner.tick()
    assert session(r, sid)["state"] == "closed"
    assert payloads(r)[-1]["text"] == "好，先收在这里，需要时再叫我。"
