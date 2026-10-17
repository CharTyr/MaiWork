// 设置页和全部设置共用的竖向快速跳转；展开由浏览器原生处理。
import { esc } from "../util.js";

export function sectionPicker(items, current, config = false) {
  const selected = items.find(([id]) => id === current) || items[0];
  const action = config ? "cfg-sec" : "set-sub";
  const key = config ? "s" : "sub";
  return `<details class="section-picker"><summary aria-label="${config ? "全部配置分类" : "全部设置分栏"}" title="${config ? "全部配置分类" : "全部设置分栏"}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01"/></svg></summary><nav aria-label="${config ? "配置分类" : "设置分栏"}">${items.map(([id, label]) => `<button type="button" data-act="${action}" data-${key}="${esc(id)}"${selected?.[0] === id ? ' aria-current="page"' : ""}>${esc(label)}</button>`).join("")}</nav></details>`;
}
