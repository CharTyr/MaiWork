// MaiWork 网页 · 开话题记录的整理（纯函数，不碰 DOM，方便用 node 测）。
//
// 一段冷场里可能判了好几次（有新候选、或隔了 30 分钟复判）：按「这段冷场从哪开始」合并成一张卡。
// 新记录有 jev.stretch_ts；旧记录（2026-10-01 前，每 30 秒判一次）用 ts - quiet_s 推出来。
// 分数只用真存了的（jev.ok_p / fit_best），旧记录没存就照实说，不编数字。

export const OK_NEED = 0.6;
export const FIT_NEED = 0.5;
const SAME_S = 90; // 两次判断的段起点相差这么多以内 = 同一段

const num = (v) => (typeof v === "number" && isFinite(v) ? v : null);
const f2 = (v) => Number(v).toFixed(2);

export function stretchStart(t) {
  const j = t && t.jev;
  const s = j ? num(j.stretch_ts) : null;
  return s != null ? s : Number(t.ts || 0) - Number(t.quiet_s || 0);
}

// log：后端给的 topic_log（新的在前）。返回每段 {rows, rep, first, n, opened, bestOk, bestFit}，新段在前。
// rep 是这段的代表：开了话题的那条；没开就是这段最新一次判断（「对 / 不对」标在它上面）。
export function stretches(log) {
  const rows = (log || []).slice().sort((a, b) => Number(b.ts || 0) - Number(a.ts || 0));
  const out = [];
  for (const t of rows) {
    const key = stretchStart(t);
    const cur = out.find((s) => Math.abs(s.key - key) <= SAME_S);
    if (cur) cur.rows.push(t);
    else out.push({ key, rows: [t] });
  }
  return out.map((s) => {
    const opened = s.rows.find((t) => !!t.opener);
    const oks = s.rows.map((t) => num(t.jev && t.jev.ok_p)).filter((v) => v != null);
    const fits = s.rows.map((t) => num(t.jev && t.jev.fit_best)).filter((v) => v != null);
    return {
      rows: s.rows,
      rep: opened || s.rows[0],
      first: s.rows[s.rows.length - 1],
      n: s.rows.length,
      opened: !!opened,
      bestOk: oks.length ? Math.max(...oks) : null,
      bestFit: fits.length ? Math.max(...fits) : null,
    };
  });
}

// 一次判断的 Jev 那句话：真实分数 + 门槛 + 原因 + 卡在哪。
export function judgeText(t) {
  const j = t && t.jev;
  if (!j) return "没问 Jev";
  const okP = num(j.ok_p);
  const reason = j.reason && j.reason !== "可以开" ? String(j.reason) : "";
  if (okP == null) {
    // 旧记录只存了 ok 结论和「原因」的把握：不再把那个把握当分数，也不写「不适合 · 可以开」
    if (j.ok) return "适合开（旧记录）";
    return `不适合${reason ? ` · ${reason}` : "（分不够）"}（旧记录）`;
  }
  const parts = [`适合度 ${f2(okP)}（要 ${f2(num(j.ok_need) ?? OK_NEED)}）`];
  if (reason) parts.push(`原因：${reason}`);
  const fit = num(j.fit_best);
  if (fit != null) parts.push(`最佳候选 ${f2(fit)}（要 ${f2(num(j.fit_need) ?? FIT_NEED)}）${j.fit_title ? `「${j.fit_title}」` : ""}`);
  if (j.stuck === "timing") parts.push("时机不对");
  else if (j.stuck === "candidate") parts.push("没合适的话题");
  return parts.join(" · ");
}

// 今天的小结（isToday(ts) 由调用方按北京时间判断）：几段冷场、开了几次、今天的最高分和门槛。
export function daySummary(log, isToday) {
  const today = (log || []).filter((t) => isToday(Number(t.ts || 0)));
  if (!today.length) return "";
  const st = stretches(today);
  const opened = st.filter((s) => s.opened).length;
  const oks = st.map((s) => s.bestOk).filter((v) => v != null);
  const fits = st.map((s) => s.bestFit).filter((v) => v != null);
  let s = `今天冷场 ${st.length} 次，开了 ${opened} 次`;
  if (oks.length) s += ` · 最高 ${f2(Math.max(...oks))}（要 ${f2(OK_NEED)}）`;
  if (fits.length) s += ` · 候选最高 ${f2(Math.max(...fits))}（要 ${f2(FIT_NEED)}）`;
  return s;
}
