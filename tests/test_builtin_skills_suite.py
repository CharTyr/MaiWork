"""内置 skill 套件：资讯核实 / 办公文档 / 通用任务 / 目标追踪 / 构想处理（2026-10-15）。

背景：MaiWork 以「首次引导后开箱即用」为目标，把四类场景的标准化做法做成随插件发布的
内置 skill（只读、网页只能看、数据目录同名不列不读）。这五个是：

| skill | roles | 来源 | 许可 |
|---|---|---|---|
| `news-verify` | worker | 自研（方法论参考 SerhiiKorniienko/bullshit-detector，MIT） | AGPL-3.0-or-later |
| `office-docs` | worker | 自研（python-docx / openpyxl / python-pptx / LibreOffice） | AGPL-3.0-or-later |
| `general-task` | worker | 自研（口径对齐 coordinator.py） | AGPL-3.0-or-later |
| `goal-tracking` | main | 改编自 openai/skills `.curated/define-goal` | Apache-2.0 |
| `idea-shaping` | main | 改编自 obra/superpowers `skills/brainstorming` | MIT |

**绝不内置** anthropics/skills 的 `docx` / `xlsx` / `pptx` / `pdf`：那四份是
source-available 的专有许可，明文禁复制 / 禁衍生 / 禁分发。`office-docs` 是自研同类。

这份测试守五件事：

1. 格式照 Agent Skills 规范（同 test_news_standard：front matter / name 与目录同名 /
   description 说清什么时候用 / 正文 <500 行 / 引用只一层深且都存在）；
2. roles 正确（worker 的只给子 agent，main 的只给主模型，坏角色不算）；
3. 改编来的 skill 必须带来源与许可证（目录里有 LICENSE + NOTICE，NOTICE 写清来源仓库、
   路径、许可证、改了什么）；
4. 正文（含 references/ 与 NOTICE）里提到的工具名必须真在注册表里——不许写不存在的工具
   （例如白名单里那个从来没注册过的 `search_chat`）；
5. 正文里写的数字 / 文件名必须和代码常量一致（改一边忘了另一边会红）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
if str(PLUGIN.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN.parent))

from CharTyr_MaiWork.maiwork import skills as skills_mod  # noqa: E402
from CharTyr_MaiWork.maiwork import skills_web  # noqa: E402
from CharTyr_MaiWork.maiwork.skills import Skills, parse_front_matter  # noqa: E402
from CharTyr_MaiWork.maiwork.store import Store  # noqa: E402

# 新内置 skill → 期望的 roles 与 license（front matter 里的 license 字段）。
NEW_SKILLS: dict[str, dict[str, object]] = {
    "news-verify": {"roles": ["worker"], "license": "AGPL-3.0-or-later"},
    "office-docs": {"roles": ["worker"], "license": "AGPL-3.0-or-later"},
    "general-task": {"roles": ["worker"], "license": "AGPL-3.0-or-later"},
    "goal-tracking": {"roles": ["main"], "license": "Apache-2.0"},
    "idea-shaping": {"roles": ["main"], "license": "MIT"},
}

# 改编件：目录里必须有许可证全文 + NOTICE（来源仓库 / 路径 / 许可证 / 改动说明）。
ADAPTED: dict[str, dict[str, str]] = {
    "goal-tracking": {
        "repo": "openai/skills",
        "path": "skills/.curated/define-goal/SKILL.md",
        "license_file": "LICENSE.txt",
        "license_mark": "Apache License",
        "upstream_license": "Apache-2.0",
    },
    "idea-shaping": {
        "repo": "obra/superpowers",
        "path": "skills/brainstorming/SKILL.md",
        "license_file": "LICENSE",
        "license_mark": "MIT License",
        "upstream_license": "MIT",
    },
}

# 顶层只许出现规范列出的键（MaiWork 自己的放 metadata 里，同 test_news_standard）。
_SPEC_TOP_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}

# 反引号里「纯小写蛇形」的记号才当工具名看（带 / . - < > 的一律不是工具名）。
_TOOL_TOKEN_RE = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`")

# 例外：反引号里是蛇形、但**不是**工具的名字（写清为什么，别偷偷加）。
# 只有「正文要照抄给模型的 JSON 字段名」这一类——工具名一律不许进这里。
_NON_TOOL_TOKENS: dict[str, str] = {
    "task_id": "目标打勾交回里的 JSON 字段（指向下级任务）",
    "needs_members": "构想可行性交回里的 JSON 字段（要不要群友参与）",
    "deliver_kind": "计划 JSON 的交付形态字段（text / view / file）",
}

SKILL_ROOT = skills_mod.BUILTIN_ROOT


def _text_of(name: str) -> str:
    return (SKILL_ROOT / name / "SKILL.md").read_text(encoding="utf-8")


def _front_lines(text: str) -> list[str]:
    lines = text.splitlines()
    assert lines[0] == "---"
    return lines[1:lines.index("---", 1)]


def _front_value(text: str, key: str) -> str:
    """顶层 key 的原样值。

    注意：`skills.parse_front_matter` 只认 name / description / roles（规范里的 license 等
    字段按设计被忽略），所以 license 要在这里自己从原文里读。
    """
    for ln in _front_lines(text):
        if ln.startswith((" ", "\t")) or ":" not in ln:
            continue
        k, _, v = ln.partition(":")
        if k.strip() == key:
            return v.strip().strip("'\"")
    return ""


def _all_builtin_markdown() -> list[tuple[str, str]]:
    """内置 skill 目录下所有 .md（含 references/ 与 NOTICE）→ [(相对路径, 正文)]。"""
    out: list[tuple[str, str]] = []
    for p in sorted(SKILL_ROOT.rglob("*.md")):
        out.append((str(p.relative_to(SKILL_ROOT)), p.read_text(encoding="utf-8")))
    return out


def _registered_tool_names(tmp_path: Path) -> set[str]:
    """真注册一遍拿到全部工具名（不调 handler，只注册）。

    register_* 的 handler 是闭包，注册时不需要真的 env / host / search，给桩就行。
    """
    from CharTyr_MaiWork.maiwork import tools_builtin, tools_exec, tools_groupspace
    from CharTyr_MaiWork.maiwork import tools_railway, tools_ssh
    from CharTyr_MaiWork.maiwork.skills_tools import register_skill_tools
    from CharTyr_MaiWork.maiwork.tools import Tools

    tools = Tools(None)  # type: ignore[arg-type]  注册不碰库
    tools_builtin.register_builtin(tools, search=None, profiles=None)
    tools_exec.register_exec_tools(
        tools, env=None, host=None, get_settings=lambda: None  # type: ignore[arg-type]
    )
    tools_railway.register_vm_tools(
        tools, get_box=lambda: None, env=None, local_env=object()  # type: ignore[arg-type]
    )
    tools_ssh.register_machine_tools(tools, get_box=lambda _tid: None, env=None)  # type: ignore[arg-type]
    tools_groupspace.register_groupspace_tools(
        tools, None, announce=_async_none, get_settings=lambda: None  # type: ignore[arg-type]
    )
    register_skill_tools(tools, Skills(str(tmp_path)))
    return {str(s["function"]["name"]) for s in tools.specs("worker")} | {
        str(s["function"]["name"]) for s in tools.specs("main")
    }


async def _async_none(*_a, **_kw) -> None:  # pragma: no cover - 只给注册用
    return None


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


# ----------------------------------------------------------------------
# 1. 格式：Agent Skills 规范
# ----------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(NEW_SKILLS))
class TestSpecFormat:
    def test_front_matter_follows_spec(self, name: str) -> None:
        text = _text_of(name)
        front = parse_front_matter(text)
        assert front.get("name") == name
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name) and len(name) <= 64
        desc = str(front.get("description") or "")
        assert 1 <= len(desc) <= 1024
        assert "使用" in desc  # 说清什么时候用
        raw = text.splitlines()
        assert raw[0] == "---"
        top_keys = {
            ln.split(":", 1)[0]
            for ln in _front_lines(text)
            if ln and not ln.startswith((" ", "\t", "#"))
        }
        assert top_keys <= _SPEC_TOP_KEYS

    def test_body_short_and_references_shallow(self, name: str) -> None:
        text = _text_of(name)
        assert len(text.splitlines()) < 500
        refs = set(re.findall(r"\]\((references/[^)]+)\)", text))
        for r in refs:
            assert (SKILL_ROOT / name / r).is_file(), r
            assert r.count("/") == 1, r

    def test_metadata_builtin_and_license(self, name: str) -> None:
        text = _text_of(name)
        front = parse_front_matter(text)
        meta = front.get("metadata") or {}
        assert meta.get("maiwork-roles") == " ".join(NEW_SKILLS[name]["roles"])  # type: ignore[arg-type]
        assert meta.get("maiwork-builtin") == "true"
        assert _front_value(text, "license") == NEW_SKILLS[name]["license"]


# ----------------------------------------------------------------------
# 2. 加载与角色门控
# ----------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(NEW_SKILLS))
def test_skills_loads_and_gates_by_role(name: str, tmp_path: Path) -> None:
    sk = Skills(str(tmp_path))
    items = {i["name"]: i for i in sk.list()}
    assert name in items
    assert items[name]["builtin"] is True
    assert items[name]["roles"] == NEW_SKILLS[name]["roles"]
    assert sk.roles(name) == NEW_SKILLS[name]["roles"]
    assert sk.read(name).startswith("---")
    for role in ("worker", "main"):
        listed = name in [i["name"] for i in sk.list(role)]
        assert listed is (role in NEW_SKILLS[name]["roles"])  # type: ignore[operator]
        assert (name in sk.hint(role)) is (role in NEW_SKILLS[name]["roles"])  # type: ignore[operator]


def test_data_dir_cannot_shadow_new_builtins(tmp_path: Path) -> None:
    for name in NEW_SKILLS:
        d = tmp_path / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: SHADOW-DESC\n---\nSHADOW-BODY\n", encoding="utf-8"
        )
    sk = Skills(str(tmp_path))
    for name in NEW_SKILLS:
        assert [i for i in sk.list() if i["name"] == name][0]["description"] != "SHADOW-DESC"
        assert "SHADOW-BODY" not in sk.read(name)


def test_web_layer_sees_them_as_readonly_builtin(store: Store, tmp_path: Path) -> None:
    view = {i["name"]: i for i in skills_web.list_view(tmp_path, store)}
    for name in NEW_SKILLS:
        assert view[name]["source"] == "builtin"
        got = skills_web.get_view(tmp_path, store, name)
        assert got and got["source"] == "builtin"
        with pytest.raises(PermissionError):
            skills_web.update(tmp_path, store, name, {"body": "改掉"})
        with pytest.raises(PermissionError):
            skills_web.delete(tmp_path, store, name)
        with pytest.raises(FileExistsError):
            skills_web.create(tmp_path, store, {"name": name, "description": "x", "body": "y"})


def test_worker_hint_lists_new_worker_skills(tmp_path: Path) -> None:
    """子 agent 的 system 提示清单（hint("worker")，最多 20 条）要带上 worker 那三个。"""
    hint = Skills(str(tmp_path)).hint("worker")
    for name in ("news-verify", "office-docs", "general-task"):
        assert name in hint


# ----------------------------------------------------------------------
# 3. 改编件：来源 + 许可证
# ----------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ADAPTED))
def test_adapted_skill_carries_license_and_notice(name: str) -> None:
    info = ADAPTED[name]
    d = SKILL_ROOT / name
    lic = d / info["license_file"]
    notice = d / "NOTICE.md"
    assert lic.is_file(), f"{name} 缺许可证全文 {info['license_file']}"
    assert notice.is_file(), f"{name} 缺 NOTICE.md"
    lic_text = lic.read_text(encoding="utf-8")
    assert info["license_mark"] in lic_text
    if info["upstream_license"] == "MIT":
        assert "Permission is hereby granted" in lic_text
    else:
        assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in lic_text
    n_text = notice.read_text(encoding="utf-8")
    assert info["repo"] in n_text            # 来源仓库
    assert info["path"] in n_text            # 上游路径
    assert info["upstream_license"] in n_text  # 上游许可证
    assert "改动" in n_text                   # 改了什么


@pytest.mark.parametrize("name", sorted(ADAPTED))
def test_adapted_skill_body_states_source(name: str) -> None:
    """正文里也要写清来源（网页只能看正文，NOTICE 是附件）。"""
    text = _text_of(name)
    assert ADAPTED[name]["repo"] in text
    assert ADAPTED[name]["upstream_license"] in text
    assert "改编" in text


@pytest.mark.parametrize("name", sorted(set(NEW_SKILLS) - set(ADAPTED)))
def test_self_developed_skills_use_project_license(name: str) -> None:
    """自研件跟 MaiWork 本体的许可走（AGPL-3.0-or-later，和 news-standard 一致）。"""
    assert NEW_SKILLS[name]["license"] == "AGPL-3.0-or-later"


def test_no_proprietary_anthropics_office_skill_copied() -> None:
    """红线：anthropics 的 docx / xlsx / pptx / pdf 是专有许可，不许内置或改写。

    目录名不许撞；正文里不许出现它们专有的做法标记（`docx` npm 库 / recalc.py /
    pptxgenjs / ooxml 这些是那四份的实现特征）。
    """
    assert not (SKILL_ROOT / "docx").exists()
    assert not (SKILL_ROOT / "xlsx").exists()
    assert not (SKILL_ROOT / "pptx").exists()
    assert not (SKILL_ROOT / "pdf").exists()
    text = _text_of("office-docs")
    for marker in ("pptxgenjs", "recalc.py", "docx-js", "npm install docx", "ooxml"):
        assert marker not in text, f"office-docs 里出现了上游专有实现特征：{marker}"


# ----------------------------------------------------------------------
# 4. 正文提到的工具名必须真在注册表里
# ----------------------------------------------------------------------


def test_referenced_tool_names_exist_in_registry(tmp_path: Path) -> None:
    registered = _registered_tool_names(tmp_path)
    assert {"web_search", "fetch_page", "read_file", "write_file", "submit_result"} <= registered
    bad: list[str] = []
    for rel, text in _all_builtin_markdown():
        for token in sorted(set(_TOOL_TOKEN_RE.findall(text))):
            if token in registered or token in _NON_TOOL_TOKENS:
                continue
            bad.append(f"{rel}: {token}")
    assert not bad, "内置 skill 提到了不存在的工具（或忘了登记非工具例外）：" + ", ".join(bad)


# ----------------------------------------------------------------------
# 5. 正文里的数字 / 文件名 = 代码常量
# ----------------------------------------------------------------------


class TestGeneralTaskMatchesCoordinator:
    def test_delivery_note_and_reply_artifacts(self) -> None:
        from CharTyr_MaiWork.maiwork import coordinator as coord

        text = _text_of("general-task")
        assert f"≤{coord._NOTE_MAX} 字" in text
        assert coord.TEXT_REPLY_NAME in text          # text 活的完整回复写 reply.md
        assert str(coord.TEXT_REPLY_MAX) in text      # 超了当群文件发
        assert "先给结论" in text or "第一句给结论" in text
        assert "内部说法" in text
        assert "submit_result" in text

    def test_brief_scale_and_audience_note(self) -> None:
        from CharTyr_MaiWork.maiwork import coordinator as coord
        from CharTyr_MaiWork.maiwork import requirements as req
        from CharTyr_MaiWork.maiwork import workers as workers_mod

        text = _text_of("general-task")
        assert coord.SCALE_BRIEF in text
        assert req.BRIEF_SCALE_TEXT in text
        # 六段调研框架是 workers.py 里的常量，skill 里要给出同名六段
        framework = workers_mod.RESEARCH_REPORT_FRAMEWORK
        for seg in ("一句话结论", "大家都同意的", "有分歧的", "吐槽最多的", "新冒头的", "没人提的"):
            assert seg in framework and seg in text
        assert "多源" in text and "单源" in text


class TestGoalTrackingMatchesGoals:
    def test_criteria_and_evidence_rules(self) -> None:
        from CharTyr_MaiWork.maiwork import goal_fit
        from CharTyr_MaiWork.maiwork import goals
        from CharTyr_MaiWork.maiwork import coordinator as coord

        text = _text_of("goal-tracking")
        assert f"连续 {goals.GOAL_EVIDENCE_MIN} 个字" in text
        assert "3–5 条" in text
        assert f"≤{coord._CRITERIA_MAX} 条" in text or f"最多 {coord._CRITERIA_MAX} 条" in text
        assert goal_fit.FIT_ONLINE in text and goal_fit.FIT_HUMAN in text
        assert "task_id" in text
        # 只靠计划 / 方案 / 网页 / 说明书不算
        assert "说明书" in text

    def test_adapted_from_define_goal(self) -> None:
        text = _text_of("goal-tracking")
        assert "define-goal" in text
        assert "可度量" in text or "可衡量" in text


class TestIdeaShapingMatchesIdeaPipeline:
    def test_kinds_and_gates(self) -> None:
        from CharTyr_MaiWork.maiwork import feeds
        from CharTyr_MaiWork.maiwork import idea_guard
        from CharTyr_MaiWork.maiwork import idea_feasibility

        text = _text_of("idea-shaping")
        assert feeds.IDEA_KIND_HELP in text and feeds.IDEA_KIND_TALK in text
        assert "由头" in text and "evidence" in text
        for seg in ("痛点", "值得做", "感兴趣"):
            assert seg in text
        assert str(feeds._IDEAS_PILE_LIMIT) in text          # 堆积闸
        assert str(idea_guard.PERSONAL_GROUP_CAP) in text     # 个人向每群上限
        assert "可行性" in text
        assert "needs_members" in text                        # 可行性闸的关键字段
        assert str(feeds._IDEA_ITEMS_MAX) in text or "items" in text
        assert idea_feasibility.__name__.endswith("idea_feasibility")  # 模块真在

    def test_adapted_from_brainstorming(self) -> None:
        text = _text_of("idea-shaping")
        assert "brainstorming" in text
        assert "先问" in text or "问清" in text


class TestNewsVerifyComplementsNewsStandard:
    def test_points_to_news_standard_and_does_not_restate_criteria(self) -> None:
        text = _text_of("news-verify")
        assert "news-standard" in text          # 分工，不重复
        assert "独立来源" in text                # 按独立来源数算，不按链接数
        assert "最强反驳" in text                # steelman 先于否定
        assert "不确定" in text
        # 收录 / 打分的硬数字只写在 news-standard 一处，不许在这里再抄一遍
        assert "新鲜感 ≤" not in text
        assert "相关度 ≥" not in text
        assert "60 天内" not in text

    def test_news_specialist_can_read_it(self, tmp_path: Path) -> None:
        """资讯岗位的默认 skill 名单要带上它（否则专岗 read_skill 拿不到）。"""
        from CharTyr_MaiWork.maiwork.agents import Agents

        s = Store(tmp_path / "a.db")
        s.migrate()
        try:
            agents = Agents(s, lambda: None)
            skills = agents.profile("news")["skills"]
            assert "news-verify" in skills
            assert "news-standard" in skills
        finally:
            s.close()


class TestOfficeDocsEnvironmentFirst:
    def test_says_check_environment_before_writing(self) -> None:
        text = _text_of("office-docs")
        for lib in ("python-docx", "openpyxl", "python-pptx"):
            assert lib in text
        assert "soffice" in text or "LibreOffice" in text
        assert "run_command" in text          # 先查环境
        assert "自检" in text
        assert "占位" in text or "待补充" in text
