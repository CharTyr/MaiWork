// MaiWork 网页 · 群页「这个群」区（docs/17 §八.4）：本群规矩 + 本群做法（skill）。
// 只给总管理员和本群群管理员；群友只看得到群画像（在 group.js）。
// 本群规矩：管理员 / 群管理员写，MaiWork 自动流程从来不改；改一次留一版，可回退。
// 本群做法：MaiWork 从验收和反馈里总结，每个岗位一份 + 通用执行按「一类活」积累；可改、锁定、回退、归档。
import { state, $ } from "../state.js";
import { api } from "../api.js";
import { esc, toast, SVG } from "../util.js";

export const RULES_MAX = 3000;
export const SKILL_DESC_MAX = 120;
export const SKILL_NAME_MAX = 40;
export const skillBodyMax = (kind) => (kind === "task" ? 4000 : 2500);
const KIND_ORDER = ["news", "idea", "goal", "task"];
const KIND_TITLES = { news: "资讯", idea: "构想", goal: "目标", task: "通用执行" };
const SOURCES = { auto: "MaiWork 改的", admin: "你改的", rollback: "回退的", migrate: "旧设置迁来" };
const WHO = { admin: "网页改的", migrate: "旧设置迁来", admin_chat: "聊天里记的" };
const shortDay = (ts) => (ts ? new Date(ts * 1000).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" }) : "");
const longTime = (ts) => new Date((ts || 0) * 1000).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
const enc = encodeURIComponent;
const rulesUrl = (gid, tail = "") => `/api/groups/${enc(gid)}/rules${tail}`;
const skillsUrl = (gid, id, tail = "") => `/api/groups/${enc(gid)}/skills${id != null ? `/${enc(id)}` : ""}${tail}`;

// 当前群的数据和界面状态；换群就整份换掉（没保存的表单、展开的历史不带过去）
function ctx(gid) {
  if (!state.gctx || state.gctx.gid !== gid) state.gctx = { gid, rules: null, skills: null, error: "", edit: null, open: null, hist: null, drafts: {} };
  return state.gctx;
}

export function repaintCtx() {
  document.querySelectorAll(".gctx-slot").forEach((el) => (el.innerHTML = ctxInner()));
}

export async function loadCtx(gid) {
  const c = ctx(gid);
  c.loading = true;
  c.error = "";
  try {
    const [rules, sk] = await Promise.all([api("GET", rulesUrl(gid)), api("GET", skillsUrl(gid))]);
    if (state.gctx !== c) return; // 期间换了群
    c.rules = rules || { body: "" };
    c.skills = (sk && Array.isArray(sk.skills)) ? sk.skills : [];
  } catch (err) {
    if (state.gctx !== c) return;
    c.error = err.message;
  }
  c.loading = false;
  repaintCtx();
}

// 群页调用：第一次进这个群就去拉，先放个占位，拉回来原地填上
export function ctxSection(gid) {
  const c = ctx(gid);
  if (c.rules == null && !c.loading && !c.error) loadCtx(gid);
  return `<div class="gctx-slot" data-g="${esc(gid)}">${ctxInner()}</div>`;
}

function ctxInner() {
  const c = state.gctx;
  if (!c) return "";
  if (c.error) return `<p class="err" role="alert">${esc(c.error)}</p><button class="btn small" data-act="gctx-reload">刷新</button>`;
  if (c.rules == null) return `<p class="fine">读取中…</p>`;
  return rulesBlock(c) + skillsBlock(c);
}

// 编辑中的文字：整页重画（比如定时刷新）时不丢
const draft = (c, id, fallback) => (id in c.drafts ? c.drafts[id] : fallback);
document.addEventListener("input", (e) => {
  const t = e.target;
  if (state.gctx && t && t.id && String(t.id).startsWith("gctx-")) state.gctx.drafts[t.id] = t.value;
});

/* ───── 本群规矩 ───── */

function rulesBlock(c) {
  const r = c.rules || {};
  const body = String(r.body || "");
  const meta = r.updated ? `${shortDay(r.updated)} ${WHO[r.updated_by] || ""}`.trim() : "";
  let inner;
  if (c.edit === "rules") {
    inner = `<label class="fine" for="gctx-rules">本群规矩</label>
      <textarea id="gctx-rules" rows="8" maxlength="${RULES_MAX}" aria-describedby="gctx-rules-help gctx-rules-err" placeholder="比如：少点手机评测，多找开源硬件">${esc(draft(c, "gctx-rules", body))}</textarea>
      <p class="fine" id="gctx-rules-help">MaiWork 只照做不改；最多 ${RULES_MAX} 字</p>
      <p class="err" id="gctx-rules-err" role="alert" hidden></p>
      <div class="actions"><button class="btn small primary" data-act="gctx-rules-save" data-g="${esc(c.gid)}">保存</button><button class="btn small" data-act="gctx-cancel">取消</button></div>`;
  } else {
    const histOpen = c.hist && c.hist.id === "rules";
    inner = `<div class="pf skill">
        <div>${body ? `<div class="skill-body">${esc(body)}</div>` : `<p class="fine">还没有。写下本群必须照做的事</p>`}${meta ? `<div class="pf-meta">${esc(meta)}</div>` : ""}</div>
        <div class="pf-acts"><button type="button" class="btn small" data-act="gctx-hist" data-id="rules" data-g="${esc(c.gid)}" aria-expanded="${histOpen ? "true" : "false"}">历史</button><button type="button" class="icon-btn" data-act="gctx-rules-edit" aria-label="${body ? "修改" : "写规矩"}" title="${body ? "修改" : "写规矩"}">${SVG.pen}</button></div>
      </div>${histOpen ? historyBlock(c, "rules") : ""}`;
  }
  return `<div class="skills gctx-rules">
    <div class="pf-name">本群规矩</div>
    <p class="fine">必须照做；和做法冲突时听这里的</p>
    ${inner}
  </div>`;
}

/* ───── 本群做法（skill） ───── */

const titleOf = (kind) => KIND_TITLES[kind] || (((state.agents && state.agents.profiles) || []).find((p) => p.kind === kind) || {}).title || kind;

function skillsBlock(c) {
  const all = c.skills || [];
  const kinds = [...KIND_ORDER, ...[...new Set(all.map((s) => s.kind))].filter((k) => !KIND_ORDER.includes(k) && k !== "main")];
  return `<div class="gctx-skills">
    <div class="pf-name">本群做法</div>
    <p class="fine">MaiWork 自己总结的经验，可改可锁</p>
    ${kinds.map((k) => (k === "task" ? taskBlock(c, all.filter((s) => s.kind === "task")) : agentBlock(c, k, all.filter((s) => s.kind === k)))).join("")}
  </div>`;
}

function skillForm(c, kind, s) {
  const id = s ? String(s.id) : "new";
  const task = kind === "task";
  const k = esc(kind);
  const max = skillBodyMax(kind);
  const ph = task ? "做法：\n1. 先……\n   - 注意：……（因为……）\n2. 再……" : "做法：\n1. 先……\n   - 注意：……（因为……）\n偏好：……";
  return `<div class="skill-form" data-kind="${k}" data-id="${esc(id)}">
    ${task ? `<label class="fine" for="gctx-name-${k}">名称（一类活）</label>
    <input id="gctx-name-${k}" maxlength="${SKILL_NAME_MAX}" aria-describedby="gctx-err-${k}" value="${esc(draft(c, `gctx-name-${kind}`, (s && s.name) || ""))}" placeholder="如：整理群活动报名表"${s ? " disabled" : ""} />
    <label class="fine" for="gctx-desc-${k}">何时用</label>
    <input id="gctx-desc-${k}" maxlength="${SKILL_DESC_MAX}" aria-describedby="gctx-err-${k}" value="${esc(draft(c, `gctx-desc-${kind}`, (s && s.description) || ""))}" placeholder="如：整理报名表的时候" />` : ""}
    <label class="fine" for="gctx-body-${k}">做法</label>
    <textarea id="gctx-body-${k}" rows="10" maxlength="${max}" aria-describedby="gctx-help-${k} gctx-err-${k}" placeholder="${esc(ph)}">${esc(draft(c, `gctx-body-${kind}`, (s && s.body) || ""))}</textarea>
    <p class="fine" id="gctx-help-${k}">按步骤写，附一句为什么；最多 ${max} 字</p>
    <p class="err" id="gctx-err-${k}" role="alert" hidden></p>
    <div class="actions"><button class="btn small primary" data-act="gctx-skill-save" data-kind="${k}" data-id="${esc(id)}" data-g="${esc(c.gid)}">保存</button><button class="btn small" data-act="gctx-cancel">取消</button></div>
  </div>`;
}

function skillMeta(kind, s) {
  return [
    s.locked ? "已锁定" : "",
    s.status === "archived" ? "已归档" : "",
    kind === "task" ? `用过 ${Number(s.uses) || 0} 次` : "",
    s.updated ? `${shortDay(s.updated)} 改过` : "",
  ].filter(Boolean).join(" · ");
}

function skillActions(c, kind, s) {
  const k = esc(kind), id = esc(String(s.id)), g = esc(c.gid);
  const btn = (act, icon, label, extra = "") => `<button type="button" class="icon-btn" data-act="${act}" data-kind="${k}" data-id="${id}" data-g="${g}" aria-label="${esc(label)}" title="${esc(label)}"${extra}>${icon}</button>`;
  const histOpen = c.hist && String(c.hist.id) === String(s.id);
  const hist = `<button type="button" class="btn small" data-act="gctx-hist" data-kind="${k}" data-id="${id}" data-g="${g}" aria-expanded="${histOpen ? "true" : "false"}">历史</button>`;
  if (s.status === "archived") return `${hist}${btn("gctx-skill-status", SVG.archive, "恢复", ` data-to="active"`)}${btn("gctx-skill-del", SVG.trash, "删除")}`;
  return `${hist}${btn("gctx-skill-edit", SVG.pen, "修改")}${btn("gctx-skill-lock", SVG.pin, s.locked ? "解锁" : "锁定", ` aria-pressed="${s.locked ? "true" : "false"}"`)}${kind === "task" ? `${btn("gctx-skill-status", SVG.archive, "归档", ` data-to="archived"`)}${btn("gctx-skill-del", SVG.trash, "删除")}` : ""}`;
}

function historyBlock(c, kind, s) {
  const h = c.hist;
  if (h.error) return `<p class="err" role="alert">${esc(h.error)}</p>`;
  if (!h.versions) return `<p class="fine">读取中…</p>`;
  if (!h.versions.length) return `<p class="fine">没有旧版本</p>`;
  const rules = kind === "rules";
  return `<div class="skill-hist">${h.versions.map((v) => `<details><summary>${esc(longTime(v.ts))} · ${esc(rules ? WHO[v.updated_by] || v.updated_by || "" : SOURCES[v.source] || v.source || "")}${v.note ? ` · ${esc(v.note)}` : ""}</summary>
    <div class="skill-body">${esc(v.body || "") || `<span class="fine">（空）</span>`}</div>
    <div class="actions"><button class="btn small" data-act="gctx-restore" data-kind="${esc(kind)}" data-id="${esc(rules ? "rules" : String(s.id))}" data-vid="${esc(String(v.id))}" data-g="${esc(c.gid)}">用这一版</button></div>
  </details>`).join("")}</div>`;
}

function agentBlock(c, kind, list) {
  const s = list.find((x) => x.status !== "archived");
  const ed = c.edit;
  const editing = ed && ed.kind === kind && (ed.id === "new" || (s && String(ed.id) === String(s.id)));
  let inner;
  if (editing) inner = skillForm(c, kind, s || null);
  else if (s) inner = `<div class="pf skill">
      <div><div class="skill-body">${esc(s.body || "") || `<span class="fine">（空）</span>`}</div><div class="pf-meta">${esc(skillMeta(kind, s))}</div></div>
      <div class="pf-acts">${skillActions(c, kind, s)}</div>
    </div>${c.hist && String(c.hist.id) === String(s.id) ? historyBlock(c, kind, s) : ""}`;
  else inner = `<p class="fine">还没有，有反馈后次日开始写</p><div class="actions"><button class="btn small" data-act="gctx-skill-add" data-kind="${esc(kind)}">自己写</button></div>`;
  return `<div class="skills"><div class="set-name">${esc(titleOf(kind))}</div>${inner}</div>`;
}

function taskRow(c, s) {
  const ed = c.edit;
  if (ed && ed.kind === "task" && String(ed.id) === String(s.id)) return skillForm(c, "task", s);
  const open = String(c.open) === String(s.id);
  return `<div class="pf skill${s.status === "archived" ? " archived" : ""}">
    <div>
      <button type="button" class="skill-name" data-act="gctx-skill-open" data-id="${esc(String(s.id))}" aria-expanded="${open ? "true" : "false"}">${esc(s.name || "")}</button>
      ${s.description ? `<div class="pf-meta">${esc(s.description)}</div>` : ""}
      <div class="pf-meta">${esc(skillMeta("task", s))}</div>
      ${open ? `<div class="skill-body">${esc(s.body || "")}</div>` : ""}
    </div>
    <div class="pf-acts">${skillActions(c, "task", s)}</div>
  </div>${c.hist && String(c.hist.id) === String(s.id) ? historyBlock(c, "task", s) : ""}`;
}

function taskBlock(c, all) {
  const active = all.filter((s) => s.status !== "archived");
  const archived = all.filter((s) => s.status === "archived");
  const adding = c.edit && c.edit.kind === "task" && c.edit.id === "new";
  return `<div class="skills">
    <div class="set-name">做事经验 · ${active.length}${adding ? "" : `<button type="button" class="pf-add" data-act="gctx-skill-add" data-kind="task" aria-label="新建">${SVG.plus}</button>`}</div>
    <p class="fine">做完难活会记下方法；30 天不用自动归档</p>
    ${adding ? skillForm(c, "task", null) : ""}
    ${active.map((s) => taskRow(c, s)).join("") || (adding ? "" : `<p class="fine">还没有</p>`)}
    ${archived.length ? `<details><summary>已归档 · ${archived.length}</summary><p class="fine">不再使用，可恢复</p>${archived.map((s) => taskRow(c, s)).join("")}</details>` : ""}
  </div>`;
}

/* ───── 动作 ───── */

function clearFormError(key) {
  const ids = key === "rules" ? ["gctx-rules"] : [`gctx-name-${key}`, `gctx-desc-${key}`, `gctx-body-${key}`];
  for (const id of ids) $(id)?.removeAttribute?.("aria-invalid");
  const box = $(`gctx-err-${key}`) || $(`gctx-${key}-err`);
  if (box) { box.textContent = ""; box.hidden = true; }
}

function formError(key, msg, inputId = null) {
  const box = $(`gctx-err-${key}`) || $(`gctx-${key}-err`);
  const input = inputId && $(inputId);
  if (input && input.setAttribute) input.setAttribute("aria-invalid", "true");
  if (box) { box.textContent = msg; box.hidden = false; } else toast(msg, true);
}

const currentContext = c => state.gctx === c && state.g === c.gid;

// 写操作：群换了就不写（防把 A 群的操作落到 B 群）
function sameGroup(el) {
  const g = el.dataset.g;
  const c = state.gctx;
  if (!c || (g && g !== c.gid) || (state.g && c.gid !== state.g)) { toast("群已切换，请重试", true); return null; }
  return c;
}

function replaceKind(c, kind, list) {
  if (!Array.isArray(list)) return;
  c.skills = (c.skills || []).filter((s) => s.kind !== kind).concat(list.filter((s) => s.kind === kind));
}

const clearForm = (c) => { c.edit = null; c.hist = null; c.drafts = {}; };

async function write(el, c, method, url, body, okText, apply, errKey) {
  el.disabled = true;
  try {
    const out = await api(method, url, body);
    if (!currentContext(c)) return true;
    apply(out || {});
    clearForm(c);
    repaintCtx();
    if (okText) toast(okText);
  } catch (err) {
    if (!currentContext(c)) return true;
    el.disabled = false;
    if (errKey) formError(errKey, err.message); else toast(err.message, true);
  }
  return true;
}

const focusSoon = (id) => setTimeout(() => { const t = $(id); if (t && t.focus) t.focus(); }, 0);

// 处理 gctx-* 动作；不是本模块的返回 false
export async function actCtx(action, el) {
  if (!action || !action.startsWith("gctx-")) return false;
  const d = el.dataset;
  const c = state.gctx;
  if (!c) return true;
  switch (action) {
    case "gctx-reload": c.error = ""; c.rules = null; repaintCtx(); await loadCtx(c.gid); return true;
    case "gctx-cancel": clearForm(c); repaintCtx(); return true;
    case "gctx-rules-edit": clearForm(c); c.edit = "rules"; repaintCtx(); focusSoon("gctx-rules"); return true;
    case "gctx-skill-add": clearForm(c); c.edit = { kind: d.kind, id: "new" }; repaintCtx(); focusSoon(d.kind === "task" ? "gctx-name-task" : `gctx-body-${d.kind}`); return true;
    case "gctx-skill-edit": clearForm(c); c.edit = { kind: d.kind, id: d.id }; repaintCtx(); focusSoon(`gctx-body-${d.kind}`); return true;
    case "gctx-skill-open": c.open = String(c.open) === String(d.id) ? null : d.id; repaintCtx(); return true;
    case "gctx-rules-save": {
      if (!sameGroup(el)) return true;
      clearFormError("rules");
      const body = (($("gctx-rules") || {}).value || "").trim();
      if (body.length > RULES_MAX) return formError("rules", `最多 ${RULES_MAX} 字。`, "gctx-rules"), true;
      return write(el, c, "PUT", rulesUrl(c.gid), { body }, body ? "已保存，下次照做" : "已清空", (out) => (c.rules = out), "rules");
    }
    case "gctx-skill-save": {
      if (!sameGroup(el)) return true;
      const kind = d.kind, isNew = d.id === "new", task = kind === "task";
      clearFormError(kind);
      const body = (($(`gctx-body-${kind}`) || {}).value || "").trim();
      const name = task ? (($(`gctx-name-${kind}`) || {}).value || "").trim() : "";
      const description = task ? (($(`gctx-desc-${kind}`) || {}).value || "").trim() : "";
      const max = skillBodyMax(kind);
      if (task && isNew && !name) return formError(kind, "请填名称", `gctx-name-${kind}`), true;
      if (task && !description) return formError(kind, "请写何时用", `gctx-desc-${kind}`), true;
      if (!body) return formError(kind, "请写做法", `gctx-body-${kind}`), true;
      if (body.length > max) return formError(kind, `做法最多 ${max} 字。`, `gctx-body-${kind}`), true;
      if (name.length > SKILL_NAME_MAX) return formError(kind, `名称最多 ${SKILL_NAME_MAX} 字。`, `gctx-name-${kind}`), true;
      if (description.length > SKILL_DESC_MAX) return formError(kind, `「何时用」最多 ${SKILL_DESC_MAX} 字。`, `gctx-desc-${kind}`), true;
      const payload = isNew ? (task ? { kind, name, description, body } : { kind, body }) : task ? { description, body } : { body };
      return write(el, c, isNew ? "POST" : "PATCH", skillsUrl(c.gid, isNew ? null : d.id), payload,
        isNew ? "已保存，下次会参考" : "已保存，旧版在「历史」", (out) => replaceKind(c, kind, out.skills), kind);
    }
    case "gctx-skill-lock": {
      if (!sameGroup(el)) return true;
      const on = el.getAttribute("aria-pressed") !== "true";
      return write(el, c, "PATCH", skillsUrl(c.gid, d.id), { locked: on }, on ? "已锁定" : "已解锁", (out) => replaceKind(c, d.kind, out.skills));
    }
    case "gctx-skill-status": {
      if (!sameGroup(el)) return true;
      const to = d.to === "active" ? "active" : "archived";
      return write(el, c, "PATCH", skillsUrl(c.gid, d.id), { status: to }, to === "active" ? "已恢复" : "已归档", (out) => replaceKind(c, d.kind, out.skills));
    }
    case "gctx-skill-del": {
      if (!sameGroup(el)) return true;
      if (!confirm("删掉？连历史一起删、找不回；只是不想用可「归档」")) return true;
      return write(el, c, "DELETE", skillsUrl(c.gid, d.id), undefined, "删掉了", (out) => replaceKind(c, d.kind, out.skills));
    }
    case "gctx-hist": {
      if (c.hist && String(c.hist.id) === String(d.id)) { c.hist = null; repaintCtx(); return true; }
      const mine = { id: d.id, versions: null };
      c.hist = mine;
      repaintCtx();
      try {
        const out = await api("GET", d.id === "rules" ? rulesUrl(c.gid, "/versions") : skillsUrl(c.gid, d.id, "/versions"));
        if (c.hist !== mine || state.gctx !== c) return true;
        mine.versions = out && Array.isArray(out.versions) ? out.versions : [];
      } catch (err) {
        if (c.hist !== mine) return true;
        mine.error = err.message;
      }
      repaintCtx();
      return true;
    }
    case "gctx-restore": {
      if (!sameGroup(el)) return true;
      if (!confirm("换成这一版？当前内容会先存一版")) return true;
      if (d.id === "rules") return write(el, c, "POST", rulesUrl(c.gid, `/versions/${enc(d.vid)}/restore`), {}, "已换回", (out) => (c.rules = out));
      return write(el, c, "POST", skillsUrl(c.gid, d.id, `/versions/${enc(d.vid)}/restore`), {}, "已换回", (out) => replaceKind(c, d.kind, out.skills));
    }
    default: return false;
  }
}
