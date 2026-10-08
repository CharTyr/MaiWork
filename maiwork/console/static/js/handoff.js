// MaiWork 网页 · 交接包（docs/24）：把构想 / 任务写成一份说明，复制或下载给自己的个人 agent。
// 预览 GET 不计数；真点了复制 / 下载才 POST /taken 记一次（失败不影响复制）。
import { $, desktop, state } from "./state.js";
import { SVG, esc, toast } from "./util.js";
import { api } from "./api.js";
import { clientId, loading } from "./pages/news.js";
import { findIdea, ideaItems, ideaPicked } from "./pages/ideas.js";
import { openSheet, repaintSheet } from "./sheet.js";

let seq = 0;

function ideaItemsParam(id) {
  const it = findIdea(`I-${id}`);
  if (!it) return [];
  const all = ideaItems(it);
  const open = it.state === "new" || it.state === "wanted";
  const picked = open ? ideaPicked(it) : all;
  // 全选 = 不带参数（口径同「直接开工」：不带 = 全部）
  return all.length && picked.length < all.length ? picked.map((x) => x.no) : [];
}

export async function openHandoff(kind, id) {
  const my = ++seq;
  const items = kind === "idea" ? ideaItemsParam(id) : [];
  // 手机上详情就在抽屉里，交接包会占掉抽屉：记下来，关掉时回到详情
  const back = !desktop.matches && state.sheet === "detail";
  state.handoff = { kind, id: String(id), items, back, data: null, err: "" };
  openSheet("handoff");
  const path =
    kind === "idea"
      ? `/api/handoff/idea/${encodeURIComponent(id)}${items.length ? `?items=${items.join(",")}` : ""}`
      : `/api/handoff/task/${encodeURIComponent(id)}`;
  try {
    const data = await api("GET", path);
    if (my !== seq || !state.handoff) return;
    state.handoff.data = data;
  } catch (err) {
    if (my !== seq || !state.handoff) return;
    state.handoff.err = err.status === 409 ? "这份内容里有不该带出去的信息，没法生成" : err.message;
  }
  if (state.sheet === "handoff") repaintSheet();
}

export function handoffSheet() {
  const h = state.handoff;
  const head = `<h1 class="h-page">交给我的 agent</h1><p class="h-meta sheet-lead">复制或下载，交给你自己的 AI 接着做</p>`;
  if (!h) return head;
  if (h.err) return `${head}<div class="ho-err"><b>没法带出去</b><span>${esc(h.err)}</span></div>`;
  if (!h.data) return head + loading();
  const d = h.data;
  return `${head}
    ${d.truncated ? `<p class="fine ho-cut">内容较长，已省略部分参考资料 / 验收意见</p>` : ""}
    <textarea class="ho-text" id="ho-text" readonly spellcheck="false" aria-label="交接包内容">${esc(d.markdown || "")}</textarea>
    <div class="actions ho-acts">
      <button class="btn primary" data-act="handoff-copy">${SVG.copy}复制</button>
      <button class="btn" data-act="handoff-download">下载 .md</button>
    </div>
    <p class="fine">不含人名、群聊原话和本群链接</p>`;
}

function taken(action) {
  const h = state.handoff;
  if (!h) return;
  const body = { action, client: clientId() };
  if (h.items.length) body.items = h.items;
  api("POST", `/api/handoff/${h.kind}/${encodeURIComponent(h.id)}/taken`, body).catch(() => {});
}

export async function handoffCopy() {
  const h = state.handoff;
  if (!h || !h.data) return;
  try {
    await navigator.clipboard.writeText(h.data.markdown || "");
    toast("已复制，贴给你的 AI 就行");
    taken("copy");
  } catch (err) {
    // 网页不是 https 时浏览器常不给剪贴板：选中全文，让人自己复制
    const box = $("ho-text");
    if (box) {
      box.focus();
      box.select();
    }
    toast("已选中全文，请手动复制", true);
    taken("copy");
  }
}

export function handoffDownload() {
  const h = state.handoff;
  if (!h || !h.data) return;
  const blob = new Blob([h.data.markdown || ""], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = h.data.filename || "maiwork-handoff.md";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  taken("download");
}
