// MaiWork 网页 · 设置 ·「模型」页里的「快速判断」：Jev 这类判断模型用哪家服务。
// 后端：GET /api/settings/jev、PUT /api/settings/jev/use、
//       PUT|DELETE /api/settings/jev/endpoints/{id}、POST /api/settings/jev/endpoints/{id}/test。
// 密钥只进不出：响应只有 key_set，表单里的密钥框永远是空的。
import { state, $ } from "../state.js";
import { esc, ico, SVG, tip, tipLabel, toast, msText } from "../util.js";
import { api } from "../api.js";
import { repaintSheet } from "../sheet.js";

export const JEV_PROTOCOLS = {
  systemone: { name: "Jev 格式（System One）", hint: "TypeSafe 官方、OpenRouter、OpenCode、Vercel、Upstage 等服务均使用此格式。" },
  openai_decisions: { name: "OpenAI Decisions", hint: "OpenAI 的 /v1/decisions 接口。" },
};
const protoName = (p) => (JEV_PROTOCOLS[p] || { name: p || "未知格式" }).name;
const LOCAL_HOST = /^http:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(\/|$)/i;

export async function loadJev() {
  state.jevError = "";
  try {
    state.jev = await api("GET", "/api/settings/jev");
  } catch (err) {
    state.jevError = err.message;
  }
}

export function jevFormSheet() {
  const v = state.jev;
  if (!v) return "";
  if (state.jevEdit) return jevForm(state.jevEdit.draft, !!state.jevEdit.isNew);
  if (state.jevPick) return pickBox(presetsLeft(v));
  return "";
}

export function jevSection() {
  const v = state.jev;
  const inSheet = state.sheet === "settings-form";
  const edit = inSheet ? null : state.jevEdit;
  const left = v ? presetsLeft(v) : [];
  const picking = !inSheet && !!(v && state.jevPick && !edit);
  // 平时只给一个「添加」：还有没接的预设就先让人挑一家，全接过了直接进自定义表单
  const addBtn = !v || picking || (edit && edit.isNew) ? "" : `<button class="btn small" data-act="${left.length ? "jev-pick" : "jev-new"}">添加</button>`;
  const head = `<div class="h-sub-row"><h2 class="h-sub">快速判断${tip("Jev 等判断模型可在一两秒内回答「是否」「选哪个」类问题，比主模型更快、成本更低。同一时间只能使用一个服务。")}</h2>${addBtn}</div>`;
  if (!v) return head + (state.jevError ? `<p class="err" role="alert">${esc(state.jevError)}</p>` : `<p class="fine">加载中…</p>`);
  const eps = v.endpoints || [];
  const rows = eps.map((ep) => (edit && !edit.isNew && edit.id === ep.id ? jevForm(edit.draft, false) : jevRow(ep, v.use))).join("");
  return `${head}
    ${v.use && !eps.some((e) => e.id === v.use) ? `<div class="warn-box">所选服务已不存在，快速判断暂由主模型处理。请重新选择服务。</div>` : ""}
    ${v.enabled === false ? `<p class="fine">快速判断已在「全部设置」中关闭，重新开启后以下选择才会生效。</p>` : ""}
    ${picking ? pickBox(left) : ""}
    ${edit && edit.isNew ? jevForm(edit.draft, true) : ""}
    ${rows ? `<div class="set-group">${rows}</div>` : ""}
    ${quickJudgeBlock(v.quick_judge)}`;
}

// 「添加」后的挑选框：没接的预设一行一个，最后是自定义
function pickBox(left) {
  const opt = (act, id, name, note, tag, logo) => `<button type="button" class="jev-pick" data-act="${act}"${id ? ` data-id="${esc(id)}"` : ""}>
      <span class="jev-pick-ico">${logo || ico("gear")}</span>
      <span class="jev-pick-name">${esc(name)}${tag ? `<span class="tag">${esc(tag)}</span>` : ""}</span>
      ${note ? `<span class="jev-pick-note${act === "jev-preset" ? " mono" : ""}">${esc(note)}</span>` : ""}
      <span class="jev-pick-go" aria-hidden="true">${SVG.right}</span>
    </button>`;
  return `<div class="login ext-form" id="jv-pick">
    <div class="ext-form-h">选择服务${tip("预设服务已填好地址和模型，只需填写密钥。")}</div>
    <div class="jev-picks" role="group" aria-label="可添加的服务">
      ${left.map((p) => opt("jev-preset", p.id, p.name, p.model, p.protocol === "systemone" || protoName(p.protocol) === p.name ? "" : protoName(p.protocol), logoOf(p))).join("")}
      ${opt("jev-new", "", "自定义", "手动填写地址和接口格式", "", "")}
    </div>
    <div class="actions"><button class="btn" type="button" data-act="jev-pick-cancel">取消</button></div>
  </div>`;
}

// 没配 Jev / Jev 拿不准时：用模型快速判断群里 @ 的是不是派活（[quick_judge]）
function quickJudgeBlock(q) {
  if (!q) return "";
  const models = (state.mdl && state.mdl.models) || [];
  const opts = [`<option value=""${q.model ? "" : " selected"}>与主模型相同</option>`]
    .concat(models.map((m) => `<option value="${esc(m.id)}"${m.id === q.model ? " selected" : ""}>${esc(m.name || m.model)}</option>`));
  if (q.model && !models.some((m) => m.id === q.model)) opts.push(`<option value="${esc(q.model)}" selected>（模型已删除，将使用主模型）</option>`);
  return `<div class="login ext-form mdl-form" id="qj-form" style="margin-top:18px">
    <div class="ext-form-h">任务识别备用方案${tip("未配置 Jev 或 Jev 无法确定时，使用模型判断群内 @ 消息是否为任务请求。判断在后台进行，不影响 MaiBot 回复。")}</div>
    ${q.today ? `<p class="fine" style="margin:0">今日已判断 ${esc(q.today)} 次</p>` : ""}
    <label class="chk"><input id="qj-enabled" type="checkbox"${q.enabled ? " checked" : ""} />启用</label>
    ${tipLabel("qj-model", "判断模型", "判断任务简单，选择成本低、速度快的模型即可。")}
    <select id="qj-model">${opts.join("")}</select>
    <div class="lbl-row"><label class="chk"><input id="qj-keyword" type="checkbox"${q.keyword_filter ? " checked" : ""} />关键词预筛选</label>${tip("不含「帮我」「整理」「查一下」「提醒我」等请求词的 @ 消息直接视为闲聊，不产生调用费用；表述含糊的任务请求可能被遗漏。")}</div>
    <div class="actions"><button class="btn primary" type="button" data-act="qj-save">保存</button></div>
  </div>`;
}

function keyText(ep) {
  if (!ep.key_set) return "密钥<b>未填写</b>";
  if (ep.key_source === "env") return "密钥来自服务器环境变量";
  return "密钥已填写";
}

// 预设自带的图标（后端给 logo 路径）；名字就在旁边，所以 alt 留空
function logoOf(p, cls) {
  return p && p.logo ? `<img class="jev-logo ${cls || ""}" src="${esc(p.logo)}" alt="" loading="lazy" />` : "";
}

function jevRow(ep, use) {
  const inUse = ep.id === use;
  return `<div class="set-row" data-jid="${esc(ep.id)}">${logoOf(presetOf(ep.preset)) || ico(inUse ? "sparkles" : "link")}<div>
      <div class="set-name">${esc(ep.name || ep.id)}${(ep.protocol || "systemone") === "systemone" ? "" : `<span class="tag">${esc(protoName(ep.protocol))}</span>`}${inUse ? `<span class="tag">正在使用</span>` : ""}</div>
      <div class="set-text"><span class="mono">${esc(ep.model || "未填写模型")}</span> · ${keyText(ep)}</div>
      <div class="set-text mono-link">${esc(ep.url || "")}</div>
    </div><span class="row-btns">
      ${inUse ? "" : `<button class="btn small" data-act="jev-use" data-id="${esc(ep.id)}">切换至此</button>`}
      <button class="btn small" data-act="jev-edit" data-id="${esc(ep.id)}">修改</button>
      ${ep.builtin || inUse ? "" : `<button class="btn small ghost" data-act="jev-del" data-id="${esc(ep.id)}">删除</button>`}
    </span></div>`;
}

// 预设里还没接上的（按预设 id 认；内置 typesafe 永远在列表里）
function presetsLeft(v) {
  const used = new Set();
  for (const ep of v.endpoints || []) { used.add(ep.id); if (ep.preset) used.add(ep.preset); }
  return (v.presets || []).filter((p) => !used.has(p.id));
}

function presetOf(id) {
  return ((state.jev && state.jev.presets) || []).find((p) => p.id === id) || null;
}

function jevForm(d, isNew) {
  const p = presetOf(d.preset);
  const builtin = !!d.builtin;
  const models = (p && p.models) || [];
  const docs = (p && p.docs_url) || "";
  const keyUrl = (p && p.key_url) || "";
  const proto = d.protocol || "systemone";
  return `<div class="login ext-form mdl-form" id="jv-form">
    <div class="ext-form-h">${logoOf(p, "jev-logo-h")}${isNew ? (p ? `添加「${esc(p.name)}」` : "自定义判断服务") : `修改「${esc(d.name || d.id)}」`}${p && p.note ? tip(esc(p.note)) : ""}</div>
    ${docs || keyUrl ? `<p class="fine" style="margin:0">${docs ? `<a href="${esc(docs)}" target="_blank" rel="noopener">文档</a>` : ""}${docs && keyUrl ? " · " : ""}${keyUrl ? `<a href="${esc(keyUrl)}" target="_blank" rel="noopener">获取密钥</a>` : ""}</p>` : ""}
    <label for="jv-name">名称</label><input id="jv-name" maxlength="40" value="${esc(d.name || "")}" placeholder="例如：自建判断服务"${builtin ? " readonly" : ""} />
    ${tipLabel("jv-proto", "接口格式", esc((JEV_PROTOCOLS[proto] || JEV_PROTOCOLS.systemone).hint))}
    <select id="jv-proto"${builtin ? " disabled" : ""}>${Object.entries(JEV_PROTOCOLS).map(([k, x]) => `<option value="${k}"${k === proto ? " selected" : ""}>${esc(x.name)}</option>`).join("")}</select>
    <label for="jv-url">地址</label><input id="jv-url" type="url" inputmode="url" spellcheck="false" value="${esc(d.url || "")}" placeholder="https://…/v1/systemone" />
    ${tipLabel("jv-model", "模型", models.length > 1 ? `可选：${models.map(esc).join("、")}` : "")}
    <input id="jv-model" list="jv-models" spellcheck="false" autocomplete="off" value="${esc(d.model || "")}" placeholder="例如 jev-1.13" />
    <datalist id="jv-models">${models.map((x) => `<option value="${esc(x)}"></option>`).join("")}</datalist>
    <label for="jv-key">密钥</label><input id="jv-key" type="password" autocomplete="new-password" placeholder="${d.key_set ? "已填写，留空则不修改" : "输入密钥"}" />
    ${d.key_source === "env" ? `<p class="fine" style="margin:2px 0 0">当前使用服务器环境变量中的密钥，此处填写的密钥不会生效。</p>` : ""}
    <div class="actions" style="margin-top:4px"><button class="btn" type="button" data-act="jev-test">测试连接</button>${tip("测试会实际调用一次服务，产生少量费用。")}<span class="fine" id="jv-check" role="status" aria-live="polite" style="margin:0;align-self:center"></span></div>
    <p class="err" id="jv-err" role="alert" hidden></p>
    <div class="actions"><button class="btn primary" type="button" data-act="jev-save">保存</button><button class="btn" type="button" data-act="jev-cancel">取消</button></div>
  </div>`;
}

function showErr(text) {
  const e = $("jv-err");
  if (!e) return toast(text, true);
  e.textContent = text;
  e.hidden = false;
}

// 读表单 + 检查；不合格抛中文错误
function readForm(edit) {
  const v = (id) => ($(id) && typeof $(id).value === "string" ? $(id).value.trim() : "");
  const d = edit.draft || {};
  const out = {
    name: v("jv-name") || d.name || "",
    preset: d.preset || "",
    protocol: d.builtin ? "systemone" : v("jv-proto") || d.protocol || "systemone",
    url: v("jv-url"),
    model: v("jv-model"),
  };
  const key = v("jv-key");
  if (!/^https:\/\//i.test(out.url) && !LOCAL_HOST.test(out.url)) throw new Error("地址须以 https:// 开头（本机服务可使用 http://localhost）。");
  if (/[{}]/.test(out.url)) throw new Error("地址中仍有未替换的占位符（例如 {account_id}），请替换为实际值。");
  if (!out.model) throw new Error("请填写模型。");
  if (out.model.length > 200) throw new Error("模型名称过长。");
  if (key) out.api_key = key;
  return { body: out, key };
}

function newId() {
  return "j" + Math.random().toString(36).slice(2, 8).replace(/[^a-z0-9]/g, "0");
}

function freeId(base) {
  const taken = new Set(((state.jev && state.jev.endpoints) || []).map((e) => e.id));
  if (!taken.has(base)) return base;
  for (let i = 2; i < 100; i++) if (!taken.has(`${base}-${i}`)) return `${base}-${i}`;
  return newId();
}

export async function actJev(action, el) {
  const v = state.jev;
  const edit = state.jevEdit;
  const id = el && el.dataset ? el.dataset.id : "";
  switch (action) {
    case "jev-pick":
      state.jevPick = true;
      state.jevEdit = null;
      repaintSheet();
      return true;
    case "jev-pick-cancel":
      state.jevPick = false;
      repaintSheet();
      return true;
    case "jev-new":
      state.jevPick = false;
      state.jevEdit = { isNew: true, id: "", draft: { id: freeId(newId()), preset: "", protocol: "systemone", url: "", model: "" } };
      state.jevEdit.id = state.jevEdit.draft.id;
      repaintSheet();
      return true;
    case "jev-preset": {
      const p = presetOf(id);
      if (!p) return true;
      state.jevPick = false;
      const jid = freeId(p.id);
      state.jevEdit = { isNew: true, id: jid, draft: { id: jid, preset: p.id, name: p.name, protocol: p.protocol, url: p.url, model: p.model } };
      repaintSheet();
      return true;
    }
    case "jev-edit": {
      const ep = v && (v.endpoints || []).find((x) => x.id === id);
      if (!ep) return true;
      state.jevPick = false;
      state.jevEdit = { isNew: false, id: ep.id, draft: { ...ep } };
      repaintSheet();
      return true;
    }
    case "jev-cancel":
      state.jevEdit = null;
      repaintSheet();
      return true;
    case "jev-save": {
      if (!edit) return true;
      let form;
      try { form = readForm(edit); } catch (err) { showErr(err.message); return true; }
      if (edit.isNew && !form.key) { showErr("新添加的服务须填写密钥。"); return true; }
      el.disabled = true;
      try {
        state.jev = await api("PUT", `/api/settings/jev/endpoints/${encodeURIComponent(edit.id)}`, form.body);
        state.jevEdit = null;
        toast("已保存");
        repaintSheet();
      } catch (err) {
        showErr(err.message);
      } finally {
        el.disabled = false;
      }
      return true;
    }
    case "jev-test": {
      if (!edit) return true;
      const out = $("jv-check");
      let form;
      try { form = readForm(edit); } catch (err) { showErr(err.message); return true; }
      const body = { protocol: form.body.protocol, url: form.body.url, model: form.body.model };
      if (form.key) body.api_key = form.key;
      if (out) { out.textContent = "正在测试…"; out.style.color = ""; }
      el.disabled = true;
      try {
        const r = await api("POST", `/api/settings/jev/endpoints/${encodeURIComponent(edit.id)}/test`, body);
        if (out) {
          out.textContent = r && r.ok ? `连接成功，耗时 ${msText(r.ms || 0)}` : (r && r.error) || "连接失败";
          out.style.color = r && r.ok ? "" : "var(--red)";
        }
      } catch (err) {
        if (out) { out.textContent = err.message; out.style.color = "var(--red)"; }
      } finally {
        el.disabled = false;
      }
      return true;
    }
    case "jev-use":
      try {
        state.jev = await api("PUT", "/api/settings/jev/use", { id });
        toast("已切换");
        repaintSheet();
      } catch (err) {
        toast(err.message, true);
      }
      return true;
    case "jev-del": {
      const ep = v && (v.endpoints || []).find((x) => x.id === id);
      if (!confirm(`删除「${(ep && ep.name) || id}」？`)) return true;
      try {
        state.jev = await api("DELETE", `/api/settings/jev/endpoints/${encodeURIComponent(id)}`);
        toast("已删除");
        repaintSheet();
      } catch (err) {
        toast(err.message, true);
      }
      return true;
    }
    case "qj-save": {
      const q = (v && v.quick_judge) || {};
      const box = (x) => ($(x) && typeof $(x).checked === "boolean" ? $(x).checked : null);
      const enabled = box("qj-enabled");
      const keyword = box("qj-keyword");
      const body = {
        "quick_judge.enabled": enabled === null ? !!q.enabled : enabled,
        "quick_judge.model": $("qj-model") ? String($("qj-model").value) : String(q.model || ""),
        "quick_judge.keyword_filter": keyword === null ? !!q.keyword_filter : keyword,
      };
      el.disabled = true;
      try {
        await api("PUT", "/api/settings/config", body);
        await loadJev();
        toast("已保存");
        repaintSheet();
      } catch (err) {
        toast(err.message, true);
      } finally {
        el.disabled = false;
      }
      return true;
    }
    default:
      return false;
  }
}
