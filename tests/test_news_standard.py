"""资讯标准 = 内置 skill `skills/news-standard/`（Agent Skills 格式）。

- 格式按 agentskills.io/specification：SKILL.md 有 YAML front matter，name 与目录同名、
  小写字母数字和单横线、≤64；description 1–1024 字且说清做什么 + 什么时候用；
  正文 <500 行；引用文件相对路径、只一层深且都存在。
- 资讯流水线四个环节从这里取文字（news_standard.for_*），文中写的数字和 feeds.py 的常量一致。
- skills 列表里有它（内置、只读、worker + main 都能读）；网页改 / 删 / 同名新建都拒。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import feeds as feeds_mod
from CharTyr_MaiWork.maiwork import news_standard
from CharTyr_MaiWork.maiwork.skills import Skills, parse_front_matter

SKILL = news_standard.SKILL_DIR


def _front_raw(text: str) -> list[str]:
    lines = text.splitlines()
    assert lines[0] == "---"
    end = lines.index("---", 1)
    return lines[1:end]


class TestSpecFormat:
    def test_skill_md_frontmatter_follows_spec(self) -> None:
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        front = parse_front_matter(text)
        name = front.get("name")
        assert name == SKILL.name == "news-standard"
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name) and len(name) <= 64
        desc = str(front.get("description") or "")
        assert 1 <= len(desc) <= 1024
        assert "使用" in desc  # 说清什么时候用
        # 规范外的顶层键只允许规范列出的这些（MaiWork 自己的放 metadata 里）
        top_keys = {ln.split(":", 1)[0] for ln in _front_raw(text) if ln and not ln.startswith((" ", "\t", "#"))}
        assert top_keys <= {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}

    def test_body_short_and_references_one_level(self) -> None:
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        assert len(text.splitlines()) < 500
        refs = set(re.findall(r"\]\((references/[^)]+)\)", text))
        assert refs == {"references/finding.md", "references/criteria.md", "references/scoring.md"}
        for r in refs:
            assert (SKILL / r).is_file(), r
            assert r.count("/") == 1

    def test_metadata_roles_parsed(self) -> None:
        front = parse_front_matter((SKILL / "SKILL.md").read_text(encoding="utf-8"))
        assert front.get("metadata", {}).get("maiwork-roles") == "worker"


class TestNumbersMatchCode:
    """skill 里写的数字 = 程序硬判的常量（改一边忘了另一边会红）。"""

    def test_numbers(self) -> None:
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        crit = (SKILL / "references" / "criteria.md").read_text(encoding="utf-8")
        scoring = (SKILL / "references" / "scoring.md").read_text(encoding="utf-8")
        assert f"超过 {feeds_mod._NEWS_MAX_AGE_DAYS} 天" in skill
        assert f"最近 {feeds_mod._NEWS_MAX_AGE_DAYS} 天" in crit
        assert f"{feeds_mod.GUIDE_MAX_AGE_DAYS} 天内" in skill and f"{feeds_mod.GUIDE_MAX_AGE_DAYS} 天内" in crit
        assert f"文章相关度 ≥{feeds_mod._GUIDE_MIN_RELEVANCE:g}" in skill
        assert f"信息量 ≥{feeds_mod._GUIDE_MIN_INFO:g}" in skill
        assert f"平均 ≥{feeds_mod._GUIDE_MIN_AVG:g}" in skill
        assert f"每轮最多 {feeds_mod._GUIDE_ROUND_CAP} 篇" in skill
        assert f"相关度 {feeds_mod._EXPLORE_MIN_RELEVANCE:g} 的资讯" in skill
        assert f"值得聊 ≥{feeds_mod._EXPLORE_MIN_CHAT:g} 或意外度 ≥{feeds_mod._EXPLORE_MIN_SURPRISE:g}" in skill
        assert f"信息量 ≥{feeds_mod._EXPLORE_MIN_INFO:g}" in skill
        assert f"信息量、意外度都要 ≥{feeds_mod._GUIDE_EXPLORE_MIN_INFO:g}" in skill
        assert feeds_mod._GUIDE_EXPLORE_MIN_INFO == feeds_mod._GUIDE_EXPLORE_MIN_SURPRISE
        assert f"新鲜感 ≤{feeds_mod._NOVELTY_REJECT_MAX:g}" in skill
        assert f"群友大概已经知道的事 ≤{feeds_mod._NOVELTY_REJECT_MAX:g}" in scoring
        assert f"超过 {feeds_mod._NEWS_MAX_AGE_DAYS} 天的一律 2 分以下" in scoring


class TestSections:
    def test_for_focus(self) -> None:
        t = news_standard.for_focus()
        assert "## 定关注点" in t and "## 跳一步" in t and "## 找候选" not in t

    def test_for_collect_with_and_without_guides(self) -> None:
        t = news_standard.for_collect(True)
        assert "## 资讯" in t and "## 文章" in t and "不算文章" in t and "## 找候选" in t
        t2 = news_standard.for_collect(False)
        assert "## 文章" not in t2 and "## 资讯" in t2

    def test_for_scoring(self) -> None:
        t = news_standard.for_scoring()
        assert "## 相关度 relevance" in t and "## 文章" in t and "## 意外度 surprise" in t

    def test_missing_section_returns_empty(self) -> None:
        assert news_standard.section("finding", "没有这一节") == ""


class TestInjectedIntoPrompts:
    def test_focus_prompt_uses_standard(self, tmp_path) -> None:
        from fakes import FakeModelsQueue
        from test_feeds_freshness import _FOCUS_JSON, _TimePatch, _make_feeds, _run, GID

        _s, settings, feeds, models, *_r = _make_feeds(tmp_path, models=FakeModelsQueue(ready=True, replies=[_FOCUS_JSON]))
        with _TimePatch():
            _run(feeds._plan_focus(GID, settings))
        prompt = str(models.calls[0][1][-1]["content"])
        assert news_standard.section("finding", "跳一步") in prompt

    def test_collect_brief_uses_standard(self, tmp_path) -> None:
        from fakes import FakeModelsQueue
        from test_feeds_freshness import (_FOCUS_JSON, FakeWorkers, _TimePatch, _cand, _make_feeds,
                                          _ok_report, _post, _posts_json, _run, _score, _scores_json, GID)

        _s, settings, feeds, models, workers, *_r = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, _scores_json(_score(0)), _posts_json(_post(0, "量子芯片全新架构发布"))]),
            workers=FakeWorkers(_ok_report({"items": [_cand(0)]})),
        )
        with _TimePatch():
            _run(feeds.prepare_news(GID))
        # 两阶段恒生效：文章/资讯的分寸在核验子 agent 的 brief 里；撒网（2026-10-01 起
        # 代码按计划搜）不再派子 agent——「怎么找」那套在定关注点的提示词里。
        by_mark = lambda p: "\n".join(
            str(c["brief"]) for c in workers.calls if str(c.get("task_id") or "").startswith(p))
        verify_briefs = by_mark("feeds-verify:")
        assert by_mark("feeds-discover:") == ""  # 撒网不再派子 agent
        assert news_standard.section("criteria", "文章") in verify_briefs
        assert news_standard.section("finding", "找候选") in verify_briefs
        focus_prompt = str(models.calls[0][1][0]["content"])
        assert news_standard.section("finding", "跳一步") in focus_prompt

    def test_score_prompt_uses_standard(self, tmp_path) -> None:
        from fakes import FakeModelsQueue
        from test_feeds_freshness import _TimePatch, _cand, _make_feeds, _run, _score, _scores_json, GID

        _s, settings, feeds, models, *_r = _make_feeds(tmp_path, models=FakeModelsQueue(ready=True, replies=[_scores_json(_score(0))]))
        with _TimePatch():
            _run(feeds._score(GID, settings, [_cand(0)]))
        prompt = str(models.calls[0][1][0]["content"])
        assert news_standard.section("scoring", "相关度 relevance") in prompt
        assert news_standard.section("criteria", "同一件事") in prompt


class TestBuiltinSkillListed:
    def test_listed_readonly_for_both_roles(self, tmp_path) -> None:
        sk = Skills(tmp_path)
        items = {i["name"]: i for i in sk.list()}
        assert items["news-standard"]["builtin"] is True
        # 只给子 agent：给主模型会让排计划回合每次都带 skill 工具
        assert items["news-standard"]["roles"] == ["worker"]
        assert "news-standard" in sk.hint("worker") and "news-standard" not in sk.hint("main")
        assert sk.read("news-standard").startswith("---")
        assert "## 相关度 relevance" in sk.read_file("news-standard", "references/scoring.md")

    def test_data_dir_cannot_shadow_builtin(self, tmp_path) -> None:
        d = tmp_path / "skills" / "news-standard"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: news-standard\ndescription: 冒充的\n---\n假的\n", encoding="utf-8")
        sk = Skills(tmp_path)
        assert [i for i in sk.list() if i["name"] == "news-standard"][0]["description"] != "冒充的"
        assert "假的" not in sk.read("news-standard")

    def test_web_layer(self, tmp_path) -> None:
        from CharTyr_MaiWork.maiwork import skills_web
        from CharTyr_MaiWork.maiwork.store import Store

        store = Store(tmp_path / "t.db")
        store.migrate()
        view = {i["name"]: i for i in skills_web.list_view(tmp_path, store)}
        assert view["news-standard"]["source"] == "builtin"
        got = skills_web.get_view(tmp_path, store, "news-standard")
        assert got and got["source"] == "builtin" and "references/scoring.md" in got["files"]
        with pytest.raises(PermissionError):
            skills_web.update(tmp_path, store, "news-standard", {"body": "改掉"})
        with pytest.raises(PermissionError):
            skills_web.delete(tmp_path, store, "news-standard")
        with pytest.raises(FileExistsError):
            skills_web.create(tmp_path, store, {"name": "news-standard", "description": "x", "body": "y"})
        store.close()
