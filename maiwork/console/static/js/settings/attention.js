// MaiWork 网页 · 设置 · 概况里的「需要你处理」（2026-10-15 线上巡检后加）：
// - 群友 @ 的派活请求拿不准、挂在「待复核」没人管（GET /api/asks/review；在群页「任务」里处理）；
// - 资讯卡片没发出去 / 不确定发没发出去（GET /api/news/cards；可手动重发，每张每天 1 次）。
// 都只总管理员看；刷新失败保留旧记录并提示，模块未提供时隐藏对应部分。
import { state } from "../state.js";
import { esc, ico, tip, toast } from "../util.js";
import { api } from "../api.js";
import { repaintSheet } from "../sheet.js";

export async function loadAttention() {
  const previous = state.attention || {};
  const errors = {};
  const load = (key, url) => api("GET", url).catch((err) => {
    if ([404, 503].includes(err.status)) return null;
    errors[key] = err.message;
    return previous[key] || null;
  });
  const [asks, cards] = await Promise.all([
    load("asks", "/api/asks/review"),
    load("cards", "/api/news/cards?limit=10"),
  ]);
  state.attention = { asks, cards };
  state.attentionErr = errors;
}

const CARD_STATE = { failed: "没发出去", uncertain: "不确定是否发出" };
const FALLBACK_STATE = {
  queued: "文字版正在排队",
  sent: "已改发文字版",
  failed: "文字版也没发出去",
  uncertain: "文字版不确定是否发出",
  dropped: "文字版已取消",
  no_items: "没有可发的文字内容",
};

function when(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function asksBlock(a) {
  if (!a || !a.total) return "";
  // 按群汇总：每群一行，点了去这个群的「任务」页处理
  const by = new Map();
  for (const it of a.items || []) {
    const g = by.get(it.group_id) || { name: it.group_name, n: 0, oldest: it.wait_text };
    g.n += 1;
    by.set(it.group_id, g);
  }
  const rows = [...by.entries()]
    .map(
      ([gid, g]) => `<div class="set-row">${ico("speech")}<div><div class="set-name">${esc(g.name || `群 ${gid}`)}</div><div class="set-text">${Number((a.group_counts || {})[gid]) || g.n} 条 @ 请求等你判断，最久等了 ${esc(g.oldest || "")}</div></div><span class="row-btns"><button class="btn small" data-act="att-asks" data-g="${esc(gid)}">去处理</button></span></div>`
    )
    .join("");
  return `<h3 class="h-mini">待复核的 @ 请求${tip("群友 @ 时说的话像在派活，但 MaiWork 拿不准，没有自动处理。转为请求后仍需审批；超过 14 天没处理会自动归档。")}</h3>${rows}`;
}

function cardsBlock(c) {
  const list = (c && c.cards) || [];
  if (!list.length) return "";
  const rows = list
    .map((k) => {
      const title = (k.titles || []).slice(0, 2).join("；") || `第 ${k.id} 张卡片`;
      const fallback = FALLBACK_STATE[(k.fallback || {}).status];
      const fb = fallback ? ` · ${fallback}` : "";
      return `<div class="set-row cp">${ico("newspaper")}<div><div class="set-name">${esc(k.group_name || `群 ${k.group_id}`)} · ${esc(when(k.created))}</div><div class="set-text">${esc(CARD_STATE[k.status] || k.status)}${fb}：${esc(title)}</div>${k.error ? `<p class="fine">${esc(k.error)}</p>` : ""}</div><span class="row-btns"><button class="btn small" data-act="att-resend" data-id="${esc(k.id)}">重发</button></span></div>`;
    })
    .join("");
  return `<h3 class="h-mini">没发出去的资讯卡片${tip("没发出去的卡片会自动改发一条文字版；也可以手动重发，每张每天一次。「不确定」的可能已经发出，重发前请先到群里查看。")}</h3>${rows}`;
}

export function attentionSection() {
  const a = state.attention;
  if (!a) return "";
  const errors = Object.values(state.attentionErr || {});
  const error = errors.length ? `<p class="h-meta" role="status">待处理事项刷新失败：${esc(errors.join("；"))} <button type="button" class="link-btn" data-act="att-reload">重试</button></p>` : "";
  const body = error + asksBlock(a.asks) + cardsBlock(a.cards);
  return body ? `<h2 class="h-sub">需要你处理</h2>${body}` : "";
}

export async function actAttention(act, el) {
  switch (act) {
    case "att-reload": {
      el.disabled = true;
      await loadAttention();
      repaintSheet();
      return true;
    }
    case "att-asks": {
      const { go } = await import("../router.js"); // 动态引入：router 也引设置页，避免循环
      go({ page: null, g: el.dataset.g, tab: "tasks", detail: null, filter: "all" });
      return true;
    }
    case "att-resend": {
      el.disabled = true;
      try {
        const out = await api("POST", `/api/news/cards/${encodeURIComponent(el.dataset.id)}/resend`);
        toast((out && out.message) || "已重新排队发送");
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
        return true;
      }
      await loadAttention();
      repaintSheet();
      return true;
    }
  }
  return false;
}
