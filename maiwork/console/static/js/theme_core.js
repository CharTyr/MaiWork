// MaiWork 网页 · 主题的纯数据部分（不碰 DOM，可在 node 里测）。
// 主题只换外观；清单写死，和后端 sound_packs.THEMES 一致。

export const THEMES = [
  { id: "default", name: "默认" },
  { id: "retro", name: "复古终端" },
];
export const THEME_KEY = "mw-theme"; // 个人覆盖："" = 跟随默认
export const DEFAULT_KEY = "mw-theme-default"; // 上次拿到的全站默认（首屏防闪用）
export const SOUND_KEY = "mw-sound"; // "on" | "off"；没设过按「减少动态效果」决定
export const KINDS = ["hover", "click", "open", "close", "success", "error"];

const known = (id) => THEMES.some((t) => t.id === id);

// 个人覆盖优先，其次全站默认，都不认识就回默认主题
export function resolveTheme(personal, serverDefault) {
  if (personal && known(personal)) return personal;
  if (serverDefault && known(serverDefault)) return serverDefault;
  return "default";
}

export const themeName = (id) => (THEMES.find((t) => t.id === id) || THEMES[0]).name;
