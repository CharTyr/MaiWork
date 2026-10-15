"""内置 skill research-report（2026-10-15）：调研类交付的标准做法，指向 render_report 和同一份零件清单。"""

from __future__ import annotations

import re
from pathlib import Path

from CharTyr_MaiWork.maiwork import viz_spec
from CharTyr_MaiWork.maiwork.skills import Skills

SKILL = Path(viz_spec.__file__).parent / "builtin_skills" / "research-report" / "SKILL.md"


def test_research_report_is_builtin_worker_skill(tmp_path) -> None:
    sk = Skills(str(tmp_path))
    items = {i["name"]: i for i in sk.list()}
    assert items["research-report"]["builtin"] is True
    assert items["research-report"]["roles"] == ["worker"]
    assert "research-report" in sk.hint("worker")
    body = sk.read("research-report")
    for must in ("render_report", "[不确定]", "独立来源", "一句话结论", "fetch_page"):
        assert must in body


def test_component_names_in_skill_exist_in_catalog() -> None:
    """skill 里点名的零件都必须是报告用途里真有的（改零件清单忘了改 skill 会红）。"""
    text = SKILL.read_text(encoding="utf-8")
    table = text[text.index("| 段落 | 用哪个零件 |"):text.index("规矩：")]
    named = set(re.findall(r"`([a-z]+)`", table)) - {"caption"}  # caption 是报告字段，不是零件
    report = {k for k, v in viz_spec.CATALOG.items() if "report" in v["profiles"]}
    assert named and named <= report, named - report


def test_research_framework_points_to_skill() -> None:
    from CharTyr_MaiWork.maiwork.workers import RESEARCH_REPORT_FRAMEWORK

    assert "research-report" in RESEARCH_REPORT_FRAMEWORK and "render_report" in RESEARCH_REPORT_FRAMEWORK
