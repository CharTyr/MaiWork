// MaiWork 网页 · 设置修改审计记录与一键撤销（全局「系统 → 修改记录」+ 各群「本群设置 → 本群修改记录」）。
import { admin, gadmin, state } from "../state.js";
import { esc, tip, toast, when } from "../util.js";
import { api } from "../api.js";
import { repaintSheet } from "../sheet.js";
import { renderView } from "../render.js";
import { loading } from "../pages/news.js";

export const ACTOR_NAMES = {
  admin_web: "网页修改",
  group_admin: "群管理员",
  admin_chat: "主模型对话",
  auto_source: "自动订阅",
  revert: "撤销恢复",
};

const ACTOR_FILTERS = [
  ["", "全部来源"],
  ["admin_web", "网页修改"],
  ["group_admin", "群管理员"],
  ["admin_chat", "主模型对话"],
  ["auto_source", "自动订阅"],
  ["revert", "撤销恢复"],
];

function groupTitle(scope) {
  if (!scope || scope === "global") return "全局";
  const rawGid = String(scope).startsWith("group:") ? String(scope).slice(6) : String(scope);
  const g = (state.groups || []).find((x) => x.id === rawGid) || ((state.settings && state.settings.groups) || []).find((x) => x.id === rawGid);
  return g ? g.name || `群 ${g.id}` : `群 ${rawGid}`;
}

function prettyJson(raw) {
  if (raw == null || raw === "") return "（空）";
  if (typeof raw === "object") {
    try { return JSON.stringify(raw, null, 2); } catch (_) { return String(raw); }
  }
  const s = String(raw);
  try {
    return JSON.stringify(JSON.parse(s), null, 2);
  } catch (_) {
    return s;
  }
}

export async function loadChanges(more = false) {
  if (!admin()) return;
  const cur = state.changes || { actor: "", items: [], hasMore: false, loading: false, error: "" };
  const actor = cur.actor || "";
  const beforeId = more && cur.items.length ? cur.items[cur.items.length - 1].id : 0;
  cur.loading = true;
  cur.error = "";
  state.changes = cur;
  const qs = new URLSearchParams();
  if (actor) qs.set("actor", actor);
  qs.set("limit", "50");
  if (beforeId > 0) qs.set("before_id", String(beforeId));
  try {
    const res = await api("GET", `/api/settings/changes?${qs.toString()}`);
    const list = Array.isArray(res && res.changes) ? res.changes : [];
    cur.items = more ? cur.items.concat(list) : list;
    cur.hasMore = Boolean(res && res.has_more);
  } catch (e) {
    cur.error = e.message;
  } finally {
    cur.loading = false;
  }
}

export async function loadGroupChanges(gid, more = false) {
  if (!gadmin() || !gid) return;
  const bag = (state.groupChanges = state.groupChanges || {});
  const cur = bag[gid] || { items: [], hasMore: false, loading: false, error: "", loaded: false };
  const beforeId = more && cur.items.length ? cur.items[cur.items.length - 1].id : 0;
  cur.loading = true;
  cur.error = "";
  bag[gid] = cur;
  const qs = new URLSearchParams({ limit: "20" });
  if (beforeId > 0) qs.set("before_id", String(beforeId));
  try {
    const res = await api("GET", `/api/groups/${encodeURIComponent(gid)}/changes?${qs.toString()}`);
    const list = Array.isArray(res && res.changes) ? res.changes : [];
    cur.items = more ? cur.items.concat(list) : list;
    cur.hasMore = Boolean(res && res.has_more);
    cur.loaded = true;
  } catch (e) {
    cur.error = e.message;
  } finally {
    cur.loading = false;
  }
}

export function changeCardHTML(c, opts = {}) {
  const forGroup = !!opts.forGroup;
  const gid = opts.gid || "";
  const actorLabel = ACTOR_NAMES[c.actor] || c.actor || "系统";
  const scopeLabel = forGroup ? "" : groupTitle(c.scope);
  const blockedByRole = !admin() && c.target_key === "group_approval";
  const canRevert = Boolean(c.revertible) && !c.reverted_at && !blockedByRole;

  let statusBadge = "";
  if (c.reverted_at) {
    statusBadge = `<span class="tag">已撤销</span>`;
  } else if (!c.revertible) {
    statusBadge = `<span class="tag warn">涉及凭据不可撤销</span>`;
  } else if (blockedByRole) {
    statusBadge = `<span class="tag">仅总管理员可撤销</span>`;
  }

  const hasDiff = (c.before_json != null && c.before_json !== "") || (c.after_json != null && c.after_json !== "");
  return `<div class="chg-card${c.reverted_at ? " is-reverted" : ""}" data-chg-id="${esc(c.id)}">
    <div class="chg-top">
      <div class="chg-badges">
        <span class="ntag">${esc(actorLabel)}</span>
        ${scopeLabel ? `<span class="tag">${esc(scopeLabel)}</span>` : ""}
        ${statusBadge}
      </div>
      <span class="chg-time">${esc(when(c.ts))}</span>
    </div>
    <div class="chg-sum">${esc(c.summary || c.target_key || "设置修改")}</div>
    ${c.reason ? `<div class="chg-reason">原因：${esc(c.reason)}</div>` : ""}
    <div class="chg-foot">
      ${hasDiff ? `<details class="chg-diff"><summary>查看变更前后值</summary>
        <div class="chg-diff-grid">
          <div><div class="fine">修改前</div><pre>${esc(prettyJson(c.before_json))}</pre></div>
          <div><div class="fine">修改后</div><pre>${esc(prettyJson(c.after_json))}</pre></div>
        </div>
      </details>` : `<span></span>`}
      ${canRevert ? `<button type="button" class="btn small" data-act="chg-revert" data-id="${esc(c.id)}"${forGroup ? ` data-g="${esc(gid)}"` : ""}>撤销</button>` : ""}
    </div>
  </div>`;
}

export function changesPage() {
  const cur = state.changes;
  if (!cur && admin()) {
    void loadChanges().then(() => repaintSheet());
    return loading();
  }
  const st = cur || { actor: "", items: [], hasMore: false, loading: false, error: "" };
  const filterBar = `<div class="filters chg-filters" role="group" aria-label="按修改来源筛选">
    ${ACTOR_FILTERS.map(
      ([k, label]) => `<button type="button" class="filter" data-act="chg-actor" data-actor="${k}" aria-pressed="${(st.actor || "") === k}">${label}</button>`
    ).join("")}
  </div>`;
  const head = `<div class="h-sub-row">
    <h2 class="h-sub h-sub-tight">修改记录${tip("记录网页、群管理员、主模型对话与自动订阅对设置的改动。密码与密钥仅记录修改动作，不保存明文且不可自动回退。")}</h2>
    <button type="button" class="btn small" data-act="chg-refresh"${st.loading ? " disabled" : ""}>刷新</button>
  </div>${filterBar}`;
  if (st.error) return `${head}<div class="warn-box" role="alert">${esc(st.error)}</div>`;
  if (st.loading && !st.items.length) return `${head}${loading()}`;
  if (!st.items.length) return `${head}<p class="h-meta">暂无修改记录</p>`;
  return `${head}
    <div class="chg-list">${st.items.map((c) => changeCardHTML(c)).join("")}</div>
    ${st.hasMore ? `<div class="actions"><button type="button" class="btn small" data-act="chg-more"${st.loading ? " disabled" : ""}>加载更多</button></div>` : ""}`;
}

export function groupChangesSection(gid) {
  if (!gadmin() || !gid) return "";
  const bag = state.groupChanges || {};
  const cur = bag[gid];
  const openAttr = cur && cur.open ? " open" : "";
  let inner = "";
  if (!cur || (!cur.loaded && !cur.loading && !cur.error)) {
    inner = `<div class="actions"><button type="button" class="btn small" data-act="chg-group-load" data-g="${esc(gid)}">查看本群近期修改</button></div>`;
  } else if (cur.error) {
    inner = `<div class="warn-box" role="alert">${esc(cur.error)} <button type="button" class="btn small" data-act="chg-group-load" data-g="${esc(gid)}">重试</button></div>`;
  } else if (cur.loading && !cur.items.length) {
    inner = loading();
  } else if (!cur.items.length) {
    inner = `<p class="fine">暂无本群修改记录</p>`;
  } else {
    inner = `<div class="chg-list compact">${cur.items.map((c) => changeCardHTML(c, { forGroup: true, gid })).join("")}</div>
      ${cur.hasMore ? `<div class="actions"><button type="button" class="btn small" data-act="chg-group-more" data-g="${esc(gid)}"${cur.loading ? " disabled" : ""}>加载更多</button></div>` : ""}`;
  }
  return `<div class="gctl-changes" data-g="${esc(gid)}">
    <div class="h-sub-row">
      <h2 class="h-sub">本群修改记录${tip("记录本群开关、主动发言、来源与审批设置的改动，支持单项撤销恢复。")}</h2>
      ${cur && cur.loaded ? `<button type="button" class="btn small" data-act="chg-group-load" data-g="${esc(gid)}"${cur.loading ? " disabled" : ""}>刷新</button>` : ""}
    </div>
    <div class="gctl-changes-body"${openAttr}>${inner}</div>
  </div>`;
}

export async function actChanges(action, el) {
  if (!String(action || "").startsWith("chg-")) return false;
  switch (action) {
    case "chg-actor": {
      state.changes = state.changes || { actor: "", items: [], hasMore: false, loading: false, error: "" };
      state.changes.actor = el.dataset.actor || "";
      await loadChanges(false);
      repaintSheet();
      return true;
    }
    case "chg-refresh":
      await loadChanges(false);
      repaintSheet();
      return true;
    case "chg-more":
      el.disabled = true;
      await loadChanges(true);
      repaintSheet();
      return true;
    case "chg-group-load": {
      const gid = el.dataset.g || state.g;
      if (!gid) return true;
      await loadGroupChanges(gid, false);
      renderView();
      return true;
    }
    case "chg-group-more": {
      const gid = el.dataset.g || state.g;
      if (!gid) return true;
      el.disabled = true;
      await loadGroupChanges(gid, true);
      renderView();
      return true;
    }
    case "chg-revert": {
      const id = el.dataset.id;
      const gid = el.dataset.g || "";
      if (!id) return true;
      const path = gid
        ? `/api/groups/${encodeURIComponent(gid)}/changes/${encodeURIComponent(id)}/revert`
        : `/api/settings/changes/${encodeURIComponent(id)}/revert`;
      el.disabled = true;
      try {
        try {
          await api("POST", path, {});
        } catch (err) {
          if (err && err.status === 409) {
            if (!confirm(`${err.message}\n确定强制恢复到该次修改前的值吗？`)) {
              el.disabled = false;
              return true;
            }
            await api("POST", path, { force: true });
          } else {
            throw err;
          }
        }
        toast("已恢复原设置");
        if (gid) {
          state.groupControls = null;
          await loadGroupChanges(gid, false);
          renderView();
        } else {
          await loadChanges(false);
          const { loadSettings } = await import("../router.js");
          const { loadRules } = await import("./rules.js");
          await Promise.all([loadSettings().catch(() => null), state.rules ? loadRules().catch(() => null) : null]);
          repaintSheet();
        }
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
      }
      return true;
    }
    default:
      return false;
  }
}
