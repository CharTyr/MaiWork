// MaiWork 网页 · 程序生成的小鱼头像。
// 同一个种子永远画出同一条鱼：一整块圆润纯色 + 一对白色眼睛，透明底、不带影子，默认像真鱼一样游（见文件末尾）。
// 做法：按鱼种在轮廓上撒点（身体、尾巴、鳍），各点随机偏一点，再用平滑曲线连成一圈。

export const FISH_COLORS = [
  "#5CC87A", "#F0A03C", "#EE6C33", "#8B5CF0", "#6E6E73", "#52B8A4",
  "#3F7CF2", "#1FA3F5", "#F2609A", "#F2B92B", "#9BC63B", "#FF8A70",
  "#5A5FD6", "#2FC4B2", "#B98150", "#B18CF5", "#E84B5A", "#4BA3C7",
];

// 字符串 → 32 位整数（cyrb53 的简化版），再喂给 mulberry32 做可复现的随机数。
function hashSeed(str) {
  let h1 = 0xdeadbeef, h2 = 0x41c6ce57;
  for (let i = 0; i < str.length; i++) {
    const ch = str.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return (h1 ^ h2) >>> 0;
}

function rng(seed) {
  let a = hashSeed(String(seed));
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const PI = Math.PI;
const TOP = 1.5 * PI;   // 背（y 向下，角度顺时针：0 = 鼻尖，π/2 = 肚子，π = 尾巴，3π/2 = 背）
const BOTTOM = 0.5 * PI;

// 每个鱼种：给出身体、尾巴、鳍、眼睛位置的参数范围。u(lo, hi) 取随机值，ok(p) 按概率。
const SPECIES = {
  pebble: { name: "圆圆鱼", make: (u, ok) => ({
    a: u(30, 34), b: u(27, 31), taper: u(0, 0.1), tailW: 0.3,
    tail: { type: "fork", L: u(13, 17), S: u(11, 14) },
    fins: ok(0.6) ? [{ at: TOP + u(-0.2, 0.1), h: u(0.1, 0.16), w: 0.3 }] : [],
    eye: { t: -0.55, k: 0.55 } }) },
  classic: { name: "小鲫鱼", make: (u, ok) => ({
    a: u(36, 40), b: u(21, 25), taper: u(0.1, 0.2), tailW: 0.28,
    tail: { type: "fork", L: u(17, 22), S: u(13, 16) },
    fins: [{ at: TOP - u(0, 0.25), h: u(0.22, 0.3), w: 0.26 }, ...(ok(0.5) ? [{ at: BOTTOM + 0.3, h: 0.16, w: 0.2 }] : [])],
    eye: { t: -0.45, k: 0.6 } }) },
  eel: { name: "长条鳗", make: (u) => ({
    a: u(42, 48), b: u(12, 14.5), n: 2.4, taper: u(0.05, 0.15), tailW: 0.55,
    tail: { type: "round", L: u(11, 14), S: u(11, 13) }, fins: [],
    wave: { amp: u(3, 5), ph: u(0, 2 * PI) },
    eye: { t: -0.35, k: 0.45 } }) },
  disc: { name: "神仙鱼", make: (u) => ({
    a: u(22, 26), b: u(26, 30), taper: u(0, 0.12), tailW: 0.3,
    tail: { type: "fan", L: u(11, 14), S: u(11, 14) },
    fins: [{ at: TOP - u(0.05, 0.25), h: u(0.4, 0.55), w: 0.22 }, { at: BOTTOM + u(0.05, 0.25), h: u(0.35, 0.5), w: 0.22 }],
    eye: { t: -0.6, k: 0.5 } }) },
  drop: { name: "水滴鱼", make: (u, ok) => ({
    a: u(32, 36), b: u(24, 28), taper: u(0.38, 0.5), tailW: 0.24,
    tail: { type: "spade", L: u(13, 17), S: u(7, 9) },
    fins: ok(0.4) ? [{ at: TOP + 0.2, h: 0.12, w: 0.3 }] : [],
    eye: { t: -0.55, k: 0.5 } }) },
  goldfish: { name: "金鱼", make: (u) => ({
    a: u(25, 29), b: u(23, 26), taper: u(0.05, 0.15), tailW: 0.34,
    tail: { type: "fan", L: u(22, 27), S: u(21, 26) },
    fins: [{ at: TOP - u(0, 0.2), h: u(0.22, 0.32), w: 0.28 }],
    eye: { t: -0.55, k: 0.55 } }) },
  puffer: { name: "河豚", make: (u) => ({
    a: u(28, 31), b: u(28, 31), taper: 0, tailW: 0.24,
    tail: { type: "fork", L: u(8, 10), S: u(7, 9) },
    ripple: { s: u(0.08, 0.12), k: Math.round(u(11, 15)) }, fins: [],
    eye: { t: -0.7, k: 0.42 } }) },
  sunfish: { name: "翻车鱼", make: (u) => ({
    a: u(30, 33), b: u(26, 29), taper: u(0, 0.1), tailW: 0.8,
    tail: { type: "none", L: u(9, 12), S: u(17, 20) },
    fins: [{ at: TOP - u(0.4, 0.55), h: u(0.5, 0.62), w: 0.2 }, { at: BOTTOM + u(0.4, 0.55), h: u(0.5, 0.62), w: 0.2 }],
    eye: { t: -0.6, k: 0.5 } }) },
  shark: { name: "小鲨", make: (u) => ({
    a: u(42, 46), b: u(16, 19), taper: u(0.12, 0.2), tailW: 0.22,
    tail: { type: "shark", L: u(20, 24), S: u(16, 19) },
    fins: [{ at: TOP + u(-0.05, 0.1), h: u(0.75, 0.9), w: 0.13 }, { at: BOTTOM - 0.25, h: 0.35, w: 0.12 }],
    eye: { t: -0.33, k: 0.6 } }) },
  betta: { name: "斗鱼", make: (u) => ({
    a: u(24, 27), b: u(17, 20), taper: u(0.05, 0.15), tailW: 0.45,
    tail: { type: "ribbon", L: u(27, 32), S: u(14, 17) },
    fins: [{ at: TOP - u(0.25, 0.4), h: u(0.6, 0.8), w: 0.34 }, { at: BOTTOM + u(0.25, 0.4), h: u(0.6, 0.8), w: 0.34 }],
    eye: { t: -0.45, k: 0.55 } }) },
  angler: { name: "灯笼鱼", make: (u) => ({
    a: u(30, 33), b: u(25, 28), n: 2.2, taper: u(0.3, 0.4), tailW: 0.3,
    tail: { type: "round", L: u(11, 13), S: u(9, 11) }, fins: [],
    lure: { reach: u(0.75, 0.95), lift: u(0.9, 1.1) },
    eye: { t: -0.4, k: 0.52 } }) },
  boxfish: { name: "箱鲀", make: (u) => ({
    a: u(29, 33), b: u(22, 25), n: 4, taper: u(0, 0.08), tailW: 0.32,
    tail: { type: "round", L: u(11, 13), S: u(9, 11) }, fins: [],
    eye: { t: -0.5, k: 0.55 } }) },
};

export const FISH_SPECIES = Object.fromEntries(Object.entries(SPECIES).map(([k, v]) => [k, v.name]));

// 尾巴的点（相对尾巴根 xr、中线 0），顺时针：从下往上。
function tailPoints(t, xr) {
  const { L, S } = t;
  const P = (fx, fy) => [xr - L * fx, S * fy];
  switch (t.type) {
    case "fork": return [P(0.95, 1), P(0.5, 0.05), P(0.95, -1)];
    case "fan": return [P(0.65, 1), P(1, 0.5), P(0.82, 0), P(1, -0.5), P(0.65, -1)];
    case "round": return [P(0.55, 0.85), P(1, 0), P(0.55, -0.85)];
    case "spade": return [P(0.45, 0.6), P(1, 0), P(0.45, -0.6)];
    case "none": return [P(0.4, 0.75), P(0.7, 0.3), P(0.45, 0), P(0.7, -0.3), P(0.4, -0.75)];
    case "shark": return [P(0.7, 0.75), P(0.35, 0), P(1, -1.1)];
    case "ribbon": return [P(0.35, 1.25), P(0.95, 1.05), P(1.1, 0.25), P(0.9, -0.45), P(0.5, -1.05)];
    default: return [];
  }
}

function build(spec) {
  const { a, b, n = 2, taper = 0, tailW, fins = [], ripple } = spec;
  const e = 2 / n;
  const sp = (v) => Math.sign(v) * Math.abs(v) ** e;
  const P = (th, withFins = true) => {
    const c = Math.cos(th), s = Math.sin(th);
    let f = 1;
    if (withFins) for (const fin of fins) f += fin.h * Math.exp(-(((th - fin.at) / fin.w) ** 2));
    if (ripple) f += ripple.s * (0.5 + 0.5 * Math.cos(ripple.k * th)) ** 3;
    return [a * sp(c) * f, b * sp(s) * (1 + taper * c) * f];
  };
  const N = 26, pts = [];
  for (let i = 0; i <= N; i++) pts.push(P((PI - tailW) * (i / N)));
  const bot = P(PI - tailW), top = P(PI + tailW);
  pts.push(...tailPoints(spec.tail, Math.min(bot[0], top[0])));
  for (let i = 0; i < N; i++) pts.push(P(PI + tailW + (PI - tailW) * (i / N)));
  const eyeP = P(spec.eye.t, false);
  const eye = [eyeP[0] * spec.eye.k + a * 0.12, eyeP[1] * spec.eye.k];
  let lure = null;
  if (spec.lure) {
    const root = P(2 * PI - 0.95, false);
    lure = { root, tip: [a * spec.lure.reach + 4, -b * spec.lure.lift - 6] };
  }
  return { pts, eye, lure };
}

// 闭合的 Catmull-Rom 平滑曲线 → 三次贝塞尔路径。
function smoothPath(pts, digits = 1) {
  const n = pts.length, f = (v) => v.toFixed(digits);
  let d = `M${f(pts[0][0])} ${f(pts[0][1])}`;
  for (let i = 0; i < n; i++) {
    const p0 = pts[(i - 1 + n) % n], p1 = pts[i], p2 = pts[(i + 1) % n], p3 = pts[(i + 2) % n];
    const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
    const c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
    d += `C${f(c1[0])} ${f(c1[1])} ${f(c2[0])} ${f(c2[1])} ${f(p2[0])} ${f(p2[1])}`;
  }
  return d + "Z";
}

// 眼睛的神态。每个 draw(ev) 画一只眼（以 (0,0) 为中心），wink 返回 [前眼, 后眼]。
const f1 = (v) => v.toFixed(1);
const slash = (len, lean, w = 4.6) => {
  const dx = (Math.sin(lean) * len) / 2, dy = (Math.cos(lean) * len) / 2;
  return `<line x1="${f1(-dx)}" y1="${f1(-dy)}" x2="${f1(dx)}" y2="${f1(dy)}" fill="none" stroke-width="${w}"/>`;
};
const arc = (w, h, sw = 3.6) => `<path d="M${f1(-w)} ${f1(h / 2)}Q0 ${f1(-h * 1.3)} ${f1(w)} ${f1(h / 2)}" fill="none" stroke-width="${sw}"/>`;
const DEG = PI / 180;
const EYES = {
  slash:   { name: "斜眼",   weight: 6, draw: (ev) => slash(ev(7.5, 9.5), -ev(12, 22) * DEG) },
  upright: { name: "直眼",   weight: 2, draw: (ev) => slash(ev(7.5, 9.5), ev(-3, 3) * DEG) },
  lean:    { name: "反斜",   weight: 1, draw: (ev) => slash(ev(7.5, 9), ev(12, 20) * DEG) },
  big:     { name: "大眼",   weight: 1, draw: (ev) => slash(ev(10.5, 12), -ev(8, 16) * DEG, 5.6) },
  tiny:    { name: "小眼",   weight: 1, draw: (ev) => slash(ev(4.5, 5.5), -ev(5, 15) * DEG, 4) },
  dots:    { name: "圆点",   weight: 2, draw: (ev) => `<circle r="${f1(ev(3.1, 3.7))}" stroke="none"/>` },
  beads:   { name: "椭圆",   weight: 1, draw: (ev) => `<ellipse rx="${f1(ev(2.4, 2.8))}" ry="${f1(ev(4, 4.8))}" stroke="none"/>` },
  happy:   { name: "笑眼",   weight: 2, draw: (ev) => arc(ev(3.2, 3.8), ev(3, 3.6)) },
  sleepy:  { name: "困",     weight: 1, draw: (ev) => `<path d="M-3 -0.6Q0 ${f1(ev(2.6, 3.2))} 3 -0.6" fill="none" stroke-width="3.2"/>` },
  flat:    { name: "眯眼",   weight: 1, draw: (ev) => `<line x1="-2.4" y1="0" x2="2.4" y2="0" fill="none" stroke-width="3.6"/>` },
  wink:    { name: "眨眼",   weight: 1, draw: (ev) => [slash(ev(7.5, 9), -ev(12, 20) * DEG), arc(3.2, 3)] },
};
export const FISH_EYES = Object.fromEntries(Object.entries(EYES).map(([k, v]) => [k, v.name]));

function eyeTraits(seed, opts = {}) {
  const E = rng(`${seed}#eyes`);
  const total = Object.values(EYES).reduce((n, e) => n + e.weight, 0);
  let x = E() * total, eyes = "slash";
  for (const [k, e] of Object.entries(EYES)) { if ((x -= e.weight) < 0) { eyes = k; break; } }
  if (opts.eyes && EYES[opts.eyes]) eyes = opts.eyes;
  return { eyes, E };
}

// 种子 → 鱼的特征（种类、颜色等），不画图。opts.species 可以指定鱼种（预览用）。
export function fishTraits(seed, opts = {}) {
  const R = rng(seed);
  const keys = Object.keys(SPECIES);
  const species = opts.species && SPECIES[opts.species] ? opts.species : keys[Math.floor(R() * keys.length)];
  const color = FISH_COLORS[Math.floor(R() * FISH_COLORS.length)];
  const { eyes } = eyeTraits(seed, opts);
  return { species, name: SPECIES[species].name, color, eyes, eyeName: EYES[eyes].name, R };
}

export function fishSvg(seed, opts = {}) {
  const { species, color, R } = fishTraits(seed, opts);
  const u = (lo, hi) => lo + (hi - lo) * R();
  const ok = (p) => R() < p;
  const spec = SPECIES[species].make(u, ok);
  let { pts, eye, lure } = build(spec);

  // 长条鱼身体带一点波浪
  if (spec.wave) {
    const fq = (2 * PI) / (spec.a * 1.7);
    const wv = (p) => [p[0], p[1] + spec.wave.amp * Math.sin(p[0] * fq + spec.wave.ph)];
    pts = pts.map(wv); eye = wv(eye);
  }
  // 整体稍微歪一点头
  const rot = u(-8, 8) * (PI / 180), cr = Math.cos(rot), sr = Math.sin(rot);
  const rt = (p) => [p[0] * cr - p[1] * sr, p[0] * sr + p[1] * cr];
  pts = pts.map(rt); eye = rt(eye);
  if (lure) lure = { root: rt(lure.root), tip: rt(lure.tip) };

  // 缩放进 100×100 的画布，居中
  const all = lure ? [...pts, [lure.tip[0] + 5, lure.tip[1] - 5], [lure.tip[0] - 5, lure.tip[1] + 5]] : pts;
  const xs = all.map((p) => p[0]), ys = all.map((p) => p[1]);
  const minx = Math.min(...xs), maxx = Math.max(...xs), miny = Math.min(...ys), maxy = Math.max(...ys);
  const sc = Math.min(84 / (maxx - minx), 76 / (maxy - miny));
  const tx = 50 - ((minx + maxx) / 2) * sc, ty = 50 - ((miny + maxy) / 2) * sc;
  const T = (p) => [p[0] * sc + tx, p[1] * sc + ty];
  pts = pts.map(T);

  // 眼睛：白色、大小固定（不随鱼身缩放），像同一家族；神态另用一串随机数，换眼神不影响鱼身
  const f = (v) => v.toFixed(1);
  const [ex, ey] = T(eye);
  const { eyes: eyeStyle, E } = eyeTraits(seed, opts);
  const ev = (lo, hi) => lo + (hi - lo) * E();
  const gap = ev(9, 11.5) * (eyeStyle === "dots" || eyeStyle === "beads" ? 0.95 : 1);
  const one = EYES[eyeStyle].draw(ev);
  const drawn = eyeStyle === "wink" ? [one[0], one[1]] : [one, one];
  const deg = (rot * 180) / PI;
  const place = (x, y, g) => `<g transform="translate(${f(x)} ${f(y)}) rotate(${f(deg)})">${g}</g>`;
  const e2 = [ex - gap * cr, ey - gap * sr];
  const eyesSvg = place(ex, ey, drawn[0]) + place(e2[0], e2[1], drawn[1]);

  const size = opts.size || 64;
  let lureSvg = "";
  if (lure) {
    const r0 = T(lure.root), r1 = T(lure.tip);
    const mid = [(r0[0] + r1[0]) / 2 - 2, Math.min(r0[1], r1[1]) - 6];
    lureSvg = `<path d="M${f(r0[0])} ${f(r0[1])}Q${f(mid[0])} ${f(mid[1])} ${f(r1[0])} ${f(r1[1])}" fill="none" stroke="${color}" stroke-width="2.6" stroke-linecap="round"/><circle cx="${f(r1[0])}" cy="${f(r1[1])}" r="4.2" fill="${color}"/>`;
  }
  const label = opts.title ? `<title>${String(opts.title).replace(/[<&>"]/g, "")}</title>` : "";

  // 游动用的几何：每个轮廓点离鱼头多远（0 = 鼻尖，1 = 尾巴尖），往哪边摆（垂直于鱼身）。
  const ax = [cr, sr], nrm = [-sr, cr];
  const along = (p) => p[0] * ax[0] + p[1] * ax[1];
  const ss = pts.map(along), sHead = Math.max(...ss), sTail = Math.min(...ss), span = sHead - sTail || 1;
  const M = rng(`${seed}#swim`);   // 节奏另用一串随机数，不影响鱼身；每条鱼快慢、起步不同
  const geom = {
    pts, ts: ss.map((s) => (sHead - s) / span), n: nrm, eyeT: (sHead - along([ex, ey])) / span,
    style: SWIM[species] || SWIM.classic, cx: 50, cy: 50,
    beat: 0.9 + M() * 0.35, bob: 2.8 + M() * 1.4, surge: 5.5 + M() * 3, lag: M() * 20,
  };
  const id = opts.still ? 0 : registerSwim(`${seed}|${opts.species || ""}|${opts.eyes || ""}`, geom);
  if (id) startSwimLoop();
  return `<svg class="fish${id ? " fish-swim" : ""}"${id ? ` data-fish-id="${id}"` : ""} viewBox="0 0 100 100" width="${size}" height="${size}" xmlns="http://www.w3.org/2000/svg" role="img" aria-hidden="${opts.title ? "false" : "true"}">${label}`
    + `<g class="fish-bob">`
    + (lureSvg ? `<g class="fish-head">${lureSvg}</g>` : "")
    + `<path class="fish-body" d="${smoothPath(pts)}" fill="${color}"/>`
    + `<g class="fish-head" fill="#fff" stroke="#fff" stroke-linecap="round" stroke-linejoin="round">${eyesSvg}</g>`
    + `</g></svg>`;
}

// ---------------------------------------------------------------------------
// 游动：不是整块晃，而是像真鱼一样——一道波从头传到尾，鱼头几乎不动、越往尾巴摆得越大；
// 时快时慢（摆几下、再滑行一会儿），游快时尾巴摆得更大；身体随上浮下沉微微抬头低头。
// 不同鱼种游法不同：长条鳗整条身子扭，箱鲀、河豚、翻车鱼只有尾巴扑腾。
// amp 尾巴摆幅（画布单位）、pow 摆幅从头到尾的增长曲线、waves 身上同时有几成波、hz 每秒摆几次。
const SWIM = {
  pebble:   { amp: 3.6, pow: 2.2, waves: 0.55, hz: 1.5 },
  classic:  { amp: 4.2, pow: 2.0, waves: 0.6,  hz: 1.3 },
  eel:      { amp: 4.2, pow: 1.0, waves: 1.15, hz: 1.0 },
  disc:     { amp: 3.0, pow: 2.6, waves: 0.5,  hz: 1.2 },
  drop:     { amp: 4.2, pow: 2.4, waves: 0.6,  hz: 1.4 },
  goldfish: { amp: 3.8, pow: 2.4, waves: 0.6,  hz: 1.0 },
  puffer:   { amp: 2.6, pow: 4.0, waves: 0.4,  hz: 2.2 },
  sunfish:  { amp: 2.4, pow: 3.5, waves: 0.4,  hz: 0.9 },
  shark:    { amp: 4.0, pow: 1.8, waves: 0.7,  hz: 1.0 },
  betta:    { amp: 4.6, pow: 2.2, waves: 0.8,  hz: 0.8 },
  angler:   { amp: 3.2, pow: 2.6, waves: 0.55, hz: 1.2 },
  boxfish:  { amp: 2.6, pow: 4.0, waves: 0.4,  hz: 2.0 },
};

// 某一时刻（秒）的姿态。纯函数：同一条鱼同一时刻永远一样，方便测试。
export function swimPose(g, time) {
  const t = time + g.lag, st = g.style, TAU = 2 * PI;
  // 时快时慢：速度在 0.45～1.55 倍之间慢慢起伏；相位是速度的积分，所以变速是连续的，不会突然跳。
  const W = TAU / g.surge, c = 0.55;
  const phase = TAU * st.hz * g.beat * (t + (c * Math.sin(W * t)) / W);
  const effort = 0.5 + 0.5 * Math.cos(W * t);          // 1 = 正在用力游，0 = 滑行
  const A = st.amp * (0.45 + 0.55 * effort);
  const env = (u) => 0.05 + 0.95 * u ** st.pow;
  const sway = (u) => env(u) * A * Math.sin(phase - st.waves * TAU * u);
  const pts = g.pts.map((p, i) => {
    const w = sway(g.ts[i]);
    return [p[0] + g.n[0] * w, p[1] + g.n[1] * w];
  });
  const hw = sway(g.eyeT);
  // 上浮下沉 + 抬头低头（上浮时鼻尖微微朝上），用力游时往前送一点
  const b = (TAU * t) / g.bob;
  const y = 1.8 * Math.sin(b), pitch = 2.2 * Math.cos(b), x = 0.9 * (effort - 0.5);
  return {
    d: smoothPath(pts, 2),
    head: `translate(${(g.n[0] * hw).toFixed(2)} ${(g.n[1] * hw).toFixed(2)})`,
    body: `translate(${x.toFixed(2)} ${y.toFixed(2)}) rotate(${pitch.toFixed(2)} ${g.cx} ${g.cy})`,
  };
}

const SWIM_GEOMS = new Map();   // id → 几何
const SWIM_IDS = new Map();     // 种子 → id（同一条鱼只存一份）
function registerSwim(key, geom) {
  let id = SWIM_IDS.get(key);
  if (id == null) { id = SWIM_IDS.size + 1; SWIM_IDS.set(key, id); }
  SWIM_GEOMS.set(id, geom);
  return id;
}
export const swimGeom = (id) => SWIM_GEOMS.get(Number(id));

// 全页一个动画循环：每 0.4 秒（或页面重画后）重新找一遍页面上会游的鱼；连续一阵子找不到就停，下次画鱼再启动。
// 系统开了「减少动态效果」就不动；标签页在后台时浏览器自己会暂停。
let swimOn = false;
function startSwimLoop() {
  if (swimOn || typeof requestAnimationFrame !== "function" || typeof document === "undefined" || typeof document.querySelectorAll !== "function") return;
  if (typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  swimOn = true;
  let items = [], lastScan = -1e9, empty = 0;
  const tick = (now) => {
    // 页面重画后旧的鱼会被换掉：发现有鱼离开页面就立刻重找，不等 0.4 秒
    if (now - lastScan > 400 || items.some((x) => !x.body.isConnected)) {
      lastScan = now;
      items = [...document.querySelectorAll("svg.fish-swim[data-fish-id]")].map((el) => ({
        g: swimGeom(el.dataset.fishId), body: el.querySelector(".fish-body"), bob: el.querySelector(".fish-bob"),
        heads: [...el.querySelectorAll(".fish-head")],
      })).filter((x) => x.g && x.body);
      empty = items.length ? 0 : empty + 1;
      if (empty > 5) { swimOn = false; return; }
    }
    const sec = now / 1000;
    for (const x of items) {
      const p = swimPose(x.g, sec);
      x.body.setAttribute("d", p.d);
      x.bob && x.bob.setAttribute("transform", p.body);
      for (const h of x.heads) h.setAttribute("transform", p.head);
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}
