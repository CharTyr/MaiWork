"""首次引导完成页：没有引导步骤、只有设置子页的项也要给「前往设置」（2026-10-25）。

静态检查前端源码：按钮形状、处理函数先收起引导再进设置页、「全部设置」带栏目。
"""

from __future__ import annotations

from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "maiwork/console/static/js/onboarding.js"


def _src() -> str:
    assert JS.is_file(), f"找不到前端文件：{JS}"
    return JS.read_text(encoding="utf-8")


def test_done_pane_renders_set_button_for_sub_only_items() -> None:
    src = _src()
    pane = src[src.index("function onbDonePane()"):src.index("// 引导「联网搜索」一步")]
    assert 'data-act="onb-set"' in pane
    assert "data-sub=" in pane and "data-sec=" in pane
    # ok 的项不给按钮；有 step 的仍走 onb-goto（回到引导那一步）
    assert 'it.state === "ok" ? ""' in pane and 'data-act="onb-goto"' in pane


def test_set_handler_closes_onboarding_then_enters_settings() -> None:
    src = _src()
    body = src[src.index('if (a === "onb-set")'):]
    body = body[:body.index("\n  }\n") + 4]
    assert body.index('closeOnboarding("done")') < body.index("enterSettings(sub)")
    assert "if (onb.open) return" in body  # 没关掉（保存失败）就留在引导里
    assert "state.cfgSec" in body
    assert "enterSettings" in src[:src.index("export const onb") if "export const onb" in src else 2000]
