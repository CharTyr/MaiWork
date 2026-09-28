// MaiWork 网页 · 群脉搏卡片：最近 24 小时的发言量曲线。
import { BJ, dayIndex, esc, ico, pad } from "./util.js";
import { quiet } from "./api.js";

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

export function pulseCard(g, v, compact) {
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
