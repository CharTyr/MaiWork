// MaiWork 网页 · 设置 ·「模型」：端点和模型库（2026-10 改版）。
// 这里只管「有哪些端点、从端点里加了哪些模型、每个模型能干什么」；
// 谁用哪个模型（主模型、各专岗）在「专岗」页里选。
import { state, $ } from "../state.js";
import { esc, ico, toast, when } from "../util.js";
import { api } from "../api.js";
import { repaintSheet } from "../sheet.js";
import { loading } from "../pages/news.js";

export function feedsSettings(f, page) {
  if (!f) return "";
  const manual = f.blocked_domains || [];
  const auto = f.auto_blocked || [];
  return `
    ${page ? `` : `<h2 class="h-sub">屏蔽的资讯来源</h2>`}
    ${
      manual.length || auto.length
        ? [
            ...manual.map((d) => `<div class="set-row">${ico("lock")}<div><div class="set-name mono">${esc(d)}</div><div class="set-text">你屏蔽的</div></div><button class="btn small" data-act="unblock-domain" data-domain="${esc(d)}">解除</button></div>`),
            ...auto.map((d) => `<div class="set-row">${ico("lock")}<div><div class="set-name mono">${esc(d)}</div><div class="set-text">自动屏蔽</div></div><span></span></div>`),
          ].join("")
        : `<p class="h-meta">还没有</p>`
    }`;
}

export const fullLink = (g) => (g.link && /^https?:/.test(g.link) ? g.link : `${location.origin}/#/${g.token}/news`);

export const draft = { models: null }; // 老引导用的临时列表（引导改版后仍共用这个对象）

export const PROTOCOLS = {
  openai: { name: "OpenAI 兼容", hint: "/chat/completions，绝大多数中转和国产模型都用这个", ph: "https://…/v1" },
  anthropic: { name: "Anthropic", hint: "/v1/messages，直连 Claude", ph: "https://api.anthropic.com" },
  responses: { name: "OpenAI Responses", hint: "/v1/responses，GPT 系列的思考强度走这个更完整", ph: "https://api.openai.com/v1" },
};
export const EFFORTS = ["low", "medium", "high", "xhigh", "max"];
export const EFFORT_NAMES = { low: "低", medium: "中", high: "高", xhigh: "很高", max: "最高" };
const kTok = (n) => (n >= 1000000 ? `${+(n / 1000000).toFixed(1)}M` : n >= 1000 ? `${Math.round(n / 1000)}K` : String(n || 0));
const newId = (p) => p + Math.random().toString(36).slice(2, 8);
const intIn = (v, lo, hi, d) => { const n = parseInt(v, 10); return Number.isFinite(n) ? Math.max(lo, Math.min(hi, n)) : d; };

export async function loadModels() {
  state.mdlError = "";
  try {
    const [cat, agents] = await Promise.all([api("GET", "/api/settings/endpoints"), api("GET", "/api/agents").catch(() => null)]);
    state.mdl = { endpoints: cat.endpoints || [], models: cat.models || [] };
    if (agents) state.agents = agents;
  } catch (err) {
    state.mdlError = err.message;
  }
}

// 哪些专岗在用这个模型（主用 / 备用）
export function usersOf(modelId) {
  const out = [];
  for (const p of (state.agents && state.agents.profiles) || []) {
    if (p.model === modelId) out.push(p.title);
    else if (p.backup === modelId) out.push(`${p.title}（备用）`);
  }
  return out;
}

export function modelsPage() {
  const m = state.mdl;
  if (!m && state.mdlError) return `<p class="err" role="alert">${esc(state.mdlError)}</p><button class="btn" data-act="mdl-reload">重新加载</button>`;
  if (!m) return loading();
  const s = state.settings || {};
  const ready = s.models ? s.models.ready !== false : true;
  const edit = state.mdlEdit;
  return `
    <p class="h-meta">先加端点，再从端点里加模型。每个专岗用哪个模型，去「专岗」页选。</p>
    ${ready ? "" : `<div class="warn-box">主模型还没选好模型，MaiWork 暂时不会工作<button class="btn small" data-act="set-sub" data-sub="agents" style="margin-left:8px">去专岗页选</button></div>`}
    <div class="h-sub-row"><h2 class="h-sub">端点</h2><button class="btn small" data-act="mdl-ep-new">加一个端点</button></div>
    ${edit && edit.type === "ep" && edit.isNew ? endpointForm(edit.draft, true) : ""}
    ${m.endpoints.length ? m.endpoints.map(endpointBlock).join("") : edit && edit.isNew ? "" : `<p class="h-meta">还没有端点。点「加一个端点」，填地址和密钥。</p>`}
    <h2 class="h-sub">Jev</h2>
    <div class="set-row">${ico("sparkles")}<div><div class="set-name">Jev</div><div class="set-text">快速判断群消息用的，不是大模型，单独配密钥 <button type="button" class="link-btn" data-act="cfg-goto" data-s="jev">去填 Jev 密钥</button></div></div><span></span></div>`;
}

function endpointBlock(ep) {
  const edit = state.mdlEdit;
  const models = state.mdl.models.filter((x) => x.endpoint === ep.id);
  const head = edit && edit.type === "ep" && !edit.isNew && edit.id === ep.id
    ? endpointForm(edit.draft, false)
    : `<div class="set-row mdl-ep">${ico("link")}<div>
        <div class="set-name">${esc(ep.name || ep.id)}<span class="tag">${esc((PROTOCOLS[ep.protocol] || PROTOCOLS.openai).name)}</span></div>
        <div class="set-text"><span class="mono">${esc(ep.base_url || "没填地址")}</span> · 密钥${ep.key_set ? "已填" : "<b>没填</b>"}${ep.checked_at ? ` · ${esc(when(ep.checked_at))}测过` : ""}</div>
      </div><span class="row-btns"><button class="btn small" data-act="mdl-ep-edit" data-id="${esc(ep.id)}">改</button><button class="btn small ghost" data-act="mdl-ep-del" data-id="${esc(ep.id)}">删</button></span></div>`;
  const newModel = edit && edit.type === "model" && edit.isNew && edit.draft.endpoint === ep.id ? modelForm(edit.draft, true) : "";
  return `<section class="mdl-ep-box">
    ${head}
    <div class="mdl-list">
      ${models.map(modelRow).join("") || (newModel ? "" : `<p class="fine">这个端点下还没有模型。</p>`)}
      ${newModel}
      ${newModel ? "" : `<button class="btn small" data-act="mdl-model-new" data-ep="${esc(ep.id)}">从这个端点加模型</button>`}
    </div>
  </section>`;
}

function modelRow(x) {
  const edit = state.mdlEdit;
  if (edit && edit.type === "model" && !edit.isNew && edit.id === x.id) return modelForm(edit.draft, false);
  const users = usersOf(x.id);
  const chips = [
    (x.efforts || []).length ? `思考强度 ${x.efforts.map((e) => EFFORT_NAMES[e] || e).join(" / ")}` : "不调思考强度",
    x.vision ? "能看图" : "",
    `上下文 ${kTok(x.context_window)}`,
    `最大输出 ${kTok(x.max_tokens)}`,
  ].filter(Boolean);
  return `<div class="set-row mdl-model">${ico("robot")}<div>
      <div class="set-name">${esc(x.name || x.model)}${x.name && x.name !== x.model ? ` <span class="fine mono">${esc(x.model)}</span>` : ""}</div>
      <div class="set-text">${chips.map(esc).join(" · ")}</div>
      <div class="set-text">${users.length ? `在用：${esc(users.join("、"))}` : "还没有专岗在用"}</div>
    </div><span class="row-btns"><button class="btn small" data-act="mdl-model-edit" data-id="${esc(x.id)}">改</button><button class="btn small ghost" data-act="mdl-model-del" data-id="${esc(x.id)}">删</button></span></div>`;
}

function endpointForm(d, isNew) {
  const proto = PROTOCOLS[d.protocol] || PROTOCOLS.openai;
  const avail = (state.mdlAvail || {})[d.id];
  return `<div class="login ext-form mdl-form" id="mdl-ep-form">
    <div class="ext-form-h">${isNew ? "加一个端点" : `改「${esc(d.name || d.id)}」`}</div>
    <label for="ep-name">名字</label><input id="ep-name" maxlength="40" value="${esc(d.name || "")}" placeholder="比如：公司中转、Claude 官方" />
    <label for="ep-proto">接口格式</label>
    <select id="ep-proto">${Object.entries(PROTOCOLS).map(([k, v]) => `<option value="${k}"${k === d.protocol ? " selected" : ""}>${esc(v.name)}</option>`).join("")}</select>
    <p class="fine" id="ep-proto-hint" style="margin:2px 0 0">${esc(proto.hint)}</p>
    <label for="ep-url">地址</label><input id="ep-url" type="url" inputmode="url" spellcheck="false" value="${esc(d.base_url || "")}" placeholder="${esc(proto.ph)}" />
    <label for="ep-key">API 密钥</label><input id="ep-key" type="password" autocomplete="new-password" placeholder="${d.key_set ? "已填写 · 留空就不改" : "粘贴密钥"}" />
    <div class="actions" style="margin-top:4px"><button class="btn" type="button" data-act="mdl-ep-test">测试连接</button><span class="fine" id="ep-check" style="margin:0;align-self:center">${avail ? `找到 ${avail.length} 个模型` : ""}</span></div>
    <details class="mdl-more"><summary>重试和请求频率</summary>
      <div class="two-col">
        <div><label for="ep-retries">最多重试几次</label><input id="ep-retries" type="number" min="0" max="10" value="${esc(d.retries ?? 5)}" /></div>
        <div><label for="ep-delay">每次间隔（秒）</label><input id="ep-delay" type="number" min="1" max="60" value="${esc(d.retry_delay_s ?? 10)}" /></div>
        <div><label for="ep-conc">同时最多几个请求</label><input id="ep-conc" type="number" min="1" max="8" value="${esc(d.max_concurrency ?? 2)}" /></div>
        <div><label for="ep-rpm">每分钟最多（0 = 不限）</label><input id="ep-rpm" type="number" min="0" max="600" value="${esc(d.max_rpm ?? 0)}" /></div>
      </div>
      <p class="fine">出错时隔一会儿再试，还不行就换备用模型；经常提示请求太多，就把频率调小。</p>
    </details>
    <p class="err" id="ep-err" hidden></p>
    <div class="actions"><button class="btn primary" type="button" data-act="mdl-ep-save">保存端点</button><button class="btn" type="button" data-act="mdl-cancel">取消</button></div>
  </div>`;
}

function modelForm(d, isNew) {
  const ep = state.mdl.endpoints.find((e) => e.id === d.endpoint) || {};
  const avail = (state.mdlAvail || {})[d.endpoint] || ep.available || [];
  const efforts = new Set(d.efforts || []);
  return `<div class="login ext-form mdl-form" id="mdl-model-form">
    <div class="ext-form-h">${isNew ? `从「${esc(ep.name || ep.id || "")}」加模型` : `改「${esc(d.name || d.model)}」`}</div>
    <label for="mo-model">模型 ID</label>
    <input id="mo-model" list="mo-avail" spellcheck="false" autocomplete="off" value="${esc(d.model || "")}" placeholder="${avail.length ? "从列表里挑，或者直接填" : "填端点里的模型名，比如 gpt-5.2"}" />
    <datalist id="mo-avail">${avail.map((x) => `<option value="${esc(x)}"></option>`).join("")}</datalist>
    ${avail.length ? "" : `<p class="fine" style="margin:2px 0 0">想从列表里挑，先在端点那里点「测试连接」。<button type="button" class="link-btn" data-act="mdl-avail" data-ep="${esc(d.endpoint)}">现在拉一下列表</button></p>`}
    <label for="mo-name">显示名（可不填）</label><input id="mo-name" maxlength="60" value="${esc(d.name && d.name !== d.model ? d.name : "")}" placeholder="不填就用模型 ID" />
    <label>支持的思考强度</label>
    <div class="mdl-efforts" role="group" aria-label="支持的思考强度">${EFFORTS.map((e) => `<label class="chk"><input type="checkbox" class="mo-effort" value="${e}"${efforts.has(e) ? " checked" : ""} />${EFFORT_NAMES[e]}（${e}）</label>`).join("")}</div>
    <p class="fine" style="margin:2px 0 0">勾这个模型真能用的几档；都不勾 = 不传思考强度。专岗只能在这里勾了的里面选。</p>
    <label class="chk"><input id="mo-vision" type="checkbox"${d.vision ? " checked" : ""} />能看图片</label>
    <div class="two-col">
      <div><label for="mo-ctx">最大上下文（tokens）</label><input id="mo-ctx" type="number" min="8192" max="2000000" step="1024" value="${esc(d.context_window ?? 128000)}" /></div>
      <div><label for="mo-max">最大输出（tokens）</label><input id="mo-max" type="number" min="1024" max="1000000" step="1024" value="${esc(d.max_tokens ?? 32768)}" /></div>
    </div>
    <p class="fine" style="margin:2px 0 0">最大上下文 = 模型一次能记住多长，快满时 MaiWork 会先把前面的整理成摘要。最大输出 = 一次回答最多写多长。</p>
    <p class="err" id="mo-err" hidden></p>
    <div class="actions"><button class="btn primary" type="button" data-act="mdl-model-save">保存模型</button><button class="btn" type="button" data-act="mdl-cancel">取消</button></div>
  </div>`;
}

function showErr(id, text) {
  const e = $(id);
  if (!e) return toast(text, true);
  e.textContent = text;
  e.hidden = false;
}

function readEndpoint() {
  const v = (id) => ($(id) ? $(id).value.trim() : "");
  return {
    name: v("ep-name"), protocol: v("ep-proto") || "openai", base_url: v("ep-url"), api_key: v("ep-key") || undefined,
    retries: intIn(v("ep-retries"), 0, 10, 5), retry_delay_s: intIn(v("ep-delay"), 1, 60, 10),
    max_concurrency: intIn(v("ep-conc"), 1, 8, 2), max_rpm: intIn(v("ep-rpm"), 0, 600, 0),
  };
}

function readModel() {
  const v = (id) => ($(id) ? $(id).value.trim() : "");
  return {
    model: v("mo-model"), name: v("mo-name") || v("mo-model"),
    efforts: [...document.querySelectorAll(".mo-effort:checked")].map((x) => x.value),
    vision: !!($("mo-vision") && $("mo-vision").checked),
    context_window: intIn(v("mo-ctx"), 8192, 2000000, 128000), max_tokens: intIn(v("mo-max"), 1024, 1000000, 32768),
  };
}

async function reloadAll() {
  await loadModels();
  const { loadSettings } = await import("../router.js");
  await loadSettings().catch(() => null);
  repaintSheet();
}

async function testEndpoint(epId, body, out) {
  const r = await api("POST", `/api/settings/endpoints/${encodeURIComponent(epId)}/test`, body);
  if (r && r.ok) {
    state.mdlAvail = { ...(state.mdlAvail || {}), [epId]: r.models || [] };
    if (out) { out.textContent = `连上了，找到 ${(r.models || []).length} 个模型`; out.style.color = ""; }
    return true;
  }
  if (out) { out.textContent = (r && r.error) || "没连上"; out.style.color = "var(--red)"; }
  return false;
}

export async function actModels(action, el) {
  const m = state.mdl;
  switch (action) {
    case "mdl-reload": await loadModels(); repaintSheet(); return true;
    case "mdl-cancel": state.mdlEdit = null; repaintSheet(); return true;
    case "mdl-ep-new":
      state.mdlEdit = { type: "ep", isNew: true, draft: { id: newId("e"), protocol: "openai", retries: 5, retry_delay_s: 10, max_concurrency: 2, max_rpm: 0 } };
      repaintSheet(); setTimeout(() => $("ep-name") && $("ep-name").focus(), 0); return true;
    case "mdl-ep-edit": {
      const ep = m && m.endpoints.find((e) => e.id === el.dataset.id);
      if (!ep) return true;
      state.mdlEdit = { type: "ep", isNew: false, id: ep.id, draft: { ...ep } };
      repaintSheet(); return true;
    }
    case "mdl-ep-test": {
      const d = state.mdlEdit && state.mdlEdit.draft;
      const body = readEndpoint();
      const out = $("ep-check");
      if (!/^https?:\/\/\S+$/.test(body.base_url)) { out.textContent = "地址要以 http:// 或 https:// 开头"; out.style.color = "var(--red)"; return true; }
      if (!(d && d.key_set) && !body.api_key) { out.textContent = "先填密钥再测"; out.style.color = "var(--red)"; return true; }
      el.disabled = true; out.style.color = ""; out.textContent = "正在连…";
      try { await testEndpoint(d.id, { base_url: body.base_url, api_key: body.api_key, protocol: body.protocol }, out); }
      catch (err) { out.textContent = err.message; out.style.color = "var(--red)"; }
      finally { el.disabled = false; }
      return true;
    }
    case "mdl-ep-save": {
      const edit = state.mdlEdit;
      if (!edit) return true;
      const body = readEndpoint();
      const problem = !body.name ? "给端点起个名字。" : !/^https?:\/\/\S+$/.test(body.base_url) ? "地址要以 http:// 或 https:// 开头。" : !edit.draft.key_set && !body.api_key ? "还没填密钥。" : "";
      if (problem) return showErr("ep-err", problem), true;
      el.disabled = true;
      try {
        await api("PUT", `/api/settings/endpoints/${encodeURIComponent(edit.draft.id)}`, body);
        const wasNew = edit.isNew;
        state.mdlEdit = null;
        await reloadAll();
        toast(wasNew ? "端点加好了，接着从这个端点加模型" : "端点改好了");
      } catch (err) { el.disabled = false; showErr("ep-err", err.message); }
      return true;
    }
    case "mdl-ep-del": {
      const ep = m && m.endpoints.find((e) => e.id === el.dataset.id);
      if (!ep) return true;
      if (m.models.some((x) => x.endpoint === ep.id)) return toast("这个端点下还有模型，先把模型删掉", true), true;
      if (!confirm(`删掉端点「${ep.name || ep.id}」？它的密钥也会一起删。`)) return true;
      try { await api("DELETE", `/api/settings/endpoints/${encodeURIComponent(ep.id)}`); await reloadAll(); toast("删掉了"); }
      catch (err) { toast(err.message, true); }
      return true;
    }
    case "mdl-avail": {
      // 只换列表数据，不重建表单（docs/13 A09）：先把整张表单收进 draft，成功 / 失败都不丢未保存的字段
      el.disabled = true;
      if (state.mdlEdit && state.mdlEdit.type === "model") {
        const cur = readModel();
        Object.assign(state.mdlEdit.draft, cur, { name: cur.name === cur.model ? "" : cur.name });
      }
      try {
        const ok = await testEndpoint(el.dataset.ep, {}, null);
        const dl = $("mo-avail");
        const list = (state.mdlAvail || {})[el.dataset.ep] || [];
        if (ok && dl) {
          dl.innerHTML = list.map((x) => `<option value="${esc(x)}"></option>`).join("");
          if (el.parentElement) el.parentElement.hidden = true; // 有列表了，提示行收起
        }
        el.disabled = false;
        toast(ok ? `找到 ${list.length} 个模型，在「模型 ID」框里可以直接选` : "没连上，检查一下端点的地址和密钥", !ok);
      } catch (err) { toast(err.message, true); el.disabled = false; }
      return true;
    }
    case "mdl-model-new":
      state.mdlEdit = { type: "model", isNew: true, draft: { id: newId("m"), endpoint: el.dataset.ep, efforts: [], vision: false, context_window: 128000, max_tokens: 32768 } };
      repaintSheet(); setTimeout(() => $("mo-model") && $("mo-model").focus(), 0); return true;
    case "mdl-model-edit": {
      const x = m && m.models.find((y) => y.id === el.dataset.id);
      if (!x) return true;
      state.mdlEdit = { type: "model", isNew: false, id: x.id, draft: { ...x } };
      repaintSheet(); return true;
    }
    case "mdl-model-save": {
      const edit = state.mdlEdit;
      if (!edit) return true;
      const body = { ...readModel(), endpoint: edit.draft.endpoint };
      const problem = !body.model ? "填一下模型 ID。" : body.max_tokens >= body.context_window ? "最大输出要比最大上下文小。" : "";
      if (problem) return showErr("mo-err", problem), true;
      el.disabled = true;
      try {
        await api("PUT", `/api/settings/model-list/${encodeURIComponent(edit.draft.id)}`, body);
        const wasNew = edit.isNew;
        state.mdlEdit = null;
        await reloadAll();
        toast(wasNew ? "模型加好了，去「专岗」页分给要用它的专岗" : "模型改好了");
      } catch (err) { el.disabled = false; showErr("mo-err", err.message); }
      return true;
    }
    case "mdl-model-del": {
      const x = m && m.models.find((y) => y.id === el.dataset.id);
      if (!x) return true;
      const users = usersOf(x.id);
      if (users.length) return toast(`还有专岗在用它（${users.join("、")}），先在「专岗」页换掉`, true), true;
      if (!confirm(`从模型库删掉「${x.name || x.model}」？`)) return true;
      try { await api("DELETE", `/api/settings/model-list/${encodeURIComponent(x.id)}`); await reloadAll(); toast("删掉了"); }
      catch (err) { toast(err.message, true); }
      return true;
    }
    default: return false;
  }
}

// 接口格式一换，提示和地址占位跟着换
document.addEventListener("change", (event) => {
  if (event.target && event.target.id === "ep-proto") {
    const p = PROTOCOLS[event.target.value] || PROTOCOLS.openai;
    if ($("ep-proto-hint")) $("ep-proto-hint").textContent = p.hint;
    if ($("ep-url")) $("ep-url").placeholder = p.ph;
  }
});
