"""host.py 里调用的每一项宿主能力都必须在 _manifest.json 的 capabilities 里声明。

线上宿主按插件声明发能力令牌（src/plugin_runtime/host/authorization.py check_capability，
2026-10-10 读线上源码）：没声明的能力调用直接被拒，而本地测试全用假宿主、测不出来。
2026-10-10 新加 `message.get_by_id`（取群友引用的原图）时差点漏声明——这道测试守住它。
`adapter.napcat.*` 不是能力名，是经 `api.call` 转发的适配器动作，不在此列。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _called_capabilities() -> set[str]:
    src = (PLUGIN_DIR / "maiwork" / "host.py").read_text(encoding="utf-8")
    names = set(re.findall(r'_call\(\s*"([a-z_]+\.[a-z_.]+)"', src))
    return {n for n in names if not n.startswith("adapter.")}


def test_every_called_capability_is_declared():
    manifest = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
    declared = set(manifest["capabilities"])
    called = _called_capabilities()
    assert "message.get_by_time_in_chat" in called  # 正则确实抓到了东西
    assert called - declared == set(), f"没在 _manifest.json 声明：{sorted(called - declared)}"


def test_message_get_by_id_declared_for_request_images():
    manifest = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
    assert "message.get_by_id" in manifest["capabilities"]
