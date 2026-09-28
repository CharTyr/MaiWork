"""MCP（Streamable HTTP）最小客户端——只覆盖 search.py 调 tavily_search 用到的交互。

交互事实（2026-09-27 对第三方托管的 Tavily MCP 端点实测，详见 docs/06-宿主接口事实.md）：
- URL 形如 https://<host>/mcp；鉴权头 `Authorization: Bearer <key>`；请求头带
  `Content-Type: application/json` 和 `Accept: application/json, text/event-stream`。
- 三步：POST initialize（响应 200 + `mcp-session-id` 头 + SSE 正文）→
  POST notifications/initialized（带 Mcp-Session-Id 头，202 空正文）→ POST tools/call。
- 响应既可能是 SSE（`data:` 行里一条 JSON-RPC）也可能是纯 JSON；按响应体解析，不认 content-type。
- 会话失效：initialize 以外返回 HTTP 404，或 400 且错误文字提到 session——清掉会话，
  重新 initialize 后重试一次。
- tools/call 的业务失败：`result.isError == true`，content[0].text 是错误文字；
  协议失败走标准 JSON-RPC error。两者和 HTTP 非 2xx 一律 → MCPError（消息已遮密钥、截 300 字）。

并发安全：initialize 走 asyncio.Lock，锁内双检，并发调用只建一个会话。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

logger = logging.getLogger("maiwork.mcp")

_PROTOCOL_VERSION = "2025-03-26"
_MCP_SESSION_HEADER = "mcp-session-id"
_REQUEST_SESSION_HEADER = "Mcp-Session-Id"


class MCPError(Exception):
    """MCP 调用失败（网络/HTTP/JSON-RPC error/isError/解析）；message 已遮密钥再抛。"""


class McpSessionClient:
    """懒初始化 + 会话缓存 + 短时连接：一次调用 = initialize（如需要）+ 若干次 POST。

    调用方保证 settings 里的 URL/密钥稳定；长生命周期要释放连接用 aclose()。
    """

    def __init__(
        self, url: str, key: str, *, timeout_s: int = 20, transport: Any = None, headers: dict[str, str] | None = None
    ) -> None:
        self._url = str(url or "").strip()
        self._key = str(key or "")
        self._timeout = max(1, int(timeout_s))
        self._transport = transport
        # 额外请求头（extensions 的 MCP 用；含密钥，只进请求，不落日志）
        self._extra_headers: dict[str, str] = {str(k): str(v) for k, v in (headers or {}).items()}
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None
        # 无状态服务端（initialize 不给 mcp-session-id，如 You.com）：初始化过一次就够，不必每次重来
        self._ready = False
        self._init_lock = asyncio.Lock()
        self._rpc_id = 0

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        client, self._client = self._client, None
        self._session_id = None
        self._ready = False
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # 关闭失败不影响其他清理
                logger.warning("MCP 客户端关闭连接出错", exc_info=True)

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, session_retry: bool = True
    ) -> dict[str, Any]:
        """tools/call → result（dict，通常含 content/isError 字段）。

        会话失效（404，或 400 且错误提到 session）→ 清会话、重连一次再调；连接失败不重试。
        """
        await self._ensure_session()
        try:
            result = await self._tool_call_once(name, arguments)
        except MCPError as e:
            if not session_retry or not _session_expired(e):
                raise
            logger.info("MCP 会话失效，重新 initialize 后重试一次")
            await self._reconnect()
            result = await self._tool_call_once(name, arguments)
        if not isinstance(result, dict):
            raise MCPError("tools/call 的 result 不是对象")
        if result.get("isError"):
            raise MCPError(self._masked(f"MCP 工具报错：{_content_text(result)}"))
        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        """tools/list → 服务端声明的工具列表（[{"name","description","inputSchema"}…]）。

        供扩展（extensions.py）启动时拿工具清单；非列表按空处理。
        """
        await self._ensure_session()
        payload = self._request_payload("tools/list", {})
        resp = await self._send(payload, include_session=True)
        result = self._expect_result(resp, payload["id"])
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            return []
        return [t for t in tools if isinstance(t, dict)]

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(transport=self._transport, timeout=self._timeout)
        return self._client

    async def _ensure_session(self) -> None:
        if self._session_id or self._ready:
            return
        async with self._init_lock:
            if self._session_id or self._ready:
                return
            await self._initialize_once()

    async def _reconnect(self) -> None:
        self._session_id = None
        self._ready = False
        async with self._init_lock:
            if self._session_id:
                return
            await self._initialize_once()

    async def _initialize_once(self) -> None:
        payload = self._request_payload(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "maiwork", "version": "0.1.0"},
            },
        )
        resp = await self._send(payload, include_session=False)
        result = self._expect_result(resp, payload["id"])
        server = result.get("serverInfo") if isinstance(result, dict) else None
        server_name = ""
        if isinstance(server, dict):
            server_name = str(server.get("name") or "")
        self._session_id = resp.headers.get(_MCP_SESSION_HEADER) or None
        if not self._session_id:
            logger.info("MCP initialize 响应没带 mcp-session-id 头（无状态服务端），后续请求不带会话头")
        logger.info("MCP 会话已建立：server=%s session=%s", server_name or "?", "有" if self._session_id else "无")
        notify = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        try:
            await self._send(notify, include_session=True)
        except MCPError:
            # 通知失败就清掉刚拿的会话，下一次调用会完整重连；本次失败由调用方看到
            self._session_id = None
            self._ready = False
            raise
        self._ready = True

    # ------------------------------------------------------------------
    # 单次请求
    # ------------------------------------------------------------------

    async def _tool_call_once(self, name: str, arguments: dict[str, Any]) -> Any:
        payload = self._request_payload("tools/call", {"name": name, "arguments": arguments})
        resp = await self._send(payload, include_session=True)
        return self._expect_result(resp, payload["id"])

    def _request_payload(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._rpc_id += 1
        return {"jsonrpc": "2.0", "id": self._rpc_id, "method": method, "params": params}

    async def _send(self, payload: dict[str, Any], *, include_session: bool) -> httpx.Response:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        headers.update(self._extra_headers)
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        if include_session and self._session_id:
            headers[_REQUEST_SESSION_HEADER] = self._session_id
        try:
            resp = await self._http().post(self._url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise MCPError(self._masked(f"MCP 请求失败：{e}")) from None
        if not 200 <= resp.status_code < 300:
            raise MCPError(self._masked(f"MCP 服务返回 HTTP {resp.status_code}：{resp.text}"))
        return resp

    def _expect_result(self, resp: httpx.Response, rpc_id: Any) -> Any:
        """SSE / 纯 JSON → 匹配的 JSON-RPC 响应 → result；error 抛 MCPError。"""
        try:
            message = _parse_message(resp, rpc_id)
        except ValueError as e:
            raise MCPError(self._masked(f"MCP 返回解析失败：{e}")) from None
        if not isinstance(message.get("result"), dict):
            raise MCPError("MCP 返回里没有 result 对象")
        return message["result"]

    def _masked(self, text: str) -> str:
        out = str(text or "")
        if self._key:
            out = out.replace(self._key, "***")
        # 额外头里的值也可能被服务端回显进错误文本，一律遮掉
        for v in self._extra_headers.values():
            if v:
                out = out.replace(v, "***")
        return out[:300]


def _session_expired(error: MCPError) -> bool:
    """错误看起来是会话失效：HTTP 404，或 400 且错误文字提到 session（大小写不敏感）。"""
    msg = str(error)
    lower = msg.lower()
    return "http 404" in lower or ("http 400" in lower and "session" in lower)


def _content_text(result: dict[str, Any]) -> str:
    """tool result 的 content[0].text；没有就是整段 JSON 兜底。"""
    content = result.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and isinstance(first.get("text"), str):
            return first["text"]
    try:
        return json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(result)


def _parse_message(resp: httpx.Response, rpc_id: Any) -> dict[str, Any]:
    """从响应体里取出匹配的 JSON-RPC 响应对象；按正文内容判型，不看 content-type。

    - 正文是 JSON 对象 → 直接是它。
    - 正文是 SSE（text/event-stream）→ 逐 data: 行取 JSON，挑 id 匹配且带 result/error 的那条。
    """
    text = resp.text or ""
    stripped = text.strip()
    candidates: list[str] = []
    first_char = stripped.lstrip("\ufeff")[:1]
    if first_char in "{[":
        candidates.append(stripped)
    else:
        candidates.extend(_sse_data_lines(text))
    if not candidates:
        raise ValueError(f"响应体是空的：{_preview(text)}")

    fallback: dict[str, Any] | None = None
    for chunk in candidates:
        try:
            msg = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict) or not isinstance(msg.get("jsonrpc"), str):
            continue
        if msg.get("method") and "result" not in msg and "error" not in msg:
            continue  # 这是请求/通知帧，不是响应
        if msg.get("error") is not None:
            raise ValueError(f"JSON-RPC error：{json.dumps(msg['error'], ensure_ascii=False)}")
        if msg.get("id") == rpc_id:
            return msg
        if fallback is None and "result" in msg:
            fallback = msg  # id 对不上：先记着，全找完没匹配再用它
    if fallback is not None:
        return fallback
    raise ValueError(f"响应体里找不到 id={rpc_id} 的 JSON-RPC 响应：{_preview(text)}")


def _sse_data_lines(text: str) -> list[str]:
    """SSE 的 data: 行按块拼起来，块之间空行分开。event:/id:/注释等一律忽略。"""
    out: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            if current:
                out.append("\n".join(current))
                current = []
            continue
        if line.startswith("data:"):
            current.append(line[5:].lstrip(" "))
    if current:
        out.append("\n".join(current))
    return out


def _preview(text: str) -> str:
    """响应体兜底展示：换行转义、截 200 字。"""
    return (text or "")[:200].replace("\n", "\\n") or "<空>"
