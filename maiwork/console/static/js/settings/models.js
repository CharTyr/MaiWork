// MaiWork 网页 · 设置 ·「模型」。
import { state } from "../state.js";
import { esc, ico, when } from "../util.js";

export function feedsSettings(f, page) {
  if (!f) return "";
  const manual = f.blocked_domains || [];
  const auto = f.auto_blocked || [];
  return `
    ${page ? `` : `<h2 class="h-sub">屏蔽的资讯来源</h2>`}
    ${
      manual.length || auto.length
        ? [
            ...manual.map((d) => `<div class="set-row">${ico("lock")}<div><div class="set-name mono">${esc(d)}</div><div class="set-text">你屏蔽的</div></div><button class="btn small" data-act="unblock-domain" data-domain="${esc(d)}">解除</button></div>`),
            ...auto.map((d) => `<div class="set-row">${ico("lock")}<div><div class="set-name mono">${esc(d)}</div><div class="set-text">自动屏蔽</div></div><span></span></div>`),
          ].join("")
        : `<p class="h-meta">还没有</p>`
    }`;
}

export const fullLink = (g) => (g.link && /^https?:/.test(g.link) ? g.link : `${location.origin}/#/${g.token}/news`);

function modelsSummary(m, page) {
  m = m || {};
  const backup = (b) => (b ? `备用 ${esc(b)}` : "没设备用");
  const row = (icon, name, text) => `<div class="set-row">${ico(icon)}<div><div class="set-name">${name}</div><div class="set-text">${text}</div></div><span></span></div>`;
  return `
    ${page ? "" : `<div class="h-sub-row"><h2 class="h-sub">模型</h2><button class="btn small" data-act="models">修改</button></div>`}
    ${m.ready ? "" : `<div class="warn-box">还没配好模型，MaiWork 暂时不会工作</div>`}
    ${row("link", "端点", `<span class="mono">${esc(m.base_url) || "没填"}</span> · 密钥${m.key_set ? "已填" : "<b>没填</b>"}`)}
    ${row("robot", "主模型", `${m.main ? `<span class="mono">${esc(m.main)}</span>` : "<b>没选</b>"} · ${backup(m.main_backup)}<br>负责思考和安排`)}
    ${row("tools", "子 agent 模型", `${m.worker ? `<span class="mono">${esc(m.worker)}</span>` : "<b>没选</b>"} · ${backup(m.worker_backup)}<br>负责动手干活`)}
    ${row("sparkles", "Jev", `快速判断群消息 · 密钥${m.jev_key_set === false ? "<b>没填</b>" : m.jev_key_set ? "已填" : "在「全部配置」里"} <button type="button" class="link-btn" data-act="cfg-goto" data-s="jev">去填 Jev 密钥</button>`)}`;
}

export const draft = { models: null }; // 模型表单里临时拉到的列表

function modelSelect(id, value, list, allowEmpty) {
  const opts = (allowEmpty ? [`<option value="">不用备用</option>`] : [`<option value="" disabled ${value ? "" : "selected"}>选一个模型</option>`]).concat(
    list.map((x) => `<option value="${esc(x)}" ${x === value ? "selected" : ""}>${esc(x)}</option>`)
  );
  // 列表里没有的旧值也保留，避免端点换了以后悄悄丢掉
  if (value && !list.includes(value)) opts.push(`<option value="${esc(value)}" selected>${esc(value)}（端点里没找到）</option>`);
  return `<select id="${id}" name="${id}">${opts.join("")}</select>`;
}

export function modelsPage() {
  return modelsSummary((state.settings || {}).models, true) + modelsForm();
}

function modelsForm() {
  const m = (state.settings && state.settings.models) || {};
  const list = draft.models || m.available || [];
  const checked = draft.models ? `刚刚测过 · 找到 ${draft.models.length} 个模型` : m.checked_at ? `${when(m.checked_at)} 测过 · 找到 ${(m.available || []).length} 个模型` : "";
  return `
    <h2 class="h-sub">修改</h2>
    <p class="h-meta sheet-lead">填一个 OpenAI 兼容的地址，再选模型</p>
    <form id="models" class="login" autocomplete="off">
      <label for="m-url">端点地址</label>
      <input id="m-url" name="m-url" type="url" inputmode="url" spellcheck="false" value="${esc(m.base_url || "")}" placeholder="https://…/v1" />
      <label for="m-key" style="margin-top:6px">API 密钥</label>
      <input id="m-key" name="m-key" type="password" autocomplete="new-password" placeholder="${m.key_set ? "已填写 · 留空就不改" : "粘贴密钥"}" />
      
      <div class="actions" style="margin-top:4px"><button class="btn" type="button" data-act="models-test">测试连接</button><span class="fine" id="m-check" style="margin:0;align-self:center">${esc(checked)}</span></div>

      <h2 class="h-sub">主模型</h2>
      <p class="fine" style="margin:0 0 4px">负责思考和安排，选聪明的</p>
      ${modelSelect("m-main", m.main, list)}
      <label for="m-main-b">出错时换用</label>
      ${modelSelect("m-main-b", m.main_backup, list, true)}

      <h2 class="h-sub">子 agent 模型</h2>
      <p class="fine" style="margin:0 0 4px">负责动手干活，选便宜耐用的</p>
      ${modelSelect("m-worker", m.worker, list)}
      <label for="m-worker-b">出错时换用</label>
      ${modelSelect("m-worker-b", m.worker_backup, list, true)}

      <h2 class="h-sub">失败重试</h2>
      <p class="fine" style="margin:0 0 4px">出错时隔一会儿再试，还不行就换备用模型</p>
      <div class="two-col">
        <div><label for="m-retries">最多重试几次</label><input id="m-retries" type="number" min="0" max="10" step="1" value="${esc(m.retries ?? 5)}" /></div>
        <div><label for="m-retry-delay">每次间隔（秒）</label><input id="m-retry-delay" type="number" min="1" max="60" step="1" value="${esc(m.retry_delay_s ?? 10)}" /></div>
      </div>

      <h2 class="h-sub">请求频率</h2>
      <p class="fine" style="margin:0 0 4px">经常提示请求太多，就调小一点</p>
      <div class="two-col">
        <div><label for="m-conc">同时最多几个请求</label><input id="m-conc" type="number" min="1" max="8" step="1" value="${esc(m.max_concurrency ?? 2)}" /></div>
        <div><label for="m-rpm">每分钟最多（0 = 不限）</label><input id="m-rpm" type="number" min="0" max="600" step="1" value="${esc(m.max_rpm ?? 0)}" /></div>
      </div>
      <h2 class="h-sub">上下文长度</h2>
      <p class="fine" style="margin:0 0 4px">填模型能记住的最大长度（tokens）。对话快满时，MaiWork 会把前面的内容整理成摘要再接着做</p>
      <div class="two-col">
        <div><label for="m-ctx">上下文长度（tokens）</label><input id="m-ctx" type="number" min="8192" max="2000000" step="1024" value="${esc(m.context_window ?? 128000)}" /></div>
        <div></div>
      </div>
      <p class="err" id="m-err" hidden></p>
      <button class="btn primary wide" type="submit">保存</button>
      
    </form>`;
}
