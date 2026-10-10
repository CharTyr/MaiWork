// MaiWork 网页 · 设置 ·「模型」页里的「快速判断」：Jev 这类判断模型用哪家服务。
// 后端：GET /api/settings/jev、PUT /api/settings/jev/use、
//       PUT|DELETE /api/settings/jev/endpoints/{id}、POST /api/settings/jev/endpoints/{id}/test。
// 密钥只进不出：响应只有 key_set，表单里的密钥框永远是空的。
import { state, $ } from "../state.js";
import { esc, ico, toast, msText } from "../util.js";
import { api } from "../api.js";
import { repaintSheet } from "../sheet.js";

export const JEV_PROTOCOLS = {
  systemone: { name: "Jev 格式（System One）", hint: "TypeSafe 官方、OpenRouter、OpenCode、Vercel、Upstage 等都是这种" },
  openai_decisions: { name: "OpenAI Decisions", hint: "OpenAI 的 /v1/decisions" },
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

export function jevSection() {
  const v = state.jev;
  const head = `<div class="h-sub-row"><h2 class="h-sub">快速判断</h2>${v ? `<button class="btn small" data-act="jev-new">自定义</button>` : ""}</div>
    <p class="h-meta">Jev 这类判断模型：一两秒答「是不是、选哪个」，比主模型快、便宜。只能选一家用</p>`;
  if (!v) return head + (state.jevError ? `<p class="err" role="alert">${esc(state.jevError)}</p>` : `<p class="fine">读取中…</p>`);
  const edit = state.jevEdit;
  const eps = v.endpoints || [];
  return `${head}
    ${v.use && !eps.some((e) => e.id === v.use) ? `<div class="warn-box">选的服务不见了，快速判断现在走主模型。在下面换一个</div>` : ""}
    ${v.enabled === false ? `<p class="fine">快速判断已在「全部设置」里关掉，下面的选择开回来才生效</p>` : ""}
    ${edit && edit.isNew ? jevForm(edit.draft, true) : ""}
    ${eps.map((ep) => (edit && !edit.isNew && edit.id === ep.id ? jevForm(edit.draft, false) : jevRow(ep, v.use))).join("")}
    ${presetsLeft(v).length ? `<div class="set-text" style="margin-top:14px">还可以接：</div>${presetsLeft(v).map(presetRow).join("")}` : ""}
    ${quickJudgeBlock(v.quick_judge)}`;
}

// 没配 Jev / Jev 拿不准时：用模型快速判断群里 @ 的是不是派活（[quick_judge]）
function quickJudgeBlock(q) {
  if (!q) return "";
  const models = (state.mdl && state.mdl.models) || [];
  const opts = [`<option value=""${q.model ? "" : " selected"}>跟主模型一样</option>`]
    .concat(models.map((m) => `<option value="${esc(m.id)}"${m.id === q.model ? " selected" : ""}>${esc(m.name || m.model)}</option>`));
  if (q.model && !models.some((m) => m.id === q.model)) opts.push(`<option value="${esc(q.model)}" selected>（已删掉的模型，会改用主模型）</option>`);
  return `<div class="login ext-form mdl-form" id="qj-form" style="margin-top:18px">
    <div class="ext-form-h">没有 Jev 时怎么判断派活</div>
    <p class="fine" style="margin:0">没配 Jev、或 Jev 拿不准时，用模型判断群里 @ 的是不是派活。在后台判，不耽误 MaiBot 说话${q.today ? ` · 今天判断 ${esc(q.today)} 次` : ""}</p>
    <label class="chk"><input id="qj-enabled" type="checkbox"${q.enabled ? " checked" : ""} />打开</label>
    <label for="qj-model">用哪个模型</label>
    <select id="qj-model">${opts.join("")}</select>
    <p class="fine" style="margin:2px 0 0">选个便宜快的就够，判断很简单</p>
    <label class="chk"><input id="qj-keyword" type="checkbox"${q.keyword_filter ? " checked" : ""} />先用请求词过一遍</label>
    <p class="fine" style="margin:2px 0 0">没有「帮我、整理、查一下、提醒我」这类词的 @ 直接当闲聊，不花钱；说法含糊的派活可能漏掉</p>
    <label for="qj-max">每群每天最多判断几次</label>
    <input id="qj-max" type="number" min="0" max="500" value="${esc(q.daily_max ?? 30)}" />
    <p class="err" id="qj-err" role="alert" hidden></p>
    <div class="actions"><button class="btn primary" type="button" data-act="qj-save">保存</button></div>
  </div>`;
}

function keyText(ep) {
  if (!ep.key_set) return "密钥<b>没填</b>";
  if (ep.key_source === "env") return "密钥在服务器环境变量里";
  return "密钥已填";
}

function jevRow(ep, use) {
  const inUse = ep.id === use;
  return `<div class="set-row" data-jid="${esc(ep.id)}">${ico(inUse ? "sparkles" : "link")}<div>
      <div class="set-name">${esc(ep.name || ep.id)}<span class="tag">${esc(protoName(ep.protocol))}</span>${inUse ? `<span class="tag">正在用</span>` : ""}</div>
      <div class="set-text"><span class="mono">${esc(ep.model || "没填模型")}</span> · ${keyText(ep)}</div>
      <div class="set-text mono-link">${esc(ep.url || "")}</div>
    </div><span class="row-btns">
      ${inUse ? "" : `<button class="btn small" data-act="jev-use" data-id="${esc(ep.id)}">换用</button>`}
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

function presetRow(p) {
  return `<div class="set-row">${ico("link")}<div>
      <div class="set-name">${esc(p.name)}<span class="tag">${esc(protoName(p.protocol))}</span></div>
      <div class="set-text"><span class="mono">${esc(p.model)}</span>${p.note ? ` · ${esc(p.note)}` : ""}</div>
    </div><span class="row-btns"><button class="btn small" data-act="jev-preset" data-id="${esc(p.id)}">接上</button></span></div>`;
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
    <div class="ext-form-h">${isNew ? (p ? `接上「${esc(p.name)}」` : "自定义判断服务") : `修改「${esc(d.name || d.id)}」`}</div>
    ${p && p.note ? `<p class="fine" style="margin:0">${esc(p.note)}</p>` : ""}
    ${docs || keyUrl ? `<p class="fine" style="margin:2px 0 0">${docs ? `<a href="${esc(docs)}" target="_blank" rel="noopener">看文档</a>` : ""}${docs && keyUrl ? " · " : ""}${keyUrl ? `<a href="${esc(keyUrl)}" target="_blank" rel="noopener">拿密钥</a>` : ""}</p>` : ""}
    <label for="jv-name">名称</label><input id="jv-name" maxlength="40" value="${esc(d.name || "")}" placeholder="如：自己搭的判断模型"${builtin ? " readonly" : ""} />
    <label for="jv-proto">接口格式</label>
    <select id="jv-proto"${builtin ? " disabled" : ""}>${Object.entries(JEV_PROTOCOLS).map(([k, x]) => `<option value="${k}"${k === proto ? " selected" : ""}>${esc(x.name)}</option>`).join("")}</select>
    <p class="fine" style="margin:2px 0 0">${esc((JEV_PROTOCOLS[proto] || JEV_PROTOCOLS.systemone).hint)}</p>
    <label for="jv-url">地址</label><input id="jv-url" type="url" inputmode="url" spellcheck="false" value="${esc(d.url || "")}" placeholder="https://…/v1/systemone" />
    <label for="jv-model">模型</label>
    <input id="jv-model" list="jv-models" spellcheck="false" autocomplete="off" value="${esc(d.model || "")}" placeholder="如 jev-1.13" />
    <datalist id="jv-models">${models.map((x) => `<option value="${esc(x)}"></option>`).join("")}</datalist>
    ${models.length > 1 ? `<p class="fine" style="margin:2px 0 0">可选：${models.map(esc).join("、")}</p>` : ""}
    <label for="jv-key">密钥</label><input id="jv-key" type="password" autocomplete="new-password" placeholder="${d.key_set ? "已填，留空不改" : "粘贴密钥"}" />
    ${d.key_source === "env" ? `<p class="fine" style="margin:2px 0 0">服务器环境变量里的密钥优先，这里填的不会生效</p>` : ""}
    <div class="actions" style="margin-top:4px"><button class="btn" type="button" data-act="jev-test">测试</button><span class="fine" id="jv-check" role="status" aria-live="polite" style="margin:0;align-self:center"></span></div>
    <p class="fine" style="margin:2px 0 0">测试会真问一次，花一点点钱</p>
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
  if (!/^https:\/\//i.test(out.url) && !LOCAL_HOST.test(out.url)) throw new Error("地址要以 https:// 开头（本机自己搭的可以用 http://localhost）");
  if (/[{}]/.test(out.url)) throw new Error("地址里还有没替换的占位（比如 {account_id}），换成你自己的");
  if (!out.model) throw new Error("模型不能空着");
  if (out.model.length > 200) throw new Error("模型名太长了");
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
    case "jev-new":
      state.jevEdit = { isNew: true, id: "", draft: { id: freeId(newId()), preset: "", protocol: "systemone", url: "", model: "" } };
      state.jevEdit.id = state.jevEdit.draft.id;
      repaintSheet();
      return true;
    case "jev-preset": {
      const p = presetOf(id);
      if (!p) return true;
      const jid = freeId(p.id);
      state.jevEdit = { isNew: true, id: jid, draft: { id: jid, preset: p.id, name: p.name, protocol: p.protocol, url: p.url, model: p.model } };
      repaintSheet();
      return true;
    }
    case "jev-edit": {
      const ep = v && (v.endpoints || []).find((x) => x.id === id);
      if (!ep) return true;
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
      if (edit.isNew && !form.key) { showErr("新接的服务要填密钥"); return true; }
      el.disabled = true;
      try {
        state.jev = await api("PUT", `/api/settings/jev/endpoints/${encodeURIComponent(edit.id)}`, form.body);
        state.jevEdit = null;
        toast("存好了");
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
      if (out) { out.textContent = "在问…"; out.style.color = ""; }
      el.disabled = true;
      try {
        const r = await api("POST", `/api/settings/jev/endpoints/${encodeURIComponent(edit.id)}/test`, body);
        if (out) {
          out.textContent = r && r.ok ? `通了，用了 ${msText(r.ms || 0)}` : (r && r.error) || "没通";
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
        toast("换好了");
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
        toast("删掉了");
        repaintSheet();
      } catch (err) {
        toast(err.message, true);
      }
      return true;
    }
    case "qj-save": {
      const q = (v && v.quick_judge) || {};
      const box = (x) => ($(x) && typeof $(x).checked === "boolean" ? $(x).checked : null);
      const raw = $("qj-max") ? String($("qj-max").value).trim() : String(q.daily_max ?? 30);
      const max = /^\d+$/.test(raw) ? parseInt(raw, 10) : NaN;
      if (!Number.isFinite(max) || max > 500) {
        const e = $("qj-err");
        if (e) { e.textContent = "每天次数填 0 到 500 的整数"; e.hidden = false; } else toast("每天次数填 0 到 500 的整数", true);
        return true;
      }
      const enabled = box("qj-enabled");
      const keyword = box("qj-keyword");
      const body = {
        "quick_judge.enabled": enabled === null ? !!q.enabled : enabled,
        "quick_judge.model": $("qj-model") ? String($("qj-model").value) : String(q.model || ""),
        "quick_judge.keyword_filter": keyword === null ? !!q.keyword_filter : keyword,
        "quick_judge.daily_max": max,
      };
      el.disabled = true;
      try {
        await api("PUT", "/api/settings/config", body);
        await loadJev();
        toast("存好了");
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
