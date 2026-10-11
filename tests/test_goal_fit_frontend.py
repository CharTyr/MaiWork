"""真实 JS：目标贴合群聊（.trellis/tasks/10-11-goals-fit-group-chat）的网页部分。

- 没留依据的旧勾是「待确认」：不算进完成数，群管理员能「算数 / 撤掉」，群友只看到待确认；
- 目标判断 fit=human：卡片和详情都提示「要群友自己动手，MaiWork 盯不了」；
- 待批的目标请求带判断：human+替代 → 「按任务批准」；human 无替代 → 没有批准按钮；
- 群指令卡多一段「也可以直接 @ 机器人说」。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[1] / "maiwork/console/static/js"
CHECK = r"""
import fs from 'node:fs'; import path from 'node:path'; import vm from 'node:vm';
const cfg = %s;
const esc = (s) => String(s ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const role = cfg.role || 'admin';
const state = {me: {role, bot: {name: 'MaiBot'}}, g: 'g1', groups: [{id: 'g1'}], detail: cfg.detail || null, tasks: {}, filter: 'all'};
const view = cfg.view || {};
const defaults = {state, esc, SVG: {check: '<i data-tick></i>', copy: '', lock: ''}, DOT: {}, STATUS: {},
  admin: () => role === 'admin', gadmin: () => ['admin', 'group_admin'].includes(role), desktop: {matches: true},
  dur: () => '1天', when: () => '昨天', now: () => 100, msText: () => '', safeUrl: (u) => u,
  ico: () => '<i></i>', agentFish: () => '<svg></svg>', emptyState: (i, t, b) => `<p>${t}</p>`,
  findIdea: () => null, ideaItems: () => [], ideaDetail: () => '', gview: () => view, FILTERS: [{id: 'all', label: '全部', match: () => true}],
  loading: () => '', taskRow: () => '', sheetChrome: () => '', renderSide: () => {}, api: async () => ({}),
  ctxSection: () => '', grp: () => ({id: 'g1'}), $: () => null};
const real = new Set(cfg.real);
const context = vm.createContext({console, Date, URLSearchParams, document: {addEventListener() {}}});
const mods = new Map();
async function load(file) {
  if (mods.has(file)) return mods.get(file);
  const src = fs.readFileSync(file, 'utf8');
  let m;
  if (real.has(path.relative(cfg.root, file))) {
    m = new vm.SourceTextModule(src, {context, identifier: file});
    mods.set(file, m);
    await m.link((s, p) => load(path.resolve(path.dirname(p.identifier), s)));
  } else {
    const names = [...new Set([...src.matchAll(/^export\s+(?:async\s+)?(?:function|const|let|class)\s+([A-Za-z_$][A-Za-z0-9_$]*)/gm)].map((x) => x[1]))];
    m = new vm.SyntheticModule(names, function () { for (const n of names) this.setExport(n, defaults[n] ?? (() => '')); }, {context});
    mods.set(file, m);
    await m.link(() => {});
  }
  return m;
}
const m = await load(path.join(cfg.root, cfg.real[0]));
await m.evaluate();
const fn = m.namespace[cfg.call];
const html = cfg.call === 'detailHTML' ? fn() : fn({id: 'g1'}, view);
console.log(JSON.stringify({html}));
"""


def _run(tmp_path, root, real, call, **cfg):
    node = shutil.which("node")
    assert node, "没有 Node，不能验证目标网页"
    assert Path(root).is_dir(), "真实前端目录缺失"
    f = tmp_path / "goalfit.mjs"
    f.write_text(CHECK % json.dumps({"root": str(root), "real": real, "call": call, **cfg}), encoding="utf-8")
    r = subprocess.run([node, "--experimental-vm-modules", str(f)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])["html"]


def render(tmp_path, real, call, **cfg):
    return _run(tmp_path, JS, real, call, **cfg)


def _goal(**kw):
    g = {
        "id": "G-1", "title": "分线攻略", "body": "说明", "state": "active", "next_check_ts": 200,
        "criteria": [
            {"text": "群投票定线", "done": True, "pending": True, "backed": False},
            {"text": "整理 mod 清单", "done": True, "pending": False, "backed": True, "task_id": "T-3", "ts": 50},
            {"text": "汇总攻略", "done": False},
        ],
    }
    g.update(kw)
    return g


DETAIL = ["detail.js", "pages/goals.js"]
WORK = ["pages/tasks.js", "pages/goals.js"]


def test_goal_card_counts_only_backed_ticks(tmp_path):
    html = render(tmp_path, WORK, "viewTasks", view={"goals": {"agent": [_goal()]}})
    assert "1 / 3" in html and "2 / 3" not in html
    assert "1 条待确认" in html
    assert "width:33.33" in html  # 进度条也只按有依据的算


@pytest.mark.parametrize("role", ["admin", "group_admin"])
def test_admin_sees_confirm_and_revoke_on_pending_tick(tmp_path, role):
    html = render(tmp_path, DETAIL, "detailHTML", role=role,
                  detail={"type": "goal", "id": "G-1"}, view={"goals": {"agent": [_goal()]}})
    assert "待确认" in html and "先不算完成" in html
    assert 'data-op="confirm" data-id="G-1" data-i="0"' in html
    assert 'data-op="revoke" data-id="G-1" data-i="0"' in html
    assert 'data-i="1"' not in html  # 有依据的那条不出按钮
    assert "1 / 3" in html


def test_member_sees_pending_without_buttons(tmp_path):
    html = render(tmp_path, DETAIL, "detailHTML", role="member",
                  detail={"type": "goal", "id": "G-1"}, view={"goals": {"agent": [_goal()]}})
    assert "待确认" in html
    assert 'data-op="confirm"' not in html and 'data-op="revoke"' not in html


def test_finished_goal_has_no_confirm_buttons(tmp_path):
    html = render(tmp_path, DETAIL, "detailHTML", role="admin",
                  detail={"type": "goal", "id": "G-1"}, view={"goals": {"agent": [_goal(state="cancelled")]}})
    assert 'data-op="confirm"' not in html


def test_old_api_without_pending_field_falls_back_to_proof_fields(tmp_path):
    g = _goal(criteria=[{"text": "旧勾", "done": True}, {"text": "新勾", "done": True, "evidence": "群里说了"}])
    html = render(tmp_path, WORK, "viewTasks", view={"goals": {"agent": [g]}})
    assert "1 / 2" in html and "1 条待确认" in html


def test_human_fit_goal_is_flagged(tmp_path):
    g = _goal(fit="human", fit_reason="要大家自己开档截图")
    card = render(tmp_path, WORK, "viewTasks", view={"goals": {"agent": [g]}})
    assert "要群友自己动手，MaiWork 盯不了" in card
    detail = render(tmp_path, DETAIL, "detailHTML", role="admin",
                    detail={"type": "goal", "id": "G-1"}, view={"goals": {"agent": [g]}})
    assert "要群友自己动手，MaiWork 盯不了：要大家自己开档截图" in detail
    assert "不做了" in detail


def test_online_fit_goal_not_flagged(tmp_path):
    card = render(tmp_path, WORK, "viewTasks", view={"goals": {"agent": [_goal(fit="online")]}})
    assert "盯不了" not in card


def _pending(fit):
    return {"tasks": {"pending": [{"id": "R-30", "title": "盯着打卡", "age_s": 10, "goal_fit": fit}], "list": []}}


def test_pending_goal_human_with_alt_approves_as_task(tmp_path):
    html = render(tmp_path, WORK, "viewTasks", role="admin",
                  view=_pending({"kind": "human", "reason": "要群友开档", "alt_title": "整理 mod 清单"}))
    assert "要群友自己动手，MaiWork 盯不了：要群友开档" in html
    assert "批准后改成一次性任务开工：整理 mod 清单" in html
    assert ">按任务批准<" in html and 'data-op="reject"' in html


def test_pending_goal_human_without_alt_cannot_be_approved(tmp_path):
    html = render(tmp_path, WORK, "viewTasks", role="admin", view=_pending({"kind": "human", "reason": "要到场"}))
    assert "只能不批" in html
    assert 'data-op="approve"' not in html and 'data-op="reject"' in html


@pytest.mark.parametrize("fit,text", [
    ({"kind": "online", "reason": "查官网更新"}, "能在网上盯：查官网更新"),
    ({"kind": "error"}, "没判断出能不能盯"),
])
def test_pending_goal_online_or_error_approves_normally(tmp_path, fit, text):
    html = render(tmp_path, WORK, "viewTasks", role="admin", view=_pending(fit))
    assert text in html and ">批准<" in html


def test_pending_without_fit_unchanged(tmp_path):
    html = render(tmp_path, WORK, "viewTasks", role="admin", view=_pending(None))
    assert ">批准<" in html and "盯不了" not in html and "能在网上盯" not in html


def test_command_card_shows_at_bot_tips(tmp_path):
    view = {"commands": {"items": [{"who": "所有人", "syntax": "/mw", "example": "", "desc": "看进度"}],
                         "tips": [{"say": "@MaiBot 提醒我 周五晚八点 开黑", "desc": "到点 @ 你"}],
                         "text": "x"}}
    # group.js 依赖多，只渲染指令卡这一段：复制一份前端，给 commandsBlock 临时加个导出
    patched = tmp_path / "js"
    shutil.copytree(JS, patched)
    gp = patched / "pages/group.js"
    gp.write_text(gp.read_text(encoding="utf-8") + "\nexport function __cmds(g, v) { return commandsBlock(v); }\n",
                  encoding="utf-8")
    html = _run(tmp_path, patched, ["pages/group.js"], "__cmds", view=view)
    assert "也可以直接 @ 机器人说" in html
    assert "@MaiBot 提醒我 周五晚八点 开黑" in html and "到点 @ 你" in html


def test_confirm_buttons_have_touch_target():
    css = (JS.parent / "style.css").read_text(encoding="utf-8")
    assert ".checks .ck-acts .link-btn { min-height: 44px;" in css


def test_goal_op_posts_criterion_route():
    src = (JS / "actions.js").read_text(encoding="utf-8")
    assert "/criteria/${encodeURIComponent(el.dataset.i)}" in src
