// MaiWork 网页 · 抽屉：登录、编辑表单、打开 / 关闭。
import { $, CATS, desktop, isGA, state } from "./state.js";
import { RATE_REASONS } from "./pages/news.js";
import { SVG, esc } from "./util.js";
import { gname, grp } from "./api.js";
import { detailHTML, loadTask } from "./detail.js";
import { groupPicker } from "./settings/index.js";
import { draft } from "./settings/models.js";
import { chatSide } from "./chat.js";
import { renderRail, renderSide, renderView } from "./render.js";
import { syncHash } from "./router.js";

function loginSheet() {
  if (isGA())
    return `
    <h1 class="h-page">群管理员</h1>
    <p class="h-meta sheet-lead">你管的是「${esc(gname(grp()))}」：能批准本群派的活、改群画像、本群规矩和做法。关注成员的个人画像只能看。</p>
    <div class="actions" style="margin-top:22px"><button class="btn" data-act="logout">退出</button></div>
    <p class="fine" style="margin-top:22px">要用别的密码登录，先退出。</p>`;
  return `
    <h1 class="h-page">管理员</h1>
    
    <form id="login" class="login" autocomplete="off">
      <label for="pw">密码</label>
      <input id="pw" name="pw" type="password" autocomplete="current-password" placeholder="管理员或群管理员密码" />
      <p class="err" ${state.loginError ? "" : "hidden"}>${esc(state.loginError)}</p>
      <button class="btn primary wide" type="submit">进入管理</button>
      <p class="fine">忘了密码？在服务器上 MaiWork 数据目录的 console_password.txt 里</p>
    </form>`;
}

function rateSheet(e) {
  const picked = new Set(e.reasons || []);
  return `
    <h1 class="h-page">评价这条资讯</h1>
    <p class="h-meta sheet-lead">${esc(e.title || "")}</p>
    <form id="edit" class="login" autocomplete="off">
      <label>哪里不好（可以多选）</label>
      <div class="rate-chips">${RATE_REASONS.map(
        ([k, n]) => `<button type="button" class="rate-chip" data-act="rate-chip" data-k="${k}" aria-pressed="${picked.has(k)}">${esc(n)}</button>`
      ).join("")}</div>
      <label for="ed-text">想补一句（可选）</label>
      <input id="ed-text" maxlength="60" spellcheck="false" placeholder="比如「上周就看过了」" value="${esc(e.note || "")}" />
      <p class="fine">下一轮找资讯会参考大家的评价。</p>
      <p class="err" id="ed-err" hidden></p>
      <button class="btn primary wide" type="submit">提交</button>
      ${e.had ? `<button class="btn wide" type="button" data-act="rate-clear" style="margin-top:10px">撤回我的评价</button>` : ""}
    </form>`;
}

function editSheet() {
  const e = state.editing || {};
  if (e.kind === "rate") return rateSheet(e);
  if (e.kind === "ga")
    return `
    <h1 class="h-page">群管理员 · ${esc(e.name || "")}</h1>
    <p class="h-meta sheet-lead">能批准本群派的活、改群画像、本群规矩和做法；看不到别的群和全局设置</p>
    <form id="edit" class="login" autocomplete="off">
      <label for="ga-pw">网页密码</label>
      <input id="ga-pw" type="password" autocomplete="new-password" placeholder="${e.password_set ? "已设置 · 留空就不改" : "至少 8 位 · 可以不填"}" />
      <p class="fine">批准与免批名单在群页「谁能批本群的活」里设置，这里只改网页密码。</p>
      <p class="err" id="ed-err" hidden></p>
      <button class="btn primary wide" type="submit">保存</button>
      ${e.password_set ? `<button class="btn wide" type="button" data-act="ga-clear" style="margin-top:10px">清掉网页密码</button>` : ""}
    </form>`;
  const title = e.kind === "focus" ? "加一个关注成员" : e.id ? "改这一条" : `加到「${(CATS.find((c) => c[0] === e.cat) || [, ""])[1]}」`;
  const isFocus = e.kind === "focus";
  return `
    <h1 class="h-page">${esc(title)}</h1>
    <p class="h-meta sheet-lead">${isFocus ? "个人画像只有管理员看得到" : "改过的条目 MaiWork 不会再动"}</p>
    <form id="edit" class="login" autocomplete="off">
      ${
        isFocus
          ? `<label for="ed-text">QQ 号</label><input id="ed-text" inputmode="numeric" spellcheck="false" placeholder="比如 10001" />`
          : `<label for="ed-text">内容</label><textarea id="ed-text" rows="3" spellcheck="false">${esc(e.text || "")}</textarea>`
      }
      <p class="err" id="ed-err" hidden></p>
      <button class="btn primary wide" type="submit">保存</button>
    </form>`;
}

/* ───────────── 抽屉 ───────────── */

export const sheetChrome = () => `<div class="grab"></div><button class="sheet-close" data-act="close" aria-label="关闭">${SVG.close}</button>`;

function sheetHTML(kind) {
  if (kind === "chats") return chatsSheet();
  if (kind === "groups") return groupPicker();
  if (kind === "login") return loginSheet();
  if (kind === "edit") return editSheet();
  return detailHTML();
}

function chatsSheet() {
  return `<h1 class="h-page">对话</h1>` + chatSide();
}

export function openSheet(kind) {
  const was = state.sheet;
  state.sheet = kind;
  const sh = $("sheet");
  sh.innerHTML = sheetChrome() + sheetHTML(kind);
  sh.hidden = false;
  $("scrim").hidden = false;
  sh.scrollTop = 0;
  if (!was) {
    requestAnimationFrame(() => {
      sh.classList.add("on");
      $("scrim").classList.add("on");
    });
  }
  sh.setAttribute("tabindex", "-1");
  sh.focus({ preventScroll: true });
}

export function repaintSheet() {
  if (state.page === "settings") {
    const y = window.scrollY;
    renderView();
    renderSide();
    renderRail();
    window.scrollTo(0, y);
  }
  if (!state.sheet) return;
  const sh = $("sheet");
  const top = sh.scrollTop;
  sh.innerHTML = sheetChrome() + sheetHTML(state.sheet);
  sh.scrollTop = top;
}

export function closeSheet() {
  if (!state.sheet) return;
  if (state.sheet === "detail") {
    state.detail = null;
    syncHash();
    renderView();
  }
  state.sheet = null;
  draft.models = null;
  const sh = $("sheet");
  sh.classList.remove("on");
  $("scrim").classList.remove("on");
  setTimeout(() => {
    if (!state.sheet) {
      sh.hidden = true;
      $("scrim").hidden = true;
    }
  }, 420);
}

export function openDetail(type, id) {
  state.detail = { type, id };
  syncHash();
  if (type === "task") loadTask(id);
  if (desktop.matches) {
    renderView();
    renderSide();
    $("side").scrollTop = 0;
  } else {
    openSheet("detail");
  }
}
