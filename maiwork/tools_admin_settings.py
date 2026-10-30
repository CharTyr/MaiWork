"""tools_admin_settings.py：管理员对话里「改设置」的那批工具（docs/02 §18）。

管理员在网页和主模型对话时，主模型能做到网页「设置 → 工具 / 专岗 / 模型」能做的事：

- MCP：mcp_add / mcp_update / mcp_test / mcp_reload
- 专岗：list_agents / agent_update / agent_create / agent_delete / agent_detail（只读看一个岗的身份、
  有效工具名单；给了服务群群号再带本群记忆 / 本群做法 / 规矩里相关的条目）
- 端点与模型库：list_models / endpoint_save / endpoint_delete / model_save / model_delete

**不另写规则**：校验和写入一律调网页接口背后的同一套函数——MCP 走 extensions_web
（create / update / try_connect，和 svc.reload_mcp），端点 / 模型库 / 自定义专岗走
settings_ops，改岗位走 svc.agents.update_profile（PUT /api/agents/{kind} 的落点）。
这里只多做两件事：

1. 确认门（用户定）：
   - 一律要确认（在 tools_admin.CONFIRM_TOOLS 里）：mcp_add、agent_delete、endpoint_delete、model_delete；
   - 看情形要确认（tools_admin._CONDITIONAL_CONFIRM）：mcp_update 改了地址 / roles 新加 main /
     工具白名单放宽（加工具名、从有白名单变成不限）；endpoint_save 新建端点 / 改地址 / 改协议
     （数据会发去新地址、换一种发法）；
   - 其余直接做：mcp_test、mcp_reload、MCP 只收窄的改动、改岗位模型三件套 / 做不动时换用 / 启停、
     新建自定义专岗、加 / 改模型库条目、改端点地址和协议以外的字段。
   要确认的场景先过同一套校验再写小票：不合格的当场拒，不留一张注定失败的票。
2. 密钥一律不经过对话（用户定）：参数表里根本没有 headers / api_key；模型硬塞了
   （或地址里夹查询串 / 用户名密码）也拒，并给出网页设置页的链接让管理员自己去填。
   读回来的东西照网页「只进不出」：只说密钥填没填、请求头只给名字。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from . import extensions_web, settings_ops
from .tools import ToolContext, ToolResult

logger = logging.getLogger("maiwork.tools_admin_settings")

# 参数名像密钥的（大小写 / 横线不敏感）：一律拒，指路网页
_SECRET_ARG_NAMES = frozenset({
    "api_key", "apikey", "key", "keys", "headers", "header", "remove_headers",
    "token", "access_token", "secret", "secrets", "password", "passwd",
    "authorization", "auth", "bearer", "cookie", "cookies", "credential", "credentials",
})
_SECRET_ARG_PARTS = ("api_key", "apikey", "secret", "password", "header", "authorization", "credential")

_MCP_FIELDS = ("name", "url", "tools", "roles", "enabled", "timeout_s")
_AGENT_FIELDS = ("model", "effort", "backup", "escalate", "enabled")
_ENDPOINT_FIELDS = ("name", "protocol", "base_url", "retries", "retry_delay_s", "max_concurrency", "max_rpm")
_MODEL_FIELDS = (
    "endpoint", "model", "name", "efforts", "vision", "context_window", "max_tokens",
    "use", "image_api", "image_default_size", "image_max_n",
)

_SUB_LABEL = {"extensions": "工具", "models": "模型", "agents": "专岗"}
_ROLE_ZH = {"worker": "子 agent", "main": "主模型"}


def _ok(output: str, data: Any = None) -> ToolResult:
    return ToolResult(ok=True, output=output, data=data)


def _bad(error: str) -> ToolResult:
    return ToolResult(ok=False, output="", error=error)


def settings_link(settings: Any, sub: str) -> str:
    """网页设置页的链接（console.public_url + /#/settings/<页>）；没公开网址 → ""。

    拼法和群链接（card_push.group_link 的 public_url + /#/…）同一个口径。
    """
    public_url = str(getattr(getattr(settings, "console", None), "public_url", "") or "").rstrip("/")
    return f"{public_url}/#/settings/{sub}" if public_url else ""


def where(settings: Any, sub: str) -> str:
    """「网页『设置 → 模型』（链接）」这样一句人话。"""
    label = f"网页「设置 → {_SUB_LABEL.get(sub, sub)}」"
    link = settings_link(settings, sub)
    return f"{label}（{link}）" if link else f"{label}（在 MaiWork 网页里打开）"


def secret_args(args: dict) -> list[str]:
    """参数里像密钥的那些名字。"""
    out: list[str] = []
    for k in args:
        low = str(k).strip().lower().replace("-", "_")
        if low in _SECRET_ARG_NAMES or any(p in low for p in _SECRET_ARG_PARTS):
            out.append(str(k))
    return out


def url_secret_problem(url: Any) -> str:
    """地址里夹着可能是密钥的东西（查询串 / 片段 / 用户名密码）→ 中文原因；干净 → ""。"""
    u = str(url or "").strip()
    if not u:
        return ""
    try:
        parts = urlsplit(u)
    except ValueError:
        return ""
    if parts.query or parts.fragment or "@" in (parts.netloc or ""):
        return "地址里带了查询参数或用户名密码，可能夹着密钥"
    return ""


def register_settings_tools(
    svc: Any,
    *,
    settings: Callable[[], Any],
    register: Callable[..., None],
    need_confirm: Callable[[str, dict, str], ToolResult | None],
) -> None:
    """把改设置的工具注册进管理员工具表（由 tools_admin.register_admin_tools 调）。"""

    # ------------------------------------------------------------------
    # 公共关卡
    # ------------------------------------------------------------------

    def _no_secret(args: dict, sub: str, url_keys: tuple[str, ...] = ()) -> ToolResult | None:
        bad = secret_args(args)
        if bad:
            return _bad(
                f"密钥不经过对话：参数里不能带 {'、'.join(bad)}。请管理员自己到{where(settings(), sub)}填写"
                "（网页上只进不出）；这次什么都没改。"
            )
        for key in url_keys:
            problem = url_secret_problem(args.get(key))
            if problem:
                return _bad(
                    f"{problem}。密钥不经过对话：请在这里只给不带参数的地址，"
                    f"密钥由管理员自己到{where(settings(), sub)}填写；这次什么都没改。"
                )
        return None

    def _unknown(args: dict, allowed: tuple[str, ...], hint: str = "") -> ToolResult | None:
        extra = sorted(str(k) for k in args if str(k) not in allowed)
        if extra:
            return _bad(f"不认识的参数：{'、'.join(extra)}（只认 {'、'.join(allowed)}）{hint}")
        return None

    def _pick(args: dict, fields: tuple[str, ...]) -> dict:
        return {k: args[k] for k in fields if k in args}

    # ------------------------------------------------------------------
    # 一、MCP（extensions_web + svc.reload_mcp，和网页「设置 → 工具」同一套）
    # ------------------------------------------------------------------

    def _ext_ready() -> ToolResult | None:
        if getattr(svc, "extensions", None) is None or getattr(svc, "store", None) is None or settings() is None:
            return _bad("这个 MaiWork 还没接 MCP 扩展管理，现在改不了")
        return None

    def _roles_zh(roles: Any) -> str:
        return "、".join(_ROLE_ZH.get(str(r), str(r)) for r in (roles or [])) or "子 agent"

    def _tools_zh(tools: Any) -> str:
        tools = list(tools or [])
        return f"只开放 {'、'.join(tools)}" if tools else "工具不限"

    async def _reload_view(name: str) -> str:
        """改完立刻重连（同网页），回一句连接状态。"""
        try:
            res = await svc.reload_mcp(name)
        except Exception:
            logger.exception("管理员对话改 MCP 扩展 %s 后重连出错", name)
            return "重连出错了，可以稍后让我再重连一次"
        if not isinstance(res, dict):
            return "这个扩展现在没在运转"
        if res.get("ok"):
            return f"已连上，{int(res.get('tools') or 0)} 个工具可用"
        return f"没连上：{res.get('error') or '原因不明'}"

    async def mcp_add(ctx: ToolContext, args: dict) -> ToolResult:
        deny = _no_secret(args, "extensions", ("url",)) or _unknown(args, _MCP_FIELDS)
        if deny is not None:
            return deny
        not_ready = _ext_ready()
        if not_ready is not None:
            return not_ready
        body = _pick(args, _MCP_FIELDS)
        try:  # 网页新增的同一套校验（名字 / https / roles / 重名），不落库
            entry = extensions_web.preview_create(svc.store, settings(), body)
        except FileExistsError as e:
            return _bad(str(e))
        except ValueError as e:
            return _bad(str(e))
        name = str(entry["name"])
        pending = need_confirm(
            "mcp_add", args,
            f"新增 MCP 扩展「{name}」：地址 {extensions_web.url_no_query(entry['url'])}，"
            f"给{_roles_zh(entry['roles'])}用，{_tools_zh(entry['tools'])}"
            f"{'' if entry['enabled'] else '，先不打开'}；之后 MaiWork 会连这个地址",
        )
        if pending is not None:
            return pending
        try:
            entry = extensions_web.create(svc.store, settings(), body)
        except FileExistsError as e:
            return _bad(str(e))
        except ValueError as e:
            return _bad(str(e))
        state = await _reload_view(name) if entry.get("enabled") else "先没打开"
        return _ok(
            f"已加上 MCP 扩展「{name}」（{state}）。如果这个服务要密钥（请求头），"
            f"请管理员自己到{where(settings(), 'extensions')}填写，密钥不经过对话。",
            data={k: v for k, v in entry.items() if k != "header_names"},
        )

    async def mcp_update(ctx: ToolContext, args: dict) -> ToolResult:
        deny = _no_secret(args, "extensions", ("url",)) or _unknown(args, _MCP_FIELDS)
        if deny is not None:
            return deny
        not_ready = _ext_ready()
        if not_ready is not None:
            return not_ready
        name = str(args.get("name") or "").strip()
        if not name:
            return _bad("要给 name")
        source = extensions_web.source_of(settings(), svc.store, name)
        if source is None:
            return _bad(f"没有叫「{name}」的 MCP 扩展")
        if source != "web":
            return _bad("这个扩展来自配置文件，只能开关不能改——请改 config.toml")
        body = {k: v for k, v in _pick(args, _MCP_FIELDS).items() if k != "name"}
        if not body:
            return _bad("没有要改的字段（可改 url / tools / roles / enabled / timeout_s）")
        try:  # 网页修改的同一套校验，先算出改前改后（不落库）
            old, new = extensions_web.preview_update(svc.store, name, body)
        except KeyError:
            return _bad(f"没有叫「{name}」的 MCP 扩展")
        except ValueError as e:
            return _bad(str(e))
        changes: list[str] = []
        loosen: list[str] = []
        if new.get("url") != old.get("url"):
            line = f"地址改成 {extensions_web.url_no_query(new.get('url'))}"
            changes.append(line)
            loosen.append("改地址")
        old_roles, new_roles = set(old.get("roles") or []), set(new.get("roles") or [])
        if old_roles != new_roles:
            changes.append(f"给{_roles_zh(sorted(new_roles))}用")
            if "main" in new_roles and "main" not in old_roles:
                loosen.append("主模型也能用")
        old_tools, new_tools = list(old.get("tools") or []), list(new.get("tools") or [])
        if old_tools != new_tools:
            changes.append(_tools_zh(new_tools))
            if old_tools and not new_tools:
                loosen.append("工具白名单放开成不限")
            elif old_tools and set(new_tools) - set(old_tools):
                loosen.append(f"白名单新加 {'、'.join(t for t in new_tools if t not in old_tools)}")
        if bool(new.get("enabled", True)) != bool(old.get("enabled", True)):
            changes.append("打开" if new.get("enabled") else "关掉")
        if new.get("timeout_s") != old.get("timeout_s"):
            changes.append(f"超时 {new.get('timeout_s')} 秒")
        if not changes:
            return _ok(f"MCP 扩展「{name}」没有变化，什么都没改")
        if loosen:
            pending = need_confirm(
                "mcp_update", args,
                f"改 MCP 扩展「{name}」：{'；'.join(changes)}（放宽：{'、'.join(loosen)}）",
            )
            if pending is not None:
                return pending
        try:
            extensions_web.update(svc.store, name, body)
        except KeyError:
            return _bad(f"没有叫「{name}」的 MCP 扩展")
        except ValueError as e:
            return _bad(str(e))
        state = await _reload_view(name)
        return _ok(f"已改 MCP 扩展「{name}」：{'；'.join(changes)}（{state}）")

    async def mcp_test(ctx: ToolContext, args: dict) -> ToolResult:
        deny = _no_secret(args, "extensions", ("url",)) or _unknown(args, ("name", "url", "timeout_s"))
        if deny is not None:
            return deny
        name = str(args.get("name") or "").strip()
        url = str(args.get("url") or "").strip()
        headers: dict[str, str] = {}
        if name:
            store = getattr(svc, "store", None)
            setting = next(
                (s for s, _src in extensions_web.merged(settings(), store) if s.name == name), None,
            ) if store is not None else None
            if setting is None:
                return _bad(f"没有叫「{name}」的 MCP 扩展")
            # 已存的请求头只发给它自己的地址：带着头去试别的地址等于把密钥送出去
            if url and url != setting.url:
                return _bad(
                    "测已存的 MCP 时不能换地址（它已存的请求头只发给自己的地址）。"
                    "想试一个新地址，只给 url、不给 name"
                )
            url = setting.url
            headers = extensions_web.stored_headers_for(store, settings(), name)
        if not url:
            return _bad("要给 name（测已存的）或 url（测一个新地址）")
        if not name:
            # 只给地址：挡内网 / 本机（主模型会读群聊，群友可能诱导它去探内网）。
            # 和 fetch_page / 装 skill 同一套检查（tools_builtin._host_is_forbidden：IP 字面量、
            # localhost、解析结果任一落在内网都拒，解析失败也拒）。网页「测试连接」不走这里，行为不变。
            from . import tools_builtin

            try:
                host = urlsplit(url).hostname or ""
            except ValueError:
                host = ""
            if await asyncio.to_thread(tools_builtin._host_is_forbidden, host, tools_builtin._default_resolver):
                return _bad(
                    f"这个地址（{host or '空'}）指向内网或本机，或者解析不出来，不去连。"
                    "要连内网里的 MCP，请管理员自己在网页「设置 → 工具」里加"
                )
        try:
            timeout_s = max(1, min(60, int(args.get("timeout_s") or 20)))
        except (TypeError, ValueError):
            timeout_s = 20
        try:
            res = await extensions_web.try_connect(
                url, headers, timeout_s=timeout_s, transport=getattr(svc, "extensions_transport", None),
            )
        except ValueError as e:
            return _bad(str(e))
        error = str(res.get("error") or "")
        for v in headers.values():  # 双保险：错误里万一带了头值
            if v:
                error = error.replace(v, "***")
        shown = extensions_web.url_no_query(url)
        if not res.get("ok"):
            return _ok(f"没连上 {shown}：{error or '原因不明'}", data={"ok": False, "tools": [], "error": error})
        tools = [str(t) for t in (res.get("tools") or [])]
        return _ok(
            f"连上了 {shown}，对方有 {len(tools)} 个工具：{'、'.join(tools) or '（一个也没有）'}"
            + ("" if name or tools else "。如果它要密钥才列得出工具，请管理员先在网页上填好再测"),
            data={"ok": True, "tools": tools, "error": ""},
        )

    async def mcp_reload(ctx: ToolContext, args: dict) -> ToolResult:
        deny = _unknown(args, ("name",))
        if deny is not None:
            return deny
        name = str(args.get("name") or "").strip()
        if not name:
            return _bad("要给 name")
        reloader = getattr(svc, "reload_mcp", None)
        if not callable(reloader):
            return _bad("MCP 扩展还没开")
        try:
            res = await reloader(name)
        except Exception as e:
            logger.exception("管理员对话重连 MCP 扩展 %s 出错", name)
            return _bad(f"重连出错：{type(e).__name__}")
        if res is None:
            return _bad(f"没有叫「{name}」的 MCP 扩展")
        if res.get("ok"):
            return _ok(f"已重连 MCP 扩展「{name}」，现在 {int(res.get('tools') or 0)} 个工具可用", data=dict(res))
        return _ok(f"重连了 MCP 扩展「{name}」，但没连上：{res.get('error') or '原因不明'}", data=dict(res))

    # ------------------------------------------------------------------
    # 二、专岗（svc.agents.update_profile / settings_ops.agent_*，和网页「设置 → 专岗」同一套）
    # ------------------------------------------------------------------

    def _agents() -> Any:
        return getattr(svc, "agents", None)

    def _profile_line(p: dict) -> str:
        bits = [f"{p.get('kind')}「{p.get('title')}」：{'开着' if p.get('enabled', True) else '停用'}"]
        bits.append(f"模型 {p.get('model') or '（没选，跟默认）'}" + (f"（强度 {p['effort']}）" if p.get("effort") else ""))
        if p.get("backup"):
            bits.append(f"备用 {p['backup']}")
        if p.get("escalate"):
            bits.append(f"做不动时换用 {p['escalate']}")
        return "，".join(bits)

    async def list_agents(ctx: ToolContext, args: dict) -> ToolResult:
        mod = _agents()
        if mod is None:
            return _bad("专岗功能还没开")
        rows = [
            {k: p.get(k) for k in ("kind", "title", "enabled", "model", "effort", "backup", "escalate")}
            for p in mod.profiles()
        ]
        lines = [f"- {_profile_line(r)}" for r in rows]
        return _ok("专岗（模型填的是模型库条目 id）：\n" + "\n".join(lines), data=rows)

    async def agent_update(ctx: ToolContext, args: dict) -> ToolResult:
        deny = _no_secret(args, "agents") or _unknown(
            args, ("kind", *_AGENT_FIELDS), f"；名字、人设、技能请管理员到{where(settings(), 'agents')}改",
        )
        if deny is not None:
            return deny
        mod = _agents()
        if mod is None:
            return _bad("专岗功能还没开")
        kind = str(args.get("kind") or "").strip()
        patch = _pick(args, _AGENT_FIELDS)
        if not patch:
            return _bad("没有要改的字段（可改 model / effort / backup / escalate / enabled）")
        try:  # PUT /api/agents/{kind} 的同一个落点
            p = mod.update_profile(kind, patch)
        except ValueError as e:
            return _bad(str(e) or "这个值改不了")
        except Exception as e:
            logger.exception("管理员对话改岗位 %s 出错", kind)
            return _bad(f"改岗位出错：{type(e).__name__}")
        return _ok(f"已改好：{_profile_line(p)}", data={k: p.get(k) for k in ("kind", "title", *_AGENT_FIELDS)})

    async def agent_create(ctx: ToolContext, args: dict) -> ToolResult:
        deny = _no_secret(args, "agents") or _unknown(args, ("title",))
        if deny is not None:
            return deny
        try:
            p = await settings_ops.agent_create_custom(svc, str(args.get("title") or ""))
        except settings_ops.OpError as e:
            return _bad(e.message)
        return _ok(
            f"已建自定义专岗「{p.get('title')}」（编号 {p.get('kind')}）。模型没选时用默认的；"
            f"人设和规则可以到{where(settings(), 'agents')}写",
            data={k: p.get(k) for k in ("kind", "title", *_AGENT_FIELDS)},
        )

    async def agent_delete(ctx: ToolContext, args: dict) -> ToolResult:
        deny = _unknown(args, ("kind",))
        if deny is not None:
            return deny
        kind = str(args.get("kind") or "").strip()
        try:  # 和网页删除同样的前置检查：内置 / 不存在 / 有未结交接单，当场拒不写票
            p = settings_ops.check_agent_delete(svc, kind)
        except settings_ops.OpError as e:
            return _bad(e.message)
        pending = need_confirm(
            "agent_delete", args,
            f"删掉自定义专岗「{p.get('title')}」（{kind}）：岗位设置和各群给它攒的做法一起删，人格文档挪进回收目录",
        )
        if pending is not None:
            return pending
        try:
            deleted = settings_ops.agent_delete_custom(svc, kind)
        except settings_ops.OpError as e:
            return _bad(e.message)
        return _ok(f"已删掉自定义专岗「{p.get('title')}」（{deleted}）")

    # ------------------------------------------------------------------
    # 二b、专岗详情（只读）：这个岗能用什么工具、在某个服务群记了什么、会什么做法
    # ------------------------------------------------------------------

    _RULE_WORDS = ("规矩", "规则", "记住", "记得", "记下")

    def _clip(text: Any, limit: int = 80) -> str:
        s = str(text or "").strip()
        return s if len(s) <= limit else s[:limit] + "…"

    def _date_of(ts: Any) -> str:
        try:
            v = float(ts or 0.0)
        except (TypeError, ValueError):
            return ""
        if v <= 0:
            return ""
        try:
            from . import clock

            return clock.bj(v).strftime("%Y-%m-%d")
        except Exception:
            return ""

    def _rule_title(line: str) -> str:
        """规矩一行的人话标题：去掉行首记号，截 80 字。"""
        s = str(line or "").strip()
        for mark in ("- ", "· ", "* ", "• "):
            if s.startswith(mark):
                s = s[len(mark):].strip()
                break
        return _clip(s, 80)

    def _rules_for(mod: Any, gid_s: str, kind_s: str) -> dict:
        """本群规矩里和这岗相关的行：条数 + 最近 3 条标题（不全文）。

        相关 = 行里出现这个岗的 kind 或显示名；兜底加「规矩 / 规则 / 记住」这类词
        （管理员用 remember 记进规矩的一行没写岗名，但也是给 MaiWork 各岗的硬规矩）。
        """
        p = mod.profile(kind_s)
        pats = [kind_s, str(p.get("title") or "").strip()]
        pats = [x for x in pats if x] + list(_RULE_WORDS)
        body = str((mod.group_rules_get(gid_s) or {}).get("body") or "")
        hits: list[tuple[int, str]] = []
        for i, raw in enumerate(body.splitlines()):
            line = raw.strip()
            if not line:
                continue
            low = line.lower()
            if any(str(pt).lower() in low for pt in pats):
                hits.append((i, line))
        return {
            "count": len(hits),
            "recent": [_rule_title(ln) for _i, ln in hits[-3:]],
        }

    async def agent_detail(ctx: ToolContext, args: dict) -> ToolResult:
        deny = _no_secret(args, "agents") or _unknown(args, ("kind", "group_id"))
        if deny is not None:
            return deny
        mod = _agents()
        if mod is None:
            return _bad("专岗功能还没开")
        kind = str(args.get("kind") or "").strip()
        if not kind:
            return _bad("要给 kind（岗位编号，list_agents 里有）")
        try:  # 不存在的岗位：Agents._kind_known 的同一句话
            p = mod.profile(kind)
        except ValueError as e:
            return _bad(str(e))
        kind_s = str(p.get("kind") or kind)
        data: dict[str, Any] = {
            "kind": kind_s,
            "title": str(p.get("title") or ""),
            "enabled": bool(p.get("enabled", True)),
            "model": str(p.get("model") or ""),
            "effort": str(p.get("effort") or ""),
            "backup": str(p.get("backup") or ""),
            "escalate": str(p.get("escalate") or ""),
            "group_id": "",
            "tools": [],
        }
        # 有效工具名：和开工前能力自检同一个入口（specialists.effective_tools）；
        # requested 用岗位配置里那份 tools（没配就是岗位默认上限），不再叠加别的名单。
        requested = p.get("tools")
        sp = getattr(svc, "specialists", None)
        if sp is not None and callable(getattr(sp, "effective_tools", None)):
            try:
                tools = [str(t) for t in sp.effective_tools(kind_s, requested, p)]
            except Exception:
                logger.exception("算岗位 %s 有效工具出错", kind_s)
                tools = []
        else:  # 服务包没接 Specialists（精简测试包）：按 specialists.py 同一口径手算
            from . import specialists as _sp_mod

            default = _sp_mod._SPECIALIST_DEFAULT_TOOLS.get(kind_s)
            req = None if requested is None else [str(x) for x in requested if str(x or "").strip()]
            if default is None:
                base = req or []
            elif req is None:
                base = list(default)
            else:
                base = [n for n in req if n in set(default)]
            if "submit_result" not in base:
                base.append("submit_result")
            tools = base
        data["tools"] = tools

        out_lines = [_profile_line(p), f"有效工具（{len(tools)} 个）：{'、'.join(tools) or '（无）'}"]

        gid_raw = str(args.get("group_id") or "").strip()
        if not gid_raw:
            out_lines.append("想看它在某个服务群记了什么、会什么做法，给 group_id 再看（只限服务群）。")
            return _ok("\n".join(out_lines), data=data)

        # 给了群号：本群三块。服务群闸由 Agents._verify_served 把关（memory / skills /
        # group_rules_get 入口先过它），非服务群让它自然抛成中文错误。
        try:
            gid_s = mod._verify_served(gid_raw)  # noqa: SLF001
        except ValueError as e:
            return _bad(str(e))
        data["group_id"] = gid_s

        mem = mod.memory(gid_s, kind_s)
        learned = list(mem.get("learned") or [])
        mem_items = [{"text": _clip(e.get("text"), 80)} for e in learned[-10:]]
        data["memory"] = {"count": len(learned), "items": mem_items}
        out_lines.append(f"本群记忆 {len(learned)} 条（最近 {len(mem_items)} 条）：")
        out_lines.extend(f"- {it['text']}" for it in mem_items)
        if not mem_items:
            out_lines.append("- （还没攒下记忆）")

        if kind_s == "main":
            data["skills"] = []
            out_lines.append("主模型岗没有本群做法（它不写做法）。")
        else:
            sk_rows = mod.skills(gid_s, kind_s, include_archived=False)
            sk_items = [
                {
                    "name": str(s.get("name") or ""),
                    "description": _clip(s.get("description"), 80),
                    "locked": bool(s.get("locked")),
                    "status": str(s.get("status") or ""),
                    "uses": int(s.get("uses") or 0),
                    "last_used": _date_of(s.get("last_used")),
                }
                for s in sk_rows
            ]
            data["skills"] = sk_items
            out_lines.append(f"本群做法 {len(sk_items)} 份（在用的）：")
            for s in sk_items:
                tail = []
                if s["locked"]:
                    tail.append("已锁定")
                tail.append(f"被用过 {s['uses']} 次")
                if s["last_used"]:
                    tail.append(f"上次 {s['last_used']}")
                desc = f"：{s['description']}" if s["description"] else ""
                out_lines.append(f"- {s['name']}{desc}（{'，'.join(tail)}）")
            if not sk_items:
                out_lines.append("- （还没有做法）")

        rules = _rules_for(mod, gid_s, kind_s)
        data["rules"] = rules
        if rules["count"]:
            out_lines.append(f"本群规矩里和这岗相关的 {rules['count']} 条，最近 {len(rules['recent'])} 条：")
            out_lines.extend(f"- {t}" for t in rules["recent"])
        else:
            out_lines.append("本群规矩里没有和这岗相关的条目。")
        return _ok("\n".join(out_lines), data=data)

    # ------------------------------------------------------------------
    # 三、端点与模型库（settings_ops，和网页「设置 → 模型」同一套）
    # ------------------------------------------------------------------

    async def list_models(ctx: ToolContext, args: dict) -> ToolResult:
        view = settings_ops.endpoints_view(svc)  # 网页同一个视图：只进不出
        lines = ["端点："]
        for e in view["endpoints"]:
            heads = e.get("header_names") or []
            lines.append(
                f"- {e['id']}「{e['name']}」：{e['protocol']}，{e['base_url']}，密钥{'已填' if e.get('key_set') else '没填'}"
                + (f"，另有请求头 {'、'.join(heads)}（只显示名字）" if heads else "")
            )
        if not view["endpoints"]:
            lines.append("（一个也没有）")
        lines.append("模型库：")
        for m in view["models"]:
            lines.append(
                f"- {m['id']}「{m['name']}」：端点 {m['endpoint']}，服务端模型名 {m['model']}"
                + (f"，思考强度 {'/'.join(m['efforts'])}" if m.get("efforts") else "")
                + ("，生图" if m.get("use") == "image" else "")
            )
        if not view["models"]:
            lines.append("（一条也没有）")
        lines.append(f"密钥只能管理员自己到{where(settings(), 'models')}填。")
        return _ok("\n".join(lines), data=view)

    def _endpoint_view(eid: str) -> dict:
        ep = next((e for e in (getattr(settings(), "endpoints", ()) or ()) if str(e.id) == eid), None)
        return settings_ops.endpoint_public(svc, ep) if ep is not None else {"id": eid}

    async def endpoint_save(ctx: ToolContext, args: dict) -> ToolResult:
        deny = _no_secret(args, "models", ("base_url",)) or _unknown(args, ("id", *_ENDPOINT_FIELDS))
        if deny is not None:
            return deny
        eid = str(args.get("id") or "").strip()
        if not eid:
            return _bad("要给端点 id")
        body = _pick(args, _ENDPOINT_FIELDS)
        try:  # 网页保存的同一套校验，先算出改前改后（不写盘）
            old, cand = settings_ops.preview_endpoint(svc, eid, body)
        except settings_ops.OpError as e:
            return _bad(e.message)
        if old is None and not body:
            return _bad("新建端点至少要给 base_url")
        changes: list[str] = []
        risky: list[str] = []
        if old is None:
            changes.append(f"新增端点，地址 {cand['base_url']}，协议 {cand['protocol']}")
            risky.append("新增端点")
        else:
            if cand["base_url"] != old["base_url"]:
                changes.append(f"地址改成 {cand['base_url']}")
                risky.append("改地址")
            if cand["protocol"] != old["protocol"]:
                changes.append(f"协议改成 {cand['protocol']}")
                risky.append("改协议")
            for key, zh in (("name", "名字"), ("retries", "重试次数"), ("retry_delay_s", "重试间隔秒数"),
                            ("max_concurrency", "最大并发"), ("max_rpm", "每分钟上限")):
                if cand.get(key) != old.get(key):
                    changes.append(f"{zh}改成 {cand.get(key)}")
        if not changes:
            return _ok(f"端点「{eid}」没有变化，什么都没改")
        if risky:
            pending = need_confirm(
                "endpoint_save", args,
                f"{'新增' if old is None else '修改'}模型端点「{eid}」：{'；'.join(changes)}"
                f"（之后用这个端点的模型请求{'和已存的密钥' if old and old.get('api_key') else ''}"
                f"都会发到 {cand['base_url']}）",
            )
            if pending is not None:
                return pending
        try:
            saved = await settings_ops.save_endpoint(svc, eid, body)
        except settings_ops.OpError as e:
            return _bad(e.message)
        tail = (
            "" if str(saved.get("api_key") or "")
            else f"这个端点还没填密钥：请管理员自己到{where(settings(), 'models')}填写（密钥不经过对话）。"
        )
        return _ok(f"已保存端点「{eid}」：{'；'.join(changes)}。{tail}".rstrip(), data=_endpoint_view(eid))

    async def endpoint_delete(ctx: ToolContext, args: dict) -> ToolResult:
        deny = _unknown(args, ("id",))
        if deny is not None:
            return deny
        eid = str(args.get("id") or "").strip()
        try:  # 网页删除的同样前置检查：不存在 / 还有模型挂着 → 当场拒，不写票
            settings_ops.check_endpoint_delete(svc, eid)
        except settings_ops.OpError as e:
            return _bad(e.message)
        pending = need_confirm("endpoint_delete", args, f"删掉模型端点「{eid}」（连同它存的密钥）")
        if pending is not None:
            return pending
        try:
            await settings_ops.delete_endpoint(svc, eid)
        except settings_ops.OpError as e:
            return _bad(e.message)
        return _ok(f"已删掉端点「{eid}」")

    async def model_save(ctx: ToolContext, args: dict) -> ToolResult:
        deny = _no_secret(args, "models") or _unknown(args, ("id", *_MODEL_FIELDS))
        if deny is not None:
            return deny
        mid = str(args.get("id") or "").strip()
        if not mid:
            return _bad("要给模型库条目 id")
        body = _pick(args, _MODEL_FIELDS)
        if not body:
            return _bad("没有要改的字段")
        existed = any(str(m.id) == mid for m in (getattr(settings(), "model_list", ()) or ()))
        try:  # PUT /api/settings/model-list/{id} 的同一个落点
            saved = await settings_ops.save_model(svc, mid, body)
        except settings_ops.OpError as e:
            return _bad(e.message)
        return _ok(
            f"已{'改好' if existed else '加上'}模型库条目「{mid}」：端点 {saved['endpoint']}，服务端模型名 {saved['model']}"
            "。要让某个岗位用它，再改那个岗位的模型",
            data=saved,
        )

    async def model_delete(ctx: ToolContext, args: dict) -> ToolResult:
        deny = _unknown(args, ("id",))
        if deny is not None:
            return deny
        mid = str(args.get("id") or "").strip()
        try:  # 网页删除的同样前置检查：不存在 / 还有专岗在用 → 当场拒，不写票
            settings_ops.check_model_delete(svc, mid)
        except settings_ops.OpError as e:
            return _bad(e.message)
        pending = need_confirm("model_delete", args, f"从模型库删掉「{mid}」")
        if pending is not None:
            return pending
        try:
            await settings_ops.delete_model(svc, mid)
        except settings_ops.OpError as e:
            return _bad(e.message)
        return _ok(f"已从模型库删掉「{mid}」")

    # ------------------------------------------------------------------
    # 注册（参数表里没有任何密钥字段）
    # ------------------------------------------------------------------

    _str = {"type": "string"}
    _mcp_props = {
        "name": {"type": "string", "description": "扩展名字（字母、数字、下划线、横线，1~32 个字符）"},
        "url": {"type": "string", "description": "https:// 开头的 MCP 地址；不要带查询参数（密钥不经过对话）"},
        "tools": {"type": "array", "items": _str, "description": "工具白名单；空列表 = 不限"},
        "roles": {"type": "array", "items": {"type": "string", "enum": ["worker", "main"]},
                  "description": "给谁用：worker = 子 agent，main = 主模型"},
        "enabled": {"type": "boolean", "description": "开 / 关"},
        "timeout_s": {"type": "integer", "minimum": 1, "description": "超时秒数"},
    }
    _ep_props = {
        "id": {"type": "string", "description": "端点 id（新建时自己起，字母数字）"},
        "name": {"type": "string", "description": "显示名"},
        "protocol": {"type": "string", "enum": ["openai", "anthropic", "responses"], "description": "协议"},
        "base_url": {"type": "string", "description": "接口地址（http(s)://…，不要带查询参数）"},
        "retries": {"type": "integer"}, "retry_delay_s": {"type": "integer"},
        "max_concurrency": {"type": "integer"}, "max_rpm": {"type": "integer"},
    }
    _model_props = {
        "id": {"type": "string", "description": "模型库条目 id"},
        "endpoint": {"type": "string", "description": "挂在哪个端点（端点 id）"},
        "model": {"type": "string", "description": "服务端的模型名"},
        "name": {"type": "string", "description": "显示名"},
        "efforts": {"type": "array", "items": _str, "description": "支持的思考强度（low / medium / high / xhigh / max）"},
        "vision": {"type": "boolean", "description": "能不能看图"},
        "context_window": {"type": "integer"}, "max_tokens": {"type": "integer"},
        "use": {"type": "string", "enum": ["chat", "image"], "description": "用途：聊天 / 生图"},
        "image_api": {"type": "string"}, "image_default_size": {"type": "string"}, "image_max_n": {"type": "integer"},
    }
    _no_key = "参数里没有、也不许放密钥：要密钥的告诉管理员去网页设置页自己填。"

    specs: list[tuple[dict, Callable[[ToolContext, dict], Awaitable[ToolResult]], float]] = [
        ({"name": "mcp_add",
          "description": "新增一个 MCP 扩展（要管理员确认；确认后保存并立刻连一次）。" + _no_key,
          "parameters": {"type": "object", "properties": _mcp_props, "required": ["name", "url"]}}, mcp_add, 60.0),
        ({"name": "mcp_update",
          "description": "改一个网页加的 MCP 扩展。改地址、给主模型用、放宽工具白名单要管理员确认；"
                         "关掉、删工具名、去掉主模型这类收窄的直接改。改完立刻重连。" + _no_key,
          "parameters": {"type": "object", "properties": _mcp_props, "required": ["name"]}}, mcp_update, 60.0),
        ({"name": "mcp_test",
          "description": "试连一个 MCP，列出对方的工具（不保存）。给 name 测已存的（用它存的地址和请求头），"
                         "或只给 url 测一个新地址（不带任何请求头）。",
          "parameters": {"type": "object", "properties": {
              "name": _mcp_props["name"], "url": _mcp_props["url"], "timeout_s": _mcp_props["timeout_s"]}}},
         mcp_test, 70.0),
        ({"name": "mcp_reload",
          "description": "让一个 MCP 扩展重连、刷新工具列表。",
          "parameters": {"type": "object", "properties": {"name": _mcp_props["name"]}, "required": ["name"]}},
         mcp_reload, 60.0),
        ({"name": "list_agents",
          "description": "看各专岗（含主模型、自定义专岗）现在用哪个模型、思考强度、备用、做不动时换用、开没开。",
          "parameters": {"type": "object", "properties": {}}}, list_agents, 30.0),
        ({"name": "agent_update",
          "description": "改一个岗位的模型 / 思考强度 / 备用模型 / 做不动时换用 / 启用停用（填模型库条目 id）。",
          "parameters": {"type": "object", "properties": {
              "kind": {"type": "string", "description": "岗位编号（list_agents 里的 kind）"},
              "model": {"type": "string", "description": "模型库条目 id；空串 = 不选"},
              "effort": {"type": "string", "description": "思考强度（所选模型支持的之一）；空串 = 默认"},
              "backup": {"type": "string", "description": "备用模型的条目 id；空串 = 不要"},
              "escalate": {"type": "string", "description": "做不动时换用的条目 id；主模型岗没有这一项"},
              "enabled": {"type": "boolean", "description": "开 / 停"},
          }, "required": ["kind"]}}, agent_update, 30.0),
        ({"name": "agent_create",
          "description": "新建一个自定义专岗（起个名字）。",
          "parameters": {"type": "object", "properties": {
              "title": {"type": "string", "description": "专岗名字（≤40 字）"}}, "required": ["title"]}},
         agent_create, 30.0),
        ({"name": "agent_delete",
          "description": "删掉一个自定义专岗（要管理员确认；内置岗位删不了）。",
          "parameters": {"type": "object", "properties": {
              "kind": {"type": "string", "description": "自定义专岗编号（c_ 开头）"}}, "required": ["kind"]}},
         agent_delete, 30.0),
        ({"name": "agent_detail",
          "description": "只读看一个岗位：身份（模型 / 强度 / 备用 / 做不动时换用 / 启停）和它实际"
                         "能用的工具名单。给 group_id（只能是服务群）再看它在这个群记下的经验、"
                         "在用的本群做法、本群规矩里和它相关的条目。什么都不改，不用确认。",
          "parameters": {"type": "object", "properties": {
              "kind": {"type": "string", "description": "岗位编号（list_agents 里的 kind）"},
              "group_id": {"type": "string", "description": "服务群群号（可选；给了才看本群的记忆 / 做法 / 规矩）"},
          }, "required": ["kind"]}}, agent_detail, 30.0),
        ({"name": "list_models",
          "description": "看模型端点和模型库（只说密钥填没填，不给密钥本身）。",
          "parameters": {"type": "object", "properties": {}}}, list_models, 30.0),
        ({"name": "endpoint_save",
          "description": "新增或修改一个模型端点。新增、改地址、改协议要管理员确认（数据会发去新地址）；"
                         "名字、重试、并发这类直接改。" + _no_key,
          "parameters": {"type": "object", "properties": _ep_props, "required": ["id"]}}, endpoint_save, 60.0),
        ({"name": "endpoint_delete",
          "description": "删掉一个模型端点（要管理员确认；还有模型挂在上面时删不了）。",
          "parameters": {"type": "object", "properties": {"id": _ep_props["id"]}, "required": ["id"]}},
         endpoint_delete, 60.0),
        ({"name": "model_save",
          "description": "新增或修改模型库的一条（挂到已有端点上）。直接改。",
          "parameters": {"type": "object", "properties": _model_props, "required": ["id"]}}, model_save, 60.0),
        ({"name": "model_delete",
          "description": "从模型库删掉一条（要管理员确认；还有岗位在用时删不了）。",
          "parameters": {"type": "object", "properties": {"id": _model_props["id"]}, "required": ["id"]}},
         model_delete, 60.0),
    ]
    for spec, handler, timeout_s in specs:
        register(spec, handler, timeout_s=timeout_s)
