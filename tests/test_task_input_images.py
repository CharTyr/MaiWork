"""线上 T-14（2026-10-10）：群友引用 / 附带图片派的活，子 agent 必须拿到原图。

线上那条任务：群友引用了一张图，说「把它改得凶一点，加一句『我可不是什么好好小姐』」。
MaiWork 建了任务，子 agent 只看到需求文本里的「[image]」，看不到图，另画了一个角色，
验收打回，最后停在等人。图其实在宿主拿得到（`message.get_by_id` +
`include_binary_data=True`）。

这里测三层：
- `Host.message_by_id`：能力名 / 参数形状 / 失败返回 None（绝不抛）；
- `Coordinator._collect_request_images`：被引用那条消息的图排前面、原图落盘字节一致、
  上限 4 张 / 单张 ≤ 10MB / 认魔数 / 失败不打断任务 / 已经收过不重复拉；
- 接进流程：计划提示词、每条活的 brief、验收提示词都点名原图；验收不许把
  `artifacts/<任务>/input/` 下的原图当成品。

复用 `test_coordinator.py` 里的假对象（同目录在 sys.path 上，仓库惯例）。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from fakes import FakeCtx, FakeHost
from test_coordinator import (
    GID,
    NOW,
    FakeWorkers,
    ModelsQueue,
    _Profiles,
    _Settings,
    _build,
    _create_task,
    _plan,
    _review,
)

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.environments.local import LocalEnv
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.host import Host
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks
from CharTyr_MaiWork.maiwork.tools import Tools

pytestmark = pytest.mark.asyncio

_TEN_MB = 10 * 1024 * 1024


# ---------------------------------------------------------------------------
# fixtures（本地定义，不依赖别的测试模块的 fixture 收集）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    holding = [NOW]
    monkeypatch.setattr(clock, "now", lambda: holding[0])
    return holding


@pytest.fixture
def mem_store(tmp_path: Path):
    store = Store(tmp_path / "maiwork.db")
    store.migrate()
    yield store
    store.close()


@pytest.fixture
def settings(tmp_path: Path):
    return _Settings(tmp_path / "workspaces")


@pytest.fixture
def env(settings):
    return LocalEnv(lambda: settings)


@pytest.fixture
def tasks(mem_store: Store, settings) -> Tasks:
    return Tasks(mem_store, lambda: settings)


@pytest.fixture
def goals(mem_store: Store, settings) -> Goals:
    return Goals(mem_store, lambda: settings)


@pytest.fixture
def tools(mem_store: Store):
    return Tools(mem_store)


# ---------------------------------------------------------------------------
# 造消息 / 造请求
# ---------------------------------------------------------------------------


def _png(body: bytes = b"png-body") -> bytes:
    """一个魔数对得上的假 PNG（只测类型识别，不解码像素）。"""
    return b"\x89PNG\r\n\x1a\n" + body


def _jpg(body: bytes = b"jpg-body") -> bytes:
    return b"\xff\xd8\xff\xe0" + body


def _image_seg(data: bytes, *, with_binary: bool = True, type_: str = "image") -> dict:
    seg: dict[str, Any] = {
        "type": type_,
        "data": "宿主给的 content 字符串",
        "hash": "deadbeef",
    }
    if with_binary:
        seg["binary_data_base64"] = base64.b64encode(data).decode("ascii")
    return seg


def _b64_seg(b64: str, *, type_: str = "image") -> dict:
    """直接给 base64 文本（测坏 base64 / 非图片字节用）。"""
    return {"type": type_, "data": "x", "binary_data_base64": b64}


def _msg(mid: str, segments: list[dict], *, reply_to: str = "") -> dict:
    raw = list(segments)
    if reply_to:
        raw.insert(0, {"type": "reply", "data": {"target_message_id": reply_to}})
    return {"message_id": mid, "raw_message": raw, "processed_plain_text": "[图片]"}


def _seed_request(store: Store, request_id: str, message_id: str) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO requests (id, group_id, kind, title, quote, message_id, status, created, updated)"
            " VALUES (?, ?, 'task', '改图', '', ?, 'approved', ?, ?)",
            (request_id, GID, message_id, NOW, NOW),
        )


def _kinds(store: Store, tid: str) -> list[tuple[str, dict]]:
    rows = store.read().execute(
        "SELECT kind, payload FROM events WHERE entity='task' AND entity_id=? ORDER BY id", (tid,)
    ).fetchall()
    return [(str(r["kind"]), json.loads(r["payload"] or "{}")) for r in rows]


def _coordinator(
    *, mem_store, settings, env, tools, tasks, goals, models, workers, host=None
):
    return _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=workers, host=host,
    )


def _write_artifact(env, tasks, tid: str):
    """假 worker 交回前真的写出 artifacts/<任务>/index.html。"""

    async def _do() -> None:
        ws = env.workspace(tasks.get(tid)["workspace"])
        (ws / "artifacts" / tid).mkdir(parents=True, exist_ok=True)
        (ws / "artifacts" / tid / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    return _do


# ---------------------------------------------------------------------------
# Host.message_by_id
# ---------------------------------------------------------------------------


class TestHostMessageById:
    async def test_passes_message_id_chat_id_and_include_binary_data(self) -> None:
        msg = {"message_id": "m1", "raw_message": []}
        ctx = FakeCtx({"message.get_by_id": {"success": True, "message": msg}})
        h = Host(ctx)
        got = await h.message_by_id("m1", "sess-1", include_binary_data=True)
        assert got == msg
        name, kw = ctx.calls[0]
        assert name == "message.get_by_id"
        assert kw["message_id"] == "m1"
        assert kw["chat_id"] == "sess-1"
        assert kw["include_binary_data"] is True
        assert "timeout_ms" in kw

    async def test_omits_chat_id_when_session_empty(self) -> None:
        ctx = FakeCtx({"message.get_by_id": {"success": True, "message": {"message_id": "m1"}}})
        h = Host(ctx)
        got = await h.message_by_id("m1")
        assert got == {"message_id": "m1"}
        _name, kw = ctx.calls[0]
        assert "chat_id" not in kw
        assert kw["include_binary_data"] is False

    async def test_returns_none_when_capability_raises(self) -> None:
        ctx = FakeCtx({"message.get_by_id": RuntimeError("宿主炸了")})
        h = Host(ctx)
        assert await h.message_by_id("m1", "sess-1") is None

    async def test_returns_none_on_unsuccess(self) -> None:
        ctx = FakeCtx({"message.get_by_id": {"success": False, "error": "没有这条消息"}})
        h = Host(ctx)
        assert await h.message_by_id("m1", "sess-1") is None

    async def test_returns_none_when_message_missing(self) -> None:
        ctx = FakeCtx({"message.get_by_id": {"success": True, "message": None}})
        h = Host(ctx)
        assert await h.message_by_id("m1", "sess-1") is None


# ---------------------------------------------------------------------------
# _collect_request_images：落盘 / 顺序 / 上限 / 失败
# ---------------------------------------------------------------------------


async def test_quoted_image_saved_locally_and_quoted_first(
    mem_store: Store, settings, env, tools, tasks, goals
):
    """被引用的那条消息的图排前面，然后是请求自己附的图。"""
    tid = _create_task(tasks, request_id="R1")
    _seed_request(mem_store, "R1", "m-req")
    quoted = _png(b"quoted-body")
    own = _jpg(b"own-body")
    host = FakeHost(by_id={
        "m-req": _msg("m-req", [_image_seg(own)], reply_to="m-quoted"),
        "m-quoted": _msg("m-quoted", [_image_seg(quoted)]),
    })
    coord = _coordinator(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=ModelsQueue(), workers=FakeWorkers(), host=host,
    )
    ws_name = str(tasks.get(tid)["workspace"])
    paths = await coord._collect_request_images(tid, GID, ws_name)
    assert paths == [f"artifacts/{tid}/input/原图1.png", f"artifacts/{tid}/input/原图2.jpg"]
    ws = env.workspace(ws_name)
    assert (ws / paths[0]).read_bytes() == quoted
    assert (ws / paths[1]).read_bytes() == own
    # 拉了两条消息（请求 + 被引用那条），都带 include_binary_data
    assert [c[0] for c in host.by_id_calls] == ["m-req", "m-quoted"]
    assert all(c[2] is True for c in host.by_id_calls)


async def test_emoji_segment_also_collected(mem_store, settings, env, tools, tasks, goals):
    """表情 / 贴纸段（type=emoji）和图片一样收。"""
    tid = _create_task(tasks, request_id="R1")
    _seed_request(mem_store, "R1", "m-req")
    sticker = _png(b"sticker-body")
    host = FakeHost(by_id={"m-req": _msg("m-req", [_image_seg(sticker, type_="emoji")])})
    coord = _coordinator(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=ModelsQueue(), workers=FakeWorkers(), host=host,
    )
    paths = await coord._collect_request_images(tid, GID, str(tasks.get(tid)["workspace"]))
    assert paths == [f"artifacts/{tid}/input/原图1.png"]


async def test_missing_binary_data_records_event_and_task_still_runs(
    mem_store: Store, settings, env, tools, tasks, goals
):
    """宿主已经把文件删了（没有 binary_data_base64）：一张不落、记一笔、任务照常跑完。"""
    tid = _create_task(tasks, request_id="R1")
    _seed_request(mem_store, "R1", "m-req")
    host = FakeHost(by_id={"m-req": _msg("m-req", [_image_seg(b"x", with_binary=False)])})
    models = ModelsQueue(replies=[_plan(), _review(pass_=True)])
    workers = FakeWorkers()
    workers.before_return = _write_artifact(env, tasks, tid)
    coord = _coordinator(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=workers, host=host,
    )
    await coord.run_task(tid)
    assert tasks.get(tid)["status"] == "completed"
    ws = env.workspace(tasks.get(tid)["workspace"])
    assert not (ws / "artifacts" / tid / "input").exists()
    kinds = [k for k, _ in _kinds(mem_store, tid)]
    assert "task.input_images_missing" in kinds
    assert "task.input_images" not in kinds


async def test_non_image_bytes_and_bad_base64_are_skipped(
    mem_store: Store, settings, env, tools, tasks, goals
):
    """不是图片的字节 / 解不开的 base64 一律跳过，只记 missing。"""
    tid = _create_task(tasks, request_id="R1")
    _seed_request(mem_store, "R1", "m-req")
    host = FakeHost(by_id={"m-req": _msg("m-req", [
        _b64_seg(base64.b64encode(b"this is not an image").decode("ascii")),
        _b64_seg("!!!not-base64!!!"),
    ])})
    coord = _coordinator(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=ModelsQueue(), workers=FakeWorkers(), host=host,
    )
    paths = await coord._collect_request_images(tid, GID, str(tasks.get(tid)["workspace"]))
    assert paths == []
    kinds = [k for k, _ in _kinds(mem_store, tid)]
    assert "task.input_images_missing" in kinds


async def test_image_larger_than_ten_mb_is_skipped(
    mem_store: Store, settings, env, tools, tasks, goals
):
    """单张解码后超过 10MB：跳过（记 missing），别的原照收。"""
    tid = _create_task(tasks, request_id="R1")
    _seed_request(mem_store, "R1", "m-req")
    big = b"\x89PNG" + b"\x00" * _TEN_MB  # 4 字节 + 10MB > 10MB
    small = _png(b"small")
    host = FakeHost(by_id={"m-req": _msg("m-req", [
        _image_seg(big), _image_seg(small),
    ])})
    coord = _coordinator(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=ModelsQueue(), workers=FakeWorkers(), host=host,
    )
    paths = await coord._collect_request_images(tid, GID, str(tasks.get(tid)["workspace"]))
    assert paths == [f"artifacts/{tid}/input/原图1.png"]
    ws = env.workspace(tasks.get(tid)["workspace"])
    assert (ws / paths[0]).read_bytes() == small


async def test_more_than_four_images_capped(
    mem_store: Store, settings, env, tools, tasks, goals
):
    """最多 4 张：第 5 张起不要（被引用的排前面，所以留下的是前 4 张）。"""
    tid = _create_task(tasks, request_id="R1")
    _seed_request(mem_store, "R1", "m-req")
    # 被引用那条消息里 2 张，请求自己 3 张：合计 5 张，只留前 4 张（引用的排前面）
    host = FakeHost(by_id={
        "m-req": _msg("m-req", [_image_seg(_png(b"p2")), _image_seg(_png(b"p3")),
                                _image_seg(_png(b"p4"))], reply_to="m-quoted"),
        "m-quoted": _msg("m-quoted", [_image_seg(_png(b"p0")), _image_seg(_png(b"p1"))]),
    })
    coord = _coordinator(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=ModelsQueue(), workers=FakeWorkers(), host=host,
    )
    paths = await coord._collect_request_images(tid, GID, str(tasks.get(tid)["workspace"]))
    assert len(paths) == 4
    ws = env.workspace(tasks.get(tid)["workspace"])
    assert [p.rsplit("/", 1)[-1] for p in paths] == [f"原图{i}.png" for i in (1, 2, 3, 4)]


async def test_already_collected_input_dir_is_not_refetched_on_second_attempt(
    mem_store: Store, settings, env, tools, tasks, goals
):
    """第二轮尝试（打回重跑）：input/ 里已经有文件就不再拉宿主。"""
    tid = _create_task(tasks, request_id="R1")
    _seed_request(mem_store, "R1", "m-req")
    host = FakeHost(by_id={
        "m-req": _msg("m-req", [_image_seg(_png(b"own"))], reply_to="m-quoted"),
        "m-quoted": _msg("m-quoted", [_image_seg(_png(b"quoted"))]),
    })
    workers = FakeWorkers()
    workers.before_return = _write_artifact(env, tasks, tid)
    models = ModelsQueue(replies=[
        _plan(), _review(pass_=False, artifact="", review="没过：不像原图"),
        _plan(), _review(pass_=True),
    ])
    coord = _coordinator(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=workers, host=host,
    )
    await coord.run_task(tid)
    assert tasks.get(tid)["status"] == "completed"
    # 第一轮拉了 2 条（请求 + 被引用），第二轮直接复用，不再问宿主
    assert len(host.by_id_calls) == 2


async def test_no_request_host_none_and_host_error_are_all_empty(
    mem_store: Store, settings, env, tools, tasks, goals
):
    """构想派生的任务（没有 request）/ 没接宿主 / 宿主炸了：都返回 [] 且任务照跑。"""
    # 1) 没有 request_id
    tid = _create_task(tasks, source="idea")
    host = FakeHost(by_id={"m-req": _msg("m-req", [_image_seg(_png())])})
    coord = _coordinator(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=ModelsQueue(), workers=FakeWorkers(), host=host,
    )
    assert await coord._collect_request_images(
        tid, GID, str(tasks.get(tid)["workspace"])
    ) == []
    assert host.by_id_calls == []
    assert host.session_calls == []

    # 2) 没接宿主
    tid2 = _create_task(tasks, request_id="R2")
    _seed_request(mem_store, "R2", "m-req2")
    coord2 = _coordinator(
        mem_store=mem_store, settings=settings, env=env, tools=Tools(mem_store),
        tasks=tasks, goals=goals, models=ModelsQueue(), workers=FakeWorkers(), host=None,
    )
    assert await coord2._collect_request_images(
        tid2, GID, str(tasks.get(tid2)["workspace"])
    ) == []

    # 3) 宿主炸了（message_by_id 抛）
    tid3 = _create_task(tasks, request_id="R3")
    _seed_request(mem_store, "R3", "m-req3")
    bad = FakeHost(by_id={"m-req3": _msg("m-req3", [_image_seg(_png())])},
                   message_error=RuntimeError("宿主炸了"))
    workers = FakeWorkers()
    workers.before_return = _write_artifact(env, tasks, tid3)
    models = ModelsQueue(replies=[
        _plan(), _review(pass_=True, artifact=f"artifacts/{tid3}/index.html"),
    ])
    coord3 = _coordinator(
        mem_store=mem_store, settings=settings, env=env, tools=Tools(mem_store),
        tasks=tasks, goals=goals, models=models, workers=workers, host=bad,
    )
    await coord3.run_task(tid3)
    assert tasks.get(tid3)["status"] == "completed"


# ---------------------------------------------------------------------------
# 接进流程：计划提示词 / 每条活的 brief / 验收提示词
# ---------------------------------------------------------------------------


async def test_plan_brief_and_review_all_mention_input_images(
    mem_store: Store, settings, env, tools, tasks, goals
):
    """引用图落盘之后：计划、每条活的 brief、验收都点名原图路径。"""
    tid = _create_task(tasks, request_id="R1", req="把这张图改得凶一点")
    _seed_request(mem_store, "R1", "m-req")
    quoted = _png(b"quoted")
    host = FakeHost(by_id={
        "m-req": _msg("m-req", [{"type": "text", "data": "改得凶一点"}], reply_to="m-quoted"),
        "m-quoted": _msg("m-quoted", [_image_seg(quoted)]),
    })
    models = ModelsQueue(replies=[_plan(), _review(pass_=True)])
    workers = FakeWorkers()
    workers.before_return = _write_artifact(env, tasks, tid)
    coord = _coordinator(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=workers, host=host,
    )
    await coord.run_task(tid)

    rel = f"artifacts/{tid}/input/原图1.png"
    plan_prompt = models.calls[0][1][-1]["content"]
    assert rel in plan_prompt
    assert "不要另画一张" in plan_prompt
    assert rel in workers.calls[0]["brief"]
    review_prompt = [c for c in models.calls if c[2].get("purpose") == "coordinator.review"][0]
    assert rel in review_prompt[1][-1]["content"]
    assert "对照原图" in review_prompt[1][-1]["content"]

    kinds = [k for k, _ in _kinds(mem_store, tid)]
    assert "task.input_images" in kinds
    payload = dict(_kinds(mem_store, tid))["task.input_images"]
    assert int(payload["count"]) == 1


async def test_review_rejects_input_image_as_deliverable(
    mem_store: Store, settings, env, tools, tasks, goals
):
    """验收不能把群友给的原图本身当成品（原图是材料，不是交付物）。"""
    tid = _create_task(tasks)
    ws = env.workspace(tasks.get(tid)["workspace"])
    (ws / "artifacts" / tid / "input").mkdir(parents=True, exist_ok=True)
    (ws / "artifacts" / tid / "input" / "原图1.png").write_bytes(_png(b"quoted"))
    models = ModelsQueue(replies=[
        _review(pass_=True, artifact=f"artifacts/{tid}/input/原图1.png"),
    ])
    coord = _coordinator(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(),
    )
    review = await coord._review(
        tasks.get(tid),
        {"criteria": ["在原图基础上改"], "deliver_kind": "view"},
        "完成", ["evidence"], [],
    )
    assert review["pass"] is False
    assert "原图" in review["review"]


async def test_normal_artifact_still_passes_with_input_images(
    mem_store: Store, settings, env, tools, tasks, goals
):
    """原图存在时，正常成品（artifacts/<任务>/index.html）照旧能过。"""
    tid = _create_task(tasks)
    ws = env.workspace(tasks.get(tid)["workspace"])
    (ws / "artifacts" / tid / "input").mkdir(parents=True, exist_ok=True)
    (ws / "artifacts" / tid / "input" / "原图1.png").write_bytes(_png(b"quoted"))
    (ws / "artifacts" / tid / "index.html").write_text("<html>改好了</html>", encoding="utf-8")
    models = ModelsQueue(replies=[_review(pass_=True, artifact=f"artifacts/{tid}/index.html")])
    coord = _coordinator(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(),
    )
    review = await coord._review(
        tasks.get(tid),
        {"criteria": ["在原图基础上改"], "deliver_kind": "view"},
        "完成", ["evidence"], [],
    )
    assert review["pass"] is True
