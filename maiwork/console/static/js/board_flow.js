// MaiWork 网页 · 看板顶部实时处理流向图（5 列 SVG 贝塞尔连线 + 请求飞包动画 + SSE 推送）。
// 仅总管理员停留在看板且流向面板展开、页面处于前台时建立 EventSource("/api/board/events")。
import { admin, state } from "./state.js";
import { dur, esc, hhmm, msText, now } from "./util.js";
import { fishSvg } from "./fish.js";
import { burst, clearFlights, drop, fly, hold, packet, rehold, setFlightHost } from "./flight.js";

const NS = "http://www.w3.org/2000/svg";
const DEFAULT_WORKERS = [
  ["news", "资讯"],
  ["idea", "构想"],
  ["goal", "目标"],
  ["task", "通用执行"],
];

let sse = null;
const inflight = new Map(); // flight_id -> { dot, stage, group_id, role }

setFlightHost(() => (typeof document !== "undefined" ? document.getElementById("board-flow") : null));

function workerNodes(lanes) {
  const map = new Map(DEFAULT_WORKERS);
  const seeds = new Map();
  for (const l of lanes || []) {
    if (!l || !l.kind || l.kind === "main") continue;
    map.set(l.kind, l.kind_title || map.get(l.kind) || l.kind);
    if (typeof l.fish_seed === "string") seeds.set(l.kind, l.fish_seed);
  }
  return [...map.entries()].slice(0, 6).map(([kind, title]) => ({
    kind,
    title,
    seed: seeds.get(kind) || "",
  }));
}

function sourceGroups(b, filterGid) {
  const out = [];
  const add = (id, name) => {
    if (!id) return;
    if (filterGid && id !== filterGid) return;
    if (!out.some((g) => g.id === id)) out.push({ id: String(id), name: String(name || id) });
  };
  for (const g of state.groups || []) add(g.id, g.name || `群 ${g.id}`);
  for (const l of (b && b.lanes) || []) {
    for (const x of [...(l.running || []), ...(l.history || [])]) add(x.group_id, x.group_name);
  }
  const live = (b && b.live) || {};
  for (const a of live.active || []) add(a.group_id, a.group_name);
  return out.slice(0, 5);
}

function mainFishSeed(b) {
  const lane = ((b && b.lanes) || []).find((l) => l.kind === "main");
  return (lane && typeof lane.fish_seed === "string" && lane.fish_seed) || "";
}

function normalizeRole(role) {
  const r = String(role || "");
  if (!r || r === "main" || r === "admin_chat" || r === "jev") return r || "main";
  if (r.startsWith("worker:")) return r.slice(7) || "task";
  if (r.startsWith("specialist:")) return r.slice(11) || "task";
  if (r.includes(".")) return r.split(".")[0];
  return r;
}

function stageLabel(stage, status) {
  const st = {
    signal: "群内信号",
    gate: "Jev 判断",
    main: "主模型",
    worker: "专岗执行",
    outbox: "交付发送",
  }[stage] || stage || "运行";
  const suffix = status === "first_token" ? "已出首字" : status === "error" ? "失败" : status === "done" ? "完成" : "开始";
  return `${st} · ${suffix}`;
}

function flowInnerHTML(b, filterGid = "") {
  const live = (b && b.live) || {};
  const activeAll = Array.isArray(live.active) ? live.active : [];
  const active = filterGid ? activeAll.filter((a) => !a.group_id || a.group_id === filterGid) : activeAll;
  const recentAll = Array.isArray(live.recent) ? live.recent : [];
  const recent = (filterGid ? recentAll.filter((e) => !e.group_id || e.group_id === filterGid) : recentAll).slice(-4).reverse();

  const groups = sourceGroups(b, filterGid);
  const workers = workerNodes(b && b.lanes);

  const gateActive = active.filter((a) => a.stage === "gate");
  const mainActive = active.filter((a) => a.stage === "main");
  const outActive = active.filter((a) => a.stage === "outbox");

  const gateRecent = recentAll.filter((e) => e.stage === "gate" && e.ms > 0);
  const lastGateMs = gateRecent.length ? gateRecent[gateRecent.length - 1].ms : 0;
  const mainFt = mainActive.find((a) => a.first_token_ms > 0);
  const mainElapsed = mainActive.length ? Math.max(0, Math.round(now() - (mainActive[0].started_ts || now()))) : 0;

  const srcCol = groups.length
    ? groups
        .map((g) => {
          const busy = active.some((a) => a.group_id === g.id);
          return `<div class="fl-node fl-src-node${busy ? " on" : ""}" data-fl="s:${esc(g.id)}">
            <span class="dot ${busy ? "running" : "ok"}"></span>
            <span class="fl-name">${esc(g.name)}</span>
          </div>`;
        })
        .join("")
    : `<div class="fl-node fl-src-node" data-fl="s:_all"><span class="dot"></span><span class="fl-name">服务群</span></div>`;

  const workerCol = workers
    .map((w) => {
      const wAct = active.filter((a) => a.stage === "worker" && normalizeRole(a.role) === w.kind);
      const lane = ((b && b.lanes) || []).find((l) => l.kind === w.kind);
      const runCnt = wAct.length || ((lane && (lane.running || []).filter((x) => !filterGid || x.group_id === filterGid).length) || 0);
      const ft = wAct.find((a) => a.first_token_ms > 0);
      return `<div class="fl-node fl-worker-node${runCnt ? " on" : ""}" data-fl="w:${esc(w.kind)}">
        <span class="fl-fish">${fishSvg(`agent:${w.kind}:${w.seed}`, { size: 22, still: !runCnt })}</span>
        <div class="fl-txt">
          <b>${esc(w.title)}</b>
          <span>${runCnt ? `执行中 ${runCnt}` : "空闲"}${ft ? ` · 首字 ${esc(msText(ft.first_token_ms))}` : ""}</span>
        </div>
      </div>`;
    })
    .join("");

  const outSent = recentAll.filter((e) => e.stage === "outbox" && e.status === "done").length;

  const ticker = recent.length
    ? `<div class="bflow-ticker">${recent
        .map((e) => {
          const g = e.group_name || e.group_id || "";
          const t = e.first_token_ms > 0 && e.status === "first_token" ? `首字 ${msText(e.first_token_ms)}` : e.ms > 0 ? msText(e.ms) : "";
          return `<span class="bflow-ev${e.ok === false ? " bad" : ""}">
            <time>${esc(hhmm(e.ts || now()))}</time>
            <b>${esc(stageLabel(e.stage, e.status))}</b>
            ${g ? `<span>${esc(g)}</span>` : ""}
            ${t ? `<em>${esc(t)}</em>` : ""}
          </span>`;
        })
        .join("")}</div>`
    : "";

  return `<div class="bflow-grid">
      <div class="fl-col fl-src"><div class="fl-cap">信号来源</div>${srcCol}</div>
      <div class="fl-col fl-gate">
        <div class="fl-cap">快速判断</div>
        <div class="fl-node fl-gate-node${gateActive.length ? " on" : ""}" data-fl="gate">
          <div class="fl-txt">
            <b>Jev 门控</b>
            <span>${gateActive.length ? `判断中 ${gateActive.length}` : lastGateMs ? `最近 ${esc(msText(lastGateMs))}` : "待命"}</span>
          </div>
        </div>
      </div>
      <div class="fl-col fl-main">
        <div class="fl-cap">决策中枢</div>
        <div class="fl-node fl-main-node${mainActive.length ? " on" : ""}" data-fl="main">
          <span class="fl-fish">${fishSvg(`agent:main:${mainFishSeed(b)}`, { size: 32, still: !mainActive.length })}</span>
          <div class="fl-txt">
            <b>主模型</b>
            <span>${mainActive.length ? `思考中 · ${esc(dur(mainElapsed))}` : "待命"}${mainFt ? ` · 首字 ${esc(msText(mainFt.first_token_ms))}` : ""}</span>
          </div>
        </div>
      </div>
      <div class="fl-col fl-worker"><div class="fl-cap">专岗执行</div>${workerCol}</div>
      <div class="fl-col fl-out">
        <div class="fl-cap">结果出口</div>
        <div class="fl-node fl-out-node${outActive.length ? " on" : ""}" data-fl="outbox">
          <div class="fl-txt">
            <b>网页与群消息</b>
            <span>${outActive.length ? `投递中 ${outActive.length}` : outSent ? `近期投递 ${outSent}` : "就绪"}</span>
          </div>
        </div>
      </div>
    </div>
    <svg class="bflow-wires" aria-hidden="true"></svg>
    ${ticker}`;
}

export function boardFlowHTML(b, filterGid = "") {
  const collapsed = !!state.boardFlowCollapsed;
  const live = (b && b.live) || {};
  const activeCnt = Array.isArray(live.active)
    ? live.active.filter((a) => !filterGid || !a.group_id || a.group_id === filterGid).length
    : 0;
  return `<section class="bflow-card${collapsed ? " is-collapsed" : ""}" aria-label="实时处理流向">
    <header class="bflow-head">
      <div class="bflow-title">
        <span class="dot ${activeCnt ? "running" : "ok"}"></span>
        <b>实时流向</b>
        <span class="bflow-sub">${activeCnt ? `${activeCnt} 项在途调用` : "当前空闲 · 实时监听运行事件"}</span>
      </div>
      <button type="button" class="btn small ghost" data-act="board-flow-toggle" aria-expanded="${!collapsed}">${collapsed ? "展开" : "收起"}</button>
    </header>
    ${collapsed ? "" : `<div class="bflow" id="board-flow" data-filter="${esc(filterGid)}">${flowInnerHTML(b, filterGid)}</div>`}
  </section>`;
}

function drawWires() {
  if (typeof document === "undefined") return;
  const box = document.getElementById("board-flow");
  if (!box) return;
  const svg = box.querySelector(".bflow-wires");
  if (!svg) return;
  const br = box.getBoundingClientRect();
  if (br.width < 640 || br.height < 40) {
    svg.innerHTML = "";
    return;
  }
  svg.setAttribute("viewBox", `0 0 ${br.width.toFixed(1)} ${br.height.toFixed(1)}`);
  const pos = (el, side) => {
    const r = el.getBoundingClientRect();
    return [side === "r" ? r.right - br.left : r.left - br.left, r.top - br.top + r.height / 2];
  };
  const curve = (a, b) => {
    const dx = Math.max(24, Math.abs(b[0] - a[0]) * 0.46);
    return `M${a[0].toFixed(1)},${a[1].toFixed(1)} C${(a[0] + dx).toFixed(1)},${a[1].toFixed(1)} ${(b[0] - dx).toFixed(1)},${b[1].toFixed(1)} ${b[0].toFixed(1)},${b[1].toFixed(1)}`;
  };
  const paths = [];
  const addWire = (fromEl, toEl, key) => {
    if (!fromEl || !toEl) return;
    const p = document.createElementNS(NS, "path");
    p.setAttribute("d", curve(pos(fromEl, "r"), pos(toEl, "l")));
    p.setAttribute("data-k", key);
    paths.push(p);
  };
  const gate = box.querySelector('[data-fl="gate"]');
  const main = box.querySelector('[data-fl="main"]');
  const out = box.querySelector('[data-fl="outbox"]');
  for (const s of box.querySelectorAll('[data-fl^="s:"]')) {
    addWire(s, gate, `${s.dataset.fl}>gate`);
  }
  addWire(gate, main, "gate>main");
  for (const w of box.querySelectorAll('[data-fl^="w:"]')) {
    addWire(main, w, `main>${w.dataset.fl}`);
    addWire(w, out, `${w.dataset.fl}>outbox`);
  }
  addWire(main, out, "main>outbox");
  svg.replaceChildren(...paths);
}

function legForFlight(a) {
  const box = typeof document !== "undefined" ? document.getElementById("board-flow") : null;
  const gid = a.group_id && box && box.querySelector(`[data-fl="s:${a.group_id}"]`) ? `s:${a.group_id}` : box && box.querySelector('[data-fl^="s:"]') ? box.querySelector('[data-fl^="s:"]').dataset.fl : "s:_all";
  if (a.stage === "gate") return { k: `${gid}>gate` };
  if (a.stage === "main") return { k: "gate>main" };
  if (a.stage === "worker") {
    const wk = `w:${normalizeRole(a.role)}`;
    return box && box.querySelector(`[data-fl="${wk}"]`) ? { k: `main>${wk}` } : { k: "gate>main" };
  }
  if (a.stage === "outbox") {
    const wk = `w:${normalizeRole(a.role)}`;
    return a.role && a.role !== "main" && box && box.querySelector(`[data-fl="${wk}"]`) ? { k: `${wk}>outbox` } : { k: "main>outbox" };
  }
  return { k: `${gid}>gate` };
}

function syncActiveHolds() {
  const live = (state.board && state.board.live) || {};
  const active = Array.isArray(live.active) ? live.active : [];
  const activeIds = new Set(active.map((a) => a.flight_id).filter(Boolean));
  for (const [fid, rec] of inflight.entries()) {
    if (!activeIds.has(fid)) {
      drop(rec.dot, 180);
      inflight.delete(fid);
    }
  }
  for (const a of active) {
    if (!a.flight_id || inflight.has(a.flight_id)) continue;
    const dot = packet(a.stage === "worker" ? "worker" : a.stage === "gate" ? "gate" : "main");
    if (!dot) continue;
    hold(dot, legForFlight(a));
    inflight.set(a.flight_id, { dot, stage: a.stage, group_id: a.group_id, role: a.role });
  }
}

function repaintFlowBox() {
  if (typeof document === "undefined" || !state.board) return;
  const box = document.getElementById("board-flow");
  if (!box) return;
  const sky = box.querySelector(":scope > .bflow-sky");
  const filterGid = box.dataset.filter || "";
  box.innerHTML = flowInnerHTML(state.board, filterGid);
  if (sky) box.append(sky);
  drawWires();
  rehold();
  const card = box.closest(".bflow-card");
  const sub = card && card.querySelector(".bflow-sub");
  const dot = card && card.querySelector(".bflow-title > .dot");
  const active = ((state.board.live && state.board.live.active) || []).filter((a) => !filterGid || !a.group_id || a.group_id === filterGid);
  if (sub) sub.textContent = active.length ? `${active.length} 项在途调用` : "当前空闲 · 实时监听运行事件";
  if (dot) dot.className = `dot ${active.length ? "running" : "ok"}`;
}

function applyRuntimeEvent(ev) {
  if (!state.board) return;
  const live = (state.board.live = state.board.live || { seq: 0, active: [], recent: [] });
  live.seq = Math.max(Number(live.seq) || 0, Number(ev.seq) || 0);
  live.recent = [...(live.recent || []), ev].slice(-30);
  const fid = ev.flight_id || "";
  if (fid) {
    const list = [...(live.active || [])];
    const idx = list.findIndex((x) => x.flight_id === fid);
    if (ev.status === "done" || ev.status === "error") {
      if (idx >= 0) list.splice(idx, 1);
    } else if (idx >= 0) {
      list[idx] = { ...list[idx], ...ev };
    } else if (ev.status === "start" || ev.status === "first_token") {
      list.push({
        flight_id: fid,
        started_ts: ev.ts || now(),
        stage: ev.stage,
        group_id: ev.group_id,
        group_name: ev.group_name,
        role: ev.role,
        model: ev.model,
        first_token_ms: ev.first_token_ms || 0,
      });
    }
    live.active = list;
  }
  repaintFlowBox();
  const leg = legForFlight(ev);
  if (ev.status === "start" && fid) {
    const prev = inflight.get(fid);
    if (prev) drop(prev.dot, 120);
    const dot = packet(ev.stage === "worker" ? "worker" : ev.stage === "gate" ? "gate" : "main");
    if (dot) {
      inflight.set(fid, { dot, stage: ev.stage, group_id: ev.group_id, role: ev.role });
      fly(dot, [leg], 380).then(() => {
        if (inflight.get(fid)?.dot === dot) hold(dot, leg);
      });
    }
  } else if (ev.status === "first_token" && fid) {
    const cur = inflight.get(fid);
    if (cur && cur.dot) cur.dot.classList.add("streaming");
  } else if (ev.status === "done" || ev.status === "error") {
    const cur = fid ? inflight.get(fid) : null;
    if (cur) {
      inflight.delete(fid);
      burst(cur.dot, ev.ok === false || ev.status === "error" ? "err" : "done");
    } else {
      const dot = packet(ev.stage === "outbox" ? "out" : "gate");
      if (dot) fly(dot, [leg], 340).then(() => burst(dot, ev.ok === false ? "err" : "done"));
    }
  }
}

export function stopBoardSSE() {
  if (sse) {
    try { sse.close(); } catch (_) {}
    sse = null;
  }
  inflight.clear();
  clearFlights();
}

export function syncBoardSSE() {
  const want =
    typeof EventSource !== "undefined" &&
    typeof document !== "undefined" &&
    !document.hidden &&
    admin() &&
    state.page === "board" &&
    !state.boardFlowCollapsed;
  if (!want) {
    stopBoardSSE();
    return;
  }
  if (sse) return;
  try {
    sse = new EventSource("/api/board/events");
  } catch (_) {
    sse = null;
    return;
  }
  sse.addEventListener("snapshot", (e) => {
    try {
      const snap = JSON.parse(e.data || "{}");
      if (state.board) {
        state.board.live = snap;
        repaintFlowBox();
        syncActiveHolds();
      }
    } catch (_) {}
  });
  sse.addEventListener("runtime", (e) => {
    try {
      applyRuntimeEvent(JSON.parse(e.data || "{}"));
    } catch (_) {}
  });
  sse.onerror = () => {
    // 连接异常时交由浏览器自动重连；离开看板时由 stopBoardSSE 彻底关闭
  };
}

export function mountBoardFlow() {
  if (typeof document === "undefined") return;
  if (state.page !== "board" || state.boardFlowCollapsed) {
    stopBoardSSE();
    return;
  }
  drawWires();
  syncActiveHolds();
  syncBoardSSE();
}

if (typeof window !== "undefined" && window.addEventListener) {
  window.addEventListener("resize", () => {
    if (state.page === "board" && !state.boardFlowCollapsed) {
      drawWires();
      rehold();
    }
  });
}
if (typeof document !== "undefined" && document.addEventListener) {
  document.addEventListener("visibilitychange", () => {
    if (state.page === "board") syncBoardSSE();
    else stopBoardSSE();
  });
}
