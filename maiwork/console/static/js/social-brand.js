// 固定的本地品牌资源；未知平台沿用通用图标。
import { ico } from "./util.js";

export function socialLogo(id) {
  const known = ["twitter", "xiaohongshu", "bilibili", "youtube", "reddit"];
  return known.includes(id)
    ? `<img class="social-logo" src="/static/assets/logos/social/${id}-128.png" width="30" height="30" alt="" />`
    : ico("speech");
}
