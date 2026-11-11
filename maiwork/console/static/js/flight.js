// MaiWork 网页 · 看板流向图上的请求飞包动画。
// 每个运行事件对应一个沿真实 SVG 连线运动的小圆点；仅在有在途动画且未开启减弱动态效果时请求动画帧。
import { calm } from "./util.js";

const NS = "http://www.w3.org/2000/svg";
const EASE = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

let trips = [];
let frame = 0;
let host = null;

export function setFlightHost(fn) {
  host = fn;
}

function sky() {
  if (typeof document === "undefined") return null;
  const box = host ? host() : null;
  if (!box) return null;
  let s = box.querySelector(":scope > .bflow-sky");
  if (!s) {
    s = document.createElementNS(NS, "svg");
    s.setAttribute("class", "bflow-sky");
    s.setAttribute("aria-hidden", "true");
    box.append(s);
  }
  const r = box.getBoundingClientRect();
  if (r.width > 0 && r.height > 0) {
    s.setAttribute("viewBox", `0 0 ${r.width.toFixed(1)} ${r.height.toFixed(1)}`);
  }
  return s;
}

function escSel(k) {
  if (typeof CSS !== "undefined" && CSS.escape) return CSS.escape(k);
  return String(k).replace(/["\\]/g, "\\$&");
}

function wire(k) {
  const box = host ? host() : null;
  if (!box) return null;
  return box.querySelector(`.bflow-wires path[data-k="${escSel(k)}"]`);
}

export function packet(cls = "main") {
  const s = sky();
  if (!s) return null;
  const c = document.createElementNS(NS, "circle");
  c.setAttribute("r", "4.2");
  c.setAttribute("class", `bflow-pkt ${cls}`);
  c.setAttribute("visibility", "hidden");
  s.append(c);
  return c;
}

function place(dot, leg, f) {
  const p = leg ? wire(leg.k) : null;
  if (!p || !dot || !dot.isConnected) return false;
  let L = 0;
  try {
    L = p.getTotalLength();
  } catch (_) {
    return false;
  }
  if (!L) return false;
  const clamped = Math.max(0, Math.min(1, f));
  const pt = p.getPointAtLength((leg.back ? 1 - clamped : clamped) * L);
  dot.setAttribute("cx", pt.x.toFixed(1));
  dot.setAttribute("cy", pt.y.toFixed(1));
  dot.setAttribute("visibility", "visible");
  return true;
}

function tick(now) {
  frame = 0;
  sky();
  trips = trips.filter((tr) => {
    const k = Math.min(1, Math.max(0, (now - tr.t0) / Math.max(1, tr.ms)));
    const e = EASE(k) * tr.legs.length;
    const i = Math.min(tr.legs.length - 1, Math.floor(e));
    const ok = place(tr.dot, tr.legs[i], e - i);
    if (k >= 1 || !ok) {
      tr.res();
      return false;
    }
    return true;
  });
  if (trips.length && typeof requestAnimationFrame === "function") {
    frame = requestAnimationFrame(tick);
  }
}

export function fly(dot, legs, ms = 420) {
  return new Promise((res) => {
    if (!dot || !Array.isArray(legs) || !legs.length) {
      res();
      return;
    }
    if (calm() || (typeof document !== "undefined" && document.hidden) || typeof requestAnimationFrame !== "function") {
      place(dot, legs[legs.length - 1], 1);
      res();
      return;
    }
    trips.push({ dot, legs, ms, t0: performance.now(), res });
    if (!frame) frame = requestAnimationFrame(tick);
  });
}

export function hold(dot, leg) {
  if (!dot || !leg) return;
  if (place(dot, leg, 1)) {
    dot.classList.add("held");
    dot._hold = leg;
  }
}

export function rehold() {
  const box = host ? host() : null;
  if (!box) return;
  const s = box.querySelector(":scope > .bflow-sky");
  if (!s) return;
  sky();
  for (const d of s.querySelectorAll(".bflow-pkt.held")) {
    if (d._hold) place(d, d._hold, 1);
  }
}

export function drop(dot, ms = 240) {
  if (!dot || !dot.isConnected) return;
  dot.classList.remove("held");
  dot.classList.add("gone");
  setTimeout(() => dot.remove(), calm() ? 0 : ms);
}

export function burst(dot, cls = "done") {
  if (!dot) return;
  dot.classList.add(cls);
  drop(dot, 460);
}

export function clearFlights() {
  if (frame && typeof cancelAnimationFrame === "function") cancelAnimationFrame(frame);
  frame = 0;
  for (const tr of trips) tr.res();
  trips = [];
  const box = host ? host() : null;
  const s = box && box.querySelector(":scope > .bflow-sky");
  if (s) s.innerHTML = "";
}
