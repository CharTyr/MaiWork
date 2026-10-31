// MaiWork 网页 · 成品页内预览；HTML 隔离同时由服务端 CSP 和 iframe 沙箱保证。
import { $, gadmin, state } from "./state.js";
import { esc, safeAssetUrl, toast } from "./util.js";
import { api } from "./api.js";
import { openSheet, sheetChrome } from "./sheet.js";
import { loadTask } from "./detail.js";

const REPUBLISH_ERR = {
  throttled: "操作过于频繁，请稍后再试",
  not_due: "当前外链未到期",
  no_herenow: "外链服务暂不可用，请直接使用本地预览",
  publish_failed: "外链生成失败，请稍后再试",
  no_artifact: "未找到可重发的成品文件",
  empty: "成品目录没有可公开文件",
  not_found: "未找到该任务的托管外链",
};

let preview = null;
export function openPreview(rec, tid) {
  const src = rec && (rec.local_url || (!rec.expired && rec.url));
  if (safeAssetUrl(src) === "#") return;
  preview = { src, url: src, localUrl: String(rec.local_url || ""), expired: !!rec.expired, tid: String(tid || ""), group: state.g };
  openSheet("preview");
}

export function previewSheet() {
  if (!preview) return "";
  return `<h1 class="h-page">成品预览</h1>
    <div class="pv-bar">
      <a class="btn small" href="${safeAssetUrl(preview.url)}" target="_blank" rel="noopener noreferrer">在新标签打开</a>
      ${preview.localUrl && (preview.expired || gadmin()) && preview.tid ? `<button class="btn small" data-act="preview-republish" data-tid="${esc(preview.tid)}">重新生成外链</button>` : ""}
    </div>
    <div id="pv-frame" class="pv-frame"><iframe src="${safeAssetUrl(preview.src)}" sandbox="allow-scripts" referrerpolicy="no-referrer" title="成品预览"></iframe></div>`;
}

export async function actPreview(el) {
  if (el.dataset.act === "preview") {
    openPreview({ url: el.dataset.url, local_url: el.dataset.localUrl, expired: el.dataset.expired === "1" }, el.dataset.tid);
    return true;
  }
  if (el.dataset.act !== "preview-republish") return false;
  const current = preview;
  if (!current || el.disabled || el.dataset.tid !== current.tid) return true;
  const visible = () => preview === current && state.g === current.group && state.sheet === "preview";
  el.disabled = true;
  el.textContent = "正在重新生成…";
  try {
    const r = await api("POST", `/api/tasks/${encodeURIComponent(current.tid)}/republish`, {});
    if (!visible()) return true;
    if (!r || !r.ok || safeAssetUrl(r.url) === "#") {
      throw new Error((r && REPUBLISH_ERR[r.reason]) || "重新生成外链失败");
    }
    current.src = current.url = r.url;
    current.expired = false;
    $("sheet").innerHTML = sheetChrome() + previewSheet();
    toast("已生成新链接");
    await loadTask(current.tid);
  } catch (e) {
    if (visible()) {
      toast(e.message, true);
      el.disabled = false;
      el.textContent = "重新生成外链";
    }
  }
  return true;
}
