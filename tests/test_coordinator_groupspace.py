"""coordinator.py 群空间小回合（docs/07 §11.4、tools_groupspace.py）。

验收通过、交付之前：app 有 group_space 且这个群 capabilities 里有任一能力为真时，
给主模型开一个「群空间」小回合（最多 4 轮工具调用，工具只给这个群能力允许的那几个，
走 Tools.call，actor="主模型"，落 tool_calls）。只有任务明确需要时才用；不需要就
直接回答 {"done": true}；能力全 False 时连模型都不叫（零额外开销）。

红线对应用例：
- 能力全 False → 不多调模型；
- 有能力且模型调了 group_notice_send → 工具真的被调、tool_calls 有记录（actor=主模型）；
- 模型直接 {"done": true} → 不调任何群空间工具；
- 子 agent 工具列表里始终没有这 4 个群空间工具。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# 复用 test_coordinator.py 里的假对象（同目录在 sys.path 上；tests/test_extensions.py
# 也有同样的跨测试模块导入惯例）。
from test_coordinator import (  # noqa: F401
    GID,
    NOW,
    FakeDelivery,
    FakeOutbox,
    FakeWorkers,
    ReplayChatResult,
    _Profiles,
    _Settings,
    _create_task,
    _plan,
    _review,
)

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.coordinator import Coordinator, _GROUPSPACE_TOOLS
from CharTyr_MaiWork.maiwork.environments.local import LocalEnv
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks
from CharTyr_MaiWork.maiwork.tools import Tools
from CharTyr_MaiWork.maiwork.tools_exec import register_exec_tools
from CharTyr_MaiWork.maiwork.tools_groupspace import register_groupspace_tools

pytestmark = pytest.mark.asyncio

_CAP_KEYS = ("files_list", "files_manage", "notice_read", "notice_send", "album_list", "album_upload")


def _caps(**on: bool) -> dict[str, bool]:
    out = {k: False for k in _CAP_KEYS}
    for k, v in on.items():
        out[k] = bool(v)
    return out


class FakeGroupSpace:
    """假的 GroupSpace：记录能力查询 + 真被调到的群空间动作。"""

    def __init__(self, caps: dict[str, bool] | None = None) -> None:
        self.caps = dict(caps or _caps())
        self.cap_calls: list[str] = []
        self.notices: list[str] = []
        self.announces: list[tuple[str, str]] = []
        self.album_uploads: list[tuple[str, str, str]] = []

    async def capabilities_async(self, group_id: str, *, now: float | None = None) -> dict[str, bool]:
        self.cap_calls.append(str(group_id))
        return dict(self.caps)

    async def send_notice(self, group_id, content, *, announce=None, now=None) -> None:
        text = str(content)
        self.notices.append(text)
        if announce is not None:
            preview = text[:30]
            await announce(str(group_id), f"我要发一条群公告：{preview}")
            self.announces.append((str(group_id), f"我要发一条群公告：{preview}"))

    async def list_files(self, group_id, folder_id=None):
        return []

    async def create_folder(self, group_id, name, folder_id=None) -> None:
        raise AssertionError("这个用例不该建文件夹")

    async def delete_file(self, group_id, file_id) -> None:
        raise AssertionError("这个用例不该删文件")

    async def rename_file(self, group_id, file_id, name) -> None:
        raise AssertionError("这个用例不该改文件名")

    async def move_file(self, group_id, file_id, folder_id) -> None:
        raise AssertionError("这个用例不该移文件")

    async def upload_to_album(self, group_id, album_id, path) -> None:
        self.album_uploads.append((str(group_id), str(album_id), str(path)))


class ScriptedModels:
    """假 Models：按顺序回放脚本项（str → 文本结果；ReplayChatResult → 原样；异常 → 抛）。"""

    def __init__(self, script: list, ready: bool = True) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, list[dict], dict]] = []
        self._ready = ready

    def settings(self):
        ready = self._ready

        class _S:
            def ready(self):
                return ready

        return _S()

    async def chat(self, role=None, messages=None, **kwargs):
        self.calls.append((role, [dict(m) for m in messages], kwargs))
        if not self.script:
            return ReplayChatResult("{}")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, ReplayChatResult):
            return item
        return ReplayChatResult(str(item))

    def purposes(self) -> list[str]:
        return [str(c[2].get("purpose") or "") for c in self.calls]

    def tool_names_of(self, purpose: str) -> list[list[str]]:
        """每个该 purpose 的回合，模型拿到的工具名额名单。"""
        out: list[list[str]] = []
        for _role, _msgs, kw in self.calls:
            if str(kw.get("purpose") or "") != purpose:
                continue
            specs = kw.get("tools") or []
            out.append([str((s.get("function") or {}).get("name") or "") for s in specs])
        return out


def _tool_call(name: str, args: dict, call_id: str = "call-gs-1") -> ReplayChatResult:
    return ReplayChatResult(
        "",
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
        ],
    )


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
def tools(mem_store: Store) -> Tools:
    return Tools(mem_store)


@pytest.fixture
def tasks(mem_store: Store, settings) -> Tasks:
    return Tasks(mem_store, lambda: settings)


@pytest.fixture
def goals(mem_store: Store, settings) -> Goals:
    return Goals(mem_store, lambda: settings)


def _build(
    *,
    mem_store: Store,
    settings,
    env,
    tools: Tools,
    tasks: Tasks,
    goals: Goals,
    models,
    workers,
    group_space=None,
    delivery=None,
    outbox=None,
) -> Coordinator:
    delivery = delivery or FakeDelivery()
    outbox = outbox or FakeOutbox()
    register_exec_tools(
        tools,
        env=env,
        host=None,
        get_settings=lambda: settings,
        session_of=lambda gid: "sess-1",
    )
    return Coordinator(
        mem_store,
        models,
        workers,
        tools,
        tasks,
        goals,
        delivery,
        outbox,
        env,
        _Profiles(),
        lambda: settings,
        group_space=group_space,
    )


def _write_artifact_hook(env, tasks, tid):
    async def _write():
        ws = env.workspace(tasks.get(tid)["workspace"])
        (ws / "artifacts" / tid).mkdir(parents=True, exist_ok=True)
        (ws / "artifacts" / tid / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    return _write


# ---------------------------------------------------------------------------
# 1. 能力全 False → 不多调模型
# ---------------------------------------------------------------------------


async def test_all_caps_false_no_extra_model_call(
    mem_store: Store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks)
    gs = FakeGroupSpace(caps=_caps())  # 六项能力全 False
    models = ScriptedModels([_plan(), _review(pass_=True)])
    workers = FakeWorkers()
    workers.before_return = _write_artifact_hook(env, tasks, tid)
    delivery = FakeDelivery()

    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=workers, group_space=gs, delivery=delivery,
    )
    await coordinator.run_task(tid)

    assert tasks.get(tid)["status"] == "completed"
    assert len(delivery.delivered) == 1
    # 能力查过（知道能不能做），但一次群空间模型回合都没开：总共就 计划 + 验收 两次
    assert gs.cap_calls == [GID]
    assert models.purposes() == ["coordinator.plan", "coordinator.review"]


# ---------------------------------------------------------------------------
# 2. 有能力 + 模型调了 group_notice_send → 工具真被调、tool_calls 记录 actor=主模型
# ---------------------------------------------------------------------------


async def test_notice_send_tool_called_and_logged(
    mem_store: Store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks)
    gs = FakeGroupSpace(caps=_caps(notice_send=True))
    announce_calls: list[tuple[str, str]] = []

    async def _announce(gid: str, text: str) -> None:
        announce_calls.append((gid, text))

    register_groupspace_tools(tools, gs, announce=_announce)

    models = ScriptedModels(
        [
            _plan(),
            _review(pass_=True),
            _tool_call("group_notice_send", {"content": "明天下午停服维护"}),
            '{"done": true}',
        ]
    )
    workers = FakeWorkers()
    workers.before_return = _write_artifact_hook(env, tasks, tid)
    delivery = FakeDelivery()

    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=workers, group_space=gs, delivery=delivery,
    )
    await coordinator.run_task(tid)

    # 交付照常
    assert tasks.get(tid)["status"] == "completed"
    assert len(delivery.delivered) == 1
    # 工具真的被调到了（公告内容原样进群空间）
    assert gs.notices == ["明天下午停服维护"]
    assert announce_calls and announce_calls[0][0] == GID
    assert announce_calls[0][1] == "我要发一条群公告：明天下午停服维护"
    # tool_calls 有记录，actor 是主模型
    rows = mem_store.read().execute(
        "SELECT actor, tool, ok FROM tool_calls WHERE tool='group_notice_send' ORDER BY id"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["actor"] == "主模型"
    assert rows[0]["ok"] == 1
    # 这个群只开了 notice_send：群空间回合里只给这一个工具（调工具那轮 + 收尾那轮）
    assert models.tool_names_of("coordinator.groupspace") == [["group_notice_send"]] * 2


# ---------------------------------------------------------------------------
# 3. 模型直接 {"done": true} → 不调任何群空间工具
# ---------------------------------------------------------------------------


async def test_done_true_calls_no_tool(
    mem_store: Store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks)
    gs = FakeGroupSpace(caps=_caps(notice_send=True, files_list=True))
    register_groupspace_tools(tools, gs, announce=lambda gid, text: None)

    models = ScriptedModels([_plan(), _review(pass_=True), '{"done": true}'])
    workers = FakeWorkers()
    workers.before_return = _write_artifact_hook(env, tasks, tid)
    delivery = FakeDelivery()

    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=workers, group_space=gs, delivery=delivery,
    )
    await coordinator.run_task(tid)

    assert tasks.get(tid)["status"] == "completed"
    assert len(delivery.delivered) == 1
    assert gs.notices == []
    assert gs.album_uploads == []
    rows = mem_store.read().execute(
        "SELECT tool FROM tool_calls WHERE tool IN ('group_files_list','group_file_manage',"
        "'group_notice_send','group_album_upload')"
    ).fetchall()
    assert rows == []
    # 群空间回合只开了一次（模型说不用就没后续）
    assert models.purposes().count("coordinator.groupspace") == 1
    # 这个群开了两项能力：两项都给了模型（要不要用由模型判断；顺序跟能力表一致）
    assert models.tool_names_of("coordinator.groupspace") == [
        ["group_files_list", "group_notice_send"]
    ]


# ---------------------------------------------------------------------------
# 4. 子 agent 工具列表里始终没有这 4 个群空间工具
# ---------------------------------------------------------------------------


async def test_worker_tools_never_include_groupspace(
    mem_store: Store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks)
    gs = FakeGroupSpace(
        caps=_caps(
            files_list=True, files_manage=True, notice_read=True,
            notice_send=True, album_list=True, album_upload=True,
        )
    )
    register_groupspace_tools(tools, gs, announce=lambda gid, text: None)

    requested = ["group_files_list", "group_notice_send", "write_file",
                 "group_album_upload", "group_file_manage"]
    models = ScriptedModels(
        [
            _plan(jobs=[{"brief": "写一页总结", "tools": requested}]),
            _review(pass_=True),
            '{"done": true}',
        ]
    )
    workers = FakeWorkers()
    workers.before_return = _write_artifact_hook(env, tasks, tid)

    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=workers, group_space=gs,
    )
    await coordinator.run_task(tid)

    # 主模型点名要了群空间工具，派给子 agent 时被摘掉（只剩 write_file）
    assert len(workers.calls) == 1
    assert workers.calls[0]["tools"] == ["write_file"]
    assert not set(workers.calls[0]["tools"]) & set(_GROUPSPACE_TOOLS)
    # 就算硬塞，子 agent 这个角色也拿不到这 4 个工具的规格（roles={"main"}）
    worker_specs = tools.specs("worker", requested)
    names = [s["function"]["name"] for s in worker_specs]
    assert names == ["write_file"]
    # 主模型自己的群空间回合：能力全开 → 4 个都给它
    assert models.tool_names_of("coordinator.groupspace") == [list(_GROUPSPACE_TOOLS)]
