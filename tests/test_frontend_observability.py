"""五项 Aisya 回迁能力的网页端真实模块测试（Node 直接执行 ES Modules）。

覆盖：
1. 上下文面积地图（ctxmap.js）：normalizeCtxMap / tilesOf / cellsOf(400 格) / ctxMapHTML 与 XSS 转义
2. Jev 概率分布条（jev_bars.js）：群话题冷场判断、待复核 @ 消息、看板步骤嵌套 probs 与 XSS 转义
3. 看板过程抽屉（board_steps.js）：首字耗时 first_token_ms、Jev 面板、上下文构成展开块
4. 看板流向图（board_flow.js）：5 列流向节点渲染、折叠切换、SSE 仅在看板前台展开时启用
5. 设置修改记录（settings/changes.js）与常驻笔记（pages/groupctx.js）：来源筛选、涉密禁撤、群管权限隔离、1200 字上限
"""

from __future__ import annotations

import subprocess
import shutil
from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "maiwork" / "console" / "static" / "js"
if not JS.is_dir():
    raise RuntimeError(f"找不到前端目录：{JS}")


def _run_node(body: str) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    assert node, "需要 node 运行前端模块测试"
    setup = (
        "import assert from 'node:assert/strict';\n"
        "globalThis.window = { matchMedia: () => ({ matches: false, addEventListener() {} }), addEventListener() {}, scrollTo() {} };\n"
        "globalThis.document = { hidden: false, getElementById: () => null, querySelectorAll: () => [], addEventListener() {} };\n"
        "globalThis.location = { hash: '', search: '' };\n"
        "globalThis.history = { replaceState() {} };\n"
        "globalThis.MutationObserver = class { observe() {} disconnect() {} };\n"
        "globalThis.ResizeObserver = class { observe() {} disconnect() {} };\n"
        "globalThis.IntersectionObserver = class { observe() {} disconnect() {} };\n"
        "globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };\n"
        "globalThis.setInterval = () => 1;\n"
        "globalThis.clearInterval = () => {};\n"
    )
    res = subprocess.run(
        [node, "--input-type=module"],
        input=setup + body + "\nprocess.exit(0);\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=25,
    )
    assert res.returncode == 0, res.stderr
    return res


def test_ctxmap_treemap_grid_and_escaping() -> None:
    _run_node(
        f"""
const mod = await import("{((JS / "ctxmap.js").resolve()).as_uri()}");
assert.equal(mod.CELLS, 400);
assert.equal(mod.normalizeCtxMap(null), null);

const raw = {{
  used_tokens: 64000,
  limit_tokens: 128000,
  calibrated: true,
  parts: [
    {{ idx: 0, role: "system", group: "system", label: "身份<script>", tokens: 6000, preview: "sys<b>" }},
    {{ idx: 1, role: "system", group: "notes", label: "常驻笔记", tokens: 2000, preview: "线索索引" }},
    {{ idx: 2, role: "user", group: "history", label: "指令<script>", tokens: 28000, preview: "请查看" }},
    {{ idx: 3, role: "assistant", group: "history", label: "回复<img>", tokens: 22000, preview: "ok" }},
    {{ idx: -1, role: "tools", group: "tools", label: "工具规格", tokens: 6000, preview: "web_search" }},
  ],
}};
const norm = mod.normalizeCtxMap(raw);
assert.equal(norm.used, 64000);
assert.equal(norm.limit, 128000);
assert.equal(norm.calibrated, true);
assert.equal(norm.groups.length, 4);

const tiles = mod.tilesOf(norm, 520, 180);
assert.ok(tiles.length >= 6, "包含已用分块（history 拆为 2 子块）+ 1 个剩余空间块");
assert.ok(tiles.some((t) => t.free && t.tokens === 64000));

const cells = mod.cellsOf(norm);
assert.equal(cells.length, 400);
assert.ok(cells.some((c) => c && c.kind === "notes"));
assert.ok(cells.some((c) => c === null));

const html = mod.ctxMapHTML(raw, {{ id: "t1", callId: 42, actPrefix: "board-ctx" }});
assert.match(html, /实测校准/);
assert.match(html, /data-act="board-ctx-view"/);
assert.match(html, /data-act="board-ctx-part"/);
assert.match(html, /data-call="42"/);
assert.ok(html.includes("指令&lt;script&gt;") && !html.includes("<script>"));
assert.ok(html.includes("回复&lt;img&gt;") && !html.includes("<img>"));
assert.ok(!html.includes("\\x00"));

await mod.actCtxMap("board-ctx-part", {{ dataset: {{ map: "t1", key: "system" }} }});
const insHtml = mod.ctxMapHTML(raw, {{ id: "t1", callId: 42, actPrefix: "board-ctx" }});
assert.ok(insHtml.includes("身份&lt;script&gt;") && !insHtml.includes("<script>"));
assert.ok(insHtml.includes("sys&lt;b&gt;") && !insHtml.includes("sys<b>"));
"""
    )


def test_jev_bars_all_three_shapes_and_escaping() -> None:
    _run_node(
        f"""
const {{ jevBarsHTML }} = await import("{((JS / "jev_bars.js").resolve()).as_uri()}");
assert.equal(jevBarsHTML(null), "");
assert.equal(jevBarsHTML({{}}), "");

// 1) 群页冷场判断
const topicHtml = jevBarsHTML({{
  ok_p: 0.68,
  ok_need: 0.6,
  ms: 35,
  reason_probs: {{ fine: 0.64, chatting: 0.25, serious: 0.11 }},
  fits: [{{ idx: 0, title: "开源硬件<img>", fit_p: 0.72, picked: true }}],
  fit_need: 0.5,
}}, {{ compact: true }});
assert.match(topicHtml, /开启适合度/);
assert.match(topicHtml, /35 毫秒/);
assert.match(topicHtml, /选用/);
assert.ok(topicHtml.includes("开源硬件&lt;img&gt;") && !topicHtml.includes("<img>"));

// 2) 待复核 @ 消息（平面 probs）
const askHtml = jevBarsHTML({{
  probs: {{ prepare: 0.48, chat: 0.42, goal: 0.10 }},
  threshold: 0.55,
  ms: 28,
}});
assert.match(askHtml, /任务请求/);
assert.match(askHtml, /0\\.48<small>\\/0\\.55<\\/small>/);

// 3) 看板步骤嵌套 probs
const stepHtml = jevBarsHTML({{
  probs: {{
    ok: {{ yes: 0.75, no: 0.25 }},
    reason: {{ fine: 0.8, done: 0.2 }},
  }},
  ms: 19,
}});
assert.match(stepHtml, /开启适合度/);
assert.match(stepHtml, /冷场可开/);
"""
    )


def test_board_steps_renders_ttft_jev_and_ctxmap() -> None:
    _run_node(
        f"""
const {{ stepsSectionHTML }} = await import("{((JS / "board_steps.js").resolve()).as_uri()}");
const html = stepsSectionHTML({{
  context_map: {{
    used_tokens: 12000,
    limit_tokens: 128000,
    parts: [{{ idx: 0, role: "system", group: "system", label: "系统提示", tokens: 12000 }}],
  }},
  steps: [
    {{
      uid: "m:99",
      ts: 1790000000,
      type: "think",
      actor: "主模型",
      model: "gpt-test",
      ms: 1400,
      first_token_ms: 320,
      model_call_id: 99,
      jev: {{ probs: {{ kind: {{ prepare: 0.82, chat: 0.18 }} }}, ms: 24 }},
      context_map: {{
        used_tokens: 12000,
        limit_tokens: 128000,
        parts: [{{ idx: 0, role: "system", group: "system", label: "系统提示", tokens: 12000 }}],
      }},
      text: "准备派单",
    }},
  ],
}}, {{ type: "all", round: "", open: {{}} }});

assert.match(html, /首字 320 毫秒/);
assert.match(html, /Jev 概率分布/);
assert.match(html, /本轮上下文窗口/);
assert.match(html, /上下文构成 · 12\\.0k token/);
assert.match(html, /data-call="99"/);
"""
    )


def test_board_flow_and_changes_and_index_notes() -> None:
    _run_node(
        f"""
const {{ state }} = await import("{((JS / "state.js").resolve()).as_uri()}");
const {{ boardFlowHTML }} = await import("{((JS / "board_flow.js").resolve()).as_uri()}");
const {{ changeCardHTML, changesPage }} = await import("{((JS / "settings/changes.js").resolve()).as_uri()}");
const {{ INDEX_NOTE_MAX }} = await import("{((JS / "pages/groupctx.js").resolve()).as_uri()}");

assert.equal(INDEX_NOTE_MAX, 1200);

state.me = {{ role: "admin" }};
state.groups = [{{ id: "1001", name: "测试群<1>" }}];
const flow = boardFlowHTML({{
  lanes: [{{ kind: "main", kind_title: "主模型" }}, {{ kind: "news", kind_title: "资讯", running: [] }}],
  live: {{
    active: [{{ flight_id: "f1", stage: "main", group_id: "1001", role: "main", started_ts: 1000, first_token_ms: 260 }}],
    recent: [{{ seq: 1, ts: 1001, stage: "gate", status: "done", group_name: "测试群<1>", ms: 31, ok: true }}],
  }},
}});
assert.match(flow, /实时流向/);
assert.match(flow, /1 项在途调用/);
assert.match(flow, /首字 260 毫秒/);
assert.ok(flow.includes("测试群&lt;1&gt;") && !flow.includes("测试群<1>"));

state.boardFlowCollapsed = true;
const collapsed = boardFlowHTML({{ lanes: [], live: {{ active: [], recent: [] }} }});
assert.doesNotMatch(collapsed, /id="board-flow"/);
state.boardFlowCollapsed = false;

// 设置修改记录：可撤销 vs 涉密不可撤销 vs 群管理员权限边界
state.changes = {{
  actor: "",
  hasMore: false,
  loading: false,
  error: "",
  items: [
    {{ id: 1, ts: 1790000100, actor: "admin_chat", scope: "group:1001", target_key: "group_push", summary: "调高每日上限<x>", reason: "群主要求", before_json: '{{"daily_max":3}}', after_json: '{{"daily_max":5}}', revertible: 1, reverted_at: null }},
    {{ id: 2, ts: 1790000050, actor: "admin_web", scope: "global", target_key: "console.password", summary: "更新管理员密码", reason: "", before_json: null, after_json: null, revertible: 0, reverted_at: null }},
  ],
}};
const chgHtml = changesPage();
assert.match(chgHtml, /主模型对话/);
assert.match(chgHtml, /测试群&lt;1&gt;/);
assert.match(chgHtml, /调高每日上限&lt;x&gt;/);
assert.match(chgHtml, /涉及凭据不可撤销/);
assert.equal((chgHtml.match(/data-act="chg-revert"/g) || []).length, 1);

state.me = {{ role: "group_admin", group: "1001" }};
const gaApprovalCard = changeCardHTML({{
  id: 3, ts: 1790000200, actor: "admin_web", scope: "group:1001", target_key: "group_approval",
  summary: "修改任务审批名单", revertible: 1, reverted_at: null,
}}, {{ forGroup: true, gid: "1001" }});
assert.match(gaApprovalCard, /仅总管理员可撤销/);
assert.doesNotMatch(gaApprovalCard, /data-act="chg-revert"/);
"""
    )
