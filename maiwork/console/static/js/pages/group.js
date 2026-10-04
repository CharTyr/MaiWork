// MaiWork 网页 · 「群」页：话题、这个群（本群规矩 / 本群做法 / 群画像）、关注成员、群空间。
import { CATS, TONES, admin, gadmin } from "../state.js";
import { SVG, dayIndex, dayWord, dur, esc, hhmm, ico, now, richText, safeUrl, when } from "../util.js";
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
  const times = s.n > 1 ? `这段判了 ${s.n} 次${s.bestOk != null && !opened ? `，适合开最高 ${s.bestOk.toFixed(2)}` : ""}。` : "";
  const r = t.result;
  return `
    <article class="item ruled enter" style="--i:${i}">
      ${ico(opened ? "speech" : "hourglass")}
      <div>
        <h3 class="item-title">${esc(span)} · ${opened ? "开了话题" : "忍住了没开"}</h3>
        <p class="item-body soft">安静了 ${esc(dur(t.quiet_s))}${t.usual_gap_s ? `，平时这个点 ${esc(dur(t.usual_gap_s))}一条` : ""}。${esc(times)}Jev：${esc(judgeText(t))}${j && j.detail ? `（${esc(j.detail)}）` : ""}。${t.pick && t.pick.title && opened ? `挑的是「${esc(t.pick.title)}」。` : ""}</p>
        ${opened ? `<div class="opener">${esc(t.opener)}</div>` : ""}
        ${opened ? `<div class="status"><span class="dot ${r && r.replies ? "used" : ""}"></span><span class="status-text">${r ? (r.replies ? `${r.replies} 人接话${r.followups ? `，MaiBot 接着聊了 ${r.followups} 句` : ""}` : "10 分钟内没人接，下次隔久一点") : "等着看有没有人接"}</span></div>` : ""}
        ${
          gadmin()
            ? `<div class="verdict">这次判断对吗？
          <button class="btn" data-act="verdict" data-id="${t.id}" data-v="right" aria-pressed="${t.verdict === "right"}">对</button>
          <button class="btn" data-act="verdict" data-id="${t.id}" data-v="wrong" aria-pressed="${t.verdict === "wrong"}">不对</button>
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
        <div class="pf-name">${esc(sec.name || name)}${gadmin() ? `<button class="pf-add" data-act="pf-add" data-cat="${cat}" aria-label="加一条">${SVG.plus}</button>` : ""}</div>
        ${
          entries.length
            ? entries
                .map((e) => {
                  const meta = e.locked
                    ? gadmin()
                      ? e.source === "admin"
                        ? "你加的 · 不会被改写"
                        : "已锁定 · 不会被改写"
                      : "管理员确认过"
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
            : `<div class="pf pf-none"><div class="pf-meta">还没有</div></div>`
        }
      </div>`;
  }).join("");
}

function groupSpaceBlock(v, g) {
  const plat = gplat(g, v);
  if (plat !== "qq") {
    const who = plat === "telegram" ? "Telegram 群" : "QQ 官方机器人所在的群";
    return `<h2 class="h-sub">群空间</h2><p class="h-meta">${who}没有群文件、群公告和群相册，成品会用网页链接交付</p>`;
  }
  const gs = v && v.group_space;
  if (!gs) return "";
  const role = { owner: "群主", admin: "管理员", member: "普通成员" }[gs.role] || "身份未知";
  const cap = [
    ["files_list", "看群文件"],
    ["files_manage", "整理自己传的文件"],
    ["notice_send", "发群公告"],
    ["album_upload", "传群相册"],
  ];
  const any = cap.some(([k]) => gs[k]);
  return `
    <h2 class="h-sub">群空间 <small>${esc(role)}</small></h2>
    ${
      any
        ? `<div class="caps">${cap.map(([k, n]) => `<span class="cap ${gs[k] ? "on" : ""}">${gs[k] ? SVG.check : ""}${n}</span>`).join("")}</div>`
        : `<p class="h-meta">暂时用不了</p>`
    }`;
}

function focusSection(v) {
  const focus = (v && v.focus) || [];
  let html = `<div class="h-sub-row"><h2 class="h-sub">关注成员 <span class="private">${SVG.lock}只有管理员看得到</span></h2><button class="btn small" data-act="focus-add">加一个人</button></div>`;
  if (!focus.length) return html + `<p class="h-meta">还没有</p>`;
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
        ${admin() ? `<button class="icon-btn" data-act="focus-rm" data-uid="${esc(p.user_id)}" aria-label="不再关注">${SVG.close}</button>` : ""}
      </div>`
      )
      .join("")
  );
}

// 关注成员显示名：群名片（QQ昵称）；不回落成 QQ 号
const pname = (p) => String(p.display_name || p.name || "群友");

function personaBlock(p) {
  const ps = p.persona;
  if (!ps) return `<div class="person-note">${esc(p.note || "还没攒够了解。")}</div>`;
  const list = (label, arr) =>
    arr && arr.length ? `<div class="pa-row"><span class="pa-k">${label}</span><span class="pa-v">${arr.map(esc).join("、")}</span></div>` : "";
  return `
    <div class="person-note">${esc(ps.summary || p.note || "")}</div>
    <div class="pa">
      ${list("在做", ps.doing)}${list("关心", ps.cares)}${list("提过", ps.asked)}
      ${ps.style ? `<div class="pa-row"><span class="pa-k">说话</span><span class="pa-v">${esc(ps.style)}</span></div>` : ""}
    </div>
    ${ps.updated_ts ? `<div class="pa-ts">${esc(dayWord(ps.updated_ts))}更新 · 结合了 MaiBot 的长期记忆</div>` : ""}`;
}

function personalBlock(p) {
  const pe = p.personal;
  // 给这个人的构想放在「构想」页（卡片底部带头像），这里只列给 ta 找的资讯
  if (!pe || !(pe.news || []).length) return "";
  const news = (pe.news || []).slice(0, 3);
  return `
    <div class="pers">
      <div class="pers-h">给 ${esc(pname(p))} 找的${pe.last_ts ? ` · ${esc(dayWord(pe.last_ts))}` : ""}</div>
      ${news
        .map((n) => {
          const src = (n.sources || [])[0];
          return `
        <div class="pers-row">
          <div class="pers-main">
            ${src ? `<a class="pers-title" href="${safeUrl(src.url)}" target="_blank" rel="noopener noreferrer">${esc(n.title)}</a>` : `<span class="pers-title">${esc(n.title)}</span>`}
            <div class="pers-body">${richText(n.body || n.summary || "")}</div>
          </div>
          ${admin() ? `<button class="btn small" data-act="mention-member" data-id="${n.id}" data-name="${esc(pname(p))}">在群里提给 ta</button>` : ""}
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

// 这个群：本群规矩 + 本群做法，只给总管理员和本群群管理员（群友看不到）
function ctxBlock(g) {
  if (!gadmin()) return "";
  return `<h2 class="h-sub">这个群 <span class="private">${SVG.lock}只有管理员看得到</span></h2>${ctxSection(g.id)}`;
}

export function viewGroup(g, v) {
  const members = g.members ? `${g.members} 人 · ` : "";
  let html = `<h1 class="h-page">${esc(gname(g))}</h1><p class="h-meta">${platTag(g, v)}${members}工作区 <span class="mono">${esc((v && v.workspace) || "")}</span></p>`;
  html += pulseCard(g, v, false);
  if (!v) return html + loading();
  if (g.fresh) {
    html += emptyState("seedling", "还在熟悉这个群", "");
    return gadmin() ? html + ctxBlock(g) + controlsSection(g.id, v) + focusSection(v) : html;
  }
  const log = v.topic_log || [];
  html += `<h2 class="h-sub">开话题记录 </h2>`;
  const today = daySummary(log, (ts) => dayIndex(ts) === dayIndex(now()));
  if (today) html += `<p class="h-meta">${esc(today)}</p>`;
  html += log.length ? stretches(log).map((s, k) => topicItem(s, k)).join("") : `<p class="h-meta">还没有</p>`;
  html += ctxBlock(g);
  html += `<h2 class="h-sub">群画像 </h2>`;
  html += profileSection(v);
  if (gadmin()) html += controlsSection(g.id, v) + groupSpaceBlock(v, g) + focusSection(v);
  return html;
}
