"""tools_groupspace.py（群空间工具，roles={"main"}，docs/02-设计.md §10）：

- group_files_list：看群文件（根目录或指定文件夹）。
- group_file_manage：delete / rename / move / mkdir（只动机器人自己传的文件）。
- group_notice_send：发群公告（先在群里说一句预告；每群每天最多 1 条）。
- group_album_upload：把工作区里的成品图传进群相册。

由主模型决定、子 agent 不直接调。能力不具备 → 工具返回中文原因
（适配器没开放 / 机器人不是管理员 / 只能动我自己传的文件）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from .tools import Tool, ToolContext, ToolResult, Tools
from .tools_admin import _check_upload_path

logger = logging.getLogger("maiwork.tools_groupspace")

_ACTIONS = ("delete", "rename", "move", "mkdir")


def register_groupspace_tools(
    tools: Tools,
    group_space: Any,  # platforms.qq_onebot.GroupSpace
    *,
    announce: Callable[[str, str], Awaitable[None]],
    get_settings: Callable[[], Any] | None = None,  # 给了就校验相册路径在工作区内
) -> None:
    """把群空间工具注册进 tools。announce = 发公告前在群里说一句的回调（app 接 outbox）。

    get_settings：相册上传路径校验用（workspace_root / workspace_of）；没给的场景
    （纯能力测试）不校验路径——线上 app 一定会给。
    """

    def _workspace_dir_of(gid: str) -> Path | None:
        if get_settings is None:
            return None
        try:
            settings = get_settings()
        except Exception:
            return None
        root = getattr(settings, "workspace_root", None)
        if not root:
            return None
        workspace_of = getattr(settings, "workspace_of", None)
        name = workspace_of(gid) if callable(workspace_of) else f"g{gid}"
        return Path(root) / str(name or f"g{gid}")

    async def group_files_list(ctx: ToolContext, args: dict) -> ToolResult:
        gid = str(ctx.group_id or "").strip()
        folder_id = str(args.get("folder_id") or "").strip() or None
        try:
            items = await group_space.list_files(gid, folder_id=folder_id)
        except PermissionError as e:
            return ToolResult(ok=False, output="", error=str(e))
        except Exception as e:
            logger.exception("列群文件出错（群 %s）", gid)
            return ToolResult(ok=False, output="", error=f"列群文件失败：{e}")
        if not items:
            return ToolResult(ok=True, output="（空的，一个文件也没有）", data=[])
        lines = []
        for it in items:
            if it.get("type") == "folder":
                lines.append(f"[文件夹] {it.get('name', '')}（folder_id={it.get('folder_id', '')}）")
            else:
                lines.append(f"[文件] {it.get('name', '')}（file_id={it.get('file_id', '')}，{it.get('size', 0)} 字节）")
        return ToolResult(ok=True, output="\n".join(lines), data=items)

    async def group_file_manage(ctx: ToolContext, args: dict) -> ToolResult:
        gid = str(ctx.group_id or "").strip()
        action = str(args.get("action") or "").strip().lower()
        file_id = str(args.get("file_id") or "").strip()
        name = str(args.get("name") or "").strip()
        folder_id = str(args.get("folder_id") or "").strip()
        if action not in _ACTIONS:
            return ToolResult(ok=False, output="", error=f"action 只能是：{'、'.join(_ACTIONS)}")
        try:
            if action == "delete":
                if not file_id:
                    return ToolResult(ok=False, output="", error="delete 要给 file_id")
                await group_space.delete_file(gid, file_id)
                return ToolResult(ok=True, output=f"已删除群文件 {file_id}")
            if action == "rename":
                if not (file_id and name):
                    return ToolResult(ok=False, output="", error="rename 要给 file_id 和 name")
                await group_space.rename_file(gid, file_id, name)
                return ToolResult(ok=True, output=f"已把文件改名为「{name}」")
            if action == "move":
                if not (file_id and folder_id):
                    return ToolResult(ok=False, output="", error="move 要给 file_id 和 folder_id")
                await group_space.move_file(gid, file_id, folder_id)
                return ToolResult(ok=True, output=f"已把文件移到文件夹 {folder_id}")
            # mkdir
            if not name:
                return ToolResult(ok=False, output="", error="mkdir 要给 name")
            await group_space.create_folder(gid, name, folder_id or None)
            return ToolResult(ok=True, output=f"已建文件夹「{name}」")
        except PermissionError as e:
            return ToolResult(ok=False, output="", error=str(e))
        except Exception as e:
            logger.exception("群文件管理出错（群 %s，%s）", gid, action)
            return ToolResult(ok=False, output="", error=f"群文件管理失败：{e}")

    async def group_notice_send(ctx: ToolContext, args: dict) -> ToolResult:
        gid = str(ctx.group_id or "").strip()
        content = str(args.get("content") or "").strip()
        if not content:
            return ToolResult(ok=False, output="", error="content 不能为空")
        try:
            await group_space.send_notice(gid, content, announce=announce)
        except PermissionError as e:
            return ToolResult(ok=False, output="", error=str(e))
        except ValueError as e:
            return ToolResult(ok=False, output="", error=str(e))
        except Exception as e:
            logger.exception("发群公告出错（群 %s）", gid)
            return ToolResult(ok=False, output="", error=f"发群公告失败：{e}")
        return ToolResult(ok=True, output=f"公告已发出（并已在群里预告）：{content[:30]}")

    async def group_album_upload(ctx: ToolContext, args: dict) -> ToolResult:
        gid = str(ctx.group_id or "").strip()
        path = str(args.get("path") or "").strip()
        album_id = str(args.get("album_id") or "").strip()
        if not path:
            return ToolResult(ok=False, output="", error="path 不能为空（工作区内成品图的绝对路径）")
        # 路径必须在这个群的工作区里、不许是符号链接（和 outbox / 管理员对话同一套口径），
        # 不然一个被注入的 path 能把服务器上任何文件传进群相册
        if get_settings is not None:
            try:
                _check_upload_path(path, _workspace_dir_of(gid))
            except ValueError as e:
                return ToolResult(ok=False, output="", error=str(e))
        try:
            await group_space.upload_to_album(gid, album_id, path)
        except PermissionError as e:
            return ToolResult(ok=False, output="", error=str(e))
        except Exception as e:
            logger.exception("传群相册出错（群 %s）", gid)
            return ToolResult(ok=False, output="", error=f"传群相册失败：{e}")
        return ToolResult(ok=True, output=f"已把 {path} 传进群相册 {album_id or '（默认相册）'}")

    tools.register(
        Tool(
            name="group_files_list",
            description="看本群的群文件（群空间）。根目录或指定文件夹；适配器旧版时不可用。",
            parameters={
                "type": "object",
                "properties": {
                    "folder_id": {"type": "string", "description": "文件夹 ID；空 = 根目录（可选）"},
                },
            },
            roles=frozenset({"main"}),
            handler=group_files_list,
            summarize=lambda args, res: (
                f"看群文件{('（文件夹 ' + str(args.get('folder_id')) + '）') if args.get('folder_id') else ''}",
                res.output.splitlines()[0] if res.ok and res.output else (res.error or "没列出东西"),
            ),
            timeout_s=30.0,
        )
    )
    tools.register(
        Tool(
            name="group_file_manage",
            description="管理本群群文件：delete 删 / rename 改名 / move 移文件夹 / mkdir 建文件夹。只能动机器人自己传的文件。",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(_ACTIONS), "description": "delete / rename / move / mkdir"},
                    "file_id": {"type": "string", "description": "文件 ID（delete / rename / move 必填）"},
                    "name": {"type": "string", "description": "新名字（rename）或文件夹名（mkdir）"},
                    "folder_id": {"type": "string", "description": "目标文件夹 ID（move）或父文件夹（mkdir，可选）"},
                },
                "required": ["action"],
            },
            roles=frozenset({"main"}),
            handler=group_file_manage,
            summarize=lambda args, res: (
                f"群文件 {args.get('action', '')}：{args.get('file_id') or args.get('name') or ''}",
                "完成" if res.ok else (res.error or "失败"),
            ),
            timeout_s=30.0,
        )
    )
    tools.register(
        Tool(
            name="group_notice_send",
            description="发本群群公告。会先在群里说一句「我要发一条群公告：…」，每群每天最多 1 条。机器人是群主或管理员才行。",
            parameters={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "公告内容"},
                },
                "required": ["content"],
            },
            roles=frozenset({"main"}),
            handler=group_notice_send,
            summarize=lambda args, res: (
                f"发群公告：{str(args.get('content', ''))[:30]}",
                "已发" if res.ok else (res.error or "失败"),
            ),
            timeout_s=30.0,
        )
    )
    tools.register(
        Tool(
            name="group_album_upload",
            description="把工作区里的一张成品图传进本群群相册。机器人是群主或管理员才行；适配器旧版时不可用。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区内成品图的绝对路径"},
                    "album_id": {"type": "string", "description": "相册 ID；空 = 默认相册（〔待实测〕可能必填）"},
                },
                "required": ["path", "album_id"],
            },
            roles=frozenset({"main"}),
            handler=group_album_upload,
            summarize=lambda args, res: (
                f"传群相册：{args.get('path', '')}",
                "完成" if res.ok else (res.error or "失败"),
            ),
            timeout_s=60.0,
        )
    )
