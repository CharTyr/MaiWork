"""skill 扩展的两个工具：list_skills / read_skill（docs/02 §10、docs/07 §10.9）。

两个角色（worker / main）都能调，但**内容按调用者角色过滤**：主模型只看到 / 只读得到
roles 含 main 的 skill，子 agent 只看到 / 只读得到 roles 含 worker 的（roles 见
skills.py；调用者角色从 ToolContext 判，role 空则按 actor 引导式判断）。
skill 目录只读；子 agent 要用里面的脚本，得 read_skill 出来后
write_file 到自己工作区再跑（绝不直接执行数据目录里的东西）。
"""

from __future__ import annotations

import logging
from typing import Any

from .skills import Skills
from .tools import Tool, ToolContext, ToolResult, Tools, role_of

logger = logging.getLogger("maiwork.skills_tools")


def register_skill_tools(tools: Tools, skills: Skills) -> None:
    """把 list_skills / read_skill 注册进 tools（MaiWork 自己的注册表，不挂 MaiBot planner）。"""

    # 让 coordinator 能问「现在有没有给主模型的 skill」（排计划回合据此决定给不给 skill 工具）
    try:
        tools.skill_registry = skills  # type: ignore[attr-defined]
    except Exception:
        pass

    def _allowed_skills(ctx: ToolContext) -> tuple[str, ...] | None:
        """专岗（specialists.py）给的本轮 skill 白名单；None = 通才不过滤."""
        allowed = getattr(ctx, "allowed_skills", None)
        if allowed is None:
            return None
        try:
            return tuple(str(x) for x in allowed)
        except Exception:
            return None

    async def list_skills(ctx: ToolContext, args: dict) -> ToolResult:
        role = role_of(ctx)
        try:
            items = skills.list(role)
        except Exception:
            logger.exception("list_skills 读目录出错")
            return ToolResult(ok=False, output="", error="读 skill 目录出错了")
        # 岗位白名单（专岗）：只列交集；roles/全局开关已由 skills.list 处理。
        allowed = _allowed_skills(ctx)
        if allowed is not None:
            allowed_set = set(allowed)
            items = [i for i in items if str(i.get("name") or "") in allowed_set]
        if not items:
            return ToolResult(
                ok=True,
                output="现在还没有你能用的 skill（管理员还没放，或者都是给另一个角色用的）",
                data=[],
            )
        lines = []
        for item in items:
            desc = str(item.get("description") or "").strip()
            lines.append(f"- {item.get('name')}：{desc}" if desc else f"- {item.get('name')}")
        return ToolResult(
            ok=True,
            output="可用的 skill（用 read_skill 读全文）：\n" + "\n".join(lines),
            data=items,
        )

    async def read_skill(ctx: ToolContext, args: dict) -> ToolResult:
        name = str(args.get("name") or "").strip()
        if not name:
            return ToolResult(ok=False, output="", error="name 不能为空")
        role = role_of(ctx)
        # 角色对不上的 skill 当「没有」——不 leak 名字，也不给读（附属文件同理）
        skill_roles = skills.roles(name)
        if skill_roles is None or role not in skill_roles:
            return ToolResult(
                ok=False, output="", error=f"没有叫「{name}」的 skill（先用 list_skills 看有哪些）"
            )
        # 岗位白名单（专岗）：不在白名单当成「没有」；模型不能凭名字猜读。
        allowed = _allowed_skills(ctx)
        if allowed is not None and name not in allowed:
            return ToolResult(
                ok=False, output="", error=f"没有叫「{name}」的 skill（先用 list_skills 看有哪些）"
            )
        file_rel = str(args.get("file") or "").strip()
        try:
            if file_rel:
                text = skills.read_file(name, file_rel)
                if text is None:
                    return ToolResult(
                        ok=False,
                        output="",
                        error=f"读不到 skill「{name}」里的「{file_rel}」：不存在，或路径越界/是符号链接（不允许）",
                    )
                return ToolResult(ok=True, output=text, data={"name": name, "file": file_rel})
            text = skills.read(name)
            if text is None:
                return ToolResult(ok=False, output="", error=f"没有叫「{name}」的 skill（先用 list_skills 看有哪些）")
            return ToolResult(ok=True, output=text, data={"name": name})
        except Exception:
            logger.exception("read_skill 出错（%s %s）", name, file_rel)
            return ToolResult(ok=False, output="", error="读 skill 出错了")

    tools.register(
        Tool(
            name="list_skills",
            description="列出你能用的 skill（管理员放在数据目录 skills/ 下的技能说明；只列 roles 含你这个角色的），给出名字和一句描述。",
            parameters={"type": "object", "properties": {}},
            roles=frozenset({"main", "worker"}),
            handler=list_skills,
            summarize=lambda args, res: ("看有哪些 skill", res.output.splitlines()[0] if res.ok and res.output else (res.error or "空")),
            timeout_s=10.0,
        )
    )
    tools.register(
        Tool(
            name="read_skill",
            description="读一个 skill 的 SKILL.md 全文；给 file 参数则读它目录里的附属文件（只读，越界和符号链接会被拒）。要用里面的脚本就先把内容 write_file 到自己的工区再跑。",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "skill 名（list_skills 给出的）"},
                    "file": {"type": "string", "description": "附属文件的相对路径（可选；不给则读 SKILL.md）"},
                },
                "required": ["name"],
            },
            roles=frozenset({"main", "worker"}),
            handler=read_skill,
            summarize=lambda args, res: (
                f"读 skill：{args.get('name', '')}" + (f" 里的 {args.get('file')}" if args.get("file") else ""),
                ("读到 %d 字" % len(res.output)) if res.ok else (res.error or "读不到"),
            ),
            timeout_s=10.0,
        )
    )
