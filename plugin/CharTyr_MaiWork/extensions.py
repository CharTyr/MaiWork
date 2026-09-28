"""MCP 扩展（docs/02-设计.md §10「插件（skill + MCP）」、docs/07 §10.9）。

MaiWork 自己加载、只注册进自己的 Tools（**不挂到 MaiBot 的 planner 上**，红线）。

- 配置：[[extensions.mcp]]（name / url(必须 https://) / headers(表) / tools(白名单) /
  roles("worker" 默认，可含 "main") / enabled）；规范化在 config.py。
- start(tools)：对每个启用的 MCP 建 McpSessionClient → initialize → tools/list，
  工具注册成 mcp_<name>_<工具名>（非法字符 → _，总长 ≤64；撞名/非法跳过并记日志），
  参数透传 inputSchema，调用走 tools/call。
- 调用结果：result.content 里 type=="text" 的 text 全部拼接（截 8000 字）；
  isError → ToolResult(ok=False)。每次调用照常经 Tools.call 落 tool_calls 表。
- **连接失败不影响启动**：该扩展标 ok=False、error 记原因，健康和 settings 视图展示。
- reload(name, tools)：管理员接口用——重连 + 刷工具清单；返回 {"ok", "tools", "error"}，
  不带 headers。
- headers 里的密钥只由代码读取：app._known_secrets 把它们并进 Tools 的最终遮罩。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from .config import McpExtensionSetting
from .mcp_client import MCPError, McpSessionClient
from .tools import Tool, ToolContext, ToolResult, Tools

logger = logging.getLogger("maiwork.extensions")

_TOOL_NAME_MAX = 64            # tools.py 的注册上限
_OUTPUT_MAX = 8000             # 返回文本截 8000 字
_NAME_CLEAN_RE = re.compile(r"[^A-Za-z0-9_-]")


def _host_only(url: str) -> str:
    """URL 只留 https://host[:port]（去掉路径和查询，防 query 里带 token 被回显/落日志）。"""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return ""
    if not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def _registered_name(ext_name: str, tool_name: str) -> str:
    """mcp_<name>_<工具名>：非法字符替换成 _，总长 ≤64。"""
    raw = f"mcp_{ext_name}_{tool_name}"
    return _NAME_CLEAN_RE.sub("_", raw)[:_TOOL_NAME_MAX]


class _McpExt:
    """一个 MCP 扩展的运行状态：client（连得上才有）、工具名映射、健康信息。"""

    def __init__(self, setting: McpExtensionSetting, *, transport: Any) -> None:
        self.setting = setting
        self.client: McpSessionClient | None = None
        if setting.enabled:
            self.client = McpSessionClient(
                setting.url,
                "",
                timeout_s=setting.timeout_s,
                transport=transport,
                headers=dict(setting.headers),
            )
        self.tools: dict[str, str] = {}  # 注册名 → 远端工具名
        self.tool_specs: dict[str, dict] = {}  # 远端工具名 → {"name","description","inputSchema"}（搜索绑定 / 候选清单用）
        self.ok = False
        self.error = ""

    def tools_remote(self) -> dict[str, dict]:
        """远端工具名 → spec（搜索绑定 / 候选清单用；未连接时为空）。"""
        return dict(self.tool_specs)


class Extensions:
    """[[extensions.mcp]] 的运行体：启动连接、注册工具、健康信息、reload。

    store 给了就把网页加的条目一起合并（extensions_web.merged_entries）；
    不给（老测试）只看 config.toml 的 [[extensions.mcp]]，行为和以前一模一样。
    """

    def __init__(self, get_settings: Callable[[], Any], *, transport: Any = None, store: Any = None) -> None:
        self._get_settings = get_settings
        self._transport = transport
        self._store = store
        self._exts: dict[str, _McpExt] = {}

    def _entries(self) -> tuple[McpExtensionSetting, ...]:
        """生效的 MCP 条目：config + 网页合并（网页同名优先、disabled 名单盖开关）。
        每次现读 kv——网页改完立刻生效，没有缓存问题。"""
        settings = self._get_settings()
        config_entries = getattr(getattr(settings, "extensions", None), "mcp", ()) or ()
        if self._store is None:
            return tuple(config_entries)
        try:
            from .extensions_web import merged_entries

            return merged_entries(settings, self._store)
        except Exception:
            logger.exception("合并网页 MCP 条目出错，本次只用配置文件里的")
            return tuple(config_entries)

    # ------------------------------------------------------------------
    # 启动 / 关闭
    # ------------------------------------------------------------------

    async def start(self, tools: Tools) -> None:
        """对每个启用的 MCP 连接并注册工具；单个失败不影响其他、不影响插件启动。"""
        self._exts = {}
        for setting in self._entries():
            ext = _McpExt(setting, transport=self._transport)
            self._exts[setting.name] = ext
            if not setting.enabled:
                continue
            await self._connect_and_register(ext, tools)

    async def aclose(self) -> None:
        exts, self._exts = self._exts, {}
        for ext in exts.values():
            if ext.client is not None:
                try:
                    await ext.client.aclose()
                except Exception:
                    logger.warning("关闭 MCP 扩展 %s 的连接出错", ext.setting.name, exc_info=True)

    # close() 的别名，app 用 getattr 找 "close"
    close = aclose

    async def _connect_and_register(self, ext: _McpExt, tools: Tools) -> None:
        """initialize → tools/list → 注册。任何失败：ok=False、error 记中文原因，不抛。"""
        ext.tools = {}
        ext.tool_specs = {}
        ext.ok = False
        ext.error = ""
        client = ext.client
        setting = ext.setting
        if client is None:
            return
        try:
            remote_tools = await client.list_tools()
        except MCPError as e:
            ext.error = f"连不上：{e}"
            logger.warning("MCP 扩展 %s（%s）连接失败：%s", setting.name, _host_only(setting.url), ext.error)
            return
        except Exception as e:
            ext.error = f"连接出错：{type(e).__name__}"
            logger.warning("MCP 扩展 %s 连接出意外错：%s", setting.name, type(e).__name__, exc_info=True)
            return
        whitelist = set(setting.tools)
        registered = 0
        for spec in remote_tools:
            remote_name = str(spec.get("name") or "").strip()
            if not remote_name:
                continue
            ext.tool_specs[remote_name] = {
                "name": remote_name,
                "description": str(spec.get("description") or ""),
                "inputSchema": spec.get("inputSchema") if isinstance(spec.get("inputSchema"), dict) else {},
            }
            if whitelist and remote_name not in whitelist:
                continue
            name = _registered_name(setting.name, remote_name)
            schema = spec.get("inputSchema")
            parameters = schema if isinstance(schema, dict) else {"type": "object", "properties": {}}
            description = str(spec.get("description") or "").strip() or f"MCP 扩展 {setting.name} 的工具 {remote_name}"
            try:
                tools.register(
                    Tool(
                        name=name,
                        description=description,
                        parameters=parameters,
                        roles=frozenset(setting.roles),
                        handler=self._make_handler(ext, remote_name),
                        timeout_s=float(setting.timeout_s + 10),
                    )
                )
            except ValueError:
                # 撞名 / 名字不合法：跳过这个工具，不拖垮整家
                logger.warning("MCP 扩展 %s 的工具 %s 注册名 %s 冲突/不合法，已跳过", setting.name, remote_name, name)
                continue
            ext.tools[name] = remote_name
            registered += 1
        ext.ok = True
        logger.info("MCP 扩展 %s 已连上：%d 个工具可用", setting.name, registered)

    def _make_handler(self, ext: _McpExt, remote_name: str) -> Any:
        """mcp_<name>_<工具名> 的 handler：tools/call → 文本拼接（截 8000）；isError → ok=False。"""

        async def _handler(ctx: ToolContext, args: dict) -> ToolResult:
            client = ext.client
            if client is None:
                return ToolResult(ok=False, output="", error=f"MCP 扩展 {ext.setting.name} 没连上")
            try:
                result = await client.call_tool(remote_name, args if isinstance(args, dict) else {})
            except MCPError as e:
                return ToolResult(ok=False, output="", error=f"MCP 扩展 {ext.setting.name} 调用失败：{e}")
            except Exception as e:
                logger.warning("MCP 扩展 %s 调用 %s 出意外错", ext.setting.name, remote_name, exc_info=True)
                return ToolResult(ok=False, output="", error=f"MCP 扩展 {ext.setting.name} 调用出错：{type(e).__name__}")
            is_error = bool(result.get("isError"))
            text = _join_content_text(result)
            if not text:
                try:
                    text = json.dumps(result, ensure_ascii=False)
                except (TypeError, ValueError):
                    text = str(result)
            if len(text) > _OUTPUT_MAX:
                text = text[: _OUTPUT_MAX - 10] + "\n……（已截断）"
            if is_error:
                return ToolResult(ok=False, output=text, error=text[:500])
            return ToolResult(ok=True, output=text)

        return _handler

    # ------------------------------------------------------------------
    # 健康信息 / reload
    # ------------------------------------------------------------------

    def info(self) -> list[dict[str, Any]]:
        """各 MCP 扩展的健康信息（settings/健康视图用；不带 headers，密钥不回显）。"""
        out: list[dict[str, Any]] = []
        for setting in self._entries():
            ext = self._exts.get(setting.name)
            out.append(
                {
                    "name": setting.name,
                    "url": _host_only(setting.url),
                    "enabled": bool(setting.enabled),
                    "ok": bool(ext.ok) if ext is not None else False,
                    "tools": len(ext.tools) if ext is not None else 0,
                    "error": str(ext.error) if ext is not None else "",
                }
            )
        return out

    async def reload(self, name: str, tools: Tools) -> dict | None:
        """重连一个扩展并刷新工具清单。返回 None = 根本没配这个名字。

        旧注册名先摘掉（Tools.unregister 没有就先整个重建注册表，见 tools.py），
        再按新的 tools/list 重新注册。失败只影响这一家：ok=False、error 记原因。
        """
        setting = next((e for e in self._entries() if e.name == str(name or "")), None)
        if setting is None:
            return None
        ext = _McpExt(setting, transport=self._transport)
        old = self._exts.pop(setting.name, None)
        # 摘掉旧工具
        if old is not None and old.tools:
            try:
                for old_name in list(old.tools.keys()):
                    tools.unregister(old_name)
            except Exception:
                logger.exception("摘掉 MCP 扩展 %s 的旧工具出错", setting.name)
        if old is not None and old.client is not None:
            try:
                await old.client.aclose()
            except Exception:
                logger.warning("关闭 MCP 扩展 %s 的旧连接出错", setting.name, exc_info=True)
        self._exts[setting.name] = ext
        if setting.enabled:
            await self._connect_and_register(ext, tools)
        return {"ok": bool(ext.ok), "tools": len(ext.tools), "error": str(ext.error or "")}

    async def remove(self, name: str, tools: Tools) -> bool:
        """网页删除一个扩展：摘掉它的工具、关连接、从运行状态里移除。没有 → False。"""
        ext = self._exts.pop(str(name or ""), None)
        if ext is None:
            return False
        if ext.tools:
            try:
                for old_name in list(ext.tools.keys()):
                    tools.unregister(old_name)
            except Exception:
                logger.exception("摘掉 MCP 扩展 %s 的工具出错", name)
        if ext.client is not None:
            try:
                await ext.client.aclose()
            except Exception:
                logger.warning("关闭 MCP 扩展 %s 的连接出错", name, exc_info=True)
        return True

    def runtime_of(self, name: str) -> Any:
        """某个扩展的运行状态（_McpExt）；没有 → None。console 拼视图用。"""
        return self._exts.get(str(name or ""))

    def tool_spec(self, ext_name: str, tool_name: str) -> dict | None:
        """某个扩展某个远端工具的 spec（含 inputSchema）；扩展没连上 / 没这工具 → None。

        搜索绑定校验（save_binding）和 search.py 的参数映射用它。
        """
        ext = self._exts.get(str(ext_name or ""))
        if ext is None:
            return None
        return ext.tool_specs.get(str(tool_name or ""))


def _join_content_text(result: dict[str, Any]) -> str:
    """result.content 里 type=="text" 的 text 全部拼接（换行分隔）。"""
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(p for p in parts if p)
