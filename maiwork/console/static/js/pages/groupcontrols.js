// 群页唯一的发送与审批入口。缓存、草稿和异步结果都绑定当前群与身份。
import { $, admin, gadmin, state } from "../state.js";
import { esc, toast, when } from "../util.js";
import { api } from "../api.js";
import { loading } from "./news.js";

const PUSH_KEYS = ["topics_enabled", "news_card_enabled", "news_card_count", "idea_mention_enabled", "daily_max", "quiet_hours"];
const BOOLS = new Set(["topics_enabled", "news_card_enabled", "idea_mention_enabled", "required"]);
const NUMBERS = new Set(["news_card_count", "daily_max"]);
const role = () => (state.me && state.me.role) || "none";
const current = c => !!c && state.g === c.gid && role() === c.role && gadmin();

function frame(gid) {
  let c = state.groupControls;
  if (!c || c.gid !== gid || c.role !== role()) {
    c = state.groupControls = { gid, role: role(), push: null, approval: null, loading: false, error: "", errorField: "", edit: null, draft: null, saving: false };
  }
  return c;
}

async function load(c) {
  if (c.loading || !current(c)) return;
  c.loading = true;
  c.error = "";
  try {
    const [push, approval] = await Promise.all([
      api("GET", `/api/groups/${encodeURIComponent(c.gid)}/push`),
      api("GET", `/api/groups/${encodeURIComponent(c.gid)}/approval`),
    ]);
    if (!current(c) || state.groupControls !== c) return;
    c.push = push;
    c.approval = approval;
  } catch (e) {
    if (current(c)) c.error = e.message;
  } finally {
    c.loading = false;
    repaintControls();
  }
}

export function controlsSection(gid, snapshot = null) {
  if (!gadmin() || state.g !== gid) return "";
  const c = frame(gid);
  const fresh = snapshot && snapshot.card_push && snapshot.card_push.group_push;
  if (fresh && !c.edit && !c.saving) c.push = fresh;
  if ((!c.push || !c.approval) && !c.loading && !c.error) void load(c);
  return `<div class="groupctl-slot" data-g="${esc(gid)}">${controlsHTML(c)}</div>`;
}

export function repaintControls() {
  const c = state.groupControls;
  if (!current(c)) return;
  for (const el of document.querySelectorAll(".groupctl-slot")) {
    if (el.dataset.g === c.gid) el.innerHTML = controlsHTML(c);
  }
}

function switchRow(c, key, label, help, value) {
  return `<div class="set-row cp-row gctl-row"><div><label class="set-name" for="gctl-${key}">${label}</label><div class="set-text" id="gctl-${key}-help">${help}</div></div>
    <label class="switch"><input id="gctl-${key}" data-gctl="${key}" type="checkbox" ${value ? "checked" : ""} aria-label="${label}" aria-describedby="gctl-${key}-help" /><span></span></label></div>`;
}

function fieldErrorAttrs(c, key, help = "") {
  return `aria-describedby="${help ? help + " " : ""}gctl-${c.edit}-error"${c.errorField === key ? ' aria-invalid="true"' : ""}`;
}

function pushHTML(c) {
  const p = c.push || {}, cfg = p.config || {};
  let html = `<div class="h-sub-row"><h2 class="h-sub">往群里发</h2>${c.edit !== "push" ? `<button class="btn small" data-act="gctl-push-edit" data-g="${esc(c.gid)}">改设置</button>` : ""}</div>`;
  if (c.edit === "push") {
    const d = c.draft;
    html += switchRow(c, "topics_enabled", "冷场开话题", "只开一句，后续对话归 MaiBot；判断门槛不变。", d.topics_enabled);
    html += switchRow(c, "news_card_enabled", "资讯卡片", "每批挑最值得看的做成图片；已配公开地址时附本群链接。", d.news_card_enabled);
    html += switchRow(c, "idea_mention_enabled", "构想提一嘴", "关心式问一句；个人向只 @ 本人，不说画像。", d.idea_mention_enabled);
    html += `<div class="login">
      <label for="gctl-news_card_count">每张资讯卡放几条</label><select id="gctl-news_card_count" data-gctl="news_card_count" ${fieldErrorAttrs(c, "news_card_count")}>${[1, 2, 3].map(n => `<option value="${n}" ${n === Number(d.news_card_count) ? "selected" : ""}>${n} 条</option>`).join("")}</select>
      <label for="gctl-daily_max">每天主动发送总上限</label><input id="gctl-daily_max" data-gctl="daily_max" type="number" min="0" max="24" step="1" value="${esc(d.daily_max)}" ${fieldErrorAttrs(c, "daily_max", "gctl-cap-help")} />
      <p class="fine" id="gctl-cap-help">开话题、资讯卡片和提一嘴共用这一个上限。0 表示不限，不建议。</p>
      <label for="gctl-quiet_hours">睡觉时段</label><input id="gctl-quiet_hours" data-gctl="quiet_hours" value="${esc(d.quiet_hours)}" placeholder="23:00-08:00" ${fieldErrorAttrs(c, "quiet_hours", "gctl-quiet-help")} />
      <p class="fine" id="gctl-quiet-help">北京时间，写成 HH:MM-HH:MM；这段时间不发。</p>
      ${errorHTML(c)}
    </div>`;
    html += saveActions(c, "push");
  } else {
    const line = (label, on) => `<div class="set-row gctl-row"><div><div class="set-name">${label}</div><div class="set-text">${on ? "已开" : "已关"}</div></div></div>`;
    html += line("冷场开话题", cfg.topics_enabled) + line("资讯卡片", cfg.news_card_enabled) + line("构想提一嘴", cfg.idea_mention_enabled);
    html += `<p class="fine">每天主动发送总上限：${Number(cfg.daily_max) === 0 ? "不限" : `${esc(cfg.daily_max)} 条`} · 睡觉时段 ${esc(cfg.quiet_hours || "23:00-08:00")}<br />今天实际发出 ${Number(p.sent_today) || 0} 条；总额度用了 ${Number(p.quota_used) || 0}。结果不明的发送暂占额度，避免重复发。</p>`;
    if ((p.recent || []).length) html += `<details><summary>最近发送</summary>${p.recent.map(r => `<p class="fine">${esc(when(r.ts))} · ${esc(r.state || r.status)}${r.text ? ` · ${esc(r.text)}` : ""}</p>`).join("")}</details>`;
  }
  return html;
}

function approvalHTML(c) {
  const a = c.approval || {};
  let html = `<div class="h-sub-row"><h2 class="h-sub">谁能批本群的活</h2>${admin() && c.edit !== "approval" ? `<button class="btn small" data-act="gctl-approval-edit" data-g="${esc(c.gid)}">改名单</button>` : ""}</div>`;
  if (c.edit === "approval" && admin()) {
    const d = c.draft;
    html += `<div class="login">
      <label for="gctl-approvers">群里能批准的人</label><textarea id="gctl-approvers" data-gctl="approvers" rows="3" aria-describedby="gctl-accounts-help">${esc(d.approvers)}</textarea>
      <label for="gctl-exempt_users">派活免批的人</label><textarea id="gctl-exempt_users" data-gctl="exempt_users" rows="3" aria-describedby="gctl-accounts-help">${esc(d.exempt_users)}</textarea>
      <p class="fine" id="gctl-accounts-help">一行一个平台账号，如 qq:账号；只影响这个群，不会改别的群。</p>
    </div>`;
    html += switchRow(c, "required", "本群派活要批准", "关掉后本群整群免批；默认开着，只给信任的人单独免批。", d.required);
    html += errorHTML(c) + saveActions(c, "approval");
  } else {
    const list = xs => xs && xs.length ? xs.map(esc).join("、") : "还没设置";
    html += `<p class="h-meta">${a.required && !a.exempt_group ? "本群派活默认要批准" : "本群整群免批"}</p><p class="fine">群里能批准：${list(a.approvers)}<br />免批的人：${list(a.exempt_users)}</p>`;
    if (!admin()) html += `<p class="fine">批准与免批名单由总管理员设置；你可以在「在做的事」里批准本群的活。</p>`;
  }
  return html;
}

function errorHTML(c) {
  if (!c.edit && !c.error) return "";
  return `<p id="gctl-${c.edit || "form"}-error" class="err gctl-error" role="alert" ${c.error ? "" : "hidden"}>${esc(c.error)}</p>`;
}

function saveActions(c, kind) {
  return `<div class="actions"><button class="btn primary" data-act="gctl-${kind}-save" data-g="${esc(c.gid)}" ${c.saving ? "disabled" : ""}>${c.saving ? "保存中…" : "保存"}</button><button class="btn" data-act="gctl-cancel" data-g="${esc(c.gid)}" ${c.saving ? "disabled" : ""}>不改了</button></div>`;
}

function controlsHTML(c) {
  if (!c.push || !c.approval) return c.error ? `<div class="warn-box" role="alert">${esc(c.error)} <button class="btn small" data-act="gctl-reload" data-g="${esc(c.gid)}">重试</button></div>` : loading();
  return pushHTML(c) + approvalHTML(c) + (!c.edit ? `<button class="btn small" data-act="gctl-reload" data-g="${esc(c.gid)}">刷新发送记录与名单</button>` : "") + (!c.edit ? errorHTML(c) : "");
}

function readDraft(c) {
  const keys = c.edit === "push" ? PUSH_KEYS : ["approvers", "exempt_users", "required"];
  const d = { ...c.draft };
  for (const key of keys) {
    const el = $("gctl-" + key);
    if (!el) continue;
    if (BOOLS.has(key)) d[key] = !!el.checked;
    else if (NUMBERS.has(key)) d[key] = el.value.trim() === "" ? NaN : Number(el.value);
    else if (["approvers", "exempt_users"].includes(key)) d[key] = el.value;
    else d[key] = el.value.trim();
  }
  c.draft = d;
  return d;
}

const accountLines = text => text.split(/\n/).map(x => x.trim()).filter(Boolean);

function checkPush(d) {
  if (!Number.isInteger(d.daily_max) || d.daily_max < 0 || d.daily_max > 24) return { field: "daily_max", message: "总上限要填 0–24 的整数" };
  if (!Number.isInteger(d.news_card_count) || d.news_card_count < 1 || d.news_card_count > 3) return { field: "news_card_count", message: "每张资讯卡放 1–3 条" };
  if (!/^(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d$/.test(d.quiet_hours)) return { field: "quiet_hours", message: "睡觉时段写成 HH:MM-HH:MM，如 23:00-08:00" };
  return null;
}

export async function actControls(action, el) {
  if (!String(action || "").startsWith("gctl-")) return false;
  const c = state.groupControls;
  if (!current(c) || el.dataset.g !== c.gid || c.saving) return true;
  if (action === "gctl-reload") { await load(c); return true; }
  if (action === "gctl-cancel") { c.edit = null; c.draft = null; c.error = ""; repaintControls(); return true; }
  const kind = action.includes("approval") ? "approval" : "push";
  if (kind === "approval" && !admin()) return true;
  if (action.endsWith("-edit")) {
    c.edit = kind;
    c.error = "";
    c.errorField = "";
    c.draft = kind === "push" ? Object.fromEntries(PUSH_KEYS.map(k => [k, c.push.config[k]])) : {
      approvers: c.approval.approvers.join("\n"), exempt_users: c.approval.exempt_users.join("\n"), required: !!c.approval.required && !c.approval.exempt_group,
    };
    repaintControls();
    return true;
  }
  if (!action.endsWith("-save") || c.edit !== kind) return true;
  try {
    c.errorField = "";
    const d = readDraft(c);
    const invalid = kind === "push" ? checkPush(d) : null;
    if (invalid) { c.error = invalid.message; c.errorField = invalid.field; return true; }
    const body = kind === "push" ? d : { approvers: accountLines(d.approvers), exempt_users: accountLines(d.exempt_users), required: d.required, exempt_group: false };
    c.saving = true;
    c.error = "";
    repaintControls();
    const result = await api("PUT", `/api/groups/${encodeURIComponent(c.gid)}/${kind}`, body);
    if (!current(c) || state.groupControls !== c) return true;
    c[kind] = result;
    c.edit = null;
    c.draft = null;
    toast("保存好了，只影响这个群");
  } catch (e) {
    if (current(c)) c.error = e.message;
  } finally {
    c.saving = false;
    repaintControls();
  }
  return true;
}

// 本地草稿跟着输入走；轮询重画不会把刚输入的字吞掉。
for (const event of ["input", "change"]) document.addEventListener(event, e => {
  const c = state.groupControls;
  const key = e.target && e.target.dataset && e.target.dataset.gctl;
  if (!current(c) || !c.edit || !key || c.saving) return;
  readDraft(c);
});
