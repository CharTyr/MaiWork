"""工作交付粗糙度整改回归测试：
1. SessionSpeech：拦任务号/验收话术/半截断尾，提示不塞 task_id，max_tokens 充足，
   同版本返工不重复发开场白，text 交付不把说明+总结+正文叠成三遍，brief 首轮不追问收尾。
2. 群聊 text 调研：走聊天直答规范，不套六段报告八股和「（单源）/（多源）」核验标签。
3. 群内失败通知 / 等人提问 / 6 小时提醒：刮掉 R1/判定没做到/验收模型原话/告诉验收，句读收尾。
4. 两步交接保住原始 https 链接 + 下游分页读全；成品闸拦「调研记录未收录 / 本页不编造链接 / 已打开率」。
5. 群文件载体边界写清，不把排障卡或百字短文案引导成裸 .md/.txt 群文件。
6. 控制台任务详情「成果」只列真实成品与 webonly 兜底，排除会话发言/说明/失败/提问，text 标「群内回复」且不把整段正文绑成单条外链。
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from CharTyr_MaiWork.maiwork import clock, deliverable_check
from CharTyr_MaiWork.maiwork.outbox import Delivery
from CharTyr_MaiWork.maiwork.session_speech import SessionSpeech, gate_text
from CharTyr_MaiWork.maiwork.tools import Tools

from test_outbox import _make as _make_outbox, _seed_task
from test_session_delivery import coordinator as _session_coordinator
from test_session_runner import GID, linked_task, open_idle, rig  # noqa: F401
from test_task_answer import G1, _app
from tests.test_coordinator import (  # noqa: F401
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

pytestmark = pytest.mark.asyncio


def _speech(r, reply_text: str = "帮你查好了，都在下面。"):
    models = SimpleNamespace(
        chat=AsyncMock(return_value=SimpleNamespace(text=reply_text, tool_calls=[]))
    )
    identity = SimpleNamespace(prompt_block=lambda key: "# MaiWork 的身份\n说话干脆，语气轻松。")
    s = SessionSpeech(r.store, r.outbox, lambda: r.settings, models=models, identity=identity)
    return s, models


# ===========================================================================
# 1. SessionSpeech 门禁、token 预算、开场去重与 text 交付去重
# ===========================================================================


async def test_gate_text_rejects_task_id_review_jargon_and_truncated_tail():
    r_tid = gate_text("T-19 交付了：字节跳动查好了。")
    assert not r_tid.text and r_tid.reason == "internal"

    r_jargon = gate_text("这轮验收通过了，交付说明：官网和公众号都在下面。")
    assert not r_jargon.text and r_jargon.reason == "internal"

    r_judge = gate_text("R1 没过（判定没做到），验收模型原话：缺链接。")
    assert not r_judge.text and r_judge.reason == "internal"

    r_trunc = gate_text("字节跳动客服电话是 400-8899-")
    assert not r_trunc.text and r_trunc.reason == "truncated"

    r_clean = gate_text("字节跳动没有统一公开总机，官网和招聘公众号都在下面。")
    assert r_clean.text and not r_clean.reason


async def test_natural_omits_task_id_from_facts_and_uses_ample_max_tokens(rig):
    sid = open_idle(rig)
    tid = linked_task(rig, sid)
    s, models = _speech(rig, reply_text="这活儿我接下了，查好跟你说。")
    await s.start(rig.tasks.get(tid))

    assert models.chat.await_count == 1
    msgs = models.chat.await_args.args[1]
    kwargs = models.chat.await_args.kwargs
    prompt_body = "\n".join(str(m.get("content") or "") for m in msgs)
    assert tid not in prompt_body
    assert "task_id" not in prompt_body
    assert kwargs["task_id"] == tid
    assert kwargs["max_tokens"] >= 1024


async def test_start_deduplicates_across_attempts_of_same_req_version(rig):
    sid = open_idle(rig)
    tid = linked_task(rig, sid, status="running")
    s, models = _speech(rig, reply_text="这活儿接下了，马上查。")
    await s.start(rig.tasks.get(tid))
    rows = rig.store.read().execute("SELECT key FROM outbox WHERE task_id=?", (tid,)).fetchall()
    assert len(rows) == 1
    # 同一版（req_version=1）第 2 次尝试重跑：不再发第二条开场白
    await s.start(rig.tasks.get(tid))
    assert models.chat.await_count == 1
    rows = rig.store.read().execute("SELECT key FROM outbox WHERE task_id=?", (tid,)).fetchall()
    assert len(rows) == 1

    # 用户追问升到第 2 版（req_version=2）：允许新回合发开场回应
    with rig.store.tx() as conn:
        conn.execute("UPDATE tasks SET req_version=2 WHERE id=?", (tid,))
    await s.start(rig.tasks.get(tid))
    assert models.chat.await_count == 2


async def test_finish_deduplicates_overlapping_note_for_non_text(rig):
    sid = open_idle(rig)
    tid = linked_task(rig, sid)
    s, _models = _speech(
        rig,
        reply_text="字节跳动校招投递入口整理好了，点开就能看。这段工作要不要先收尾？",
    )
    out = await s.finish(
        rig.tasks.get(tid), "字节跳动校招投递入口整理好了，点开就能看。", deliver_kind="view"
    )
    assert out.count("字节跳动校招投递入口整理好了") == 1
    assert "要不要先收尾" in out


async def test_session_text_delivery_brief_turn1_sends_clean_reply_only(rig, tmp_path: Path):
    """复现 T-18：brief + text 首轮直答，不叠 60 字说明、不说「链接在上面」、不追问收尾。"""
    rig.settings = replace(
        rig.settings,
        workspace_root=tmp_path / "work",
        environments=replace(rig.settings.environments, workspace_root=tmp_path / "work", local_mode="direct"),
    )
    sid = open_idle(rig)
    tid = linked_task(rig, sid, status="reviewing")
    s, models = _speech(rig, reply_text="链接在上面，这段工作要不要先收尾？再@我确认。")
    coord = _session_coordinator(rig, outbox=rig.outbox)
    coord._session_speech = s  # noqa: SLF001
    ws_name = str(rig.tasks.get(tid)["workspace"])
    path = coord._env.workspace(ws_name) / "artifacts" / tid / "reply.md"  # noqa: SLF001
    path.parent.mkdir(parents=True, exist_ok=True)
    reply_body = (
        "Anthropic 刚发布了 Claude Mythos Preview，定位前沿网络安全研究模型，"
        "目前只向 Project Glasswing 合作方开放，未对普通用户开放 API。\n"
        "官方说明：https://www.anthropic.com/glasswing"
    )
    path.write_text(reply_body, encoding="utf-8")

    await coord._handle_passed(  # noqa: SLF001
        tid, GID, ws_name,
        {"deliver_kind": "text", "scale": "brief"},
        {"review": "通过", "note": "Claude Mythos Preview 不对公众开放，链接在上面。", "artifact": f"artifacts/{tid}/reply.md"},
    )

    row = rig.store.read().execute(
        "SELECT payload FROM outbox WHERE task_id=? AND key LIKE '%:text'", (tid,)
    ).fetchone()
    assert row is not None
    sent_text = json.loads(row["payload"])["text"]
    assert sent_text == reply_body
    assert "链接在上面" not in sent_text
    assert "要不要先收尾" not in sent_text
    assert models.chat.await_count == 0


async def test_session_text_delivery_standard_does_not_repeat_review_note(rig, tmp_path: Path):
    """复现 T-19：standard + text 会话交付可带一句简短过渡，但绝不把 review.note 再拼一遍。"""
    rig.settings = replace(
        rig.settings,
        workspace_root=tmp_path / "work",
        environments=replace(rig.settings.environments, workspace_root=tmp_path / "work", local_mode="direct"),
    )
    sid = open_idle(rig)
    tid = linked_task(rig, sid, status="reviewing")
    s, _models = _speech(rig, reply_text="帮你查好了，都在下面。")
    coord = _session_coordinator(rig, outbox=rig.outbox)
    coord._session_speech = s  # noqa: SLF001
    ws_name = str(rig.tasks.get(tid)["workspace"])
    path = coord._env.workspace(ws_name) / "artifacts" / tid / "reply.md"  # noqa: SLF001
    path.parent.mkdir(parents=True, exist_ok=True)
    reply_body = "字节跳动没有统一对外总机，招聘可走 https://jobs.bytedance.com/ 投递。"
    path.write_text(reply_body, encoding="utf-8")

    review_note = "字节跳动官网和招聘公众号都在下面，没有统一公开总机。"
    await coord._handle_passed(  # noqa: SLF001
        tid, GID, ws_name,
        {"deliver_kind": "text", "scale": "standard"},
        {"review": "通过", "note": review_note, "artifact": f"artifacts/{tid}/reply.md"},
    )

    row = rig.store.read().execute(
        "SELECT payload FROM outbox WHERE task_id=? AND key LIKE '%:text'", (tid,)
    ).fetchone()
    assert row is not None
    sent_text = json.loads(row["payload"])["text"]
    assert "帮你查好了，都在下面。" in sent_text
    assert reply_body in sent_text
    assert review_note not in sent_text


# ===========================================================================
# 2. 群聊 text 调研去八股化与篇幅收口
# ===========================================================================


async def test_text_research_job_uses_chat_framework_without_six_section_scaffolding(
    mem_store, settings, env, tasks, goals
):
    tid = _create_task(tasks, req="搜搜 Claude Mythos 发布了啥", reply=None)
    workers = FakeWorkers()
    coord = _build(
        mem_store=mem_store, settings=settings, env=env, tools=Tools(mem_store),
        tasks=tasks, goals=goals, models=ModelsQueue(), workers=workers,
    )
    ws_name = str(tasks.get(tid)["workspace"])

    await coord._run_job(  # noqa: SLF001
        brief="查一下 Claude Mythos", tools=["web_search", "fetch_page", "write_file"],
        gid=GID, tid=tid, job_idx=1, ws_name=ws_name,
        job_type="research", deliver_kind="text",
    )
    extra_text = str(workers.calls[0]["system_extra"])
    assert "大家都同意的" not in extra_text
    assert "吐槽最多的" not in extra_text
    assert "单源" in extra_text and ("不要" in extra_text or "不标" in extra_text)

    # 中间步骤（steps_dir 非空）或 view 交付照旧给完整六段调研框架
    workers.calls.clear()
    await coord._run_job(  # noqa: SLF001
        brief="查一下 Claude Mythos", tools=["web_search", "fetch_page", "write_file"],
        gid=GID, tid=tid, job_idx=1, ws_name=ws_name,
        job_type="research", deliver_kind="text", steps_dir=f"artifacts/{tid}/steps/1",
    )
    extra_steps = str(workers.calls[0]["system_extra"])
    assert "一句话结论" in extra_steps and "大家都同意的" in extra_steps


async def test_enrich_brief_text_forbids_report_headings_and_caps_brief_scale(
    mem_store, settings, env, tools, tasks, goals
):
    coord = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools,
        tasks=tasks, goals=goals, models=ModelsQueue(), workers=FakeWorkers(),
    )
    brief = coord._enrich_brief("查一下", "T-18", "text", scale="brief")  # noqa: SLF001
    assert "单源" in brief
    assert "一句话结论" in brief
    assert "300" in brief


# ===========================================================================
# 3. 失败通知、人工等待提问与 6 小时提醒改说人话
# ===========================================================================


async def test_handle_unpassed_humanizes_r_codes_and_reviewer_jargon(
    mem_store, settings, env, tools, tasks, goals
):
    """复现 T-17：群失败消息不露 R1 /（判定没做到）/（验收模型原话：…）/ 引用核对 / 半截字。"""
    tid = _create_task(tasks, title="找构图相似的真人图", req="找这张图的真人原图")
    tasks.transition(tid, "running")
    tasks.transition(tid, "reviewing")
    outbox = FakeOutbox()
    coord = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools,
        tasks=tasks, goals=goals, models=ModelsQueue(), workers=FakeWorkers(), outbox=outbox,
    )
    raw_review = (
        "没过：R1 找到与原图构图高度相似的真人照片（判定没做到）；"
        "加分项没做到：R2 给出图片来源链接"
        "（验收模型原话：没过：全网搜图均未找到匹配的真人原图，子agent已穷尽公开检索手段仍无果，核心需求未达成。）\n"
        "引用核对：0 条链接，0 条没打开过"
    )
    coord._handle_unpassed(tid, 3, GID, raw_review, "image")  # noqa: SLF001

    text = outbox.enqueued[-1]["payload"]["text"]
    assert text.startswith("「找构图相似的真人图」没做成：")
    assert "未找到匹配的真人原图" in text
    for bad in ("R1", "R2", "判定没做到", "加分项没做到", "验收模型原话", "引用核对", "子agent"):
        assert bad not in text


async def test_ask_human_strips_reviewer_jargon_and_cuts_at_punctuation(
    mem_store, settings, env, tools, tasks, goals
):
    """复现 T-7：needs_human 里的「并告诉验收」刮掉，超长在句读收尾不留半截词。"""
    tid = _create_task(tasks, title="改图", req="把图改凶一点", requester_id="u1", requester_name="小明")
    tasks.transition(tid, "running")
    aid = tasks.start_attempt(tid)
    outbox = FakeOutbox()
    coord = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools,
        tasks=tasks, goals=goals, models=ModelsQueue(), workers=FakeWorkers(), outbox=outbox,
    )
    coord._ask_human(  # noqa: SLF001
        tid, GID, attempt_id=aid, attempt_n=1,
        human_items=[{
            "id": "R1",
            "text": "在原图基础上把角色表情改凶狠并加上台词文字",
            "needs_human": (
                "请发起人重新发送那张图的清晰原图或无压缩大图，收到后按原图重改并告诉验收；"
                "如果有多张不同清晰度的原图版本也可以一并发来方便比对画面细节；"
                "另外如果有角色设定图或参考表情图也可以顺手附上作为辅助参考材料避免走样变形或者漏掉关键配饰角落元素与文字边框阴影细节以及角色瞳孔高光颜色导致画面模糊不清"
            ),
        }],
    )
    q = str(tasks.get(tid)["question"])
    assert "告诉验收" not in q
    assert "画面模糊不清" not in q  # 超出 200 字预算的部分在句读处收尾，不硬截半截词
    assert "比对画面细节。" in q
    assert "R1" in q
    assert q.endswith("做完后回复我结果，我接着做。")


async def test_wait_remind_cuts_long_question_at_punctuation(tmp_path: Path):
    app = _app(tmp_path)
    await app.start()
    try:
        now = clock.now()
        long_q = (
            "这一步需要有人参与：R1 在原图基础上把角色改成凶狠表情并加上台词——"
            "请发起人重新发送那张图的原图或大图，收到后按原图重改。准备好的材料在任务页里。做完后回复我结果，我接着做。"
        )
        tid = app.tasks.create(G1, title="改图", req="改图", criteria=[], source="user", status="queued")
        app.tasks.transition(tid, "running")
        app.tasks.transition(tid, "waiting_input", question=long_q, question_ts=now - 7 * 3600)
        app._tasks_round(now)  # noqa: SLF001

        row = app.store.read().execute(
            "SELECT payload FROM outbox WHERE key=?", (f"task-wait-remind:{tid}",)
        ).fetchone()
        assert row is not None
        remind_text = json.loads(row["payload"])["text"]
        # 句读收尾：以完整句号接提示括号，不出现半句残词直接拼括号
        assert "。（引用这条消息回复我就行）" in remind_text
        assert remind_text.endswith("（引用这条消息回复我就行）")
    finally:
        await app.stop()


# ===========================================================================
# 4. 两步调研交接保住链接 + 成品闸拦过程自白 + 群文件载体边界
# ===========================================================================


async def test_steps_brief_requires_urls_and_handoff_reminds_pagination(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, req="对比几款阅读器")
    ws_path = env.workspace(str(tasks.get(tid)["workspace"]))
    step_file = ws_path / "artifacts" / tid / "steps" / "1" / "notes.md"
    step_file.parent.mkdir(parents=True, exist_ok=True)
    step_file.write_text("资料：https://example.com/review", encoding="utf-8")

    coord = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools,
        tasks=tasks, goals=goals, models=ModelsQueue(), workers=FakeWorkers(),
    )
    step_brief = coord._enrich_brief(  # noqa: SLF001
        "搜集评测", tid, "view", steps_dir=f"artifacts/{tid}/steps/1"
    )
    assert "https://" in step_brief and "链接" in step_brief

    from CharTyr_MaiWork.maiwork.workers import WorkerReport

    rep = WorkerReport(
        ok=True, summary="搜好了",
        evidence=[f"artifacts/{tid}/steps/1/notes.md"],
        data={"artifacts": [f"artifacts/{tid}/steps/1/notes.md"]},
    )
    handed = coord._add_dep_handoff_to_brief(  # noqa: SLF001
        "做成对比网页", [1], [rep], ws_name=str(tasks.get(tid)["workspace"]), tid=tid
    )
    assert "offset" in handed


async def test_deliverable_check_catches_process_confessions_and_audit_jargon():
    """复现 T-16 / T-1：成品里写「这次调研记录未收录 / 本页不编造链接 / 已打开率」一律拦下。"""
    s1 = deliverable_check.scan_text("外媒评测这次调研记录未收录具体链接，本页不编造链接。")
    assert deliverable_check.has_issues(s1)
    assert s1["placeholder"]

    s2 = deliverable_check.scan_text("本报告链接已打开率 100%，详见 README-断档说明。")
    assert deliverable_check.has_issues(s2)
    assert s2["internal"]


async def test_plan_and_file_brief_clarify_formal_file_boundary(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, req="做一张排障速查卡")
    models = ModelsQueue(replies=[json.dumps({
        "criteria": ["做排障卡"], "deliver_kind": "view",
        "jobs": [{"brief": "做排障卡", "tools": ["write_file"]}], "question": None,
    }, ensure_ascii=False)])
    coord = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools,
        tasks=tasks, goals=goals, models=models, workers=FakeWorkers(),
    )
    await coord._plan(tasks.get(tid))  # noqa: SLF001
    plan_prompt = models.calls[-1][1][-1]["content"]
    assert ".docx" in plan_prompt and ".md" in plan_prompt

    file_brief = coord._enrich_brief("整理成文档", tid, "file")  # noqa: SLF001
    assert ".md" in file_brief and ".docx" in file_brief


# ===========================================================================
# 5. 控制台任务详情「成果」过滤与标签修复
# ===========================================================================


async def test_delivery_records_filters_non_artifacts_and_labels_text_and_image(tmp_path: Path):
    store, _cfg, _host, _pushes, _mentions, ob = _make_outbox(tmp_path)
    _seed_task(store, "T-1")
    _seed_task(store, "T-2")
    _seed_task(store, "T-3")
    delivery = Delivery(store, ob)

    # 非成品行：会话开场白、进展、提问、等待提醒、失败告知、交付跟进说明
    ob.enqueue("session:speech:S1:1:start", GID, "text", {"text": "接下了", "push_kind": "command"}, task_id="T-1")
    ob.enqueue("session:speech:S1:1:progress:1", GID, "text", {"text": "还在查", "push_kind": "status"}, task_id="T-1")
    ob.enqueue("ask:T-1:1", GID, "text", {"text": "问一句", "push_kind": "status"}, task_id="T-1")
    ob.enqueue("human:T-1:1", GID, "text", {"text": "要人帮忙", "push_kind": "status"}, task_id="T-1")
    ob.enqueue("task-wait-remind:T-1", GID, "text", {"text": "还在等", "push_kind": "status"}, task_id="T-1")
    ob.enqueue("task:T-1:failed", GID, "text", {"text": "没做成", "push_kind": "status"}, task_id="T-1")
    ob.enqueue("task:T-1:deliver:note", GID, "text", {"text": "点开就能看", "push_kind": "delivery"}, task_id="T-1")

    # 真正的文字成品（里面带一个参考链接，不能把整段 200 字回复劫持成单条外链）
    reply_msg = "结论如下：\n1. 官方入口在 https://example.com/guide.pdf\n2. 无需注册即可查看。"
    ob.enqueue("task:T-1:deliver:text", GID, "text", {"text": reply_msg, "push_kind": "delivery"}, task_id="T-1")

    recs = delivery.delivery_records("T-1")
    assert [r["key"] for r in recs] == ["task:T-1:deliver:text"]
    assert recs[0]["kind"] == "群内回复"
    assert recs[0]["url"] is None
    assert recs[0]["text"] == reply_msg

    # 图片成品标「图片」
    img_path = tmp_path / "card.png"
    img_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    ob.enqueue(
        "task:T-2:deliver", GID, "image",
        {"path": str(img_path), "note": "画好了", "push_kind": "delivery"}, task_id="T-2",
    )
    img_recs = delivery.delivery_records("T-2")
    assert len(img_recs) == 1
    assert img_recs[0]["kind"] == "图片"

    # 双渠道都挂掉时的 :webonly 兜底仍标「网页副本」
    ob.enqueue(
        "task:T-3:deliver:webonly", GID, "text",
        {"text": "做好了，但发群文件和网页都没成功，成品在 MaiWork 网页里", "push_kind": "delivery"},
        task_id="T-3",
    )
    web_recs = delivery.delivery_records("T-3")
    assert len(web_recs) == 1
    assert web_recs[0]["kind"] == "网页副本"


async def test_detail_js_renders_text_and_image_deliveries_cleanly():
    js = (
        Path(__file__).resolve().parent.parent
        / "maiwork" / "console" / "static" / "js" / "detail.js"
    ).read_text(encoding="utf-8")
    assert "群内回复" in js
    assert "图片" in js
    assert "deliv:" in js
