// 设置页和全部设置共用的竖向快速跳转；展开由浏览器原生处理。
import { esc } from "../util.js";

export function sectionPicker(items, current, config = false) {
  const selected = items.find(([id]) => id === current) || items[0];
  const action = config ? "cfg-sec" : "set-sub";
  const key = config ? "s" : "sub";
  return `<details class="section-picker"><summary>跳转分栏<span class="section-current">${esc(selected?.[1] || "全部")}</span></summary><nav aria-label="${config ? "配置分类" : "设置分栏"}">${items.map(([id, label]) => `<button type="button" data-act="${action}" data-${key}="${esc(id)}"${selected?.[0] === id ? ' aria-current="page"' : ""}>${esc(label)}</button>`).join("")}</nav></details>`;
}
