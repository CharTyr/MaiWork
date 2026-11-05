// MaiWork 网页 · 设置 · 独立「技能」中心与阅读级技能详情抽屉。
import { $, state } from "../state.js";
import { SVG, esc, ico, toast } from "../util.js";
import { agentFish, api } from "../api.js";
import { repaintSheet } from "../sheet.js";
import { loading } from "../pages/news.js";
import { mdLite } from "../chat.js";
import { enterChatWithDraft, enterChatWithSkill } from "../chat_slash.js";
import { confirmExecTools, execConfirmNeeded, loadExt } from "./ext.js";

const CORE_ORDER = ["main", "news", "idea", "goal", "task"];

export function skillIconName(skill) {
  const s = `${(skill && skill.name) || ""} ${(skill && skill.description) || ""}`.toLowerCase();
  if (/news|digest|rss|资讯|新闻|日报/.test(s)) return "newspaper";
  if (/idea|spark|构想|灵感|脑暴/.test(s)) return "bulb";
  if (/goal|target|目标|跟进|巡检/.test(s)) return "bullseye";
  if (/search|research|radar|搜索|调研|检索|抓取/.test(s)) return "magnifier";
  if (/report|weekly|doc|summary|memo|周报|报告|文档|总结|纪要/.test(s)) return "memo";
  if (/file|archive|xlsx|csv|pdf|ppt|表格|归档|导出/.test(s)) return "filebox";
  if (/deploy|release|launch|上线|部署|发布/.test(s)) return "rocket";
  if (/test|check|qa|debug|测试|回归|核对|排查/.test(s)) return "testtube";
  if (/design|ui|image|card|poster|视觉|设计|画图|卡片/.test(s)) return "palette";
  if (/code|dev|script|tool|api|代码|脚本|接口/.test(s)) return "tools";
  return skill && skill.source === "builtin" ? "books" : "sparkles";
}

export function skillIcon(skill, cls = "sk-ico") {
  return `<span class="${cls}">${ico(skillIconName(skill))}</span>`;
}

function orderedProfiles() {
  const list = (state.agents && state.agents.profiles) || [];
  return [...list].sort((a, b) => {
    const ia = CORE_ORDER.indexOf(a.kind);
    const ib = CORE_ORDER.indexOf(b.kind);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
}

function isOpenGeneralist(p) {
  return Boolean(p && (p.kind === "task" || !CORE_ORDER.includes(p.kind)) && p.skills === null);
}

export function skillsForRole(skills, roleKind, profiles) {
  const list = skills || [];
  if (!roleKind) return list;
  if (roleKind === "main") {
    return list.filter((s) => !(s.roles || []).length || (s.roles || []).includes("main"));
  }
  const workerSkills = list.filter((s) => !(s.roles || []).length || (s.roles || []).includes("worker"));
  const prof = (profiles || []).find((p) => p.kind === roleKind);
  if (!prof || isOpenGeneralist(prof)) return workerSkills;
  const bound = new Set(prof.skills || []);
  return workerSkills.filter((s) => bound.has(s.name));
}

function suggestionSlug(sug, existingNames) {
  const raw = String((sug && sug.name) || "").trim();
  const base = /^[A-Za-z0-9_-]{1,56}$/.test(raw)
    ? raw.toLowerCase()
    : `group-skill-${(sug && sug.id) || 1}`;
  if (!existingNames.has(base)) return base;
  let n = 2;
  while (existingNames.has(`${base}-${n}`)) n++;
  return `${base}-${n}`;
}

function renderSkillCard(k) {
  const active = k.enabled !== false;
  return `<button type="button" class="sk-card" data-act="skill-open" data-name="${esc(k.name)}">
    ${skillIcon(k)}
    <span class="sk-card-main">
      <span class="sk-card-name">${esc(k.name)}${k.source === "file" ? `<span class="tag">文件</span>` : ""}</span>
      <span class="sk-card-desc">${esc(k.description || "暂无简介")}</span>
    </span>
    <span class="sk-card-state" title="${esc(active ? "已启用" : k.disabled_reason || "已停用")}">${
      active ? `<span class="sk-check">${SVG.check}</span>` : `<span class="ntag">停用</span>`
    }</span>
  </button>`;
}

function renderSkillGroup(groupKey, title, items, q) {
  if (!items.length) {
    if (q || state.skillRole) return "";
    if (groupKey === "custom") {
      return `<div class="sk-sec"><div class="sk-sec-h"><span>${esc(title)}</span></div><p class="fine sk-empty-note">暂未添加自定义技能，可从右上角「添加」新建或上传 zip 压缩包。</p></div>`;
    }
    return "";
  }
  const expanded = Boolean(q || (state.skillMore && state.skillMore[groupKey]));
  const limit = 6;
  const shown = expanded ? items : items.slice(0, limit);
  const rest = items.length - shown.length;
  const nextNames = items
    .slice(limit, limit + 2)
    .map((x) => x.name)
    .join("、");
  return `<div class="sk-sec">
    <div class="sk-sec-h"><span>${esc(title)}</span><span class="sk-sec-cnt">${items.length}</span></div>
    <div class="sk-grid">${shown.map(renderSkillCard).join("")}</div>
    ${
      rest > 0
        ? `<button type="button" class="link-btn sk-more-btn" data-act="skills-more" data-group="${groupKey}">${
            nextNames ? `查看 ${esc(nextNames)}，` : ""
          }展开其余 ${rest} 项</button>`
        : !q && items.length > limit
          ? `<button type="button" class="link-btn sk-more-btn" data-act="skills-more" data-group="${groupKey}">收起</button>`
          : ""
    }
  </div>`;
}

export function skillsPage() {
  const x = state.ext;
  if (!x) return loading((state.loadErr || {}).ext, "ext");
  const q = String(state.skillQ || "").trim().toLowerCase();
  const profiles = orderedProfiles();
  const role = state.skillRole || "";
  const allSkills = skillsForRole(x.skills || [], role, profiles).filter(
    (s) => !q || [s.name, s.description].some((v) => String(v || "").toLowerCase().includes(q))
  );
  const customs = allSkills.filter((s) => s.source !== "builtin");
  const builtins = allSkills.filter((s) => s.source === "builtin");
  const sugs = ((x.skill_suggestions || []).filter(
    (s) => !q || [s.name, s.description, s.group_name].some((v) => String(v || "").toLowerCase().includes(q))
  )).slice(0, 4);

  const sugHTML = sugs.length
    ? `<div class="sk-sec">
        <div class="sk-sec-h"><span>来自各群的经验</span></div>
        <div class="sk-sug-grid">
          ${sugs
            .map((s) => {
              const rawIdx = (x.skill_suggestions || []).indexOf(s);
              return `<div class="sk-sug-card">
                ${skillIcon({ name: s.name, description: s.description })}
                <div class="sk-card-main">
                  <div class="sk-card-name">${esc(s.name)}</div>
                  <div class="sk-card-desc">${esc(s.description || (s.group_name ? `来自 ${s.group_name}` : "群内经验沉淀"))}</div>
                </div>
                <button type="button" class="btn small sk-sug-btn" data-act="skill-promote" data-idx="${rawIdx}">设为全局技能</button>
              </div>`;
            })
            .join("")}
        </div>
      </div>`
    : "";

  const roleBar = profiles.length
    ? `<div class="sk-roles" role="tablist" aria-label="按角色筛选技能">
        <button type="button" role="tab" aria-selected="${!role}" class="sk-role-pill${!role ? " on" : ""}" data-act="skills-role" data-role="">全部</button>
        ${profiles
          .map(
            (p) => `<button type="button" role="tab" aria-selected="${role === p.kind}" class="sk-role-pill${role === p.kind ? " on" : ""}" data-act="skills-role" data-role="${esc(p.kind)}">
              <span class="agent-fish xs">${agentFish(p.kind, 20, role === p.kind ? {} : { still: true })}</span>
              <span>${esc(p.title)}</span>
            </button>`
          )
          .join("")}
      </div>`
    : "";

  const groupsHTML = renderSkillGroup("custom", "自建与已安装", customs, q) + renderSkillGroup("builtin", "系统内置", builtins, q);

  return `<div class="sk-page">
    <div class="sk-top">
      <div class="sk-title-wrap">
        <h2 class="h-sub sk-title">技能</h2>
        <p class="fine sk-sub">通过专用技能扩展主模型与各专岗的工作方式</p>
      </div>
      <div class="sk-top-acts">
        <label class="sk-search" aria-label="搜索技能">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.8-3.8"/></svg>
          <input id="skill-q" type="search" placeholder="搜索技能" value="${esc(state.skillQ || "")}" autocomplete="off" />
        </label>
        <button type="button" class="icon-btn sk-refresh" data-act="skills-refresh" title="刷新技能清单" aria-label="刷新技能清单">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 11a8 8 0 1 0 2 5.3"/><path d="M20 4v7h-7"/></svg>
        </button>
        <input type="file" id="skill-zip" accept=".zip,application/zip" hidden />
        <details class="section-picker sk-add-menu">
          <summary class="btn primary small sk-add-btn"><span>添加</span>${SVG.down}</summary>
          <nav aria-label="添加技能">
            <button type="button" class="sk-menu-item" data-act="skill-new-form">${ico("memo")}<span>新建技能</span></button>
            <button type="button" class="sk-menu-item" data-act="skill-upload-pick">${ico("package")}<span>上传 zip 压缩包</span></button>
            <button type="button" class="sk-menu-item" data-act="skill-make-in-chat">${ico("sparkles")}<span>在对话中生成技能</span></button>
          </nav>
        </details>
      </div>
    </div>
    ${sugHTML}
    ${roleBar}
    ${groupsHTML || `<p class="h-meta">无匹配的技能</p>`}
  </div>`;
}

export function skillDetailView(k) {
  const d = state.extEdit && state.extEdit.data;
  if (!d) return `<div class="sk-detail">${loading()}</div>`;
  const name = (k && k.name) || d.name || "";
  const src = d.source || (k && k.source) || "web";
  const isBuiltin = src === "builtin";
  const manual = (k ? k.manual_enabled : d.manual_enabled) !== false;
  const active = (k ? k.enabled : d.enabled) !== false;
  const disabledReason = (k && k.disabled_reason) || d.disabled_reason || "";
  const roles = d.roles && d.roles.length ? d.roles : (k && k.roles && k.roles.length ? k.roles : ["worker"]);
  const canMain = roles.includes("main");
  const canWorker = roles.includes("worker");
  const scopeLabel = canMain && canWorker ? "主模型与子专岗可用" : canMain ? "仅主模型可用" : "子专岗可用";
  const srcLabel = isBuiltin ? "系统内置" : src === "file" ? "服务器文件" : "自定义";
  const specialists = orderedProfiles().filter((p) => p.kind !== "main");

  const bindHTML =
    canWorker && specialists.length
      ? `<div class="sk-bind-box">
          <div class="sk-bind-label">挂载专岗</div>
          <div class="sk-bind-list">
            ${specialists
              .map((p) => {
                const checked = isOpenGeneralist(p) || (Array.isArray(p.skills) && p.skills.includes(name));
                return `<button type="button" role="checkbox" aria-checked="${checked}" class="sk-bind-pill${checked ? " on" : ""}" data-act="skill-bind-agent" data-name="${esc(name)}" data-agent="${esc(p.kind)}" data-checked="${checked ? "1" : "0"}">
                  <span class="agent-fish xs">${agentFish(p.kind, 18, { still: true })}</span>
                  <span>${esc(p.title)}</span>
                  ${checked ? `<span class="sk-bind-ck">${SVG.check}</span>` : ""}
                </button>`;
              })
              .join("")}
          </div>
        </div>`
      : "";

  const toolsHTML = (d.allowed_tools || []).length
    ? `<div class="sk-meta-row"><span class="fine">声明工具：</span><span class="ext-tools">${d.allowed_tools.map((t) => `<span class="ntag">${esc(t)}</span>`).join("")}</span></div>`
    : "";
  const filesHTML = (d.files || []).length
    ? `<div class="sk-meta-row"><span class="fine">附件清单：${d.files.map(esc).join("、")}</span></div>`
    : "";

  return `<div class="sk-detail">
    <div class="sk-detail-top">
      ${skillIcon({ name, description: d.description, source: src }, "sk-ico lg")}
      <div class="sk-detail-acts">
        <button type="button" class="switch${manual ? " on" : ""}" role="switch" aria-checked="${manual}" data-act="skill-switch" data-name="${esc(name)}" data-on="${manual ? "0" : "1"}" aria-label="${manual ? "停用技能" : "启用技能"}" title="${manual ? "停用技能" : "启用技能"}"><span></span></button>
        ${
          isBuiltin
            ? `<button type="button" class="btn small" data-act="skill-clone" data-name="${esc(name)}">复制并自定义</button>`
            : `<button type="button" class="btn small" data-act="skill-edit-mode" data-name="${esc(name)}">修改</button>`
        }
        <a class="btn small" href="/api/extensions/skills/${encodeURIComponent(name)}/export" download="${esc(name)}.zip">导出 zip</a>
      </div>
    </div>
    <h1 class="sk-detail-title">${esc(name)} <span class="sk-detail-sub">Skill</span></h1>
    <div class="sk-detail-tags">
      <span class="ntag ${active ? "ok" : ""}">${esc(active ? "已启用" : disabledReason || "已停用")}</span>
      <span class="ntag">${esc(scopeLabel)}</span>
      <span class="ntag">${esc(srcLabel)}</span>
    </div>
    <p class="sk-detail-desc">${esc(d.description || "暂无简介")}</p>
    ${bindHTML}
    ${toolsHTML}
    ${filesHTML}
    <div class="sk-preview md">${mdLite(d.body || "", { copyable: false })}</div>
    <div class="sk-detail-foot">
      ${isBuiltin ? `<span></span>` : `<button type="button" class="btn danger small sk-del-btn" data-act="skill-del" data-name="${esc(name)}">删除</button>`}
      <button type="button" class="btn primary sk-try-btn" data-act="skill-try-chat" data-name="${esc(name)}">在对话中试用</button>
    </div>
  </div>`;
}

function closeAddMenu() {
  if (typeof document.querySelectorAll !== "function") return;
  for (const d of document.querySelectorAll("details.sk-add-menu[open]")) d.open = false;
}

export async function actSkills(a, el) {
  switch (a) {
    case "skills-role":
      state.skillRole = el.dataset.role || "";
      repaintSheet();
      return true;
    case "skills-more": {
      const g = el.dataset.group || "builtin";
      state.skillMore = state.skillMore || {};
      state.skillMore[g] = !state.skillMore[g];
      repaintSheet();
      return true;
    }
    case "skills-refresh":
      el.disabled = true;
      try {
        await loadExt();
        repaintSheet();
        toast("已刷新技能清单");
      } catch (err) {
        toast(err.message, true);
      } finally {
        el.disabled = false;
      }
      return true;
    case "skill-open": {
      const name = el.dataset.name;
      if (!name) return true;
      state.searchSheet = null;
      state.extEdit = { kind: "skill", name, mode: "view", data: null };
      repaintSheet();
      try {
        const d = await api("GET", `/api/extensions/skills/${encodeURIComponent(name)}`);
        if (state.extEdit && state.extEdit.name === name) {
          state.extEdit.data = d;
          repaintSheet();
        }
      } catch (err) {
        state.extEdit = null;
        repaintSheet();
        toast(err.message, true);
      }
      return true;
    }
    case "skill-new-form":
      closeAddMenu();
      state.searchSheet = null;
      state.extEdit = {
        kind: "skill",
        name: null,
        mode: "edit",
        data: { name: "", description: "", roles: ["main", "worker"], allowed_tools: [], body: "" },
      };
      repaintSheet();
      setTimeout(() => $("k-name")?.focus(), 40);
      return true;
    case "skill-upload-pick":
      closeAddMenu();
      $("skill-zip")?.click?.();
      return true;
    case "skill-make-in-chat":
      closeAddMenu();
      await enterChatWithDraft("", "请根据以下工作需求，编写一份 MaiWork 全局技能（包含英文短横线名称、一句话简介、适用范围与 Markdown 详细步骤）：\n");
      return true;
    case "skill-promote": {
      const list = (state.ext && state.ext.skill_suggestions) || [];
      const sug = list[Number(el.dataset.idx)];
      if (!sug) return true;
      const existing = new Set(((state.ext && state.ext.skills) || []).map((s) => s.name));
      const slug = suggestionSlug(sug, existing);
      state.searchSheet = null;
      state.extEdit = {
        kind: "skill",
        name: null,
        mode: "edit",
        data: {
          name: slug,
          description: sug.description || sug.name || "",
          roles: ["main", "worker"],
          allowed_tools: [],
          body: sug.body || `## ${sug.name || "操作说明"}\n\n${sug.description || ""}`,
        },
      };
      repaintSheet();
      setTimeout(() => $("k-name")?.focus(), 40);
      return true;
    }
    case "skill-switch": {
      const name = el.dataset.name;
      const on = el.dataset.on === "1";
      el.disabled = true;
      try {
        await api("POST", `/api/extensions/skills/${encodeURIComponent(name)}/toggle`, { enabled: on });
        await loadExt();
        const cur = ((state.ext && state.ext.skills) || []).find((s) => s.name === name);
        if (state.extEdit && state.extEdit.name === name && state.extEdit.data && cur) {
          Object.assign(state.extEdit.data, {
            enabled: cur.enabled,
            manual_enabled: cur.manual_enabled,
            disabled_reason: cur.disabled_reason,
          });
        }
        repaintSheet();
        toast(!on ? "已停用" : cur && cur.enabled === false ? cur.disabled_reason || "已启用，等待配套服务开启" : "已启用");
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
      }
      return true;
    }
    case "skill-edit-mode":
      if (state.extEdit && state.extEdit.kind === "skill") {
        state.extEdit.mode = "edit";
        repaintSheet();
      }
      return true;
    case "skill-clone": {
      const name = el.dataset.name || (state.extEdit && state.extEdit.name) || "";
      let d = state.extEdit && state.extEdit.data;
      if (!d && name) {
        try {
          d = await api("GET", `/api/extensions/skills/${encodeURIComponent(name)}`);
        } catch (err) {
          toast(err.message, true);
          return true;
        }
      }
      if (!d) return true;
      const existing = new Set(((state.ext && state.ext.skills) || []).map((s) => s.name));
      let cloneName = `${name}-custom`.slice(0, 60);
      let seq = 2;
      while (existing.has(cloneName)) {
        cloneName = `${name.slice(0, 52)}-custom-${seq++}`;
      }
      state.extEdit = {
        kind: "skill",
        name: null,
        mode: "edit",
        data: {
          name: cloneName,
          description: d.description || "",
          roles: d.roles && d.roles.length ? [...d.roles] : ["worker"],
          allowed_tools: d.allowed_tools ? [...d.allowed_tools] : [],
          body: d.body || "",
          source: "web",
        },
      };
      repaintSheet();
      setTimeout(() => $("k-name")?.focus(), 40);
      return true;
    }
    case "skill-bind-agent": {
      const name = el.dataset.name;
      const agentKind = el.dataset.agent;
      const profiles = (state.agents && state.agents.profiles) || [];
      const p = profiles.find((x) => x.kind === agentKind);
      if (!p || !name) return true;
      const workerAll = ((state.ext && state.ext.skills) || [])
        .filter((s) => !(s.roles || []).length || (s.roles || []).includes("worker"))
        .map((s) => s.name);
      const curSet = new Set(isOpenGeneralist(p) ? workerAll : p.skills || []);
      if (el.dataset.checked === "1") curSet.delete(name);
      else curSet.add(name);
      const nextList = [...curSet];
      const payloadSkills =
        (p.kind === "task" || !CORE_ORDER.includes(p.kind)) && workerAll.length && nextList.length === workerAll.length
          ? null
          : nextList;
      el.disabled = true;
      try {
        const path = `/api/agents/${encodeURIComponent(agentKind)}`;
        try {
          await api("PUT", path, { skills: payloadSkills });
        } catch (ex) {
          if (!execConfirmNeeded(ex) || !confirmExecTools(ex)) throw ex;
          await api("PUT", path, { skills: payloadSkills, confirm_exec: true });
        }
        p.skills = payloadSkills;
        repaintSheet();
        toast("已更新专岗技能挂载");
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
      }
      return true;
    }
    case "skill-try-chat": {
      const name = el.dataset.name;
      const sk =
        ((state.ext && state.ext.skills) || []).find((s) => s.name === name) ||
        (state.extEdit && state.extEdit.data) ||
        { name };
      state.extEdit = null;
      await enterChatWithSkill({ name: sk.name || name, scope: "global", description: sk.description || "" });
      return true;
    }
  }
  return false;
}
