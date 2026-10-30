"""tools_admin.py：管理员对话专用的工具层（docs/02-设计.md「管理员对话」）。

一句话说清它干什么：管理员在网页上和 MaiWork 的主模型对话时，主模型能调的那批工具
在这里注册；能直接读的读，能直接改的改，**会对外发东西 / 会删东西 / 会改安全相关设置
（含放宽）的**一律不当时动手——先写一张「待确认」小票（admin_chat_pending 表），等管理
员点「同意」。

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
- 没有任何工具能碰密钥 / MaiBot 宿主（重启、改宿主配置、装插件都不存在）；端点 / 模型库 / MCP / 专岗
  能改（tools_admin_settings.py，和网页同一套校验），但参数里不许有密钥，读回来也只说填没填。
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

from . import clock, members, rules, settings_ops
from .tools import Tool, ToolContext, ToolResult, Tools

logger = logging.getLogger("maiwork.tools_admin")

# 管理员工具起的后台任务：留一份强引用（asyncio 对任务只有弱引用，不留可能跑一半被 GC），
# 跑完自己摘掉
_BG_TASKS: set[asyncio.Task] = set()


def _spawn_bg(coro: Awaitable[Any]) -> None:
    """起后台任务并登记；没有运行中的事件循环时抛 RuntimeError（协程已关掉）。"""
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        raise
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    task.add_done_callback(_log_bg_exception)


def _log_bg_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("管理员工具的后台任务出错", exc_info=exc)

# 只有 bot 管理员能用（tools.ToolContext.role 的角色名）
ROLE = "admin"
_ROLES = frozenset({ROLE})

# 要管理员确认才动手的工具名：group_deliver 的每个 action（发消息 / 公告 / 相册 /
# 管群文件）都先写小票。
CONFIRM_TOOLS = frozenset({"group_deliver"})

# 看情形才要确认的工具：工具自己判断「这个 action 这次是放宽 / 要删 / 要装 / 会对外说」
# 才调 _need_confirm，其余 action 直接做：
# - mcp_manage：add / delete 一律；update 改地址 / 加 main / 白名单放宽
# - agent_manage：delete 一律；update 挂声明执行类工具的做法
# - model_manage：两个 delete 一律；endpoint_save 新建 / 改地址 / 改协议
# - skill_manage：install / delete 一律；write 声明执行类工具
# - rules_manage：set 白名单外字段 / 放宽安全设置
# - profile_manage：bulk_delete
# - source_manage：rss_remove
# - task_manage：create（开工后 coordinator 会往群里发消息）
# - goal_manage：create / pause / resume / cancel（后台检查会往群里发汇报）
# - request_manage：approve（群友派的活要 bot 管理员批准）
# - social_toggle：开平台才确认
# - tool_run：执行类 / 只在派活回合用的工具才写小票（按 Tools.traits 特性位）
_CONDITIONAL_CONFIRM = frozenset({"mcp_manage", "agent_manage", "model_manage",
                                  "skill_manage", "rules_manage",
                                  "profile_manage", "source_manage",
                                  "task_manage", "goal_manage", "request_manage",
                                  "social_toggle",
                                  "tool_run"})

# 确认话术（固定，测试断言这一句；对话循环也靠它识别「等确认」）
NEED_CONFIRM = "已请求管理员确认，同意后我再动手"

# set_rules 的确认口径（2026-10-12 改成白名单）：**只有**下面这些「无安全影响的调参项」
# 才允许不经管理员确认直接写 config.toml 并热生效——都是资讯 / 画像 / 快速判断的节奏与
# 数量。其余任何字段（包括将来新增的、schema 里还没有的）一律先写小票等管理员点头。
# 为什么：管理员在网页和主模型对话时，主模型读到的群消息 / 网页正文 / 工具输出里可能藏
# 注入指令，不能让它替管理员改运行账号（换成 root 就是提权）、服务地址（密钥随请求发
# 出去）、密钥文件路径、SSH 机器、控制台监听地址这些安全边界。
_SET_RULES_NO_CONFIRM: frozenset[str] = frozenset(
    {
        "feeds.news_slots",
        "feeds.news_jitter_minutes",
        "feeds.max_items",
        "feeds.lookback_days",
        "feeds.web_min_avg",
        "feeds.pool_min_avg",
        "feeds.guides",
        "feeds.collect_minutes",
        "feeds.viz_per_day",
        "topics.candidate_ttl_hours",
        "profile.batch_messages",
        "profile.max_interval_hours",
        "profile.backfill_days",
        "profile.backfill_max_messages",
        "profile.weekly_day",
        "profile.read_interval_minutes",
        "jev.timeout_ms",
        "quick_judge.keyword_filter",
    }
)

# 不在白名单、但沿用 _loosens_security 细粒度口径的键：只有「放宽」才要确认
# （关掉自动审核、调低每天自动批上限、把开话题最小间隔调大都不用确认）。这几项 0.8.0
# 就是这么定的，既有行为保持不变，所以不并进白名单。
_SET_RULES_FINE_GRAINED: frozenset[str] = frozenset(
    {
        "approval.remind",
        "approval.auto_review",
        "approval.auto_review_daily",
        "topics.min_gap_hours",
    }
)

_REFUSED = "管理员没有同意，这次动作已取消"
_NOT_IN_SERVICE = "这不是配置里的服务群，MaiWork 不读也不动它"
_ADMIN_ONLY = "只有 bot 管理员能用，当前角色不允许"
_NO_CHAT = "管理员对话还没绑定，这个动作先记不下小票：请在网页对话里重发一次"
_NO_CHAT_READ = "这段对话没绑定，读不了原文：请让管理员在网页的对话页面里叫我读（只读当前对话，不猜别的）"

MAX_SUMMARY = 200

# 「当前是哪段对话 / 哪条消息」按 asyncio Task 各存一份：两个管理员对话并发跑时，
# B 的 bind_chat 不会覆盖 A 的，小票不会串到别的对话里。(0, 0) = 没绑定。
_CHAT_CTX: ContextVar[tuple[int, int]] = ContextVar("mw_admin_chat_ctx", default=(0, 0))
# 「管理员已点同意」只由 PendingGate.execute 在执行那一下置上，而且值是这次动作的
# 指纹 (工具名, 规范化参数的 sha256)：execute 期间派出的子任务会继承这个 ContextVar，
# 但它再调确认类工具时参数对不上指纹就照样要写小票，不能把「已批准」当通行证用。
# 模型传来的参数一律不算数（参数里写什么都不影响这里的值）。
_APPROVED_CTX: ContextVar[tuple[str, str] | None] = ContextVar("mw_admin_approved", default=None)


# ----------------------------------------------------------------------
# 从网址装 skill（skill_install，2026-10-15）：只认公网 https，下载在**管理员点头之后**
# ----------------------------------------------------------------------

SKILL_FETCH_MAX = 5 * 1024 * 1024
_GH_RE = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+)/(?:blob|tree)/([^/]+)/(.+?)/?$")


def skill_source_url(url: str) -> tuple[str, str]:
    """把管理员给的网址换成真正要下载的地址：(下载地址, "zip" | "md")。不认识的抛 ValueError（中文）。

    - 直接的 .zip 链接 → zip；
    - GitHub 上某个 skill 目录 / SKILL.md 页面 → raw.githubusercontent.com 的 SKILL.md（只装这一份文件；
      带脚本 / 附件的 skill 请管理员下载 zip 后在网页「设置 → 工具」上传）；
    - 直接的 …/SKILL.md 原文链接 → md。"""
    u = str(url or "").strip()
    if not u.lower().startswith("https://") or any(c in u for c in " \n\r\t\"'<>"):
        raise ValueError("只认 https 开头的网址")
    m = _GH_RE.match(u)
    if m:
        owner, repo, branch, path = m.groups()
        if not path.endswith("SKILL.md"):
            path = path.rstrip("/") + "/SKILL.md"
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}", "md"
    low = u.lower().split("?", 1)[0]
    if low.endswith(".zip"):
        return u, "zip"
    if low.endswith("/skill.md"):
        return u, "md"
    raise ValueError(
        "无法从这个网址识别 skill。请提供 GitHub 上 skill 目录或 SKILL.md 的链接，"
        "或一个 .zip 下载链接；skills.sh 的页面请先打开它对应的 GitHub 仓库再复制链接。"
    )


async def fetch_public(url: str, max_bytes: int = SKILL_FETCH_MAX) -> bytes:
    """下载一个公网 https 地址（防 SSRF：每一跳都查不是内网 / 本机；最多跟 3 次跳转；有大小上限）。"""
    from urllib.parse import urljoin, urlsplit

    import aiohttp

    from .tools_builtin import _default_resolver, _host_is_forbidden

    cur = url
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for _ in range(4):
            parts = urlsplit(cur)
            if parts.scheme != "https" or not parts.hostname:
                raise ValueError("只认 https 网址")
            if await asyncio.to_thread(_host_is_forbidden, parts.hostname, _default_resolver):
                raise ValueError("这个地址指向内网或本机，不下载")
            async with session.get(cur, allow_redirects=False) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    cur = urljoin(cur, resp.headers.get("Location", ""))
                    continue
                if resp.status != 200:
                    raise ValueError(f"下载失败（HTTP {resp.status}）")
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        raise ValueError(f"文件太大（超过 {max_bytes // 1024 // 1024}MB）")
                return bytes(buf)
    raise ValueError("跳转太多次，不下载")


def settings_link(settings: Any, sub: str) -> str:
    """网页设置页链接（public_url + /#/settings/<页>）；没公开网址 → ""。实现在 tools_admin_settings。"""
    from .tools_admin_settings import settings_link as _link

    return _link(settings, sub)


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


def _funnel_line(funnel: Any) -> str:
    """一批资讯的漏斗要点行（list_news 管理员视图用）；没 funnel → ""。

    只挑管理员看得懂的几项：搜了几次、候选多少（按来源计数，补找带回来的标
    agent:<工具名>）、打开多少、留下多少；补找交回 / 留下 / 丢弃 / 出错。
    哪段没有就跳过哪段，绝不把整坨 stats JSON 倒给管理员。
    """
    if not isinstance(funnel, dict) or not funnel:
        return ""
    parts: list[str] = []
    if funnel.get("queries") is not None:
        parts.append(f"搜 {int(funnel.get('queries') or 0)} 次")
    if funnel.get("discovered") is not None:
        prov = ""
        providers = funnel.get("providers")
        if isinstance(providers, dict) and providers:
            prov = "（" + "、".join(
                f"{k} {int(v or 0)}"
                for k, v in sorted(
                    providers.items(), key=lambda kv: (-int(kv[1] or 0), str(kv[0]))
                )
            ) + "）"
        parts.append(f"候选 {int(funnel.get('discovered') or 0)}{prov}")
    if funnel.get("opened") is not None:
        parts.append(f"打开 {int(funnel.get('opened') or 0)}")
    if funnel.get("kept") is not None:
        parts.append(f"留下 {int(funnel.get('kept') or 0)}")
    seg = "，".join(parts)
    supp = funnel.get("supplement")
    if isinstance(supp, dict) and supp:
        if supp.get("ran"):
            supp_seg = (
                f"补找：交回 {int(supp.get('returned') or 0)}"
                f" / 留下 {int(supp.get('kept') or 0)}"
                f" / 丢弃 {int(supp.get('not_in_output') or 0)}（未在工具输出出现过）"
            )
            if supp.get("error"):
                supp_seg += f"，出错 {supp['error']}"
        elif supp.get("error"):
            supp_seg = f"补找没跑成：{supp['error']}"
        else:
            supp_seg = "补找没跑"
        seg = (seg + "；" + supp_seg) if seg else supp_seg
    return ("  漏斗：" + seg) if seg else ""


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
        """有效设置（config.toml 热更新后的值）；读不到就用服务包的原始设置兜底。"""
        getter = getattr(svc, "get_settings", None)
        base = None
        if callable(getter):
            try:
                base = getter()
            except Exception:
                logger.exception("管理员工具读设置失败")
        if base is None:
            return getattr(svc, "_settings", None)
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

    def _feature_deny(gid: str, module: str) -> ToolResult | None:
        """这个群的这个模块关了 → 中文错误；开着 / 查不到 → None（不挡）。

        任务关闭连带停目标（用户已确认），所以 create_goal 也走 task 开关。
        """
        try:
            from . import group_features

            store = getattr(svc, "store", None)
            if store is None:
                return None
            settings = _settings()
            if not group_features.enabled(store, gid, module, settings):
                zh = {"news": "资讯", "idea": "构想", "task": "任务"}.get(module, module)
                return _bad(f"本群的「{zh}」功能已关闭（群管理页可重新开启）")
        except Exception:
            logger.debug("管理员工具查功能开关失败（群 %s %s）", gid, module, exc_info=True)
        return None

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
        store = getattr(svc, "store", None)
        try:
            from .skills import Skills

            # 接 store/settings 后，停用的 skill 在管理员工具里同样 read/list 不到
            return Skills(data_dir, store=store, settings=_settings)
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
        if tool in CONFIRM_TOOLS or tool in _CONDITIONAL_CONFIRM:
            if _APPROVED_CTX.get() == _fingerprint(tool, args):
                return None
            return gate.queue(tool, _norm_args(args), summary)
        return None

    # 群的「安静/睡觉时段」0.8.0 起每个群自己一份（group_push，群页面管）；
    # 全局推送上限 / 睡觉时段不再从这里改，_quiet_minutes 也随之删掉。

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
        # 画像条目里提到群友写成 {@平台id}：给人看的文字（含 data）都换成当前名字
        entries = [dict(e, text=members.render(svc.store, gid, e.get("text", ""))) for e in entries]
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
            # 每批先给一行漏斗要点：搜了几次、候选多少（含补找的 agent:<工具名> 来源）、
            # 打开 / 留下多少、补找交回 / 留下 / 丢弃。老批次没有 stats.funnel 就不加。
            stats = b.get("stats")
            fline = _funnel_line(
                (stats or {}).get("funnel") if isinstance(stats, dict) else None)
            if fline:
                lines.append(fline)
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
            "SELECT id, group_id, kind, title, quote, requester_id, requester_name, status,"
            " task_id, goal_id, created"
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
            f"（{members.name_of(svc.store, r['group_id'], r['requester_id'], fallback=r['requester_name']) or '不认识的群友'}"
            f"，群 {r['group_id']}）"
            for r in data
        ]
        return _ok(f"「{status}」请求 {len(data)} 条：\n" + "\n".join(lines), data=data)

    async def read_admin_history(ctx: ToolContext, args: dict) -> ToolResult:
        """读**当前这段**管理员对话的原文（含被压缩截断的工具输出全文），按 id / 字符窗口分页。

        参数里没有 chat_id，也绝不看模型传的 chat_id：归属只认 ctx.chat_id
        （admin_chat._run_call 每轮挂上的），所以模型拿不到「读别的对话」的口子；
        没绑定对话就直接拒绝，不猜一段来读。
        """
        from . import admin_chat as _admin_chat

        chat = getattr(svc, "admin_chat", None)
        if chat is None:
            return _bad("管理员对话层还没就位，读不了对话原文")
        try:
            cid = int(getattr(ctx, "chat_id", 0) or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid <= 0:
            return _bad(_NO_CHAT_READ)
        after = _int(args.get("after"), 0, 0, 2**31)
        limit = _int(args.get("limit"), _admin_chat.READ_HISTORY_DEFAULT_LIMIT, 1, _admin_chat.READ_HISTORY_MAX_LIMIT)
        offset = _int(args.get("offset"), 0, 0, 2**31)
        chars = _int(args.get("chars"), _admin_chat.READ_HISTORY_DEFAULT_CHARS, 1, _admin_chat.READ_HISTORY_MAX_CHARS)
        try:
            page = chat.read_history(cid, after=after, limit=limit, offset=offset, chars=chars)
        except ValueError as e:
            return _bad(str(e))
        except Exception as e:
            logger.exception("读管理员对话原文失败（对话 %s）", cid)
            return _bad(f"读对话原文失败：{e}")
        msgs = list(page.get("messages") or [])
        if not msgs:
            return _ok(f"这段对话里没有 id > {after} 的消息", data=page)
        lines: list[str] = []
        for m in msgs:
            try:
                mid = int(m.get("id") or 0)
            except (TypeError, ValueError):
                mid = 0
            head = f"#{mid} [{m.get('role')}" + (f" {m.get('name')}" if m.get("name") else "") + "]"
            body = str(m.get("content") or "")
            if m.get("has_more_content"):
                body += (
                    f"\n……（这条还有，用 after={max(mid - 1, 0)} offset={m.get('next_offset')} "
                    f"chars={chars} 接着读同一条）"
                )
            lines.append(head + "\n" + body)
        if page.get("has_more"):
            nxt = page.get("next_after")
            nxt_off = int(page.get("next_offset") or 0)
            hint = f"after={nxt} offset={nxt_off}" if nxt_off else f"after={nxt}"
            tail = f"（共 {len(msgs)} 条；接着往下读用 {hint}）"
        else:
            tail = f"（共 {len(msgs)} 条，后面没有了）"
        return _ok("\n\n".join(lines) + "\n" + tail, data=page)

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

    async def _read_model_logs(ctx: ToolContext, args: dict) -> ToolResult:
        sql = "SELECT ts, purpose, role, model, group_id, ok, status, ms, error FROM model_calls"
        where: list[str] = []
        params: list[Any] = []
        if bool(args.get("failed_only")):
            where.append("ok=0")
        gid_raw = str(args.get("group_id") or ctx.group_id or "").strip()
        if gid_raw and gid_raw != "*":
            gid, err = _served(gid_raw)
            if err is not None:
                return err
            where.append("group_id=?")
            params.append(gid)
        else:
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

    async def _read_tool_logs(ctx: ToolContext, args: dict) -> ToolResult:
        """读 tool_calls 表：子 agent / 主模型 / 管理员工具的真实调用记录。

        管理员问「这批资讯到底有没有用上 flovia_x」就靠它——工具名、入参、
        返回 / 报错、耗时都落过库。服务群口径同模型调用：只看服务群和全局
        （没群号）的行，非服务群零读取；服务群名单读不到时只看全局，宁可少看。
        """
        sql = (
            "SELECT ts, group_id, task_id, actor, tool, input, output, ms, ok, error"
            " FROM tool_calls"
        )
        where: list[str] = []
        params: list[Any] = []
        gid_raw = str(args.get("group_id") or ctx.group_id or "").strip()
        if gid_raw and gid_raw != "*":
            gid, err = _served(gid_raw)
            if err is not None:
                return err
            where.append("group_id=?")
            params.append(gid)
        else:
            # 没指定群（含 "*"）：服务群的 + 全局的一起看（read_logs 同口径）
            served = sorted(_served_groups(_settings()))
            if served:
                where.append(f"(group_id IN ({','.join('?' * len(served))}) OR group_id='')")
                params.extend(served)
            else:
                where.append("group_id=''")
        task_id = str(args.get("task_id") or "").strip()
        if task_id:  # 前缀匹配：feeds-verify 能捞出本轮核验 / 补找的全部调用
            where.append("substr(task_id, 1, ?) = ?")
            params.extend([len(task_id), task_id])
        tool_q = str(args.get("tool") or "").strip()
        if tool_q:  # 包含匹配：flovia 能捞出 mcp_flovia_flovia_x
            where.append("instr(tool, ?) > 0")
            params.append(tool_q)
        if bool(args.get("failed_only")):
            where.append("ok=0")
        sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(_int(args.get("limit"), 30, 1, 200))
        try:
            rows = svc.store.read().execute(sql, params).fetchall()
        except Exception as e:
            return _bad(f"读工具调用记录失败：{e}")
        data = [dict(r) for r in rows]
        if not data:
            return _ok("没有符合条件的工具调用记录", data=[])
        lines = []
        for r in data:
            t = clock.bj(float(r["ts"] or 0)).strftime("%m-%d %H:%M")
            gid = str(r["group_id"] or "")
            state = "成功" if int(r["ok"] or 0) else "失败"
            lines.append(
                f"[{t}] {r['actor']}" + (f" 群{gid}" if gid else "")
                + f" {r['tool']} {state} {int(r['ms'] or 0)}ms"
            )
            inp = _tail(str(r["input"] or ""), 300)
            if inp:
                lines.append(f"  入参: {inp}")
            outp = _tail(str(r["output"] or ""), 500)
            if outp:
                lines.append(f"  输出: {outp}")
            err_t = _tail(str(r["error"] or ""), 300)
            if err_t:
                lines.append(f"  错误: {err_t}")
        return _ok(f"最近 {len(data)} 条工具调用：\n" + "\n".join(lines), data=data)

    async def read_logs(ctx: ToolContext, args: dict) -> ToolResult:
        """统一读日志入口：模型调用 + 工具调用。

        - kind="models"（默认）：模型调用日志；kind="tools"：tool_calls 真实工具调用；
        - kind 没给但传了 tool 或 task_id → 自动按 "tools" 查；
        - kind="all"：两段都回，data={"model_calls": [...], "tool_calls": [...]}。
        """
        kind = str(args.get("kind") or "").strip().lower()
        if not kind:
            kind = "tools" if (args.get("tool") or args.get("task_id")) else "models"
        if kind not in ("models", "tools", "all"):
            return _bad("kind 只能是 models / tools / all")
        if kind == "models":
            return await _read_model_logs(ctx, args)
        if kind == "tools":
            return await _read_tool_logs(ctx, args)
        m_res = await _read_model_logs(ctx, args)
        if not m_res.ok:
            return m_res
        t_res = await _read_tool_logs(ctx, args)
        if not t_res.ok:
            return t_res
        return _ok(
            m_res.output + "\n\n" + t_res.output,
            data={"model_calls": m_res.data, "tool_calls": t_res.data},
        )

    async def tool_run(ctx: ToolContext, args: dict) -> ToolResult:
        """亲手试调一个注册给子 agent（worker）的工具一次。

        - 还是走 Tools.call 唯一入口（as_role="worker" 强制按 worker 那格查名），
          照常落 tool_calls：actor 记这段管理员对话、task_id 记 admin-tool:<对话号>；
        - 执行类 / 只在派活回合用的工具先出小票（看 Tools.traits 特性位，不是名单）；
        - needs_workspace → svc.env.ephemeral("admin-tool") 的单独临时工作区，
          出来就删；环境开不出就中文报错，绝不偷摸不带工作区跑；
        - 回显真实输出 / 真实错误（截 ~4000 字），失败不装成功。
        """
        name = str(args.get("name") or "").strip()
        if not name:
            return _bad("要给 name（已注册的工具名）")
        tools_obj = getattr(svc, "tools", None)
        if tools_obj is None:
            return _bad("工具层没就位，这次调不了")
        tr: dict | None = None
        traits_fn = getattr(tools_obj, "traits", None)
        if callable(traits_fn):
            try:
                tr = traits_fn(name)
            except Exception:
                tr = None
        if tr is None:
            getter = getattr(tools_obj, "get", None)
            if callable(getter) and getter(name, ROLE) is not None:
                return _bad(f"「{name}」是管理员工具，管理员对话里直接就能用，不用试调")
            return _bad(
                f"没有注册叫「{name}」的工具。能试调的是注册给子 agent 的工具："
                "名单看 agent_manage 的 detail（「有效工具 / 做法带来的工具」段）或网页专岗页"
            )
        roles = tr.get("roles") or []
        if "worker" not in roles:
            return _bad(
                f"工具「{name}」没注册给子 agent（角色：{'、'.join(str(r) for r in roles) or '无'}），"
                "不能在这里试调"
            )
        run_args = args.get("args")
        if run_args is None:
            run_args = {}
        if not isinstance(run_args, dict):
            return _bad("args 要是一个 JSON 对象（不给就按空参数调）")
        run_args = _norm_args(run_args)
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        # 记是哪段管理员对话试的：task_id = admin-tool:<对话号>。
        # ctx.chat_id 是 admin_chat 每轮动态挂的；小票执行路径从票里的存根拿回来。
        raw_cid = getattr(ctx, "chat_id", None)
        if raw_cid is None:
            raw_cid = args.get("chat_id")
        try:
            cid = int(raw_cid or 0)
        except (TypeError, ValueError):
            cid = 0
        ticket_args = {"name": name, "args": run_args, "group_id": gid, "chat_id": cid}
        if tr.get("exec_class") or tr.get("task_only"):
            digest = _tail(
                json.dumps(Tools._mask_sensitive(run_args),
                           ensure_ascii=False, sort_keys=True, default=str),
                120,
            )
            kind = "执行类" if tr.get("exec_class") else "只在派活回合用的"
            pending = _need_confirm(
                "tool_run", ticket_args, f"试调{kind}工具 {name}（参数 {digest}）")
            if pending is not None:
                return pending
        # 要工作区的给单独临时工作区（LocalEnv.ephemeral 同款：用到才建、出来就删）。
        # 管理员点头之后才开——没确认前不碰资源。
        ws_ctx: Any = None
        ws_path: Any = None
        if tr.get("needs_workspace"):
            env = getattr(svc, "env", None)
            eph = getattr(env, "ephemeral", None) if env is not None else None
            if not callable(eph):
                return _bad(
                    f"工具「{name}」要在单独工作区里跑，但执行环境开不出临时工作区")
            try:
                ws_ctx = eph("admin-tool", deadline_ts=clock.now() + 120)
                ws_path = await ws_ctx.__aenter__()
            except Exception as e:
                logger.exception("tool_run 开临时工作区失败（%s）", name)
                return _bad(f"开临时工作区失败，这次没调成：{e}")
        try:
            run_ctx = ToolContext(
                group_id=gid,
                task_id=f"admin-tool:{cid}",
                actor=str(getattr(ctx, "actor", "") or "主模型（管理员对话）"),
                workspace=ws_path,
                role="worker",
            )
            result = await tools_obj.call(name, run_args, run_ctx, as_role="worker")
        finally:
            if ws_ctx is not None:
                try:
                    await ws_ctx.__aexit__(None, None, None)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("tool_run 临时工作区收尾失败（%s）", name)
        if not isinstance(result, ToolResult):
            result = ToolResult(ok=bool(result), output=str(result))
        if result.ok:
            return _ok(
                f"试调 {name} 成功：\n{_tail(result.output, 4000)}",
                data={"ok": True, "tool": name, "output": result.output},
            )
        body = result.error or result.output
        return _bad(f"试调 {name} 没跑成：{_tail(body, 4000)}")

    def _flat_rules(settings: Any) -> list[str]:
        """全局还能改的规则（节.字段=值）。

        0.8.0 起「谁能批本群的活 / 免批」「冷场开话题 / 往群里发多少 / 几点不打扰」是
        每个群自己一份（kv["group_approval.<群号>"] / kv["group_push.<群号>"]），这里
        不再列全局那几行——列了会让人以为还能从全局改；去群页面看/改。
        """
        out: list[str] = []
        for section in ("topics", "approval", "feeds"):
            obj = getattr(settings, section, None)
            if obj is None:
                continue
            for field in (
                "min_gap_hours", "candidate_ttl_hours",
                "remind", "auto_review", "auto_review_daily",
                "news_slots", "max_items", "web_min_avg", "pool_min_avg", "guides",
            ):
                if hasattr(obj, field):
                    out.append(f"{section}.{field}={getattr(obj, field)}")
        return out

    async def get_rules(ctx: ToolContext, args: dict) -> ToolResult:
        settings = _settings()
        if settings is None:
            return _bad("读不到规则：设置没就位")
        lines = _flat_rules(settings)
        tail = ("每群的「派活审批」和「主动发言（开话题 / 资讯卡 / 每日上限 / 睡觉时段）」"
                "在每个群自己的页面里管（这里不再从全局改；config.toml 里那几行只作新群第一次的种子）。")
        return _ok("现在的规则：\n" + "\n".join(lines) + "\n" + tail,
                   data={"flat": lines, "group_page_note": tail})

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
        hint = (
            "要加 MCP、测连接、重连、改它给谁用和工具白名单，可以直接在对话里说（新加和放宽会先等管理员确认）；"
            "要密钥（请求头）的请管理员自己到网页「设置 → 工具」填。"
        )
        if not lines:
            return _ok("现在一个扩展也没有。" + hint, data=data)
        return _ok("\n".join(lines) + "\n" + hint, data=data)

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
        entry = dict(row)
        # 返回给人的文字里 {@平台id} 换成当前名字（调用方可能把它回显出去）；库里不动原文
        entry["text"] = members.render(svc.store, gid, entry.get("text", ""))
        return entry

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
        pending = _need_confirm("profile_manage", args, f"一次删掉群 {gid} 的 {len(raw_ids)} 条画像")
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
        pending = _need_confirm("source_manage", args, f"从群 {gid} 删掉 RSS 源「{target.get('title') or feed_id}」")
        if pending is not None:
            return pending
        try:
            _rss.remove_feed(svc.store, gid, feed_id)
        except Exception as e:
            return _bad(f"删 RSS 源失败：{e}")
        return _ok(f"已从群 {gid} 删掉 RSS 源 {feed_id}")

    async def block_domain(ctx: ToolContext, args: dict) -> ToolResult:
        from . import feeds as _feeds

        raw = str(args.get("domain") or "").strip().lower().strip(".")
        domain = raw[4:] if raw.startswith("www.") else raw
        if not _DOMAIN_RE.match(domain):
            return _bad(f"「{raw}」不像一个域名（要像 example.com）")
        blocked = bool(args.get("blocked", True))
        gid, err = _served(args.get("group_id") or ctx.group_id)
        if err is not None:
            return err
        current = set(_feeds.blocked_domains(svc.store, gid))
        if blocked:
            current.add(domain)
        else:
            current.discard(domain)
        try:
            names = _feeds.blocked_domains_set(svc.store, gid, sorted(current))
        except Exception as e:
            return _bad(f"写屏蔽名单失败：{e}")
        return _ok(
            f"已对群 {gid}{'屏蔽' if blocked else '取消屏蔽'} {domain}；这个群的屏蔽名单共 {len(names)} 个域名",
            data={"group_id": gid, "blocked_domains": names},
        )

    def _validated_patch(patch: Any) -> dict[str, dict[str, Any]] | ToolResult:
        """先把 patch 逐字段验一遍（返回 {节: {字段: 规范值}} 或错误结果）。

        先验再问：值本身就不合法（比如推送上限 99）时直接报错，不该记一张
        「待确认」小票让管理员去点一个根本执行不了的动作。校验跟网页「全部配置」
        同一套（rules.CONFIG_SCHEMA），不是旧白名单了。
        0.8.0：归每个群自己管的键（approval.required/admins/exempt_*、topics.enabled/
        speaker/per_day、delivery.push_per_day/quiet_hours）直接拒，并说清「到群页管理」——
        绝不悄悄写一份没人听的全局值。
        """
        if not isinstance(patch, dict) or not patch:
            return _bad('要给 patch，形如 {"feeds": {"max_items": 12}}')
        out: dict[str, dict[str, Any]] = {}
        try:
            for section, fields in patch.items():
                if not isinstance(fields, dict):
                    continue
                for field, value in fields.items():
                    key = f"{section}.{field}"
                    spec = rules.CONFIG_BY_KEY.get(key)
                    if spec is None:
                        raise ValueError(f'不认识或不能在这里改的字段 "{key}"')
                    hint = rules.group_managed_hint(key)
                    if hint:
                        raise ValueError(f"「{rules.full_label(key)}」现在每个群自己一份，到「{hint}」里改，"
                                         "不从全局改（config.toml 里那一行只作新群的种子）")
                    if spec.get("readonly"):
                        raise ValueError(f"{spec['label']}不能从这里改：{spec.get('readonly_reason') or '只能改 config.toml 文件'}")
                    if spec["type"] == "secret":
                        raise ValueError(f"{spec['label']}是密钥，这里不许动（到网页「全部设置」里改）")
                    out.setdefault(str(section), {})[str(field)] = rules._validate_generic(spec, value)
        except ValueError as e:
            return _bad(str(e))
        if not out:
            return _bad("patch 里没有能改的字段")
        return out

    def _loosens_security(validated: dict[str, dict[str, Any]]) -> str:
        """验过的 patch 里有没有「放宽安全 / 让 MaiWork 更常往群里发东西」的；返回中文说明（没有 → ""）。

        判定口径（宁可多确认一次，不让模型绕过确认）：
        - approval.remind：提醒开关（关掉 = 权限边界变松）一律要管理员点头；
        - approval.auto_review：从「关」变「开」= 放宽（要确认）；关掉不用；
        - approval.auto_review_daily：上限调大 = 放宽（要确认）；调低不用；
        - topics.min_gap_hours 变小 = 开话题更勤（要确认）。
        0.8.0：管理员名单 / 免批 / 批准开关、推送上限 / 睡觉时段、开话题开关与上限都归
        每个群自己管了，根本进不到这里（_validated_patch 已拒）；这里只剩仍归全局的几项。
        比「现在值」时以有效设置为准；读不到当前值时，但凡沾边一律算放宽（要确认）。
        2026-10-12 起它只负责 _SET_RULES_FINE_GRAINED 那几项；白名单以外的其余字段由
        _strict_keys 一律要确认（不靠这里判「是不是放宽」）。
        """
        out: list[str] = []
        approval = validated.get("approval") or {}
        _APPROVAL_ZH = {
            "remind": "待批提醒开关",
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
            if field == "remind":
                out.append(f"关掉{label}" if value is False else f"打开{label}")
            else:
                members = "、".join(str(m) for m in value) if isinstance(value, list) else str(value)
                out.append(f"改动{label}（{members or '清空'}）")
        topics = validated.get("topics") or {}
        now_topics = getattr(current, "topics", None)
        if "min_gap_hours" in topics:
            new_gap = topics["min_gap_hours"]
            now_gap = getattr(now_topics, "min_gap_hours", None)
            if not isinstance(now_gap, int) or (isinstance(new_gap, int) and new_gap < now_gap):
                out.append(f"开话题最小间隔缩到 {new_gap} 小时")

        # 去重保序
        seen: set[str] = set()
        uniq = [x for x in out if not (x in seen or seen.add(x))]
        return "；".join(uniq)

    def _strict_keys(validated: dict[str, dict[str, Any]]) -> list[str]:
        """验过的 patch 里「不是无安全影响的调参项」的键 → 一律要先确认。

        白名单口径：_SET_RULES_NO_CONFIRM 里那些键（资讯 / 画像 / 快速判断的节奏与数量）
        可以直接改；_SET_RULES_FINE_GRAINED（approval.remind / auto_review /
        auto_review_daily / topics.min_gap_hours）交给 _loosens_security 判「是不是放宽」，
        不算在这里；**其余任何键**——包括将来新增、schema 里还没出现的——都算需要确认。
        """
        out: list[str] = []
        for section, fields in validated.items():
            for field in fields:
                key = f"{section}.{field}"
                if key in _SET_RULES_NO_CONFIRM or key in _SET_RULES_FINE_GRAINED:
                    continue
                out.append(key)
        return sorted(out)

    def _patch_summary(validated: dict[str, dict[str, Any]], loosen: str) -> str:
        """小票摘要：patch 里**全部**要改的字段都列出来（节.键 → 新值，名单类显示成员），
        超长截断但写明「共改 N 项」——管理员点头前看到的必须和真正会改的一致。

        全部字段都用中文名（和网页「全部配置」同一套：rules.CONFIG_BY_KEY 的 label），
        不给管理员看 approval.admins 这种英文键名；万一哪个键没登记中文名才退回「节.字段」。
        loosen 是 _loosens_security / _strict_keys 给出的「为什么这次要确认」中文说明，
        放得下就缀在末尾，让管理员一眼看出这次碰了安全相关设置。
        """
        def _label(key: str) -> str:
            return rules.full_label(key)

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
        note = f"（涉及安全设置：{loosen}）" if loosen else ""
        if len(head) + len(note) <= MAX_SUMMARY - 1:
            return head + note
        # 截断：尽量多装几项，末尾写明总数；放得下就把「涉及安全设置」一起带上
        suffix = f"…（共改 {total} 项）"
        if len(suffix) + len(note) < MAX_SUMMARY:
            suffix += note
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
        # 要确认的两种情况，先合起来判（摘要列出 patch 里的全部字段——点同意前看到的就是
        # 真要改的）：
        # ① 白名单之外的任何字段（含将来新增的）：一律先让管理员点头，不判「是不是放宽」；
        # ② approval.* / topics.min_gap_hours：沿用细粒度口径，只有放宽才要点头。
        loosen = _loosens_security(validated)
        strict = _strict_keys(validated)
        if strict:
            # 摘要末尾已有「（涉及安全设置：…）」，这里只给中文名，不再重复前缀
            names = "、".join(rules.full_label(k) for k in strict)
            loosen = f"{loosen}；{names}" if loosen else names
        if loosen:
            pending = _need_confirm("rules_manage", args, _patch_summary(validated, loosen))
            if pending is not None:
                return pending
        # 2026-10 docs/18 第一步：不再写 kv 覆盖层，和网页「全部配置」走同一条
        # save_config_patch 直写 config.toml 的路径，写完立刻热生效。
        flat: dict[str, Any] = {}
        for section, fields in validated.items():
            for field, value in fields.items():
                flat[f"{section}.{field}"] = value
        try:
            plugin_dir, data_dir = svc.config_file_ops()
        except Exception:
            return _bad("这台机器拿不到 config.toml 的位置，改不了（去网页「全部设置」里改）")
        try:
            changed = rules.save_config_patch(svc.store, flat, base=base, plugin_dir=plugin_dir)
        except ValueError as e:
            return _bad(str(e))
        except Exception as e:
            return _bad(f"写 config.toml 失败：{e}")
        # 热生效（网页路径同款；失败微博页面对齐：文件已写，宿主文件监控会补一次）
        try:
            from . import config_file as _cf

            apply_fn = getattr(svc, "apply_config_text", None)
            if callable(apply_fn):
                await apply_fn(_cf.read_text(plugin_dir))
        except Exception:
            logger.exception("set_rules 写后应用出错（文件已写，宿主文件监控会补一次）")
        lines: list[str] = []
        for key in changed:
            sec, _, fld = key.partition(".")
            value = flat.get(sec + "." + fld, validated.get(sec, {}).get(fld))
            name = rules.full_label(key)
            if isinstance(value, bool):
                value = "开" if value else "关"
            elif isinstance(value, (list, tuple)):
                value = "、".join(str(m) for m in value) or "（清空）"
            lines.append(f"{name} → {value}")
        if not lines:
            return _ok("这些值和配置文件里一样，等于没改", data=flat)
        return _ok("规则已写进 config.toml 并立刻生效：\n" + "\n".join(lines), data=flat)

    async def identity_edit(ctx: ToolContext, args: dict) -> ToolResult:
        kind = str(args.get("kind") or "").strip().lower()
        text = str(args.get("text") or "")
        # 「每群三份」收尾 2026-10-03：kind=group 整条退役（每群内容换成本群规矩
        # /api/groups/{gid}/rules + 本群做法 /api/groups/{gid}/skills；本工具现在只管全局三份）
        if kind not in ("soul", "agents", "memory"):
            return _bad("kind 只能是 soul / agents / memory（每群内容走网页「本群规矩 / 本群做法」）")
        ident = _identity()
        if ident is None:
            return _bad("身份文件没就位，这次改不了")
        try:
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
        pending = _need_confirm("group_deliver", args, f"发到群 {gid}：{_tail(text, 100)}")
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
        pending = _need_confirm("group_deliver", args, f"给群 {gid} 发群公告：{_tail(content, 100)}")
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
        pending = _need_confirm("group_deliver", args, f"群 {gid} 的群文件 {action}")
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
        pending = _need_confirm("group_deliver", args, f"把 {path} 传进群 {gid} 的相册")
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
        pending = _need_confirm("skill_manage", args, f"删掉 skill「{name}」")
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

    async def skill_write(ctx: ToolContext, args: dict) -> ToolResult:
        """管理员写 / 改一份做法（skill）；声明执行类工具会先出待确认小票。"""
        name = str(args.get("name") or "").strip()
        if not name:
            return _bad("要给 name（做法名，小写字母/数字/连字符）")
        settings = _settings()
        data_dir = getattr(settings, "data_dir", None) or getattr(svc, "data_dir", None)
        store = getattr(svc, "store", None)
        if data_dir is None or store is None:
            return _bad("skill 目录不可用，本次写不了")
        from . import skills_web

        body: dict[str, Any] = {"name": name}
        if "description" in args:
            body["description"] = str(args.get("description") or "")
        if "body" in args:
            body["body"] = str(args.get("body") or "")
        allowed_raw = args.get("allowed_tools")
        if allowed_raw is not None:
            body["allowed_tools"] = allowed_raw
            try:
                allowed_list = skills_web.normalize_allowed_tools(allowed_raw)
            except ValueError:
                return _bad("allowed_tools 要是工具名列表（或空格分隔的字符串）")
        else:
            allowed_list = []
        if "roles" in args:
            body["roles"] = args["roles"]
        try:
            existing = skills_web.get_view(data_dir, store, name)
        except Exception:
            existing = None
        if existing is not None and existing.get("builtin"):
            return _bad(f"「{name}」是内置做法，改不了（只能在网页停用它）")
        # 声明执行类工具（挂到调研岗就真能用）→ 待确认小票（exec_grants 唯一判定；
        # 传原值，字符串写法在它内部同样归一化）
        grants = settings_ops.exec_grants(svc, skill_name=name, new_allowed=allowed_raw) \
            if allowed_raw is not None else []
        if grants:
            pending = _need_confirm(
                "skill_manage", args,
                f"{'新建' if existing is None else '改'}做法「{name}」：声明执行类工具"
                f"（{'、'.join(grants)}），挂上调研岗后它就能跑命令 / 写文件等，同意才写",
            )
            if pending is not None:
                return pending
        try:
            if existing is None:
                view = skills_web.create(data_dir, store, body)
            else:
                view = skills_web.update(data_dir, store, name, body)
        except (ValueError, FileExistsError, PermissionError) as e:
            return _bad(str(e))
        except Exception as e:
            return _bad(f"写做法失败：{e}")
        # 回报：声明工具的注册/执行类拆分 + 哪些调研岗挂着它
        declared = [str(t) for t in (view.get("allowed_tools") or [])]
        registered: list[str] = []
        exec_names: list[str] = []
        unknown: list[str] = []
        tools = getattr(svc, "tools", None)
        for t in declared:
            try:
                tr = tools.traits(t) if tools is not None else None
            except Exception:
                tr = None
            if tr is None or "worker" not in (tr.get("roles") or ()):
                unknown.append(t)
            else:
                registered.append(t)
                if tr.get("exec_class"):
                    exec_names.append(t)
        attached: list[str] = []
        agents = getattr(svc, "agents", None)
        if agents is not None:
            try:
                for k in ("news", "idea", "goal"):
                    if name in list(agents.profile(k).get("skills") or []):
                        attached.append(k)
            except Exception:
                pass
        lines = [f"做法「{name}」已{'新建' if existing is None else '改好'}。"]
        if declared:
            lines.append(
                f"声明的工具：已注册 {'、'.join(registered) or '（无）'}"
                + (f"；没注册/连不上 {'、'.join(unknown)}" if unknown else "")
                + (f"；其中执行类 {'、'.join(exec_names)}" if exec_names else "")
            )
        if attached:
            lines.append(f"现在挂在调研岗：{'、'.join(attached)}")
        elif declared:
            lines.append("还没挂到岗位，用 agent_manage 的 update（add_skills）挂上才会生效")
        return _ok("\n".join(lines), data=view)

    async def social_toggle(ctx: ToolContext, args: dict) -> ToolResult:
        """开 / 关一个社交平台（开要先确认，会后台装 CLI；关直接执行并取消在跑的安装）。"""
        from . import tools_admin_settings as _tas

        bad_secret = _tas.secret_args(args)
        if bad_secret:
            return _bad(
                f"密钥不经过对话：参数里不能带 {'、'.join(bad_secret)}。"
                "Cookie / 登录态只能管理员自己到网页「设置 → 登录态」填；这次什么都没改。"
            )
        extra = sorted(str(k) for k in args if str(k) not in ("platform", "enabled"))
        if extra:
            return _bad(f"不认识的参数：{'、'.join(extra)}（只认 platform、enabled）")
        platform = str(args.get("platform") or "").strip()
        enabled = args.get("enabled")
        from . import social_platforms

        if platform not in social_platforms.PLATFORMS:
            return _bad(f"没有这个社交平台：{platform or '（没给 platform）'}（"
                        f"现有：{'、'.join(social_platforms.PLATFORMS)}）")
        store = getattr(svc, "store", None)
        if store is None:
            return _bad("存储没就位")
        row = next(r for r in social_platforms.list_platforms(store) if r["id"] == platform)
        label = str(row.get("name") or platform)
        if bool(enabled):
            pending = _need_confirm(
                "social_toggle", args,
                f"打开社交平台「{label}」：资讯每批的保底补搜会自动搜它"
                + ("" if row.get("installed") else "（还没装，会先在后台安装）"),
            )
            if pending is not None:
                return pending
            social_platforms.enable_platform(store, platform)
            start = getattr(svc, "start_social_install", None)
            if not row.get("installed") and callable(start):
                start(platform)
            return _ok(f"已打开社交平台「{label}」"
                       + ("" if row.get("installed") or not callable(start)
                          else "，正在后台安装（env_overview 能看进度）")
                       + "。Cookie 只能管理员自己到网页「设置 → 登录态」填。")
        cancel = getattr(svc, "cancel_social_install", None)
        if callable(cancel):
            await cancel(platform)
        social_platforms.disable_platform(store, platform)
        return _ok(f"已关闭社交平台「{label}」。Cookie 要改请到网页「设置 → 登录态」填。")

    async def skill_install(ctx: ToolContext, args: dict) -> ToolResult:
        url = str(args.get("url") or "").strip()
        replace = bool(args.get("replace"))
        allow_exec = bool(args.get("allow_exec_tools"))
        try:
            src, kind = skill_source_url(url)
        except ValueError as e:
            return _bad(str(e))
        settings = _settings()
        data_dir = getattr(settings, "data_dir", None) or getattr(svc, "data_dir", None)
        if data_dir is None or getattr(svc, "store", None) is None:
            return _bad("skill 目录不可用，本次无法安装")
        pending = _need_confirm(
            "skill_manage", args,
            f"从 {url} 下载并安装一个 skill" + ("（同名的会被覆盖）" if replace else "")
            + ("；允许它声明执行类工具" if allow_exec else "")
            + "；这是外部内容，建议先在网页上查看它的内容",
        )
        if pending is not None:
            return pending
        try:
            blob = await fetch_public(src)
        except ValueError as e:
            return _bad(str(e))
        except Exception as e:
            return _bad(f"下载失败：{e}")
        from . import skills_web
        from .skills import parse_front_matter

        if kind == "md":
            import io
            import zipfile

            try:
                text = blob.decode("utf-8")
                name = str((parse_front_matter(text) or {}).get("name") or "").strip()
            except Exception:
                name = ""
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name or ""):
                return _bad(
                    "下载到的文件不是合格的 SKILL.md：开头缺少 name 字段，"
                    "或 name 含小写字母、数字、连字符以外的字符"
                )
            mem = io.BytesIO()
            with zipfile.ZipFile(mem, "w") as zf:
                zf.writestr(f"{name}/SKILL.md", text)
            blob = mem.getvalue()
        # 装前看一遍它声明的工具：带执行类工具时管理员得明确同意（allow_exec_tools=true 重试
        # 会带新指纹出第二张确认票）
        declared_tools: list[str] = []
        try:
            import zipfile as _zf
            import io as _io

            with _zf.ZipFile(_io.BytesIO(blob)) as zf:
                for n in zf.namelist():
                    if n.rstrip("/").endswith("SKILL.md"):
                        fm = parse_front_matter(zf.read(n).decode("utf-8", "replace")) or {}
                        declared_tools = [str(t) for t in (fm.get("allowed_tools") or [])]
                        break
        except Exception:
            declared_tools = []
        if declared_tools and not allow_exec:
            tools = getattr(svc, "tools", None)
            exec_names = [t for t in declared_tools
                          if (tr := (tools.traits(t) if tools is not None else None)) is not None
                          and tr.get("exec_class") and "worker" in (tr.get("roles") or ())]
            if exec_names:
                return _bad(
                    f"这个 skill 声明了执行类工具（{'、'.join(exec_names)}），挂上调研岗后它就能"
                    "跑命令 / 写文件等。要装请明确同意：重试时把 allow_exec_tools 设为 true。"
                )
        try:
            # 同网页上传一样在本线程装（store 不能跨线程用；包有大小上限，很快）
            view = skills_web.install_zip(svc.store, data_dir, blob, filename=url.rsplit("/", 1)[-1], replace=replace)
        except FileExistsError as e:
            return _bad(f"{e}（如需覆盖，请说明「覆盖同名 skill」后重试）")
        except ValueError as e:
            return _bad(f"安装失败：{e}")
        name = str((view or {}).get("name") or "") if isinstance(view, dict) else ""
        if name:
            return _ok(f"已安装 skill「{name}」；网页「设置 → 工具」里能看到、能停用", data=view)
        return _ok("已安装 skill；网页「设置 → 工具」里能看到、能停用", data=view)

    async def env_overview(ctx: ToolContext, args: dict) -> ToolResult:
        """现在这套 MaiWork 能做到什么（模型 / 搜索 / 干活环境 / 交付 / skill / MCP），缺的怎么补。"""
        lines: list[str] = ["任务能否做成，主要看模型能力和下面这些环境能力。"]
        mark = {"ok": "✅", "warn": "⚠️", "wait": "⏳", "off": "⛔"}
        data: dict[str, Any] = {"items": []}
        try:
            from . import onboarding

            for it in onboarding.items(svc):
                data["items"].append(it)
                lines.append(f"{mark.get(str(it.get('state')), '•')} {it.get('title')}：{it.get('text')}")
        except Exception:
            logger.exception("env_overview 读能力清单出错")
            lines.append("（能力清单暂时读取失败）")
        env = getattr(_settings(), "environments", None)
        try:
            from .environments import vm_platforms as _vmp

            _on = _vmp.enabled_kinds(_settings())
        except Exception:
            _on = ()
        if env is not None:
            if _on:
                names = "、".join(
                    (_vmp.get(k).label if _vmp.get(k) else k)
                    + ("（未填写密钥）" if _vmp.get(k) and not _vmp.get(k).has_key(_settings()) else "")
                    for k in _on
                )
                tail = "；排第一的优先使用，不可用时自动切换另一家" if len(_on) > 1 else ""
                lines.append(f"• 一次性云端机器：已启用 {names}{tail}")
            else:
                lines.append("• 一次性云端机器：均未启用")
        # 可选依赖（办公文档三库；2026-10-15 用户定：可选安装，不进必装依赖）：
        # 装了说装了，没装指路网页一键装（只读上次探测结果，不在这里现探测）
        try:
            deps = getattr(svc, "optional_deps", None)
            if deps is not None:
                view = deps.status()
                rows = []
                for g in view.get("groups") or []:
                    if g.get("kind") == "asset":
                        # 画图组件（中文字体 / 浏览器）：直接用状态里那句话
                        rows.append({"id": g.get("id"), "state": g.get("state"), "installed": []})
                        lines.append(f"• {g.get('label')}：{g.get('summary') or g.get('state')}"
                                     + (f"（{g.get('hint')}）" if g.get("hint") else ""))
                        continue
                    have = [
                        f"{p.get('name')} {p.get('version') or ''}".strip()
                        for p in (g.get("packages") or []) if p.get("installed")
                    ]
                    rows.append({"id": g.get("id"), "state": g.get("state"), "installed": have})
                    state = str(g.get("state") or "")
                    last = g.get("last_install") or {}
                    why = (
                        f"；上次安装失败：{last.get('error')}"
                        if str(last.get("state")) == "failed" and last.get("error") else ""
                    )
                    if state == "ok":
                        soffice = (g.get("tools") or [{}])[0]
                        lines.append(
                            f"• {g.get('label')}：已安装（{'、'.join(have)}）；"
                            + (
                                "LibreOffice 已安装（可转 PDF、可重算公式）"
                                if soffice.get("found") else "LibreOffice 未安装（无法转 PDF、无法重算 Excel 公式）"
                            )
                        )
                    elif state in ("installing", "uninstalling"):
                        lines.append(f"• {g.get('label')}：正在安装或卸载…")
                    elif state == "partial":
                        lines.append(
                            f"• {g.get('label')}：只安装了一部分（{'、'.join(have)}）；"
                            "请到网页「设置 → 工具」重新安装"
                        )
                    elif state == "unknown":
                        lines.append(f"• {g.get('label')}：尚未检测（打开网页「设置 → 工具」会自动检测一次）")
                    else:
                        lines.append(
                            f"• {g.get('label')}：未安装（可选）。需要交付 Word / Excel / PPT 时，"
                            f"可在网页「设置 → 工具」一键安装（装到 MaiWork 自己的目录，"
                            f"不改动 MaiBot 的环境）；未安装也可交付 Markdown、CSV 或 HTML{why}"
                        )
                if rows:
                    data["optional_deps"] = rows
        except Exception:
            logger.exception("env_overview 读可选依赖出错")
        skills = _skills()
        if skills is not None:
            try:
                rows = skills.list()
                builtin = [r for r in rows if r.get("builtin")]
                custom = [r for r in rows if not r.get("builtin")]
                data["skills"] = [r.get("name") for r in rows]
                lines.append(
                    f"• skill：自带 {len(builtin)} 个（{'、'.join(str(r.get('name')) for r in builtin)}）"
                    + (f"，另装了 {len(custom)} 个" if custom else "")
                )
            except Exception:
                pass
        exts = getattr(svc, "extensions", None)
        if exts is not None:
            try:
                info = list(exts.info())
                lines.append(f"• MCP 连接：{len(info)} 个，开着的 {sum(1 for i in info if i.get('enabled'))} 个")
            except Exception:
                pass
        try:  # 社交平台逐平台说清：开没开 / 装没装 / Cookie 填没填（永不出 Cookie 值）
            from . import social_platforms

            for row in social_platforms.list_platforms(getattr(svc, "store", None)):
                state = "开" if row.get("enabled") else "关"
                _running = getattr(svc, "social_install_running", None)
                installing = bool(row.get("installing")) or (
                    callable(_running) and bool(_running(row["id"])))
                install = "安装中" if installing else ("已装" if row.get("installed") else "没装")
                if row.get("needs_cookie"):
                    cookie = "Cookie 已填" if row.get("cookie_set") else "Cookie 没填"
                else:
                    cookie = "不需要 Cookie"
                tail = f"（最近错误：{row['last_error']}）" if row.get("last_error") else ""
                lines.append(f"• {row.get('name') or row['id']}：{state} · {install} · {cookie}{tail}")
            lines.append(
                "资讯每批的保底补搜会自动搜已启用的平台（最多 3 个方向）；"
                "Twitter/X 不填 Cookie 时走网页搜索备选"
            )
        except Exception:
            logger.exception("env_overview 读社交平台出错")
        lines.append(
            "怎么补：开关和数量这类设置可以直接改（安全相关的会先等你确认）；装 skill 请提供网址；"
            "MCP 服务可以直接让我加、测连接、重连、改白名单（新加、改地址、放宽权限会先等你确认）；"
            "专岗换模型 / 思考强度 / 备用、建自定义专岗，以及模型端点和模型库，也可以在对话里让我改"
            "（新加端点、改端点地址或协议、删除会先等你确认）。"
            "密钥请自己在网页上填（「设置 → 模型」「设置 → 工具」「设置 → 全部设置 → 运行环境」），"
            "不要发在对话里。"
        )
        return _ok("\n".join(lines), data=data)

    # ------------------------------------------------------------------
    # 合并工具分派器：把 action 写进 args 再分派给上面的具体函数——
    # 小票指纹按「调 _need_confirm 时那份 args」算，execute 回放原样送回，
    # 两边一致才能免第二次确认（implement.md 风险点）。
    # ------------------------------------------------------------------

    _SKILL_ACTIONS = ("list", "write", "install", "delete")

    async def skill_manage(ctx: ToolContext, args: dict) -> ToolResult:
        """做法（skill）统一入口：list 看扩展和做法；write 写 / 改；install 从网址装；delete 删。"""
        a = str(args.get("action") or "list").strip().lower()
        if a not in _SKILL_ACTIONS:
            return _bad(f"不认识的 action「{a or '（没给）'}」：可用 {'、'.join(_SKILL_ACTIONS)}")
        args = {**args, "action": a}
        handlers = {
            "list": list_extensions,
            "write": skill_write,
            "install": skill_install,
            "delete": skill_delete,
        }
        return await handlers[a](ctx, args)

    async def rules_manage(ctx: ToolContext, args: dict) -> ToolResult:
        """规则统一入口：get 看现在生效的；set 改（patch 形如 {"节": {"字段": 值}}）。

        不给 action 时：带了 patch 就按 set，否则按 get。
        """
        a = str(args.get("action") or "").strip().lower()
        if not a:
            a = "set" if isinstance(args.get("patch"), dict) else "get"
        if a not in ("get", "set"):
            return _bad(f"不认识的 action「{a}」：可用 get、set")
        args = {**args, "action": a}
        return await (set_rules if a == "set" else get_rules)(ctx, args)

    _IDENTITY_ACTIONS = ("get", "edit", "remember")

    async def identity_manage(ctx: ToolContext, args: dict) -> ToolResult:
        """身份统一入口：get 看 SOUL / AGENTS / MEMORY；edit 改其中一份；remember 记一件事。

        不给 action 时：kind+text → edit；只有 text → remember；都没有 → get。
        """
        a = str(args.get("action") or "").strip().lower()
        if not a:
            if str(args.get("kind") or "").strip() and args.get("text") is not None:
                a = "edit"
            elif args.get("text") is not None:
                a = "remember"
            else:
                a = "get"
        if a not in _IDENTITY_ACTIONS:
            return _bad(f"不认识的 action「{a}」：可用 {'、'.join(_IDENTITY_ACTIONS)}")
        args = {**args, "action": a}
        handlers = {"get": get_identity, "edit": identity_edit, "remember": remember}
        return await handlers[a](ctx, args)

    _PROFILE_ACTIONS = ("read", "add", "edit", "lock", "unlock", "delete", "bulk_delete", "refresh")

    async def profile_manage(ctx: ToolContext, args: dict) -> ToolResult:
        """画像统一入口：read 看（默认）；add/edit/lock/unlock/delete 改单条；
        bulk_delete 批量删（要确认）；refresh 现在重新整理（后台跑）。"""
        a = str(args.get("action") or "read").strip().lower()
        if a not in _PROFILE_ACTIONS:
            return _bad(f"不认识的 action「{a or '（没给）'}」：可用 {'、'.join(_PROFILE_ACTIONS)}")
        args = {**args, "action": a}
        if a == "read":
            return await read_profile(ctx, args)
        if a == "refresh":
            return await refresh_profile(ctx, args)
        if a == "bulk_delete":
            return await profile_bulk_delete(ctx, args)
        return await profile_edit(ctx, args)  # add / edit / lock / unlock / delete

    _FOCUS_ACTIONS = ("list", "add", "remove", "auto")

    async def focus_manage(ctx: ToolContext, args: dict) -> ToolResult:
        """关注成员统一入口：list 看（默认）；add / remove / auto 调。"""
        a = str(args.get("action") or "list").strip().lower()
        if a not in _FOCUS_ACTIONS:
            return _bad(f"不认识的 action「{a or '（没给）'}」：可用 {'、'.join(_FOCUS_ACTIONS)}")
        args = {**args, "action": a}
        if a == "list":
            return await list_focus(ctx, args)
        return await focus_edit(ctx, args)

    _SOURCE_ACTIONS = ("rss_add", "rss_remove", "block_domain", "unblock_domain")

    async def source_manage(ctx: ToolContext, args: dict) -> ToolResult:
        """资讯来源统一入口：rss_add / rss_remove（删要确认）/ block_domain / unblock_domain。"""
        a = str(args.get("action") or "").strip().lower()
        if a not in _SOURCE_ACTIONS:
            return _bad(f"不认识的 action「{a or '（没给）'}」：可用 {'、'.join(_SOURCE_ACTIONS)}")
        if a == "unblock_domain":
            a = "block_domain"
            args = {**args, "blocked": False}
        args = {**args, "action": a}
        if a == "rss_add":
            return await rss_add(ctx, args)
        if a == "rss_remove":
            return await rss_remove(ctx, args)
        return await block_domain(ctx, args)

    async def news_manage(ctx: ToolContext, args: dict) -> ToolResult:
        """资讯统一入口：list 看最近备出来的（默认）；run_now 现在备一批（后台跑）。"""
        a = str(args.get("action") or "list").strip().lower()
        if a not in ("list", "run_now"):
            return _bad(f"不认识的 action「{a}」：可用 list、run_now")
        args = {**args, "action": a}
        return await (run_news_now if a == "run_now" else list_news)(ctx, args)

    async def idea_manage(ctx: ToolContext, args: dict) -> ToolResult:
        """构想统一入口：list 看（默认）；make_now 现在想一个（后台跑，同群互斥）。"""
        a = str(args.get("action") or "list").strip().lower()
        if a not in ("list", "make_now"):
            return _bad(f"不认识的 action「{a}」：可用 list、make_now")
        args = {**args, "action": a}
        return await (make_idea_now if a == "make_now" else list_ideas)(ctx, args)

    _TASK_ACTIONS = ("list", "detail", "create", "cancel")

    async def task_manage(ctx: ToolContext, args: dict) -> ToolResult:
        """任务统一入口：list 看（默认；给了 task_id 没给 action 按 detail）；detail 看详情；
        create 建并开工（要确认）；cancel 取消。"""
        a = str(args.get("action") or "").strip().lower()
        if not a:
            a = "detail" if str(args.get("task_id") or "").strip() else "list"
        if a not in _TASK_ACTIONS:
            return _bad(f"不认识的 action「{a}」：可用 {'、'.join(_TASK_ACTIONS)}")
        args = {**args, "action": a}
        handlers = {"list": list_tasks, "detail": task_detail, "create": create_task, "cancel": cancel_task}
        return await handlers[a](ctx, args)

    _GOAL_ACTIONS = ("list", "create", "pause", "resume", "cancel", "set_interval",
                     "set_url", "set_guidance", "add_criterion", "remove_criterion", "edit_criterion")

    async def goal_manage(ctx: ToolContext, args: dict) -> ToolResult:
        """目标统一入口：list 看（默认）；create 立一个（要确认）；
        其余 action 走原 goal_adjust（pause/resume/cancel 要确认）。"""
        a = str(args.get("action") or "list").strip().lower()
        if a not in _GOAL_ACTIONS:
            return _bad(f"不认识的 action「{a or '（没给）'}」：可用 {'、'.join(_GOAL_ACTIONS)}")
        args = {**args, "action": a}
        if a == "list":
            return await list_goals(ctx, args)
        if a == "create":
            return await create_goal(ctx, args)
        return await goal_adjust(ctx, args)

    async def request_manage(ctx: ToolContext, args: dict) -> ToolResult:
        """群友派的活统一入口：list 看（默认）；approve 批（要确认）；reject 拒。"""
        a = str(args.get("action") or "list").strip().lower()
        if a not in ("list", "approve", "reject"):
            return _bad(f"不认识的 action「{a}」：可用 list、approve、reject")
        args = {**args, "action": a}
        handlers = {"list": list_requests, "approve": approve_request, "reject": reject_request}
        return await handlers[a](ctx, args)

    _DELIVER_ACTIONS = ("message", "notice", "album_upload", "delete", "rename", "move", "mkdir", "rmdir")

    async def group_deliver(ctx: ToolContext, args: dict) -> ToolResult:
        """往服务群交付 / 管群文件统一入口（全部动作都要管理员确认）：
        message 群里说一句；notice 发群公告；album_upload 传群相册；
        delete / rename / move / mkdir / rmdir 管群文件（file_ 前缀写法也认）。"""
        a = str(args.get("action") or "").strip().lower()
        if a.startswith("file_"):
            a = a[len("file_"):]
        if a not in _DELIVER_ACTIONS:
            return _bad(f"不认识的 action「{a or '（没给）'}」：可用 {'、'.join(_DELIVER_ACTIONS)}")
        args = {**args, "action": a}
        if a == "message":
            return await send_group_message(ctx, args)
        if a == "notice":
            return await group_notice_send(ctx, args)
        if a == "album_upload":
            return await group_album_upload(ctx, args)
        return await group_file_manage(ctx, args)

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
            rid = str(out.get("run_id") or "") if isinstance(out, dict) else ""
            tail = f"（运行编号 {rid}）" if rid else ""
            return _ok(f"已经让群 {gid} 现在备一批资讯（后台跑，好了会在网页上出现）{tail}")
        # 没有现成入口（老服务包 / 测试）：自己起一个后台任务跑。
        # 开工前提照样先过一遍，别嘴上说开工、后台立刻静默退出（A11）。
        precheck = getattr(feeds, "news_precheck", None)
        if callable(precheck):
            try:
                why = str(precheck(gid) or "")
            except Exception as e:
                return _bad(f"检查备料开工前提出错：{e}")
            if why:
                return _bad(why)
        prepare = getattr(feeds, "prepare_news", None)
        if not callable(prepare):
            return _bad("这个 MaiWork 的资讯模块没有备料入口")
        try:
            _spawn_bg(prepare(gid))
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
            _spawn_bg(_run())
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
            _spawn_bg(_run())
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
        deny = _feature_deny(gid, "task")
        if deny is not None:
            return deny
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
            "task_manage",
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
        # 任务关闭连带停目标（用户已确认）：立目标也走 task 开关
        deny = _feature_deny(gid, "task")
        if deny is not None:
            return deny
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
            "goal_manage",
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
        deny = _feature_deny(gid, "task")
        if deny is not None:
            return deny
        # 群友派的活要 **bot 管理员**批准（红线）；模型可能被群内容注入，
        # 绝不能一句话就替管理员把活批了——先写小票，管理员在网页点同意才算数
        pending = _need_confirm("request_manage", args, f"批准请求 {rid}（{_tail(row['title'], 60)}，群 {gid}）")
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

    async def goal_adjust(ctx: ToolContext, args: dict) -> ToolResult:
        """调整目标：改标准、暂停/恢复/取消、改检查频率。"""
        goal_id = str(args.get("goal_id") or "").strip()
        if not goal_id:
            return _bad("要给 goal_id")
        goal = svc.goals.get(goal_id)
        if goal is None:
            return _bad(f"没有目标 {goal_id}")
        gid, err = _served(str(goal.get("group_id") or ""))
        if err is not None:
            return err

        action = str(args.get("action") or "").strip()
        if not action:
            return _bad("要给 action：add_criterion / remove_criterion / edit_criterion / pause / resume / cancel / set_interval / set_url / set_guidance")

        # 暂停/恢复/取消 要确认（危险操作）
        if action in ("pause", "resume", "cancel"):
            pending = _need_confirm(
                "goal_manage",
                args,
                f"对目标 {goal_id}「{goal['title']}」执行 {action}",
            )
            if pending is not None:
                return pending

        try:
            if action == "pause":
                svc.goals._set_state(goal_id, "paused")
                return _ok(f"已暂停目标 {goal_id}", data={"goal_id": goal_id, "state": "paused"})
            elif action == "resume":
                svc.goals._set_state(goal_id, "active")
                return _ok(f"已恢复目标 {goal_id}", data={"goal_id": goal_id, "state": "active"})
            elif action == "cancel":
                svc.goals._set_state(goal_id, "cancelled")
                return _ok(f"已取消目标 {goal_id}", data={"goal_id": goal_id, "state": "cancelled"})
            elif action == "set_interval":
                hours = svc.goals.set_check_interval(goal_id, args.get("hours"))
                return _ok(f"已把目标 {goal_id} 的检查间隔改成 {hours:g} 小时", data={"goal_id": goal_id, "hours": hours})
            elif action in ("set_url", "set_guidance"):
                field = "url" if action == "set_url" else "guidance"
                context = svc.goals.set_context(goal_id, field, args.get(field, args.get("text", "")))
                return _ok(f"已更新目标 {goal_id} 的{'来源网址' if field == 'url' else '补充指导'}", data={"goal_id": goal_id, "context": context})
            elif action in ("add_criterion", "remove_criterion", "edit_criterion"):
                op = action.removesuffix("_criterion")
                svc.goals.adjust_criterion(goal_id, op, index=args.get("index"), text=args.get("text", ""))
                return _ok(f"已更新目标 {goal_id} 的完成标准", data={"goal_id": goal_id})
            else:
                return _bad(f"不认识的 action「{action}」")
        except Exception as e:
            return _bad(f"调整目标失败：{e}")

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
                "name": "profile_manage",
                "description": (
                    "看 / 改一个服务群的画像：action=read 读条目（带编号，默认）；"
                    "add 新增（category 取 recent / interest / ongoing / convention / resource）；"
                    "edit 改文字 / lock 锁定 / unlock 解锁 / delete 删（给 entry_id）；"
                    "bulk_delete 一次删多条（entry_ids，要管理员确认）；"
                    "refresh 现在重新整理画像（后台跑，不往群里发东西）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["read", "add", "edit", "lock", "unlock", "delete", "bulk_delete", "refresh"]},
                        "group_id": {"type": "string", "description": "群号（可选）"},
                        "category": {"type": "string", "description": "add 时的类别"},
                        "text": {"type": "string", "description": "add / edit 时的文字"},
                        "entry_id": {"type": "integer", "description": "edit / lock / unlock / delete 时的条目编号"},
                        "entry_ids": {"type": "array", "items": {"type": "integer"}, "description": "bulk_delete 要删的条目编号"},
                    },
                },
            },
            profile_manage,
        ),
        (
            {
                "name": "focus_manage",
                "description": "看 / 调一个服务群的关注成员：action=list 看（默认）；add 一直关注 / remove 取消 / auto 改回自动挑。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["list", "add", "remove", "auto"]},
                        "group_id": {"type": "string", "description": "群号（可选）"},
                        "user_id": {"type": "string", "description": "群友 QQ 号（add / remove / auto 用）"},
                    },
                },
            },
            focus_manage,
        ),
        (
            {
                "name": "news_manage",
                "description": "看 / 备一个服务群的资讯：action=list 看最近备出来的（默认）；run_now 现在备一批（后台跑，不占今天的定时时段）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["list", "run_now"]},
                        "group_id": {"type": "string", "description": "群号（可选）"},
                        "days": {"type": "integer", "description": "list 用：看最近几天，默认 7"},
                    },
                },
            },
            news_manage,
        ),
        (
            {
                "name": "idea_manage",
                "description": "看 / 想一个服务群的构想：action=list 看现在的（默认）；make_now 现在想一个（后台跑，同群同时只想一个）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["list", "make_now"]},
                        "group_id": {"type": "string", "description": "群号（可选）"},
                    },
                },
            },
            idea_manage,
        ),
        (
            {
                "name": "task_manage",
                "description": (
                    "看 / 派 / 取消任务：action=list 看任务列表（默认；给了 task_id 没给 action 按 detail）；"
                    "detail 看详情（要求、完成标准、最近的工具调用）；"
                    "create 建一个并立刻开工（要管理员确认；只用于需要子 agent 产出东西的活，"
                    "MaiWork 自己的维护别派：刷新画像用 profile_manage 的 refresh、备资讯用 "
                    "news_manage 的 run_now、想构想用 idea_manage 的 make_now）；cancel 取消。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["list", "detail", "create", "cancel"]},
                        "group_id": {"type": "string", "description": "群号（可选）"},
                        "task_id": {"type": "string", "description": "任务编号（detail / cancel 用）"},
                        "title": {"type": "string", "description": "create 用：任务名字"},
                        "request": {"type": "string", "description": "create 用：要干什么，说清楚"},
                        "criteria": {"type": "array", "items": {"type": "string"}, "description": "create 用：完成标准（可选）"},
                        "reason": {"type": "string", "description": "cancel 用：为什么取消（可选）"},
                    },
                },
            },
            task_manage,
        ),
        (
            {
                "name": "goal_manage",
                "description": (
                    "看 / 立 / 调目标：action=list 看（默认）；create 立一个后台目标"
                    "（按完成标准一直盯；有进展 / 完成时会在群里说一句，所以要确认）；"
                    "调整用 add_criterion / remove_criterion / edit_criterion / set_interval / "
                    "set_url / set_guidance（给 goal_id）；pause / resume / cancel 要确认。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作（见描述；默认 list）"},
                        "group_id": {"type": "string", "description": "群号（可选）"},
                        "goal_id": {"type": "string", "description": "目标编号（调整用）"},
                        "title": {"type": "string", "description": "create 用：目标名字"},
                        "body": {"type": "string", "description": "create 用：目标说明（可选）"},
                        "criteria": {"type": "array", "items": {"type": "string"}, "description": "create 用：怎么算做到（至少一条）"},
                        "check_hours": {"type": "integer", "description": "create 用：每几小时检查一次（可选）"},
                        "index": {"type": "integer", "description": "改标准时用，从 0 开始"},
                        "text": {"type": "string", "description": "改标准时的内容"},
                        "hours": {"type": "number", "minimum": 1, "maximum": 72, "description": "set_interval 用：检查间隔小时数（1–72）"},
                        "url": {"type": "string", "maxLength": 2048, "description": "set_url 用：来源网址（空串清除）"},
                        "guidance": {"type": "string", "maxLength": 4000, "description": "set_guidance 用：补充指导（空串清除）"},
                    },
                },
            },
            goal_manage,
        ),
        (
            {
                "name": "request_manage",
                "description": "看 / 批 / 拒群友派的活：action=list 看（默认）；approve 批准（批准后自动落成任务并开工，要管理员确认）；reject 拒绝。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["list", "approve", "reject"]},
                        "request_id": {"type": "string", "description": "请求编号（approve / reject 用）"},
                        "reason": {"type": "string", "description": "reject 用：为什么拒（可选）"},
                        "group_id": {"type": "string", "description": "list 用：群号（可选，默认当前群；填 * 看所有服务群）"},
                        "status": {"type": "string", "description": "list 用：pending / approved / rejected / expired，默认 pending"},
                    },
                },
            },
            request_manage,
        ),
        (
            {
                "name": "read_admin_history",
                "description": (
                    "读当前这段管理员对话的原文（含被截断 / 压缩的工具结果全文）。"
                    "只能读当前对话，参数里没有也认不了别的对话号。"
                    "after=从哪条消息 id 之后读（默认 0），limit=一次几条（默认 4，最多 20）；"
                    "offset / chars=每条按字符窗口取（默认 0 / 4000）；"
                    "一次整体最多 2 万字，没读完的最后一条用返回里的 after / offset 接着读。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "after": {"type": "integer", "description": "只看 id 大于它的消息（默认 0）"},
                        "limit": {"type": "integer", "description": "一次最多几条，默认 4，最多 20"},
                        "offset": {"type": "integer", "description": "每条从第几个字开始取，默认 0"},
                        "chars": {"type": "integer", "description": "每条最多取多少字，默认 4000，最多 20000"},
                    },
                },
            },
            read_admin_history,
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
                "description": (
                    "看日志：kind=models 看模型调用（哪次失败了、什么错，默认）；"
                    "kind=tools 看真实工具调用记录（子 agent / 主模型 / 管理员自己的：谁、什么时候、"
                    "调了什么工具、入参、返回或报错、耗时、成败）；kind=all 两段都回。"
                    "传了 tool 或 task_id 不给 kind 时自动按 tools 查；task_id 按前缀过滤，"
                    "tool 按名字包含过滤；只给服务群和全局的记录，失败原因已遮密钥。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["models", "tools", "all"],
                                 "description": "看哪种日志：models（模型调用，默认）/ tools（工具调用）/ all（两种）"},
                        "group_id": {"type": "string", "description": "群号（可选，默认当前聚焦群；填 * 看所有服务群）"},
                        "task_id": {"type": "string", "description": "任务号前缀（比如 feeds-verify、T-9、admin-tool；tools 用）"},
                        "tool": {"type": "string", "description": "工具名包含（比如 flovia 能捞出 mcp_flovia_flovia_x；tools 用）"},
                        "failed_only": {"type": "boolean", "description": "只看失败的"},
                        "limit": {"type": "integer", "description": "最多几条，默认 20（tools 默认 30），最多 200"},
                    },
                },
            },
            read_logs,
        ),
        (
            {
                "name": "tool_run",
                "description": (
                    "亲手试调一个注册给子 agent 的工具一次：回显真实返回或真实报错，"
                    "照常记进工具调用记录（read_logs 能查回来）。"
                    "执行类和只在派活回合用的工具会先出待确认小票；"
                    "要工作区的在单独临时工作区里跑。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "已注册工具名（agent_manage 的 detail 里看到的）"},
                        "args": {"type": "object", "description": "工具参数（JSON 对象，默认 {}）"},
                        "group_id": {"type": "string", "description": "工具拿群上下文时的群号（可选，默认当前聚焦群）"},
                    },
                    "required": ["name"],
                },
            },
            tool_run,
            150.0,  # 内层还要等子 agent 工具自己的超时 + 开关临时工作区，放宽到 150 秒
        ),
        (
            {
                "name": "rules_manage",
                "description": (
                    "看 / 改现在生效的规则（主动发言 / 开话题 / 派活审批 / 资讯，含网页改过的覆盖）："
                    "action=get 看（默认）；action=set 改，patch 形如 {\"delivery\": {\"push_per_day\": 5}}，"
                    "直接写进 config.toml 立刻生效；放宽安全设置时要管理员确认。不给 action 时带了 patch 按 set。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["get", "set"], "description": "看 / 改（默认看）"},
                        "patch": {"type": "object", "description": "set 用：{节: {字段: 值}}，配置里的任意可改字段"},
                    },
                },
            },
            rules_manage,
        ),
        (
            {
                "name": "identity_manage",
                "description": (
                    "看 / 改身份与工作记忆、记一件事：action=get 看 SOUL / AGENTS / MEMORY（默认）；"
                    "action=edit 改其中一份（kind 取 soul / agents / memory，text 给新全文；每群内容走网页"
                    "「本群规矩 / 本群做法」）；action=remember 记一件事（带 group_id 进本群「本群规矩」"
                    "追加一行，不带进全局 MEMORY.md；去重与隐私闸照旧）。不给 action 时：kind+text 按 edit、"
                    "只有 text 按 remember、都没有按 get。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["get", "edit", "remember"], "description": "看 / 改 / 记"},
                        "kind": {"type": "string", "enum": ["soul", "agents", "memory"], "description": "edit 用：改哪份"},
                        "text": {"type": "string", "description": "edit 的新全文 / remember 要记住的事"},
                        "group_id": {"type": "string", "description": "remember 用：记在某个服务群名下（可选；不填记全局）"},
                    },
                },
            },
            identity_manage,
        ),
        (
            {
                "name": "source_manage",
                "description": (
                    "管一个服务群的资讯来源：action=rss_add 加 RSS 源；rss_remove 删 RSS 源（要管理员确认）；"
                    "block_domain 屏蔽域名 / unblock_domain 取消屏蔽（只对这个群生效）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["rss_add", "rss_remove", "block_domain", "unblock_domain"]},
                        "group_id": {"type": "string", "description": "群号（可选）"},
                        "url": {"type": "string", "description": "rss_add 用：RSS / Atom 地址"},
                        "title": {"type": "string", "description": "rss_add 用：标题（可选）"},
                        "feed_id": {"type": "string", "description": "rss_remove 用：RSS 源编号"},
                        "domain": {"type": "string", "description": "block_domain / unblock_domain 用：域名，如 example.com"},
                    },
                    "required": ["action"],
                },
            },
            source_manage,
        ),
        (
            {
                "name": "skill_manage",
                "description": (
                    "管做法（skill）：action=list 看现在的扩展（MCP 连接）和做法（默认）；"
                    "action=write 新建 / 改一份做法（名字、一句话说明、正文、roles、allowed-tools；"
                    "声明执行类工具会先出待确认小票，同名内置做法改不了）；"
                    "action=install 从网址装一个做法（要确认，确认后才下载；认 GitHub 上 skill 目录 / "
                    "SKILL.md 的链接或 .zip，只装管理员点名要的）；action=delete 删一个做法（要确认）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["list", "write", "install", "delete"],
                                   "description": "看 / 写 / 装 / 删（默认看）"},
                        "name": {"type": "string", "description": "做法名（write / delete 用；小写字母/数字/连字符）"},
                        "description": {"type": "string", "description": "write 用：一句话说明"},
                        "body": {"type": "string", "description": "write 用：做法正文（写给子 agent 看的：什么时候用这些工具、怎么用）"},
                        "allowed_tools": {"type": "array", "items": {"type": "string"},
                                          "description": "write 用：允许调用的工具注册名（如 mcp_flovia_flovia_x、run_command）"},
                        "roles": {"type": "array", "items": {"type": "string"},
                                  "description": "write 用：默认 worker；一般不用给"},
                        "url": {"type": "string", "description": "install 用：https 网址"},
                        "replace": {"type": "boolean", "description": "install 用：已有同名做法时覆盖（默认否）"},
                        "allow_exec_tools": {"type": "boolean",
                                             "description": "install 用：同意安装声明了执行类工具的做法（默认否；"
                                                           "需要时上一次调用会说明声明了哪些）"},
                    },
                },
            },
            skill_manage,
            60.0,  # install 要下载 + 解压，放宽到 60 秒
        ),
        (
            {
                "name": "group_deliver",
                "description": (
                    "往一个服务群交付 / 管群文件（每个 action 都要管理员确认）："
                    "message 群里说一句话；notice 发群公告（机器人得是群主或管理员）；"
                    "album_upload 把工作区里的成品图传进群相册；"
                    "delete / rename / move / mkdir / rmdir 管群文件"
                    "（只能动机器人自己传 / 自己建的，删文件夹时里面只能有自己的东西）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["message", "notice", "album_upload", "delete", "rename", "move", "mkdir", "rmdir"]},
                        "group_id": {"type": "string", "description": "群号（可选）"},
                        "text": {"type": "string", "description": "message 用：要说的话"},
                        "content": {"type": "string", "description": "notice 用：公告内容"},
                        "path": {"type": "string", "description": "album_upload 用：工作区内图片的绝对路径"},
                        "album_id": {"type": "string", "description": "album_upload 用：相册编号"},
                        "file_id": {"type": "string", "description": "文件编号"},
                        "name": {"type": "string", "description": "新名字 / 文件夹名"},
                        "folder_id": {"type": "string", "description": "目标文件夹 / 要删的文件夹"},
                    },
                    "required": ["action"],
                },
            },
            group_deliver,
        ),
        (
            {
                "name": "social_toggle",
                "description": (
                    "开 / 关一个社交平台（twitter、xiaohongshu、bilibili、youtube、reddit）。"
                    "打开要先确认，会自动补搜；没装会在后台装。Cookie 不经过对话，"
                    "让管理员到网页「设置 → 登录态」自己填。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "platform": {"type": "string", "description": "平台 id"},
                        "enabled": {"type": "boolean", "description": "true 开 / false 关"},
                    },
                    "required": ["platform", "enabled"],
                },
            },
            social_toggle,
        ),
        (
            {
                "name": "env_overview",
                "description": (
                    "看现在这套 MaiWork 能做到什么：模型、联网搜索、干活环境（本机 / 一次性云端机器）、交付、"
                    "skill、MCP，各自能用 / 受限 / 没开，以及缺的怎么补。管理员问「能不能做 XX」「环境配好没」"
                    "或要帮他配置环境时先看这个。"
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            env_overview,
        ),
    ]

    for spec, handler, *rest in _SPECS:
        # 第三项可选：这个工具自己的超时（tool_run 内层还要等 worker 工具跑完，放宽）
        _register(spec, handler, timeout_s=float(rest[0]) if rest else 30.0)

    # 改设置的那批（MCP / 专岗 / 端点与模型库）：和网页设置页同一套校验，密钥不经过对话
    from .tools_admin_settings import register_settings_tools

    register_settings_tools(svc, settings=_settings, register=_register, need_confirm=_need_confirm)

    logger.info("管理员对话工具已注册：%d 个（其中 %d 个一律要确认）", len(tools.specs(ROLE)), len(CONFIRM_TOOLS))
    return gate
