// MaiWork 网页 · 所有按钮的点击动作（data-act）。
import { $, admin, state, ui } from "./state.js";
import { reveal, toast } from "./util.js";
import { api, grp, gview } from "./api.js";
import { FB_KEY, RATE_KEY, clientId, myFb, myRates } from "./pages/news.js";
import { findIdea, ideaAsk, ideaItems, ideaPicked } from "./pages/ideas.js";
import { refreshDetail } from "./detail.js";
import { handoffCopy, handoffDownload, openHandoff } from "./handoff.js";
import { checkedProviders, headerRow, loadExt, pickedTool, readHeaders } from "./settings/ext.js";
import { actNews } from "./actions_news.js";
import { actCtx } from "./pages/groupctx.js";
import { actControls } from "./pages/groupcontrols.js";
import { actAgents } from "./settings/agents.js";
import { actModels } from "./settings/models.js";
import { actJev } from "./settings/jev.js";
import { chipAdd, rowsAdd } from "./settings/rules.js";
import { loadUsage, loadUsageDay } from "./settings/usage.js";
import { avatarSaved } from "./settings/identity.js";
import { loadLogs, loadLogSummary } from "./settings/logs.js";
import { closeSheet, openDetail, openSheet, repaintSheet } from "./sheet.js";
import { chatPoll, enterChat, loadChat, loadChats, paintChat } from "./chat.js";
import { renderSide, renderView } from "./render.js";
import { dismissUpdate } from "./update.js";
import { enterSettings, go, loadGroups, loadSettings, loadView, reboot, renderTopBits, syncHash } from "./router.js";

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
  if (await actCtx(a, el)) return;
  if (await actControls(a, el)) return;
  if (await actAgents(a, el)) return;
  if (await actModels(a, el)) return;
  if (await actJev(a, el)) return;
  const g = grp();
  switch (a) {
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
        toast("等它说完再压缩");
        break;
      }
      el.disabled = true;
      const label = el.textContent;
      el.textContent = "压缩中…";
      try {
        await api("POST", `/api/chat/${encodeURIComponent(C.id)}/compact`);
        await loadChat(C.id);
        paintChat(true);
        toast("压缩好了");
      } catch (err) {
        toast(err.status === 409 ? "等它说完再压缩" : err.message, true);
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
      el.disabled = true;
      try {
        const r = await api("POST", `/api/chat/pending/${encodeURIComponent(el.dataset.id)}`, { approve: el.dataset.ok === "1" });
        toast(el.dataset.ok === "1" ? (r.status === "done" ? "已去做了" : `没做成：${r.result || ""}`) : "好，不做了", r.status === "failed");
        await loadChat(state.chatId, true);
        if (state.chat) state.chat.running = true;
        paintChat(true);
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
    case "ext-new":
    case "ext-edit": {
      state.extEdit = { kind: el.dataset.kind, name: el.dataset.name || null, data: null };
      repaintSheet();
      if (el.dataset.kind === "skill" && el.dataset.name) {
        try {
          state.extEdit.data = await api("GET", `/api/extensions/skills/${encodeURIComponent(el.dataset.name)}`);
        } catch (err) {
          state.extEdit = null;
          toast(err.message, true);
        }
        repaintSheet();
      }
      const first = document.querySelector(".ext-form input:not([readonly]), .ext-form textarea");
      if (first) first.focus({ preventScroll: false });
      break;
    }
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
      if (!list.querySelector(".ci")) list.innerHTML = `<span class="ci-empty">还没有</span>`;
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
      if (!confirm("清空这个密钥？")) break;
      try {
        state.rules = await api("PUT", "/api/settings/config", { [el.dataset.f]: null });
        await loadSettings();
        repaintSheet();
        toast("清掉了");
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
        toast("要 http:// 或 https:// 开头的网址", true);
        break;
      }
      try {
        await avatarSaved(await api("POST", "/api/settings/avatar", { url }));
        toast("头像换好了");
      } catch (err) {
        toast(err.message, true);
      }
      break;
    }
    case "avatar-reset":
      try {
        await avatarSaved(await api("DELETE", "/api/settings/avatar"));
        toast("已恢复同步 MaiBot");
      } catch (err) {
        toast(err.message, true);
      }
      break;
    case "cfg-goto":
      state.cfgSec = el.dataset.s || "";
      await enterSettings("rules");
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
      const ask = el.dataset.auto === "1" ? "删掉这个来源？以后不再推荐" : "删掉这个 RSS？";
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
        toast("开始找了，几分钟后出现在这里");
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
            if (st.state === "done") toast(st.reason || "新资讯到了");
            else if (st.state === "skipped") toast(`这次没找到新的：${st.reason || "没合适的"}`);
            else toast(`没找成：${st.reason || "出错了"}`, true);
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
    case "mcp-paste":
      state.extEdit = { kind: "mcp-paste", name: null, data: null };
      repaintSheet();
      setTimeout(() => $("mp-text") && $("mp-text").focus(), 50);
      break;
    case "ext-cancel":
      state.extEdit = null;
      repaintSheet();
      break;
    case "hdr-add":
      $("x-hdrs").insertAdjacentHTML("beforeend", headerRow("", false));
      break;
    case "hdr-del": {
      const row = el.closest(".hdr-row");
      if (row.dataset.old === "1") {
        row.dataset.removed = row.dataset.removed === "1" ? "0" : "1";
        row.classList.toggle("removed", row.dataset.removed === "1");
      } else row.remove();
      break;
    }
    case "mcp-test": {
      const out = $("x-check");
      const url = $("x-url").value.trim();
      if (!/^https:\/\/\S+$/.test(url)) {
        out.textContent = "地址要以 https:// 开头";
        out.style.color = "var(--red)";
        break;
      }
      const f = $("mcp-form");
      const { headers } = readHeaders();
      el.disabled = true;
      out.style.color = "";
      out.textContent = "连接中…";
      try {
        const r = await api("POST", "/api/extensions/mcp/test", { url, headers, name: f.dataset.name || undefined });
        out.textContent = r.ok ? `已连上，${(r.tools || []).length} 个工具` : `没连上：${r.error || "原因不明"}`;
        out.style.color = r.ok ? "" : "var(--red)";
      } catch (err) {
        out.textContent = err.message;
        out.style.color = "var(--red)";
      } finally {
        el.disabled = false;
      }
      break;
    }
    case "mcp-toggle": {
      el.disabled = true;
      try {
        await api("POST", `/api/extensions/mcp/${encodeURIComponent(el.dataset.name)}/toggle`, { enabled: el.dataset.on === "1" });
        await loadExt();
        repaintSheet();
        toast(el.dataset.on === "1" ? "已开启" : "已关闭");
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
      }
      break;
    }
    case "search-edit":
      state.searchEdit = true;
      repaintSheet();
      break;
    case "search-cancel":
      state.searchEdit = false;
      repaintSheet();
      break;
    case "search-save": {
      const { mcp, tool } = pickedTool("sx-tool");
      const ex = pickedTool("sx-extract");
      el.disabled = true;
      try {
        state.extSearch = await api("PUT", "/api/extensions/search", {
          mcp,
          tool,
          extract_mcp: ex.mcp,
          extract_tool: ex.tool,
          fallback: checkedProviders("sx-fb", mcp),
          broad: checkedProviders("sx-br", mcp),
        });
        state.searchEdit = false;
        await loadExt();
        if (state.settings) await loadSettings();
        repaintSheet();
        toast(`搜索已换成 ${mcp}`);
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
      }
      break;
    }
    case "search-off": {
      if (!confirm("关闭联网搜索？资讯会停更")) break;
      try {
        await api("DELETE", "/api/extensions/search");
        state.searchEdit = false;
        await loadExt();
        if (state.settings) await loadSettings();
        repaintSheet();
        toast("已关闭联网搜索");
      } catch (err) {
        toast(err.message, true);
      }
      break;
    }
    case "mcp-del":
    case "skill-del": {
      const isMcp = a === "mcp-del";
      if (!confirm(`删掉「${el.dataset.name}」？${isMcp ? "密钥一起删" : "整个 skill 一起删"}`)) break;
      try {
        await api("DELETE", `/api/extensions/${isMcp ? "mcp" : "skills"}/${encodeURIComponent(el.dataset.name)}`);
        state.extEdit = null;
        await loadExt();
        repaintSheet();
        toast("删掉了");
      } catch (err) {
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
        toast(`${el.dataset.what || "链接"}复制好了`);
      } catch (err) {
        toast("复制失败，请手动复制", true);
      }
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
      if (!confirm("清除后，已登录的人会被退出。确定？")) break;
      try {
        state.ga[ed.gid] = await api("DELETE", `/api/groups/${encodeURIComponent(ed.gid)}/group-admin/password`);
        closeSheet();
        repaintSheet();
        toast("清掉了");
      } catch (err) {
        toast(err.message, true);
      }
      break;
    }
    case "reset-link": {
      if (!confirm("换新后旧链接失效，群友要重新发 /mw 网页。确定？")) break;
      try {
        await api("POST", `/api/groups/${encodeURIComponent(el.dataset.g)}/token`, {});
        await Promise.all([loadGroups(), loadSettings()]);
        repaintSheet();
        toast("已换新，旧链接失效");
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
      syncHash();
      renderView();
      renderSide();
      break;
    case "fb": {
      const { kind, id, v } = el.dataset;
      const all = myFb();
      const key = `${kind}:${id}`;
      const prev = all[key] || null;
      const next = prev === v ? null : v;
      try {
        const r = await api("POST", `/api/${kind}/${encodeURIComponent(id)}/feedback`, { value: next, prev });
        if (next) all[key] = next;
        else delete all[key];
        localStorage.setItem(FB_KEY, JSON.stringify(all));
        patchView((vw) => {
          const list = kind === "news" ? (vw.news || []).flatMap((b) => b.items || []) : vw.ideas || [];
          const it = list.find((x) => String(x.id) === String(id));
          const nf = r && (r.feedback || (r.up !== undefined ? { up: r.up, down: r.down } : null));
          if (it && nf) it.feedback = nf;
        });
        renderView();
        refreshDetail();
        if (next) toast(next === "up" ? "记下了，多找这类" : "记下了，少找这类");
      } catch (err) {
        toast(err.message, true);
      }
      break;
    }
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
        toast(op === "do" ? "已开工" : "不会再提了");
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
      }
      break;
    }
    case "req": {
      const op = el.dataset.op;
      el.disabled = true;
      try {
        await api("POST", `/api/requests/${encodeURIComponent(el.dataset.id)}/${op}`, {});
        await loadView(true);
        renderTopBits();
        renderView();
        renderSide();
        toast(op === "approve" ? "已批准，会通知发起人" : "已拒绝，会通知发起人");
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
      }
      break;
    }
    case "task-op":
    case "goal-op": {
      const kind = a === "task-op" ? "tasks" : "goals";
      const labels = { pause: "已暂停", resume: "已继续", cancel: a === "task-op" ? "已取消" : "不做了", retry: "已重新排队", redeliver: "已重发" };
      if (el.dataset.op === "cancel" && !confirm("确定取消？已做的不会撤回")) break;
      el.disabled = true;
      try {
        const r = await api("POST", `/api/${kind}/${encodeURIComponent(el.dataset.id)}/${el.dataset.op}`, {});
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
        toast(labels[el.dataset.op] || "好了");
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
      }
      break;
    }
    case "mention-member": {
      if (!confirm(`让 MaiBot 找机会跟 ${el.dataset.name || "ta"} 提一嘴？不会透露原因`)) break;
      el.disabled = true;
      try {
        const r = await api("POST", `/api/news/${encodeURIComponent(el.dataset.id)}/mention-to-member`, {});
        if (r && r.ok === false) throw new Error("内容涉及个人画像，没发出");
        el.textContent = "交给 MaiBot 了";
        toast("交给 MaiBot 了，它会找机会提");
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
      }
      break;
    }
    case "mcp-reload": {
      el.disabled = true;
      try {
        const r = await api("POST", `/api/extensions/mcp/${encodeURIComponent(el.dataset.name)}/reload`, {});
        await Promise.all([loadSettings(), state.page === "settings" ? loadExt() : null]);
        repaintSheet();
        toast(r && r.ok ? `已重连，${r.tools || 0} 个工具` : `没连上：${(r && r.error) || "原因不明"}`, !(r && r.ok));
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
        toast("撤回了");
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
      if (blocked && !confirm(`本群不再看 ${el.dataset.domain}？可在设置里解除`)) break;
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
        toast("已复制，去群里 @MaiBot 粘贴");
      } catch (err) {
        toast("复制失败，请手动复制", true);
      }
      break;
    }
    case "handoff":
      openHandoff(el.dataset.kind, el.dataset.id);
      break;
    case "handoff-copy":
      handoffCopy();
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
        if (value) toast("记下了，会用来改进");
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
        toast("已删除，不会再加回");
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
