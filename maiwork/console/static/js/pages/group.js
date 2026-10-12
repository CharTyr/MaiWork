// MaiWork 网页 · 「群」页：话题、这个群（本群规矩 / 本群做法 / 群画像）、关注成员、群空间。
import { CATS, TONES, admin, gadmin } from "../state.js";
import { SVG, dayIndex, dayWord, dur, esc, hhmm, ico, now, richText, safeUrl, tip, when } from "../util.js";
import { daySummary, judgeText, stretches } from "../topiclog.js";
import { gname, gplat, platTag } from "../api.js";
import { pulseCard } from "../pulse.js";
import { emptyState, loading } from "./news.js";
import { ctxSection } from "./groupctx.js";
import { controlsSection } from "./groupcontrols.js";

// 一段冷场一张卡：同一段里判了几次合在一起，Jev 那句用这段的代表（开了的那条 / 最新一次）。
function topicItem(s, i) {
  const t = s.rep;
  const opened = s.opened;
  const j = t.jev;
  const span = s.n > 1 ? `${when(s.first.ts)}–${hhmm(s.rows[0].ts)}` : when(t.ts);
  const times = s.n > 1 ? `共判断 ${s.n} 次${s.bestOk != null && !opened ? `，最高 ${s.bestOk.toFixed(2)} 分` : ""}。` : "";
  const r = t.result;
  return `
    <article class="item ruled enter" style="--i:${i}">
      ${ico(opened ? "speech" : "hourglass")}
      <div>
        <h3 class="item-title">${esc(span)} · ${opened ? "已开启话题" : "未开启话题"}</h3>
        <p class="item-body soft">已静默 ${esc(dur(t.quiet_s))}${t.usual_gap_s ? `（平时约 ${esc(dur(t.usual_gap_s))}一条消息）` : ""}。${esc(times)}Jev：${esc(judgeText(t))}${j && j.detail ? `（${esc(j.detail)}）` : ""}。${t.pick && t.pick.title && opened ? `选用「${esc(t.pick.title)}」。` : ""}</p>
        ${opened ? `<div class="opener">${esc(t.opener)}</div>` : ""}
        ${opened ? `<div class="status"><span class="dot ${r && r.replies ? "used" : ""}"></span><span class="status-text">${r ? (r.replies ? `${r.replies} 人回应${r.followups ? `，MaiBot 回复 ${r.followups} 句` : ""}` : "无人回应，将延长下次间隔") : "等待回应"}</span></div>` : ""}
        ${
          gadmin()
            ? `<div class="verdict">判断是否正确？
          <button class="btn" data-act="verdict" data-id="${t.id}" data-v="right" aria-pressed="${t.verdict === "right"}">正确</button>
          <button class="btn" data-act="verdict" data-id="${t.id}" data-v="wrong" aria-pressed="${t.verdict === "wrong"}">错误</button>
        </div>`
            : ""
        }
      </div>
    </article>`;
}

function profileSection(v) {
  const secs = (v && v.profile) || [];
  const byCat = Object.fromEntries(secs.map((s) => [s.category, s]));
  let i = 0;
  return CATS.map(([cat, name]) => {
    const sec = byCat[cat] || { entries: [] };
    const entries = sec.entries || [];
    if (!entries.length && !gadmin()) return "";
    return `
      <div class="pf-sec enter" style="--i:${i++}">
        <div class="pf-name">${esc(sec.name || name)}${gadmin() ? `<button class="pf-add" data-act="pf-add" data-cat="${cat}" aria-label="添加">${SVG.plus}</button>` : ""}</div>
        ${
          entries.length
            ? entries
                .map((e) => {
                  const meta = e.locked
                    ? gadmin()
                      ? e.source === "admin"
                        ? "管理员添加 · 不会自动修改"
                        : "已锁定 · 不会自动修改"
                      : "管理员认可"
                    : [e.evidence_count ? `依据 ${e.evidence_count} 条消息` : "", e.last_ts ? `最近 ${dayWord(e.last_ts)}` : ""].filter(Boolean).join(" · ");
                  return `
            <div class="pf" data-entry="${e.id}">
              <div><div class="pf-text">${esc(e.text)}</div><div class="pf-meta">${esc(meta)}</div></div>
              ${
                gadmin()
                  ? `<div class="pf-acts">
                <button class="icon-btn" data-act="pf-edit" data-id="${e.id}" aria-label="修改">${SVG.pen}</button>
                <button class="icon-btn" data-act="pf-lock" data-id="${e.id}" aria-pressed="${!!e.locked}" aria-label="${e.locked ? "解锁" : "锁定"}">${SVG.lock}</button>
                <button class="icon-btn" data-act="pf-del" data-id="${e.id}" aria-label="删除">${SVG.trash}</button>
              </div>`
                  : ""
              }
            </div>`;
                })
                .join("")
            : `<div class="pf pf-none"><div class="pf-meta">暂无</div></div>`
        }
      </div>`;
  }).join("");
}

function groupSpaceBlock(v, g) {
  const plat = gplat(g, v);
  if (plat !== "qq") {
    const who = plat === "telegram" ? "Telegram 群" : "QQ 官方机器人群";
    return `<h2 class="h-sub">群空间</h2><p class="h-meta">${who}不支持群文件，成果通过网页链接发送</p>`;
  }
  const gs = v && v.group_space;
  if (!gs) return "";
  const role = { owner: "群主", admin: "管理员", member: "成员" }[gs.role] || "未知";
  const cap = [
    ["files_list", "查看群文件"],
    ["files_manage", "管理自有文件"],
    ["notice_send", "发布公告"],
    ["album_upload", "上传相册"],
  ];
  const any = cap.some(([k]) => gs[k]);
  return `
    <h2 class="h-sub">群空间 <small>${esc(role)}</small></h2>
    ${
      any
        ? `<div class="caps">${cap.map(([k, n]) => `<span class="cap ${gs[k] ? "on" : ""}">${gs[k] ? SVG.check : ""}${n}</span>`).join("")}</div>`
        : `<p class="h-meta">暂不可用</p>`
    }`;
}

function focusSection(v) {
  const focus = (v && v.focus) || [];
  let html = `<div class="h-sub-row"><h2 class="h-sub">关注成员 <span class="private">${SVG.lock}仅管理员可见</span></h2><button class="btn small" data-act="focus-add">添加</button></div>`;
  if (!focus.length) return html + `<p class="h-meta">暂无</p>`;
  return (
    html +
    focus
      .map(
        (p, k) => `
      <div class="person enter" style="--i:${k}">
        <div class="face" style="background:${TONES[Math.abs(hash(p.user_id)) % TONES.length]}">${esc(pname(p).slice(0, 1))}${p.avatar ? `<img src="${esc(p.avatar)}" alt="" loading="lazy" onerror="this.remove()" />` : ""}</div>
        <div>
          <div class="person-name">${esc(pname(p))}${(p.reasons || []).map((r) => `<span class="tag">${esc(r)}</span>`).join("")}</div>
          ${personaBlock(p)}
          ${personalBlock(p)}
        </div>
        ${admin() ? `<button class="icon-btn" data-act="focus-rm" data-uid="${esc(p.user_id)}" aria-label="取消关注">${SVG.close}</button>` : ""}
      </div>`
      )
      .join("")
  );
}

// 关注成员显示名：群名片（QQ昵称）；不回落成 QQ 号
const pname = (p) => String(p.display_name || p.name || "群成员");

function personaBlock(p) {
  const ps = p.persona;
  if (!ps) return `<div class="person-note">${esc(p.note || "信息不足")}</div>`;
  const list = (label, arr) =>
    arr && arr.length ? `<div class="pa-row"><span class="pa-k">${label}</span><span class="pa-v">${arr.map(esc).join("、")}</span></div>` : "";
  return `
    <div class="person-note">${esc(ps.summary || p.note || "")}</div>
    <div class="pa">
      ${list("近期事务", ps.doing)}${list("关注点", ps.cares)}${list("提过的问题", ps.asked)}
      ${ps.style ? `<div class="pa-row"><span class="pa-k">说话风格</span><span class="pa-v">${esc(ps.style)}</span></div>` : ""}
    </div>
    ${ps.updated_ts ? `<div class="pa-ts">${esc(dayWord(ps.updated_ts))}更新 · 包含 MaiBot 的记忆</div>` : ""}`;
}

function personalBlock(p) {
  const pe = p.personal;
  // 给这个人的构想放在「构想」页（卡片底部带头像），这里只列给 ta 找的资讯
  if (!pe || !(pe.news || []).length) return "";
  const news = (pe.news || []).slice(0, 3);
  return `
    <div class="pers">
      <div class="pers-h">为 ${esc(pname(p))} 推荐${pe.last_ts ? ` · ${esc(dayWord(pe.last_ts))}` : ""}</div>
      ${news
        .map((n) => {
          const src = (n.sources || [])[0];
          return `
        <div class="pers-row">
          <div class="pers-main">
            ${src ? `<a class="pers-title" href="${safeUrl(src.url)}" target="_blank" rel="noopener noreferrer">${esc(n.title)}</a>` : `<span class="pers-title">${esc(n.title)}</span>`}
            <div class="pers-body">${richText(n.body || n.summary || "")}</div>
          </div>
          ${admin() ? `<button class="btn small" data-act="mention-member" data-id="${n.id}" data-name="${esc(pname(p))}">在群内提及</button>` : ""}
        </div>`;
        })
        .join("")}
    </div>`;
}

export function hash(s) {
  let h = 0;
  for (const c of String(s)) h = (h * 31 + c.charCodeAt(0)) | 0;
  return h;
}

// 群指令：/mw 有哪些、谁能用、怎么写（数据来自后端 commands.HELP_ITEMS）；复制整段好转发到群公告
function commandsBlock(v) {
  const c = v && v.commands;
  const items = (c && c.items) || [];
  if (!items.length) return "";
  const groups = [];
  for (const it of items) {
    let g = groups.find((x) => x.who === it.who);
    if (!g) groups.push((g = { who: it.who, list: [] }));
    g.list.push(it);
  }
  return `
    <div class="h-sub-row"><h2 class="h-sub">群指令${tip("在群内直接发送以下指令即可，无需 @ 机器人。")}</h2>${c.text ? `<button class="btn small" data-act="copy" data-what="群指令" data-link="${esc(c.text)}">${SVG.copy}复制</button>` : ""}</div>
    <div class="cmds">${groups
      .map(
        (g) => `<div class="cmds-who">${esc(g.who)}</div>${g.list
          .map(
            (it) => `<div class="cmd-row"><code class="mono">${esc(it.syntax)}</code><span class="cmd-desc">${esc(it.desc)}${
              it.example && it.example !== it.syntax ? `<span class="cmd-eg">示例：<code class="mono">${esc(it.example)}</code></span>` : ""
            }</span></div>`
          )
          .join("")}`
      )
      .join("")}${tipsRows(c.tips)}</div>`;
}

// 不用指令、直接 @ 机器人说的大白话用法（commands.USAGE_TIPS，后端已换好机器人名字）
function tipsRows(tips) {
  const list = Array.isArray(tips) ? tips : [];
  if (!list.length) return "";
  return `<div class="cmds-who">也可以 @ 机器人发送</div>${list
    .map((t) => `<div class="cmd-row"><code class="mono">${esc(t.say)}</code><span class="cmd-desc">${esc(t.desc)}</span></div>`)
    .join("")}`;
}

// 这个群：本群规矩 + 本群做法，只给总管理员和本群群管理员（群友看不到）
function ctxBlock(g) {
  if (!gadmin()) return "";
  return `<h2 class="h-sub">本群 <span class="private">${SVG.lock}仅管理员可见</span></h2>${ctxSection(g.id)}`;
}

export function viewGroup(g, v) {
  const members = g.members ? `${g.members} 人 · ` : "";
  let html = `<h1 class="h-page">${esc(gname(g))}</h1><p class="h-meta">${platTag(g, v)}${members}工作区 <span class="mono">${esc((v && v.workspace) || "")}</span></p>`;
  html += pulseCard(g, v, false);
  if (!v) return html + loading();
  if (g.fresh) {
    html += emptyState("seedling", "正在建立群画像", "") + commandsBlock(v);
    return gadmin() ? html + ctxBlock(g) + controlsSection(g.id, v) + focusSection(v) : html;
  }
  const log = v.topic_log || [];
  html += `<h2 class="h-sub">话题记录</h2>`;
  const today = daySummary(log, (ts) => dayIndex(ts) === dayIndex(now()));
  if (today) html += `<p class="h-meta">${esc(today)}</p>`;
  html += log.length ? stretches(log).map((s, k) => topicItem(s, k)).join("") : `<p class="h-meta">暂无</p>`;
  html += ctxBlock(g);
  html += `<h2 class="h-sub">群画像</h2>`;
  html += profileSection(v);
  html += commandsBlock(v);
  if (gadmin()) html += controlsSection(g.id, v) + groupSpaceBlock(v, g) + focusSection(v);
  return html;
}
