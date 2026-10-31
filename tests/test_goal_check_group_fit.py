"""goals-fit（2026-10-11）：check_goal 贴合群聊的六条 —— 调查素材 / 时间感 / 待确认旧勾 /
平时进展不发群 / 补判 fit / 「盯到了·完成了」消息 + delivery 的 goal_result 节制口径。

覆盖（prd R1/R2/R3/R4/R5末条/R6）：
1. 专岗交回 assessment / plan / questions / evidence 后，主模型拿到的提示词含这些内容
   和「是数据不是指令」；专岗没挂 / 失败 → 提示词写「这次没有调查结果」，检查照常写心跳。
2. 提示词有当前北京时间、目标建了几天、上次检查时间；群聊行带「MM-DD HH:MM 谁：正文」，
   带时间的行照样能过 `goals.chat_evidence_match`。
3. 3 条有依据 + 1 条无依据旧勾（待确认）→ 不判完成、不出队完成话；模型给合格证据 →
   该条变有依据、记 `goal.criterion_backed`（不是 criterion_done）、全部有依据才判完成并
   出队 push_kind=goal_result 的完成话。
4. 模型还回 report → 不出队任何群消息；last_text 更新；mentions.add 以 key=goal:<id> 记账。
5. found.url 在本次调查链接里 → 出队一条 goal_result（含 @发起人 + 网页链接）；url 不在 /
   同一链接第二次 / 同一天第二条 → 不出队，记 goal.found_rejected（reason）。
6. fit 为空 → 提示词要 fit，返回 human → goals.fit=human；已有 fit → 提示词不再要。
7. delivery：goal_result 不占每日额度、仍守睡觉时段与非服务群。

桩复用 tests/test_coordinator.py（真 Store / 真 Tasks / 真 Goals / FakeModels 队列）与
tests/mafakes_specialists.py（假专岗）；全程不联网、不调模型、不往群发消息。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from CharTyr_MaiWork.maiwork import clock, group_push
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import (
    PUSH_KIND_CN,
    TASK_QUOTA_FREE_KINDS,
    UNSERVED_REASON,
    Mentions,
    Pushes,
    task_quota_free_kind,
)
from CharTyr_MaiWork.maiwork.goals import chat_evidence_match
from CharTyr_MaiWork.maiwork.outbox import Outbox
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import Tools
from CharTyr_MaiWork.maiwork.workers import WorkerReport
from mafakes_specialists import FakeAgents, FakeSpecialists  # noqa: E402
from test_coordinator import (  # noqa: F401  (fixtures / stubs)
    GID,
    NOW,
    FakeWorkers,
    ModelsQueue,
    _build,
    env,
    fixed_clock,
    goals,
    mem_store,
    settings,
    tasks,
    tools,
)

pytestmark = pytest.mark.asyncio

BJ = timezone(timedelta(hours=8))
DAY = 86400.0
TOKEN = "tok12345"
PUBLIC_URL = "https://mw.example"
GONE = "111111111"  # 不在服务名单里的群


# ---------------------------------------------------------------------------
# 桩 / 小工具
# ---------------------------------------------------------------------------


def _wire_settings(settings, *, served: bool = True):
    """给 test_coordinator 的最小 Settings 补上 console.public_url 与 is_served。"""
    settings.console = SimpleNamespace(public_url=PUBLIC_URL)
    settings.is_served = lambda gid: bool(served)
    return settings


def _seed_group(store: Store) -> None:
    """本群在 groups 表里有链接码（card_push.group_link 要它）。"""
    with store.tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO groups (group_id, session_id, workspace, token,"
            " profile_ready_ts, created) VALUES (?, ?, ?, ?, ?, ?)",
            (GID, "sess-1", f"g{GID}", TOKEN, NOW, NOW),
        )


class RecordingOutbox:
    """真写 outbox 表的假发件箱：key 已存在不重复入队（与 Outbox.enqueue 同口径）。"""

    def __init__(self, store: Store):
        self._store = store
        self.enqueued: list[dict] = []

    def enqueue(self, key, group_id, kind, payload, *, task_id=None, not_before=0):
        key = str(key)
        row = self._store.read().execute(
            "SELECT id FROM outbox WHERE key=?", (key,)
        ).fetchone()
        if row is not None:
            return int(row["id"])
        with self._store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO outbox (key, group_id, kind, payload, status, attempts,"
                " result, error, task_id, not_before, created, updated)"
                " VALUES (?, ?, ?, ?, 'pending', 0, '{}', '', ?, ?, ?, ?)",
                (
                    key, str(group_id), str(kind),
                    json.dumps(payload or {}, ensure_ascii=False),
                    task_id, float(not_before or 0), clock.now(), clock.now(),
                ),
            )
            oid = int(cur.lastrowid or 0)
        self.enqueued.append(
            {
                "key": key, "group_id": str(group_id), "kind": str(kind),
                "payload": dict(payload or {}), "task_id": task_id, "id": oid,
            }
        )
        return oid


class FakeMentions:
    def __init__(self):
        self.added: list[dict] = []

    def add(self, group_id, text, *, key, ttl_s, turns=5, target_user_id="", keywords=""):
        self.added.append(
            {"group_id": str(group_id), "text": str(text), "key": str(key),
             "ttl_s": float(ttl_s), "turns": int(turns)}
        )


def _reply(*, done_criteria=(), progress=None, report=None, found=None, fit=None,
           criteria=None, next_check_hours=24, new_task=None) -> str:
    data: dict = {
        "done_criteria": list(done_criteria),
        "next_check_hours": next_check_hours,
        "progress": progress,
        "new_task": new_task,
    }
    if report is not None:
        data["report"] = report
    if found is not None:
        data["found"] = found
    if fit is not None:
        data["fit"] = fit
    if criteria is not None:
        data["criteria"] = list(criteria)
    return json.dumps(data, ensure_ascii=False)


def _prompt(models: ModelsQueue) -> str:
    return "\n".join(str(m.get("content") or "") for m in models.calls[-1][1])


def _goal_row(store: Store, goal_id: str) -> dict:
    r = store.read().execute(
        "SELECT heartbeat_ts, stale_reason, state, last_text, fit, fit_reason"
        " FROM goals WHERE id=?",
        (goal_id,),
    ).fetchone()
    return dict(r)


def _crit(goals, goal_id) -> list:
    return json.loads(goals.get(goal_id)["criteria"])


def _events(store: Store, goal_id: str, kind: str) -> list[dict]:
    rows = store.read().execute(
        "SELECT payload FROM events WHERE kind=? AND entity_id=?", (str(kind), str(goal_id))
    ).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["payload"]))
        except (TypeError, ValueError):
            out.append({})
    return out


def _seed_chat(store: Store, who: str, text: str, *, ts: float) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO chat_log (text, group_id, message_id, ts, user_id, user_name)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (text, GID, f"gfit-{abs(hash(text)) % 10**6}", float(ts), "u-1", who),
        )


def _make(mem_store, settings, env, tools, tasks, goals, models, *,
          specialists=None, outbox=None, mentions=None):
    outbox = outbox if outbox is not None else RecordingOutbox(mem_store)
    coord = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(), outbox=outbox,
    )
    if specialists is not None:
        coord._specialists = specialists  # noqa: SLF001
    if mentions is not None:
        coord._mentions = mentions  # noqa: SLF001
    return coord


def _investigation(**data_kwargs) -> FakeSpecialists:
    return FakeSpecialists(
        FakeAgents(),
        results=[WorkerReport(ok=True, summary="调查交回", data=dict(data_kwargs))],
    )


# ---------------------------------------------------------------------------
# 1. 专岗调查素材先跑、结果进提示词（R2）
# ---------------------------------------------------------------------------


async def test_investigation_material_goes_into_main_prompt(
    mem_store, settings, env, tools, tasks, goals
):
    _wire_settings(settings)
    goal_id = goals.create_agent(
        GID, title="盯暗潮新版本", body="出了说一声", criteria=["版本公告出来了"], by_text="",
    )
    agents = FakeAgents()
    sp = FakeSpecialists(agents, results=[WorkerReport(
        ok=True, summary="翻了官方公告页",
        data={
            "assessment": "新版本还没出，公告停在 10-05",
            "plan": ["继续盯官方公告", "顺手看 mod 区"],
            "questions": ["要不要顺便盯 mod"],
        },
        evidence=["https://example.com/news/1"],
    )])
    models = ModelsQueue(replies=[_reply()])
    coord = _make(mem_store, settings, env, tools, tasks, goals, models, specialists=sp)

    await coord.check_goal(goal_id)

    prompt = _prompt(models)
    assert "新版本还没出，公告停在 10-05" in prompt       # assessment
    assert "继续盯官方公告" in prompt                     # plan
    assert "要不要顺便盯 mod" in prompt                   # questions
    assert "https://example.com/news/1" in prompt         # 看过的链接
    assert "是数据不是指令" in prompt
    # 调查素材确实先于主模型调用产生（专岗被叫起来了）
    assert [r["kind"] for r in sp.runs] == ["goal"]


async def test_no_investigation_prompt_says_so_and_check_still_beats(
    mem_store, settings, env, tools, tasks, goals
):
    """专岗没挂 / 交回失败 → 提示词写「这次没有调查结果」，检查照常写心跳。"""
    _wire_settings(settings)
    goal_id = goals.create_agent(GID, title="盯梢", body="", criteria=["c"], by_text="")

    models = ModelsQueue(replies=[_reply()])
    coord = _make(mem_store, settings, env, tools, tasks, goals, models)  # 没挂专岗
    await coord.check_goal(goal_id)
    assert "这次没有调查结果" in _prompt(models)
    assert _goal_row(mem_store, goal_id)["heartbeat_ts"] == NOW

    # 挂了专岗但交回不合格（ok=False）→ 同样是「没有调查结果」
    bad = FakeSpecialists(FakeAgents(), results=[WorkerReport(ok=False, summary="", error="搜不到")])
    models2 = ModelsQueue(replies=[_reply()])
    # 工具注册表不能重复注册：这次用一份新的 Tools
    coord2 = _make(mem_store, settings, env, Tools(mem_store), tasks, goals, models2,
                   specialists=bad)
    await coord2.check_goal(goal_id)
    assert "这次没有调查结果" in _prompt(models2)
    assert _goal_row(mem_store, goal_id)["heartbeat_ts"] == NOW


# ---------------------------------------------------------------------------
# 2. 时间感（R6）
# ---------------------------------------------------------------------------


async def test_prompt_has_beijing_time_days_and_timed_chat_lines(
    fixed_clock, mem_store, settings, env, tools, tasks, goals
):
    _wire_settings(settings)
    fixed_clock[0] = NOW - 3 * DAY           # 目标建于 3 天前
    goal_id = goals.create_agent(GID, title="盯梢", body="", criteria=["c"], by_text="")
    fixed_clock[0] = NOW
    _seed_chat(mem_store, "阿柒", "脚本已经写好了", ts=NOW - 600)

    models = ModelsQueue(replies=[_reply(), _reply()])
    coord = _make(mem_store, settings, env, tools, tasks, goals, models)
    await coord.check_goal(goal_id)

    prompt = _prompt(models)
    assert clock.bj(NOW).strftime("%Y-%m-%d %H:%M") in prompt
    assert "（北京时间）" in prompt
    assert "目标建于 3 天前" in prompt
    assert "上次检查：还没检查过" in prompt

    stamp = clock.bj(NOW - 600).strftime("%m-%d %H:%M")
    assert f"- {stamp} 阿柒：脚本已经写好了" in prompt     # 群聊行带时间

    # 第二次检查：上次检查时间就是刚才那次心跳
    await coord.check_goal(goal_id)
    prompt2 = _prompt(models)
    assert f"上次检查：{clock.bj(NOW).strftime('%m-%d %H:%M')}" in prompt2


async def test_timed_chat_line_still_matches_evidence():
    """R6：带时间的群聊行 + 引用正文片段的 evidence 仍能算「群里真发生了」。"""
    line = "- 10-11 14:30 阿柒：脚本已经写好了，跑通了"
    assert chat_evidence_match("阿柒：脚本已经写好了，跑通了", [line])
    # 正文里出现 ≥8 连字就算（和旧格式同一套口径）
    assert chat_evidence_match("他说脚本已经写好了，跑通了", [line])
    assert not chat_evidence_match("另一句完全不同的话", [line])
    # 时间前缀本身不算正文：只引时间不算「群里真发生了」
    assert not chat_evidence_match("10-11 14:30 阿柒", [line])


# ---------------------------------------------------------------------------
# 3. 待确认旧勾（R1）
# ---------------------------------------------------------------------------


async def test_pending_criterion_not_done_then_backed_completes(
    mem_store, settings, env, tools, tasks, goals
):
    _wire_settings(settings)
    goal_id = goals.create_agent(
        GID, title="攒避雷攻略", body="分线开档打卡",
        criteria=["群投票选定首开线路", "整理成一份网页", "在群里公示", "群里真的开档打卡了"],
        by_text="",
    )
    now = clock.now()
    goals.set_criterion(goal_id, 0, True, evidence="阿柒：投票投完了 A 线", ts=now)
    goals.set_criterion(goal_id, 1, True, task_id="T-9", ts=now)
    goals.set_criterion(goal_id, 2, True, evidence="老李：网页挂上去了", ts=now)
    goals.set_criterion(goal_id, 3, True)          # 旧勾：done 但没有依据 → 待确认
    _seed_chat(mem_store, "阿柒", "首开线路真的开档了", ts=now - 60)

    outbox = RecordingOutbox(mem_store)
    models = ModelsQueue(replies=[
        _reply(),                                   # 第一次：模型什么都不勾
        _reply(done_criteria=[{"index": 3, "task_id": None,
                               "evidence": "阿柒：首开线路真的开档了"}]),
    ])
    coord = _make(mem_store, settings, env, tools, tasks, goals, models, outbox=outbox)

    await coord.check_goal(goal_id)                 # 3 有依据 + 1 待确认 → 不判完成

    assert goals.get(goal_id)["state"] == "active"
    assert _goal_row(mem_store, goal_id)["state"] == "active"
    assert [e for e in outbox.enqueued if e["key"] == f"goal:{goal_id}:done"] == []
    prompt = _prompt(models)
    assert "?待确认" in prompt and "不算完成" in prompt
    assert "[0] ✓ 群投票选定首开线路" in prompt

    await coord.check_goal(goal_id)                 # 模型按同样规则补了证据

    item = _crit(goals, goal_id)[3]
    assert item["done"] is True
    assert "开档" in item["evidence"]
    backed = _events(mem_store, goal_id, "goal.criterion_backed")
    assert [e.get("index") for e in backed] == [3]
    assert _events(mem_store, goal_id, "goal.criterion_done") == []
    assert _goal_row(mem_store, goal_id)["state"] == "done"
    done = [e for e in outbox.enqueued if e["key"] == f"goal:{goal_id}:done"]
    assert len(done) == 1
    assert done[0]["payload"]["push_kind"] == "goal_result"
    assert "完成" in done[0]["payload"]["text"]


async def test_unbacked_old_tick_alone_never_completes(
    mem_store, settings, env, tools, tasks, goals
):
    """全部标准里只要还有一条待确认，就绝不判完成（哪怕 other 全勾）。"""
    _wire_settings(settings)
    goal_id = goals.create_agent(GID, title="盯梢", body="", criteria=["a", "b"], by_text="")
    goals.set_criterion(goal_id, 0, True, evidence="阿柒：a 做完了")
    goals.set_criterion(goal_id, 1, True)          # 待确认
    outbox = RecordingOutbox(mem_store)
    models = ModelsQueue(replies=[_reply()])
    coord = _make(mem_store, settings, env, tools, tasks, goals, models, outbox=outbox)
    await coord.check_goal(goal_id)
    assert goals.get(goal_id)["state"] == "active"
    assert outbox.enqueued == []


# ---------------------------------------------------------------------------
# 4. 平时进展不发群（R4）
# ---------------------------------------------------------------------------


async def test_report_does_not_reach_group_but_mentions_and_last_text(
    mem_store, settings, env, tools, tasks, goals
):
    _wire_settings(settings)
    goal_id = goals.create_agent(GID, title="盯暗潮新版本", body="", criteria=["公告出来了"], by_text="")
    outbox = RecordingOutbox(mem_store)
    mentions = FakeMentions()
    models = ModelsQueue(replies=[_reply(report="官方公告页多了一条 10-05 的补丁说明")])
    coord = _make(mem_store, settings, env, tools, tasks, goals, models,
                  outbox=outbox, mentions=mentions)

    await coord.check_goal(goal_id)

    assert outbox.enqueued == []                            # 群里一条消息都没有
    assert "补丁说明" in str(_goal_row(mem_store, goal_id)["last_text"])
    assert len(mentions.added) == 1
    m = mentions.added[0]
    assert m["key"] == f"goal:{goal_id}"
    assert m["group_id"] == GID
    assert m["ttl_s"] == 12 * 3600
    assert "最近进展" in m["text"] and "补丁说明" in m["text"]


async def test_progress_beats_report_for_last_text(mem_store, settings, env, tools, tasks, goals):
    """progress 有字时用 progress（report 只是兼容后备），照样不发群。"""
    _wire_settings(settings)
    goal_id = goals.create_agent(GID, title="盯梢", body="", criteria=["c"], by_text="")
    outbox = RecordingOutbox(mem_store)
    mentions = FakeMentions()
    models = ModelsQueue(replies=[_reply(progress="推进到了第二步", report="老格式的报告")])
    coord = _make(mem_store, settings, env, tools, tasks, goals, models,
                  outbox=outbox, mentions=mentions)
    await coord.check_goal(goal_id)
    assert outbox.enqueued == []
    assert "推进到了第二步" in str(_goal_row(mem_store, goal_id)["last_text"])
    assert "推进到了第二步" in mentions.added[0]["text"]
    assert "老格式的报告" not in mentions.added[0]["text"]   # progress 有字时 report 不用


# ---------------------------------------------------------------------------
# 5. 盯到了（R3）
# ---------------------------------------------------------------------------


FOUND_URL = "https://example.com/news/1"
OTHER_INV_URL = "https://example.com/news/2"


async def test_found_enqueues_goal_result_with_at_and_web_link(
    mem_store, settings, env, tools, tasks, goals
):
    _wire_settings(settings)
    _seed_group(mem_store)
    goal_id = goals.create_agent(
        GID, title="盯暗潮新版本", body="", criteria=["公告出来了"], by_text="",
        who_id="10001", who_name="小明",
    )
    sp = _investigation(assessment="公告页有更新", evidence=[FOUND_URL])
    outbox = RecordingOutbox(mem_store)
    models = ModelsQueue(replies=[_reply(
        found={"text": "新版本 10-05 上线了", "url": FOUND_URL}
    )])
    coord = _make(mem_store, settings, env, tools, tasks, goals, models,
                  specialists=sp, outbox=outbox)

    await coord.check_goal(goal_id)

    found = [e for e in outbox.enqueued if e["key"].startswith(f"goal:{goal_id}:found:")]
    assert len(found) == 1
    payload = found[0]["payload"]
    assert payload["push_kind"] == "goal_result"
    # 真 @ 走载荷（QQ 只有 at_user 才真的 @ 到人）；正文里不许再拼「@名字」
    assert payload["at_user"] == "10001"
    assert payload["at_name"] == "小明"
    assert not payload["text"].startswith("@")
    assert "新版本 10-05 上线了" in payload["text"]
    assert f"{PUBLIC_URL}/#/{TOKEN}/tasks/{goal_id}" in payload["text"]
    assert _events(mem_store, goal_id, "goal.found")
    assert mem_store.kv_get(f"goal.found_urls.{goal_id}") == ["example.com/news/1"]


async def test_found_without_who_id_has_no_at(
    mem_store, settings, env, tools, tasks, goals
):
    """管理员直接立的目标没有 who_id → 载荷不带 at_user / at_name，正文照发。"""
    _wire_settings(settings)
    goal_id = goals.create_agent(GID, title="盯梢", body="", criteria=["c"], by_text="")
    sp = _investigation(assessment="有更新", evidence=[FOUND_URL])
    outbox = RecordingOutbox(mem_store)
    models = ModelsQueue(replies=[_reply(found={"text": "出了", "url": FOUND_URL})])
    coord = _make(mem_store, settings, env, tools, tasks, goals, models,
                  specialists=sp, outbox=outbox)
    await coord.check_goal(goal_id)
    found = [e for e in outbox.enqueued if ":found:" in e["key"]]
    assert len(found) == 1
    payload = found[0]["payload"]
    assert "at_user" not in payload and "at_name" not in payload
    assert payload["text"].startswith("出了")


async def test_found_scrubbed_not_sent(
    mem_store, settings, env, tools, tasks, goals
):
    """「盯到了」那句命中关注成员注记片段 → 隐私闸拦下：不发，记 goal.found_rejected scrubbed。"""
    _wire_settings(settings)
    goal_id = goals.create_agent(GID, title="盯梢", body="", criteria=["c"], by_text="")
    with mem_store.tx() as conn:
        conn.execute(
            "INSERT INTO focus_members (group_id, user_id, name, note, pinned, removed, updated)"
            " VALUES (?, 'ufan', '阿帆', '他最近在备考注册建筑师考试，周三晚上没空', 0, 0, 0)",
            (GID,),
        )
    sp = _investigation(assessment="有更新", evidence=[FOUND_URL])
    outbox = RecordingOutbox(mem_store)
    models = ModelsQueue(replies=[_reply(
        found={"text": "阿帆最近在备考注册建筑师考试", "url": FOUND_URL}
    )])
    coord = _make(mem_store, settings, env, tools, tasks, goals, models,
                  specialists=sp, outbox=outbox)
    await coord.check_goal(goal_id)
    assert outbox.enqueued == []
    assert [e.get("reason") for e in _events(mem_store, goal_id, "goal.found_rejected")] == [
        "scrubbed"
    ]


async def test_done_message_carries_at_payload_not_text_at(
    mem_store, settings, env, tools, tasks, goals
):
    """完成话同样：@ 走载荷，正文不带「@名字」。"""
    _wire_settings(settings)
    goal_id = goals.create_agent(
        GID, title="盯暗潮新版本", body="", criteria=["公告出来了"], by_text="",
        who_id="10001", who_name="小明",
    )
    _seed_chat(mem_store, "阿柒", "版本公告已经出来了", ts=NOW - 60)
    outbox = RecordingOutbox(mem_store)
    models = ModelsQueue(replies=[_reply(
        done_criteria=[{"index": 0, "task_id": None, "evidence": "阿柒：版本公告已经出来了"}],
        progress="公告出来了",
    )])
    coord = _make(mem_store, settings, env, tools, tasks, goals, models, outbox=outbox)
    await coord.check_goal(goal_id)
    done = [e for e in outbox.enqueued if e["key"] == f"goal:{goal_id}:done"]
    assert len(done) == 1
    payload = done[0]["payload"]
    assert payload["push_kind"] == "goal_result"
    assert payload["at_user"] == "10001" and payload["at_name"] == "小明"
    assert not payload["text"].startswith("@")
    assert "完成了" in payload["text"]


async def test_found_url_not_in_investigation_rejected(
    mem_store, settings, env, tools, tasks, goals
):
    _wire_settings(settings)
    goal_id = goals.create_agent(GID, title="盯梢", body="", criteria=["c"], by_text="")
    sp = _investigation(assessment="公告页有更新", evidence=[FOUND_URL])
    outbox = RecordingOutbox(mem_store)
    models = ModelsQueue(replies=[_reply(
        found={"text": "听说出了", "url": "https://example.com/编的"}
    )])
    coord = _make(mem_store, settings, env, tools, tasks, goals, models,
                  specialists=sp, outbox=outbox)
    await coord.check_goal(goal_id)

    assert [e for e in outbox.enqueued if ":found:" in e["key"]] == []
    assert [e.get("reason") for e in _events(mem_store, goal_id, "goal.found_rejected")] == [
        "url_not_in_investigation"
    ]


async def test_found_without_investigation_rejected(
    mem_store, settings, env, tools, tasks, goals
):
    """没挂专岗（调查为 None）→ found 一律拒。"""
    _wire_settings(settings)
    goal_id = goals.create_agent(GID, title="盯梢", body="", criteria=["c"], by_text="")
    outbox = RecordingOutbox(mem_store)
    models = ModelsQueue(replies=[_reply(
        found={"text": "听说出了", "url": FOUND_URL}
    )])
    coord = _make(mem_store, settings, env, tools, tasks, goals, models, outbox=outbox)
    await coord.check_goal(goal_id)
    assert outbox.enqueued == []
    assert [e.get("reason") for e in _events(mem_store, goal_id, "goal.found_rejected")] == [
        "no_investigation"
    ]


async def test_found_bad_shape_rejected(mem_store, settings, env, tools, tasks, goals):
    """found 不是对象 / 没 text / url 不是 http(s) → bad_shape。"""
    _wire_settings(settings)
    goal_id = goals.create_agent(GID, title="盯梢", body="", criteria=["c"], by_text="")
    sp = FakeSpecialists(FakeAgents(), results=[
        WorkerReport(ok=True, summary="", data={"assessment": "有更新"}, evidence=[FOUND_URL]),
        WorkerReport(ok=True, summary="", data={"assessment": "有更新"}, evidence=[FOUND_URL]),
        WorkerReport(ok=True, summary="", data={"assessment": "有更新"}, evidence=[FOUND_URL]),
    ])
    outbox = RecordingOutbox(mem_store)
    models = ModelsQueue(replies=[
        _reply(found="出了"),                                     # 不是对象
        _reply(found={"text": "", "url": FOUND_URL}),             # 没正文
        _reply(found={"text": "出了", "url": "不是链接"}),          # url 不是 http(s)
    ])
    coord = _make(mem_store, settings, env, tools, tasks, goals, models,
                  specialists=sp, outbox=outbox)
    for _ in range(3):
        await coord.check_goal(goal_id)
    assert [e for e in outbox.enqueued if ":found:" in e["key"]] == []
    assert [e.get("reason") for e in _events(mem_store, goal_id, "goal.found_rejected")] == [
        "bad_shape", "bad_shape", "bad_shape"
    ]


async def test_found_same_link_second_time_rejected_duplicate(
    fixed_clock, mem_store, settings, env, tools, tasks, goals
):
    _wire_settings(settings)
    goal_id = goals.create_agent(GID, title="盯梢", body="", criteria=["c"], by_text="")
    sp = FakeSpecialists(FakeAgents(), results=[
        WorkerReport(ok=True, summary="", data={"assessment": "有更新"}, evidence=[FOUND_URL]),
        WorkerReport(ok=True, summary="", data={"assessment": "有更新"}, evidence=[FOUND_URL]),
    ])
    outbox = RecordingOutbox(mem_store)
    found_reply = _reply(found={"text": "出了", "url": FOUND_URL})
    models = ModelsQueue(replies=[found_reply, found_reply])
    coord = _make(mem_store, settings, env, tools, tasks, goals, models,
                  specialists=sp, outbox=outbox)

    await coord.check_goal(goal_id)
    assert len([e for e in outbox.enqueued if ":found:" in e["key"]]) == 1

    fixed_clock[0] = NOW + DAY                    # 第二天：同一条链接仍不重复报
    await coord.check_goal(goal_id)
    assert len([e for e in outbox.enqueued if ":found:" in e["key"]]) == 1
    reasons = [e.get("reason") for e in _events(mem_store, goal_id, "goal.found_rejected")]
    assert "duplicate" in reasons


async def test_found_second_same_day_rejected_daily_limit(
    mem_store, settings, env, tools, tasks, goals
):
    _wire_settings(settings)
    goal_id = goals.create_agent(GID, title="盯梢", body="", criteria=["c"], by_text="")
    sp = FakeSpecialists(FakeAgents(), results=[
        WorkerReport(ok=True, summary="", data={"assessment": "有更新"},
                     evidence=[FOUND_URL, OTHER_INV_URL]),
        WorkerReport(ok=True, summary="", data={"assessment": "又有更新"},
                     evidence=[FOUND_URL, OTHER_INV_URL]),
    ])
    outbox = RecordingOutbox(mem_store)
    models = ModelsQueue(replies=[
        _reply(found={"text": "出了", "url": FOUND_URL}),
        _reply(found={"text": "又出了", "url": OTHER_INV_URL}),
    ])
    coord = _make(mem_store, settings, env, tools, tasks, goals, models,
                  specialists=sp, outbox=outbox)

    await coord.check_goal(goal_id)
    await coord.check_goal(goal_id)

    found = [e for e in outbox.enqueued if ":found:" in e["key"]]
    assert len(found) == 1
    assert found[0]["key"] == f"goal:{goal_id}:found:{clock.day_key(NOW)}"
    reasons = [e.get("reason") for e in _events(mem_store, goal_id, "goal.found_rejected")]
    assert reasons == ["daily_limit"]


# ---------------------------------------------------------------------------
# 6. 补判 fit（R5 末条）
# ---------------------------------------------------------------------------


async def test_fit_asked_when_empty_and_saved(mem_store, settings, env, tools, tasks, goals):
    _wire_settings(settings)
    goal_id = goals.create_agent(GID, title="分线开档打卡", body="", criteria=["c"], by_text="")
    models = ModelsQueue(replies=[_reply(
        fit={"kind": "human", "reason": "要群友自己开档截图到场"}
    )])
    coord = _make(mem_store, settings, env, tools, tasks, goals, models)

    await coord.check_goal(goal_id)

    prompt = _prompt(models)
    assert '"fit"' in prompt and "online" in prompt and "human" in prompt
    assert "群友自己投票、开档、截图、到场" in prompt
    row = _goal_row(mem_store, goal_id)
    assert row["fit"] == "human"
    assert "开档" in row["fit_reason"]


async def test_fit_not_asked_when_already_judged(mem_store, settings, env, tools, tasks, goals):
    _wire_settings(settings)
    goal_id = goals.create_agent(GID, title="盯官方公告", body="", criteria=["c"], by_text="")
    goals.set_fit(goal_id, "online", "MaiWork 上网就能盯")
    models = ModelsQueue(replies=[_reply(fit={"kind": "human", "reason": "乱回"})])
    coord = _make(mem_store, settings, env, tools, tasks, goals, models)

    await coord.check_goal(goal_id)

    assert '"fit"' not in _prompt(models)
    assert _goal_row(mem_store, goal_id)["fit"] == "online"     # 已有值不改


# ---------------------------------------------------------------------------
# 7. delivery：goal_result 不占额度、仍守睡觉时段 / 服务群
# ---------------------------------------------------------------------------


def _push_env(tmp_path, *, serve=(GID,)):
    store = Store(tmp_path / "push.db")
    store.migrate()
    settings, problems = load_settings({
        "environments": {"workspace_root": str(tmp_path)},
        "groups": {"serve": [{"group": f"qq:{g}"} for g in serve]},
    })
    assert not problems, problems
    return store, settings, Pushes(store, lambda: settings)


class _PushHost:
    """假宿主：只记 send_text（含 at_user / at_name 真 @ 参数）。"""

    def __init__(self):
        self.texts: list[dict] = []

    async def send_text(self, session_id, text, *, reply_to="", at_user="", at_name=""):
        self.texts.append({
            "session_id": session_id, "text": text, "reply_to": reply_to,
            "at_user": at_user, "at_name": at_name,
        })
        return SimpleNamespace(sent=True, message_id="m1")

    async def upload_group_file(self, group_id, path, name):
        return "f1"


def _ts(hour: int, minute: int = 0) -> float:
    return datetime(2026, 10, 15, hour, minute, tzinfo=BJ).timestamp()


async def test_goal_result_is_quota_free_but_keeps_quiet_hours(tmp_path):
    store, settings, pushes = _push_env(tmp_path)
    noon, sleep = _ts(12), _ts(23, 30)

    assert "goal_result" in TASK_QUOTA_FREE_KINDS
    assert task_quota_free_kind(None, "goal_result") == "goal_result"
    assert PUSH_KIND_CN["goal_result"] == "目标结果"

    group_push.set_config(
        store, GID,
        {"daily_max": 5, "quiet_hours": "23:00-08:00", "news_card_enabled": True},
        settings, now=noon - 10,
    )
    for i in range(5):                      # 每日额度用满
        pushes.record(GID, "topic", f"已经发过的第 {i + 1} 条", noon)

    assert pushes.can_push(GID, "topic", noon) == (False, "今天推够了")
    assert pushes.can_push(GID, "goal_result", noon) == (True, "")   # 不占额度、不被额度挡
    assert pushes.can_push(GID, "goal_result", sleep) == (False, "睡觉时段")
    assert pushes.can_push(GONE, "goal_result", noon) == (False, UNSERVED_REASON)

    pushes.record(GID, "goal_result", "目标结果一条", noon)
    assert pushes.count_used(GID, noon) == 5        # 目标结果没占额度
    store.close()


async def test_outbox_flush_goal_result_sends_real_at(tmp_path):
    """整条链路：goal_result + at_user 的行照常发出，host.send_text 收到真 @ 参数。

    （顺带锁住：个人提一嘴的复核闸只认 key=idea_mention:…，goal 消息带 at_user 不会被误拦。）
    """
    store, settings, pushes = _push_env(tmp_path)
    host = _PushHost()
    ob = Outbox(store, host, pushes, Mentions(store, lambda: settings), lambda: settings)
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, session_id) VALUES (?, ?)", (GID, "sess-1")
        )
    noon = _ts(12)
    ob.enqueue(
        f"goal:G-1:found:{clock.day_key(noon)}", GID, "text",
        {
            "text": "新版本出了\nhttps://mw.example/#/tok/tasks/G-1",
            "push_kind": "goal_result", "at_user": "10001", "at_name": "小明",
        },
    )
    await ob.flush(noon)

    assert len(host.texts) == 1
    assert host.texts[0]["at_user"] == "10001"
    assert host.texts[0]["at_name"] == "小明"
    assert "新版本出了" in host.texts[0]["text"]
    row = store.read().execute(
        "SELECT status, kind FROM outbox WHERE key=?", (f"goal:G-1:found:{clock.day_key(noon)}",)
    ).fetchone()
    assert str(row["status"]) == "sent"
    assert str(row["kind"]) == "text"
    store.close()
