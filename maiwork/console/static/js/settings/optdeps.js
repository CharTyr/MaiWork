// MaiWork 网页 · 设置 ·「工具」里的「可选组件」（2026-10-15 用户定：办公文档三库以可选安装方式提供）。
// 后端 GET /api/optional-deps、POST /api/optional-deps/{id}/install|uninstall（只总管理员；装 / 卸在后台跑）。
// 装在 MaiWork 自己的目录里，不碰 MaiBot 的环境；没装时 office-docs 会退回 Markdown / 表格 / 网页交付。
import { state } from "../state.js";
import { esc, ico, tip, toast } from "../util.js";
import { api } from "../api.js";
import { repaintSheet } from "../sheet.js";

export async function loadOptDeps() {
  let loaded = false;
  try {
    state.optDeps = await api("GET", "/api/optional-deps");
    state.optDepsErr = "";
    loaded = true;
  } catch (e) {
    // 后台刷新失败保留上次进度；503 表示这个版本没有该模块。
    if (e.status === 503) state.optDeps = null;
    state.optDepsErr = e.status === 503 ? "" : e.message;
  }
  schedulePoll();
  return loaded;
}

// 装 / 卸的时候每 3 秒看一次进度；离开「工具」页就停
let pollTimer = 0;
function busy() {
  return ((state.optDeps || {}).groups || []).some((g) => g.installing || g.uninstalling) || !!(state.optDeps || {}).checking;
}
function schedulePoll() {
  clearTimeout(pollTimer);
  if (!busy()) return;
  pollTimer = setTimeout(async () => {
    if (state.page !== "settings" || state.setSub !== "extensions") return;
    const running = ((state.optDeps || {}).groups || []).filter((g) => g.installing || g.uninstalling);
    const loaded = await loadOptDeps();
    repaintSheet();
    if (loaded && running.length && !busy()) {
      const finished = ((state.optDeps || {}).groups || []).filter((g) => running.some((r) => r.id === g.id));
      if (finished.length !== running.length || finished.some((g) => !["ok", "failed", "interrupted"].includes((g.last_install || {}).state))) return;
      const failed = finished.some((g) => ["failed", "interrupted"].includes((g.last_install || {}).state));
      const uninstall = running.some((g) => g.uninstalling);
      toast(failed ? `${uninstall ? "卸载没成功" : "没装成功"}，原因见「可选组件」` : uninstall ? "已卸载" : "已安装", failed);
    }
  }, 3000);
}

function pkgText(p) {
  if (p.installed === true) return `${esc(p.name)} ${esc(p.version || "")}`.trim();
  if (p.installed === false) return `<span class="od-miss">${esc(p.name)} 未装</span>`;
  return `${esc(p.name)} 未检查`;
}

function stateText(g) {
  if (g.installing) return "正在安装，通常需要一到两分钟，可以先离开这个页面";
  if (g.uninstalling) return "正在卸载…";
  const n = g.installed_count || 0;
  const total = g.total || (g.packages || []).length;
  if (g.state === "ok") return "已安装，子 agent 可以直接生成和编辑这类文件";
  if (g.state === "partial") return `装了一部分（${n}/${total}），建议重新安装`;
  if (g.state === "missing") return "未安装。不装也能用：会改用 Markdown、表格或网页交付";
  return "还没检查";
}

function groupRow(g) {
  const li = g.last_install || {};
  const doing = g.installing || g.uninstalling;
  const ok = g.state === "ok";
  const btns = doing
    ? `<span class="h-meta">处理中…</span>`
    : `<span class="row-btns">${
        ok
          ? `<button class="btn small" data-act="od-uninstall" data-id="${esc(g.id)}">卸载</button>`
          : `<button class="btn small primary" data-act="od-install" data-id="${esc(g.id)}">${g.state === "partial" || li.state === "failed" ? "重新安装" : "安装"}</button>`
      }</span>`;
  const warn =
    li.state === "failed" && !doing
      ? `<div class="warn-box" style="margin-top:8px"><b>上次操作没成功：</b>${esc(li.error || "原因未知")}</div>`
      : li.state === "interrupted" && !doing
        ? `<div class="warn-box" style="margin-top:8px">上次操作被打断（插件重新加载了），请重新操作</div>`
        : "";
  const tools = (g.tools || [])
    .filter((t) => !t.found)
    .map((t) => `<p class="fine">${esc(t.note || `${t.name} 未找到`)}</p>`)
    .join("");
  const pkgs = (g.packages || []).length ? `<div class="set-text">${(g.packages || []).map(pkgText).join(" · ")}</div>` : "";
  return `<div class="set-row cp">${ico("filebox")}<div>
      <div class="set-name">${esc(g.label || g.id)}</div>
      <div class="set-text">${esc(stateText(g))}</div>${pkgs}${warn}${tools}
    </div><span style="display:flex;align-items:center;gap:10px">${btns}<span class="dot ${ok ? "ok" : doing || g.state === "partial" ? "pending" : ""}"></span></span></div>`;
}

export function optDepsSection() {
  const v = state.optDeps;
  if (!v && !state.optDepsErr) return "";
  const head = `<div class="h-sub-row"><h2 class="h-sub">可选组件${tip("按需安装的额外能力。安装在 MaiWork 自己的目录里，不会改动 MaiBot 的运行环境。")}</h2></div>`;
  if (!v) return `${head}<p class="h-meta">读取失败：${esc(state.optDepsErr)} <button type="button" class="link-btn" data-act="od-reload">重试</button></p>`;
  if (v.available === false) return `${head}<p class="h-meta">${esc(v.note || "本机执行没有开启，暂时不能安装")}</p>`;
  const refreshError = state.optDepsErr ? `<p class="h-meta" role="status">刷新失败：${esc(state.optDepsErr)} <button type="button" class="link-btn" data-act="od-reload">重试</button></p>` : "";
  return `${head}${refreshError}${(v.groups || []).map(groupRow).join("")}${v.check_error ? `<p class="fine">检查出错：${esc(v.check_error)}</p>` : ""}`;
}

export async function actOptDeps(a, el) {
  switch (a) {
    case "od-reload":
      await loadOptDeps();
      repaintSheet();
      return true;
    case "od-install":
    case "od-uninstall": {
      const id = el.dataset.id;
      if (a === "od-uninstall" && !confirm("卸载办公文档组件？之后子 agent 会改用 Markdown、表格或网页交付。")) return true;
      el.disabled = true;
      try {
        state.optDeps = await api("POST", `/api/optional-deps/${encodeURIComponent(id)}/${a === "od-install" ? "install" : "uninstall"}`);
        toast(a === "od-install" ? "开始安装" : "开始卸载");
      } catch (err) {
        toast(err.message, true);
        await loadOptDeps();
      }
      repaintSheet();
      schedulePoll();
      return true;
    }
  }
  return false;
}
