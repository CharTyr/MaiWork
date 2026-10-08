// MaiWork 网页 · 「在做的事」里的持续目标与提醒。
import { state } from "../state.js";
import { dur, esc, ico, now, when } from "../util.js";
import { agentFish } from "../api.js";

export function nextText(goal) {
  if (goal.state === "paused") return "暂停中";
  return goal.next_check_ts ? `${when(goal.next_check_ts)}后检查` : "等新消息";
}

export function goalSections(g, v) {
  let html = "";
  const goals = (v && v.goals) || { agent: [], member: [] };
  const agent = goals.agent || [];
  const member = goals.member || [];
  if (!agent.length && !member.length) {
    return "";
  }
  let i = 0;
  if (agent.length) {
    html += `<h2 class="h-sub h-fish">${agentFish("goal", 28)}<span>正在推进 <small>${agent.length} 个</small></span></h2>`;
    html += agent
      .map((goal) => {
        const crit = goal.criteria || [];
        const done = crit.filter((c) => c.done).length;
        const sel = state.detail && state.detail.id === goal.id ? " selected" : "";
        return `
        <article class="item ruled tap enter${sel}" style="--i:${i++}" data-act="goal" data-id="${esc(goal.id)}" tabindex="0">
          ${ico(goal.icon || "bullseye")}
          <div>
            <h3 class="item-title">${esc(goal.title)}</h3>
            <p class="item-body soft">${esc(goal.body)}</p>
            ${crit.length ? `<div class="progress" aria-label="完成 ${done} / ${crit.length}"><i style="width:${(done / crit.length) * 100}%"></i></div>` : ""}
            <div class="status"><span class="dot ${goal.stale ? "failed" : goal.state === "paused" ? "" : "running"}"></span><span class="status-text">${goal.stale ? `<b class="bad-t">卡在：${esc(goal.stale_reason || "")}</b>` : `${crit.length ? `${done} / ${crit.length} · ` : ""}${esc(nextText(goal))}`}</span></div>
          </div>
        </article>`;
      })
      .join("");
  }
  if (member.length) {
    html += `<h2 class="h-sub">提醒 <small>${member.length} 件</small></h2>`;
    html += member
      .map((m) => {
        const due = m.repeat === "daily" ? "每天" : m.due_ts ? when(m.due_ts) : "没定时间";
        const rem = m.remind_ts ? `${when(m.remind_ts)} 提醒` : "";
        const left = m.until_ts ? ` · 循环还剩 ${dur(m.until_ts - now())}` : "";
        return `
        <article class="item ruled enter" style="--i:${i++}">
          ${ico(m.icon || "alarm")}
          <div>
            <h3 class="item-title"><span class="who">${esc(m.who)}</span> · ${esc(m.title)}</h3>
            <div class="status" style="margin-top:4px"><span class="dot pending"></span><span class="status-text">截止 ${esc(due)}${rem ? ` · ${esc(rem)}` : ""}${esc(left)}</span></div>
          </div>
        </article>`;
      })
      .join("");
  }
  return html;
}
