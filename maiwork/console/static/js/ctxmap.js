// MaiWork 网页 · 上下文窗口面积地图（Squarified Treemap + 400 格视图）。
// 兼容纯函数单测（normalizeCtxMap / tilesOf / cellsOf / ctxMapHTML 不依赖真实 DOM）与点击查看原文。
import { api } from "./api.js";
import { esc, tokens } from "./util.js";

export const CELLS = 400;
const TILE_MIN = 1350;
const TILE_MAX = 10;
const TILE_FLOOR = 640;
const TILE_SIDE = 15;

export const CTX_GROUPS = {
  system: ["身份与规矩", "#7c6cf0"],
  rules: ["本群规则", "#e06c9f"],
  skills: ["做法与技能", "#14b8a6"],
  memory: ["群画像与记忆", "#f59e0b"],
  notes: ["专岗笔记与便条", "#8b5cf6"],
  tools: ["工具规格", "#3898ec"],
  summary: ["前情摘要", "#22c55e"],
  history: ["对话与指令", "#5b6ef5"],
  tool_io: ["工具调用与返回", "#f0703a"],
  free: ["剩余空间", ""],
};

const GROUP_ORDER = ["system", "rules", "skills", "memory", "notes", "tools", "summary", "history", "tool_io"];

// 模块内轻量交互态（不污染业务 state）：视图模式、选中的分块、按需拉取的 model_call 原文缓存
const viewModes = new Map();
const selectedTile = new Map();
const callCache = new Map();
const rawRegistry = new Map();

export function normalizeCtxMap(raw) {
  if (!raw || typeof raw !== "object") return null;
  const limit = Math.max(1, Number(raw.limit_tokens || raw.limit) || 128000);
  const rawParts = Array.isArray(raw.parts) ? raw.parts : [];
  const byGroup = new Map();
  for (const k of GROUP_ORDER) byGroup.set(k, []);
  rawParts.forEach((p, idx) => {
    if (!p || typeof p !== "object") return;
    const g = CTX_GROUPS[p.group] ? p.group : "history";
    const tok = Math.max(0, Math.round(Number(p.tokens) || 0));
    if (tok <= 0) return;
    byGroup.get(g).push({
      id: `${g}::${idx}`,
      msgIdx: typeof p.idx === "number" ? p.idx : -1,
      group: g,
      role: String(p.role || ""),
      label: String(p.label || CTX_GROUPS[g][0]),
      tokens: tok,
      chars: Math.max(0, Number(p.chars) || 0),
      spanCount: Math.max(1, Number(p.span_count) || 1),
      preview: String(p.preview || ""),
    });
  });
  const groups = [];
  let sumTok = 0;
  for (const [g, items] of byGroup.entries()) {
    if (!items.length) continue;
    const tok = items.reduce((s, x) => s + x.tokens, 0);
    sumTok += tok;
    groups.push({
      kind: g,
      label: (CTX_GROUPS[g] && CTX_GROUPS[g][0]) || g,
      color: (CTX_GROUPS[g] && CTX_GROUPS[g][1]) || "var(--blue)",
      tokens: tok,
      items,
    });
  }
  // 兼容只给 groups 汇总、没给细分 parts 的简化数据
  if (!groups.length && Array.isArray(raw.groups)) {
    for (const gp of raw.groups) {
      const g = gp && CTX_GROUPS[gp.key] ? gp.key : "history";
      const tok = Math.max(0, Math.round(Number(gp && gp.tokens) || 0));
      if (tok <= 0) continue;
      sumTok += tok;
      groups.push({
        kind: g,
        label: String((gp && gp.label) || CTX_GROUPS[g][0]),
        color: CTX_GROUPS[g][1] || "var(--blue)",
        tokens: tok,
        items: [{ id: `${g}::0`, msgIdx: -1, group: g, role: g, label: String((gp && gp.label) || CTX_GROUPS[g][0]), tokens: tok, chars: 0, spanCount: 1, preview: "" }],
      });
    }
  }
  const used = Math.max(sumTok, Math.round(Number(raw.used_tokens ?? raw.used) || 0));
  if (!groups.length && used <= 0) return null;
  return {
    used,
    limit: Math.max(limit, used, 1),
    calibrated: Boolean(raw.calibrated),
    groups,
  };
}

// Bruls, Huizing, van Wijk squarified treemap
export function squarify(vals, x, y, w, h) {
  const out = [];
  const clean = (vals || []).filter((a) => a && a.v > 0);
  const total = clean.reduce((s, a) => s + a.v, 0);
  if (!total || w <= 0 || h <= 0) return out;
  const k = (w * h) / total;
  const worst = (row, side) => {
    let sum = 0, hi = 0, lo = Infinity;
    for (const a of row) {
      const s = a.v * k;
      sum += s;
      if (s > hi) hi = s;
      if (s < lo) lo = s;
    }
    if (!sum || !lo || !side) return Infinity;
    return Math.max((side * side * hi) / (sum * sum), (sum * sum) / (side * side * lo));
  };
  const lay = (row) => {
    const sum = row.reduce((s, a) => s + a.v * k, 0);
    if (w >= h) {
      const cw = h > 0 ? sum / h : 0;
      let cy = y;
      for (const a of row) {
        const ch = cw > 0 ? (a.v * k) / cw : 0;
        out.push({ a, x, y: cy, w: cw, h: ch });
        cy += ch;
      }
      x += cw;
      w -= cw;
    } else {
      const rh = w > 0 ? sum / w : 0;
      let cx = x;
      for (const a of row) {
        const cw = rh > 0 ? (a.v * k) / rh : 0;
        out.push({ a, x: cx, y, w: cw, h: rh });
        cx += cw;
      }
      y += rh;
      h -= rh;
    }
  };
  let row = [];
  for (const a of clean) {
    const side = Math.min(w, h);
    if (!row.length || worst([...row, a], side) <= worst(row, side)) {
      row.push(a);
      continue;
    }
    lay(row);
    row = [a];
  }
  if (row.length) lay(row);
  return out;
}

export function tilesOf(ctx, W = 520, H = 180) {
  const c = ctx && Array.isArray(ctx.groups) ? ctx : normalizeCtxMap(ctx);
  if (!c || W <= 0 || H <= 0) return [];
  const used = c.groups.filter((g) => g.tokens > 0);
  const usedTok = used.reduce((s, g) => s + g.tokens, 0);
  const scale = Math.max(c.limit, usedTok, 1);
  const across = W >= H;
  const longSide = across ? W : H;
  let usedLen = longSide;
  if (usedTok < scale && usedTok > 0) {
    usedLen = Math.min(longSide, Math.max(longSide * Math.pow(usedTok / scale, 0.38), Math.min(longSide, 44)));
  } else if (usedTok <= 0) {
    usedLen = 0;
  }
  const ur = across ? [0, 0, usedLen, H] : [0, 0, W, usedLen];
  const floored = (vals, area) => {
    const sum = vals.reduce((s, a) => s + a.v, 0);
    const lo = (sum * TILE_FLOOR) / Math.max(1, area);
    return vals.map((a) => ({ ...a, v: Math.max(a.v, lo) }));
  };
  const partVals = floored(used.map((g) => ({ v: g.tokens, g })), ur[2] * ur[3]);
  let rects = squarify(partVals, ur[0], ur[1], ur[2], ur[3]);
  for (let n = 0; n < 6; n++) {
    const thin = rects.filter((r) => Math.min(r.w, r.h) < TILE_SIDE);
    if (!thin.length) break;
    for (const r of thin) r.a.v *= (TILE_SIDE / Math.max(1, Math.min(r.w, r.h))) * 1.05;
    rects = squarify(partVals, ur[0], ur[1], ur[2], ur[3]);
  }
  const tiles = [];
  for (const r of rects) {
    const g = r.a.g;
    const items = [...(g.items || [])].filter((it) => it.tokens > 0).sort((a, b) => b.tokens - a.tokens);
    const isum = items.reduce((s, it) => s + it.tokens, 0);
    const area = r.w * r.h;
    const whole = () =>
      tiles.push({
        key: g.kind,
        kind: g.kind,
        label: g.label,
        color: g.color,
        it: items.length === 1 ? items[0] : null,
        its: items,
        tokens: g.tokens,
        x: r.x,
        y: r.y,
        w: r.w,
        h: r.h,
      });
    if (items.length < 2 || area < TILE_MIN * 2) {
      whole();
      continue;
    }
    const kept = [];
    const rest = [];
    for (const it of items) {
      if (kept.length < TILE_MAX && (it.tokens / Math.max(1, isum)) * area >= TILE_MIN) kept.push(it);
      else rest.push(it);
    }
    const vals = kept.map((it) => ({ v: it.tokens, it }));
    if (rest.length === 1 && !kept.length) vals.push({ v: rest[0].tokens, it: rest[0] });
    else if (rest.length) vals.push({ v: rest.reduce((s, it) => s + it.tokens, 0), rest });
    const laid = squarify(floored(vals, area), r.x, r.y, r.w, r.h);
    if (laid.some((q) => Math.min(q.w, q.h) < 13)) {
      whole();
      continue;
    }
    for (const q of laid) {
      const it = q.a.it || null;
      const subItems = it ? [it] : q.a.rest || [];
      const tok = it ? it.tokens : subItems.reduce((s, y) => s + y.tokens, 0);
      tiles.push({
        key: `${g.kind}::${it ? it.id : "~rest"}`,
        kind: g.kind,
        label: it ? it.label : `其余 ${subItems.length} 段`,
        color: g.color,
        it,
        its: subItems,
        tokens: isum ? Math.round((tok / isum) * g.tokens) : tok,
        x: q.x,
        y: q.y,
        w: q.w,
        h: q.h,
      });
    }
  }
  if (usedTok < scale) {
    const fr = across ? [usedLen, 0, W - usedLen, H] : [0, usedLen, W, H - usedLen];
    tiles.push({
      key: "~free",
      free: true,
      kind: "free",
      label: "剩余空间",
      color: "var(--line)",
      it: null,
      its: [],
      tokens: scale - usedTok,
      x: fr[0],
      y: fr[1],
      w: fr[2],
      h: fr[3],
    });
  }
  return tiles;
}

export function cellsOf(ctx) {
  const c = ctx && Array.isArray(ctx.groups) ? ctx : normalizeCtxMap(ctx);
  if (!c) return Array(CELLS).fill(null);
  const used = c.groups.filter((g) => g.tokens > 0);
  const usedTok = used.reduce((s, g) => s + g.tokens, 0);
  if (!used.length || !usedTok) return Array(CELLS).fill(null);
  const n = Math.min(CELLS, Math.max(used.length, Math.round((usedTok / Math.max(c.limit, usedTok, 1)) * CELLS)));
  const want = used.map((g) => (g.tokens / usedTok) * n);
  const got = want.map((w) => Math.max(1, Math.floor(w)));
  let left = n - got.reduce((s, x) => s + x, 0);
  const order = want.map((w, i) => [w - Math.floor(w), i]).sort((a, b) => b[0] - a[0]);
  for (let j = 0; left > 0 && order.length; j = (j + 1) % order.length, left--) got[order[j][1]]++;
  for (let j = got.length - 1; left < 0 && j >= 0; j--) {
    while (left < 0 && got[j] > 1) {
      got[j]--;
      left++;
    }
  }
  const cells = [];
  used.forEach((g, i) => {
    for (let cIdx = 0; cIdx < got[i]; cIdx++) cells.push(g);
  });
  while (cells.length < CELLS) cells.push(null);
  return cells.slice(0, CELLS);
}

function inspectorHTML(c, mapId, callId, actPrefix) {
  const selKey = selectedTile.get(mapId);
  if (!selKey) return "";
  const tiles = tilesOf(c, 520, 180);
  const hit = tiles.find((t) => t.key === selKey) || tiles.find((t) => t.kind === selKey);
  if (!hit || hit.free) return "";
  const groupTitle = (CTX_GROUPS[hit.kind] && CTX_GROUPS[hit.kind][0]) || hit.kind;
  const cached = callId ? callCache.get(Number(callId)) : null;
  const items = hit.its && hit.its.length ? hit.its : [];
  const rows = items.slice(0, 8).map((it) => {
    let fullText = it.preview || "";
    if (cached && cached !== "loading" && cached.request && Array.isArray(cached.request.messages) && it.msgIdx >= 0) {
      const m = cached.request.messages[it.msgIdx];
      if (m) {
        const rawC = typeof m.content === "string" ? m.content : JSON.stringify(m.content || m, null, 2);
        if (rawC) fullText = rawC.slice(0, 2400);
      }
    } else if (cached && cached !== "loading" && hit.kind === "tools" && cached.request && cached.request.tools) {
      fullText = JSON.stringify(cached.request.tools, null, 2).slice(0, 2400);
    }
    return `<div class="cm-ins-item">
      <div class="cm-ins-meta"><b>${esc(it.label)}</b><span>${tokens(it.tokens)} token${it.spanCount > 1 ? ` · 合并 ${it.spanCount} 段` : ""}</span></div>
      ${fullText ? `<pre class="cm-ins-pre">${esc(fullText)}</pre>` : `<p class="fine">暂无文本预览</p>`}
    </div>`;
  }).join("");
  const loadingNote = cached === "loading" ? `<div class="fine">正在加载完整脱敏原文…</div>` : "";
  return `<div class="cm-ins">
    <div class="cm-ins-head">
      <span><i style="background:${hit.color}"></i><b>${esc(groupTitle)}</b> · ${tokens(hit.tokens)} token</span>
      <button type="button" class="link-btn" data-act="${esc(actPrefix)}-part" data-map="${esc(mapId)}" data-key="">收起</button>
    </div>
    ${loadingNote}
    ${rows}
  </div>`;
}

// opts: { id, callId, compact, actPrefix ("board-ctx" | "ctxmap") }
export function ctxMapHTML(raw, opts = {}) {
  const c = normalizeCtxMap(raw);
  if (!c) return "";
  const mapId = String(opts.id || "default");
  const callId = opts.callId ? Number(opts.callId) || 0 : 0;
  const compact = !!opts.compact;
  const actPrefix = opts.actPrefix || "ctxmap";
  rawRegistry.set(mapId, { raw, opts });

  const mode = viewModes.get(mapId) || "map";
  const W = compact ? 360 : 540;
  const H = compact ? 132 : 176;
  const pct = Math.min(100, Math.round((c.used / Math.max(1, c.limit)) * 100));
  const selKey = selectedTile.get(mapId) || "";
  const pctPos = (a, b) => +((a / Math.max(1, b)) * 100).toFixed(2);

  const tiles = tilesOf(c, W, H)
    .map((tl) => {
      const tiny = tl.w < 54 || tl.h < 28 ? " bare" : tl.h < 42 ? " low" : "";
      const isOn = selKey && (selKey === tl.key || selKey === tl.kind) ? " on" : "";
      const sub = tl.free ? `${pctPos(tl.tokens, c.limit).toFixed(0)}%` : tokens(tl.tokens);
      const title = `${tl.label} · ${tokens(tl.tokens)} token`;
      if (tl.free) {
        return `<span class="cm-tile free${tiny}" title="${esc(title)}" style="left:calc(${pctPos(tl.x, W)}% + 1.5px);top:calc(${pctPos(tl.y, H)}% + 1.5px);width:max(0px,calc(${pctPos(tl.w, W)}% - 3px));height:max(0px,calc(${pctPos(tl.h, H)}% - 3px))"><b class="n">${esc(tl.label)}</b><span class="v">${esc(sub)}</span></span>`;
      }
      return `<button type="button" class="cm-tile k-${esc(tl.kind)}${tiny}${isOn}" data-act="${esc(actPrefix)}-part" data-map="${esc(mapId)}" data-key="${esc(tl.key)}"${callId ? ` data-call="${callId}"` : ""} title="${esc(title)}" style="--k:${tl.color};left:calc(${pctPos(tl.x, W)}% + 1.5px);top:calc(${pctPos(tl.y, H)}% + 1.5px);width:max(0px,calc(${pctPos(tl.w, W)}% - 3px));height:max(0px,calc(${pctPos(tl.h, H)}% - 3px))"><b class="n">${esc(tl.label)}</b><span class="v">${esc(sub)}</span></button>`;
    })
    .join("");

  const cells = cellsOf(c)
    .map((g) => {
      if (!g) return `<i class="free" title="剩余空间"></i>`;
      return `<button type="button" class="cm-cell k-${esc(g.kind)}" data-act="${esc(actPrefix)}-part" data-map="${esc(mapId)}" data-key="${esc(g.kind)}"${callId ? ` data-call="${callId}"` : ""} style="--k:${g.color}" title="${esc(g.label)} · ${tokens(g.tokens)} token"></button>`;
    })
    .join("");

  const legend = c.groups
    .map(
      (g) =>
        `<button type="button" class="cm-leg${selKey === g.kind ? " on" : ""}" data-act="${esc(actPrefix)}-part" data-map="${esc(mapId)}" data-key="${esc(g.kind)}"${callId ? ` data-call="${callId}"` : ""}><i style="background:${g.color}"></i><span>${esc(g.label)}</span><b>${tokens(g.tokens)}</b></button>`
    )
    .join("");

  return `<div class="ctxmap${compact ? " compact" : ""}" data-ctxmap-box="${esc(mapId)}">
    <div class="cm-bar">
      <span class="cm-sum"><b>${tokens(c.used)}</b> / ${tokens(c.limit)} token（${pct}%）<span class="tag">${c.calibrated ? "实测校准" : "按字数估算"}</span></span>
      <span class="seg cm-seg" role="group" aria-label="视图切换">
        <button type="button" data-act="${esc(actPrefix)}-view" data-map="${esc(mapId)}" data-v="map" aria-pressed="${mode === "map"}">方块</button>
        <button type="button" data-act="${esc(actPrefix)}-view" data-map="${esc(mapId)}" data-v="grid" aria-pressed="${mode === "grid"}">方格</button>
      </span>
    </div>
    <div class="cm-viz" data-look="${mode}">
      <div class="cm-map" role="group" aria-label="上下文面积图">${tiles}</div>
      <div class="cm-grid" role="group" aria-label="上下文 400 格图">${cells}</div>
    </div>
    <div class="cm-legs">${legend}</div>
    ${inspectorHTML(c, mapId, callId, actPrefix)}
  </div>`;
}

function repaintBox(mapId) {
  if (typeof document === "undefined") return;
  const rec = rawRegistry.get(mapId);
  if (!rec) return;
  const escId = typeof CSS !== "undefined" && CSS.escape ? CSS.escape(mapId) : mapId;
  for (const el of document.querySelectorAll(`[data-ctxmap-box="${escId}"]`)) {
    el.outerHTML = ctxMapHTML(rec.raw, rec.opts);
  }
}

export async function actCtxMap(action, el) {
  if (!action || !el) return false;
  const isView = action === "ctxmap-view" || action === "board-ctx-view";
  const isPart = action === "ctxmap-part" || action === "board-ctx-part";
  if (!isView && !isPart) return false;
  const mapId = el.dataset.map || "default";
  if (isView) {
    viewModes.set(mapId, el.dataset.v === "grid" ? "grid" : "map");
    repaintBox(mapId);
    return true;
  }
  const key = el.dataset.key || "";
  const cur = selectedTile.get(mapId) || "";
  if (!key || cur === key) {
    selectedTile.delete(mapId);
    repaintBox(mapId);
    return true;
  }
  selectedTile.set(mapId, key);
  const callId = Number(el.dataset.call) || 0;
  if (callId > 0 && !callCache.has(callId)) {
    callCache.set(callId, "loading");
    repaintBox(mapId);
    try {
      const detail = await api("GET", `/api/logs/model-calls/${encodeURIComponent(callId)}`);
      callCache.set(callId, detail || {});
    } catch (_) {
      callCache.delete(callId);
    }
  }
  repaintBox(mapId);
  return true;
}
