// MaiWork 网页 · 点击动作（资讯找法相关）：预设搜索服务、优质来源、漏斗展开。
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
        toast(el.dataset.on !== "1" ? "已停用" : skill && skill.enabled === false ? skill.disabled_reason || "已启用，等配套服务开启" : "已启用");
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
        toast(clear ? "已改回免费" : r && r.bound ? `已开启，现在用 ${r.label || id} 搜索` : key ? "密钥已保存" : "已开启");
      } catch (e2) {
        el.disabled = false;
        if (err) {
          err.textContent = e2.message;
          err.hidden = false;
        } else toast(e2.message, true);
      }
      return true;
    }
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
    default:
      return false;
  }
  return true;
}
