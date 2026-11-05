"""topic-opener skill 补种 / 开场白身份与草稿护栏 / 上游思考链混进 content 的兜底。"""

from __future__ import annotations

from pathlib import Path

from CharTyr_MaiWork.maiwork import skills_web, voice
from CharTyr_MaiWork.maiwork.models import Models, _strip_leaked_reasoning
from CharTyr_MaiWork.maiwork.skills import Skills
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.topics import Topics, _OPENER_MAX_CHARS

_FORBIDDEN = ("由头", "关心", "要我帮忙吗", "要不要帮忙", "画像依据", "测试AI",
              "乐于助人", "简短口语", "你扮演", "群里的 AI 助手")


def _store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.migrate()
    return s


# ---------------- models._strip_leaked_reasoning / _parse_chat ----------------

def test_strip_reasoning_dump_equal():
    assert _strip_leaked_reasoning("想一想……", "想一想……") == ("", "想一想……")
    assert _strip_leaked_reasoning(" 想 \n", "想") == ("", "想")


def test_strip_think_tags():
    assert _strip_leaked_reasoning("<think>草稿</think>\n正文", "") == ("正文", "草稿")
    assert _strip_leaked_reasoning("<think>没写完", "") == ("", "没写完")
    assert _strip_leaked_reasoning("<think>x", "", has_tool_calls=True) == ("<think>x", "")
    assert _strip_leaked_reasoning("正常", "思考") == ("正常", "思考")


def test_parse_chat_clears_dumped_content():
    data = {"choices": [{"message": {"content": "得用小雪的口吻", "reasoning_content": "得用小雪的口吻"}}]}
    r = Models._parse_chat(data, "m")
    assert r.text == ""
    assert r.reasoning == "得用小雪的口吻"
    assert r.raw_message["content"] == ""


# ---------------- voice ----------------

def test_speak_rules_forbid_persona_tags():
    assert "人设标签" in voice.SPEAK_RULES[1]
    assert "MaiWork 的身份" in voice.SPEAK_RULES[1]


def test_soul_names():
    soul = "我是小雪，昵称：阿雪、Snow。喜欢游戏。"
    names = voice.soul_names(soul)
    assert "小雪" in names and "阿雪" in names and "Snow" in names


def test_leaks_identity():
    p = voice.Persona(soul="我是小雪，昵称：阿雪、Snow。")
    assert p.leaks_identity("小雪觉得这次续作不错")
    assert p.leaks_identity("snow 表示很赞")
    assert not p.leaks_identity("snowboard 也挺好玩")
    assert p.leaks_identity("作为一个游戏玩家，我觉得帧率太低")
    assert p.leaks_identity("我作为鉴赏家必须说两句")
    assert p.leaks_identity("MaiWork 觉得不错")
    assert not p.leaks_identity("作为续作，这次地图小了一圈")
    assert not p.leaks_identity("小雪山新地图挺好看", allow_text="小雪山 DLC")
    assert not p.leaks_identity("")


def test_is_leaked_draft():
    for s in ("不超过50字，挑个具体细节：帧率", "只回开场白", "得保持轻松的口吻",
              "<think>嗯", "冷场了，随口聊一句吧", "控制在 40 字", "用小雪的口吻说"):
        assert voice.is_leaked_draft(s), s
    assert not voice.is_leaked_draft("Win11 更新后帧率掉了一截，回滚能救吗")


# ---------------- skills_web seeding ----------------

def test_default_skill_text_clean():
    for w in _FORBIDDEN:
        assert w not in skills_web.TOPIC_OPENER_DESCRIPTION, w
        assert w not in skills_web.TOPIC_OPENER_DEFAULT_BODY, w
    assert "75" in skills_web.TOPIC_OPENER_DEFAULT_BODY


def test_seed_once_and_respect_delete(tmp_path):
    store = _store(tmp_path)
    skills_web.ensure_seeded_skills(tmp_path, store)
    md = tmp_path / "skills" / "topic-opener" / "SKILL.md"
    assert md.exists()
    assert "topic-opener" in store.kv_get(skills_web.KV_SKILLS_SEEDED)
    skills_web.delete(tmp_path, store, "topic-opener")
    skills_web.ensure_seeded_skills(tmp_path, store)
    assert not md.exists()
    skills_web.ensure_seeded_skills(tmp_path, None)  # 不抛


# ---------------- topics ----------------

def test_opener_cap_and_no_slicing():
    assert _OPENER_MAX_CHARS == 75
    assert Topics._clean_opener("长" * 80) is None
    s = "短句" + " https://a.example/" + "x" * 100
    assert Topics._clean_opener(s) == s.strip()
    assert Topics._clean_opener("a https://a.example/1 b https://b.example/2") == "a https://a.example/1 b"


def test_opener_skill_section(tmp_path):
    t = Topics.__new__(Topics)
    t._skills = None
    assert "topic-opener" in t._opener_skill_section()
    store = _store(tmp_path)
    skills_web.ensure_seeded_skills(tmp_path, store)
    t._skills = Skills(tmp_path, store=store)
    assert "冷场开话题写法" in t._opener_skill_section()
    skills_web.toggle(tmp_path, store, "topic-opener", False)
    t._skills = Skills(tmp_path, store=store)
    assert t._opener_skill_section() == ""


# ---------------- 误伤回归 ----------------

def test_leaked_draft_no_false_positive_on_tone_word():
    assert not voice.is_leaked_draft("官方用教训玩家的口吻发补丁说明，评论区直接炸了")
    assert not voice.is_leaked_draft("这封致歉信完全是甩锅的口吻，谁买账啊")
    for s in ("用小雪的口吻说", "我得是小雪的口吻，玩家", "按轻松的口吻来一句"):
        assert voice.is_leaked_draft(s), s


def test_generic_soul_nouns_not_names():
    soul = "我是玩家，说话简短。我是这群的老熟人。"
    assert not voice.Persona(soul=soul).leaks_identity("老玩家能忍过半小时吗")


# ---------------- 端到端：_build_opener_prompt / check ----------------

import json  # noqa: E402

import pytest  # noqa: E402

from fakes import FakeHost, FakeModelsQueue, SignalsStub  # noqa: E402
from test_topics import GID, SID, _bj_ts, _Jev, _make_topics, _seed_candidate  # noqa: E402

_CFG = {"topics": {"enabled": True, "per_day": 10, "min_gap_hours": 0, "speaker": "maiwork"}}


class _Identity:
    def __init__(self, blocks=None) -> None:
        self.blocks = dict(blocks or {})

    def prompt_block(self, kind, **kw):
        return self.blocks.get(kind, "")


@pytest.mark.asyncio
async def test_prompt_soul_once_and_live_skill_edit_survives_reseed(tmp_path):
    store, settings, topics, *_ = _make_topics(tmp_path / "db")
    topics._identity = _Identity({"soul": "独特标记口吻"})
    cand = {"kind": "news", "title": "T", "brief": "B", "link": ""}
    prompt = await topics._build_opener_prompt(GID, SID, cand, [])
    assert json.dumps(prompt, ensure_ascii=False).count("独特标记口吻") == 1

    sstore = _store(tmp_path)
    skills_web.ensure_seeded_skills(tmp_path, sstore)
    topics._skills = Skills(tmp_path, store=sstore)
    skills_web.update(tmp_path, sstore, "topic-opener", {"body": "自定义破冰规则：一律抛二选一"})
    dumped = json.dumps(await topics._build_opener_prompt(GID, SID, cand, []), ensure_ascii=False)
    assert "自定义破冰规则：一律抛二选一" in dumped
    skills_web.ensure_seeded_skills(tmp_path, sstore)
    topics._skills = Skills(tmp_path, store=sstore)
    dumped = json.dumps(await topics._build_opener_prompt(GID, SID, cand, []), ensure_ascii=False)
    assert "自定义破冰规则：一律抛二选一" in dumped


async def _run_check(tmp_path, reply, identity=None):
    signals = SignalsStub()
    signals.mark(GID, SID, _bj_ts(2026, 9, 27, 14, 0))
    host = FakeHost(msgs=[], session_id=SID)
    store, settings, topics, *_ = _make_topics(
        tmp_path, signals=signals, jev=_Jev(), host=host,
        models=FakeModelsQueue(ready=True, replies=[reply]), cfg=_CFG,
    )
    if identity is not None:
        topics._identity = identity
    now = _bj_ts(2026, 9, 27, 15, 0)
    _seed_candidate(store, check_now=now, title="测试候选")
    out = await topics.check(GID, now)
    if hasattr(host, "send_text_calls"):
        assert host.send_text_calls == []
    rows = store.read().execute("SELECT result FROM topic_log WHERE group_id=?", (GID,)).fetchall()
    queued = store.read().execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    assert queued == 0
    return out, " ".join(r["result"] or "" for r in rows)


@pytest.mark.asyncio
async def test_check_rejects_leaked_draft(tmp_path):
    out, log = await _run_check(
        tmp_path, "冷场之后随口聊一句。不超过75字，不点名，不招揽。我得是小雪的口吻，玩家。挑个具体点：水之神殿")
    assert out == "rejected:leaked_draft"
    assert "思考草稿" in log


@pytest.mark.asyncio
async def test_check_rejects_soul_name_leak(tmp_path):
    ident = _Identity({"soul": "我是 小雪（MaiWork 是它的后台搭档）。性格：你叫阿雪、snowy。"})
    out, _ = await _run_check(tmp_path, "阿雪觉得这次重制版水之神殿肯定没动", ident)
    assert out == "rejected:self_intro"


@pytest.mark.asyncio
async def test_check_rejects_too_long_without_truncating(tmp_path):
    out, log = await _run_check(tmp_path, "这波更新把帧率砍了一半，" * 8)
    assert out == "rejected:too_long"
    assert "75" in log
