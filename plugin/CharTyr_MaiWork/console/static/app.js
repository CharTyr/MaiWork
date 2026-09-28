/* MaiWork 网页 · 纯前端，无依赖。数据全部来自 /api（接口约定见 docs/07-代码接口.md §9）。
 * 两种身份：管理员（cookie）看全部群；群友（网址里的群链接码，随请求头 X-MW-Group 发出）只看本群、只读。 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const desktop = window.matchMedia("(min-width: 1180px)");

  const TABS = [
    { id: "news", label: "资讯" },
    { id: "ideas", label: "构想" },
    { id: "goals", label: "目标" },
    { id: "tasks", label: "任务" },
    { id: "group", label: "群" },
  ];
  const STATUS = {
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
  const DOT = { waiting_input: "waiting", shelved: "", completed: "done", paused: "", cancelled: "", rejected: "failed" };
  const ACTIVE = ["running", "reviewing", "queued"];
  const FILTERS = [
    { id: "all", label: "全部", match: () => true },
    { id: "active", label: "进行中", match: (t) => ACTIVE.includes(t.status) },
    { id: "waiting", label: "等人回复", match: (t) => ["waiting_input", "shelved", "paused"].includes(t.status) },
    { id: "done", label: "已完成", match: (t) => t.status === "completed" },
    { id: "failed", label: "没做成", match: (t) => ["failed", "cancelled", "rejected"].includes(t.status) },
  ];
  const CATS = [
    ["recent", "最近在聊"],
    ["interest", "长期兴趣"],
    ["ongoing", "在做的事"],
    ["convention", "约定和说法"],
    ["resource", "常用资源"],
  ];
  const TONES = ["#FFD8A8", "#D0BFFF", "#A5D8FF", "#B2F2BB", "#FFC9C9", "#FFEC99", "#C3FAE8", "#EEBEFA"];

  const state = {
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
  const admin = () => state.me && state.me.role === "admin";

  /* ───────────── 小工具 ───────────── */

  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const safeUrl = (u) => (/^https?:\/\//i.test(String(u || "")) ? esc(u) : "#");
  // 正文里的 [文字](https://…) 渲染成链接；其余一律转义（内容来自网上和模型，不能信）
  const richText = (t) =>
    esc(t)
      .replace(/\[([^\]\n]{1,80})\]\((https?:\/\/[^\s)]+)\)/g, (_, txt, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${txt}</a>`)
      .replace(/`([^`\n]{1,120})`/g, (_, code) => `<code class="icode">${code}</code>`);

  const P = 'fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"';
  const SVG = {
    news: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><rect x="5" y="3.5" width="14" height="17" rx="3.2"/><path d="M9 8.5h6M9 12h6M9 15.5h3.5"/></svg>`,
    ideas: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><path d="M9.2 18h5.6M10.2 21h3.6"/><path d="M12 3a6 6 0 0 0-3.7 10.7c.6.5 1 1.2 1 2V16h5.4v-.3c0-.8.4-1.5 1-2A6 6 0 0 0 12 3z"/></svg>`,
    goals: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><rect x="3.5" y="3.5" width="17" height="17" rx="4.5"/><path d="M8 12.3l2.8 2.8 5.3-5.6"/></svg>`,
    tasks: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><rect x="3.5" y="7" width="17" height="12.5" rx="3.2"/><path d="M9 7V5.6A1.6 1.6 0 0 1 10.6 4h2.8A1.6 1.6 0 0 1 15 5.6V7M3.5 12.3h17"/></svg>`,
    group: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><circle cx="7.4" cy="7.4" r="3.3"/><path d="M16.6 4.2l3.4 5.8h-6.8z"/><rect x="4.1" y="13.3" width="6.6" height="6.6" rx="1.8"/><circle cx="16.6" cy="16.6" r="3.3"/></svg>`,
    sliders: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><path d="M3.5 8h9M17.5 8h3M3.5 16h3M11.5 16h9"/><circle cx="15" cy="8" r="2.5"/><circle cx="9" cy="16" r="2.5"/></svg>`,
    down: `<svg viewBox="0 0 24 24" ${P} stroke-width="2.4"><path d="M6 9l6 6 6-6"/></svg>`,
    left: `<svg viewBox="0 0 24 24" ${P} stroke-width="2.2"><path d="M15 6l-6 6 6 6"/></svg>`,
    right: `<svg viewBox="0 0 24 24" ${P} stroke-width="2.2"><path d="M9 6l6 6-6 6"/></svg>`,
    close: `<svg viewBox="0 0 24 24" ${P} stroke-width="2.4"><path d="M6 6l12 12M18 6L6 18"/></svg>`,
    check: `<svg viewBox="0 0 24 24" ${P} stroke-width="3"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>`,
    copy: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><rect x="8" y="8" width="12" height="12" rx="2.5"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></svg>`,
    more: `<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="5.5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="18.5" cy="12" r="1.8"/></svg>`,
    up: `<svg viewBox="0 0 24 24" ${P} stroke-width="1.9"><path d="M7 11v9H4.5v-9zM7 11l3.8-7c1.3 0 2.2.9 2.2 2.2V9.5h5a2 2 0 0 1 2 2.3l-1.1 6.4a2.2 2.2 0 0 1-2.2 1.8H7"/></svg>`,
    dn: `<svg viewBox="0 0 24 24" ${P} stroke-width="1.9" style="transform:rotate(180deg)"><path d="M7 11v9H4.5v-9zM7 11l3.8-7c1.3 0 2.2.9 2.2 2.2V9.5h5a2 2 0 0 1 2 2.3l-1.1 6.4a2.2 2.2 0 0 1-2.2 1.8H7"/></svg>`,
    lock: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><rect x="5" y="11" width="14" height="9" rx="2.5"/><path d="M8.5 11V8a3.5 3.5 0 0 1 7 0v3"/></svg>`,
    key: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><circle cx="8" cy="15" r="4"/><path d="M11 12l8-8M16 7l2.5 2.5M14 9l2 2"/></svg>`,
    trash: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><path d="M4.5 7h15M10 7V5h4v2M6.8 7l.9 12.2h8.6l.9-12.2"/></svg>`,
    pen: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><path d="M14.5 5.5l4 4M4.5 19.5l1-4.5L16 4.5a1.4 1.4 0 0 1 2 0l1.5 1.5a1.4 1.4 0 0 1 0 2L9 18.5z"/></svg>`,
    plus: `<svg viewBox="0 0 24 24" ${P} stroke-width="2.2"><path d="M12 5v14M5 12h14"/></svg>`,
    send: `<svg viewBox="0 0 24 24" ${P} stroke-width="2.2"><path d="M12 19V5M5.5 11.5 12 5l6.5 6.5"/></svg>`,
    chat: `<svg viewBox="0 0 24 24" ${P} stroke-width="1.9"><path d="M20 11.5a7.5 7.5 0 0 1-10.9 6.7L4.5 19.5l1.3-4A7.5 7.5 0 1 1 20 11.5z"/></svg>`,
    ext: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><path d="M14 4h6v6M20 4l-9 9M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10"/></svg>`,
  };

  const ICONS = new Set("robot newspaper bulb monitor chart alarm moon speech tools package books filebox testtube palette seedling hourglass memo camera rocket joystick sparkles magnifier floppy link bell calendar dumpling bullseye flag pushpin lotus sleeping herb gear lock cloud teacup mailbox".split(" "));
  // 群头像：有 avatar 就盖一张真图在 emoji 上，图挂了就删掉露出 emoji
  const gface = (g) => `<span class="gav">${ico(g.icon)}${g.avatar ? `<img class="gav-img" src="${esc(g.avatar)}" alt="" loading="lazy" onerror="this.remove()" />` : ""}</span>`;
  const ico = (name, cls = "ico") => `<img class="${cls}" src="/static/assets/icons/${ICONS.has(name) ? name : "sparkles"}.png" alt="" loading="lazy" />`;
  const enterAttr = (i) => `class="enter" style="--i:${i}"`;
  let flash = true; // 切换页面时才播放进场动画

  const calm = () => window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  // 刚展开的那一块淡入；只加在这一次点开的元素上，定时刷新重画时不会再播
  function reveal(headSel) {
    const h = document.querySelector(headSel);
    const n = h && h.nextElementSibling;
    if (n && h.getAttribute("aria-expanded") === "true") n.classList.add("reveal");
  }
  function toast(text, bad) {
    const t = $("toast");
    const was = t.classList.contains("on");
    t.textContent = text;
    t.classList.toggle("bad", !!bad);
    t.classList.add("on");
    // 上一条还没消失又来一条：轻轻弹一下，让人看出换了
    if (was && t.animate)
      t.animate(
        calm() ? [{ opacity: 0.55 }, { opacity: 1 }] : [{ transform: "translate(-50%, 0) scale(0.96)" }, { transform: "translate(-50%, 0) scale(1)" }],
        { duration: 160, easing: "cubic-bezier(0.2, 0.8, 0.2, 1)" }
      );
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => t.classList.remove("on"), 2400);
  }

  /* ───────────── 时间（一律按北京时间显示） ───────────── */

  const now = () => Date.now() / 1000 + state.skew;
  const BJ = 8 * 3600;
  const bjDate = (ts) => new Date((ts + BJ) * 1000); // 用 getUTC* 读出北京时间
  const pad = (n) => String(n).padStart(2, "0");
  const hhmm = (ts) => {
    const d = bjDate(ts);
    return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
  };
  const dayIndex = (ts) => Math.floor((ts + BJ) / 86400);
  const WEEK = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
  function dayWord(ts) {
    const diff = dayIndex(ts) - dayIndex(now());
    if (diff === 0) return "今天";
    if (diff === -1) return "昨天";
    if (diff === 1) return "明天";
    if (diff === 2) return "后天";
    if (diff > -7 && diff < 0) return WEEK[bjDate(ts).getUTCDay()];
    const d = bjDate(ts);
    return `${d.getUTCMonth() + 1} 月 ${d.getUTCDate()} 日`;
  }
  const when = (ts) => (ts ? (dayIndex(ts) === dayIndex(now()) ? hhmm(ts) : `${dayWord(ts)} ${hhmm(ts)}`) : "");
  function dur(s) {
    s = Math.max(0, Math.round(s || 0));
    if (s < 60) return "不到 1 分钟";
    const m = Math.round(s / 60);
    if (m < 60) return `${m} 分钟`;
    const h = Math.floor(m / 60);
    if (h < 24) return m % 60 && h < 6 ? `${h} 小时 ${m % 60} 分钟` : `${h} 小时`;
    return `${Math.floor(h / 24)} 天`;
  }
  function slotName(ts) {
    const h = bjDate(ts).getUTCHours();
    const part = h < 6 ? "凌晨" : h < 11 ? "早上" : h < 13 ? "中午" : h < 17 ? "午后" : h < 20 ? "傍晚" : "晚上";
    const dw = dayWord(ts);
    return `${dw === "今天" ? "今天" : dw}${part}`;
  }
  const msText = (ms) => (ms >= 1000 ? (ms / 1000).toFixed(1) + " 秒" : Math.round(ms) + " 毫秒");
  function tokens(n) {
    n = n || 0;
    if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e5 ? 0 : 1) + "k";
    return String(n);
  }

  /* ───────────── 接口 ───────────── */

  async function api(method, path, body) {
    const headers = { Accept: "application/json" };
    if (state.ref && !admin()) headers["X-MW-Group"] = state.ref;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    let res;
    try {
      res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin" });
    } catch (e) {
      throw new Error("连不上 MaiWork，检查一下网络");
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

  const grp = () => state.groups.find((x) => x.id === state.g) || null;
  const gview = () => (state.view && state.view.id === state.g ? state.view : null);
  const groupRef = (g) => (admin() ? g.id : state.ref);
  const gname = (g) => (g && (g.name || `群 ${g.id}`)) || "";
  // 太长的名字：平时省略号，鼠标放上去 / 手指按住时滚动显示全名（滚动距离运行时量）
  const mq = (text) => `<span class="mq" title="${esc(text)}"><span class="mq-in">${esc(text)}</span></span>`;

  function quiet(g) {
    if (!g) return { text: "", usual: "", live: false };
    const q = g.quiet || {};
    if (g.fresh) return { text: "还在熟悉这个群", usual: g.read_since ? `已读 ${dur(now() - g.read_since)}的聊天记录` : "刚开始读聊天记录", live: false };
    if (!q.last_msg_ts) return { text: "还没收到消息", usual: "", live: false };
    const gap = now() - q.last_msg_ts;
    const usual = q.usual_gap_s ? `平时这个点 ${dur(q.usual_gap_s)}一条` : "";
    if (gap < 300) return { text: "有人在聊", usual, live: true };
    return { text: `安静 ${dur(gap)}`, usual, live: true };
  }

  /* ───────────── 群脉搏：最近 24 小时的发言量 ───────────── */

  function smoothPath(pts) {
    let d = `M${pts[0][0]},${pts[0][1]}`;
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[i - 1] || pts[i];
      const p1 = pts[i];
      const p2 = pts[i + 1];
      const p3 = pts[i + 2] || p2;
      const c1 = [p1[0] + (p2[0] - p0[0]) / 6, Math.min(110, p1[1] + (p2[1] - p0[1]) / 6)];
      const c2 = [p2[0] - (p3[0] - p1[0]) / 6, Math.min(110, p2[1] - (p3[1] - p1[1]) / 6)];
      d += ` C${c1[0].toFixed(1)},${c1[1].toFixed(1)} ${c2[0].toFixed(1)},${c2[1].toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
    }
    return d;
  }

  function sleepBands(start, end, spec) {
    const m = /^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$/.exec(spec || "");
    if (!m) return [];
    const a = +m[1] * 60 + +m[2];
    const b = +m[3] * 60 + +m[4];
    const out = [];
    for (let day = dayIndex(start) - 1; day <= dayIndex(end); day++) {
      const base = day * 86400 - BJ;
      const s = base + a * 60;
      const e = base + (b <= a ? b + 1440 : b) * 60;
      const l = Math.max(s, start);
      const r = Math.min(e, end);
      if (r > l) out.push([l, r]);
    }
    return out;
  }

  function pulseCard(g, v, compact) {
    const p = (v && v.pulse) || null;
    const q = quiet(g);
    const head = `
      <div class="pulse-head">
        <div class="pulse-title">群脉搏 <span class="pulse-span">· 最近 24 小时</span></div>
        <div class="pulse-now"><b>${esc(q.text)}</b>${q.usual ? `<br />${esc(q.usual)}` : ""}</div>
      </div>`;
    if (!p || !p.bins || !p.bins.length) {
      return `<section class="pulse-card${compact ? " compact" : ""}" aria-label="群脉搏">${head}<div class="pulse pulse-empty">正在读聊天记录…</div></section>`;
    }
    const bins = p.bins;
    const step = p.step || 900;
    const end = p.end;
    const start = end - bins.length * step;
    const pct = (ts) => Math.max(0, Math.min(100, ((ts - start) / (end - start)) * 100));
    const max = Math.max(1, ...bins);
    const H = 110;
    const pts = bins.map((c, i) => [(i / (bins.length - 1)) * 1000, H - (c / max) * H * 0.9 - 2]);
    const line = smoothPath(pts);
    const area = `${line} L1000,${H} L0,${H} Z`;
    const total = bins.reduce((a, b) => a + b, 0);

    const bands = [
      ...sleepBands(start, end, p.sleep).map(([l, r], k) => {
        const L = pct(l);
        const W = pct(r) - L;
        return `<div class="band sleep" style="left:${L}%;width:${W}%">${k === 0 && W > 12 ? `<span class="band-label">${ico("moon", "")}睡觉时段</span>` : ""}</div>`;
      }),
      ...(p.spells || []).map((s) => {
        const L = pct(s.from);
        return `<div class="band cold" style="left:${L}%;width:${Math.max(0.6, pct(s.to) - L)}%" title="${esc(s.note || "冷场")}"></div>`;
      }),
    ].join("");
    const marks = (p.topics || [])
      .filter((t) => t.at >= start)
      .map((t) => {
        const L = pct(t.at);
        return `<div class="mark${L < 30 ? " left" : ""}" style="left:${L}%"><span class="mark-label">开话题${t.replies ? ` · ${t.replies} 人接` : ""}</span></div>`;
      })
      .join("");
    const tickHours = compact ? [0, 8, 16] : [0, 4, 8, 12, 16, 20];
    const ticks = [];
    for (let day = dayIndex(start); day <= dayIndex(end); day++) {
      for (const h of tickHours) {
        const ts = day * 86400 - BJ + h * 3600;
        const L = pct(ts);
        if (ts > start && L < (compact ? 84 : 90) && L > 4) ticks.push(`<span style="left:${L}%">${pad(h)}:00</span>`);
      }
    }
    return `
      <section class="pulse-card${compact ? " compact" : ""}" aria-label="群脉搏">
        ${head}
        <div class="pulse" style="${compact ? "height:104px" : ""}">
          ${bands}
          <svg viewBox="0 0 1000 ${H}" preserveAspectRatio="none" aria-hidden="true">
            <path class="area" d="${area}" />
            <path class="line" d="${line}" pathLength="1" />
          </svg>
          ${marks}
          ${total ? "" : `<div class="pulse-none">这 24 小时没人说话</div>`}
          <div class="axis">${ticks.join("")}<span style="left:100%">现在</span></div>
        </div>
        ${
          compact
            ? ""
            : `<div class="legend">
                <span><i class="l-sleep"></i>睡觉时段，不开话题</span>
                <span><i class="l-cold"></i>冷场</span>
                <span><i class="l-topic"></i>MaiWork 开了话题</span>
              </div>`
        }
      </section>`;
  }

  /* ───────────── 各页面 ───────────── */

  function emptyState(iconName, title, text) {
    return `<div class="empty enter">${ico(iconName)}<b>${esc(title)}</b><span>${esc(text)}</span></div>`;
  }
  const loading = () => `<div class="loading"><span></span><span></span><span></span></div>`;

  const FB_KEY = "mw-fb";
  const myFb = () => {
    try {
      return JSON.parse(localStorage.getItem(FB_KEY) || "{}");
    } catch (e) {
      return {};
    }
  };
  function fbButtons(kind, it) {
    const mine = myFb()[`${kind}:${it.id}`];
    const f = it.feedback || {};
    return `<span class="fb">
      <button data-act="fb" data-kind="${kind}" data-id="${it.id}" data-v="up" aria-pressed="${mine === "up"}" aria-label="有用">${SVG.up}${f.up ? `<i>${f.up}</i>` : ""}</button>
      <button data-act="fb" data-kind="${kind}" data-id="${it.id}" data-v="down" aria-pressed="${mine === "down"}" aria-label="没用">${SVG.dn}${f.down ? `<i>${f.down}</i>` : ""}</button>
    </span>`;
  }

  function newsStatus(s) {
    s = s || {};
    const k = s.kind || "new";
    if (k === "used") return `${when(s.at)} 冷场时拿来开了话题${s.replies ? ` · ${s.replies} 人接话` : " · 没人接"}`;
    if (k === "mentioned") return `${when(s.at)} MaiBot 在聊天里提过`;
    if (k === "pool") return s.expires_ts ? `在话题候选里 · 还能放 ${dur(s.expires_ts - now())}` : "在话题候选里";
    if (k === "expired") return "没找到合适的时机，已过期";
    return "刚备好";
  }

  const SCORE_NAMES = [["info", "信息量"], ["source", "来源"], ["relevance", "相关"], ["timeliness", "时效"], ["chat", "可聊"]];

  function verifyBlock(v) {
    if (!v || !v.status) return "";
    const ok = v.status === "passed";
    const steps = (v.steps || []).slice(0, 4);
    return `
      <div class="verify ${ok ? "ok" : "bad"}">
        <div class="verify-head">${ico("testtube", "")}${ok ? "我在一次性机器上试了一下，能跑" : "我试了一下，没跑通"}${v.minutes ? `<span> · 花了 ${v.minutes} 分钟</span>` : ""}</div>
        ${v.summary ? `<div class="verify-text">${esc(v.summary)}</div>` : ""}
        ${steps.length ? `<ol class="verify-steps">${steps.map((x) => `<li>${esc(x)}</li>`).join("")}</ol>` : ""}
      </div>`;
  }

  function reasonBlock(it) {
    const reason = it.reason || it.why;
    const refs = (it.refs || []).slice(0, 3);
    const aud = it.audience || [];
    if (!reason && !refs.length && !aud.length) return "";
    return `
      <div class="reason">
        <div class="reason-h">我发这条的原因</div>
        ${reason ? `<p class="reason-t">${esc(reason)}</p>` : ""}
        ${
          refs.length
            ? `<div class="refs">${refs
                .map((r) => `<div class="ref-row"><span class="ref-when">${esc(when(r.ts))}</span><span class="ref-who">${esc(r.who)}</span><span class="ref-text">「${esc(r.text)}」</span></div>`)
                .join("")}</div>`
            : ""
        }
        ${aud.length ? `<div class="aud">可能用得上：${aud.map((n) => `<b>${esc(n)}</b>`).join("、")}</div>` : ""}
        ${it.profile_ref ? `<div class="aud">对上了群画像里的「${esc(it.profile_ref)}」</div>` : ""}
      </div>`;
  }

  const VOTE_KEY = "mw-chatvote";
  const myVotes = () => {
    try {
      return JSON.parse(localStorage.getItem(VOTE_KEY) || "{}");
    } catch (e) {
      return {};
    }
  };

  function newsItem(it, i, guide) {
    const sc = it.scores || null;
    const voted = !!myVotes()[it.id];
    const tags = [
      it.topic ? `<span class="ntag">${esc(it.topic)}</span>` : "",
      it.angle === "diverse" ? `<span class="ntag alt">换个角度</span>` : "",
      it.sensitive ? `<span class="ntag warn">争议话题</span>` : "",
      it.verify && it.verify.status === "passed" ? `<span class="ntag ok">实测过</span>` : "",
      admin() && sc && Number(sc.avg) > 0 ? `<span class="ntag score" title="${SCORE_NAMES.map(([k, n]) => `${n} ${sc[k] ?? "-"}`).join(" · ")}">${Number(sc.avg).toFixed(1)} 分</span>` : "",
    ].join("");
    const img = it.image_url && /^https?:\/\//i.test(it.image_url) ? `<img class="n-img" src="${esc(it.image_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()" />` : "";
    return `
      <article class="item news-item enter" style="--i:${i}">
        ${ico(it.icon || (guide ? "books" : "newspaper"))}
        <div>
          ${tags ? `<div class="ntags">${tags}</div>` : ""}
          <h2 class="item-title">${esc(it.title)}</h2>
          <p class="item-body">${richText(it.body || it.summary)}</p>
          ${img}
          ${verifyBlock(it.verify)}
          ${reasonBlock(it)}
          ${
            (it.sources || []).length
              ? `<div class="sources">${it.sources
                  .map((s) => `<a class="src" href="${safeUrl(s.url)}" target="_blank" rel="noopener noreferrer"><div class="src-site">${esc(s.site || "")}</div><div class="src-title">${esc(s.title || s.url)}</div></a>`)
                  .join("")}</div>`
              : ""
          }
          <div class="status">
            <span class="dot ${esc((it.status || {}).kind || "")}"></span>
            <span class="status-text">${esc(guide ? guideStatus(it) : newsStatus(it.status))}</span>
            ${guide ? "" : `<button class="chatvote" data-act="chat-vote" data-id="${it.id}" aria-pressed="${voted}" title="想在群里聊这个">${SVG.chat}${it.chat_votes ? `<i>${it.chat_votes}</i>` : ""}</button>`}
            ${fbButtons("news", it)}
          </div>
        </div>
      </article>`;
  }

  function guideStatus(it) {
    const when_ = it.published_ts ? `${dayWord(it.published_ts)}发布 · ` : "";
    return `${when_}${it.created_ts ? dayWord(it.created_ts) + "找到" : "核对过还适用"}`;
  }

  function rejectedBlock(batch) {
    const list = batch.rejected || [];
    if (!admin() || !list.length) return "";
    const open = state.openRejected === batch.id;
    return `
      <div class="rej">
        <button class="rej-head" data-act="rej-toggle" data-id="${batch.id}" aria-expanded="${open}">
          被筛掉的 ${list.length} 条 <span class="rej-hint">只有你看得到，用来调标准</span>${SVG.down}
        </button>
        ${
          open
            ? `<div class="rej-list">${list
                .map(
                  (r) => `
            <div class="rej-row">
              <div class="rej-main">
                <a href="${safeUrl(r.url)}" target="_blank" rel="noopener noreferrer" class="rej-title">${esc(r.title || r.url)}</a>
                <div class="rej-why"><span class="ntag ${r.gate === "hard" ? "warn" : ""}">${r.gate === "hard" ? "硬性淘汰" : "分数不够"}</span>${esc(r.reason || "")}${r.avg != null ? ` · ${Number(r.avg).toFixed(1)} 分` : ""}</div>
              </div>
              ${r.site ? `<button class="btn small" data-act="block-domain" data-domain="${esc(r.site)}">屏蔽 ${esc(r.site)}</button>` : ""}
            </div>`
                )
                .join("")}</div>`
            : ""
        }
      </div>`;
  }

  function prefBox(v) {
    const pref = (v && v.feeds_pref) || "";
    if (!pref && !admin()) return "";
    return `<div class="pref">${ico("pushpin", "")}<div class="pref-t">${pref ? `<b>这个群想看：</b>${esc(pref)}` : `还没写这个群想看什么。写一句，MaiWork 每轮备料都会照着找。`}</div>${admin() ? `<button class="btn small" data-act="pref-edit">${pref ? "改" : "写一句"}</button>` : ""}</div>`;
  }

  function newsRunBtn() {
    if (!admin()) return "";
    const running = state.newsRunning === state.g;
    return `<button class="btn small news-run" data-act="news-run" ${running ? "disabled" : ""}>${running ? "在备料…" : "现在就备一批"}</button>`;
  }

  function newsSwitch() {
    const t = state.newsTab || "news";
    return prefBox(gview()) + `<div class="news-bar"><div class="seg" role="tablist">
      <button role="tab" data-act="news-tab" data-t="news" aria-selected="${t === "news"}">资讯</button>
      <button role="tab" data-act="news-tab" data-t="guides" aria-selected="${t === "guides"}">好文</button>
    </div>${newsRunBtn()}</div>`;
  }

  function viewNews(g, v) {
    if (g.fresh) {
      return `<h1 class="h-page">资讯</h1>` + emptyState("seedling", "还在熟悉这个群", "群画像成形之前不出资讯，免得乱推。");
    }
    if ((state.newsTab || "news") === "guides") {
      const guides = (v && v.guides) || [];
      let html = newsSwitch() + `<h1 class="h-page">好文</h1><p class="h-meta">教程、好文章、好工具。不看新不新，只看现在还适不适用</p>`;
      if (!guides.length) return html + emptyState("books", "还没有好文", "备料时会顺便找对这个群有用的教程和文章。");
      return html + guides.map((it, k) => newsItem(it, k, true)).join("");
    }
    const news = (v && v.news) || [];
    if (!news.length) {
      return newsSwitch() + `<h1 class="h-page">资讯</h1><p class="h-meta">每天几个时段按群画像去找，没有值得看的就不出</p>` + emptyState("newspaper", "还没有资讯", "下一批备料时会按这个群在聊的去找。");
    }
    let i = 0;
    return (
      newsSwitch() +
      news
        .map((batch, b) => {
          const rc = batch.rejected_count || 0;
          const head = `<h1 class="h-page" ${b ? 'style="margin-top:46px"' : ""}>${esc(slotName(batch.slot_ts))}</h1><p class="h-meta">${hhmm(batch.slot_ts)} 备料 · 找了 ${batch.found || 0} 条，留下 ${batch.kept || 0} 条${rc ? `，筛掉 ${rc} 条` : ""}</p>`;
          if (batch.skipped || !(batch.items || []).length) {
            return head + `<div class="skipped enter" style="--i:${i++}">${ico("teacup")}<span>${esc(batch.note || "这一批没有值得看的，跳过了。")}</span></div>` + rejectedBlock(batch);
          }
          return head + batch.items.map((it) => newsItem(it, i++, false)).join("") + rejectedBlock(batch);
        })
        .join("")
    );
  }

  const IDEA_STATE = { new: "", wanted: "有人想要，等管理员批准", pending: "等管理员批准", started: "已经在做了", dismissed: "已收起" };
  const ideaDot = (st) => (st === "started" ? "running" : st === "wanted" || st === "pending" ? "pending" : "");

  function viewIdeas(g, v) {
    const ideas = (v && v.ideas) || [];
    let html = `<h1 class="h-page">构想</h1><p class="h-meta">按这个群最近在聊的、在做的想出来的。点开一条，复制要求，到群里 @MaiBot 发出去，MaiWork 就会接下来${admin() ? "" : "（管理员批准后开工）"}</p>`;
    if (!ideas.length) {
      return html + emptyState("bulb", "还没有构想", g.fresh ? "等群画像成形后，MaiWork 会开始提想法。" : "最近没有想到适合这个群的点子，不硬凑。");
    }
    html += ideas
      .map((it, k) => {
        const did = `I-${it.id}`;
        const sel = state.detail && state.detail.id === did ? " selected" : "";
        const st = IDEA_STATE[it.state] || "";
        return `
        <article class="item ruled tap enter${sel}${it.state === "dismissed" ? " faded" : ""}" style="--i:${k}" data-act="idea-open" data-id="${esc(did)}" tabindex="0">
          ${ico(it.icon || "bulb")}
          <div>
            <h2 class="item-title">${esc(it.title)}</h2>
            <p class="item-body soft clamp3">${esc(it.body)}</p>
            ${st ? `<div class="status"><span class="dot ${ideaDot(it.state)}"></span><span class="status-text">${esc(st)}</span></div>` : ""}
            ${forWho(it.for_member)}
          </div>
        </article>`;
      })
      .join("");
    return html;
  }

  function findIdea(did) {
    const v = gview();
    const id = String(did || "").replace(/^I-/, "");
    return v && v.ideas ? v.ideas.find((x) => String(x.id) === id) : null;
  }

  // 复制出去的要求：到群里 @MaiBot 粘贴发送；末尾的「构想 #id」让 MaiWork 认出是哪条构想
  function ideaAsk(it) {
    const lines = [`帮我做这个构想：${it.title}`];
    if (it.body) lines.push(it.body);
    if (it.step) lines.push(`第一步：${it.step}`);
    lines.push(`（构想 #${it.id}）`);
    return lines.join("\n");
  }

  function ideaDetail(it) {
    const st = IDEA_STATE[it.state] || "新想法";
    const f = it.feasibility || {};
    const rows = [];
    if (it.step) rows.push(["第一步", it.step]);
    if (it.effort) rows.push(["要多久", it.effort]);
    if (f.note) rows.push([f.level === "ok" ? "能做" : f.level === "need" ? "需要帮忙" : "可能能做", f.note]);
    if (it.basis) rows.push(["为什么想到这个", it.basis]);
    const open = it.state === "new" || it.state === "wanted";
    const more = state.ideaMore === it.id;
    const menu = more
      ? `<div class="idea-menu">
          <div class="idea-menu-row"><span>有没有用</span>${fbButtons("ideas", it)}</div>
          ${admin() && open ? `<button class="idea-menu-btn" data-act="idea" data-op="do" data-id="${it.id}">${SVG.check}<span>不用发到群里，直接开工</span></button>` : ""}
          ${admin() && open ? `<button class="idea-menu-btn danger" data-act="idea" data-op="dismiss" data-id="${it.id}">${SVG.close}<span>收起，以后不再提</span></button>` : ""}
        </div>`
      : "";
    let main;
    if (open) main = `<button class="btn primary idea-go" data-act="idea-copy" data-id="${it.id}">${SVG.copy}复制要求</button>`;
    else if (it.state === "started" && it.task_id) main = `<button class="btn primary idea-go" data-act="task" data-id="${esc(it.task_id)}">看任务</button>`;
    else if (it.state === "pending" && admin()) main = `<button class="btn primary idea-go" data-act="tab" data-tab="tasks">去批准</button>`;
    else main = `<button class="btn idea-go" disabled>${esc(st)}</button>`;
    return `
      <div class="dt-head">
        ${ico(it.icon || "bulb")}
        <div>
          <h2 class="dt-title">${esc(it.title)}</h2>
          <div class="dt-state"><span class="dot ${ideaDot(it.state)}"></span>${esc(st)}${it.created_ts ? ` · ${esc(dayWord(it.created_ts))}想到的` : ""}</div>
        </div>
      </div>
      <div class="dt-sec"><div class="dt-text">${esc(it.body)}</div></div>
      ${rows.length ? `<div class="dt-sec idea-rows"><div class="dt-label">怎么做</div>${rows.map(([k, t]) => `<div class="idea-row"><div class="idea-row-k">${esc(k)}</div><div class="idea-row-v">${esc(t)}</div></div>`).join("")}</div>` : ""}
      ${forWho(it.for_member)}
      <div class="idea-bar">
        ${menu}
        <div class="idea-bar-row">
          <button class="idea-more" data-act="idea-more" data-id="${it.id}" aria-expanded="${more}" aria-label="更多">${SVG.more}</button>
          ${main}
        </div>
        ${open ? `<p class="idea-how">复制后到群里 @MaiBot 粘贴发送，MaiWork 认得出是这条构想${admin() ? "" : "，管理员批准后开工"}。</p>` : ""}
      </div>`;
  }

  // 构想「给谁的」：指明了人就在卡片底部放头像 + 名字；面向全群的不放
  function forWho(m) {
    if (!m || !m.user_id) return "";
    const name = String(m.name || "群友");
    return `<div class="for-who"><span class="face" style="background:${TONES[Math.abs(hash(m.user_id)) % TONES.length]}">${esc(name.slice(0, 1))}${m.avatar ? `<img src="${esc(m.avatar)}" alt="" loading="lazy" onerror="this.remove()" />` : ""}</span><span>想给 <b>${esc(name)}</b></span></div>`;
  }

  function nextText(goal) {
    if (goal.state === "paused") return "暂停中";
    return goal.next_check_ts ? `${when(goal.next_check_ts)} 检查` : "等新消息";
  }

  function viewGoals(g, v) {
    let html = `<h1 class="h-page">目标</h1>`;
    const goals = (v && v.goals) || { agent: [], member: [] };
    const agent = goals.agent || [];
    const member = goals.member || [];
    if (!agent.length && !member.length) {
      return html + emptyState("bullseye", "还没有目标", "有人在群里说「帮我们盯着……」或「提醒我……」时，会记在这里。");
    }
    let i = 0;
    if (agent.length) {
      html += `<h2 class="h-sub">我在推进 <small>${agent.length} 个 · 闲时静默推进，有结果再说</small></h2>`;
      html += agent
        .map((goal) => {
          const crit = goal.criteria || [];
          const done = crit.filter((c) => c.done).length;
          const sel = state.detail && state.detail.id === goal.id ? " selected" : "";
          return `
          <article class="item ruled tap enter${sel}" style="--i:${i++}" data-act="goal" data-id="${esc(goal.id)}" tabindex="0">
            ${ico(goal.icon || "bullseye")}
            <div>
              <h3 class="item-title">${esc(goal.title)}</h3>
              <p class="item-body soft">${esc(goal.body)}</p>
              ${crit.length ? `<div class="progress" aria-label="完成 ${done} / ${crit.length}"><i style="width:${(done / crit.length) * 100}%"></i></div>` : ""}
              <div class="status"><span class="dot ${goal.stale ? "failed" : goal.state === "paused" ? "" : "running"}"></span><span class="status-text">${goal.stale ? `<b class="bad-t">卡住了：${esc(goal.stale_reason || "")}</b>` : `${crit.length ? `${done} / ${crit.length} · ` : ""}${esc(nextText(goal))}`}</span></div>
            </div>
          </article>`;
        })
        .join("");
    }
    if (member.length) {
      html += `<h2 class="h-sub">帮大家记着 <small>${member.length} 件 · 到点提醒</small></h2>`;
      html += member
        .map((m) => {
          const due = m.repeat === "daily" ? "每天" : m.due_ts ? when(m.due_ts) : "没定时间";
          const rem = m.remind_ts ? `${when(m.remind_ts)} 提醒` : "";
          const left = m.until_ts ? ` · 循环还剩 ${dur(m.until_ts - now())}` : "";
          return `
          <article class="item ruled enter" style="--i:${i++}">
            ${ico(m.icon || "alarm")}
            <div>
              <h3 class="item-title"><span class="who">${esc(m.who)}</span> · ${esc(m.title)}</h3>
              <div class="status" style="margin-top:4px"><span class="dot pending"></span><span class="status-text">截止 ${esc(due)}${rem ? ` · ${esc(rem)}` : ""}${esc(left)}</span></div>
            </div>
          </article>`;
        })
        .join("");
    }
    return html;
  }

  function taskRow(t, i) {
    const sel = state.detail && state.detail.id === t.id ? " selected" : "";
    return `
      <button class="row enter${sel}" style="--i:${i}" data-act="task" data-id="${esc(t.id)}">
        ${ico(t.icon || "package")}
        <span>
          <span class="row-title">${esc(t.title)}</span>
          <span class="row-meta"><span class="dot ${DOT[t.status] !== undefined ? DOT[t.status] : t.status}"></span><span>${STATUS[t.status] || esc(t.status)}${t.meta ? ` · ${esc(t.meta)}` : ""}${t.undelivered ? ` · <b class="warn-t">做完了但还没发出去</b>` : ""}</span></span>
        </span>
        <span class="chev">${SVG.right}</span>
      </button>`;
  }

  function viewTasks(g, v) {
    let html = `<h1 class="h-page">任务</h1>`;
    const tasks = (v && v.tasks) || { pending: [], list: [] };
    const pending = tasks.pending || [];
    const list = tasks.list || [];
    const botName = (state.me && state.me.bot && state.me.bot.name) || "MaiBot";
    if (!pending.length && !list.length) {
      return html + emptyState("package", "还没有任务", `群里有人 @ ${botName} 请它准备东西，或者在构想里点了「做这个」，任务就会出现在这里。`);
    }
    let i = 0;
    if (pending.length) {
      html += `<h2 class="h-sub" style="margin-top:22px">${admin() ? "等你批准" : "等管理员批准"} <small>${pending.length} 件</small></h2>`;
      html += pending
        .map(
          (p) => `
          <article class="item ruled enter" style="--i:${i++}">
            ${ico(p.icon || "magnifier")}
            <div>
              <h3 class="item-title">${esc(p.title)}</h3>
              ${p.quote ? `<div class="quote"><span class="quote-by">${esc(p.who)} · ${esc(when(p.ts))}</span>${esc(p.quote)}</div>` : ""}
              <div class="via">${esc(p.via || "")}${p.age_s > 86400 ? ` · <b class="warn-t">等了 ${dur(p.age_s)}</b>` : ""}</div>
              ${
                admin()
                  ? `<div class="actions">
                <button class="btn primary" data-act="req" data-op="approve" data-id="${esc(p.id)}">批准</button>
                <button class="btn" data-act="req" data-op="reject" data-id="${esc(p.id)}">拒绝</button>
              </div>`
                  : `<div class="note-ok"><span class="dot pending"></span>管理员批准后开工</div>`
              }
            </div>
          </article>`
        )
        .join("");
    }
    if (list.length) {
      html += `<h2 class="h-sub">全部任务</h2>`;
      html += `<div class="filters" role="group" aria-label="按状态筛选">${FILTERS.map((f) => {
        const n = list.filter(f.match).length;
        return `<button class="filter" data-act="filter" data-f="${f.id}" aria-pressed="${state.filter === f.id}">${f.label}<span class="n">${n}</span></button>`;
      }).join("")}</div>`;
      const f = FILTERS.find((x) => x.id === state.filter) || FILTERS[0];
      const rows = list.filter(f.match);
      html += rows.length ? `<div>${rows.map((t) => taskRow(t, i++)).join("")}</div>` : `<p class="h-meta" style="margin-top:18px">这一栏现在是空的。</p>`;
    }
    return html;
  }

  function topicItem(t, i) {
    const opened = !!t.opener;
    const j = t.jev;
    const jevText = j ? `${j.ok ? "适合开" : `不适合 · ${j.reason || ""}`}${j.confidence != null ? ` · 把握 ${Number(j.confidence).toFixed(2)}` : ""}${j.detail ? `（${j.detail}）` : ""}` : "没问 Jev";
    const r = t.result;
    return `
      <article class="item ruled enter" style="--i:${i}">
        ${ico(opened ? "speech" : "hourglass")}
        <div>
          <h3 class="item-title">${esc(when(t.ts))} · ${opened ? "开了话题" : "忍住了没开"}</h3>
          <p class="item-body soft">安静了 ${esc(dur(t.quiet_s))}${t.usual_gap_s ? `，平时这个点 ${esc(dur(t.usual_gap_s))}一条` : ""}。Jev：${esc(jevText)}。${t.pick && opened ? `挑的是「${esc(t.pick.title)}」。` : ""}</p>
          ${opened ? `<div class="opener">${esc(t.opener)}</div>` : ""}
          ${opened ? `<div class="status"><span class="dot ${r && r.replies ? "used" : ""}"></span><span class="status-text">${r ? (r.replies ? `${r.replies} 人接话${r.followups ? `，MaiBot 接着聊了 ${r.followups} 句` : ""}` : "10 分钟内没人接，下次隔久一点") : "等着看有没有人接"}</span></div>` : ""}
          ${
            admin()
              ? `<div class="verdict">这次判断对吗？
            <button class="btn" data-act="verdict" data-id="${t.id}" data-v="right" aria-pressed="${t.verdict === "right"}">对</button>
            <button class="btn" data-act="verdict" data-id="${t.id}" data-v="wrong" aria-pressed="${t.verdict === "wrong"}">不对</button>
          </div>`
              : ""
          }
        </div>
      </article>`;
  }

  function profileSection(v) {
    const secs = (v && v.profile) || [];
    const byCat = Object.fromEntries(secs.map((s) => [s.category, s]));
    let i = 0;
    return CATS.map(([cat, name]) => {
      const sec = byCat[cat] || { entries: [] };
      const entries = sec.entries || [];
      if (!entries.length && !admin()) return "";
      return `
        <div class="pf-sec enter" style="--i:${i++}">
          <div class="pf-name">${esc(sec.name || name)}${admin() ? `<button class="pf-add" data-act="pf-add" data-cat="${cat}" aria-label="加一条">${SVG.plus}</button>` : ""}</div>
          ${
            entries.length
              ? entries
                  .map((e) => {
                    const meta = e.locked
                      ? admin()
                        ? e.source === "admin"
                          ? "你加的 · 不会被改写"
                          : "已锁定 · 不会被改写"
                        : "管理员确认过"
                      : [e.evidence_count ? `依据 ${e.evidence_count} 条消息` : "", e.last_ts ? `最近 ${dayWord(e.last_ts)}` : ""].filter(Boolean).join(" · ");
                    return `
              <div class="pf" data-entry="${e.id}">
                <div><div class="pf-text">${esc(e.text)}</div><div class="pf-meta">${esc(meta)}</div></div>
                ${
                  admin()
                    ? `<div class="pf-acts">
                  <button class="icon-btn" data-act="pf-edit" data-id="${e.id}" aria-label="修改">${SVG.pen}</button>
                  <button class="icon-btn" data-act="pf-lock" data-id="${e.id}" aria-pressed="${!!e.locked}" aria-label="${e.locked ? "解锁" : "锁定"}">${SVG.lock}</button>
                  <button class="icon-btn" data-act="pf-del" data-id="${e.id}" aria-label="删除">${SVG.trash}</button>
                </div>`
                    : ""
                }
              </div>`;
                  })
                  .join("")
              : `<div class="pf pf-none"><div class="pf-meta">还没有</div></div>`
          }
        </div>`;
    }).join("");
  }

  function groupSpaceBlock(v) {
    const gs = v && v.group_space;
    if (!gs) return "";
    const role = { owner: "群主", admin: "管理员", member: "普通成员" }[gs.role] || "身份未知";
    const cap = [
      ["files_list", "看群文件"],
      ["files_manage", "整理自己传的文件"],
      ["notice_send", "发群公告"],
      ["album_upload", "传群相册"],
    ];
    const any = cap.some(([k]) => gs[k]);
    return `
      <h2 class="h-sub">群空间 <small>机器人在这个群是${esc(role)}</small></h2>
      ${
        any
          ? `<div class="caps">${cap.map(([k, n]) => `<span class="cap ${gs[k] ? "on" : ""}">${gs[k] ? SVG.check : ""}${n}</span>`).join("")}</div>`
          : `<p class="h-meta">现在都用不了：QQ 适配器还是旧版，或者机器人不是管理员。升级适配器后会自动开放。</p>`
      }`;
  }

  function focusSection(v) {
    const focus = (v && v.focus) || [];
    let html = `<div class="h-sub-row"><h2 class="h-sub">关注成员 <span class="private">${SVG.lock}只有管理员看得到</span></h2><button class="btn small" data-act="focus-add">加一个人</button></div>`;
    if (!focus.length) return html + `<p class="h-meta">还没有。MaiWork 会从最活跃的、请它准备过东西的人里挑。</p>`;
    return (
      html +
      focus
        .map(
          (p, k) => `
        <div class="person enter" style="--i:${k}">
          <div class="face" style="background:${TONES[Math.abs(hash(p.user_id)) % TONES.length]}">${esc(pname(p).slice(0, 1))}${p.avatar ? `<img src="${esc(p.avatar)}" alt="" loading="lazy" onerror="this.remove()" />` : ""}</div>
          <div>
            <div class="person-name">${esc(pname(p))}${(p.reasons || []).map((r) => `<span class="tag">${esc(r)}</span>`).join("")}</div>
            ${personaBlock(p)}
            ${personalBlock(p)}
          </div>
          <button class="icon-btn" data-act="focus-rm" data-uid="${esc(p.user_id)}" aria-label="不再关注">${SVG.close}</button>
        </div>`
        )
        .join("")
    );
  }

  // 关注成员显示名：群名片（QQ昵称）；不回落成 QQ 号
  const pname = (p) => String(p.display_name || p.name || "群友");

  function personaBlock(p) {
    const ps = p.persona;
    if (!ps) return `<div class="person-note">${esc(p.note || "还没攒够了解。")}</div>`;
    const list = (label, arr) =>
      arr && arr.length ? `<div class="pa-row"><span class="pa-k">${label}</span><span class="pa-v">${arr.map(esc).join("、")}</span></div>` : "";
    return `
      <div class="person-note">${esc(ps.summary || p.note || "")}</div>
      <div class="pa">
        ${list("在做", ps.doing)}${list("关心", ps.cares)}${list("提过", ps.asked)}
        ${ps.style ? `<div class="pa-row"><span class="pa-k">说话</span><span class="pa-v">${esc(ps.style)}</span></div>` : ""}
      </div>
      ${ps.updated_ts ? `<div class="pa-ts">${esc(dayWord(ps.updated_ts))}更新 · 结合了 MaiBot 的长期记忆</div>` : ""}`;
  }

  function personalBlock(p) {
    const pe = p.personal;
    // 给这个人的构想放在「构想」页（卡片底部带头像），这里只列给 ta 找的资讯
    if (!pe || !(pe.news || []).length) return "";
    const news = (pe.news || []).slice(0, 3);
    return `
      <div class="pers">
        <div class="pers-h">给 ${esc(pname(p))} 找的${pe.last_ts ? ` · ${esc(dayWord(pe.last_ts))}` : ""}</div>
        ${news
          .map((n) => {
            const src = (n.sources || [])[0];
            return `
          <div class="pers-row">
            <div class="pers-main">
              ${src ? `<a class="pers-title" href="${safeUrl(src.url)}" target="_blank" rel="noopener noreferrer">${esc(n.title)}</a>` : `<span class="pers-title">${esc(n.title)}</span>`}
              <div class="pers-body">${richText(n.body || n.summary || "")}</div>
            </div>
            <button class="btn small" data-act="mention-member" data-id="${n.id}" data-name="${esc(pname(p))}">在群里提给 ta</button>
          </div>`;
          })
          .join("")}
      </div>`;
  }

  function hash(s) {
    let h = 0;
    for (const c of String(s)) h = (h * 31 + c.charCodeAt(0)) | 0;
    return h;
  }

  function viewGroup(g, v) {
    const members = g.members ? `${g.members} 人 · ` : "";
    let html = `<h1 class="h-page">${esc(gname(g))}</h1><p class="h-meta">${members}工作区 <span class="mono">${esc((v && v.workspace) || "")}</span></p>`;
    html += pulseCard(g, v, false);
    if (!v) return html + loading();
    if (g.fresh) {
      html += emptyState("seedling", "群画像还没成形", "MaiWork 读够聊天记录后，会在这里整理出这个群在聊什么、关心什么。");
      return admin() ? html + focusSection(v) : html;
    }
    const log = v.topic_log || [];
    html += `<h2 class="h-sub">开话题记录 <small>冷场时 MaiWork 开的头</small></h2>`;
    html += log.length ? log.map((t, k) => topicItem(t, k)).join("") : `<p class="h-meta">还没有冷场到需要开话题。</p>`;
    html += `<h2 class="h-sub">群画像 <small>${admin() ? "改过、锁定的以你为准" : "MaiWork 眼中的这个群"}</small></h2>`;
    html += profileSection(v);
    if (admin()) html += groupSpaceBlock(v) + focusSection(v);
    return html;
  }

  /* ───────────── 详情 ───────────── */

  function findGoal(id) {
    const v = gview();
    return v && v.goals ? (v.goals.agent || []).find((x) => x.id === id) : null;
  }
  function findTaskRow(id) {
    const v = gview();
    return v && v.tasks ? (v.tasks.list || []).find((x) => x.id === id) : null;
  }

  function taskDetail(id) {
    const t = state.tasks[id];
    const row = findTaskRow(id);
    if (!t) {
      if (t === null) return `<div class="empty">${ico("hourglass")}<b>找不到这个任务</b><span>可能不在这个群，或者已经被清理。</span></div>`;
      return row ? `<div class="dt-head">${ico(row.icon || "package")}<div><h2 class="dt-title">${esc(row.title)}</h2></div></div>${loading()}` : loading();
    }
    const tl = t.timeline || [];
    const st = t.status;
    const dot = DOT[st] !== undefined ? DOT[st] : st;
    let acts = "";
    if (admin()) {
      const b = (op, label, primary) => `<button class="btn${primary ? " primary" : ""}" data-act="task-op" data-op="${op}" data-id="${esc(t.id)}">${label}</button>`;
      if (["running", "reviewing", "queued", "waiting_input"].includes(st)) acts = b("pause", "暂停") + b("cancel", "取消");
      else if (st === "paused" || st === "shelved") acts = b("resume", "继续", true) + b("cancel", "取消");
      else if (st === "failed") acts = b("retry", "重试", true);
      else if (st === "completed") acts = b("redeliver", t.undelivered ? "再发一次" : "重新发布", t.undelivered);
    }
    return `
      <div class="dt-head">
        ${ico(t.icon || "package")}
        <div>
          <h2 class="dt-title">${esc(t.title)}</h2>
          <div class="dt-state"><span class="dot ${dot}"></span>${STATUS[st] || esc(st)} · <span class="mono">${esc(t.id)}</span></div>
        </div>
      </div>
      <div class="dt-sec"><div class="dt-label">要做什么</div><div class="dt-text">${esc(t.req || t.meta || "")}</div></div>
      ${t.question ? `<div class="dt-sec"><div class="dt-label">在等回答</div><div class="quote">${esc(t.question)}</div></div>` : ""}
      ${(t.criteria || []).length ? `<div class="dt-sec"><div class="dt-label">怎样算完成</div><ul class="dt-list">${t.criteria.map((c) => `<li>${esc(c)}</li>`).join("")}</ul></div>` : ""}
      ${admin() && t.env ? `<div class="dt-sec"><div class="dt-label">在哪里做</div><div class="dt-text">${esc(t.env)}</div></div>` : ""}
      ${
        !admin() && t.steps
          ? `<div class="dt-sec"><div class="dt-label">过程</div><div class="dt-text">已经做了 ${t.steps} 步${st === "running" ? "，还在继续" : ""}。每一步的细节只有管理员看得到。</div></div>`
          : ""
      }
      ${
        admin() && tl.length
          ? `<div class="dt-sec"><div class="dt-label">过程${st === "running" ? " · 实时" : ""}</div><ol class="tl">${tl
              .map(
                (s) => `
            <li class="${s.ok ? "ok" : "bad"}">
              <div class="tl-top"><span>${esc(when(s.ts))}</span><span class="tl-actor">${esc(s.actor)}</span><span class="tl-tool">${esc(s.tool)}</span><span class="tl-ms">${s.ms != null ? msText(s.ms) : ""}</span></div>
              <div class="tl-io">${esc(s.input)}<span class="arrow">→</span><span class="tl-out">${esc(s.output)}</span></div>
            </li>`
              )
              .join("")}</ol></div>`
          : ""
      }
      ${t.review ? `<div class="dt-sec"><div class="dt-label">验收意见</div><div class="dt-text">${esc(t.review)}</div></div>` : ""}
      ${
        (t.delivery || []).length
          ? `<div class="dt-sec"><div class="dt-label">交付</div>${t.delivery
              .map((x) => {
                const icon = x.kind === "群文件" ? "package" : x.kind === "here.now" ? "link" : "filebox";
                const title = x.url ? `<a href="${safeUrl(x.url)}" target="_blank" rel="noopener noreferrer">${esc(x.text)}</a>` : esc(x.text);
                return `<div class="deliv">${ico(icon)}<div><div class="deliv-k">${esc(x.kind)}</div><div class="deliv-t">${title}</div><div class="deliv-s">${esc(x.state)}</div></div></div>`;
              })
              .join("")}</div>`
          : ""
      }
      ${acts ? `<div class="actions" style="margin-top:28px">${acts}</div>` : ""}`;
  }

  function goalDetail(goal) {
    const crit = goal.criteria || [];
    const done = crit.filter((c) => c.done).length;
    const row = goal.task_id && findTaskRow(goal.task_id);
    let acts = "";
    if (admin() && goal.state !== "done" && goal.state !== "cancelled") {
      acts = `<div class="actions" style="margin-top:28px">${
        goal.state === "paused"
          ? `<button class="btn primary" data-act="goal-op" data-op="resume" data-id="${esc(goal.id)}">继续</button>`
          : `<button class="btn" data-act="goal-op" data-op="pause" data-id="${esc(goal.id)}">暂停</button>`
      }<button class="btn" data-act="goal-op" data-op="cancel" data-id="${esc(goal.id)}">不做了</button></div>`;
    }
    return `
      <div class="dt-head">
        ${ico(goal.icon || "bullseye")}
        <div>
          <h2 class="dt-title">${esc(goal.title)}</h2>
          <div class="dt-state"><span class="dot ${goal.state === "paused" ? "" : "running"}"></span>${goal.state === "paused" ? "暂停中" : "推进中"}${crit.length ? ` · ${done} / ${crit.length}` : ""}</div>
        </div>
      </div>
      <div class="dt-sec"><div class="dt-text">${esc(goal.body)}</div></div>
      ${
        crit.length
          ? `<div class="dt-sec"><div class="dt-label">完成标准</div>
        <ul class="checks">${crit.map((c) => `<li class="${c.done ? "done" : ""}"><span class="tick">${c.done ? SVG.check : ""}</span>${esc(c.text)}</li>`).join("")}</ul></div>`
          : ""
      }
      ${goal.last ? `<div class="dt-sec"><div class="dt-label">最近一次</div><div class="dt-text">${esc(when(goal.last.ts))} ${esc(goal.last.text)}</div></div>` : ""}
      <div class="dt-sec"><div class="dt-label">下次</div><div class="dt-text">${esc(nextText(goal))}</div></div>
      ${goal.by ? `<div class="dt-sec"><div class="dt-label">来源</div><div class="dt-text">${esc(goal.by)}</div></div>` : ""}
      ${row ? `<div class="dt-sec"><div class="dt-label">正在做的任务</div>${taskRow(row, 0)}</div>` : ""}
      ${acts}`;
  }

  function detailHTML() {
    if (!state.detail) return "";
    if (state.detail.type === "task") return taskDetail(state.detail.id);
    if (state.detail.type === "idea") {
      const it = findIdea(state.detail.id);
      return it ? ideaDetail(it) : `<div class="empty">${ico("bulb")}<b>找不到这条构想</b></div>`;
    }
    const goal = findGoal(state.detail.id);
    return goal ? goalDetail(goal) : `<div class="empty">${ico("bullseye")}<b>找不到这个目标</b></div>`;
  }

  async function loadTask(id) {
    try {
      state.tasks[id] = await api("GET", `/api/tasks/${encodeURIComponent(id)}`);
    } catch (e) {
      state.tasks[id] = null;
    }
    if (state.detail && state.detail.id === id) refreshDetail();
  }

  function refreshDetail() {
    if (desktop.matches) renderSide();
    else if (state.sheet === "detail") {
      const sh = $("sheet");
      const top = sh.scrollTop;
      sh.innerHTML = sheetChrome() + detailHTML();
      sh.scrollTop = top;
    }
  }

  /* ───────────── 设置 / 群切换（抽屉） ───────────── */

  function groupPicker() {
    return `<h1 class="h-page">切换群</h1>${state.groups
      .map(
        (g) => `
      <button class="gpick" data-act="group" data-g="${esc(g.id)}">
        ${gface(g)}
        <span class="gpick-t"><span class="gpick-name" style="display:block">${mq(gname(g))}</span><span class="gpick-sub">${g.members ? `${g.members} 人 · ` : ""}${esc(quiet(g).text)}</span></span>
        ${g.id === state.g ? SVG.check : ""}
      </button>`
      )
      .join("")}`;
  }

  const SET_SUBS = [
    ["overview", "总览", "gear", "各个群、今天的用量、运行状态"],
    ["identity", "身份", "lotus", "SOUL、做事规矩、工作记忆"],
    ["models", "模型", "robot", "端点、密钥、主模型和子 agent 模型"],
    ["extensions", "扩展", "tools", "MCP 和 skill，可给主模型或子 agent"],
    ["usage", "用量", "chart", "每天的 tokens 和调用次数，能看以前的"],
    ["sources", "资讯来源", "newspaper", "RSS 和屏蔽的来源"],
    ["links", "群链接", "link", "群友看到的专属链接"],
    ["rules", "全部配置", "moon", "配置文件里的每一项，都能在这里改"],
    ["logs", "请求日志", "memo", "最近的模型调用、工具调用和失败记录"],
  ];

  function setNav() {
    return `<div class="set-nav" role="tablist">${SET_SUBS.map(
      ([id, label]) => `<button role="tab" data-act="set-sub" data-sub="${id}" aria-selected="${state.setSub === id}">${label}</button>`
    ).join("")}</div>`;
  }

  function settingsPage() {
    const s = state.settings;
    const sub = SET_SUBS.find((x) => x[0] === state.setSub) || SET_SUBS[0];
    const head = `<h1 class="h-page">${sub[0] === "overview" ? "设置" : esc(sub[1])}</h1>${setNav()}`;
    if (!s) return head + loading();
    const body = { overview: settingsOverview, models: modelsPage, extensions: extPage, usage: usagePage, sources: sourcesPage, links: linksPage, rules: rulesPage, identity: identityPage, logs: logsPage }[sub[0]];
    return head + `<div class="set-body">${body(s)}</div>`;
  }

  function settingsSide() {
    const s = state.settings || {};
    return `
      <h2 class="h-sub" style="margin-top:6px">设置</h2>
      ${SET_SUBS.map(
        ([id, label, icon, hint]) => `
        <button class="gpick side-set${state.setSub === id ? " on" : ""}" data-act="set-sub" data-sub="${id}">
          ${ico(icon)}<span><span class="gpick-name" style="display:block;font-size:16px">${label}</span><span class="gpick-sub">${hint}</span></span>
          ${id === "models" && s.models && !s.models.ready ? `<span class="r-badge" style="position:static">!</span>` : `<span></span>`}
        </button>`
      ).join("")}`;
  }

  function linksPage(s) {
    return `
      ${(s.groups || [])
        .map(
          (g) => `<div class="set-row">${ico("link")}<div><div class="set-name">${esc(g.name || `群 ${g.id}`)}</div><div class="set-text mono-link">${esc(fullLink(g))}</div></div><span class="row-btns"><button class="btn small" data-act="copy" data-link="${esc(fullLink(g))}">复制</button><button class="btn small" data-act="reset-link" data-g="${esc(g.id)}">重置</button></span></div>`
        )
        .join("") || `<p class="h-meta">还没有服务群。</p>`}
      <p class="fine">群友打开自己群的链接，只看得到这个群；看不到别的群、关注成员和设置。链接泄露了就点「重置」，旧链接马上失效。</p>`;
  }

  function settingsOverview(s) {
    const u = (s.usage && s.usage.today) || {};
    const alert = (s.usage && s.usage.alert_daily_tokens) || 0;
    const used = (u.main || 0) + (u.worker || 0);
    const r = s.rules || {};
    return `
      ${(s.problems || []).length ? `<div class="warn-box"><b>配置里有几处问题，已经先跳过：</b><br />${s.problems.map(esc).join("<br />")}</div>` : ""}
      <h2 class="h-sub" style="margin-top:20px">各个群</h2>
      ${
        state.groups.length
          ? state.groups
              .map(
                (g) => `
        <button class="gpick" data-act="group" data-g="${esc(g.id)}">
          ${gface(g)}
          <span class="gpick-t"><span class="gpick-name" style="display:block;font-size:16px">${mq(gname(g))}</span>
          <span class="gpick-sub">${g.fresh ? "刚开始服务 · 还在熟悉" : `今天 ${g.today.news} 条资讯 · 开话题 ${g.today.topics} 次 · 待批 ${g.today.pending}`}</span></span>
          <span class="chev" style="color:var(--gray-2)">${SVG.right}</span>
        </button>`
              )
              .join("")
          : `<p class="h-meta">还没有服务群。去「全部配置 → 服务群」加上群号，保存后就会出现在这里。</p>`
      }
      ${s.models && !s.models.ready ? `<div class="warn-box" style="margin-top:18px">还没配好模型，MaiWork 现在不会做任何要用模型的事。<button class="btn small" data-act="set-sub" data-sub="models" style="margin-left:8px">去配</button></div>` : ""}
      <div class="h-sub-row"><h2 class="h-sub">今天的用量</h2><button class="btn small" data-act="set-sub" data-sub="usage">看以前的</button></div>
      <div class="usage">
        <div><b>${tokens(u.main)}</b><span>主模型 · tokens</span></div>
        <div><b>${tokens(u.worker)}</b><span>子 agent · tokens</span></div>
        <div><b>${u.jev || 0}</b><span>Jev 判断 · 次</span></div>
      </div>
      ${
        ((s.usage && s.usage.alerts) || []).length
          ? `<div class="warn-box"><b>今天超过提醒线了：</b><br />${s.usage.alerts.map((a) => esc(a.text)).join("<br />")}</div>`
          : ""
      }
      <p class="fine">${alert ? `每日提醒线 ${tokens(alert)} tokens，今天用了 ${Math.round((used / alert) * 100)}%。` : "没设提醒线。"}只提醒，不暂停。${u.errors ? ` 今天有 ${u.errors} 次调用出错。` : ""}</p>
      <h2 class="h-sub">运行状态</h2>
      ${(s.health || []).map((h) => `<div class="set-row">${ico(h.icon || "gear")}<div><div class="set-name">${esc(h.name)}</div><div class="set-text">${esc(h.text)}</div></div><span class="dot ${h.state === "ok" ? "ok" : h.state === "warn" ? "pending" : ""}"></span></div>`).join("")}
      <div class="actions" style="margin-top:22px"><button class="btn" data-act="onb-restart">重新引导</button><button class="btn" data-act="logout">退出管理员</button></div>`;
  }

  const ROLE_NAMES = { worker: "子 agent", main: "主模型" };

  function extPage() {
    const x = state.ext;
    if (!x) return loading();
    const ed = state.extEdit;
    const mcp = x.mcp || [];
    const skills = x.skills || [];
    const newMcp = ed && ed.kind === "mcp" && !ed.name;
    const pasting = ed && ed.kind === "mcp-paste";
    const newSkill = ed && ed.kind === "skill" && !ed.name;
    return `
      <p class="h-meta">给 MaiWork 自己用：每一项都可以选「只给主模型」「只给子 agent」或「两边都能用」。主模型就是你在「和 MaiWork 聊」里对话、以及派活验收的那个。都不会挂到 MaiBot 上；在这里改的存在服务器数据目录里，不会让 MaiBot 重载插件。</p>
      ${searchCard()}
      <div class="h-sub-row"><h2 class="h-sub">MCP <small>接外部工具</small></h2>${newMcp || pasting ? "" : `<span class="row-btns"><button class="btn small" data-act="mcp-paste">粘贴配置</button><button class="btn small" data-act="ext-new" data-kind="mcp">加一个</button></span>`}</div>
      ${pasting ? mcpPasteForm() : ""}
      ${newMcp ? mcpForm(null) : ""}
      ${mcp.length ? mcp.map((m) => (ed && ed.kind === "mcp" && ed.name === m.name ? mcpForm(m) : mcpRow(m))).join("") : newMcp ? "" : `<p class="h-meta">还没有。</p>`}
      <div class="h-sub-row"><h2 class="h-sub">skill <small>写给模型的做事说明</small></h2>${newSkill ? "" : `<span class="row-btns"><label class="btn small file-btn">上传 zip<input type="file" id="skill-zip" accept=".zip,application/zip" hidden /></label><button class="btn small" data-act="ext-new" data-kind="skill">加一个</button></span>`}</div>
      ${newSkill ? skillForm(null) : ""}
      ${skills.length ? skills.map((k) => (ed && ed.kind === "skill" && ed.name === k.name ? skillForm(k) : skillRow(k))).join("") : newSkill ? "" : `<p class="h-meta">还没有。</p>`}`;
  }

  // 联网搜索：从已接的 MCP 里挑一个工具来用（后端 GET/PUT/DELETE /api/extensions/search）
  function searchCard() {
    const sx = state.extSearch;
    if (!sx) return "";
    const b = sx.binding;
    const st = sx.status || {};
    const cands = (sx.candidates || []).filter((c) => (c.tools || []).length);
    const editing = state.searchEdit || !b;
    const head = `<div class="h-sub-row"><h2 class="h-sub">联网搜索 <small>找资讯、查资料都用它</small></h2>${!editing ? `<span class="row-btns"><button class="btn small" data-act="search-edit">换一个</button></span>` : ""}</div>`;
    const stLine = `<div class="set-text ext-st"><span class="dot ${st.ok ? "ok" : b ? "failed" : ""}"></span>${esc(b ? st.text || "" : "还没指定：资讯和个人向内容会暂停找新的")}</div>`;
    if (!editing) {
      return `${head}
        <div class="ext-row search-now">
          ${ico("magnifier")}
          <div class="ext-main">
            <div class="set-name">${esc(b.mcp)} · <span class="mono">${esc(b.tool)}</span></div>
            ${b.extract_tool ? `<div class="set-text">抓网页正文：<span class="mono">${esc(b.extract_tool)}</span></div>` : ""}
            ${stLine}
          </div>
        </div>
        <p class="fine search-agents">想让它搜得更合你意（比如只看中文站、优先官方来源），写进「设置 → 身份」的 AGENTS.md 就行。</p>`;
    }
    if (!cands.length) {
      return `${head}${stLine}<p class="h-meta">先在下面接一个能搜索的 MCP（比如 Tavily、Exa、You.com 的 MCP），接好以后回这里选它。</p>`;
    }
    const all = cands.flatMap((c) => c.tools.map((t) => ({ mcp: c.mcp, ...t })));
    const pick = b ? all.find((t) => t.mcp === b.mcp && t.name === b.tool) : all.find((t) => t.guess === "search") || all[0];
    const opt = (t, sel) => `<option value="${esc(t.mcp + "\u0000" + t.name)}" ${sel ? "selected" : ""}>${esc(t.mcp)} · ${esc(t.name)}${t.guess === "search" ? "（像搜索）" : ""}</option>`;
    return `${head}${b ? stLine : ""}
      <div class="login ext-form search-form">
        <label for="sx-tool">用哪个工具搜索</label>
        <select id="sx-tool">${all.map((t) => opt(t, pick && t.mcp === pick.mcp && t.name === pick.name)).join("")}</select>
        <label for="sx-extract">抓网页正文 <span class="fine-inline">可选，网页打不开时用它读全文</span></label>
        <select id="sx-extract">${extractOpts(pick ? pick.mcp : "", b ? b.extract_tool : null)}</select>
        <div class="actions">
          <button class="btn primary" data-act="search-save">用这个</button>
          ${b ? `<button class="btn" data-act="search-cancel">取消</button><button class="btn danger" data-act="search-off">不用联网搜索</button>` : ""}
        </div>
      </div>`;
  }

  // 抓正文工具只能从同一个 MCP 里选；cur=null 时按名字猜
  function extractOpts(mcp, cur) {
    const c = ((state.extSearch && state.extSearch.candidates) || []).find((x) => x.mcp === mcp);
    const tools = (c && c.tools) || [];
    const guess = cur === null ? (tools.find((t) => t.guess === "extract") || {}).name || "" : cur || "";
    return `<option value="" ${guess ? "" : "selected"}>不用</option>` + tools.map((t) => `<option value="${esc(t.name)}" ${t.name === guess ? "selected" : ""}>${esc(t.name)}${t.guess === "extract" ? "（像抓正文）" : ""}</option>`).join("");
  }

  function mcpRow(m) {
    const st = !m.enabled
      ? `<span class="dot"></span>关着`
      : m.ok
        ? `<span class="dot ok"></span>连上了 · ${m.tools || 0} 个工具`
        : `<span class="dot failed"></span>没连上：${esc(m.error || "")}`;
    const roles = roleText(m.roles);
    const hdrs = (m.header_names || []).length ? ` · 请求头 ${m.header_names.map(esc).join("、")}（已填）` : "";
    return `
      <div class="ext-row">
        ${ico("link")}
        <div class="ext-main">
          <div class="set-name">${esc(m.name)}${m.source === "config" ? `<span class="tag">配置文件里的</span>` : ""}${m.search_role === "search" ? `<span class="tag">联网搜索</span>` : m.search_role === "extract" ? `<span class="tag">抓正文</span>` : ""}</div>
          <div class="set-text mono-link">${esc(m.url || "")}</div>
          <div class="set-text ext-st">${st} · 给${esc(roles)}用${hdrs}</div>
          ${
            (m.tool_names || []).length
              ? `<div class="ext-tools">${m.tool_names.slice(0, 12).map((t) => `<span class="ntag">${esc(t)}</span>`).join("")}${m.tool_names.length > 12 ? `<span class="ntag">共 ${m.tool_names.length} 个</span>` : ""}</div>`
              : ""
          }
        </div>
        <div class="row-btns ext-btns">
          <button class="btn small" data-act="mcp-toggle" data-name="${esc(m.name)}" data-on="${m.enabled ? "0" : "1"}">${m.enabled ? "关掉" : "打开"}</button>
          ${m.enabled ? `<button class="btn small" data-act="mcp-reload" data-name="${esc(m.name)}">重连</button>` : ""}
          ${m.source === "config" ? "" : `<button class="btn small" data-act="ext-edit" data-kind="mcp" data-name="${esc(m.name)}">改</button>`}
        </div>
      </div>`;
  }

  function mcpPasteForm() {
    return `
      <form id="mcp-paste-form" class="login ext-form" autocomplete="off">
        <div class="ext-form-h">粘贴 MCP 配置</div>
        <p class="fine" style="margin:0">支持 Claude Desktop / Cursor 的 <span class="mono">mcpServers</span>、VS Code 的 <span class="mono">servers</span>，或单个服务器的 <span class="mono">{"type":"http","url":…,"headers":…}</span>。只支持远程（http / sse）的，要在本机跑命令的那种（<span class="mono">command</span>）不支持。</p>
        <textarea id="mp-text" class="mono-area" rows="9" spellcheck="false" placeholder='{"mcpServers": {"tavily": {"type": "http", "url": "https://…/mcp", "headers": {"Authorization": "Bearer …"}}}}'></textarea>
        <p class="err" id="mp-err" hidden></p>
        <div class="actions">
          <button class="btn primary" type="submit">读出来</button>
          <button type="button" class="btn" data-act="ext-cancel">取消</button>
        </div>
      </form>`;
  }

  // 解析各家 MCP 客户端配置 → [{name, url, headers, skipped?}]
  function parseMcpConfig(text) {
    let data;
    try {
      data = JSON.parse(text);
    } catch (e) {
      // 允许只粘了 "名字": {...} 这一段
      try {
        data = JSON.parse(`{${text.trim().replace(/,\s*$/, "")}}`);
      } catch (e2) {
        throw new Error("这段不是合法的 JSON，检查一下有没有少括号或多逗号。");
      }
    }
    if (!data || typeof data !== "object") throw new Error("没读出服务器配置。");
    const isServer = (o) => o && typeof o === "object" && (o.url || o.serverUrl || o.command);
    let entries;
    if (isServer(data)) entries = [["", data]];
    else {
      const map = data.mcpServers || data.servers || (data.mcp && data.mcp.servers) || data;
      entries = Object.entries(map).filter(([, v]) => isServer(v));
    }
    if (!entries.length) throw new Error("没找到 MCP 服务器（要有 url）。");
    const slug = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32);
    return entries.map(([key, v]) => {
      const url = String(v.url || v.serverUrl || "");
      let name = slug(key);
      if (!name && url) {
        try {
          const host = new URL(url).hostname.split(".");
          name = slug(host.length > 2 ? host[host.length - 3] || host[0] : host[0]);
        } catch (e) {}
      }
      if (v.command) return { name: name || "mcp", skipped: "要在本机跑命令（command），不支持" };
      if (!/^https:\/\//i.test(url)) return { name: name || "mcp", skipped: "地址不是 https" };
      const headers = {};
      Object.entries(v.headers || v.requestInit?.headers || {}).forEach(([k, val]) => {
        if (typeof val === "string") headers[k] = val;
      });
      return { name: name || "mcp", url, headers };
    });
  }

  // 给谁用：三选一（主模型 / 子 agent / 两边都能用）
  const ROLE_PICKS = [["main", "只给主模型", "和 MaiWork 聊、派活验收时能用"], ["worker", "只给子 agent", "动手干活时能用"], ["both", "两边都能用", ""]];
  const rolePick = (roles) => {
    roles = roles && roles.length ? roles : ["worker"];
    return roles.includes("main") && roles.includes("worker") ? "both" : roles.includes("main") ? "main" : "worker";
  };
  const roleText = (roles) => ({ main: "主模型", worker: "子 agent", both: "主模型和子 agent" })[rolePick(roles)];
  function roleChecks(prefix, roles) {
    const cur = rolePick(roles);
    return `<div class="seg role-seg" role="radiogroup">${ROLE_PICKS.map(
      ([k, n, hint]) => `<label title="${esc(hint)}"><input type="radio" name="${prefix}-role" value="${k}" ${cur === k ? "checked" : ""} /><span>${n}</span></label>`
    ).join("")}</div>`;
  }

  function headerRow(name, isOld, value) {
    return `
      <div class="hdr-row" data-old="${isOld ? "1" : "0"}">
        <input class="hdr-name" spellcheck="false" placeholder="名字，比如 Authorization" value="${esc(name || "")}" ${isOld ? "readonly" : ""} />
        <input class="hdr-val" type="password" autocomplete="new-password" placeholder="${isOld ? "已填 · 留空就不改" : "值，比如 Bearer …"}" value="${esc(value || "")}" />
        <button type="button" class="icon-btn" data-act="hdr-del" aria-label="删掉这个请求头">${SVG.trash}</button>
      </div>`;
  }

  function mcpForm(m) {
    const isNew = !m;
    const pre = (isNew && state.extEdit && state.extEdit.prefill) || null;
    m = m || { roles: ["worker"], enabled: true, timeout_s: 30, header_names: [], tools_filter: [], ...(pre || {}) };
    return `
      <form id="mcp-form" class="login ext-form" autocomplete="off" data-name="${esc(isNew ? "" : m.name)}">
        <div class="ext-form-h">${isNew ? "加一个 MCP" : `改 ${esc(m.name)}`}</div>
        ${pre ? `<p class="fine" style="margin:0">从粘贴的配置里读出来的，看一眼没问题就保存。</p>` : ""}
        ${isNew ? `<label for="x-name">名字</label><input id="x-name" spellcheck="false" placeholder="只用字母、数字、下划线、横线，比如 github" value="${esc(m.name || "")}" />` : ""}
        <label for="x-url">地址</label>
        <input id="x-url" type="url" inputmode="url" spellcheck="false" value="${esc(m.url || "")}" placeholder="https://…/mcp" />
        <label>请求头 <small class="lbl-hint">密钥只进不出，保存后网页上看不到</small></label>
        <div id="x-hdrs">${isNew ? Object.entries(m.headers || {}).map(([h, v]) => headerRow(h, false, v)).join("") : (m.header_names || []).map((h) => headerRow(h, true)).join("")}</div>
        <button type="button" class="btn small" data-act="hdr-add" style="align-self:flex-start">加一个请求头</button>
        <label for="x-tools">只用这些工具 <small class="lbl-hint">空着 = 全部；多个用逗号隔开</small></label>
        <input id="x-tools" spellcheck="false" value="${esc((m.tools_filter || []).join(", "))}" />
        <label>给谁用</label>
        ${roleChecks("x", m.roles)}
        <label for="x-timeout">超时（秒）</label>
        <input id="x-timeout" type="number" min="5" max="300" value="${esc(m.timeout_s || 30)}" />
        <label class="chk"><input type="checkbox" id="x-enabled" ${m.enabled !== false ? "checked" : ""} />打开</label>
        <div class="actions"><button type="button" class="btn" data-act="mcp-test">测试连接</button><span class="fine" id="x-check" style="margin:0;align-self:center"></span></div>
        <p class="err" id="x-err" hidden></p>
        <div class="actions">
          <button class="btn primary" type="submit">保存</button>
          <button type="button" class="btn" data-act="ext-cancel">取消</button>
          ${isNew ? "" : `<button type="button" class="btn danger" data-act="mcp-del" data-name="${esc(m.name)}">删掉</button>`}
        </div>
      </form>`;
  }

  function skillRow(k) {
    const roles = roleText(k.roles);
    return `
      <div class="ext-row">
        ${ico("books")}
        <div class="ext-main">
          <div class="set-name">${esc(k.name)}${k.source === "file" ? `<span class="tag">服务器上放的</span>` : ""}</div>
          <div class="set-text">${esc(k.description || "没写描述")}</div>
          <div class="set-text ext-st">给${esc(roles)}用${k.updated_ts ? ` · ${esc(dayWord(k.updated_ts))}改过` : ""}</div>
        </div>
        <div class="row-btns ext-btns"><button class="btn small" data-act="ext-edit" data-kind="skill" data-name="${esc(k.name)}">改</button></div>
      </div>`;
  }

  function skillForm(k) {
    const isNew = !k;
    const d = (state.extEdit && state.extEdit.data) || {};
    if (!isNew && !state.extEdit.data) return `<div class="ext-form">${loading()}</div>`;
    return `
      <form id="skill-form" class="login ext-form" autocomplete="off" data-name="${esc(isNew ? "" : k.name)}">
        <div class="ext-form-h">${isNew ? "加一个 skill" : `改 ${esc(k.name)}`}</div>
        ${isNew ? `<label for="k-name">名字</label><input id="k-name" spellcheck="false" placeholder="只用字母、数字、下划线、横线，比如 write-report" />` : ""}
        <label for="k-desc">一句话描述 <small class="lbl-hint">模型靠这句决定要不要读它</small></label>
        <input id="k-desc" value="${esc(d.description || "")}" placeholder="比如：写周报时的格式和注意事项" />
        <label>给谁用</label>
        ${roleChecks("k", d.roles)}
        <label for="k-body">内容 <small class="lbl-hint">怎么做、注意什么、例子；最多 40KB</small></label>
        <textarea id="k-body" class="mono-area" rows="14" spellcheck="false">${esc(d.body || "")}</textarea>
        ${(d.files || []).length ? `<p class="fine" style="margin:0">附带的文件（只能在服务器上改）：${d.files.map(esc).join("、")}</p>` : ""}
        <p class="err" id="k-err" hidden></p>
        <div class="actions">
          <button class="btn primary" type="submit">保存</button>
          <button type="button" class="btn" data-act="ext-cancel">取消</button>
          ${isNew ? "" : `<button type="button" class="btn danger" data-act="skill-del" data-name="${esc(k.name)}">删掉</button>`}
        </div>
      </form>`;
  }

  function readRoles(prefix) {
    const el = document.querySelector(`input[name="${prefix}-role"]:checked`);
    const v = el ? el.value : "worker";
    return v === "both" ? ["main", "worker"] : [v];
  }

  function readHeaders() {
    const headers = {};
    const remove = [];
    document.querySelectorAll("#x-hdrs .hdr-row").forEach((row) => {
      const n = row.querySelector(".hdr-name").value.trim();
      const v = row.querySelector(".hdr-val").value;
      if (!n) return;
      if (row.dataset.removed === "1") remove.push(n);
      else if (row.dataset.old === "1") headers[n] = v; // 空 = 不改
      else if (v) headers[n] = v;
    });
    return { headers, remove };
  }

  async function loadExt() {
    try {
      state.ext = await api("GET", "/api/extensions");
    } catch (e) {
      state.ext = { mcp: [], skills: [] };
      toast(e.message, true);
    }
    try {
      state.extSearch = await api("GET", "/api/extensions/search");
    } catch (e) {
      state.extSearch = null;
    }
  }

  /* ───── 全部配置（config.toml 的每一项；网页改的直接写进插件的 config.toml） ───── */
  // 后端 GET /api/settings/config：{file, sections:[{id,label,fields:[{key,label,help,type,value,default,changed,...}]}], reload_pending:[]}
  // 密钥字段只给 {set, source: env|file|none}，值只进不出
  const CFG_CHIPS = { "approval.admins": "accounts", "approval.exempt_users": "accounts", "approval.exempt_groups": "groups", "feeds.news_slots": "times" };
  const cfgKind = (f) => CFG_CHIPS[f.key] || (f.type === "time_list" ? "times" : f.type);
  const cfgId = (key) => `c-${String(key).replace(/[^a-z0-9_]/gi, "-")}`;
  const SRC_NAMES = { file: "config.toml 里", env: "服务器环境变量", none: "" };

  async function loadRules() {
    try {
      state.rules = await api("GET", "/api/settings/config");
    } catch (e) {
      state.rules = null;
      toast(e.message, true);
    }
  }

  function cfgField(key) {
    for (const s of (state.rules && state.rules.sections) || []) for (const f of s.fields || []) if (f.key === key) return f;
    return null;
  }

  const PLATFORM_NAMES = { qq: "QQ", telegram: "Telegram", discord: "Discord", wechat: "微信", kook: "KOOK" };
  // 「平台:账号」（MaiBot 标准写法）；只写数字当 qq；认不出返回 ""
  function normAccount(raw) {
    const t = String(raw || "").trim();
    if (!t) return "";
    if (!t.includes(":")) return /^\d+$/.test(t) ? `qq:${t}` : "";
    const i = t.indexOf(":");
    const plat = t.slice(0, i).trim().toLowerCase();
    const acc = t.slice(i + 1).trim();
    return /^[a-z][a-z0-9_]{0,15}$/.test(plat) && /^\S{1,64}$/.test(acc) ? `${plat}:${acc}` : "";
  }

  function chipHTML(kind, v) {
    let label = esc(v);
    if (kind !== "times") {
      const i = v.indexOf(":");
      const plat = v.slice(0, i);
      label = `<span class="ci-plat">${esc(PLATFORM_NAMES[plat] || plat)}</span>${esc(v.slice(i + 1))}`;
    }
    return `<span class="ci" data-v="${esc(v)}">${label}<button type="button" class="ci-x" data-act="chip-del" aria-label="删掉 ${esc(v)}">${SVG.close}</button></span>`;
  }

  function chipEditor(id, kind, vals) {
    const items = (vals || []).map((v) => chipHTML(kind, kind === "times" ? v : normAccount(v) || v)).join("");
    const adder =
      kind === "times"
        ? `<input type="time" class="ca-time" step="60" aria-label="新增时间" />`
        : `<input class="ca-plat" value="qq" list="mw-platforms" spellcheck="false" aria-label="平台" /><span class="ca-colon">:</span><input class="ca-acc" inputmode="${kind === "groups" ? "numeric" : "text"}" spellcheck="false" placeholder="${kind === "groups" ? "群号" : "账号"}" aria-label="${kind === "groups" ? "群号" : "账号"}" />`;
    return `
      <div class="chips" id="${id}" data-kind="${kind}">
        <div class="ci-list">${items || `<span class="ci-empty">还没有</span>`}</div>
        <div class="ci-add">${adder}<button type="button" class="btn small" data-act="chip-add">加上</button></div>
        <p class="ci-err" hidden></p>
      </div>`;
  }

  function chipAdd(box) {
    const kind = box.dataset.kind;
    const err = box.querySelector(".ci-err");
    let v = "";
    if (kind === "times") {
      v = (box.querySelector(".ca-time").value || "").slice(0, 5);
      if (!/^\d{2}:\d{2}$/.test(v)) v = "";
    } else {
      const plat = box.querySelector(".ca-plat").value.trim() || "qq";
      const acc = box.querySelector(".ca-acc").value.trim();
      v = acc ? normAccount(`${plat}:${acc}`) : "";
      if (kind === "groups" && v && !/^[a-z][a-z0-9_]*:\d+$/.test(v)) v = "";
    }
    if (!v) {
      err.textContent = kind === "times" ? "选一个时间" : kind === "groups" ? "填群号（数字）" : "填账号，平台默认 qq";
      err.hidden = false;
      return;
    }
    err.hidden = true;
    const list = box.querySelector(".ci-list");
    if ([...list.querySelectorAll(".ci")].some((c) => c.dataset.v === v)) {
      err.textContent = "已经有了";
      err.hidden = false;
      return;
    }
    const empty = list.querySelector(".ci-empty");
    if (empty) empty.remove();
    list.insertAdjacentHTML("beforeend", chipHTML(kind, v));
    if (kind === "times") {
      const all = [...list.querySelectorAll(".ci")].sort((a, b) => a.dataset.v.localeCompare(b.dataset.v));
      all.forEach((c) => list.appendChild(c));
      box.querySelector(".ca-time").value = "";
    } else box.querySelector(".ca-acc").value = "";
  }

  // 多行表格编辑器（服务的群、SSH 服务器）：cols = [[字段, 占位提示], ...]
  function rowsEditor(id, cols, rows) {
    const one = (r) => `<div class="re-row">${cols.map(([k, ph]) => `<input data-k="${k}" spellcheck="false" placeholder="${esc(ph)}" value="${esc((r || {})[k] ?? "")}" />`).join("")}<button type="button" class="icon-btn" data-act="re-del" aria-label="删掉这一行">${SVG.trash}</button></div>`;
    return `<div class="rows-ed" id="${id}" data-cols="${esc(JSON.stringify(cols))}"><div class="re-list">${(rows || []).map(one).join("")}</div><button type="button" class="btn small" data-act="re-add">${SVG.plus}加一行</button></div>`;
  }
  function rowsAdd(box) {
    const cols = JSON.parse(box.dataset.cols || "[]");
    box.querySelector(".re-list").insertAdjacentHTML("beforeend", `<div class="re-row">${cols.map(([k, ph]) => `<input data-k="${k}" spellcheck="false" placeholder="${esc(ph)}" />`).join("")}<button type="button" class="icon-btn" data-act="re-del" aria-label="删掉这一行">${SVG.trash}</button></div>`);
    const first = box.querySelector(".re-row:last-child input");
    if (first) first.focus();
  }
  const ROWS_COLS = {
    serve_groups: [["group", "qq:群号"], ["workspace", "工作区名（可空）"]],
    ssh_list: [["name", "名字"], ["host", "root@1.2.3.4"], ["note", "备注（可空）"]],
  };

  function cfgInput(f) {
    const id = cfgId(f.key);
    const kind = cfgKind(f);
    const val = f.value;
    if (f.readonly) {
      const shown = Array.isArray(val) ? (val.length ? val.map((x) => (typeof x === "object" ? Object.values(x).filter(Boolean).join(" ") : x)).join("、") : "（空）") : val === true ? "开" : val === false ? "关" : String(val ?? "");
      return `<span class="mono cfg-ro">${esc(shown)}</span>`;
    }
    if (kind === "accounts" || kind === "groups" || kind === "times") return chipEditor(id, kind, val);
    if (kind === "serve_groups" || kind === "ssh_list") return rowsEditor(id, ROWS_COLS[kind], val);
    if (kind === "bool") return `<label class="switch"><input type="checkbox" id="${id}" ${val ? "checked" : ""} /><span></span></label>`;
    if (kind === "enum") {
      const opts = (f.options || []).map((o) => (typeof o === "object" ? [o.value, o.label || o.value] : [o, o === "" ? "不用" : o]));
      return `<select id="${id}">${opts.map(([v, l]) => `<option value="${esc(v)}" ${v === val ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
    }
    if (kind === "int" || kind === "float") {
      const lim = `${f.min != null ? ` min="${f.min}"` : ""}${f.max != null ? ` max="${f.max}"` : ""}`;
      return `<input id="${id}" type="number" step="${kind === "int" ? 1 : 0.1}"${lim} value="${esc(val ?? "")}" />`;
    }
    if (kind === "list_str") return `<textarea id="${id}" class="cfg-list" rows="3" spellcheck="false" placeholder="一行一个">${esc((val || []).join("\n"))}</textarea>`;
    if (kind === "secret") {
      const src = SRC_NAMES[f.source] || "";
      const pw = f.key === "console.password";
      return `<input id="${id}" type="password" autocomplete="new-password" placeholder="${f.set ? `已设置${src ? `（${src}）` : ""} · 留空就不改` : "没设置 · 粘贴进来"}" />${
        pw ? `<input id="${id}-cur" type="password" autocomplete="current-password" placeholder="改密码要先填现在的密码" style="margin-top:8px" />` : ""
      }${f.source === "file" && f.set && !pw ? `<button type="button" class="link-btn" data-act="cfg-clear" data-f="${esc(f.key)}">清空</button>` : ""}`;
    }
    return `<input id="${id}" spellcheck="false" value="${esc(val ?? "")}" />`;
  }

  function cfgShow(v) {
    if (Array.isArray(v)) return v.length ? v.map((x) => (typeof x === "object" && x ? Object.values(x).filter(Boolean).join(" ") : x)).join("、") : "（空）";
    if (v === true) return "开";
    if (v === false) return "关";
    return v === "" || v == null ? "（空）" : String(v);
  }

  function rulesPage() {
    const r = state.rules;
    if (!r) return loading();
    const secs = r.sections || [];
    const pending = r.reload_pending || [];
    const jump = state.cfgSec && secs.some((s) => s.id === state.cfgSec) ? state.cfgSec : "";
    const shown = jump ? secs.filter((s) => s.id === jump) : secs;
    return `
      <p class="h-meta">插件配置文件（${esc(r.file || "config.toml")}）里的每一项都在这里。点「保存」直接写进这个文件，MaiWork 马上按新值来，不用重启、也不会连带其他插件。标「改过」的是和默认值不一样的，点「恢复默认」退回去。</p>
      ${pending.length ? `<div class="warn-box">有 ${pending.length} 项改了还没生效（${pending.map((k) => esc((cfgField(k) || {}).label || k)).join("、")}）：要等插件下次重载。重载会让 MaiBot 的全部插件一起重载，请你挑空闲时自己做。</div>` : ""}
      <div class="cfg-jump" role="tablist"><button type="button" role="tab" aria-selected="${!jump}" data-act="cfg-sec" data-s="">全部</button>${secs.map((s) => `<button type="button" role="tab" aria-selected="${jump === s.id}" data-act="cfg-sec" data-s="${esc(s.id)}">${esc(s.label)}</button>`).join("")}</div>
      <datalist id="mw-platforms">${Object.keys(PLATFORM_NAMES).map((p) => `<option value="${p}">${PLATFORM_NAMES[p]}</option>`).join("")}</datalist>
      <form id="rules-form" class="rules-form" autocomplete="off">
        ${shown
          .map(
            (s) => `
          <h2 class="h-sub" id="cfg-${esc(s.id)}">${esc(s.label)}</h2>
          ${(s.fields || [])
            .map((f) => {
              const kind = cfgKind(f);
              const wide = ["accounts", "groups", "times", "serve_groups", "ssh_list", "list_str"].includes(kind);
              const over = kind !== "secret" && f.changed;
              return `
              <div class="rule-row${kind === "bool" && !f.readonly ? " is-bool" : ""}${wide ? " is-list" : ""}">
                <div class="rule-l">
                  <label for="${cfgId(f.key)}" class="set-name">${esc(f.label || f.key)}${over ? `<span class="tag">改过</span>` : ""}${f.applies === "reload" ? `<span class="tag tag-warn">重载后生效</span>` : ""}</label>
                  ${f.help ? `<div class="set-text">${esc(f.help)}</div>` : ""}
                  ${f.readonly ? `<div class="set-text">${SVG.lock} 只能在配置文件里改${f.readonly_reason ? `：${esc(f.readonly_reason)}` : ""}</div>` : ""}
                  ${over && !f.readonly ? `<div class="set-text">默认是：<span class="mono">${esc(cfgShow(f.default))}</span> <button type="button" class="link-btn" data-act="rule-reset" data-f="${esc(f.key)}">恢复默认</button></div>` : ""}
                </div>
                <div class="rule-r">${cfgInput(f)}</div>
              </div>`;
            })
            .join("")}`
          )
          .join("")}
        <p class="err" id="r-err" hidden></p>
        <div class="actions sticky-actions"><button class="btn primary" type="submit">保存</button></div>
      </form>`;
  }

  // 只收改动过的字段（没动的不发，免得撞上「只能在文件里改」的旧值）
  function readRules() {
    const out = {};
    for (const s of (state.rules && state.rules.sections) || []) {
      for (const f of s.fields || []) {
        if (f.readonly) continue;
        const kind = cfgKind(f);
        const el = $(cfgId(f.key));
        if (!el) continue;
        let v;
        if (kind === "secret") {
          v = el.value;
          if (!v) continue;
          out[f.key] = v;
          if (f.key === "console.password") {
            const cur = ($(`${cfgId(f.key)}-cur`) || {}).value || "";
            if (!cur) throw new Error("改网页密码要先填现在的密码");
            out.current_password = cur;
          }
          continue;
        }
        if (kind === "bool") v = el.checked;
        else if (kind === "accounts" || kind === "groups" || kind === "times") v = [...el.querySelectorAll(".ci")].map((c) => c.dataset.v);
        else if (kind === "serve_groups" || kind === "ssh_list")
          v = [...el.querySelectorAll(".re-row")]
            .map((row) => Object.fromEntries([...row.querySelectorAll("input")].map((i) => [i.dataset.k, i.value.trim()])))
            .filter((o) => Object.values(o).some(Boolean));
        else if (kind === "int") v = el.value.trim() === "" ? NaN : parseInt(el.value, 10);
        else if (kind === "float") v = el.value.trim() === "" ? NaN : parseFloat(el.value);
        else if (kind === "list_str") v = el.value.split(/\n/).map((x) => x.trim()).filter(Boolean);
        else v = el.value.trim();
        if ((kind === "int" || kind === "float") && !Number.isFinite(v)) throw new Error(`「${f.label || f.key}」要填数字`);
        if (JSON.stringify(v) !== JSON.stringify(f.value)) out[f.key] = v;
      }
    }
    return out;
  }

  /* ───── 用量：按天看历史 ───── */
  async function loadUsage(days) {
    const u = (state.usage = state.usage || { days: 30, sel: "" });
    if (days) u.days = days;
    try {
      u.hist = await api("GET", `/api/usage/history?days=${u.days}`);
      const list = (u.hist && u.hist.days) || [];
      if (!u.sel || !list.some((d) => d.day === u.sel)) u.sel = list.length ? list[list.length - 1].day : "";
      if (u.sel) await loadUsageDay(u.sel);
    } catch (e) {
      u.hist = null;
      toast(e.message, true);
    }
  }
  async function loadUsageDay(day) {
    const u = state.usage;
    u.sel = day;
    u.detail = null;
    try {
      u.detail = await api("GET", `/api/usage/history?date=${encodeURIComponent(day)}`);
      if (u.detail && !u.detail.day) u.detail.day = day;
    } catch (e) {
      toast(e.message, true);
    }
  }

  const md = (day) => String(day || "").slice(5).replace("-", "/");

  function usagePage() {
    const u = state.usage;
    if (!u || !u.hist) return loading();
    const list = u.hist.days || [];
    const t = u.hist.totals || {};
    const max = Math.max(1, ...list.map((d) => (d.main || 0) + (d.worker || 0)));
    const bars = list
      .map((d) => {
        const tot = (d.main || 0) + (d.worker || 0);
        const hm = ((d.main || 0) / max) * 100;
        const hw = ((d.worker || 0) / max) * 100;
        return `<button type="button" class="ub${u.sel === d.day ? " on" : ""}" data-act="usage-day" data-d="${esc(d.day)}" title="${esc(d.day)} · ${tokens(tot)} tokens · ${d.calls || 0} 次调用${d.errors ? ` · ${d.errors} 次出错` : ""}">
          <span class="ub-col"><span class="ub-w" style="height:${hw}%"></span><span class="ub-m" style="height:${hm}%"></span></span>
          <span class="ub-x">${esc(md(d.day))}</span></button>`;
      })
      .join("");
    return `
      <div class="h-sub-row"><h2 class="h-sub" style="margin-top:14px">最近 ${u.days} 天</h2>
        <span class="seg" role="tablist" style="margin:0">${[7, 14, 30].map((n) => `<button role="tab" aria-selected="${u.days === n}" data-act="usage-range" data-n="${n}">${n} 天</button>`).join("")}</span></div>
      <div class="usage usage-4">
        <div><b>${tokens(t.main)}</b><span>主模型 · tokens</span></div>
        <div><b>${tokens(t.worker)}</b><span>子 agent · tokens</span></div>
        <div><b>${t.calls || 0}</b><span>模型调用 · 次${t.errors ? `（出错 ${t.errors}）` : ""}</span></div>
        <div><b>${t.jev || 0}</b><span>Jev 判断 · 次</span></div>
      </div>
      <div class="ubars" style="--n:${list.length}">${bars}</div>
      <p class="fine"><span class="lg lg-m"></span>主模型 <span class="lg lg-w"></span>子 agent · 点一根柱子看那天的明细</p>
      <label class="u-pick"><span>直接看某一天</span><input type="date" class="u-date" value="${esc(u.sel || "")}" min="${esc((list[0] || {}).day || "")}" max="${esc((list[list.length - 1] || {}).day || "")}" /></label>
      ${usageDay(u)}`;
  }

  function usageDay(u) {
    const d = u.detail;
    if (!u.sel) return "";
    if (!d) return `<h2 class="h-sub">${esc(u.sel)}</h2>` + loading();
    const tbl = (head, rows) =>
      rows.length ? `<div class="utbl" style="--c:${head.length - 1}"><div class="utr uth">${head.map((h) => `<span>${h}</span>`).join("")}</div>${rows.map((r) => `<div class="utr">${r.map((c) => `<span>${c}</span>`).join("")}</div>`).join("")}</div>` : `<p class="h-meta">没有。</p>`;
    const hours = d.by_hour || [];
    const hmax = Math.max(1, ...hours);
    return `
      <h2 class="h-sub">${esc(d.day)} 的明细</h2>
      <div class="uhours">${hours.map((n, h) => `<span title="${h} 点 · ${tokens(n)} tokens" style="height:${Math.max(n ? 6 : 2, (n / hmax) * 100)}%"></span>`).join("")}</div>
      <div class="uhours-x"><span>0 点</span><span>6</span><span>12</span><span>18</span><span>23</span></div>
      <h3 class="h-mini">按模型</h3>
      ${tbl(["模型", "调用", "输入", "输出", "出错", "平均耗时"], (d.by_model || []).map((m) => [`<span class="mono">${esc(m.model || "?")}</span> <small>${esc(ROLE_NAMES[m.role] || m.role || "")}</small>`, m.calls || 0, tokens(m.prompt), tokens(m.completion), m.errors || 0, fmtMs(m.avg_ms)]))}
      <h3 class="h-mini">按用途</h3>
      ${tbl(["用途", "调用", "tokens"], (d.by_purpose || []).map((p) => [esc(p.purpose || "其他"), p.calls || 0, tokens(p.tokens)]))}
      <h3 class="h-mini">按群</h3>
      ${tbl(["群", "调用", "tokens"], (d.by_group || []).map((g) => [esc(g.name || (g.group_id ? `群 ${g.group_id}` : "不属于哪个群")), g.calls || 0, tokens(g.tokens)]))}
      <p class="fine">这天 Jev 判断了 ${d.jev || 0} 次。</p>`;
  }

  /* ───── 资讯来源：屏蔽 + RSS ───── */
  function sourcesPage(s) {
    const rss = ((s.feeds || {}).rss) || {};
    const groups = s.groups || [];
    return `
      <h2 class="h-sub" style="margin-top:18px">RSS <small>指定要看的来源，更准</small></h2>
      <p class="h-meta">每轮备料会先看这里的新文章，和搜到的一起过同一套质量标准，不合格的照样不上。每个群最多 20 个。</p>
      ${
        groups.length
          ? groups
              .map((g) => {
                const list = rss[g.id] || [];
                return `
          <div class="rss-group">
            <div class="rss-gname">${mq(g.name || `群 ${g.id}`)}</div>
            ${list
              .map(
                (f) => `
              <div class="set-row">${ico("newspaper")}<div><div class="set-name">${esc(f.title || f.url)}</div><div class="set-text mono-link">${esc(f.url)}</div>${f.last_error ? `<div class="set-text bad-t">上次没取到：${esc(f.last_error)}</div>` : f.last_ok_ts ? `<div class="set-text">${esc(dayWord(f.last_ok_ts))}取过</div>` : ""}</div>
                <span class="row-btns"><button class="btn small" data-act="rss-toggle" data-g="${esc(g.id)}" data-id="${esc(f.id)}" data-on="${f.enabled ? "0" : "1"}">${f.enabled ? "停用" : "启用"}</button><button class="btn small" data-act="rss-del" data-g="${esc(g.id)}" data-id="${esc(f.id)}">删掉</button></span></div>`
              )
              .join("")}
            <form class="rss-add" data-g="${esc(g.id)}" autocomplete="off">
              <input type="url" inputmode="url" spellcheck="false" placeholder="https://…/feed.xml" />
              <button class="btn small" type="submit">加上</button>
            </form>
          </div>`;
              })
              .join("")
          : `<p class="h-meta">还没有服务群。</p>`
      }
      <h2 class="h-sub">屏蔽的来源</h2>
      ${feedsSettings(s.feeds, true)}`;
  }

  /* ───── 身份：SOUL / AGENTS / 工作记忆 ───── */
  const AV_SRC = { custom: "你自定义的", qq: "跟 MaiBot 的 QQ 头像同步", platform: "跟 MaiBot 在其他平台的头像同步", default: "默认头像（MaiBot 没有 QQ 账号可同步）" };
  function avatarBlock() {
    const a = state.avatarCfg;
    const src = a ? AV_SRC[a.source] || "" : "";
    return `
      <h2 class="h-sub">头像</h2>
      <div class="av-row">
        <img class="top-avatar av-big" src="${esc((a && a.url) || avatar())}" alt="" onerror="this.onerror=null;this.src='/static/assets/bot.jpg'" />
        <div class="av-main">
          <div class="set-text">网页左上角和各处 MaiWork 的头像。默认跟 MaiBot 的 QQ 头像同步；没有 QQ 就用其他平台的。</div>
          ${a ? `<div class="set-text">现在：<b>${esc(src)}</b></div>` : ""}
          <div class="row-btns" style="margin-top:8px">
            <label class="btn small file-btn">上传图片<input type="file" id="avatar-file" accept="image/png,image/jpeg,image/webp,image/gif" hidden /></label>
            <button type="button" class="btn small" data-act="avatar-url">用网址</button>
            ${a && a.source === "custom" ? `<button type="button" class="btn small" data-act="avatar-reset">恢复同步</button>` : ""}
          </div>
          <p class="fine" style="margin:4px 0 0">图片最大 2MB，png / jpg / webp / gif。</p>
        </div>
      </div>`;
  }
  async function avatarSaved(a) {
    state.avatarCfg = a;
    if (state.me && state.me.bot && a && a.url) state.me.bot.avatar = a.url;
    render();
    // 引导里的头像步骤：原地换图 + 刷新按钮（不重播整页动画）
    if (onb.open && ONB_STEPS[onb.i].id === "look") {
      const pane = document.querySelector("#onb .onb-pane");
      if (pane) pane.innerHTML = onbPane("look");
      toast("头像换好了");
    }
  }

  async function loadIdentity() {
    api("GET", "/api/settings/avatar")
      .then((a) => ((state.avatarCfg = a), state.setSub === "identity" && repaintSheet()))
      .catch(() => (state.avatarCfg = null));
    try {
      state.identity = await api("GET", "/api/identity");
    } catch (e) {
      state.identity = null;
      toast(e.message, true);
    }
  }

  function idBlock(kind, title, lead, item, limit, extra) {
    item = item || {};
    const text = item.text || "";
    return `
      <form class="id-form login" data-kind="${kind}" autocomplete="off">
        <div class="h-sub-row"><h2 class="h-sub">${title}</h2>${extra || ""}</div>
        <p class="h-meta" style="margin-top:-6px">${lead}</p>
        <textarea class="mono-area id-text" rows="10" spellcheck="false" data-limit="${limit || 16384}">${esc(text)}</textarea>
        <div class="id-foot"><span class="fine id-count">${new Blob([text]).size} / ${limit || 16384} 字节${item.updated_ts ? ` · ${esc(dayWord(item.updated_ts))}改过` : ""}</span><button class="btn primary small" type="submit">保存</button></div>
      </form>`;
  }

  function identityPage(s) {
    const d = state.identity;
    if (!d) return loading();
    const lim = d.limits || {};
    const gm = d.group_memory || {};
    const groups = s.groups || [];
    return `
      <p class="h-meta">MaiWork 自己的身份和记忆，存在服务器数据目录里，改了下一次就生效。</p>
      ${avatarBlock()}
      ${idBlock("soul", "SOUL.md", "MaiWork 说话的口吻和性格：写资讯、开场白、交付说明时都照这个来。" + (d.soul && d.soul.synced_from_maibot ? "（现在是从 MaiBot 同步来的）" : ""), d.soul, lim.soul, `<button type="button" class="btn small" data-act="soul-sync">从 MaiBot 同步</button>`)}
      ${idBlock("agents", "AGENTS.md", "给 MaiWork 主模型和子 agent 的做事规矩：派活、干活、验收时都会看。", d.agents, lim.agents)}
      ${idBlock("memory", "工作记忆（全局）", "和具体群、具体人无关的经验：你的偏好、哪些工具和来源好用、做事的教训。所有群都会用到，所以这里不能写具体的群和人。", d.memory, lim.memory)}
      <h2 class="h-sub">每个群的工作记忆</h2>
      <p class="h-meta" style="margin-top:-6px">MaiWork 在这个群干活学到的东西（这个群喜欢什么形式、哪类资讯被点没用）。只在这个群里用，不会带到别的群。</p>
      ${
        groups.length
          ? groups
              .map((g) => idBlock(`group:${g.id}`, esc(g.name || `群 ${g.id}`), "", gm[g.id], lim.group_memory || 16384))
              .join("")
          : `<p class="h-meta">还没有服务群。</p>`
      }`;
  }

  /* ───── 请求日志（管理员） ───── */
  async function loadLogs(more) {
    const L = (state.logs = state.logs || { tab: "model", failed: false, items: [], next: null, summary: null, open: {} });
    const base = L.tab === "model" ? "/api/logs/model-calls" : "/api/logs/tool-calls";
    const q = new URLSearchParams({ limit: "50" });
    if (L.failed) q.set("failed", "1");
    if (more && L.next) q.set("before_id", String(L.next));
    try {
      const [list, summary] = await Promise.all([api("GET", `${base}?${q}`), more ? Promise.resolve(L.summary) : api("GET", "/api/logs/summary")]);
      L.items = more ? L.items.concat(list.items || []) : list.items || [];
      L.next = list.next_before_id || null;
      L.summary = summary;
    } catch (e) {
      toast(e.message, true);
    }
  }

  const fmtMs = (ms) => (ms == null ? "" : ms >= 1000 ? `${(ms / 1000).toFixed(1)} 秒` : `${ms} 毫秒`);
  const hms = (ts) => {
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  };

  function logsPage() {
    const L = state.logs;
    if (!L || !L.summary) return loading();
    const t = (L.summary && L.summary.today) || {};
    const lf = L.summary.last_failure;
    return `
      <p class="h-meta">最近的模型调用和工具调用，排查问题用。只有管理员看得到；密钥不会出现在这里。模型调用记录留 3 天。</p>
      <div class="usage" style="margin-top:14px">
        <div><b>${t.calls || 0}</b><span>今天调用模型 · 次</span></div>
        <div><b class="${t.failed ? "bad-t" : ""}">${t.failed || 0}</b><span>失败 · 次</span></div>
        <div><b>${t.retried || 0}</b><span>重试 · 次</span></div>
      </div>
      ${lf ? `<div class="warn-box" style="margin-top:12px"><b>最近一次失败：</b>${esc(hms(lf.ts))} · ${esc(lf.purpose_name || lf.purpose || "")} · ${esc(lf.model || "")}<br />${esc(lf.error || "")}</div>` : ""}
      <div class="log-bar">
        <div class="seg" role="tablist">
          <button role="tab" data-act="log-tab" data-t="model" aria-selected="${L.tab === "model"}">模型调用</button>
          <button role="tab" data-act="log-tab" data-t="tool" aria-selected="${L.tab === "tool"}">工具调用</button>
        </div>
        <label class="chk"><input type="checkbox" data-act="log-failed" ${L.failed ? "checked" : ""} />只看失败</label>
        <button class="btn small" data-act="log-refresh">刷新</button>
      </div>
      <div class="logs">
        ${L.items.length ? L.items.map((it) => (L.tab === "model" ? modelLogRow(it) : toolLogRow(it))).join("") : `<p class="h-meta">${L.failed ? "没有失败记录。" : "还没有记录。"}</p>`}
      </div>
      ${L.next ? `<div class="actions" style="justify-content:center"><button class="btn small" data-act="log-more">再往前看</button></div>` : ""}`;
  }

  function modelLogRow(it) {
    const open = state.logs.open[`m${it.id}`];
    const tok = (it.prompt_tokens || 0) + (it.completion_tokens || 0);
    return `
      <div class="log-row ${it.ok ? "" : "bad"}">
        <button class="log-head" data-act="log-open" data-kind="m" data-id="${it.id}" aria-expanded="${!!open}">
          <span class="dot ${it.ok ? "ok" : "failed"}"></span>
          <span class="log-main">
            <span class="log-title">${esc(it.purpose_name || it.purpose || "模型调用")}${it.attempt > 1 ? `<span class="tag">第 ${it.attempt} 次尝试</span>` : ""}</span>
            <span class="log-sub">${esc(hms(it.ts))} · ${esc(it.model || "")} · ${esc(fmtMs(it.ms))}${tok ? ` · ${tokens(tok)} tokens` : ""}${it.group_name ? ` · ${esc(it.group_name)}` : ""}${it.task_id ? ` · ${esc(it.task_id)}` : ""}</span>
            ${!it.ok && it.error ? `<span class="log-err">${esc(it.error)}</span>` : ""}
          </span>
          ${SVG.down}
        </button>
        ${open ? `<div class="log-detail">${open === "loading" ? loading() : modelLogDetail(open)}</div>` : ""}
      </div>`;
  }

  function modelLogDetail(d) {
    const req = d.request || {};
    const res = d.response || {};
    const msgs = (req.messages || [])
      .map(
        (m) => `
        <div class="lm">
          <div class="lm-role">${esc({ system: "系统提示", user: "发给模型", assistant: "模型", tool: "工具结果" }[m.role] || m.role)}${m.name ? ` · ${esc(m.name)}` : ""}</div>
          ${m.content ? `<pre class="lm-text">${esc(m.content)}</pre>` : ""}
          ${(m.tool_calls || []).map((tc) => `<pre class="lm-text lm-tc">调用 ${esc(tc.name || "")}：${esc(tc.arguments || "")}</pre>`).join("")}
        </div>`
      )
      .join("");
    return `
      ${d.error ? `<div class="warn-box">${esc(d.error)}${d.status ? `（HTTP ${d.status}）` : ""}</div>` : ""}
      <div class="ld-meta">${req.json_mode ? "要求返回 JSON · " : ""}${(req.tools || []).length ? `可用工具：${req.tools.map(esc).join("、")}` : "没给工具"}</div>
      <h3 class="ld-h">请求</h3>
      ${msgs || `<p class="h-meta">（没记下）</p>`}
      <h3 class="ld-h">回复</h3>
      ${
        d.ok
          ? `${res.text ? `<pre class="lm-text">${esc(res.text)}</pre>` : ""}${(res.tool_calls || []).map((tc) => `<pre class="lm-text lm-tc">调用 ${esc(tc.name || "")}：${esc(tc.arguments || "")}</pre>`).join("")}${!res.text && !(res.tool_calls || []).length ? `<p class="h-meta">（空）</p>` : ""}`
          : `<p class="h-meta">这次没拿到回复。</p>`
      }`;
  }

  function toolLogRow(it) {
    const open = state.logs.open[`t${it.id}`];
    return `
      <div class="log-row ${it.ok ? "" : "bad"}">
        <button class="log-head" data-act="log-open" data-kind="t" data-id="${it.id}" aria-expanded="${!!open}">
          <span class="dot ${it.ok ? "ok" : "failed"}"></span>
          <span class="log-main">
            <span class="log-title mono">${esc(it.tool || "")}<span class="tag">${esc(it.actor || "")}</span></span>
            <span class="log-sub">${esc(hms(it.ts))} · ${esc(fmtMs(it.ms))}${it.group_name ? ` · ${esc(it.group_name)}` : ""}${it.task_id ? ` · ${esc(it.task_id)}` : ""}</span>
            <span class="log-in">${esc(it.input || "")}</span>
            ${!it.ok && it.error ? `<span class="log-err">${esc(it.error)}</span>` : ""}
          </span>
          ${SVG.down}
        </button>
        ${
          open
            ? `<div class="log-detail">${
                open === "loading"
                  ? loading()
                  : `<h3 class="ld-h">输入</h3><pre class="lm-text">${esc(open.input || "")}</pre><h3 class="ld-h">${open.ok ? "输出" : "出错"}</h3><pre class="lm-text">${esc((open.ok ? open.output : open.error || open.output) || "")}</pre>`
              }</div>`
            : ""
        }
      </div>`;
  }

  function feedsSettings(f, page) {
    if (!f) return "";
    const manual = f.blocked_domains || [];
    const auto = f.auto_blocked || [];
    return `
      ${page ? `<p class="h-meta">这些网站的内容不会再出现在资讯里。</p>` : `<h2 class="h-sub">屏蔽的资讯来源</h2>`}
      ${
        manual.length || auto.length
          ? [
              ...manual.map((d) => `<div class="set-row">${ico("lock")}<div><div class="set-name mono">${esc(d)}</div><div class="set-text">你屏蔽的</div></div><button class="btn small" data-act="unblock-domain" data-domain="${esc(d)}">解除</button></div>`),
              ...auto.map((d) => `<div class="set-row">${ico("lock")}<div><div class="set-name mono">${esc(d)}</div><div class="set-text">被标「没用」太多次，自动屏蔽</div></div><span></span></div>`),
            ].join("")
          : `<p class="h-meta">还没有。在资讯页「被筛掉的」里可以一键屏蔽来源。</p>`
      }`;
  }

  const fullLink = (g) => (g.link && /^https?:/.test(g.link) ? g.link : `${location.origin}/#/${g.token}/news`);

  function modelsSummary(m, page) {
    m = m || {};
    const backup = (b) => (b ? `备用 ${esc(b)}` : "没设备用");
    const row = (icon, name, text) => `<div class="set-row">${ico(icon)}<div><div class="set-name">${name}</div><div class="set-text">${text}</div></div><span></span></div>`;
    return `
      ${page ? "" : `<div class="h-sub-row"><h2 class="h-sub">模型</h2><button class="btn small" data-act="models">修改</button></div>`}
      ${m.ready ? "" : `<div class="warn-box">还没配好模型，MaiWork 现在不会做任何要用模型的事。点「修改」填上端点和模型。</div>`}
      ${row("link", "端点", `<span class="mono">${esc(m.base_url) || "没填"}</span> · 密钥${m.key_set ? "已填" : "<b>没填</b>"}`)}
      ${row("robot", "主模型", `${m.main ? `<span class="mono">${esc(m.main)}</span>` : "<b>没选</b>"} · ${backup(m.main_backup)}<br>理解群、出资讯和构想、派活和验收`)}
      ${row("tools", "子 agent 模型", `${m.worker ? `<span class="mono">${esc(m.worker)}</span>` : "<b>没选</b>"} · ${backup(m.worker_backup)}<br>真正动手干活`)}
      ${row("sparkles", "Jev", `判断群消息值不值得理的小模型，单独连 TypeSafe · 密钥${m.jev_key_set === false ? "<b>没填</b>" : m.jev_key_set ? "已填" : "在「全部配置」里"} <button type="button" class="link-btn" data-act="cfg-goto" data-s="jev">去填 Jev 密钥</button>`)}`;
  }

  const draft = { models: null }; // 模型表单里临时拉到的列表

  function modelSelect(id, value, list, allowEmpty) {
    const opts = (allowEmpty ? [`<option value="">不用备用</option>`] : [`<option value="" disabled ${value ? "" : "selected"}>选一个模型</option>`]).concat(
      list.map((x) => `<option value="${esc(x)}" ${x === value ? "selected" : ""}>${esc(x)}</option>`)
    );
    // 列表里没有的旧值也保留，避免端点换了以后悄悄丢掉
    if (value && !list.includes(value)) opts.push(`<option value="${esc(value)}" selected>${esc(value)}（端点里没找到）</option>`);
    return `<select id="${id}" name="${id}">${opts.join("")}</select>`;
  }

  function modelsPage() {
    return modelsSummary((state.settings || {}).models, true) + modelsForm();
  }

  function modelsForm() {
    const m = (state.settings && state.settings.models) || {};
    const list = draft.models || m.available || [];
    const checked = draft.models ? `刚刚测过 · 找到 ${draft.models.length} 个模型` : m.checked_at ? `${when(m.checked_at)} 测过 · 找到 ${(m.available || []).length} 个模型` : "";
    return `
      <h2 class="h-sub">修改</h2>
      <p class="h-meta sheet-lead">MaiWork 用自己的模型，不占用 MaiBot 的。填一个 OpenAI 兼容的地址（比如 NewAPI），再选模型。</p>
      <form id="models" class="login" autocomplete="off">
        <label for="m-url">端点地址</label>
        <input id="m-url" name="m-url" type="url" inputmode="url" spellcheck="false" value="${esc(m.base_url || "")}" placeholder="https://…/v1" />
        <label for="m-key" style="margin-top:6px">API 密钥</label>
        <input id="m-key" name="m-key" type="password" autocomplete="new-password" placeholder="${m.key_set ? "已填写 · 留空就不改" : "粘贴密钥"}" />
        <p class="fine" style="margin:0">保存后写进服务器上插件的 config.toml；网页上看不到密钥，只会显示「已填」。</p>
        <div class="actions" style="margin-top:4px"><button class="btn" type="button" data-act="models-test">测试连接</button><span class="fine" id="m-check" style="margin:0;align-self:center">${esc(checked)}</span></div>

        <h2 class="h-sub">主模型</h2>
        <p class="fine" style="margin:0 0 4px">理解群、出资讯和构想、派活和验收。选聪明一点的。</p>
        ${modelSelect("m-main", m.main, list)}
        <label for="m-main-b">出错时换用</label>
        ${modelSelect("m-main-b", m.main_backup, list, true)}

        <h2 class="h-sub">子 agent 模型</h2>
        <p class="fine" style="margin:0 0 4px">真正动手写代码、查资料、做文件。用得最多，选便宜耐用的。</p>
        ${modelSelect("m-worker", m.worker, list)}
        <label for="m-worker-b">出错时换用</label>
        ${modelSelect("m-worker-b", m.worker_backup, list, true)}

        <h2 class="h-sub">失败重试</h2>
        <p class="fine" style="margin:0 0 4px">模型端点连不上、超时、限流或出错时，隔一会儿再试；试完还不行才换备用模型。后台主循环里的调用（比如冷场开场白）只重试 1 次，免得卡住别的事。</p>
        <div class="two-col">
          <div><label for="m-retries">最多重试几次</label><input id="m-retries" type="number" min="0" max="10" step="1" value="${esc(m.retries ?? 5)}" /></div>
          <div><label for="m-retry-delay">每次间隔（秒）</label><input id="m-retry-delay" type="number" min="1" max="60" step="1" value="${esc(m.retry_delay_s ?? 10)}" /></div>
        </div>

        <h2 class="h-sub">请求频率</h2>
        <p class="fine" style="margin:0 0 4px">端点说「请求太多」（429）时，整个端点会先歇一会儿再发（10 秒起，越限越久，最多 2 分钟）。经常被限就把同时请求调成 1，或者设个每分钟上限。</p>
        <div class="two-col">
          <div><label for="m-conc">同时最多几个请求</label><input id="m-conc" type="number" min="1" max="8" step="1" value="${esc(m.max_concurrency ?? 2)}" /></div>
          <div><label for="m-rpm">每分钟最多（0 = 不限）</label><input id="m-rpm" type="number" min="0" max="600" step="1" value="${esc(m.max_rpm ?? 0)}" /></div>
        </div>
        <p class="err" id="m-err" hidden></p>
        <button class="btn primary wide" type="submit">保存</button>
        <p class="fine">保存后下一次调用就用新设置，正在跑的任务不受影响。不会让 MaiBot 重载插件。</p>
      </form>`;
  }

  function loginSheet() {
    return `
      <h1 class="h-page">管理员</h1>
      <p class="h-meta sheet-lead">输入密码后能看到所有群，也能批准任务、修改群画像、查看设置。</p>
      <form id="login" class="login" autocomplete="off">
        <label for="pw">密码</label>
        <input id="pw" name="pw" type="password" autocomplete="current-password" placeholder="管理员密码" />
        <p class="err" ${state.loginError ? "" : "hidden"}>${esc(state.loginError)}</p>
        <button class="btn primary wide" type="submit">进入管理</button>
        <p class="fine">密码在服务器上 MaiWork 的数据目录里（console_password.txt），或者是 config.toml 里 [console] 的 password。</p>
      </form>`;
  }

  function editSheet() {
    const e = state.editing || {};
    const title = e.kind === "focus" ? "加一个关注成员" : e.kind === "pref" ? "这个群想看什么" : e.id ? "改这一条" : `加到「${(CATS.find((c) => c[0] === e.cat) || [, ""])[1]}」`;
    const isFocus = e.kind === "focus";
    return `
      <h1 class="h-page">${esc(title)}</h1>
      <p class="h-meta sheet-lead">${isFocus ? "填这个人的 QQ 号。关注成员的个人画像只有管理员看得到，不会出现在群里。" : e.kind === "pref" ? "一句话写清想看什么、不想看什么，比如「多找自部署和开源硬件的，少一点手机评测」。群友也看得到这句。" : "你改过或加的条目会自动锁定，MaiWork 以后不会改写它。"}</p>
      <form id="edit" class="login" autocomplete="off">
        ${
          isFocus
            ? `<label for="ed-text">QQ 号</label><input id="ed-text" inputmode="numeric" spellcheck="false" placeholder="比如 10001" />`
            : `<label for="ed-text">内容</label><textarea id="ed-text" rows="3" spellcheck="false">${esc(e.text || "")}</textarea>`
        }
        <p class="err" id="ed-err" hidden></p>
        <button class="btn primary wide" type="submit">保存</button>
      </form>`;
  }

  /* ───────────── 和 MaiWork 聊（管理员） ───────────── */
  // 轻量 Markdown：先整体转义，再认少数几种写法（加粗、行内代码、链接、列表、标题、代码块）
  function mdLite(src) {
    const parts = String(src || "").split(/```/);
    return parts
      .map((chunk, i) => {
        if (i % 2 === 1) return `<pre class="md-code">${esc(chunk.replace(/^[a-zA-Z0-9_-]*\n/, ""))}</pre>`;
        const lines = esc(chunk).split("\n");
        let html = "";
        let list = null;
        const inline = (t) =>
          t
            .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
            .replace(/`([^`\n]{1,200})`/g, '<code class="icode">$1</code>')
            .replace(/\[([^\]\n]{1,120})\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
        const flush = () => {
          if (list) html += `</${list}>`;
          list = null;
        };
        for (const raw of lines) {
          const line = raw.trimEnd();
          let m;
          if ((m = line.match(/^\s*[-*•]\s+(.*)$/))) {
            if (list !== "ul") (flush(), (html += "<ul>"), (list = "ul"));
            html += `<li>${inline(m[1])}</li>`;
          } else if ((m = line.match(/^\s*\d+[.)、]\s+(.*)$/))) {
            if (list !== "ol") (flush(), (html += "<ol>"), (list = "ol"));
            html += `<li>${inline(m[1])}</li>`;
          } else if ((m = line.match(/^#{1,4}\s+(.*)$/))) {
            flush();
            html += `<p class="md-h">${inline(m[1])}</p>`;
          } else if (!line.trim()) {
            flush();
          } else {
            flush();
            html += `<p>${inline(line)}</p>`;
          }
        }
        flush();
        return html;
      })
      .join("");
  }

  async function loadChats() {
    try {
      const r = await api("GET", "/api/chat");
      state.chats = r.chats || [];
    } catch (e) {
      state.chats = state.chats || [];
      toast(e.message, true);
    }
  }

  async function loadChat(id, incremental) {
    const C = state.chat && state.chat.id === id && incremental ? state.chat : { id, messages: [], pending: [], running: false, info: null };
    const last = C.messages.length ? C.messages[C.messages.length - 1].id : 0;
    try {
      const r = await api("GET", `/api/chat/${encodeURIComponent(id)}${incremental && last ? `?after=${last}` : ""}`);
      const seen = new Set(C.messages.map((m) => m.id));
      C.messages = C.messages.concat((r.messages || []).filter((m) => !seen.has(m.id)));
      C.pending = r.pending || [];
      C.running = !!r.running;
      C.info = r.chat || C.info;
      state.chat = C;
    } catch (e) {
      if (e.status === 404) {
        state.chat = null;
        state.chatId = null;
      } else toast(e.message, true);
    }
  }

  let chatTimer = null;
  function chatPoll() {
    clearTimeout(chatTimer);
    const C = state.chat;
    if (state.page !== "chat" || !C || !(C.running || (C.pending || []).length)) return;
    chatTimer = setTimeout(async () => {
      const before = C.messages.length;
      const wasRunning = C.running;
      await loadChat(C.id, true);
      if (state.page !== "chat" || !state.chat || state.chat.id !== C.id) return;
      if (state.chat.messages.length !== before || state.chat.running !== wasRunning) {
        paintChat(true);
        if (!state.chat.running) loadChats().then(() => renderSide());
      }
      chatPoll();
    }, C.running ? 1500 : 5000);
  }

  function chatToolMsg(m) {
    const meta = m.meta || {};
    const ok = meta.ok !== false;
    // 要确认的动作：工具层只记了小票，没真做——别画成绿点「做完了」
    const asked = ok && /^已请求管理员确认/.test(String(m.content || ""));
    // 小票还挂着 → 「待你确认」；已经同意/拒绝过 → 灰掉，结果看下面的系统提示
    const pid = asked ? (String(m.content).match(/编号\s*(\d+)/) || [])[1] : null;
    const waiting = asked && ((state.chat && state.chat.pending) || []).some((p) => String(p.id) === String(pid) && p.status === "pending");
    const open = state.chatOpen && state.chatOpen[m.id];
    return `
      <div class="cm-tool ${ok ? "" : "bad"}${asked ? " asked" : ""}${asked && !waiting ? " done" : ""}">
        <button class="cm-tool-h" data-act="chat-tool" data-id="${m.id}" aria-expanded="${!!open}">
          <span class="dot ${asked && waiting ? "pending" : asked ? "" : ok ? "ok" : "failed"}"></span>
          <span class="cm-tool-l">${esc(meta.label || m.name || "用了一个工具")}</span>${asked ? `<span class="cm-tool-s">${waiting ? "待你确认" : "问过你了"}</span>` : ""}
          ${SVG.down}
        </button>
        ${open ? `<pre class="lm-text">${esc(m.content || "")}</pre>` : ""}
      </div>`;
  }

  function chatMsgHTML(m) {
    if (m.role === "user") return `<div class="cm cm-user"><div class="cm-bubble">${esc(m.content || "")}</div></div>`;
    if (m.role === "tool") return chatToolMsg(m);
    if (m.role === "system_note") return `<div class="cm-note">${esc(m.content || "")}</div>`;
    // assistant：有字就显示；只有工具调用没字的，工具卡片自己会出现
    const calls = (m.tool_calls || []).length;
    if (!String(m.content || "").trim()) return calls ? "" : "";
    return `
      <div class="cm cm-bot">
        <img class="cm-ava" src="${esc(avatar())}" onerror="this.onerror=null;this.src='/static/assets/bot.jpg'" alt="" />
        <div class="cm-text md">${mdLite(m.content)}</div>
      </div>`;
  }

  function pendingCard(p) {
    return `
      <div class="cm-pending">
        <div class="cm-pending-h">${ico("lock")}<span>要你确认</span></div>
        <div class="cm-pending-t">${esc(p.summary || p.tool)}</div>
        ${
          p.args && Object.keys(p.args).length
            ? `<details class="cm-pending-d"><summary>看具体参数</summary><pre class="lm-text">${esc(JSON.stringify(p.args, null, 2))}</pre></details>`
            : ""
        }
        <div class="actions">
          <button class="btn primary small" data-act="chat-confirm" data-id="${p.id}" data-ok="1">确认，去做</button>
          <button class="btn small" data-act="chat-confirm" data-id="${p.id}" data-ok="0">不要</button>
        </div>
      </div>`;
  }

  const CHAT_SUGGEST = [
    "这几个群最近怎么样？",
    "今天的资讯为什么这么少？",
    "把 19:00 的备料改成 20:30",
    "帮我盯一下这个群里有没有人问 NAS 的问题",
  ];

  function chatStream() {
    const C = state.chat;
    if (!C) return "";
    const msgs = C.messages.map(chatMsgHTML).join("");
    const pend = (C.pending || []).filter((p) => p.status === "pending").map(pendingCard).join("");
    const empty = !C.messages.length
      ? `<div class="chat-empty">
          <img src="${esc(avatar())}" onerror="this.onerror=null;this.src='/static/assets/bot.jpg'" alt="" />
          <p>直接说你想要什么。我能看各群的情况、改群画像和规则、马上备一批资讯、派任务、设目标……会让群友看到的事和删除会先请你确认。</p>
          <div class="chat-sugs">${CHAT_SUGGEST.map((t) => `<button class="btn small" data-act="chat-suggest" data-t="${esc(t)}">${esc(t)}</button>`).join("")}</div>
        </div>`
      : "";
    const typing = C.running ? `<div class="cm cm-bot cm-typing"><img class="cm-ava" src="${esc(avatar())}" onerror="this.onerror=null;this.src='/static/assets/bot.jpg'" alt="" /><div class="cm-dots"><i></i><i></i><i></i></div></div>` : "";
    return empty + msgs + pend + typing;
  }

  function chatPage() {
    const C = state.chat;
    const info = (C && C.info) || {};
    const groups = state.groups || [];
    return `
      <div class="chat-head">
        <h1 class="h-page">和 MaiWork 聊</h1>
        <div class="chat-tools">
          ${
            C
              ? `<select class="chat-group" data-act="chat-group" aria-label="这段对话主要聊哪个群">
                  <option value="">不限群</option>
                  ${groups.map((g) => `<option value="${esc(g.id)}" ${info.group_id === g.id ? "selected" : ""}>${esc(gname(g))}</option>`).join("")}
                </select>`
              : ""
          }
          <button class="btn small chat-list-btn" data-act="chat-list">对话</button>
          <button class="btn small" data-act="chat-new">新对话</button>
        </div>
      </div>
      <div class="chat-stream" id="chat-stream">${C ? chatStream() : loading()}</div>
      <form id="chat-form" class="chat-composer" autocomplete="off">
        <textarea id="chat-input" rows="1" placeholder="跟 MaiWork 说点什么…" ${C && C.running ? "" : ""}></textarea>
        <button class="chat-send" type="submit" aria-label="发送" ${C && C.running ? "disabled" : ""}>${SVG.send || "↑"}</button>
      </form>`;
  }

  function chatSide() {
    const list = state.chats || [];
    return `
      <div class="h-sub-row" style="margin-top:6px"><h2 class="h-sub">对话</h2><button class="btn small" data-act="chat-new">新对话</button></div>
      ${
        list.length
          ? list
              .map(
                (c) => `
          <button class="chat-item${state.chatId === c.id ? " on" : ""}" data-act="chat-open" data-id="${c.id}">
            <span class="chat-item-t">${esc(c.title || "新对话")}${c.running ? `<span class="dot running"></span>` : ""}</span>
            <span class="chat-item-s">${esc(dayWord(c.updated))}${c.group_name ? ` · ${esc(c.group_name)}` : ""}</span>
          </button>`
              )
              .join("")
          : `<p class="h-meta">还没有对话。</p>`
      }`;
  }

  // 只重画消息区，保住输入框里正在打的字和滚动位置
  function paintChat(stickBottom) {
    const box = $("chat-stream");
    if (!box || state.page !== "chat") return;
    const nearBottom = box.getBoundingClientRect().bottom - window.innerHeight < 160;
    box.innerHTML = state.chat ? chatStream() : loading();
    const send = document.querySelector(".chat-send");
    if (send) send.disabled = !!(state.chat && state.chat.running);
    if (stickBottom || nearBottom) window.scrollTo({ top: document.body.scrollHeight, behavior: stickBottom === "smooth" ? "smooth" : "auto" });
  }

  async function enterChat(id) {
    if (!admin()) return;
    closeSheet();
    await loadChats();
    let cid = id || state.chatId || (state.chats[0] && state.chats[0].id);
    if (!cid) {
      const c = await api("POST", "/api/chat", { group_id: state.g || "" });
      state.chats = [c].concat(state.chats || []);
      cid = c.id;
    }
    state.chatId = cid;
    state.chat = null;
    go({ page: "chat", detail: null });
    await loadChat(cid, false);
    render();
    paintChat(true);
    chatPoll();
  }

  /* ───────────── 渲染 ───────────── */

  const avatar = () => (state.me && state.me.bot && state.me.bot.avatar) || "/static/assets/bot.jpg";
  const botName = () => (state.me && state.me.bot && state.me.bot.name) || "MaiBot";

  function renderTop() {
    if (state.page === "chat") {
      $("top").innerHTML = `
        <img class="top-avatar" src="${esc(avatar())}" onerror="this.onerror=null;this.src='/static/assets/bot.jpg'" alt="${esc(botName())}" />
        <div class="chip">和 MaiWork 聊</div>
        <div class="chip-sub">管理员</div>
        <button class="round-btn" data-act="tab" data-tab="${state.tab}" aria-label="回到群">${SVG.close}</button>`;
      return;
    }
    if (state.page === "settings") {
      $("top").innerHTML = `
        <img class="top-avatar" src="${esc(avatar())}" onerror="this.onerror=null;this.src='/static/assets/bot.jpg'" alt="${esc(botName())}" />
        <div class="chip">设置</div>
        <div class="chip-sub">管理员</div>
        <button class="round-btn" data-act="tab" data-tab="${state.tab}" aria-label="回到群">${SVG.close}</button>`;
      return;
    }
    const g = grp();
    const q = quiet(g);
    $("top").innerHTML = `
      <img class="top-avatar" src="${esc(avatar())}" onerror="this.onerror=null;this.src='/static/assets/bot.jpg'" alt="${esc(botName())}" />
      ${admin() && state.groups.length > 1 ? `<button class="chip" data-act="groups" aria-label="切换群，当前：${esc(gname(g))}">${mq(gname(g))}${SVG.down}</button>` : `<div class="chip">${mq(gname(g))}</div>`}
      <div class="chip-sub"><span class="dot ${q.live ? "ok" : ""}"></span>${esc(q.text)}</div>
      ${
        admin()
          ? `<button class="round-btn left" data-act="chat" aria-label="和 MaiWork 聊">${SVG.chat}</button><button class="round-btn" data-act="settings" aria-label="设置">${SVG.sliders}</button>`
          : `<button class="round-btn" data-act="login" aria-label="管理员登录">${SVG.key}</button>`
      }`;
  }

  const pendingCount = (g) => (g && g.today ? g.today.pending || 0 : 0);

  function renderTabbar() {
    const g = grp();
    const idx = state.page ? -1 : TABS.findIndex((t) => t.id === state.tab);
    const n = pendingCount(g);
    $("tabbar").innerHTML =
      `<span class="tab-pill" style="transform:translateX(${Math.max(0, idx) * 100}%);opacity:${idx < 0 ? 0 : 1}"></span>` +
      TABS.map(
        (t) => `
        <button class="tab" data-act="tab" data-tab="${t.id}" aria-current="${idx >= 0 && t.id === state.tab}">
          ${SVG[t.id]}<span class="sr">${t.label}</span>
          ${admin() && t.id === "tasks" && n ? `<span class="badge">${n}</span>` : ""}
        </button>`
      ).join("");
  }

  function renderRail() {
    const g = grp();
    const list = admin() ? state.groups : g ? [g] : [];
    const n = pendingCount(g);
    $("rail").innerHTML = `
      <div class="brand">
        <img src="${esc(avatar())}" onerror="this.onerror=null;this.src='/static/assets/bot.jpg'" alt="${esc(botName())}" />
        <div class="brand-t"><div class="brand-name">MaiWork</div><div class="brand-sub">${admin() ? "管理员 · 全部群" : esc(botName()) + "的后台"}</div></div>
      </div>
      ${admin() ? `<div class="rail-label">群</div>` : ""}
      ${list
        .map(
          (x) => `
        <button class="r-item" data-act="group" data-g="${esc(x.id)}" aria-current="${x.id === state.g}" title="${esc(gname(x))}">
          ${gface(x)}
          <span class="r-text"><span class="r-name">${mq(gname(x))}</span><span class="r-sub">${esc(quiet(x).text)}</span></span>
          ${admin() && pendingCount(x) ? `<span class="r-badge">${pendingCount(x)}</span><span class="r-dot"></span>` : ""}
        </button>`
        )
        .join("")}
      <div class="rail-sep"></div>
      <div class="rail-label">${mq(gname(g))}</div>
      ${TABS.map(
        (t) => `
        <button class="r-item" data-act="tab" data-tab="${t.id}" aria-current="${!state.page && t.id === state.tab}" title="${t.label}">
          ${SVG[t.id]}<span class="r-text"><span class="r-name">${t.label}</span></span>
          ${admin() && t.id === "tasks" && n ? `<span class="r-badge">${n}</span><span class="r-dot"></span>` : ""}
        </button>`
      ).join("")}
      <div class="rail-foot">
        ${admin() ? `<button class="r-item" data-act="chat" title="和 MaiWork 聊" aria-current="${state.page === "chat"}">${SVG.chat}<span class="r-text"><span class="r-name">和 MaiWork 聊</span></span></button>` : ""}
        ${
          admin()
            ? `<button class="r-item" data-act="settings" title="设置" aria-current="${state.page === "settings"}">${SVG.sliders}<span class="r-text"><span class="r-name">设置</span></span>${state.settings && state.settings.models && !state.settings.models.ready ? `<span class="r-badge">!</span><span class="r-dot"></span>` : ""}</button>`
            : `<button class="r-item" data-act="login" title="管理员">${SVG.key}<span class="r-text"><span class="r-name">管理员</span></span></button>`
        }
      </div>`;
  }

  function upcomingIcon(u) {
    return u.icon || "calendar";
  }

  function renderSide() {
    if (!desktop.matches) return;
    if (state.page === "chat") {
      $("side").innerHTML = chatSide();
      return;
    }
    if (state.page === "settings") {
      $("side").innerHTML = settingsSide();
      return;
    }
    const g = grp();
    if (!g) {
      $("side").innerHTML = "";
      return;
    }
    if (state.detail) {
      $("side").innerHTML = `<button class="side-close" data-act="side-close" aria-label="关闭详情">${SVG.close}</button>${detailHTML()}`;
      return;
    }
    const v = gview();
    const t = g.today || {};
    const up = (v && v.upcoming) || [];
    $("side").innerHTML = `
      ${pulseCard(g, v, true)}
      <h2 class="h-sub">今天</h2>
      <div class="stats">
        <button class="stat" data-act="tab" data-tab="news"><b>${t.news || 0}</b><span>条资讯</span></button>
        <button class="stat" data-act="tab" data-tab="group"><b>${t.topics || 0}</b><span>次开话题</span></button>
        <button class="stat" data-act="tab" data-tab="tasks"><b>${t.pending || 0}</b><span>${admin() ? "件等你批准" : "件等批准"}</span></button>
        <button class="stat" data-act="tab" data-tab="tasks"><b>${t.running || 0}</b><span>件在做</span></button>
      </div>
      ${
        up.length
          ? `<h2 class="h-sub">接下来</h2>${up.map((u) => `<div class="next">${ico(upcomingIcon(u))}<div><div class="next-time">${esc(when(u.at))}</div><div class="next-text">${esc(u.text)}</div></div></div>`).join("")}`
          : ""
      }`;
  }

  // 横向滚动的分栏（设置子页、全部配置的分节）：重绘前记下滚到哪，重绘后从原位置
  // 平滑滚到「选中项居中」——两边被盖住的项能露出来，也不会每点一下就跳回最左
  const TAB_SCROLLERS = [".set-nav", ".cfg-jump"];
  function tabScrolls() {
    const out = {};
    for (const sel of TAB_SCROLLERS) {
      const el = document.querySelector(`#view ${sel}`);
      if (el) out[sel] = el.scrollLeft;
    }
    return out;
  }
  function centerTabs(was) {
    for (const sel of TAB_SCROLLERS) {
      const bar = document.querySelector(`#view ${sel}`);
      if (!bar || bar.scrollWidth <= bar.clientWidth) continue;
      const on = bar.querySelector('[aria-selected="true"]');
      const from = was && sel in was ? was[sel] : null;
      if (from != null) bar.scrollLeft = from;
      if (!on) continue;
      const max = bar.scrollWidth - bar.clientWidth;
      const br = bar.getBoundingClientRect();
      const r = on.getBoundingClientRect();
      const center = bar.scrollLeft + (r.left - br.left) + r.width / 2;
      const target = Math.max(0, Math.min(max, center - bar.clientWidth / 2));
      if (Math.abs(target - bar.scrollLeft) < 2) continue;
      // 第一次出现（没有旧位置）直接就位；之后用平滑滚动，减少动态效果时也直接就位
      bar.scrollTo({ left: target, behavior: from == null || calm() ? "auto" : "smooth" });
    }
  }

  function renderView() {
    if (state.page === "chat") {
      const typed = $("chat-input") ? $("chat-input").value : "";
      $("view").innerHTML = chatPage();
      if (typed && $("chat-input")) $("chat-input").value = typed;
      flash = false;
      return;
    }
    if (state.page === "settings") {
      const was = tabScrolls();
      $("view").innerHTML = settingsPage();
      centerTabs(was);
      flash = false;
      return;
    }
    const g = grp();
    const v = gview();
    const views = { news: viewNews, ideas: viewIdeas, goals: viewGoals, tasks: viewTasks, group: viewGroup };
    const el = $("view");
    el.innerHTML = !v && state.tab !== "group" ? `<h1 class="h-page">${TABS.find((t) => t.id === state.tab).label}</h1>${loading()}` : views[state.tab](g, v);
    if (!flash) el.querySelectorAll(".enter").forEach((x) => x.classList.remove("enter"));
    flash = false;
  }

  function landing() {
    const bad = state.badLink;
    return `
      <div class="landing">
        <img class="top-avatar" src="${esc(avatar())}" onerror="this.onerror=null;this.src='/static/assets/bot.jpg'" alt="" style="width:120px;height:120px" />
        <h1 class="h-page" style="margin-top:22px">MaiWork</h1>
        <p class="landing-text">${esc(botName())}在群里的后台：资讯、构想、目标和任务都在这里。</p>
        ${bad ? `<div class="warn-box" style="width:100%;text-align:left">这个链接打不开了：可能是管理员重置过，或者复制时少了几个字。请在群里重新发 <b>/mw 网页</b> 拿新链接。</div>` : ""}
        <div class="landing-box">
          <div class="landing-t">群友</div>
          <p>在群里发 <b>/mw 网页</b>，${esc(botName())}会回一个本群专属链接，打开就能看到这个群的内容。</p>
        </div>
        <button class="btn primary" data-act="login" style="margin-top:20px;height:48px;padding:0 28px">管理员登录</button>
      </div>`;
  }

  function noGroups() {
    return `
      <div class="landing">
        <img class="top-avatar" src="${esc(avatar())}" onerror="this.onerror=null;this.src='/static/assets/bot.jpg'" alt="" style="width:120px;height:120px" />
        <h1 class="h-page" style="margin-top:22px">还没有服务群</h1>
        <p class="landing-text">在插件的 config.toml 里 [groups] 下加上群号、把 enabled 打开，群就会出现在这里。</p>
        <button class="btn primary" data-act="settings" style="height:48px;padding:0 28px">打开设置</button>
      </div>`;
  }

  function render() {
    const onSettings = admin() && (state.page === "settings" || state.page === "chat");
    const lone = !onSettings && (!state.me || state.me.role === "none" || (admin() && !state.groups.length));
    document.body.classList.toggle("no-link", lone);
    document.body.classList.toggle("on-chat", state.page === "chat");
    document.body.classList.remove("booting");
    if (lone) {
      $("view").innerHTML = admin() ? noGroups() : landing();
      document.title = "MaiWork";
      return;
    }
    renderTop();
    renderTabbar();
    renderRail();
    renderView();
    renderSide();
    if (onSettings) {
      document.title = state.page === "chat" ? "和 MaiWork 聊 · MaiWork" : "设置 · MaiWork";
      return;
    }
    const label = TABS.find((t) => t.id === state.tab).label;
    document.title = `${label} · ${gname(grp())} · MaiWork`;
  }

  /* ───────────── 抽屉 ───────────── */

  const sheetChrome = () => `<div class="grab"></div><button class="sheet-close" data-act="close" aria-label="关闭">${SVG.close}</button>`;

  function sheetHTML(kind) {
    if (kind === "chats") return chatsSheet();
    if (kind === "groups") return groupPicker();
    if (kind === "login") return loginSheet();
    if (kind === "edit") return editSheet();
    return detailHTML();
  }

  function chatsSheet() {
    return `<h1 class="h-page">对话</h1>` + chatSide();
  }

  function openSheet(kind) {
    const was = state.sheet;
    state.sheet = kind;
    const sh = $("sheet");
    sh.innerHTML = sheetChrome() + sheetHTML(kind);
    sh.hidden = false;
    $("scrim").hidden = false;
    sh.scrollTop = 0;
    if (!was) {
      requestAnimationFrame(() => {
        sh.classList.add("on");
        $("scrim").classList.add("on");
      });
    }
    sh.setAttribute("tabindex", "-1");
    sh.focus({ preventScroll: true });
  }

  function repaintSheet() {
    if (state.page === "settings") {
      const y = window.scrollY;
      renderView();
      renderSide();
      renderRail();
      window.scrollTo(0, y);
    }
    if (!state.sheet) return;
    const sh = $("sheet");
    const top = sh.scrollTop;
    sh.innerHTML = sheetChrome() + sheetHTML(state.sheet);
    sh.scrollTop = top;
  }

  function closeSheet() {
    if (!state.sheet) return;
    if (state.sheet === "detail") {
      state.detail = null;
      syncHash();
      renderView();
    }
    state.sheet = null;
    draft.models = null;
    const sh = $("sheet");
    sh.classList.remove("on");
    $("scrim").classList.remove("on");
    setTimeout(() => {
      if (!state.sheet) {
        sh.hidden = true;
        $("scrim").hidden = true;
      }
    }, 420);
  }

  function openDetail(type, id) {
    state.detail = { type, id };
    syncHash();
    if (type === "task") loadTask(id);
    if (desktop.matches) {
      renderView();
      renderSide();
      $("side").scrollTop = 0;
    } else {
      openSheet("detail");
    }
  }

  /* ───────────── 路由 ───────────── */

  function syncHash() {
    if (state.page === "chat") {
      const h = `#/chat/${state.chatId || ""}`;
      if (location.hash !== h) history.replaceState(null, "", h);
      return;
    }
    if (state.page === "settings") {
      const h = `#/settings/${state.setSub}`;
      if (location.hash !== h) history.replaceState(null, "", h);
      return;
    }
    const g = grp();
    if (!g) return;
    const ref = admin() ? g.id : state.ref;
    const h = `#/${ref}/${state.tab}` + (state.detail ? `/${state.detail.id}` : "");
    if (location.hash !== h) history.replaceState(null, "", h);
  }

  function parseHash() {
    const parts = location.hash.replace(/^#\/?/, "").split("/");
    if (parts[0] === "chat") return { chat: true, id: parts[1] || "", ref: "", tab: "", sub: "" };
    if (parts[0] === "settings") return { settings: true, sub: parts[1] || "overview", ref: "", tab: "", id: "" };
    return { ref: decodeURIComponent(parts[0] || ""), tab: parts[1] || "", id: decodeURIComponent(parts[2] || "") };
  }

  function applyHash() {
    const h = parseHash();
    state.page = h.settings && admin() ? "settings" : h.chat && admin() ? "chat" : null;
    if (h.chat && h.id) state.chatId = Number(h.id) || h.id;
    if (h.settings) state.setSub = SET_SUBS.some((x) => x[0] === h.sub) ? h.sub : "overview";
    if (TABS.some((x) => x.id === h.tab)) state.tab = h.tab;
    if (admin()) {
      const hit = state.groups.find((x) => x.id === h.ref || x.token === h.ref);
      state.g = hit ? hit.id : state.g && state.groups.some((x) => x.id === state.g) ? state.g : state.groups.length ? state.groups[0].id : null;
    }
    if (!h.chat && !h.settings && h.id) state.detail = { type: /^G-/.test(h.id) ? "goal" : /^I-/.test(h.id) ? "idea" : "task", id: h.id };
    else state.detail = null;
  }

  async function loadMe() {
    const h = parseHash();
    state.ref = h.ref;
    const me = await api("GET", "/api/me");
    state.me = me;
    if (me.now) state.skew = me.now - Date.now() / 1000;
    state.badLink = me.role === "none" && !!h.ref;
    if (me.role === "member") state.g = me.group;
  }

  async function loadGroups() {
    if (!state.me || state.me.role === "none") {
      state.groups = [];
      return;
    }
    state.groups = (await api("GET", "/api/groups")) || [];
  }

  async function loadView(quietly) {
    const g = grp();
    if (!g) return;
    const id = g.id;
    try {
      const v = await api("GET", `/api/groups/${encodeURIComponent(groupRef(g))}`);
      if (state.g !== id) return;
      state.view = v;
      const i = state.groups.findIndex((x) => x.id === id);
      if (i >= 0) state.groups[i] = Object.assign({}, state.groups[i], pickSummary(v));
    } catch (e) {
      if (!quietly) toast(e.message, true);
      if (e.status === 401 || e.status === 403) return reboot();
    }
  }

  const pickSummary = (v) => ({ name: v.name, icon: v.icon, members: v.members, fresh: v.fresh, quiet: v.quiet, today: v.today, read_since: v.read_since });

  async function loadSettings() {
    if (!admin()) return;
    try {
      state.settings = await api("GET", "/api/settings");
    } catch (e) {
      toast(e.message, true);
    }
  }

  async function reboot() {
    state.view = null;
    state.settings = null;
    state.tasks = {};
    try {
      await loadMe();
      await loadGroups();
    } catch (e) {
      state.me = state.me || { role: "none" };
      toast(e.message, true);
    }
    applyHash();
    if (!admin() && state.me && state.me.role === "member") state.g = state.me.group;
    syncHash();
    flash = true;
    render();
    if (grp()) await loadView();
    const settingsReady = admin()
      ? loadSettings().then(async () => {
          if (state.page === "settings" && state.setSub === "extensions") await loadExt();
          if (state.page === "settings" && state.setSub === "rules") await loadRules();
          if (state.page === "settings" && state.setSub === "identity") await loadIdentity();
          if (state.page === "settings" && state.setSub === "logs") await loadLogs();
          renderRail();
          repaintSheet();
        })
      : null;
    flash = true;
    render();
    if (state.detail) {
      if (state.detail.type === "task") loadTask(state.detail.id);
      if (!desktop.matches) openSheet("detail");
    }
    // 网址带 ?open=settings / ?open=models 时直接打开（方便从别处跳过来，也方便截图核对）
    if (admin() && state.page === "chat") enterChat(state.chatId);
    const open = new URLSearchParams(location.search).get("open");
    if (admin() && open === "onboarding") {
      await settingsReady;
      openOnboarding(null);
    } else if (admin()) settingsReady.then(maybeOnboard);
    if (admin() && ["settings", "models", "extensions"].includes(open)) {
      await settingsReady;
      enterSettings(open === "settings" ? "overview" : open);
    }
  }

  async function enterSettings(sub) {
    if (!admin()) return;
    closeSheet();
    state.extEdit = null;
    go({ page: "settings", setSub: sub || state.setSub || "overview", detail: null });
    if (!state.settings) await loadSettings();
    if (state.setSub === "extensions") await loadExt();
    if (state.setSub === "rules") await loadRules();
    if (state.setSub === "usage") await loadUsage();
    if (state.setSub === "identity") await loadIdentity();
    if (state.setSub === "logs") await loadLogs();
    if (state.page === "settings") {
      renderView();
      renderSide();
      renderRail();
    }
  }

  function go(patch) {
    const gChanged = patch.g && patch.g !== state.g;
    Object.assign(state, patch);
    if (gChanged) state.view = null;
    flash = true;
    syncHash();
    render();
    window.scrollTo({ top: 0 });
    if (gChanged) loadView().then(() => ((flash = true), render()));
  }

  /* 定时刷新：页面在前台时每 30 秒拉一次当前群 */
  setInterval(() => {
    if (document.hidden || !grp()) return;
    loadView(true).then(() => {
      if (state.page) return renderRail();
      if (state.sheet && state.sheet !== "detail") return renderTopBits();
      renderTopBits();
      renderView();
      if (!state.detail) renderSide();
    });
  }, 30000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && grp() && !state.page) loadView(true).then(() => (renderTopBits(), renderView(), renderSide()));
  });
  function renderTopBits() {
    if (!grp() && !state.page) return;
    renderTop();
    renderTabbar();
    renderRail();
  }

  document.addEventListener("change", async (e) => {
    if (e.target.id === "sx-tool" && $("sx-extract")) {
      $("sx-extract").innerHTML = extractOpts(e.target.value.split("\u0000")[0], null);
      return;
    }
    if (e.target.classList && e.target.classList.contains("chat-group") && state.chat) {
      try {
        await api("PATCH", `/api/chat/${encodeURIComponent(state.chat.id)}`, { group_id: e.target.value });
        if (state.chat.info) state.chat.info.group_id = e.target.value;
        await loadChats();
        renderSide();
        toast(e.target.value ? "这段对话聚焦在这个群了" : "不限群");
      } catch (err) {
        toast(err.message, true);
      }
      return;
    }
    if (e.target.classList && e.target.classList.contains("u-date") && state.usage) {
      const day = e.target.value;
      if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return;
      loadUsageDay(day).then(repaintSheet);
      repaintSheet();
      return;
    }
    if (e.target.dataset && e.target.dataset.act === "log-failed") {
      state.logs.failed = e.target.checked;
      state.logs.open = {};
      await loadLogs();
      repaintSheet();
      return;
    }
    if (e.target.id === "avatar-file") {
      const file = e.target.files && e.target.files[0];
      e.target.value = "";
      if (!file) return;
      if (file.size > 2 * 1024 * 1024) return toast("图片太大了，最多 2MB", true);
      if (!/^image\/(png|jpeg|webp|gif)$/.test(file.type)) return toast("只支持 png / jpg / webp / gif", true);
      try {
        const data = await new Promise((ok, bad) => {
          const r = new FileReader();
          r.onload = () => ok(String(r.result).split(",")[1] || "");
          r.onerror = () => bad(new Error("读不了这张图"));
          r.readAsDataURL(file);
        });
        await avatarSaved(await api("POST", "/api/settings/avatar", { data, mime: file.type }));
        toast("头像换好了");
      } catch (err) {
        toast(err.message, true);
      }
      return;
    }
    if (e.target.id !== "skill-zip") return;
    const file = e.target.files && e.target.files[0];
    e.target.value = "";
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) return toast("zip 太大了，最多 5MB", true);
    const upload = async (replace) => {
      const res = await fetch(`/api/extensions/skills/upload${replace ? "?replace=1" : ""}`, {
        method: "POST",
        headers: { "Content-Type": "application/zip", "X-Filename": encodeURIComponent(file.name) },
        body: file,
        credentials: "same-origin",
      });
      let data = null;
      try {
        data = await res.json();
      } catch (x) {}
      return { res, data };
    };
    toast("在装…");
    try {
      let { res, data } = await upload(false);
      if (res.status === 409 && confirm(`${(data && data.error) || "已经有同名的 skill 了"}。要替换吗？`)) ({ res, data } = await upload(true));
      if (!res.ok) throw new Error((data && data.error) || `出错了（${res.status}）`);
      await loadExt();
      repaintSheet();
      toast(`装好了：${data.name}，下一次就能用`);
    } catch (err) {
      toast(err.message, true);
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.target.id === "chat-input" && e.key === "Enter" && !e.shiftKey && !e.isComposing && desktop.matches) {
      e.preventDefault();
      $("chat-form").requestSubmit();
      return;
    }
  });
  document.addEventListener("input", (e) => {
    if (e.target.id !== "chat-input") return;
    e.target.style.height = "auto";
    e.target.style.height = `${Math.min(e.target.scrollHeight, 220)}px`;
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || !e.target.closest) return;
    const box = e.target.closest(".chips");
    if (!box || !e.target.closest(".ci-add")) return;
    e.preventDefault();
    chipAdd(box);
  });
  document.addEventListener("input", (e) => {
    if (!e.target.classList || !e.target.classList.contains("id-text")) return;
    const n = new Blob([e.target.value]).size;
    const lim = Number(e.target.dataset.limit) || 16384;
    const c = e.target.closest(".id-form").querySelector(".id-count");
    c.textContent = `${n} / ${lim} 字节`;
    c.classList.toggle("bad-t", n > lim);
  });

  /* 长名字滚动：只在真的放不下时才动；尊重「减少动态效果」 */
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  function mqStart(el) {
    const inner = el.firstElementChild;
    if (!inner || reduceMotion.matches) return;
    const d = inner.scrollWidth - el.clientWidth;
    if (d <= 2) return;
    el.style.setProperty("--mq-d", `${-d}px`);
    el.style.setProperty("--mq-t", `${Math.max(1.6, d / 45).toFixed(2)}s`);
    el.classList.add("mq-on");
  }
  const mqStop = (el) => el.classList.remove("mq-on");
  document.addEventListener("pointerover", (e) => {
    if (e.pointerType !== "mouse") return;
    const el = e.target.closest && e.target.closest(".mq");
    if (el && !el.contains(e.relatedTarget)) mqStart(el);
  });
  document.addEventListener("pointerout", (e) => {
    if (e.pointerType !== "mouse") return;
    const el = e.target.closest && e.target.closest(".mq");
    if (el && !el.contains(e.relatedTarget)) mqStop(el);
  });
  document.addEventListener(
    "touchstart",
    (e) => {
      const host = e.target.closest && e.target.closest(".r-item, .gpick, .chip, .rail-label");
      const el = host && host.querySelector(".mq");
      if (!el) return;
      mqStart(el);
      clearTimeout(el._mqT);
      el._mqT = setTimeout(() => mqStop(el), 4200);
    },
    { passive: true }
  );

  /* ───────────── 交互 ───────────── */

  function patchView(fn) {
    const v = gview();
    if (v) fn(v);
  }
  function findEntry(id) {
    const v = gview();
    for (const s of (v && v.profile) || []) for (const e of s.entries || []) if (String(e.id) === String(id)) return e;
    return null;
  }

  async function act(el, e) {
    const a = el.dataset.act;
    const g = grp();
    switch (a) {
      case "tab":
        closeSheet();
        go({ page: null, tab: el.dataset.tab, detail: null });
        break;
      case "group":
        if (!admin()) break;
        closeSheet();
        go({ page: null, g: el.dataset.g, detail: null, filter: "all" });
        break;
      case "groups":
        openSheet("groups");
        break;
      case "chat":
        await enterChat();
        break;
      case "chat-open":
        closeSheet();
        await enterChat(Number(el.dataset.id));
        break;
      case "chat-list":
        await loadChats();
        openSheet("chats");
        break;
      case "chat-new": {
        try {
          const c = await api("POST", "/api/chat", { group_id: (state.chat && state.chat.info && state.chat.info.group_id) || state.g || "" });
          closeSheet();
          await enterChat(c.id);
          setTimeout(() => $("chat-input") && $("chat-input").focus(), 50);
        } catch (err) {
          toast(err.message, true);
        }
        break;
      }
      case "chat-suggest":
        $("chat-input").value = el.dataset.t;
        $("chat-input").focus();
        break;
      case "chat-tool":
        state.chatOpen[el.dataset.id] = !state.chatOpen[el.dataset.id];
        paintChat(false);
        reveal(`.cm-tool-h[data-id="${el.dataset.id}"]`);
        break;
      case "chat-confirm": {
        el.disabled = true;
        try {
          const r = await api("POST", `/api/chat/pending/${encodeURIComponent(el.dataset.id)}`, { approve: el.dataset.ok === "1" });
          toast(el.dataset.ok === "1" ? (r.status === "done" ? "好，已经去做了" : `没做成：${r.result || ""}`) : "好，这件不做了", r.status === "failed");
          await loadChat(state.chatId, true);
          if (state.chat) state.chat.running = true;
          paintChat(true);
          chatPoll();
        } catch (err) {
          el.disabled = false;
          toast(err.message, true);
          // 409：别的页面已经处理过 / 上一句还在跑 → 重新拉一遍，把过期的确认卡片收掉
          if (err.status === 409 && state.chatId) {
            await loadChat(state.chatId, false);
            paintChat(false);
            chatPoll();
          }
        }
        break;
      }
      case "settings":
        await enterSettings(state.page === "settings" ? state.setSub : "overview");
        break;
      case "set-sub":
        await enterSettings(el.dataset.sub);
        break;
      case "models":
        await enterSettings("models");
        break;
      case "ext-new":
      case "ext-edit": {
        state.extEdit = { kind: el.dataset.kind, name: el.dataset.name || null, data: null };
        repaintSheet();
        if (el.dataset.kind === "skill" && el.dataset.name) {
          try {
            state.extEdit.data = await api("GET", `/api/extensions/skills/${encodeURIComponent(el.dataset.name)}`);
          } catch (err) {
            state.extEdit = null;
            toast(err.message, true);
          }
          repaintSheet();
        }
        const first = document.querySelector(".ext-form input:not([readonly]), .ext-form textarea");
        if (first) first.focus({ preventScroll: false });
        break;
      }
      case "log-tab":
        state.logs.tab = el.dataset.t;
        state.logs.open = {};
        await loadLogs();
        repaintSheet();
        break;
      case "log-refresh":
        state.logs.open = {};
        await loadLogs();
        repaintSheet();
        break;
      case "log-more":
        el.disabled = true;
        await loadLogs(true);
        repaintSheet();
        break;
      case "log-open": {
        const k = `${el.dataset.kind}${el.dataset.id}`;
        if (state.logs.open[k]) {
          delete state.logs.open[k];
          repaintSheet();
          break;
        }
        state.logs.open[k] = "loading";
        repaintSheet();
        reveal(`.log-head[data-kind="${el.dataset.kind}"][data-id="${el.dataset.id}"]`);
        try {
          state.logs.open[k] = await api("GET", `/api/logs/${el.dataset.kind === "m" ? "model-calls" : "tool-calls"}/${encodeURIComponent(el.dataset.id)}`);
        } catch (err) {
          delete state.logs.open[k];
          toast(err.message, true);
        }
        repaintSheet();
        reveal(`.log-head[data-kind="${el.dataset.kind}"][data-id="${el.dataset.id}"]`);
        break;
      }
      case "chip-add":
        chipAdd(el.closest(".chips"));
        break;
      case "chip-del": {
        const list = el.closest(".ci-list");
        el.closest(".ci").remove();
        if (!list.querySelector(".ci")) list.innerHTML = `<span class="ci-empty">还没有</span>`;
        break;
      }
      case "rule-reset": {
        try {
          state.rules = await api("POST", "/api/settings/config/reset", { field: el.dataset.f });
          await loadSettings();
          repaintSheet();
          toast("恢复成默认值了");
        } catch (err) {
          toast(err.message, true);
        }
        break;
      }
      case "cfg-clear": {
        if (!confirm("把 config.toml 里的这个密钥清空？")) break;
        try {
          state.rules = await api("PUT", "/api/settings/config", { [el.dataset.f]: null });
          await loadSettings();
          repaintSheet();
          toast("清掉了");
        } catch (err) {
          toast(err.message, true);
        }
        break;
      }
      case "usage-range":
        await loadUsage(parseInt(el.dataset.n, 10) || 30);
        repaintSheet();
        break;
      case "usage-day":
        loadUsageDay(el.dataset.d).then(repaintSheet);
        repaintSheet();
        break;
      case "avatar-url": {
        const url = (prompt("图片网址（https://…）") || "").trim();
        if (!url) break;
        if (!/^https?:\/\//i.test(url)) {
          toast("要 http:// 或 https:// 开头的网址", true);
          break;
        }
        try {
          await avatarSaved(await api("POST", "/api/settings/avatar", { url }));
          toast("头像换好了");
        } catch (err) {
          toast(err.message, true);
        }
        break;
      }
      case "avatar-reset":
        try {
          await avatarSaved(await api("DELETE", "/api/settings/avatar"));
          toast("恢复成跟 MaiBot 同步了");
        } catch (err) {
          toast(err.message, true);
        }
        break;
      case "cfg-goto":
        state.cfgSec = el.dataset.s || "";
        await enterSettings("rules");
        break;
      case "cfg-sec":
        state.cfgSec = el.dataset.s || "";
        repaintSheet();
        break;
      case "re-add":
        rowsAdd(el.closest(".rows-ed"));
        break;
      case "re-del":
        el.closest(".re-row").remove();
        break;
      case "rss-toggle":
      case "rss-del": {
        const g = el.dataset.g;
        const id = el.dataset.id;
        if (a === "rss-del" && !confirm("删掉这个 RSS 源？")) break;
        try {
          if (a === "rss-del") await api("DELETE", `/api/groups/${encodeURIComponent(g)}/rss/${encodeURIComponent(id)}`);
          else await api("POST", `/api/groups/${encodeURIComponent(g)}/rss/${encodeURIComponent(id)}/toggle`, { enabled: el.dataset.on === "1" });
          await loadSettings();
          repaintSheet();
        } catch (err) {
          toast(err.message, true);
        }
        break;
      }
      case "soul-sync": {
        if (!confirm("用 MaiBot 现在的人格重新生成 SOUL.md？现在这份会先存一个备份。")) break;
        el.disabled = true;
        try {
          const r = await api("POST", "/api/identity/soul/sync", {});
          await loadIdentity();
          repaintSheet();
          toast(r && r.preview_changed === false ? "和 MaiBot 现在的人格一样，没变" : "同步好了");
        } catch (err) {
          el.disabled = false;
          toast(err.message, true);
        }
        break;
      }
      case "news-run": {
        const gid = state.g;
        el.disabled = true;
        try {
          await api("POST", `/api/groups/${encodeURIComponent(gid)}/news/run`, {});
          state.newsRunning = gid;
          renderView();
          toast("开始备料了，一般几分钟，好了会自己出现在这里");
          // 每 20 秒看一眼，最多 15 分钟
          const before = ((gview() || {}).news || []).length ? (gview().news[0].id || 0) : 0;
          let n = 0;
          const tick = async () => {
            n += 1;
            await loadView(true);
            const v = gview();
            const top = v && (v.news || []).length ? v.news[0].id || 0 : 0;
            if (top && top !== before) {
              state.newsRunning = null;
              renderView();
              toast("新的一批资讯到了");
              return;
            }
            if (n >= 45 || state.g !== gid) {
              state.newsRunning = null;
              renderView();
              return;
            }
            setTimeout(tick, 20000);
          };
          setTimeout(tick, 20000);
        } catch (err) {
          el.disabled = false;
          toast(err.message, true);
        }
        break;
      }
      case "mcp-paste":
        state.extEdit = { kind: "mcp-paste", name: null, data: null };
        repaintSheet();
        setTimeout(() => $("mp-text") && $("mp-text").focus(), 50);
        break;
      case "ext-cancel":
        state.extEdit = null;
        repaintSheet();
        break;
      case "hdr-add":
        $("x-hdrs").insertAdjacentHTML("beforeend", headerRow("", false));
        break;
      case "hdr-del": {
        const row = el.closest(".hdr-row");
        if (row.dataset.old === "1") {
          row.dataset.removed = row.dataset.removed === "1" ? "0" : "1";
          row.classList.toggle("removed", row.dataset.removed === "1");
        } else row.remove();
        break;
      }
      case "mcp-test": {
        const out = $("x-check");
        const url = $("x-url").value.trim();
        if (!/^https:\/\/\S+$/.test(url)) {
          out.textContent = "地址要以 https:// 开头";
          out.style.color = "var(--red)";
          break;
        }
        const f = $("mcp-form");
        const { headers } = readHeaders();
        el.disabled = true;
        out.style.color = "";
        out.textContent = "正在连…";
        try {
          const r = await api("POST", "/api/extensions/mcp/test", { url, headers, name: f.dataset.name || undefined });
          out.textContent = r.ok ? `连上了，${(r.tools || []).length} 个工具` : `没连上：${r.error || "未知原因"}`;
          out.style.color = r.ok ? "" : "var(--red)";
        } catch (err) {
          out.textContent = err.message;
          out.style.color = "var(--red)";
        } finally {
          el.disabled = false;
        }
        break;
      }
      case "mcp-toggle": {
        el.disabled = true;
        try {
          await api("POST", `/api/extensions/mcp/${encodeURIComponent(el.dataset.name)}/toggle`, { enabled: el.dataset.on === "1" });
          await loadExt();
          repaintSheet();
          toast(el.dataset.on === "1" ? "打开了" : "关掉了，子 agent 下一次就用不到它");
        } catch (err) {
          el.disabled = false;
          toast(err.message, true);
        }
        break;
      }
      case "search-edit":
        state.searchEdit = true;
        repaintSheet();
        break;
      case "search-cancel":
        state.searchEdit = false;
        repaintSheet();
        break;
      case "search-save": {
        const [mcp, tool] = ($("sx-tool").value || "").split("\u0000");
        el.disabled = true;
        try {
          state.extSearch = await api("PUT", "/api/extensions/search", { mcp, tool, extract_tool: $("sx-extract").value || "" });
          state.searchEdit = false;
          await loadExt();
          if (state.settings) await loadSettings();
          repaintSheet();
          toast(`联网搜索改用 ${mcp} 了，马上生效`);
        } catch (err) {
          el.disabled = false;
          toast(err.message, true);
        }
        break;
      }
      case "search-off": {
        if (!confirm("不用联网搜索？资讯和给关注成员找的内容会暂停找新的，直到重新指定。")) break;
        try {
          await api("DELETE", "/api/extensions/search");
          state.searchEdit = false;
          await loadExt();
          if (state.settings) await loadSettings();
          repaintSheet();
          toast("不联网搜索了");
        } catch (err) {
          toast(err.message, true);
        }
        break;
      }
      case "mcp-del":
      case "skill-del": {
        const isMcp = a === "mcp-del";
        if (!confirm(`删掉「${el.dataset.name}」？${isMcp ? "它的密钥也会一起删。" : "整个 skill 目录都会删掉。"}`)) break;
        try {
          await api("DELETE", `/api/extensions/${isMcp ? "mcp" : "skills"}/${encodeURIComponent(el.dataset.name)}`);
          state.extEdit = null;
          await loadExt();
          repaintSheet();
          toast("删掉了");
        } catch (err) {
          toast(err.message, true);
        }
        break;
      }
      case "models-test": {
        const url = $("m-url").value.trim();
        const out = $("m-check");
        const m = (state.settings && state.settings.models) || {};
        const key = $("m-key").value.trim();
        if (!/^https?:\/\/\S+$/.test(url)) {
          out.textContent = "地址要以 http:// 或 https:// 开头";
          out.style.color = "var(--red)";
          break;
        }
        if (!m.key_set && !key) {
          out.textContent = "先填密钥再测";
          out.style.color = "var(--red)";
          break;
        }
        el.disabled = true;
        out.style.color = "";
        out.textContent = "正在连…";
        try {
          const r = await api("POST", "/api/settings/models/test", { base_url: url, api_key: key || undefined });
          if (r.ok) {
            draft.models = r.models || [];
            // 重画下拉，保留已经填的地址和密钥
            const keep = { url, key, main: $("m-main").value, mb: $("m-main-b").value, w: $("m-worker").value, wb: $("m-worker-b").value };
            repaintSheet();
            $("m-url").value = keep.url;
            $("m-key").value = keep.key;
            for (const [id, val] of [["m-main", keep.main], ["m-main-b", keep.mb], ["m-worker", keep.w], ["m-worker-b", keep.wb]]) if (val) $(id).value = val;
            toast(`连上了，找到 ${draft.models.length} 个模型`);
          } else {
            out.textContent = r.error || "没连上";
            out.style.color = "var(--red)";
          }
        } catch (err) {
          out.textContent = err.message;
          out.style.color = "var(--red)";
        } finally {
          const b = document.querySelector('[data-act="models-test"]');
          if (b) b.disabled = false;
        }
        break;
      }
      case "login":
        state.loginError = "";
        openSheet("login");
        setTimeout(() => $("pw") && $("pw").focus(), 350);
        break;
      case "logout":
        try {
          await api("POST", "/api/logout", {});
        } catch (err) {
          /* 忽略 */
        }
        closeSheet();
        state.me = null;
        state.page = null;
        state.ext = null;
        history.replaceState(null, "", "#/");
        await reboot();
        toast("已退出管理员");
        break;
      case "copy":
        try {
          await navigator.clipboard.writeText(el.dataset.link);
          toast("链接复制好了");
        } catch (err) {
          toast("复制不了，手动选中复制吧", true);
        }
        break;
      case "reset-link": {
        if (!confirm("重置后旧链接马上失效，群友要重新发 /mw 网页 拿新链接。确定重置？")) break;
        try {
          await api("POST", `/api/groups/${encodeURIComponent(el.dataset.g)}/token`, {});
          await Promise.all([loadGroups(), loadSettings()]);
          repaintSheet();
          toast("重置好了，旧链接已失效");
        } catch (err) {
          toast(err.message, true);
        }
        break;
      }
      case "close":
        closeSheet();
        break;
      case "side-close":
        state.detail = null;
        syncHash();
        renderView();
        renderSide();
        break;
      case "fb": {
        const { kind, id, v } = el.dataset;
        const all = myFb();
        const key = `${kind}:${id}`;
        const prev = all[key] || null;
        const next = prev === v ? null : v;
        try {
          const r = await api("POST", `/api/${kind}/${encodeURIComponent(id)}/feedback`, { value: next, prev });
          if (next) all[key] = next;
          else delete all[key];
          localStorage.setItem(FB_KEY, JSON.stringify(all));
          patchView((vw) => {
            const list = kind === "news" ? (vw.news || []).flatMap((b) => b.items || []) : vw.ideas || [];
            const it = list.find((x) => String(x.id) === String(id));
            const nf = r && (r.feedback || (r.up !== undefined ? { up: r.up, down: r.down } : null));
            if (it && nf) it.feedback = nf;
          });
          renderView();
          refreshDetail();
          if (next) toast(next === "up" ? "记下了：有用，以后多找这类" : "记下了：没用，以后少找这类");
        } catch (err) {
          toast(err.message, true);
        }
        break;
      }
      case "idea": {
        const op = el.dataset.op;
        el.disabled = true;
        try {
          await api("POST", `/api/ideas/${encodeURIComponent(el.dataset.id)}/${op}`, {});
          state.ideaMore = null;
          await loadView(true);
          renderTopBits();
          renderView();
          renderSide();
          refreshDetail();
          toast(op === "do" ? "已开工，排进任务了" : op === "want" ? "已经告诉管理员了" : "收起了，以后不再提");
        } catch (err) {
          el.disabled = false;
          toast(err.message, true);
        }
        break;
      }
      case "req": {
        const op = el.dataset.op;
        el.disabled = true;
        try {
          await api("POST", `/api/requests/${encodeURIComponent(el.dataset.id)}/${op}`, {});
          await loadView(true);
          renderTopBits();
          renderView();
          renderSide();
          toast(op === "approve" ? "已批准，会告诉发起的人" : "已拒绝，会告诉发起的人");
        } catch (err) {
          el.disabled = false;
          toast(err.message, true);
        }
        break;
      }
      case "task-op":
      case "goal-op": {
        const kind = a === "task-op" ? "tasks" : "goals";
        const labels = { pause: "已暂停", resume: "继续了", cancel: a === "task-op" ? "已取消" : "不做了", retry: "重新排队了", redeliver: "重新发出去了" };
        if (el.dataset.op === "cancel" && !confirm("确定取消？已经发生的外部操作不会撤回。")) break;
        el.disabled = true;
        try {
          const r = await api("POST", `/api/${kind}/${encodeURIComponent(el.dataset.id)}/${el.dataset.op}`, {});
          if (kind === "tasks" && r && r.id) state.tasks[r.id] = r;
          if (r && r.redeliver_warning) {
            toast(r.redeliver_warning, true);
            el.disabled = false;
            await loadView(true);
            refreshDetail();
            break;
          }
          await loadView(true);
          renderTopBits();
          renderView();
          refreshDetail();
          toast(labels[el.dataset.op] || "好了");
        } catch (err) {
          el.disabled = false;
          toast(err.message, true);
        }
        break;
      }
      case "mention-member": {
        if (!confirm(`让 MaiBot 在群里合适的时候把这条提给 ${el.dataset.name || "ta"}？不会说出 MaiWork 怎么知道 ta 关心这个。`)) break;
        el.disabled = true;
        try {
          const r = await api("POST", `/api/news/${encodeURIComponent(el.dataset.id)}/mention-to-member`, {});
          if (r && r.ok === false) throw new Error("这条的文字碰到了私下画像的内容，没交给 MaiBot");
          el.textContent = "交给 MaiBot 了";
          toast("交给 MaiBot 了，它会在合适的时候提");
        } catch (err) {
          el.disabled = false;
          toast(err.message, true);
        }
        break;
      }
      case "mcp-reload": {
        el.disabled = true;
        try {
          const r = await api("POST", `/api/extensions/mcp/${encodeURIComponent(el.dataset.name)}/reload`, {});
          await Promise.all([loadSettings(), state.page === "settings" ? loadExt() : null]);
          repaintSheet();
          toast(r && r.ok ? `重新连上了，${r.tools || 0} 个工具` : `没连上：${(r && r.error) || "未知原因"}`, !(r && r.ok));
        } catch (err) {
          el.disabled = false;
          toast(err.message, true);
        }
        break;
      }
      case "chat-vote": {
        const id = el.dataset.id;
        const votes = myVotes();
        if (votes[id]) {
          toast("已经说过想聊了");
          break;
        }
        try {
          const r = await api("POST", `/api/news/${encodeURIComponent(id)}/chat-vote`, {});
          votes[id] = 1;
          localStorage.setItem(VOTE_KEY, JSON.stringify(votes));
          patchView((vw) => {
            const it = (vw.news || []).flatMap((b) => b.items || []).find((x) => String(x.id) === String(id));
            if (it && r) it.chat_votes = r.chat_votes;
          });
          renderView();
          toast("记下了：想聊的人多了，MaiBot 会找机会在群里提");
        } catch (err) {
          toast(err.message, true);
        }
        break;
      }
      case "pref-edit":
        state.editing = { kind: "pref", text: (gview() || {}).feeds_pref || "" };
        openSheet("edit");
        setTimeout(() => $("ed-text") && $("ed-text").focus(), 350);
        break;
      case "news-tab":
        state.newsTab = el.dataset.t;
        flash = true;
        renderView();
        break;
      case "rej-toggle":
        state.openRejected = state.openRejected === +el.dataset.id ? null : +el.dataset.id;
        renderView();
        reveal(`.rej-head[data-id="${el.dataset.id}"]`);
        break;
      case "block-domain":
      case "unblock-domain": {
        const blocked = a === "block-domain";
        if (blocked && !confirm(`以后不再从 ${el.dataset.domain} 找资讯？可以在设置里解除。`)) break;
        try {
          await api("POST", "/api/feeds/domains", { domain: el.dataset.domain, blocked });
          if (state.settings) await loadSettings();
          repaintSheet();
          toast(blocked ? `屏蔽了 ${el.dataset.domain}` : `解除了 ${el.dataset.domain}`);
        } catch (err) {
          toast(err.message, true);
        }
        break;
      }
      case "filter":
        state.filter = el.dataset.f;
        renderView();
        break;
      case "task":
        openDetail("task", el.dataset.id);
        break;
      case "goal":
        openDetail("goal", el.dataset.id);
        break;
      case "idea-open":
        state.ideaMore = null;
        openDetail("idea", el.dataset.id);
        break;
      case "idea-more":
        state.ideaMore = state.ideaMore === Number(el.dataset.id) ? null : Number(el.dataset.id);
        refreshDetail();
        break;
      case "idea-copy": {
        const it = findIdea(el.dataset.id);
        if (!it) break;
        try {
          await navigator.clipboard.writeText(ideaAsk(it));
          toast("复制好了：到群里 @MaiBot 粘贴发送");
        } catch (err) {
          toast("复制不了，手动选中复制吧", true);
        }
        break;
      }
      case "verdict": {
        const id = el.dataset.id;
        const t = ((gview() || {}).topic_log || []).find((x) => String(x.id) === String(id));
        const value = t && t.verdict === el.dataset.v ? null : el.dataset.v;
        try {
          await api("POST", `/api/topics/${encodeURIComponent(id)}/verdict`, { value });
          if (t) t.verdict = value;
          renderView();
          if (value) toast("标注好了，用来调冷场判断");
        } catch (err) {
          toast(err.message, true);
        }
        break;
      }
      case "pf-lock": {
        const en = findEntry(el.dataset.id);
        if (!en) break;
        try {
          await api("PATCH", `/api/profile/${en.id}`, { locked: !en.locked });
          en.locked = !en.locked;
          renderView();
          toast(en.locked ? "锁定了，MaiWork 不会再改这条" : "解锁了");
        } catch (err) {
          toast(err.message, true);
        }
        break;
      }
      case "pf-del": {
        const en = findEntry(el.dataset.id);
        if (!en) break;
        try {
          await api("DELETE", `/api/profile/${en.id}`);
          patchView((vw) => vw.profile.forEach((s) => (s.entries = (s.entries || []).filter((x) => x.id !== en.id))));
          renderView();
          toast("删掉了，这条不会再被写回来");
        } catch (err) {
          toast(err.message, true);
        }
        break;
      }
      case "pf-edit": {
        const en = findEntry(el.dataset.id);
        if (!en) break;
        state.editing = { kind: "entry", id: en.id, text: en.text };
        openSheet("edit");
        setTimeout(() => $("ed-text") && $("ed-text").focus(), 350);
        break;
      }
      case "pf-add":
        state.editing = { kind: "entry", cat: el.dataset.cat, text: "" };
        openSheet("edit");
        setTimeout(() => $("ed-text") && $("ed-text").focus(), 350);
        break;
      case "focus-add":
        state.editing = { kind: "focus" };
        openSheet("edit");
        setTimeout(() => $("ed-text") && $("ed-text").focus(), 350);
        break;
      case "focus-rm": {
        const uid = el.dataset.uid;
        try {
          await api("POST", `/api/groups/${encodeURIComponent(g.id)}/focus`, { user_id: uid, action: "remove" });
          patchView((vw) => (vw.focus = (vw.focus || []).filter((p) => p.user_id !== uid)));
          renderView();
          toast("不再关注这个人，TA 的个人画像也删掉了");
        } catch (err) {
          toast(err.message, true);
        }
        break;
      }
    }
  }

  // ───────────── 首次安装引导 ─────────────
  // 全屏一张卡片，一步一屏：打招呼 → 模型 → 服务的群 → 可选密钥 → 管理员 → 完成。
  // 每一步「下一步」时保存这一步；任何一步都能「跳过引导」。动画只动 transform / opacity，
  // 换步时正在播的动画会被立刻收尾（连点不会卡住、不会叠在一起）。
  const ONB_STEPS = [
    { id: "hello", name: "开始" },
    { id: "models", name: "模型" },
    { id: "groups", name: "群" },
    { id: "keys", name: "可选" },
    { id: "admins", name: "管理员" },
    { id: "look", name: "头像" },
    { id: "done", name: "完成" },
  ];
  const onb = { open: false, i: 0, busy: false, cfg: null, models: null, info: null, done: {}, anims: [] };
  const EASE_SPRING = "cubic-bezier(0.34, 1.4, 0.64, 1)";

  const onbField = (key) => {
    for (const s of (onb.cfg && onb.cfg.sections) || []) for (const f of s.fields) if (f.key === key) return f;
    return null;
  };

  function onbPane(id) {
    const m = (state.settings && state.settings.models) || {};
    const bot = (state.me && state.me.bot) || {};
    if (id === "hello")
      return `
        <div class="onb-hero"><img class="onb-avatar" src="${esc(bot.avatar || "/static/assets/bot.jpg")}" alt="" /><span class="onb-spark">${ico("sparkles", "ico")}</span></div>
        <h1 class="onb-title">欢迎用 MaiWork</h1>
        <p class="onb-lead">MaiWork 会读懂你服务的群，主动出资讯、提构想、接活干活。先花两分钟把几样东西配好。</p>
        <ul class="onb-list">
          <li>${ico("robot")}<div><b>模型</b><span>MaiWork 用自己的模型，不占 MaiBot 的。必填。</span></div></li>
          <li>${ico("speech")}<div><b>服务的群</b><span>只在这些群里读消息、做事。</span></div></li>
          <li>${ico("lock")}<div><b>管理员和密钥</b><span>可以先不填，以后在设置里改。</span></div></li>
        </ul>`;
    if (id === "models") {
      const list = onb.models || m.available || [];
      const sel = (sid, v, empty) =>
        `<select id="${sid}">${[empty ? `<option value="">不用备用</option>` : `<option value="" disabled ${v ? "" : "selected"}>${list.length ? "选一个模型" : "先测试连接"}</option>`]
          .concat(list.map((x) => `<option value="${esc(x)}" ${x === v ? "selected" : ""}>${esc(x)}</option>`))
          .concat(v && !list.includes(v) ? [`<option value="${esc(v)}" selected>${esc(v)}</option>`] : [])
          .join("")}</select>`;
      return `
        <div class="onb-step-ico">${ico("robot")}</div>
        <h1 class="onb-title">连上模型</h1>
        <p class="onb-lead">填一个 OpenAI 兼容的地址（比如 NewAPI）和密钥，测一下，再选模型。</p>
        <div class="login onb-form">
          <label for="onb-url">端点地址</label>
          <input id="onb-url" type="url" inputmode="url" spellcheck="false" value="${esc(m.base_url || "")}" placeholder="https://…/v1" />
          <label for="onb-key">API 密钥</label>
          <input id="onb-key" type="password" autocomplete="new-password" placeholder="${m.key_set ? "已填写 · 留空就不改" : "粘贴密钥"}" />
          <div class="onb-test"><button class="btn" type="button" data-act="onb-test">测试连接</button><span class="onb-status" id="onb-status">${list.length ? `<i class="onb-ok">${SVG.check}</i>找到 ${list.length} 个模型` : ""}</span></div>
          <div class="onb-picks ${list.length ? "" : "is-off"}">
            <label for="onb-main">主模型 <span class="fine-inline">理解群、派活验收，选聪明的</span></label>
            ${sel("onb-main", m.main)}
            <label for="onb-worker">子 agent 模型 <span class="fine-inline">真正干活，选便宜耐用的</span></label>
            ${sel("onb-worker", m.worker)}
          </div>
        </div>`;
    }
    if (id === "groups") {
      const f = onbField("groups.serve");
      return `
        <div class="onb-step-ico">${ico("speech")}</div>
        <h1 class="onb-title">服务哪些群</h1>
        <p class="onb-lead">写成 <span class="mono">qq:群号</span>。MaiWork 只在这些群里读消息和做事，别的群一概不碰。</p>
        <div class="login onb-form">${f ? rowsEditor("onb-serve", ROWS_COLS.serve_groups, f.value) : `<p class="fine">读不到配置，先跳过这步。</p>`}</div>
        <p class="fine">改完马上生效，会直接写进插件的 config.toml。</p>`;
    }
    if (id === "keys") {
      const jev = onbField("jev.api_key") || {};
      return `
        <div class="onb-step-ico">${ico("lock")}</div>
        <h1 class="onb-title">可选：快速判断</h1>
        <p class="onb-lead">不填也能用，填了判断群消息会更快。密钥只存服务器上，网页看不到。</p>
        <div class="login onb-form">
          <label for="onb-jev">Jev 密钥 <span class="fine-inline">判断群消息值不值得理</span></label>
          <input id="onb-jev" type="password" autocomplete="new-password" placeholder="${jev.set ? "已设置 · 留空就不改" : "没设置 · 可以不填"}" />
        </div>
        <p class="fine">联网搜索不在这里设：引导结束后，去「设置 → 扩展」接一个能搜索的 MCP，再在最上面的「联网搜索」里选它。</p>`;
    }
    if (id === "admins") {
      const f = onbField("approval.admins");
      return `
        <div class="onb-step-ico">${ico("bell")}</div>
        <h1 class="onb-title">谁是管理员</h1>
        <p class="onb-lead">群友派的活要管理员批准才开工。填上你的 QQ 号，就能在群里和网页上批准。</p>
        <div class="login onb-form">${f ? chipEditor("onb-admins", "accounts", f.value) : `<p class="fine">读不到配置，先跳过这步。</p>`}</div>`;
    }
    if (id === "look") {
      const a = state.avatarCfg;
      const src = (a && a.url) || bot.avatar || "/static/assets/bot.jpg";
      return `
        <div class="onb-hero"><img class="onb-avatar" id="onb-av" src="${esc(src)}" alt="" onerror="this.onerror=null;this.src='/static/assets/bot.jpg'" /></div>
        <h1 class="onb-title">可选：换个头像</h1>
        <p class="onb-lead">默认跟 MaiBot 的 QQ 头像同步。想让网页里的 MaiWork 用别的图，可以在这里换；以后在「设置 → 身份」也能改。</p>
        <div class="onb-av-btns">
          <label class="btn file-btn">上传图片<input type="file" id="avatar-file" accept="image/png,image/jpeg,image/webp,image/gif" hidden /></label>
          <button type="button" class="btn" data-act="avatar-url">用网址</button>
          ${a && a.source === "custom" ? `<button type="button" class="btn ghost" data-act="avatar-reset">恢复同步</button>` : ""}
        </div>
        <p class="fine">${a ? esc(AV_SRC[a.source] || "") : ""}${a ? " · " : ""}png / jpg / webp / gif，最大 2MB。</p>`;
    }
    const c = (onb.info && onb.info.checks) || {};
    const row = (ok, name, text, i) =>
      `<li class="onb-sum" style="--i:${i}"><span class="onb-mark ${ok ? "ok" : ""}">${ok ? SVG.check : ""}</span><div><b>${name}</b><span>${text}</span></div></li>`;
    return `
      <div class="onb-done-mark"><svg viewBox="0 0 52 52"><circle cx="26" cy="26" r="24"/><path d="M15 27l7.5 7.5L37.5 19"/></svg></div>
      <h1 class="onb-title">都准备好了</h1>
      <p class="onb-lead">没配的以后在「设置」里随时补。想再走一遍引导，在设置概况最下面点「重新引导」。</p>
      <ul class="onb-list onb-summary">
        ${row(c.models, "模型", c.models ? "配好了" : "还没配，MaiWork 暂时不会做要用模型的事", 0)}
        ${row(c.groups, "服务的群", c.groups ? "已有群" : "还没有", 1)}
        ${row(c.jev, "Jev 快速判断", c.jev ? "密钥已填" : "没填，判断会走主模型（慢一些）", 2)}
        ${row(c.search, "联网搜索", c.search ? "开了" : "没开", 3)}
        ${row(c.admins, "管理员", c.admins ? "已填" : "还没填", 4)}
      </ul>`;
  }

  function onbShell() {
    const n = ONB_STEPS.length - 1;
    return `
      <div class="onb-scrim"></div>
      <div class="onb-card" role="dialog" aria-modal="true" aria-label="MaiWork 首次引导">
        <header class="onb-head">
          <div class="onb-dots">${ONB_STEPS.map((s, i) => `<span class="onb-dot" data-i="${i}" title="${s.name}"></span>`).join("")}</div>
          <button class="btn ghost small onb-skip" type="button" data-act="onb-skip">跳过引导</button>
        </header>
        <div class="onb-bar"><i style="transform:scaleX(${onb.i / n})"></i></div>
        <div class="onb-stage" id="onb-stage"></div>
        <p class="err onb-err" id="onb-err" hidden></p>
        <footer class="onb-foot" id="onb-foot"></footer>
      </div>`;
  }

  function onbFoot() {
    const id = ONB_STEPS[onb.i].id;
    const back = onb.i > 0 && id !== "done" ? `<button class="btn ghost" type="button" data-act="onb-back">上一步</button>` : `<span></span>`;
    const later = ["keys", "admins", "groups", "look"].includes(id) ? `<button class="btn" type="button" data-act="onb-later">这步先不填</button>` : id === "models" ? `<button class="btn" type="button" data-act="onb-later">稍后再配</button>` : "";
    const next = id === "hello" ? "开始配置" : id === "done" ? "进入 MaiWork" : "保存，下一步";
    return `${back}<div class="onb-foot-r">${later}<button class="btn primary" type="button" data-act="onb-next">${next}</button></div>`;
  }

  // 收掉还在播的动画：连点「下一步」时直接跳到终点，不排队
  function onbSettle() {
    for (const a of onb.anims) {
      try {
        a.finish();
      } catch (_) {}
    }
    onb.anims = [];
    document.querySelectorAll(".onb-pane.leaving").forEach((p) => p.remove());
  }
  const onbAnim = (el, frames, opts) => {
    if (!el || !el.animate) return null;
    const a = el.animate(frames, Object.assign({ fill: "both" }, opts));
    onb.anims.push(a);
    return a;
  };

  function onbPaint(dir) {
    const stage = $("onb-stage");
    const root = $("onb");
    if (!stage || !root) return;
    onbSettle();
    const quiet = calm();
    const n = ONB_STEPS.length - 1;
    // 进度条和圆点
    const bar = root.querySelector(".onb-bar i");
    bar.style.transform = `scaleX(${onb.i / n})`;
    root.querySelectorAll(".onb-dot").forEach((d, i) => {
      d.classList.toggle("on", i === onb.i);
      d.classList.toggle("past", i < onb.i);
    });
    $("onb-err").hidden = true;
    $("onb-foot").innerHTML = onbFoot();
    const old = stage.querySelector(".onb-pane:not(.leaving)");
    const pane = document.createElement("div");
    pane.className = `onb-pane onb-${ONB_STEPS[onb.i].id}`;
    pane.innerHTML = onbPane(ONB_STEPS[onb.i].id);
    stage.appendChild(pane);
    stage.scrollTop = 0;
    const dx = dir === 0 ? 0 : dir > 0 ? 36 : -36;
    if (old) {
      old.classList.add("leaving");
      onbAnim(old, quiet ? [{ opacity: 1 }, { opacity: 0 }] : [{ opacity: 1, transform: "translateX(0)" }, { opacity: 0, transform: `translateX(${-dx * 0.6}px)` }], {
        duration: quiet ? 140 : 200,
        easing: "cubic-bezier(0.4, 0, 1, 1)",
      }).finished.then(() => old.remove(), () => old.remove());
    }
    // 新一屏：整体从运动方向滑进来，里面的块依次浮起（最多错开 6 块）
    const delay = old ? (quiet ? 60 : 90) : 0;
    onbAnim(pane, quiet ? [{ opacity: 0 }, { opacity: 1 }] : [{ opacity: 0, transform: `translateX(${dx}px)` }, { opacity: 1, transform: "translateX(0)" }], {
      duration: quiet ? 180 : 420,
      delay,
      easing: "cubic-bezier(0.32, 0.72, 0, 1)",
    });
    if (!quiet)
      [...pane.children].slice(0, 6).forEach((c, i) =>
        onbAnim(c, [{ opacity: 0, transform: "translateY(10px)" }, { opacity: 1, transform: "translateY(0)" }], { duration: 380, delay: delay + 40 + i * 45, easing: "cubic-bezier(0.2, 0.8, 0.2, 1)" })
      );
    const id = ONB_STEPS[onb.i].id;
    if (id === "hello" && !quiet) {
      onbAnim(pane.querySelector(".onb-avatar"), [{ transform: "scale(0.6)", opacity: 0 }, { transform: "scale(1)", opacity: 1 }], { duration: 560, delay: delay + 60, easing: EASE_SPRING });
      onbAnim(pane.querySelector(".onb-spark"), [{ transform: "scale(0) rotate(-40deg)", opacity: 0 }, { transform: "scale(1) rotate(0)", opacity: 1 }], { duration: 520, delay: delay + 320, easing: EASE_SPRING });
    }
    if (id === "done") {
      const mark = pane.querySelector(".onb-done-mark");
      if (quiet) mark.classList.add("drawn");
      else {
        onbAnim(mark, [{ transform: "scale(0.5)", opacity: 0 }, { transform: "scale(1)", opacity: 1 }], { duration: 520, delay: delay + 40, easing: EASE_SPRING });
        setTimeout(() => mark.classList.add("drawn"), delay + 180);
        pane.querySelectorAll(".onb-sum").forEach((li, i) =>
          onbAnim(li, [{ opacity: 0, transform: "translateY(8px)" }, { opacity: 1, transform: "translateY(0)" }], { duration: 360, delay: delay + 420 + i * 70, easing: "cubic-bezier(0.2, 0.8, 0.2, 1)" })
        );
        pane.querySelectorAll(".onb-mark.ok").forEach((m, i) =>
          onbAnim(m, [{ transform: "scale(0.4)" }, { transform: "scale(1)" }], { duration: 420, delay: delay + 480 + i * 70, easing: EASE_SPRING })
        );
      }
    }
    // 焦点跟着走（键盘用户），等进场动画开始后再给，免得跳滚
    setTimeout(() => {
      const first = pane.querySelector("input:not([type=hidden]), select");
      if (first && !first.value && !matchMedia("(pointer: coarse)").matches) first.focus({ preventScroll: true });
    }, delay + 60);
  }

  function onbError(msg) {
    const err = $("onb-err");
    err.textContent = msg;
    err.hidden = false;
    if (!calm()) onbAnim(err, [{ transform: "translateX(0)" }, { transform: "translateX(-6px)" }, { transform: "translateX(5px)" }, { transform: "translateX(-3px)" }, { transform: "translateX(0)" }], { duration: 320, easing: "ease-out" });
  }

  async function openOnboarding(info) {
    if (onb.open || !admin()) return;
    onb.open = true;
    onb.i = 0;
    onb.info = info || null;
    onb.models = null;
    if (!state.settings) await loadSettings();
    try {
      onb.cfg = await api("GET", "/api/settings/config");
    } catch (_) {
      onb.cfg = null;
    }
    const root = document.createElement("div");
    root.id = "onb";
    root.className = "onb";
    root.innerHTML = onbShell();
    document.body.appendChild(root);
    document.body.classList.add("onb-lock");
    const quiet = calm();
    onbAnim(root.querySelector(".onb-scrim"), [{ opacity: 0 }, { opacity: 1 }], { duration: 260, easing: "linear" });
    onbAnim(root.querySelector(".onb-card"), quiet ? [{ opacity: 0 }, { opacity: 1 }] : [{ opacity: 0, transform: "translateY(24px) scale(0.97)" }, { opacity: 1, transform: "translateY(0) scale(1)" }], {
      duration: quiet ? 200 : 520,
      easing: "cubic-bezier(0.32, 0.72, 0, 1)",
    });
    onbPaint(0);
  }

  async function closeOnboarding(action) {
    const root = $("onb");
    if (!root) return;
    try {
      onb.info = await api("POST", "/api/onboarding", { action });
    } catch (e) {
      toast(e.message, true);
    }
    onbSettle();
    onb.open = false;
    const quiet = calm();
    const card = root.querySelector(".onb-card");
    root.style.pointerEvents = "none";
    onbAnim(root.querySelector(".onb-scrim"), [{ opacity: 1 }, { opacity: 0 }], { duration: 240, easing: "linear" });
    const a = onbAnim(card, quiet ? [{ opacity: 1 }, { opacity: 0 }] : [{ opacity: 1, transform: "scale(1)" }, { opacity: 0, transform: action === "done" ? "scale(1.03)" : "translateY(16px) scale(0.97)" }], {
      duration: 240,
      easing: "cubic-bezier(0.4, 0, 1, 1)",
    });
    const gone = () => {
      root.remove();
      document.body.classList.remove("onb-lock");
    };
    if (a) a.finished.then(gone, gone);
    else gone();
    onb.anims = [];
    loadSettings().then(() => (renderRail(), state.page === "settings" && renderView()));
    if (action === "skip") toast("跳过了，以后在设置概况里可以重新引导");
  }

  // 这一步要保存的东西；返回错误文字（空 = 过）
  async function onbSave(id) {
    const v = (x) => ($(x) ? $(x).value.trim() : "");
    if (id === "models") {
      const m = (state.settings && state.settings.models) || {};
      if (!/^https?:\/\/\S+$/.test(v("onb-url"))) return "端点地址要以 http:// 或 https:// 开头。";
      if (!m.key_set && !v("onb-key")) return "还没填密钥。";
      if (!v("onb-main") || !v("onb-worker")) return "先点「测试连接」，再选主模型和子 agent 模型。";
      const r = await api("PUT", "/api/settings/models", {
        base_url: v("onb-url"),
        api_key: v("onb-key") || undefined,
        main: v("onb-main"),
        main_backup: m.main_backup || "",
        worker: v("onb-worker"),
        worker_backup: m.worker_backup || "",
      });
      if (state.settings) state.settings.models = r;
      return "";
    }
    const patch = {};
    if (id === "groups" && $("onb-serve"))
      patch["groups.serve"] = [...$("onb-serve").querySelectorAll(".re-row")]
        .map((row) => Object.fromEntries([...row.querySelectorAll("input")].map((i) => [i.dataset.k, i.value.trim()])))
        .filter((o) => o.group || o.workspace)
        .map((o) => ({ group: /^\d+$/.test(o.group) ? `qq:${o.group}` : o.group, workspace: o.workspace }));
    if (id === "keys") {
      if (v("onb-jev")) patch["jev.api_key"] = v("onb-jev");
    }
    if (id === "admins" && $("onb-admins")) patch["approval.admins"] = [...$("onb-admins").querySelectorAll(".ci")].map((c) => c.dataset.v);
    // 没变的不提交，免得把「要重载」标记白白点亮
    for (const k of Object.keys(patch)) {
      const f = onbField(k);
      if (f && f.type !== "secret" && JSON.stringify(f.value) === JSON.stringify(patch[k])) delete patch[k];
    }
    if (!Object.keys(patch).length) return "";
    onb.cfg = await api("PUT", "/api/settings/config", patch).then(() => api("GET", "/api/settings/config"));
    return "";
  }

  async function onbGo(delta) {
    const to = Math.max(0, Math.min(ONB_STEPS.length - 1, onb.i + delta));
    if (to === onb.i) return;
    onb.i = to;
    if (ONB_STEPS[to].id === "look" && !state.avatarCfg) {
      try {
        state.avatarCfg = await api("GET", "/api/settings/avatar");
      } catch (_) {}
    }
    if (ONB_STEPS[to].id === "done") {
      try {
        onb.info = await api("GET", "/api/onboarding");
      } catch (_) {}
    }
    onbPaint(delta);
  }

  async function onbAct(el) {
    const a = el.dataset.act;
    if (onb.busy && a !== "onb-skip") return;
    const id = ONB_STEPS[onb.i].id;
    if (a === "onb-restart") {
      if (onb.open) return;
      try {
        const info = await api("POST", "/api/onboarding", { action: "reset" });
        return openOnboarding(info);
      } catch (e) {
        return toast(e.message, true);
      }
    }
    if (a === "onb-skip") return closeOnboarding("skip");
    if (a === "onb-back") return onbGo(-1);
    if (a === "onb-later") return onbGo(1);
    if (a === "onb-test") {
      const out = $("onb-status");
      const url = $("onb-url").value.trim();
      const key = $("onb-key").value.trim();
      const m = (state.settings && state.settings.models) || {};
      if (!/^https?:\/\/\S+$/.test(url)) return onbError("地址要以 http:// 或 https:// 开头。");
      if (!m.key_set && !key) return onbError("先填密钥再测。");
      $("onb-err").hidden = true;
      el.disabled = true;
      out.innerHTML = `<i class="onb-spin"></i>正在连…`;
      try {
        const r = await api("POST", "/api/settings/models/test", { base_url: url, api_key: key || undefined });
        if (!r.ok) {
          out.textContent = "";
          onbError(r.error || "没连上");
          return;
        }
        onb.models = r.models || [];
        // 只重画模型下拉，保留已经填的地址和密钥
        const keep = { main: $("onb-main").value, worker: $("onb-worker").value };
        const tmp = document.createElement("div");
        const mm = Object.assign({}, m, { main: keep.main || m.main, worker: keep.worker || m.worker });
        const saved = state.settings && state.settings.models;
        if (state.settings) state.settings.models = mm;
        tmp.innerHTML = onbPane("models");
        if (state.settings) state.settings.models = saved;
        const picks = el.closest(".onb-pane").querySelector(".onb-picks");
        picks.replaceWith(tmp.querySelector(".onb-picks"));
        const np = el.closest(".onb-pane").querySelector(".onb-picks");
        out.innerHTML = `<i class="onb-ok">${SVG.check}</i>连上了，找到 ${onb.models.length} 个模型`;
        if (!calm()) {
          onbAnim(out.querySelector(".onb-ok"), [{ transform: "scale(0.3)", opacity: 0 }, { transform: "scale(1)", opacity: 1 }], { duration: 460, easing: EASE_SPRING });
          [...np.children].forEach((c, i) => onbAnim(c, [{ opacity: 0, transform: "translateY(8px)" }, { opacity: 1, transform: "translateY(0)" }], { duration: 340, delay: 60 + i * 40, easing: "cubic-bezier(0.2, 0.8, 0.2, 1)" }));
        }
      } catch (e) {
        out.textContent = "";
        onbError(e.message);
      } finally {
        el.disabled = false;
      }
      return;
    }
    if (a === "onb-next") {
      if (id === "done") return closeOnboarding("done");
      if (id === "hello") return onbGo(1);
      onb.busy = true;
      el.disabled = true;
      const label = el.textContent;
      el.innerHTML = `<i class="onb-spin"></i>保存中`;
      try {
        const bad = await onbSave(id);
        if (bad) return onbError(bad);
        await onbGo(1);
      } catch (e) {
        onbError(e.message);
      } finally {
        onb.busy = false;
        if (el.isConnected) {
          el.disabled = false;
          el.textContent = label;
        }
      }
    }
  }

  async function maybeOnboard() {
    if (!admin() || onb.open) return;
    try {
      const info = await api("GET", "/api/onboarding");
      if (info && info.show) openOnboarding(info);
    } catch (_) {}
  }

  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-act]");
    if (!el || el.disabled) return;
    if (el.dataset.act.startsWith("onb-")) return void onbAct(el);
    act(el, e);
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (onb.open) return;
      if (state.sheet) closeSheet();
      else if (state.detail) {
        state.detail = null;
        syncHash();
        renderView();
        renderSide();
      }
    }
    if ((e.key === "Enter" || e.key === " ") && e.target.matches(".item.tap")) {
      e.preventDefault();
      e.target.click();
    }
  });
  $("scrim").addEventListener("click", closeSheet);

  document.addEventListener("submit", async (e) => {
    const f = e.target;
    e.preventDefault();
    const btn = f.querySelector('[type="submit"]');
    if (f.id === "login") {
      const pw = $("pw").value;
      if (!pw) return;
      btn.disabled = true;
      try {
        await api("POST", "/api/login", { password: pw });
        closeSheet();
        await reboot();
        toast("已进入管理员，能看到全部群");
      } catch (err) {
        state.loginError = err.status === 429 ? "错太多次了，过 10 分钟再试。" : err.status === 401 ? "密码不对，再试一次。" : err.message;
        const p = f.querySelector(".err");
        p.textContent = state.loginError;
        p.hidden = false;
        $("pw").select();
      } finally {
        btn.disabled = false;
      }
      return;
    }
    if (f.id === "models") {
      const v = (id) => $(id).value.trim();
      const err = $("m-err");
      const m = (state.settings && state.settings.models) || {};
      const problem = !/^https?:\/\/\S+$/.test(v("m-url"))
        ? "端点地址要以 http:// 或 https:// 开头。"
        : !m.key_set && !v("m-key")
          ? "还没填密钥。"
          : !v("m-main") || !v("m-worker")
            ? "主模型和子 agent 模型都要选。"
            : (v("m-main-b") && v("m-main-b") === v("m-main")) || (v("m-worker-b") && v("m-worker-b") === v("m-worker"))
              ? "备用模型不能和原来的一样。"
              : "";
      if (problem) {
        err.textContent = problem;
        err.hidden = false;
        return;
      }
      btn.disabled = true;
      const newKey = !!v("m-key");
      try {
        const r = await api("PUT", "/api/settings/models", {
          base_url: v("m-url"),
          api_key: newKey ? v("m-key") : undefined,
          main: v("m-main"),
          main_backup: v("m-main-b"),
          worker: v("m-worker"),
          worker_backup: v("m-worker-b"),
          retries: Math.max(0, Math.min(10, parseInt(v("m-retries"), 10) || 0)),
          retry_delay_s: Math.max(1, Math.min(60, parseInt(v("m-retry-delay"), 10) || 10)),
          max_concurrency: Math.max(1, Math.min(8, parseInt(v("m-conc"), 10) || 2)),
          max_rpm: Math.max(0, Math.min(600, parseInt(v("m-rpm"), 10) || 0)),
        });
        if (state.settings) state.settings.models = r;
        draft.models = null;
        repaintSheet();
        loadSettings().then(() => (repaintSheet(), renderRail()));
        toast(newKey ? "保存好了，新密钥已替换旧的" : "保存好了，下一次调用就用新设置");
      } catch (ex) {
        err.textContent = ex.message;
        err.hidden = false;
      } finally {
        btn.disabled = false;
      }
      return;
    }
    if (f.id === "chat-form") {
      const input = $("chat-input");
      const text = input.value.trim();
      if (!text || !state.chat) return;
      if (state.chat.running) return toast("上一句还在处理，等一下", true);
      btn.disabled = true;
      try {
        await api("POST", `/api/chat/${encodeURIComponent(state.chat.id)}/messages`, { text });
        input.value = "";
        input.style.height = "";
        state.chat.running = true;
        await loadChat(state.chat.id, true);
        state.chat.running = true;
        paintChat("smooth");
        chatPoll();
      } catch (ex) {
        btn.disabled = false;
        toast(ex.message, true);
      }
      return;
    }
    if (f.id === "rules-form") {
      const errEl = $("r-err");
      let body;
      try {
        body = readRules();
      } catch (ex) {
        errEl.textContent = ex.message;
        errEl.hidden = false;
        return;
      }
      if (!Object.keys(body).length) {
        toast("没有改动");
        return;
      }
      btn.disabled = true;
      try {
        state.rules = await api("PUT", "/api/settings/config", body);
        await loadSettings();
        repaintSheet();
        toast((state.rules.reload_pending || []).length ? "写进 config.toml 了；标「重载后生效」的要等插件重载" : "写进 config.toml 了，马上生效");
      } catch (ex) {
        errEl.textContent = ex.message;
        errEl.hidden = false;
      } finally {
        btn.disabled = false;
      }
      return;
    }
    if (f.classList.contains("rss-add")) {
      const input = f.querySelector("input");
      const url = input.value.trim();
      if (!/^https?:\/\/\S+$/.test(url)) return toast("地址要以 http:// 或 https:// 开头", true);
      btn.disabled = true;
      btn.textContent = "在取…";
      try {
        const r = await api("POST", `/api/groups/${encodeURIComponent(f.dataset.g)}/rss`, { url });
        await loadSettings();
        repaintSheet();
        toast(`加上了：${r.title || url}${r.items_count != null ? `，现在有 ${r.items_count} 篇` : ""}`);
      } catch (ex) {
        btn.disabled = false;
        btn.textContent = "加上";
        toast(ex.message, true);
      }
      return;
    }
    if (f.classList.contains("id-form")) {
      const kind = f.dataset.kind;
      const text = f.querySelector(".id-text").value;
      const limit = Number(f.querySelector(".id-text").dataset.limit) || 16384;
      if (new Blob([text]).size > limit) return toast(`太长了，最多 ${limit} 字节`, true);
      btn.disabled = true;
      try {
        const path = kind.startsWith("group:") ? `/api/identity/group-memory/${encodeURIComponent(kind.slice(6))}` : `/api/identity/${kind}`;
        await api("PUT", path, { text });
        await loadIdentity();
        repaintSheet();
        toast("保存好了");
      } catch (ex) {
        toast(ex.message, true);
      } finally {
        btn.disabled = false;
      }
      return;
    }
    if (f.id === "mcp-paste-form") {
      const errEl = $("mp-err");
      let list;
      try {
        list = parseMcpConfig($("mp-text").value);
      } catch (ex) {
        errEl.textContent = ex.message;
        errEl.hidden = false;
        return;
      }
      const ok = list.filter((x) => !x.skipped);
      const skipped = list.filter((x) => x.skipped);
      if (!ok.length) {
        errEl.textContent = skipped.map((x) => `${x.name}：${x.skipped}`).join("；");
        errEl.hidden = false;
        return;
      }
      if (ok.length === 1) {
        state.extEdit = { kind: "mcp", name: null, prefill: { ...ok[0], roles: ["worker"], enabled: true, timeout_s: 30 } };
        repaintSheet();
        if (skipped.length) toast(`跳过了：${skipped.map((x) => `${x.name}（${x.skipped}）`).join("、")}`, true);
        return;
      }
      btn.disabled = true;
      const done = [];
      const failed = [];
      for (const x of ok) {
        try {
          const r = await api("POST", "/api/extensions/mcp", { name: x.name, url: x.url, headers: x.headers, tools: [], roles: ["worker"], enabled: true, timeout_s: 30 });
          done.push(`${x.name}${r.ok ? "" : "（加上了但没连上）"}`);
        } catch (ex) {
          failed.push(`${x.name}：${ex.message}`);
        }
      }
      btn.disabled = false;
      state.extEdit = null;
      await loadExt();
      repaintSheet();
      toast([done.length ? `加上了 ${done.join("、")}` : "", failed.length ? `没加上 ${failed.join("；")}` : "", skipped.length ? `跳过 ${skipped.map((x) => x.name).join("、")}` : ""].filter(Boolean).join("。"), !!failed.length);
      return;
    }
    if (f.id === "mcp-form" || f.id === "skill-form") {
      const isMcp = f.id === "mcp-form";
      const errEl = $(isMcp ? "x-err" : "k-err");
      const name = (f.dataset.name || ($(isMcp ? "x-name" : "k-name") || {}).value || "").trim();
      const fail = (t) => {
        errEl.textContent = t;
        errEl.hidden = false;
      };
      if (!/^[A-Za-z0-9_-]{1,64}$/.test(name)) return fail("名字只能用字母、数字、下划线、横线。");
      let body;
      if (isMcp) {
        const url = $("x-url").value.trim();
        if (!/^https:\/\/\S+$/.test(url)) return fail("地址要以 https:// 开头。");
        const roles = readRoles("x");
        if (!roles.length) return fail("至少选一个「给谁用」。");
        const { headers, remove } = readHeaders();
        body = {
          name,
          url,
          headers,
          remove_headers: remove,
          tools: $("x-tools").value.split(/[,，\s]+/).map((t) => t.trim()).filter(Boolean),
          roles,
          enabled: $("x-enabled").checked,
          timeout_s: Number($("x-timeout").value) || 30,
        };
      } else {
        const roles = readRoles("k");
        if (!roles.length) return fail("至少选一个「给谁用」。");
        const text = $("k-body").value;
        if (!text.trim()) return fail("内容不能是空的。");
        if (new Blob([text]).size > 40000) return fail("内容太长了，最多 40KB。");
        body = { name, description: $("k-desc").value.trim(), roles, body: text };
      }
      btn.disabled = true;
      try {
        const kindPath = isMcp ? "mcp" : "skills";
        if (f.dataset.name) await api("PUT", `/api/extensions/${kindPath}/${encodeURIComponent(f.dataset.name)}`, body);
        else await api("POST", `/api/extensions/${kindPath}`, body);
        state.extEdit = null;
        await Promise.all([loadExt(), loadSettings()]);
        repaintSheet();
        toast(isMcp ? "保存好了，子 agent 下一次就能用" : "保存好了，子 agent 下一次就能看到");
      } catch (ex) {
        fail(ex.message);
      } finally {
        btn.disabled = false;
      }
      return;
    }
    if (f.id === "edit") {
      const ed = state.editing || {};
      const text = $("ed-text").value.trim();
      const err = $("ed-err");
      if (!text) {
        err.textContent = ed.kind === "focus" ? "填一个 QQ 号。" : "内容不能是空的。";
        err.hidden = false;
        return;
      }
      btn.disabled = true;
      try {
        const g = grp();
        if (ed.kind === "pref") {
          await api("PUT", `/api/groups/${encodeURIComponent(g.id)}/feeds-pref`, { text });
        } else if (ed.kind === "focus") {
          if (!/^\d{5,12}$/.test(text)) throw new Error("QQ 号应该是 5 到 12 位数字。");
          await api("POST", `/api/groups/${encodeURIComponent(g.id)}/focus`, { user_id: text, action: "add" });
        } else if (ed.id) {
          await api("PATCH", `/api/profile/${ed.id}`, { text });
        } else {
          await api("POST", `/api/groups/${encodeURIComponent(g.id)}/profile`, { category: ed.cat, text });
        }
        closeSheet();
        await loadView(true);
        renderView();
        toast(ed.kind === "focus" ? "加上了" : ed.kind === "pref" ? "记下了，下一轮备料就照这个找" : "保存好了，这条已锁定");
      } catch (ex) {
        err.textContent = ex.message;
        err.hidden = false;
      } finally {
        btn.disabled = false;
      }
    }
  });

  desktop.addEventListener("change", () => {
    closeSheet();
    render();
  });
  window.addEventListener("hashchange", () => {
    const h = parseHash();
    if (!admin() && h.ref !== state.ref) return reboot();
    applyHash();
    if (state.page === "chat") {
      enterChat(state.chatId);
      return;
    }
    if (state.page === "settings") {
      flash = true;
      render();
      Promise.all([
        state.settings ? null : loadSettings(),
        state.setSub === "extensions" && !state.ext ? loadExt() : null,
        state.setSub === "rules" ? loadRules() : null,
        state.setSub === "usage" ? loadUsage() : null,
        state.setSub === "identity" ? loadIdentity() : null,
        state.setSub === "logs" ? loadLogs() : null,
      ]).then(() => render());
      return;
    }
    flash = true;
    const same = grp() && gview();
    if (!same) state.view = null;
    render();
    if (!same) loadView().then(() => ((flash = true), render()));
    if (state.detail && state.detail.type === "task") loadTask(state.detail.id);
  });

  reboot();
})();
