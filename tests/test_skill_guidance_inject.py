"""main 角色的内置 skill 在纯 JSON 回合里也生效（2026-10-15）：目标检查注入 goal-tracking、
构想生成注入 idea-shaping；停用了就不注入。"""

from __future__ import annotations

from CharTyr_MaiWork.maiwork import skills as skills_mod


class _Reg:
    def __init__(self, text):
        self.text = text

    def read(self, name):
        return self.text


def test_guidance_strips_front_matter_and_titles() -> None:
    g = skills_mod.guidance(_Reg("---\nname: x\n---\n# 做法\n照做"), "x")
    assert g.startswith("【做法参考（内置 skill x") and "照做" in g and "name: x" not in g


def test_guidance_empty_when_disabled_and_reads_builtin_without_registry() -> None:
    assert skills_mod.guidance(_Reg(None), "goal-tracking") == ""
    for name in ("goal-tracking", "idea-shaping"):
        g = skills_mod.guidance(None, name)
        assert name in g and len(g) > 200


def test_guidance_truncates() -> None:
    g = skills_mod.guidance(_Reg("a" * 9000), "x", max_chars=100)
    assert g.endswith("…（后略）")


def test_wired_into_goal_and_idea_prompts() -> None:
    from pathlib import Path

    root = Path(skills_mod.__file__).parent
    assert '_skill_guidance(getattr(self._tools, "skill_registry", None), "goal-tracking")' in (
        root / "coordinator.py").read_text(encoding="utf-8")
    assert '_skill_guidance(getattr(self, "_skills", None), "idea-shaping")' in (
        root / "feeds.py").read_text(encoding="utf-8")
