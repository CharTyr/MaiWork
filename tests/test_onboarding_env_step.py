"""首次引导「能做到什么」一步（2026-10-15 用户定）：说清任务做不做得成主要看模型能力和当前环境能做什么，
鼓励先在「和 MaiWork 聊」里把环境配好；前后端步骤名单保持同一份。"""

from __future__ import annotations

import re
from pathlib import Path

from CharTyr_MaiWork.maiwork import onboarding

JS = Path(onboarding.__file__).parent / "console" / "static" / "js" / "onboarding.js"


def _js_steps() -> list[str]:
    text = JS.read_text(encoding="utf-8")
    block = text[text.index("export const ONB_STEPS = ["):text.index("];", text.index("export const ONB_STEPS = ["))]
    return re.findall(r'id: "([a-z]+)"', block)


def test_steps_same_on_both_sides_and_env_before_done() -> None:
    assert list(onboarding.STEPS) == _js_steps()
    assert onboarding.STEPS[-2:] == ("env", "done")


def test_env_pane_explains_limits_and_points_to_admin_chat() -> None:
    text = JS.read_text(encoding="utf-8")
    pane = text[text.index('if (id === "env")'):text.index("return onbDonePane();")]
    for must in ("模型", "环境", "和 MaiWork 聊", "skill"):
        assert must in pane, must
