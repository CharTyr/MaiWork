"""网页文案按身份分：群管理员进不了设置页，给他看的话不能叫他去「设置 → …」。

只做静态检查（读 JS 源码），不开浏览器。
"""

from __future__ import annotations

import re
from pathlib import Path

JS = Path(__file__).resolve().parent.parent / "maiwork" / "console" / "static" / "js"


def _read(name: str) -> str:
    p = JS / name
    if not p.is_file():
        raise AssertionError(f"找不到前端文件 {p}")
    return p.read_text(encoding="utf-8")


def test_paused_task_settings_hint_only_for_top_admin() -> None:
    """任务因用量 / 时长上限自动暂停：只有总管理员的说明里才提「设置 → 全部设置 → 任务上限」。"""
    src = _read("detail.js")
    m = re.search(r"function pausedBlock\(t\) \{(.*?)\n\}", src, re.S)
    assert m, "detail.js 里找不到 pausedBlock"
    body = m.group(1)
    assert "任务上限" in body, "总管理员仍应被告知上限在哪改"
    # 提到设置页的那句话必须挂在 admin() 分支上（不能是 gadmin()，群管理员也会命中）：
    # 往前找离它最近的「xxx() ?」条件
    for m2 in re.finditer(r"设置 →", body):
        conds = re.findall(r"(\w*admin)\(\)\s*\?", body[: m2.start()])
        assert conds and conds[-1] == "admin", f"提到设置页的文案没挂在 admin() 分支上（最近的条件是 {conds[-1:] or '无'}）"
    # 群管理员仍要能看到「继续 / 取消」的说明，只是不提设置页
    assert "继续" in body and re.search(r"gadmin\(\)\s*\?\s*act\b", body), "群管理员应仍有「继续 / 取消」的说明"


def test_no_settings_hint_behind_gadmin_anywhere() -> None:
    """设置页以外的模块里，gadmin() 分支的文案都不许指向设置页。"""
    bad = []
    for p in sorted(JS.rglob("*.js")):
        if p.name.startswith("._") or p.parent.name == "settings":
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "gadmin()" in line and re.search(r"设置 →|「设置」|在设置里", line):
                bad.append(f"{p.relative_to(JS).as_posix()}:{i}")
    assert not bad, f"这些给群管理员看的文案指向了他进不去的设置页：{bad}"
