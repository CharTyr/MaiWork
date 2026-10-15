// MaiWork 网页 · 设置 ·「模型」：端点和模型库（2026-10 改版）。
// 这里只管「有哪些端点、从端点里加了哪些模型、每个模型能干什么」；
// 谁用哪个模型（主模型、各专岗）在「专岗」页里选。
import { state, $ } from "../state.js";
import { esc, ico, tip, tipLabel, toast, when } from "../util.js";
import { api } from "../api.js";
import { repaintSheet } from "../sheet.js";
import { loading } from "../pages/news.js";
import { jevSection, loadJev } from "./jev.js";

// 屏蔽名单按群（docs/18 第一步）：blocked_domains / auto_blocked 都是 {群号: [域名]}
export function feedsSettings(f, groups) {
  if (!f) return "";
  const pick = (m, gid) => (m && !Array.isArray(m) && Array.isArray(m[gid]) ? m[gid] : []);
  const list = groups || [];
  if (!list.length) return `<p class="h-meta">暂无群</p>`;
  return list
    .map((g) => {
      const manual = pick(f.blocked_domains, g.id);
      const auto = pick(f.auto_blocked, g.id);
      const rows = [
        ...manual.map((d) => `<div class="set-row">${ico("lock")}<div><div class="set-name mono">${esc(d)}</div><div class="set-text">手动屏蔽</div></div><button class="btn small" data-act="unblock-domain" data-g="${esc(g.id)}" data-domain="${esc(d)}">取消屏蔽</button></div>`),
        ...auto.map((d) => `<div class="set-row">${ico("lock")}<div><div class="set-name mono">${esc(d)}</div><div class="set-text">自动屏蔽</div></div><span></span></div>`),
      ].join("");
      return `
    <div class="rss-group">
      <div class="rss-gname">${esc(g.name || `群 ${g.id}`)}</div>
      ${rows || `<p class="fine">暂无</p>`}
    </div>`;
    })
    .join("");
}

export const fullLink = (g) => (g.link && /^https?:/.test(g.link) ? g.link : `${location.origin}/#/${g.token}/news`);

export const draft = { models: null }; // 老引导用的临时列表（引导改版后仍共用这个对象）

export const PROTOCOLS = {
  openai: { name: "OpenAI 兼容", hint: "适用于大多数中转服务和国产模型。", ph: "https://…/v1" },
  anthropic: { name: "Anthropic", hint: "用于直连 Anthropic Claude。", ph: "https://api.anthropic.com" },
  responses: { name: "OpenAI Responses", hint: "OpenAI Responses 接口，GPT 系列的思考强度选项更完整。", ph: "https://api.openai.com/v1" },
};
export const EFFORTS = ["low", "medium", "high", "xhigh", "max"];
export const EFFORT_NAMES = { low: "低", medium: "中", high: "高", xhigh: "很高", max: "最高" };
// 模型用途（2026-10-15 生图第一期）：聊天 / 干活，或者只用来画图。
export const USE_NAMES = { chat: "聊天", image: "生图" };
// 生图接口形状：只收「OpenAI 兼容形状」的两家（同步、一次拿结果）。
export const IMAGE_APIS = {
  openai: {
    name: "OpenAI 兼容",
    hint: "OpenAI、火山方舟（豆包 Seedream）等：POST 到 /images/generations，尺寸字段用 size、张数用 n。",
  },
  siliconflow: {
    name: "硅基流动",
    hint: "硅基流动：地址路径一样，但字段名不同（image_size / batch_size），返回的图片地址只活 1 小时。",
  },
};
const isImageModel = (x) => (x && (x.use || "chat")) === "image";
const kTok = (n) => (n >= 1000000 ? `${+(n / 1000000).toFixed(1)}M` : n >= 1000 ? `${Math.round(n / 1000)}K` : String(n || 0));
const newId = (p) => p + Math.random().toString(36).slice(2, 8);
const intIn = (v, lo, hi, d) => { const n = parseInt(v, 10); return Number.isFinite(n) ? Math.max(lo, Math.min(hi, n)) : d; };

export async function loadModels() {
  state.mdlError = "";
  try {
    // 生图那半（用哪个生图模型 + 常用服务预设）单独一条接口：端点的响应形状是既有契约，不往里塞。
    const [cat, agents, , img] = await Promise.all([
      api("GET", "/api/settings/endpoints"),
      api("GET", "/api/agents").catch(() => null),
      loadJev(),
      api("GET", "/api/settings/imagegen").catch(() => null),
    ]);
    state.mdl = {
      endpoints: cat.endpoints || [], models: cat.models || [],
      image_model: (img && img.model) || "", imagegen_presets: (img && img.presets) || [],
    };
    state.mdlVerification = {}; // 刷新配置后不复用界面里上一次的验证/待保存建议
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
  if (!m && state.mdlError) return `<p class="err" role="alert">${esc(state.mdlError)}</p><button class="btn" data-act="mdl-reload">刷新</button>`;
  if (!m) return loading();
  const s = state.settings || {};
  const ready = s.models ? s.models.ready !== false : true;
  const edit = state.mdlEdit;
  return `
    ${ready ? "" : `<div class="warn-box">未设置主模型，MaiWork 无法运行<button class="btn small" data-act="set-sub" data-sub="agents" style="margin-left:8px">前往设置</button></div>`}
    <div class="h-sub-row"><h2 class="h-sub">端点${tip("端点是模型服务的接入地址。先添加端点，再在端点下添加模型，然后到「专岗」中为各岗位指定模型。")}</h2><button class="btn small" data-act="mdl-ep-new">添加</button></div>
    ${edit && edit.type === "ep" && edit.isNew ? endpointForm(edit.draft, true) : ""}
    ${m.endpoints.length ? m.endpoints.map(endpointBlock).join("") : edit && edit.isNew ? "" : `<p class="h-meta">暂无端点</p>`}
    ${jevSection()}`;
}

function endpointBlock(ep) {
  const edit = state.mdlEdit;
  const models = state.mdl.models.filter((x) => x.endpoint === ep.id);
  const head = edit && edit.type === "ep" && !edit.isNew && edit.id === ep.id
    ? endpointForm(edit.draft, false)
    : `<div class="set-row mdl-ep">${ico("link")}<div>
        <div class="set-name">${esc(ep.name || ep.id)}<span class="tag">${esc((PROTOCOLS[ep.protocol] || PROTOCOLS.openai).name)}</span></div>
        <div class="set-text"><span class="mono">${esc(ep.base_url || "未填写地址")}</span> · 密钥${ep.key_set ? "已填写" : "<b>未填写</b>"}${ep.checked_at ? ` · ${esc(when(ep.checked_at))} 已测试` : ""}</div>
      </div><span class="row-btns"><button class="btn small" data-act="mdl-ep-edit" data-id="${esc(ep.id)}">修改</button><button class="btn small ghost" data-act="mdl-ep-del" data-id="${esc(ep.id)}">删除</button></span></div>`;
  const newModel = edit && edit.type === "model" && edit.isNew && edit.draft.endpoint === ep.id ? modelForm(edit.draft, true) : "";
  return `<section class="mdl-ep-box">
    ${head}
    <div class="mdl-list">
      ${models.map(modelRow).join("") || (newModel ? "" : `<p class="fine">暂无模型</p>`)}
      ${newModel}
      ${newModel ? "" : `<button class="btn small" data-act="mdl-model-new" data-ep="${esc(ep.id)}">添加模型</button>`}
    </div>
  </section>`;
}

function modelMeta(x) {
  if (isImageModel(x)) {
    return [
      `用途 ${USE_NAMES.image}`,
      `接口 ${(IMAGE_APIS[x.image_api] || IMAGE_APIS.openai).name}`,
      x.image_default_size ? `默认尺寸 ${x.image_default_size}` : "默认尺寸跟随服务商",
      `一次最多 ${x.image_max_n || 1} 张`,
    ].join(" · ");
  }
  return [
    (x.efforts || []).length ? `思考强度 ${x.efforts.map((e) => EFFORT_NAMES[e] || e).join(" / ")}` : "不支持思考强度",
    x.vision ? "支持图片" : "",
    `上下文 ${kTok(x.context_window)}`,
    `最大输出 ${kTok(x.max_tokens)}`,
  ].filter(Boolean).join(" · ");
}

function verificationMessage(r) {
  if (r.stale) return "设置已更改，请重新验证。";
  if (!r.ok) return `验证失败：${r.error || "模型无响应"}`;
  if (r.suggested_max_tokens) return `最大输出需调整为 ${r.suggested_max_tokens}，尚未保存。${r.note || ""}`;
  return r.tools_ok ? "验证通过" : `可以回复，但工具调用失败：${r.note || "工具调用未通过"}`;
}

function modelRow(x) {
  const edit = state.mdlEdit;
  if (edit && edit.type === "model" && !edit.isNew && edit.id === x.id) return modelForm(edit.draft, false);
  const users = usersOf(x.id);
  const image = isImageModel(x);
  const currentImage = image && String((state.mdl && state.mdl.image_model) || "") === x.id;
  const result = (state.mdlVerification || {})[x.id];
  // 生图模型不跑「验证」（那是聊天口径：会发测试对话，白花钱），改给「设为生图模型」。
  const acts = image
    ? `${currentImage
        ? `<span class="tag">当前生图模型</span>`
        : `<button type="button" class="btn small" style="min-height:44px;margin-top:8px" data-act="mdl-image-model" data-id="${esc(x.id)}">设为生图模型</button>`}${tip("生图模型只在任务需要画图时用；它不参与聊天，所以不用「验证」。")}`
    : `<button type="button" class="btn small" style="min-height:44px;margin-top:8px" data-act="mdl-model-verify" data-id="${esc(x.id)}">验证</button>${tip("发送几条测试消息，检查模型能否正常回复和调用工具。会消耗少量 token。")}
      <div class="set-text mdl-verify-result" role="status" aria-live="polite"${result ? "" : " hidden"}>${result ? esc(verificationMessage(result)) : ""}</div>`;
  return `<div class="set-row mdl-model">${ico("robot")}<div>
      <div class="set-name">${esc(x.name || x.model)}${image ? `<span class="tag">${esc(USE_NAMES.image)}</span>` : ""}${x.name && x.name !== x.model ? ` <span class="fine mono">${esc(x.model)}</span>` : ""}</div>
      <div class="set-text mdl-meta">${esc(modelMeta(x))}</div>
      <div class="set-text">${image ? "给子 agent 画图用" : users.length ? `使用者：${esc(users.join("、"))}` : "未被使用"}</div>
      ${acts}
    </div><span class="row-btns"><button class="btn small" data-act="mdl-model-edit" data-id="${esc(x.id)}">修改</button><button class="btn small ghost" data-act="mdl-model-del" data-id="${esc(x.id)}">删除</button></span></div>`;
}

let headerSeq = 0;
function endpointHeaderRow(name = "", old = false) {
  const id = `ep-hdr-${++headerSeq}`;
  return `<div class="mdl-header-row" data-old="${old ? "1" : "0"}">
    <div class="mdl-header-field"><label for="${id}-name">名称</label><input id="${id}-name" class="ep-header-name mono" maxlength="128" spellcheck="false" autocomplete="off" value="${esc(name)}" placeholder="例如 User-Agent"${old ? " readonly" : ""} /></div>
    <div class="mdl-header-field"><label for="${id}-value">值${old ? " · 已填" : ""}</label><input id="${id}-value" class="ep-header-value" type="password" maxlength="8192" spellcheck="false" autocomplete="new-password" aria-describedby="ep-headers-help" placeholder="${old ? "留空则不修改" : "请求头的值"}" /></div>
    <button class="btn small ghost" type="button" data-act="mdl-header-del" aria-label="删除${name ? `请求头 ${esc(name)}` : "这个请求头"}">删除</button>
  </div>`;
}

function endpointForm(d, isNew) {
  const proto = PROTOCOLS[d.protocol] || PROTOCOLS.openai;
  const avail = (state.mdlAvail || {})[d.id];
  return `<div class="login ext-form mdl-form" id="mdl-ep-form">
    <div class="ext-form-h">${isNew ? "添加端点" : `修改「${esc(d.name || d.id)}」`}</div>
    <label for="ep-name">名称</label><input id="ep-name" maxlength="40" value="${esc(d.name || "")}" placeholder="例如：公司中转、Claude 官方" />
    ${tipLabel("ep-proto", "接口格式", `<p id="ep-proto-hint">${esc(proto.hint)}</p>`)}
    <select id="ep-proto">${Object.entries(PROTOCOLS).map(([k, v]) => `<option value="${k}"${k === d.protocol ? " selected" : ""}>${esc(v.name)}</option>`).join("")}</select>
    <label for="ep-url">地址</label><input id="ep-url" type="url" inputmode="url" spellcheck="false" value="${esc(d.base_url || "")}" placeholder="${esc(proto.ph)}" />
    <label for="ep-key">API 密钥</label><input id="ep-key" type="password" autocomplete="new-password" placeholder="${d.key_set ? "已填写，留空则不修改" : "输入密钥"}" />
    <div class="actions" style="margin-top:4px"><button class="btn" type="button" data-act="mdl-ep-test">测试连接</button><span class="fine" id="ep-check" style="margin:0;align-self:center">${avail ? `可用模型 ${avail.length} 个` : ""}</span></div>
    <details class="mdl-more"><summary>重试与频率</summary>
      <div class="two-col">
        <div><label for="ep-retries">重试次数</label><input id="ep-retries" type="number" min="0" max="10" value="${esc(d.retries ?? 5)}" /></div>
        <div><label for="ep-delay">间隔秒数</label><input id="ep-delay" type="number" min="1" max="60" value="${esc(d.retry_delay_s ?? 10)}" /></div>
        <div><label for="ep-conc">并发数</label><input id="ep-conc" type="number" min="1" max="8" value="${esc(d.max_concurrency ?? 2)}" /></div>
        <div>${tipLabel("ep-rpm", "每分钟请求上限", "0 表示不限制。频繁出现「请求过多」错误时，可调低并发数或每分钟上限。")}<input id="ep-rpm" type="number" min="0" max="600" value="${esc(d.max_rpm ?? 0)}" /></div>
      </div>
    </details>
    <details class="mdl-more mdl-advanced" id="ep-advanced">
      <summary>高级${(d.header_names || []).length ? ` · ${(d.header_names || []).length} 个自定义请求头` : ""}</summary>
      <div class="mdl-advanced-body">
        <div class="set-name">自定义请求头${tip(`<p>本端点的所有请求都会附带这些请求头，同名请求头会被替换。</p><p id="ep-headers-help">值保存后不再显示；留空则保留原值。</p><p>最多 32 项，仅支持英文字符；Host 等传输相关请求头不可修改。</p>`)}</div>
        <div class="seg mdl-header-modes" role="group" aria-label="填写方式">
          <button type="button" data-act="mdl-header-mode" data-mode="rows" aria-pressed="true">逐行</button>
          <button type="button" data-act="mdl-header-mode" data-mode="json" aria-pressed="false">JSON</button>
        </div>
        <div id="ep-headers-rowsbox">
          <div id="ep-headers">${(d.header_names || []).map((h) => endpointHeaderRow(h, true)).join("")}</div>
          <button type="button" class="btn small" data-act="mdl-header-add">添加</button>
        </div>
        <div id="ep-headers-jsonbox" hidden>
          ${tipLabel("ep-headers-json", "JSON", `<p id="ep-headers-json-help">格式为 {"名称": "值"}。值写 "" 表示保留原值，未列出的请求头将被删除。此处为明文显示，请注意周围环境。</p>`)}
          <textarea id="ep-headers-json" class="mono-area" rows="8" spellcheck="false" autocomplete="off" autocapitalize="off" aria-describedby="ep-headers-json-help">${esc(headersJsonText(d.header_names || []))}</textarea>
        </div>
      </div>
    </details>
    <p class="err" id="ep-err" role="alert" hidden></p>
    <div class="actions"><button class="btn primary" type="button" data-act="mdl-ep-save">保存</button><button class="btn" type="button" data-act="mdl-cancel">取消</button></div>
  </div>`;
}

// 填入了常用生图服务、但当前端点地址对不上（2026-10-15 巡检 M7）：模型挂在端点下，地址和密钥在端点上，
// 只填模型名会让请求打到别家。对不上就在表单里明确提示，不悄悄改端点。
const normUrl = (u) => String(u || "").trim().replace(/\/+$/, "").toLowerCase();
export function presetMismatch(d) {
  const p = d && d._preset;
  if (!p || !p.base_url) return "";
  const ep = ((state.mdl && state.mdl.endpoints) || []).find((e) => e.id === d.endpoint) || {};
  if (normUrl(ep.base_url) === normUrl(p.base_url)) return "";
  return `<div class="warn-box">「${esc(p.label)}」的地址是 <span class="mono">${esc(p.base_url)}</span>，当前端点「${esc(ep.name || ep.id || d.endpoint || "")}」的地址是 <span class="mono">${esc(ep.base_url || "未填写")}</span>，两者不一致。请先新建一个端点，填上这个地址和${p.key_url ? `从 <span class="mono">${esc(p.key_url)}</span> 获取的` : "对应的"}密钥，再在新端点下添加这个模型。</div>`;
}

function imageModelFields(d) {
  const presets = (state.mdl && state.mdl.imagegen_presets) || [];
  const api = IMAGE_APIS[d.image_api] || IMAGE_APIS.openai;
  return `
    ${presetMismatch(d)}
    ${tipLabel("mo-image-api", "接口形状", `<p id="mo-image-api-hint">${esc(api.hint)}</p>`)}
    <select id="mo-image-api" aria-describedby="mo-image-api-hint">${Object.entries(IMAGE_APIS).map(([k, v]) => `<option value="${k}"${k === (d.image_api || "openai") ? " selected" : ""}>${esc(v.name)}</option>`).join("")}</select>
    <div class="two-col">
      <div>${tipLabel("mo-image-size", "默认尺寸（可选）", "不填就让服务商按默认来。例如 1024x1024、1536x1024；火山方舟也可以填档位，如 2K。")}<input id="mo-image-size" maxlength="40" spellcheck="false" value="${esc(d.image_default_size || "")}" placeholder="例如 1024x1024" /></div>
      <div>${tipLabel("mo-image-n", "一次最多几张", "子 agent 一次调用最多能要几张（1~4）。")}<input id="mo-image-n" type="number" min="1" max="4" value="${esc(d.image_max_n ?? 1)}" /></div>
    </div>
    <p class="fine">生图模型不参与聊天，只在任务需要画图时用，所以不用填思考强度和上下文长度。</p>
    ${presets.length ? `<details class="mdl-more"><summary>常用生图服务（点「填入」自动填好模型和接口形状）</summary>
      <div>${presets.map((p) => `<div class="set-row"><div>
        <div class="set-name">${esc(p.label)}</div>
        <div class="set-text"><span class="mono">${esc(p.base_url)}</span> · 模型 ${esc(p.model)}<br>${esc(p.note || "")}</div>
      </div><button type="button" class="btn small" data-act="mdl-preset" data-preset="${esc(p.id)}">填入</button></div>`).join("")}</div>
    </details>` : ""}`;
}

function modelForm(d, isNew) {
  const ep = state.mdl.endpoints.find((e) => e.id === d.endpoint) || {};
  const avail = (state.mdlAvail || {})[d.endpoint] || ep.available || [];
  const efforts = new Set(d.efforts || []);
  const image = isImageModel(d);
  return `<div class="login ext-form mdl-form" id="mdl-model-form">
    <div class="ext-form-h">${isNew ? `添加模型 · ${esc(ep.name || ep.id || "")}` : `修改「${esc(d.name || d.model)}」`}</div>
    <label for="mo-model">模型 ID</label>
    <input id="mo-model" list="mo-avail" spellcheck="false" autocomplete="off" value="${esc(d.model || "")}" placeholder="${avail.length ? "从列表选择或直接输入" : "例如 gpt-5.2"}" />
    <datalist id="mo-avail">${avail.map((x) => `<option value="${esc(x)}"></option>`).join("")}</datalist>
    ${avail.length ? "" : `<p class="fine" style="margin:2px 0 0"><button type="button" class="link-btn" data-act="mdl-avail" data-ep="${esc(d.endpoint)}">获取可用模型列表</button></p>`}
    <label for="mo-name">显示名（可选）</label><input id="mo-name" maxlength="60" value="${esc(d.name && d.name !== d.model ? d.name : "")}" placeholder="留空则使用模型 ID" />
    <div class="lbl-row"><label>用途</label>${tip("「聊天」用于对话和干活；「生图」只用来画图。生图模型不能当主模型或子 agent 的聊天模型。")}</div>
    <div class="seg mdl-seg" role="group" aria-label="用途">
      <button type="button" class="mdl-use" data-act="mdl-use" data-use="chat" aria-pressed="${image ? "false" : "true"}">聊天</button>
      <button type="button" class="mdl-use" data-act="mdl-use" data-use="image" aria-pressed="${image ? "true" : "false"}">生图</button>
    </div>
    ${image ? imageModelFields(d) : `
    <div class="lbl-row"><label>思考强度</label>${tip("只勾选模型实际支持的档位；未勾选时不传该参数。")}</div>
    <div class="mdl-efforts" role="group" aria-label="支持的思考强度">${EFFORTS.map((e) => `<label class="chk"><input type="checkbox" class="mo-effort" value="${e}"${efforts.has(e) ? " checked" : ""} />${EFFORT_NAMES[e]}（${e}）</label>`).join("")}</div>
    <label class="chk"><input id="mo-vision" type="checkbox"${d.vision ? " checked" : ""} />支持图片输入</label>
    <div class="two-col">
      <div>${tipLabel("mo-ctx", "上下文长度（tokens）", "上下文长度是模型单次能处理的总长度；最大输出是单次回复的最大长度，须小于上下文长度。")}<input id="mo-ctx" type="number" min="8192" max="2000000" step="1024" value="${esc(d.context_window ?? 128000)}" /></div>
      <div><label for="mo-max">最大输出（tokens）</label><input id="mo-max" type="number" min="1024" max="1000000" step="1024" value="${esc(d.max_tokens ?? 32768)}" /></div>
    </div>`}
    <p class="err" id="mo-err" hidden></p>
    <div class="actions"><button class="btn primary" type="button" data-act="mdl-model-save">保存</button><button class="btn" type="button" data-act="mdl-cancel">取消</button></div>
  </div>`;
}

function showErr(id, text) {
  const e = $(id);
  if (!e) return toast(text, true);
  e.textContent = text;
  e.hidden = false;
}

const HEADER_NAME = /^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$/;
const TRANSPORT_HEADERS = new Set(["host", "content-length", "transfer-encoding", "connection", "proxy-authorization", "proxy-authenticate", "proxy-connection", "keep-alive", "te", "trailer", "upgrade", "via"]);
const HEADER_JSON_ERR = "格式不正确，应为 {\"名称\": \"值\"}";
const savedHeaderNames = () => {
  const d = state.mdlEdit && state.mdlEdit.draft;
  return new Set(((d && d.header_names) || []).map((n) => String(n).toLowerCase()));
};
const jsonMode = () => { const box = $("ep-headers-jsonbox"); return !!(box && !box.hidden); };
// 默认 JSON：只列已存名称、值留空（= 保留原值），绝不带出已存的值
const headersJsonText = (names) => JSON.stringify(Object.fromEntries(names.map((n) => [n, ""])), null, 2);

// 两种填写方式共用一套检查：entries = [[名称, 值, 是否已保存]]
function checkedHeaders(entries) {
  if (entries.length > 32) throw new Error("最多 32 项。");
  const headers = Object.create(null);
  const names = new Set();
  for (const [rawName, rawValue, isSaved] of entries) {
    const name = String(rawName).trim();
    const value = rawValue || "";
    if (!HEADER_NAME.test(name)) throw new Error("名称只能包含英文字符，且不能有空格。");
    const lower = name.toLowerCase();
    if (names.has(lower)) throw new Error("名称重复。");
    if (TRANSPORT_HEADERS.has(lower)) throw new Error("Host 等传输相关请求头不可修改。");
    if (value.length > 8192 || /[^\t\x20-\x7e]/.test(value)) throw new Error("值只能包含英文字符，且不能换行。");
    if (!value && !isSaved) throw new Error("新增的请求头必须填写值，不需要请删除。");
    names.add(lower);
    headers[name] = value; // 已存项空值由服务端保留；缺失的名称由服务端删除
  }
  return headers;
}

// JSON 文本 → [[名称, 值或 null]]；错误提示不带 JSON 原文和值
function parseHeadersJson(text) {
  if (!String(text).trim()) throw new Error("内容不能为空；如需清空，请填写 {}。");
  let data;
  try { data = JSON.parse(text); } catch { throw new Error(HEADER_JSON_ERR); }
  if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error(HEADER_JSON_ERR);
  return Object.keys(data).map((key) => {
    const value = data[key];
    if (value !== null && typeof value !== "string") throw new Error("值必须是用英文双引号括起的字符串。");
    return [key, value];
  });
}

const headerRows = () => [...document.querySelectorAll("#ep-headers .mdl-header-row")];
const rowName = (row) => row.querySelector(".ep-header-name").value.trim();
const rowValue = (row) => row.querySelector(".ep-header-value").value;

function readEndpointHeaders() {
  if (jsonMode()) {
    const saved = savedHeaderNames();
    return checkedHeaders(parseHeadersJson($("ep-headers-json").value).map(([name, value]) => [name, value, saved.has(name.trim().toLowerCase())]));
  }
  return checkedHeaders(headerRows().map((row) => [rowName(row), rowValue(row), row.dataset.old === "1"]));
}

function readEndpoint() {
  const v = (id) => ($(id) ? $(id).value.trim() : "");
  return {
    name: v("ep-name"), protocol: v("ep-proto") || "openai", base_url: v("ep-url"), api_key: v("ep-key") || undefined,
    headers: readEndpointHeaders(),
    retries: intIn(v("ep-retries"), 0, 10, 5), retry_delay_s: intIn(v("ep-delay"), 1, 60, 10),
    max_concurrency: intIn(v("ep-conc"), 1, 8, 2), max_rpm: intIn(v("ep-rpm"), 0, 600, 0),
  };
}

function readModel() {
  const v = (id) => ($(id) ? $(id).value.trim() : "");
  const d = (state.mdlEdit && state.mdlEdit.draft) || {};
  const seg = document.querySelector('.mdl-use[aria-pressed="true"]');
  const image = !!(seg && seg.dataset.use === "image");
  return {
    model: v("mo-model"), name: v("mo-name") || v("mo-model"),
    use: image ? "image" : "chat",
    efforts: [...document.querySelectorAll(".mo-effort:checked")].map((x) => x.value),
    vision: !!($("mo-vision") && $("mo-vision").checked),
    // 生图表单不显示这两项：保留原值（别把已存的配置清掉）
    context_window: $("mo-ctx") ? intIn(v("mo-ctx"), 8192, 2000000, 128000) : (d.context_window ?? 128000),
    max_tokens: $("mo-max") ? intIn(v("mo-max"), 1024, 1000000, 32768) : (d.max_tokens ?? 32768),
    image_api: v("mo-image-api") || "openai",
    image_default_size: v("mo-image-size"),
    image_max_n: $("mo-image-n") ? intIn(v("mo-image-n"), 1, 4, 1) : (d.image_max_n ?? 1),
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
    if (out) { out.textContent = `连接成功，可用模型 ${(r.models || []).length} 个`; out.style.color = ""; }
    return true;
  }
  if (out) { out.textContent = (r && r.error) || "连接失败"; out.style.color = "var(--red)"; }
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
    case "mdl-header-add": {
      const list = $("ep-headers");
      if (!list) return true;
      if (list.querySelectorAll(".mdl-header-row").length >= 32) return showErr("ep-err", "最多 32 项。"), true;
      list.insertAdjacentHTML("beforeend", endpointHeaderRow());
      list.lastElementChild.querySelector(".ep-header-name").focus();
      return true;
    }
    case "mdl-header-del": {
      const row = el.closest(".mdl-header-row");
      if (row) row.remove();
      return true;
    }
    case "mdl-header-mode": {
      const toJson = el.dataset.mode === "json";
      const jsonBox = $("ep-headers-jsonbox"), rowsBox = $("ep-headers-rowsbox"), text = $("ep-headers-json"), list = $("ep-headers");
      if (!jsonBox || !rowsBox || !text || !list || toJson === !jsonBox.hidden) return true;
      if ($("ep-err")) $("ep-err").hidden = true;
      try {
        if (toJson) {
          // 按行 → JSON：带上已输入的值；空白行跳过，同名重复不悄悄合并
          const pairs = Object.create(null);
          for (const row of headerRows()) {
            const name = rowName(row), value = rowValue(row);
            if (!name && !value) continue;
            if (name in pairs) throw new Error("名称重复。");
            pairs[name] = value;
          }
          text.value = JSON.stringify(pairs, null, 2);
        } else {
          // JSON → 按行：已保存的名称（不分大小写）当作已存项，名称只读、值留空 = 保留
          const saved = savedHeaderNames();
          const entries = parseHeadersJson(text.value);
          list.innerHTML = entries.map(([name]) => endpointHeaderRow(name, saved.has(name.trim().toLowerCase()))).join("");
          headerRows().forEach((row, i) => { row.querySelector(".ep-header-value").value = entries[i][1] || ""; });
        }
      } catch (err) { showErr("ep-err", err.message); return true; }
      jsonBox.hidden = !toJson;
      rowsBox.hidden = toJson;
      document.querySelectorAll('[data-act="mdl-header-mode"]').forEach((b) => b.setAttribute("aria-pressed", String((b.dataset.mode === "json") === toJson)));
      return true;
    }
    case "mdl-ep-test": {
      const d = state.mdlEdit && state.mdlEdit.draft;
      let body;
      if ($("ep-err")) $("ep-err").hidden = true;
      try { body = readEndpoint(); }
      catch (err) { showErr("ep-err", err.message); return true; }
      const out = $("ep-check");
      if (!/^https?:\/\/\S+$/.test(body.base_url)) { out.textContent = "地址须以 http:// 或 https:// 开头"; out.style.color = "var(--red)"; return true; }
      if (!(d && d.key_set) && !body.api_key) { out.textContent = "请先填写密钥"; out.style.color = "var(--red)"; return true; }
      el.disabled = true; out.style.color = ""; out.textContent = "正在连接…";
      try { await testEndpoint(d.id, { base_url: body.base_url, api_key: body.api_key, protocol: body.protocol, headers: body.headers }, out); }
      catch (err) { out.textContent = err.message; out.style.color = "var(--red)"; }
      finally { el.disabled = false; }
      return true;
    }
    case "mdl-ep-save": {
      const edit = state.mdlEdit;
      if (!edit) return true;
      let body;
      if ($("ep-err")) $("ep-err").hidden = true;
      try { body = readEndpoint(); }
      catch (err) { showErr("ep-err", err.message); return true; }
      const problem = !body.name ? "请填写端点名称。" : !/^https?:\/\/\S+$/.test(body.base_url) ? "地址须以 http:// 或 https:// 开头。" : !edit.draft.key_set && !body.api_key ? "请填写密钥。" : "";
      if (problem) return showErr("ep-err", problem), true;
      el.disabled = true;
      try {
        await api("PUT", `/api/settings/endpoints/${encodeURIComponent(edit.draft.id)}`, body);
        const wasNew = edit.isNew;
        state.mdlEdit = null;
        await reloadAll();
        toast(wasNew ? "端点已添加，可继续添加模型" : "已保存");
      } catch (err) { el.disabled = false; showErr("ep-err", err.message); }
      return true;
    }
    case "mdl-ep-del": {
      const ep = m && m.endpoints.find((e) => e.id === el.dataset.id);
      if (!ep) return true;
      if (m.models.some((x) => x.endpoint === ep.id)) return toast("请先删除该端点下的模型", true), true;
      if (!confirm(`删除「${ep.name || ep.id}」？密钥将一并删除。`)) return true;
      try { await api("DELETE", `/api/settings/endpoints/${encodeURIComponent(ep.id)}`); await reloadAll(); toast("已删除"); }
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
        toast(ok ? `已获取 ${list.length} 个可用模型` : "连接失败，请检查地址和密钥", !ok);
      } catch (err) { toast(err.message, true); el.disabled = false; }
      return true;
    }
    case "mdl-model-new":
      state.mdlEdit = { type: "model", isNew: true, draft: { id: newId("m"), endpoint: el.dataset.ep, use: "chat", efforts: [], vision: false, context_window: 128000, max_tokens: 32768, image_api: "openai", image_default_size: "", image_max_n: 1 } };
      repaintSheet(); setTimeout(() => $("mo-model") && $("mo-model").focus(), 0); return true;
    case "mdl-use": {
      // 切「聊天 / 生图」：先把当前表单收进 draft（重画表单，别丢没保存的输入）
      const edit = state.mdlEdit;
      if (!edit || edit.type !== "model") return true;
      const want = el.dataset.use === "image" ? "image" : "chat";
      if ((edit.draft.use || "chat") === want) return true;
      Object.assign(edit.draft, readModel(), { use: want });
      if ($("mo-err")) $("mo-err").hidden = true;
      repaintSheet();
      return true;
    }
    case "mdl-preset": {
      // 常用生图服务一键填入（只填模型名和接口形状；地址 / 密钥在端点上填）
      const edit = state.mdlEdit;
      if (!edit || edit.type !== "model") return true;
      const p = ((state.mdl && state.mdl.imagegen_presets) || []).find((x) => x.id === el.dataset.preset);
      if (!p) return true;
      Object.assign(edit.draft, readModel(), { use: "image", image_api: p.image_api, model: p.model, _preset: p });
      if (!edit.draft.name) edit.draft.name = p.label;
      repaintSheet();
      toast(presetMismatch(edit.draft) ? `已填好模型 ${p.model}，但端点地址不一致，请查看表单里的提示` : `已按「${p.label}」填好模型 ${p.model}`);
      return true;
    }
    case "mdl-image-model": {
      el.disabled = true;
      try {
        await api("PUT", "/api/settings/imagegen", { model: el.dataset.id });
        await loadModels();
        repaintSheet();
        toast("已设为生图模型");
      } catch (err) { el.disabled = false; toast(err.message, true); }
      return true;
    }
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
      // 生图模型不看上下文 / 最大输出（那两个字段在生图表单里不显示）
      const problem = !body.model ? "请填写模型 ID。"
        : body.use === "chat" && body.max_tokens >= body.context_window ? "最大输出须小于上下文长度。"
        : "";
      if (problem) return showErr("mo-err", problem), true;
      el.disabled = true;
      try {
        await api("PUT", `/api/settings/model-list/${encodeURIComponent(edit.draft.id)}`, body);
        const wasNew = edit.isNew;
        state.mdlEdit = null;
        await reloadAll();
        toast(body.use === "image"
          ? (wasNew ? "已添加生图模型，可在模型行点「设为生图模型」" : "已保存")
          : (wasNew ? "已添加，可在「专岗」中分配" : "已保存"));
      } catch (err) { el.disabled = false; showErr("mo-err", err.message); }
      return true;
    }
    case "mdl-model-verify": {
      const id = el.dataset.id;
      const row = el.closest(".mdl-model");
      const out = row && row.querySelector(".mdl-verify-result");
      const label = el.textContent;
      el.disabled = true;
      el.textContent = "正在验证…";
      if (out) out.hidden = true;
      try {
        const r = await api("POST", `/api/settings/model-list/${encodeURIComponent(id)}/verify`, {});
        if (!el.isConnected || !out) return true;
        state.mdlVerification = { ...(state.mdlVerification || {}), [id]: r };
        out.textContent = verificationMessage(r);
        out.hidden = false;
        if (r.ok && !r.stale && r.suggested_max_tokens) {
          const save = document.createElement("button");
          save.className = "btn small";
          save.style.minHeight = "44px";
          save.style.margin = "8px 0";
          save.dataset.act = "mdl-model-apply-limit";
          save.dataset.id = id;
          save.textContent = `应用建议值 ${r.suggested_max_tokens}`;
          out.appendChild(document.createElement("br"));
          out.appendChild(save);
        }
      } catch (e) {
        if (out && el.isConnected) { out.textContent = `验证未完成：${e.message}`; out.hidden = false; }
      } finally {
        if (el.isConnected) { el.disabled = false; el.textContent = label; }
      }
      return true;
    }
    case "mdl-model-apply-limit": {
      const id = el.dataset.id;
      const r = (state.mdlVerification || {})[id];
      const row = el.closest(".mdl-model");
      const out = row && row.querySelector(".mdl-verify-result");
      if (!r || !r.suggested_max_tokens) return toast("已过期，请重新验证", true), true;
      el.disabled = true;
      try {
        await api("PUT", `/api/settings/model-list/${encodeURIComponent(id)}`, {
          max_tokens: r.suggested_max_tokens, if_fingerprint: r.requested_fingerprint,
        });
        await loadModels();
        const saved = state.mdl && state.mdl.models.find((x) => x.id === id);
        if (!saved || Number(saved.max_tokens) !== Number(r.suggested_max_tokens)) throw new Error("无法确认已生效");
        const applied = { ...r, suggested_max_tokens: 0 };
        state.mdlVerification = { ...(state.mdlVerification || {}), [id]: applied };
        if (out) { out.textContent = `最大输出已改为 ${saved.max_tokens}。${verificationMessage(applied)}`; out.hidden = false; }
        if (row) row.querySelector(".mdl-meta").textContent = modelMeta(saved);
      } catch (e) {
        if (out) {
          // 保留建议按钮，失败原因就在当前区域，不擦掉其他模型的未保存表单。
          const error = document.createElement("div");
          error.textContent = `保存失败：${e.message}`;
          out.appendChild(error);
          out.hidden = false;
        }
        if (el.isConnected) el.disabled = false;
      }
      return true;
    }
    case "mdl-model-del": {
      const x = m && m.models.find((y) => y.id === el.dataset.id);
      if (!x) return true;
      const users = usersOf(x.id);
      if (users.length) return toast(`${users.join("、")} 正在使用该模型，请先在「专岗」中更换`, true), true;
      if (!confirm(`删除「${x.name || x.model}」？`)) return true;
      try { await api("DELETE", `/api/settings/model-list/${encodeURIComponent(x.id)}`); await reloadAll(); toast("已删除"); }
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
