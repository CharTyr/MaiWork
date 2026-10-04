// MaiWork 网页 · 设置 ·「全部配置」：config.toml 的每一项。
import { $, state } from "../state.js";
import { SVG, esc, toast } from "../util.js";
import { api } from "../api.js";
import { loading } from "../pages/news.js";

/* ───── 全部配置（config.toml 的每一项；网页改的直接写进插件的 config.toml） ───── */
// 后端 GET /api/settings/config：{file, sections:[{id,label,fields:[{key,label,help,type,value,default,changed,...}]}], reload_pending:[]}
// 密钥字段只给 {set, source: env|file|none}，值只进不出
const CFG_CHIPS = { "approval.admins": "accounts", "approval.exempt_users": "accounts", "approval.exempt_groups": "groups", "feeds.news_slots": "times" };
const cfgKind = (f) => CFG_CHIPS[f.key] || (f.type === "time_list" ? "times" : f.type);
const cfgId = (key) => `c-${String(key).replace(/[^a-z0-9_]/gi, "-")}`;
const SRC_NAMES = { file: "config.toml 里", env: "服务器环境变量", none: "" };

export async function loadRules() {
  try {
    state.rules = await api("GET", "/api/settings/config");
  } catch (e) {
    state.rules = null;
    toast(e.message, true);
  }
}

function cfgField(key) {
  for (const s of (state.rules && state.rules.sections) || []) for (const f of s.fields || []) if (f.key === key) return f;
  return null;
}

const PLATFORM_NAMES = { qq: "QQ", telegram: "Telegram", discord: "Discord", wechat: "微信", kook: "KOOK" };
// 「平台:账号」（MaiBot 标准写法）；只写数字当 qq；认不出返回 ""
function normAccount(raw) {
  const t = String(raw || "").trim();
  if (!t) return "";
  if (!t.includes(":")) return /^\d+$/.test(t) ? `qq:${t}` : "";
  const i = t.indexOf(":");
  const plat = t.slice(0, i).trim().toLowerCase();
  const acc = t.slice(i + 1).trim();
  return /^[a-z][a-z0-9_]{0,15}$/.test(plat) && /^\S{1,64}$/.test(acc) ? `${plat}:${acc}` : "";
}

function chipHTML(kind, v) {
  let label = esc(v);
  if (kind !== "times") {
    const i = v.indexOf(":");
    const plat = v.slice(0, i);
    label = `<span class="ci-plat">${esc(PLATFORM_NAMES[plat] || plat)}</span>${esc(v.slice(i + 1))}`;
  }
  return `<span class="ci" data-v="${esc(v)}">${label}<button type="button" class="ci-x" data-act="chip-del" aria-label="删掉 ${esc(v)}">${SVG.close}</button></span>`;
}

export function chipEditor(id, kind, vals) {
  const items = (vals || []).map((v) => chipHTML(kind, kind === "times" ? v : normAccount(v) || v)).join("");
  const adder =
    kind === "times"
      ? `<input type="time" class="ca-time" step="60" aria-label="新增时间" />`
      : `<input class="ca-plat" value="qq" list="mw-platforms" spellcheck="false" aria-label="平台" /><span class="ca-colon">:</span><input class="ca-acc" inputmode="${kind === "groups" ? "numeric" : "text"}" spellcheck="false" placeholder="${kind === "groups" ? "群号" : "账号"}" aria-label="${kind === "groups" ? "群号" : "账号"}" />`;
  return `
    <div class="chips" id="${id}" data-kind="${kind}">
      <div class="ci-list">${items || `<span class="ci-empty">还没有</span>`}</div>
      <div class="ci-add">${adder}<button type="button" class="btn small" data-act="chip-add">加上</button></div>
      <p class="ci-err" hidden></p>
    </div>`;
}

export function chipAdd(box) {
  const kind = box.dataset.kind;
  const err = box.querySelector(".ci-err");
  let v = "";
  if (kind === "times") {
    v = (box.querySelector(".ca-time").value || "").slice(0, 5);
    if (!/^\d{2}:\d{2}$/.test(v)) v = "";
  } else {
    const plat = box.querySelector(".ca-plat").value.trim() || "qq";
    const acc = box.querySelector(".ca-acc").value.trim();
    v = acc ? normAccount(`${plat}:${acc}`) : "";
    if (kind === "groups" && v && !/^[a-z][a-z0-9_]*:\d+$/.test(v)) v = "";
  }
  if (!v) {
    err.textContent = kind === "times" ? "选一个时间" : kind === "groups" ? "填群号（数字）" : "填账号，平台默认 qq";
    err.hidden = false;
    return;
  }
  err.hidden = true;
  const list = box.querySelector(".ci-list");
  if ([...list.querySelectorAll(".ci")].some((c) => c.dataset.v === v)) {
    err.textContent = "已经有了";
    err.hidden = false;
    return;
  }
  const empty = list.querySelector(".ci-empty");
  if (empty) empty.remove();
  list.insertAdjacentHTML("beforeend", chipHTML(kind, v));
  if (kind === "times") {
    const all = [...list.querySelectorAll(".ci")].sort((a, b) => a.dataset.v.localeCompare(b.dataset.v));
    all.forEach((c) => list.appendChild(c));
    box.querySelector(".ca-time").value = "";
  } else box.querySelector(".ca-acc").value = "";
}

// 多行表格编辑器（服务的群、SSH 服务器）：cols = [[字段, 占位提示], ...]
export function rowsEditor(id, cols, rows) {
  const one = (r) => `<div class="re-row">${cols.map(([k, ph]) => `<input data-k="${k}" spellcheck="false" placeholder="${esc(ph)}" value="${esc((r || {})[k] ?? "")}" />`).join("")}<button type="button" class="icon-btn" data-act="re-del" aria-label="删掉这一行">${SVG.trash}</button></div>`;
  return `<div class="rows-ed" id="${id}" data-cols="${esc(JSON.stringify(cols))}"><div class="re-list">${(rows || []).map(one).join("")}</div><button type="button" class="btn small" data-act="re-add">${SVG.plus}加一行</button></div>`;
}
export function rowsAdd(box) {
  const cols = JSON.parse(box.dataset.cols || "[]");
  box.querySelector(".re-list").insertAdjacentHTML("beforeend", `<div class="re-row">${cols.map(([k, ph]) => `<input data-k="${k}" spellcheck="false" placeholder="${esc(ph)}" />`).join("")}<button type="button" class="icon-btn" data-act="re-del" aria-label="删掉这一行">${SVG.trash}</button></div>`);
  const first = box.querySelector(".re-row:last-child input");
  if (first) first.focus();
}
export const ROWS_COLS = {
  serve_groups: [["group", "qq:群号 / telegram:群 ID / qqbot:群 openid"], ["workspace", "工作区名（可空）"]],
  ssh_list: [["name", "名字"], ["host", "user@1.2.3.4:22"], ["note", "配置 / 用途（主模型看得到）"]],
};

function cfgInput(f) {
  const id = cfgId(f.key);
  const kind = cfgKind(f);
  const val = f.value;
  if (f.readonly) {
    const shown = Array.isArray(val) ? (val.length ? val.map((x) => (typeof x === "object" ? Object.values(x).filter(Boolean).join(" ") : x)).join("、") : "（空）") : val === true ? "开" : val === false ? "关" : String(val ?? "");
    return `<span class="mono cfg-ro">${esc(shown)}</span>`;
  }
  if (kind === "accounts" || kind === "groups" || kind === "times") return chipEditor(id, kind, val);
  if (kind === "serve_groups" || kind === "ssh_list") return rowsEditor(id, ROWS_COLS[kind], val);
  if (kind === "bool") return `<label class="switch"><input type="checkbox" id="${id}" ${val ? "checked" : ""} /><span></span></label>`;
  if (kind === "enum") {
    const opts = (f.options || []).map((o) => (typeof o === "object" ? [o.value, o.label || o.value] : [o, o === "" ? "不用" : o]));
    return `<select id="${id}">${opts.map(([v, l]) => `<option value="${esc(v)}" ${v === val ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
  }
  if (kind === "int" || kind === "float") {
    const lim = `${f.min != null ? ` min="${f.min}"` : ""}${f.max != null ? ` max="${f.max}"` : ""}`;
    return `<input id="${id}" type="number" step="${kind === "int" ? 1 : 0.1}"${lim} value="${esc(val ?? "")}" />`;
  }
  if (kind === "list_str") return `<textarea id="${id}" class="cfg-list" rows="3" spellcheck="false" placeholder="一行一个">${esc((val || []).join("\n"))}</textarea>`;
  if (kind === "secret") {
    const src = SRC_NAMES[f.source] || "";
    const pw = f.key === "console.password";
    return `<input id="${id}" type="password" autocomplete="new-password" placeholder="${f.set ? `已设置${src ? `（${src}）` : ""} · 留空就不改` : "没设置 · 粘贴进来"}" />${
      pw ? `<input id="${id}-cur" type="password" autocomplete="current-password" placeholder="改密码要先填现在的密码" style="margin-top:8px" />` : ""
    }${f.source === "file" && f.set && !pw ? `<button type="button" class="link-btn" data-act="cfg-clear" data-f="${esc(f.key)}">清空</button>` : ""}`;
  }
  return `<input id="${id}" spellcheck="false" value="${esc(val ?? "")}" />`;
}

function cfgShow(v) {
  if (Array.isArray(v)) return v.length ? v.map((x) => (typeof x === "object" && x ? Object.values(x).filter(Boolean).join(" ") : x)).join("、") : "（空）";
  if (v === true) return "开";
  if (v === false) return "关";
  return v === "" || v == null ? "（空）" : String(v);
}

function cfgRow(f) {
  const kind = cfgKind(f);
  const wide = ["accounts", "groups", "times", "serve_groups", "ssh_list", "list_str"].includes(kind);
  const over = kind !== "secret" && f.changed;
  return `
    <div class="rule-row${kind === "bool" && !f.readonly ? " is-bool" : ""}${wide ? " is-list" : ""}">
      <div class="rule-l">
        <label for="${cfgId(f.key)}" class="set-name">${esc(f.label || f.key)}${over ? `<span class="tag">改过</span>` : ""}${f.applies === "reload" ? `<span class="tag tag-warn">重载后生效</span>` : ""}</label>
        ${f.help ? `<div class="set-text" id="${cfgId(f.key)}-help">${esc(f.help)}</div>` : ""}
        ${f.readonly ? `<div class="set-text">${SVG.lock} 只能在服务器上改</div>` : ""}
        ${over && !f.readonly ? `<div class="set-text">默认是：<span class="mono">${esc(cfgShow(f.default))}</span> <button type="button" class="link-btn" data-act="rule-reset" data-f="${esc(f.key)}">恢复默认</button></div>` : ""}
      </div>
      <div class="rule-r">${cfgInput(f)}</div>
    </div>`;
}

function cfgSection(s) {
  const fields = s.fields || [];
  const basic = fields.filter(f => !f.advanced);
  const advanced = fields.filter(f => f.advanced);
  return `<h2 class="h-sub" id="cfg-${esc(s.id)}">${esc(s.label)}</h2>` +
    basic.map(cfgRow).join("") + (advanced.length ?
      `<details class="cfg-advanced"><summary>高级 · ${advanced.length} 项</summary><p class="fine">机器资源、处理节奏和服务地址，一般不用改。</p>${advanced.map(cfgRow).join("")}</details>` : "");
}

export function rulesPage() {
  const r = state.rules;
  if (!r) return loading();
  const secs = r.sections || [];
  const pending = r.reload_pending || [];
  const jump = state.cfgSec && secs.some((s) => s.id === state.cfgSec) ? state.cfgSec : "";
  const shown = jump ? secs.filter((s) => s.id === jump) : secs;
  return `
    <p class="h-meta">保存后马上生效</p>
    ${pending.length ? `<div class="warn-box">有 ${pending.length} 项改了还没生效（${pending.map((k) => esc((cfgField(k) || {}).label || k)).join("、")}），要等插件重载后才生效</div>` : ""}
    <div class="cfg-jump" role="tablist"><button type="button" role="tab" aria-selected="${!jump}" data-act="cfg-sec" data-s="">全部</button>${secs.map((s) => `<button type="button" role="tab" aria-selected="${jump === s.id}" data-act="cfg-sec" data-s="${esc(s.id)}">${esc(s.label)}</button>`).join("")}</div>
    <datalist id="mw-platforms">${Object.keys(PLATFORM_NAMES).map((p) => `<option value="${p}">${PLATFORM_NAMES[p]}</option>`).join("")}</datalist>
    <form id="rules-form" class="rules-form" autocomplete="off">
      ${shown.map(cfgSection).join("")}
      <p class="err" id="r-err" hidden></p>
      <div class="actions sticky-actions"><button class="btn primary" type="submit">保存</button></div>
    </form>`;
}

// 只收改动过的字段（没动的不发，免得撞上「只能在文件里改」的旧值）
export function readRules() {
  const out = {};
  for (const s of (state.rules && state.rules.sections) || []) {
    for (const f of s.fields || []) {
      if (f.readonly) continue;
      const kind = cfgKind(f);
      const el = $(cfgId(f.key));
      if (!el) continue;
      let v;
      if (kind === "secret") {
        v = el.value;
        if (!v) continue;
        out[f.key] = v;
        if (f.key === "console.password") {
          const cur = ($(`${cfgId(f.key)}-cur`) || {}).value || "";
          if (!cur) throw new Error("改网页密码要先填现在的密码");
          out.current_password = cur;
        }
        continue;
      }
      if (kind === "bool") v = el.checked;
      else if (kind === "accounts" || kind === "groups" || kind === "times") v = [...el.querySelectorAll(".ci")].map((c) => c.dataset.v);
      else if (kind === "serve_groups" || kind === "ssh_list")
        v = [...el.querySelectorAll(".re-row")]
          .map((row) => Object.fromEntries([...row.querySelectorAll("input")].map((i) => [i.dataset.k, i.value.trim()])))
          .filter((o) => Object.values(o).some(Boolean));
      else if (kind === "int") v = el.value.trim() === "" ? NaN : parseInt(el.value, 10);
      else if (kind === "float") v = el.value.trim() === "" ? NaN : parseFloat(el.value);
      else if (kind === "list_str") v = el.value.split(/\n/).map((x) => x.trim()).filter(Boolean);
      else v = el.value.trim();
      if ((kind === "int" || kind === "float") && !Number.isFinite(v)) throw new Error(`「${f.label || f.key}」要填数字`);
      if (JSON.stringify(v) !== JSON.stringify(f.value)) out[f.key] = v;
    }
  }
  return out;
}
