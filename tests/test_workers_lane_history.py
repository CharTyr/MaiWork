"""Workers.run 的 lane 续用（docs/20 §6.2 / §5.3）：

- history 给了就接着上次的对话干：system 现算 + 旧前情 + 这次的说明；
- 跑完 history 原地换成这一轮结束时的对话（不含 system，悬着的工具调用补了回复）；
- 历史里本轮不给的工具改写成文字；模型硬调也被 Tools.call 拦下；
- escalate=True 原样透传给 models.chat（换升级模型）；没给就不传，老调用方不受影响。
"""

from __future__ import annotations

import pytest

from CharTyr_MaiWork.maiwork.workers import Workers
from tests.test_workers import FakeChatResult, ReplayModels, _tool_call, store, tools  # noqa: F401

pytestmark = pytest.mark.asyncio

GID = "900000001"

OLD = [
    {"role": "user", "content": "第一次的说明：查 MaiBot"},
    {"role": "assistant", "content": "", "tool_calls": [_tool_call("web_search", {"query": "MaiBot"}, "o1")]},
    {"role": "tool", "tool_call_id": "o1", "name": "web_search", "content": "旧结果：官网 a.com"},
    {"role": "assistant", "content": "", "tool_calls": [_tool_call("submit_result", {"summary": "第一次交回"}, "o2")]},
]


def _submit(summary, cid="s1"):
    return FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": summary}, cid)])


async def test_history_continues_previous_conversation(store, tools):  # noqa: F811
    models = ReplayModels([_submit("第二次交回")])
    w = Workers(models, tools)
    history = [dict(m) for m in OLD]
    report = await w.run("返工：补上下载链接", group_id=GID, tools=["web_search"], task_id="T-1", history=history)
    assert report.ok and report.summary == "第二次交回"
    _role, sent, _kw = models.calls[0]
    assert sent[0]["role"] == "system"
    contents = [str(m.get("content") or "") for m in sent]
    assert "第一次的说明：查 MaiBot" in contents
    assert "旧结果：官网 a.com" in contents
    assert sent[-1] == {"role": "user", "content": "返工：补上下载链接"}
    # 上次交回悬着的 submit_result 补了回复，严格端点不 400
    ids = [m.get("tool_call_id") for m in sent if m["role"] == "tool"]
    assert "o2" in ids


async def test_history_updated_in_place_without_system(store, tools):  # noqa: F811
    models = ReplayModels([
        FakeChatResult(tool_calls=[_tool_call("web_search", {"query": "x"}, "c1")]),
        _submit("交回", "c2"),
    ])
    w = Workers(models, tools)
    history: list[dict] = []
    await w.run("查 x", group_id=GID, tools=["web_search"], task_id="T-1", history=history)
    assert history, "跑完要把这一轮的对话写回 history"
    assert all(m["role"] != "system" for m in history)
    assert history[0] == {"role": "user", "content": "查 x"}
    # 末尾的 submit_result 有补的回复
    assert history[-1]["role"] == "tool" and history[-1]["tool_call_id"] == "c2"


async def test_history_tools_not_allowed_rewritten(store, tools):  # noqa: F811
    models = ReplayModels([_submit("交回")])
    w = Workers(models, tools)
    # 这一轮不给 web_search
    await w.run("返工", group_id=GID, tools=[], task_id="T-1", history=[dict(m) for m in OLD])
    _role, sent, _kw = models.calls[0]
    for m in sent:
        for tc in m.get("tool_calls") or []:
            assert tc["function"]["name"] != "web_search"
    assert any("这一轮不能再用" in str(m.get("content") or "") for m in sent)


async def test_forged_call_to_removed_tool_is_blocked(store, tools):  # noqa: F811
    models = ReplayModels([
        FakeChatResult(tool_calls=[_tool_call("web_search", {"query": "x"}, "c1")]),
        _submit("交回", "c2"),
    ])
    w = Workers(models, tools)
    history = [dict(m) for m in OLD]
    await w.run("返工", group_id=GID, tools=[], task_id="T-1", history=history)
    tool_msgs = [m for m in history if m["role"] == "tool" and m.get("tool_call_id") == "c1"]
    assert tool_msgs and "出错了" in tool_msgs[0]["content"]


async def test_escalate_passed_to_models(store, tools):  # noqa: F811
    models = ReplayModels([_submit("交回")])
    w = Workers(models, tools)
    await w.run("活", group_id=GID, tools=[], task_id="T-1", history=[], escalate=True)
    assert models.calls[0][2].get("escalate") is True


async def test_no_escalate_kwarg_by_default(store, tools):  # noqa: F811
    models = ReplayModels([_submit("交回")])
    w = Workers(models, tools)
    await w.run("活", group_id=GID, tools=[], task_id="T-1")
    assert "escalate" not in models.calls[0][2]


async def test_report_carries_challenge(store, tools):  # noqa: F811
    """docs/20 第三步：submit_result 里的 challenge 进 WorkerReport.challenge。"""
    ch = {"reason": "官方下载页已经下线", "evidence": [], "suggestion": "改用镜像站"}
    models = ReplayModels([FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "做了", "challenge": ch}, "c1")])])
    rep = await Workers(models, tools).run("活", group_id=GID, tools=[], task_id="T-1")
    assert rep.challenge == ch


async def test_escalate_fixed_for_whole_run(store, tools):  # noqa: F811
    """§5.4：只在派新说明 / 压缩时换模型，一条说明干到一半不换——同一次 run 每次调用 escalate 一致。"""
    models = ReplayModels([
        FakeChatResult(tool_calls=[_tool_call("web_search", {"query": "x"}, "a1")]),
        _submit("好了"),
    ])
    await Workers(models, tools).run("活", group_id=GID, tools=["web_search"], task_id="T-1", history=[], escalate=True)
    assert len(models.calls) == 2 and all(c[2].get("escalate") is True for c in models.calls)
