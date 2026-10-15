"""可选依赖的接线：能力清单（onboarding）、管理员 env_overview、内置 skill 文案。

用户 2026-10-15 定：办公文档三库以「可选安装」方式提供，**不进 _manifest.json 的必装
依赖**。所以三处要如实说清楚「装了 / 没装（去网页一键装）」：
- `onboarding.items()` 的办公文档一项（首次引导完成页 / 设置页的能力清单）；
- 管理员对话的 `env_overview`（现在这套 MaiWork 能做到什么）；
- 内置 skill `office-docs` 的「先查环境」段（没装就回落 Markdown / CSV / HTML）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_admin_env_tools import app  # noqa: F401  （复用真 app 夹具）
from test_onboarding_web import _login, _start  # noqa: F401
from test_tools_admin_real import _call

READY_MODELS = {"base_url": "https://ep.test/v1", "api_key": "sk-t", "main": "m", "worker": "w"}


@pytest.mark.asyncio
async def test_onboarding_has_office_item(tmp_path: Path) -> None:
    app_, client = await _start(tmp_path, READY_MODELS)
    try:
        await _login(client)
        d = await (await client.get("/api/onboarding")).json()
        keys = [i["key"] for i in d["items"]]
        assert "office" in keys, keys
        by = {i["key"]: i for i in d["items"]}
        item = by["office"]
        assert item["state"] in ("ok", "warn", "off", "wait")
        # 还没查过（网页「设置 → 工具」打开时才会探测）→ 不许瞎说「装了」
        assert "办公" in item["title"]
        assert "网页" in item["text"]
    finally:
        await client.close()
        await app_.stop()


@pytest.mark.asyncio
async def test_onboarding_office_item_reports_installed(tmp_path: Path) -> None:
    """探测过、装好了 → 说清楚装了什么，并带上 LibreOffice 有没有。"""
    import json
    import types

    app_, client = await _start(tmp_path, READY_MODELS)
    try:
        deps = app_.optional_deps
        assert deps is not None

        class _Env:
            async def run(self, name, command, *, timeout_s=None, memory=None, cwd=""):
                pkgs = {
                    "python-docx": {"installed": True, "version": "1.1.2"},
                    "openpyxl": {"installed": True, "version": "3.1.5"},
                    "python-pptx": {"installed": True, "version": "0.6.23"},
                }
                body = json.dumps({"python": "/usr/bin/python3", "version": "3.12.3", "packages": pkgs})
                return types.SimpleNamespace(
                    exit_code=0, stdout="MAIWORK_DEPS_JSON=" + body + "\n/usr/bin/soffice\n",
                    stderr="", ms=1, timed_out=False, oom=False,
                )

        deps._env = _Env()
        await deps.refresh()
        await _login(client)
        d = await (await client.get("/api/onboarding")).json()
        item = {i["key"]: i for i in d["items"]}["office"]
        assert item["state"] == "ok"
        assert "python-docx" in item["text"] and "openpyxl" in item["text"] and "python-pptx" in item["text"]
        assert "LibreOffice" in item["text"]
    finally:
        await client.close()
        await app_.stop()


@pytest.mark.asyncio
async def test_env_overview_reports_office_libs(app) -> None:  # noqa: F811
    r = await _call(app, "env_overview", {})
    assert r.ok, r.error
    assert "办公文档" in r.output
    keys = [i["key"] for i in (r.data or {}).get("items", [])]
    assert "office" in keys, keys


def test_office_skill_tells_how_to_install() -> None:
    """内置 skill 要写清楚：没装时去哪一键装、装不了回落什么形态。"""
    text = (
        Path(__file__).resolve().parents[1] / "maiwork" / "builtin_skills" / "office-docs" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "一键装" in text
    assert "设置" in text and "工具" in text
    # 回落形态必须写明，不然子 agent 会硬写脚本白烧一轮
    assert "Markdown" in text and "CSV" in text and "HTML" in text
    # 三个库和 LibreOffice 仍然要点名（skill 正文的既有要求）
    for token in ("python-docx", "openpyxl", "python-pptx", "soffice"):
        assert token in text
