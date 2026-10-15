"""交付报告网页工具 render_report（2026-10-15 intelligent UI）：零件规格 → 统一样式网页，
走 write_file 同一套范围检查；规格不合格就拒，不许写 HTML。"""

from __future__ import annotations

import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.environments.local import LocalEnv
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import ToolContext, Tools
from CharTyr_MaiWork.maiwork.tools_exec import register_exec_tools

from fakes import FakeHost

SPEC = {
    "title": "三款笔记软件对比",
    "caption": "Obsidian 最适合本群",
    "blocks": [
        {"type": "heading", "text": "结论"},
        {"type": "text", "text": "按价格和协作看，推荐 Obsidian。"},
        {"type": "table", "columns": ["软件", "价格"], "rows": [["Obsidian", "免费"], ["Notion", "$10/月"]]},
        {"type": "note", "text": "Notion 团队版价格官网未写明，标为不确定。"},
        {"type": "sources", "items": [{"title": "Obsidian 官网", "url": "https://obsidian.md/pricing"}]},
    ],
}


@pytest.fixture
def setup(tmp_path):
    s, _ = load_settings({"environments": {"local_mode": "direct", "workspace_root": str(tmp_path / "ws-root")}})
    env = LocalEnv(lambda: s)
    store = Store(tmp_path / "t.db")
    store.migrate()
    tools = Tools(store)
    register_exec_tools(tools, env=env, host=FakeHost(), get_settings=lambda: s)
    ws = env.workspace("ws-1")
    ctx = ToolContext(group_id="g1", task_id="T-1", actor="子 agent #1", role="worker", workspace=ws,
                      artifact_scope=("artifacts/T-1",))
    yield tools, ctx, ws
    store.close()


@pytest.mark.asyncio
async def test_render_report_writes_styled_page(setup) -> None:
    tools, ctx, ws = setup
    r = await tools.call("render_report", {"path": "artifacts/T-1/report.html", **SPEC, "foot": "资料截至 2026-10-15"}, ctx)
    assert r.ok, r.error
    doc = (ws / "artifacts/T-1/report.html").read_text(encoding="utf-8")
    assert "<h1 class=\"rp-h\">三款笔记软件对比</h1>" in doc
    assert "vs-tbl" in doc and "https://obsidian.md/pricing" in doc
    assert "<script" not in doc.lower() and "default-src 'none'" in doc
    assert "资料截至 2026-10-15" in doc


@pytest.mark.asyncio
async def test_render_report_rejects_bad_spec_and_path(setup) -> None:
    tools, ctx, ws = setup
    r = await tools.call("render_report", {"path": "artifacts/T-1/r.md", **SPEC}, ctx)
    assert not r.ok and ".html" in r.error
    r = await tools.call("render_report", {"path": "artifacts/T-1/r.html", "title": "x",
                                           "blocks": [{"type": "html", "html": "<b>x</b>"}]}, ctx)
    assert not r.ok and "零件清单" in r.error
    assert not (ws / "artifacts/T-1/r.html").exists()


@pytest.mark.asyncio
async def test_render_report_respects_artifact_scope(setup) -> None:
    tools, ctx, _ws = setup
    r = await tools.call("render_report", {"path": "artifacts/T-2/report.html", **SPEC}, ctx)
    assert not r.ok


def test_render_report_is_worker_tool_with_catalog(setup) -> None:
    tools, _ctx, _ws = setup
    specs = {s["function"]["name"]: s["function"] for s in tools.specs("worker")}
    assert "render_report" in specs
    assert "sources" in specs["render_report"]["description"]
    assert "render_report" not in {s["function"]["name"] for s in tools.specs("main")}
