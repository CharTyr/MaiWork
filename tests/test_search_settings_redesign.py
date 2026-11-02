"""搜索服务与网页读取设置重做测试（后端联动、实测接口、密钥遮罩、前端抽屉防轮询冲刷）。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork import extensions_web, search_binding, search_presets_web
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.store import Store

STATIC = Path(__file__).resolve().parent.parent / "maiwork" / "console" / "static"
JS = STATIC / "js"
PASSWORD = "搜索重做测试密码-9527"
G1 = "900000001"
FAKE_TVLY_KEY = "tvly-SECRET-KEY-778899"
FAKE_JINA_KEY = "jina_SECRET_KEY_112233"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def settings():
    s, _ = load_settings({})
    return s


def test_activate_syncs_extract_tool_on_key_add_and_clear(settings, store):
    """You.com 免密钥无抓正文工具；补密钥后自动绑上 you-contents，清除密钥后自动摘除防悬空报错。"""
    name, bound = search_presets_web.activate(store, settings, "you")
    assert name == "you" and bound is True
    b0 = search_binding.get_binding(store)
    assert b0["mcp"] == "you" and b0["extract_tool"] == "" and b0["extract_mcp"] == ""

    # 补上密钥：主搜索仍是 you → 自动同步绑上 you-contents
    search_presets_web.activate(store, settings, "you", key="ydc-FAKE-KEY-001")
    b1 = search_binding.get_binding(store)
    assert b1["mcp"] == "you"
    assert b1["extract_mcp"] == "you" and b1["extract_tool"] == "you-contents"

    # 清除密钥改回免密钥版：profile=free 没有 you-contents → 自动清空抓正文绑定
    search_presets_web.activate(store, settings, "you", clear_key=True)
    b2 = search_binding.get_binding(store)
    assert b2["mcp"] == "you"
    assert b2["extract_mcp"] == "" and b2["extract_tool"] == ""


def test_presets_view_propagates_runtime_error_and_scrubs_secret(settings, store):
    """MCP 运行体 client 壳还在但 ok=False 时，presets_view 必须报 ok=False 且带脱敏后的错误原因。"""
    search_presets_web.activate(store, settings, "tavily", key=FAKE_TVLY_KEY)
    bad_rt = SimpleNamespace(
        client=object(),
        ok=False,
        error=f"HTTP 401 Unauthorized: Bearer {FAKE_TVLY_KEY}",
        headers={"Authorization": f"Bearer {FAKE_TVLY_KEY}"},
    )
    view = {
        p["id"]: p
        for p in search_presets_web.presets_view(
            settings, store, lambda n: bad_rt if n == "tavily" else None
        )
    }
    tv = view["tavily"]
    assert tv["enabled"] is True
    assert tv["ok"] is False
    assert "401" in tv["error"]
    assert FAKE_TVLY_KEY not in tv["error"]


def test_set_primary_switches_main_syncs_extract_and_cleans_lists(settings, store):
    """一键设为主用：切换主搜索、同步同家网页读取工具，并把新主家从 fallback / broad 中剔除且保留原顺序。"""
    search_presets_web.setup(
        store,
        settings,
        [{"id": "keenable"}, {"id": "tavily", "key": FAKE_TVLY_KEY}, {"id": "exa"}, {"id": "you"}],
    )
    b_init = search_binding.get_binding(store)
    b_init["broad"] = ["tavily", "you"]
    search_binding.set_binding(store, b_init)

    name = search_presets_web.set_primary(store, settings, "tavily")
    assert name == "tavily"
    b = search_binding.get_binding(store)
    assert b["mcp"] == "tavily" and b["tool"] == "tavily_search"
    assert b["extract_mcp"] == "tavily" and b["extract_tool"] == "tavily_extract"
    assert b["fallback"] == ["exa", "you"]
    assert b["broad"] == ["you"]


def test_menu_path_points_to_tools_and_platforms(settings, store):
    """报错和状态引导统一指向现行一级导航「设置 → 工具与平台」。"""
    ok, text = search_binding.status_of(store, settings, lambda _n: None)
    assert ok is False and "设置 → 工具与平台" in text
    with pytest.raises(ValueError) as ei:
        search_binding.save_binding(
            store, settings, {"mcp": "ghost", "tool": "s"}, tool_spec_of=lambda _m, _t: None
        )
    assert "设置 → 工具与平台" in str(ei.value)


def _sse(payload: dict, session: str = "sess-preset") -> httpx.Response:
    body = "event: message\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
    return httpx.Response(
        200, text=body, headers={"content-type": "text/event-stream", "mcp-session-id": session}
    )


class _PresetMcpStub:
    def __init__(self) -> None:
        self.mode = "ok"
        self.calls: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        method = body.get("method")
        if method == "initialize":
            return _sse({
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "serverInfo": {"name": "stub", "version": "1"},
                },
            })
        if method == "notifications/initialized":
            return httpx.Response(202, text="")
        if method == "tools/list":
            host = request.url.host or ""
            if "keenable" in host:
                tools = [
                    {"name": "search_web_pages", "description": "search", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}}}},
                    {"name": "fetch_page_content", "description": "extract", "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}}},
                ]
            else:
                tools = [
                    {"name": "tavily_search", "description": "search", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}}}},
                    {"name": "tavily_extract", "description": "extract", "inputSchema": {"type": "object", "properties": {"urls": {"type": "array"}}}},
                ]
            return _sse({"jsonrpc": "2.0", "id": body["id"], "result": {"tools": tools}})
        if method == "tools/call":
            params = body.get("params") or {}
            self.calls.append({"url": str(request.url), "name": params.get("name"), "arguments": params.get("arguments")})
            if self.mode == "quota":
                return _sse({
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "isError": False,
                        "structuredContent": {"code": "monthly_cap_reached"},
                        "content": [{"type": "text", "text": '{"code":"monthly_cap_reached"}'}],
                    },
                })
            if self.mode == "leak_err":
                return _sse({
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": f"Invalid key {FAKE_TVLY_KEY}"}],
                    },
                })
            return _sse({
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "structuredContent": {
                        "results": [
                            {"title": "Result 1", "url": "https://example.com/1", "content": "alpha"},
                            {"title": "Result 2", "url": "https://example.com/2", "content": "beta"},
                        ]
                    },
                    "content": [{"type": "text", "text": ""}],
                },
            })
        return _sse({"jsonrpc": "2.0", "id": body.get("id", 1), "result": {}})


def _write_plugin_toml(plug_dir: Path, data_dir: Path) -> None:
    import tomlkit

    plug_dir.mkdir(parents=True, exist_ok=True)
    doc = tomlkit.document()
    for section, values in {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}]},
        "console": {"listen": "127.0.0.1:0", "password": PASSWORD, "public_url": ""},
        "models": {"base_url": "https://ep.test/v1", "api_key": "sk-test", "main": "main-m", "worker": "worker-m"},
        "storage": {"data_dir": str(data_dir)},
        "approval": {"admins": ["qq:10001"]},
        "reader": {"jina_enabled": True, "jina_api_key": FAKE_JINA_KEY},
    }.items():
        t = tomlkit.table()
        for k, v in values.items():
            t[k] = v
        doc[section] = t
    (plug_dir / "config.toml").write_text(tomlkit.dumps(doc), encoding="utf-8")


@pytest.mark.asyncio
async def test_api_preset_primary_real_test_and_reader_summary(tmp_path: Path):
    """完整 HTTP 契约：鉴权/CSRF、一键设为主用、真实搜一条检测（含配额耗尽识别与密钥遮罩）、Jina 状态只进不出。"""
    data_dir = tmp_path / "data"
    plug_dir = tmp_path / "plug"
    _write_plugin_toml(plug_dir, data_dir)
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}]},
        "console": {"listen": "127.0.0.1:0", "password": PASSWORD, "public_url": ""},
        "models": {"base_url": "https://ep.test/v1", "api_key": "sk-test", "main": "main-m", "worker": "worker-m"},
        "storage": {"data_dir": str(data_dir)},
        "approval": {"admins": ["qq:10001"]},
        "reader": {"jina_enabled": True, "jina_api_key": FAKE_JINA_KEY},
    }
    stub = _PresetMcpStub()
    app = MaiWorkApp(FakeCtx({"config.get": "987654321"}), raw, plugin_dir=plug_dir)
    app.profiles_cls = FakeProfiles
    app.extensions_transport = httpx.MockTransport(stub)
    await app.start()
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    try:
        # 1. 未登录 401
        assert (await client.post("/api/extensions/presets/tavily/test", json={})).status == 401

        assert (await client.post("/api/login", json={"password": PASSWORD})).status == 200

        # 2. 跨站 Origin 被 CSRF 门拦下
        r_csrf = await client.post(
            "/api/extensions/presets/tavily/test",
            json={},
            headers={"Origin": "https://evil.example"},
        )
        assert r_csrf.status == 403

        # 3. GET /api/extensions/search 带 reader 摘要但绝不泄露 Jina 密钥
        r_sx = await client.get("/api/extensions/search")
        assert r_sx.status == 200
        sx_text = await r_sx.text()
        assert FAKE_JINA_KEY not in sx_text
        sx = json.loads(sx_text)
        assert sx["reader"]["jina_enabled"] is True
        assert sx["reader"]["jina_key_set"] is True
        assert sx["reader"]["ok"] is True

        # 4. 先启用 keenable（自动成主用），再启用 tavily，然后一键将 tavily 设为主用
        r1 = await client.post("/api/extensions/presets/keenable", json={})
        assert r1.status == 200
        r2 = await client.post("/api/extensions/presets/tavily", json={"key": FAKE_TVLY_KEY})
        assert r2.status == 200
        r_pri = await client.post("/api/extensions/presets/tavily", json={"set_primary": True})
        assert r_pri.status == 200
        pri = await r_pri.json()
        assert pri["ok"] is True
        assert pri["search"]["binding"]["mcp"] == "tavily"
        assert pri["search"]["binding"]["extract_tool"] == "tavily_extract"

        # 5. 实测搜索接口：正常出结果
        r_ok = await client.post("/api/extensions/presets/tavily/test", json={})
        assert r_ok.status == 200
        d_ok = await r_ok.json()
        assert d_ok["ok"] is True and d_ok["count"] == 2 and isinstance(d_ok["ms"], int)
        assert stub.calls and stub.calls[-1]["name"] == "tavily_search"

        # 6. 实测搜索接口：配额用尽（monthly_cap_reached 即使 isError=False 也要判失败）
        stub.mode = "quota"
        r_cap = await client.post("/api/extensions/presets/tavily/test", json={})
        assert r_cap.status == 200
        d_cap = await r_cap.json()
        assert d_cap["ok"] is False and "额度" in d_cap["error"]

        # 7. 实测搜索接口：远端错误原文含密钥时必须脱敏
        stub.mode = "leak_err"
        r_leak = await client.post("/api/extensions/presets/tavily/test", json={})
        assert r_leak.status == 200
        leak_raw = await r_leak.text()
        assert FAKE_TVLY_KEY not in leak_raw
        d_leak = json.loads(leak_raw)
        assert d_leak["ok"] is False and "***" in d_leak["error"]
    finally:
        await client.close()
        await app.stop()


def test_frontend_search_redesign_renders_and_protects_sheet(tmp_path: Path):
    """前端搜索与网页读取模块：统一预设行、网页读取区、抽屉编辑防轮询冲刷、备用顺序调整。"""
    node = shutil.which("node")
    assert node is not None, "本机缺少 node，无法运行前端模块检查"
    assert (JS / "settings/search.js").is_file(), "缺少前端模块 settings/search.js"

    script = tmp_path / "run_search_ui.mjs"
    script.write_text(
        f"""
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const ROOT = {json.dumps(str(JS))};
let renderCount = 0;
const mkClassList = () => ({{ add() {{}}, remove() {{}}, toggle() {{}}, contains: () => false }});
const dom = {{
  sheet: {{
    innerHTML: '', hidden: true, scrollTop: 0, offsetHeight: 480, dataset: {{}}, style: {{}},
    classList: mkClassList(), setAttribute() {{}}, focus() {{}},
    querySelector() {{ return null; }}, querySelectorAll() {{ return []; }},
    addEventListener() {{}}, removeEventListener() {{}},
  }},
  scrim: {{ hidden: true, style: {{}}, classList: mkClassList() }},
  app: {{ inert: false, setAttribute() {{}}, removeAttribute() {{}} }},
  tabbar: {{ inert: false, setAttribute() {{}}, removeAttribute() {{}} }},
}};
const context = vm.createContext({{
  console, URLSearchParams, Date, CSS: {{ escape: (x) => String(x) }},
  setTimeout: (fn) => {{ fn(); return 1; }}, clearTimeout() {{}}, setInterval() {{}},
  requestAnimationFrame: (fn) => fn(), confirm: () => true,
  location: {{ hash: '', search: '', origin: 'https://mw.test' }},
  history: {{ replaceState() {{}} }},
  window: {{ matchMedia: () => ({{ matches: false, addEventListener() {{}} }}), scrollY: 0, scrollTo() {{}} }},
  document: {{
    hidden: false, activeElement: null, contains: () => false,
    getElementById: (id) => dom[id] || null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {{}}, removeEventListener() {{}},
  }},
}});
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({{ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }}[c]));
const stubs = {{
  esc, calm: () => true, ico: (k) => `<i data-ico="${{k}}"></i>`,
  tip: (t) => `<span class="tip">${{t}}</span>`,
  tipLabel: (f, t) => `<label for="${{f}}">${{t}}</label>`,
  toast() {{}}, when: () => '今天', dayWord: () => '今天', msText: (ms) => `${{ms}}ms`,
  SVG: {{ close: 'x', right: '>', trash: 'd' }},
  api: async () => ({{}}),
  loading: () => '<p>加载中</p>',
  loadOptDeps: async () => {{}}, optDepsSection: () => '',
  renderView: () => {{ renderCount += 1; }}, renderSide: () => {{}}, renderRail: () => {{}},
  syncHash: () => {{}}, sfx: () => {{}},
}};
const mods = new Map();
const real = new Set(['state.js', 'sheet.js', 'settings/ext.js', 'settings/search.js']);
function getMod(file) {{
  if (mods.has(file)) return mods.get(file);
  const src = fs.readFileSync(file, 'utf8');
  let mod;
  if (real.has(path.relative(ROOT, file).split(path.sep).join('/'))) {{
    mod = new vm.SourceTextModule(src, {{ context, identifier: file }});
  }} else {{
    const names = [...new Set([...src.matchAll(/^export\\s+(?:async\\s+)?(?:function|const|let|class)\\s+(\\w+)/gm)].map((m) => m[1]))];
    mod = new vm.SyntheticModule(names, function () {{
      for (const n of names) this.setExport(n, stubs[n] ?? (() => ''));
    }}, {{ context }});
  }}
  mods.set(file, mod);
  return mod;
}}
for (const r of real) getMod(path.join(ROOT, r));
for (const m of mods.values()) if (m.status === 'unlinked') await m.link((spec, parent) => getMod(path.resolve(path.dirname(parent.identifier), spec)));
for (const r of real) {{ const m = mods.get(path.join(ROOT, r)); if (m.status !== 'evaluated') await m.evaluate(); }}

const st = mods.get(path.join(ROOT, 'state.js')).namespace;
const sh = mods.get(path.join(ROOT, 'sheet.js')).namespace;
const ex = mods.get(path.join(ROOT, 'settings/ext.js')).namespace;
const sx = mods.get(path.join(ROOT, 'settings/search.js')).namespace;

const state = st.state;
state.page = 'settings';
state.setSub = 'extensions';
state.ext = {{
  mcp: [
    {{ name: 'keenable', url: 'https://api.keenable.ai/mcp', enabled: true, ok: true, tools: 2, tool_names: ['search_web_pages', 'fetch_page_content'], roles: ['worker'], source: 'web' }},
    {{ name: 'tavily', url: 'https://mcp.tavily.com/mcp/', enabled: true, ok: false, error: 'HTTP 401', tools: 2, tool_names: ['tavily_search', 'tavily_extract'], roles: ['worker'], source: 'web' }},
    {{ name: 'exa', url: 'https://mcp.exa.ai/mcp', enabled: true, ok: true, tools: 2, tool_names: ['web_search_advanced_exa', 'crawling_exa'], roles: ['worker'], source: 'web' }},
  ],
  skills: [],
}};
state.extPresets = {{
  presets: [
    {{ id: 'keenable', label: 'Keenable', free: true, free_note: '免费额度', key_page_url: 'https://keenable.ai', logo: '/static/assets/logos/keenable.png', entry: 'keenable', source: 'web', enabled: true, ok: true, error: '', key_set: false }},
    {{ id: 'tavily', label: 'Tavily', free: true, free_note: '每月 1000 次', key_page_url: 'https://app.tavily.com', logo: '/static/assets/logos/tavily.png', entry: 'tavily', source: 'web', enabled: true, ok: false, error: 'HTTP 401', key_set: true }},
    {{ id: 'exa', label: 'Exa', free: true, free_note: '免费可用', key_page_url: 'https://exa.ai', logo: '/static/assets/logos/exa.png', entry: 'exa', source: 'web', enabled: true, ok: true, error: '', key_set: false }},
    {{ id: 'tinyfish', label: 'TinyFish', free: false, free_note: '', key_page_url: 'https://agent.tinyfish.ai', logo: '/static/assets/logos/tinyfish.png', entry: null, source: null, enabled: false, ok: false, error: '', key_set: false }},
  ],
}};
state.extSearch = {{
  binding: {{ mcp: 'keenable', tool: 'search_web_pages', extract_mcp: 'keenable', extract_tool: 'fetch_page_content', fallback: ['tavily', 'exa'], broad: ['exa'] }},
  status: {{ ok: true, text: '用 keenable 的 search_web_pages（抓正文 fetch_page_content）' }},
  candidates: [
    {{ mcp: 'keenable', tools: [{{ name: 'search_web_pages', guess: 'search' }}, {{ name: 'fetch_page_content', guess: 'extract' }}] }},
    {{ mcp: 'tavily', tools: [{{ name: 'tavily_search', guess: 'search' }}, {{ name: 'tavily_extract', guess: 'extract' }}] }},
    {{ mcp: 'exa', tools: [{{ name: 'web_search_advanced_exa', guess: 'search' }}, {{ name: 'crawling_exa', guess: 'extract' }}] }},
  ],
  reader: {{ jina_enabled: true, jina_key_set: false, ok: true, text: '先用 Jina Reader（免费额度，每分钟 20 次）' }},
}};

const pageHtml = ex.extPage('search');

// 1. 点预设「密钥」进 #sheet，背景不内联展开，且 preserveSheet: true 保护输入
await sx.actSearch('preset-key', {{ dataset: {{ id: 'tavily' }} }});
const keySheetHtml = dom.sheet.innerHTML;
const bgWhileSheet = ex.extPage('search');
dom.sheet.innerHTML = '<input id="typing-secret" value="keep" />';
sh.repaintSheet({{ preserveSheet: true }});
const keptSecret = dom.sheet.innerHTML;
sh.closeSheet();
const clearedAfterClose = state.searchSheet ?? null;

// 2. 点 Jina「填写密钥」进 #sheet
await sx.actSearch('jina-key', {{ dataset: {{}} }});
const jinaSheetHtml = dom.sheet.innerHTML;
sh.closeSheet();

// 3. 打开链路与高级绑定抽屉，测试备用顺序移动
await sx.actSearch('search-chain-open', {{ dataset: {{}} }});
const beforeMove = [...state.searchSheet.fallback];
await sx.actSearch('sx-fb-move', {{ dataset: {{ name: 'exa', dir: '-1' }} }});
const afterMove = [...state.searchSheet.fallback];
const chainSheetHtml = dom.sheet.innerHTML;
sh.closeSheet();

console.log(JSON.stringify({{
  pageHtml, keySheetHtml, bgWhileSheet, keptSecret, clearedAfterClose,
  jinaSheetHtml, beforeMove, afterMove, chainSheetHtml,
}}));
""",
        encoding="utf-8",
    )
    res = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])

    page = out["pageHtml"]
    assert "搜索服务" in page and "网页读取" in page and "Jina Reader" in page
    assert "主用" in page and "备用 #1" in page and "备用 #2" in page and "同时搜索" in page
    assert 'data-act="preset-primary"' in page and 'data-act="preset-test"' in page
    assert "HTTP 401" in page and "warn-box" in page
    assert "web_search_advanced_exa" not in page and "子 agent可用" not in page

    assert 'id="pk-key"' in out["keySheetHtml"] and "清除密钥" in out["keySheetHtml"]
    assert 'id="pk-key"' not in out["bgWhileSheet"], "密钥表单只能在抽屉里打开，不能在原位内联展开"
    assert out["keptSecret"] == '<input id="typing-secret" value="keep" />'
    assert out["clearedAfterClose"] is None

    assert 'id="jina-key-input"' in out["jinaSheetHtml"]
    assert out["beforeMove"] == ["tavily", "exa"]
    assert out["afterMove"] == ["exa", "tavily"]
    assert 'id="sx-tool"' in out["chainSheetHtml"] and 'id="sx-extract"' in out["chainSheetHtml"]
