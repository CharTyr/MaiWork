// 引导中的可选社交平台：只启停本步修改的选项，安装由后端异步执行。
import { esc, ico, tip } from "./util.js";
import { api } from "./api.js";
import { socialLogo } from "./social-brand.js";

let platforms = null;
let error = "";

export async function loadOnbSocial() {
  try {
    const data = await api("GET", "/api/social/platforms");
    if (!Array.isArray(data?.platforms)) throw new Error("平台列表格式有误");
    platforms = data.platforms;
    error = "";
  } catch (e) {
    platforms = null;
    error = e.message;
  }
}

export function onbSocialPane() {
  const rows = (platforms || []).map((p) => `<label class="onb-social-option"><input type="checkbox" name="onb-social-platform" value="${esc(p.id)}" ${p.enabled ? "checked" : ""} />${socialLogo(p.id)}<span><b>${esc(p.name)}</b>${p.needs_cookie ? `<span class="set-text">需要登录态</span>` : ""}</span></label>`).join("");
  return `<div class="onb-step-ico">${ico("speech")}</div><h1 class="onb-title">社交平台（可选）${tip("勾选后自动安装组件。需要登录的平台，请在完成引导后前往「设置 → 社交平台」填写登录态，填写框中附有 Cookie 获取步骤。安装失败可在「社交平台」中重试。")}</h1><div class="onb-social-list">${error ? `<p class="err" role="alert">${esc(error)}</p><button class="btn" data-act="onb-social-reload">重试</button>` : rows || `<p class="fine">暂无平台，可跳过此步</p>`}</div>`;
}

export async function saveOnbSocial() {
  if (!platforms) return;
  for (const input of document.querySelectorAll('[name="onb-social-platform"]')) {
    const p = platforms.find((item) => item.id === input.value);
    if (!p || p.enabled === input.checked) continue;
    const result = await api("POST", `/api/social/platforms/${encodeURIComponent(p.id)}/${input.checked ? "enable" : "disable"}`);
    if (result.ok === false) throw new Error(result.error || `${p.name}：保存失败`);
    // 部分成功后重试不重复提交已经成功的变更。
    p.enabled = input.checked;
  }
}
