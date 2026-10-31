// 社交平台与登录态：复用设置页、原生抽屉及同源 API；凭据仅在提交时读取。
import { esc, ico, tip, toast } from "../util.js";
import { api } from "../api.js";
import { closeSheet, openSheet, repaintSheet } from "../sheet.js";
import { state } from "../state.js";
import { loading } from "../pages/news.js";
import { socialLogo } from "../social-brand.js";

export const social = { platforms: null, err: "", busy: "", errors: {} };
let loadSeq = 0;
let pollTimer = 0;
let pollDeadline = 0;
let savingCookie = false;

// 重新登录时废弃旧身份的数据和仍在途的响应。
export function resetSocial() {
  loadSeq++;
  clearTimeout(pollTimer);
  pollDeadline = 0;
  social.platforms = null;
  social.err = "";
  social.busy = "";
  social.errors = {};
  state.cookieEdit = null;
}

export async function loadSocial() {
  const seq = ++loadSeq;
  try {
    const data = await api("GET", "/api/social/platforms");
    if (seq !== loadSeq) return false;
    if (!Array.isArray(data?.platforms)) throw new Error("平台列表格式有误");
    social.platforms = data.platforms;
    social.err = "";
    schedulePoll();
    return true;
  } catch (e) {
    if (seq === loadSeq) social.err = e.message;
    return false;
  }
}

function repaint() {
  // 背景安装进度只更新页面，不能替换正在输入的登录抽屉。
  repaintSheet({ preserveSheet: true });
}

function schedulePoll() {
  clearTimeout(pollTimer);
  const running = (social.platforms || []).some((p) => p.installing);
  if (!running && !pollDeadline) return;
  if (!pollDeadline) pollDeadline = Date.now() + 180000;
  pollTimer = setTimeout(async () => {
    const visible = state.page === "settings" && ["extensions", "social"].includes(state.setSub);
    if (!visible || document.hidden) { pollDeadline = 0; return; }
    if (Date.now() >= pollDeadline) {
      pollDeadline = 0;
      social.err = "安装进度等待超时，请刷新查看结果";
      repaint();
      return;
    }
    const loaded = await loadSocial();
    if (loaded && !(social.platforms || []).some((p) => p.installing)) {
      clearTimeout(pollTimer);
      pollDeadline = 0;
    } else schedulePoll();
    repaint();
  }, 2000);
}

function rowError(p) {
  const error = social.errors[p.id] || p.last_error;
  return error ? `<div class="warn-box" role="status">${esc(error)}</div>` : "";
}

function button(action, p, text, primary = false) {
  return `<button class="btn${primary ? " primary" : ""}" type="button" data-act="social-${action}" data-id="${esc(p.id)}" ${social.busy || (p.installing && action !== "disable") ? "disabled" : ""}>${text}</button>`;
}

function platformRow(p) {
  let status = !p.enabled ? "未启用" : p.installing ? "正在安装…" : !p.installed ? "未安装" : "已安装";
  if (p.enabled && !p.installing && p.installed) {
    status = p.needs_cookie && !p.cookie_set ? "待填写登录态" : p.last_error || social.errors[p.id] ? "检测未通过" : p.last_check ? "已检测" : "待检测";
  }
  const actions = !p.enabled ? button("enable", p, "启用", true) :
    `${!p.installed && !p.installing ? button("install", p, "重新安装") : button("check", p, "检测")}${button("disable", p, "停用")}`;
  const login = p.needs_cookie || p.supports_cookie;
  const cookieState = login ? (p.cookie_set ? "登录态已填" : "登录态未填") : "无需填写登录态";
  const cookieActions = login ? `${button("cookie", p, p.cookie_set ? "重填登录态" : "填写登录态", !p.cookie_set)}${p.cookie_set ? button("cookie-clear", p, "清空登录态") : ""}` : "";
  return `<div class="set-row social-row">${socialLogo(p.id)}<div><div class="set-name">${esc(p.name)}</div><div class="set-text" role="status">${esc(status)} · ${cookieState}</div>${rowError(p)}</div><span class="row-btns">${actions}${cookieActions}</span></div>`;
}

function content(rows) {
  const error = social.err ? `<div class="warn-box" role="status">${esc(social.err)} <button class="btn" data-act="social-reload">重试</button></div>` : "";
  if (social.platforms === null) return error || loading();
  return error + (rows ? `<div class="set-group">${rows}</div>` : `<p class="h-meta">暂无平台</p>`);
}

export function socialPage() {
  const enabled = (social.platforms || []).some((p) => p.enabled && p.installed && !p.installing);
  return `<div class="social-settings"><div class="h-sub-row"><h2 class="h-sub">平台${tip("启用后自动安装所需组件，并按群关注点搜索。需要登录的平台在同一行填写登录态；安装完成后可单独检测。")}</h2><button class="btn" data-act="social-check-all" ${social.busy || !enabled ? "disabled" : ""}>${social.busy === "all" ? "正在检测…" : "全部检测"}</button></div>${content((social.platforms || []).map(platformRow).join(""))}</div>`;
}

function cookieHelp(e) {
  const guides = {
    twitter: ["https://x.com", "必须包含 auth_token 和 ct0。"],
    xiaohongshu: ["https://www.xiaohongshu.com", "建议包含 a1 和 web_session；是否有效以检测结果为准。"],
    reddit: ["https://www.reddit.com", "复制完整 Cookie；是否有效以检测结果为准。"],
  };
  const [url, fields] = guides[e.id] || ["", e.guide || "复制已登录平台的完整 Cookie。"];
  return `<section id="cookie-help" class="cookie-guide" aria-label="获取 Cookie"><h2 class="h-sub">获取 Cookie</h2>
    <ol><li>使用电脑上的 Chrome 或 Edge，${url ? `打开 <a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>` : "打开平台网站"}，登录自己的账号。</li>
    <li>按 F12（Mac：⌘⌥I），选择「Network / 网络」，再刷新页面。在平台内搜索一次，选择发往该平台的请求。</li>
    <li>打开「Headers / 标头」→「Request Headers / 请求标头」，找到 <code>Cookie</code>。复制它右侧的完整值；没有 Cookie 时换一个已登录的请求，不要复制响应里的 Set-Cookie。</li>
    <li>将值粘贴到下方，不要带 <code>Cookie:</code> 前缀、换行或引号。格式为 <code>名称=值; 名称=值</code>。${esc(fields)}</li>
    <li>保存后，在该平台行点击「检测」。若提示登录失效，重新登录平台后再次复制。</li></ol>
    <p class="fine">Cookie 相当于登录凭证，仅填写自己的登录态，不要分享。保存后不回显。手机不便获取时，可在电脑打开此设置页填写。</p></section>`;
}

export function socialCookieSheet() {
  const e = state.cookieEdit || {};
  return `<h1 class="h-page">${esc(e.name || "登录态")}</h1>${cookieHelp(e)}<form id="social-cookie-form" class="login" autocomplete="off">
    <label for="cookie-input">Cookie</label>
    <input id="cookie-input" type="password" autocomplete="new-password" maxlength="8000" spellcheck="false" autocapitalize="off" aria-describedby="cookie-help cookie-err" aria-invalid="false" required />
    <p class="err" id="cookie-err" role="alert" hidden></p><button class="btn primary wide" type="submit">保存</button></form>`;
}

document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (form?.id !== "social-cookie-form") return;
  event.preventDefault();
  const edit = state.cookieEdit;
  if (savingCookie || !edit?.id) return;
  const input = document.getElementById("cookie-input");
  const err = document.getElementById("cookie-err");
  const submit = form.querySelector('[type="submit"]');
  const cookie = (input?.value || "").trim();
  err.hidden = true;
  input.setAttribute("aria-invalid", "false");
  if (!cookie || cookie.length > 8000) {
    err.textContent = !cookie ? "请填写 Cookie" : "Cookie 最多 8000 字符";
    err.hidden = false;
    input.setAttribute("aria-invalid", "true");
    return;
  }
  savingCookie = true;
  submit.disabled = true;
  submit.textContent = "正在保存…";
  form.setAttribute("aria-busy", "true");
  try {
    await api("POST", `/api/social/platforms/${encodeURIComponent(edit.id)}/cookie`, { cookie });
    input.value = "";
    if (state.cookieEdit === edit) {
      state.cookieEdit = null;
      if (state.sheet === "social-cookie") closeSheet();
    }
    toast("已保存");
    await loadSocial();
    repaint();
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
  } finally {
    savingCookie = false;
    submit.disabled = false;
    submit.textContent = "保存";
    form.setAttribute("aria-busy", "false");
  }
});

export async function actSocial(a, el) {
  if (!a.startsWith("social-")) return false;
  if (a === "social-reload") { await loadSocial(); repaint(); return true; }
  if (social.busy) return true;
  if (a === "social-check-all") {
    const enabled = (social.platforms || []).filter((p) => p.enabled && p.installed && !p.installing);
    social.busy = "all";
    repaint();
    let ok = 0;
    for (const p of enabled) {
      try {
        const result = await api("POST", `/api/social/platforms/${encodeURIComponent(p.id)}/check`);
        social.errors[p.id] = result.ok ? "" : result.error || "检测失败";
        if (result.ok) ok++;
      } catch (e) { social.errors[p.id] = e.message; }
    }
    social.busy = "";
    await loadSocial();
    repaint();
    toast(`检测完成：${ok} 个通过，${enabled.length - ok} 个失败`, ok !== enabled.length);
    return true;
  }
  const id = el.dataset.id;
  const p = (social.platforms || []).find((item) => item.id === id);
  if (!p) return true;
  if (a === "social-cookie") {
    state.cookieEdit = { id, name: p.name, guide: p.cookie_guide };
    openSheet("social-cookie");
    return true;
  }
  if (a === "social-cookie-clear" && !confirm(`清空 ${p.name} 的登录态？需要登录的搜索会停止。`)) return true;
  const operation = { "social-enable": "enable", "social-disable": "disable", "social-install": "install", "social-check": "check", "social-cookie-clear": "cookie" }[a];
  if (!operation) return false;
  social.busy = id;
  social.errors[id] = "";
  repaint();
  try {
    const result = await api(operation === "cookie" ? "DELETE" : "POST", `/api/social/platforms/${encodeURIComponent(id)}/${operation}`);
    if (result.ok === false) throw new Error(result.error || "操作失败");
    if (["enable", "install"].includes(operation)) pollDeadline = Date.now() + 180000;
    toast({ enable: "已启用", disable: "已停用", install: "已开始安装", check: "检测通过", cookie: "已清空" }[operation]);
  } catch (e) {
    social.errors[id] = e.message;
    toast(e.message, true);
  } finally {
    social.busy = "";
    await loadSocial();
    repaint();
  }
  return true;
}
