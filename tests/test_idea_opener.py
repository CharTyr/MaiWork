"""topics._build_opener_prompt：构想候选的开场白要「关心式问法」（2026-10 与用户定）。

- kind == "idea"：从 ideas 表按 ref_id 读 origin / title / body；有由头就说「话说之前…怎么样了？
  要我帮忙吗？」，没有就按构想内容自然问一句要不要帮忙；不点名任何群友、不提个人情况。
- kind == "news"：保持原来的写法（随口一提，把候选素材带出来）。
- 两个都用 voice.persona 的人设：只认 SOUL（2026-10-01 用户定），不自我介绍、不寒暄。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
from CharTyr_MaiWork.maiwork.host import Msg
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.topics import Topics

from fakes import FakeHost, FakeModelsQueue, FakeProfiles, SignalsStub

pytestmark = pytest.mark.asyncio

GID = "111"
SID = "sess-1"
NOW = 1_790_000_000.0


class Jev:
    def available(self) -> bool:
        return False

    async def ask(self, *a: Any, **kw: Any):
        return None


class Identity:
    def __init__(self, blocks: Dict[str, str] | None = None) -> None:
        self.blocks = dict(blocks or {})

    def prompt_block(self, kind: str, **kw: Any) -> str:
        return self.blocks.get(kind, "")


def _msgs() -> List[Msg]:
    return [
        Msg(id="m1", ts=NOW - 600, user_id="u1", user_name="阿一",
            text="这周的百层挑战又没过", is_bot=False, is_at=False, is_picture=False, reply_to=""),
        Msg(id="m2", ts=NOW - 300, user_id="bot", user_name="麦麦",
            text="慢慢来，下周再试", is_bot=True, is_at=False, is_picture=False, reply_to=""),
    ]


def _make(tmp_path, *, identity=None, host=None) -> tuple:
    store = Store(tmp_path / "t.db")
    store.migrate()
    settings, _ = load_settings({})
    if host is None:
        host = FakeHost(msgs=_msgs(), session_id=SID)
    topics = Topics(
        store, host, FakeModelsQueue(ready=True), Jev(), FakeProfiles(),
        Mentions(store, lambda: settings), Pushes(store, lambda: settings),
        lambda: settings, SignalsStub(), identity,
    )
    return store, topics


def _seed_idea(store: Store, *, title: str, body: str, origin: str) -> int:
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO ideas (group_id, title, body, basis, state, created, updated, origin)"
            " VALUES (?, ?, ?, '画像依据（不该进群里）', 'new', ?, ?, ?)",
            (GID, title, body, clock.now(), clock.now(), origin),
        )
        return int(cur.lastrowid)


def _candidate(ref_id: int, *, title: str, brief: str) -> Dict[str, Any]:
    return {"id": 1, "kind": "idea", "ref_id": ref_id, "title": title, "brief": brief, "link": ""}


async def test_idea_opener_with_origin(tmp_path) -> None:
    """有由头：提示词要求「话说之前大家聊的那个{origin}…要我帮忙…吗？」式关心问法。"""
    store, topics = _make(tmp_path)
    iid = _seed_idea(store, title="我可以帮大家把百层挑战的战报汇总",
                     body="每周汇总一次战报", origin="涂击队百层挑战")
    prompt = await topics._build_opener_prompt(
        GID, SID, _candidate(iid, title="我可以帮大家把百层挑战的战报汇总", brief="每周汇总一次战报"),
        _msgs(),
    )
    text = json.dumps(prompt, ensure_ascii=False)
    assert "涂击队百层挑战" in text           # 由头
    assert "关心" in text                     # 关心式问法
    assert "问句" in text                     # 结尾问句
    assert "不点名" in text                   # 群向：不点名任何人
    assert "别推销" in text                   # 不许推销腔
    assert "画像依据" not in text             # basis 不进提示词（会引画像）


async def test_idea_opener_without_origin(tmp_path) -> None:
    """没由头：按构想内容自然问一句要不要帮忙。"""
    store, topics = _make(tmp_path)
    iid = _seed_idea(store, title="我可以帮大家整理一份复习资料",
                     body="把大家整理的资料汇总成一页", origin="")
    prompt = await topics._build_opener_prompt(
        GID, SID, _candidate(iid, title="我可以帮大家整理一份复习资料", brief="把资料汇总成一页"), _msgs(),
    )
    text = json.dumps(prompt, ensure_ascii=False)
    assert "复习资料" in text
    assert "没给由头" in text or "没有由头" in text
    assert "关心" in text


@pytest.mark.parametrize("kind", ["idea", "news"])
async def test_opener_voice_is_soul_only(tmp_path, kind) -> None:
    """人设只认 SOUL（2026-10-01 用户定）：MaiBot 的人格设定不进提示词；要求不自我介绍、不寒暄。"""
    host = FakeHost(msgs=_msgs(), session_id=SID)
    store, topics = _make(tmp_path, host=host,
                          identity=Identity({"soul": "## MaiWork 的身份\n你是这群的老熟人。"}))
    iid = _seed_idea(store, title="我可以帮大家把百层挑战的战报汇总",
                     body="每周汇总一次", origin="涂击队百层挑战")
    cand = _candidate(iid, title="T", brief="B")
    cand["kind"] = kind
    prompt = await topics._build_opener_prompt(GID, SID, cand, _msgs())
    text = json.dumps(prompt, ensure_ascii=False)
    assert "测试AI" not in text and "乐于助人" not in text and "简短口语" not in text  # FakeHost.config
    assert "老熟人" in text                # SOUL
    assert "不自我介绍" in text and "不寒暄" in text
    assert "你扮演" not in text and "群里的 AI 助手" not in text


async def test_opener_without_soul_reads_no_maibot_persona(tmp_path) -> None:
    host = FakeHost(msgs=_msgs(), session_id=SID)
    store, topics = _make(tmp_path, host=host, identity=Identity({"soul": ""}))
    candidate = {"id": 2, "kind": "news", "ref_id": 7, "title": "新开源 FPGA 开发板发布",
                 "brief": "配置不错", "link": "https://example.com/board"}
    prompt = await topics._build_opener_prompt(GID, SID, candidate, _msgs())
    text = json.dumps(prompt, ensure_ascii=False)
    assert "测试AI" not in text and "乐于助人" not in text and "简短口语" not in text


async def test_news_opener_keeps_old_shape(tmp_path) -> None:
    """news 候选：还是「随手起个话头」的写法，不出现构想那套由头要求。"""
    store, topics = _make(tmp_path)
    candidate = {"id": 2, "kind": "news", "ref_id": 7, "title": "新开源 FPGA 开发板发布",
                 "brief": "配置不错", "link": "https://example.com/board"}
    prompt = await topics._build_opener_prompt(GID, SID, candidate, _msgs())
    text = json.dumps(prompt, ensure_ascii=False)
    assert "新开源 FPGA 开发板发布" in text
    assert "随手起个话头" in text
    assert "播报腔" in text
    assert "由头" not in text
    assert "关心" not in text
