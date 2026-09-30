// MaiWork 网页 · 点击动作（资讯找法相关）：预设搜索服务、口味小结、优质来源、新找法开关、漏斗展开。
// 从 actions.js 拆出来（单文件 900 行上限）；act() 先问这里，处理了返回 true。
import { $, state } from "./state.js";
import { reveal, toast } from "./util.js";
import { api } from "./api.js";
import { loadExt } from "./settings/ext.js";
import { openSheet, repaintSheet } from "./sheet.js";
import { renderView } from "./render.js";

export async function actNews(a, el) {
  switch (a) {
    case "skill-toggle": {
      el.disabled = true;
      try {
        await api("POST", `/api/extensions/skills/${encodeURIComponent(el.dataset.name)}/toggle`, { enabled: el.dataset.on === "1" });
        await loadExt();
        repaintSheet();
        const skill = ((state.ext || {}).skills || []).find((k) => k.name === el.dataset.name);
        toast(el.dataset.on !== "1" ? "已停用，模型将不再使用这份 skill" : skill && skill.enabled === false ? skill.disabled_reason || "已设为启用，等待配套服务开启" : "已启用，模型现在可以使用这份 skill");
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
      }
      return true;
    }
    case "preset-open":
      state.presetEdit = el.dataset.id;
      repaintSheet();
      setTimeout(() => $("pk-key") && $("pk-key").focus({ preventScroll: true }), 30);
      return true;
    case "preset-cancel":
      state.presetEdit = null;
      repaintSheet();
      return true;
    case "preset-save":
    case "preset-clear-key": {
      const id = el.dataset.id;
      const clear = el.dataset.act === "preset-clear-key";
      const key = clear ? "" : (($("pk-key") && $("pk-key").value) || "").trim();
      const err = $("pk-err");
      el.disabled = true;
      try {
        const body = clear ? { clear_key: true } : key ? { key } : {};
        const r = await api("POST", `/api/extensions/presets/${encodeURIComponent(id)}`, body);
        state.presetEdit = null;
        await loadExt();
        repaintSheet();
        toast(clear ? "改回免密钥了" : r && r.bound ? `打开了，联网搜索现在用 ${r.label || id}` : key ? "密钥保存好了" : "打开了");
      } catch (e2) {
        el.disabled = false;
        if (err) {
          err.textContent = e2.message;
          err.hidden = false;
        } else toast(e2.message, true);
      }
      return true;
    }
    case "taste-edit":
      state.editing = { kind: "taste", text: ((state.taste || {})[state.g] || {}).text || "" };
      openSheet("edit");
      setTimeout(() => $("ed-text") && $("ed-text").focus(), 350);
      return true;
    case "trusted-toggle": {
      const gid = el.dataset.g;
      el.disabled = true;
      try {
        const r = await api("POST", `/api/groups/${encodeURIComponent(gid)}/trusted-sources`, { domain: el.dataset.domain, removed: el.dataset.on === "1" });
        state.trusted = state.trusted || {};
        state.trusted[gid] = r;
        repaintSheet();
      } catch (e2) {
        el.disabled = false;
        toast(e2.message, true);
      }
      return true;
    }
    case "funnel-toggle":
      state.openFunnel = state.openFunnel === +el.dataset.id ? null : +el.dataset.id;
      renderView();
      reveal(`.funnel .rej-head[data-id="${el.dataset.id}"]`);
      return true;
    case "two-phase": {
      const gid = state.g;
      const on = !(state.twoPhase && state.twoPhase[gid]);
      if (on && !confirm("这个群改用新找法？先只看搜索结果广撒网，再挑最有希望的几条打开核对。下一批资讯开始生效，随时可以关。")) break;
      el.disabled = true;
      try {
        const r = await api("PUT", `/api/groups/${encodeURIComponent(gid)}/feeds-two-phase`, { on });
        state.twoPhase = state.twoPhase || {};
        state.twoPhase[gid] = !!(r && r.on);
        renderView();
        toast(state.twoPhase[gid] ? "这个群改用新找法了，下一批开始生效" : "改回老找法了");
      } catch (e2) {
        el.disabled = false;
        toast(e2.message, true);
      }
      return true;
    }
    default:
      return false;
  }
  return true;
}
