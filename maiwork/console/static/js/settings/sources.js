// MaiWork 网页 · 设置 ·「资讯来源」：屏蔽 + RSS。
import { dayWord, esc, ico } from "../util.js";
import { mq } from "../api.js";
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
    <h2 class="h-sub">屏蔽的来源</h2>
    ${feedsSettings(s.feeds, true)}`;
}
