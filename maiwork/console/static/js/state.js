// MaiWork 网页 · 全局常量和共享状态（state / ui），以及身份判断。

export const $ = (id) => document.getElementById(id);
export const desktop = window.matchMedia("(min-width: 1180px)");

export const TABS = [
  { id: "news", label: "资讯" },
  { id: "ideas", label: "构想" },
  { id: "goals", label: "目标" },
  { id: "tasks", label: "任务" },
  { id: "group", label: "群" },
];
export const STATUS = {
  pending_approval: "等批准",
  queued: "排队中",
  running: "进行中",
  waiting_input: "等人回复",
  shelved: "搁置",
  reviewing: "验收中",
  completed: "已完成",
  failed: "失败",
  paused: "暂停",
  cancelled: "已取消",
  rejected: "被拒",
};
export const DOT = { waiting_input: "waiting", shelved: "", completed: "done", paused: "", cancelled: "", rejected: "failed" };
const ACTIVE = ["running", "reviewing", "queued"];
export const FILTERS = [
  { id: "all", label: "全部", match: () => true },
  { id: "active", label: "进行中", match: (t) => ACTIVE.includes(t.status) },
  { id: "waiting", label: "等人回复", match: (t) => ["waiting_input", "shelved", "paused"].includes(t.status) },
  { id: "done", label: "已完成", match: (t) => t.status === "completed" },
  { id: "failed", label: "没做成", match: (t) => ["failed", "cancelled", "rejected"].includes(t.status) },
];
export const CATS = [
  ["recent", "最近在聊"],
  ["interest", "长期兴趣"],
  ["ongoing", "在做的事"],
  ["convention", "约定和说法"],
  ["resource", "常用资源"],
];
export const TONES = ["#FFD8A8", "#D0BFFF", "#A5D8FF", "#B2F2BB", "#FFC9C9", "#FFEC99", "#C3FAE8", "#EEBEFA"];

// 切换页面时才播放进场动画（多个模块会改它，所以放在对象里）
export const ui = { flash: true };

export const state = {
  ga: {}, // 群号 → {password_set, accounts}（设置 → 群链接 里的群管理员）
  me: null, // {role, group, bot, now}
  groups: [],
  view: null, // 当前群的 GroupView
  g: null, // 当前群号
  ref: "", // 网址里的群引用（群友 = 链接码）
  tab: "news",
  filter: "all",
  detail: null, // {type: "task"|"goal", id}
  sheet: null,
  settings: null,
  update: null, // /api/update 的结果（更新提醒，只有总管理员有）
  tasks: {}, // 任务详情缓存
  badLink: false,
  loginError: "",
  skew: 0, // 服务器时间 - 本机时间
  page: null, // "settings" = 设置页；"chat" = 和 MaiWork 聊（都不属于任何群）
  chats: [],
  chatId: null,
  chat: null,
  chatOpen: {},
  setSub: "overview",
  ext: null, // /api/extensions
  extEdit: null, // {kind: "mcp"|"skill", name: null|string, data}
};
export const admin = () => state.me && state.me.role === "admin";
// 群管理员：能管自己那一个群（批准、画像、资讯偏好…），看不到全局设置；个人画像只读
export const isGA = () => !!(state.me && state.me.role === "group_admin");
export const gadmin = () => admin() || isGA();
