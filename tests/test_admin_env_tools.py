"""管理员对话配环境（2026-10-15 用户定：引导里鼓励「先和 MaiWork 聊把环境配好」，主模型要有这个能力）：
- env_overview：看现在能做到什么、缺的怎么补（密钥只让管理员自己在网页填）；
- skill_install：从网址装 skill，必须先管理员确认、确认后才下载；只认公网 https。"""

from __future__ import annotations

import pytest

from CharTyr_MaiWork.maiwork import tools_admin
from CharTyr_MaiWork.maiwork.skills import Skills

from test_tools_admin_real import _call, _call_confirmed, app  # noqa: F401  （fixture 复用）

MD = "---\nname: demo-remote\ndescription: 测试用的远程 skill\n---\n# 正文\n照做。\n"


def test_source_url_mapping() -> None:
    f = tools_admin.skill_source_url
    assert f("https://github.com/o/r/tree/main/skills/foo") == (
        "https://raw.githubusercontent.com/o/r/main/skills/foo/SKILL.md", "md")
    assert f("https://github.com/o/r/blob/main/skills/foo/SKILL.md") == (
        "https://raw.githubusercontent.com/o/r/main/skills/foo/SKILL.md", "md")
    assert f("https://a.com/x.zip?dl=1") == ("https://a.com/x.zip?dl=1", "zip")
    for bad in ("http://a.com/x.zip", "https://skills.sh/o/r/foo", "file:///etc/passwd", "https://a.com/x zip.zip"):
        with pytest.raises(ValueError):
            f(bad)


@pytest.mark.asyncio
async def test_fetch_public_refuses_internal() -> None:
    for url in ("https://127.0.0.1/x.zip", "https://localhost/a/SKILL.md", "https://10.0.0.8/x.zip"):
        with pytest.raises(ValueError):
            await tools_admin.fetch_public(url)


@pytest.mark.asyncio
async def test_skill_install_needs_confirm_then_downloads(app, monkeypatch) -> None:  # noqa: F811
    fetched: list[str] = []

    async def fake_fetch(url, max_bytes=tools_admin.SKILL_FETCH_MAX):
        fetched.append(url)
        return MD.encode("utf-8")

    monkeypatch.setattr(tools_admin, "fetch_public", fake_fetch)
    url = "https://github.com/o/r/tree/main/skills/demo-remote"
    from CharTyr_MaiWork.maiwork.admin_chat import AdminChat

    gate = app.admin_pending
    gate.bind_chat(int(AdminChat(app).create("900000001")["id"]), 0)
    first = await _call(app, "skill_install", {"url": url})
    assert first.ok and "已请求管理员确认" in first.output
    assert fetched == []  # 没点头前不下载
    r, _t = await _call_confirmed(app, "skill_install", {"url": url})
    assert r.ok, r.error
    assert fetched[-1] == "https://raw.githubusercontent.com/o/r/main/skills/demo-remote/SKILL.md"
    assert Skills(app.get_settings().data_dir).read("demo-remote") is not None


@pytest.mark.asyncio
async def test_skill_install_rejects_bad_url_without_ticket(app) -> None:  # noqa: F811
    r = await _call(app, "skill_install", {"url": "http://a.com/x.zip"})
    assert not r.ok and "https" in r.error


@pytest.mark.asyncio
async def test_env_overview_lists_abilities_and_key_rule(app) -> None:  # noqa: F811
    r = await _call(app, "env_overview", {})
    assert r.ok, r.error
    assert "模型能力" in r.output and "联网搜索" in r.output
    assert "research-report" in r.output  # 自带 skill 列出来
    assert "密钥请自己在网页上填" in r.output
    # 不能把内部工具名 / 参数名 / Markdown 星号写给管理员看
    for bad in ("set_rules", "skill_install", "**"):
        assert bad not in r.output, bad


def test_admin_prompt_guides_env_setup(app) -> None:  # noqa: F811
    from CharTyr_MaiWork.maiwork.admin_chat import AdminChat

    p = AdminChat(app)._system_prompt({"group_id": ""})
    assert "env_overview" in p and "skill_install" in p and "不要在对话里发密钥" in p
