"""群页「群指令」卡片：列出 /mw 指令（谁能用、写法、例子），一键复制整段帮助发群公告。

数据来自 group_view 的 `commands`（后端 commands.HELP_ITEMS 一处定义）；复制复用通用的
`data-act="copy"`（data-link = 帮助全文，data-what = 群指令）。用 node 真跑 group.js，无网络。
"""
import json
import shutil
import subprocess
from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "maiwork/console/static/js"
GROUP = JS / "pages/group.js"
CHECK = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const CFG = %s;
const esc = (s) => String(s ?? '').replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const context = vm.createContext({ console });
const id = (x) => x;
const stubs = {
  '../state.js': { CATS: [], TONES: ['#eee'], state: { groupSub: 'feed' }, admin: () => CFG.admin, gadmin: () => CFG.admin },
  '../util.js': { tip: (h) => (h ? '<details class="tip">' + h + '</details>' : ''), tipLabel: (f, t, h) => '<label for="' + f + '">' + t + '</label>' + (h ? '<details class="tip">' + h + '</details>' : ''), SVG: new Proxy({}, { get: (_, k) => `[${String(k)}]` }), dayIndex: () => 0, dayWord: () => '今天', dur: () => '', esc,
                  hhmm: () => '', ico: () => '', now: () => 0, richText: esc, safeUrl: id, when: () => '' },
  '../topiclog.js': { daySummary: () => '', judgeText: () => '', stretches: () => [] },
  '../api.js': { gname: (g) => g.name || g.id, gplat: () => 'qq', platTag: () => '' },
  '../pulse.js': { pulseCard: () => '' },
  './news.js': { emptyState: () => '[empty]', loading: () => '[loading]' },
  './groupctx.js': { ctxSection: () => '' },
  './groupcontrols.js': { controlsSection: () => '' },
};
const modules = new Map();
async function link(spec) {
  if (!modules.has(spec)) {
    const exports = stubs[spec];
    if (!exports) throw new Error('unexpected import: ' + spec);
    const mod = new vm.SyntheticModule(Object.keys(exports), function () {
      for (const [k, v] of Object.entries(exports)) this.setExport(k, v);
    }, { context });
    modules.set(spec, mod); await mod.link(link);
  }
  return modules.get(spec);
}
const mod = new vm.SourceTextModule(fs.readFileSync(CFG.path, 'utf8'), { context });
await mod.link(link); await mod.evaluate();
const html = mod.namespace.viewGroup({ id: 'g1', name: '测试群', fresh: !!CFG.fresh }, CFG.v);
console.log(JSON.stringify({ html }));
"""

ITEMS = [
    {"who": "所有人", "syntax": "/mw do 要做的事", "example": "/mw do 整理一份<b>清单</b>", "desc": "直接派活"},
    {"who": "所有人", "syntax": "/mw 帮助", "example": "/mw 帮助", "desc": "看全部指令"},
    {"who": "批准人", "syntax": "/mw 批准 [ID]", "example": "/mw 批准 R-12", "desc": "批准待批的活"},
]
TEXT = "MaiWork 指令\n大家都能用：\n/mw do 要做的事 — 直接派活\n批准人：\n/mw 批准 [ID]"


def run_js(tmp_path, **cfg):
    node = shutil.which("node")
    assert node, "没有 Node，无法验证群页「群指令」卡片"
    assert GROUP.is_file(), "群页前端文件缺失"
    script = tmp_path / "cmds.mjs"
    v = {"topic_log": [], "commands": {"items": ITEMS, "text": TEXT}}
    v.update(cfg.pop("v", {}))
    script.write_text(CHECK % json.dumps({"path": str(GROUP), "admin": False, "v": v, **cfg}), encoding="utf-8")
    r = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])["html"]


def test_card_lists_every_command_grouped_by_who(tmp_path):
    html = run_js(tmp_path)
    assert "群指令" in html
    for it in ITEMS:
        assert it["syntax"] in html
    assert "直接派活" in html and "批准待批的活" in html
    # 谁能用分组写出来
    assert "所有人" in html and "批准人" in html
    # 例子要转义
    assert "<b>清单</b>" not in html and "&lt;b&gt;清单&lt;/b&gt;" in html


def test_copy_button_reuses_generic_copy_with_full_help_text(tmp_path):
    html = run_js(tmp_path)
    assert 'data-act="copy"' in html
    assert 'data-what="群指令"' in html
    assert f'data-link="{TEXT}"' in html  # 只转义 & " < >，换行原样留着


def test_card_shown_to_everyone_and_on_fresh_group(tmp_path):
    assert "群指令" in run_js(tmp_path, admin=False)
    assert "群指令" in run_js(tmp_path, admin=True)
    assert "群指令" in run_js(tmp_path, fresh=True)


def test_no_commands_data_no_card(tmp_path):
    html = run_js(tmp_path, v={"commands": None})
    assert "群指令" not in html
