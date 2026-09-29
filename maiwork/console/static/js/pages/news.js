// MaiWork 网页 · 「资讯」页。
import { admin, gadmin, state } from "../state.js";
import { SVG, dayWord, dur, esc, hhmm, ico, now, richText, safeUrl, slotName, when } from "../util.js";
import { api, gview } from "../api.js";

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
  // 默认收起，点标题展开；展开状态记在内存里，轮询重画后不会自己合上
  const open = !!reasonOpen[it.id];
  return `
    <details class="reason" data-rid="${esc(it.id)}"${open ? " open" : ""}>
      <summary class="reason-h">我发这条的原因</summary>
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
  ["old", "太旧了"],
  ["useless", "没什么用"],
  ["low", "质量不高"],
  ["offtopic", "和本群无关"],
  ["dup", "以前发过"],
  ["wrong", "说得不准"],
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
  return `<div class="ratings"><div class="ratings-h">${r.total} 位群友评价了这条</div>${counts ? `<div class="ntags">${counts}</div>` : ""}${notes}</div>`;
}

function newsItem(it, i, guide) {
  const sc = it.scores || null;
  const rated = !!myRates()[it.id];
  const tags = [
    it.topic ? `<span class="ntag">${esc(it.topic)}</span>` : "",
    it.angle === "diverse" ? `<span class="ntag alt">换个角度</span>` : "",
    it.angle === "explore" ? `<span class="ntag ex">拓展</span>` : "",
    it.sensitive ? `<span class="ntag warn">争议话题</span>` : "",
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
        ${it.bridge && it.angle === "explore" ? `<p class="n-bridge"><span aria-hidden="true">↳</span>${esc(it.bridge)}</p>` : ""}
        <p class="item-body">${richText(it.body || it.summary)}</p>
        ${img}
        ${verifyBlock(it.verify)}
        ${reasonBlock(it)}
        ${ratingsBlock(it)}
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
          <button class="ratebtn" data-act="rate-open" data-id="${it.id}" aria-pressed="${rated}" title="说说这条哪里不好">${SVG.pen}<i>${rated ? "已评价" : "评价"}</i></button>
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
  return `${when_}${it.created_ts ? dayWord(it.created_ts) + "找到" : "核对过还适用"}`;
}

// 每轮找资讯的账：有统计就说搜了几次、看了几篇、收了几条；老数据没统计按旧说法
function batchStats(batch) {
  const st = batch.stats;
  if (st && typeof st === "object") return `搜了 ${st.searches || 0} 次，看了 ${st.pages || 0} 篇，收了 ${st.kept || 0} 条`;
  return `找了 ${batch.found || 0} 条，留下 ${batch.kept || 0} 条`;
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
              <div class="rej-why"><span class="ntag ${r.gate === "hard" || r.gate === "score" ? "warn" : ""}">${r.gate === "hard" ? "硬性淘汰" : r.gate === "score" ? "没评上分" : "分数不够"}</span>${esc(r.reason || "")}${r.avg != null ? ` · ${Number(r.avg).toFixed(1)} 分` : ""}</div>
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
  if (!pref && !gadmin()) return "";
  return `<div class="pref">${ico("pushpin", "")}<div class="pref-t">${pref ? `<b>这个群想看：</b>${esc(pref)}` : `想看什么、不想看什么，可以写一句`}</div>${gadmin() ? `<button class="btn small" data-act="pref-edit">${pref ? "改" : "写一句"}</button>` : ""}</div>`;
}

function newsRunBtn() {
  if (!gadmin()) return "";
  const running = state.newsRunning === state.g;
  return `<button class="btn small news-run" data-act="news-run" ${running ? "disabled" : ""}>${running ? "在备料…" : "现在就备一批"}</button>`;
}

function newsSwitch() {
  const t = state.newsTab || "news";
  return prefBox(gview()) + `<div class="news-bar"><div class="seg" role="tablist">
    <button role="tab" data-act="news-tab" data-t="news" aria-selected="${t === "news"}">资讯</button>
    <button role="tab" data-act="news-tab" data-t="guides" aria-selected="${t === "guides"}">文章</button>
  </div>${newsRunBtn()}</div>`;
}

export function viewNews(g, v) {
  if (g.fresh) {
    return `<h1 class="h-page">资讯</h1>` + emptyState("seedling", "还在熟悉这个群", "熟悉之后就开始找资讯。");
  }
  if ((state.newsTab || "news") === "guides") {
    const guides = (v && v.guides) || [];
    let html = newsSwitch() + `<h1 class="h-page">文章</h1><p class="h-meta">教程、好文章和好用的工具</p>`;
    if (!guides.length) return html + emptyState("books", "还没有文章", "");
    return html + guides.map((it, k) => newsItem(it, k, true)).join("");
  }
  const news = (v && v.news) || [];
  if (!news.length) {
    return newsSwitch() + `<h1 class="h-page">资讯</h1>` + emptyState("newspaper", "还没有资讯", "");
  }
  let i = 0;
  return (
    newsSwitch() +
    news
      .map((batch, b) => {
        const rc = batch.rejected_count || 0;
        const head = `<h1 class="h-page" ${b ? 'style="margin-top:46px"' : ""}>${esc(slotName(batch.slot_ts))}</h1><p class="h-meta">${hhmm(batch.slot_ts)} 备料 · ${batchStats(batch)}${rc ? `，筛掉 ${rc} 条` : ""}</p>`;
        if (batch.skipped || !(batch.items || []).length) {
          return head + `<div class="skipped enter" style="--i:${i++}">${ico("teacup")}<span>${esc(batch.note || "这一批没有值得看的，跳过了。")}</span></div>` + rejectedBlock(batch);
        }
        return head + batch.items.map((it) => newsItem(it, i++, false)).join("") + rejectedBlock(batch);
      })
      .join("")
  );
}
