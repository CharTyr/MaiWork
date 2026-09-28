/* MaiWork 控制台原型 · 纯前端，无依赖。数据来自 data.js，所有操作只改内存里的示例数据。 */
(() => {
  "use strict";

  const D = window.MW;
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
    running: "进行中",
    reviewing: "验收中",
    queued: "排队中",
    waiting: "等人回复",
    done: "已完成",
    failed: "失败",
  };
  const FILTERS = [
    { id: "all", label: "全部", match: () => true },
    { id: "active", label: "进行中", match: (t) => ["running", "reviewing", "queued"].includes(t.status) },
    { id: "waiting", label: "等人回复", match: (t) => t.status === "waiting" },
    { id: "done", label: "已完成", match: (t) => t.status === "done" },
    { id: "failed", label: "失败", match: (t) => t.status === "failed" },
  ];

  const state = { g: "tinker", tab: "news", filter: "all", detail: null, sheet: null, fb: {}, loginError: false };
  // 身份：member = 拿着本群链接的群成员，只看本群；admin = 输入过密码的管理员，看全部群
  if (new URLSearchParams(location.search).get("demo") === "admin") sessionStorage.setItem("mw-role", "admin"); // 仅原型：方便截图
  let role = sessionStorage.getItem("mw-role") === "admin" ? "admin" : "member";
  const admin = () => role === "admin";
  const PROTO_PASSWORD = "maiwork";

  /* ───────────── 小工具 ───────────── */

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
    up: `<svg viewBox="0 0 24 24" ${P} stroke-width="1.9"><path d="M7 11v9H4.5v-9zM7 11l3.8-7c1.3 0 2.2.9 2.2 2.2V9.5h5a2 2 0 0 1 2 2.3l-1.1 6.4a2.2 2.2 0 0 1-2.2 1.8H7"/></svg>`,
    dn: `<svg viewBox="0 0 24 24" ${P} stroke-width="1.9" style="transform:rotate(180deg)"><path d="M7 11v9H4.5v-9zM7 11l3.8-7c1.3 0 2.2.9 2.2 2.2V9.5h5a2 2 0 0 1 2 2.3l-1.1 6.4a2.2 2.2 0 0 1-2.2 1.8H7"/></svg>`,
    lock: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><rect x="5" y="11" width="14" height="9" rx="2.5"/><path d="M8.5 11V8a3.5 3.5 0 0 1 7 0v3"/></svg>`,
    key: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><circle cx="8" cy="15" r="4"/><path d="M11 12l8-8M16 7l2.5 2.5M14 9l2 2"/></svg>`,
    trash: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><path d="M4.5 7h15M10 7V5h4v2M6.8 7l.9 12.2h8.6l.9-12.2"/></svg>`,
  };

  const ico = (name, cls = "ico") => `<img class="${cls}" src="assets/icons/${name}.png" alt="" loading="lazy" />`;
  const grp = () => D.groups.find((x) => x.id === state.g) || D.groups[0];
  const enter = (i) => `class="enter" style="--i:${i}"`;
  let flash = true; // 切换页面时才播放进场动画

  function toast(text) {
    const t = $("toast");
    t.textContent = text;
    t.classList.add("on");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => t.classList.remove("on"), 2200);
  }

  /* ───────────── 群脉搏：最近 24 小时的发言量 ───────────── */

  const toMin = (hhmm) => {
    const [h, m] = hhmm.split(":").map(Number);
    return h * 60 + m;
  };
  const START = D.now.minutes - 1440; // 相对「周六 00:00」的分钟数，周五为负
  const pct = (m) => Math.max(0, Math.min(100, ((m - START) / 1440) * 100));

  function rng(seed) {
    let a = seed >>> 0;
    return () => {
      a = (a + 0x6d2b79f5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function base(h) {
    if (h < 1) return 2.2;
    if (h < 7.5) return 0.15;
    if (h < 9) return 1.6;
    if (h < 12) return 3;
    if (h < 14) return 5.2;
    if (h < 18) return 3.2;
    if (h < 23) return 6.2;
    return 3;
  }

  function pulseBins(g) {
    const r = rng(g.pulse.seed);
    const spells = g.pulse.spells.map((s) => [toMin(s.from), toMin(s.to)]);
    const topics = g.pulse.topics.map((t) => toMin(t.at));
    const bins = [];
    for (let i = 0; i < 96; i++) {
      const m = START + i * 15;
      const h = (((m % 1440) + 1440) % 1440) / 60;
      let v = base(h) * g.pulse.factor * (0.45 + r() * 1.1);
      if (spells.some(([a, b]) => m + 15 > a && m < b)) v = 0.1;
      if (topics.some((t) => m + 15 > t && m < t + 30)) v *= 2.3;
      bins.push(v);
    }
    return bins;
  }

  function smoothPath(pts) {
    let d = `M${pts[0][0]},${pts[0][1]}`;
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[i - 1] || pts[i];
      const p1 = pts[i];
      const p2 = pts[i + 1];
      const p3 = pts[i + 2] || p2;
      const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
      const c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
      d += ` C${c1[0].toFixed(1)},${c1[1].toFixed(1)} ${c2[0].toFixed(1)},${c2[1].toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
    }
    return d;
  }

  function pulseCard(g, compact) {
    const bins = pulseBins(g);
    const max = Math.max(...bins);
    const H = 110;
    const pts = bins.map((v, i) => [(i / (bins.length - 1)) * 1000, H - (v / max) * H * 0.9 - 2]);
    const line = smoothPath(pts);
    const area = `${line} L1000,${H} L0,${H} Z`;

    const sleepL = pct(-60);
    const sleepR = pct(8 * 60);
    const bands = [
      `<div class="band sleep" style="left:${sleepL}%;width:${sleepR - sleepL}%"><span class="band-label">${ico("moon", "")}睡觉时段</span></div>`,
      ...g.pulse.spells.map((s) => {
        const l = pct(toMin(s.from));
        const w = pct(toMin(s.to)) - l;
        return `<div class="band cold" style="left:${l}%;width:${w}%" title="${s.note}"></div>`;
      }),
    ].join("");
    const marks = g.pulse.topics
      .map((t) => {
        const l = pct(toMin(t.at));
        return `<div class="mark${l < 30 ? " left" : ""}" style="left:${l}%"><span class="mark-label">开话题 · ${t.replies} 人接</span></div>`;
      })
      .join("");
    const ticks = (compact
      ? [["00:00", 0], ["08:00", 480], ["16:00", 960]]
      : [["00:00", 0], ["04:00", 240], ["08:00", 480], ["12:00", 720], ["16:00", 960]])
      .map(([t, m]) => `<span style="left:${pct(m)}%">${t}</span>`)
      .join("");

    return `
      <section class="pulse-card${compact ? " compact" : ""}" aria-label="群脉搏">
        <div class="pulse-head">
          <div class="pulse-title">群脉搏 <span style="color:var(--gray);font-weight:500;font-size:13px">· 最近 24 小时</span></div>
          <div class="pulse-now"><b>${g.quiet.text}</b><br />${g.quiet.usual}</div>
        </div>
        <div class="pulse" style="${compact ? "height:104px" : ""}">
          ${bands}
          <svg viewBox="0 0 1000 ${H}" preserveAspectRatio="none" aria-hidden="true">
            <path class="area" d="${area}" />
            <path class="line" d="${line}" pathLength="1" />
          </svg>
          ${marks}
          <div class="axis">${ticks}<span style="left:100%">现在</span></div>
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
    return `<div class="empty enter">${ico(iconName)}<b>${title}</b><span>${text}</span></div>`;
  }

  function viewNews(g) {
    if (g.fresh) {
      return `<h1 class="h-page">资讯</h1>` + emptyState("seedling", "还在熟悉这个群", "MaiWork 已经读了 3 天的聊天记录。群画像成形之前不出资讯，免得乱推。");
    }
    let i = 0;
    return g.news
      .map((batch, b) => {
        const head = `<h1 class="h-page" ${b ? 'style="margin-top:46px"' : ""}>${batch.slot}</h1><p class="h-meta">${batch.meta}</p>`;
        if (batch.skipped) {
          return head + `<div class="skipped enter" style="--i:${i++}">${ico("teacup")}<span>${batch.skipped}</span></div>`;
        }
        const items = batch.items
          .map((it, k) => {
            const key = `${g.id}:${b}:${k}`;
            const fb = state.fb[key];
            return `
            <article class="item enter" style="--i:${i++}">
              ${ico(it.icon)}
              <div>
                <h2 class="item-title">${it.title}</h2>
                <p class="item-body">${it.body}</p>
                <p class="why"><b>为什么给这个群：</b>${it.why}</p>
                <div class="sources">${it.sources
                  .map((s) => `<a class="src" href="#" data-act="link"><div class="src-site">${s.site}</div><div class="src-title">${s.title}</div></a>`)
                  .join("")}</div>
                <div class="status">
                  <span class="dot ${it.status.kind}"></span>
                  <span class="status-text">${it.status.text}</span>
                  <span class="fb">
                    <button data-act="fb" data-key="${key}" data-v="up" aria-pressed="${fb === "up"}" aria-label="有用">${SVG.up}</button>
                    <button data-act="fb" data-key="${key}" data-v="down" aria-pressed="${fb === "down"}" aria-label="没用">${SVG.dn}</button>
                  </span>
                </div>
              </div>
            </article>`;
          })
          .join("");
        return head + items;
      })
      .join("");
  }

  function viewIdeas(g) {
    let html = `<h1 class="h-page">构想</h1><p class="h-meta">${admin() ? "按这个群最近在聊的、在做的想出来的，做不做你说了算" : "按这个群最近在聊的、在做的想出来的。想要哪个就点一下，管理员批准后开工"}</p>`;
    if (!g.ideas.length) {
      return html + emptyState("bulb", "还没有构想", g.fresh ? "等群画像成形后，MaiWork 会开始提想法。" : "最近没有想到适合这个群的点子，不硬凑。");
    }
    html += g.ideas
      .map((it, k) => {
        let act = "";
        if (it.state === "new" && admin()) {
          act = `<div class="actions"><button class="btn primary" data-act="idea-do" data-k="${k}">做这个</button><button class="btn" data-act="idea-no" data-k="${k}">不用了</button></div>`;
        } else if (it.state === "new") {
          act = `<div class="actions"><button class="btn primary" data-act="idea-want" data-k="${k}">想要这个</button></div>`;
        } else if (it.state === "wanted") {
          act = `<div class="note-ok"><span class="dot pending"></span>已经告诉管理员了，批准后就开工</div>`;
        } else if (it.state === "pending") {
          act = admin()
            ? `<div class="note-ok"><span class="dot pending"></span>蓝莓山竹说了「做吧」，在任务里等你批准 <button class="btn ghost" data-act="tab" data-tab="tasks" style="color:var(--blue)">去看看</button></div>`
            : `<div class="note-ok"><span class="dot pending"></span>蓝莓山竹说了「做吧」，等管理员批准</div>`;
        } else if (it.state === "started") {
          act = `<div class="note-ok"><span class="dot running"></span>已经开工，排进任务了</div>`;
        } else {
          act = `<div class="note-ok"><span class="dot"></span>已收起，以后不会再提这个</div>`;
        }
        return `
        <article class="item ruled enter" style="--i:${k}">
          ${ico(it.icon)}
          <div>
            <h2 class="item-title">${it.title}</h2>
            <p class="item-body soft">${it.body}</p>
            <dl class="facts">
              <div><dt>依据</dt><dd>${it.basis}</dd></div>
              <div><dt>第一步</dt><dd>${it.step}</dd></div>
              <div><dt>要多久</dt><dd>${it.effort}</dd></div>
            </dl>
            ${act}
          </div>
        </article>`;
      })
      .join("");
    return html;
  }

  function viewGoals(g) {
    let html = `<h1 class="h-page">目标</h1>`;
    const { agent, member } = g.goals;
    if (!agent.length && !member.length) {
      return html + emptyState("bullseye", "还没有目标", "有人在群里说「帮我们盯着……」或「提醒我……」时，会记在这里。");
    }
    let i = 0;
    if (agent.length) {
      html += `<h2 class="h-sub">我在推进 <small>${agent.length} 个 · 闲时静默推进，有结果再说</small></h2>`;
      html += agent
        .map((goal) => {
          const done = goal.criteria.filter((c) => c.done).length;
          const sel = state.detail && state.detail.id === goal.id ? " selected" : "";
          return `
          <article class="item ruled tap enter${sel}" style="--i:${i++}" data-act="goal" data-id="${goal.id}" tabindex="0">
            ${ico(goal.icon)}
            <div>
              <h3 class="item-title">${goal.title}</h3>
              <p class="item-body soft">${goal.body}</p>
              <div class="progress" aria-label="完成 ${done} / ${goal.criteria.length}"><i style="width:${(done / goal.criteria.length) * 100}%"></i></div>
              <div class="status"><span class="dot running"></span><span class="status-text">${done} / ${goal.criteria.length} · ${goal.next}</span></div>
            </div>
          </article>`;
        })
        .join("");
    }
    if (member.length) {
      html += `<h2 class="h-sub">帮大家记着 <small>${member.length} 件 · 到点提醒</small></h2>`;
      html += member
        .map(
          (m) => `
          <article class="item ruled enter" style="--i:${i++}">
            ${ico(m.icon)}
            <div>
              <h3 class="item-title"><span class="who">${m.who}</span> · ${m.title}</h3>
              <div class="status" style="margin-top:4px"><span class="dot pending"></span><span class="status-text">截止 ${m.due} · ${m.remind}</span></div>
            </div>
          </article>`
        )
        .join("");
    }
    return html;
  }

  function taskRow(t, i) {
    const sel = state.detail && state.detail.id === t.id ? " selected" : "";
    return `
      <button class="row enter${sel}" style="--i:${i}" data-act="task" data-id="${t.id}">
        ${ico(t.icon)}
        <span>
          <span class="row-title">${t.title}</span>
          <span class="row-meta"><span class="dot ${t.status}"></span><span>${STATUS[t.status]} · ${t.meta}</span></span>
        </span>
        <span class="chev">${SVG.right}</span>
      </button>`;
  }

  function viewTasks(g) {
    let html = `<h1 class="h-page">任务</h1>`;
    const { pending, list } = g.tasks;
    if (!pending.length && !list.length) {
      return html + emptyState("package", "还没有任务", "群里有人 @ 东雪莲请她准备东西，或者你在构想里点了「做这个」，任务就会出现在这里。");
    }
    let i = 0;
    if (pending.length) {
      html += `<h2 class="h-sub" style="margin-top:22px">${admin() ? "等你批准" : "等管理员批准"} <small>${pending.length} 件</small></h2>`;
      html += pending
        .map(
          (p) => `
          <article class="item ruled enter" style="--i:${i++}">
            ${ico(p.icon)}
            <div>
              <h3 class="item-title">${p.title}</h3>
              <div class="quote"><span class="quote-by">${p.who} · ${p.when}</span>${p.quote}</div>
              <div class="via">${p.via}</div>
              ${
                admin()
                  ? `<div class="actions">
                <button class="btn primary" data-act="approve" data-id="${p.id}">批准</button>
                <button class="btn" data-act="reject" data-id="${p.id}">拒绝</button>
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
      const f = FILTERS.find((x) => x.id === state.filter);
      const rows = list.filter(f.match);
      html += rows.length
        ? `<div>${rows.map((t) => taskRow(t, i++)).join("")}</div>`
        : `<p class="h-meta" style="margin-top:18px">这一栏现在是空的。</p>`;
    }
    return html;
  }

  function viewGroup(g) {
    let html = `<h1 class="h-page">${g.name}</h1><p class="h-meta">${g.members} 人 · 工作区 <span class="mono">${g.id}</span></p>`;
    html += pulseCard(g, false);

    if (g.fresh) {
      return html + emptyState("seedling", "群画像还没成形", "MaiWork 读满一周聊天记录后，会在这里整理出这个群在聊什么、关心什么。");
    }

    let i = 0;
    html += `<h2 class="h-sub">开话题记录 <small>冷场时 MaiWork 开的头</small></h2>`;
    html += g.topicLog.length
      ? g.topicLog
          .map(
            (t, k) => `
        <article class="item ruled enter" style="--i:${i++}">
          ${ico(t.opener ? "speech" : "hourglass")}
          <div>
            <h3 class="item-title">${t.time} · ${t.opener ? "开了话题" : "忍住了没开"}</h3>
            <p class="item-body soft">${t.quiet}，${t.usual}。Jev：${t.jev}。</p>
            ${t.opener ? `<div class="opener">${t.opener}</div>` : ""}
            ${t.result ? `<div class="status"><span class="dot used"></span><span class="status-text">${t.result}</span></div>` : ""}
            ${
              admin()
                ? `<div class="verdict">这次判断对吗？
              <button class="btn" data-act="verdict" data-k="${k}" data-v="right" aria-pressed="${t.verdict === "right"}">对</button>
              <button class="btn" data-act="verdict" data-k="${k}" data-v="wrong" aria-pressed="${t.verdict === "wrong"}">不对</button>
            </div>`
                : ""
            }
          </div>
        </article>`
          )
          .join("")
      : `<p class="h-meta">今天还没有冷场到需要开话题。</p>`;

    html += `<h2 class="h-sub">群画像 <small>${admin() ? "改过、锁定的以你为准" : "MaiWork 眼中的这个群"}</small></h2>`;
    html += g.profile
      .map(
        (sec, s) => `
        <div class="pf-sec enter" style="--i:${i++}">
          <div class="pf-name">${sec.name}</div>
          ${sec.entries
            .map(
              (e, k) => `
            <div class="pf">
              <div><div class="pf-text">${e.text}</div><div class="pf-meta">${e.locked ? (admin() ? "已锁定 · 不会被改写" : "管理员确认过") : e.meta}</div></div>
              ${
                admin()
                  ? `<div class="pf-acts">
                <button class="icon-btn" data-act="pf-lock" data-s="${s}" data-k="${k}" aria-pressed="${!!e.locked}" aria-label="锁定">${SVG.lock}</button>
                <button class="icon-btn" data-act="pf-del" data-s="${s}" data-k="${k}" aria-label="删除">${SVG.trash}</button>
              </div>`
                  : ""
              }
            </div>`
            )
            .join("")}
        </div>`
      )
      .join("");

    if (!admin()) return html;
    html += `<h2 class="h-sub">关注成员 <span class="private">${SVG.lock}只有管理员看得到</span></h2>`;
    html += g.focus
      .map(
        (p) => `
        <div class="person enter" style="--i:${i++}">
          <div class="face" style="background:${p.tone}">${p.name.slice(0, 1)}</div>
          <div>
            <div class="person-name">${p.name}${p.reasons.map((r) => `<span class="tag">${r}</span>`).join("")}</div>
            <div class="person-note">${p.note}</div>
          </div>
        </div>`
      )
      .join("");
    return html;
  }

  /* ───────────── 详情 ───────────── */

  // 群友只能在本群里找；管理员能在全部群里找（真实实现里由服务端按身份过滤，这里只是模拟）
  const scope = () => (admin() ? D.groups : [grp()]);
  function findTask(id) {
    for (const g of scope()) {
      const t = g.tasks.list.find((x) => x.id === id);
      if (t) return t;
    }
    return null;
  }
  function findGoal(id) {
    for (const g of scope()) {
      const x = g.goals.agent.find((y) => y.id === id);
      if (x) return x;
    }
    return null;
  }

  function taskDetail(t) {
    const d = t.detail || { req: "", criteria: [], env: "", timeline: [], delivery: [], review: "" };
    return `
      <div class="dt-head">
        ${ico(t.icon)}
        <div>
          <h2 class="dt-title">${t.title}</h2>
          <div class="dt-state"><span class="dot ${t.status}"></span>${STATUS[t.status]} · <span style="font-family:var(--mono);font-size:12px">${t.id}</span></div>
        </div>
      </div>
      <div class="dt-sec"><div class="dt-label">要做什么</div><div class="dt-text">${d.req || t.meta}</div></div>
      ${d.criteria.length ? `<div class="dt-sec"><div class="dt-label">怎样算完成</div><ul class="dt-list">${d.criteria.map((c) => `<li>${c}</li>`).join("")}</ul></div>` : ""}
      ${admin() && d.env ? `<div class="dt-sec"><div class="dt-label">在哪里做</div><div class="dt-text">${d.env}</div></div>` : ""}
      ${
        !admin() && d.timeline.length
          ? `<div class="dt-sec"><div class="dt-label">过程</div><div class="dt-text">已经做了 ${d.timeline.length} 步${t.status === "running" ? "，还在继续" : ""}。每一步的细节只有管理员看得到。</div></div>`
          : ""
      }
      ${
        admin() && d.timeline.length
          ? `<div class="dt-sec"><div class="dt-label">过程${t.status === "running" ? " · 实时" : ""}</div><ol class="tl">${d.timeline
              .map(
                (s) => `
            <li class="${s.ok ? "ok" : "bad"}">
              <div class="tl-top"><span>${s.t}</span><span class="tl-actor">${s.actor}</span><span class="tl-tool">${s.tool}</span><span class="tl-ms">${s.ms >= 1000 ? (s.ms / 1000).toFixed(1) + " 秒" : s.ms + " 毫秒"}</span></div>
              <div class="tl-io">${s.input}<span class="arrow">→</span><span class="tl-out">${s.output}</span></div>
            </li>`
              )
              .join("")}</ol></div>`
          : ""
      }
      ${d.review ? `<div class="dt-sec"><div class="dt-label">验收意见</div><div class="dt-text">${d.review}</div></div>` : ""}
      ${
        d.delivery.length
          ? `<div class="dt-sec"><div class="dt-label">交付</div>${d.delivery
              .map((x) => `<div class="deliv">${ico(x.icon)}<div><div class="deliv-k">${x.kind}</div><div class="deliv-t">${x.text}</div><div class="deliv-s">${x.state}</div></div></div>`)
              .join("")}</div>`
          : ""
      }
      <div class="actions" style="margin-top:28px"${admin() ? "" : " hidden"}>
        ${
          ["running", "reviewing", "queued", "waiting"].includes(t.status)
            ? `<button class="btn" data-act="noop" data-msg="已暂停（原型）">暂停</button><button class="btn" data-act="noop" data-msg="已取消（原型）">取消</button>`
            : t.status === "failed"
              ? `<button class="btn primary" data-act="noop" data-msg="已重新排队（原型）">重试</button>`
              : `<button class="btn" data-act="noop" data-msg="已重新发布，24 小时有效（原型）">重新发布</button>`
        }
      </div>`;
  }

  function goalDetail(goal) {
    const done = goal.criteria.filter((c) => c.done).length;
    const t = goal.task && findTask(goal.task);
    return `
      <div class="dt-head">
        ${ico(goal.icon)}
        <div>
          <h2 class="dt-title">${goal.title}</h2>
          <div class="dt-state"><span class="dot running"></span>推进中 · ${done} / ${goal.criteria.length}</div>
        </div>
      </div>
      <div class="dt-sec"><div class="dt-text">${goal.body}</div></div>
      <div class="dt-sec"><div class="dt-label">完成标准</div>
        <ul class="checks">${goal.criteria.map((c) => `<li class="${c.done ? "done" : ""}"><span class="tick">${c.done ? SVG.check : ""}</span>${c.text}</li>`).join("")}</ul>
      </div>
      <div class="dt-sec"><div class="dt-label">最近一次</div><div class="dt-text">${goal.last}</div></div>
      <div class="dt-sec"><div class="dt-label">下次</div><div class="dt-text">${goal.next}</div></div>
      <div class="dt-sec"><div class="dt-label">来源</div><div class="dt-text">${goal.by}</div></div>
      ${t ? `<div class="dt-sec"><div class="dt-label">正在做的任务</div>${taskRow(t, 0)}</div>` : ""}`;
  }

  function detailHTML() {
    if (!state.detail) return "";
    if (state.detail.type === "task") {
      const t = findTask(state.detail.id);
      return t ? taskDetail(t) : "";
    }
    const goal = findGoal(state.detail.id);
    return goal ? goalDetail(goal) : "";
  }

  /* ───────────── 设置 / 群切换（抽屉） ───────────── */

  function groupPicker() {
    return `<h1 class="h-page">切换群</h1>${D.groups
      .map(
        (g) => `
      <button class="gpick" data-act="group" data-g="${g.id}">
        ${ico(g.icon)}
        <span><span class="gpick-name" style="display:block">${g.name}</span><span class="gpick-sub">${g.members} 人 · ${g.quiet.text}</span></span>
        ${g.id === state.g ? SVG.check : ""}
      </button>`
      )
      .join("")}`;
  }

  function settings() {
    return `
      <h1 class="h-page">设置</h1>
      <h2 class="h-sub" style="margin-top:20px">各个群</h2>
      ${D.groups
        .map(
          (g) => `
        <button class="gpick" data-act="group" data-g="${g.id}">
          ${ico(g.icon)}
          <span><span class="gpick-name" style="display:block;font-size:16px">${g.name}</span>
          <span class="gpick-sub">${g.fresh ? "刚开始服务 · 还在熟悉" : `今天 ${g.today.news} 条资讯 · 开话题 ${g.today.topics} 次 · 待批 ${g.tasks.pending.length}`}</span></span>
          <span class="chev" style="color:var(--gray-2)">${SVG.right}</span>
        </button>`
        )
        .join("")}
      ${modelsSummary()}
      <h2 class="h-sub">今天的用量</h2>
      <div class="usage">${D.usage.today.map((u) => `<div><b>${u.value}</b><span>${u.label} · ${u.unit}</span></div>`).join("")}</div>
      <p class="fine">${D.usage.alert}。只提醒，不暂停。</p>
      <h2 class="h-sub">运行状态</h2>
      ${D.health.map((h) => `<div class="set-row">${ico(h.icon)}<div><div class="set-name">${h.name}</div><div class="set-text">${h.text}</div></div><span class="dot ${h.state}"></span></div>`).join("")}
      <h2 class="h-sub">规则</h2>
      ${D.rules.map((r) => `<div class="set-row">${ico(r.icon)}<div><div class="set-name">${r.name}</div><div class="set-text">${r.text}</div></div><span class="chev" style="color:var(--gray-2);width:16px">${SVG.right}</span></div>`).join("")}
      <h2 class="h-sub">群成员看到的链接</h2>
      ${D.groups
        .map(
          (g) => `<div class="set-row">${ico("link")}<div><div class="set-name">${g.name}</div><div class="set-text" style="font-family:var(--mono);font-size:12.5px">https://mw.example/g/${g.token}</div></div><button class="btn" data-act="copy" data-t="${g.token}" style="height:34px;font-size:13px">复制</button></div>`
        )
        .join("")}
      <p class="fine">群成员打开自己群的链接，只看得到这个群；看不到别的群、关注成员和设置。链接泄露了可以重置。</p>
      <div class="actions" style="margin-top:22px"><button class="btn" data-act="logout">退出管理员</button></div>
      <div class="proto-note">这是界面原型：所有群、消息、任务都是虚构的示例数据。按钮可以点，但只会改这个页面里的数据，不会碰到 MaiBot。</div>`;
  }

  /* 模型：设置里的摘要 + 单独的修改抽屉 */
  function modelsSummary() {
    const m = D.models;
    const ready = m.baseUrl && m.keySet && m.main && m.worker;
    const backup = (b) => (b ? `备用 ${b}` : "没设备用");
    const row = (icon, name, text) => `<div class="set-row">${ico(icon)}<div><div class="set-name">${name}</div><div class="set-text">${text}</div></div><span></span></div>`;
    return `
      <div class="h-sub-row"><h2 class="h-sub">模型</h2><button class="btn" data-act="models" style="height:34px;font-size:13px">修改</button></div>
      ${ready ? "" : `<div class="warn-box">还没配好模型，MaiWork 现在什么都不会做。点「修改」填上端点和模型。</div>`}
      ${row("link", "端点", `<span class="mono">${m.baseUrl || "没填"}</span> · 密钥${m.keySet ? "已填" : "<b>没填</b>"}`)}
      ${row("robot", "主模型", `${m.main ? `<span class="mono">${m.main}</span>` : "<b>没选</b>"} · ${backup(m.mainBackup)}<br>理解群、出资讯和构想、派活和验收`)}
      ${row("tools", "子 agent 模型", `${m.worker ? `<span class="mono">${m.worker}</span>` : "<b>没选</b>"} · ${backup(m.workerBackup)}<br>真正动手干活`)}
      ${row("sparkles", "Jev", "单独连 TypeSafe，不用在这里配")}`;
  }

  function modelSelect(id, value, list, allowEmpty) {
    const opts = (allowEmpty ? [`<option value="">不用备用</option>`] : [`<option value="" disabled ${value ? "" : "selected"}>选一个模型</option>`])
      .concat(list.map((x) => `<option value="${x}" ${x === value ? "selected" : ""}>${x}</option>`));
    // 列表里没有的旧值也保留，避免端点换了以后悄悄丢掉
    if (value && !list.includes(value)) opts.push(`<option value="${value}" selected>${value}（端点里没找到）</option>`);
    return `<select id="${id}" name="${id}">${opts.join("")}</select>`;
  }

  function modelsSheet() {
    const m = D.models;
    return `
      <button class="back" data-act="settings">${SVG.left}设置</button>
      <h1 class="h-page">模型</h1>
      <p class="h-meta" style="font-size:15px;line-height:1.6">MaiWork 用自己的模型，不占用 MaiBot 的。填一个 OpenAI 兼容的地址（比如 NewAPI），再选模型。</p>
      <form id="models" class="login" autocomplete="off">
        <label for="m-url">端点地址</label>
        <input id="m-url" name="m-url" type="url" inputmode="url" spellcheck="false" value="${m.baseUrl}" placeholder="https://…/v1" />
        <label for="m-key" style="margin-top:6px">API 密钥</label>
        <input id="m-key" name="m-key" type="password" autocomplete="new-password" placeholder="${m.keySet ? "已填写 · 留空就不改" : "粘贴密钥"}" />
        <p class="fine" style="margin:0">密钥只存在服务器上，保存后网页上也看不到，只会显示「已填」。</p>
        <div class="actions" style="margin-top:4px"><button class="btn" type="button" data-act="models-test">测试连接</button><span class="fine" id="m-check" style="margin:0;align-self:center">${m.checked || ""}</span></div>

        <h2 class="h-sub">主模型</h2>
        <p class="fine" style="margin:0 0 4px">理解群、出资讯和构想、派活和验收。选聪明一点的。</p>
        ${modelSelect("m-main", m.main, m.available)}
        <label for="m-main-b">出错时换用</label>
        ${modelSelect("m-main-b", m.mainBackup, m.available, true)}

        <h2 class="h-sub">子 agent 模型</h2>
        <p class="fine" style="margin:0 0 4px">真正动手写代码、查资料、做文件。用得最多，选便宜耐用的。</p>
        ${modelSelect("m-worker", m.worker, m.available)}
        <label for="m-worker-b">出错时换用</label>
        ${modelSelect("m-worker-b", m.workerBackup, m.available, true)}

        <p class="err" id="m-err" hidden></p>
        <button class="btn primary" type="submit" style="width:100%;height:48px;margin-top:10px">保存</button>
        <p class="fine">保存后下一次调用就用新设置，正在跑的任务不受影响。不会让 MaiBot 重载插件。</p>
      </form>`;
  }

  function loginSheet() {
    return `
      <h1 class="h-page">管理员</h1>
      <p class="h-meta" style="font-size:15px;line-height:1.6">输入密码后能看到所有群，也能批准任务、修改群画像、查看设置。</p>
      <form id="login" class="login" autocomplete="off">
        <label for="pw">密码</label>
        <input id="pw" name="pw" type="password" inputmode="text" autocomplete="current-password" placeholder="管理员密码" />
        <p class="err" ${state.loginError ? "" : "hidden"}>密码不对，再试一次。</p>
        <button class="btn primary" type="submit" style="width:100%;height:48px;margin-top:6px">进入管理</button>
        <p class="fine">原型里的密码是 <span style="font-family:var(--mono)">${PROTO_PASSWORD}</span></p>
      </form>`;
  }

  /* ───────────── 渲染 ───────────── */

  function renderTop() {
    const g = grp();
    $("top").innerHTML = `
      <img class="top-avatar" src="${D.bot.avatar}" alt="${D.bot.name}" />
      ${
        admin()
          ? `<button class="chip" data-act="groups" aria-label="切换群，当前：${g.name}">${g.name}${SVG.down}</button>`
          : `<div class="chip">${g.name}</div>`
      }
      <div class="chip-sub"><span class="dot ${g.fresh ? "" : "ok"}"></span>${g.quiet.text}</div>
      ${
        admin()
          ? `<button class="round-btn" data-act="settings" aria-label="设置">${SVG.sliders}</button>`
          : `<button class="round-btn" data-act="login" aria-label="管理员登录">${SVG.key}</button>`
      }`;
  }

  function renderTabbar() {
    const g = grp();
    const idx = TABS.findIndex((t) => t.id === state.tab);
    $("tabbar").innerHTML =
      `<span class="tab-pill" style="transform:translateX(${idx * 100}%)"></span>` +
      TABS.map(
        (t) => `
        <button class="tab" data-act="tab" data-tab="${t.id}" aria-current="${t.id === state.tab}">
          ${SVG[t.id]}<span class="sr">${t.label}</span>
          ${admin() && t.id === "tasks" && g.tasks.pending.length ? `<span class="badge">${g.tasks.pending.length}</span>` : ""}
        </button>`
      ).join("");
  }

  function renderRail() {
    const g = grp();
    $("rail").innerHTML = `
      <div class="brand">
        <img src="${D.bot.avatar}" alt="${D.bot.name}" />
        <div class="brand-t"><div class="brand-name">MaiWork</div><div class="brand-sub">${admin() ? "管理员 · 全部群" : D.bot.name + "在这个群"}</div></div>
      </div>
      ${admin() ? `<div class="rail-label">群</div>` : ""}
      ${(admin() ? D.groups : [g])
        .map(
          (x) => `
        <button class="r-item" data-act="group" data-g="${x.id}" aria-current="${x.id === state.g}" title="${x.name}">
          ${ico(x.icon)}
          <span class="r-text"><span class="r-name">${x.name}</span><span class="r-sub">${x.quiet.text}</span></span>
          ${admin() && x.tasks.pending.length ? `<span class="r-badge">${x.tasks.pending.length}</span><span class="r-dot"></span>` : ""}
        </button>`
        )
        .join("")}
      <div class="rail-sep"></div>
      <div class="rail-label">${g.name}</div>
      ${TABS.map(
        (t) => `
        <button class="r-item" data-act="tab" data-tab="${t.id}" aria-current="${t.id === state.tab}" title="${t.label}">
          ${SVG[t.id]}<span class="r-text"><span class="r-name">${t.label}</span></span>
          ${admin() && t.id === "tasks" && g.tasks.pending.length ? `<span class="r-badge">${g.tasks.pending.length}</span><span class="r-dot"></span>` : ""}
        </button>`
      ).join("")}
      <div class="rail-foot">
        ${
          admin()
            ? `<button class="r-item" data-act="settings" title="设置">${SVG.sliders}<span class="r-text"><span class="r-name">设置</span></span></button>`
            : `<button class="r-item" data-act="login" title="管理员">${SVG.key}<span class="r-text"><span class="r-name">管理员</span></span></button>`
        }
      </div>`;
  }

  function renderSide() {
    if (!desktop.matches) return;
    const g = grp();
    if (state.detail) {
      $("side").innerHTML = `<button class="side-close" data-act="side-close" aria-label="关闭详情">${SVG.close}</button>${detailHTML()}`;
      return;
    }
    const running = g.tasks.list.filter((t) => ["running", "reviewing", "queued"].includes(t.status)).length;
    $("side").innerHTML = `
      ${pulseCard(g, true)}
      <h2 class="h-sub">今天</h2>
      <div class="stats">
        <button class="stat" data-act="tab" data-tab="news" style="text-align:left"><b>${g.today.news}</b><span>条资讯</span></button>
        <button class="stat" data-act="tab" data-tab="group" style="text-align:left"><b>${g.today.topics}</b><span>次开话题</span></button>
        <button class="stat" data-act="tab" data-tab="tasks" style="text-align:left"><b>${g.tasks.pending.length}</b><span>${admin() ? "件等你批准" : "件等批准"}</span></button>
        <button class="stat" data-act="tab" data-tab="tasks" style="text-align:left"><b>${running}</b><span>件在做</span></button>
      </div>
      <h2 class="h-sub">接下来</h2>
      ${g.upcoming.map((u) => `<div class="next">${ico(u.icon)}<div><div class="next-time">${u.time}</div><div class="next-text">${u.text}</div></div></div>`).join("")}`;
  }

  function renderView() {
    const g = grp();
    const views = { news: viewNews, ideas: viewIdeas, goals: viewGoals, tasks: viewTasks, group: viewGroup };
    const v = $("view");
    v.innerHTML = views[state.tab](g);
    if (!flash) v.querySelectorAll(".enter").forEach((el) => el.classList.remove("enter"));
    flash = false;
  }

  function landing() {
    return `
      <div class="landing">
        <img class="top-avatar" src="${D.bot.avatar}" alt="${D.bot.name}" style="width:120px;height:120px" />
        <h1 class="h-page" style="margin-top:22px">MaiWork</h1>
        <p class="landing-text">${D.bot.name}在群里的后台：资讯、构想、目标和任务都在这里。</p>
        <div class="landing-box">
          <div class="landing-t">群友</div>
          <p>在群里发 <b>/mw 网页</b>，${D.bot.name}会回一个本群专属链接，打开就能看到这个群的内容。</p>
          <div class="landing-t" style="margin-top:14px">原型里试试看</div>
          ${D.groups.map((g) => `<a class="gpick" href="#/${g.token}/news">${ico(g.icon)}<span><span class="gpick-name" style="display:block;font-size:16px">${g.name}</span><span class="gpick-sub">群链接 · <span style="font-family:var(--mono)">/g/${g.token}</span></span></span>${SVG.right}</a>`).join("")}
        </div>
        <button class="btn primary" data-act="login" style="margin-top:20px;height:48px;padding:0 28px">管理员登录</button>
      </div>`;
  }

  function render() {
    document.body.classList.toggle("no-link", !!state.noLink);
    if (state.noLink) {
      $("view").innerHTML = landing();
      document.title = "MaiWork";
      return;
    }
    renderTop();
    renderTabbar();
    renderRail();
    renderView();
    renderSide();
    const label = TABS.find((t) => t.id === state.tab).label;
    document.title = `${label} · ${grp().name} · MaiWork`;
  }

  /* ───────────── 抽屉 ───────────── */

  function openSheet(kind) {
    state.sheet = kind;
    const html =
      kind === "groups" ? groupPicker() : kind === "settings" ? settings() : kind === "models" ? modelsSheet() : kind === "login" ? loginSheet() : detailHTML();
    const sh = $("sheet");
    sh.innerHTML = `<div class="grab"></div><button class="sheet-close" data-act="close" aria-label="关闭">${SVG.close}</button>${html}`;
    sh.hidden = false;
    $("scrim").hidden = false;
    sh.scrollTop = 0;
    requestAnimationFrame(() => {
      sh.classList.add("on");
      $("scrim").classList.add("on");
    });
    sh.setAttribute("tabindex", "-1");
    sh.focus({ preventScroll: true });
  }

  function closeSheet() {
    if (!state.sheet) return;
    if (state.sheet === "detail") {
      state.detail = null;
      syncHash();
    }
    state.sheet = null;
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
    if (state.noLink) return;
    const h = `#/${grp().token}/${state.tab}` + (state.detail ? `/${state.detail.id}` : "");
    if (location.hash !== h) history.replaceState(null, "", h);
  }
  function readHash() {
    const [, g, tab] = location.hash.split("/");
    const hit = D.groups.find((x) => x.token === g || (admin() && x.id === g));
    state.noLink = !hit && !admin();
    if (hit) state.g = hit.id;
    if (TABS.some((x) => x.id === tab)) state.tab = tab;
    const id = location.hash.split("/")[3];
    if (id && findTask(id)) state.detail = { type: "task", id };
    else if (id && findGoal(id)) state.detail = { type: "goal", id };
    else state.detail = null;
  }

  function go(patch) {
    Object.assign(state, patch);
    flash = true;
    syncHash();
    render();
    window.scrollTo({ top: 0 });
  }

  /* ───────────── 交互 ───────────── */

  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-act]");
    if (!el) return;
    const act = el.dataset.act;
    const g = grp();

    switch (act) {
      case "tab":
        closeSheet();
        go({ tab: el.dataset.tab, detail: null });
        break;
      case "group":
        if (!admin()) break;
        closeSheet();
        go({ g: el.dataset.g, detail: null, filter: "all" });
        break;
      case "groups":
        openSheet("groups");
        break;
      case "settings":
        if (admin()) openSheet("settings");
        break;
      case "models":
        if (admin()) openSheet("models");
        break;
      case "models-test": {
        const url = $("m-url").value.trim();
        const out = $("m-check");
        if (!/^https?:\/\/\S+$/.test(url)) {
          out.textContent = "地址要以 http:// 或 https:// 开头";
          out.style.color = "var(--red)";
          break;
        }
        if (!D.models.keySet && !$("m-key").value.trim()) {
          out.textContent = "先填密钥再测";
          out.style.color = "var(--red)";
          break;
        }
        el.disabled = true;
        out.style.color = "";
        out.textContent = "正在连…";
        setTimeout(() => {
          el.disabled = false;
          D.models.checked = `刚刚测过 · 找到 ${D.models.available.length} 个模型`;
          out.textContent = D.models.checked + "（原型：假装连上了）";
        }, 700);
        break;
      }
      case "login":
        state.loginError = false;
        openSheet("login");
        setTimeout(() => $("pw") && $("pw").focus(), 350);
        break;
      case "logout":
        role = "member";
        sessionStorage.removeItem("mw-role");
        closeSheet();
        go({ detail: null });
        toast("已退出管理员，现在只看这个群");
        break;
      case "copy":
        navigator.clipboard && navigator.clipboard.writeText(`https://mw.example/g/${el.dataset.t}`).catch(() => {});
        toast("链接复制好了（原型地址）");
        break;
      case "idea-want": {
        const it = g.ideas[+el.dataset.k];
        it.state = "wanted";
        g.tasks.pending.push({ id: "T-" + (1000 + Math.floor(Math.random() * 900)), icon: it.icon, title: it.title.replace(/^我可以/, ""), who: "群友（网页）", when: "刚刚", quote: "在网页上点了「想要这个」", via: "来自构想" });
        renderTabbar();
        renderRail();
        renderSide();
        renderView();
        toast("已经告诉管理员了");
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
      case "link":
        e.preventDefault();
        toast("原型里的链接不会跳转");
        break;
      case "fb": {
        const { key, v } = el.dataset;
        state.fb[key] = state.fb[key] === v ? undefined : v;
        renderView();
        if (state.fb[key]) toast(v === "up" ? "记下了：有用，以后多找这类" : "记下了：没用，以后少找这类");
        break;
      }
      case "idea-do": {
        const it = g.ideas[+el.dataset.k];
        it.state = "started";
        g.tasks.list.unshift({ id: "T-" + (1000 + Math.floor(Math.random() * 900)), icon: it.icon, title: it.title.replace(/^我可以/, ""), status: "queued", meta: "你刚批准的 · 等空出来的子 agent" });
        renderView();
        renderSide();
        toast("已开工，排进任务了");
        break;
      }
      case "idea-no":
        g.ideas[+el.dataset.k].state = "dismissed";
        renderView();
        toast("收起了，以后不再提");
        break;
      case "approve":
      case "reject": {
        const i = g.tasks.pending.findIndex((p) => p.id === el.dataset.id);
        const [p] = g.tasks.pending.splice(i, 1);
        if (act === "approve") {
          g.tasks.list.unshift({ id: p.id, icon: p.icon, title: p.title, status: "queued", meta: `${p.who} 请求的 · 刚批准，等空出来的子 agent` });
          toast(`已批准，会在群里告诉 ${p.who}`);
        } else {
          toast(`已拒绝，会在群里告诉 ${p.who}`);
        }
        const idea = g.ideas.find((x) => x.state === "pending" && p.id === "T-0931");
        if (idea) idea.state = act === "approve" ? "started" : "dismissed";
        renderView();
        renderTabbar();
        renderRail();
        renderSide();
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
      case "verdict": {
        const t = g.topicLog[+el.dataset.k];
        t.verdict = t.verdict === el.dataset.v ? null : el.dataset.v;
        renderView();
        if (t.verdict) toast("标注好了，用来调冷场判断");
        break;
      }
      case "pf-lock": {
        const e2 = g.profile[+el.dataset.s].entries[+el.dataset.k];
        e2.locked = !e2.locked;
        renderView();
        toast(e2.locked ? "锁定了，MaiWork 不会再改这条" : "解锁了");
        break;
      }
      case "pf-del":
        g.profile[+el.dataset.s].entries.splice(+el.dataset.k, 1);
        renderView();
        toast("删掉了，这条不会再被写回来");
        break;
      case "noop":
        toast(el.dataset.msg);
        break;
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (state.sheet) closeSheet();
      else if (state.detail) {
        state.detail = null;
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
  document.addEventListener("submit", (e) => {
    if (e.target.id === "models") {
      e.preventDefault();
      const v = (id) => $(id).value.trim();
      const err = $("m-err");
      const problem = !/^https?:\/\/\S+$/.test(v("m-url"))
        ? "端点地址要以 http:// 或 https:// 开头。"
        : !D.models.keySet && !v("m-key")
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
      const newKey = !!v("m-key");
      Object.assign(D.models, {
        baseUrl: v("m-url"),
        keySet: D.models.keySet || newKey,
        main: v("m-main"),
        mainBackup: v("m-main-b"),
        worker: v("m-worker"),
        workerBackup: v("m-worker-b"),
      });
      openSheet("settings");
      toast(newKey ? "保存好了，新密钥已替换旧的" : "保存好了，下一次调用就用新设置");
      return;
    }
    if (e.target.id !== "login") return;
    e.preventDefault();
    if ($("pw").value === PROTO_PASSWORD) {
      role = "admin";
      sessionStorage.setItem("mw-role", "admin");
      state.noLink = false;
      closeSheet();
      go({});
      toast("已进入管理员模式，能看到全部群");
    } else {
      state.loginError = true;
      e.target.querySelector(".err").hidden = false;
      $("pw").select();
    }
  });
  desktop.addEventListener("change", () => {
    closeSheet();
    render();
  });
  window.addEventListener("hashchange", () => {
    readHash();
    flash = true;
    render();
  });

  readHash();
  syncHash();
  render();
  if (state.detail && !desktop.matches) openSheet("detail");
  // 仅原型：?sheet=settings / ?sheet=models 直接打开对应抽屉，方便截图
  const demoSheet = new URLSearchParams(location.search).get("sheet");
  if (admin() && ["settings", "models"].includes(demoSheet)) openSheet(demoSheet);
})();
