"""构想页前端「先手小样（preview）」渲染与安全过滤测试（Node VM 真跑 ideas.js，零外网）。"""
import json
import shutil
import subprocess
from pathlib import Path

IDEAS = Path(__file__).resolve().parents[1] / "maiwork/console/static/js/pages/ideas.js"
CHECK = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const CFG = %s;
const esc = (s) => String(s ?? '')
  .replaceAll('&', '&amp;')
  .replaceAll('"', '&quot;')
  .replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;')
  .replaceAll("'", '&#39;');
const context = vm.createContext({ console });
const state = { ideaMore: null, ideaOff: {}, detail: null };
const stubs = {
  '../state.js': { TONES: ['#000'], gadmin: () => CFG.admin, state },
  '../util.js': {
    tip: (h) => (h ? '<details class="tip">' + h + '</details>' : ''),
    SVG: new Proxy({}, { get: (_, k) => `[${String(k)}]` }),
    dayWord: () => '今天',
    esc,
    ico: () => '',
  },
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
    modules.set(spec, mod);
    await mod.link(link);
  }
  return modules.get(spec);
}
const mod = new vm.SourceTextModule(fs.readFileSync(CFG.path, 'utf8'), { context });
await mod.link(link);
await mod.evaluate();
console.log(JSON.stringify({
  list: mod.namespace.viewIdeas({ fresh: false }, { ideas: [CFG.idea] }),
  detail: mod.namespace.ideaDetail(CFG.idea),
}));
"""


def render(tmp_path, idea, admin=False):
    node = shutil.which("node")
    assert node, "需要 Node 验证构想页先手小样前端渲染"
    script = tmp_path / "ideas_preview.mjs"
    script.write_text(
        CHECK % json.dumps({"path": str(IDEAS), "idea": idea, "admin": admin}),
        encoding="utf-8",
    )
    r = subprocess.run(
        [node, "--experimental-vm-modules", str(script)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_help_idea_with_verified_preview_renders_badge_points_and_sources(tmp_path):
    idea = {
        "id": 12,
        "kind": "help",
        "title": "整理手柄摇杆漂移自检速查单",
        "body": "把拆壳前能做的固件和校准步骤列成一页纸。",
        "state": "new",
        "items": [{"no": 1, "kind": "task", "title": "出手柄自检单", "desc": ""}],
        "preview": {
            "summary": "官方固件 20.0 后新增了底座与摇杆快速复核入口",
            "points": [
                "先在设置里看无线频段与固件版本，排除蓝牙干扰",
                "校准前记录死区偏移，能区分电位器磨损与排线松动",
            ],
            "sources": [
                {"title": "Nintendo 官方校准说明", "url": "https://support.example.com/joycon"},
            ],
        },
    }
    out = render(tmp_path, idea)
    assert "附先手小样" in out["list"]
    assert "官方固件 20.0 后新增了底座与摇杆快速复核入口" in out["list"]
    assert "先手小样" in out["detail"]
    assert "排除蓝牙干扰" in out["detail"]
    assert "电位器磨损与排线松动" in out["detail"]
    assert 'href="https://support.example.com/joycon"' in out["detail"]
    assert "Nintendo 官方校准说明" in out["detail"]


def test_preview_strips_unsafe_urls_and_escapes_html(tmp_path):
    bad_only = {
        "id": 13,
        "kind": "help",
        "title": "整理速查表",
        "body": "正文",
        "state": "new",
        "items": [{"no": 1, "kind": "task", "title": "做表", "desc": ""}],
        "preview": {
            "summary": "<script>alert(1)</script>",
            "points": ["<b>要点</b>"],
            "sources": [{"title": "坏链接", "url": "javascript:alert(1)"}],
        },
    }
    out_bad = render(tmp_path, bad_only)
    assert "附先手小样" not in out_bad["list"]
    assert "先手小样" not in out_bad["detail"]
    assert "javascript:" not in out_bad["detail"]

    mixed = {
        **bad_only,
        "preview": {
            "summary": "<img src=x onerror=1>结论",
            "points": ["<b>第一条</b>"],
            "sources": [
                {"title": "坏", "url": "javascript:alert(1)"},
                {"title": "<script>源站</script>", "url": "https://docs.example.com/safe"},
            ],
        },
    }
    out_ok = render(tmp_path, mixed)
    assert "<script>" not in out_ok["detail"] and "<img" not in out_ok["list"]
    assert "&lt;b&gt;第一条&lt;/b&gt;" in out_ok["detail"]
    assert "javascript:" not in out_ok["detail"]
    assert 'href="https://docs.example.com/safe"' in out_ok["detail"]


def test_talk_idea_never_renders_preview(tmp_path):
    talk = {
        "id": 14,
        "kind": "talk",
        "title": "聊聊今年最耐玩的小游戏",
        "body": "各说一款",
        "state": "new",
        "items": [],
        "preview": {
            "summary": "不该显示",
            "points": ["不该显示要点"],
            "sources": [{"title": "源", "url": "https://example.com/a"}],
        },
    }
    out = render(tmp_path, talk)
    assert "话题" in out["list"]
    assert "附先手小样" not in out["list"]
    assert "先手小样" not in out["detail"]
