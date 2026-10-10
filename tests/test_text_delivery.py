"""T-13 直接回文字的交付（线上事故 2026-10-10）+ MaiBot 已经回过就不重复发。

线上发生了什么：
- 群友让 bot 写一封约 100 字的信，MaiWork 把它计划成 deliver_kind="text"（随口一问的
  规模档）。子 agent 把信只写在交回总结里，验收通过后 `_handle_passed` 对 text 只把那条
  ≤60 字的交付说明入队当群消息（「信写好了，林诗栋口吻三件事都说到，125字够数直接用」）
  ——信本身从没发出去。
- 同一时间宿主 MaiBot（同一个 QQ 账号）已经用引用回复答过这件事了，MaiWork 又发了一条，
  等于重复刷屏。

本文件测：
A. text 交付的成品 = 工作区 `artifacts/<任务>/reply.md` 里的回复原文：子 agent 提示里
   钉死要写这个文件；验收认这个文件（不在 / 空的 → 不通过；成品扫描也扫它）；
   交付时 ≤1500 字就把原文（过隐私闸后）发进群、超过 1500 字当群文件发；读不到就回落
   老行为（只发交付说明）；`reenqueue_missing` 补发也优先发这份原文。
B. text 交付前先看 MaiBot 是不是已经引用回复过这条需求：真答了（去掉引用的原话后还剩
   ≥30 字、且那条消息不是 MaiWork 自己发的）就不往群里发，任务照常完成、留一条事件。
   没 request / 读消息出错 / 只回一句「好的稍等」→ 一律照常交付（失败放行）。

模型桩（ModelsQueue / FakeWorkers / _build / _create_task 等）复用
tests/test_coordinator.py，假宿主复用 tests/fakes.py 的 FakeHost。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fakes import FakeHost
from tests.test_coordinator import (  # noqa: F401  (fixtures)
    GID,
    NOW,
    FakeDelivery,
    FakeOutbox,
    FakeWorkers,
    ModelsQueue,
    _build,
    _create_task,
    env,
    fixed_clock,
    goals,
    mem_store,
    settings,
    tasks,
    tools,
)

from CharTyr_MaiWork.maiwork.host import HostError, Msg
from CharTyr_MaiWork.maiwork.outbox import Delivery

pytestmark = pytest.mark.asyncio

# 125 字的信（线上那条需求是「写一封约 100 字的信」，事故里被交付说明顶掉的正文）
LETTER = (
    "林老师，您好！上次说的公开课教案我已经整理好了，一共三个环节：先是十分钟的课堂导入，"
    "用一段短视频引出问题；然后是小组讨论，重点放在学生自己找规律；最后留十五分钟做随堂练习。"
    "教具和课件都在附件里，您要是觉得节奏太快，我可以再压缩一点，也能换更活的做法。"
)
# 事故里真正被发进群的那条 ≤60 字交付说明
NOTE = "信写好了，三件事都说到，直接用"
REPLY_NAME = "reply.md"


def _coordinator_module():
    """延迟导入：先写测试跑红时函数 / 常量还不存在 → 算失败（不是 skip、不是假绿）。"""
    from CharTyr_MaiWork.maiwork import coordinator

    return coordinator


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _text_plan(*, jobs=None, criteria=None) -> str:
    return json.dumps(
        {
            "criteria": criteria if criteria is not None else ["把信写清楚"],
            "deliver_kind": "text",
            "scale": "standard",
            "jobs": jobs if jobs is not None
            else [{"brief": "写一封信", "tools": ["write_file"]}],
            "question": None,
        },
        ensure_ascii=False,
    )


def _text_review(*, artifact="", note=NOTE, review="看着不错") -> str:
    return json.dumps(
        {"pass": True, "review": review, "missing": [], "artifact": artifact, "note": note},
        ensure_ascii=False,
    )


def _text_plan_dict() -> dict:
    return {"criteria": ["把信写清楚"], "deliver_kind": "text"}


def _reply_path(env, task, tid, name=REPLY_NAME) -> Path:
    d = env.workspace(str(task["workspace"])) / "artifacts" / tid
    d.mkdir(parents=True, exist_ok=True)
    return d / name


def _coordinator(mem_store, settings, env, tools, tasks, goals, replies, host=None,
                 workers=None, delivery=None, outbox=None):
    return _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=ModelsQueue(replies=replies), workers=workers or FakeWorkers(),
        host=host, delivery=delivery, outbox=outbox,
    )


def _seed_request(store, *, request_id="REQ-1", message_id="m-ask", quote="帮我写封信",
                  created=None) -> None:
    created = NOW - 120 if created is None else created
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO requests (id, group_id, kind, title, quote, message_id, status,"
            " created, updated) VALUES (?, ?, 'task', ?, ?, ?, 'approved', ?, ?)",
            (request_id, GID, "写封信", quote, message_id, created, created),
        )


def _seed_own_sent(store, *, message_id: str) -> None:
    """MaiWork 自己发过的一条群消息（outbox 已发 + result.message_id）。"""
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO outbox (key, group_id, kind, payload, status, result, created, updated)"
            " VALUES (?, ?, 'text', '{}', 'sent', ?, ?, ?)",
            (f"task:T-own:{message_id}", GID,
             json.dumps({"message_id": message_id}, ensure_ascii=False), NOW, NOW),
        )


def _events(store, tid: str) -> list[dict]:
    rows = store.read().execute(
        "SELECT kind, payload FROM events WHERE entity='task' AND entity_id=?", (tid,)
    ).fetchall()
    out = []
    for r in rows:
        try:
            data = json.loads(r["payload"] or "{}")
        except (TypeError, ValueError):
            data = {}
        out.append({"kind": str(r["kind"]), "payload": data if isinstance(data, dict) else {}})
    return out


def _deliver_rows(outbox) -> list[dict]:
    return [e for e in outbox.enqueued if str(e["key"]).endswith(":deliver:text")]


# ---------------------------------------------------------------------------
# A1. 子 agent 提示：text 的成品是 reply.md 里的回复原文
# ---------------------------------------------------------------------------


async def test_brief_tells_worker_to_write_reply_md_for_text(
    mem_store, settings, env, tools, tasks, goals
):
    coordinator = _coordinator(mem_store, settings, env, tools, tasks, goals, replies=[])

    brief = coordinator._enrich_brief("写一封信", "T-1", "text")

    assert REPLY_NAME in brief
    assert "字数" in brief  # 明说不许写「全文共 125 字」
    # 中间步骤（有 steps_dir）各写各的步骤目录，不当成品
    steps = coordinator._enrich_brief(
        "写一封信", "T-1", "text", steps_dir="artifacts/T-1/steps/1"
    )
    assert REPLY_NAME not in steps
    # view / file 分支一个字不动
    assert REPLY_NAME not in coordinator._enrich_brief("做一页", "T-1", "file")


# ---------------------------------------------------------------------------
# A2. 验收：text 的成品是 reply.md，不在 / 空 / 有内部用语都过不了
# ---------------------------------------------------------------------------


async def test_review_text_without_reply_md_is_not_passed(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, title="写封信", req="帮我写封信", reply=None)
    coordinator = _coordinator(
        mem_store, settings, env, tools, tasks, goals, replies=[_text_review(artifact="")],
    )

    review = await coordinator._review(
        tasks.get(tid), _text_plan_dict(), "信写完了", ["e1"], [],
    )

    assert review["pass"] is False
    assert REPLY_NAME in review["review"]
    assert review["artifact"] == f"artifacts/{tid}/{REPLY_NAME}"


async def test_review_text_with_empty_reply_md_is_not_passed(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, title="写封信", req="帮我写封信")
    _reply_path(env, tasks.get(tid), tid).write_text("  \n\t", encoding="utf-8")
    coordinator = _coordinator(
        mem_store, settings, env, tools, tasks, goals,
        replies=[_text_review(artifact=f"artifacts/{tid}/{REPLY_NAME}")],
    )

    review = await coordinator._review(
        tasks.get(tid), _text_plan_dict(), "信写完了", ["e1"], [],
    )

    assert review["pass"] is False
    assert REPLY_NAME in review["review"]


async def test_review_text_with_reply_md_passes(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, title="写封信", req="帮我写封信")
    _reply_path(env, tasks.get(tid), tid).write_text(LETTER, encoding="utf-8")
    coordinator = _coordinator(
        mem_store, settings, env, tools, tasks, goals, replies=[_text_review(artifact="")],
    )

    review = await coordinator._review(
        tasks.get(tid), _text_plan_dict(), "信写完了", ["e1"], [],
    )

    assert review["pass"] is True
    assert review["artifact"] == f"artifacts/{tid}/{REPLY_NAME}"


async def test_review_scans_reply_md_for_internal_words(
    mem_store, settings, env, tools, tasks, goals
):
    """成品扫描要真扫 reply.md：正文里出现 job1 这种内部用语 → 改判不通过。"""
    tid = _create_task(tasks, title="写封信", req="帮我写封信")
    _reply_path(env, tasks.get(tid), tid).write_text(
        "信写好了，按 job1 的输出照抄的，你看行不行。", encoding="utf-8"
    )
    coordinator = _coordinator(
        mem_store, settings, env, tools, tasks, goals, replies=[_text_review(artifact="")],
    )

    review = await coordinator._review(
        tasks.get(tid), _text_plan_dict(), "信写完了", ["e1"], [],
    )

    assert review["pass"] is False
    assert "内部用语" in review["review"]


async def test_review_prompt_points_text_artifact_at_reply_md(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, title="写封信", req="帮我写封信")
    _reply_path(env, tasks.get(tid), tid).write_text(LETTER, encoding="utf-8")
    models = ModelsQueue(replies=[_text_review(artifact="")])
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(),
    )

    await coordinator._review(tasks.get(tid), _text_plan_dict(), "信写完了", ["e1"], [])

    prompt = models.calls[-1][1][-1]["content"]
    assert REPLY_NAME in prompt
    assert "可以留空" not in prompt


# ---------------------------------------------------------------------------
# A3. 交付：正文发进群（不是那条 60 字说明）；太长当群文件发
# ---------------------------------------------------------------------------


async def test_text_task_delivers_reply_md_not_the_note(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, title="写封信", req="帮我写封信")
    outbox, delivery = FakeOutbox(), FakeDelivery()
    workers = FakeWorkers()

    async def _write():
        _reply_path(env, tasks.get(tid), tid).write_text(LETTER, encoding="utf-8")

    workers.before_return = _write
    coordinator = _coordinator(
        mem_store, settings, env, tools, tasks, goals,
        replies=[_text_plan(), _text_review(note=NOTE)],
        workers=workers, delivery=delivery, outbox=outbox,
    )

    await coordinator.run_task(tid)

    assert tasks.get(tid)["status"] == "completed"
    rows = _deliver_rows(outbox)
    assert len(rows) == 1
    assert rows[0]["kind"] == "text"
    assert rows[0]["payload"]["text"] == LETTER
    assert rows[0]["payload"]["text"] != NOTE
    assert rows[0]["payload"]["push_kind"] == "delivery"
    assert delivery.delivered == []


async def test_text_reply_over_limit_is_delivered_as_group_file(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, title="写封信", req="帮我写封信")
    long_text = "这是一段很长的回复，写得比较细。\n" * 200
    assert len(long_text) > 1500
    outbox, delivery = FakeOutbox(), FakeDelivery()
    workers = FakeWorkers()

    async def _write():
        _reply_path(env, tasks.get(tid), tid).write_text(long_text, encoding="utf-8")

    workers.before_return = _write
    coordinator = _coordinator(
        mem_store, settings, env, tools, tasks, goals,
        replies=[_text_plan(), _text_review(note=NOTE)],
        workers=workers, delivery=delivery, outbox=outbox,
    )

    await coordinator.run_task(tid)

    assert tasks.get(tid)["status"] == "completed"
    assert _deliver_rows(outbox) == []
    assert len(delivery.delivered) == 1
    d = delivery.delivered[0]
    assert d["kind"] == "file"
    assert d["path"].name == REPLY_NAME
    assert d["name"] == "回复.md"
    assert d["note"] == NOTE


async def test_text_task_reply_md_missing_at_delivery_falls_back_to_note(
    mem_store, settings, env, tools, tasks, goals
):
    """验收后文件没了（不该发生）→ 只发交付说明，绝不崩。"""
    tid = _create_task(tasks, title="写封信", req="帮我写封信", reply=None)
    outbox = FakeOutbox()
    coordinator = _coordinator(
        mem_store, settings, env, tools, tasks, goals, replies=[], outbox=outbox,
    )
    tasks.transition(tid, "running")
    tasks.transition(tid, "reviewing")

    await coordinator._handle_passed(
        tid, GID, tasks.get(tid)["workspace"], {"deliver_kind": "text"},
        {"review": "通过", "note": NOTE, "artifact": f"artifacts/{tid}/{REPLY_NAME}"},
    )

    assert tasks.get(tid)["status"] == "completed"
    rows = _deliver_rows(outbox)
    assert len(rows) == 1
    assert rows[0]["payload"]["text"] == NOTE


# ---------------------------------------------------------------------------
# B. MaiBot 已经用引用回复答过 → 不重复发进群
# ---------------------------------------------------------------------------


def _host_with_bot_reply(*, reply_text: str, reply_id: str = "m-bot",
                         request_mid: str = "m-ask") -> FakeHost:
    return FakeHost(msgs=[
        Msg(id=request_mid, ts=NOW - 120, user_id="111", user_name="甲",
            text="帮我写封信", is_bot=False, is_at=False, is_picture=False, reply_to=""),
        Msg(id=reply_id, ts=NOW - 70, user_id="10001", user_name="麦麦",
            text=reply_text, is_bot=True, is_at=False, is_picture=False,
            reply_to=request_mid),
    ])


async def test_maibot_already_answered_skips_group_delivery(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, title="写封信", req="帮我写封信", request_id="REQ-1")
    _seed_request(mem_store)
    host = _host_with_bot_reply(reply_text="帮我写封信\n" + LETTER)
    outbox, delivery = FakeOutbox(), FakeDelivery()
    workers = FakeWorkers()

    async def _write():
        _reply_path(env, tasks.get(tid), tid).write_text(LETTER, encoding="utf-8")

    workers.before_return = _write
    coordinator = _coordinator(
        mem_store, settings, env, tools, tasks, goals,
        replies=[_text_plan(), _text_review(note=NOTE)],
        host=host, workers=workers, delivery=delivery, outbox=outbox,
    )

    await coordinator.run_task(tid)

    assert tasks.get(tid)["status"] == "completed"
    assert _deliver_rows(outbox) == []
    assert delivery.delivered == []
    events = _events(mem_store, tid)
    notes = " ".join(str(e["payload"].get("note") or "") for e in events)
    assert "MaiBot 已经在群里回过" in notes
    assert any("maibot" in e["kind"] for e in events)


async def test_maibot_answered_before_request_was_recorded_still_skips(
    mem_store, settings, env, tools, tasks, goals
):
    """线上 T-13：原话 16:17 发出，MaiWork 读群 16:19 才记下需求。MaiBot 要是在这
    两分钟里就回了，回复早于需求创建时刻——往前看的窗口要盖住这段（不能只看 60 秒）。"""
    tid = _create_task(tasks, title="写封信", req="帮我写封信", request_id="REQ-1")
    _seed_request(mem_store)  # 需求创建 = NOW - 120
    host = FakeHost(msgs=[
        Msg(id="m-ask", ts=NOW - 900, user_id="111", user_name="甲",
            text="帮我写封信", is_bot=False, is_at=False, is_picture=False, reply_to=""),
        Msg(id="m-bot", ts=NOW - 600, user_id="10001", user_name="麦麦",
            text="帮我写封信 " + LETTER, is_bot=True, is_at=False, is_picture=False,
            reply_to="m-ask"),
    ])
    outbox, delivery = FakeOutbox(), FakeDelivery()
    workers = FakeWorkers()

    async def _write():
        _reply_path(env, tasks.get(tid), tid).write_text(LETTER, encoding="utf-8")

    workers.before_return = _write
    coordinator = _coordinator(
        mem_store, settings, env, tools, tasks, goals,
        replies=[_text_plan(), _text_review(note=NOTE)],
        host=host, workers=workers, delivery=delivery, outbox=outbox,
    )

    await coordinator.run_task(tid)

    assert tasks.get(tid)["status"] == "completed"
    assert _deliver_rows(outbox) == []


async def test_maibot_long_ack_shorter_than_half_our_reply_still_delivers(
    mem_store, settings, env, tools, tasks, goals
):
    """MaiBot 回了一句长点的「接下了」（≥30 字但不到我们回复的一半）→ 不算答过，照常交付。"""
    tid = _create_task(tasks, title="写封信", req="帮我写封信", request_id="REQ-1")
    _seed_request(mem_store)
    ack = "收到收到，这个交给我来弄吧，稍等一下下哦，等我弄好了马上就发到群里来，别着急哈"
    assert 30 <= len(ack) < len(LETTER) / 2
    host = _host_with_bot_reply(reply_text="帮我写封信 " + ack)
    outbox, delivery = FakeOutbox(), FakeDelivery()
    workers = FakeWorkers()

    async def _write():
        _reply_path(env, tasks.get(tid), tid).write_text(LETTER, encoding="utf-8")

    workers.before_return = _write
    coordinator = _coordinator(
        mem_store, settings, env, tools, tasks, goals,
        replies=[_text_plan(), _text_review(note=NOTE)],
        host=host, workers=workers, delivery=delivery, outbox=outbox,
    )

    await coordinator.run_task(tid)

    rows = _deliver_rows(outbox)
    assert len(rows) == 1
    assert rows[0]["payload"]["text"] == LETTER


async def test_maibot_short_ack_still_delivers(
    mem_store, settings, env, tools, tasks, goals
):
    """MaiBot 只回了一句「好的稍等」（去掉引用的原话不足 30 字）→ 照常交付。"""
    tid = _create_task(tasks, title="写封信", req="帮我写封信", request_id="REQ-1")
    _seed_request(mem_store)
    host = _host_with_bot_reply(reply_text="帮我写封信\n好的稍等")
    outbox = FakeOutbox()
    workers = FakeWorkers()

    async def _write():
        _reply_path(env, tasks.get(tid), tid).write_text(LETTER, encoding="utf-8")

    workers.before_return = _write
    coordinator = _coordinator(
        mem_store, settings, env, tools, tasks, goals,
        replies=[_text_plan(), _text_review(note=NOTE)],
        host=host, workers=workers, outbox=outbox,
    )

    await coordinator.run_task(tid)

    rows = _deliver_rows(outbox)
    assert len(rows) == 1
    assert rows[0]["payload"]["text"] == LETTER


async def test_maibot_reply_that_is_our_own_message_still_delivers(
    mem_store, settings, env, tools, tasks, goals
):
    """那条引用回复其实是 MaiWork 自己发的（outbox 里记着 message_id）→ 照常交付。"""
    tid = _create_task(tasks, title="写封信", req="帮我写封信", request_id="REQ-1")
    _seed_request(mem_store)
    _seed_own_sent(mem_store, message_id="m-bot")
    host = _host_with_bot_reply(reply_text="帮我写封信\n" + LETTER)
    outbox = FakeOutbox()
    workers = FakeWorkers()

    async def _write():
        _reply_path(env, tasks.get(tid), tid).write_text(LETTER, encoding="utf-8")

    workers.before_return = _write
    coordinator = _coordinator(
        mem_store, settings, env, tools, tasks, goals,
        replies=[_text_plan(), _text_review(note=NOTE)],
        host=host, workers=workers, outbox=outbox,
    )

    await coordinator.run_task(tid)

    rows = _deliver_rows(outbox)
    assert len(rows) == 1
    assert rows[0]["payload"]["text"] == LETTER


async def test_text_task_without_request_still_delivers(
    mem_store, settings, env, tools, tasks, goals
):
    """构想派生的任务没有 request（或 request 没 message_id）→ 照常交付。"""
    tid = _create_task(tasks, title="写封信", req="帮我写封信")
    host = _host_with_bot_reply(reply_text="帮我写封信\n" + LETTER)
    outbox = FakeOutbox()
    workers = FakeWorkers()

    async def _write():
        _reply_path(env, tasks.get(tid), tid).write_text(LETTER, encoding="utf-8")

    workers.before_return = _write
    coordinator = _coordinator(
        mem_store, settings, env, tools, tasks, goals,
        replies=[_text_plan(), _text_review(note=NOTE)],
        host=host, workers=workers, outbox=outbox,
    )

    await coordinator.run_task(tid)

    rows = _deliver_rows(outbox)
    assert len(rows) == 1
    assert rows[0]["payload"]["text"] == LETTER


class _BrokenMessagesHost(FakeHost):
    """读消息就报错的宿主（失败放行：照常交付）。"""

    async def messages(self, *args, **kwargs):
        raise HostError("读群消息超时")


async def test_host_messages_error_still_delivers(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, title="写封信", req="帮我写封信", request_id="REQ-1")
    _seed_request(mem_store)
    host = _BrokenMessagesHost()
    outbox = FakeOutbox()
    workers = FakeWorkers()

    async def _write():
        _reply_path(env, tasks.get(tid), tid).write_text(LETTER, encoding="utf-8")

    workers.before_return = _write
    coordinator = _coordinator(
        mem_store, settings, env, tools, tasks, goals,
        replies=[_text_plan(), _text_review(note=NOTE)],
        host=host, workers=workers, outbox=outbox,
    )

    await coordinator.run_task(tid)

    rows = _deliver_rows(outbox)
    assert len(rows) == 1
    assert rows[0]["payload"]["text"] == LETTER


# ---------------------------------------------------------------------------
# C. 管理员重发（reenqueue_missing）：text 也发 reply.md 原文
# ---------------------------------------------------------------------------


class _StubOutbox:
    """只给 Delivery 用的最小发件箱：记 enqueue，给 _get_settings。"""

    def __init__(self, settings) -> None:
        self._settings = settings
        self.enqueued: list[dict] = []

    def _get_settings(self):
        return self._settings

    def enqueue(self, key, group_id, kind, payload, *, task_id=None, not_before=0):
        self.enqueued.append({
            "key": key, "group_id": group_id, "kind": kind,
            "payload": dict(payload or {}), "task_id": task_id,
        })
        return len(self.enqueued)


def _seed_completed_text_task(store, tid: str, workspace: str, title="写封信") -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tasks (id, group_id, workspace, title, status, delivery_kind,"
            " created, updated) VALUES (?, ?, ?, ?, 'completed', 'text', 0, 0)",
            (tid, GID, workspace, title),
        )


async def test_reenqueue_missing_text_uses_reply_md(mem_store, settings, env):
    settings.is_served = lambda group_id: True
    ws_name = settings.workspace_of(GID)
    outbox = _StubOutbox(settings)
    delivery = Delivery(mem_store, outbox)

    _seed_completed_text_task(mem_store, "T-1", ws_name)
    _reply_path(env, {"workspace": ws_name}, "T-1").write_text(LETTER, encoding="utf-8")

    assert await delivery.reenqueue_missing("T-1", env) is True
    assert outbox.enqueued[-1]["key"] == "task:T-1:deliver:text"
    assert outbox.enqueued[-1]["payload"]["text"] == LETTER

    # 没有 reply.md / 空文件 → 老行为：兜底交付说明
    _seed_completed_text_task(mem_store, "T-2", ws_name, title="月度账单")
    _reply_path(env, {"workspace": ws_name}, "T-2").write_text("   ", encoding="utf-8")

    assert await delivery.reenqueue_missing("T-2", env) is True
    assert "月度账单" in outbox.enqueued[-1]["payload"]["text"]
    assert outbox.enqueued[-1]["payload"]["text"] != LETTER


async def test_reenqueue_missing_text_without_env_keeps_fallback_note(
    mem_store, settings
):
    """env 拿不到（老调用）→ 照旧只发兜底说明，不许报错。"""
    settings.is_served = lambda group_id: True
    outbox = _StubOutbox(settings)
    delivery = Delivery(mem_store, outbox)
    _seed_completed_text_task(mem_store, "T-3", settings.workspace_of(GID), title="月度账单")

    assert await delivery.reenqueue_missing("T-3", None) is True
    assert "月度账单" in outbox.enqueued[-1]["payload"]["text"]
