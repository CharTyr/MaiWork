// MaiWork 网页 · 「资讯」页。
import { admin, gadmin, state } from "../state.js";
import { SVG, dayWord, dur, esc, hhmm, ico, now, richText, safeUrl, slotName, tokens, when } from "../util.js";
import { agentFish, api, gview } from "../api.js";

/* ───────────── 各页面 ───────────── */

export function emptyState(iconName, title, text) {
  return `<div class="empty enter">${ico(iconName)}<b>${esc(title)}</b>${text ? `<span>${esc(text)}</span>` : ""}</div>`;
}
export const loading = () => `<div class="loading"><span></span><span></span><span></span></div>`;

export const FB_KEY = "mw-fb";
export const myFb = () => {
  try {
    return JSON.parse(localStorage.getItem(FB_KEY) || "{}");
  } catch (e) {
    return {};
  }
};
export function fbButtons(kind, it) {
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
  if (k === "used") return `${when(s.at)} 拿去开话题了${s.replies ? ` · ${s.replies} 人接` : " · 没人接"}`;
  if (k === "pool") return s.expires_ts ? `留着开话题 · 还剩 ${dur(s.expires_ts - now())}` : "留着开话题";
  if (k === "expired") return "没等到时机，过期了";
  return "新到";
}

const SCORE_NAMES = [["info", "干货"], ["source", "来源"], ["relevance", "相关"], ["timeliness", "新鲜"], ["chat", "好聊"]];

function verifyBlock(v) {
  if (!v || !v.status) return "";
  const ok = v.status === "passed";
  const steps = (v.steps || []).slice(0, 4);
  return `
    <div class="verify ${ok ? "ok" : "bad"}">
      <div class="verify-head">${ico("testtube", "")}${ok ? "实测能跑" : "实测没跑通"}${v.minutes ? `<span> · 花了 ${v.minutes} 分钟</span>` : ""}</div>
      ${v.summary ? `<div class="verify-text">${esc(v.summary)}</div>` : ""}
      ${steps.length ? `<ol class="verify-steps">${steps.map((x) => `<li>${esc(x)}</li>`).join("")}</ol>` : ""}
    </div>`;
}

function reasonBlock(it) {
  const reason = it.reason || it.why;
  const refs = (it.refs || []).slice(0, 3);
  const aud = it.audience || [];
  if (!reason && !refs.length && !aud.length) return "";
  // 默认收起，点标题展开；展开状态记在内存里，轮询重画后不会自己合上
  const open = !!reasonOpen[it.id];
  return `
    <details class="reason" data-rid="${esc(it.id)}"${open ? " open" : ""}>
      <summary class="reason-h">为什么推这条</summary>
      ${reason ? `<p class="reason-t">${esc(reason)}</p>` : ""}
      ${
        refs.length
          ? `<div class="refs">${refs
              .map((r) => `<div class="ref-row"><span class="ref-when">${esc(when(r.ts))}</span><span class="ref-who">${esc(r.who)}</span><span class="ref-text">「${esc(r.text)}」</span></div>`)
              .join("")}</div>`
          : ""
      }
      ${aud.length ? `<div class="aud">可能用得上：${aud.map((n) => `<b>${esc(n)}</b>`).join("、")}</div>` : ""}
      ${it.profile_ref ? `<div class="aud">因为群里关心「${esc(it.profile_ref)}」</div>` : ""}
    </details>`;
}
const reasonOpen = {};
document.addEventListener(
  "toggle",
  (e) => {
    const d = e.target;
    if (d && d.matches && d.matches("details.reason")) reasonOpen[d.dataset.rid] = d.open;
  },
  true
);

// 资讯评价（2026-09-29 取代「想在群里聊」气泡）：挑理由 + 可选一句话，下一轮找资讯照着改
export const RATE_KEY = "mw-rate";
export const RATE_REASONS = [
  ["old", "太旧"],
  ["useless", "没用"],
  ["low", "质量差"],
  ["offtopic", "不相关"],
  ["dup", "发过了"],
  ["wrong", "不准确"],
];
export const myRates = () => {
  try {
    return JSON.parse(localStorage.getItem(RATE_KEY) || "{}");
  } catch (e) {
    return {};
  }
};
// 这个浏览器的随机标识：服务器靠它让「同一浏览器对同一条只算一份，再评就是改」
export function clientId() {
  let id = "";
  try {
    id = localStorage.getItem("mw-client") || "";
  } catch (e) {}
  if (!/^[A-Za-z0-9_-]{8,64}$/.test(id)) {
    const b = new Uint8Array(12);
    crypto.getRandomValues(b);
    id = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
    try {
      localStorage.setItem("mw-client", id);
    } catch (e) {}
  }
  return id;
}
const reasonName = (k) => (RATE_REASONS.find((r) => r[0] === k) || [, k])[1];

// 管理员看得到每条收到的评价（群友只看得到自己评了什么）
function ratingsBlock(it) {
  const r = it.ratings;
  if (!admin() || !r || !r.total) return "";
  const counts = Object.entries(r.counts || {})
    .sort((a, b) => b[1] - a[1])
    .map(([k, n]) => `<span class="ntag warn">${esc(reasonName(k))} ×${n}</span>`)
    .join("");
  const notes = (r.notes || []).slice(0, 3).map((n) => `<div class="rating-note">「${esc(n)}」</div>`).join("");
  return `<div class="ratings"><div class="ratings-h">${r.total} 人评价过</div>${counts ? `<div class="ntags">${counts}</div>` : ""}${notes}</div>`;
}

function newsItem(it, i, guide) {
  const sc = it.scores || null;
  const rated = !!myRates()[it.id];
  const tags = [
    it.topic ? `<span class="ntag">${esc(it.topic)}</span>` : "",
    it.followup ? `<span class="ntag fu">后续</span>` : "",
    it.angle === "diverse" ? `<span class="ntag alt">新角度</span>` : "",
    it.angle === "explore" ? `<span class="ntag ex">延伸</span>` : "",
    it.sensitive ? `<span class="ntag warn">有争议</span>` : "",
    it.verify && it.verify.status === "passed" ? `<span class="ntag ok">实测过</span>` : "",
    admin() && sc && Number(sc.avg) > 0 ? `<span class="ntag score" title="${SCORE_NAMES.map(([k, n]) => `${n} ${sc[k] ?? "-"}`).join(" · ")}">${Number(sc.avg).toFixed(1)} 分</span>` : "",
  ].join("");
  const img = it.image_url && /^https?:\/\//i.test(it.image_url)
    ? `<img class="n-img" src="${esc(it.image_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()" />`
    : it.viz
      ? `<iframe class="n-viz" data-viz="${esc(it.id)}" sandbox="allow-scripts" referrerpolicy="no-referrer" title="图解：${esc(it.title)}"></iframe>`
      : "";
  return `
    <article class="item news-item enter" style="--i:${i}">
      ${ico(it.icon || (guide ? "books" : "newspaper"))}
      <div>
        ${tags ? `<div class="ntags">${tags}</div>` : ""}
        <h2 class="item-title">${esc(it.title)}</h2>
        ${it.followup ? `<p class="n-bridge n-fu"><span aria-hidden="true">↻</span>${it.followup.of_title ? `接上条「${esc(it.followup.of_title)}」：` : ""}${esc(it.followup.new_fact || "")}</p>` : ""}
        ${it.bridge && it.angle === "explore" ? `<p class="n-bridge"><span aria-hidden="true">↳</span>${esc(it.bridge)}</p>` : ""}
        <p class="item-body">${richText(it.body || it.summary)}</p>
        ${img}
        ${verifyBlock(it.verify)}
        ${reasonBlock(it)}
        ${ratingsBlock(it)}
        ${
          (it.sources || []).length
            ? `<div class="sources">${it.sources
                .map((s, k) => `<a class="src" href="${k === 0 && it.id ? `/go/${encodeURIComponent(it.id)}?c=${encodeURIComponent(clientId())}${state.ref && !gadmin() ? `&g=${encodeURIComponent(state.ref)}` : ""}` : safeUrl(s.url)}" target="_blank" rel="noopener noreferrer"><div class="src-site">${esc(s.site || "")}</div><div class="src-title">${esc(s.title || s.url)}</div></a>`)
                .join("")}</div>`
            : ""
        }
        <div class="status">
          <span class="dot ${esc((it.status || {}).kind || "")}"></span>
          <span class="status-text">${esc(guide ? guideStatus(it) : newsStatus(it.status))}</span>
          <button class="ratebtn" data-act="rate-open" data-id="${it.id}" aria-pressed="${rated}" title="哪里不好？">${SVG.pen}<i>${rated ? "已评价" : "评价"}</i></button>
          ${fbButtons("news", it)}
        </div>
      </div>
    </article>`;
}

/* ───────────── 图解（news_viz）：沙箱 iframe，按需取、缓存，报高度 ─────────────
   iframe 只给 allow-scripts（没有 allow-same-origin）：图解页碰不到本页登录信息、不能联网（CSP）、不能跳转本页。
   页面轮询会整块重画，所以内容和高度都缓存在内存里，重画时不闪、不重新请求。 */
const vizDoc = {};
const vizH = {};
const vizLoading = {};
function fillViz(frame) {
  const id = frame.dataset.viz;
  if (!id || frame.dataset.ready) return;
  frame.dataset.ready = "1";
  if (vizH[id]) frame.style.height = vizH[id] + "px"; // 高度不写进 HTML：轮询比对时内容才不会因高度变而重画
  if (vizDoc[id]) {
    frame.srcdoc = vizDoc[id];
    return;
  }
  if (vizLoading[id]) {
    vizLoading[id].then(() => vizDoc[id] && document.contains(frame) && (frame.srcdoc = vizDoc[id]));
    return;
  }
  vizLoading[id] = api("GET", `/api/news/${encodeURIComponent(id)}/viz`)
    .then((r) => {
      vizDoc[id] = (r && r.html) || "";
      if (vizDoc[id] && document.contains(frame)) frame.srcdoc = vizDoc[id];
      else if (!vizDoc[id]) frame.remove();
    })
    .catch(() => frame.remove())
    .finally(() => delete vizLoading[id]);
}
new MutationObserver(() => document.querySelectorAll("iframe.n-viz:not([data-ready])").forEach(fillViz)).observe(document.documentElement, { childList: true, subtree: true });
window.addEventListener("message", (e) => {
  const d = e.data;
  if (!d || d.mwviz !== 1 || typeof d.h !== "number") return;
  for (const f of document.querySelectorAll("iframe.n-viz")) {
    if (f.contentWindow === e.source) {
      const h = Math.max(80, Math.min(1400, Math.ceil(d.h)));
      vizH[f.dataset.viz] = h;
      f.style.height = h + "px";
    }
  }
});

function guideStatus(it) {
  const when_ = it.published_ts ? `${dayWord(it.published_ts)}发布 · ` : "";
  return `${when_}${it.created_ts ? dayWord(it.created_ts) + "找到" : "已复查"}`;
}

// 每轮找资讯的账：有统计就说搜了几次、看了几篇、收了几条；老数据没统计按旧说法
function batchStats(batch) {
  const st = batch.stats;
  const f = st && st.funnel;
  if (f && typeof f === "object") return `搜 ${f.queries || st.searches || 0} 次 · 看 ${f.opened || st.pages || 0} 篇 · 留 ${st.kept || 0} 条`;
  if (st && typeof st === "object") return `搜 ${st.searches || 0} 次 · 看 ${st.pages || 0} 篇 · 留 ${st.kept || 0} 条`;
  return `找 ${batch.found || 0} 条 · 留 ${batch.kept || 0} 条`;
}

// 这一轮怎么找的（两段式才有，只给管理员）：漏斗每步剩多少 + 各关注点 + 各家搜索服务 + 预筛刷掉的原因 + 各段耗时
const FUNNEL_STEPS = [["queries", "搜索"], ["discovered", "线索"], ["prefiltered", "初筛后"], ["picked", "挑中"], ["opened", "打开了"], ["returned", "看过"], ["kept", "上网页"]];
const TIMING_NAMES = { discover: "广撒网", floor: "补搜", prefilter: "初筛", pick: "挑选", verify: "打开看" };
const secs = (v) => {
  const s = Math.max(0, Math.round(Number(v) || 0));
  return s < 60 ? `${s} 秒` : `${(s / 60).toFixed(1)} 分钟`;
};
// 这轮的模型用量（C03，只给管理员）：只算服务商实报的；没报用量的次数单列，不显示金额
function usageLine(batch) {
  const u = batch.stats && batch.stats.usage;
  if (!u || typeof u !== "object") return "";
  const kept = (batch.stats && batch.stats.kept != null ? batch.stats.kept : 0) || batch.kept || 0;
  let s = `这轮用量 ${tokens(u.reported || 0)} tokens`;
  if (u.cache_read) s += `（其中缓存读 ${tokens(u.cache_read)}）`;
  if (u.unknown_calls) s += `；有 ${u.unknown_calls} 次没报用量`;
  if (kept > 0) s += `；入选 ${kept} 条，平均每条约 ${tokens(Math.round((u.reported || 0) / kept))}`;
  return s;
}

// 中文 / 外文各多少（资讯偏中文的整改，docs/18）：问法按 zh/en，线索和上网页按 zh/foreign；旧批次没有就不显示
const LANG_ROWS = [["query_langs", "搜的词", "en", "英文"], ["discovered_langs", "线索", "foreign", "外文"], ["kept_langs", "上网页", "foreign", "外文"]];
export function langMix(f) {
  if (!f || typeof f !== "object") return "";
  return LANG_ROWS.map(([key, name, other, otherName]) => {
    const m = f[key];
    if (!m || typeof m !== "object") return "";
    const zh = Number(m.zh) || 0;
    const fo = Number(m[other]) || 0;
    if (!zh && !fo) return "";
    return `<div class="fn-row"><span class="fn-q">${name}</span><span class="fn-n">中文 ${zh} · ${otherName} ${fo}</span></div>`;
  }).join("");
}

function funnelBlock(batch) {
  const f = batch.stats && batch.stats.funnel;
  if (!admin() || !f || typeof f !== "object") return "";
  const open = state.openFunnel === batch.id;
  const kept = batch.stats.kept != null ? batch.stats.kept : f.kept;
  const steps = FUNNEL_STEPS.map(([k, n]) => [n, k === "kept" ? kept : f[k]]).filter(([, v]) => v != null);
  const kv = (obj) => Object.entries(obj || {}).filter(([, v]) => v != null);
  const perFocus = (f.per_focus || []).map((x) => `<div class="fn-row"><span class="fn-q">${esc(x.query || "")}</span><span class="fn-n">搜 ${x.queries || 0} 次 · ${x.cands || 0} 条线索</span></div>`).join("");
  const provs = kv(f.providers).map(([k, v]) => `<span class="ntag">${esc(k)} ${v}</span>`).join("");
  const rejects = kv(f.rejects).sort((a, b) => b[1] - a[1]).map(([k, v]) => `<span class="ntag warn">${esc(k)} ${v}</span>`).join("");
  const times = kv(f.timings_s).map(([k, v]) => `<span class="ntag">${esc(TIMING_NAMES[k] || k)} ${secs(v)}</span>`).join("");
  const usageTxt = usageLine(batch);
  const langs = langMix(f);
  return `
    <div class="rej funnel">
      <button class="rej-head" data-act="funnel-toggle" data-id="${batch.id}" aria-expanded="${open}">
        这轮怎么找的 <span class="rej-hint">${steps.map(([, v]) => v || 0).join(" → ")}</span>${SVG.down}
      </button>
      ${
        open
          ? `<div class="rej-list fn-box">
          <div class="fn-steps">${steps.map(([n, v], i) => `${i ? `<span class="fn-arrow">→</span>` : ""}<span class="fn-step"><b>${v || 0}</b><small>${n}</small></span>`).join("")}</div>
          ${perFocus ? `<div class="fn-h">按关注点</div>${perFocus}` : ""}
          ${langs ? `<div class="fn-h">中外来源</div>${langs}` : ""}
          ${provs ? `<div class="fn-h">按搜索服务</div><div class="fn-tags">${provs}</div>` : ""}
          ${rejects ? `<div class="fn-h">初筛刷掉的</div><div class="fn-tags">${rejects}</div>` : ""}
          ${times ? `<div class="fn-h">各步耗时</div><div class="fn-tags">${times}</div>` : ""}
          ${usageTxt ? `<div class="fn-h">模型用量</div><p class="fine" style="margin:0">${esc(usageTxt)}</p>` : ""}
        </div>`
          : ""
      }
    </div>`;
}

function rejectedBlock(batch) {
  const list = batch.rejected || [];
  if (!admin() || !list.length) return "";
  const open = state.openRejected === batch.id;
  return `
    <div class="rej">
      <button class="rej-head" data-act="rej-toggle" data-id="${batch.id}" aria-expanded="${open}">
        筛掉的 ${list.length} 条 <span class="rej-hint">仅你可见</span>${SVG.down}
      </button>
      ${
        open
          ? `<div class="rej-list">${list
              .map(
                (r) => `
          <div class="rej-row">
            <div class="rej-main">
              <a href="${safeUrl(r.url)}" target="_blank" rel="noopener noreferrer" class="rej-title">${esc(r.title || r.url)}</a>
              <div class="rej-why"><span class="ntag ${r.gate === "hard" || r.gate === "score" ? "warn" : ""}">${r.gate === "hard" ? "直接淘汰" : r.gate === "score" ? "没打分" : "分不够"}</span>${esc(r.reason || "")}${r.avg != null ? ` · ${Number(r.avg).toFixed(1)} 分` : ""}</div>
              ${r.src && r.src.query ? `<div class="rej-src">搜「${esc(r.src.query)}」得到${r.src.provider ? ` · ${esc(r.src.provider)}` : ""}</div>` : ""}
            </div>
            ${r.site ? `<button class="btn small" data-act="block-domain" data-domain="${esc(r.site)}">屏蔽 ${esc(r.site)}</button>` : ""}
          </div>`
              )
              .join("")}</div>`
          : ""
      }
    </div>`;
}

function newsRunBtn() {
  if (!gadmin()) return "";
  const running = state.newsRunning === state.g;
  return `<button class="btn small news-run" data-act="news-run" ${running ? "disabled" : ""}>${running ? "正在找…" : "现在找一批"}</button>`;
}

function newsSwitch() {
  const t = state.newsTab || "news";
  return `<div class="news-bar"><div class="seg" role="tablist">
    <button role="tab" data-act="news-tab" data-t="news" aria-selected="${t === "news"}">资讯</button>
    <button role="tab" data-act="news-tab" data-t="guides" aria-selected="${t === "guides"}">文章</button>
  </div>${newsRunBtn()}</div>`;
}

export function viewNews(g, v) {
  if (g.fresh) {
    return `<h1 class="h-page h-fish">${agentFish("news", 44)}<span>资讯</span></h1>` + emptyState("seedling", "正在了解这个群", "了解后就开始找资讯");
  }
  if ((state.newsTab || "news") === "guides") {
    const guides = (v && v.guides) || [];
    let html = newsSwitch() + `<h1 class="h-page">文章</h1><p class="h-meta">教程、好文章和好工具</p>`;
    if (!guides.length) return html + emptyState("books", "还没有文章", "");
    return html + guides.map((it, k) => newsItem(it, k, true)).join("");
  }
  const news = (v && v.news) || [];
  if (!news.length) {
    return newsSwitch() + `<h1 class="h-page h-fish">${agentFish("news", 44)}<span>资讯</span></h1>` + emptyState("newspaper", "还没有资讯", "");
  }
  let i = 0;
  return (
    newsSwitch() +
    news
      .map((batch, b) => {
        const rc = batch.rejected_count || 0;
        const head = `<h1 class="h-page${b ? "" : " h-fish"}" ${b ? 'style="margin-top:46px"' : ""}>${b ? "" : agentFish("news", 44)}<span>${esc(slotName(batch.slot_ts))}</span></h1><p class="h-meta">${hhmm(batch.slot_ts)} 找资讯 · ${batchStats(batch)}${rc ? ` · 筛掉 ${rc} 条` : ""}</p>`;
        if (batch.skipped || !(batch.items || []).length) {
          // 收了东西但资讯栏是空的：收的都是文章，指过去，别说「没有值得看的」
          const got = Number((batch.stats && batch.stats.kept) || batch.kept || 0);
          const note = got > 0
            ? `${ico("books")}<span>另有 ${got} 篇在「文章」里</span><button class="btn small" data-act="news-tab" data-t="guides">去看</button>`
            : `${ico("teacup")}<span>${esc(batch.note || "这批没有好的，跳过了")}</span>`;
          return head + `<div class="skipped enter${got > 0 ? " to-guides" : ""}" style="--i:${i++}">${note}</div>` + funnelBlock(batch) + rejectedBlock(batch);
        }
        return head + batch.items.map((it) => newsItem(it, i++, false)).join("") + funnelBlock(batch) + rejectedBlock(batch);
      })
      .join("")
  );
}
