"""T-11 质量闸回归（线上真实失败样例：《Z.A.T.O.》防剧透入门表）。

线上发生了什么：主模型计划只派两步（第 1 步调研 brief 只写「核实免费/DLC/时长/分支/汉化」，
第 2 步制作 brief 明写「不另扩搜」），成品里关键节点、结局、电波梗三块写成
「job1 的四项核实未覆盖该范围……不另扩搜……待补充：需另开一次调研」；验收模型却判全部通过。
成品里还出现了 job1 / research.md / T-11 这些群友看得见的内部用语。

三条红线回归：
- A 计划覆盖：每条**原话**必须项都要有一步负责取材/产出；job 可用 `covers` 声明；
  有 job 给了 covers 时，代码把没被覆盖的原话必须项补进「第一条调研活」的 brief。
- B 验收不认留空：原话必须项 met=true 但证据是「没去做」的写法 → 判没做到；
  「查过但查不到并写清查过哪里」是合法的。
- C 成品扫描：最终成品文本里出现留空标记或内部用语 → 即使模型判过也改判不通过。

模型桩（ModelsQueue / FakeWorkers / _build / _create_task 等）复用 tests/test_coordinator.py。
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from tests.test_coordinator import (  # noqa: F401  (fixtures)
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


# ---------------------------------------------------------------------------
# T-11 的真实文字
# ---------------------------------------------------------------------------

# 锁定的需求清单（kv task.requirements.T-11）：
# R1【原话·实做】核实免费、时长7-8小时、分支与汉化状态并附依据
# R2【原话·文稿】按汉化/生肉状态整理关键节点并分级遮挡
# R3【原话·文稿】写电波梗简介与结局避雷且遮挡剧透
# R4【原话·文稿】存成一份可留下的入门表DOC成品
# R5【补充·实做】DOC放artifacts下引用链接逐条打开核实
# R0【底线】内容真实、不编造；交付物能正常打开
T11_RAW_REQS = [
    {"text": "核实免费、时长7-8小时、分支与汉化状态并附依据", "origin": "原话", "kind": "实做"},
    {"text": "按汉化/生肉状态整理关键节点并分级遮挡", "origin": "原话", "kind": "文稿"},
    {"text": "写电波梗简介与结局避雷且遮挡剧透", "origin": "原话", "kind": "文稿"},
    {"text": "存成一份可留下的入门表DOC成品", "origin": "原话", "kind": "文稿"},
    {"text": "DOC放artifacts下引用链接逐条打开核实", "origin": "补充", "kind": "实做"},
]

T11_REQ_TEXT = "帮我们做一页《Z.A.T.O.》的防剧透入门表"

# 线上成品里三块「没做」的原话（真实措辞）
T11_EMPTY_CLAUSES = [
    "job1 的四项核实未覆盖该范围，且本次不另扩搜，故不展开、不编造；"
    "如需逐章节点表请另开一次调研",
    "job1 research.md 未收录；本次任务要求不另扩搜，故不编造｜待补充：需另开一次调研",
]

T11_DELIVERABLE_TEXT = (
    "《Z.A.T.O.》防剧透入门表\n"
    "一、免费与时长：免费；时长约 7-8 小时；分支 3 条；汉化：民间汉化进行中。\n"
    "二、关键节点：" + T11_EMPTY_CLAUSES[0] + "\n"
    "三、结局避雷：" + T11_EMPTY_CLAUSES[1] + "\n"
    "四、电波梗：同上。\n"
)

# 合法写法：「查不到」+ 写清查过哪里（不许被当成留空）
T11_LEGIT_NOT_FOUND_TEXT = (
    "《Z.A.T.O.》防剧透入门表\n"
    "一、免费与时长：免费；时长约 7-8 小时。\n"
    "二、汉化状态：查过官网、Steam 商店页和作者推特（2026-10-05 逐个打开），"
    "都没查到官方中文发售信息；民间汉化进度只在贴吧见到一条未确认的说法，已在表里如实写明。\n"
    "三、关键节点：按汉化/生肉两条线列出，剧透部分折叠遮挡。\n"
)


def _req():
    from CharTyr_MaiWork.maiwork import requirements

    return requirements


def _deliverable_check():
    from CharTyr_MaiWork.maiwork import deliverable_check

    return deliverable_check


def _t11_items():
    return _req().normalize_requirements(T11_RAW_REQS, T11_REQ_TEXT)


def _coordinator(mem_store, settings, env, tools, tasks, goals, replies, workers=None):
    return _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=ModelsQueue(replies=replies), workers=workers or FakeWorkers(),
    )


def _locked_plan(items, *, deliver_kind="view", jobs=None, research=False):
    return {
        "criteria": _req().criteria_texts(items),
        "requirements": items,
        "deliver_kind": deliver_kind,
        "scale": "standard",
        "jobs": jobs if jobs is not None else [
            {"brief": "干这条活", "tools": ["write_file"], "type": "other",
             "after": [], "agent": "task"}
        ],
        "question": "",
        "research": research,
    }


def _review_json(items, *, pass_=True, artifact="", review="看着不错", note="入门表做好了"):
    return json.dumps(
        {"pass": pass_, "review": review, "missing": [], "artifact": artifact, "note": note,
         "items": items},
        ensure_ascii=False,
    )


def _write_artifact(env, task, tid, name, text):
    ws = env.workspace(task["workspace"])
    d = ws / "artifacts" / tid
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")
    return f"artifacts/{tid}/{name}"


# ---------------------------------------------------------------------------
# A. requirements.judge 不认「留空」（只针对原话必须项）
# ---------------------------------------------------------------------------


async def test_judge_placeholder_evidence_on_original_item_is_not_met():
    """原话必须项 met=true，但证据是「没覆盖 / 不另扩搜 / 待补充」→ 判没做到。"""
    r = _req()
    items = _t11_items()
    verdicts = [
        {"id": "R1", "met": True, "evidence": "第二节汉化状态明文，附两条链接"},
        {"id": "R2", "met": True,
         "evidence": "HTML 第二节写了 L1-A/L1-B details 折叠；缺逐章节点：未覆盖该范围，不另扩搜"},
        {"id": "R3", "met": True, "evidence": "job1 research.md 未收录；待补充：需另开一次调研"},
        {"id": "R4", "met": True, "evidence": "artifacts/T-11/index.html"},
        {"id": "R5", "met": True, "evidence": "链接逐条打开核实"},
        {"id": "R0", "met": True, "evidence": "页面能打开"},
    ]
    j = r.judge(items, verdicts)
    assert j["pass"] is False
    unmet = {u["id"]: u["why"] for u in j["unmet_blocking"]}
    assert unmet.get("R2") == r.WHY_PLACEHOLDER
    assert unmet.get("R3") == r.WHY_PLACEHOLDER
    assert "没覆盖" in r.WHY_PLACEHOLDER


async def test_judge_not_found_but_says_where_it_looked_is_still_met():
    """「查过 A、B 都没查到，已写明查过哪里」是合法证据，不许当留空。"""
    r = _req()
    items = _t11_items()
    verdicts = [
        {"id": "R1", "met": True,
         "evidence": "查过官网、Steam 商店页、作者推特（逐个打开），都没查到官方中文；已写明查过哪里"},
        {"id": "R2", "met": True, "evidence": "按汉化/生肉两条线列了节点，剧透折叠"},
        {"id": "R3", "met": True, "evidence": "写了电波梗简介，结局单独折叠避雷"},
        {"id": "R4", "met": True, "evidence": "artifacts/T-11/index.html"},
        {"id": "R5", "met": True, "evidence": "链接逐条打开核实"},
        {"id": "R0", "met": True, "evidence": "页面能打开"},
    ]
    j = r.judge(items, verdicts)
    assert j["pass"] is True
    assert j["unmet_blocking"] == []
    # 「查不到」本身不是标记词
    assert r.placeholder_marker("查不到官方中文") == ""
    assert r.placeholder_marker("这条还没确认，等作者回复") == ""


async def test_judge_placeholder_rule_skips_floor_and_bonus():
    """底线 R0 和补充项不受留空规则影响（原话没有的降不了级，补充没做到也不影响通过）。"""
    r = _req()
    items = _t11_items()
    verdicts = [
        {"id": "R1", "met": True, "evidence": "免费；时长 7-8 小时"},
        {"id": "R2", "met": True, "evidence": "节点表已列"},
        {"id": "R3", "met": True, "evidence": "结局避雷已写"},
        {"id": "R4", "met": True, "evidence": "DOC 成品在任务里"},
        {"id": "R5", "met": True, "evidence": "此处不写：引用核对留待后续"},
        {"id": "R0", "met": True, "evidence": "占位"},
    ]
    j = r.judge(items, verdicts)
    assert j["pass"] is True
    assert [u["id"] for u in j["unmet_bonus"]] == []


# ---------------------------------------------------------------------------
# B. deliverable_check：成品文本扫描（留空标记 + 内部用语）
# ---------------------------------------------------------------------------


async def test_scan_flags_t11_empty_clauses_and_internal_words():
    dc = _deliverable_check()
    got = dc.scan_text(T11_DELIVERABLE_TEXT, task_id="T-11")
    assert got["placeholder"], "留空标记必须命中"
    joined = " ".join(got["placeholder"])
    assert "未覆盖" in joined
    assert "不另扩搜" in joined or "待补充" in joined
    assert got["internal"], "内部用语必须命中"
    internal = " ".join(got["internal"])
    assert "job1" in internal
    assert "research.md" in internal


async def test_scan_ignores_legit_not_found_wording():
    dc = _deliverable_check()
    got = dc.scan_text(T11_LEGIT_NOT_FOUND_TEXT, task_id="T-11")
    assert got["placeholder"] == []
    assert got["internal"] == []


@pytest.mark.parametrize(
    "text,kind",
    [
        ("这一步 job 2 干完了", "internal"),
        ("看 job3 的输出", "internal"),
        ("文件在 artifacts/T-11/index.html", "internal"),
        ("详见 steps/2/notes.md", "internal"),
        ("本任务 T-11 的入门表", "internal"),
        ("未覆盖该范围", "placeholder"),
        ("本次不另扩搜", "placeholder"),
        ("此处不写", "placeholder"),
        ("留待后续调研", "placeholder"),
        ("待补充：需另开一次调研", "placeholder"),
    ],
)
async def test_scan_individual_markers(text, kind):
    dc = _deliverable_check()
    got = dc.scan_text(text, task_id="T-11")
    assert got[kind], f"{text!r} 应该命中 {kind}"


@pytest.mark.parametrize(
    "text",
    [
        # 正常成品里本来就会出现的字眼：成品闸是硬闸（命中就返工），不能误伤
        "意外险未覆盖的情况：自残、酒驾。",
        "该作品未收录于 Steam 中国区，可在 itch.io 购买。",
        "页面先放一个占位图，加载完再换。",
        "细节不展开，想看可以点开折叠区。",
        "本文对比了几家厂商的主模型和子 agent 编排方式。",
        "GitHub Actions 会把构建结果上传到 artifacts/ 目录。",
        "In 2024 the job 2024 report was published.",
    ],
)
async def test_scan_does_not_flag_normal_content(text):
    dc = _deliverable_check()
    got = dc.scan_text(text, task_id="T-11")
    assert got == {"placeholder": [], "internal": []}, got


async def test_scan_task_number_is_exact_not_all_t_numbers():
    dc = _deliverable_check()
    assert dc.scan_text("入门表 T-11 成品", task_id="T-11")["internal"]
    # 别的任务号 / 相似前缀不算（不泛匹配所有 T-数字）
    assert dc.scan_text("入门表 T-12 成品", task_id="T-11")["internal"] == []
    assert dc.scan_text("入门表 T-110 成品", task_id="T-11")["internal"] == []
    assert dc.scan_text("入门表 T-1 成品", task_id="T-11")["internal"] == []


async def test_scan_dedupes_and_caps_hits():
    dc = _deliverable_check()
    text = "不另扩搜 " * 40
    got = dc.scan_text(text, task_id="T-11")
    assert 0 < len(got["placeholder"]) <= dc.MAX_HITS
    assert len(set(got["placeholder"])) == len(got["placeholder"])
    assert all(len(h) <= dc.HIT_LEN for h in got["placeholder"])


async def test_read_deliverable_text_html_and_docx(tmp_path):
    dc = _deliverable_check()
    html = tmp_path / "index.html"
    html.write_text(
        "<html><head><style>.x{color:red}</style></head><body>"
        "<p>一、关键节点：未覆盖该范围</p><script>var a='不另扩搜';</script>"
        "</body></html>",
        encoding="utf-8",
    )
    text = dc.read_deliverable_text(html)
    assert "未覆盖该范围" in text
    assert "<p>" not in text  # 标签去掉
    assert "var a" not in text  # script 内容不算成品文字

    docx = tmp_path / "intro.docx"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "word/document.xml",
            "<w:document><w:body><w:p><w:r><w:t>关键节点未收录，需另开一次调研</w:t></w:r>"
            "</w:p></w:body></w:document>",
        )
    docx.write_bytes(buf.getvalue())
    docx_text = dc.read_deliverable_text(docx)
    assert "未收录" in docx_text
    assert dc.scan_text(docx_text, task_id="T-11")["placeholder"]


async def test_read_deliverable_text_caps_bytes(tmp_path):
    dc = _deliverable_check()
    big = tmp_path / "big.md"
    big.write_text("水" * (dc.MAX_BYTES + 1000) + "不另扩搜", encoding="utf-8")
    text = dc.read_deliverable_text(big)
    assert len(text.encode("utf-8")) <= dc.MAX_BYTES + 4
    assert "不另扩搜" not in text  # 超出上限的部分不读


# ---------------------------------------------------------------------------
# C. _plan：covers 覆盖校验（原话必须项都要有人负责）
# ---------------------------------------------------------------------------


def _t11_jobs_with_covers():
    """线上那两个 job：调研只声明 R1/R5，制作声明 R4，R2/R3 没人负责。"""
    return [
        {"brief": "核实免费、时长、分支、汉化状态并附依据，结果写 artifacts/T-11/research.md",
         "type": "research", "tools": ["web_search", "fetch_page", "write_file", "read_file"],
         "covers": ["R1", "R5"]},
        {"brief": "按第 1 步的 research.md 做 DOC 成品，不另扩搜",
         "type": "build", "tools": ["write_file", "read_file"], "after": [1],
         "covers": ["R4"]},
    ]


async def test_plan_appends_coverage_instruction_for_uncovered_original_items(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, req=T11_REQ_TEXT)
    models = ModelsQueue(replies=[json.dumps({
        "criteria": ["做一页防剧透入门表"],
        "deliver_kind": "file",
        "scale": "standard",
        "requirements": T11_RAW_REQS,
        "jobs": _t11_jobs_with_covers(),
        "question": None,
    }, ensure_ascii=False)])
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(),
    )

    plan = await coordinator._plan(tasks.get(tid))

    jobs = plan["jobs"]
    assert len(jobs) == 2
    research_brief = jobs[0]["brief"]
    assert "另外必须为这些要求取材/产出：" in research_brief
    assert "R2" in research_brief and "R3" in research_brief
    # 已被 covers 覆盖的原话（R1/R4）和补充项（R5）都不补
    assert "R1" not in research_brief.split("另外必须为这些要求取材/产出：")[1]
    assert "R4" not in research_brief.split("另外必须为这些要求取材/产出：")[1]
    assert "R5" not in research_brief.split("另外必须为这些要求取材/产出：")[1]
    # 制作步的 brief 一个字不动
    assert jobs[1]["brief"] == "按第 1 步的 research.md 做 DOC 成品，不另扩搜"
    # 计划提示要交代 covers 字段和「停止扩搜只针对加分项/旁支」的口径
    prompt = models.calls[-1][1][-1]["content"]
    assert '"covers"' in prompt
    assert "每条原话必须项" in prompt
    assert "不另扩搜" in prompt


async def test_plan_without_covers_keeps_old_behaviour(
    mem_store, settings, env, tools, tasks, goals
):
    """所有 job 都没给 covers（旧模型）→ 一个字都不动，兼容旧行为。"""
    tid = _create_task(tasks, req=T11_REQ_TEXT)
    jobs_in = [dict(j) for j in _t11_jobs_with_covers()]
    for j in jobs_in:
        j.pop("covers")
    models = ModelsQueue(replies=[json.dumps({
        "criteria": ["做一页防剧透入门表"],
        "deliver_kind": "file",
        "scale": "standard",
        "requirements": T11_RAW_REQS,
        "jobs": jobs_in,
        "question": None,
    }, ensure_ascii=False)])
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(),
    )

    plan = await coordinator._plan(tasks.get(tid))

    assert plan["jobs"][0]["brief"] == jobs_in[0]["brief"]
    assert plan["jobs"][1]["brief"] == jobs_in[1]["brief"]
    assert "另外必须为这些要求取材/产出：" not in plan["jobs"][0]["brief"]


async def test_plan_coverage_always_lands_on_first_research_job(
    mem_store, settings, env, tools, tasks, goals
):
    """优先补进第一条调研活；没有调研活就补进第一条活。"""
    tid = _create_task(tasks, req=T11_REQ_TEXT)
    items = _t11_items()
    _req().save(mem_store, tid, 1, items)
    jobs_in = [
        {"brief": "做成品", "type": "build", "tools": ["write_file"], "covers": ["R4"]},
        {"brief": "查资料", "type": "research", "tools": ["web_search", "fetch_page", "write_file"],
         "covers": ["R1"]},
    ]
    coordinator = _coordinator(mem_store, settings, env, tools, tasks, goals, replies=[json.dumps({
        "criteria": items[0]["text"],
        "deliver_kind": "file",
        "scale": "standard",
        "jobs": jobs_in,
        "question": None,
    }, ensure_ascii=False)])

    plan = await coordinator._plan(tasks.get(tid))

    assert "另外必须为这些要求取材/产出：" not in plan["jobs"][0]["brief"]
    tail = plan["jobs"][1]["brief"].split("另外必须为这些要求取材/产出：")[1]
    assert "R2" in tail and "R3" in tail


# ---------------------------------------------------------------------------
# D. _review：成品扫描命中 → 模型判过也改判不通过
# ---------------------------------------------------------------------------


async def test_review_t11_deliverable_flips_pass_to_fail(
    mem_store, settings, env, tools, tasks, goals
):
    """T-11 原样复现：验收模型逐条判过，但成品里是「未覆盖 / 不另扩搜」+ job1 / research.md。"""
    tid = _create_task(tasks, req=T11_REQ_TEXT)
    items = _t11_items()
    task = tasks.get(tid)
    artifact = _write_artifact(env, task, tid, "index.html", T11_DELIVERABLE_TEXT)
    verdicts = [
        {"id": "R1", "met": True, "evidence": "第二节写了免费与时长，附两条链接"},
        {"id": "R2", "met": True, "evidence": "HTML 第三节 L1-A/L1-B details 折叠，无明文剧透"},
        {"id": "R3", "met": True, "evidence": "HTML 第四节 L2 结局折叠避雷 + DLC 剧透警告"},
        {"id": "R4", "met": True, "evidence": f"成品在 {artifact}"},
        {"id": "R5", "met": True, "evidence": "链接逐条打开核实"},
        {"id": "R0", "met": True, "evidence": "页面能打开"},
    ]
    coordinator = _coordinator(mem_store, settings, env, tools, tasks, goals, replies=[
        _review_json(verdicts, pass_=True, artifact=artifact),
    ])

    review = await coordinator._review(
        tasks.get(tid), _locked_plan(items), "做完了", ["证据"], [],
    )

    assert review["pass"] is False
    # 模型自己逐条判过（judge 也是过）——是成品扫描改判的
    assert review["items_judgement"]["pass"] is True
    text = review["review"]
    assert "未覆盖" in text or "不另扩搜" in text
    assert "job1" in text or "research.md" in text
    assert "补上原话要的内容" in text
    assert "确实查不到就写清查过哪里" in text
    assert "内部用语" in text


async def test_review_clean_t11_deliverable_still_passes(
    mem_store, settings, env, tools, tasks, goals
):
    """合规成品（查不到但写清查过哪里、无内部用语）照常通过。"""
    tid = _create_task(tasks, req=T11_REQ_TEXT)
    items = _t11_items()
    task = tasks.get(tid)
    artifact = _write_artifact(env, task, tid, "index.html", T11_LEGIT_NOT_FOUND_TEXT)
    verdicts = [
        {"id": "R1", "met": True, "evidence": "第二节写了免费与时长"},
        {"id": "R2", "met": True, "evidence": "第三节按两条线列节点，折叠遮挡"},
        {"id": "R3", "met": True, "evidence": "第四节电波梗 + 结局折叠避雷"},
        {"id": "R4", "met": True, "evidence": f"成品在 {artifact}"},
        {"id": "R5", "met": True, "evidence": "链接逐条打开核实"},
        {"id": "R0", "met": True, "evidence": "页面能打开"},
    ]
    coordinator = _coordinator(mem_store, settings, env, tools, tasks, goals, replies=[
        _review_json(verdicts, pass_=True, artifact=artifact),
    ])

    review = await coordinator._review(
        tasks.get(tid), _locked_plan(items), "做完了", ["证据"], [],
    )

    assert review["pass"] is True


async def test_review_scan_error_does_not_break_review(
    mem_store, settings, env, tools, tasks, goals, monkeypatch
):
    """扫描自己出错 → 跳过并记日志，不许打断验收。"""
    from CharTyr_MaiWork.maiwork import deliverable_check

    tid = _create_task(tasks, req=T11_REQ_TEXT)
    items = _t11_items()
    task = tasks.get(tid)
    artifact = _write_artifact(env, task, tid, "index.html", T11_DELIVERABLE_TEXT)

    def _boom(path):
        raise OSError("读不出来")

    monkeypatch.setattr(deliverable_check, "read_deliverable_text", _boom)
    verdicts = [
        {"id": "R1", "met": True, "evidence": "第二节写了免费与时长"},
        {"id": "R2", "met": True, "evidence": "第三节折叠遮挡"},
        {"id": "R3", "met": True, "evidence": "第四节折叠避雷"},
        {"id": "R4", "met": True, "evidence": f"成品在 {artifact}"},
        {"id": "R5", "met": True, "evidence": "链接逐条打开核实"},
        {"id": "R0", "met": True, "evidence": "页面能打开"},
    ]
    coordinator = _coordinator(mem_store, settings, env, tools, tasks, goals, replies=[
        _review_json(verdicts, pass_=True, artifact=artifact),
    ])

    review = await coordinator._review(
        tasks.get(tid), _locked_plan(items), "做完了", ["证据"], [],
    )

    assert review["pass"] is True
    assert review["review"].startswith("通过")


async def test_review_prompt_states_placeholder_and_internal_rules(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, req=T11_REQ_TEXT)
    items = _t11_items()
    coordinator = _coordinator(mem_store, settings, env, tools, tasks, goals, replies=[
        _review_json([
            {"id": r["id"], "met": True, "evidence": "证据"}
            for r in items
        ], pass_=True, artifact=""),
    ])

    await coordinator._review(tasks.get(tid), _locked_plan(items), "总结", ["证据"], [])

    prompt = coordinator._models.calls[-1][1][-1]["content"]
    assert "未覆盖" in prompt and "不另扩搜" in prompt
    assert "不算做到" in prompt
    assert "查不到" in prompt and "查过哪里" in prompt
    assert "群友看得懂" in prompt
    assert "job 编号" in prompt and "主模型" in prompt


# ---------------------------------------------------------------------------
# E. 子 agent 提示：成品是给群友看的
# ---------------------------------------------------------------------------


async def test_worker_brief_says_deliverable_is_for_group(
    mem_store, settings, env, tools, tasks, goals
):
    from CharTyr_MaiWork.maiwork.workers import AUDIENCE_NOTE

    coordinator = _coordinator(mem_store, settings, env, tools, tasks, goals, replies=[])
    brief = coordinator._enrich_brief("做一页入门表", "T-11", "file")
    assert AUDIENCE_NOTE in brief
    assert "群友" in AUDIENCE_NOTE
    assert "job 编号" in AUDIENCE_NOTE
    assert "research.md" in AUDIENCE_NOTE
    assert "主模型" in AUDIENCE_NOTE
