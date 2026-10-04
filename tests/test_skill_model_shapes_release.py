"""0.8.0 收口：自学回包（模型输出）的 JSON 类型严格性。

复核发现（`lessons.py`）：`_model_action` 只对 `pass` / `skip` 严格（必须是非空字符串），
其他形状的值直接交给下游 `str(...)`：
- `{"write": {"a": 1}}` → `str(dict)` 把 **Python repr**（`{'a': 1}`）当正文学进 skill；
- `patch` 的 `old` / `new` 用 `str(...)`：`new` 是数字 / 布尔 / 对象就写成 `"3"` / `"{'a': 1}"`；
  `new` 是 `null` / 缺字段又被 `or ""` 当成「删除」；
- `_exec_add_apply` 的 `name` / `description` / `body`、`_exec_merge_apply` 的 `into` / `from` / `body`
  同样 `str(...)`，而且 `description` 超 120 字是**默默截断**（不是拒绝）。

要求（只拒错形状、不新增必填字段；与现有合法输出兼容）：
- 合法回包的 `write` / `body` / `description` / `name` / `into` / `from` 各项以及 `old` / `new`
  必须是正确 JSON 类型（字符串）；dict / array / bool / int / NULL 一律整包不合法；
- 允许 `new=""`（删掉一段），只不许 `old` 为空；
- `description` 现场就限 120 字：超了整包不合法（**不是**截到 120 再写）；正文 ≤4000 超了整包不合法；
- 单个动作 + 未知 metadata 键可以保留；两个动作键混在一起不合法；
- 不合法 → valid=False：不推进 last_reflect / last_curate / votes / topics、不记 skills.change、
  不留 skill 行 / 初始版本行，attempt gate 一小时守住；
- task 的 patch 包字段按真实 code / prompt：`{"patch": {"name": …, "edits": [{"old","new"}…]}}`
  （不是 `skill` 字段）。

用真实 Store + 真实复盘入口（`_run_specialist_kind` / `_run_exec_kind` / `_run_curate` / `run`）
验证，不只是测 helper。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
if str(PLUGIN.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN.parent))

from CharTyr_MaiWork.maiwork import lessons as lessons_mod  # noqa: E402
from test_skills_review_fixes import (  # noqa: E402
    G,
    NOW,
    env,  # noqa: F401  (fixture)
    _Models,
    _run,
    _seed_handoff,
    _seed_task_handoff,
    _state,
)


def _daily(store, models, agents, now=NOW, *, scrub=None, kind="news"):
    return asyncio.run(lessons_mod._run_specialist_kind(
        store, models, agents, G, kind, now, scrub=scrub, recent_topics=None))


def _exec(store, models, agents, now=NOW, *, scrub=None):
    return asyncio.run(lessons_mod._run_exec_kind(store, models, agents, G, now, scrub=scrub))


def _curate(store, models, agents, now=NOW, *, scrub=None):
    return asyncio.run(lessons_mod._run_curate(store, models, agents, G, now, scrub=scrub))


def _vtotal(store) -> int:
    return int(store.read().execute(
        "SELECT COUNT(*) AS c FROM agent_skill_versions").fetchone()["c"])


def _events(store) -> list:
    return store.read().execute(
        "SELECT payload FROM events WHERE kind='skills.change' AND group_id=?", (G,)).fetchall()


def _assert_daily_invalid(store, kind="news", now=NOW):
    st = _state(store, kind)
    assert not st.get("last_reflect"), st
    assert float(st.get("last_attempt") or 0) == now, st
    assert _events(store) == []


# ======================================================================
# 0) `_model_action`：所有形状的容器类型（不只 pass / skip）
# ======================================================================


class TestModelActionContainerTypes:
    def test_pass_skip_must_be_nonempty_string(self):
        for bad in (123, "", [], {}, True, None):
            assert lessons_mod._model_action({"pass": bad}) is None
            assert lessons_mod._model_action({"skip": bad}) is None
        assert lessons_mod._model_action({"pass": "没什么"}) == ("pass", "没什么")

    @pytest.mark.parametrize("bad", [{"a": 1}, [1, 2], 3, 3.5, True, False])
    def test_write_must_be_json_string(self, bad):
        assert lessons_mod._model_action({"write": bad}) is None

    def test_write_string_ok(self):
        assert lessons_mod._model_action({"write": "做法"}) == ("write", "做法")

    @pytest.mark.parametrize("bad", ["甲的活", 3, True, 1.5])
    def test_patch_must_be_list_or_object(self, bad):
        assert lessons_mod._model_action({"patch": bad}) is None

    def test_patch_list_or_dict_ok(self):
        assert lessons_mod._model_action({"patch": []}) == ("patch", [])
        pack = {"name": "甲的活", "edits": []}
        assert lessons_mod._model_action({"patch": pack}) == ("patch", pack)

    @pytest.mark.parametrize("key", ["add", "merge"])
    @pytest.mark.parametrize("bad", ["甲的活", [1], 3, True])
    def test_add_merge_must_be_object(self, key, bad):
        assert lessons_mod._model_action({key: bad}) is None

    def test_single_action_with_unknown_metadata_is_kept(self):
        assert lessons_mod._model_action({"write": "x", "note": "元数据", "confidence": 0.9}) == ("write", "x")

    def test_two_action_keys_still_invalid(self):
        assert lessons_mod._model_action({"pass": "没新东西", "write": "整篇"}) is None
        assert lessons_mod._model_action({"patch": [], "merge": {}}) is None

    def test_null_value_is_not_an_action(self):
        for key in lessons_mod._ACTION_KEYS:
            assert lessons_mod._model_action({key: None}) is None


# ======================================================================
# 1) 专岗每日复盘：write / patch 的字段类型
# ======================================================================


class TestDailyWriteTypes:
    @pytest.mark.parametrize("bad", [{"a": 1}, [1, 2], 123, 12.5, True, None])
    def test_wrong_json_type_is_invalid_no_trace(self, env, bad):
        store, agents = env
        sid = agents.skill_add(G, "news", description="d", body="")
        _seed_handoff(store, "news")
        before = _vtotal(store)
        models = _Models([{"write": bad}])
        assert _daily(store, models, agents) == 0
        assert agents.skill_get(G, sid)["body"] == "", "不许把 Python repr / 数字学进正文"
        _assert_daily_invalid(store)
        assert _vtotal(store) == before
        assert len(models.calls) == 1

    @pytest.mark.parametrize("text", ["123", "12.5", "true", "null", "做法：先核对材料再交接"])
    def test_json_strings_including_quoted_numbers_are_valid(self, env, text):
        store, agents = env
        sid = agents.skill_add(G, "news", description="d", body="")
        _seed_handoff(store, "news")
        models = _Models([{"write": text}])
        assert _daily(store, models, agents) == 1
        assert agents.skill_get(G, sid)["body"] == text
        assert float(_state(store, "news").get("last_reflect") or 0) == NOW


_BAD_EDITS = [
    {"old": "先核对", "new": {"a": 1}},
    {"old": "先核对", "new": [1]},
    {"old": "先核对", "new": 3},
    {"old": "先核对", "new": True},
    {"old": "先核对", "new": None},
    {"old": "先核对"},                      # 缺 new（NULL 形状）
    {"old": {"a": 1}, "new": "改了"},
    {"old": None, "new": "改了"},
    {"old": 7, "new": "改了"},
    {"old": "", "new": "改了"},             # old 不许空
]


class TestDailyPatchTypes:
    @pytest.mark.parametrize("edit", _BAD_EDITS)
    def test_wrong_json_types_are_invalid_no_trace(self, env, edit):
        store, agents = env
        sid = agents.skill_add(G, "news", description="d", body="先核对材料再交接")
        _seed_handoff(store, "news")
        before = _vtotal(store)
        models = _Models([{"patch": [edit]}])
        assert _daily(store, models, agents) == 0
        assert agents.skill_get(G, sid)["body"] == "先核对材料再交接"
        _assert_daily_invalid(store)
        assert _vtotal(store) == before

    def test_empty_new_is_allowed_deletion(self, env):
        store, agents = env
        sid = agents.skill_add(G, "news", description="d", body="保留这句\n删掉这句")
        _seed_handoff(store, "news")
        before = _vtotal(store)
        models = _Models([{"patch": [{"old": "\n删掉这句", "new": ""}]}])
        assert _daily(store, models, agents) == 1
        assert agents.skill_get(G, sid)["body"] == "保留这句"
        assert _vtotal(store) == before + 1        # 旧正文照常留一版
        assert float(_state(store, "news").get("last_reflect") or 0) == NOW

    def test_task_patch_pack_shape_is_not_a_daily_shape(self, env):
        """"专岗每日只收 list 形状的 patch；task 的 {name, edits} 包不算。"""
        store, agents = env
        sid = agents.skill_add(G, "news", description="d", body="先核对材料再交接")
        _seed_handoff(store, "news")
        models = _Models([{"patch": {"name": "news-本群做法",
                                     "edits": [{"old": "先核对材料", "new": "先核对材料再交接"}]}}])
        assert _daily(store, models, agents) == 0
        assert agents.skill_get(G, sid)["body"] == "先核对材料再交接"
        _assert_daily_invalid(store)

    def test_single_action_with_unknown_metadata_still_applies(self, env):
        store, agents = env
        sid = agents.skill_add(G, "news", description="d", body="先核对材料再交接")
        _seed_handoff(store, "news")
        models = _Models([{"patch": [{"old": "先核对材料", "new": "先核对材料再核对"}],
                           "note": "元数据不是动作", "confidence": 0.98}])
        assert _daily(store, models, agents) == 1
        assert "再核对" in agents.skill_get(G, sid)["body"]

    def test_mixed_action_with_wrong_typed_second_key_is_invalid(self, env):
        store, agents = env
        sid = agents.skill_add(G, "news", description="d", body="先核对材料")
        _seed_handoff(store, "news")
        models = _Models([{"pass": "没新东西", "write": {"a": 1}}])
        assert _daily(store, models, agents) == 0
        assert agents.skill_get(G, sid)["body"] == "先核对材料"
        _assert_daily_invalid(store)


def test_daily_invalid_shape_holds_one_hour_attempt_gate(env):
    store, agents = env
    sid = agents.skill_add(G, "news", description="d", body="")
    _seed_handoff(store, "news")
    models = _Models([{"write": {"a": 1}}, {"write": "下小时写的第一版"}])
    assert _daily(store, models, agents, NOW) == 0
    assert len(models.calls) == 1
    assert _daily(store, models, agents, NOW + 600) == 0
    assert len(models.calls) == 1                       # 同一小时内不重试
    assert _daily(store, models, agents, NOW + 3600) == 1
    assert agents.skill_get(G, sid)["body"] == "下小时写的第一版"
    st = _state(store, "news")
    assert float(st.get("last_reflect") or 0) == NOW + 3600
    assert "last_attempt" not in st


# ======================================================================
# 2) 通用执行复盘（task）：patch 包字段 / add / merge
# ======================================================================


class TestExecPatchTypes:
    def test_prompt_contract_field_is_name_not_skill(self, env):
        """真实 prompt 里 task 的 patch 包字段是 `name` + `edits`（按 code / prompt，不凭印象）。"""
        store, agents = env
        a = agents.skill_add(G, "task", name="整理报名", description="A", body="步骤A")
        _seed_task_handoff(store)
        models = _Models([{"patch": {"name": "整理报名",
                                     "edits": [{"old": "步骤A", "new": "步骤A改"}]}}])
        assert _exec(store, models, agents) == 1
        assert "步骤A改" in agents.skill_get(G, a)["body"]
        prompt = models.prompt_of("skills_reflect.task")
        assert '"patch": {"name"' in prompt and '"edits"' in prompt

    def test_wrong_field_skill_is_invalid(self, env):
        store, agents = env
        a = agents.skill_add(G, "task", name="整理报名", description="A", body="步骤A")
        _seed_task_handoff(store)
        models = _Models([{"patch": {"skill": "整理报名",
                                     "edits": [{"old": "步骤A", "new": "步骤A改"}]}}])
        assert _exec(store, models, agents) == 0
        assert agents.skill_get(G, a)["body"] == "步骤A"
        _assert_daily_invalid(store, "task")

    @pytest.mark.parametrize("pack", [
        "整理报名",
        [{"old": "步骤A", "new": "x"}],
        3,
        None,
        {"name": {"a": 1}, "edits": [{"old": "步骤A", "new": "x"}]},
        {"name": 5, "edits": [{"old": "步骤A", "new": "x"}]},
        {"name": "整理报名", "edits": "步骤A"},
        {"name": "整理报名", "edits": [1]},
        {"name": "整理报名", "edits": [{"old": "步骤A", "new": {"a": 1}}]},
        {"name": "整理报名", "edits": [{"old": "步骤A", "new": 9}]},
        {"name": "整理报名", "edits": [{"old": "步骤A", "new": None}]},
        {"name": "整理报名", "edits": [{"old": "步骤A"}]},
        {"name": "整理报名", "edits": [{"old": {"a": 1}, "new": "x"}]},
        {"name": "整理报名", "edits": [{"old": "", "new": "x"}]},
    ])
    def test_wrong_shapes_are_invalid_no_trace(self, env, pack):
        store, agents = env
        a = agents.skill_add(G, "task", name="整理报名", description="A", body="步骤A")
        _seed_task_handoff(store)
        before = _vtotal(store)
        models = _Models([{"patch": pack}])
        assert _exec(store, models, agents) == 0
        assert agents.skill_get(G, a)["body"] == "步骤A"
        _assert_daily_invalid(store, "task")
        assert _vtotal(store) == before

    def test_empty_new_deletion_applies(self, env):
        store, agents = env
        a = agents.skill_add(G, "task", name="整理报名", description="A", body="步骤A\n多余一句")
        _seed_task_handoff(store)
        models = _Models([{"patch": {"name": "整理报名",
                                     "edits": [{"old": "\n多余一句", "new": ""}]}}])
        assert _exec(store, models, agents) == 1
        assert agents.skill_get(G, a)["body"] == "步骤A"

    def test_unknown_metadata_on_single_action_applies(self, env):
        store, agents = env
        a = agents.skill_add(G, "task", name="整理报名", description="A", body="步骤A")
        _seed_task_handoff(store)
        models = _Models([{"patch": {"name": "整理报名",
                                     "edits": [{"old": "步骤A", "new": "步骤A改"}]},
                           "reason": "元数据"}])
        assert _exec(store, models, agents) == 1
        assert "步骤A改" in agents.skill_get(G, a)["body"]


class TestExecAddTypes:
    def test_valid_add_with_quoted_numbers(self, env):
        store, agents = env
        _seed_task_handoff(store)
        models = _Models([{"add": {"name": "整理报名", "description": "120", "body": "步骤1：登记"}}])
        assert _exec(store, models, agents) == 1
        rows = agents.skills(G, "task")
        assert len(rows) == 1 and rows[0]["name"] == "整理报名"
        assert rows[0]["description"] == "120"

    def test_missing_description_is_still_accepted(self, env):
        """description 不是新必填字段：没给就按空串（与现有合法输出兼容）。"""
        store, agents = env
        _seed_task_handoff(store)
        models = _Models([{"add": {"name": "整理报名", "body": "步骤1：登记"}}])
        assert _exec(store, models, agents) == 1
        assert agents.skills(G, "task")[0]["description"] == ""

    def test_description_exactly_120_kept_whole(self, env):
        store, agents = env
        _seed_task_handoff(store)
        desc = "甲" * 120
        models = _Models([{"add": {"name": "整理报名", "description": desc, "body": "步骤1：登记"}}])
        assert _exec(store, models, agents) == 1
        assert agents.skills(G, "task")[0]["description"] == desc

    def test_overlong_description_rejected_not_truncated(self, env):
        store, agents = env
        _seed_task_handoff(store)
        models = _Models([{"add": {"name": "整理报名", "description": "甲" * 121,
                                   "body": "步骤1：登记"}}])
        assert _exec(store, models, agents) == 0
        assert agents.skills(G, "task", include_archived=True) == []
        assert _vtotal(store) == 0
        _assert_daily_invalid(store, "task")

    @pytest.mark.parametrize("add", [
        {"name": {"a": 1}, "description": "d", "body": "步骤"},
        {"name": [1], "description": "d", "body": "步骤"},
        {"name": 5, "description": "d", "body": "步骤"},
        {"name": True, "description": "d", "body": "步骤"},
        {"name": "", "description": "d", "body": "步骤"},
        {"name": "整理报名", "description": 5, "body": "步骤"},
        {"name": "整理报名", "description": {"a": 1}, "body": "步骤"},
        {"name": "整理报名", "description": [1], "body": "步骤"},
        {"name": "整理报名", "description": True, "body": "步骤"},
        {"name": "整理报名", "description": "d", "body": {"a": 1}},
        {"name": "整理报名", "description": "d", "body": 5},
        {"name": "整理报名", "description": "d", "body": ""},
        {"name": "整理报名", "description": "d", "body": "乙" * 4001},
    ])
    def test_wrong_json_types_leave_no_rows_or_versions(self, env, add):
        store, agents = env
        _seed_task_handoff(store)
        before = _vtotal(store)
        models = _Models([{"add": add}])
        assert _exec(store, models, agents) == 0
        assert agents.skills(G, "task", include_archived=True) == []
        assert _vtotal(store) == before
        _assert_daily_invalid(store, "task")


class TestExecMergeTypes:
    def test_valid_merge_with_strings_applies(self, env):
        store, agents = env
        a = agents.skill_add(G, "task", name="整理报名", description="A", body="步骤A")
        b = agents.skill_add(G, "task", name="改日程", description="B", body="步骤B")
        _seed_task_handoff(store)
        models = _Models([{"merge": {"from": ["整理报名", "改日程"], "into": "整理报名",
                                     "body": "步骤A + 步骤B"}}])
        assert _exec(store, models, agents) == 2
        assert "步骤A + 步骤B" in agents.skill_get(G, a)["body"]
        assert agents.skill_get(G, b)["status"] == "archived"

    @pytest.mark.parametrize("merge", [
        "整理报名",
        ["整理报名"],
        3,
        {"from": "整理报名", "into": "整理报名", "body": "x"},
        {"from": ["整理报名", {"a": 1}], "into": "整理报名", "body": "x"},
        {"from": ["整理报名", 5], "into": "整理报名", "body": "x"},
        {"from": ["整理报名", "改日程"], "into": 5, "body": "x"},
        {"from": ["整理报名", "改日程"], "into": {"a": 1}, "body": "x"},
        {"from": ["整理报名", "改日程"], "into": "整理报名", "body": {"a": 1}},
        {"from": ["整理报名", "改日程"], "into": "整理报名", "body": 5},
        {"from": ["整理报名", "改日程"], "into": "整理报名", "body": [1]},
        {"from": ["整理报名", "改日程"], "into": "整理报名", "body": None},
    ])
    def test_wrong_json_types_do_not_merge(self, env, merge):
        store, agents = env
        a = agents.skill_add(G, "task", name="整理报名", description="A", body="步骤A")
        b = agents.skill_add(G, "task", name="改日程", description="B", body="步骤B")
        _seed_task_handoff(store)
        models = _Models([{"merge": merge}])
        assert _exec(store, models, agents) == 0
        assert agents.skill_get(G, a)["body"] == "步骤A"
        assert agents.skill_get(G, b)["body"] == "步骤B"
        assert agents.skill_get(G, b)["status"] == "active"
        _assert_daily_invalid(store, "task")


# ======================================================================
# 3) 每周整理（task）：类型不对不推进 last_curate
# ======================================================================


class TestCurateTypes:
    def test_wrong_body_type_does_not_advance_last_curate(self, env):
        store, agents = env
        a = agents.skill_add(G, "task", name="整理报名", description="A", body="步骤A")
        b = agents.skill_add(G, "task", name="改日程", description="B", body="步骤B")
        models = _Models([{"merge": {"from": ["整理报名", "改日程"], "into": "整理报名",
                                     "body": 123}}])
        assert _curate(store, models, agents) == 0
        assert agents.skill_get(G, a)["body"] == "步骤A"
        assert agents.skill_get(G, b)["status"] == "active"
        st = _state(store, "task")
        assert not st.get("last_curate")
        assert float(st.get("last_curate_attempt") or 0) == NOW
        assert _events(store) == []

    def test_wrong_from_item_type_does_not_advance(self, env):
        store, agents = env
        a = agents.skill_add(G, "task", name="整理报名", description="A", body="步骤A")
        b = agents.skill_add(G, "task", name="改日程", description="B", body="步骤B")
        models = _Models([{"merge": {"from": ["整理报名", {"x": 1}], "into": "整理报名",
                                     "body": "合并"}}])
        assert _curate(store, models, agents) == 0
        assert agents.skill_get(G, a)["body"] == "步骤A"
        assert agents.skill_get(G, b)["status"] == "active"
        assert not _state(store, "task").get("last_curate")

    def test_valid_merge_still_advances_last_curate(self, env):
        store, agents = env
        a = agents.skill_add(G, "task", name="整理报名", description="A", body="步骤A")
        b = agents.skill_add(G, "task", name="改日程", description="B", body="步骤B")
        models = _Models([{"merge": {"from": ["整理报名", "改日程"], "into": "整理报名",
                                     "body": "步骤A + 步骤B"}}])
        assert _curate(store, models, agents) > 0
        st = _state(store, "task")
        assert float(st.get("last_curate") or 0) == NOW
        assert "last_curate_attempt" not in st
        assert agents.skill_get(G, b)["status"] == "archived"
        assert len(_events(store)) == 1


# ======================================================================
# 4) 真实入口 `lessons.run`：整包不合法 → 不留任何痕迹
# ======================================================================


def test_full_run_wrong_type_add_leaves_no_trace(env):
    store, agents = env
    _seed_task_handoff(store)
    models = _Models([{"add": {"name": {"a": 1}, "description": "d", "body": "步骤"}}])
    out = _run(store, models, agents)
    assert out["changes"] == 0
    assert agents.skills(G, "task", include_archived=True) == []
    assert _vtotal(store) == 0
    assert _events(store) == []
    st = _state(store, "task")
    assert not st.get("last_reflect")
    assert float(st.get("last_attempt") or 0) == NOW


def test_full_run_wrong_type_write_leaves_no_trace(env):
    store, agents = env
    sid = agents.skill_add(G, "news", description="d", body="")
    _seed_handoff(store, "news")
    models = _Models([{"write": [1, 2, 3]}])
    out = _run(store, models, agents)
    assert out["changes"] == 0
    assert agents.skill_get(G, sid)["body"] == ""
    assert _events(store) == []
    st = _state(store, "news")
    assert not st.get("last_reflect")
    assert float(st.get("last_attempt") or 0) == NOW


# ======================================================================
# 5) votes / topics 快照：不合法回包一律不推进（真实状态 kv）
# ======================================================================


def _news_item(store, now: float = NOW) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO news_items (batch_id, group_id, title, url_key, keywords, rejected,"
            " up, down, created) VALUES (1, ?, '《条目》', 'k/1', '[]', 0, 0, 3, ?)",
            (G, now - 3600),
        )


def _daily_with_topics(store, models, agents, now: float = NOW):
    return asyncio.run(lessons_mod._run_specialist_kind(
        store, models, agents, G, "news", now, scrub=None,
        recent_topics=["报名表", "日程表"],
    ))


def test_valid_shape_advances_votes_and_topics_snapshot(env):
    """对照面：回包合法时 votes / 话题指纹照常推进（证明上一测不是「本来就没写」）。"""
    store, agents = env
    sid = agents.skill_add(G, "news", description="d", body="先核对材料")
    _news_item(store)
    _seed_handoff(store, "news")
    models = _Models([{"patch": [{"old": "先核对材料", "new": "先核对材料再交接"}]}])
    assert _daily_with_topics(store, models, agents) == 1
    st = _state(store, "news")
    assert float(st.get("last_reflect") or 0) == NOW
    assert isinstance(st.get("votes"), dict)          # 点踩快照推进
    assert st.get("topics_fp") and float(st.get("last_topics") or 0) == NOW


def test_invalid_shape_does_not_advance_votes_or_topics(env):
    store, agents = env
    sid = agents.skill_add(G, "news", description="d", body="先核对材料")
    _news_item(store)
    _seed_handoff(store, "news")
    before = _vtotal(store)
    models = _Models([{"patch": [{"old": "先核对材料", "new": {"a": 1}}]}])
    assert _daily_with_topics(store, models, agents) == 0
    st = _state(store, "news")
    assert not st.get("last_reflect")
    assert float(st.get("last_attempt") or 0) == NOW
    assert "votes" not in st                          # 点踩快照不推进
    assert "topics_fp" not in st and "last_topics" not in st
    assert agents.skill_get(G, sid)["body"] == "先核对材料"
    assert _vtotal(store) == before
    assert _events(store) == []
