// MaiWork 网页 · 更新提醒（只给总管理员；只提醒，更新去 MaiBot 自己的插件管理点）。
import { admin, state } from "./state.js";
import { SVG, esc, when } from "./util.js";
import { api } from "./api.js";

const SEEN_KEY = "mw-upd-seen"; // 点过「知道了」的版本号：同一版不再在群页顶上提醒

export async function loadUpdate() {
  if (!admin()) return (state.update = null);
  try {
    state.update = await api("GET", "/api/update");
  } catch (e) {
    state.update = null; // 查不到就不提醒，不报错
  }
  return state.update;
}

export const hasUpdate = () => !!(admin() && state.update && state.update.newer && state.update.latest);

function seen() {
  try {
    return localStorage.getItem(SEEN_KEY) || "";
  } catch (e) {
    return "";
  }
}

export function dismissUpdate() {
  if (!state.update) return;
  try {
    localStorage.setItem(SEEN_KEY, state.update.latest || "");
  } catch (e) {}
}

// 群页最上面一条小提示：有新版且这一版还没点过「知道了」
export function updateBanner() {
  if (!hasUpdate() || seen() === state.update.latest) return "";
  const u = state.update;
  return `
    <div class="upd-banner" role="status">
      <span class="upd-dot" aria-hidden="true"></span>
      <div class="upd-t"><b>有新版 ${esc(u.latest)}</b><span>当前 ${esc(u.current)}${u.notes ? ` · ${esc(u.notes.replace(/^v?[\d.]+[：:\s]*/, ""))}` : ""}</span></div>
      <button class="btn small" data-act="upd-how">怎么更新</button>
      <button class="upd-x" data-act="upd-dismiss" aria-label="知道了">${SVG.close}</button>
    </div>`;
}

// 设置 · 总览里的「版本」一节：一直显示当前版本；有新版时给步骤和按钮
export function updateSection() {
  if (!admin()) return "";
  const u = state.update;
  if (!u) return "";
  if (u.enabled === false) {
    return `<h2 class="h-sub">版本</h2><p class="fine">当前 ${esc(u.current || "?")} · 已关闭检查更新</p>`;
  }
  const checked = u.checked_ts ? `上次检查 ${esc(when(u.checked_ts))}` : "还没查过";
  if (!u.newer) {
    return `<h2 class="h-sub">版本</h2><p class="fine">当前 ${esc(u.current || "?")}${u.latest ? "，已是最新" : ""}。${checked}${u.error ? "（这次没连上，稍后再查）" : ""}</p>`;
  }
  const go = u.maibot_webui_url
    ? `<a class="btn primary small" href="${esc(u.maibot_webui_url)}" target="_blank" rel="noopener noreferrer">打开 MaiBot</a>`
    : "";
  return `
    <h2 class="h-sub">版本</h2>
    <div class="upd-card">
      <div class="upd-head"><b>有新版 ${esc(u.latest)}</b><span>当前 ${esc(u.current)} · ${checked}</span></div>
      ${u.notes ? `<p class="upd-notes">${esc(u.notes)}</p>` : ""}
      <ol class="upd-steps">
        <li>打开 MaiBot 网页，进「插件管理」${u.maibot_webui_url ? "" : "（在「全部设置 → 网页」填上 MaiBot 网址，这里就有直达按钮）"}</li>
        <li>找到 MaiWork 点「更新」，设置和数据都保留</li>
        <li>所有插件会重载一次，挑群里不忙时</li>
      </ol>
      <div class="actions">${go}<a class="btn small" href="${esc(u.repo_url || "https://github.com/CharTyr/MaiWork")}/commits/main" target="_blank" rel="noopener noreferrer">看更新内容</a></div>
    </div>`;
}
