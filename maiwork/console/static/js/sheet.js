// MaiWork 网页 · 抽屉：登录、编辑表单、打开 / 关闭。
import { $, CATS, desktop, isGA, state, ui } from "./state.js";
import { RATE_REASONS } from "./pages/news.js";
import { SVG, calm, esc, tip } from "./util.js";
import { gname, grp } from "./api.js";
import { detailHTML, loadTask } from "./detail.js";
import { handoffSheet } from "./handoff.js";
import { groupPicker } from "./settings/index.js";
import { draft, modelsFormSheet } from "./settings/models.js";
import { jevFormSheet } from "./settings/jev.js";
import { extFormSheet } from "./settings/ext.js";
import { agentsFormSheet } from "./settings/agents.js";
import { chatSide } from "./chat.js";
import { renderRail, renderSide, renderView } from "./render.js";
import { syncHash } from "./router.js";
import { appearanceSheet } from "./theme.js";
import { socialCookieSheet } from "./settings/social.js";
import { sfx } from "./sound.js";
import { boardItemSheet } from "./board.js";
import { previewSheet } from "./preview.js";

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

function hasSettingsForm() {
  return Boolean(
    state.mdlEdit ||
    state.jevPick ||
    state.jevEdit ||
    state.extEdit ||
    state.agentNew ||
    state.agentEdit ||
    state.agentFishEdit
  );
}

function clearSettingsForm() {
  state.mdlEdit = null;
  state.jevPick = false;
  state.jevEdit = null;
  state.extEdit = null;
  state.agentNew = false;
  state.agentEdit = null;
  state.agentFishEdit = null;
  state.agentFishDraft = null;
}

function settingsFormSheet() {
  if (state.mdlEdit) return modelsFormSheet();
  if (state.jevEdit || state.jevPick) return jevFormSheet();
  if (state.extEdit) return extFormSheet();
  if (state.agentNew || state.agentEdit || state.agentFishEdit) return agentsFormSheet();
  return "";
}

function sheetHTML(kind) {
  if (kind === "chats") return chatsSheet();
  if (kind === "groups") return groupPicker();
  if (kind === "appearance") return appearanceSheet();
  if (kind === "social-cookie") return socialCookieSheet();
  if (kind === "settings-form") return settingsFormSheet();
  if (kind === "login") return loginSheet();
  if (kind === "edit") return editSheet();
  if (kind === "handoff") return handoffSheet();
  if (kind === "board-item") return boardItemSheet();
  if (kind === "preview") return previewSheet();
  return detailHTML();
}

function chatsSheet() {
  return `<h1 class="h-page">对话</h1>` + chatSide();
}

// §4.3 抽屉焦点陷进：开时记住焦点、关时还原；Tab 只能在抽屉里转
let _sheetPrevFocus = null;
let _sheetBack = null;
let _closeTimer = 0;
let _dragBound = false;
let _drag = null;

// 手机底部抽屉跟手下滑的位移、遮罩透明度与松手是否关闭（导出纯函数便于单测）
export function sheetDragMath(dy, velocity, height = 480) {
  const h = Math.max(220, Number(height) || 480);
  const offset = dy < 0 ? Math.max(-28, dy * 0.18) : dy;
  const scrim = Math.max(0, Math.min(1, 1 - Math.max(0, dy) / (h * 0.78)));
  const distThreshold = Math.min(110, h * 0.28);
  const dismiss = dy > 0 && velocity > -0.25 && (dy >= distThreshold || (dy >= 24 && velocity >= 0.45));
  return { offset, scrim, dismiss };
}

function _clearDragInline() {
  const sh = $("sheet");
  const sc = $("scrim");
  if (sh) {
    sh.classList.remove("dragging");
    if (sh.style) sh.style.transform = "";
  }
  if (sc) {
    sc.classList.remove("dragging");
    if (sc.style) sc.style.opacity = "";
  }
}

function _bindSheetDrag(sh) {
  if (_dragBound || !sh || !sh.addEventListener) return;
  _dragBound = true;
  const isWide = () => window.matchMedia && window.matchMedia("(min-width: 760px)").matches;
  const stamp = (e) => e.timeStamp || (typeof performance !== "undefined" ? performance.now() : Date.now());
  sh.addEventListener("pointerdown", (e) => {
    if (_drag || !state.sheet || calm() || isWide()) return;
    if (e.button && e.button !== 0) return;
    const t = e.target;
    const fromGrab = !!(t && t.closest && t.closest(".grab"));
    if (!fromGrab) {
      if (sh.scrollTop > 0) return;
      if (t && t.closest && t.closest("button, a, input, textarea, select, label, summary, iframe, .ho-text, .pv-frame")) return;
    }
    const ts = stamp(e);
    _drag = { id: e.pointerId, x0: e.clientX, y0: e.clientY, lastY: e.clientY, lastT: ts, v: 0, fromGrab, locked: false };
  });
  sh.addEventListener("pointermove", (e) => {
    if (!_drag || e.pointerId !== _drag.id) return;
    if (!state.sheet || calm() || isWide()) {
      _drag = null;
      _clearDragInline();
      return;
    }
    const dx = e.clientX - _drag.x0;
    const dy = e.clientY - _drag.y0;
    if (!_drag.locked) {
      if (Math.abs(dx) > Math.abs(dy) && Math.abs(dx) > 6) {
        _drag = null;
        return;
      }
      if (!_drag.fromGrab && (dy < -6 || sh.scrollTop > 0)) {
        _drag = null;
        return;
      }
      if (Math.abs(dy) < 6 || (!_drag.fromGrab && dy <= 0)) return;
      _drag.locked = true;
      sh.setPointerCapture?.(e.pointerId);
      sh.classList.add("dragging");
      $("scrim")?.classList.add("dragging");
    }
    e.preventDefault?.();
    const ts = stamp(e);
    const dt = ts - _drag.lastT;
    if (dt > 0) {
      const instV = (e.clientY - _drag.lastY) / dt;
      _drag.v = _drag.v ? _drag.v * 0.35 + instV * 0.65 : instV;
      _drag.lastY = e.clientY;
      _drag.lastT = ts;
    }
    const { offset, scrim } = sheetDragMath(dy, _drag.v, sh.offsetHeight || 480);
    if (sh.style) sh.style.transform = `translateY(${offset.toFixed(1)}px)`;
    const sc = $("scrim");
    if (sc && sc.style) sc.style.opacity = scrim.toFixed(3);
  });
  const endDrag = (cancel) => (e) => {
    if (!_drag || e.pointerId !== _drag.id) return;
    const d = _drag;
    _drag = null;
    if (!d.locked) return;
    try {
      sh.releasePointerCapture?.(e.pointerId);
    } catch (_) {}
    const dy = e.clientY - d.y0;
    const ts = stamp(e);
    const dt = ts - d.lastT;
    const v = dt > 0 && dt < 80 ? (d.v ? d.v * 0.4 + ((e.clientY - d.lastY) / dt) * 0.6 : (e.clientY - d.lastY) / dt) : d.v;
    if (!cancel && state.sheet && sheetDragMath(dy, v, sh.offsetHeight || 480).dismiss) {
      closeSheet();
      return;
    }
    _clearDragInline();
  };
  sh.addEventListener("pointerup", endDrag(false));
  sh.addEventListener("pointercancel", endDrag(true));
}

const _sheetInert = (on) => {
  for (const id of ["app", "tabbar"]) {
    const el = $(id);
    if (on) el?.setAttribute("inert", ""); else el?.removeAttribute("inert");
  }
};

function _sheetTrap(e) {
  if (e.key !== "Tab" || !state.sheet) return;
  const sh = $("sheet");
  if (!sh || sh.hidden) return;
  const focusables = [...sh.querySelectorAll(
    'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])'
  )].filter((el) => el.offsetParent !== null && !el.matches(":disabled"));
  if (!focusables.length) { e.preventDefault(); sh.focus(); return; }
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  const inside = focusables.includes(document.activeElement);
  if (e.shiftKey) {
    if (!inside || document.activeElement === first) { e.preventDefault(); last.focus(); }
  } else if (!inside || document.activeElement === last) {
    e.preventDefault(); first.focus();
  }
}

let _sheetPrevSel = "";

function _focusSelector(el) {
  if (!el || !el.dataset || !el.dataset.act) return "";
  const q = (v) => (typeof CSS !== "undefined" && CSS.escape ? CSS.escape(v) : String(v).replace(/["\\]/g, "\\$&"));
  let s = `[data-act="${q(el.dataset.act)}"]`;
  for (const k of ["id", "ep", "name", "kind"]) {
    if (el.dataset[k]) s += `[data-${k}="${q(el.dataset[k])}"]`;
  }
  return s;
}

function _restoreSheetFocus() {
  const target = _sheetPrevFocus && document.contains(_sheetPrevFocus)
    ? _sheetPrevFocus
    : _sheetPrevSel && typeof document.querySelector === "function"
      ? document.querySelector(_sheetPrevSel)
      : null;
  if (target && typeof target.focus === "function") target.focus({ preventScroll: true });
  _sheetPrevFocus = null;
  _sheetPrevSel = "";
}

export function openSheet(kind) {
  const was = state.sheet;
  clearTimeout(_closeTimer);
  _drag = null;
  _clearDragInline();
  if (kind !== "preview" || was !== "preview") _sheetBack = kind === "preview" && was === "detail" ? "detail" : null;
  if (!was) {
    _sheetPrevFocus = document.activeElement; // 第一扇抽屉记焦点
    _sheetPrevSel = _focusSelector(_sheetPrevFocus);
  }
  state.sheet = kind;
  const sh = $("sheet");
  if (sh.dataset) sh.dataset.kind = kind || "";
  _bindSheetDrag(sh);
  sh.innerHTML = sheetChrome() + sheetHTML(kind);
  sh.hidden = false;
  $("scrim").hidden = false;
  sh.scrollTop = 0;
  if (!was) {
    _sheetInert(true);
    document.addEventListener("keydown", _sheetTrap, true);
    sfx("open");
    requestAnimationFrame(() => {
      if (!state.sheet || sh.hidden) return;
      sh.classList.add("on");
      $("scrim").classList.add("on");
    });
  }
  sh.setAttribute("tabindex", "-1");
  sh.focus({ preventScroll: true });
}

export function repaintSheet(opts = {}) {
  let openedNow = false;
  if (!opts.preserveSheet && state.page === "settings") {
    if (hasSettingsForm()) {
      if (state.sheet !== "settings-form") {
        openSheet("settings-form");
        openedNow = true;
      }
    } else if (state.sheet === "settings-form") {
      closeSheet({ skipRender: true });
    }
  }
  if (state.page === "settings") {
    const y = window.scrollY;
    renderView();
    renderSide();
    renderRail();
    window.scrollTo(0, y);
  }
  if (!state.sheet || opts.preserveSheet || openedNow) return;
  const sh = $("sheet");
  const top = sh.scrollTop;
  sh.innerHTML = sheetChrome() + sheetHTML(state.sheet);
  sh.scrollTop = top;
}

// 路由/登录身份重置必须无条件解除背景禁用，不走预览的返回栈。
export function resetSheet() {
  clearTimeout(_closeTimer);
  _drag = null;
  _clearDragInline();
  state.sheet = null;
  _sheetBack = null;
  draft.models = null;
  clearSettingsForm();
  document.removeEventListener("keydown", _sheetTrap, true);
  _sheetInert(false);
  for (const id of ["sheet", "scrim"]) {
    const el = $(id);
    if (el) {
      if (el.dataset) el.dataset.kind = "";
      el.classList.remove("on");
      el.hidden = true;
    }
  }
  _restoreSheetFocus();
}

export function closeSheet(opts = {}) {
  if (!state.sheet) { resetSheet(); return; }
  if (state.sheet === "preview" && _sheetBack === "detail" && state.detail && !desktop.matches) {
    openSheet("detail");
    return;
  }
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
  if (state.sheet === "board-item") {
    state.boardItem = null;
    state.boardOpen = {};
    if (state.page === "board") renderView();
  }
  if (state.sheet === "settings-form") {
    clearSettingsForm();
    if (!opts.skipRender && state.page === "settings") renderView();
  }
  state.sheet = null;
  draft.models = null;
  _drag = null;
  _clearDragInline();
  sfx("close");
  const sh = $("sheet");
  if (sh.dataset) sh.dataset.kind = "";
  sh.classList.remove("on");
  $("scrim").classList.remove("on");
  document.removeEventListener("keydown", _sheetTrap, true);
  _sheetInert(false);
  // 焦点还给开抽屉前的那个元素（若视图重绘过则按原按钮标识还原）
  _restoreSheetFocus();
  clearTimeout(_closeTimer);
  const finishClose = () => {
    clearTimeout(_closeTimer);
    sh.removeEventListener?.("transitionend", onEnd);
    if (!state.sheet) {
      sh.hidden = true;
      $("scrim").hidden = true;
    }
  };
  const onEnd = (ev) => {
    if (ev && ev.target && ev.target !== sh) return;
    finishClose();
  };
  sh.addEventListener("transitionend", onEnd);
  _closeTimer = setTimeout(finishClose, 280);
}

export function openDetail(type, id) {
  state.detail = { type, id };
  syncHash();
  if (type === "task") loadTask(id);
  if (desktop.matches) {
    ui.sideFlash = true;
    renderView();
    renderSide();
    $("side").scrollTop = 0;
  } else {
    openSheet("detail");
  }
}
