"""专岗「主模型」(main) + 每岗模型选择字段（model / effort / backup）测试（改版 1a）。

阶段 1a：
- agents.KINDS 保持四个跑得动的岗位（news/idea/goal/task）——main 不跑交接单。
- profiles() 多一个内置 kind "main"（主模型）：配模型用，没有记忆 / 交接单。
- 每个岗位（含 main）多三个字段：model（[[model_list]] 的 id）、effort（该模型支持的
  思考强度之一）、backup（备用模型 id）；空串 = 没选。
- 写路径严格校验（模型 id 必须存在于当前设置的模型库、effort 必须落在所选模型的
  efforts 里、backup 不能和 model 相同）；读路径容错（库里存坏了按没选处理）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.agents import KINDS, Agents
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"


def _settings_with_models():
    settings, _ = load_settings(
        {
            "groups": {"serve": [{"group": f"qq:{G1}"}]},
            "endpoints": [{"id": "default", "base_url": "https://api.test/v1", "api_key": "sk-x"}],
            "model_list": [
                {"id": "m1", "endpoint": "default", "model": "gpt-main", "efforts": ["low", "high"]},
                {"id": "m2", "endpoint": "default", "model": "gpt-bak", "efforts": []},
                {"id": "w1", "endpoint": "default", "model": "gpt-worker"},
            ],
        }
    )
    return settings


@pytest.fixture
def agents(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite3")
    store.migrate()
    settings = _settings_with_models()
    a = Agents(store, lambda: settings)
    yield a, store
    store.close()


class TestMainKindExists:
    def test_kinds_unchanged_no_main(self) -> None:
        """跑交接单的岗位清单不含 main（main 不是专岗执行者）。"""
        assert list(KINDS) == ["news", "idea", "goal", "task"]

    def test_profiles_main_first(self, agents) -> None:
        a, _ = agents
        kinds = [p["kind"] for p in a.profiles()]
        assert kinds == ["main", "news", "idea", "goal", "task"]

    def test_main_defaults(self, agents) -> None:
        a, _ = agents
        p = a.profile("main")
        assert p["title"] == "主模型"
        assert p["model"] == ""
        assert p["effort"] == ""
        assert p["backup"] == ""
        assert p["fish_seed"] == ""
        # main 不跑交接单：不裁剪任务工具 / 技能名单（与 task 同为 None）
        assert p["tools"] is None
        assert p["skills"] is None

    def test_all_profiles_have_model_fields(self, agents) -> None:
        a, _ = agents
        for p in a.profiles():
            assert p["model"] == "" and p["effort"] == "" and p["backup"] == ""


class TestMainNotRunnable:
    def test_begin_main_rejected(self, agents) -> None:
        a, _ = agents
        with pytest.raises(ValueError, match="交接单"):
            a.begin(G1, "main", "brief")

    def test_memory_main_rejected(self, agents) -> None:
        a, _ = agents
        mem = a.memory(G1, "main")
        # 「每群三份」收尾：memory() 不再返回 notes；main 是不落经验的，learned 也空
        assert "notes" not in mem
        assert mem["learned"] == []

    def test_remember_main_no_persist(self, agents) -> None:
        a, _ = agents
        a.remember(G1, "main", "xx")
        assert a.memory(G1, "main")["learned"] == []

    def test_prompt_main_is_plain(self, agents) -> None:
        a, _ = agents
        text = a.prompt(G1, "main")
        assert "主模型" in text

    def test_update_profile_main_ok(self, agents) -> None:
        a, _ = agents
        p = a.update_profile("main", {"title": "主脑", "fish_seed": "koi-main"})
        assert p["title"] == "主脑"
        assert p["fish_seed"] == "koi-main"

    def test_unknown_kind_still_rejected(self, agents) -> None:
        a, _ = agents
        with pytest.raises(ValueError):
            a.profile("ghost")
        with pytest.raises(ValueError):
            a.update_profile("ghost", {"title": "x"})


class TestModelFieldsValidation:
    def test_model_must_exist(self, agents) -> None:
        a, _ = agents
        with pytest.raises(ValueError, match="模型库"):
            a.update_profile("main", {"model": "ghost"})
        p = a.update_profile("main", {"model": "m1"})
        assert p["model"] == "m1"

    def test_model_type_strict(self, agents) -> None:
        a, _ = agents
        with pytest.raises(ValueError):
            a.update_profile("main", {"model": 123})
        with pytest.raises(ValueError):
            a.update_profile("main", {"backup": ["m1"]})

    def test_effort_must_be_in_model_efforts(self, agents) -> None:
        a, _ = agents
        # m1 支持 low/high
        with pytest.raises(ValueError, match="思考强度"):
            a.update_profile("main", {"model": "m1", "effort": "max"})
        p = a.update_profile("main", {"model": "m1", "effort": "high"})
        assert p["effort"] == "high"
        # m2 不支持任何强度
        with pytest.raises(ValueError, match="思考强度"):
            a.update_profile("main", {"model": "m2", "effort": "low"})

    def test_effort_without_model_rejected(self, agents) -> None:
        a, _ = agents
        with pytest.raises(ValueError):
            a.update_profile("worker" if False else "task", {"effort": "low"})

    def test_effort_unknown_value(self, agents) -> None:
        a, _ = agents
        with pytest.raises(ValueError):
            a.update_profile("main", {"model": "m1", "effort": "turbo"})

    def test_backup_differs_from_model(self, agents) -> None:
        a, _ = agents
        with pytest.raises(ValueError, match="备用"):
            a.update_profile("main", {"model": "m1", "backup": "m1"})
        p = a.update_profile("main", {"model": "m1", "backup": "m2"})
        assert p["backup"] == "m2"

    def test_backup_must_exist(self, agents) -> None:
        a, _ = agents
        with pytest.raises(ValueError, match="模型库"):
            a.update_profile("main", {"model": "m1", "backup": "ghost"})

    def test_switch_model_resets_incompatible_effort(self, agents) -> None:
        a, _ = agents
        a.update_profile("main", {"model": "m1", "effort": "high"})
        # 换到不认识的 effort 的模型：同一次 patch 里换 → 400；只换 model 时 effort 不该默默报废
        with pytest.raises(ValueError, match="思考强度"):
            a.update_profile("main", {"model": "m2"})
        # 明确清掉 effort 再换就行
        p = a.update_profile("main", {"model": "m2", "effort": ""})
        assert p["model"] == "m2" and p["effort"] == ""

    def test_non_main_kinds_also_take_model(self, agents) -> None:
        a, _ = agents
        p = a.update_profile("task", {"model": "w1", "backup": "m2"})
        assert p["model"] == "w1" and p["backup"] == "m2"

    def test_clear_fields_with_empty(self, agents) -> None:
        a, _ = agents
        a.update_profile("main", {"model": "m1", "effort": "low", "backup": "m2"})
        p = a.update_profile("main", {"model": "", "effort": "", "backup": ""})
        assert p["model"] == "" and p["effort"] == "" and p["backup"] == ""


class TestReadToleratesBadStoredValues:
    def test_bad_stored_values_fall_back_empty(self, agents) -> None:
        a, store = agents
        with store.tx() as conn:
            store.kv_set(conn, "agents.profiles", {
                "main": {"model": "ghost", "effort": "turbo", "backup": "also-ghost"},
                "task": {"model": 123, "effort": True, "backup": None},
            })
        p_main = a.profile("main")
        assert p_main["model"] == "" and p_main["effort"] == "" and p_main["backup"] == ""
        p_task = a.profile("task")
        assert p_task["model"] == "" and p_task["effort"] == "" and p_task["backup"] == ""

    def test_stored_effort_not_in_model_efforts_cleared(self, agents) -> None:
        a, store = agents
        with store.tx() as conn:
            store.kv_set(conn, "agents.profiles", {
                "main": {"model": "m2", "effort": "low"},  # m2 不支持任何强度
            })
        p = a.profile("main")
        assert p["model"] == "m2" and p["effort"] == ""

    def test_stored_backup_equal_model_cleared(self, agents) -> None:
        a, store = agents
        with store.tx() as conn:
            store.kv_set(conn, "agents.profiles", {
                "main": {"model": "m1", "backup": "m1"},
            })
        p = a.profile("main")
        assert p["model"] == "m1" and p["backup"] == ""


class TestMainNotInSpecialists:
    def test_specialists_rejects_main(self) -> None:
        """specialists.run("main", …) 早拒：主模型不是能跑交接单的专岗（只挂模型选择）。
        （2026-10 改版 3/4：专岗名单不再钉死在 specialists._VALID_KINDS——agents._kind_known
        才是唯一真源；main 这条闸挪进了 specialists.run 入口。）"""
        import asyncio
        from pathlib import Path
        import tempfile

        from CharTyr_MaiWork.maiwork.agents import Agents
        from CharTyr_MaiWork.maiwork.config import load_settings
        from CharTyr_MaiWork.maiwork.specialists import Specialists
        from CharTyr_MaiWork.maiwork.store import Store

        tmp = Path(tempfile.mkdtemp())
        store = Store(tmp / "t.db")
        store.migrate()
        settings, _ = load_settings({"groups": {"serve": [{"group": "qq:111"}]}})
        agents = Agents(store, lambda: settings)
        spec = Specialists(agents, workers=None, skills=None)
        report = asyncio.new_event_loop().run_until_complete(
            spec.run("main", "brief", group_id="111")
        )
        assert report.ok is False
        assert "主模型" in (report.error or "")


class TestEscalateField:
    """任务双岗协作（docs/20 §5.3）：每个干活岗位的「做不动时换用」模型 escalate。

    空串 = 没选 = 用主模型的链（用户 2026-10-05 定）；主模型岗没有这个选项。
    """

    def test_default_empty(self, agents) -> None:
        a, _ = agents
        for p in a.profiles():
            assert p["escalate"] == ""

    def test_set_and_clear(self, agents) -> None:
        a, _ = agents
        p = a.update_profile("task", {"model": "w1", "escalate": "m2"})
        assert p["escalate"] == "m2"
        p = a.update_profile("task", {"escalate": ""})
        assert p["escalate"] == ""

    def test_must_exist(self, agents) -> None:
        a, _ = agents
        with pytest.raises(ValueError, match="模型库"):
            a.update_profile("task", {"escalate": "ghost"})

    def test_type_strict(self, agents) -> None:
        a, _ = agents
        with pytest.raises(ValueError):
            a.update_profile("task", {"escalate": 3})

    def test_main_rejects_escalate(self, agents) -> None:
        a, _ = agents
        with pytest.raises(ValueError, match="主模型"):
            a.update_profile("main", {"escalate": "m2"})

    def test_custom_kind_takes_escalate(self, agents) -> None:
        a, _ = agents
        kind = a.create_custom("制图师")["kind"]
        assert a.profile(kind)["escalate"] == ""
        assert a.update_profile(kind, {"escalate": "m1"})["escalate"] == "m1"

    def test_stored_bad_value_cleared(self, agents) -> None:
        a, store = agents
        with store.tx() as conn:
            store.kv_set(conn, "agents.profiles", {"task": {"model": "w1", "escalate": "ghost"},
                                                   "main": {"escalate": "m1"}})
        assert a.profile("task")["escalate"] == ""
        assert a.profile("main")["escalate"] == ""
