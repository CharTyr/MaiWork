"""网页看板（js/board.js）：只总管理员、只看不动、停在看板时 10 秒刷新。

看板页是 2026-10-29 用户要的：实时看 MaiWork 各专岗在做什么、最近 N 小时做了什么。
这里做源码级检查（board.js 依赖浏览器的 matchMedia，node 里不好直接跑）。
"""

from __future__ import annotations

import re
from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "maiwork" / "console" / "static" / "js"
if not JS.is_dir():  # 找不到插件目录直接报错，不许假绿
    raise RuntimeError(f"找不到前端目录：{JS}")


def _read(name: str) -> str:
    return (JS / name).read_text(encoding="utf-8")


def test_board_reads_only_board_api() -> None:
    src = _read("board.js")
    calls = re.findall(r'api\(\s*"([A-Z]+)"\s*,\s*[`"]([^`"$?]+)', src)
    # 只读：列表 + 单件过程明细，都是 GET
    assert sorted(calls) == [("GET", "/api/board"), ("GET", "/api/board/items/")]


def test_board_has_no_operation_buttons() -> None:
    src = _read("board.js")
    acts = set(re.findall(r'data-act="([a-z-]+)"', src))
    # 只许筛选和跳转到现有页面，不许暂停 / 取消 / 批准之类的动作
    assert acts <= {"board-filter", "board-item"}, acts


def test_board_is_admin_only_in_router_and_rail() -> None:
    router = _read("router.js")
    assert 'h.board && admin() ? "board"' in router
    render = _read("render.js")
    # 左栏入口只在 admin() 时出现
    assert re.search(r'admin\(\) \? `<button class="r-item" data-act="board"', render)


def test_board_polls_every_10s_only_when_visible_on_board() -> None:
    router = _read("router.js")
    m = re.search(r"setInterval\(\(\) => \{\n  if \(document\.hidden \|\| state\.page !== \"board\" \|\| !admin\(\)\) return;.*?\}, (\d+)\);", router, re.S)
    assert m, "看板定时刷新没接上"
    assert m.group(1) == "10000"


def test_board_hides_failure_state_honestly() -> None:
    src = _read("board.js")
    assert 'loading(fail, "board")' in src
    assert "连接中断" in src
    router = _read("router.js")
    assert 'what === "board"' in router


def test_board_lanes_show_running_history_and_fish() -> None:
    src = _read("board.js")
    assert "lane.running" in src and "lane.history" in src
    assert "进行中" in src and "历史" in src
    assert "fishSvg(`agent:${kind}:${seed}`" in src  # 和各页一样的专岗小鱼


def test_board_item_sheet_shows_tools_and_thinking() -> None:
    src = _read("board.js")
    assert 's.type === "tool"' in src and 's.type === "think"' in src
    for word in ("输入", "输出", "思考", "工具"):
        assert word in src
    sheet = _read("sheet.js")
    assert 'kind === "board-item"' in sheet


def test_board_think_step_shows_reasoning_or_redacted_note() -> None:
    src = _read("board.js")
    assert "s.reasoning" in src and "思考过程" in src
    assert "s.reasoning_redacted" in src
