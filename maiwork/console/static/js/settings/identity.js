// MaiWork 网页 · 设置 ·「身份」：头像、SOUL / AGENTS / 工作记忆。
import { state } from "../state.js";
import { dayWord, esc, toast } from "../util.js";
import { api } from "../api.js";
import { loading } from "../pages/news.js";
import { repaintSheet } from "../sheet.js";
import { avatar, render } from "../render.js";
import { ONB_STEPS, onb, onbPane } from "../onboarding.js";

/* ───── 身份：SOUL / AGENTS / 工作记忆 ───── */
export const AV_SRC = { custom: "你自定义的", qq: "跟 MaiBot 的 QQ 头像同步", platform: "跟 MaiBot 在其他平台的头像同步", default: "默认头像（MaiBot 没有 QQ 账号可同步）" };
function avatarBlock() {
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
    .then((a) => ((state.avatarCfg = a), state.setSub === "identity" && repaintSheet()))
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

export function identityPage(s) {
  const d = state.identity;
  if (!d) return loading();
  const lim = d.limits || {};
  const gm = d.group_memory || {};
  const groups = s.groups || [];
  return `
    
    ${avatarBlock()}
    ${idBlock("soul", "SOUL.md", "说话的口吻和性格" + (d.soul && d.soul.synced_from_maibot ? " · 已和 MaiBot 同步" : ""), d.soul, lim.soul, `<button type="button" class="btn small" data-act="soul-sync">从 MaiBot 同步</button>`)}
    ${idBlock("agents", "AGENTS.md", "做事的规矩和偏好", d.agents, lim.agents)}
    ${idBlock("memory", "工作记忆（全局）", "所有群通用的经验，不写具体的群和人", d.memory, lim.memory)}
    <h2 class="h-sub">每个群的工作记忆</h2>
    <p class="h-meta" style="margin-top:-6px">每个群各自的经验，只在那个群里用</p>
    ${
      groups.length
        ? groups
            .map((g) => idBlock(`group:${g.id}`, esc(g.name || `群 ${g.id}`), "", gm[g.id], lim.group_memory || 16384))
            .join("")
        : `<p class="h-meta">还没有服务群。</p>`
    }`;
}
