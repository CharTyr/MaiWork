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
      <div class="upd-t"><b>MaiWork 有新版 ${esc(u.latest)}</b><span>现在是 ${esc(u.current)}${u.notes ? ` · ${esc(u.notes.replace(/^v?[\d.]+[：:\s]*/, ""))}` : ""}</span></div>
      <button class="btn small" data-act="upd-how">怎么更新</button>
      <button class="upd-x" data-act="upd-dismiss" aria-label="知道了，这一版不再提醒">${SVG.close}</button>
    </div>`;
}

// 设置 · 总览里的「版本」一节：一直显示当前版本；有新版时给步骤和按钮
export function updateSection() {
  if (!admin()) return "";
  const u = state.update;
  if (!u) return "";
  if (u.enabled === false) {
    return `<h2 class="h-sub">版本</h2><p class="fine">现在是 ${esc(u.current || "?")}。更新检查已关（配置 [console] update_check）。</p>`;
  }
  const checked = u.checked_ts ? `上次检查 ${esc(when(u.checked_ts))}` : "还没查过";
  if (!u.newer) {
    return `<h2 class="h-sub">版本</h2><p class="fine">现在是 ${esc(u.current || "?")}${u.latest ? "，已经是最新" : ""}。${checked}${u.error ? "（这次没连上 GitHub，过会儿再查）" : ""}</p>`;
  }
  const go = u.maibot_webui_url
    ? `<a class="btn primary small" href="${esc(u.maibot_webui_url)}" target="_blank" rel="noopener noreferrer">打开 MaiBot 网页</a>`
    : "";
  return `
    <h2 class="h-sub">版本</h2>
    <div class="upd-card">
      <div class="upd-head"><b>有新版 ${esc(u.latest)}</b><span>现在是 ${esc(u.current)} · ${checked}</span></div>
      ${u.notes ? `<p class="upd-notes">${esc(u.notes)}</p>` : ""}
      <ol class="upd-steps">
        <li>打开 MaiBot 自己的网页${u.maibot_webui_url ? "" : "（MaiWork 没记它的地址，可以在配置 [console] maibot_webui_url 里填上，这里就会有直达按钮）"}，进「插件管理」。</li>
        <li>找到 MaiWork，点「更新」。你的配置（config.toml）会保留，任务、资讯这些数据不在插件目录里，不受影响。</li>
        <li>更新会让 MaiBot 的全部插件重新加载一次，挑群里不忙的时候点。</li>
      </ol>
      <div class="actions">${go}<a class="btn small" href="${esc(u.repo_url || "https://github.com/CharTyr/MaiWork")}/commits/main" target="_blank" rel="noopener noreferrer">看改了什么</a></div>
    </div>`;
}
