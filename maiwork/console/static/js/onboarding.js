// MaiWork 网页 · 首次安装引导。
import { $, admin, state } from "./state.js";
import { SVG, calm, esc, ico, toast } from "./util.js";
import { api, gname } from "./api.js";
import { ROWS_COLS, chipEditor, rowsEditor } from "./settings/rules.js";
import { AV_SRC } from "./settings/identity.js";
import { PROTOCOLS, loadModels } from "./settings/models.js";
import { loadAgents } from "./settings/agents.js";
import { render } from "./render.js";
import { applyHash, loadGroups, loadSettings, loadView } from "./router.js";

// ───────────── 首次安装引导 ─────────────
// 全屏一张卡片，一步一屏：打招呼 → 模型（端点 + 主模型 / 专岗模型）→ 服务的群 → 可选密钥 → 联网搜索 → 管理员 → 头像 → 完成。
// 每一步「下一步」时保存这一步；任何一步都能「跳过引导」。动画只动 transform / opacity，
// 换步时正在播的动画会被立刻收尾（连点不会卡住、不会叠在一起）。
export const ONB_STEPS = [
  { id: "hello", name: "开始" },
  { id: "models", name: "模型" },
  { id: "groups", name: "群" },
  { id: "keys", name: "可选" },
  { id: "search", name: "搜索" },
  { id: "admins", name: "管理员" },
  { id: "look", name: "头像" },
  { id: "done", name: "完成" },
];
export const onb = { open: false, i: 0, busy: false, cfg: null, models: null, info: null, done: {}, anims: [], presets: null, sxPicked: null };
const EASE_SPRING = "cubic-bezier(0.34, 1.4, 0.64, 1)";
document.addEventListener("change", (event) => {
  if (event.target && event.target.id === "onb-proto" && $("onb-url")) $("onb-url").placeholder = (PROTOCOLS[event.target.value] || PROTOCOLS.openai).ph;
});
// 引导里用的端点：模型库里的第一个；还没有就是个空的「default」
const onbEndpoint = () => (((state.mdl && state.mdl.endpoints) || [])[0]) || { id: "default", name: "默认端点", protocol: "openai" };

const onbField = (key) => {
  for (const s of (onb.cfg && onb.cfg.sections) || []) for (const f of s.fields) if (f.key === key) return f;
  return null;
};

export function onbPane(id) {
  const m = (state.settings && state.settings.models) || {};
  const bot = (state.me && state.me.bot) || {};
  if (id === "hello")
    return `
      <div class="onb-hero"><img class="onb-avatar" src="${esc(bot.avatar || "/static/assets/logo.png")}" alt="" /><span class="onb-spark">${ico("sparkles", "ico")}</span></div>
      <h1 class="onb-title">欢迎用 MaiWork</h1>
      <p class="onb-lead">花两分钟配好几样东西就能开始</p>
      <ul class="onb-list">
        <li>${ico("robot")}<div><b>模型</b><span>必填：连一个端点，挑主模型</span></div></li>
        <li>${ico("speech")}<div><b>服务的群</b><span>MaiWork 在哪些群工作</span></div></li>
        <li>${ico("lock")}<div><b>管理员和密钥</b><span>可以之后再填</span></div></li>
      </ul>`;
  if (id === "models") {
    // 2026-10：端点 + 模型库。引导里只加一个端点、挑主模型和专岗用的模型；细项去「模型」页调
    const ep = onbEndpoint();
    const list = onb.models || ep.available || [];
    const byId = (mid) => { const x = ((state.mdl && state.mdl.models) || []).find((y) => y.id === mid); return x ? x.model : ""; };
    const prof = (k) => (((state.agents && state.agents.profiles) || []).find((p) => p.kind === k) || {});
    const curMain = byId(prof("main").model), curWork = byId(prof("task").model);
    // 下拉 + 手填二合一（docs/13 A03）：列表接口 404 的端点也能直接填模型 ID
    const sel = (sid, v, empty) =>
      `<input id="${sid}" list="onb-model-list" spellcheck="false" autocomplete="off" value="${esc(v || "")}" placeholder="${esc(empty || (list.length ? "从列表选，或手填模型 ID" : "手填模型 ID，或先测试连接拉列表"))}" />`;
    const proto = ep.protocol || "openai";
    return `
      <div class="onb-step-ico">${ico("robot")}</div>
      <h1 class="onb-title">连上模型</h1>
      <p class="onb-lead">先连一个端点，再挑主模型。更多端点、思考强度这些，之后在「模型」和「专岗」页里调</p>
      <div class="login onb-form">
        <label for="onb-proto">接口格式</label>
        <select id="onb-proto">${Object.entries(PROTOCOLS).map(([k, v]) => `<option value="${k}"${k === proto ? " selected" : ""}>${esc(v.name)}</option>`).join("")}</select>
        <label for="onb-url">端点地址</label>
        <input id="onb-url" type="url" inputmode="url" spellcheck="false" value="${esc(ep.base_url || "")}" placeholder="${esc((PROTOCOLS[proto] || PROTOCOLS.openai).ph)}" />
        <label for="onb-key">API 密钥</label>
        <input id="onb-key" type="password" autocomplete="new-password" placeholder="${ep.key_set ? "已填写 · 留空就不改" : "粘贴密钥"}" />
        <div class="onb-test"><button class="btn" type="button" data-act="onb-test">测试连接</button><span class="onb-status" id="onb-status">${list.length ? `<i class="onb-ok">${SVG.check}</i>找到 ${list.length} 个模型` : ""}</span></div>
        <div class="onb-picks">
          <datalist id="onb-model-list">${list.map((x) => `<option value="${esc(x)}"></option>`).join("")}</datalist>
          <label for="onb-main">主模型 <span class="fine-inline">负责想和验收，选聪明的</span></label>
          ${sel("onb-main", onb.pickMain || curMain)}
          <label for="onb-worker">各专岗用的模型 <span class="fine-inline">负责动手，选便宜耐用的</span></label>
          ${sel("onb-worker", onb.pickWork || (curWork && curWork !== curMain ? curWork : ""), "留空 = 跟主模型一样")}
          <p class="fine" id="onb-verify-note">保存时会验证所选模型：发一句很短的问话、做一次空工具测试（共 2~6 次小请求，会用掉一点点 token）</p>
        </div>
      </div>`;
  }
  if (id === "groups") {
    const f = onbField("groups.serve");
    return `
      <div class="onb-step-ico">${ico("speech")}</div>
      <h1 class="onb-title">服务哪些群</h1>
      <p class="onb-lead">MaiWork 只在这些群里工作</p>
      <div class="login onb-form">${f ? rowsEditor("onb-serve", ROWS_COLS.serve_groups, f.value) : `<p class="fine">读不到配置，先跳过这步。</p>`}</div>
      `;
  }
  if (id === "keys") {
    const jev = onbField("jev.api_key") || {};
    return `
      <div class="onb-step-ico">${ico("lock")}</div>
      <h1 class="onb-title">可选：快速判断</h1>
      <p class="onb-lead">不填也能用，填了反应更快</p>
      <div class="login onb-form">
        <label for="onb-jev">Jev 密钥 </label>
        <input id="onb-jev" type="password" autocomplete="new-password" placeholder="${jev.set ? "已设置 · 留空就不改" : "没设置 · 可以不填"}" />
      </div>
      `;
  }
  if (id === "search") return onbSearchPane();
  if (id === "admins") {
    const f = onbField("approval.admins");
    return `
      <div class="onb-step-ico">${ico("bell")}</div>
      <h1 class="onb-title">谁是管理员</h1>
      <p class="onb-lead">群友派的活要管理员批准才开工</p>
      <div class="login onb-form">${f ? chipEditor("onb-admins", "accounts", f.value) : `<p class="fine">读不到配置，先跳过这步。</p>`}</div>
      ${onbGaBlock()}`;
  }
  if (id === "look") {
    const a = state.avatarCfg;
    const src = (a && a.url) || bot.avatar || "/static/assets/logo.png";
    return `
      <div class="onb-hero"><img class="onb-avatar" id="onb-av" src="${esc(src)}" alt="" onerror="this.onerror=null;this.src='/static/assets/logo.png'" /></div>
      <h1 class="onb-title">可选：换个头像</h1>
      <p class="onb-lead">默认和 MaiBot 一样，也可以换一张</p>
      <div class="onb-av-btns">
        <label class="btn file-btn">上传图片<input type="file" id="avatar-file" accept="image/png,image/jpeg,image/webp,image/gif" hidden /></label>
        <button type="button" class="btn" data-act="avatar-url">用网址</button>
        ${a && a.source === "custom" ? `<button type="button" class="btn ghost" data-act="avatar-reset">恢复同步</button>` : ""}
      </div>
      <p class="fine">${a ? esc(AV_SRC[a.source] || "") : ""}${a ? " · " : ""}png / jpg / webp / gif，最大 2MB。</p>`;
  }
  return onbDonePane();
}

// 完成页（docs/13 A08/G01/G02）：按后端给的能力清单逐项说「能用 / 受限 / 在等 / 没开」；
// 必要条件（模型、群）缺了，大标题不说「准备好了」，只说「先存下」，并给可点的「去补」。
const MARK = { ok: "ok", warn: "warn", wait: "wait", off: "" };
function onbDonePane() {
  const info = onb.info || {};
  const items = info.items || [];
  const usable = !!info.usable;
  const miss = (info.missing || []).map((k) => ({ models: "模型", groups: "服务的群" })[k] || k);
  const row = (it, i) => {
    const goto = it.state !== "ok" && it.step && it.step !== "done" ? `<button type="button" class="link-btn" data-act="onb-goto" data-step="${esc(it.step)}">去补</button>` : "";
    const copy = it.copy ? `<button type="button" class="link-btn" data-act="copy" data-link="${esc(it.copy)}" data-what="链接">复制链接</button>` : "";
    return `<li class="onb-sum" style="--i:${i}"><span class="onb-mark ${MARK[it.state] || ""}">${it.state === "ok" ? SVG.check : it.state === "wait" ? "…" : it.state === "warn" ? "!" : ""}</span><div><b>${esc(it.title)}${goto}${copy}</b><span>${esc(it.text)}</span></div></li>`;
  };
  return `
    ${usable ? `<div class="onb-done-mark"><svg viewBox="0 0 52 52"><circle cx="26" cy="26" r="24"/><path d="M15 27l7.5 7.5L37.5 19"/></svg></div>` : `<div class="onb-step-ico">${ico("floppy")}</div>`}
    <h1 class="onb-title">${usable ? "可以开始用了" : "先存下了，还差一点"}</h1>
    <p class="onb-lead">${usable ? "下面是现在各项能做到哪一步；标了感叹号的能用但有限制，随时可以在「设置」里补" : `还没配好：${esc(miss.join("、"))}。MaiWork 要这些才能干活，点「去补」接着填`}</p>
    <ul class="onb-list onb-summary">${items.map(row).join("")}</ul>`;
}

// 引导「联网搜索」一步：挑要用的搜索服务（预设，后端 /api/extensions/presets）。
// 勾上的第一家当主搜索，其余当备用；免费的密钥可填可不填，要密钥的给官网链接。
function onbSearchPane() {
  const list = onb.presets || [];
  if (!onb.sxPicked) {
    const on = list.filter((p) => p.entry && p.enabled).map((p) => p.id);
    onb.sxPicked = on.length ? on : list.some((p) => p.id === "keenable") ? ["keenable"] : [];
  }
  const rows = list
    .map((p) => {
      const on = onb.sxPicked.includes(p.id);
      const ph = p.key_set ? "已填 · 留空就不改" : p.free ? "可以不填 · 不填就用免费额度" : "粘贴 API 密钥";
      return `
      <div class="onb-sx${on ? " on" : ""}" data-id="${esc(p.id)}">
        <label class="onb-sx-head">
          <input type="checkbox" class="onb-sx-chk" value="${esc(p.id)}" ${on ? "checked" : ""} data-act="onb-sx-toggle" />
          <img class="onb-sx-logo" src="${esc(p.logo)}" alt="" />
          <span class="onb-sx-name"><b>${esc(p.label)}</b><span>${esc(p.free_note || "")}</span></span>
          ${p.free ? `<span class="ntag ok">免密钥可用</span>` : `<span class="ntag warn">要密钥</span>`}
        </label>
        <div class="onb-sx-key login">
          <input id="onb-sx-key-${esc(p.id)}" type="password" autocomplete="new-password" spellcheck="false" placeholder="${esc(ph)}" />
          <a href="${esc(p.key_page_url)}" target="_blank" rel="noopener noreferrer">${p.free ? "想要更高额度？去官网拿密钥 ↗" : `还没有密钥？去 ${esc(p.label)} 官网拿 ↗`}</a>
        </div>
      </div>`;
    })
    .join("");
  return `
    <div class="onb-step-ico">${ico("magnifier")}</div>
    <h1 class="onb-title">联网搜索</h1>
    <p class="onb-lead">MaiWork 找资讯、干活要上网搜。勾上要用的，第一个当主搜索，其余当备用</p>
    <div class="onb-sx-list">${list.length ? rows : `<p class="fine">读不到搜索服务清单，先跳过这步，之后在「设置 → 扩展」里打开。</p>`}</div>`;
}

// 引导「管理员」一步里的各群群管理员（可选）：网页密码 + 群里能批准的人
const onbServed = () => {
  const f = onbField("groups.serve");
  // 群 ID：QQ 去掉 qq: 剩数字；Telegram 去掉 telegram:/tg: 剩群 ID（如 -1001234567890）；QQ 官方去掉 qqbot: 剩 openid
  return ((f && f.value) || [])
    .map((r) => String((r && r.group) || "").trim().replace(/^(qq|telegram|tg|qqbot):/i, ""))
    .filter((x) => /^-?[0-9A-Za-z:=_|.]{1,64}$/.test(x));
};
function onbGaBlock() {
  const gids = onbServed().filter((gid) => onb.ga && onb.ga[gid]);
  if (!gids.length) return "";
  return `
    <div class="onb-ga-head"><b>各群的群管理员</b><span>可选 · 只能管自己的群</span></div>
    ${gids
      .map((gid) => {
        const x = onb.ga[gid];
        const g = state.groups.find((y) => y.id === gid);
        return `
      <div class="onb-ga login">
        <div class="onb-ga-name">${esc(gname(g) || `群 ${gid}`)}</div>
        <label for="onb-ga-pw-${gid}">网页密码</label>
        <input id="onb-ga-pw-${gid}" type="password" autocomplete="new-password" placeholder="${x.password_set ? "已设置 · 留空就不改" : "至少 8 位 · 可以不填"}" />
        <label>群里能批准的人</label>
        ${chipEditor(`onb-ga-${gid}`, "accounts", x.accounts || [])}
      </div>`;
      })
      .join("")}`;
}

function onbShell() {
  const n = ONB_STEPS.length - 1;
  return `
    <div class="onb-scrim"></div>
    <div class="onb-card" role="dialog" aria-modal="true" aria-label="MaiWork 首次引导">
      <header class="onb-head">
        <div class="onb-dots">${ONB_STEPS.map((s, i) => `<span class="onb-dot" data-i="${i}" title="${s.name}"></span>`).join("")}</div>
        <button class="btn ghost small onb-skip" type="button" data-act="onb-skip">跳过引导</button>
      </header>
      <div class="onb-bar"><i style="transform:scaleX(${onb.i / n})"></i></div>
      <div class="onb-stage" id="onb-stage"></div>
      <p class="err onb-err" id="onb-err" hidden></p>
      <footer class="onb-foot" id="onb-foot"></footer>
    </div>`;
}

function onbFoot() {
  const id = ONB_STEPS[onb.i].id;
  const back = onb.i > 0 && id !== "done" ? `<button class="btn ghost" type="button" data-act="onb-back">上一步</button>` : `<span></span>`;
  const later = ["keys", "search", "admins", "groups", "look"].includes(id) ? `<button class="btn" type="button" data-act="onb-later">这步先不填</button>` : id === "models" ? `<button class="btn" type="button" data-act="onb-later">稍后再配</button>` : "";
  const usable = !!(onb.info && onb.info.usable);
  const next = id === "hello" ? "开始配置" : id === "done" ? (usable ? "进入 MaiWork" : "先存下，稍后继续") : "保存，下一步";
  return `${back}<div class="onb-foot-r">${later}<button class="btn primary" type="button" data-act="onb-next">${next}</button></div>`;
}

// 收掉还在播的动画：连点「下一步」时直接跳到终点，不排队
function onbSettle() {
  for (const a of onb.anims) {
    try {
      a.finish();
    } catch (_) {}
  }
  onb.anims = [];
  document.querySelectorAll(".onb-pane.leaving").forEach((p) => p.remove());
}
const onbAnim = (el, frames, opts) => {
  if (!el || !el.animate) return null;
  const a = el.animate(frames, Object.assign({ fill: "both" }, opts));
  onb.anims.push(a);
  return a;
};

function onbPaint(dir) {
  const stage = $("onb-stage");
  const root = $("onb");
  if (!stage || !root) return;
  onbSettle();
  const quiet = calm();
  const n = ONB_STEPS.length - 1;
  // 进度条和圆点
  const bar = root.querySelector(".onb-bar i");
  bar.style.transform = `scaleX(${onb.i / n})`;
  root.querySelectorAll(".onb-dot").forEach((d, i) => {
    d.classList.toggle("on", i === onb.i);
    d.classList.toggle("past", i < onb.i);
  });
  $("onb-err").hidden = true;
  $("onb-foot").innerHTML = onbFoot();
  const old = stage.querySelector(".onb-pane:not(.leaving)");
  const pane = document.createElement("div");
  pane.className = `onb-pane onb-${ONB_STEPS[onb.i].id}`;
  pane.innerHTML = onbPane(ONB_STEPS[onb.i].id);
  stage.appendChild(pane);
  stage.scrollTop = 0;
  const dx = dir === 0 ? 0 : dir > 0 ? 36 : -36;
  if (old) {
    old.classList.add("leaving");
    onbAnim(old, quiet ? [{ opacity: 1 }, { opacity: 0 }] : [{ opacity: 1, transform: "translateX(0)" }, { opacity: 0, transform: `translateX(${-dx * 0.6}px)` }], {
      duration: quiet ? 140 : 200,
      easing: "cubic-bezier(0.4, 0, 1, 1)",
    }).finished.then(() => old.remove(), () => old.remove());
  }
  // 新一屏：整体从运动方向滑进来，里面的块依次浮起（最多错开 6 块）
  const delay = old ? (quiet ? 60 : 90) : 0;
  onbAnim(pane, quiet ? [{ opacity: 0 }, { opacity: 1 }] : [{ opacity: 0, transform: `translateX(${dx}px)` }, { opacity: 1, transform: "translateX(0)" }], {
    duration: quiet ? 180 : 420,
    delay,
    easing: "cubic-bezier(0.32, 0.72, 0, 1)",
  });
  if (!quiet)
    [...pane.children].slice(0, 6).forEach((c, i) =>
      onbAnim(c, [{ opacity: 0, transform: "translateY(10px)" }, { opacity: 1, transform: "translateY(0)" }], { duration: 380, delay: delay + 40 + i * 45, easing: "cubic-bezier(0.2, 0.8, 0.2, 1)" })
    );
  const id = ONB_STEPS[onb.i].id;
  if (id === "hello" && !quiet) {
    onbAnim(pane.querySelector(".onb-avatar"), [{ transform: "scale(0.6)", opacity: 0 }, { transform: "scale(1)", opacity: 1 }], { duration: 560, delay: delay + 60, easing: EASE_SPRING });
    onbAnim(pane.querySelector(".onb-spark"), [{ transform: "scale(0) rotate(-40deg)", opacity: 0 }, { transform: "scale(1) rotate(0)", opacity: 1 }], { duration: 520, delay: delay + 320, easing: EASE_SPRING });
  }
  if (id === "done") {
    const mark = pane.querySelector(".onb-done-mark");
    if (quiet) mark.classList.add("drawn");
    else {
      onbAnim(mark, [{ transform: "scale(0.5)", opacity: 0 }, { transform: "scale(1)", opacity: 1 }], { duration: 520, delay: delay + 40, easing: EASE_SPRING });
      setTimeout(() => mark.classList.add("drawn"), delay + 180);
      pane.querySelectorAll(".onb-sum").forEach((li, i) =>
        onbAnim(li, [{ opacity: 0, transform: "translateY(8px)" }, { opacity: 1, transform: "translateY(0)" }], { duration: 360, delay: delay + 420 + i * 70, easing: "cubic-bezier(0.2, 0.8, 0.2, 1)" })
      );
      pane.querySelectorAll(".onb-mark.ok").forEach((m, i) =>
        onbAnim(m, [{ transform: "scale(0.4)" }, { transform: "scale(1)" }], { duration: 420, delay: delay + 480 + i * 70, easing: EASE_SPRING })
      );
    }
  }
  // 焦点跟着走（键盘用户），等进场动画开始后再给，免得跳滚
  setTimeout(() => {
    const first = pane.querySelector("input:not([type=hidden]), select");
    if (first && !first.value && !matchMedia("(pointer: coarse)").matches) first.focus({ preventScroll: true });
  }, delay + 60);
}

function onbError(msg) {
  const err = $("onb-err");
  err.textContent = msg;
  err.hidden = false;
  if (!calm()) onbAnim(err, [{ transform: "translateX(0)" }, { transform: "translateX(-6px)" }, { transform: "translateX(5px)" }, { transform: "translateX(-3px)" }, { transform: "translateX(0)" }], { duration: 320, easing: "ease-out" });
}

export async function openOnboarding(info) {
  if (onb.open || !admin()) return;
  onb.open = true;
  onb.info = info || null;
  // 接着上次的那一步（in_progress 记了步）；没记就从头
  const resume = info && info.state === "in_progress" ? ONB_STEPS.findIndex((s) => s.id === info.step) : -1;
  onb.i = resume > 0 ? resume : 0;
  onb.models = null;
  onb.pickMain = onb.pickWork = "";
  if (!state.settings) await loadSettings();
  // 模型那一步要知道已有的端点、模型库和各专岗现在用的模型
  await Promise.all([loadModels(), state.agents ? null : loadAgents()]).catch(() => null);
  try {
    onb.cfg = await api("GET", "/api/settings/config");
  } catch (_) {
    onb.cfg = null;
  }
  const root = document.createElement("div");
  root.id = "onb";
  root.className = "onb";
  root.innerHTML = onbShell();
  document.body.appendChild(root);
  document.body.classList.add("onb-lock");
  const quiet = calm();
  onbAnim(root.querySelector(".onb-scrim"), [{ opacity: 0 }, { opacity: 1 }], { duration: 260, easing: "linear" });
  onbAnim(root.querySelector(".onb-card"), quiet ? [{ opacity: 0 }, { opacity: 1 }] : [{ opacity: 0, transform: "translateY(24px) scale(0.97)" }, { opacity: 1, transform: "translateY(0) scale(1)" }], {
    duration: quiet ? 200 : 520,
    easing: "cubic-bezier(0.32, 0.72, 0, 1)",
  });
  if (onb.i > 0) {
    onb.i -= 1;
    await onbGo(1); // 走一遍进入那步要加载的东西（搜索清单、群管理员…）
  } else onbPaint(0);
  root.addEventListener("keydown", onbTrapFocus);
}

// 模态里循环焦点：Tab 不跑到背后的页面上（design.md 8.4 待补齐项）
function onbTrapFocus(e) {
  if (e.key !== "Tab") return;
  const card = document.querySelector("#onb .onb-card");
  if (!card) return;
  const els = [...card.querySelectorAll("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])")].filter((x) => !x.disabled && x.offsetParent !== null && !x.closest(".leaving"));
  if (!els.length) return;
  const first = els[0], last = els[els.length - 1];
  if (e.shiftKey && (document.activeElement === first || !card.contains(document.activeElement))) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && (document.activeElement === last || !card.contains(document.activeElement))) {
    e.preventDefault();
    first.focus();
  }
}

async function closeOnboarding(action) {
  const root = $("onb");
  if (!root) return;
  try {
    onb.info = await api("POST", "/api/onboarding", { action });
  } catch (e) {
    toast(e.message, true);
  }
  onbSettle();
  onb.open = false;
  const quiet = calm();
  const card = root.querySelector(".onb-card");
  root.style.pointerEvents = "none";
  onbAnim(root.querySelector(".onb-scrim"), [{ opacity: 1 }, { opacity: 0 }], { duration: 240, easing: "linear" });
  const a = onbAnim(card, quiet ? [{ opacity: 1 }, { opacity: 0 }] : [{ opacity: 1, transform: "scale(1)" }, { opacity: 0, transform: action === "done" ? "scale(1.03)" : "translateY(16px) scale(0.97)" }], {
    duration: 240,
    easing: "cubic-bezier(0.4, 0, 1, 1)",
  });
  const gone = () => {
    root.remove();
    document.body.classList.remove("onb-lock");
  };
  if (a) a.finished.then(gone, gone);
  else gone();
  onb.anims = [];
  loadSettings().then(() => render());
  if (action === "skip") toast("跳过了，以后在设置概况里可以重新引导");
  else if (action === "done" && onb.info && !onb.info.usable) toast("先存下了，设置概况里可以接着引导");
}

// 这一步要保存的东西；返回错误文字（空 = 过）
async function onbSave(id) {
  const v = (x) => ($(x) ? $(x).value.trim() : "");
  if (id === "models") {
    const ep = onbEndpoint();
    if (!/^https?:\/\/\S+$/.test(v("onb-url"))) return "端点地址要以 http:// 或 https:// 开头。";
    if (!ep.key_set && !v("onb-key")) return "还没填密钥。";
    if (!v("onb-main")) return "先选或手填主模型 ID（点「测试连接」可以拉出列表）。";
    const epId = ep.id || "default";
    await api("PUT", `/api/settings/endpoints/${encodeURIComponent(epId)}`, {
      name: ep.name || "默认端点", protocol: v("onb-proto") || "openai", base_url: v("onb-url"), api_key: v("onb-key") || undefined,
      retries: ep.retries ?? 5, retry_delay_s: ep.retry_delay_s ?? 10, max_concurrency: ep.max_concurrency ?? 2, max_rpm: ep.max_rpm ?? 0,
    });
    await loadModels();
    // 模型库里这个端点已有同名模型就复用，没有就加一条（能力参数用默认值，之后在「模型」页细调）
    const ensure = async (name) => {
      if (!name) return "";
      const hit = ((state.mdl && state.mdl.models) || []).find((x) => x.endpoint === epId && x.model === name);
      if (hit) return hit.id;
      const mid = "m" + Math.random().toString(36).slice(2, 8);
      await api("PUT", `/api/settings/model-list/${mid}`, { endpoint: epId, model: name, name, efforts: [], vision: false, context_window: 128000, max_tokens: 32768 });
      await loadModels();
      return mid;
    };
    const mainId = await ensure(v("onb-main"));
    const workId = (await ensure(v("onb-worker"))) || "";
    await api("PUT", "/api/agents/main", { model: mainId, effort: "" });
    for (const k of ["news", "idea", "goal", "task"]) await api("PUT", `/api/agents/${k}`, { model: workId, effort: "" });
    await loadSettings().catch(() => null);
    // 验证所选模型（docs/13 A03）：连上了 ≠ 选中的模型能干活
    const bad = await onbVerify([mainId, workId].filter((x, i, a) => x && a.indexOf(x) === i));
    return bad;
  }
  const patch = {};
  if (id === "groups" && $("onb-serve"))
    patch["groups.serve"] = [...$("onb-serve").querySelectorAll(".re-row")]
      .map((row) => Object.fromEntries([...row.querySelectorAll("input")].map((i) => [i.dataset.k, i.value.trim()])))
      .filter((o) => o.group || o.workspace)
      .map((o) => ({ group: /^\d+$/.test(o.group) ? `qq:${o.group}` : o.group, workspace: o.workspace }));
  if (id === "keys") {
    if (v("onb-jev")) patch["jev.api_key"] = v("onb-jev");
  }
  if (id === "search" && onb.presets) {
    const picked = (onb.presets || []).filter((p) => (onb.sxPicked || []).includes(p.id));
    if (!picked.length) return "";
    const items = [];
    for (const p of picked) {
      const key = v(`onb-sx-key-${p.id}`);
      if (!p.free && !key && !p.key_set) return `${p.label} 要先填 API 密钥（勾上后输入框下方有去官网拿密钥的链接），或者不勾它。`;
      items.push(key ? { id: p.id, key } : { id: p.id });
    }
    const r = await api("POST", "/api/extensions/presets-setup", { items });
    onb.presets = (r && r.presets) || onb.presets;
    return "";
  }
  if (id === "admins" && onb.ga) {
    for (const gid of Object.keys(onb.ga)) {
      const box = $(`onb-ga-${gid}`);
      if (!box) continue;
      const pw = ($(`onb-ga-pw-${gid}`) || {}).value || "";
      const accounts = [...box.querySelectorAll(".ci")].map((c) => c.dataset.v);
      if (pw && pw.length < 8) return "群管理员密码至少 8 位。";
      const same = JSON.stringify(accounts) === JSON.stringify(onb.ga[gid].accounts || []);
      if (!pw && same) continue;
      try {
        onb.ga[gid] = await api("PUT", `/api/groups/${encodeURIComponent(gid)}/group-admin`, { password: pw || undefined, accounts });
      } catch (e) {
        return `群 ${gid}：${e.message}`;
      }
    }
  }
  if (id === "admins" && $("onb-admins")) patch["approval.admins"] = [...$("onb-admins").querySelectorAll(".ci")].map((c) => c.dataset.v);
  // 没变的不提交，免得把「要重载」标记白白点亮
  for (const k of Object.keys(patch)) {
    const f = onbField(k);
    if (f && f.type !== "secret" && JSON.stringify(f.value) === JSON.stringify(patch[k])) delete patch[k];
  }
  if (!Object.keys(patch).length) return "";
  onb.cfg = await api("PUT", "/api/settings/config", patch).then(() => api("GET", "/api/settings/config"));
  if (Object.hasOwn(patch, "groups.serve")) {
    await loadGroups();
    applyHash();
    await loadView();
  }
  return "";
}

// 逐个验证所选模型：能回答才放行；工具不通、最大输出被降档只提示不拦
async function onbVerify(ids) {
  const out = $("onb-status");
  const notes = [];
  for (const mid of ids) {
    const entry = ((state.mdl && state.mdl.models) || []).find((x) => x.id === mid) || {};
    if (out) out.innerHTML = `<i class="onb-spin"></i>正在验证 ${esc(entry.model || mid)}（一句短问话 + 一次空工具测试）…`;
    let r;
    try {
      r = await api("POST", `/api/settings/model-list/${encodeURIComponent(mid)}/verify`, {});
    } catch (e) {
      r = { ok: false, error: e.message };
    }
    if (!r.ok) {
      if (out) out.textContent = "";
      return `${entry.model || mid} 没能正常回答：${r.error || "验证没通过"}。可以换个模型，或先「稍后再配」。`;
    }
    if (r.suggested_max_tokens) {
      try {
        await api("PUT", `/api/settings/model-list/${encodeURIComponent(mid)}`, Object.assign({}, entry, { max_tokens: r.suggested_max_tokens }));
        await loadModels();
      } catch (_) {}
    }
    if (r.note) notes.push(`${entry.model || mid}：${r.note}`);
  }
  if (out) out.innerHTML = `<i class="onb-ok">${SVG.check}</i>验证通过`;
  onb.verifyNotes = notes;
  return "";
}

// 记下走到了哪一步（docs/13 A04）：刷新 / 关页重进回到这里；失败不拦人
function onbRemember() {
  const step = ONB_STEPS[onb.i].id;
  if (step === "hello") return;
  api("POST", "/api/onboarding", { action: "progress", step }).catch(() => null);
}

async function onbGo(delta) {
  const to = Math.max(0, Math.min(ONB_STEPS.length - 1, onb.i + delta));
  if (to === onb.i) return;
  onb.i = to;
  if (ONB_STEPS[to].id === "admins") {
    onb.ga = {};
    await Promise.all(
      onbServed().map((gid) =>
        api("GET", `/api/groups/${encodeURIComponent(gid)}/group-admin`)
          .then((r) => (onb.ga[gid] = r))
          .catch(() => {})
      )
    );
  }
  if (ONB_STEPS[to].id === "search" && !onb.presets) {
    try {
      onb.presets = ((await api("GET", "/api/extensions/presets")) || {}).presets || [];
    } catch (_) {
      onb.presets = [];
    }
  }
  if (ONB_STEPS[to].id === "look" && !state.avatarCfg) {
    try {
      state.avatarCfg = await api("GET", "/api/settings/avatar");
    } catch (_) {}
  }
  if (ONB_STEPS[to].id === "done") {
    try {
      onb.info = await api("GET", "/api/onboarding");
    } catch (_) {}
  }
  onbRemember();
  onbPaint(delta);
}

export async function onbAct(el) {
  const a = el.dataset.act;
  if (onb.busy && a !== "onb-skip") return;
  const id = ONB_STEPS[onb.i].id;
  if (a === "onb-restart") {
    if (onb.open) return;
    try {
      const info = await api("POST", "/api/onboarding", { action: "reset" });
      return openOnboarding(info);
    } catch (e) {
      return toast(e.message, true);
    }
  }
  if (a === "onb-skip") return closeOnboarding("skip");
  if (a === "onb-sx-toggle") {
    const pid = el.value;
    const set = new Set(onb.sxPicked || []);
    if (el.checked) set.add(pid);
    else set.delete(pid);
    // 保持清单顺序（第一个勾上的当主搜索）
    onb.sxPicked = (onb.presets || []).map((p) => p.id).filter((x) => set.has(x));
    const row = el.closest(".onb-sx");
    if (row) row.classList.toggle("on", el.checked);
    if (el.checked && row) row.scrollIntoView({ block: "nearest", behavior: calm() ? "auto" : "smooth" });
    if (el.checked && row) setTimeout(() => row.querySelector(".onb-sx-key input") && !matchMedia("(pointer: coarse)").matches && row.querySelector(".onb-sx-key input").focus({ preventScroll: true }), 60);
    return;
  }
  if (a === "onb-back") return onbGo(-1);
  if (a === "onb-goto") {
    const to = ONB_STEPS.findIndex((s) => s.id === el.dataset.step);
    if (to >= 0) return onbGo(to - onb.i);
    return;
  }
  if (a === "onb-continue") {
    if (onb.open) return;
    try {
      const cur = await api("GET", "/api/onboarding");
      const step = cur.next_step || cur.step || "models";
      const info = await api("POST", "/api/onboarding", { action: "progress", step: step === "done" ? "models" : step });
      return openOnboarding(info);
    } catch (e) {
      return toast(e.message, true);
    }
  }
  if (a === "onb-later") return onbGo(1);
  if (a === "onb-test") {
    const out = $("onb-status");
    const url = $("onb-url").value.trim();
    const key = $("onb-key").value.trim();
    const ep = onbEndpoint();
    if (!/^https?:\/\/\S+$/.test(url)) return onbError("地址要以 http:// 或 https:// 开头。");
    if (!ep.key_set && !key) return onbError("先填密钥再测。");
    $("onb-err").hidden = true;
    el.disabled = true;
    out.innerHTML = `<i class="onb-spin"></i>正在连…`;
    try {
      const r = await api("POST", `/api/settings/endpoints/${encodeURIComponent(ep.id || "default")}/test`, { base_url: url, api_key: key || undefined, protocol: $("onb-proto").value });
      if (!r.ok) {
        out.textContent = "";
        onbError(`${r.error || "没连上"}（有的服务不提供模型列表：地址和密钥没错的话，直接在下面手填模型 ID，保存时会真的发一句话验证）`);
        return;
      }
      onb.models = r.models || [];
      // 只重画模型下拉，保留已经填的地址和密钥
      onb.pickMain = $("onb-main").value || onb.pickMain;
      onb.pickWork = $("onb-worker").value || onb.pickWork;
      const tmp = document.createElement("div");
      tmp.innerHTML = onbPane("models");
      const picks = el.closest(".onb-pane").querySelector(".onb-picks");
      picks.replaceWith(tmp.querySelector(".onb-picks"));
      const np = el.closest(".onb-pane").querySelector(".onb-picks");
      out.innerHTML = `<i class="onb-ok">${SVG.check}</i>连上了，找到 ${onb.models.length} 个模型`;
      if (!calm()) {
        onbAnim(out.querySelector(".onb-ok"), [{ transform: "scale(0.3)", opacity: 0 }, { transform: "scale(1)", opacity: 1 }], { duration: 460, easing: EASE_SPRING });
        [...np.children].forEach((c, i) => onbAnim(c, [{ opacity: 0, transform: "translateY(8px)" }, { opacity: 1, transform: "translateY(0)" }], { duration: 340, delay: 60 + i * 40, easing: "cubic-bezier(0.2, 0.8, 0.2, 1)" }));
      }
    } catch (e) {
      out.textContent = "";
      onbError(e.message);
    } finally {
      el.disabled = false;
    }
    return;
  }
  if (a === "onb-next") {
    if (id === "done") return closeOnboarding("done");
    if (id === "hello") return onbGo(1);
    onb.busy = true;
    el.disabled = true;
    const label = el.textContent;
    el.innerHTML = `<i class="onb-spin"></i>保存中`;
    try {
      onb.verifyNotes = [];
      const bad = await onbSave(id);
      if (bad) return onbError(bad);
      await onbGo(1);
      if ((onb.verifyNotes || []).length) toast(onb.verifyNotes.join("；"));
    } catch (e) {
      onbError(e.message);
    } finally {
      onb.busy = false;
      if (el.isConnected) {
        el.disabled = false;
        el.textContent = label;
      }
    }
  }
}

export async function maybeOnboard() {
  if (!admin() || onb.open) return;
  try {
    const info = await api("GET", "/api/onboarding");
    if (info && info.show) openOnboarding(info);
  } catch (_) {}
}
