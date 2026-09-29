// MaiWork 网页 · 设置 ·「扩展」：MCP、技能、联网搜索。
import { state } from "../state.js";
import { SVG, dayWord, esc, ico, toast } from "../util.js";
import { api } from "../api.js";
import { loading } from "../pages/news.js";

export const ROLE_NAMES = { worker: "子 agent", main: "主模型" };

export function extPage() {
  const x = state.ext;
  if (!x) return loading();
  const ed = state.extEdit;
  const mcp = x.mcp || [];
  const skills = x.skills || [];
  const newMcp = ed && ed.kind === "mcp" && !ed.name;
  const pasting = ed && ed.kind === "mcp-paste";
  const newSkill = ed && ed.kind === "skill" && !ed.name;
  return `
    <p class="h-meta">给 MaiWork 接上外部工具和做事说明</p>
    ${searchCard()}
    <div class="h-sub-row"><h2 class="h-sub">MCP</h2>${newMcp || pasting ? "" : `<span class="row-btns"><button class="btn small" data-act="mcp-paste">粘贴配置</button><button class="btn small" data-act="ext-new" data-kind="mcp">加一个</button></span>`}</div>
    ${pasting ? mcpPasteForm() : ""}
    ${newMcp ? mcpForm(null) : ""}
    ${mcp.length ? mcp.map((m) => (ed && ed.kind === "mcp" && ed.name === m.name ? mcpForm(m) : mcpRow(m))).join("") : newMcp ? "" : `<p class="h-meta">还没有。</p>`}
    <div class="h-sub-row"><h2 class="h-sub">skill</h2>${newSkill ? "" : `<span class="row-btns"><label class="btn small file-btn">上传 zip<input type="file" id="skill-zip" accept=".zip,application/zip" hidden /></label><button class="btn small" data-act="ext-new" data-kind="skill">加一个</button></span>`}</div>
    ${newSkill ? skillForm(null) : ""}
    ${skills.length ? skills.map((k) => (ed && ed.kind === "skill" && ed.name === k.name ? skillForm(k) : skillRow(k))).join("") : newSkill ? "" : `<p class="h-meta">还没有。</p>`}`;
}

// 联网搜索：从已接的 MCP 里挑一个工具来用（后端 GET/PUT/DELETE /api/extensions/search）。
// 「抓网页正文」可以另选任意一家 MCP 的工具，不必和搜索同一家。
function searchCard() {
  const sx = state.extSearch;
  if (!sx) return "";
  const b = sx.binding;
  const st = sx.status || {};
  const cands = (sx.candidates || []).filter((c) => (c.tools || []).length);
  const editing = state.searchEdit || !b;
  const head = `<div class="h-sub-row"><h2 class="h-sub">联网搜索</h2>${!editing ? `<span class="row-btns"><button class="btn small" data-act="search-edit">换一个</button></span>` : ""}</div>`;
  const stLine = `<div class="set-text ext-st"><span class="dot ${st.ok ? "ok" : b ? "failed" : ""}"></span>${esc(b ? st.text || "" : "还没选")}</div>`;
  if (!editing) {
    const exMcp = b.extract_mcp || b.mcp;
    return `${head}
      <div class="ext-row search-now">
        ${ico("magnifier")}
        <div class="ext-main">
          <div class="set-name">${esc(b.mcp)} · <span class="mono">${esc(b.tool)}</span></div>
          ${b.extract_tool ? `<div class="set-text">抓网页正文：${esc(exMcp)} · <span class="mono">${esc(b.extract_tool)}</span></div>` : ""}
          ${stLine}
        </div>
      </div>
      <p class="fine search-agents">搜索偏好可以写进「身份 → AGENTS.md」</p>`;
  }
  if (!cands.length) {
    return `${head}${stLine}<p class="h-meta">先在下面接一个能搜索的 MCP，比如 Tavily、Exa</p>`;
  }
  const all = cands.flatMap((c) => c.tools.map((t) => ({ mcp: c.mcp, ...t })));
  const pick = b ? all.find((t) => t.mcp === b.mcp && t.name === b.tool) : all.find((t) => t.guess === "search") || all[0];
  // 抓正文：有绑定就用绑定的；没有就猜一个「像抓正文」的（同一家优先）
  let exPick = null;
  if (b && b.extract_tool) exPick = all.find((t) => t.mcp === (b.extract_mcp || b.mcp) && t.name === b.extract_tool) || null;
  else if (!b) exPick = all.find((t) => t.guess === "extract" && pick && t.mcp === pick.mcp) || all.find((t) => t.guess === "extract") || null;
  const same = (t, p) => !!p && t.mcp === p.mcp && t.name === p.name;
  // 扩展名和工具名分开放在 data-* 里（别拼成一个值：HTML 属性里的 NUL 会被浏览器换掉，拆不开）
  const opt = (t, sel, hint) =>
    `<option value="${esc(t.mcp + " · " + t.name)}" data-mcp="${esc(t.mcp)}" data-tool="${esc(t.name)}" ${sel ? "selected" : ""}>${esc(t.mcp)} · ${esc(t.name)}${t.guess === hint ? (hint === "search" ? "（像搜索）" : "（像抓正文）") : ""}</option>`;
  return `${head}${b ? stLine : ""}
    <div class="login ext-form search-form">
      <label for="sx-tool">用哪个工具搜索</label>
      <select id="sx-tool">${all.map((t) => opt(t, same(t, pick), "search")).join("")}</select>
      <label for="sx-extract">抓网页正文 <span class="fine-inline">可选 · 可以选另一家</span></label>
      <select id="sx-extract"><option value="" ${exPick ? "" : "selected"}>不用</option>${all.map((t) => opt(t, same(t, exPick), "extract")).join("")}</select>
      <div class="actions">
        <button class="btn primary" data-act="search-save">用这个</button>
        ${b ? `<button class="btn" data-act="search-cancel">取消</button><button class="btn danger" data-act="search-off">不用联网搜索</button>` : ""}
      </div>
    </div>`;
}

// 下拉当前选中的 {mcp, tool}（id = sx-tool 搜索 / sx-extract 抓正文；选「不用」→ 两个都是 ""）
export function pickedTool(id) {
  const sel = document.getElementById(id);
  const o = sel && sel.selectedOptions && sel.selectedOptions[0];
  return { mcp: (o && o.dataset.mcp) || "", tool: (o && o.dataset.tool) || "" };
}

function mcpRow(m) {
  const st = !m.enabled
    ? `<span class="dot"></span>关着`
    : m.ok
      ? `<span class="dot ok"></span>连上了 · ${m.tools || 0} 个工具`
      : `<span class="dot failed"></span>没连上：${esc(m.error || "")}`;
  const roles = roleText(m.roles);
  const hdrs = (m.header_names || []).length ? ` · 请求头 ${m.header_names.map(esc).join("、")}（已填）` : "";
  return `
    <div class="ext-row">
      ${ico("link")}
      <div class="ext-main">
        <div class="set-name">${esc(m.name)}${m.source === "config" ? `<span class="tag">配置文件里的</span>` : ""}${m.search_role === "search" ? `<span class="tag">联网搜索</span>` : m.search_role === "extract" ? `<span class="tag">抓正文</span>` : ""}</div>
        <div class="set-text mono-link">${esc(m.url || "")}</div>
        <div class="set-text ext-st">${st} · 给${esc(roles)}用${hdrs}</div>
        ${
          (m.tool_names || []).length
            ? `<div class="ext-tools">${m.tool_names.slice(0, 12).map((t) => `<span class="ntag">${esc(t)}</span>`).join("")}${m.tool_names.length > 12 ? `<span class="ntag">共 ${m.tool_names.length} 个</span>` : ""}</div>`
            : ""
        }
      </div>
      <div class="row-btns ext-btns">
        <button class="btn small" data-act="mcp-toggle" data-name="${esc(m.name)}" data-on="${m.enabled ? "0" : "1"}">${m.enabled ? "关掉" : "打开"}</button>
        ${m.enabled ? `<button class="btn small" data-act="mcp-reload" data-name="${esc(m.name)}">重连</button>` : ""}
        ${m.source === "config" ? "" : `<button class="btn small" data-act="ext-edit" data-kind="mcp" data-name="${esc(m.name)}">改</button>`}
      </div>
    </div>`;
}

function mcpPasteForm() {
  return `
    <form id="mcp-paste-form" class="login ext-form" autocomplete="off">
      <div class="ext-form-h">粘贴 MCP 配置</div>
      <p class="fine" style="margin:0">支持 Claude Desktop、Cursor、VS Code 格式，仅限远程（http / sse）</p>
      <textarea id="mp-text" class="mono-area" rows="9" spellcheck="false" placeholder='{"mcpServers": {"tavily": {"type": "http", "url": "https://…/mcp", "headers": {"Authorization": "Bearer …"}}}}'></textarea>
      <p class="err" id="mp-err" hidden></p>
      <div class="actions">
        <button class="btn primary" type="submit">读出来</button>
        <button type="button" class="btn" data-act="ext-cancel">取消</button>
      </div>
    </form>`;
}

// 解析各家 MCP 客户端配置 → [{name, url, headers, skipped?}]
export function parseMcpConfig(text) {
  let data;
  try {
    data = JSON.parse(text);
  } catch (e) {
    // 允许只粘了 "名字": {...} 这一段
    try {
      data = JSON.parse(`{${text.trim().replace(/,\s*$/, "")}}`);
    } catch (e2) {
      throw new Error("这段不是合法的 JSON，检查一下有没有少括号或多逗号。");
    }
  }
  if (!data || typeof data !== "object") throw new Error("没读出服务器配置。");
  const isServer = (o) => o && typeof o === "object" && (o.url || o.serverUrl || o.command);
  let entries;
  if (isServer(data)) entries = [["", data]];
  else {
    const map = data.mcpServers || data.servers || (data.mcp && data.mcp.servers) || data;
    entries = Object.entries(map).filter(([, v]) => isServer(v));
  }
  if (!entries.length) throw new Error("没找到 MCP 服务器（要有 url）。");
  const slug = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32);
  return entries.map(([key, v]) => {
    const url = String(v.url || v.serverUrl || "");
    let name = slug(key);
    if (!name && url) {
      try {
        const host = new URL(url).hostname.split(".");
        name = slug(host.length > 2 ? host[host.length - 3] || host[0] : host[0]);
      } catch (e) {}
    }
    if (v.command) return { name: name || "mcp", skipped: "要在本机跑命令（command），不支持" };
    if (!/^https:\/\//i.test(url)) return { name: name || "mcp", skipped: "地址不是 https" };
    const headers = {};
    Object.entries(v.headers || v.requestInit?.headers || {}).forEach(([k, val]) => {
      if (typeof val === "string") headers[k] = val;
    });
    return { name: name || "mcp", url, headers };
  });
}

// 给谁用：三选一（主模型 / 子 agent / 两边都能用）
const ROLE_PICKS = [["main", "只给主模型", "和 MaiWork 聊、派活验收时能用"], ["worker", "只给子 agent", "动手干活时能用"], ["both", "两边都能用", ""]];
const rolePick = (roles) => {
  roles = roles && roles.length ? roles : ["worker"];
  return roles.includes("main") && roles.includes("worker") ? "both" : roles.includes("main") ? "main" : "worker";
};
const roleText = (roles) => ({ main: "主模型", worker: "子 agent", both: "主模型和子 agent" })[rolePick(roles)];
function roleChecks(prefix, roles) {
  const cur = rolePick(roles);
  return `<div class="seg role-seg" role="radiogroup">${ROLE_PICKS.map(
    ([k, n, hint]) => `<label title="${esc(hint)}"><input type="radio" name="${prefix}-role" value="${k}" ${cur === k ? "checked" : ""} /><span>${n}</span></label>`
  ).join("")}</div>`;
}

export function headerRow(name, isOld, value) {
  return `
    <div class="hdr-row" data-old="${isOld ? "1" : "0"}">
      <input class="hdr-name" spellcheck="false" placeholder="名字，比如 Authorization" value="${esc(name || "")}" ${isOld ? "readonly" : ""} />
      <input class="hdr-val" type="password" autocomplete="new-password" placeholder="${isOld ? "已填 · 留空就不改" : "值，比如 Bearer …"}" value="${esc(value || "")}" />
      <button type="button" class="icon-btn" data-act="hdr-del" aria-label="删掉这个请求头">${SVG.trash}</button>
    </div>`;
}

function mcpForm(m) {
  const isNew = !m;
  const pre = (isNew && state.extEdit && state.extEdit.prefill) || null;
  m = m || { roles: ["worker"], enabled: true, timeout_s: 30, header_names: [], tools_filter: [], ...(pre || {}) };
  return `
    <form id="mcp-form" class="login ext-form" autocomplete="off" data-name="${esc(isNew ? "" : m.name)}">
      <div class="ext-form-h">${isNew ? "加一个 MCP" : `改 ${esc(m.name)}`}</div>
      ${pre ? `` : ""}
      ${isNew ? `<label for="x-name">名字</label><input id="x-name" spellcheck="false" placeholder="只用字母、数字、下划线、横线，比如 github" value="${esc(m.name || "")}" />` : ""}
      <label for="x-url">地址</label>
      <input id="x-url" type="url" inputmode="url" spellcheck="false" value="${esc(m.url || "")}" placeholder="https://…/mcp" />
      <label>请求头 <small class="lbl-hint">密钥只进不出，保存后网页上看不到</small></label>
      <div id="x-hdrs">${isNew ? Object.entries(m.headers || {}).map(([h, v]) => headerRow(h, false, v)).join("") : (m.header_names || []).map((h) => headerRow(h, true)).join("")}</div>
      <button type="button" class="btn small" data-act="hdr-add" style="align-self:flex-start">加一个请求头</button>
      <label for="x-tools">只用这些工具 <small class="lbl-hint">空着 = 全部；多个用逗号隔开</small></label>
      <input id="x-tools" spellcheck="false" value="${esc((m.tools_filter || []).join(", "))}" />
      <label>给谁用</label>
      ${roleChecks("x", m.roles)}
      <label for="x-timeout">超时（秒）</label>
      <input id="x-timeout" type="number" min="5" max="300" value="${esc(m.timeout_s || 30)}" />
      <label class="chk"><input type="checkbox" id="x-enabled" ${m.enabled !== false ? "checked" : ""} />打开</label>
      <div class="actions"><button type="button" class="btn" data-act="mcp-test">测试连接</button><span class="fine" id="x-check" style="margin:0;align-self:center"></span></div>
      <p class="err" id="x-err" hidden></p>
      <div class="actions">
        <button class="btn primary" type="submit">保存</button>
        <button type="button" class="btn" data-act="ext-cancel">取消</button>
        ${isNew ? "" : `<button type="button" class="btn danger" data-act="mcp-del" data-name="${esc(m.name)}">删掉</button>`}
      </div>
    </form>`;
}

function skillRow(k) {
  const roles = roleText(k.roles);
  return `
    <div class="ext-row">
      ${ico("books")}
      <div class="ext-main">
        <div class="set-name">${esc(k.name)}${k.source === "file" ? `<span class="tag">服务器上放的</span>` : k.source === "builtin" ? `<span class="tag">内置</span>` : ""}</div>
        <div class="set-text">${esc(k.description || "没写描述")}</div>
        <div class="set-text ext-st">给${esc(roles)}用${k.updated_ts ? ` · ${esc(dayWord(k.updated_ts))}改过` : ""}</div>
      </div>
      <div class="row-btns ext-btns"><button class="btn small" data-act="ext-edit" data-kind="skill" data-name="${esc(k.name)}">${k.source === "builtin" ? "看" : "改"}</button></div>
    </div>`;
}

function skillForm(k) {
  const isNew = !k;
  const d = (state.extEdit && state.extEdit.data) || {};
  if (!isNew && !state.extEdit.data) return `<div class="ext-form">${loading()}</div>`;
  if (d.source === "builtin") {
    // 内置 skill 随插件发布：只能看，不能改删（要改标准得发新版本）
    return `
    <div class="login ext-form">
      <div class="ext-form-h">${esc(k.name)}<span class="tag">内置</span></div>
      <p class="fine" style="margin:0">随 MaiWork 一起发布，这里只能看。给${esc(roleText(d.roles))}用。</p>
      <label>一句话描述</label>
      <p class="set-text" style="margin:0">${esc(d.description || "")}</p>
      <label for="k-body">内容</label>
      <textarea id="k-body" class="mono-area" rows="14" readonly spellcheck="false">${esc(d.body || "")}</textarea>
      ${(d.files || []).length ? `<p class="fine" style="margin:0">附带文件：${d.files.map(esc).join("、")}</p>` : ""}
      <div class="actions"><button type="button" class="btn" data-act="ext-cancel">收起</button></div>
    </div>`;
  }
  return `
    <form id="skill-form" class="login ext-form" autocomplete="off" data-name="${esc(isNew ? "" : k.name)}">
      <div class="ext-form-h">${isNew ? "加一个 skill" : `改 ${esc(k.name)}`}</div>
      ${isNew ? `<label for="k-name">名字</label><input id="k-name" spellcheck="false" placeholder="只用字母、数字、下划线、横线，比如 write-report" />` : ""}
      <label for="k-desc">一句话描述 <small class="lbl-hint">模型靠这句决定要不要读它</small></label>
      <input id="k-desc" value="${esc(d.description || "")}" placeholder="比如：写周报时的格式和注意事项" />
      <label>给谁用</label>
      ${roleChecks("k", d.roles)}
      <label for="k-body">内容 <small class="lbl-hint">怎么做、注意什么、例子；最多 40KB</small></label>
      <textarea id="k-body" class="mono-area" rows="14" spellcheck="false">${esc(d.body || "")}</textarea>
      ${(d.files || []).length ? `<p class="fine" style="margin:0">附带文件：${d.files.map(esc).join("、")}</p>` : ""}
      <p class="err" id="k-err" hidden></p>
      <div class="actions">
        <button class="btn primary" type="submit">保存</button>
        <button type="button" class="btn" data-act="ext-cancel">取消</button>
        ${isNew ? "" : `<button type="button" class="btn danger" data-act="skill-del" data-name="${esc(k.name)}">删掉</button>`}
      </div>
    </form>`;
}

export function readRoles(prefix) {
  const el = document.querySelector(`input[name="${prefix}-role"]:checked`);
  const v = el ? el.value : "worker";
  return v === "both" ? ["main", "worker"] : [v];
}

export function readHeaders() {
  const headers = {};
  const remove = [];
  document.querySelectorAll("#x-hdrs .hdr-row").forEach((row) => {
    const n = row.querySelector(".hdr-name").value.trim();
    const v = row.querySelector(".hdr-val").value;
    if (!n) return;
    if (row.dataset.removed === "1") remove.push(n);
    else if (row.dataset.old === "1") headers[n] = v; // 空 = 不改
    else if (v) headers[n] = v;
  });
  return { headers, remove };
}

export async function loadExt() {
  try {
    state.ext = await api("GET", "/api/extensions");
  } catch (e) {
    state.ext = { mcp: [], skills: [] };
    toast(e.message, true);
  }
  try {
    state.extSearch = await api("GET", "/api/extensions/search");
  } catch (e) {
    state.extSearch = null;
  }
}
