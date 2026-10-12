// MaiWork 网页 · 抽屉：登录、编辑表单、打开 / 关闭。
import { $, CATS, desktop, isGA, state } from "./state.js";
import { RATE_REASONS } from "./pages/news.js";
import { SVG, esc, tip } from "./util.js";
import { gname, grp } from "./api.js";
import { detailHTML, loadTask } from "./detail.js";
import { handoffSheet } from "./handoff.js";
import { groupPicker } from "./settings/index.js";
import { draft } from "./settings/models.js";
import { chatSide } from "./chat.js";
import { renderRail, renderSide, renderView } from "./render.js";
import { syncHash } from "./router.js";

function loginSheet() {
  if (isGA())
    return `
    <h1 class="h-page">群管理员</h1>
    <div class="h-meta sheet-lead">当前管理：「${esc(gname(grp()))}」${tip("群管理员可审批任务、修改群画像和本群规则；关注成员的个人画像仅可查看。")}</div>
    <div class="actions" style="margin-top:22px"><button class="btn" data-act="logout">退出登录</button></div>`;
  return `
    <h1 class="h-page">管理员登录</h1>
    
    <form id="login" class="login" autocomplete="off">
      <label for="pw">密码</label>
      <input id="pw" name="pw" type="password" autocomplete="current-password" placeholder="管理员或群管理员密码" />
      <p class="err" ${state.loginError ? "" : "hidden"}>${esc(state.loginError)}</p>
      <button class="btn primary wide" type="submit">登录</button>
      <div class="fine">忘记密码${tip("管理员密码保存在服务器的 console_password.txt 文件中。")}</div>
    </form>`;
}

function rateSheet(e) {
  const picked = new Set(e.reasons || []);
  return `
    <h1 class="h-page">评价</h1>
    <p class="h-meta sheet-lead">${esc(e.title || "")}</p>
    <form id="edit" class="login" autocomplete="off">
      <label>问题（可多选）</label>
      <div class="rate-chips">${RATE_REASONS.map(
        ([k, n]) => `<button type="button" class="rate-chip" data-act="rate-chip" data-k="${k}" aria-pressed="${picked.has(k)}">${esc(n)}</button>`
      ).join("")}</div>
      <div class="lbl-row"><label for="ed-text">补充说明（可选）</label>${tip("下次搜集资讯时会参考此评价。")}</div>
      <input id="ed-text" maxlength="60" spellcheck="false" placeholder="例如：上周已看过" value="${esc(e.note || "")}" />
      <p class="err" id="ed-err" hidden></p>
      <button class="btn primary wide" type="submit">提交</button>
      ${e.had ? `<button class="btn wide" type="button" data-act="rate-clear" style="margin-top:10px">撤回评价</button>` : ""}
    </form>`;
}

function editSheet() {
  const e = state.editing || {};
  if (e.kind === "rate") return rateSheet(e);
  if (e.kind === "ga")
    return `
    <h1 class="h-page">群管理员 · ${esc(e.name || "")}</h1>
    <p class="h-meta sheet-lead">仅可管理本群：审批任务、修改群画像和本群规则</p>
    <form id="edit" class="login" autocomplete="off">
      <label for="ga-pw">登录密码</label>
      <input id="ga-pw" type="password" autocomplete="new-password" placeholder="${e.password_set ? "已设置，留空则不修改" : "至少 8 位，可留空"}" />
      <p class="fine">审批名单在群页面的「任务审批」中设置</p>
      <p class="err" id="ed-err" hidden></p>
      <button class="btn primary wide" type="submit">保存</button>
      ${e.password_set ? `<button class="btn wide" type="button" data-act="ga-clear" style="margin-top:10px">清除密码</button>` : ""}
    </form>`;
  const title = e.kind === "focus" ? "添加关注" : e.id ? "修改" : `添加到「${(CATS.find((c) => c[0] === e.cat) || [, ""])[1]}」`;
  const isFocus = e.kind === "focus";
  return `
    <h1 class="h-page">${esc(title)}</h1>
    <p class="h-meta sheet-lead">${isFocus ? "仅管理员可见" : "修改后 MaiWork 不再自动更改此项"}</p>
    <form id="edit" class="login" autocomplete="off">
      ${
        isFocus
          ? `<label for="ed-text">QQ 号</label><input id="ed-text" inputmode="numeric" spellcheck="false" placeholder="例如 10001" />`
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
  if (kind === "handoff") return handoffSheet();
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
  if (state.sheet === "handoff") {
    // 手机上从详情抽屉打开的交接包：关掉回到详情，不把详情一起关没
    const back = state.handoff && state.handoff.back && state.detail;
    state.handoff = null;
    if (back) {
      openSheet("detail");
      return;
    }
  }
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
