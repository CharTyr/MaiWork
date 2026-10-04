"""统一注入（docs/17 §八.2）：每个环节的提示词，都喝同一段「这个群该怎么做」。

group_context(gid, kind) 输出两段（空段不输出）：

- 【本群规矩（管理员定的，必须照做）】
  group_rules.body：管理员 / 群管理员写的硬规矩；和做法冲突时以规矩为准。

- 【本群<岗位>的做法（MaiWork 总结的，是参考）】
  本群 skill 的正文。专岗（news/idea/goal/自定义）是那一份专岗 skill；
  kind=task 时列本群至多 12 份通用执行 skill 的「名字：description」清单
  （给主模型看，子 agent 用 read_skill 读全文）。

调用方：feeds / personal / coordinator / agents.prompt / topics / identity（记忆注入）等。
一段一段；两段中间空一行。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("maiwork.group_context")

KIND_TITLES = {
    "news": "资讯",
    "idea": "构想",
    "goal": "目标",
    "task": "通用执行",
}


def _kind_title(kind: str, custom_titles: dict[str, str] | None = None) -> str:
    if kind in KIND_TITLES:
        return KIND_TITLES[kind]
    if custom_titles:
        t = str(custom_titles.get(kind) or "").strip()
        if t:
            return t
    return kind


def _rules_block(agents: Any, gid: str) -> str:
    try:
        row = agents.group_rules_get(gid)
    except Exception:
        logger.debug("读本群规矩出错（群 %s），按空出", gid, exc_info=True)
        return ""
    body = str(row.get("body") or "").strip()
    if not body:
        return ""
    return "【本群规矩（管理员定的，必须照做）】\n" + body


def _skill_block(agents: Any, gid: str, kind: str, *, titles: dict[str, str] | None = None) -> str:
    try:
        items = agents.skills(gid, kind, include_archived=False)
    except Exception:
        logger.debug("读本群做法出错（群 %s 岗 %s），按空出", gid, kind, exc_info=True)
        return ""
    if not items:
        return ""
    if str(kind) == "task":
        # 通用执行：列名字 + description（主模型排计划时用它决定要 read_skill 哪份）
        lines = []
        for it in items:
            name = str(it.get("name") or "").strip()
            if not name:
                continue
            desc = str(it.get("description") or "").strip()
            lines.append(f"- 本群/{name}：{desc}" if desc else f"- 本群/{name}")
        if not lines:
            return ""
        title = _kind_title("task", titles)
        return f"【本群{title}的做法（MaiWork 总结的，是参考）】\n" + "\n".join(lines)
    # 专岗：至多 1 份
    body = str(items[0].get("body") or "").strip()
    if not body:
        return ""
    title = _kind_title(str(kind), titles)
    return f"【本群{title}的做法（MaiWork 总结的，是参考）】\n" + body


def group_context(
    agents: Any,
    gid: str,
    kind: str,
    *,
    include_rules: bool = True,
    include_skill: bool = True,
    custom_titles: dict[str, str] | None = None,
) -> str:
    """输出「这个群该怎么做」统一两段。agents 只是 tools/_scrub 之类就 getattr 不出
    group_rules_get → 空串（调用方硬回落）。"""
    gid_s = str(gid or "").strip()
    if agents is None or not gid_s:
        return ""
    parts: list[str] = []
    if include_rules:
        block = _rules_block(agents, gid_s)
        if block:
            parts.append(block)
    if include_skill and kind != "main":
        block = _skill_block(agents, gid_s, str(kind), titles=custom_titles)
        if block:
            parts.append(block)
    if not parts:
        return ""
    if len(parts) == 2:
        head = ("下面这段里的「规矩」是管理员定的硬规矩，「做法」是 MaiWork 从验收和反馈"
                "里总结的参考；冲突时以规矩为准。\n")
    else:
        # 只有一段：标题里已明说「参考 / 必须照做」
        head = ""
    return head + "\n\n".join(parts) + "\n"
