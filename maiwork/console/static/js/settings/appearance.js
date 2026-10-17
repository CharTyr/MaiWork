// MaiWork 网页 · 设置 → 外观（只总管理员）：全站默认主题、主题绑定的音效包、音效包安装 / 卸载。
// 音效包是另外装的内容，默认不装；默认主题不能绑定音效包（后端同样拒绝）。
import { esc, ico, tip, toast } from "../util.js";
import { api } from "../api.js";
import { THEMES, themeName } from "../theme_core.js";
import { SOUND_KINDS } from "../sound.js";
import { applyTheme, currentTheme, loadTheme, personalTheme, theme } from "../theme.js";
import { repaintSheet } from "../sheet.js";

export const ap = { packs: null, err: "", busy: "" };

export async function loadAppearance() {
  ap.err = "";
  try {
    const [packs] = await Promise.all([api("GET", "/api/sound-packs"), loadTheme()]);
    ap.packs = (packs && packs.packs) || packs || [];
  } catch (e) {
    ap.err = e.message;
    ap.packs = ap.packs || [];
  }
}

// sounds 可能是种类列表（["click", …]）或 {种类: 文件名}
const kindNames = (sounds) =>
  SOUND_KINDS.filter(([k]) => sounds && (Array.isArray(sounds) ? sounds.includes(k) : sounds[k]))
    .map(([, n]) => n)
    .join("、") || "无";

function packRow(p) {
  const src = p.source && p.source !== "upload" ? `<div class="set-text mono-link">${esc(p.source)}</div>` : `<div class="set-text">网页上传</div>`;
  return `<div class="set-row">${ico("joystick")}<div><div class="set-name">${esc(p.name || p.id)}</div>${src}<div class="set-text">包含：${esc(kindNames(p.sounds))}</div></div>
    <span class="row-btns"><button class="btn small danger" data-act="pack-del" data-id="${esc(p.id)}" data-name="${esc(p.name || p.id)}">卸载</button></span></div>`;
}

function bindRow(t, packs, bound) {
  const cur = (bound[t.id] && bound[t.id].pack) || "";
  return `<div class="set-row">${ico("palette")}<div><label class="set-name" for="bind-${esc(t.id)}">${esc(t.name)}</label><div class="set-text">${cur ? "已绑定音效包" : "无音效"}</div></div>
    <select id="bind-${esc(t.id)}" class="theme-bind" data-theme="${esc(t.id)}" ${packs.length ? "" : "disabled"}>
      <option value="">无音效</option>
      ${packs.map((p) => `<option value="${esc(p.id)}" ${p.id === cur ? "selected" : ""}>${esc(p.name || p.id)}</option>`).join("")}
    </select></div>`;
}

export function appearancePage() {
  const info = theme.info || {};
  const def = info.default || "default";
  const bound = info.sounds || {};
  const packs = ap.packs || [];
  const mine = personalTheme();
  return `
    <div class="set-row">${ico("monitor")}<div><div class="set-name">本浏览器</div><div class="set-text">${mine ? `已选「${esc(themeName(mine))}」` : "跟随默认"}，当前为「${esc(themeName(currentTheme()))}」</div></div>
      <span class="row-btns"><button class="btn small" data-act="appearance">更改</button></span></div>

    <h2 class="h-sub">全站默认主题${tip("未在本浏览器单独选择主题的访客使用此主题。")}</h2>
    <div class="filters" role="group" aria-label="全站默认主题">${THEMES.map(
      (t) => `<button class="filter" data-act="theme-default" data-theme="${esc(t.id)}" aria-pressed="${t.id === def}">${esc(t.name)}</button>`
    ).join("")}</div>

    <h2 class="h-sub">主题音效${tip("默认主题始终无音效。其他主题可绑定一个已安装的音效包；访客可在「外观」中关闭音效。")}</h2>
    ${THEMES.filter((t) => t.id !== "default").map((t) => bindRow(t, packs, bound)).join("")}

    <div class="h-sub-row"><h2 class="h-sub">音效包${tip("zip 内放 hover、click、open、close、success、error 六种声音（ogg / mp3 / wav，单个不超过 1MB），也可用 pack.json 指定名称和文件。仅支持公开的 GitHub 仓库。")}</h2>
      <span class="row-btns"><label class="btn small file-btn">上传 zip<input type="file" id="pack-zip" accept=".zip,application/zip" hidden /></label></span></div>
    ${ap.err ? `<div class="warn-box">${esc(ap.err)}</div>` : ""}
    ${packs.length ? packs.map(packRow).join("") : `<p class="h-meta">暂无音效包</p>`}
    <form id="pack-gh" class="login" autocomplete="off" style="margin-top:18px">
      <label for="pack-url">从 GitHub 安装</label>
      <input id="pack-url" type="url" spellcheck="false" placeholder="https://github.com/用户/仓库/tree/分支/目录" />
      <button class="btn small" type="button" data-act="pack-github" ${ap.busy ? "disabled" : ""}>${ap.busy === "github" ? "正在安装…" : "安装"}</button>
    </form>`;
}

async function refresh() {
  await loadAppearance();
  applyTheme();
  repaintSheet();
}

async function putTheme(body, ok) {
  try {
    theme.info = await api("PUT", "/api/theme", body);
    applyTheme();
    toast(ok);
  } catch (e) {
    toast(e.message, true);
  }
  await refresh();
}

export async function actAppearance(a, el) {
  if (a === "theme-default") {
    await putTheme({ default: el.dataset.theme }, `已将全站默认设为「${themeName(el.dataset.theme)}」`);
    return true;
  }
  if (a === "pack-del") {
    if (!confirm(`确定卸载音效包「${el.dataset.name}」？绑定它的主题将变为无音效。`)) return true;
    el.disabled = true;
    try {
      await api("DELETE", `/api/sound-packs/${encodeURIComponent(el.dataset.id)}`);
      toast("已卸载");
    } catch (e) {
      el.disabled = false;
      toast(e.message, true);
    }
    await refresh();
    return true;
  }
  if (a === "pack-github") {
    const input = document.getElementById("pack-url");
    const url = ((input && input.value) || "").trim();
    if (!/^https:\/\/github\.com\/[^/]+\/[^/]+/.test(url)) {
      toast("地址格式不正确，请填写 https://github.com/用户/仓库", true);
      return true;
    }
    ap.busy = "github";
    repaintSheet();
    try {
      const p = await api("POST", "/api/sound-packs/github", { url });
      toast(`已安装「${(p && p.name) || "音效包"}」`);
    } catch (e) {
      toast(e.message, true);
    }
    ap.busy = "";
    await refresh();
    return true;
  }
  return false;
}

// 绑定下拉框和 zip 上传都是 change 事件，不走 data-act
document.addEventListener("change", async (e) => {
  const t = e.target;
  if (t && t.classList && t.classList.contains("theme-bind")) {
    await putTheme({ sounds: { [t.dataset.theme]: t.value || null } }, t.value ? "已绑定音效包" : "已设为无音效");
    return;
  }
  if (!t || t.id !== "pack-zip") return;
  const file = t.files && t.files[0];
  t.value = "";
  if (!file) return;
  if (file.size > 10 * 1024 * 1024) return toast("压缩包超过 10MB", true);
  toast("正在安装…");
  try {
    const res = await fetch("/api/sound-packs/upload", {
      method: "POST",
      headers: { "Content-Type": "application/zip", "X-Filename": encodeURIComponent(file.name) },
      body: file,
      credentials: "same-origin",
    });
    let data = null;
    try {
      data = await res.json();
    } catch (x) {}
    if (!res.ok) throw new Error((data && data.error) || `请求失败（${res.status}）`);
    toast(`已安装「${(data && data.name) || file.name}」`);
  } catch (err) {
    toast(err.message, true);
  }
  await refresh();
});
