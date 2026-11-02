// MaiWork 网页 · 设置 ·「搜索与网页读取」：统一预设搜索行、一键设为主用、实测检测、抽屉密钥与网页读取配置。
import { $, state } from "../state.js";
import { esc, ico, tip, tipLabel, toast } from "../util.js";
import { api } from "../api.js";
import { repaintSheet } from "../sheet.js";
import { loadExt } from "./ext.js";

export const searchUi = { busy: "", checks: {}, errors: {} };

const presetList = () => (state.extPresets && state.extPresets.presets) || [];
export const presetByEntry = (name) => presetList().find((p) => p.entry === name) || null;
const presetById = (id) => presetList().find((p) => p.id === id) || null;
const enabledPresets = () => presetList().filter((p) => p.entry && p.enabled);
const searchBinding = () => (state.extSearch && state.extSearch.binding) || null;
export const presetLogo = (p, cls = "ico ext-logo") => `<img class="${cls}" src="${esc(p.logo)}" alt="" loading="lazy" />`;
const freeTag = (p) => (p.free ? `<span class="ntag ok">免费可用</span>` : `<span class="ntag warn">需要密钥</span>`);

const labelOf = (name) => {
  const p = presetByEntry(name);
  return p ? p.label : name;
};

export function searchRoleTags(name) {
  const b = searchBinding();
  if (!b) return "";
  const out = [];
  const fbIdx = (b.fallback || []).indexOf(name);
  if (fbIdx >= 0) out.push(`<span class="tag">备用 #${fbIdx + 1}</span>`);
  if ((b.broad || []).includes(name)) out.push(`<span class="tag">同时搜索</span>`);
  return out.join("");
}

function pickedTool(id) {
  const sel = $(id);
  const o = sel && sel.selectedOptions && sel.selectedOptions[0];
  return { mcp: (o && o.dataset && o.dataset.mcp) || "", tool: (o && o.dataset && o.dataset.tool) || "" };
}

function syncChainSelects(ed) {
  if (!ed || ed.kind !== "chain") return;
  if ($("sx-tool")) {
    const main = pickedTool("sx-tool");
    if (main.mcp) {
      ed.mcp = main.mcp;
      ed.tool = main.tool;
      ed.fallback = (ed.fallback || []).filter((n) => n !== main.mcp);
      ed.broad = (ed.broad || []).filter((n) => n !== main.mcp);
    }
  }
  if ($("sx-extract")) {
    const ex = pickedTool("sx-extract");
    ed.extract_mcp = ex.mcp;
    ed.extract_tool = ex.tool;
  }
}

function chainSummaryRow() {
  const sx = state.extSearch;
  if (!sx) return "";
  const b = sx.binding;
  const st = sx.status || {};
  if (!b) {
    return `<div class="set-group"><div class="set-row social-row">${ico("magnifier")}<div><div class="set-name">当前未启用联网搜索</div><div class="set-text">在下方启用任一搜索服务后自动生效</div></div><span class="row-btns"><button class="btn small" type="button" data-act="search-chain-open">高级绑定</button></span></div></div>`;
  }
  const p = presetByEntry(b.mcp);
  const fb = (b.fallback || []).map((n, i) => `${i + 1}. ${labelOf(n)}`);
  const br = (b.broad || []).map(labelOf);
  const stDot = st.ok ? "ok" : "failed";
  return `
    <div class="set-group">
      <div class="set-row social-row">
        ${p ? presetLogo(p) : ico("magnifier")}
        <div>
          <div class="set-name">主用：${esc(labelOf(b.mcp))}${p ? "" : ` · <span class="mono">${esc(b.tool)}</span>`}</div>
          <div class="set-text ext-st"><span class="dot ${stDot}"></span>${esc(st.text || "")}</div>
          ${fb.length ? `<div class="set-text">备用顺序：${esc(fb.join(" → "))}</div>` : ""}
          ${br.length ? `<div class="set-text">同时搜索：${esc(br.join("、"))}</div>` : ""}
        </div>
        <span class="row-btns">
          <button class="btn small" type="button" data-act="search-chain-open">链路与多源</button>
          <button class="btn small" type="button" data-act="search-off">停用搜索</button>
        </span>
      </div>
    </div>`;
}

function presetServiceRow(p) {
  const b = searchBinding();
  const isPrimary = !!(b && p.entry && b.mcp === p.entry && p.enabled);
  const fbIdx = b && p.entry ? (b.fallback || []).indexOf(p.entry) : -1;
  const inBroad = !!(b && p.entry && (b.broad || []).includes(p.entry));
  const busy = searchUi.busy === p.id;
  const chk = searchUi.checks[p.id];
  const rowErr = searchUi.errors[p.id] || (p.enabled && !p.ok ? p.error : "");
  const badges = [
    isPrimary ? `<span class="ntag ok">主用</span>` : "",
    fbIdx >= 0 ? `<span class="tag">备用 #${fbIdx + 1}</span>` : "",
    inBroad ? `<span class="tag">同时搜索</span>` : "",
    p.source === "config" ? `<span class="tag">配置文件</span>` : "",
    !p.enabled ? freeTag(p) : "",
  ].join("");

  let statusLine = "";
  if (!p.enabled) {
    const quota = p.key_set ? "密钥已填写" : p.free_note || (p.free ? "免费可用" : "需填写密钥");
    statusLine = `<span class="dot"></span>未启用 · ${esc(quota)}`;
  } else if (p.ok && !rowErr) {
    const quota = p.key_set ? "自有密钥" : "免费额度";
    const checkNote = chk ? ` · 检测通过（${chk.count} 条，${chk.ms}ms）` : "";
    statusLine = `<span class="dot ok"></span>已连接 · ${esc(quota)}${esc(checkNote)}`;
  } else {
    const quota = p.key_set ? "自有密钥" : "免费额度";
    statusLine = `<span class="dot failed"></span>连接异常 · ${esc(quota)}`;
  }

  let btns = "";
  if (!p.enabled) {
    if (p.free || p.key_set) {
      btns += `<button class="btn primary small" type="button" data-act="preset-enable" data-id="${esc(p.id)}" ${searchUi.busy ? "disabled" : ""}>启用</button>`;
      if (p.source !== "config") {
        btns += `<button class="btn small" type="button" data-act="preset-key" data-id="${esc(p.id)}" ${searchUi.busy ? "disabled" : ""}>${p.key_set ? "密钥" : "填写密钥"}</button>`;
      }
    } else {
      btns += `<button class="btn primary small" type="button" data-act="preset-key" data-id="${esc(p.id)}" ${searchUi.busy ? "disabled" : ""}>填写密钥并启用</button>`;
    }
  } else {
    if (!isPrimary) {
      btns += `<button class="btn small" type="button" data-act="preset-primary" data-id="${esc(p.id)}" ${searchUi.busy ? "disabled" : ""}>设为主用</button>`;
    }
    btns += `<button class="btn small" type="button" data-act="preset-test" data-id="${esc(p.id)}" ${searchUi.busy ? "disabled" : ""}>${busy ? "正在检测…" : "检测"}</button>`;
    if (p.source !== "config") {
      btns += `<button class="btn small" type="button" data-act="preset-key" data-id="${esc(p.id)}" ${searchUi.busy ? "disabled" : ""}>${p.key_set ? "密钥" : "填写密钥"}</button>`;
    }
    btns += `<button class="btn small" type="button" data-act="preset-disable" data-id="${esc(p.id)}" data-entry="${esc(p.entry || "")}" ${searchUi.busy ? "disabled" : ""}>停用</button>`;
  }

  return `
    <div class="set-row social-row preset-row">
      ${presetLogo(p)}
      <div>
        <div class="set-name">${esc(p.label)}${badges}</div>
        <div class="set-text ext-st" role="status">${statusLine}</div>
        ${rowErr ? `<div class="warn-box" role="status">${esc(rowErr)}</div>` : ""}
      </div>
      <span class="row-btns">${btns}</span>
    </div>`;
}

function readerSection() {
  const sx = state.extSearch;
  if (!sx) return "";
  const rd = sx.reader || { jina_enabled: true, jina_key_set: false, ok: true, text: "" };
  const b = sx.binding;
  const exMcp = b && (b.extract_mcp || b.mcp);
  const exText = b && b.extract_tool
    ? `当前使用 ${labelOf(exMcp)} · ${b.extract_tool}`
    : "未指定 · Jina 失败时直接抓取网页";
  const jinaWarn = rd.jina_enabled && rd.ok === false && rd.text ? `<div class="warn-box" role="status">${esc(rd.text)}</div>` : "";
  const jinaSub = !rd.jina_enabled ? "已停用 · 跳过 Jina Reader" : rd.ok !== false && rd.text ? rd.text : "已启用";
  return `
    <div class="h-sub-row"><h2 class="h-sub">网页读取${tip("读取网页正文时按「Jina Reader → 网页读取工具 → 直接抓取」顺序执行，前一步失败时自动切换下一步。")}</h2></div>
    <div class="set-group">
      <div class="set-row social-row">
        ${ico("books")}
        <div>
          <div class="set-name">Jina Reader<span class="tag">${rd.jina_key_set ? "500 次/分钟" : "免费 20 次/分钟"}</span></div>
          <div class="set-text ext-st"><span class="dot ${rd.jina_enabled ? (rd.ok === false ? "failed" : "ok") : ""}"></span>${esc(jinaSub)}</div>
          ${jinaWarn}
        </div>
        <span class="row-btns">
          <button class="btn small" type="button" data-act="jina-key">${rd.jina_key_set ? "密钥" : "填写密钥"}</button>
          <button class="btn small" type="button" data-act="jina-toggle" data-on="${rd.jina_enabled ? "0" : "1"}">${rd.jina_enabled ? "停用" : "启用"}</button>
        </span>
      </div>
      <div class="set-row social-row">
        ${ico("link")}
        <div>
          <div class="set-name">网页读取工具<span class="tag">备用读取</span></div>
          <div class="set-text">${esc(exText)}</div>
        </div>
        <span class="row-btns">
          <button class="btn small" type="button" data-act="search-chain-open" data-focus="extract">选择工具</button>
        </span>
      </div>
    </div>`;
}

export function searchSection() {
  const rows = presetList().map(presetServiceRow).join("");
  return `
    <div class="search-settings">
      <div class="h-sub-row"><h2 class="h-sub">搜索服务${tip("启用任一服务后可设为主用；「检测」会发起一次真实搜索查询，可识别密钥错误或配额耗尽。")}</h2></div>
      ${chainSummaryRow()}
      ${rows ? `<div class="set-group">${rows}</div>` : ""}
      ${readerSection()}
    </div>`;
}

function presetKeySheet(ed) {
  const p = presetById(ed.id);
  if (!p) return "";
  const ph = p.key_set ? "已填写，留空则保持不变" : p.free ? "可留空，使用免费额度" : "输入 API 密钥";
  const note = p.free_note || (p.free ? "支持免密钥使用；填写自有密钥可获得更高配额。" : "该服务需要填写 API 密钥后启用。");
  return `
    <h1 class="h-page">${esc(p.label)}</h1>
    <div class="login ext-form">
      <p class="fine" style="margin:0">${esc(note)}保存后密钥不会回显。</p>
      <p class="fine preset-get"><a href="${esc(p.key_page_url)}" target="_blank" rel="noopener noreferrer">前往 ${esc(p.label)} 官网获取密钥 ↗</a></p>
      <label for="pk-key">API 密钥${p.free ? ` <small class="lbl-hint">可选</small>` : ""}</label>
      <input id="pk-key" type="password" autocomplete="new-password" spellcheck="false" placeholder="${esc(ph)}" />
      <p class="err" id="pk-err" role="alert" hidden></p>
      <div class="actions">
        <button type="button" class="btn primary" data-act="preset-save" data-id="${esc(p.id)}">${p.enabled ? "保存" : "保存并启用"}</button>
        ${p.entry && p.free && p.key_set ? `<button type="button" class="btn danger" data-act="preset-clear-key" data-id="${esc(p.id)}">清除密钥</button>` : ""}
        <button type="button" class="btn" data-act="search-sheet-cancel">取消</button>
      </div>
    </div>`;
}

function jinaKeySheet() {
  const rd = (state.extSearch && state.extSearch.reader) || {};
  return `
    <h1 class="h-page">Jina Reader 密钥</h1>
    <div class="login ext-form">
      <p class="fine" style="margin:0">未填写密钥时每分钟可调用约 20 次（按服务器 IP 计）；填写密钥后每分钟可调用 500 次。保存后不回显。</p>
      <p class="fine preset-get"><a href="https://jina.ai/reader/" target="_blank" rel="noopener noreferrer">前往 Jina AI 官网免费申请密钥 ↗</a></p>
      <label for="jina-key-input">Jina API 密钥</label>
      <input id="jina-key-input" type="password" autocomplete="new-password" spellcheck="false" placeholder="${rd.jina_key_set ? "已填写，输入新密钥可替换" : "输入 Jina API 密钥"}" />
      <p class="err" id="jina-key-err" role="alert" hidden></p>
      <div class="actions">
        <button type="button" class="btn primary" data-act="jina-key-save">保存</button>
        ${rd.jina_key_set ? `<button type="button" class="btn danger" data-act="jina-key-clear">清除密钥</button>` : ""}
        <button type="button" class="btn" data-act="search-sheet-cancel">取消</button>
      </div>
    </div>`;
}

function fallbackOrderList(ed, mainMcp) {
  const avail = enabledPresets().filter((p) => p.entry !== mainMcp);
  if (!avail.length) return `<p class="fine" style="margin:0">暂无其他已启用的预设搜索服务</p>`;
  const fb = (ed.fallback || []).filter((n) => avail.some((p) => p.entry === n));
  const rest = avail.filter((p) => !fb.includes(p.entry));
  const checkedRows = fb.map((name, idx) => {
    const p = presetByEntry(name);
    if (!p) return "";
    return `
      <div class="fb-order-row is-on">
        <button type="button" class="prov-chk is-on" data-act="sx-fb-toggle" data-name="${esc(name)}" aria-pressed="true">${presetLogo(p, "prov-logo")}<span>#${idx + 1} ${esc(p.label)}</span></button>
        <span class="fb-move-btns">
          <button type="button" class="btn small" data-act="sx-fb-move" data-name="${esc(name)}" data-dir="-1" aria-label="上移 ${esc(p.label)}" ${idx === 0 ? "disabled" : ""}>↑</button>
          <button type="button" class="btn small" data-act="sx-fb-move" data-name="${esc(name)}" data-dir="1" aria-label="下移 ${esc(p.label)}" ${idx === fb.length - 1 ? "disabled" : ""}>↓</button>
        </span>
      </div>`;
  });
  const uncheckedRows = rest.map(
    (p) => `<div class="fb-order-row"><button type="button" class="prov-chk" data-act="sx-fb-toggle" data-name="${esc(p.entry)}" aria-pressed="false">${presetLogo(p, "prov-logo")}<span>${esc(p.label)}</span></button></div>`
  );
  return `<div class="fb-order-list">${[...checkedRows, ...uncheckedRows].join("")}</div>`;
}

function broadToggleList(ed, mainMcp) {
  const avail = enabledPresets().filter((p) => p.entry !== mainMcp);
  if (!avail.length) return `<p class="fine" style="margin:0">暂无其他已启用的预设搜索服务</p>`;
  const br = ed.broad || [];
  return `<div class="prov-checks">${avail
    .map((p) => {
      const on = br.includes(p.entry);
      return `<button type="button" class="prov-chk${on ? " is-on" : ""}" data-act="sx-br-toggle" data-name="${esc(p.entry)}" aria-pressed="${on}">${presetLogo(p, "prov-logo")}<span>${esc(p.label)}</span></button>`;
    })
    .join("")}</div>`;
}

function chainFormSheet(ed) {
  const sx = state.extSearch || {};
  const b = sx.binding;
  const cands = (sx.candidates || []).filter((c) => (c.tools || []).length);
  if (!cands.length) {
    return `<h1 class="h-page">搜索链路与高级绑定</h1><p class="fine">暂无已连接并暴露工具的搜索服务，请先在列表里启用一个服务。</p>`;
  }
  const all = cands.flatMap((c) => c.tools.map((t) => ({ mcp: c.mcp, ...t })));
  const pick = ed.mcp && ed.tool
    ? all.find((t) => t.mcp === ed.mcp && t.name === ed.tool) || all[0]
    : all.find((t) => t.guess === "search") || all[0];
  const exPick = ed.extract_tool
    ? all.find((t) => t.mcp === (ed.extract_mcp || ed.mcp) && t.name === ed.extract_tool) || null
    : null;
  const same = (t, p) => !!p && t.mcp === p.mcp && t.name === p.name;
  const opt = (t, sel, hint) =>
    `<option value="${esc(t.mcp + " · " + t.name)}" data-mcp="${esc(t.mcp)}" data-tool="${esc(t.name)}" ${sel ? "selected" : ""}>${esc(labelOf(t.mcp))} · ${esc(t.name)}${t.guess === hint ? (hint === "search" ? "（搜索）" : "（网页读取）") : ""}</option>`;
  const curMain = (pick && pick.mcp) || ed.mcp || "";
  return `
    <h1 class="h-page">搜索链路与高级绑定</h1>
    <div class="login ext-form search-form">
      ${tipLabel("sx-tool", "主用搜索工具", "支持选用预设搜索服务或自定义 MCP 暴露的搜索工具。")}
      <select id="sx-tool">${all.map((t) => opt(t, same(t, pick), "search")).join("")}</select>
      ${tipLabel("sx-extract", "网页读取工具（可选）", "Jina Reader 读取失败时改用该工具；可与主搜索来自不同服务。")}
      <select id="sx-extract"><option value="" ${exPick ? "" : "selected"}>不使用</option>${all.map((t) => opt(t, same(t, exPick), "extract")).join("")}</select>
      <div class="lbl-row"><label>备用服务顺序</label>${tip("主用服务失败时按 #1、#2 顺序自动切换。点击服务可加入或移出备用，右侧箭头调整顺序。")}</div>
      ${fallbackOrderList(ed, curMain)}
      <div class="lbl-row"><label>同时搜索</label>${tip("搜集资讯时并行调用这些服务以提高覆盖面，搜索消耗相应增加。")}</div>
      ${broadToggleList(ed, curMain)}
      <p class="err" id="sx-err" role="alert" hidden></p>
      <div class="actions">
        <button type="button" class="btn primary" data-act="search-save">保存</button>
        <button type="button" class="btn" data-act="search-sheet-cancel">取消</button>
        ${b ? `<button type="button" class="btn danger" data-act="search-off">关闭联网搜索</button>` : ""}
      </div>
    </div>`;
}

export function searchFormSheet() {
  const ed = state.searchSheet;
  if (!ed) return "";
  if (ed.kind === "preset-key") return presetKeySheet(ed);
  if (ed.kind === "jina-key") return jinaKeySheet();
  if (ed.kind === "chain") return chainFormSheet(ed);
  return "";
}

async function refreshAfterWrite(reloadSettings) {
  await loadExt();
  if (state.settings && reloadSettings) await reloadSettings();
  repaintSheet();
}

export async function actSearch(a, el, reloadSettings) {
  switch (a) {
    case "preset-key":
    case "preset-open":
      state.extEdit = null;
      state.searchSheet = { kind: "preset-key", id: el.dataset.id };
      repaintSheet();
      setTimeout(() => $("pk-key") && $("pk-key").focus({ preventScroll: true }), 30);
      return true;
    case "jina-key":
      state.extEdit = null;
      state.searchSheet = { kind: "jina-key" };
      repaintSheet();
      setTimeout(() => $("jina-key-input") && $("jina-key-input").focus({ preventScroll: true }), 30);
      return true;
    case "search-chain-open":
    case "search-edit": {
      const b = searchBinding();
      state.extEdit = null;
      state.searchSheet = {
        kind: "chain",
        focus: (el.dataset && el.dataset.focus) || "",
        mcp: (b && b.mcp) || "",
        tool: (b && b.tool) || "",
        extract_mcp: (b && (b.extract_mcp || b.mcp)) || "",
        extract_tool: (b && b.extract_tool) || "",
        fallback: [...((b && b.fallback) || [])],
        broad: [...((b && b.broad) || [])],
      };
      repaintSheet();
      if (el.dataset && el.dataset.focus === "extract") {
        setTimeout(() => $("sx-extract") && $("sx-extract").focus({ preventScroll: true }), 30);
      }
      return true;
    }
    case "search-sheet-cancel":
    case "preset-cancel":
    case "search-cancel":
      state.searchSheet = null;
      repaintSheet();
      return true;
    case "sx-fb-toggle": {
      const ed = state.searchSheet;
      if (!ed || ed.kind !== "chain") return true;
      syncChainSelects(ed);
      const name = el.dataset.name;
      ed.fallback = ed.fallback.includes(name) ? ed.fallback.filter((n) => n !== name) : [...ed.fallback, name];
      repaintSheet();
      return true;
    }
    case "sx-fb-move": {
      const ed = state.searchSheet;
      if (!ed || ed.kind !== "chain") return true;
      syncChainSelects(ed);
      const name = el.dataset.name;
      const dir = Number(el.dataset.dir) || 0;
      const list = [...(ed.fallback || [])];
      const idx = list.indexOf(name);
      const next = idx + dir;
      if (idx >= 0 && next >= 0 && next < list.length) {
        [list[idx], list[next]] = [list[next], list[idx]];
        ed.fallback = list;
        repaintSheet();
      }
      return true;
    }
    case "sx-br-toggle": {
      const ed = state.searchSheet;
      if (!ed || ed.kind !== "chain") return true;
      syncChainSelects(ed);
      const name = el.dataset.name;
      ed.broad = ed.broad.includes(name) ? ed.broad.filter((n) => n !== name) : [...ed.broad, name];
      repaintSheet();
      return true;
    }
    case "preset-enable":
    case "preset-primary": {
      const id = el.dataset.id;
      const makePrimary = a === "preset-primary";
      el.disabled = true;
      try {
        const r = await api("POST", `/api/extensions/presets/${encodeURIComponent(id)}`, makePrimary ? { set_primary: true } : {});
        if (r && r.ok === false && r.error) searchUi.errors[id] = r.error;
        else delete searchUi.errors[id];
        await refreshAfterWrite(reloadSettings);
        if (r && r.ok === false && r.error) {
          toast(`${makePrimary ? "已设为主用，连接失败" : "已启用，连接失败"}：${r.error}`, true);
        } else {
          toast(makePrimary ? `已将 ${(r && r.label) || id} 设为主用` : r && r.bound ? `已启用并设为主用：${r.label || id}` : "已启用");
        }
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
      }
      return true;
    }
    case "preset-disable": {
      const id = el.dataset.id;
      const entry = el.dataset.entry;
      if (!entry) return true;
      el.disabled = true;
      try {
        await api("POST", `/api/extensions/mcp/${encodeURIComponent(entry)}/toggle`, { enabled: false });
        delete searchUi.errors[id];
        delete searchUi.checks[id];
        await refreshAfterWrite(reloadSettings);
        toast("已停用");
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
      }
      return true;
    }
    case "preset-test": {
      const id = el.dataset.id;
      if (searchUi.busy) return true;
      searchUi.busy = id;
      delete searchUi.errors[id];
      repaintSheet({ preserveSheet: true });
      try {
        const r = await api("POST", `/api/extensions/presets/${encodeURIComponent(id)}/test`, {});
        if (r && r.ok) {
          searchUi.checks[id] = { count: r.count || 0, ms: r.ms || 0 };
          toast(`检测通过（返回 ${r.count || 0} 条，${r.ms || 0}ms）`);
        } else {
          delete searchUi.checks[id];
          const msg = (r && r.error) || "检测失败";
          searchUi.errors[id] = msg;
          toast(msg, true);
        }
      } catch (err) {
        delete searchUi.checks[id];
        searchUi.errors[id] = err.message;
        toast(err.message, true);
      } finally {
        searchUi.busy = "";
        repaintSheet({ preserveSheet: true });
      }
      return true;
    }
    case "preset-save":
    case "preset-clear-key": {
      const id = el.dataset.id;
      const clear = a === "preset-clear-key";
      if (clear && !confirm("确定清除该密钥并改用免费额度？")) return true;
      const key = clear ? "" : (($("pk-key") && $("pk-key").value) || "").trim();
      const errEl = $("pk-err");
      if (errEl) errEl.hidden = true;
      el.disabled = true;
      try {
        const body = clear ? { clear_key: true } : key ? { key } : {};
        const r = await api("POST", `/api/extensions/presets/${encodeURIComponent(id)}`, body);
        state.searchSheet = null;
        if (r && r.ok === false && r.error) searchUi.errors[id] = r.error;
        else delete searchUi.errors[id];
        delete searchUi.checks[id];
        await refreshAfterWrite(reloadSettings);
        if (r && r.ok === false && r.error) {
          toast(`已保存，连接失败：${r.error}`, true);
        } else {
          toast(clear ? "已清除密钥" : r && r.bound ? `已启用并设为主用：${r.label || id}` : key ? "密钥已保存" : "已启用");
        }
      } catch (err) {
        el.disabled = false;
        if (errEl) {
          errEl.textContent = err.message;
          errEl.hidden = false;
        } else toast(err.message, true);
      }
      return true;
    }
    case "jina-toggle": {
      const on = el.dataset.on === "1";
      el.disabled = true;
      try {
        await api("PUT", "/api/settings/config", { "reader.jina_enabled": on });
        await refreshAfterWrite(reloadSettings);
        toast(on ? "已启用 Jina Reader" : "已停用 Jina Reader");
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
      }
      return true;
    }
    case "jina-key-save": {
      const input = $("jina-key-input");
      const errEl = $("jina-key-err");
      const key = ((input && input.value) || "").trim();
      if (errEl) errEl.hidden = true;
      if (!key) {
        if (errEl) {
          errEl.textContent = "请填写 Jina API 密钥";
          errEl.hidden = false;
        }
        return true;
      }
      el.disabled = true;
      try {
        await api("PUT", "/api/settings/config", { "reader.jina_api_key": key });
        if (input) input.value = "";
        state.searchSheet = null;
        await refreshAfterWrite(reloadSettings);
        toast("密钥已保存");
      } catch (err) {
        el.disabled = false;
        if (errEl) {
          errEl.textContent = err.message;
          errEl.hidden = false;
        } else toast(err.message, true);
      }
      return true;
    }
    case "jina-key-clear": {
      if (!confirm("确定清除 Jina Reader 密钥？清除后将恢复免费额度（每分钟 20 次）。")) return true;
      el.disabled = true;
      try {
        await api("PUT", "/api/settings/config", { "reader.jina_api_key": null });
        state.searchSheet = null;
        await refreshAfterWrite(reloadSettings);
        toast("已清除密钥");
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
      }
      return true;
    }
    case "search-save": {
      const ed = state.searchSheet || {};
      syncChainSelects(ed);
      const mcp = ed.mcp || "";
      const tool = ed.tool || "";
      const errEl = $("sx-err");
      if (errEl) errEl.hidden = true;
      el.disabled = true;
      try {
        state.extSearch = await api("PUT", "/api/extensions/search", {
          mcp,
          tool,
          extract_mcp: ed.extract_tool ? ed.extract_mcp || mcp : "",
          extract_tool: ed.extract_tool || "",
          fallback: (ed.fallback || []).filter((n) => n && n !== mcp),
          broad: (ed.broad || []).filter((n) => n && n !== mcp),
        });
        state.searchSheet = null;
        await refreshAfterWrite(reloadSettings);
        toast(`主用搜索已切换为 ${labelOf(mcp)}`);
      } catch (err) {
        el.disabled = false;
        if (errEl) {
          errEl.textContent = err.message;
          errEl.hidden = false;
        } else toast(err.message, true);
      }
      return true;
    }
    case "search-off": {
      if (!confirm("确定关闭联网搜索？关闭后将暂停自动更新资讯。")) return true;
      try {
        await api("DELETE", "/api/extensions/search");
        state.searchSheet = null;
        await refreshAfterWrite(reloadSettings);
        toast("已关闭联网搜索");
      } catch (err) {
        toast(err.message, true);
      }
      return true;
    }
  }
  return false;
}
