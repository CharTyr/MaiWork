// MaiWork 网页 · 所有按钮的点击动作（data-act）。
import { $, admin, state, ui } from "./state.js";
import { calm, flashCopied, flipRemove, reveal, toast } from "./util.js";
import { api, grp, gview } from "./api.js";
import { RATE_KEY, clientId, myRates } from "./pages/news.js";
import { findIdea, ideaAsk, ideaItems, ideaPicked } from "./pages/ideas.js";
import { refreshDetail } from "./detail.js";
import { handoffCopy, handoffDownload, openHandoff } from "./handoff.js";
import { actSettingsNav } from "./settings/index.js";
import { actExt } from "./settings/ext.js";
import { actNews } from "./actions_news.js";
import { actTheme } from "./theme.js";
import { actAppearance } from "./settings/appearance.js";
import { actSocial } from "./settings/social.js";
import { actCtx } from "./pages/groupctx.js";
import { actControls } from "./pages/groupcontrols.js";
import { actGroup } from "./pages/group.js";
import { actAgents } from "./settings/agents.js";
import { actModels } from "./settings/models.js";
import { actJev } from "./settings/jev.js";
import { actOptDeps } from "./settings/optdeps.js";
import { actAttention } from "./settings/attention.js";
import { actPreview } from "./preview.js";
import { chipAdd, rowsAdd } from "./settings/rules.js";
import { loadUsage, loadUsageDay } from "./settings/usage.js";
import { avatarSaved } from "./settings/identity.js";
import { loadLogs, loadLogSummary } from "./settings/logs.js";
import { closeSheet, openDetail, openSheet, repaintSheet } from "./sheet.js";
import { loadBoardItem } from "./board.js";
import { chatPoll, enterChat, loadChat, loadChats, paintChat } from "./chat.js";
import { renderSide, renderView } from "./render.js";
import { dismissUpdate } from "./update.js";
import { enterBoard, enterSettings, go, loadGroups, loadSettings, loadView, reboot, renderTopBits, retryLoad, syncHash } from "./router.js";

/* ───────────── 交互 ───────────── */

function patchView(fn) {
  const v = gview();
  if (v) fn(v);
}
function findEntry(id) {
  const v = gview();
  for (const s of (v && v.profile) || []) for (const e of s.entries || []) if (String(e.id) === String(id)) return e;
  return null;
}

export async function act(el, e) {
  const a = el.dataset.act;
  if (await actNews(a, el)) return;
  if (await actSettingsNav(a, el)) return;
  if (await actExt(a, el, loadSettings)) return;
  if (await actTheme(a, el)) return;
  if (await actAppearance(a, el)) return;
  if (await actSocial(a, el)) return;
  if (await actCtx(a, el)) return;
  if (await actControls(a, el)) return;
  if (actGroup(a, el)) {
    ui.flash = true;
    renderView();
    return;
  }
  if (await actAgents(a, el)) return;
  if (await actModels(a, el)) return;
  if (await actJev(a, el)) return;
  if (await actOptDeps(a, el)) return;
  if (await actAttention(a, el)) return;
  if (await actPreview(el)) return;
  const g = grp();
  switch (a) {
    case "load-retry":
      await retryLoad(el);
      break;
    case "tab":
      closeSheet();
      go({ page: null, tab: el.dataset.tab, detail: null });
      break;
    case "group":
      if (!admin()) break;
      closeSheet();
      go({ page: null, g: el.dataset.g, detail: null, filter: "all" });
      break;
    case "groups":
      openSheet("groups");
      break;
    case "chat":
      await enterChat();
      break;
    case "board":
      await enterBoard();
      break;
    case "board-filter":
      state.boardFilter = el.dataset.g || "";
      renderView();
      break;
    case "board-item": {
      if (!admin()) break;
      state.boardItem = { source: el.dataset.src, id: el.dataset.id, kind: el.dataset.kind, data: null, err: "" };
      state.boardOpen = {};
      openSheet("board-item");
      renderView();
      await loadBoardItem(false);
      if (state.sheet === "board-item") repaintSheet();
      break;
    }
    case "chat-open":
      closeSheet();
      await enterChat(Number(el.dataset.id));
      break;
    case "chat-list":
      await loadChats();
      openSheet("chats");
      break;
    case "chat-new": {
      try {
        const c = await api("POST", "/api/chat", { group_id: (state.chat && state.chat.info && state.chat.info.group_id) || state.g || "" });
        closeSheet();
        await enterChat(c.id);
        setTimeout(() => $("chat-input") && $("chat-input").focus(), 50);
      } catch (err) {
        toast(err.message, true);
      }
      break;
    }
    case "chat-suggest":
      $("chat-input").value = el.dataset.t;
      $("chat-input").focus();
      break;
    case "chat-summary":
      state.chatOpen["s" + el.dataset.id] = !state.chatOpen["s" + el.dataset.id];
      paintChat(false);
      break;
    case "chat-compact": {
      const C = state.chat;
      if (!C) break;
      if (C.running) {
        toast("请在回复完成后再压缩");
        break;
      }
      el.disabled = true;
      const label = el.textContent;
      el.textContent = "正在压缩…";
      try {
        await api("POST", `/api/chat/${encodeURIComponent(C.id)}/compact`);
        await loadChat(C.id);
        paintChat(true);
        toast("已压缩");
      } catch (err) {
        toast(err.status === 409 ? "请在回复完成后再压缩" : err.message, true);
      } finally {
        el.disabled = false;
        el.textContent = label;
      }
      break;
    }
    case "chat-tool":
      state.chatOpen[el.dataset.id] = !state.chatOpen[el.dataset.id];
      paintChat(false);
      reveal(`.cm-tool-h[data-id="${el.dataset.id}"]`);
      break;
    case "chat-confirm": {
      const card = el.closest && el.closest(".cm-pending");
      el.disabled = true;
      try {
        const r = await api("POST", `/api/chat/pending/${encodeURIComponent(el.dataset.id)}`, { approve: el.dataset.ok === "1" });
        toast(el.dataset.ok === "1" ? (r.status === "done" ? "已执行" : `执行失败：${r.result || ""}`) : "已取消", r.status === "failed");
        await loadChat(state.chatId, true);
        if (state.chat) state.chat.running = true;
        await flipRemove(card, $("chat-stream"), () => paintChat(true));
        chatPoll();
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
        // 409：别的页面已经处理过 / 上一句还在跑 → 重新拉一遍，把过期的确认卡片收掉
        if (err.status === 409 && state.chatId) {
          await loadChat(state.chatId, false);
          paintChat(false);
          chatPoll();
        }
      }
      break;
    }
    case "settings":
      await enterSettings(state.page === "settings" ? state.setSub : "overview");
      break;
    case "set-sub":
      await enterSettings(el.dataset.sub);
      break;
    case "models":
      await enterSettings("models");
      break;
    case "log-expand":
      state.logs.expanded = !state.logs.expanded;
      if (state.logs.expanded) await loadLogs();
      repaintSheet();
      break;
    case "log-tab":
      state.logs.tab = el.dataset.t;
      state.logs.open = {};
      await loadLogs();
      repaintSheet();
      break;
    case "log-refresh":
      if (state.logs) state.logs.open = {};
      if (state.logs && state.logs.expanded) await loadLogs();
      else await loadLogSummary();
      repaintSheet();
      break;
    case "log-more":
      el.disabled = true;
      await loadLogs(true);
      repaintSheet();
      break;
    case "log-open": {
      const k = `${el.dataset.kind}${el.dataset.id}`;
      if (state.logs.open[k]) {
        delete state.logs.open[k];
        repaintSheet();
        break;
      }
      state.logs.open[k] = "loading";
      repaintSheet();
      reveal(`.log-head[data-kind="${el.dataset.kind}"][data-id="${el.dataset.id}"]`);
      try {
        state.logs.open[k] = await api("GET", `/api/logs/${el.dataset.kind === "m" ? "model-calls" : "tool-calls"}/${encodeURIComponent(el.dataset.id)}`);
      } catch (err) {
        delete state.logs.open[k];
        toast(err.message, true);
      }
      repaintSheet();
      reveal(`.log-head[data-kind="${el.dataset.kind}"][data-id="${el.dataset.id}"]`);
      break;
    }
    case "chip-add":
      chipAdd(el.closest(".chips"));
      break;
    case "chip-del": {
      const list = el.closest(".ci-list");
      el.closest(".ci").remove();
      if (!list.querySelector(".ci")) list.innerHTML = `<span class="ci-empty">暂无</span>`;
      break;
    }
    case "rule-reset": {
      try {
        state.rules = await api("POST", "/api/settings/config/reset", { field: el.dataset.f });
        await loadSettings();
        repaintSheet();
        toast("已恢复默认");
      } catch (err) {
        toast(err.message, true);
      }
      break;
    }
    case "cfg-clear": {
      if (!confirm("确定清除该密钥？")) break;
      try {
        state.rules = await api("PUT", "/api/settings/config", { [el.dataset.f]: null });
        await loadSettings();
        repaintSheet();
        toast("已清除");
      } catch (err) {
        toast(err.message, true);
      }
      break;
    }
    case "usage-range":
      await loadUsage(parseInt(el.dataset.n, 10) || 30);
      repaintSheet();
      break;
    case "usage-day":
      loadUsageDay(el.dataset.d).then(repaintSheet);
      repaintSheet();
      break;
    case "avatar-url": {
      const url = (prompt("图片网址（https://…）") || "").trim();
      if (!url) break;
      if (!/^https?:\/\//i.test(url)) {
        toast("网址须以 http:// 或 https:// 开头", true);
        break;
      }
      try {
        await avatarSaved(await api("POST", "/api/settings/avatar", { url }));
        toast("头像已更新");
      } catch (err) {
        toast(err.message, true);
      }
      break;
    }
    case "avatar-reset":
      try {
        await avatarSaved(await api("DELETE", "/api/settings/avatar"));
        toast("已恢复为 MaiBot 头像");
      } catch (err) {
        toast(err.message, true);
      }
      break;
    case "cfg-goto":
      state.cfgSec = el.dataset.s || "";
      await enterSettings("rules", "config");
      break;
    case "cfg-sec":
      state.cfgSec = el.dataset.s || "";
      repaintSheet();
      break;
    case "re-add":
      rowsAdd(el.closest(".rows-ed"));
      break;
    case "re-del":
      el.closest(".re-row").remove();
      break;
    case "rss-toggle":
    case "rss-del": {
      const g = el.dataset.g;
      const id = el.dataset.id;
      const ask = el.dataset.auto === "1" ? "确定删除该来源？删除后不再推荐。" : "确定删除该 RSS？";
      if (a === "rss-del" && !confirm(ask)) break;
      try {
        if (a === "rss-del") await api("DELETE", `/api/groups/${encodeURIComponent(g)}/rss/${encodeURIComponent(id)}`);
        else await api("POST", `/api/groups/${encodeURIComponent(g)}/rss/${encodeURIComponent(id)}/toggle`, { enabled: el.dataset.on === "1" });
        if (state.rssAuto) delete state.rssAuto[g]; // 自动订阅记录 / 命中率跟着重拉
        await loadSettings();
        repaintSheet();
      } catch (err) {
        toast(err.message, true);
      }
      break;
    }
    case "news-run": {
      // 后端给运行编号和真实状态（docs/13 A11）：开不了当场说原因；开了就看状态，不靠时间猜
      const gid = state.g;
      el.disabled = true;
      try {
        const r = await api("POST", `/api/groups/${encodeURIComponent(gid)}/news/run`, {});
        const runId = (r && r.run_id) || "";
        state.newsRunning = gid;
        renderView();
        toast("已开始搜集，结果将在几分钟后显示");
        let n = 0;
        const tick = async () => {
          n += 1;
          let st = null;
          try {
            st = await api("GET", `/api/groups/${encodeURIComponent(gid)}/news/run`);
          } catch (_) {}
          const mine = st && (!runId || st.run_id === runId);
          if (mine && st.state !== "running") {
            state.newsRunning = null;
            if (state.g === gid) await loadView(true);
            renderView();
            if (st.state === "done") toast(st.reason || "已获取新资讯");
            else if (st.state === "skipped") toast(`本次未找到新资讯：${st.reason || "无合适内容"}`);
            else toast(`搜集失败：${st.reason || "未知错误"}`, true);
            return;
          }
          if (n >= 90 || state.g !== gid) {
            state.newsRunning = null;
            if (state.g === gid) renderView();
            return;
          }
          setTimeout(tick, 10000);
        };
        setTimeout(tick, 5000);
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
      }
      break;
    }
    case "login":
      state.loginError = "";
      openSheet("login");
      setTimeout(() => $("pw") && $("pw").focus(), 350);
      break;
    case "upd-how":
      enterSettings("overview");
      break;
    case "upd-dismiss":
      dismissUpdate();
      ui.flash = false;
      renderView();
      break;
    case "logout":
      try {
        await api("POST", "/api/logout", {});
      } catch (err) {
        /* 忽略 */
      }
      closeSheet();
      state.me = null;
      state.page = null;
      state.ext = null;
      history.replaceState(null, "", "#/");
      await reboot();
      toast("已退出");
      break;
    case "copy":
      try {
        await navigator.clipboard.writeText(el.dataset.link);
        flashCopied(el, "已复制");
        toast(`${el.dataset.what || "链接"}已复制`);
      } catch (err) { toast("复制失败，请手动复制", true); }
      break;
    case "ga-edit": {
      const gid = el.dataset.g;
      try {
        const r = state.ga[gid] || (state.ga[gid] = await api("GET", `/api/groups/${encodeURIComponent(gid)}/group-admin`));
        state.editing = { kind: "ga", gid, name: el.dataset.name, password_set: !!r.password_set };
        openSheet("edit");
      } catch (err) {
        toast(err.message, true);
      }
      break;
    }
    case "ga-clear": {
      const ed = state.editing || {};
      if (!confirm("清除后，已登录的群管理员将被退出。确定清除？")) break;
      try {
        state.ga[ed.gid] = await api("DELETE", `/api/groups/${encodeURIComponent(ed.gid)}/group-admin/password`);
        closeSheet();
        repaintSheet();
        toast("已清除");
      } catch (err) {
        toast(err.message, true);
      }
      break;
    }
    case "reset-link": {
      if (!confirm("重置后旧链接将失效，群成员需重新发送 /mw 网页获取。确定重置？")) break;
      try {
        await api("POST", `/api/groups/${encodeURIComponent(el.dataset.g)}/token`, {});
        await Promise.all([loadGroups(), loadSettings()]);
        repaintSheet();
        toast("已重置，旧链接已失效");
      } catch (err) {
        toast(err.message, true);
      }
      break;
    }
    case "close":
      closeSheet();
      break;
    case "side-close":
      state.detail = null;
      ui.sideFlash = true;
      syncHash();
      renderView();
      renderSide();
      break;
    // "fb"（资讯/构想反馈）在 actions_news.actNews 里处理
    case "idea": {
      const op = el.dataset.op;
      el.disabled = true;
      try {
        const it = op === "do" ? findIdea(el.dataset.id) : null;
        const body = it && ideaItems(it).length ? { items: ideaPicked(it).map((x) => x.no) } : {};
        await api("POST", `/api/ideas/${encodeURIComponent(el.dataset.id)}/${op}`, body);
        state.ideaMore = null;
        await loadView(true);
        renderTopBits();
        renderView();
        renderSide();
        refreshDetail();
        toast(op === "do" ? "已开始执行" : "已忽略");
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
      }
      break;
    }
    case "ask-review": {
      const gid = el.dataset.gid, card = el.closest && el.closest(".item");
      if (String(state.g) !== String(gid) || !["admin", "group_admin"].includes((state.me || {}).role)) break;
      const op = el.dataset.op;
      if (!confirm(op === "ignore" ? "忽略这条待复核？原话仍保留，不会再自动处理。" : "转为请求？仍需管理员审批，不会直接开工。")) break;
      const buttons = el.closest(".actions").querySelectorAll("button");
      buttons.forEach((b) => { b.disabled = true; });
      try {
        await api("POST", `/api/groups/${encodeURIComponent(gid)}/pending-asks/${encodeURIComponent(el.dataset.id)}`, { action: op, kind: el.dataset.kind || "task" });
        if (String(state.g) !== String(gid)) break;
        await loadView(true);
        renderTopBits();
        await flipRemove(card, $("view"), () => renderView());
        renderSide();
        toast(op === "ignore" ? "已忽略，原话仍保留" : "已转为请求，等待审批");
      } catch (err) {
        buttons.forEach((b) => { b.disabled = false; });
        if (String(state.g) === String(gid)) toast(err.message, true);
      }
      break;
    }
    case "req": {
      const op = el.dataset.op, card = el.closest && el.closest(".item");
      el.disabled = true;
      try {
        await api("POST", `/api/requests/${encodeURIComponent(el.dataset.id)}/${op}`, {});
        await loadView(true);
        renderTopBits();
        await flipRemove(card, $("view"), () => renderView());
        renderSide();
        toast(op === "approve" ? "已批准，将通知发起人" : "已拒绝，将通知发起人");
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
      }
      break;
    }
    case "task-op":
    case "goal-op": {
      const kind = a === "task-op" ? "tasks" : "goals";
      const labels = { pause: "已暂停", resume: "已继续", cancel: "已取消", retry: "已重新排队", redeliver: "已重新发送", confirm: "已确认完成", revoke: "已撤销" };
      if (el.dataset.op === "cancel" && !confirm("确定取消？已完成的部分不会撤回。")) break;
      // 目标完成标准「待确认」的旧勾：data-i 是第几条（算数 / 撤掉）
      const crit = kind === "goals" && el.dataset.i !== undefined ? `/criteria/${encodeURIComponent(el.dataset.i)}` : "";
      el.disabled = true;
      try {
        const r = await api("POST", `/api/${kind}/${encodeURIComponent(el.dataset.id)}${crit}/${el.dataset.op}`, {});
        if (kind === "tasks" && r && r.id) state.tasks[r.id] = r;
        if (r && r.redeliver_warning) {
          toast(r.redeliver_warning, true);
          el.disabled = false;
          await loadView(true);
          refreshDetail();
          break;
        }
        await loadView(true);
        renderTopBits();
        renderView();
        refreshDetail();
        toast(labels[el.dataset.op] || "已完成");
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
      }
      break;
    }
    case "mention-member": {
      if (!confirm(`让 MaiBot 在合适时机向 ${el.dataset.name || "该成员"} 提及？不会透露原因。`)) break;
      el.disabled = true;
      try {
        const r = await api("POST", `/api/news/${encodeURIComponent(el.dataset.id)}/mention-to-member`, {});
        if (r && r.ok === false) throw new Error("内容涉及个人画像，未发送");
        el.textContent = "已交给 MaiBot";
        toast("已交给 MaiBot，将在合适时机提及");
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
      }
      break;
    }
    case "rate-open": {
      const id = el.dataset.id;
      const vw = gview() || {};
      const it = [...(vw.news || []).flatMap((b) => b.items || []), ...(vw.guides || [])].find((x) => String(x.id) === String(id));
      const mine = myRates()[id] || null;
      state.editing = { kind: "rate", id, title: (it && it.title) || "", reasons: mine ? [...(mine.reasons || [])] : [], note: mine ? mine.note || "" : "", had: !!mine };
      openSheet("edit");
      break;
    }
    case "rate-chip": {
      const ed = state.editing || {};
      if (ed.kind !== "rate") break;
      const k = el.dataset.k;
      const set = new Set(ed.reasons || []);
      if (set.has(k)) set.delete(k);
      else set.add(k);
      ed.reasons = [...set];
      el.setAttribute("aria-pressed", String(set.has(k)));
      break;
    }
    case "rate-clear": {
      const ed = state.editing || {};
      if (ed.kind !== "rate") break;
      el.disabled = true;
      try {
        await api("POST", `/api/news/${encodeURIComponent(ed.id)}/rate`, { client: clientId(), reasons: [], note: "" });
        const all = myRates();
        delete all[ed.id];
        localStorage.setItem(RATE_KEY, JSON.stringify(all));
        closeSheet();
        await loadView(true);
        renderView();
        toast("已撤回");
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
      }
      break;
    }
    case "news-tab":
      state.newsTab = el.dataset.t;
      ui.flash = true;
      renderView();
      break;
    case "rej-toggle":
      state.openRejected = state.openRejected === +el.dataset.id ? null : +el.dataset.id;
      renderView();
      reveal(`.rej-head[data-id="${el.dataset.id}"]`);
      break;
    case "block-domain":
    case "unblock-domain": {
      // 屏蔽名单按群（docs/18 第一步）：资讯页按当前群，设置页的行自带群号
      const blocked = a === "block-domain";
      const bg = el.dataset.g || state.g;
      if (!bg) break;
      if (blocked && !confirm(`在本群屏蔽 ${el.dataset.domain}？可在设置中解除。`)) break;
      try {
        await api("POST", `/api/groups/${encodeURIComponent(bg)}/feeds/domains`, { domain: el.dataset.domain, blocked });
        if (state.settings) await loadSettings();
        repaintSheet();
        toast(blocked ? `已屏蔽 ${el.dataset.domain}` : `已解除 ${el.dataset.domain}`);
      } catch (err) {
        toast(err.message, true);
      }
      break;
    }
    case "filter":
      state.filter = el.dataset.f;
      renderView();
      break;
    case "task":
      openDetail("task", el.dataset.id);
      break;
    case "goal":
      openDetail("goal", el.dataset.id);
      break;
    case "idea-open":
      state.ideaMore = null;
      openDetail("idea", el.dataset.id);
      break;
    case "idea-more":
      state.ideaMore = state.ideaMore === Number(el.dataset.id) ? null : Number(el.dataset.id);
      refreshDetail();
      if (state.ideaMore != null && !calm()) document.querySelector(".idea-menu")?.classList.add("pop-in");
      break;
    case "idea-item": {
      const id = Number(el.dataset.id);
      const no = Number(el.dataset.no);
      state.ideaOff = state.ideaOff || {};
      const off = state.ideaOff[id] || [];
      state.ideaOff[id] = off.includes(no) ? off.filter((x) => x !== no) : off.concat(no);
      refreshDetail();
      break;
    }
    case "idea-copy": {
      const it = findIdea(el.dataset.id);
      if (!it) break;
      try {
        await navigator.clipboard.writeText(ideaAsk(it));
        flashCopied(el, "已复制");
        toast("已复制，请在群内 @MaiBot 并粘贴");
      } catch (err) { toast("复制失败，请手动复制", true); }
      break;
    }
    case "handoff":
      openHandoff(el.dataset.kind, el.dataset.id);
      break;
    case "handoff-copy":
      handoffCopy(el);
      break;
    case "handoff-download":
      handoffDownload();
      break;
    case "verdict": {
      const id = el.dataset.id;
      const t = ((gview() || {}).topic_log || []).find((x) => String(x.id) === String(id));
      const value = t && t.verdict === el.dataset.v ? null : el.dataset.v;
      try {
        await api("POST", `/api/topics/${encodeURIComponent(id)}/verdict`, { value });
        if (t) t.verdict = value;
        renderView();
        if (value) toast("已记录");
      } catch (err) {
        toast(err.message, true);
      }
      break;
    }
    case "pf-lock": {
      const en = findEntry(el.dataset.id);
      if (!en) break;
      try {
        await api("PATCH", `/api/profile/${en.id}`, { locked: !en.locked });
        en.locked = !en.locked;
        renderView();
        toast(en.locked ? "已锁定" : "已解锁");
      } catch (err) {
        toast(err.message, true);
      }
      break;
    }
    case "pf-del": {
      const en = findEntry(el.dataset.id);
      if (!en) break;
      try {
        await api("DELETE", `/api/profile/${en.id}`);
        patchView((vw) => vw.profile.forEach((s) => (s.entries = (s.entries || []).filter((x) => x.id !== en.id))));
        renderView();
        toast("已删除，不会再自动添加");
      } catch (err) {
        toast(err.message, true);
      }
      break;
    }
    case "pf-edit": {
      const en = findEntry(el.dataset.id);
      if (!en) break;
      state.editing = { kind: "entry", id: en.id, text: en.text };
      openSheet("edit");
      setTimeout(() => $("ed-text") && $("ed-text").focus(), 350);
      break;
    }
    case "pf-add":
      state.editing = { kind: "entry", cat: el.dataset.cat, text: "" };
      openSheet("edit");
      setTimeout(() => $("ed-text") && $("ed-text").focus(), 350);
      break;
    case "focus-add":
      state.editing = { kind: "focus" };
      openSheet("edit");
      setTimeout(() => $("ed-text") && $("ed-text").focus(), 350);
      break;
    case "focus-rm": {
      const uid = el.dataset.uid;
      try {
        await api("POST", `/api/groups/${encodeURIComponent(g.id)}/focus`, { user_id: uid, action: "remove" });
        patchView((vw) => (vw.focus = (vw.focus || []).filter((p) => p.user_id !== uid)));
        renderView();
        toast("已取消关注，画像已删除");
      } catch (err) {
        toast(err.message, true);
      }
      break;
    }
  }
}
