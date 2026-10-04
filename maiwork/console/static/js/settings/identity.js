// MaiWork 网页 · 设置 ·「记忆」页 + 头像小块（头像放在专岗页「主模型」那一栏）。
import { state } from "../state.js";
import { dayWord, esc, toast } from "../util.js";
import { api } from "../api.js";
import { loading } from "../pages/news.js";
import { repaintSheet } from "../sheet.js";
import { avatar, render } from "../render.js";
import { ONB_STEPS, onb, onbPane } from "../onboarding.js";

/* ───── 身份：SOUL / AGENTS / 工作记忆 ───── */
export const AV_SRC = { custom: "你自定义的", qq: "跟 MaiBot 的 QQ 头像同步", platform: "跟 MaiBot 在其他平台的头像同步", default: "默认头像（MaiBot 没有 QQ 账号可同步）" };
export function avatarBlock() {
  const a = state.avatarCfg;
  const src = a ? AV_SRC[a.source] || "" : "";
  return `
    <h2 class="h-sub">头像</h2>
    <div class="av-row">
      <img class="top-avatar av-big" src="${esc((a && a.url) || avatar())}" alt="" onerror="this.onerror=null;this.src='/static/assets/logo.png'" />
      <div class="av-main">
        <div class="set-text">默认和 MaiBot 的头像一样</div>
        ${a ? `<div class="set-text">现在：<b>${esc(src)}</b></div>` : ""}
        <div class="row-btns" style="margin-top:8px">
          <label class="btn small file-btn">上传图片<input type="file" id="avatar-file" accept="image/png,image/jpeg,image/webp,image/gif" hidden /></label>
          <button type="button" class="btn small" data-act="avatar-url">用网址</button>
          ${a && a.source === "custom" ? `<button type="button" class="btn small" data-act="avatar-reset">恢复同步</button>` : ""}
        </div>
        <p class="fine" style="margin:4px 0 0">图片最大 2MB，png / jpg / webp / gif。</p>
      </div>
    </div>`;
}
export async function avatarSaved(a) {
  state.avatarCfg = a;
  if (state.me && state.me.bot && a && a.url) state.me.bot.avatar = a.url;
  render();
  // 引导里的头像步骤：原地换图 + 刷新按钮（不重播整页动画）
  if (onb.open && ONB_STEPS[onb.i].id === "look") {
    const pane = document.querySelector("#onb .onb-pane");
    if (pane) pane.innerHTML = onbPane("look");
    toast("头像换好了");
  }
}

export async function loadIdentity() {
  api("GET", "/api/settings/avatar")
    .then((a) => ((state.avatarCfg = a), ["memory", "agents"].includes(state.setSub) && repaintSheet()))
    .catch(() => (state.avatarCfg = null));
  try {
    state.identity = await api("GET", "/api/identity");
  } catch (e) {
    state.identity = null;
    toast(e.message, true);
  }
}

function idBlock(kind, title, lead, item, limit, extra) {
  item = item || {};
  const text = item.text || "";
  return `
    <form class="id-form login" data-kind="${kind}" autocomplete="off">
      <div class="h-sub-row"><h2 class="h-sub">${title}</h2>${extra || ""}</div>
      <p class="h-meta" style="margin-top:-6px">${lead}</p>
      <textarea class="mono-area id-text" rows="10" spellcheck="false" data-limit="${limit || 16384}">${esc(text)}</textarea>
      <div class="id-foot"><span class="fine id-count">${new Blob([text]).size} / ${limit || 16384} 字节${item.updated_ts ? ` · ${esc(dayWord(item.updated_ts))}改过` : ""}</span><button class="btn primary small" type="submit">保存</button></div>
    </form>`;
}

// 「记忆」页（2026-10：身份页撤掉，SOUL / AGENTS 搬到各专岗；这里只放记忆）
export function memoryPage(s) {
  const d = state.identity;
  if (!d) return loading();
  const lim = d.limits || {};
  return `
    <p class="h-meta">MaiWork 做事时记下的经验。主模型会自己往里记，你也可以直接改。</p>
    ${idBlock("memory", "工作记忆（全局）", "所有群通用的经验，不写具体的群和人", d.memory, lim.memory)}
    <p class="fine">每个群自己的规矩和做法在群页的「这个群」区。</p>`;
}
