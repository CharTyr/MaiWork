"""0.8.0 review 修复：个人向全阶段按产出类型注 group_context；开场白/提一嘴只规矩。

写范围只碰 personal.py / topics.py / card_push.py（本文件是新增测试，不改任何既有测试）。

对照 docs/17 §八.2 的注入表：

| 环节 | 规矩 | 做法 |
|---|---|---|
| 个人向（找料 / 打分 / 写帖子） | ✅ | 资讯或构想 skill（看产出类型） |
| 冷场开场白、构想提一嘴 | ✅ | — |

本文件盯五件事：

1. 个人向「定关注点」一次同时产出 news 关注点 + 0–1 条 idea → 规矩 + 资讯做法 + 构想做法；
   「找料子 agent brief / 打分 / 写帖子」都是 news → 规矩 + 资讯做法，不带构想做法。
2. 规矩只拼一份（news 段已经带过，idea 段不再重复）。
3. 跨群隔离：G1 的提示词里看不到 G2 的规矩 / 做法；本群没内容就一段都不注。
4. 冷场开场白（news / idea 两种候选）与 card_push 构想提一嘴：只要规矩（kind=main），
   不注 learned skill；SOUL 人格与规矩都不减。
5. 缺组件容错：_agents 没接线 / 读库抛错 → 照跑、不抛、提示词里没有那两段。

用真 Agents + 真 Store（库里真写 group_rules / agent_skills），只把「模型」和
「子 agent」换成记录型假对象，捕真实提示词；不手工 fake 核心。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.store import Store

from fakes import FakeHost, FakeModelsQueue, FakeProfiles
from test_feeds import FakeWorkers, _ok_report
from test_personal import (
    FakeTopics,
    _WORKER_ITEMS,
    _add_focus_member,
    _personal_scores_json,
    _time_patch,
)
from test_topics import GID as TOPIC_GID
from test_topics import SID as TOPIC_SID
from test_topics import _make_topics, _text_msg

G1 = "900000001"
G2 = "123456789"
UID = "10001"
UNAME = "阿帆"

_RULES = "本群规矩：只发中文，不许发营销稿"
_NEWS_SKILL = "资讯做法正文：先看画像再挑一手来源。【NEWS-SKILL】"
_IDEA_SKILL = "构想做法正文：只给一周内能做完的小忙。【IDEA-SKILL】"
_G2_RULES = "G2 的规矩：只发英文。【G2-RULES-ONLY】"
_G2_NEWS = "G2 的资讯做法。【G2-NEWS-ONLY】"
_G2_IDEA = "G2 的构想做法。【G2-IDEA-ONLY】"
# 关注成员的画像片段（persona summary，test_personal._persona_json 同款）
_PERSONA_FRAGMENT = "在折腾 FPGA 的小项目"
_SOUL = "你是 MaiWork 的小鱼，说话短、不客套。"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Settings:
    """Agents 的 is_served 只认这些群（和 test_group_context_inject 同款最小桩）。"""

    def __init__(self, served: Any = ()) -> None:
        self.served_groups = tuple(str(g) for g in served)

    def is_served(self, gid: Any) -> bool:
        return str(gid) in self.served_groups


class _BrokenAgents:
    """读库就抛的坏组件：group_context 内部吞掉，注入点必须照跑。"""

    def group_rules_get(self, gid: str) -> Any:
        raise RuntimeError("库坏了")

    def skills(self, gid: str, kind: Any = None, **kw: Any) -> Any:
        raise RuntimeError("库坏了")


class _Soul:
    """只提供 SOUL 的假 identity（voice.persona 只读 prompt_block("soul")）。"""

    def prompt_block(self, kind: str, group_id: Any = None) -> str:
        return _SOUL if str(kind) == "soul" else ""


def _prompts(models: FakeModelsQueue) -> dict[str, str]:
    """把模型调用拼成 {purpose: 全部 content}。"""
    out: dict[str, str] = {}
    for _role, messages, kw in models.calls:
        purpose = str(kw.get("purpose") or "")
        text = "\n".join(str((m or {}).get("content") or "") for m in (messages or []))
        out[purpose] = out.get(purpose, "") + "\n" + text
    return out


# ----------------------------------------------------------------------
# 真 Agents + 真 Store：G1 有规矩 / 资讯做法 / 构想做法；G2 有一套别的内容
# ----------------------------------------------------------------------


def _seed_group(store: Store, gid: str) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, profile_ready_ts) VALUES (?, ?)", (gid, 1_700_000_000.0)
        )


def _agents_with_content(store: Store, gid: str, other: str) -> Agents:
    agents = Agents(store, lambda: _Settings((gid, other)))
    agents.group_rules_set(gid, _RULES, updated_by="admin")
    agents.skill_add(gid, "news", description="", body=_NEWS_SKILL)
    agents.skill_add(gid, "idea", description="", body=_IDEA_SKILL)
    agents.group_rules_set(other, _G2_RULES, updated_by="admin")
    agents.skill_add(other, "news", description="", body=_G2_NEWS)
    agents.skill_add(other, "idea", description="", body=_G2_IDEA)
    return agents


def _personal_env(
    tmp_path: Path,
    *,
    gid: str = G1,
    other: str = G2,
    agents: Any = "auto",
) -> tuple[Store, Any, Any, FakeModelsQueue, FakeWorkers, Any, Any]:
    """起好个人向一台车：真 Store + 真 Agents + 记录型模型/子 agent。"""
    store = Store(tmp_path / "t.db")
    store.migrate()
    _seed_group(store, gid)
    if other:
        _seed_group(store, other)
    _add_focus_member(store, UID, gid, name=UNAME)
    raw = {"plugin": {"enabled": True}, "groups": {"serve": [{"group": f"qq:{gid}"}]}}
    settings, _problems = load_settings(raw)
    models = FakeModelsQueue(
        ready=True,
        replies=[
            json.dumps(
                {
                    "focus": [{"query": "FPGA 学习板 新出", "why": "在做 FPGA 学习板"}],
                    "idea": {
                        "title": "我可以帮你把这板例程理成一页",
                        "body": "我可以帮你把这板子的例程整理成一页",
                        "step": "先列出要跑的例程",
                        "effort": "半天",
                        "origin": "FPGA 小板子",
                    },
                },
                ensure_ascii=False,
            ),
            _personal_scores_json(),
            json.dumps(
                {
                    "posts": [
                        {"i": 0, "title": "新开源 FPGA 学习板上架",
                         "body": "你在弄 FPGA 学习板，这块新板可能用得上……",
                         "reason": "你前几天在群里提到想找一块便宜的学习板",
                         "refs": [], "audience": [], "keywords": ["FPGA"]},
                        {"i": 1, "title": "本地小模型量化教程",
                         "body": "你在搭本地小模型，这个量化教程可能用得上……",
                         "reason": "你之前在弄本地模型",
                         "refs": [], "audience": [], "keywords": ["模型"]},
                    ]
                },
                ensure_ascii=False,
            ),
        ],
    )
    workers = FakeWorkers(_ok_report(_WORKER_ITEMS))
    topics = FakeTopics()
    from CharTyr_MaiWork.maiwork.personal import Personal

    personal = Personal(store, models, workers, FakeProfiles(), topics, lambda: settings)
    if agents == "auto":
        agents = _agents_with_content(store, gid, other) if other else Agents(store, lambda: _Settings((gid,)))
    personal._agents = agents  # noqa: SLF001  （app._wire_specialists 的接线点）
    return store, settings, personal, models, workers, topics, agents


def _run_personal(personal: Any, gid: str = G1) -> int:
    with _time_patch():
        return _run(personal.prepare_personal(gid, UID))


# ----------------------------------------------------------------------
# 1. 定关注点：规矩 + 资讯做法 + 构想做法（一次同时产出两种）
# ----------------------------------------------------------------------


def test_plan_focus_prompt_has_rules_news_and_idea_skill(tmp_path):
    """个人向「定关注点」按实际产出类型注：news 关注点 + 顺带那条 idea 两种做法都进来。"""
    _store, _settings_obj, personal, models, _workers, _topics, _agents = _personal_env(tmp_path)
    got = _run_personal(personal)
    assert got > 0, "这一轮该有产出"
    prompt = _prompts(models)["personal.focus"]
    assert _RULES in prompt, "本群规矩要进定关注点提示词"
    assert _NEWS_SKILL in prompt, "资讯做法要进定关注点（关注点按资讯岗挑）"
    assert _IDEA_SKILL in prompt, "构想做法要进定关注点（这一次同时产出 0–1 条 idea）"
    assert prompt.count(_RULES) == 1, "规矩只许拼一份（news 段已带，idea 段不重复）"


def test_plan_focus_prompt_no_other_group_content(tmp_path):
    """跨群隔离：G2 的规矩 / 做法一个字都不许出现在 G1 的定关注点提示词里。"""
    _store, _settings_obj, personal, models, _workers, _topics, _agents = _personal_env(tmp_path)
    _run_personal(personal)
    prompt = _prompts(models)["personal.focus"]
    for leak in (_G2_RULES, _G2_NEWS, _G2_IDEA):
        assert leak not in prompt, f"跨群泄漏：{leak}"


# ----------------------------------------------------------------------
# 2. 找料 brief / 打分 / 写帖子：都是 news → 规矩 + 资讯做法，不带构想做法
# ----------------------------------------------------------------------


def test_collect_brief_has_rules_and_news_skill_only(tmp_path):
    """找料子 agent 的 brief 注 group_context(kind=news)：规矩 + 资讯做法，不注构想做法。"""
    _store, _settings_obj, personal, _models, workers, _topics, _agents = _personal_env(tmp_path)
    _run_personal(personal)
    assert workers.calls, "该派了找料子 agent"
    brief = str(workers.calls[0]["brief"])
    assert _RULES in brief
    assert _NEWS_SKILL in brief
    assert _IDEA_SKILL not in brief, "找的是资讯，不该注构想做法"
    assert _G2_NEWS not in brief and _G2_RULES not in brief


def test_score_prompt_has_rules_and_news_skill_only(tmp_path):
    """打分按 news 注：规矩 + 资讯做法，不注构想做法。"""
    _store, _settings_obj, personal, models, _workers, _topics, _agents = _personal_env(tmp_path)
    _run_personal(personal)
    prompt = _prompts(models)["personal.score"]
    assert _RULES in prompt
    assert _NEWS_SKILL in prompt
    assert _IDEA_SKILL not in prompt
    assert _G2_NEWS not in prompt and _G2_IDEA not in prompt


def test_post_prompt_has_rules_and_news_skill_only(tmp_path):
    """写帖子按 news 注：规矩 + 资讯做法，不注构想做法。"""
    _store, _settings_obj, personal, models, _workers, _topics, _agents = _personal_env(tmp_path)
    _run_personal(personal)
    prompt = _prompts(models)["personal.post"]
    assert _RULES in prompt
    assert _NEWS_SKILL in prompt
    assert _IDEA_SKILL not in prompt
    assert _G2_NEWS not in prompt


def test_personal_items_stay_personal(tmp_path):
    """注了本群规矩之后，个人向条目照样只给本人（target_user_id）+ 不进话题候选池。"""
    store, _settings_obj, personal, _models, _workers, topics, _agents = _personal_env(tmp_path)
    _run_personal(personal)
    rows = store.read().execute("SELECT target_user_id FROM news_items ORDER BY id").fetchall()
    assert rows, "该有落库条目"
    assert all(str(r["target_user_id"]) == UID for r in rows)
    assert topics.calls == []


# ----------------------------------------------------------------------
# 3. 缺组件容错：没接线 / 读库抛错 → 照跑、不抛、不注
# ----------------------------------------------------------------------


def test_missing_agents_still_runs_and_injects_nothing(tmp_path):
    """_agents=None（专岗没接线）：个人向照跑，提示词里没有本群规矩 / 做法，也不抛。"""
    _store, _settings_obj, personal, models, workers, _topics, _agents = _personal_env(
        tmp_path, agents=None
    )
    got = _run_personal(personal)
    assert got > 0
    assert workers.calls
    captured = _prompts(models)
    for purpose in ("personal.focus", "personal.score", "personal.post"):
        blob = captured.get(purpose, "")
        assert _RULES not in blob and _NEWS_SKILL not in blob and _IDEA_SKILL not in blob


def test_broken_agents_does_not_break_pipeline(tmp_path):
    """组件在但读库抛错：group_context 吞掉，个人向照跑，不进任何本群内容。"""
    _store, _settings_obj, personal, models, workers, _topics, _agents = _personal_env(
        tmp_path, agents=_BrokenAgents()
    )
    got = _run_personal(personal)
    assert got > 0
    assert workers.calls
    blob = _prompts(models)["personal.focus"]
    assert _RULES not in blob and _NEWS_SKILL not in blob


def test_group_without_content_injects_nothing(tmp_path):
    """规矩 / 做法都在别的群：本群一段不注（真 Agents，别的群有内容）。"""
    _store, _settings_obj, personal, models, _workers, _topics, agents = _personal_env(tmp_path)
    # 抹掉本群内容，只留 G2 的
    agents.group_rules_set(G1, "", updated_by="admin")
    for skill in agents.skills(G1):
        agents.skill_update(G1, skill["id"], status="archived")
    got = _run_personal(personal)
    assert got > 0
    blob = _prompts(models)["personal.focus"]
    assert _RULES not in blob and _NEWS_SKILL not in blob and _IDEA_SKILL not in blob
    assert _G2_RULES not in blob and _G2_NEWS not in blob and _G2_IDEA not in blob


# ----------------------------------------------------------------------
# 4. 冷场开场白：只要规矩（kind=main），不注 learned skill；人格不减
# ----------------------------------------------------------------------


def _topics_with_agents(tmp_path: Path, *, gid: str = TOPIC_GID) -> tuple[Any, Any, FakeModelsQueue]:
    store, _settings_obj, topics, _host, models, *_ = _make_topics(tmp_path)
    agents = Agents(store, lambda: _Settings((gid, G2)))
    agents.group_rules_set(gid, _RULES, updated_by="admin")
    agents.skill_add(gid, "news", description="", body=_NEWS_SKILL)
    agents.skill_add(gid, "idea", description="", body=_IDEA_SKILL)
    agents.group_rules_set(G2, _G2_RULES, updated_by="admin")
    agents.skill_add(G2, "news", description="", body=_G2_NEWS)
    agents.skill_add(G2, "idea", description="", body=_G2_IDEA)
    topics._agents = agents  # noqa: SLF001
    topics._identity = _Soul()  # noqa: SLF001
    return store, topics, models


def _opener_text(topics: Any, candidate: dict[str, Any], gid: str = TOPIC_GID) -> str:
    msgs = [_text_msg(1_790_000_000.0 - 600.0, "这周末有人去漫展吗", uid="10001", name="张三")]
    out = _run(topics._build_opener_prompt(gid, TOPIC_SID, candidate, msgs))
    return "\n".join(str(m.get("content") or "") for m in out)


def test_news_opener_only_rules_no_skill(tmp_path):
    """news 候选的开场白：只注规矩（kind=main），不注资讯 / 构想 skill；人格和规矩都在。"""
    _store, topics, _models = _topics_with_agents(tmp_path)
    text = _opener_text(
        topics, {"kind": "news", "title": "测试候选", "brief": "简报", "link": "http://x"}
    )
    assert _RULES in text, "规矩要在（不减少规矩）"
    assert _SOUL in text, "SOUL 人格不减"
    assert _NEWS_SKILL not in text, "开场白不该注 learned skill"
    assert _IDEA_SKILL not in text
    assert _G2_RULES not in text and _G2_NEWS not in text


def test_idea_opener_only_rules_no_skill(tmp_path):
    """idea 候选的开场白同样只注规矩（kind=main），不注构想 / 资讯 skill。"""
    _store, topics, _models = _topics_with_agents(tmp_path)
    text = _opener_text(
        topics, {"kind": "idea", "title": "测试构想", "brief": "想法", "ref_id": 7}
    )
    assert _RULES in text
    assert _SOUL in text
    assert _IDEA_SKILL not in text and _NEWS_SKILL not in text
    assert _G2_IDEA not in text


def test_opener_without_agents_runs(tmp_path):
    """开场白没接线 agents：只少这两段，不抛。"""
    _store, _settings_obj, topics, *_ = _make_topics(tmp_path)
    topics._identity = _Soul()  # noqa: SLF001
    text = _opener_text(
        topics, {"kind": "news", "title": "测试候选", "brief": "简报", "link": "http://x"}
    )
    assert _RULES not in text and _NEWS_SKILL not in text
    assert _SOUL in text


def test_opener_never_leaks_persona(tmp_path):
    """开场白要发进群：注入的规矩段里不许出现关注成员画像片段 / 平台 id。"""
    _store, topics, _models = _topics_with_agents(tmp_path)
    text = _opener_text(
        topics, {"kind": "news", "title": "测试候选", "brief": "简报", "link": "http://x"}
    )
    assert _PERSONA_FRAGMENT not in text
    assert UID not in text


# ----------------------------------------------------------------------
# 5. card_push 构想提一嘴：只要规矩（kind=main），不注 learned skill
# ----------------------------------------------------------------------


def _idea_row(store: Store, gid: str) -> Any:
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO ideas (group_id, title, body, basis, step, effort, origin, state,"
            " created, updated, target_user_id) VALUES (?, '我可以帮你把例程理成一页',"
            " '帮你把例程整理成一页', '', '先列出例程', '半天', 'FPGA 小板子', 'new',"
            " 1.0, 1.0, '')",
            (gid,),
        )
        row_id = int(cur.lastrowid or 0)
    return store.read().execute("SELECT * FROM ideas WHERE id=?", (row_id,)).fetchone()


def _mention_env(tmp_path: Path, *, gid: str = G1, agents: Any = "auto"):
    from CharTyr_MaiWork.maiwork.card_push import IdeaMention

    store = Store(tmp_path / "t.db")
    store.migrate()
    _seed_group(store, gid)
    _seed_group(store, G2)
    raw = {"plugin": {"enabled": True}, "groups": {"serve": [{"group": f"qq:{gid}"}]}}
    settings, _problems = load_settings(raw)
    models = FakeModelsQueue(
        ready=True,
        replies=[
            json.dumps(
                {"text": "我可以帮你把例程理成一页，要不要我来弄？"}, ensure_ascii=False
            )
        ],
    )
    mention = IdeaMention(
        store, FakeHost(msgs=[], session_id="s"), models, None, None, lambda: settings
    )
    if agents == "auto":
        agents = _agents_with_content(store, gid, G2)
    mention._agents = agents  # noqa: SLF001
    mention._identity = _Soul()  # noqa: SLF001
    return store, mention, models, _idea_row(store, gid)


def test_idea_mention_only_rules_no_skill(tmp_path):
    """构想提一嘴：只要规矩，不注 learned skill；人格不减；不跨群；不露画像。"""
    _store, mention, models, row = _mention_env(tmp_path)
    text = _run(mention._write(G1, row, False, ""))
    prompt = _prompts(models)["card_push.idea_mention"]
    assert _RULES in prompt
    assert _SOUL in prompt
    assert _IDEA_SKILL not in prompt, "提一嘴不该注 learned skill"
    assert _NEWS_SKILL not in prompt
    assert _G2_RULES not in prompt and _G2_IDEA not in prompt
    assert _PERSONA_FRAGMENT not in prompt and UID not in prompt
    assert text.strip(), "写话失败也该回落模板，不能空"


def test_idea_mention_without_agents_runs(tmp_path):
    """提一嘴没接线 agents：照样出话（回落模板 / 模型话），不抛。"""
    _store, mention, _models, row = _mention_env(tmp_path, agents=None)
    text = _run(mention._write(G1, row, False, ""))
    assert text.strip()
