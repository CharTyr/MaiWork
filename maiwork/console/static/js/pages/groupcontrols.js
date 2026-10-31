// 群页唯一的发送与审批入口。缓存、草稿和异步结果都绑定当前群与身份。
import { $, admin, gadmin, state } from "../state.js";
import { esc, tip, toast, when } from "../util.js";
import { api } from "../api.js";
import { loading } from "./news.js";

const PUSH_KEYS = ["topics_enabled", "news_card_enabled", "news_card_count", "idea_mention_enabled", "daily_max", "quiet_hours"];
const FEATURE_KEYS = ["news", "idea", "task"];
const BOOLS = new Set(["topics_enabled", "news_card_enabled", "idea_mention_enabled", "required", "news", "idea", "task"]);
const NUMBERS = new Set(["news_card_count", "daily_max"]);
const role = () => (state.me && state.me.role) || "none";
const current = c => !!c && state.g === c.gid && role() === c.role && gadmin();

function frame(gid) {
  let c = state.groupControls;
  if (!c || c.gid !== gid || c.role !== role()) {
    c = state.groupControls = { gid, role: role(), push: null, approval: null, features: null, loading: false, error: "", errorField: "", edit: null, draft: null, saving: false };
  }
  return c;
}

async function load(c) {
  if (c.loading || !current(c)) return;
  c.loading = true;
  c.error = "";
  try {
    const [push, approval, features] = await Promise.all([
      api("GET", `/api/groups/${encodeURIComponent(c.gid)}/push`),
      api("GET", `/api/groups/${encodeURIComponent(c.gid)}/approval`),
      api("GET", `/api/groups/${encodeURIComponent(c.gid)}/features`),
    ]);
    if (!current(c) || state.groupControls !== c) return;
    c.push = push;
    c.approval = approval;
    c.features = features;
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
  if ((!c.push || !c.approval || !c.features) && !c.loading && !c.error) void load(c);
  return `<div class="groupctl-slot" data-g="${esc(gid)}">${controlsHTML(c)}</div>`;
}

export function repaintControls() {
  const c = state.groupControls;
  if (!current(c)) return;
  for (const el of document.querySelectorAll(".groupctl-slot")) {
    if (el.dataset.g === c.gid) el.innerHTML = controlsHTML(c);
  }
}

function switchRow(c, key, label, help, value, toggleAct = "") {
  const actAttr = toggleAct === "gctl-feature-toggle"
    ? ` data-act="gctl-feature-toggle" data-key="${key}" data-g="${esc(c.gid)}" data-on="${value ? "0" : "1"}"`
    : toggleAct === "gctl-push-toggle"
      ? ` data-act="gctl-push-toggle" data-key="${key}" data-g="${esc(c.gid)}" data-on="${value ? "0" : "1"}"`
      : "";
  return `<div class="set-row cp-row gctl-row"><div class="lbl-row"><label class="set-name" for="gctl-${key}">${label}</label>${tip(`<span id="gctl-${key}-help">${help}</span>`)}</div>
    <label class="switch"><input id="gctl-${key}" data-gctl="${key}"${actAttr} type="checkbox" ${value ? "checked" : ""}${c.saving ? " disabled" : ""} aria-label="${label}" aria-describedby="gctl-${key}-help" /><span></span></label></div>`;
}

function fieldErrorAttrs(c, key, help = "") {
  return `aria-describedby="${help ? help + " " : ""}gctl-${c.edit}-error"${c.errorField === key ? ' aria-invalid="true"' : ""}`;
}

function pushHTML(c) {
  const p = c.push || {}, cfg = p.config || {};
  const editing = c.edit === "push";
  const src = editing ? c.draft : cfg;
  const toggleAct = editing ? "" : "gctl-push-toggle";
  let html = `<div class="h-sub-row"><h2 class="h-sub">主动发言${tip("MaiWork 在本群主动发送的内容。设置仅对本群生效。")}</h2>${!editing ? `<button class="btn small" data-act="gctl-push-edit" data-g="${esc(c.gid)}">修改</button>` : ""}</div>`;
  html += `<div class="set-group">`;
  html += switchRow(c, "topics_enabled", "开启话题", "群聊冷场时发送一句开场白，后续对话由 MaiBot 接续。", src.topics_enabled, toggleAct);
  html += switchRow(c, "news_card_enabled", "资讯卡片", "选取优质资讯制作成图片发送到群内；已配置公开网址时附带本群链接。", src.news_card_enabled, toggleAct);
  html += switchRow(c, "idea_mention_enabled", "构想提及", "在群内简短提及构想；个人构想仅 @ 本人，不涉及画像内容。", src.idea_mention_enabled, toggleAct);
  html += `</div>`;
  if (editing) {
    const d = c.draft;
    html += `<div class="login">
      <label for="gctl-news_card_count">每张卡片条数</label><select id="gctl-news_card_count" data-gctl="news_card_count" ${fieldErrorAttrs(c, "news_card_count")}>${[1, 2, 3].map(n => `<option value="${n}" ${n === Number(d.news_card_count) ? "selected" : ""}>${n} 条</option>`).join("")}</select>
      <div class="lbl-row"><label for="gctl-daily_max">每日上限</label>${tip(`<span id="gctl-cap-help">以上三种内容合计；0 表示不限制（不建议）。</span>`)}</div><input id="gctl-daily_max" data-gctl="daily_max" type="number" min="0" max="24" step="1" value="${esc(d.daily_max)}" ${fieldErrorAttrs(c, "daily_max", "gctl-cap-help")} />
      <div class="lbl-row"><label for="gctl-quiet_hours">免打扰时段</label>${tip(`<span id="gctl-quiet-help">该时段内不主动发送，格式如 23:00-08:00。</span>`)}</div><input id="gctl-quiet_hours" data-gctl="quiet_hours" value="${esc(d.quiet_hours)}" placeholder="23:00-08:00" ${fieldErrorAttrs(c, "quiet_hours", "gctl-quiet-help")} />
      ${errorHTML(c)}
    </div>`;
    html += saveActions(c, "push");
  } else {
    html += `<div class="fine">每张卡片 ${Number(cfg.news_card_count) || 3} 条 · 每日上限 ${Number(cfg.daily_max) === 0 ? "不限" : `${esc(cfg.daily_max)} 条`} · 免打扰 ${esc(cfg.quiet_hours || "23:00-08:00")}<br />今日已发送 ${Number(p.sent_today) || 0} 条（占用额度 ${Number(p.quota_used) || 0}）${tip("未确认送达的消息也计入额度，以免重复发送。")}</div>`;
    if ((p.recent || []).length) html += `<details><summary>最近发送</summary>${p.recent.map(r => `<p class="fine">${esc(when(r.ts))} · ${esc(r.state || r.status)}${r.text ? ` · ${esc(r.text)}` : ""}</p>`).join("")}</details>`;
  }
  return html;
}

function featuresHTML(c) {
  const f = c.features || {};
  const help = "三个功能各自独立。关闭后本群不再新增对应工作，历史和文件保留；任务关闭会一并暂停目标检查和提醒。";
  let html = `<div class="h-sub-row"><h2 class="h-sub">功能启用${tip(help)}</h2></div><div class="set-group">`;
  html += switchRow(c, "news", "资讯", "定时采集并整理本群关心的资讯；含手动搜集、资讯卡片与来源维护。", f.news, "gctl-feature-toggle");
  html += switchRow(c, "idea", "构想", "按群画像定时或由话头触发生成构想；含手动生成与构想提及。", f.idea, "gctl-feature-toggle");
  html += switchRow(c, "task", "任务", "接收任务、审批后开工、目标追踪与提醒；关闭会暂停目标检查，正在执行的任务继续完成。", f.task, "gctl-feature-toggle");
  html += `</div>`;
  if (f.task === false) {
    html += `<p class="fine warn-inline" role="note">任务已关闭：本群的目标检查和提醒已一并暂停；已在执行的任务会继续完成，排队任务重新开启后需手动恢复。</p>`;
  }
  return html;
}

function approvalHTML(c) {  const a = c.approval || {};
  let html = `<div class="h-sub-row"><h2 class="h-sub">任务审批</h2>${admin() && c.edit !== "approval" ? `<button class="btn small" data-act="gctl-approval-edit" data-g="${esc(c.gid)}">修改</button>` : ""}</div>`;
  if (c.edit === "approval" && admin()) {
    const d = c.draft;
    html += `<div class="login">
      <div class="lbl-row"><label for="gctl-approvers">审批人</label>${tip(`<span id="gctl-accounts-help">每行一个账号，例如 qq:12345。仅对本群生效。</span>`)}</div><textarea id="gctl-approvers" data-gctl="approvers" rows="3" aria-describedby="gctl-accounts-help">${esc(d.approvers)}</textarea>
      <label for="gctl-exempt_users">免审批成员</label><textarea id="gctl-exempt_users" data-gctl="exempt_users" rows="3" aria-describedby="gctl-accounts-help">${esc(d.exempt_users)}</textarea>
    </div>`;
    html += switchRow(c, "required", "任务需审批", "关闭后本群所有任务无需审批。建议保持开启，仅为可信成员单独设置免审批。", d.required);
    html += errorHTML(c) + saveActions(c, "approval");
  } else {
    const list = xs => xs && xs.length ? xs.map(esc).join("、") : "未设置";
    html += `<p class="h-meta">${a.required && !a.exempt_group ? "任务需审批" : "任务无需审批"}</p><p class="fine">审批人：${list(a.approvers)}<br />免审批：${list(a.exempt_users)}</p>`;
    if (!admin()) html += `<p class="fine">名单由管理员设置；可在「任务」页面中审批</p>`;
  }
  return html;
}

function errorHTML(c) {
  if (!c.edit && !c.error) return "";
  return `<p id="gctl-${c.edit || "form"}-error" class="err gctl-error" role="alert" ${c.error ? "" : "hidden"}>${esc(c.error)}</p>`;
}

function saveActions(c, kind) {
  return `<div class="actions"><button class="btn primary" data-act="gctl-${kind}-save" data-g="${esc(c.gid)}" ${c.saving ? "disabled" : ""}>${c.saving ? "正在保存…" : "保存"}</button><button class="btn" data-act="gctl-cancel" data-g="${esc(c.gid)}" ${c.saving ? "disabled" : ""}>取消</button></div>`;
}

function controlsHTML(c) {
  if (!c.push || !c.approval || !c.features) return c.error ? `<div class="warn-box" role="alert">${esc(c.error)} <button class="btn small" data-act="gctl-reload" data-g="${esc(c.gid)}">重试</button></div>` : loading();
  return featuresHTML(c) + pushHTML(c) + approvalHTML(c) + (!c.edit ? `<button class="btn small" data-act="gctl-reload" data-g="${esc(c.gid)}">刷新</button>` : "") + (!c.edit ? errorHTML(c) : "");
}

function readDraft(c) {
  const keys = c.edit === "push" ? PUSH_KEYS : c.edit === "features" ? FEATURE_KEYS : ["approvers", "exempt_users", "required"];
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
  if (!Number.isInteger(d.daily_max) || d.daily_max < 0 || d.daily_max > 24) return { field: "daily_max", message: "每日上限须为 0–24" };
  if (!Number.isInteger(d.news_card_count) || d.news_card_count < 1 || d.news_card_count > 3) return { field: "news_card_count", message: "每张卡片须为 1–3 条" };
  if (!/^(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d$/.test(d.quiet_hours)) return { field: "quiet_hours", message: "时段格式应为 23:00-08:00" };
  return null;
}

export async function actControls(action, el) {
  if (!String(action || "").startsWith("gctl-")) return false;
  const c = state.groupControls;
  if (!current(c) || el.dataset.g !== c.gid || c.saving) return true;
  if (action === "gctl-reload") { await load(c); return true; }
  if (action === "gctl-cancel") { c.edit = null; c.draft = null; c.error = ""; repaintControls(); return true; }
  if (action === "gctl-feature-toggle" || action === "gctl-push-toggle") {
    const isFeat = action === "gctl-feature-toggle";
    const key = el.dataset.key;
    const allowed = isFeat ? FEATURE_KEYS : ["topics_enabled", "news_card_enabled", "idea_mention_enabled"];
    if (!allowed.includes(key)) return true;
    if (!isFeat && c.edit === "push") { readDraft(c); return true; }
    const prevObj = isFeat ? c.features : c.push.config;
    const prevVal = !!prevObj[key];
    const nextVal = typeof el.checked === "boolean" ? el.checked : el.dataset.on === "1";
    prevObj[key] = nextVal;
    c.saving = true;
    c.error = "";
    repaintControls();
    try {
      const body = isFeat
        ? Object.fromEntries(FEATURE_KEYS.map(k => [k, !!c.features[k]]))
        : Object.fromEntries(PUSH_KEYS.map(k => [k, c.push.config[k]]));
      const result = await api("PUT", `/api/groups/${encodeURIComponent(c.gid)}/${isFeat ? "features" : "push"}`, body);
      if (!current(c) || state.groupControls !== c) return true;
      if (isFeat) c.features = result;
      else c.push = result;
      toast("已保存（仅本群）");
    } catch (e) {
      prevObj[key] = prevVal;
      if (typeof el.checked === "boolean") el.checked = prevVal;
      if (current(c)) {
        c.error = e.message;
        toast(e.message, true);
      }
    } finally {
      c.saving = false;
      repaintControls();
    }
    return true;
  }
  const kind = action.includes("approval") ? "approval" : action.includes("features") ? "features" : "push";
  if (kind === "approval" && !admin()) return true;
  if (action.endsWith("-edit")) {
    c.edit = kind;
    c.error = "";
    c.errorField = "";
    c.draft = kind === "push" ? Object.fromEntries(PUSH_KEYS.map(k => [k, c.push.config[k]])) : kind === "features" ? Object.fromEntries(FEATURE_KEYS.map(k => [k, !!c.features[k]])) : {
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
    const body = kind === "push" ? d : kind === "features" ? d : { approvers: accountLines(d.approvers), exempt_users: accountLines(d.exempt_users), required: d.required, exempt_group: false };
    c.saving = true;
    c.error = "";
    repaintControls();
    const result = await api("PUT", `/api/groups/${encodeURIComponent(c.gid)}/${kind}`, body);
    if (!current(c) || state.groupControls !== c) return true;
    c[kind] = result;
    c.edit = null;
    c.draft = null;
    toast("已保存（仅本群）");
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
