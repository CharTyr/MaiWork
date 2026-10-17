"""设置快速导航使用实际模板；浏览器交互另行检查。"""
import subprocess
from pathlib import Path

JS = Path(__file__).resolve().parent.parent / "maiwork/console/static/js"


def test_vertical_picker_in_settings_and_config_sections():
    files = [JS / "settings/navigation.js", JS / "settings/index.js", JS / "settings/rules.js"]
    assert files[0].is_file(), "缺少竖向分栏选择器"
    source = "\n".join(p.read_text() for p in files)
    source = "\n".join(line for line in source.splitlines() if not line.startswith("import ")).replace("export ", "")
    setup = r'''
import assert from "node:assert/strict";
const state = {settings: null, setSub: "social", rules: {sections: [
  {id: "feeds", label: "资讯", fields: []}, {id: "tasks", label: "任务", fields: []}
]}, cfgSec: "tasks"};
const esc = s => String(s ?? "").replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;");
const loading = () => "加载中";
'''
    body = r'''
const settings = settingsPage();
assert.doesNotMatch(settings, /class="section-picker"/); // 分栏圆按钮只在「全部设置」页
assert.match(settings, /data-act="set-sub" data-sub="social" aria-selected="true"/);
assert.doesNotMatch(settings, /data-sub="social-login"|class="set-picker"/);
assert.match(settings, /class="set-nav"/); // 保留原分栏
const config = rulesPage();
assert.match(config, /class="section-tabs">\s*<details class="section-picker">/);
assert.match(config, /summary aria-label="全部配置分类"/);
assert.match(config, /data-act="cfg-sec" data-s="tasks" aria-current="page"/);
assert.match(config, /data-act="cfg-sec" data-s=""/); // 可返回全部
assert.match(config, /id="cfg-tasks"/);
assert.doesNotMatch(config, /id="cfg-feeds"/);
const escaped = sectionPicker([["bad\"", "<script>"]], "bad\"", true);
assert.ok(escaped.includes("bad&quot;"));
assert.ok(escaped.includes("&lt;script>"));
assert.doesNotMatch(escaped, /<script>/);
'''
    result = subprocess.run(["node", "--input-type=module", "-e", setup + source + body], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
