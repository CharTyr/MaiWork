"""Verify hover & click animations and in-place DOM preservation for main navigation buttons."""
import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "maiwork/console/static"


def test_tabbar_and_rail_preserve_dom_on_tab_switch():
    """Switching between 资讯 / 构想 / 任务 / 群 must update aria-current and tab-pill in place
    so CSS transitions and click animations are not destroyed by innerHTML replacement."""
    source = (STATIC / "js/render.js").read_text()
    source = "\n".join(line for line in source.splitlines() if not line.startswith("import ")).replace("export ", "")
    setup = r'''
import assert from "node:assert/strict";
const state = {me: {role:"admin"}, groups:[{id:"g1",name:"示例群",avatar:"/a.png",today:{pending:1}}], g:"g1", tab:"news", page:null};
const SVG = new Proxy({}, {get:(_,k)=>`<svg data-k="${String(k)}"></svg>`});
const TABS = [{id:"news",label:"资讯"},{id:"ideas",label:"构想"},{id:"tasks",label:"任务"},{id:"group",label:"群"}];
const admin = () => true, gadmin = () => true, isGA = () => false;
const esc = s => String(s ?? ""), ico = () => "", hasUpdate = () => false;
const grp = () => state.groups[0], gname = g => g.name, mq = s => s, platTag = () => "", quiet = () => ({text:"正常"});
const gface = g => `<span class="gav"><img src="${g.avatar}"></span>`;

function makeBar() {
  let html = "", sets = 0;
  const pill = {style: {transform: "", opacity: ""}};
  const tabs = TABS.map(t => {
    const attrs = {"aria-current": "false"};
    return {
      dataset: {act: "tab", tab: t.id},
      getAttribute: k => attrs[k],
      setAttribute: (k, v) => { attrs[k] = String(v); },
      querySelector: () => null,
      insertAdjacentHTML: () => {},
    };
  });
  return {
    style: {setProperty(){}},
    get innerHTML() { return html; },
    set innerHTML(v) { html = v; sets++; },
    querySelector: sel => (sets > 0 && sel === ".tab-pill" ? pill : null),
    querySelectorAll: sel => (sets > 0 && sel === ".tab" ? tabs : []),
    get sets() { return sets; },
    pill,
    tabs,
  };
}

function makeRail() {
  let html = "", sets = 0;
  const items = [
    {dataset: {act: "group", g: "g1"}, attrs: {"aria-current": "true"}},
    ...TABS.map(t => ({dataset: {act: "tab", tab: t.id}, attrs: {"aria-current": String(t.id === "news")}})),
    {dataset: {act: "appearance"}, attrs: {}},
    {dataset: {act: "board"}, attrs: {"aria-current": "false"}},
    {dataset: {act: "chat"}, attrs: {"aria-current": "false"}},
    {dataset: {act: "settings"}, attrs: {"aria-current": "false"}},
  ].map(x => ({
    dataset: x.dataset,
    getAttribute: k => x.attrs[k],
    setAttribute: (k, v) => { x.attrs[k] = String(v); },
  }));
  return {
    get innerHTML() { return html; },
    set innerHTML(v) { html = v; sets++; },
    querySelectorAll: sel => (sets > 0 && sel === ".r-item" ? items : []),
    get sets() { return sets; },
    items,
  };
}

const tabbar = makeBar();
const rail = makeRail();
const $ = id => (id === "tabbar" ? tabbar : rail);
'''
    body = r'''
renderTabbar();
assert.equal(tabbar.sets, 1, "首次渲染写入 tabbar innerHTML");
state.tab = "ideas";
renderTabbar();
assert.equal(tabbar.sets, 1, "切换标签时应原地更新 tabbar，不重建 DOM");
assert.equal(tabbar.pill.style.transform, "translateX(100%)");
assert.equal(tabbar.tabs[1].getAttribute("aria-current"), "true");
assert.equal(tabbar.tabs[0].getAttribute("aria-current"), "false");

renderRail();
assert.equal(rail.sets, 1, "首次渲染写入 rail innerHTML");
state.tab = "tasks";
renderRail();
assert.equal(rail.sets, 1, "切换群内标签时应原地更新 rail 的 aria-current，不重建 DOM");
const taskBtn = rail.items.find(x => x.dataset.tab === "tasks");
const ideasBtn = rail.items.find(x => x.dataset.tab === "ideas");
assert.equal(taskBtn.getAttribute("aria-current"), "true");
assert.equal(ideasBtn.getAttribute("aria-current"), "false");
'''
    result = subprocess.run(["node", "--input-type=module", "-e", setup + source + body], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_nav_hover_and_click_animations_are_smooth():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    util = (STATIC / "js/util.js").read_text(encoding="utf-8")
    main_js = (STATIC / "js/main.js").read_text(encoding="utf-8")
    for cls in ("n-l1", "i-bulb", "i-rays", "t-handle", "t-latch", "g-p1", "g-p4", "s-k1", "c-d1", "b-c1"):
        assert cls in util, f"SVG 缺少内部动画部件类名: {cls}"
    for token in (
        "--ease-spring",
        '[data-tab="news"]:hover svg',
        '[data-tab="ideas"]:hover .i-bulb',
        '[data-tab="ideas"]:hover .i-rays',
        '[data-tab="tasks"]:hover .t-handle',
        '[data-tab="group"]:hover .g-p1',
        '[data-act="settings"]:hover .s-k1',
        '[data-act="chat"]:hover .c-d1',
        '[data-act="board"]:hover .b-c1',
        '[data-act]:active svg',
    ):
        assert token in css, f"缺少导航动画样式: {token}"
    # 悬停和选中态不用 CSS keyframes 硬切（否则按下与松开会出现首尾帧跳变）
    assert '[aria-current="true"] svg { animation:' not in css
    # 点击释放采用 Web Animations composite: "add" 叠加回弹，保证从当前悬停/按下姿态无缝衔接
    assert 'composite: "add"' in main_js and "pulseNav" in main_js
