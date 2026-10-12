"""构想页：话题类构想（2026-10-11 用户拍板「先有真实需要，再有构想」，docs/18）。

- kind=talk 的是话题：列表和详情标「话题」；不需要 MaiWork 动手 → 详情主按钮是「交给我的 agent」，
  不出「复制要求」「直接开工」和「复制后到群里 @MaiBot」，提示 MaiBot 冷场时会提起；
- kind=help（帮一步）和老构想（kind 为空）一切照旧，不标类型。
用 node 真跑 ideas.js，无网络。
"""
import json
import shutil
import subprocess
from pathlib import Path

IDEAS = Path(__file__).resolve().parents[1] / "maiwork/console/static/js/pages/ideas.js"
CHECK = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const CFG = %s;
const esc = (s) => String(s ?? '').replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const context = vm.createContext({ console });
const state = { ideaMore: CFG.more ? CFG.idea.id : null, ideaOff: {} };
const stubs = {
  '../state.js': { TONES: ['#000'], gadmin: () => CFG.admin, state },
  '../util.js': { tip: (h) => (h ? '<details class="tip">' + h + '</details>' : ''), tipLabel: (f, t, h) => '<label for="' + f + '">' + t + '</label>' + (h ? '<details class="tip">' + h + '</details>' : ''), SVG: new Proxy({}, { get: (_, k) => `[${String(k)}]` }), dayWord: () => '今天', esc, ico: () => '' },
  '../api.js': { agentFish: () => '', gview: () => null },
  './news.js': { emptyState: () => '<p>EMPTY</p>', fbButtons: () => '' },
  './group.js': { hash: () => 0 },
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
console.log(JSON.stringify({
  list: mod.namespace.viewIdeas({ fresh: false }, { ideas: [CFG.idea] }),
  detail: mod.namespace.ideaDetail(CFG.idea),
}));
"""

PURE = {"id": 7, "kind": "talk", "title": "来一场手柄复活大会", "body": "各带一个坏手柄，看谁修得活", "state": "new", "items": []}
WITH_HELP = {**PURE, "id": 8, "kind": "help", "items": [{"no": 1, "kind": "task", "title": "出一份修法速查", "desc": ""}]}
LEGACY = {"id": 9, "kind": "", "title": "我可以帮你整理一份清单", "body": "b", "state": "new", "items": []}


def render(tmp_path, idea, admin=True, more=True):
    node = shutil.which("node")
    assert node, "没有 Node，无法验证构想页"
    script = tmp_path / "ideas.mjs"
    script.write_text(CHECK % json.dumps({"path": str(IDEAS), "idea": idea, "admin": admin, "more": more}), encoding="utf-8")
    r = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_talk_label_in_list_and_detail(tmp_path):
    out = render(tmp_path, PURE)
    assert 'idea-kind">话题<' in out["list"]
    assert 'idea-kind">话题<' in out["detail"]


def test_help_unknown_or_legacy_kind_has_no_label(tmp_path):
    for idea in (WITH_HELP, LEGACY, {**PURE, "kind": "weird"}):
        assert "idea-kind" not in render(tmp_path, idea)["list"]


def test_talk_idea_leads_with_handoff_not_do(tmp_path):
    d = render(tmp_path, PURE)["detail"]
    assert 'class="btn primary idea-go" data-act="handoff" data-kind="idea" data-id="7"' in d
    assert "交给我的 agent" in d
    assert 'data-act="idea-copy"' not in d
    assert 'data-op="do"' not in d
    assert "复制后在群内" not in d
    assert "这是一个话题" in d and "冷场" in d
    # 收起还在（管理员）
    assert 'data-op="dismiss"' in d
    # 底下那个小链接不重复出现
    assert d.count("交给我的 agent") == 1


def test_idea_with_help_and_legacy_keep_old_buttons(tmp_path):
    for idea in (WITH_HELP, LEGACY):
        d = render(tmp_path, idea)["detail"]
        assert 'data-act="idea-copy"' in d
        assert 'data-op="do"' in d
        assert "复制后在群内" in d
        assert "这是一个话题" not in d
        assert 'data-act="handoff"' in d  # 小链接照旧
