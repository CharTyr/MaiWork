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

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.identity import Identity, register_remember_tool
from CharTyr_MaiWork.maiwork.store import Store

from fakes import FakeModelsQueue, focus_reply

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
    from CharTyr_MaiWork.maiwork.feeds import Feeds

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


async def test_feeds_write_posts_no_maibot_fallback_without_soul(tmp_path: Path) -> None:
    """SOUL 为空：不带人设，**不再**回落去读 MaiBot 人格（2026-10-01 用户定：人设只认 SOUL.md）。"""
    from CharTyr_MaiWork.maiwork.feeds import Feeds

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
    assert "你（MaiBot）的人设" not in prompt
    assert "老麻" not in prompt and "热心肠" not in prompt


async def test_feeds_focus_and_score_inject_memory(tmp_path: Path) -> None:
    """定关注点 / 打分提示带「## 工作记忆（全局）」+ 本群三份（规矩 + 做法 skill）。

    「每群三份」收尾：每群内容只走 group_context(gid, kind)——所以 prompt 里要出现
    本群规矩（【本群规矩（管理员定的，必须照做）】）和本群<资讯>做法
    （【本群资讯的做法（MaiWork 总结的，是参考）】），别群的规矩/skill 一律不带。
    """
    from CharTyr_MaiWork.maiwork.agents import Agents
    from CharTyr_MaiWork.maiwork.feeds import Feeds

    store, profiles = _feeds_fixtures(tmp_path)
    identity = await _identity(tmp_path, store)
    identity.write("memory", "- 管理员偏好表格交付")
    settings = _settings()
    # 业务里 group_context 靠 agents 拿规矩 / skill；这里提前填好两个群各自一份
    agents = Agents(store, lambda: settings)
    agents.group_rules_set(GID, "这个群讨厌深科技长视频", updated_by="admin")
    agents.skill_add(GID, "news", description="", body="这个群爱看开源硬件", source="admin")
    agents.group_rules_set(GID_OTHER, "别群的秘密规矩", updated_by="admin")
    agents.skill_add(GID_OTHER, "news", description="", body="别群的秘密做法", source="admin")
    models = FakeModelsQueue(ready=True, replies=[
        focus_reply("q1", "q2", "q3"),  # _plan_focus（给 3 个免得触发追问重试）
        # _score（给一条对得上的：打分分批后「一条都没对上」算失败会抛错）
        '{"scores": [{"i": 0, "info": 3, "source": 3, "relevance": 3, "timeliness": 3, "chat": 3}]}',
    ])
    feeds = Feeds(store, models, None, profiles, _NoTopics(), lambda: settings, identity=identity)
    # 挂上 specialists 给 group_context 走（老代码 feeds._specialists 就是 fake 这么走的）
    feeds._specialists = type("SP", (), {"agents": agents})()
    focus = await feeds._plan_focus(GID, settings)
    assert focus and focus[0]["query"] == "q1"
    await feeds._score(GID, settings, [
        {"title": "t", "url": "https://x.com/a", "summary": "s", "kind": "news", "quote": "q"}
    ])
    assert len(models.calls) == 2
    for i in range(2):
        prompt = models.calls[i][1][-1]["content"]
        assert "## 工作记忆（全局）" in prompt
        assert "管理员偏好表格交付" in prompt
        # 本群三份：规矩 + （资讯岗的）做法，别群的绝不出现
        assert "本群规矩" in prompt
        assert "讨厌深科技长视频" in prompt
        assert "本群资讯的做法" in prompt or "爱看开源硬件" in prompt
        assert "别群的秘密规矩" not in prompt
        assert "别群的秘密做法" not in prompt


# ----------------------------------------------------------------------
# topics 开场白
# ----------------------------------------------------------------------


async def test_topics_opener_injects_soul(tmp_path: Path) -> None:
    """开场白提示带「## MaiWork 的身份」+ SOUL 内容。"""
    from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
    from CharTyr_MaiWork.maiwork.topics import Topics
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
    """子 agent 的 system 提示带它自己 kind 的 AGENTS 规矩（## 做事规矩 + 内容）。
    （2026-10 改版 3/4：跑 task 就注 task 那份；main 那份只进主模型，不往子 agent 口里塞。）"""
    from CharTyr_MaiWork.maiwork.workers import _system_prompt

    store = Store(tmp_path / "t.db")
    store.migrate()
    identity = await _identity(tmp_path, store)
    # sub agent 默认跑 task 岗：改 task 的 AGENTS.md，应该进提示词；
    # main 的（全局 API 写过去那份）不动子 agent 这口
    identity.agent_write("task", "agents", "规矩甲：先想清楚再动手。")
    identity.write("agents", "主模型的规矩不能漏给子 agent。")
    sys_prompt = _system_prompt("子 agent #1", GID, None, "", identity=identity)
    assert "## 做事规矩" in sys_prompt
    assert "规矩甲：先想清楚再动手" in sys_prompt
    assert "主模型的规矩不能漏" not in sys_prompt


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


class _AgentsSettingsShim:
    """给 Agents 用的 settings：需要 is_served；包装 _CoordSettings 但保持窄——
    每群三份的 Agents 阶级（group_rules_set / skill_add）都要求这个口。"""

    def __init__(self, base: _CoordSettings):
        self._base = base

    def is_served(self, gid: object) -> bool:
        return str(gid or "") in (self._base.groups or {})

    def __getattr__(self, name: str):
        return getattr(self._base, name)


def _coord_fixtures(tmp_path: Path, with_groups: bool = True) -> tuple:
    from CharTyr_MaiWork.maiwork.environments.local import LocalEnv
    from CharTyr_MaiWork.maiwork.goals import Goals
    from CharTyr_MaiWork.maiwork.tasks import Tasks
    from CharTyr_MaiWork.maiwork.tools import Tools
    from CharTyr_MaiWork.maiwork.tools_exec import register_exec_tools

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
    from CharTyr_MaiWork.maiwork.coordinator import Coordinator

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
    shim = _AgentsSettingsShim(settings)
    identity = Identity(tmp_path / "data", store, lambda: shim)
    await identity.ensure_started()
    identity.write("agents", "规矩乙：交付前自查完成标准。")
    identity.write("memory", "- 全局经验：图表比长段文字受欢迎")
    # 「每群三份」收尾：每群内容（本群规矩 + 本群做法）由 group_context 注入。
    # 这里把 Agents 挂出来给 coordinator 的 _group_context_safe 用。
    from CharTyr_MaiWork.maiwork.agents import Agents
    agents = Agents(store, lambda: shim)
    agents.group_rules_set(str(TC_GID), "本群规矩：别在晚上十点后 @ 人", updated_by="admin")
    agents.group_rules_set(GID_OTHER, "别群的秘密规矩", updated_by="admin")
    agents.skill_add(GID_OTHER, "task", name="别群做法", description="d", body="secret", source="admin")

    models = ModelsQueue(replies=[_plan(), _review(pass_=True), '{"done": true}'])
    workers = FakeWorkers()
    tid = _create_task(tasks)

    async def _write_real():
        ws = env.workspace(tasks.get(tid)["workspace"])
        (ws / "artifacts" / tid).mkdir(parents=True, exist_ok=True)
        (ws / "artifacts" / tid / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    workers.before_return = _write_real
    coordinator = _new_coordinator(store, models, workers, tools, tasks, goals, env, shim, identity=identity)
    # 假 Specialists：能 run（拿到 brief 直接回报 ok），又能给 _group_context_safe
    # 拿 agents（这才是把本群规矩塞进计划 / 验收提示的口子）
    from CharTyr_MaiWork.maiwork.workers import WorkerReport

    captured_briefs: list[str] = []

    class _FakeSpecialists:
        def __init__(self, agents) -> None:
            self.agents = agents

        async def run(self, kind, brief, **kw):
            captured_briefs.append(brief)
            return WorkerReport(ok=True, summary="收工", data={"items": []})

    coordinator._specialists = _FakeSpecialists(agents)
    await coordinator.run_task(tid)

    plan_prompt = models.calls[0][1][-1]["content"]
    # 领队 lane（docs/20）：验收请求接在排计划那段对话后面，身份那一大段只发一次——
    # 看整次请求里有没有（别群的照样一个字都不能有）
    review_request = "\n".join(str(m.get("content") or "") for m in models.calls[1][1])
    assert "没变" in models.calls[1][1][-1]["content"]
    for p in (plan_prompt, review_request):
        assert "## 做事规矩" in p
        assert "规矩乙：交付前自查完成标准" in p
        assert "## 工作记忆（全局）" in p
        assert "图表比长段文字受欢迎" in p
        # 每群三份：本群规矩注入，别群的绝不漏
        assert "本群规矩" in p
        assert "别在晚上十点后 @ 人" in p
        assert "别群的秘密规矩" not in p
        assert "secret" not in p


async def test_coordinator_remember_round_after_review(tmp_path: Path) -> None:
    """验收后只记与群、人无关的全局方法；自动记忆回合不得修改本群规矩。"""
    from test_coordinator import ModelsQueue, FakeWorkers, ReplayChatResult, _create_task, _plan, _review, GID as TC_GID

    store, settings, env, tools, tasks, goals = _coord_fixtures(tmp_path)
    shim = _AgentsSettingsShim(settings)
    identity = Identity(tmp_path / "data", store, lambda: shim)
    await identity.ensure_started()
    register_remember_tool(tools, identity)

    remember_args = {"scope": "global", "text": "验收前逐项对照完成标准", "reason": "通用验收方法"}
    from CharTyr_MaiWork.maiwork.agents import Agents
    group_agents = Agents(store, lambda: shim)
    original_rules = group_agents.group_rules_set(str(TC_GID), "本群不要发广告", updated_by="admin")
    original_versions = group_agents.group_rules_versions(str(TC_GID))
    remember_tc = [{
        "id": "rc-1",
        "type": "function",
        "function": {"name": "remember", "arguments": json.dumps(remember_args, ensure_ascii=False)},
    }]

    class ModelsSeq(ModelsQueue):
        async def chat(self, role=None, messages=None, **kwargs):
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
    coordinator = _new_coordinator(store, models, workers, tools, tasks, goals, env, shim, identity=identity)
    await coordinator.run_task(tid)

    # 自动回合只记全局通用方法，规矩正文、来源、时间和历史均不得变化。
    assert group_agents.group_rules_get(str(TC_GID)) == original_rules
    assert group_agents.group_rules_versions(str(TC_GID)) == original_versions
    assert "逐项对照完成标准" in identity.read("memory")["text"]
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
    from CharTyr_MaiWork.maiwork.feeds import Feeds

    store, profiles = _feeds_fixtures(tmp_path)
    identity = await _identity(tmp_path, store)
    identity.write("agents", "搜索要求：优先官方来源。")
    settings = _settings()
    models = FakeModelsQueue(ready=True, replies=[focus_reply("q1", "q2", "q3")])
    feeds = Feeds(store, models, None, profiles, _NoTopics(), lambda: settings, identity=identity)
    await feeds._plan_focus(GID, settings)
    prompt = models.calls[0][1][-1]["content"]
    assert "## 做事规矩" in prompt
    assert "优先官方来源" in prompt


async def test_personal_plan_focus_injects_agents_block(tmp_path: Path) -> None:
    """personal._plan_focus：提示词带「## 做事规矩」（AGENTS.md）。"""
    from CharTyr_MaiWork.maiwork.personal import Personal

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
