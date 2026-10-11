"""构想「先有真实需要，再有构想」（docs/18 §十，2026-10-11 用户拍板）：群向 need 那半。

契约：
- 群里最近三天一句真话都引不到 → 这轮直接不出（连模型都不叫，省一次调用）；
- JSON 要带 evidence（照抄群里一句原话，≥8 字）和三段 basis（pain / value / interest）；
  evidence 对不上本次给的群聊、basis 缺项、basis 还是老式字符串 → 不入库，
  只记进 kv `ideas.blocked.<群号>`（管理员在「构想」页看得到原因）；
- evidence 只进 debug 日志，不落库（群里原话不复制进数据库）；
- 存下来的 basis 是三行文字「痛点：…／值得做：…／大家感兴趣：…」；
- 堆积闸收紧：没处理掉的攒到 2 条就不再出新的。
"""

from __future__ import annotations

import json

from CharTyr_MaiWork.maiwork.feeds import _IDEAS_PILE_LIMIT

from fakes import FakeModelsQueue, idea_reply, seed_idea_chat
from test_feeds import GID, _make_feeds, _run, _TimePatch

NOW = 1_790_000_000.0


def _models(reply: str) -> FakeModelsQueue:
    return FakeModelsQueue(ready=True, replies=[reply])


def _feeds_with_chat(tmp_path, reply: str):
    models = _models(reply)
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path, models=models)
    seed_idea_chat(store, GID, now=NOW)
    return store, feeds, models, workers, topics


# ----------------------------------------------------------------------
# 提示词：先有需要、三条都占、evidence / basis / kind
# ----------------------------------------------------------------------


class TestPrompt:
    def _prompt(self, tmp_path) -> str:
        store, feeds, models, *_r = _feeds_with_chat(tmp_path, idea_reply())
        with _TimePatch():
            assert isinstance(_run(feeds.make_idea(GID)), int)
        return models.calls[0][1][-1]["content"]

    def test_need_first_and_no_capability_first(self, tmp_path) -> None:
        prompt = self._prompt(tmp_path)
        assert "先有群里真实的需要" in prompt
        assert "别先想「我能产出什么」" in prompt
        # 老「三关」段（能力清单当门槛）换掉了：能力清单只约束 items
        assert "只约束 items" in prompt

    def test_three_part_basis_is_mandatory_in_prompt(self, tmp_path) -> None:
        prompt = self._prompt(tmp_path)
        for word in ("痛点", "值得做", "感兴趣", "evidence"):
            assert word in prompt, word
        assert "缺一条就给 null" in prompt

    def test_writing_style_and_drafts_in_prompt(self, tmp_path) -> None:
        prompt = self._prompt(tmp_path)
        assert "别默认「整理一份速查 / 对照表 / 清单」" in prompt
        assert "不要用「我可以」「帮你」开头" in prompt
        assert "drafts" in prompt and "宁缺毋滥" in prompt

    def test_json_carries_evidence_basis_and_kinds(self, tmp_path) -> None:
        prompt = self._prompt(tmp_path)
        assert '"evidence"' in prompt and '"basis"' in prompt and '"kind"' in prompt
        assert "help" in prompt and "talk" in prompt
        assert "最多 5 个" in prompt  # items 上限照旧


# ----------------------------------------------------------------------
# evidence：群里真说过才算
# ----------------------------------------------------------------------


class TestEvidence:
    def test_no_chat_lines_returns_none_without_model_call(self, tmp_path) -> None:
        """群里最近三天没发言 → 这轮不出，且不叫模型。"""
        models = _models(idea_reply())
        store, settings, feeds, *_r = _make_feeds(tmp_path, models=models)
        with _TimePatch():
            assert _run(feeds.make_idea(GID)) is None
        assert models.calls == []
        assert store.read().execute("SELECT COUNT(*) c FROM ideas").fetchone()["c"] == 0

    def test_evidence_not_in_chat_is_blocked(self, tmp_path) -> None:
        store, feeds, models, _w, topics = _feeds_with_chat(
            tmp_path, idea_reply(evidence="群里有人说过想修手柄这事吧")
        )
        with _TimePatch():
            assert _run(feeds.make_idea(GID)) is None
        assert store.read().execute("SELECT COUNT(*) c FROM ideas").fetchone()["c"] == 0
        blocked = store.kv_get(f"ideas.blocked.{GID}", [])
        assert len(blocked) == 1
        assert blocked[0]["kind"] == "group"
        assert blocked[0]["title"] == "把手柄救活"
        assert blocked[0]["reason"] == "没引用到群里真说过的话"
        assert topics.calls == []

    def test_empty_evidence_is_blocked(self, tmp_path) -> None:
        store, feeds, *_r = _feeds_with_chat(tmp_path, idea_reply(evidence=""))
        with _TimePatch():
            assert _run(feeds.make_idea(GID)) is None
        assert len(store.kv_get(f"ideas.blocked.{GID}", [])) == 1

    def test_evidence_too_short_is_blocked(self, tmp_path) -> None:
        """少于 8 个字的引用不算（和 goals.chat_evidence_match 同一口径）。"""
        store, feeds, *_r = _feeds_with_chat(tmp_path, idea_reply(evidence="手柄"))
        with _TimePatch():
            assert _run(feeds.make_idea(GID)) is None
        blocked = store.kv_get(f"ideas.blocked.{GID}", [])
        assert len(blocked) == 1 and blocked[0]["reason"] == "没引用到群里真说过的话"

    def test_evidence_with_extra_spaces_matches(self, tmp_path) -> None:
        """比对去空白：模型抄原话时多打了空格也算引用到了。"""
        store, feeds, *_r = _feeds_with_chat(
            tmp_path, idea_reply(evidence="这 手柄 放半年了，还能 救活吗")
        )
        with _TimePatch():
            assert isinstance(_run(feeds.make_idea(GID)), int)

    def test_evidence_not_stored_in_the_row(self, tmp_path) -> None:
        """群里原话只进日志，不落库（隐私）。"""
        store, feeds, *_r = _feeds_with_chat(tmp_path, idea_reply())
        with _TimePatch():
            got = _run(feeds.make_idea(GID))
        row = store.read().execute("SELECT * FROM ideas WHERE id=?", (got,)).fetchone()
        for key in row.keys():
            assert "救活吗" not in str(row[key] or ""), key


# ----------------------------------------------------------------------
# 三段 basis
# ----------------------------------------------------------------------


class TestBasis:
    def test_missing_part_is_blocked(self, tmp_path) -> None:
        basis = {"pain": "手柄坏了就扔", "value": "省钱"}  # 少「感兴趣」
        store, feeds, *_r = _feeds_with_chat(tmp_path, idea_reply(basis=basis))
        with _TimePatch():
            assert _run(feeds.make_idea(GID)) is None
        assert store.read().execute("SELECT COUNT(*) c FROM ideas").fetchone()["c"] == 0
        blocked = store.kv_get(f"ideas.blocked.{GID}", [])
        assert len(blocked) == 1 and blocked[0]["reason"] == "痛点 / 值得做 / 感兴趣 没说全"

    def test_blank_part_is_blocked(self, tmp_path) -> None:
        basis = {"pain": "手柄坏了就扔", "value": "  ", "interest": "在折腾手柄"}
        store, feeds, *_r = _feeds_with_chat(tmp_path, idea_reply(basis=basis))
        with _TimePatch():
            assert _run(feeds.make_idea(GID)) is None
        assert len(store.kv_get(f"ideas.blocked.{GID}", [])) == 1

    def test_legacy_string_basis_is_blocked(self, tmp_path) -> None:
        """老式字符串 basis 不认（提示词只给新格式）：当没写全处理，不入库。"""
        store, feeds, *_r = _feeds_with_chat(tmp_path, idea_reply(basis="群里在聊手柄"))
        with _TimePatch():
            assert _run(feeds.make_idea(GID)) is None
        blocked = store.kv_get(f"ideas.blocked.{GID}", [])
        assert len(blocked) == 1 and blocked[0]["reason"] == "痛点 / 值得做 / 感兴趣 没说全"

    def test_basis_stored_as_three_lines(self, tmp_path) -> None:
        store, feeds, *_r = _feeds_with_chat(tmp_path, idea_reply())
        with _TimePatch():
            got = _run(feeds.make_idea(GID))
        row = store.read().execute("SELECT basis FROM ideas WHERE id=?", (got,)).fetchone()
        assert row["basis"] == (
            "痛点：手柄坏了就扔，没人知道还能不能修\n"
            "值得做：修好能省下买新的钱\n"
            "大家感兴趣：群里一直在折腾手柄和板子"
        )


# ----------------------------------------------------------------------
# 堆积闸收紧到 2
# ----------------------------------------------------------------------


class TestPileLimit:
    def test_pile_limit_is_two(self) -> None:
        assert _IDEAS_PILE_LIMIT == 2

    def test_two_unhandled_ideas_stop_the_round(self, tmp_path) -> None:
        from test_feeds import _seed_idea

        models = _models(idea_reply())
        store, settings, feeds, *_r = _make_feeds(tmp_path, models=models)
        seed_idea_chat(store, GID, now=NOW)
        _seed_idea(store, GID, title="甲", created=NOW - 100)
        _seed_idea(store, GID, title="乙", created=NOW - 200)
        with _TimePatch():
            assert _run(feeds.make_idea(GID)) is None
        assert models.calls == []


# ----------------------------------------------------------------------
# 个人向：没有群聊可引，就要求接上他自己说过的事（origin）
# ----------------------------------------------------------------------


class TestPersonalNeed:
    def _reply(self, idea: dict) -> str:
        return json.dumps(
            {
                "focus": [{"query": "FPGA", "why": "在做"}],
                "idea": idea,
            },
            ensure_ascii=False,
        )

    def _idea(self, **over) -> dict:
        idea = {
            "kind": "help",
            "title": "把这块板的例程理一下",
            "body": "理成能照着跑的一页",
            "step": "先列出要跑的例程",
            "effort": "半天",
            "origin": "FPGA 小板子",
            "basis": {"pain": "例程散着找不到", "value": "少走弯路", "interest": "他在做板子"},
            "items": [{"kind": "task", "title": "理一下例程", "desc": ""}],
            "feasibility": {
                "level": "ok", "note": "能做：整理成文档", "uses": ["write"],
                "deliver": "doc", "needs_members": False,
            },
        }
        idea.update(over)
        return idea

    def _run_personal(self, tmp_path, idea: dict):
        from test_personal import (
            GID as PGID,
            UID,
            _make_personal,
            _personal_scores_json,
            _time_patch,
        )
        from test_personal import _run as _prun

        models = FakeModelsQueue(
            ready=True,
            replies=[self._reply(idea), _personal_scores_json(), json.dumps({"posts": []}, ensure_ascii=False)],
        )
        store, settings, personal, models, _w, _t, _p = _make_personal(tmp_path, models=models)
        with _time_patch():
            _prun(personal.prepare_personal(PGID, UID))
        return store, PGID, UID

    def test_missing_origin_is_blocked(self, tmp_path) -> None:
        """个人向没有群聊可核 evidence：接不上他自己说过的事就不入库。"""
        store, gid, _uid = self._run_personal(tmp_path, self._idea(origin=""))
        assert store.read().execute("SELECT COUNT(*) c FROM ideas").fetchone()["c"] == 0
        blocked = store.kv_get(f"ideas.blocked.{gid}", [])
        assert len(blocked) == 1
        assert blocked[0]["kind"] == "personal"
        assert blocked[0]["reason"] == "没接上他自己说过的事"

    def test_missing_basis_part_is_blocked(self, tmp_path) -> None:
        store, gid, _uid = self._run_personal(
            tmp_path, self._idea(basis={"pain": "例程散着找不到", "value": "少走弯路"})
        )
        assert store.read().execute("SELECT COUNT(*) c FROM ideas").fetchone()["c"] == 0
        blocked = store.kv_get(f"ideas.blocked.{gid}", [])
        assert len(blocked) == 1 and blocked[0]["reason"] == "痛点 / 值得做 / 感兴趣 没说全"

    def test_basis_stored_as_three_lines(self, tmp_path) -> None:
        store, _gid, uid = self._run_personal(tmp_path, self._idea())
        row = store.read().execute(
            "SELECT basis, origin FROM ideas WHERE target_user_id=?", (uid,)
        ).fetchone()
        assert row["basis"].startswith("痛点：例程散着找不到\n值得做：少走弯路\n大家感兴趣：")
        assert row["origin"] == "FPGA 小板子"
