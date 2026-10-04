// MaiWork 网页 · 设置 ·「资讯来源」：屏蔽 + RSS。
import { dayWord, esc, ico } from "../util.js";
import { api, mq } from "../api.js";
import { state } from "../state.js";
import { feedsSettings } from "./models.js";

/* ───── 资讯来源：屏蔽 + RSS ───── */
export function sourcesPage(s) {
  const rss = ((s.feeds || {}).rss) || {};
  const groups = s.groups || [];
  return `
    <h2 class="h-sub" style="margin-top:18px">RSS</h2>
    <p class="h-meta">优先看这些来源，每个群最多 20 个</p>
    ${
      groups.length
        ? groups
            .map((g) => {
              const list = rss[g.id] || [];
              return `
        <div class="rss-group">
          <div class="rss-gname">${mq(g.name || `群 ${g.id}`)}</div>
          ${list
            .map(
              (f) => `
            <div class="set-row">${ico("newspaper")}<div><div class="set-name">${esc(f.title || f.url)}</div><div class="set-text mono-link">${esc(f.url)}</div>${f.last_error ? `<div class="set-text bad-t">上次没取到：${esc(f.last_error)}</div>` : f.last_ok_ts ? `<div class="set-text">${esc(dayWord(f.last_ok_ts))}取过</div>` : ""}</div>
              <span class="row-btns"><button class="btn small" data-act="rss-toggle" data-g="${esc(g.id)}" data-id="${esc(f.id)}" data-on="${f.enabled ? "0" : "1"}">${f.enabled ? "停用" : "启用"}</button><button class="btn small" data-act="rss-del" data-g="${esc(g.id)}" data-id="${esc(f.id)}">删掉</button></span></div>`
            )
            .join("")}
          <form class="rss-add" data-g="${esc(g.id)}" autocomplete="off">
            <input type="url" inputmode="url" spellcheck="false" placeholder="https://…/feed.xml" />
            <button class="btn small" type="submit">加上</button>
          </form>
        </div>`;
            })
            .join("")
        : `<p class="h-meta">还没有服务群。</p>`
    }
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
