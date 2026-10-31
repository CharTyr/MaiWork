"""网页动效与交互细节契约及行为测试（覆盖 6 项新增动效与 5 项既有动效修正）。"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "maiwork" / "console" / "static"
JS = STATIC / "js"
if not STATIC.is_dir() or not JS.is_dir():
    raise RuntimeError(f"找不到前端静态目录：{STATIC}")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _node() -> str:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("找不到 node，无法运行前端动效行为测试")
    return node


def _extract_hover_media_blocks(css: str) -> tuple[str, str]:
    """拆分出 @media (hover: hover) and (pointer: fine) 内部与外部的 CSS 文本。"""
    inside: list[str] = []
    outside: list[str] = []
    i = 0
    n = len(css)
    pat = re.compile(r"@media\s*\(hover:\s*hover\)\s*and\s*\(pointer:\s*fine\)\s*\{")
    while i < n:
        m = pat.search(css, i)
        if not m:
            outside.append(css[i:])
            break
        outside.append(css[i : m.start()])
        depth = 1
        j = m.end()
        while j < n and depth > 0:
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
            j += 1
        inside.append(css[m.end() : j - 1])
        i = j
    return "\n".join(inside), "\n".join(outside)


def test_hover_transforms_gated_for_fine_pointer() -> None:
    """Fix #1：位移/抬升类 :hover 必须包在 (hover: hover) and (pointer: fine) 中，避免触屏粘滞。"""
    css = _read(STATIC / "style.css")
    inside, outside = _extract_hover_media_blocks(css)
    # 去掉 @media (prefers-reduced-motion: reduce) 块再检查外部是否有未门控的悬停位移
    outside_no_rm = re.sub(
        r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}",
        "",
        outside,
    )
    gated_selectors = (
        ".round-btn:hover",
        '[data-tab="news"]:hover svg',
        '[data-tab="ideas"]:hover svg',
        '[data-tab="tasks"]:hover svg',
        '[data-tab="group"]:hover svg',
        '[data-act="settings"]:hover svg',
        '[data-act="chat"]:hover svg',
        '[data-act="board"]:hover svg',
        '[data-act="appearance"]:hover svg',
        ".r-item:hover",
        ".stat:hover",
    )
    for sel in gated_selectors:
        assert sel in inside, f"{sel} 应位于 @media (hover: hover) and (pointer: fine) 内"
        assert sel not in outside_no_rm, f"{sel} 不应暴露在无指针门控的全局样式中"


def test_sheet_asymmetric_timing_and_transitionend() -> None:
    """Opportunity #1 + Fix #2：抽屉进慢退快、拖拽态关过渡、关闭等待 transitionend。"""
    css = _read(STATIC / "style.css")
    sheet_js = _read(JS / "sheet.js")

    m_exit = re.search(r"\.sheet\s*\{[^}]*transition:\s*transform\s+0\.(\d+)s\s+var\(--ease\)", css)
    m_enter = re.search(r"\.sheet\.on\s*\{[^}]*transition(?:-duration)?:\s*(?:transform\s+)?0\.(\d+)s", css)
    assert m_exit and m_enter, ".sheet 与 .sheet.on 必须分别声明退出与进入过渡时长"
    exit_ms = int(m_exit.group(1).ljust(3, "0"))
    enter_ms = int(m_enter.group(1).ljust(3, "0"))
    assert exit_ms < enter_ms, f"退出时长 ({exit_ms}ms) 应快于进入时长 ({enter_ms}ms)"

    assert ".sheet.dragging" in css and "transition: none" in css
    assert "transitionend" in sheet_js, "closeSheet 应监听 transitionend 再设 hidden"
    assert "}, 420);" not in sheet_js, "不应保留截断动画的 420ms 硬编码定时器"


def test_sheet_mobile_drag_math_and_behavior() -> None:
    """Opportunity #1：手机底部抽屉跟手下滑关闭、上拉阻尼、快速轻拂关闭、回拉取消、多指与无障碍保护。"""
    script = r"""
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

const src = await readFile(%s, "utf8");
const clean = src
  .split("\n")
  .filter((l) => !l.startsWith("import "))
  .join("\n")
  .replace(/export /g, "");

let calmVal = false;
let wideVal = false;
const listeners = new Map();
const makeEl = (id) => {
  const classes = new Set();
  return {
    id,
    hidden: true,
    scrollTop: 0,
    offsetHeight: 500,
    style: { transform: "", opacity: "" },
    classes,
    classList: {
      add(c) { classes.add(c); },
      remove(c) { classes.delete(c); },
      contains(c) { return classes.has(c); },
      toggle(c, on) { if (on) classes.add(c); else classes.delete(c); },
    },
    attrs: {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    removeAttribute(k) { delete this.attrs[k]; },
    focus() {},
    querySelectorAll() { return []; },
    addEventListener(t, fn) {
      if (!listeners.has(t)) listeners.set(t, []);
      listeners.get(t).push(fn);
    },
    removeEventListener(t, fn) {
      listeners.set(t, (listeners.get(t) || []).filter((x) => x !== fn));
    },
    setPointerCapture() { this.captured = true; },
    releasePointerCapture() { this.captured = false; },
  };
};

const nodes = {
  sheet: makeEl("sheet"),
  scrim: makeEl("scrim"),
  app: makeEl("app"),
  tabbar: makeEl("tabbar"),
  side: makeEl("side"),
};
const state = { sheet: null, detail: null, page: null, editing: null, handoff: null };
const sandbox = {
  console,
  Math,
  Number,
  String,
  Set,
  Date,
  performance: { now: () => 100 },
  $: (id) => nodes[id] || null,
  CATS: [],
  desktop: { matches: false },
  isGA: () => false,
  state,
  RATE_REASONS: [],
  SVG: { close: "x" },
  esc: (s) => String(s ?? ""),
  tip: () => "",
  calm: () => calmVal,
  gname: () => "群",
  grp: () => ({}),
  detailHTML: () => "<p>body</p>",
  loadTask: () => {},
  handoffSheet: () => "",
  groupPicker: () => "",
  draft: { models: null },
  chatSide: () => "",
  renderRail: () => {},
  renderSide: () => {},
  renderView: () => {},
  syncHash: () => {},
  appearanceSheet: () => "",
  socialCookieSheet: () => "",
  sfx: () => {},
  boardItemSheet: () => "",
  previewSheet: () => "",
  ui: { sideFlash: false },
  window: {
    scrollY: 0,
    scrollTo() {},
    matchMedia: (q) => ({ matches: q.includes("760px") ? wideVal : calmVal }),
  },
  document: {
    activeElement: null,
    contains: () => false,
    addEventListener() {},
    removeEventListener() {},
  },
  requestAnimationFrame: (fn) => fn(),
  setTimeout: (fn) => 1,
  clearTimeout: () => {},
};

vm.runInNewContext(clean + "\nglobalThis.exports = { sheetDragMath, openSheet, closeSheet };", sandbox);
const { sheetDragMath, openSheet } = sandbox.exports;
assert.equal(typeof sheetDragMath, "function", "sheet.js 应导出纯计算函数 sheetDragMath");

// 1. 向上拉带阻尼，不脱离底部
const up = sheetDragMath(-100, -0.3, 500);
assert.ok(up.offset < 0 && up.offset >= -28, `上拉应有阻尼上限: ${up.offset}`);
assert.equal(up.dismiss, false);

// 2. 慢拉超过距离阈值 -> 关闭
const slowDismiss = sheetDragMath(125, 0.12, 500);
assert.equal(slowDismiss.dismiss, true);
assert.ok(slowDismiss.scrim < 1 && slowDismiss.scrim >= 0);

// 3. 短距离快速下拂 -> 关闭
const flickDismiss = sheetDragMath(48, 0.62, 500);
assert.equal(flickDismiss.dismiss, true);

// 4. 轻微慢速拖动 -> 弹回不关
const nudge = sheetDragMath(32, 0.08, 500);
assert.equal(nudge.dismiss, false);

// 5. 拉过阈值但最后往上快速回拉 -> 取消关闭
const pullBack = sheetDragMath(130, -0.35, 500);
assert.equal(pullBack.dismiss, false);

// 6. 真实指针序列：从抓手拖下关闭 + 第二根手指忽略 + 减少动态效果时不响应拖拽
openSheet("login");
assert.equal(state.sheet, "login");
const grabTarget = { closest: (sel) => (sel === ".grab" ? {} : null) };
const fire = (type, ev) => {
  for (const fn of listeners.get(type) || []) fn(ev);
};

// 减少动态效果时禁止拖拽
calmVal = true;
fire("pointerdown", { pointerId: 1, button: 0, clientX: 100, clientY: 100, timeStamp: 10, target: grabTarget });
fire("pointermove", { pointerId: 1, clientX: 100, clientY: 260, timeStamp: 120, preventDefault() {} });
assert.equal(nodes.sheet.style.transform, "");
calmVal = false;

// 正常拖拽 + 多指忽略
fire("pointerdown", { pointerId: 1, button: 0, clientX: 100, clientY: 100, timeStamp: 10, target: grabTarget });
fire("pointerdown", { pointerId: 2, button: 0, clientX: 180, clientY: 100, timeStamp: 20, target: grabTarget });
fire("pointermove", { pointerId: 2, clientX: 180, clientY: 320, timeStamp: 60, preventDefault() {} });
assert.equal(nodes.sheet.style.transform, "", "第二根手指不应劫持拖拽位移");

fire("pointermove", { pointerId: 1, clientX: 100, clientY: 240, timeStamp: 120, preventDefault() {} });
assert.ok(nodes.sheet.style.transform.startsWith("translateY("), "主指针下滑应实时更新 translateY");
assert.ok(nodes.sheet.classList.contains("dragging"));
fire("pointerup", { pointerId: 1, clientX: 100, clientY: 240, timeStamp: 150 });
assert.equal(state.sheet, null, "松手超过阈值应关闭抽屉");
assert.ok(!nodes.sheet.classList.contains("dragging"));
""" % repr(str(JS / "sheet.js"))
    res = subprocess.run([_node(), "--input-type=module", "-e", script], capture_output=True, text=True, timeout=20)
    assert res.returncode == 0, res.stderr


def test_copy_button_morph_feedback() -> None:
    """Opportunity #2：复制类按钮原地变形为「已复制」，连点不丢原文字，减少动态效果下无模糊。"""
    actions = _read(JS / "actions.js")
    handoff = _read(JS / "handoff.js")
    assert "flashCopied" in actions, "actions.js 的复制动作应调用 flashCopied"
    assert "flashCopied" in handoff, "handoff.js 的复制动作应调用 flashCopied"

    script = r"""
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

const src = await readFile(%s, "utf8");
const clean = src
  .split("\n")
  .filter((l) => !l.startsWith("import "))
  .join("\n")
  .replace(/export /g, "");

let calmVal = false;
let pendingTimer = null;
const animCalls = [];
const sandbox = {
  console, Date, Math, Number, String, Set, WeakMap,
  $: () => ({ classList: { contains: () => false, toggle() {}, add() {}, remove() {} }, dataset: {}, addEventListener() {} }),
  state: { skew: 0 },
  sfx: () => {},
  window: { matchMedia: () => ({ matches: calmVal }) },
  matchMedia: () => ({ matches: calmVal }),
  document: { querySelector: () => null },
  setTimeout: (fn, ms) => { pendingTimer = { fn, ms }; return 1; },
  clearTimeout: () => {},
};
vm.runInNewContext(clean + "\nglobalThis.exports = { flashCopied, SVG };", sandbox);
const { flashCopied } = sandbox.exports;
assert.equal(typeof flashCopied, "function", "util.js 应导出 flashCopied");

const btn = {
  innerHTML: "<svg></svg>复制要求",
  textContent: "复制要求",
  offsetWidth: 116,
  isConnected: true,
  style: { minWidth: "" },
  classList: {
    set: new Set(),
    contains(c) { return this.set.has(c); },
    add(c) { this.set.add(c); },
    remove(c) { this.set.delete(c); },
  },
  animate(frames, opts) { animCalls.push({ frames, opts }); return {}; },
};

// 连点两次也不应把「已复制」当成原始文案保存
flashCopied(btn);
assert.ok(btn.innerHTML.includes("已复制"));
assert.equal(btn.style.minWidth, "116px", "变形期间应锁住最小宽度防抖动");
flashCopied(btn);
assert.ok(btn.innerHTML.includes("已复制"));
assert.ok(animCalls[0].frames.some((f) => String(f.filter || "").includes("blur")), "常规模式含轻微模糊过渡");

// 定时器触发后恢复原按钮文案与宽度
pendingTimer.fn();
assert.equal(btn.innerHTML, "<svg></svg>复制要求");
assert.equal(btn.style.minWidth, "");

// 减少动态效果下不做 blur / scale
animCalls.length = 0;
calmVal = true;
flashCopied(btn);
assert.ok(!animCalls[0].frames.some((f) => f.filter || f.transform), "减少动态效果下仅做透明度过渡");
""" % repr(str(JS / "util.js"))
    res = subprocess.run([_node(), "--input-type=module", "-e", script], capture_output=True, text=True, timeout=20)
    assert res.returncode == 0, res.stderr


def test_origin_aware_popovers_and_dismissal() -> None:
    """Opportunity #3：气泡菜单按触发点方向缩放展开，连续开提示零延迟，点外部与 Esc 关闭。"""
    css = _read(STATIC / "style.css")
    assert "transform-origin: 26px bottom" in css, ".idea-menu 应从左下触发按钮方向展开"
    assert "transform-origin: 22px top" in css, ".section-picker 菜单应从左上按钮方向展开"
    assert "transform-origin: 18px top" in css, ".tip-pop 应从说明图标方向展开"
    assert "translateX(var(--tip-shift, 0)) scale(" in css, "tip-in 动画不得覆盖防出屏的 --tip-shift"

    script = r"""
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

const src = await readFile(%s, "utf8");
const clean = src
  .split("\n")
  .filter((l) => !l.startsWith("import "))
  .join("\n");

const docListeners = new Map();
let openTipList = [];
let openPickers = [];
let refreshCalls = 0;
const state = { ideaMore: 42 };
const moreBtn = { focused: false, focus() { this.focused = true; } };

const sandbox = {
  console, Math, Number, String, Blob, FileReader: class {},
  $: () => null,
  state,
  toast: () => {},
  api: async () => ({}),
  confirmExecTools: () => false,
  loadExt: async () => {},
  chipAdd: () => {},
  loadUsageDay: async () => {},
  avatarSaved: async () => {},
  loadLogs: async () => {},
  repaintSheet: () => {},
  loadChats: async () => {},
  renderSide: () => {},
  renderView: () => {},
  refreshDetail: () => { refreshCalls++; },
  window: { innerWidth: 390, matchMedia: () => ({ matches: false }) },
  document: {
    addEventListener(type, fn) {
      if (!docListeners.has(type)) docListeners.set(type, []);
      docListeners.get(type).push(fn);
    },
    querySelector: (s) => (s === ".idea-more" ? moreBtn : null),
    querySelectorAll: (s) => {
      if (s === "details.tip[open]") return openTipList;
      if (s === "details.section-picker[open]") return openPickers;
      return [];
    },
  },
  setTimeout: () => 1,
  clearTimeout: () => {},
};
vm.runInNewContext(clean, sandbox);

const fire = (type, ev) => {
  for (const fn of docListeners.get(type) || []) {
    fn(ev);
    if (ev._stopped) break;
  }
};

// 1. 第一个 tip 打开：正常播放动画（无 data-instant）；紧接着开第二个 tip：跳过动画（data-instant="1"）
const makeTip = () => {
  const pop = {
    dataset: {},
    style: { removeProperty() {}, setProperty() {} },
    getBoundingClientRect: () => ({ left: 40, right: 200 }),
  };
  return {
    open: true,
    pop,
    matches: (s) => s === "details.tip",
    querySelector: (s) => (s === ".tip-pop" ? pop : { focus() {} }),
  };
};
const t1 = makeTip();
openTipList = [t1];
fire("toggle", { target: t1 });
assert.equal(t1.pop.dataset.instant, undefined, "首次打开提示应播放进场动画");

const t2 = makeTip();
openTipList = [t1, t2];
fire("toggle", { target: t2 });
assert.equal(t1.open, false, "打开第二个提示时自动收起第一个");
assert.equal(t2.pop.dataset.instant, "1", "紧邻切换第二个提示应立即出现不重播动画");

// 2. Esc 优先关闭 .idea-menu 并拦截冒泡，不误关抽屉
openTipList = [];
let stopped = false;
fire("keydown", {
  key: "Escape",
  stopImmediatePropagation() { stopped = true; this._stopped = true; },
});
assert.equal(state.ideaMore, null, "Esc 应收起构想更多菜单");
assert.equal(stopped, true, "Esc 收起菜单时应阻止继续关闭抽屉");
assert.equal(refreshCalls, 1);

// 3. 点击菜单外部自动关闭 .idea-menu 与 details.section-picker
state.ideaMore = 7;
const picker = { open: true };
openPickers = [picker];
fire("click", { target: { closest: () => null } });
assert.equal(state.ideaMore, null, "点击外部应关闭 .idea-menu");
assert.equal(picker.open, false, "点击外部应关闭 .section-picker");
""" % repr(str(JS / "events.js"))
    res = subprocess.run([_node(), "--input-type=module", "-e", script], capture_output=True, text=True, timeout=20)
    assert res.returncode == 0, res.stderr


def test_card_collapse_flip_helper() -> None:
    """Opportunity #4：审批/确认卡片移除时仅用 opacity + transform 做退出与 FLIP 上移，不动画布局高度。"""
    actions = _read(JS / "actions.js")
    for act_name in ('case "chat-confirm":', 'case "ask-review":', 'case "req":'):
        idx = actions.index(act_name)
        block = actions[idx : idx + 900]
        assert "flipRemove" in block, f"{act_name} 处理成功后应调用 flipRemove 平滑收起卡片"

    script = r"""
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

const src = await readFile(%s, "utf8");
const clean = src
  .split("\n")
  .filter((l) => !l.startsWith("import "))
  .join("\n")
  .replace(/export /g, "");

let calmVal = false;
const animated = [];
const sandbox = {
  console, Date, Math, Number, String, Set, Map, WeakMap, Promise,
  $: () => null,
  state: { skew: 0 },
  sfx: () => {},
  window: { matchMedia: () => ({ matches: calmVal }) },
  matchMedia: () => ({ matches: calmVal }),
  document: { querySelector: () => null },
  setTimeout: () => 1,
  clearTimeout: () => {},
};
vm.runInNewContext(clean + "\nglobalThis.exports = { flipRemove };", sandbox);
const { flipRemove } = sandbox.exports;
assert.equal(typeof flipRemove, "function", "util.js 应导出 flipRemove");

const card = {
  isConnected: true,
  animate(frames, opts) {
    animated.push({ target: "card", frames, opts });
    return { finished: Promise.resolve() };
  },
};
const sibOld = {
  tagName: "ARTICLE",
  className: "item ruled",
  dataset: { id: "next-1" },
  textContent: "下一条",
  getBoundingClientRect: () => ({ top: 320 }),
};
const sibNew = {
  tagName: "ARTICLE",
  className: "item ruled",
  dataset: { id: "next-1" },
  textContent: "下一条",
  getBoundingClientRect: () => ({ top: 140 }),
  animate(frames, opts) {
    animated.push({ target: "sibling", frames, opts });
    return {};
  },
};
let currentItems = [sibOld];
const container = {
  querySelectorAll: () => currentItems,
};

await flipRemove(card, container, () => {
  currentItems = [sibNew];
});
assert.equal(animated.length, 2);
assert.equal(animated[0].target, "card");
assert.ok(!("height" in animated[0].frames[0]) && !("height" in animated[0].frames[1]), "严禁动画 height");
assert.equal(animated[1].target, "sibling");
assert.equal(animated[1].frames[0].transform, "translateY(180px)");
assert.equal(animated[1].frames[1].transform, "translateY(0)");
""" % repr(str(JS / "util.js"))
    res = subprocess.run([_node(), "--input-type=module", "-e", script], capture_output=True, text=True, timeout=20)
    assert res.returncode == 0, res.stderr


def test_press_feedback_and_desktop_side_transition() -> None:
    """Opportunity #5：.item.tap 与 .row 统一缩放按压反馈；桌面右栏详情仅在用户开关时淡入，轮询不重播。"""
    css = _read(STATIC / "style.css")
    assert ".item.tap:active" in css and "scale(0.985)" in css
    assert ".row:active" in css and "scale(0.985)" in css
    assert ".side.side-enter" in css

    sheet_js = _read(JS / "sheet.js")
    actions_js = _read(JS / "actions.js")
    main_js = _read(JS / "main.js")
    render_js = _read(JS / "render.js")
    assert "ui.sideFlash = true" in sheet_js
    assert "ui.sideFlash = true" in actions_js
    assert "ui.sideFlash = true" in main_js
    assert "side-enter" in render_js and "ui.sideFlash = false" in render_js


def test_details_disclosure_only_animates_on_user_toggle() -> None:
    """Opportunity #6：原生 <details> 只在用户点击展开时加 .reveal，轮询重绘已带 open 属性时不重播。"""
    script = r"""
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

const src = await readFile(%s, "utf8");
const clean = src.split("\n").filter((l) => !l.startsWith("import ")).join("\n");
const docListeners = new Map();
const sandbox = {
  console, Math, Number, String, Blob, FileReader: class {},
  $: () => null,
  state: {},
  toast: () => {},
  api: async () => ({}),
  confirmExecTools: () => false,
  loadExt: async () => {},
  chipAdd: () => {},
  loadUsageDay: async () => {},
  avatarSaved: async () => {},
  loadLogs: async () => {},
  repaintSheet: () => {},
  loadChats: async () => {},
  renderSide: () => {},
  renderView: () => {},
  refreshDetail: () => {},
  window: { innerWidth: 800, matchMedia: () => ({ matches: false }) },
  document: {
    addEventListener(type, fn) {
      if (!docListeners.has(type)) docListeners.set(type, []);
      docListeners.get(type).push(fn);
    },
    querySelector: () => null,
    querySelectorAll: () => [],
  },
  setTimeout: () => 1,
  clearTimeout: () => {},
};
vm.runInNewContext(clean, sandbox);
const fire = (type, ev) => { for (const fn of docListeners.get(type) || []) fn(ev); };

const makeDetails = () => {
  const body = { tagName: "DIV", classes: new Set(), classList: { add(c) { body.classes.add(c); }, remove(c) { body.classes.delete(c); } } };
  const summary = { tagName: "SUMMARY" };
  const d = {
    open: true,
    dataset: {},
    children: [summary, body],
    matches: (s) => s === "details" || s.includes("details:not"),
  };
  summary.closest = (s) => (s === "summary" ? summary : s === "details" ? d : null);
  return { d, summary, body };
};

// 1. 轮询插入 <details open> 触发的浏览器自动 toggle -> 不加 .reveal
const poll = makeDetails();
fire("toggle", { target: poll.d });
assert.equal(poll.body.classes.has("reveal"), false, "轮询重绘的已展开 details 不得重播动画");

// 2. 用户点击 summary 后触发 toggle -> 加 .reveal
const user = makeDetails();
fire("click", { target: user.summary });
fire("toggle", { target: user.d });
assert.equal(user.body.classes.has("reveal"), true, "用户点击展开 details 应添加 .reveal");
""" % repr(str(JS / "events.js"))
    res = subprocess.run([_node(), "--input-type=module", "-e", script], capture_output=True, text=True, timeout=20)
    assert res.returncode == 0, res.stderr


def test_nviz_no_height_transition() -> None:
    """Fix #3：资讯图解 iframe 移除触发布局重排的 height 过渡。"""
    css = _read(STATIC / "style.css")
    nviz_line = next((ln for ln in css.splitlines() if ln.strip().startswith(".n-viz {")), "")
    assert nviz_line, "style.css 应包含 .n-viz 规则"
    assert "height 0." not in nviz_line and "transition: height" not in nviz_line


def test_gentle_reduced_motion() -> None:
    """Fix #4：减少动态效果不再一刀切 0.01ms !important，保留温和透明度过渡并关闭位移动画。"""
    css = _read(STATIC / "style.css")
    assert "transition-duration: 0.01ms !important" not in css
    assert "animation-duration: 0.01ms !important" not in css
    assert "@keyframes fade-calm" in css


def test_onboarding_no_scale_from_zero() -> None:
    """Fix #5：首次引导所有图标/徽章缩放起点不低于 0.85，杜绝 scale(0) / scale(0.3) 突兀弹入。"""
    onb = _read(JS / "onboarding.js")
    scales = [float(m.group(1)) for m in re.finditer(r"scale\((\d+(?:\.\d+)?)\)", onb)]
    assert scales, "onboarding.js 中应有缩放关键帧"
    too_small = [s for s in scales if s < 0.85]
    assert not too_small, f"onboarding.js 仍存在过小的初始 scale：{too_small}"


def test_rejected_surfaces_stay_unanimated() -> None:
    """明确拒绝加动效的 4 处高频/只读区域保持克制，不混入列表交错动画。"""
    board = _read(JS / "board.js")
    chat = _read(JS / "chat.js")
    assert "bcard enter" not in board and "lane enter" not in board
    assert "cm-bot enter" not in chat and "cm-user enter" not in chat


import json
import pytest
from playwright.async_api import async_playwright, expect
from CharTyr_MaiWork.maiwork import clock
from test_console import G1, env  # noqa: E402, F401


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
async def test_motion_polish_in_real_browsers(env, browser_cache, engine: str) -> None:
    """在真实 Chromium 与 WebKit（Safari 内核）上端到端验证抽屉拖拽关闭、桌面右栏进入、气泡菜单 Esc 层级、复制按钮变形与温和减少动态效果。"""

    now = clock.now()
    with env.app.store.tx() as conn:
        conn.execute(
            "INSERT INTO ideas (group_id, title, body, basis, state, items, keywords, feasibility, target_user_id, task_id, created, updated)"
            " VALUES (?, ?, ?, '', 'new', '[]', '[]', ?, '', NULL, ?, ?)",
            (G1, "整理本周群内讨论纪要", "汇总本周高频问题并生成索引页。", json.dumps({"level": "ok", "note": ""}), now, now),
        )

    token = env.app.token_of(G1)
    origin = str(env.client.make_url("/")).rstrip("/")
    ideas_url = f"{origin}/#/{token}/ideas"

    async with async_playwright() as pw:
        browser = await getattr(pw, engine).launch(headless=True)
        try:
            # 1. 手机触屏视口：点开构想抽屉 -> 抓手跟手下滑超过阈值松手 -> 等待 transitionend 自动隐藏
            mob_ctx = await browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
            mob = await mob_ctx.new_page()
            await mob.goto(ideas_url)
            await expect(mob.locator(".item.tap")).to_be_visible()
            fine_match = await mob.evaluate("() => matchMedia('(hover: hover) and (pointer: fine)').matches")
            assert fine_match is False, "手机触屏视口不应命中精细指针悬停媒体查询"

            await mob.locator(".item.tap").first.click()
            await expect(mob.locator("#sheet.on")).to_be_visible()
            drag_state = await mob.evaluate(
                """() => {
                  const grab = document.querySelector('#sheet .grab');
                  const sh = document.getElementById('sheet');
                  grab.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 1, button: 0, clientX: 195, clientY: 120 }));
                  sh.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, cancelable: true, pointerId: 1, clientX: 195, clientY: 270 }));
                  return { dragging: sh.classList.contains('dragging'), transform: sh.style.transform };
                }"""
            )
            assert drag_state["dragging"] is True
            assert drag_state["transform"].startswith("translateY(150"), drag_state
            await mob.evaluate(
                """() => {
                  const sh = document.getElementById('sheet');
                  sh.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 1, clientX: 195, clientY: 270 }));
                }"""
            )
            await expect(mob.locator("#sheet")).to_be_hidden()
            await mob_ctx.close()

            # 2. 桌面视口：点击构想触发右栏 .side-enter；展开「更多」菜单按 Esc 只关菜单不关详情；点击复制原地变形
            desk_ctx = await browser.new_context(viewport={"width": 1280, "height": 860})
            desk = await desk_ctx.new_page()
            await desk.goto(ideas_url)
            await expect(desk.locator(".item.tap")).to_be_visible()
            await desk.evaluate(
                "() => { Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async () => {} } }); }"
            )
            await desk.locator(".item.tap").first.click()
            await expect(desk.locator("#side.side-enter")).to_be_visible()

            await desk.locator('[data-act="idea-more"]').click()
            await expect(desk.locator(".idea-menu.pop-in")).to_be_visible()
            await desk.keyboard.press("Escape")
            await expect(desk.locator(".idea-menu")).to_have_count(0)
            await expect(desk.locator('[data-act="side-close"]')).to_be_visible()

            # MutationObserver 在 is-copied 加上的同一刻原子取证（class / 文案 / minWidth 三项同刻）。
            # 若用 click 之后三条独立 protocol 读取，1.8s 还原定时器可能在读取间隙先跑（CPU 满载），
            # 使三次结果来自不同状态，而非同一时刻的反馈。
            await desk.evaluate(
                """() => {
                  const btn = document.querySelector('[data-act="idea-copy"]');
                  window.__copyShot = null;
                  new MutationObserver((_, obs) => {
                    if (btn.classList.contains('is-copied')) {
                      window.__copyShot = {
                        copied: true,
                        text: btn.textContent,
                        minWidth: btn.style.minWidth,
                      };
                      obs.disconnect();
                    }
                  }).observe(btn, { attributes: true, attributeFilter: ['class'] });
                }"""
            )
            copy_btn = desk.locator('[data-act="idea-copy"]')
            await copy_btn.click()
            await desk.wait_for_function("() => window.__copyShot !== null")
            shot = await desk.evaluate("() => window.__copyShot")
            assert shot["text"].find("已复制") >= 0, f"复制变形期间应显示已复制: {shot}"
            assert shot["minWidth"].endswith("px"), f"复制变形期间应锁定 minWidth: {shot}"
            await desk_ctx.close()

            # 3. 减少动态效果：保留 0.16s~0.18s 温和透明度过渡，抽屉无滑入位移且禁用拖拽跟手
            rm_ctx = await browser.new_context(viewport={"width": 390, "height": 844}, reduced_motion="reduce")
            rm = await rm_ctx.new_page()
            await rm.goto(ideas_url)
            await expect(rm.locator(".item.tap")).to_be_visible()
            await rm.locator(".item.tap").first.click()
            await expect(rm.locator("#sheet.on")).to_be_visible()
            rm_info = await rm.evaluate(
                """() => {
                  const sh = document.getElementById('sheet');
                  const cs = getComputedStyle(sh);
                  const grab = document.querySelector('#sheet .grab');
                  grab.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 1, button: 0, clientX: 195, clientY: 120 }));
                  sh.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, cancelable: true, pointerId: 1, clientX: 195, clientY: 280 }));
                  return { duration: cs.transitionDuration, transform: cs.transform, inlineTransform: sh.style.transform };
                }"""
            )
            assert rm_info["duration"] == "0.18s", f"减少动态效果应保留温和过渡而非 0.01ms: {rm_info}"
            assert rm_info["transform"] == "none"
            assert rm_info["inlineTransform"] == ""
            await rm_ctx.close()
        finally:
            await browser.close()
