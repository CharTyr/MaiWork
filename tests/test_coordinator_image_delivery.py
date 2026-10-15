"""任务交付的 image 形态（2026-10-15 任务 A2，coordinator 那半）：

- 计划 JSON 的 deliver_kind 认 image（认不出仍回落 file）；
- 排计划提示讲清什么时候用 image（成品是一到四张图片时直接发图 + 一句说明）；
- 验收通过后按 image 交付：图在 artifacts/<任务>/images/ 里就把那一到四张一起发；
- 交付说明兜底按 image 说人话（不是「点开就能看」）；
- 交给子 agent 的 brief 讲清成品是图片（别做成网页）。
"""

from __future__ import annotations

import json

import pytest

from tests.test_coordinator import (  # noqa: F401  （fixtures / 模型桩，见该文件）
    GID,
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
from CharTyr_MaiWork.maiwork.store import Store

pytestmark = pytest.mark.asyncio

PNG = b"\x89PNG\r\n\x1a\n" + b"payload"


def _plan_json(*, deliver_kind="image", jobs=None, criteria=None) -> str:
    return json.dumps(
        {
            "criteria": list(criteria or ["两张图都落到 artifacts/T-1/images/"]),
            "deliver_kind": deliver_kind,
            "jobs": jobs if jobs is not None
            else [{"brief": "把两张成品图整理到 artifacts/T-1/images/ 下",
                   "tools": ["write_file", "list_files"]}],
            "question": None,
        },
        ensure_ascii=False,
    )


def _review_json(artifact: str) -> str:
    return json.dumps(
        {"pass": True, "review": "通过", "missing": [], "artifact": artifact, "note": "画好了，两只猫"},
        ensure_ascii=False,
    )


def _plan_prompt(models: ModelsQueue) -> str:
    return "\n".join(
        m.get("content") or ""
        for _role, msgs, kw in models.calls
        if str(kw.get("purpose") or "") == "coordinator.plan"
        for m in msgs
        if isinstance(m.get("content"), str)
    )


def _images_dir(env, tasks, tid: str):
    ws = env.workspace(tasks.get(tid)["workspace"])
    d = ws / "artifacts" / tid / "images"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ----------------------------------------------------------------------
# 计划：认 image + 提示词讲清什么时候用
# ----------------------------------------------------------------------


async def test_plan_prompt_explains_image_kind(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks, req="画两张图", reply=None)
    models = ModelsQueue(replies=[_plan_json(), _review_json(f"artifacts/{tid}/images/a.png")])
    workers = FakeWorkers()
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=workers,
    )
    await coordinator.run_task(tid)
    prompt = _plan_prompt(models)
    assert "image" in prompt
    assert "一到四张" in prompt or "直接发图" in prompt


async def test_plan_accepts_image_kind_and_delivers_the_images_dir(
    mem_store: Store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, title="画猫", req="画两张猫的图", reply=None)
    models = ModelsQueue(replies=[_plan_json(), _review_json(f"artifacts/{tid}/images/a.png")])
    workers = FakeWorkers()
    delivery, outbox = FakeDelivery(), FakeOutbox()

    async def _write_images():
        d = _images_dir(env, tasks, tid)
        (d / "a.png").write_bytes(PNG)
        (d / "b.png").write_bytes(PNG)

    workers.before_return = _write_images
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=workers, delivery=delivery, outbox=outbox,
    )

    await coordinator.run_task(tid)

    task = tasks.get(tid)
    assert task["status"] == "completed"
    assert task["delivery_kind"] == "image"
    assert len(delivery.delivered) == 1
    d = delivery.delivered[0]
    assert d["kind"] == "image"
    assert d["path"] == env.workspace(task["workspace"]) / "artifacts" / tid / "images"
    assert d["name"] == "画猫"
    assert d["note"]


async def test_unknown_deliver_kind_still_falls_back_to_file(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, req="做点东西", reply=None)
    models = ModelsQueue(replies=[_plan_json(deliver_kind="movie"),
                                  _review_json(f"artifacts/{tid}/out.bin")])
    workers = FakeWorkers()

    async def _write_artifact():
        ws = env.workspace(tasks.get(tid)["workspace"])
        (ws / "artifacts" / tid).mkdir(parents=True, exist_ok=True)
        (ws / "artifacts" / tid / "out.bin").write_bytes(b"x")

    workers.before_return = _write_artifact
    delivery = FakeDelivery()
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=workers, delivery=delivery, outbox=FakeOutbox(),
    )
    await coordinator.run_task(tid)
    assert tasks.get(tid)["delivery_kind"] == "file"
    assert delivery.delivered[0]["kind"] == "file"


# ----------------------------------------------------------------------
# 交付说明兜底
# ----------------------------------------------------------------------


async def test_note_fallback_for_image_is_just_title_done():
    # 图和说明在同一条消息里，兜底说明只说「<标题>做好了」，不说图在哪
    from CharTyr_MaiWork.maiwork import coordinator as coord

    out = coord.delivery_note_fallback("画两只猫", "image")
    assert len(out) <= 60
    assert out.endswith("做好了")
    assert "画两只猫" in out
    assert "点开就能看" not in out


# ----------------------------------------------------------------------
# 交给子 agent 的 brief
# ----------------------------------------------------------------------


async def test_enrich_brief_for_image_tells_where_to_put_pictures(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, req="画两张图", reply=None)
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=ModelsQueue(), workers=FakeWorkers(),
    )
    out = coordinator._enrich_brief("整理两张成品图", tid, "image", tools=())
    assert f"artifacts/{tid}/images/" in out
    assert "图" in out
    assert "index.html" not in out
    # 工具名单里没有 generate_image 时，不许在 brief 里点名它
    assert "generate_image" not in out
    out2 = coordinator._enrich_brief("整理两张成品图", tid, "image", tools=("generate_image",))
    assert "generate_image" in out2
