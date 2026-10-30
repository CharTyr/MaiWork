// MaiWork 网页 · 设置 ·「扩展」：MCP、技能、联网搜索。
import { state } from "../state.js";
import { SVG, dayWord, esc, ico, tip, tipLabel, toast } from "../util.js";
import { api } from "../api.js";
import { loading } from "../pages/news.js";
import { loadOptDeps, optDepsSection } from "./optdeps.js";

export const ROLE_NAMES = { worker: "子 agent", main: "主模型" };

export function extPage() {
  const x = state.ext;
  if (!x) return loading((state.loadErr || {}).ext, "ext");
  const ed = state.extEdit;
  const mcp = x.mcp || [];
  const skills = x.skills || [];
  const newMcp = ed && ed.kind === "mcp" && !ed.name;
  const pasting = ed && ed.kind === "mcp-paste";
  const newSkill = ed && ed.kind === "skill" && !ed.name;
  return `
    ${searchCard()}
    <div class="h-sub-row"><h2 class="h-sub">MCP${tip("MCP 服务为 MaiWork 提供外部工具，例如搜索、读取网页。仅支持远程（https）服务。")}</h2>${newMcp || pasting ? "" : `<span class="row-btns"><button class="btn small" data-act="mcp-paste">粘贴配置</button><button class="btn small" data-act="ext-new" data-kind="mcp">添加</button></span>`}</div>
    ${pasting ? mcpPasteForm() : ""}
    ${newMcp ? mcpForm(null) : ""}
    ${mcp.map((m) => (ed && ed.kind === "mcp" && ed.name === m.name ? mcpForm(m) : mcpRow(m))).join("")}
    ${presetsNotAdded().map(presetRow).join("")}
    ${!mcp.length && !presetsNotAdded().length && !newMcp ? `<p class="h-meta">暂无</p>` : ""}
    <div class="h-sub-row"><h2 class="h-sub">skill${tip("skill 是给模型的操作说明，例如某类工作的做法和注意事项。模型会根据「简介」决定何时使用。")}</h2>${newSkill ? "" : `<span class="row-btns"><label class="btn small file-btn">上传 zip<input type="file" id="skill-zip" accept=".zip,application/zip" hidden /></label><button class="btn small" data-act="ext-new" data-kind="skill">添加</button></span>`}</div>
    ${newSkill ? skillForm(null) : ""}
    ${skills.length ? skills.map((k) => (ed && ed.kind === "skill" && ed.name === k.name ? skillForm(k) : skillRow(k))).join("") : newSkill ? "" : `<p class="h-meta">暂无</p>`}
    ${optDepsSection()}`;
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
  const head = `<div class="h-sub-row"><h2 class="h-sub">联网搜索${tip("选择用于联网搜索的服务和工具。搜索偏好可写入 AGENTS.md。")}</h2>${!editing ? `<span class="row-btns"><button class="btn small" data-act="search-edit">更换</button></span>` : ""}</div>`;
  const stLine = `<div class="set-text ext-st"><span class="dot ${st.ok ? "ok" : b ? "failed" : ""}"></span>${esc(b ? st.text || "" : "未设置")}</div>`;
  if (!editing) {
    const exMcp = b.extract_mcp || b.mcp;
    return `${head}
      <div class="ext-row search-now">
        ${presetByEntry(b.mcp) ? logoImg(presetByEntry(b.mcp)) : ico("magnifier")}
        <div class="ext-main">
          <div class="set-name">${esc(labelOf(b.mcp))} · <span class="mono">${esc(b.tool)}</span></div>
          ${b.extract_tool ? `<div class="set-text">网页读取：${esc(exMcp)} · <span class="mono">${esc(b.extract_tool)}</span></div>` : ""}
          ${extraProviders(b)}
          ${stLine}
        </div>
      </div>`;
  }
  if (!cands.length) {
    return `${head}${stLine}<p class="h-meta">请先在下方开启一个搜索服务</p>`;
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
    `<option value="${esc(t.mcp + " · " + t.name)}" data-mcp="${esc(t.mcp)}" data-tool="${esc(t.name)}" ${sel ? "selected" : ""}>${esc(t.mcp)} · ${esc(t.name)}${t.guess === hint ? (hint === "search" ? "（搜索）" : "（网页读取）") : ""}</option>`;
  return `${head}${b ? stLine : ""}
    <div class="login ext-form search-form">
      <label for="sx-tool">搜索工具</label>
      <select id="sx-tool">${all.map((t) => opt(t, same(t, pick), "search")).join("")}</select>
      ${tipLabel("sx-extract", "网页读取工具（可选）", "用于读取网页正文，可以与搜索工具来自不同服务。")}
      <select id="sx-extract"><option value="" ${exPick ? "" : "selected"}>不使用</option>${all.map((t) => opt(t, same(t, exPick), "extract")).join("")}</select>
      <div class="lbl-row"><label>备用服务</label>${tip("主搜索服务不可用时，按顺序改用这些服务。")}</div>
      ${providerChecks("sx-fb", b && b.fallback, "")}
      <div class="lbl-row"><label>同时搜索</label>${tip("搜集资讯时同时使用这些服务，可提高覆盖面，搜索次数会相应增加。")}</div>
      ${providerChecks("sx-br", b && b.broad, "")}
      <div class="actions">
        <button class="btn primary" data-act="search-save">保存</button>
        ${b ? `<button class="btn" data-act="search-cancel">取消</button><button class="btn danger" data-act="search-off">关闭联网搜索</button>` : ""}
      </div>
    </div>`;
}

// 备用 / 广撒网：只认已打开的预设服务（后端按预设写死工具和参数）
const searchBinding = () => (state.extSearch && state.extSearch.binding) || null;
function searchRoleTags(name) {
  const b = searchBinding();
  if (!b) return "";
  const out = [];
  if ((b.fallback || []).includes(name)) out.push(`<span class="tag">备用</span>`);
  if ((b.broad || []).includes(name)) out.push(`<span class="tag">同时搜索</span>`);
  return out.join("");
}
const enabledPresets = () => presetList().filter((p) => p.entry && p.enabled);
const labelOf = (name) => {
  const p = presetByEntry(name);
  return p ? p.label : name;
};
function extraProviders(b) {
  const fb = (b.fallback || []).map(labelOf);
  const br = (b.broad || []).map(labelOf);
  return `${fb.length ? `<div class="set-text">备用服务：${esc(fb.join("、"))}</div>` : ""}${br.length ? `<div class="set-text">同时搜索：${esc(br.join("、"))}</div>` : ""}`;
}
function providerChecks(cls, picked, hint) {
  const list = enabledPresets();
  if (!list.length) return `<p class="fine" style="margin:0">暂无已开启的搜索服务</p>`;
  return `<div class="prov-checks">${list
    .map(
      (p) =>
        `<label class="prov-chk"><input type="checkbox" class="${cls}" value="${esc(p.entry)}" ${(picked || []).includes(p.entry) ? "checked" : ""} />${logoImg(p, "prov-logo")}<span>${esc(p.label)}</span></label>`
    )
    .join("")}</div>${hint ? `<p class="fine" style="margin:0">${hint}</p>` : ""}`;
}
export const checkedProviders = (cls, main) => [...document.querySelectorAll(`input.${cls}:checked`)].map((i) => i.value).filter((v) => v && v !== main);

// 下拉当前选中的 {mcp, tool}（id = sx-tool 搜索 / sx-extract 抓正文；选「不用」→ 两个都是 ""）
export function pickedTool(id) {
  const sel = document.getElementById(id);
  const o = sel && sel.selectedOptions && sel.selectedOptions[0];
  return { mcp: (o && o.dataset.mcp) || "", tool: (o && o.dataset.tool) || "" };
}

// ───────── 预设的搜索服务（后端 GET /api/extensions/presets）─────────
// 已经接上的预设就是 MCP 列表里的普通一行（换成各家 logo、多一个「密钥」按钮）；
// 还没接的也列在 MCP 列表里，点「打开」就接上，免费的密钥可填可不填，要密钥的给官网链接。
const presetList = () => (state.extPresets && state.extPresets.presets) || [];
export const presetByEntry = (name) => presetList().find((p) => p.entry === name) || null;
const presetsNotAdded = () => presetList().filter((p) => !p.entry);
const logoImg = (p, cls = "ico ext-logo") => `<img class="${cls}" src="${esc(p.logo)}" alt="" loading="lazy" />`;
const freeTag = (p) => (p.free ? `<span class="ntag ok">免费可用</span>` : `<span class="ntag warn">需要密钥</span>`);

function presetRow(p) {
  const editing = state.presetEdit === p.id;
  return `
    <div class="ext-row preset-row${editing ? " is-open" : ""}">
      ${logoImg(p)}
      <div class="ext-main">
        <div class="set-name">${esc(p.label)}<span class="tag">预设</span>${freeTag(p)}</div>
        <div class="set-text">${esc(p.free_note || "")}</div>
        <div class="set-text ext-st"><span class="dot"></span>未开启 · 搜索服务</div>
        ${editing ? presetKeyForm(p) : ""}
      </div>
      <div class="row-btns ext-btns">${editing ? "" : `<button class="btn small" data-act="preset-open" data-id="${esc(p.id)}">${p.free ? "开启" : "填写密钥并开启"}</button>`}</div>
    </div>`;
}

// 填密钥的小表单：已接上的改密钥 / 没接上的打开时填（免费的可以不填）
function presetKeyForm(p) {
  const added = !!p.entry;
  const ph = p.key_set ? "已填写，留空则不修改" : p.free ? "可留空，使用免费额度" : "输入密钥";
  return `
    <div class="preset-form login" data-id="${esc(p.id)}">
      <label for="pk-key">API 密钥 ${p.free ? `<small class="lbl-hint">可选</small>` : ""}</label>
      <input id="pk-key" type="password" autocomplete="new-password" spellcheck="false" placeholder="${esc(ph)}" />
      <p class="fine preset-get"><a href="${esc(p.key_page_url)}" target="_blank" rel="noopener noreferrer">前往 ${esc(p.label)} 官网获取密钥 ↗</a></p>
      <p class="err" id="pk-err" hidden></p>
      <div class="actions">
        <button type="button" class="btn primary small" data-act="preset-save" data-id="${esc(p.id)}">${added ? "保存" : "开启"}</button>
        <button type="button" class="btn small" data-act="preset-cancel">取消</button>
        ${added && p.free && p.key_set ? `<button type="button" class="btn small ghost" data-act="preset-clear-key" data-id="${esc(p.id)}">清除密钥</button>` : ""}
      </div>
    </div>`;
}

function mcpRow(m) {
  const p = presetByEntry(m.name);
  const st = !m.enabled
    ? `<span class="dot"></span>已关闭`
    : m.ok
      ? `<span class="dot ok"></span>已连接 · ${m.tools || 0} 个工具`
      : `<span class="dot failed"></span>连接失败：${esc(m.error || "")}`;
  const roles = roleText(m.roles);
  const hdrs = (m.header_names || []).length ? ` · 请求头 ${m.header_names.map(esc).join("、")}` : "";
  return `
    <div class="ext-row${p ? " preset-row" : ""}">
      ${p ? logoImg(p) : ico("link")}
      <div class="ext-main">
        <div class="set-name">${p ? esc(p.label) : esc(m.name)}${p ? `<span class="tag">预设</span>` : ""}${m.source === "config" ? `<span class="tag">来自配置</span>` : ""}${m.search_role === "search" ? `<span class="tag">搜索</span>` : m.search_role === "extract" ? `<span class="tag">网页读取</span>` : ""}${searchRoleTags(m.name)}</div>
        ${p ? `<div class="set-text">${p.key_set ? "已填写密钥" : esc(p.free_note || "")}</div>` : `<div class="set-text mono-link">${esc(m.url || "")}</div>`}
        <div class="set-text ext-st">${st} · ${esc(roles)}可用${p ? "" : hdrs}</div>
        ${p && state.presetEdit === p.id ? presetKeyForm(p) : ""}
        ${
          (m.tool_names || []).length
            ? `<div class="ext-tools">${m.tool_names.slice(0, 12).map((t) => `<span class="ntag">${esc(t)}</span>`).join("")}${m.tool_names.length > 12 ? `<span class="ntag">共 ${m.tool_names.length} 个</span>` : ""}</div>`
            : ""
        }
      </div>
      <div class="row-btns ext-btns">
        <button class="btn small" data-act="mcp-toggle" data-name="${esc(m.name)}" data-on="${m.enabled ? "0" : "1"}">${m.enabled ? "关闭" : "开启"}</button>
        ${m.enabled ? `<button class="btn small" data-act="mcp-reload" data-name="${esc(m.name)}">重新连接</button>` : ""}
        ${p && m.source !== "config" && state.presetEdit !== p.id ? `<button class="btn small" data-act="preset-open" data-id="${esc(p.id)}">密钥</button>` : ""}
        ${m.source === "config" ? "" : `<button class="btn small" data-act="ext-edit" data-kind="mcp" data-name="${esc(m.name)}">修改</button>`}
      </div>
    </div>`;
}

function mcpPasteForm() {
  return `
    <form id="mcp-paste-form" class="login ext-form" autocomplete="off">
      <div class="ext-form-h">粘贴 MCP 配置${tip("支持常见的 MCP 配置格式，仅限远程服务（http / sse）。")}</div>
      <textarea id="mp-text" class="mono-area" rows="9" spellcheck="false" placeholder='{"mcpServers": {"tavily": {"type": "http", "url": "https://…/mcp", "headers": {"Authorization": "Bearer …"}}}}'></textarea>
      <p class="err" id="mp-err" hidden></p>
      <div class="actions">
        <button class="btn primary" type="submit">识别</button>
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
      throw new Error("格式不正确，请检查括号和逗号。");
    }
  }
  if (!data || typeof data !== "object") throw new Error("未识别到配置。");
  const isServer = (o) => o && typeof o === "object" && (o.url || o.serverUrl || o.command);
  let entries;
  if (isServer(data)) entries = [["", data]];
  else {
    const map = data.mcpServers || data.servers || (data.mcp && data.mcp.servers) || data;
    entries = Object.entries(map).filter(([, v]) => isServer(v));
  }
  if (!entries.length) throw new Error("未找到地址（url）。");
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
    if (v.command) return { name: name || "mcp", skipped: "不支持本地命令" };
    if (!/^https:\/\//i.test(url)) return { name: name || "mcp", skipped: "非 https 地址" };
    const headers = {};
    Object.entries(v.headers || v.requestInit?.headers || {}).forEach(([k, val]) => {
      if (typeof val === "string") headers[k] = val;
    });
    return { name: name || "mcp", url, headers };
  });
}

// 给谁用：三选一（主模型 / 子 agent / 两边都能用）
const ROLE_PICKS = [["main", "仅主模型", "用于对话和检查"], ["worker", "仅子 agent", "用于执行任务"], ["both", "全部", ""]];
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
      <input class="hdr-name" spellcheck="false" placeholder="名称，例如 Authorization" value="${esc(name || "")}" ${isOld ? "readonly" : ""} />
      <input class="hdr-val" type="password" autocomplete="new-password" placeholder="${isOld ? "已填写，留空则不修改" : "值，例如 Bearer …"}" value="${esc(value || "")}" />
      <button type="button" class="icon-btn" data-act="hdr-del" aria-label="删除">${SVG.trash}</button>
    </div>`;
}

function mcpForm(m) {
  const isNew = !m;
  const pre = (isNew && state.extEdit && state.extEdit.prefill) || null;
  m = m || { roles: ["worker"], enabled: true, timeout_s: 30, header_names: [], tools_filter: [], ...(pre || {}) };
  return `
    <form id="mcp-form" class="login ext-form" autocomplete="off" data-name="${esc(isNew ? "" : m.name)}">
      <div class="ext-form-h">${isNew ? "添加 MCP" : `修改 ${esc(m.name)}`}</div>
      ${pre ? `` : ""}
      ${isNew ? `<label for="x-name">名称</label><input id="x-name" spellcheck="false" placeholder="字母、数字、_ 或 -，例如 github" value="${esc(m.name || "")}" />` : ""}
      <label for="x-url">地址</label>
      <input id="x-url" type="url" inputmode="url" spellcheck="false" value="${esc(m.url || "")}" placeholder="https://…/mcp" />
      <div class="lbl-row"><label>请求头</label>${tip("请求头的值保存后不再显示。")}</div>
      <div id="x-hdrs">${isNew ? Object.entries(m.headers || {}).map(([h, v]) => headerRow(h, false, v)).join("") : (m.header_names || []).map((h) => headerRow(h, true)).join("")}</div>
      <button type="button" class="btn small" data-act="hdr-add" style="align-self:flex-start">添加请求头</button>
      ${tipLabel("x-tools", "限定工具", "留空表示使用全部工具；多个工具用逗号分隔。")}
      <input id="x-tools" spellcheck="false" value="${esc((m.tools_filter || []).join(", "))}" />
      <label>使用范围</label>
      ${roleChecks("x", m.roles)}
      <label for="x-timeout">超时（秒）</label>
      <input id="x-timeout" type="number" min="5" max="300" value="${esc(m.timeout_s || 30)}" />
      <label class="chk"><input type="checkbox" id="x-enabled" ${m.enabled !== false ? "checked" : ""} />启用</label>
      <div class="actions"><button type="button" class="btn" data-act="mcp-test">测试连接</button><span class="fine" id="x-check" style="margin:0;align-self:center"></span></div>
      <p class="err" id="x-err" hidden></p>
      <div class="actions">
        <button class="btn primary" type="submit">保存</button>
        <button type="button" class="btn" data-act="ext-cancel">取消</button>
        ${isNew ? "" : `<button type="button" class="btn danger" data-act="mcp-del" data-name="${esc(m.name)}">删除</button>`}
      </div>
    </form>`;
}

// ───────── skill 的「要用的工具」（allowed-tools）─────────
// 挂到资讯 / 构想 / 目标后，这些岗位会带上这里勾选的工具；候选来自 GET /api/agents 的 worker_tool_choices。
// 清单没加载到（state.toolChoices 不是数组）时只列已声明的，不断言「未注册」。
function toolChk(name, t, on, loaded) {
  const tags = t
    ? `${t.exec_class ? `<span class="ntag warn">执行类</span>` : ""}${t.task_only ? `<span class="ntag">仅派活时</span>` : ""}`
    : loaded ? `<span class="ntag warn">未注册</span>` : "";
  const label = t && t.label && t.label !== name ? `<span class="fine">${esc(t.label)}</span>` : "";
  return `<label class="chk tool-chk"><input type="checkbox" value="${esc(name)}"${on ? " checked" : ""} /><span class="mono">${esc(name)}</span>${label}${tags}</label>`;
}
function toolChecks(selected) {
  const loaded = Array.isArray(state.toolChoices);
  const choices = loaded ? state.toolChoices : [];
  const sel = new Set(selected || []);
  const known = new Set(choices.map((t) => t.name));
  const rows = [
    ...[...sel].filter((n) => !known.has(n)).map((n) => toolChk(n, null, true, loaded)),
    ...choices.map((t) => toolChk(t.name, t, sel.has(t.name), loaded)),
  ];
  return rows.length ? `<div id="k-tools" class="tool-checks">${rows.join("")}</div>` : `<p class="fine" style="margin:0">暂无可用工具</p>`;
}
const toolTags = (names) => ((names || []).length ? `<div class="ext-tools">${names.map((t) => `<span class="ntag">${esc(t)}</span>`).join("")}</div>` : "");

// 服务端判定「带执行类工具，需要确认」：400 且错误里提到执行类工具（skill 增改 / 上传、专岗挂 skill 共用）
export const execConfirmNeeded = (ex) => !!ex && ex.status === 400 && /执行类工具/.test(ex.message || "");
export function confirmExecTools(ex) {
  const m = /（([^）]+)）/.exec((ex && ex.message) || "");
  return confirm(`所选 skill 带执行类工具${m ? `（${m[1]}）` : ""}。挂到资讯、构想或目标后，这些岗位自动运行时会使用这些工具：每轮在单独的工作区运行、结束后清空，但可以访问外网。确认继续？`);
}

function skillRow(k) {
  const roles = roleText(k.roles);
  const active = k.enabled !== false;
  const manual = k.manual_enabled !== false;
  const status = active ? "已启用" : k.disabled_reason || "已停用";
  return `
    <div class="ext-row">
      ${ico("books")}
      <div class="ext-main">
        <div class="set-name">${esc(k.name)}${k.source === "file" ? `<span class="tag">服务器文件</span>` : k.source === "builtin" ? `<span class="tag">内置</span>` : ""}</div>
        <div class="set-text">${esc(k.description || "无简介")}</div>
        <div class="set-text ext-st"><span class="dot ${active ? "ok" : ""}"></span>${esc(status)} · ${esc(roles)}可用${k.updated_ts ? ` · ${esc(dayWord(k.updated_ts))}修改` : ""}</div>
        ${toolTags(k.allowed_tools)}
      </div>
      <div class="row-btns ext-btns"><button class="btn small" data-act="skill-toggle" data-name="${esc(k.name)}" data-on="${manual ? "0" : "1"}">${manual ? "停用" : "启用"}</button><button class="btn small" data-act="ext-edit" data-kind="skill" data-name="${esc(k.name)}">${k.source === "builtin" ? "查看" : "修改"}</button></div>
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
      <p class="fine" style="margin:0">内置 skill，仅可查看 · ${esc(roleText(d.roles))}可用</p>
      <label>简介</label>
      <p class="set-text" style="margin:0">${esc(d.description || "")}</p>
      <label for="k-body">内容</label>
      <textarea id="k-body" class="mono-area" rows="14" readonly spellcheck="false">${esc(d.body || "")}</textarea>
      ${(d.allowed_tools || []).length ? `<label>要用的工具</label>${toolTags(d.allowed_tools)}` : ""}
      ${(d.files || []).length ? `<p class="fine" style="margin:0">附件：${d.files.map(esc).join("、")}</p>` : ""}
      <div class="actions"><button type="button" class="btn" data-act="ext-cancel">关闭</button></div>
    </div>`;
  }
  return `
    <form id="skill-form" class="login ext-form" autocomplete="off" data-name="${esc(isNew ? "" : k.name)}">
      <div class="ext-form-h">${isNew ? "添加 skill" : `修改 ${esc(k.name)}`}</div>
      ${isNew ? `<label for="k-name">名称</label><input id="k-name" spellcheck="false" placeholder="字母、数字、_ 或 -，例如 write-report" />` : ""}
      ${tipLabel("k-desc", "简介", "模型根据简介判断何时使用该 skill。")}
      <input id="k-desc" value="${esc(d.description || "")}" placeholder="例如：周报格式和注意事项" />
      <label>使用范围</label>
      ${roleChecks("k", d.roles)}
      <div class="lbl-row"><label>要用的工具</label>${tip("挂到资讯、构想或目标后，这些岗位会带上勾选的工具，并按本 skill 的内容使用。执行类工具可以运行命令或写文件，保存时需要确认。")}</div>
      ${toolChecks(d.allowed_tools)}
      ${tipLabel("k-body", "内容", "写明做法、注意事项和示例，最多 40KB。")}
      <textarea id="k-body" class="mono-area" rows="14" spellcheck="false">${esc(d.body || "")}</textarea>
      ${(d.files || []).length ? `<p class="fine" style="margin:0">附件：${d.files.map(esc).join("、")}</p>` : ""}
      <p class="err" id="k-err" hidden></p>
      <div class="actions">
        <button class="btn primary" type="submit">保存</button>
        <button type="button" class="btn" data-act="ext-cancel">取消</button>
        ${isNew ? "" : `<button type="button" class="btn danger" data-act="skill-del" data-name="${esc(k.name)}">删除</button>`}
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
    if (state.loadErr) delete state.loadErr.ext;
  } catch (e) {
    // 取不到不能画成「暂无」：留空，让页面显示失败原因和重试
    state.ext = null;
    (state.loadErr = state.loadErr || {}).ext = e.message;
    toast(e.message, true);
  }
  try {
    state.extSearch = await api("GET", "/api/extensions/search");
  } catch (e) {
    state.extSearch = null;
  }
  try {
    state.extPresets = await api("GET", "/api/extensions/presets");
  } catch (e) {
    state.extPresets = null;
  }
  try {
    state.toolChoices = (await api("GET", "/api/agents")).worker_tool_choices || [];
  } catch (e) {
    state.toolChoices = null;
  }
  await loadOptDeps();
}
