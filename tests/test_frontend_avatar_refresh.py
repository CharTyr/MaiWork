"""Repeated rail refreshes must preserve avatars rather than replay their entrance."""
import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "maiwork/console/static"


def test_unchanged_rail_is_not_rebuilt_and_status_changes_still_render():
    source = (STATIC / "js/render.js").read_text()
    source = "\n".join(line for line in source.splitlines() if not line.startswith("import ")).replace("export ", "")
    setup = r'''
import assert from "node:assert/strict";
const state = {me: {role:"admin"}, groups:[{id:"g1",name:"示例群",avatar:"/avatar.png",today:{pending:0}}], g:"g1", tab:"news"};
const SVG = new Proxy({}, {get:()=>""}), TABS = [{id:"news",label:"资讯"},{id:"tasks",label:"任务"}];
const admin = () => true, gadmin = () => true, isGA = () => false;
const esc = s => String(s ?? ""), ico = () => "", hasUpdate = () => false;
const grp = () => state.groups[0], gname = g => g.name, mq = s => s, platTag = () => "", quiet = () => ({text:"正常"});
const gface = g => `<span class="gav" data-avatar="${g.avatar}"><img src="${g.avatar}"></span>`;
let paints = 0, html = "";
const rail = {get innerHTML(){return html;},set innerHTML(s){html=s;paints++;},querySelectorAll:()=>[]};
const $ = () => rail;
'''
    body = r'''
renderRail(); renderRail();
assert.equal(paints, 1, "相同轮询结果不能重建群头像");
state.groups[0].today.pending = 2; renderRail();
assert.equal(paints, 2, "角标变化仍需更新");
assert.match(html, /r-badge">2/);
state.groups[0].avatar = "/new-avatar.png"; renderRail();
assert.match(html, /new-avatar/);
'''
    result = subprocess.run(["node", "--input-type=module", "-e", setup + source + body], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_group_avatar_has_no_repeated_fade_or_lazy_placeholder():
    css = (STATIC / "style.css").read_text()
    assert "gav-in" not in css
    source = (STATIC / "js/util.js").read_text()
    face = next(line for line in source.splitlines() if "export const gface" in line)
    assert 'loading="lazy"' not in face
