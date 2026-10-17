"""网页「ⓘ」说明提示：说明文字默认收起，点图标才看（util.js 的 tip() / tipLabel()）。

运行真实 util.js（无浏览器），只校验生成的结构；开关行为由原生 <details> 负责，
「点外面 / 按 Esc 关掉、同时只开一个、靠右时不出屏」在 events.js 里注册。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "maiwork/console/static"
JS = ROOT / "js"
assert JS.is_dir(), f"找不到网页脚本目录：{JS}"

CHECK = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(%s, 'utf8');
const context = vm.createContext({ console, window: { matchMedia: () => ({ matches: false }) } });
const stubs = { './state.js': { $: () => null, state: { skew: 0 } }, './sound.js': { sfx: () => {} } };
async function link(spec) {
  const exports = stubs[spec];
  if (!exports) throw new Error('unexpected import: ' + spec);
  const mod = new vm.SyntheticModule(Object.keys(exports), function () {
    for (const [k, v] of Object.entries(exports)) this.setExport(k, v);
  }, { context });
  await mod.link(link);
  return mod;
}
const mod = new vm.SourceTextModule(source, { context });
await mod.link(link); await mod.evaluate();
const { tip, tipLabel } = mod.namespace;
console.log(JSON.stringify({
  plain: tip('选个便宜快的模型即可'),
  empty: tip(''),
  label: tipLabel('qj-model', '判断模型', '用于判断群消息是否为任务请求'),
  labelNoHelp: tipLabel('qj-model', '判断模型', ''),
}));
"""


def _run(tmp_path):
    node = shutil.which("node")
    assert node, "没有 Node，无法验证说明提示"
    script = tmp_path / "tip.mjs"
    script.write_text(CHECK % json.dumps(str(JS / "util.js")), encoding="utf-8")
    r = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_tip_is_collapsed_details_with_named_icon(tmp_path):
    out = _run(tmp_path)
    t = out["plain"]
    assert t.startswith('<details class="tip"') and " open" not in t.split(">")[0]
    summary = t.split("<summary", 1)[1].split("</summary>")[0]
    assert 'aria-label="说明"' in summary and "<svg" in summary
    assert "选个便宜快的模型即可" in t.split("</summary>", 1)[1]
    assert out["empty"] == ""


def test_tip_label_keeps_label_bound_to_input(tmp_path):
    out = _run(tmp_path)
    lab = out["label"]
    assert '<label for="qj-model">判断模型</label>' in lab
    # 提示不能塞进 <label> 里（点图标会把焦点抢到输入框）
    assert lab.index("</label>") < lab.index('<details class="tip"')
    assert "用于判断群消息是否为任务请求" in lab
    assert out["labelNoHelp"] == '<label for="qj-model">判断模型</label>'


def test_events_close_tips_on_outside_click_and_escape():
    src = (JS / "events.js").read_text(encoding="utf-8")
    assert "details.tip" in src
    assert "Escape" in src and "toggle" in src


def test_tip_styles_exist():
    css = (ROOT / "style.css").read_text(encoding="utf-8")
    for sel in (".tip ", ".tip > summary", ".tip-pop", ".lbl-row"):
        assert sel in css, sel


def test_tip_never_nested_inside_summary():
    """ⓘ 提示本身是 <details>：放进别的 <summary> 里，点它会连带展开 / 收起外层。"""
    js = Path(__file__).resolve().parents[1] / "maiwork" / "console" / "static" / "js"
    bad = []
    for f in sorted(js.rglob("*.js")):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for part in re.findall(r"<summary\b[^>]*>(.*?)</summary>", line):
                if "${tip(" in part:
                    bad.append(f"{f.relative_to(js)}:{i}")
    assert not bad, f"这些 ⓘ 提示放在了 <summary> 里：{bad}"


def test_tip_never_inside_paragraph():
    """<details> 不能放进 <p>：浏览器会提前结束段落，ⓘ 掉到下一行孤零零一个。"""
    js = Path(__file__).resolve().parents[1] / "maiwork" / "console" / "static" / "js"
    bad = []
    for f in sorted(js.rglob("*.js")):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for part in re.findall(r"<p\b[^>]*>(.*?)</p>", line):
                if "${tip(" in part:
                    bad.append(f"{f.relative_to(js)}:{i}")
    assert not bad, f"这些 ⓘ 提示放在了 <p> 里：{bad}"
