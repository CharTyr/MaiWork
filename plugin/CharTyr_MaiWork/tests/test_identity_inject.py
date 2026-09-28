"""身份与工作记忆的「注入点」测试：SOUL/AGENTS/记忆确实进了对应提示词。

- feeds._write_posts：有 SOUL 就按「## MaiWork 的身份」写帖子，不再走「你（MaiBot）的人设」那套；
  没有 SOUL 回落原人设逻辑（老行为不能变）。
- feeds 的定关注点 / 打分 / 构想提示带「## 工作记忆（全局）」和本群那份；别的群的不带。
- topics._build_opener_prompt：开场白提示带「## MaiWork 的身份」。
- coordinator._plan / _review：主模型提示带「## 做事规矩」「## 工作记忆（全局）」和本群那份。
- workers 的 system 提示：子 agent 干活前先领 AGENTS 规矩。
- 验收通过后 coordinator 给主模型一次「有没有值得记的经验」小回合（remember 工具，最多 2 次调用，
  模型回 {"done": true} 收工；没 identity / 模型没配好就跳过，老节奏不变）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from CharTyr_MaiWork.config import load_settings
from CharTyr_MaiWork.identity import Identity, register_remember_tool
from CharTyr_MaiWork.store import Store

from fakes import FakeModelsQueue

pytestmark = pytest.mark.asyncio

NOW = 1_790_000_000.0
GID = "111"
GID_OTHER = "222"


def _settings(cfg: dict | None = None) -> Any:
    raw = cfg or {}
    raw.setdefault("groups", {"serve": [{"group": f"qq:{GID}"}, {"group": f"qq:{GID_OTHER}"}]})
    settings, _ = load_settings(raw)
    return settings


async def _identity(tmp_path: Path, store: Store, **cfg: Any) -> Identity:
    settings = _settings(cfg or None)
    ident = Identity(tmp_path / "data", store, lambda: settings)
    await ident.ensure_started()
    return ident


# ----------------------------------------------------------------------
# feeds 写帖子
# ----------------------------------------------------------------------


def _feeds_fixtures(tmp_path: Path) -> tuple:
    from fakes import FakeProfiles

    store = Store(tmp_path / "t.db")
    store.migrate()
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, profile_ready_ts) VALUES (?, ?)",
            (GID, 1_700_000_000.0),
        )
    profiles = FakeProfiles()
    profiles.entries_map[GID] = [{"category": "interest", "text": "本地大模型"}]
    return store, profiles


class _NoTopics:
    def add_candidate(self, *a: Any, **k: Any) -> None:
        pass


async def test_feeds_write_posts_uses_soul_when_present(tmp_path: Path) -> None:
    """有 SOUL：写帖子的提示词带「## MaiWork 的身份」+ SOUL 内容，不再带「你（MaiBot）的人设」。"""
    from CharTyr_MaiWork.feeds import Feeds

    store, profiles = _feeds_fixtures(tmp_path)
    identity = await _identity(tmp_path, store)
    identity.write("soul", "我是小麻，说话口语、短句、别端着。")
    settings = _settings()
    models = FakeModelsQueue(ready=True, replies=['{"posts": [{"i": 0, "body": "b", "reason": "r", "refs": [], "audience": [], "keywords": []}]}'])
    feeds = Feeds(store, models, None, profiles, _NoTopics(), lambda: settings, identity=identity)
    item = {"title": "t", "url": "https://x.com/a", "summary": "s", "kind": "news", "quote": "q", "scores": {"avg": 4}}
    await feeds._write_posts(GID, [item], settings)
    assert models.calls, "写帖子至少要调一次主模型"
    prompt = models.calls[0][1][-1]["content"]
    assert "## MaiWork 的身份" in prompt
    assert "我是小麻" in prompt
    assert "你（MaiBot）的人设" not in prompt
    assert item.get("post", {}).get("body") == "b"


async def test_feeds_write_posts_falls_back_without_soul(tmp_path: Path) -> None:
    """SOUL 为空：回落原人设逻辑（host.config）——老行为不能变。"""
    from CharTyr_MaiWork.feeds import Feeds

    store, profiles = _feeds_fixtures(tmp_path)
    settings = _settings()

    class _Host:
        async def config(self, key: str, default: Any = None) -> Any:
            return {"bot.nickname": "老麻", "personality.personality": "热心肠", "personality.reply_style": "短"}.get(key, default)

    models = FakeModelsQueue(ready=True, replies=['{"posts": []}'])
    feeds = Feeds(store, models, None, profiles, _NoTopics(), lambda: settings, host=_Host())
    item = {"title": "t", "url": "https://x.com/a", "summary": "s", "kind": "news", "quote": "q"}
    await feeds._write_posts(GID, [item], settings)
    prompt = models.calls[0][1][-1]["content"]
    assert "你（MaiBot）的人设" in prompt
    assert "老麻" in prompt


async def test_feeds_focus_and_score_and_idea_inject_memory(tmp_path: Path) -> None:
    """定关注点 / 打分 / 构想提示带「## 工作记忆（全局）」和本群那份；别的群的不带。"""
    from CharTyr_MaiWork.feeds import Feeds

    store, profiles = _feeds_fixtures(tmp_path)
    identity = await _identity(tmp_path, store)
    identity.write("memory", "- 管理员偏好表格交付")
    identity.group_write(GID, "- 这个群讨厌深科技长视频")
    identity.group_write(GID_OTHER, "- 别群的秘密经验")
    settings = _settings()
    models = FakeModelsQueue(ready=True, replies=[
        '{"focus": [{"query": "q1", "why": "w"}]}',  # _plan_focus
        '{"scores": []}',  # _score
        '{"idea": null}',  # make_idea
    ])
    feeds = Feeds(store, models, None, profiles, _NoTopics(), lambda: settings, identity=identity)
    focus = await feeds._plan_focus(GID, settings)
    assert focus and focus[0]["query"] == "q1"
    await feeds._score(GID, settings, [
        {"title": "t", "url": "https://x.com/a", "summary": "s", "kind": "news", "quote": "q"}
    ])
    await feeds.make_idea(GID)
    assert len(models.calls) == 3
    for i in range(3):
        prompt = models.calls[i][1][-1]["content"]
        assert "## 工作记忆（全局）" in prompt
        assert "管理员偏好表格交付" in prompt
        assert "## 这个群的工作记忆" in prompt
        assert "讨厌深科技长视频" in prompt
        assert "别群的秘密经验" not in prompt


# ----------------------------------------------------------------------
# topics 开场白
# ----------------------------------------------------------------------


async def test_topics_opener_injects_soul(tmp_path: Path) -> None:
    """开场白提示带「## MaiWork 的身份」+ SOUL 内容。"""
    from CharTyr_MaiWork.delivery import Mentions, Pushes
    from CharTyr_MaiWork.topics import Topics
    from fakes import FakeHost, FakeProfiles, SignalsStub

    store = Store(tmp_path / "t.db")
    store.migrate()
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, session_id, last_msg_ts) VALUES (?, ?, ?)",
            (GID, "sess-1", NOW - 3600.0),
        )
    settings = _settings()
    identity = await _identity(tmp_path, store)
    identity.write("soul", "我是小麻，开场像随口一提。")
    topics = Topics(
        store, FakeHost(msgs=[], session_id="sess-1"), FakeModelsQueue(ready=True), None, FakeProfiles(),
        Mentions(store, lambda: settings), Pushes(store, lambda: settings),
        lambda: settings, SignalsStub(), identity=identity,
    )
    msgs = await topics._build_opener_prompt(GID, "sess-1", {"title": "T", "brief": "B", "link": ""}, [])
    joined = "\n".join(str(m.get("content") or "") for m in msgs)
    assert "## MaiWork 的身份" in joined
    assert "我是小麻，开场像随口一提" in joined


# ----------------------------------------------------------------------
# workers system 提示
# ----------------------------------------------------------------------


async def test_workers_system_prompt_carries_agents(tmp_path: Path) -> None:
    """子 agent 的 system 提示带 AGENTS 规矩（## 做事规矩 + 内容）。"""
    from CharTyr_MaiWork.workers import _system_prompt

    store = Store(tmp_path / "t.db")
    store.migrate()
    identity = await _identity(tmp_path, store)
    identity.write("agents", "规矩甲：先想清楚再动手。")
    sys_prompt = _system_prompt("子 agent #1", GID, None, "", identity=identity)
    assert "## 做事规矩" in sys_prompt
    assert "规矩甲：先想清楚再动手" in sys_prompt


# ----------------------------------------------------------------------
# coordinator：计划 / 验收注入 + 验收后 remember 小回合
# ----------------------------------------------------------------------


class _CoordSettings:
    """test_coordinator 的 GID="900000001" + 本文件的 G2 另群。"""

    def __init__(self, ws_root: Path, with_groups: bool = True):
        self.environments = type("Env", (), {
            "workspace_root": Path(ws_root), "max_parallel": 2, "local_mode": "direct",
            "run_as": "maiwork", "memory_max": "512M", "runtime_max_sec": 1800, "command_timeout_s": 300,
        })()
        self.delivery = type("Dlv", (), {"quiet_hours": "23:00-08:00"})()
        if with_groups:
            from test_coordinator import GID as _TC_GID

            self.groups = {str(_TC_GID): object(), GID_OTHER: object()}

    def workspace_of(self, group_id: str) -> str:
        return f"g{group_id}"


def _coord_fixtures(tmp_path: Path, with_groups: bool = True) -> tuple:
    from CharTyr_MaiWork.environments.local import LocalEnv
    from CharTyr_MaiWork.goals import Goals
    from CharTyr_MaiWork.tasks import Tasks
    from CharTyr_MaiWork.tools import Tools
    from CharTyr_MaiWork.tools_exec import register_exec_tools

    store = Store(tmp_path / "t.db")
    store.migrate()
    settings = _CoordSettings(tmp_path / "workspaces", with_groups=with_groups)
    env = LocalEnv(lambda: settings)
    tools = Tools(store)
    register_exec_tools(tools, env=env, host=None, get_settings=lambda: settings, session_of=lambda gid: "sess-1")
    tasks = Tasks(store, lambda: settings)
    goals = Goals(store, lambda: settings)
    return store, settings, env, tools, tasks, goals


def _new_coordinator(store, models, workers, tools, tasks, goals, env, settings, identity=None):
    from test_coordinator import FakeDelivery, FakeOutbox, _Profiles
    from CharTyr_MaiWork.coordinator import Coordinator

    kwargs: dict[str, Any] = {}
    if identity is not None:
        kwargs["identity"] = identity
    return Coordinator(
        store, models, workers, tools, tasks, goals, FakeDelivery(), FakeOutbox(),
        env, _Profiles(), lambda: settings, **kwargs,
    )


async def test_coordinator_plan_and_review_inject_blocks(tmp_path: Path) -> None:
    from test_coordinator import ModelsQueue, FakeWorkers, _create_task, _plan, _review, GID as TC_GID

    store, settings, env, tools, tasks, goals = _coord_fixtures(tmp_path)
    identity = Identity(tmp_path / "data", store, lambda: settings)
    await identity.ensure_started()
    identity.write("agents", "规矩乙：交付前自查完成标准。")
    identity.write("memory", "- 全局经验：图表比长段文字受欢迎")
    identity.group_write(str(TC_GID), "- 本群经验：别在晚上十点后 @ 人")
    identity.group_write(GID_OTHER, "- 别群的秘密")

    models = ModelsQueue(replies=[_plan(), _review(pass_=True), '{"done": true}'])
    workers = FakeWorkers()
    tid = _create_task(tasks)

    async def _write_real():
        ws = env.workspace(tasks.get(tid)["workspace"])
        (ws / "artifacts" / tid).mkdir(parents=True, exist_ok=True)
        (ws / "artifacts" / tid / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    workers.before_return = _write_real
    coordinator = _new_coordinator(store, models, workers, tools, tasks, goals, env, settings, identity=identity)
    await coordinator.run_task(tid)

    plan_prompt = models.calls[0][1][-1]["content"]
    review_prompt = models.calls[1][1][-1]["content"]
    for p in (plan_prompt, review_prompt):
        assert "## 做事规矩" in p
        assert "规矩乙：交付前自查完成标准" in p
        assert "## 工作记忆（全局）" in p
        assert "图表比长段文字受欢迎" in p
        assert "## 这个群的工作记忆" in p
        assert "别在晚上十点后 @ 人" in p
        assert "别群的秘密" not in p


async def test_coordinator_remember_round_after_review(tmp_path: Path) -> None:
    """验收 pass 后、交付前给主模型一次「记经验」小回合：带 remember 工具；调过一次后记进本群。"""
    from test_coordinator import ModelsQueue, FakeWorkers, ReplayChatResult, _create_task, _plan, _review, GID as TC_GID

    store, settings, env, tools, tasks, goals = _coord_fixtures(tmp_path)
    identity = Identity(tmp_path / "data", store, lambda: settings)
    await identity.ensure_started()
    register_remember_tool(tools, identity)

    remember_args = {"scope": "group", "text": "这个群喜欢先看结论再看过程", "reason": "交付表单反馈"}
    remember_tc = [{
        "id": "rc-1",
        "type": "function",
        "function": {"name": "remember", "arguments": json.dumps(remember_args, ensure_ascii=False)},
    }]

    class ModelsSeq(ModelsQueue):
        async def chat(self, role, messages, **kwargs):
            snap = [dict(m) for m in messages]
            self.calls.append((role, snap, kwargs))
            if not self.reply_queue:
                return ReplayChatResult("{}")
            item = self.reply_queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            if item == "REMEMBER_ROUND":
                return ReplayChatResult("", tool_calls=remember_tc)
            return ReplayChatResult(str(item))

    models = ModelsSeq(replies=[_plan(), _review(pass_=True), "REMEMBER_ROUND", '{"done": true}'])
    workers = FakeWorkers()
    tid = _create_task(tasks)

    async def _write_real():
        ws = env.workspace(tasks.get(tid)["workspace"])
        (ws / "artifacts" / tid).mkdir(parents=True, exist_ok=True)
        (ws / "artifacts" / tid / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    workers.before_return = _write_real
    coordinator = _new_coordinator(store, models, workers, tools, tasks, goals, env, settings, identity=identity)
    await coordinator.run_task(tid)

    # 记进本群文件，全局没有
    text = identity.group_read(str(TC_GID))["text"]
    assert "先看结论再看过程" in text
    assert "先看结论再看过程" not in identity.read("memory")["text"]
    # events 有一条 memory.write
    row = store.read().execute("SELECT COUNT(*) c FROM events WHERE kind='memory.write'").fetchone()
    assert int(row["c"]) == 1
    assert str(tasks.get(tid)["status"]) == "completed"


async def test_coordinator_remember_round_skipped_without_identity(tmp_path: Path) -> None:
    """没 identity（老部署/模块没就位）：验收 pass 直接交付，不额外调模型（老节奏不变）。"""
    from test_coordinator import ModelsQueue, FakeWorkers, _create_task, _plan, _review

    store, settings, env, tools, tasks, goals = _coord_fixtures(tmp_path, with_groups=False)
    models = ModelsQueue(replies=[_plan(), _review(pass_=True)])
    workers = FakeWorkers()
    tid = _create_task(tasks)

    async def _write_real():
        ws = env.workspace(tasks.get(tid)["workspace"])
        (ws / "artifacts" / tid).mkdir(parents=True, exist_ok=True)
        (ws / "artifacts" / tid / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    workers.before_return = _write_real
    coordinator = _new_coordinator(store, models, workers, tools, tasks, goals, env, settings)
    await coordinator.run_task(tid)
    assert str(tasks.get(tid)["status"]) == "completed"
    # 只有计划 / 验收两次模型调用，没有第三次（remember 小回合跳过）
    assert len(models.calls) == 2


# ----------------------------------------------------------------------
# 定关注点带 AGENTS 规矩（用户在 AGENTS.md 里写的搜索要求，定关注点时也要生效）
# ----------------------------------------------------------------------


async def test_feeds_plan_focus_injects_agents_block(tmp_path: Path) -> None:
    """feeds._plan_focus：提示词带「## 做事规矩」（AGENTS.md）。"""
    from CharTyr_MaiWork.feeds import Feeds

    store, profiles = _feeds_fixtures(tmp_path)
    identity = await _identity(tmp_path, store)
    identity.write("agents", "搜索要求：优先官方来源。")
    settings = _settings()
    models = FakeModelsQueue(ready=True, replies=['{"focus": [{"query": "q1", "why": "w"}]}'])
    feeds = Feeds(store, models, None, profiles, _NoTopics(), lambda: settings, identity=identity)
    await feeds._plan_focus(GID, settings)
    prompt = models.calls[0][1][-1]["content"]
    assert "## 做事规矩" in prompt
    assert "优先官方来源" in prompt


async def test_personal_plan_focus_injects_agents_block(tmp_path: Path) -> None:
    """personal._plan_focus：提示词带「## 做事规矩」（AGENTS.md）。"""
    from CharTyr_MaiWork.personal import Personal

    store = Store(tmp_path / "t.db")
    store.migrate()
    identity = await _identity(tmp_path, store)
    identity.write("agents", "搜索要求：别找营销号。")
    settings = _settings()
    models = FakeModelsQueue(ready=True, replies=['{"focus": [{"query": "q1", "why": "w"}], "idea": null}'])
    from fakes import FakeProfiles as _FP

    personal = Personal(
        store, models, None, _FP(), _NoTopics(), lambda: settings, identity=identity,
    )
    plan = await personal._plan_focus(GID, "张三", {"summary": "在弄本地大模型", "doing": ["跑 llama.cpp"], "cares": [], "asked": []})
    assert plan["focus"][0]["query"] == "q1"
    prompt = models.calls[0][1][-1]["content"]
    assert "## 做事规矩" in prompt
    assert "别找营销号" in prompt
