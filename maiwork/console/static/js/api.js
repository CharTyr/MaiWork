// MaiWork 网页 · 接口请求，以及当前群的取值小函数。
import { gadmin, state } from "./state.js";
import { dur, esc, now } from "./util.js";
import { fishSvg } from "./fish.js";

/* ───────────── 接口 ───────────── */

export async function api(method, path, body) {
  const headers = { Accept: "application/json" };
  if (state.ref && !gadmin()) headers["X-MW-Group"] = state.ref;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin" });
  } catch (e) {
    throw new Error("连不上，请检查网络");
  }
  let data = null;
  try {
    data = await res.json();
  } catch (e) {
    /* 空响应 */
  }
  if (!res.ok) {
    const err = new Error((data && data.error) || `出错了（${res.status}）`);
    err.status = res.status;
    throw err;
  }
  return data;
}

export const grp = () => state.groups.find((x) => x.id === state.g) || null;
export const gview = () => (state.view && state.view.id === state.g ? state.view : null);
// 专岗小鱼：管理员刚「换一条」时以设置页拿到的为准；群友从群视图的 agent_fish 拿；都没有就按岗位名固定一条
export function agentSeed(kind) {
  const p = ((state.agents && state.agents.profiles) || []).find((x) => x.kind === kind);
  if (p) return p.fish_seed || "";
  const v = gview();
  const m = v && v.agent_fish;
  return (m && typeof m[kind] === "string" && m[kind]) || "";
}
export const agentFish = (kind, size, opts = {}) => fishSvg(`agent:${kind}:${agentSeed(kind)}`, { size, ...opts });
export const groupRef = (g) => (gadmin() ? g.id : state.ref);
export const gname = (g) => (g && (g.name || `群 ${g.id}`)) || "";
// 群所在的聊天平台（后端给 platform："qq" / "telegram" / "qqbot"；老数据没有就当 qq）
export const gplat = (g, v) => String((g && g.platform) || (v && v.platform) || "qq");
const PLAT_TAGS = { telegram: `<span class="ptag tg">Telegram</span>`, qqbot: `<span class="ptag qb">QQ 官方</span>` };
export const platTag = (g, v) => PLAT_TAGS[gplat(g, v)] || "";
// 太长的名字：平时省略号，鼠标放上去 / 手指按住时滚动显示全名（滚动距离运行时量）
export const mq = (text) => `<span class="mq" title="${esc(text)}"><span class="mq-in">${esc(text)}</span></span>`;

export function quiet(g) {
  if (!g) return { text: "", usual: "", live: false };
  const q = g.quiet || {};
  if (g.fresh) return { text: "正在了解这个群", usual: g.read_since ? `已读 ${dur(now() - g.read_since)}的聊天记录` : "刚开始读聊天记录", live: false };
  if (!q.last_msg_ts) return { text: "还没收到消息", usual: "", live: false };
  const gap = now() - q.last_msg_ts;
  const usual = q.usual_gap_s ? `平时这个点 ${dur(q.usual_gap_s)}一条` : "";
  if (gap < 300) return { text: "有人在聊", usual, live: true };
  return { text: `安静 ${dur(gap)}`, usual, live: true };
}
