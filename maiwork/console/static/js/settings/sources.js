// MaiWork 网页 · 设置 ·「资讯来源」：RSS（含自动订阅）+ 优质来源 + 屏蔽。
import { dayWord, esc, ico, now } from "../util.js";
import { api, mq } from "../api.js";
import { state } from "../state.js";
import { feedsSettings } from "./models.js";

/* ───── 资讯来源：屏蔽 + RSS ───── */
export function sourcesPage(s) {
  const rss = ((s.feeds || {}).rss) || {};
  const groups = s.groups || [];
  return `
    <h2 class="h-sub" style="margin-top:18px">RSS</h2>
    <p class="h-meta">优先看这些来源，每个群最多 20 个；标「自动」的是 MaiWork 按群里的反应自己订的，删掉就不再推荐</p>
    ${groups.length ? groups.map((g) => rssGroup(g, rss[g.id] || [])).join("") : `<p class="h-meta">还没有服务群。</p>`}
    <h2 class="h-sub">优质来源</h2>
    <p class="h-meta">从每个群最近上了网页的高分资讯里自动攒出来的，找资讯时会优先去这些站看；不想要的可以移出</p>
    ${groups.map((g) => trustedGroup(g)).join("")}
    <h2 class="h-sub">屏蔽的来源</h2>
    ${feedsSettings(s.feeds, groups)}`;
}

// 优质来源（source_stats.py）：每个群懒加载进 state.trusted[群]
function trustedGroup(g) {
  const all = (state.trusted = state.trusted || {});
  if (!(g.id in all)) {
    all[g.id] = null;
    api("GET", `/api/groups/${encodeURIComponent(g.id)}/trusted-sources`)
      .then((r) => {
        all[g.id] = r || { trusted: [], removed: [] };
        const box = document.querySelector(`.trusted-g[data-g="${CSS.escape(g.id)}"]`);
        if (box) box.outerHTML = trustedGroup(g);
      })
      .catch(() => (all[g.id] = undefined));
  }
  const v = all[g.id];
  const rows = v
    ? (v.trusted || [])
        .map(
          (t) => `
      <div class="set-row">${ico("pushpin")}<div><div class="set-name">${esc(t.domain)}</div><div class="set-text">近期 ${t.high} 条高分${t.up ? `，群友点有用 ${t.up} 次` : ""}</div></div>
        <span class="row-btns"><button class="btn small" data-act="trusted-toggle" data-g="${esc(g.id)}" data-domain="${esc(t.domain)}" data-on="1">移出</button></span></div>`
        )
        .join("") +
      (v.removed || [])
        .map(
          (d) => `
      <div class="set-row is-off">${ico("pushpin")}<div><div class="set-name">${esc(d)}</div><div class="set-text">你移出的</div></div>
        <span class="row-btns"><button class="btn small" data-act="trusted-toggle" data-g="${esc(g.id)}" data-domain="${esc(d)}" data-on="0">放回</button></span></div>`
        )
        .join("")
    : "";
  return `
    <div class="rss-group trusted-g" data-g="${esc(g.id)}">
      <div class="rss-gname">${mq(g.name || `群 ${g.id}`)}</div>
      ${v == null ? `<p class="fine">读取中…</p>` : rows || `<p class="fine">还没攒出来：同一个站至少要有 2 条高分资讯</p>`}
    </div>`;
}

// RSS 一个群：源列表 + 自动订阅说明（自动订阅记录 / 来源地图 / 命中率按群懒加载进 state.rssAuto[群]，
// 接口 GET /api/groups/{gid}/rss，docs/07 §10.4e）
function rssGroup(g, list) {
  const all = (state.rssAuto = state.rssAuto || {});
  if (!(g.id in all)) {
    all[g.id] = null;
    api("GET", `/api/groups/${encodeURIComponent(g.id)}/rss`)
      .then((r) => {
        all[g.id] = r || {};
        const box = document.querySelector(`.rss-g[data-g="${CSS.escape(g.id)}"]`);
        if (box) box.outerHTML = rssGroup(g, list);
      })
      .catch(() => delete all[g.id]); // 没取到：下次画设置页再取，不卡死在「取过了」
  }
  const v = all[g.id] || null;
  const hits = (v && v.auto && v.auto.hits) || {};
  return `
        <div class="rss-group rss-g" data-g="${esc(g.id)}">
          <div class="rss-gname">${mq(g.name || `群 ${g.id}`)}</div>
          ${list.map((f) => rssRow(g, f, hits[f.id])).join("")}
          <form class="rss-add" data-g="${esc(g.id)}" autocomplete="off">
            <input type="url" inputmode="url" spellcheck="false" placeholder="https://…/feed.xml" aria-label="RSS 地址" />
            <button class="btn small" type="submit">加上</button>
          </form>
          ${v ? autoDetails(v) : ""}
        </div>`;
}

function rssRow(g, f, hit) {
  const lines = [];
  if (f.auto && f.reason) lines.push(`<div class="set-text">${esc(f.reason)}</div>`);
  if (f.auto && f.trial_until > now()) lines.push(`<div class="set-text">试用到${esc(dayWord(f.trial_until))}，两周没成绩会自己退订</div>`);
  if (hit) lines.push(`<div class="set-text">近 ${hit.days} 天给了 ${hit.cand} 条，进资讯 ${hit.kept} 条</div>`);
  if (f.last_error) lines.push(`<div class="set-text bad-t">上次没取到：${esc(f.last_error)}</div>`);
  else if (f.last_ok_ts) lines.push(`<div class="set-text">${esc(dayWord(f.last_ok_ts))}取过</div>`);
  const tag = f.auto ? ` <span class="tag rss-auto-tag">自动</span>` : "";
  return `
          <div class="set-row">${ico("newspaper")}<div><div class="set-name">${esc(f.title || f.url)}${tag}</div><div class="set-text mono-link">${esc(f.url)}</div>${lines.join("")}</div>
            <span class="row-btns"><button class="btn small" data-act="rss-toggle" data-g="${esc(g.id)}" data-id="${esc(f.id)}" data-on="${f.enabled ? "0" : "1"}">${f.enabled ? "停用" : "启用"}</button><button class="btn small" data-act="rss-del" data-g="${esc(g.id)}" data-id="${esc(f.id)}"${f.auto ? ` data-auto="1"` : ""}>删掉</button></span></div>`;
}

const LOG_VERB = { subscribed: "订上", unsubscribed: "退订", rejected: "不再推荐", skipped: "没订上" };
// 0.9.0 上线首轮把「体检没过 / 找不到订阅地址」也记成了 rejected，按原因认回「没订上」
const logVerb = (e) =>
  e.action === "rejected" && /^(体检没过|没找到订阅地址)/.test(e.reason || "") ? LOG_VERB.skipped : LOG_VERB[e.action] || e.action;

// 自动订阅：名额 + 规则 + 最近记录 + 来源地图，折叠起来，不抢源列表的位置
function autoDetails(v) {
  const a = v.auto || {};
  const q = a.quota || {};
  const used = (k) => `${(q[k] || {}).used || 0}/${(q[k] || {}).max || 0}`;
  const log = (v.auto_log || []).slice(0, 10);
  const map = ((v.source_map || {}).sources) || [];
  const logHtml = log.length
    ? `<ul>${log
        .map(
          (e) => `<li><span class="ra-title">${esc(logVerb(e))} ${esc(e.label || e.url)}</span><span class="ra-why">${esc(dayWord(e.ts))}${e.reason ? ` · ${esc(e.reason)}` : ""}</span></li>`
        )
        .join("")}</ul>`
    : `<p class="fine">还没自动订过</p>`;
  const mapHtml = map.length
    ? `<ul>${map
        .map(
          (m) => `<li><span class="ra-title">${esc(m.name || m.label)}${m.label && m.label !== m.name ? ` <span class="ra-label">${esc(m.label)}</span>` : ""}</span><span class="ra-why">${
            m.status === "verified" ? `能订 · ${esc(m.why || "")}` : `没订：${esc(m.reason || "没通过检查")}`
          }</span></li>`
        )
        .join("")}</ul>`
    : `<p class="fine">群画像成形后每周列一次</p>`;
  return `
          <details class="rss-auto">
            <summary>自动订阅：门槛来的 ${used("trusted")} · 来源地图 ${used("map")}</summary>
            ${a.rule ? `<p class="ra-rule">${esc(a.rule)}</p>` : ""}
            <div class="ra-h">最近</div>
            ${logHtml}
            <div class="ra-h">来源地图</div>
            ${mapHtml}
          </details>`;
}
