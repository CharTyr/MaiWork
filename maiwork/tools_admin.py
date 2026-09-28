"""tools_admin.py：管理员对话专用的工具层（docs/02-设计.md「管理员对话」）。

一句话说清它干什么：管理员在网页上和 MaiWork 的主模型对话时，主模型能调的那批工具
在这里注册；能直接读的读，能直接改的改，**会对外发东西 / 会删东西 / 会放宽安全设置的**
一律不当时动手——先写一张「待确认」小票（admin_chat_pending 表），等管理员点「同意」。

对外接口：
- `register_admin_tools(tools, svc) -> PendingGate`：把工具注册进 Tools，返回门闸对象；
  调用方（app）把它挂在 `svc.admin_pending`，对话循环拿到小票后调 `gate.execute(id, True/False)`。
- `PendingGate.bind_chat(chat_id, msg_id)`：每轮对话在自己的 Task 里调，小票记到这段对话上。
- `PendingGate.pending(chat_id=None) -> list[dict]`：还没决定的小票（给网页列出来）。
- `PendingGate.execute(pending_id, approve) -> ToolResult`：管理员点头/摇头后真正执行或作废。

红线（写死在代码里，不靠提示词）：
- 所有工具 roles={"admin"}，子 agent 和普通主模型都调不到（tools.py 的角色门会拒）。
- 带群号的工具都先查「是不是服务群」；非服务群零读取零动作（list_groups 只列服务群；
  read_logs / get_identity / list_extensions / skill_* / mcp_* 不涉及具体群）。
- 没有任何工具能碰密钥 / 端点 / MaiBot 宿主（重启、改配置、装插件都不存在）。
- 会往群里发东西的只有 `send_group_message` 和群空间写类（发公告会先发一句预告），
  全都在 CONFIRM_TOOLS 里，必须管理员确认后才动手。
- 「已批准」只由 PendingGate.execute 在执行那一下置上（ContextVar），而且是
  **这次动作的指纹**（工具名 + 规范化参数的散列），不是一面大家都能用的旗子：
  执行期间派出的子任务再调确认类工具，参数对不上指纹就得重新写票；
  模型参数里 `_` 开头的键一律剥掉。
- 小票归属的对话按 asyncio Task 各记一份（ContextVar），并发对话不串票；
  没绑定对话时宁可拒绝也不偷偷新建一段（小票必须有看得见的归属）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import clock, rules
from .tools import Tool, ToolContext, ToolResult, Tools

logger = logging.getLogger("maiwork.tools_admin")

# 只有 bot 管理员能用（tools.ToolContext.role 的角色名）
ROLE = "admin"
_ROLES = frozenset({ROLE})

# 要管理员确认才动手的工具名（测试 TestConfirmGate 逐个点名）
CONFIRM_TOOLS = frozenset(
    {
        "send_group_message",
        "group_notice_send",
        "group_file_manage",
        "group_album_upload",
        "rss_remove",
        "skill_delete",
        "mcp_delete",
        "profile_bulk_delete",
        "approve_request",  # 群友派的活要 bot 管理员批准；模型可能被群内容注入，绝不能替管理员拍板
        "create_task",  # 任务开工后，做完 / 做不成 / 缺信息时 coordinator 会自动往群里发消息（coordinator.py 的 outbox.enqueue）
        "create_goal",  # agent 目标后台检查时会往群里发汇报 / 完成话（coordinator 的 goal:{id}:report / goal:{id}:done）
    }
)

# 确认话术（固定，测试断言这一句；对话循环也靠它识别「等确认」）
NEED_CONFIRM = "已请求管理员确认，同意后我再动手"
_REFUSED = "管理员没有同意，这次动作已取消"
_NOT_IN_SERVICE = "这不是配置里的服务群，MaiWork 不读也不动它"
_ADMIN_ONLY = "只有 bot 管理员能用，当前角色不允许"
_NO_CHAT = "管理员对话还没绑定，这个动作先记不下小票：请在网页对话里重发一次"

MAX_SUMMARY = 200

# 「当前是哪段对话 / 哪条消息」按 asyncio Task 各存一份：两个管理员对话并发跑时，
# B 的 bind_chat 不会覆盖 A 的，小票不会串到别的对话里。(0, 0) = 没绑定。
_CHAT_CTX: ContextVar[tuple[int, int]] = ContextVar("mw_admin_chat_ctx", default=(0, 0))
# 「管理员已点同意」只由 PendingGate.execute 在执行那一下置上，而且值是这次动作的
# 指纹 (工具名, 规范化参数的 sha256)：execute 期间派出的子任务会继承这个 ContextVar，
# 但它再调确认类工具时参数对不上指纹就照样要写小票，不能把「已批准」当通行证用。
# 模型传来的参数一律不算数（参数里写什么都不影响这里的值）。
_APPROVED_CTX: ContextVar[tuple[str, str] | None] = ContextVar("mw_admin_approved", default=None)


def _norm_args(args: Any) -> dict:
    """规范化工具参数：剥掉 `_` 开头的内部键，只留 dict。"""
    if not isinstance(args, dict):
        return {}
    return {k: v for k, v in args.items() if not str(k).startswith("_")}


def _fingerprint(tool: str, args: Any) -> tuple[str, str]:
    """一次确认动作的指纹：工具名 + 规范化参数的 sha256（键排序、中文不转义）。"""
    blob = json.dumps(_norm_args(args), sort_keys=True, ensure_ascii=False, default=str)
    return (str(tool), hashlib.sha256(blob.encode("utf-8")).hexdigest())


def _check_upload_path(raw_path: str, workspace_dir: Path | None) -> Path:
    """传群相册 / 群文件前的路径检查（和 outbox._check_upload_path 同一套口径）：

    - 不能是符号链接（root 跟随链接会把工作区外的文件传进群）；
    - resolve 后必须落在 workspace_dir（这个群的工作区根目录）下。

    不合法抛 ValueError（中文），调用方按「拒 + 不写票」处理。
    """
    raw = str(raw_path or "").strip()
    if not raw:
        raise ValueError("path 不能为空")
    p = Path(raw)
    try:
        if p.is_symlink():
            raise ValueError(f"路径是符号链接，不传：{raw}")
        resolved = p.resolve()
    except OSError as e:
        raise ValueError(f"路径解析失败：{raw}（{e}）") from None
    if workspace_dir is None:
        raise ValueError("这个群的工作区还不知道在哪，先不传")
    try:
        root = workspace_dir.resolve()
    except OSError:
        root = workspace_dir.absolute()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"路径不在这个群的工作区里，不传：{raw}")
    return resolved

# 域名规范化（和 feeds 的屏蔽名单同口径）
_DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")


def _ok(output: str, data: Any = None) -> ToolResult:
    return ToolResult(ok=True, output=output, data=data)


def _bad(error: str) -> ToolResult:
    return ToolResult(ok=False, output="", error=error)


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _tail(text: Any, limit: int = MAX_SUMMARY) -> str:
    s = str(text or "").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _served_groups(settings: Any) -> set[str]:
    groups = getattr(settings, "groups", None) or {}
    try:
        return {str(k) for k in groups.keys()}
    except Exception:
        return set()


class PendingGate:
    """危险动作的「先问一句」门闸：写 admin_chat_pending，等管理员决定。"""

    def __init__(self, tools: Tools, svc: Any) -> None:
        self._tools = tools
        self._svc = svc
        # 对话循环在这里挂上「当前是哪段对话 / 哪条消息」，小票就记在这段对话上
        self.chat_id: int = 0
        self.msg_id: int = 0

    # ------------------------------------------------------------------
    # 上下文
    # ------------------------------------------------------------------

    def bind_chat(self, chat_id: Any, msg_id: Any = 0) -> None:
        """告诉门闸当前在跟哪段对话说话（每轮对话在自己的 Task 里调）。

        记在 ContextVar 里（每个 asyncio Task 一份），并发的对话互不覆盖；
        实例属性 chat_id/msg_id 只作为没绑定过时的回落（直接调门闸的场景）。
        """
        try:
            cid = max(0, int(chat_id or 0))
        except (TypeError, ValueError):
            cid = 0
        try:
            mid = max(0, int(msg_id or 0))
        except (TypeError, ValueError):
            mid = 0
        _CHAT_CTX.set((cid, mid))

    def _current(self) -> tuple[int, int]:
        """当前 Task 绑定的 (对话, 消息)；没绑定就用实例上的回落值。"""
        cid, mid = _CHAT_CTX.get()
        if cid > 0:
            return cid, mid
        return self.chat_id, self.msg_id

    @property
    def store(self) -> Any:
        return getattr(self._svc, "store", None)

    def _ensure_chat(self) -> int:
        """小票归属的对话；没绑定就返回 0（调用方按「不写票」处理）。

        以前这里会偷偷现建一段对话：票是记下了，但网页上不知道去哪看它，
        等于给危险动作开了个没人认领的后门。现在没绑定宁可不做。
        """
        cid, _mid = self._current()
        return cid

    # ------------------------------------------------------------------
    # 写小票 / 执行小票
    # ------------------------------------------------------------------

    def queue(self, tool: str, args: dict, summary: str = "") -> ToolResult:
        """记一张待确认小票，当时不做任何动作。"""
        store = self.store
        if store is None:
            return _bad("管理员对话的存储没就位，这个动作先做不了")
        try:
            chat_id = self._ensure_chat()
            if chat_id <= 0:
                return _bad(_NO_CHAT)
            msg_id = self._current()[1]
            now = clock.now()
            with store.tx() as conn:
                cur = conn.execute(
                    "INSERT INTO admin_chat_pending"
                    " (chat_id, msg_id, tool, args, summary, status, created)"
                    " VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                    (
                        int(chat_id),
                        int(msg_id),
                        str(tool),
                        json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False),
                        _tail(summary or f"要执行 {tool}"),
                        now,
                    ),
                )
                pid = int(cur.lastrowid or 0)
                if chat_id:
                    store.event(
                        conn,
                        "admin_chat.pending",
                        entity="admin_chat_pending",
                        entity_id=str(pid),
                        payload={"tool": str(tool), "summary": _tail(summary or tool)},
                    )
        except Exception as e:
            logger.exception("写待确认小票失败（tool=%s）", tool)
            return _bad(f"记不下这条待确认动作：{e}")
        return _ok(f"{NEED_CONFIRM}（编号 {pid}）", data={"pending_id": pid, "needs_confirm": True})

    def pending(self, chat_id: Any = None) -> list[dict]:
        """还没决定的小票，旧的在前。chat_id 给了就只看这段对话。"""
        store = self.store
        if store is None:
            return []
        where = ["status='pending'"]
        params: list[Any] = []
        if chat_id is not None:
            try:
                where.append("chat_id=?")
                params.append(int(chat_id))
            except (TypeError, ValueError):
                return []
        rows = store.read().execute(
            f"SELECT * FROM admin_chat_pending WHERE {' AND '.join(where)} ORDER BY id ASC",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def _row(self, pending_id: Any) -> dict | None:
        store = self.store
        if store is None:
            return None
        try:
            pid = int(pending_id)
        except (TypeError, ValueError):
            return None
        row = store.read().execute(
            "SELECT * FROM admin_chat_pending WHERE id=?", (pid,)
        ).fetchone()
        return dict(row) if row is not None else None

    def _decide(self, pending_id: int, status: str, result: str) -> None:
        store = self.store
        if store is None:
            return
        now = clock.now()
        with store.tx() as conn:
            conn.execute(
                "UPDATE admin_chat_pending SET status=?, result=?, decided=? WHERE id=?",
                (str(status), _tail(result, 500), now, int(pending_id)),
            )

    async def execute(self, pending_id: Any, approve: bool = True) -> ToolResult:
        """管理员点头（approve=True）就真执行；摇头就作废。一张小票只能决定一次。"""
        row = self._row(pending_id)
        if row is None:
            return _bad(f"没有编号 {pending_id} 的待确认动作")
        status = str(row.get("status") or "")
        if status != "pending":
            return _bad(f"这条动作已经处理过了（{status}）")
        pid = int(row["id"])
        tool = str(row.get("tool") or "")
        if not approve:
            self._decide(pid, "rejected", _REFUSED)
            return _ok(_REFUSED, data={"pending_id": pid, "approved": False})
        try:
            args = json.loads(str(row.get("args") or "{}"))
        except (TypeError, ValueError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        args = _norm_args(args)
        # 管理员点头 = 替他拍板，工具内部按管理员身份执行
        ctx = ToolContext(group_id=str(args.get("group_id") or ""), actor="bot 管理员（已确认）", role=ROLE)
        getter = getattr(self._tools, "get", None)
        tool_obj = getter(tool, ROLE) if callable(getter) else None
        if tool_obj is None:
            self._decide(pid, "failed", f"工具 {tool} 已经不在了")
            return _bad(f"工具 {tool} 已经不在了，这条动作执行不了")
        # 「已批准」只在这一下有效、只对这张票的 (工具, 参数) 有效（指纹），
        # 且只能由这里置上：执行期间派出的子任务继承到的也是这个指纹，
        # 它再调别的确认类工具（或同工具换参数）对不上指纹，照样要写小票。
        token = _APPROVED_CTX.set(_fingerprint(tool, args))
        timeout = float(getattr(tool_obj, "timeout_s", 0.0) or 0.0)
        if timeout <= 0:
            timeout = 60.0
        try:
            result = await asyncio.wait_for(tool_obj.handler(ctx, args), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning("执行已确认的动作超时（%s #%s，%ss）", tool, pid, timeout)
            self._decide(pid, "failed", f"执行超时（{timeout:g} 秒没跑完）")
            return _bad(f"执行「{tool}」超时（{timeout:g} 秒没跑完），这次动作已作废")
        except Exception as e:
            logger.exception("执行已确认的动作出错（%s #%s）", tool, pid)
            self._decide(pid, "failed", f"执行出错：{e}")
            return _bad(f"执行「{tool}」出错：{e}")
        finally:
            _APPROVED_CTX.reset(token)
        if not isinstance(result, ToolResult):
            result = ToolResult(ok=bool(result), output=str(result))
        self._decide(pid, "approved" if result.ok else "failed", result.output or result.error)
        return result


# ----------------------------------------------------------------------
# 注册
# ----------------------------------------------------------------------


def register_admin_tools(tools: Tools, svc: Any) -> PendingGate:
    """把管理员对话的工具注册进 tools，返回待确认门闸（调用方存到 svc.admin_pending）。"""
    gate = PendingGate(tools, svc)

    # ------------------------------------------------------------------
    # 公共小工具
    # ------------------------------------------------------------------

    def _settings() -> Any:
        """有效设置（config + 网页规则覆盖）。

        优先用 svc.get_settings()；它还没把规则覆盖合进来时（例如只给了基础
        Settings 的场景），这里自己按 rules.effective_settings 合一次并缓存在
        服务包上，保证「刚改完的规则」下一次读就是新值。合不出来就用原值，
        绝不把设置读挂。
        """
        getter = getattr(svc, "get_settings", None)
        base = None
        if callable(getter):
            try:
                base = getter()
            except Exception:
                logger.exception("管理员工具读设置失败")
        if base is None:
            return getattr(svc, "_settings", None)
        store = getattr(svc, "store", None)
        if store is None:
            return base
        try:
            override = rules.read_override(store)
            if not override:
                return base
            cached = getattr(svc, "_mw_effective", None)
            if isinstance(cached, tuple) and len(cached) == 3 and cached[0] is base and cached[1] == override:
                return cached[2]
            merged = rules.effective_settings(base, override)
            try:
                setattr(svc, "_mw_effective", (base, override, merged))
            except Exception:
                pass
            # 服务包的 get_settings() 还没把规则覆盖合进来时（只返回基础 Settings 的场景，
            # 例如测试里的最小服务包），把基础设置换成合并后的——这样「刚改完的规则」
            # 立刻对所有读设置的地方生效，和线上 app.get_settings() 的行为一致。
            if merged is not base and getattr(svc, "_settings", None) is base:
                try:
                    setattr(svc, "_settings", merged)
                except Exception:
                    pass
            return merged
        except Exception:
            return base

    def _served(gid: Any) -> tuple[str, ToolResult | None]:
        """返回 (群号, None) 或 ("", 中文错误)。非服务群一律挡住。"""
        g = str(gid or "").strip()
        if not g:
            return "", _bad(f"要指定一个群号。{_NOT_IN_SERVICE}")
        served = _served_groups(_settings())
        if not served:
            return "", _bad(f"现在读不到服务群名单，先别动。{_NOT_IN_SERVICE}")
        if g not in served:
            return "", _bad(f"群 {g} 不是服务群：{_NOT_IN_SERVICE}")
        return g, None

    def _profiles() -> Any:
        return getattr(svc, "profiles", None)

    def _feeds() -> Any:
        return getattr(svc, "feeds", None)

    def _identity() -> Any:
        obj = getattr(svc, "identity", None)
        if obj is not None:
            return obj
        settings = _settings()
        store = getattr(svc, "store", None)
        data_dir = getattr(settings, "data_dir", None) or getattr(svc, "data_dir", None)
        if store is None or data_dir is None:
            return None
        try:
            from .identity import Identity

            obj = Identity(data_dir, store, _settings, host=getattr(svc, "host", None))
            try:
                setattr(svc, "identity", obj)
            except Exception:
                pass
            return obj
        except Exception:
            logger.exception("管理员工具建 Identity 失败")
            return None

    def _skills() -> Any:
        settings = _settings()
        data_dir = getattr(settings, "data_dir", None) or getattr(svc, "data_dir", None)
        if data_dir is None:
            return None
        try:
            from .skills import Skills

            return Skills(data_dir)
        except Exception:
            logger.exception("管理员工具建 Skills 失败")
            return None

    def _group_space(gid: str) -> Any:
        """群空间能力对象：直接挂在服务包上，或按群取（app 的 group_space_of）。"""
        space = getattr(svc, "group_space", None)
        if space is not None:
            return space
        getter = getattr(svc, "group_space_of", None)
        if callable(getter):
            try:
                return getter(gid)
            except Exception:
                logger.exception("取群空间能力失败（群 %s）", gid)
        return None

    def _register(
        spec: dict,
        handler: Callable[[ToolContext, dict], Awaitable[ToolResult]],
        *,
        timeout_s: float = 30.0,
        summarize: Callable[[dict, ToolResult], tuple[str, str]] | None = None,
    ) -> None:
        async def _wrapped(ctx: ToolContext, args: dict) -> ToolResult:
            # 双保险：tools.call 已经按 role 挡过一次，这里再挡一次（防以后有人绕过 call）
            if str(getattr(ctx, "role", "") or "") != ROLE:
                return _bad(_ADMIN_ONLY)
            # 下划线开头的键是内部标记，模型传来的一律剥掉（防伪造 _approved 之类）
            return await handler(ctx, _norm_args(args))

        name = str(spec["name"])

        def _sum(args: dict, res: ToolResult) -> tuple[str, str]:
            if summarize is not None:
                try:
                    return summarize(args, res)
                except Exception:
                    pass
            return (name, _tail(res.output if res.ok else (res.error or ""), 120))

        tools.register(
            Tool(
                name=name,
                description=str(spec["description"]),
                parameters=spec.get("parameters") or {"type": "object", "properties": {}},
                roles=_ROLES,
                handler=_wrapped,
                summarize=_sum,
                timeout_s=timeout_s,
            )
        )

    def _need_confirm(tool: str, args: dict, summary: str) -> ToolResult | None:
        """要确认的工具统一入口：这次 (工具, 参数) 已被管理员点过头 → None（继续真做）；
        否则写小票并返回结果。比对的是指纹：execute 置上的只对票里那份参数有效。"""
        if tool in CONFIRM_TOOLS or tool == "set_rules":
            if _APPROVED_CTX.get() == _fingerprint(tool, args):
                return None
            return gate.queue(tool, _norm_args(args), summary)
        return None

    # 群的「安静/睡觉时段」在这个窗口里，MaiWork 不主动往群里发东西；
    # 窗口越窄 = 能发的时间段越长（跨午夜按 24 小时折回）。
    def _quiet_minutes(value: Any) -> int | None:
        """"HH:MM-HH:MM" → 这段一天里占多少分钟；解析不了 → None（不参与比较）。"""
        try:
            start, end = clock.parse_hhmm_range(str(value))
        except Exception:
            return None
        if end > start:
            return end - start
        if end < start:
            return 1440 - start + end
        return 1440  # 起止一样 = 全天都算安静

    def _workspace_root_of(gid: str) -> Path | None:
        """这个群的工作区根目录（workspace_root / 工作区名）；取不到 → None。"""
        settings = _settings()
        root = getattr(settings, "workspace_root", None)
        if not root:
            return None
        workspace_of = getattr(settings, "workspace_of", None)
        name = workspace_of(gid) if callable(workspace_of) else f"g{gid}"
        return Path(root) / str(name or f"g{gid}")

    # ------------------------------------------------------------------
    # 一、读类
    # ------------------------------------------------------------------

    async def list_groups(ctx: ToolContext, args: dict) -> ToolResult:
        settings = _settings()
        groups = getattr(settings, "groups", None) or {}
        if not groups:
            return _ok("现在一个服务群也没有（配置里 [groups] serve 是空的）", data=[])
        workspace_of = getattr(settings, "workspace_of", None)
        lines: list[str] = []
        data: list[dict] = []
        for gid in sorted(str(k) for k in groups.keys()):
            ws = str(workspace_of(gid)) if callable(workspace_of) else f"g{gid}"
            lines.append(f"群 {gid}（工作区 {ws}）")
            data.append({"group_id": gid, "workspace": ws})
        return _ok(f"服务群共 {len(data)} 个：\n" + "\n".join(lines), data=data)

    async def group_overview(ctx: ToolContext, args: dict) -> ToolResult:
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        settings = _settings()
        parts: list[str] = [f"群 {gid}"]
        prof = _profiles()
        if prof is not None:
            try:
                parts.append(f"画像条目 {len(prof.entries(gid))} 条")
            except Exception:
                parts.append("画像读不到")
        feed_obj = _feeds()
        if feed_obj is not None:
            try:
                parts.append(f"资讯 {len(feed_obj.news_view(gid))} 条 / 构想 {len(feed_obj.ideas_view(gid))} 条")
                parts.append(f"资讯偏好：{feed_obj.pref(gid) or '（还没写）'}")
            except Exception:
                pass
        try:
            parts.append(f"任务 {len(svc.tasks.list_view(gid))} 个")
        except Exception:
            pass
        try:
            n = svc.store.read().execute(
                "SELECT COUNT(*) AS c FROM goals WHERE group_id=? AND state='active'", (gid,)
            ).fetchone()["c"]
            parts.append(f"在盯的事 {int(n)} 件")
        except Exception:
            pass
        workspace_of = getattr(settings, "workspace_of", None)
        parts.append(f"工作区 {str(workspace_of(gid)) if callable(workspace_of) else f'g{gid}'}")
        return _ok("；".join(parts))

    async def read_profile(ctx: ToolContext, args: dict) -> ToolResult:
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        prof = _profiles()
        if prof is None:
            return _bad("画像模块没就位，这次读不到")
        try:
            entries = prof.entries(gid) or []
        except Exception as e:
            return _bad(f"读画像失败：{e}")
        if not entries:
            return _ok(f"群 {gid} 的画像还是空的", data=[])
        cat_names = {
            "recent": "最近在聊",
            "interest": "长期兴趣",
            "ongoing": "在做的事",
            "convention": "约定和说法",
            "resource": "常用资源",
        }
        lines: list[str] = []
        cat_now = ""
        for e in entries:
            cat = str(e.get("category") or "")
            if cat != cat_now:
                cat_now = cat
                lines.append(f"【{cat_names.get(cat, cat)}】")
            lock = "（锁定）" if int(e.get("locked") or 0) else ""
            lines.append(f"- #{e.get('id')} {e.get('text', '')}{lock}")
        return _ok("\n".join(lines), data=entries)

    async def list_focus(ctx: ToolContext, args: dict) -> ToolResult:
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        try:
            rows = svc.store.read().execute(
                "SELECT user_id, name, note, persona, pinned FROM focus_members"
                " WHERE group_id=? AND removed=0 ORDER BY pinned DESC, user_id ASC",
                (gid,),
            ).fetchall()
        except Exception as e:
            return _bad(f"读关注成员失败：{e}")
        data = [dict(r) for r in rows]
        if not data:
            return _ok(f"群 {gid} 现在没关注谁", data=[])
        lines = [
            f"- {r.get('name') or r.get('user_id')}（id {r.get('user_id')}"
            f"{'，管理员加的' if int(r.get('pinned') or 0) else ''}）"
            for r in data
        ]
        return _ok(f"群 {gid} 关注 {len(data)} 人：\n" + "\n".join(lines), data=data)

    async def list_news(ctx: ToolContext, args: dict) -> ToolResult:
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        feed_obj = _feeds()
        if feed_obj is None:
            return _bad("资讯模块没就位，这次读不到")
        days = _int(args.get("days"), 7, 1, 30)
        try:
            # news_view(admin=True) 返回的是**批次**列表：每批 {id, found, kept, skipped,
            # note, rejected_count, items:[{title,url,site,status_kind,...}], rejected:[...]}，
            # 不是条目列表——线上就踩过「把批次当条目读 title」得到一堆空壳的坑
            batches = feed_obj.news_view(gid, days=days, admin=True)
        except Exception as e:
            return _bad(f"读资讯失败：{e}")
        if not batches:
            return _ok(f"群 {gid} 最近 {days} 天没有资讯", data=[])
        lines: list[str] = []
        flat: list[dict] = []
        rejected_total = 0
        for b in batches:
            if not isinstance(b, dict):
                continue
            items = b.get("items") or []
            rejected = b.get("rejected") or []
            rejected_total += int(b.get("rejected_count") or len(rejected) or 0)
            if not items and not rejected:
                note = str(b.get("note") or "").strip()
                lines.append(f"一批（找到 {int(b.get('found') or 0)} 条，没留下能用的{('：' + note) if note else ''}）")
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                flat.append(it)
                title = str(it.get("title") or "").strip() or "（无标题）"
                url = str(it.get("url") or "").strip()
                site = str(it.get("site") or "").strip()
                where = url or site
                lines.append(
                    f"- [{it.get('status_kind') or 'new'}] {title}{('｜' + where) if where else ''}"
                )
            for it in rejected:
                if isinstance(it, dict):
                    lines.append(
                        f"- [被筛] {str(it.get('title') or '').strip() or '（无标题）'}"
                        f"｜{str(it.get('url') or it.get('site') or '')}（{str(it.get('reason') or '')}）"
                    )
        head = f"群 {gid} 最近 {days} 天资讯：{len(batches)} 批，留下 {len(flat)} 条"
        if rejected_total:
            head += f"；另被筛掉 {rejected_total} 条"
        return _ok(head + "：\n" + "\n".join(lines[:60]), data=batches)

    async def list_ideas(ctx: ToolContext, args: dict) -> ToolResult:
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        feed_obj = _feeds()
        if feed_obj is None:
            return _bad("构想模块没就位，这次读不到")
        try:
            items = feed_obj.ideas_view(gid)
        except Exception as e:
            return _bad(f"读构想失败：{e}")
        if not items:
            return _ok(f"群 {gid} 现在没有构想", data=[])
        lines = [f"- #{it.get('id')} [{it.get('state') or ''}] {it.get('title') or ''}" for it in items[:30]]
        return _ok(f"群 {gid} 构想 {len(items)} 条：\n" + "\n".join(lines), data=items)

    async def list_tasks(ctx: ToolContext, args: dict) -> ToolResult:
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        try:
            items = svc.tasks.list_view(gid)
        except Exception as e:
            return _bad(f"读任务失败：{e}")
        if not items:
            return _ok(f"群 {gid} 现在没有任务", data=[])
        lines = [f"- {it.get('id')} [{it.get('status')}] {it.get('title')}" for it in items]
        return _ok(f"群 {gid} 任务 {len(items)} 个：\n" + "\n".join(lines), data=items)

    async def task_detail(ctx: ToolContext, args: dict) -> ToolResult:
        tid = str(args.get("task_id") or "").strip()
        if not tid:
            return _bad("要给 task_id")
        try:
            view = svc.tasks.detail_view(tid, admin=True)
        except KeyError:
            return _bad(f"没有任务 {tid}")
        except Exception as e:
            return _bad(f"读任务详情失败：{e}")
        if not view:
            return _bad(f"没有任务 {tid}")
        # detail_view 的 dict 里没有 group_id 键（tasks._list_item 不带它）——
        # 线上就踩过「拿 view.get('group_id') 做服务群检查，永远是空」的坑；
        # 群号从 tasks 表行（Tasks.get）取
        row_gid = ""
        getter = getattr(svc.tasks, "get", None)
        if callable(getter):
            try:
                row = getter(tid)
                row_gid = str((row or {}).get("group_id") or "")
            except Exception:
                row_gid = ""
        gid, err = _served(row_gid or str(view.get("group_id") or ""))
        if err is not None:
            return err
        lines = [
            f"任务 {tid}：{view.get('title')}",
            f"状态：{view.get('status')}",
        ]
        req = view.get("request") or view.get("req") or ""
        if req:
            lines.append(f"要求：{req}")
        crit = view.get("criteria") or []
        if crit:
            lines.append(
                "完成标准：" + "；".join(str(c.get("text") if isinstance(c, dict) else c) for c in crit)
            )
        calls = view.get("timeline") or view.get("calls") or []
        if calls:
            lines.append(f"最近工具调用 {len(calls)} 条")
        return _ok("\n".join(lines), data=view)

    async def list_goals(ctx: ToolContext, args: dict) -> ToolResult:
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        try:
            rows = svc.store.read().execute(
                "SELECT id, kind, title, state, criteria, next_check_ts, stale_reason"
                " FROM goals WHERE group_id=? ORDER BY created DESC, id DESC LIMIT 50",
                (gid,),
            ).fetchall()
        except Exception as e:
            return _bad(f"读目标失败：{e}")
        data = [dict(r) for r in rows]
        if not data:
            return _ok(f"群 {gid} 现在没有目标", data=[])
        lines = [f"- {r['id']} [{r['kind']}/{r['state']}] {r['title']}" for r in data]
        return _ok(f"群 {gid} 目标 {len(data)} 个：\n" + "\n".join(lines), data=data)

    async def list_requests(ctx: ToolContext, args: dict) -> ToolResult:
        status = str(args.get("status") or "pending").strip() or "pending"
        gid_raw = str(args.get("group_id") or ctx.group_id or "").strip()
        sql = (
            "SELECT id, group_id, kind, title, quote, requester_name, status, task_id, goal_id, created"
            " FROM requests WHERE status=?"
        )
        params: list[Any] = [status]
        if gid_raw and gid_raw != "*":
            gid, err = _served(gid_raw)
            if err is not None:
                return err
            sql += " AND group_id=?"
            params.append(gid)
        else:
            # 没指定群（含 "*"）：只看服务群——库里可能有早先配置过、后来下掉群的残留，
            # 那些不是管理员该看到的东西
            served = sorted(_served_groups(_settings()))
            if not served:
                return _ok("现在读不到服务群名单，先不列", data=[])
            sql += f" AND group_id IN ({','.join('?' * len(served))})"
            params.extend(served)
        sql += " ORDER BY created DESC, id DESC LIMIT 50"
        try:
            rows = svc.store.read().execute(sql, params).fetchall()
        except Exception as e:
            return _bad(f"读待批请求失败：{e}")
        data = [dict(r) for r in rows]
        if not data:
            return _ok(f"没有「{status}」状态的请求", data=[])
        lines = [
            f"- {r['id']} [{r['kind']}] {r['title']}"
            f"（{r['requester_name'] or '不认识的群友'}，群 {r['group_id']}）"
            for r in data
        ]
        return _ok(f"「{status}」请求 {len(data)} 条：\n" + "\n".join(lines), data=data)

    async def read_chat(ctx: ToolContext, args: dict) -> ToolResult:
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        hours = _int(args.get("hours"), 24, 1, 14 * 24)
        limit = _int(args.get("limit"), 50, 1, 300)
        since = clock.now() - hours * 3600.0
        try:
            rows = svc.store.read().execute(
                "SELECT ts, user_name, user_id, text FROM chat_log"
                " WHERE group_id=? AND ts>=? ORDER BY ts DESC LIMIT ?",
                (gid, since, limit),
            ).fetchall()
        except Exception as e:
            return _bad(f"读群聊记录失败：{e}")
        data = [dict(r) for r in rows]
        if not data:
            return _ok(f"群 {gid} 最近 {hours} 小时没有留存的群聊记录", data=[])
        lines = []
        for r in reversed(data):
            t = clock.bj(float(r["ts"] or 0)).strftime("%m-%d %H:%M")
            lines.append(f"[{t}] {r['user_name'] or r['user_id']}: {r['text']}")
        return _ok(f"群 {gid} 最近 {hours} 小时 {len(data)} 条：\n" + "\n".join(lines), data=data)

    async def read_logs(ctx: ToolContext, args: dict) -> ToolResult:
        sql = "SELECT ts, purpose, role, model, group_id, ok, status, ms, error FROM model_calls"
        where: list[str] = []
        params: list[Any] = []
        if bool(args.get("failed_only")):
            where.append("ok=0")
        # 只看服务群和全局（没群号）的调用：下掉的群残留的记录不是管理员该看的
        served = sorted(_served_groups(_settings()))
        if served:
            where.append(f"(group_id IN ({','.join('?' * len(served))}) OR group_id='')")
            params.extend(served)
        else:
            where.append("group_id=''")  # 读不到服务群名单时只看全局，宁可少看
        sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        limit = _int(args.get("limit"), 20, 1, 200)
        params.append(limit)
        try:
            rows = svc.store.read().execute(sql, params).fetchall()
        except Exception as e:
            return _bad(f"读模型调用日志失败：{e}")
        data = [dict(r) for r in rows]
        if not data:
            return _ok("没有符合条件的模型调用记录", data=[])
        lines = []
        for r in data:
            t = clock.bj(float(r["ts"] or 0)).strftime("%m-%d %H:%M")
            err = str(r["error"] or "")
            lines.append(
                f"[{t}] {r['purpose']}({r['role']}) {r['model']} 群{r['group_id']} "
                f"{'成功' if int(r['ok']) else '失败'} HTTP {r['status']} {r['ms']}ms"
                + (f" {err}" if err else "")
            )
        return _ok(f"最近 {len(data)} 条模型调用：\n" + "\n".join(lines), data=data)

    def _flat_rules(settings: Any) -> list[str]:
        out: list[str] = []
        for section in ("delivery", "topics", "approval", "feeds"):
            obj = getattr(settings, section, None)
            if obj is None:
                continue
            for field in (
                "quiet_hours", "push_per_day", "enabled", "per_day", "min_gap_hours",
                "required", "admins", "exempt_groups", "exempt_users", "remind",
                "news_slots", "max_items", "web_min_avg", "pool_min_avg", "guides",
            ):
                if hasattr(obj, field):
                    out.append(f"{section}.{field}={getattr(obj, field)}")
        return out

    async def get_rules(ctx: ToolContext, args: dict) -> ToolResult:
        settings = _settings()
        store = getattr(svc, "store", None)
        if settings is None:
            return _bad("读不到规则：设置没就位")
        view: dict = {}
        if store is not None:
            try:
                view = rules.rules_view(settings, store)
            except Exception:
                logger.exception("rules_view 失败，退回有效设置")
        lines = _flat_rules(settings)
        return _ok("现在的规则：\n" + "\n".join(lines), data=view or {"flat": lines})

    async def get_identity(ctx: ToolContext, args: dict) -> ToolResult:
        ident = _identity()
        if ident is None:
            return _ok("身份文件（SOUL / AGENTS / MEMORY）还没建出来，也没有现成的可读", data={})
        out: dict[str, Any] = {}
        lines: list[str] = []
        for kind, label in (("soul", "SOUL.md"), ("agents", "AGENTS.md"), ("memory", "MEMORY.md")):
            try:
                got = ident.read(kind) or {}
                text = str(got.get("text") or "")
                out[kind] = got
                lines.append(f"【{label}】{_tail(text, 600) if text else '（空的）'}")
            except Exception as e:
                lines.append(f"【{label}】读不到：{e}")
        return _ok("\n".join(lines), data=out)

    async def list_extensions(ctx: ToolContext, args: dict) -> ToolResult:
        lines: list[str] = []
        data: list[dict] = []
        exts = getattr(svc, "extensions", None)
        if exts is not None:
            try:
                for item in exts.info():
                    data.append(dict(item))
                    lines.append(
                        f"- MCP {item.get('name')}（{'开' if item.get('enabled') else '关'}，"
                        f"{'连得上' if item.get('ok') else '没连上'}，{item.get('tools', 0)} 个工具）"
                    )
            except Exception as e:
                return _bad(f"读扩展失败：{e}")
        skills = _skills()
        if skills is not None:
            try:
                for sk in skills.list():
                    lines.append(f"- skill {sk.get('name')}：{sk.get('description') or ''}")
            except Exception:
                pass
        if not lines:
            return _ok("现在一个扩展也没有", data=data)
        return _ok("\n".join(lines), data=data)

    # ------------------------------------------------------------------
    # 二、写类（改了库，但没对外发东西、没放宽安全设置）
    # ------------------------------------------------------------------

    def _entry_of(gid: str, entry_id: Any) -> Any:
        """取本群的一条画像条目；别的群的、不存在的都拒。返回 dict 或 ToolResult(错误)。"""
        try:
            eid = int(entry_id)
        except (TypeError, ValueError):
            return _bad("这个动作要给 entry_id")
        row = svc.store.read().execute(
            "SELECT id, group_id, category, text, locked, deleted FROM profile_entries WHERE id=?",
            (eid,),
        ).fetchone()
        if row is None or int(row["deleted"]):
            return _bad(f"画像条目 #{eid} 不存在")
        if str(row["group_id"]) != gid:
            return _bad(f"画像条目 #{eid} 不属于群 {gid}，不能跨群改")
        return dict(row)

    async def profile_edit(ctx: ToolContext, args: dict) -> ToolResult:
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        prof = _profiles()
        if prof is None:
            return _bad("画像模块没就位，这次改不了画像")
        action = str(args.get("action") or "").strip().lower()
        if action == "add":
            text = str(args.get("text") or "").strip()
            if not text:
                return _bad("add 要给 text")
            try:
                eid = prof.add_entry(gid, str(args.get("category") or "").strip(), text)
            except ValueError as e:
                return _bad(str(e))
            except Exception as e:
                return _bad(f"加画像条目失败：{e}")
            return _ok(f"已加一条画像（#{eid}，已锁定）", data={"id": eid})
        if action in ("edit", "lock", "unlock", "delete"):
            entry = _entry_of(gid, args.get("entry_id"))
            if isinstance(entry, ToolResult):
                return entry
            eid = int(entry["id"])
            try:
                if action == "edit":
                    new_text = str(args.get("text") or "").strip()
                    if not new_text:
                        return _bad("edit 要给新的 text")
                    prof.edit_entry(eid, text=new_text)
                    word = "已改写"
                elif action == "lock":
                    prof.edit_entry(eid, locked=True)
                    word = "已锁定"
                elif action == "unlock":
                    prof.edit_entry(eid, locked=False)
                    word = "已解锁"
                else:
                    prof.delete_entry(eid)
                    word = "已删除"
            except ValueError as e:
                return _bad(str(e))
            except Exception as e:
                return _bad(f"改画像条目失败：{e}")
            return _ok(f"{word}画像条目 #{eid}", data={"id": eid})
        return _bad("action 只能是 add / edit / lock / unlock / delete")

    async def profile_bulk_delete(ctx: ToolContext, args: dict) -> ToolResult:
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        raw_ids = args.get("entry_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            return _bad("entry_ids 要给一个非空列表")
        args = {**args, "group_id": gid}
        pending = _need_confirm("profile_bulk_delete", args, f"一次删掉群 {gid} 的 {len(raw_ids)} 条画像")
        if pending is not None:
            return pending
        prof = _profiles()
        if prof is None:
            return _bad("画像模块没就位，这次删不了")
        done: list[int] = []
        skipped: list[str] = []
        for raw in raw_ids:
            entry = _entry_of(gid, raw)
            if isinstance(entry, ToolResult):
                skipped.append(str(raw))
                continue
            try:
                prof.delete_entry(int(entry["id"]))
                done.append(int(entry["id"]))
            except Exception:
                skipped.append(str(raw))
        text = f"已删 {len(done)} 条画像"
        if skipped:
            text += f"，跳过 {len(skipped)} 条（不属于本群或不存在）"
        return _ok(text, data={"deleted": done, "skipped": skipped})

    async def focus_edit(ctx: ToolContext, args: dict) -> ToolResult:
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        prof = _profiles()
        if prof is None:
            return _bad("画像模块没就位，这次改不了关注成员")
        uid = str(args.get("user_id") or "").strip()
        action = str(args.get("action") or "").strip().lower()
        if action not in ("add", "remove", "auto"):
            return _bad("action 只能是 add / remove / auto")
        try:
            prof.set_focus(gid, uid, action)
        except ValueError as e:
            return _bad(str(e))
        except Exception as e:
            return _bad(f"改关注成员失败：{e}")
        word = {"add": "已设成一直关注", "remove": "已取消关注", "auto": "已改回自动挑"}[action]
        return _ok(f"{word}（{uid}）", data={"user_id": uid, "action": action})

    async def set_feeds_pref(ctx: ToolContext, args: dict) -> ToolResult:
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        text = str(args.get("text") or "").strip()
        try:
            feed_obj = _feeds()
            if feed_obj is not None:
                saved = feed_obj.set_pref(gid, text)
            else:
                saved = text[:300]
                with svc.store.tx() as conn:
                    svc.store.kv_set(conn, f"feeds.pref.{gid}", saved)
        except Exception as e:
            return _bad(f"写资讯偏好失败：{e}")
        return _ok(f"群 {gid} 的资讯偏好已更新：{saved or '（清空了）'}", data={"pref": saved})

    async def rss_add(ctx: ToolContext, args: dict) -> ToolResult:
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        url = str(args.get("url") or "").strip()
        if not url:
            return _bad("要给 url")
        title = str(args.get("title") or "").strip()
        from . import rss as _rss

        try:
            feed = _rss.add_feed(
                svc.store, gid, url=url, title=title or url, feed_id="", now=clock.now()
            )
        except _rss.RssError as e:
            return _bad(str(e))
        except Exception as e:
            return _bad(f"加 RSS 源失败：{e}")
        return _ok(f"已给群 {gid} 加了一个 RSS 源：{feed.get('title') or url}", data=feed)

    async def rss_remove(ctx: ToolContext, args: dict) -> ToolResult:
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        feed_id = str(args.get("feed_id") or "").strip()
        if not feed_id:
            return _bad("要给 feed_id")
        from . import rss as _rss

        rows = _rss.list_feeds(svc.store, gid)
        target = next((f for f in rows if str(f.get("id")) == feed_id), None)
        if target is None:
            return _bad(f"群 {gid} 没有 id 为 {feed_id} 的 RSS 源")
        args = {**args, "group_id": gid}
        pending = _need_confirm("rss_remove", args, f"从群 {gid} 删掉 RSS 源「{target.get('title') or feed_id}」")
        if pending is not None:
            return pending
        try:
            _rss.remove_feed(svc.store, gid, feed_id)
        except Exception as e:
            return _bad(f"删 RSS 源失败：{e}")
        return _ok(f"已从群 {gid} 删掉 RSS 源 {feed_id}")

    async def block_domain(ctx: ToolContext, args: dict) -> ToolResult:
        raw = str(args.get("domain") or "").strip().lower().strip(".")
        domain = raw[4:] if raw.startswith("www.") else raw
        if not _DOMAIN_RE.match(domain):
            return _bad(f"「{raw}」不像一个域名（要像 example.com）")
        blocked = bool(args.get("blocked", True))
        settings = _settings()
        config_blocked = list(getattr(getattr(settings, "feeds", None), "blocked_domains", ()) or ())
        try:
            kv = svc.store.kv_get("feeds.blocked_domains", None)
            current = {str(d).lower() for d in kv} if isinstance(kv, list) else set(config_blocked)
        except Exception:
            current = set(config_blocked)
        if blocked:
            current.add(domain)
        else:
            current.discard(domain)
        names = sorted(current)
        try:
            with svc.store.tx() as conn:
                svc.store.kv_set(conn, "feeds.blocked_domains", names)
        except Exception as e:
            return _bad(f"写屏蔽名单失败：{e}")
        return _ok(
            f"已{'屏蔽' if blocked else '取消屏蔽'} {domain}；现在屏蔽名单共 {len(names)} 个域名",
            data={"blocked_domains": names},
        )

    def _validated_patch(patch: Any) -> dict[str, dict[str, Any]] | ToolResult:
        """先把 patch 逐字段验一遍（返回 {节: {字段: 规范值}} 或错误结果）。

        先验再问：值本身就不合法（比如推送上限 99）时直接报错，不该记一张
        「待确认」小票让管理员去点一个根本执行不了的动作。
        """
        if not isinstance(patch, dict) or not patch:
            return _bad('要给 patch，形如 {"delivery": {"push_per_day": 5}}')
        out: dict[str, dict[str, Any]] = {}
        try:
            for section, fields in patch.items():
                if not isinstance(fields, dict):
                    continue
                for field, value in fields.items():
                    key, normalized = rules.validate_patch(f"{section}.{field}", value)
                    sec, _, fld = key.partition(".")
                    out.setdefault(sec, {})[fld] = normalized
        except ValueError as e:
            return _bad(str(e))
        if not out:
            return _bad("patch 里没有能改的字段")
        return out

    def _loosens_security(validated: dict[str, dict[str, Any]]) -> str:
        """验过的 patch 里有没有「放宽安全 / 让 MaiWork 更常往群里发东西」的；返回中文说明（没有 → ""）。

        判定口径（宁可多确认一次，不让模型绕过确认）：
        - approval 节里除了下面两个「自动审核」键，**任何字段**（管理员名单、免批群 / 免批人、
          批准开关、提醒开关）都是权限边界，一律要管理员点头；
        - approval.auto_review：从「关」变「开」= 放宽（要确认）；关掉不用；
        - approval.auto_review_daily：上限调大 = 放宽（要确认）；调低不用；
        - delivery.push_per_day：比现在大（或 >5）= 推送变多；
        - delivery.quiet_hours：安静窗口变窄 = 能发的时间段变长；
        - topics.per_day 变大、min_gap_hours 变小、enabled 从关到开 = 开话题更勤。
        比「现在值」时以有效设置为准；读不到当前值时，但凡沾边一律算放宽（要确认）。
        """
        out: list[str] = []
        approval = validated.get("approval") or {}
        _APPROVAL_ZH = {
            "required": "派活批准开关",
            "remind": "待批提醒开关",
            "admins": "bot 管理员名单",
            "exempt_groups": "免批准的群",
            "exempt_users": "免批准的人",
            "auto_review": "自动审核轻活",
            "auto_review_daily": "每群每天最多自动批",
        }
        current = _settings()
        now_approval = getattr(current, "approval", None)
        for field, value in approval.items():
            label = _APPROVAL_ZH.get(field, f"approval.{field}")
            if field == "auto_review":
                # 只有「打开」算放宽；关掉不确认。读不到当前值时按放宽处理。
                now_on = getattr(now_approval, "auto_review", None)
                if value is True and now_on is not True:
                    out.append(f"打开{label}")
                continue
            if field == "auto_review_daily":
                # 只有「上限调大」算放宽；调低不确认。读不到当前值时按放宽处理。
                now_n = getattr(now_approval, "auto_review_daily", None)
                if not isinstance(now_n, int) or isinstance(now_n, bool):
                    now_n = None
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and (now_n is None or value > now_n)
                ):
                    out.append(f"每天最多自动批放宽到 {value}")
                continue
            if field in ("required", "remind"):
                if value is False:
                    out.append(f"关掉{label}")
                else:
                    out.append(f"打开{label}")
            else:
                members = "、".join(str(m) for m in value) if isinstance(value, list) else str(value)
                out.append(f"改动{label}（{members or '清空'}）")
        delivery = validated.get("delivery") or {}
        if "push_per_day" in delivery:
            new_push = delivery["push_per_day"]
            now_push = getattr(getattr(current, "delivery", None), "push_per_day", None)
            if not isinstance(now_push, int) or isinstance(now_push, bool):
                now_push = None
            if isinstance(new_push, int) and not isinstance(new_push, bool):
                if now_push is None or new_push > 5 or new_push > now_push:
                    out.append(f"每天推送上限放宽到 {new_push}")
        if "quiet_hours" in delivery:
            new_q = _quiet_minutes(delivery["quiet_hours"])
            now_q = _quiet_minutes(getattr(getattr(current, "delivery", None), "quiet_hours", ""))
            if new_q is None or now_q is None or new_q < now_q:
                out.append(f"安静时段收窄成 {delivery['quiet_hours']}（能发东西的时间段变长）")
        topics = validated.get("topics") or {}
        now_topics = getattr(current, "topics", None)
        if "per_day" in topics:
            new_pd = topics["per_day"]
            now_pd = getattr(now_topics, "per_day", None)
            if not isinstance(now_pd, int) or (isinstance(new_pd, int) and new_pd > now_pd):
                out.append(f"每天开话题上限放宽到 {new_pd}")
        if "min_gap_hours" in topics:
            new_gap = topics["min_gap_hours"]
            now_gap = getattr(now_topics, "min_gap_hours", None)
            if not isinstance(now_gap, int) or (isinstance(new_gap, int) and new_gap < now_gap):
                out.append(f"开话题最小间隔缩到 {new_gap} 小时")
        if "enabled" in topics:
            now_on = getattr(now_topics, "enabled", None)
            if topics["enabled"] is True and now_on is not True:
                out.append("打开冷场开话题")

        # 去重保序
        seen: set[str] = set()
        uniq = [x for x in out if not (x in seen or seen.add(x))]
        return "；".join(uniq)

    def _patch_summary(validated: dict[str, dict[str, Any]], loosen: str) -> str:
        """小票摘要：patch 里**全部**要改的字段都列出来（节.键 → 新值，名单类显示成员），
        超长截断但写明「共改 N 项」——管理员点头前看到的必须和真正会改的一致。

        全部字段都用中文名（和网页「全部配置」同一套：rules.CONFIG_BY_KEY 的 label），
        不给管理员看 approval.admins 这种英文键名；万一哪个键没登记中文名才退回「节.字段」。
        """
        def _label(key: str) -> str:
            spec = rules.CONFIG_BY_KEY.get(key) or {}
            return str(spec.get("label") or key)

        items: list[str] = []
        for section in sorted(validated.keys()):
            for field in sorted(validated[section].keys()):
                value = validated[section][field]
                if isinstance(value, list):
                    shown = "、".join(str(m) for m in value) or "（清空）"
                elif isinstance(value, bool):
                    shown = "开" if value else "关"
                else:
                    shown = str(value)
                name = _label(f"{section}.{field}")
                items.append(f"{name} → {shown}")
        total = len(items)
        head = "改规则：" + "；".join(items)
        if len(head) <= MAX_SUMMARY - 1:
            return head
        # 截断：尽量多装几项，末尾写明总数
        suffix = f"…（共改 {total} 项）"
        keep = MAX_SUMMARY - len(suffix)
        return head[: max(0, keep)] + suffix

    async def set_rules(ctx: ToolContext, args: dict) -> ToolResult:
        patch = args.get("patch")
        validated = _validated_patch(patch)
        if isinstance(validated, ToolResult):
            return validated
        base = None
        getter = getattr(svc, "base_settings", None)
        if callable(getter):
            try:
                base = getter()
            except Exception:
                base = None
        # 放宽安全设置 / 让 MaiWork 更常往群里发东西的改动，要先让管理员点头；
        # 摘要列出 patch 里的全部字段（不是只写放宽那一项）——点同意前看到的就是真要改的
        loosen = _loosens_security(validated)
        if loosen:
            pending = _need_confirm("set_rules", args, _patch_summary(validated, loosen))
            if pending is not None:
                return pending
        try:
            override = rules.save_patch(svc.store, patch, base=base)
        except ValueError as e:
            return _bad(str(e))
        except Exception as e:
            return _bad(f"存规则失败：{e}")
        settings = _settings()
        lines: list[str] = []
        for section, fields in override.items():
            for field in fields:
                value = getattr(getattr(settings, section, None), field, fields[field])
                key = f"{section}.{field}"
                name = str((rules.CONFIG_BY_KEY.get(key) or {}).get("label") or key)
                if isinstance(value, bool):
                    value = "开" if value else "关"
                elif isinstance(value, (list, tuple)):
                    value = "、".join(str(m) for m in value) or "（清空）"
                lines.append(f"{name} → {value}")
        if not lines:
            return _ok("这些值和配置文件里一样，等于没改", data=override)
        return _ok("规则已改：\n" + "\n".join(lines), data=override)

    async def identity_edit(ctx: ToolContext, args: dict) -> ToolResult:
        kind = str(args.get("kind") or "").strip().lower()
        text = str(args.get("text") or "")
        if kind not in ("soul", "agents", "memory", "group"):
            return _bad("kind 只能是 soul / agents / memory / group")
        ident = _identity()
        if ident is None:
            return _bad("身份文件没就位，这次改不了")
        try:
            if kind == "group":
                gid, err = _served(args.get("group_id") or ctx.group_id)
                if err is not None:
                    return err
                ident.group_write(gid, text)
                return _ok(f"已更新群 {gid} 的工作记忆（{len(text)} 字）")
            ident.write(kind, text)
        except ValueError as e:
            return _bad(str(e))
        except Exception as e:
            return _bad(f"写身份文件失败：{e}")
        return _ok(f"已更新 {kind} 身份文件（{len(text)} 字）")

    async def remember(ctx: ToolContext, args: dict) -> ToolResult:
        text = str(args.get("text") or "").strip()
        if not text:
            return _bad("要给 text（要记住的事）")
        gid_raw = str(args.get("group_id") or ctx.group_id or "").strip()
        gid = ""
        if gid_raw:
            gid, err = _served(gid_raw)
            if err is not None:
                return err
        ident = _identity()
        if ident is None:
            return _bad("身份文件没就位，这次记不住")
        try:
            out = ident.remember_sync(
                scope="group" if gid else "global", text=text, reason="管理员对话", group_id=gid or ""
            )
        except Exception as e:
            return _bad(f"记不住：{e}")
        if isinstance(out, dict) and not out.get("ok"):
            return _bad(str(out.get("error") or out.get("reason") or "这条不让记（隐私或去重规则挡了）"))
        return _ok(f"已记住：{_tail(text, 80)}", data=out)

    # ------------------------------------------------------------------
    # 三、危险类（一律先确认）
    # ------------------------------------------------------------------

    async def send_group_message(ctx: ToolContext, args: dict) -> ToolResult:
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        text = str(args.get("text") or "").strip()
        if not text:
            return _bad("text 不能为空")
        # 解析出来的群号（可能是对话焦点群）补进要存票的参数：票的 args 就是执行时的全部凭据，
        # 管理员看到的是哪个群、同意后动的也就是哪个群
        args = {**args, "group_id": gid}
        pending = _need_confirm("send_group_message", args, f"发到群 {gid}：{_tail(text, 100)}")
        if pending is not None:
            return pending
        outbox = getattr(svc, "outbox", None)
        if outbox is None:
            return _bad("发件箱没就位，这条发不出去")
        try:
            import hashlib

            digest = hashlib.sha1(f"{gid}|{text}|{clock.now()}".encode("utf-8")).hexdigest()[:12]
            oid = outbox.enqueue(
                f"admin-chat-send:{gid}:{digest}",
                gid,
                "text",
                {"text": text, "push_kind": "admin_chat"},
                task_id=None,
            )
            flush = getattr(outbox, "flush", None)
            if callable(flush):
                await flush(clock.now())
        except Exception as e:
            logger.exception("管理员确认的发群消息入队失败（群 %s）", gid)
            return _bad(f"发群消息失败：{e}")
        return _ok(f"已交给发件箱，马上发到群 {gid}（发件箱 #{oid}）", data={"outbox_id": oid})

    async def group_notice_send(ctx: ToolContext, args: dict) -> ToolResult:
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        content = str(args.get("content") or "").strip()
        if not content:
            return _bad("content 不能为空")
        args = {**args, "group_id": gid}
        pending = _need_confirm("group_notice_send", args, f"给群 {gid} 发群公告：{_tail(content, 100)}")
        if pending is not None:
            return pending
        space = _group_space(gid)
        if space is None:
            return _bad("群空间能力没就位，这条公告发不出去")
        announce = getattr(svc, "_groupspace_announce", None)
        try:
            await space.send_notice(gid, content, announce=announce)
        except Exception as e:
            return _bad(f"发群公告失败：{e}")
        return _ok(f"公告已发出：{_tail(content, 60)}")

    async def group_file_manage(ctx: ToolContext, args: dict) -> ToolResult:
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        action = str(args.get("action") or "").strip().lower()
        if action not in ("delete", "rename", "move", "mkdir", "rmdir"):
            return _bad("action 只能是 delete / rename / move / mkdir / rmdir")
        space = _group_space(gid)
        if space is None:
            return _bad("群空间能力没就位，这次动不了群文件")
        args = {**args, "group_id": gid}
        pending = _need_confirm("group_file_manage", args, f"群 {gid} 的群文件 {action}")
        if pending is not None:
            return pending
        try:
            if action == "delete":
                await space.delete_file(gid, str(args.get("file_id") or ""))
            elif action == "rename":
                await space.rename_file(gid, str(args.get("file_id") or ""), str(args.get("name") or ""))
            elif action == "move":
                await space.move_file(gid, str(args.get("file_id") or ""), str(args.get("folder_id") or ""))
            elif action == "rmdir":
                await space.delete_folder(gid, str(args.get("folder_id") or ""))
            else:
                await space.create_folder(gid, str(args.get("name") or ""), str(args.get("folder_id") or "") or None)
        except Exception as e:
            return _bad(f"群文件操作失败：{e}")
        return _ok(f"群文件 {action} 已做")

    async def group_album_upload(ctx: ToolContext, args: dict) -> ToolResult:
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        path = str(args.get("path") or "").strip()
        if not path:
            return _bad("path 不能为空")
        # 路径必须在**这个群的工作区**里、不许是符号链接（和 outbox 群文件上传同一套口径）；
        # 写小票前就查，不合规直接拒——票里绝不装一个指向工作区外的路径
        try:
            _check_upload_path(path, _workspace_root_of(gid))
        except ValueError as e:
            return _bad(str(e))
        args = {**args, "group_id": gid}
        pending = _need_confirm("group_album_upload", args, f"把 {path} 传进群 {gid} 的相册")
        if pending is not None:
            return pending
        space = _group_space(gid)
        if space is None:
            return _bad("群空间能力没就位，这次传不了相册")
        try:
            await space.upload_to_album(gid, str(args.get("album_id") or ""), path)
        except Exception as e:
            return _bad(f"传群相册失败：{e}")
        return _ok(f"已把 {path} 传进群相册")

    async def skill_delete(ctx: ToolContext, args: dict) -> ToolResult:
        name = str(args.get("name") or "").strip()
        if not name:
            return _bad("要给 name")
        skills = _skills()
        if skills is None:
            return _bad("skill 目录没就位，这次删不了")
        try:
            if skills.read(name) is None:
                return _bad(f"没有 skill「{name}」")
        except Exception as e:
            return _bad(f"查 skill 失败：{e}")
        pending = _need_confirm("skill_delete", args, f"删掉 skill「{name}」")
        if pending is not None:
            return pending
        settings = _settings()
        data_dir = getattr(settings, "data_dir", None) or getattr(svc, "data_dir", None)
        try:
            from . import skills_web

            skills_web.delete(data_dir, svc.store, name)
        except KeyError:
            return _bad(f"没有 skill「{name}」")
        except Exception as e:
            return _bad(f"删 skill 失败：{e}")
        return _ok(f"已删掉 skill「{name}」")

    async def mcp_toggle(ctx: ToolContext, args: dict) -> ToolResult:
        name = str(args.get("name") or "").strip()
        if not name:
            return _bad("要给 name")
        enabled = bool(args.get("enabled", True))
        exts = getattr(svc, "extensions", None)
        settings = _settings()
        if exts is None or settings is None:
            return _bad("这个 MaiWork 还没接 MCP 扩展管理，现在开不了也关不了")
        try:
            from . import extensions_web

            extensions_web.toggle(svc.store, settings, name, enabled)
            reloader = getattr(exts, "reload", None)
            if callable(reloader):
                await reloader(name, svc.tools)
        except KeyError:
            return _bad(f"没有叫「{name}」的 MCP 扩展")
        except ValueError as e:
            return _bad(str(e))
        except Exception as e:
            return _bad(f"开关 MCP 扩展失败：{e}")
        return _ok(f"MCP 扩展「{name}」已{'打开' if enabled else '关闭'}")

    async def mcp_delete(ctx: ToolContext, args: dict) -> ToolResult:
        name = str(args.get("name") or "").strip()
        if not name:
            return _bad("要给 name")
        exts = getattr(svc, "extensions", None)
        settings = _settings()
        if exts is None or settings is None:
            return _bad("这个 MaiWork 还没接 MCP 扩展管理，现在删不了")
        try:
            from . import extensions_web

            if extensions_web.source_of(settings, svc.store, name) is None:
                return _bad(f"没有叫「{name}」的 MCP 扩展")
        except Exception as e:
            return _bad(f"查 MCP 扩展失败：{e}")
        pending = _need_confirm("mcp_delete", args, f"删掉 MCP 扩展「{name}」")
        if pending is not None:
            return pending
        try:
            extensions_web.delete(svc.store, name)
            remover = getattr(exts, "remove", None)
            if callable(remover):
                await remover(name, svc.tools)
        except KeyError:
            return _bad(f"没有叫「{name}」的 MCP 扩展")
        except Exception as e:
            return _bad(f"删 MCP 扩展失败：{e}")
        return _ok(f"已删掉 MCP 扩展「{name}」")

    async def run_news_now(ctx: ToolContext, args: dict) -> ToolResult:
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        feeds = _feeds()
        if feeds is None:
            return _bad("资讯模块没就位，现在备不了料")
        runner = getattr(svc, "run_news_now", None)
        if callable(runner):
            try:
                out = runner(gid)
            except Exception as e:
                return _bad(f"开备料失败：{e}")
            if isinstance(out, dict) and not out.get("started", True):
                return _bad(str(out.get("reason") or "这个群现在备不了料"))
            return _ok(f"已经让群 {gid} 现在备一批资讯（后台跑，好了会在网页上出现）")
        # 没有现成入口（老服务包 / 测试）：自己起一个后台任务跑
        prepare = getattr(feeds, "prepare_news", None)
        if not callable(prepare):
            return _bad("这个 MaiWork 的资讯模块没有备料入口")
        try:
            asyncio.get_running_loop().create_task(prepare(gid))
        except RuntimeError:
            return _bad("现在没有事件循环，起不了后台备料")
        return _ok(f"已经让群 {gid} 现在备一批资讯（后台跑，好了会在网页上出现）")

    async def make_idea_now(ctx: ToolContext, args: dict) -> ToolResult:
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        # 优先走 app 上的方法（和 run_news_now 对称：同群互斥 + _spawn_long_job 登记）
        runner = getattr(svc, "make_idea_now", None)
        if callable(runner):
            try:
                out = runner(gid)
            except Exception as e:
                return _bad(f"开构想失败：{e}")
            if isinstance(out, dict) and not out.get("started", True):
                return _bad(str(out.get("reason") or "这个群现在出不了构想"))
            return _ok(f"已经让群 {gid} 现在想一个构想（后台跑，好了会在网页上出现）")
        feeds = _feeds()
        maker = getattr(feeds, "make_idea", None) if feeds is not None else None
        if not callable(maker):
            return _bad("这个 MaiWork 的构想模块没就位，现在出不了构想")
        # 没有 app 方法（老服务包 / 测试）的回落：同群互斥——连调多次也只起一个
        busy = getattr(svc, "_mw_idea_now_running", None)
        if not isinstance(busy, set):
            busy = set()
            try:
                setattr(svc, "_mw_idea_now_running", busy)
            except Exception:
                return _bad("服务包记不下状态，这次先不开")
        if gid in busy:
            return _bad(f"群 {gid} 已经在想构想了，等它想完")
        busy.add(gid)

        async def _run() -> None:
            try:
                await maker(gid)
            except Exception:
                logger.exception("管理员点的构想出错了（群 %s）", gid)
            finally:
                busy.discard(gid)

        try:
            asyncio.get_running_loop().create_task(_run())
        except RuntimeError:
            busy.discard(gid)
            return _bad("现在没有事件循环，起不了后台任务")
        return _ok(f"已经让群 {gid} 现在想一个构想（后台跑）")

    async def refresh_profile(ctx: ToolContext, args: dict) -> ToolResult:
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        prof = _profiles()
        if prof is None:
            return _bad("画像模块没就位，现在整理不了")
        # 优先走 app.refresh_profile_now（同群互斥 + _spawn_long_job 登记，
        # 和平时画像刷新同一个入口 profiles.refresh(force=True)）
        runner = getattr(svc, "refresh_profile_now", None)
        if callable(runner):
            try:
                out = runner(gid)
            except Exception as e:
                return _bad(f"开整理失败：{e}")
            if isinstance(out, dict) and not out.get("started", True):
                return _bad(str(out.get("reason") or "这个群现在整理不了画像"))
            return _ok(f"已经开始重新整理群 {gid} 的画像，好了会在网页群画像里看到（后台跑）")
        # 没有 app 方法（老服务包 / 测试）的回落：同群互斥，自己起后台任务
        refresh = getattr(prof, "refresh", None)
        if not callable(refresh):
            return _bad("这个 MaiWork 的画像模块没有刷新入口")
        busy = getattr(svc, "_mw_profile_now_running", None)
        if not isinstance(busy, set):
            busy = set()
            try:
                setattr(svc, "_mw_profile_now_running", busy)
            except Exception:
                return _bad("服务包记不下状态，这次先不开")
        if gid in busy:
            return _bad(f"群 {gid} 已经在整理画像了，等它整理完")
        busy.add(gid)

        async def _run() -> None:
            try:
                await refresh(gid, force=True)
            except Exception:
                logger.exception("管理员点的画像整理出错了（群 %s）", gid)
            finally:
                busy.discard(gid)

        try:
            asyncio.get_running_loop().create_task(_run())
        except RuntimeError:
            busy.discard(gid)
            return _bad("现在没有事件循环，起不了后台整理")
        return _ok(f"已经开始重新整理群 {gid} 的画像，好了会在网页群画像里看到（后台跑）")

    # ------------------------------------------------------------------
    # 四、任务 / 目标 / 请求
    # ------------------------------------------------------------------

    async def create_task(ctx: ToolContext, args: dict) -> ToolResult:
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        title = str(args.get("title") or "").strip()
        req = str(args.get("request") or args.get("req") or "").strip()
        if not title:
            return _bad("要给 title")
        if not req:
            return _bad("要给 request（说清要干什么）")
        # 任务不是「建了就完事」：开工后做完 / 做不成 / 缺信息时 coordinator 会自动
        # 往群里发消息（coordinator.py 的 outbox.enqueue）——这等于间接对外说话，
        # 所以要先写小票，管理员同意后才建任务并开工
        args = {**args, "group_id": gid}
        pending = _need_confirm(
            "create_task",
            args,
            f"会派一个子 agent 去做：{_tail(title, 60)}；做完 / 做不成时会在群 {gid} 里说一句",
        )
        if pending is not None:
            return pending
        raw_criteria = args.get("criteria")
        criteria = [str(c) for c in raw_criteria] if isinstance(raw_criteria, list) else []
        try:
            tid = svc.tasks.create(
                gid,
                title=title,
                req=req,
                criteria=criteria,
                source="admin_chat",
                requester_id="",
                requester_name="bot 管理员（管理员对话）",
                status="queued",
            )
        except Exception as e:
            return _bad(f"建任务失败：{e}")
        spawn = getattr(svc, "spawn_run_task", None)
        if callable(spawn):
            try:
                spawn(str(tid))
            except Exception:
                logger.exception("任务 %s 开工失败", tid)
        return _ok(f"已建任务 {tid}：{title}（已开工）", data={"task_id": str(tid)})

    async def create_goal(ctx: ToolContext, args: dict) -> ToolResult:
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        title = str(args.get("title") or "").strip()
        if not title:
            return _bad("要给 title")
        criteria = args.get("criteria")
        criteria_l = [str(c) for c in criteria] if isinstance(criteria, list) else []
        if not criteria_l:
            return _bad("要给 criteria（至少要一条「怎么算做到」）")
        # agent 目标立下后，后台检查会往群里发汇报 / 完成话（间接对外说话），先写小票
        args = {**args, "group_id": gid}
        pending = _need_confirm(
            "create_goal",
            args,
            f"会立一个后台目标：{_tail(title, 60)}；之后会定期检查，有进展 / 完成时会在群 {gid} 里说一句",
        )
        if pending is not None:
            return pending
        body = str(args.get("body") or args.get("request") or title).strip()
        try:
            goal_id = svc.goals.create_agent(
                gid,
                title=title,
                body=body,
                criteria=criteria_l,
                by_text="bot 管理员（管理员对话）",
            )
        except Exception as e:
            return _bad(f"建目标失败：{e}")
        hours = _int(args.get("check_hours"), 0, 0, 24 * 30)
        if hours > 0:
            setter = getattr(svc.goals, "set_next_check", None)
            if callable(setter):
                try:
                    setter(goal_id, clock.now() + hours * 3600.0)
                except Exception:
                    logger.exception("设置目标 %s 检查间隔失败", goal_id)
        return _ok(f"已立目标 {goal_id}：{title}（后台会按标准盯着）", data={"goal_id": str(goal_id)})

    async def approve_request(ctx: ToolContext, args: dict) -> ToolResult:
        rid = str(args.get("request_id") or "").strip()
        if not rid:
            return _bad("要给 request_id")
        row = svc.store.read().execute(
            "SELECT id, group_id, title, status FROM requests WHERE id=?", (rid,)
        ).fetchone()
        if row is None:
            return _bad(f"没有请求 {rid}")
        gid, err = _served(str(row["group_id"]))
        if err is not None:
            return err
        # 群友派的活要 **bot 管理员**批准（红线）；模型可能被群内容注入，
        # 绝不能一句话就替管理员把活批了——先写小票，管理员在网页点同意才算数
        pending = _need_confirm("approve_request", args, f"批准请求 {rid}（{_tail(row['title'], 60)}，群 {gid}）")
        if pending is not None:
            return pending
        try:
            out = svc.approvals.approve(rid, by="bot 管理员（管理员对话）")
        except KeyError as e:
            return _bad(str(e))
        except ValueError as e:
            return _bad(str(e))
        except Exception as e:
            return _bad(f"批准失败：{e}")
        tid = ""
        if isinstance(out, dict):
            tid = str(out.get("task_id") or out.get("goal_id") or "")
        spawn = getattr(svc, "spawn_run_task", None)
        if tid and callable(spawn):
            try:
                spawn(tid)
            except Exception:
                logger.exception("批准后的任务 %s 开工失败", tid)
        return _ok(f"已批准请求 {rid}（{row['title']}）" + (f"，落成 {tid}" if tid else ""), data=out)

    async def reject_request(ctx: ToolContext, args: dict) -> ToolResult:
        rid = str(args.get("request_id") or "").strip()
        if not rid:
            return _bad("要给 request_id")
        reason = str(args.get("reason") or "").strip()
        row = svc.store.read().execute(
            "SELECT id, group_id FROM requests WHERE id=?", (rid,)
        ).fetchone()
        if row is None:
            return _bad(f"没有请求 {rid}")
        _gid, err = _served(str(row["group_id"]))  # 非服务群的请求碰都不碰
        if err is not None:
            return err
        try:
            out = svc.approvals.reject(rid, by="bot 管理员（管理员对话）")
        except KeyError as e:
            return _bad(str(e))
        except ValueError as e:
            return _bad(str(e))
        except Exception as e:
            return _bad(f"拒绝失败：{e}")
        return _ok(f"已拒绝请求 {rid}" + (f"：{reason}" if reason else ""), data=out)

    async def cancel_task(ctx: ToolContext, args: dict) -> ToolResult:
        tid = str(args.get("task_id") or "").strip()
        if not tid:
            return _bad("要给 task_id")
        task = svc.tasks.get(tid)
        if task is None:
            return _bad(f"没有任务 {tid}")
        gid, err = _served(str(task.get("group_id") or ""))
        if err is not None:
            return err
        reason = str(args.get("reason") or "").strip() or "管理员取消了"
        try:
            svc.tasks.transition(tid, "cancelled", reason=reason)
        except Exception as e:
            return _bad(f"取消任务失败：{e}")
        # 取消要把正在跑的子 agent 也停掉：所有取消路径（网页 / /mw / 这里）
        # 都汇聚到 app.cancel_task_run 这个统一入口，不再各写一遍
        stopper = getattr(svc, "cancel_task_run", None)
        if callable(stopper):
            try:
                stopper(tid)
            except Exception:
                logger.exception("停任务 %s 的后台协程出错", tid)
        return _ok(f"已取消任务 {tid}：{reason}", data={"task_id": tid, "status": "cancelled"})

    # ------------------------------------------------------------------
    # 注册清单（name / description / parameters 给模型看；handler 是上面的函数）
    # ------------------------------------------------------------------

    _SPECS: list[tuple[dict, Any]] = [
        (
            {
                "name": "list_groups",
                "description": "列出 MaiWork 服务的群（群号 + 工作区）。",
                "parameters": {"type": "object", "properties": {}},
            },
            list_groups,
        ),
        (
            {
                "name": "group_overview",
                "description": "一个服务群的整体情况：画像条数、资讯 / 构想、任务、在盯的事、工作区。",
                "parameters": {
                    "type": "object",
                    "properties": {"group_id": {"type": "string", "description": "群号（可选，默认当前对话聚焦的群）"}},
                },
            },
            group_overview,
        ),
        (
            {
                "name": "read_profile",
                "description": "读一个服务群的画像条目（带编号，改的时候用编号）。",
                "parameters": {
                    "type": "object",
                    "properties": {"group_id": {"type": "string", "description": "群号（可选）"}},
                },
            },
            read_profile,
        ),
        (
            {
                "name": "list_focus",
                "description": "看一个服务群现在关注了哪些成员。",
                "parameters": {
                    "type": "object",
                    "properties": {"group_id": {"type": "string", "description": "群号（可选）"}},
                },
            },
            list_focus,
        ),
        (
            {
                "name": "list_news",
                "description": "看一个服务群最近备出来的资讯。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "group_id": {"type": "string", "description": "群号（可选）"},
                        "days": {"type": "integer", "description": "看最近几天，默认 7"},
                    },
                },
            },
            list_news,
        ),
        (
            {
                "name": "list_ideas",
                "description": "看一个服务群现在的构想。",
                "parameters": {
                    "type": "object",
                    "properties": {"group_id": {"type": "string", "description": "群号（可选）"}},
                },
            },
            list_ideas,
        ),
        (
            {
                "name": "list_tasks",
                "description": "看一个服务群的任务列表。",
                "parameters": {
                    "type": "object",
                    "properties": {"group_id": {"type": "string", "description": "群号（可选）"}},
                },
            },
            list_tasks,
        ),
        (
            {
                "name": "task_detail",
                "description": "看一个任务的详情：要求、完成标准、最近的工具调用。",
                "parameters": {
                    "type": "object",
                    "properties": {"task_id": {"type": "string", "description": "任务编号"}},
                    "required": ["task_id"],
                },
            },
            task_detail,
        ),
        (
            {
                "name": "list_goals",
                "description": "看一个服务群在盯的目标（agent 目标 / 成员提醒）。",
                "parameters": {
                    "type": "object",
                    "properties": {"group_id": {"type": "string", "description": "群号（可选）"}},
                },
            },
            list_goals,
        ),
        (
            {
                "name": "list_requests",
                "description": "看群友派的活（待批 / 已批 / 已拒）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "group_id": {"type": "string", "description": "群号（可选，默认当前群；填 * 看所有服务群）"},
                        "status": {"type": "string", "description": "pending / approved / rejected / expired，默认 pending"},
                    },
                },
            },
            list_requests,
        ),
        (
            {
                "name": "read_chat",
                "description": "MaiWork 留存的群聊记录（最多最近 14 天，只读）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "group_id": {"type": "string", "description": "群号（可选）"},
                        "hours": {"type": "integer", "description": "看最近几小时，默认 24"},
                        "limit": {"type": "integer", "description": "最多几条，默认 50"},
                    },
                },
            },
            read_chat,
        ),
        (
            {
                "name": "read_logs",
                "description": "看模型调用日志（哪次失败了、什么错），失败原因已遮密钥。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "failed_only": {"type": "boolean", "description": "只看失败的"},
                        "limit": {"type": "integer", "description": "最多几条，默认 20"},
                    },
                },
            },
            read_logs,
        ),
        (
            {
                "name": "get_rules",
                "description": "看现在生效的规则（推送 / 开话题 / 派活批准 / 资讯，含网页改过的覆盖）。",
                "parameters": {"type": "object", "properties": {}},
            },
            get_rules,
        ),
        (
            {
                "name": "get_identity",
                "description": "看现在的身份与工作记忆（SOUL / AGENTS / MEMORY）。",
                "parameters": {"type": "object", "properties": {}},
            },
            get_identity,
        ),
        (
            {
                "name": "list_extensions",
                "description": "看现在的扩展：MCP 连接和 skill。",
                "parameters": {"type": "object", "properties": {}},
            },
            list_extensions,
        ),
        (
            {
                "name": "profile_edit",
                "description": "改一个服务群的画像条目：add 新增 / edit 改文字 / lock 锁定 / unlock 解锁 / delete 删除。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "group_id": {"type": "string", "description": "群号（可选）"},
                        "action": {"type": "string", "enum": ["add", "edit", "lock", "unlock", "delete"]},
                        "category": {"type": "string", "description": "add 时的类别：recent / interest / ongoing / convention / resource"},
                        "text": {"type": "string", "description": "add / edit 时的文字"},
                        "entry_id": {"type": "integer", "description": "edit / lock / unlock / delete 时的条目编号"},
                    },
                    "required": ["action"],
                },
            },
            profile_edit,
        ),
        (
            {
                "name": "profile_bulk_delete",
                "description": "一次删多条画像条目（要管理员确认）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "group_id": {"type": "string", "description": "群号（可选）"},
                        "entry_ids": {"type": "array", "items": {"type": "integer"}, "description": "要删的条目编号"},
                    },
                    "required": ["entry_ids"],
                },
            },
            profile_bulk_delete,
        ),
        (
            {
                "name": "focus_edit",
                "description": "手动调一个服务群的关注成员：add 一直关注 / remove 取消 / auto 改回自动挑。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "group_id": {"type": "string", "description": "群号（可选）"},
                        "user_id": {"type": "string", "description": "群友 QQ 号"},
                        "action": {"type": "string", "enum": ["add", "remove", "auto"]},
                    },
                    "required": ["user_id", "action"],
                },
            },
            focus_edit,
        ),
        (
            {
                "name": "set_feeds_pref",
                "description": "写一个服务群的资讯偏好（一句话：这个群想看什么、不想看什么）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "group_id": {"type": "string", "description": "群号（可选）"},
                        "text": {"type": "string", "description": "一句话偏好；空 = 清掉"},
                    },
                    "required": ["text"],
                },
            },
            set_feeds_pref,
        ),
        (
            {
                "name": "rss_add",
                "description": "给一个服务群加一个 RSS 源。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "group_id": {"type": "string", "description": "群号（可选）"},
                        "url": {"type": "string", "description": "RSS / Atom 地址"},
                        "title": {"type": "string", "description": "标题（可选）"},
                    },
                    "required": ["url"],
                },
            },
            rss_add,
        ),
        (
            {
                "name": "rss_remove",
                "description": "从一个服务群删掉一个 RSS 源（要管理员确认）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "group_id": {"type": "string", "description": "群号（可选）"},
                        "feed_id": {"type": "string", "description": "RSS 源编号"},
                    },
                    "required": ["feed_id"],
                },
            },
            rss_remove,
        ),
        (
            {
                "name": "block_domain",
                "description": "屏蔽 / 取消屏蔽一个来源域名（全服务群生效）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string", "description": "域名，如 example.com"},
                        "blocked": {"type": "boolean", "description": "true 屏蔽 / false 取消屏蔽"},
                    },
                    "required": ["domain"],
                },
            },
            block_domain,
        ),
        (
            {
                "name": "set_rules",
                "description": '改规则（推送 / 开话题 / 派活批准 / 资讯），形如 {"delivery": {"push_per_day": 5}}。放宽安全设置时要管理员确认。',
                "parameters": {
                    "type": "object",
                    "properties": {
                        "patch": {"type": "object", "description": "{节: {字段: 值}}，节只能是 delivery / topics / approval / feeds"},
                    },
                    "required": ["patch"],
                },
            },
            set_rules,
        ),
        (
            {
                "name": "identity_edit",
                "description": "改身份与工作记忆：kind 取 soul / agents / memory（全局）或 group（某个群的工作记忆）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["soul", "agents", "memory", "group"]},
                        "text": {"type": "string", "description": "新的全文"},
                        "group_id": {"type": "string", "description": "kind=group 时的群号"},
                    },
                    "required": ["kind", "text"],
                },
            },
            identity_edit,
        ),
        (
            {
                "name": "remember",
                "description": "让 MaiWork 记住一件事（写进工作记忆，去重与隐私规则照旧生效）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "要记住的事"},
                        "group_id": {"type": "string", "description": "记在某个服务群名下（可选；不填记全局）"},
                    },
                    "required": ["text"],
                },
            },
            remember,
        ),
        (
            {
                "name": "send_group_message",
                "description": "让 MaiWork 在一个服务群里说一句话（要管理员确认后才会真发）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "group_id": {"type": "string", "description": "群号（可选）"},
                        "text": {"type": "string", "description": "要说的话"},
                    },
                    "required": ["text"],
                },
            },
            send_group_message,
        ),
        (
            {
                "name": "group_notice_send",
                "description": "发群公告（要管理员确认；机器人得是群主或管理员）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "group_id": {"type": "string", "description": "群号（可选）"},
                        "content": {"type": "string", "description": "公告内容"},
                    },
                    "required": ["content"],
                },
            },
            group_notice_send,
        ),
        (
            {
                "name": "group_file_manage",
                "description": (
                    "管群文件：delete / rename / move / mkdir / rmdir（要管理员确认；"
                    "文件和文件夹都只能动机器人自己传 / 自己建的，删文件夹时里面只能有自己的东西）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "group_id": {"type": "string", "description": "群号（可选）"},
                        "action": {"type": "string", "enum": ["delete", "rename", "move", "mkdir", "rmdir"]},
                        "file_id": {"type": "string", "description": "文件编号"},
                        "name": {"type": "string", "description": "新名字 / 文件夹名"},
                        "folder_id": {"type": "string", "description": "目标文件夹 / 要删的文件夹"},
                    },
                    "required": ["action"],
                },
            },
            group_file_manage,
        ),
        (
            {
                "name": "group_album_upload",
                "description": "把工作区里的一张成品图传进群相册（要管理员确认）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "group_id": {"type": "string", "description": "群号（可选）"},
                        "path": {"type": "string", "description": "工作区内图片的绝对路径"},
                        "album_id": {"type": "string", "description": "相册编号"},
                    },
                    "required": ["path"],
                },
            },
            group_album_upload,
        ),
        (
            {
                "name": "skill_delete",
                "description": "删掉一个 skill（要管理员确认）。",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "description": "skill 名字"}},
                    "required": ["name"],
                },
            },
            skill_delete,
        ),
        (
            {
                "name": "mcp_toggle",
                "description": "开 / 关一个 MCP 扩展。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "扩展名字"},
                        "enabled": {"type": "boolean", "description": "true 开 / false 关"},
                    },
                    "required": ["name"],
                },
            },
            mcp_toggle,
        ),
        (
            {
                "name": "mcp_delete",
                "description": "删掉一个网页加的 MCP 扩展（要管理员确认）。",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "description": "扩展名字"}},
                    "required": ["name"],
                },
            },
            mcp_delete,
        ),
        (
            {
                "name": "run_news_now",
                "description": "让一个服务群现在备一批资讯（后台跑，不占今天的定时时段）。",
                "parameters": {
                    "type": "object",
                    "properties": {"group_id": {"type": "string", "description": "群号（可选）"}},
                },
            },
            run_news_now,
        ),
        (
            {
                "name": "make_idea_now",
                "description": "让一个服务群现在想一个构想（后台跑）。",
                "parameters": {
                    "type": "object",
                    "properties": {"group_id": {"type": "string", "description": "群号（可选）"}},
                },
            },
            make_idea_now,
        ),
        (
            {
                "name": "refresh_profile",
                "description": "现在重新整理一个服务群的画像（后台跑；同群同时只整理一次）。不往群里发任何东西，不用确认。",
                "parameters": {
                    "type": "object",
                    "properties": {"group_id": {"type": "string", "description": "群号（可选）"}},
                },
            },
            refresh_profile,
        ),
        (
            {
                "name": "create_task",
                "description": (
                    "给一个服务群建一个任务并立刻开工（要管理员确认）。"
                    "只用于需要子 agent 产出东西的活（写文档、做网页、跑脚本等）；"
                    "MaiWork 自己的维护不要派任务：刷新画像用 refresh_profile、备资讯用 run_news_now、"
                    "想构想用 make_idea_now。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "group_id": {"type": "string", "description": "群号（可选）"},
                        "title": {"type": "string", "description": "任务名字"},
                        "request": {"type": "string", "description": "要干什么，说清楚"},
                        "criteria": {"type": "array", "items": {"type": "string"}, "description": "完成标准（可选）"},
                    },
                    "required": ["title", "request"],
                },
            },
            create_task,
        ),
        (
            {
                "name": "create_goal",
                "description": "给一个服务群立一个后台目标（按完成标准一直盯；有进展 / 完成时会在群里说一句，所以要管理员确认）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "group_id": {"type": "string", "description": "群号（可选）"},
                        "title": {"type": "string", "description": "目标名字"},
                        "body": {"type": "string", "description": "目标说明（可选）"},
                        "criteria": {"type": "array", "items": {"type": "string"}, "description": "怎么算做到（至少一条）"},
                        "check_hours": {"type": "integer", "description": "每几小时检查一次（可选）"},
                    },
                    "required": ["title", "criteria"],
                },
            },
            create_goal,
        ),
        (
            {
                "name": "approve_request",
                "description": "批准一个群友派的活（批准后自动落成任务并开工）。",
                "parameters": {
                    "type": "object",
                    "properties": {"request_id": {"type": "string", "description": "请求编号"}},
                    "required": ["request_id"],
                },
            },
            approve_request,
        ),
        (
            {
                "name": "reject_request",
                "description": "拒绝一个群友派的活。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "request_id": {"type": "string", "description": "请求编号"},
                        "reason": {"type": "string", "description": "为什么拒（可选）"},
                    },
                    "required": ["request_id"],
                },
            },
            reject_request,
        ),
        (
            {
                "name": "cancel_task",
                "description": "取消一个任务。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "任务编号"},
                        "reason": {"type": "string", "description": "为什么取消（可选）"},
                    },
                    "required": ["task_id"],
                },
            },
            cancel_task,
        ),
    ]

    for spec, handler in _SPECS:
        _register(spec, handler)

    logger.info("管理员对话工具已注册：%d 个（其中 %d 个要确认）", len(_SPECS), len(CONFIRM_TOOLS))
    return gate
