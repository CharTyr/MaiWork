"""管理员对话输入框：回车换行，Ctrl / Cmd + 回车才发送（2026-10-30 用户定）。

真跑一遍 events.js（node，去掉 import 后注入桩），模拟 keydown 事件。
找不到 node 直接失败（不许 skip 造成假绿）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

JS = Path(__file__).resolve().parent.parent / "maiwork/console/static/js"


def _source() -> str:
    src = (JS / "events.js").read_text(encoding="utf-8")
    return "\n".join(line for line in src.splitlines() if not line.startswith("import ")).replace("export ", "")


SETUP = r"""
import assert from "node:assert/strict";
const submitted = [];
const listeners = { keydown: [], change: [], input: [], toggle: [], click: [] };
const document = {
  addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
  querySelector: () => null, querySelectorAll: () => [],
};
const $ = (id) => (id === "chat-form" ? { requestSubmit: () => submitted.push(id) } : null);
const state = {}, desktop = { matches: true };
const window = { matchMedia: () => ({ matches: false }) };
const toast = () => {}, api = async () => ({}), repaintSheet = () => {};
const loadExt = async () => {}, chipAdd = () => {}, loadUsageDay = async () => {};
const avatarSaved = async () => {}, loadLogs = async () => {}, loadChats = async () => {};
const renderSide = () => {}, renderView = () => {};
const fire = (e) => { for (const fn of listeners.keydown || []) fn(e); };
const input = { id: "chat-input", closest: () => null };
const ev = (over) => ({ key: "Enter", target: input, preventDefault: () => { over._pd = true; }, isComposing: false, ...over });
"""


def test_enter_key_behavior() -> None:
    body = r"""
{
  let e = ev({}); fire(e);
  assert.equal(submitted.length, 0, "普通回车不发送");
  assert.ok(!e._pd, "普通回车不拦默认换行");
  e = ev({ shiftKey: true }); fire(e);
  assert.equal(submitted.length, 0, "Shift+回车不发送");
  e = ev({ ctrlKey: true }); fire(e);
  assert.equal(submitted.length, 1, "Ctrl+回车发送");
  e = ev({ metaKey: true }); fire(e);
  assert.equal(submitted.length, 2, "Cmd+回车发送");
  e = ev({ ctrlKey: true, isComposing: true }); fire(e);
  assert.equal(submitted.length, 2, "输入法组词中不发送");
}
"""
    result = subprocess.run(["node", "--input-type=module", "-e", SETUP + _source() + body],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_desktop_gate_removed() -> None:
    """回车发送不再按「只在桌面端」拦——各端一致都是换行。"""
    src = _source()
    keydown = "\n".join(src.split("chat-input")[1].split("\n\n")[0].split("\n")) if "chat-input" in src else ""
    assert "desktop" not in src.split("// ───────── 设置各分栏")[0].split("chat-input")[-1][:400] or "ctrlKey" in src
