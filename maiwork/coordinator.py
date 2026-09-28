"""coordinator.py（M3，docs/02 §7、docs/07 §11.4）：主模型协调器。

主模型不亲自干长活：它计划（确认完成标准、拆 1–2 个子任务）、分派给子 agent、
验收子 agent 交回的证据（必要时用只读工具核对）、决定交付方式。子 agent 不能
宣布任务完成，只有协调器能把任务改成 completed。

红线（docs/02 §7.2、AGENTS.md 设计红线）：
- 每个工作区同一时刻只有一个主模型回合（asyncio.Lock per workspace）。
- 子 agent 并发受 [environments] max_parallel 限制（asyncio.Semaphore per workspace）。
- 真实验收：deliver_kind != "text" 时，artifact 必须在工作区里真实存在，
  否则视为不通过。
- 取消后晚到的结果不复活：accept_result=False 时只记历史，不交付、不改状态。
- 模型/执行环境故障 → 任务 failed + report_error（同群同错 10 分钟一次）。
- 主模型的工具调用也经 Tools.call 落库（actor="主模型"）。
- 工具角色（roles）：主模型挑活时给的「子 agent 工具名单」按 roles 现查注册表
  （worker_job_tool_names），roles 含 main 的 MCP 工具不在里面；排计划回合
  （main_plan_tool_specs，只读：roles 含 main 的 MCP 工具 + list_skills / read_skill）
  和验收回合（main_review_tool_specs）会带上 roles 含 main 的 MCP 工具。
- 群空间（tools_groupspace.py，roles={"main"}）：验收通过、交付之前，如果 app 有
  group_space 且这个群 capabilities 里有任一能力为真，给主模型开一个「群空间」小回合
  （最多 4 轮工具调用，只给这个群能力允许的那几个工具）。只有任务明确需要时才动
  （整理群文件 / 发公告 / 传相册）；不需要就不调用模型（能力全 False 更是零模型调用）。

执行环境（docs/02 §9、docs/09 §7、docs/07 §11.1b）：
- 主模型按计划 JSON 的 "env" 选「本机隔离环境」（local，默认）还是「railway.new
  一次性 VM」（railway）。快、便宜、能直接交付的活走 local；要装一堆依赖、跑不信任
  的第三方代码、要 root/docker、要很多内存或长时间、要干净系统时选 railway。
- 选 railway：acquire(job_id=task_id) 拿一台（同时只 1 台、每天有限额；拿不到就回落
  local 并在任务 env 字段 / 时间线写明原因）；拿到后子 agent 工具集用
  vm_run/vm_put_file/vm_read_file/vm_fetch_file 替代本机命令工具（read_file /
  write_file / list_files 保留，用来写脚本、收成品）。
- 成品最后必须用 vm_fetch_file 拷回本机工作区 artifacts/<task_id>/ 才算数——验收
  和交付永远只看本机工作区。
- 任务结束（成功 / 失败 / 取消 / 异常）一定 release（try/finally）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit

from . import clock, compaction
from .host import HostError
from .models import ModelError
from .outbox import report_error as _report_error
from .store import Store
from .tools import ToolContext

logger = logging.getLogger("maiwork.coordinator")

_MAX_ATTEMPTS = 3
_PLAN_TOOL_LIMIT = 6  # 排计划阶段主模型最多用 6 轮只读工具（查资料 / 读 skill）
_REVIEW_TOOL_LIMIT = 6  # 验收阶段主模型最多用 6 轮只读工具
_REMEMBER_TOOL_LIMIT = 2  # 「记经验」小回合最多 2 次工具调用（验收通过、交付之前）
_GROUPSPACE_TOOL_LIMIT = 4  # 群空间小回合最多 4 轮工具调用（验收通过、交付之前）
_TEXT_DELIVER_FALLBACK_NOTE = "做好了，请查收"

# 群空间工具（tools_groupspace.py，roles={"main"}）→ 这个群要具备的能力键
# （platforms/qq_onebot.py 的 capabilities_async 返回那本字典）。
# 只有这个群能力为 True 的工具才给主模型；全 False → 连模型都不叫（零额外开销）。
_GROUPSPACE_TOOL_CAPS = (
    ("group_files_list", "files_list"),
    ("group_file_manage", "files_manage"),
    ("group_notice_send", "notice_send"),
    ("group_album_upload", "album_upload"),
)
_GROUPSPACE_TOOLS = tuple(name for name, _cap in _GROUPSPACE_TOOL_CAPS)

_AGENT_DONE_WORD_HINT = "目标完成"

# 目标检查带的群聊上下文（2026-10）：最近 48 小时、最多 40 条、每条截 80 字
_GOAL_CHAT_H = 48
_GOAL_CHAT_N = 40
_GOAL_CHAT_TEXT = 80

# ---------------------------------------------------------------------------
# 调研类子任务 + 验收引用核对（2026-10）
# ---------------------------------------------------------------------------

# 计划里子任务没给 type 时的关键词兜底：brief 里出现这些词就当调研/对比/盘点类。
# 偏保守：做东西（写页面、跑脚本）的 brief 一般不带这些词；带了也顶多多一段提示，不影响干活。
_RESEARCH_JOB_WORDS = (
    "调研", "调查", "对比", "盘点", "汇总", "综述", "怎么看", "口碑",
    "评测", "现状", "梳理", "搜集",
)
# 验收核对链接时扫的文本成品后缀（PDF/图片等二进制一律跳过）
_TEXT_ARTIFACT_SUFFIXES = (
    ".md", ".markdown", ".txt", ".html", ".htm", ".json", ".csv", ".tsv",
    ".yaml", ".yml", ".rst", ".org", ".xml", ".svg", ".vtt", ".srt",
)
_ARTIFACT_SCAN_MAX_FILES = 20          # 最多扫 20 个成品文件
_ARTIFACT_SCAN_MAX_BYTES = 200_000     # 每个文件最多读 200KB
_UNOPENED_URLS_IN_PROMPT = 10          # 喂给验收模型的「没打开」清单最多列 10 条
_LINK_RE = re.compile(r"https?://[^\s<>\"'()\[\]{}，。；、）】》]+", re.IGNORECASE)
# 跟踪参数：utm_* 前缀 + 这些常见名字（比对时两边都去掉）
_TRACKING_PARAM_NAMES = frozenset({
    "fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref_src", "spm",
    "si", "share_token", "share_source", "share_medium",
})


def _is_tracking_param(name: str) -> bool:
    low = str(name or "").strip().lower()
    return low.startswith("utm_") or low in _TRACKING_PARAM_NAMES


def normalize_link_for_check(url: str) -> str:
    """链接比对用的规范化：去 fragment、末尾 /、utm_* 等跟踪参数；http/https 视同；主机名小写。

    解析不了（不是 http(s)、没有主机名、端口非法）返回 ""。
    """
    raw = str(url or "").strip()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError:
        return ""
    if port and port not in (80, 443):
        host = f"{host}:{port}"
    path = parsed.path.rstrip("/")
    pairs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not _is_tracking_param(k)]
    query = urlencode(pairs)
    return f"{host}{path}{('?' + query) if query else ''}"


def extract_http_links(text: str) -> list[str]:
    """从一段文本里抽 http(s) 链接（按出现顺序、去重按规范化结果）。"""
    out: list[str] = []
    seen: set[str] = set()
    for match in _LINK_RE.finditer(str(text or "")):
        url = match.group(0).rstrip(".,;:!?、。，；：）)】」』>\"'")
        key = normalize_link_for_check(url)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(url)
    return out


def looks_like_research_brief(brief: str) -> bool:
    """计划没标 type 时的兜底：brief 里带调研/对比/盘点这类词就当调研。"""
    text = str(brief or "")
    return any(word in text for word in _RESEARCH_JOB_WORDS)

# 本机命令类工具（选 railway 时从子 agent 工具名单里换成 vm_*）
_LOCAL_EXEC_TOOLS = ("run_command", "start_process", "check_process", "stop_process")
# railway 一次性机器上给子 agent 的工具（vm_fetch_file 把成品拷回本机工作区）
_VM_TOOLS = ("vm_run", "vm_put_file", "vm_read_file", "vm_fetch_file")
# vm_* 之外，本机工作区文件工具在 railway 上也保留（写脚本、收成品）
_RAILWAY_KEEP_LOCAL = ("read_file", "write_file", "list_files")
# 专用机器（用户自己的 VPS / VM，environments/ssh.py）上给子 agent 的工具
_MACHINE_TOOLS = ("machine_run", "machine_put_file", "machine_read_file", "machine_fetch_file")


# 主模型验收回合固定给的两个只读核对工具（tools_exec 注册，roles={"main"}）
_REVIEW_TOOLS = ("inspect_file", "inspect_files")
# 排计划回合固定给的两个读 skill 工具（skills_tools 注册，roles={"main", "worker"}；
# 主模型经 ToolContext(role="main") 调，内容只给 roles 含 main 的 skill）
_PLAN_SKILL_TOOLS = ("list_skills", "read_skill")
# 兜底：注册表里一个子 agent 工具都查不到时（空注册表 / 老测试），按老文案列这九个
_BUILTIN_WORKER_TOOLS = (
    "read_file",
    "write_file",
    "list_files",
    "run_command",
    "start_process",
    "check_process",
    "stop_process",
    "read_chat_history",
    "search_memory",
)


def worker_job_tool_names(tools: Any) -> list[str]:
    """给主模型挑活用的「子 agent 工具名单」：注册表里 roles 含 worker 的工具名。

    按注册表判角色（不再手写死名单）：roles 含 main 的（群空间工具、只给主模型的
    MCP 工具）不会出现；`submit_result` 是子 agent 交回的固定通道（workers.run
    每次自动补上），也不列进候选名单；vm_*（railway 专用）只在选中 railway 时由
    _railway_job_tools 换上，同样不预先列出来。
    """
    names: list[str] = []
    try:
        specs = tools.specs("worker")
    except Exception:
        logger.exception("查子 agent 工具名单出错，退回内置名单")
        return list(_BUILTIN_WORKER_TOOLS)
    for spec in specs:
        fn = spec.get("function") if isinstance(spec, dict) else None
        name = str((fn or {}).get("name") or "").strip()
        if name and name != "submit_result" and name not in _VM_TOOLS and name not in names:
            names.append(name)
    return names or list(_BUILTIN_WORKER_TOOLS)


def main_review_tool_specs(tools: Any) -> list[dict]:
    """主模型验收回合的工具表：两个只读核对工具 + roles 含 main 的 MCP 工具。

    MCP 的 roles 在这里真生效：roles 含 main 的主模型能用（验收时核对成品）、
    只含 worker 的只给子 agent。群空间工具 / remember 有自己的专门回合，不混进来。
    """
    names = list(_REVIEW_TOOLS)
    try:
        specs = tools.specs("main")
    except Exception:
        logger.exception("查主模型工具表出错，这次只给只读核对工具")
        specs = []
    for spec in specs:
        fn = spec.get("function") if isinstance(spec, dict) else None
        name = str((fn or {}).get("name") or "").strip()
        if name.startswith("mcp_") and name not in names:
            names.append(name)
    return tools.specs("main", names)


def main_plan_tool_specs(tools: Any) -> list[dict]:
    """主模型「排计划」回合的工具表：list_skills / read_skill（只在有 roles 含 main 的
    skill 时给）+ roles 含 main 的 MCP 工具。两样都没有 → 空表，排计划照旧一次纯 JSON 调用。

    只给查资料 / 读 skill 用的工具：群空间工具、remember、inspect_file(s)、exec 工具
    都有各自的回合或用途，不混进排计划（工具只用来查，不拿来把活干了）。
    MCP 的 roles 在这里真生效：只含 worker 的 MCP 工具不出现。查注册表出错 → 记日志、
    返回空表（排计划退回「一次 json_mode 调用」的老行为）。
    """
    names = list(_PLAN_SKILL_TOOLS) if _has_main_skills(tools) else []
    try:
        specs = tools.specs("main")
    except Exception:
        logger.exception("查主模型计划工具表出错，这次不给工具")
        return []
    for spec in specs:
        fn = spec.get("function") if isinstance(spec, dict) else None
        name = str((fn or {}).get("name") or "").strip()
        if name.startswith("mcp_") and name not in names:
            names.append(name)
    return tools.specs("main", names)


def _has_main_skills(tools: Any) -> bool:
    """有没有 roles 含 main 的 skill。没有就不给排计划回合 skill 工具——
    否则 list_skills / read_skill 总是注册着，每次排计划都会白白变成多轮工具回合。
    认不出 skill 注册表（测试桩等）→ 按「有」算，交给 Tools 自己决定。"""
    registry = getattr(tools, "skill_registry", None)
    if registry is None:
        return True
    try:
        return bool(registry.list("main"))
    except Exception:
        logger.exception("查主模型 skill 出错，这次排计划不给 skill 工具")
        return False


def _parse_plan_json(text: Any) -> Any:
    """解析计划回合的 JSON 文本（容忍 ```json 代码围栏；带工具时模型爱加）。

    解析不了照旧抛 ValueError，调用方按「不是合法 JSON」处理。
    """
    raw = str(text if text is not None else "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    return json.loads(raw)


def _models_ready(models: Any) -> bool:
    """模型是否配好。没配好时不做任何要模型的事、不报错（02 §12.1、01 R11）。"""
    try:
        settings = models.settings()
        return bool(settings.ready())
    except Exception:
        return False


def _is_unconfigured_error(msg: Any) -> bool:
    """这条错误文本是不是「模型还没配好」类——这类不往群里报错。"""
    return "还没配好" in str(msg or "")


class Coordinator:
    """主模型协调器：每个工作区一把锁，自己负责一个任务的整个生命周期。"""

    def __init__(
        self,
        store: Store,
        models: Any,
        workers: Any,
        tools: Any,
        tasks: Any,
        goals: Any,
        delivery: Any,
        outbox: Any,
        env: Any,
        profiles: Any,
        get_settings: Callable[[], Any],
        *,
        host: Any = None,
        railway: Any = None,
        ssh: Any = None,
        group_space: Any = None,
        identity: Any = None,
        capability: Any = None,
    ) -> None:
        self._store = store
        self._models = models
        self._workers = workers
        self._tools = tools
        self._tasks = tasks
        self._goals = goals
        self._delivery = delivery
        self._outbox = outbox
        self._env = env
        self._profiles = profiles
        self._get_settings = get_settings
        self._host = host
        # 本机执行能力判定（environments/capability.py 的 Decision；None = 老调用/没判定，
        # 按「本机能隔离跑命令」处理，行为和以前一样）。ok=False（受限）时本机不跑命令：
        # 有一次性机器就去机器上做，没有就把话说明白、任务判失败。
        self._capability = capability
        # 一次性 VM 执行环境（RailwayEnv；None / railway=false → 不提供 railway 选项）
        self._railway = railway
        # 专用机器（SshEnv；None / 没配机器 → 不提供 ssh 选项）
        self._ssh = ssh
        # 群空间（platforms.qq_onebot.GroupSpace；None = 没开 / 没就位 → 交付前不折腾群空间）
        self._group_space = group_space
        # 身份与工作记忆（identity.py；AGENTS/记忆注入计划、验收；验收通过后给一次「记经验」小回合）
        self._identity = identity
        # 工作区 → 锁/信号量；只在事件循环里用，懒建立
        self._locks: dict[str, asyncio.Lock] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    # ------------------------------------------------------------------
    # 内部小工具
    # ------------------------------------------------------------------

    def _lock_for(self, workspace: str) -> asyncio.Lock:
        lock = self._locks.get(workspace)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[workspace] = lock
        return lock

    def _semaphore_for(self, workspace: str) -> asyncio.Semaphore:
        sem = self._semaphores.get(workspace)
        if sem is None:
            try:
                max_p = int(self._get_settings().environments.max_parallel)
            except Exception:
                max_p = 2
            sem = asyncio.Semaphore(max(1, max_p))
            self._semaphores[workspace] = sem
        return sem

    def _workspace_name(self, group_id: str) -> str:
        try:
            fn = getattr(self._get_settings(), "workspace_of", None)
            if callable(fn):
                return str(fn(group_id))
        except Exception:
            pass
        return f"g{group_id}"

    def _context_window(self) -> int:
        """主模型上下文窗口（tokens）；取不到配置就用默认 128000。"""
        try:
            settings = self._get_settings()
            return int(getattr(getattr(settings, "models", None), "context_window", None) or 128000)
        except Exception:
            return 128000

    async def _chat_main(
        self,
        messages: list[dict],
        *,
        purpose: str,
        group_id: str = "",
        task_id: str = "",
        tools: Any = None,
        json_mode: bool = False,
        retries: Any = None,
    ) -> Any:
        """主模型统一出口（0.4.0）：调之前先上下文压缩，撞上「上下文超长」裁最旧一段重试一次。

        - 压缩规则见 compaction.py：估算超触发线先截旧 tool 结果，仍超把最老一段总结成
          一条 8 节摘要（summary 失败原样继续，不抛）；
        - 「上下文超长」类错误：裁掉最旧一段再重试一次；其他错误原样抛；
        - 返回 ChatResult（原样）。
        """
        try:
            messages = await compaction.maybe_compact(
                messages,
                models=self._models,
                role="main",
                context_window=self._context_window(),
                purpose=purpose,
                group_id=group_id,
                task_id=task_id,
            )
        except Exception:
            logger.exception("主模型上下文压缩失败（%s），原样继续", purpose)
        kwargs: dict[str, Any] = {
            "group_id": group_id,
            "task_id": task_id,
            "tools": tools,
            "json_mode": json_mode,
        }
        if retries is not None:
            kwargs["retries"] = retries
        result = await compaction.chat_with_retry_on_long_context(
            messages,
            models=self._models,
            role="main",
            purpose=purpose,
            **kwargs,
        )
        # 安全网：这一调用量可能把这任务推过了 [tasks] 的线——超了就立刻 paused，
        # 由 workers / 主循环下一步看到 paused 停手，而不是把这一回合跑完才停。
        if task_id:
            try:
                await self._net_check(str(task_id))
            except Exception:
                logger.exception("安全网巡检出错（任务 %s）", task_id)
        return result

    async def _net_check(self, task_id: str) -> dict | None:
        """安全网：任务超过 [tasks] token_limit / run_seconds 就自动 paused（原因进 paused_reason）。

        逻辑真身在 Tasks.net_check（纯数据、不调模型）；这里包一层异步、不抛：
        - tokens 按「继续那时刻」的基线 kv[task.net_base.<任务ID>] 起算（没基线 = 从 0）；
        - 时长按 resume_ts（没基线 = started_ts / created）；
        - 管理员把任务 queued 恢复时 Tasks.transition 自动清原因、重记基线
          （恢复后 token 从恢复那时刻重新累计、时长重新开始跑）；
        - 返回触发原因 dict（没触发 / 任务不在 running → None）。
        """
        try:
            reason = self._tasks.net_check(task_id)
            if reason:
                logger.info("任务 %s 安全网自动暂停：%s", task_id, reason)
            return reason
        except Exception:
            logger.exception("安全网巡检出错（任务 %s）", task_id)
            return None

    def _artifact_dir(self, task_id: str) -> str:
        return f"artifacts/{task_id}"

    def _deliver_path_in_task_dir(self, ws_name: str, task_id: str, artifact: str) -> Path | None:
        """交付路径闸（插件中心审核整改 6）：成品必须在这个任务的成品目录里。

        - 先用 `_env.resolve` 拿本机绝对路径（越界 / 绝对路径 / 符号链接指出去 → PermissionError）；
        - 再把成品目录 `artifacts/<task_id>/` 也 resolve 出来，要求「解析后的真实路径」等于它
          或位于它下面；`.`、`tasks/...`、`artifacts/别的任务/...`、指到外面的符号链接都过不去。
        - 不满足 → 记日志、返回 None（调用方不交付，任务照常 done，和解析失败一个处理）。
        """
        try:
            path = self._env.resolve(ws_name, artifact)
            base = self._env.resolve(ws_name, self._artifact_dir(task_id))
            real = Path(path).resolve()
            real_base = Path(base).resolve()
        except (PermissionError, ValueError, OSError) as e:
            logger.warning("交付路径解析失败 %s：%s", artifact, e)
            return None
        if real != real_base and real_base not in real.parents:
            logger.warning(
                "交付路径不在本任务的成品目录 %s/ 里，不交付：%s", self._artifact_dir(task_id), artifact
            )
            return None
        return real

    def _artifact_exists(self, workspace_name: str, artifact: str) -> bool:
        try:
            p = self._env.resolve(workspace_name, str(artifact))
        except (PermissionError, ValueError):
            return False
        return bool(p.exists())

    # ------------------------------------------------------------------
    # 执行环境（本机 / railway.new 一次性 VM）
    # ------------------------------------------------------------------

    def _railway_available(self) -> bool:
        """能不能把 railway 当作可选项：railway 环境就位 + [environments] railway 没关。"""
        if self._railway is None:
            return False
        try:
            if not bool(getattr(self._get_settings().environments, "railway", True)):
                return False
        except Exception:
            pass
        return callable(getattr(self._railway, "acquire", None))

    def _ssh_available(self) -> bool:
        """能不能把专用机器当作可选项：SshEnv 就位 + 配了至少一台地址写对的机器。"""
        ssh = getattr(self, "_ssh", None)
        if ssh is None:
            return False
        try:
            return bool(ssh.available())
        except Exception:
            return False

    @staticmethod
    def _is_ssh_box(box: Any) -> bool:
        return str(getattr(box, "kind", "") or "") == "ssh"

    @staticmethod
    def _ssh_env_desc(box: Any) -> str:
        """任务 env 字段（专用机器）：「专用机器 · 名字」。"""
        return f"专用机器 · {getattr(box, 'name', '') or '未命名'}"

    def _remote_env_desc(self, box: Any) -> str:
        return self._ssh_env_desc(box) if self._is_ssh_box(box) else self._railway_env_desc(box)

    @staticmethod
    def _remote_job_tools(requested: list[str], box: Any) -> list[str]:
        """按机器种类换工具：专用机器换 machine_*，一次性 VM 换 vm_*；本机命令工具都去掉。"""
        if str(getattr(box, "kind", "") or "") != "ssh":
            return Coordinator._railway_job_tools(requested)
        out: list[str] = []
        for t in requested:
            if t in _LOCAL_EXEC_TOOLS or t in _VM_TOOLS:
                continue
            if t not in out:
                out.append(t)
        for t in (*_MACHINE_TOOLS, *_RAILWAY_KEEP_LOCAL):
            if t not in out:
                out.append(t)
        return out

    def _env_options(self) -> tuple[str, str, set[str]]:
        """排计划提示词里的 env 选项：(JSON 字段说明, 怎么选的说明, 允许的值)。没就位的不出现。"""
        ssh_ok = self._ssh_available()
        railway_ok = self._railway_available()
        allowed = {"local"} | ({"ssh"} if ssh_ok else set()) | ({"railway"} if railway_ok else set())
        if allowed == {"local"}:
            return ' "env": "local"（本机隔离环境干活），', "", allowed
        opts = "|".join(k for k in ("local", "ssh", "railway") if k in allowed)
        field = f' "env": "{opts}"（在哪干活，下面有说明）， "env_reason": "一句话说清为什么这么选",'
        if ssh_ok:
            field += ' "machine": "选 ssh 时想用哪台专用机器（写名字；无所谓就 null）",'
        lines = ["怎么选 env：只写写文件、查资料、做网页这类不用跑命令的轻活，选 local（本机隔离环境，快，做完能直接交付）。"]
        if ssh_ok:
            names = "、".join(
                (str(m.get("name") or "") + (f"（{m['note']}）" if str(m.get("note") or "").strip() else ""))
                for m in (self._ssh.machines() or []) if "error" not in m
            )
            lines.append(
                f"ssh 是管理员给 MaiWork 准备的专用机器（{names}）：要跑命令、装依赖、编译、跑得久、"
                "要很多内存的活**优先选 ssh**——没有时间限制，一直是同一台；一台机器同时只接一个任务，"
                "都在忙或连不上会自动换别的地方。有好几台时，按上面「做事规矩」（AGENTS.md）里写的"
                "各台机器的情况和用途挑一台，写进 machine。"
            )
        if railway_ok:
            lines.append(
                "railway 是一台一次性机器：60 分钟窗口、2 核 2G、用完即弃；同一时间只有 1 台、每天有限额，"
                "现在不一定拿得到。要跑不信任的第三方代码、要 root 或 docker、要一台干净系统时选它"
                + ("（其余要跑命令的活先选 ssh）。" if ssh_ok else "。")
            )
        lines.append("不管在哪台机器上做，成品最后都必须拷回本机工作区才能交付。")
        return field, "".join(lines), allowed

    def _normalize_env_choice(self, raw: Any) -> str:
        """计划里的 env 规范成可用的值：乱写 → local；要的那种机器没开 → 换另一种机器，都没开 → local。"""
        choice = str(raw or "").strip().lower()
        _f, _g, allowed = self._env_options()
        if choice not in ("local", "ssh", "railway"):
            return "local"
        if choice in allowed:
            return choice
        if choice in ("ssh", "railway"):
            other = "railway" if choice == "ssh" else "ssh"
            return other if other in allowed else "local"
        return "local"

    def _local_can_exec(self) -> bool:
        """本机能不能隔离跑命令（判定 ok；没判定过按能，行为和以前一样）。"""
        return bool(getattr(getattr(self, "_capability", None), "ok", True))

    def _local_env_desc(self) -> str:
        """任务 env 字段（本机）：「本机 · maiwork 用户 · 内存上限 512M」。

        受限（本机不能隔离跑命令）时写明「受限」，别让管理员以为活在本机跑得起来。
        """
        if not self._local_can_exec():
            cap = getattr(self, "_capability", None)
            why = str(getattr(cap, "reason", "") or "这台机器不能隔离跑命令")
            return f"本机 · 受限（不能隔离跑命令：{why}）"
        run_as = "maiwork"
        mem = "512M"
        try:
            run_as = str(getattr(self._get_settings().environments, "run_as", "maiwork") or "maiwork")
        except Exception:
            pass
        try:
            mem = str(getattr(self._get_settings().environments, "memory_max", "512M") or "512M")
        except Exception:
            pass
        return f"本机 · {run_as} 用户 · 内存上限 {mem}"

    @staticmethod
    def _railway_env_desc(box: Any) -> str:
        """任务 env 字段（railway）：「railway.new 一次性机器 · 2 核 2G · 到期 HH:MM」。"""
        expire_txt = ""
        try:
            expires = float(getattr(box, "expires_ts", 0) or 0)
            if expires > 0:
                expire_txt = clock.bj(expires).strftime("%H:%M")
        except Exception:
            expire_txt = ""
        base = "railway.new 一次性机器 · 2 核 2G"
        if expire_txt:
            base += f" · 到期 {expire_txt}"
        return base

    @staticmethod
    def _railway_job_tools(requested: list[str]) -> list[str]:
        """把主模型给的工具名单换成 railway 版：去掉本机命令工具、补 vm_*，保留其它。"""
        out: list[str] = []
        for t in requested:
            if t in _LOCAL_EXEC_TOOLS:
                continue  # 本机命令工具换 vm_*，不留
            if t not in out:
                out.append(t)
        for t in _VM_TOOLS:
            if t not in out:
                out.append(t)
        for t in _RAILWAY_KEEP_LOCAL:  # 本机文件工具（写脚本、收成品）必须在
            if t not in out:
                out.append(t)
        return out

    async def _fetch_acquire_reason(self) -> str:
        """从 railway 环境拿一句「为什么没拿到机器」（last_fail），拿不到就给兜底话。"""
        reason = ""
        try:
            lf = self._railway.last_fail() if self._railway is not None else None
            if isinstance(lf, dict):
                reason = str(lf.get("reason") or "").strip()
        except Exception:
            reason = ""
        return reason or "配额用完或现在同时已经有机器在用"

    @staticmethod
    def _find_artifact_symlink_escape(artifact_path: Any) -> str | None:
        """交付前递归检查 artifact（文件或目录）里的符号链接逃逸（S3）。

        - artifact 本身是符号链接 → 返回问题说明；
        - 目录里的任一符号链接 resolve 后指到 artifact 目录外 → 返回问题说明；
        - 目录内互指的符号链接（resolve 后仍在目录内）也返回问题说明——发布打包
          本来就会跳过所有符号链接，交一个带链接的东西给群友没意义。
        干净返回 None。
        """
        try:
            root = Path(artifact_path)
        except Exception:
            return "成品路径无效"
        try:
            if root.is_symlink():
                return f"成品 {root.name} 是符号链接，按验收不通过处理"
        except OSError:
            return None
        try:
            if not root.is_dir():
                return None  # 常规单文件
            base = root.resolve()
        except OSError:
            return None
        try:
            for p in root.rglob("*"):
                is_link = False
                try:
                    is_link = p.is_symlink()
                except OSError:
                    continue
                if not is_link:
                    continue
                try:
                    resolved = p.resolve()
                except OSError:
                    resolved = None
                if resolved is None:
                    return f"成品里的符号链接 {p.relative_to(root)} 解析失败，按验收不通过处理"
                return f"成品里混着符号链接 {p.relative_to(root)}，按验收不通过处理"
        except OSError:
            return None
        return None

    # ------------------------------------------------------------------
    # 主模型 JSON 调用（计划 / 验收）
    # ------------------------------------------------------------------

    def _identity_prefix(self, gid: str, *, with_memory: bool) -> str:
        """AGENTS（做事规矩）+ 可选工作记忆；没 identity / 空 → ""。主模型提示词的最前面。"""
        identity = self._identity
        if identity is None:
            return ""
        parts: list[str] = []
        try:
            block = identity.prompt_block("agents")
            if block:
                parts.append(str(block).rstrip("\n"))
        except Exception:
            pass
        if with_memory:
            try:
                block = identity.prompt_block("memory", group_id=gid)
                if block:
                    parts.append(str(block).rstrip("\n"))
            except Exception:
                pass
        return ("\n\n".join(parts) + "\n\n") if parts else ""

    async def _plan(self, task: dict, prior_review: str = "") -> dict:
        """主模型计划：决定 criteria / deliver_kind / jobs / question。"""
        gid = str(task["group_id"])
        entries = []
        try:
            entries = (self._profiles.entries(gid) or [])[:20]
        except Exception:
            entries = []
        profile_lines = [f"- {e.get('text', '')}" for e in entries]

        prompt_lines = [
            "你是 MaiWork 的主模型。这是一个 QQ 群派的活：",
            f"任务标题：{task['title']}",
            "",
            "群友提的原始需求（req）：",
            str(task["req"] or "").strip() or "（空）",
            "",
            "当前完成标准（criteria）：",
        ]
        crit = self._safe_json_list(task.get("criteria"))
        if crit:
            for c in crit:
                prompt_lines.append(f"- {c}")
        else:
            prompt_lines.append("（空，这次必须给出；完成标准要能验收，不要写「做完了」这种）")
        if profile_lines:
            prompt_lines.append("")
            prompt_lines.append("群画像要点（供你判断时参考，不要点名任何群友）：")
            prompt_lines.extend(profile_lines)
        if prior_review:
            prompt_lines.append("")
            prompt_lines.append(f"上一次验收意见：{prior_review}")

        # 执行环境可选项：只有就位的才出现在提示词里（模型不会瞎选）
        env_field, env_guide, _env_allowed = self._env_options()
        prompt_lines.append("")
        prompt_lines.append(
            "只回 JSON，不要输出别的："
            '{"criteria": ["完成标准 1", "…"],'
            ' "deliver_kind": "view|file|text"（view=做成网页给人打开看；file=做成文件给人下载/编辑；text=不用成品，直接在群里文字回复）,'
            + env_field
            + ' "jobs": [{"brief": "派给一个子 agent 的具体活，要写清楚要做什么、写到 artifacts/<任务ID>/ 下；'
            '展示类做成单页 index.html（手机能看、不依赖外部资源）",'
            ' "type": "research|build|other"（research=要查资料出结论的活：调研、对比、盘点、「大家怎么看」、找现状/口碑；'
            'build=做东西；other=其它）,'
            ' "tools": ["子 agent 工具名单里的名字"]}]（1 到 2 个）,'
            ' "question": null | "如果信息不够、不能开工，写一句要在群里问发起人的话；能开工就是 null"}'
        )
        if env_guide:
            prompt_lines.append(env_guide)
        prompt_lines.append(
            "（子 agent 工具名单：" + " / ".join(worker_job_tool_names(self._tools)) + "；"
            "只能从这里挑，别多要）"
        )

        prefix = self._identity_prefix(gid, with_memory=True)
        tid = str(task["id"])
        ws_name = str(task.get("workspace") or self._workspace_name(gid))
        specs = main_plan_tool_specs(self._tools)
        if not specs:
            # 一个排计划能用的工具都没有（roles 含 main 的 MCP / skill 工具全没注册）：
            # 行为完全不变——一次 json_mode=True 的纯 JSON 调用，不带 tools。
            result = await self._chat_main(
                [{"role": "user", "content": prefix + "\n".join(prompt_lines)}],
                json_mode=True,
                purpose="coordinator.plan",
                group_id=gid,
                task_id=tid,
            )
            try:
                data = json.loads(result.text)
            except (ValueError, TypeError) as e:
                raise ModelError(f"主模型计划返回不是合法 JSON：{e}") from None
        else:
            prompt_lines.append(
                "你可以先用这些工具查资料 / 读 skill 再定计划；工具只用来查，"
                "不要用它们直接把活干了；查完只回上面的 JSON。"
            )
            messages: list[dict] = [
                {"role": "user", "content": prefix + "\n".join(prompt_lines)}
            ]
            try:
                ws_path = self._env.workspace(ws_name)
            except Exception:
                ws_path = None
            ctx = ToolContext(
                group_id=gid, task_id=tid, actor="主模型", workspace=ws_path, role="main"
            )
            data = None
            for _round in range(_PLAN_TOOL_LIMIT):
                result = await self._chat_main(
                    messages,
                    tools=specs,
                    json_mode=False,
                    purpose="coordinator.plan",
                    group_id=gid,
                    task_id=tid,
                )
                tool_calls = result.tool_calls or []
                if tool_calls:
                    # 工具只用来查：结果作为 tool 消息回给模型，接着下一轮
                    await self._run_tool_calls(
                        messages, tool_calls, ctx, assistant_text=result.text or ""
                    )
                    continue
                try:
                    parsed = _parse_plan_json(result.text)
                except (ValueError, TypeError):
                    # 同验收：再等一轮提示一下「请只回 JSON」
                    messages.append({"role": "assistant", "content": result.text})
                    messages.append(
                        {
                            "role": "user",
                            "content": "请只回 JSON，字段按上面说的来；不要再调用工具。",
                        }
                    )
                    continue
                if isinstance(parsed, dict):
                    data = parsed
                    break
                continue
            if data is None:
                # 用满 _PLAN_TOOL_LIMIT 轮还在调工具 / 一直不给 JSON：
                # 最后强制一次（tools=None + json_mode=True）把 JSON 拿回来。
                result = await self._chat_main(
                    messages,
                    tools=None,
                    json_mode=True,
                    purpose="coordinator.plan",
                    group_id=gid,
                    task_id=tid,
                )
                try:
                    data = _parse_plan_json(result.text)
                except (ValueError, TypeError) as e:
                    raise ModelError(f"主模型计划返回不是合法 JSON：{e}") from None
        if not isinstance(data, dict):
            raise ModelError("主模型计划返回不是 JSON 对象")

        criteria = data.get("criteria")
        if not isinstance(criteria, list):
            criteria = []
        criteria = [str(c).strip() for c in criteria if str(c).strip()]
        if not criteria and not crit:
            # 原本为空而这次也没给 → 必须给（§11.4：原来为空时必须给）
            raise ModelError("主模型计划没给完成标准，任务原本又没有，没法验收")
        if not criteria:
            criteria = crit  # 保留原来的

        deliver_kind = str(data.get("deliver_kind") or "").strip()
        if deliver_kind not in ("view", "file", "text"):
            deliver_kind = "file"

        jobs_raw = data.get("jobs")
        jobs: list[dict] = []
        if isinstance(jobs_raw, list):
            for j in jobs_raw[:2]:
                if not isinstance(j, dict):
                    continue
                brief = str(j.get("brief") or "").strip()
                if not brief:
                    continue
                tools_list = [str(x) for x in (j.get("tools") or []) if str(x).strip()]
                # 群空间工具（roles={"main"}）是主模型自己用的，子 agent 名单里一律不许出现
                tools_list = [t for t in tools_list if t not in _GROUPSPACE_TOOLS]
                # 子任务类型：计划给了就认 research/build/other；没给或乱给按关键词兜底
                job_type = str(j.get("type") or "").strip().lower()
                if job_type not in ("research", "build", "other"):
                    job_type = "research" if looks_like_research_brief(brief) else "other"
                jobs.append({"brief": brief, "tools": tools_list, "type": job_type})

        question = data.get("question")
        question = str(question).strip() if question else ""

        env_choice = self._normalize_env_choice(data.get("env"))
        machine = str(data.get("machine") or "").strip()[:64] if env_choice == "ssh" else ""
        env_reason = str(data.get("env_reason") or "").strip()

        return {
            "criteria": criteria,
            "deliver_kind": deliver_kind,
            "jobs": jobs,
            "question": question,
            "env": env_choice,
            "machine": machine,
            "env_reason": env_reason,
            # 有子任务被标成调研类 → 验收时做引用核对（交付里没链接就不加那段，见 _review）
            "research": any(j["type"] == "research" for j in jobs),
        }

    # ------------------------------------------------------------------
    # run_task
    # ------------------------------------------------------------------

    async def run_task(self, task_id: str) -> None:
        """一条任务的完整生命周期。

        任务不是 queued → 直接返回。每个工作区一把锁，同一时刻一个主模型回合。
        模型没配好：保持 queued 直接返回（不转 running、不开尝试、不报错，02 §12.1）。
        """
        # 每次循环都重查 settings（配置热更新后就绪了会自然开工）
        task = self._tasks.get(task_id)
        if task is None:
            return
        if str(task["status"]) != "queued":
            return
        if not _models_ready(self._models):
            logger.debug("模型还没配好，任务 %s 保持排队，不开工", task_id)
            return
        ws_name = str(task.get("workspace") or self._workspace_name(task["group_id"]))
        async with self._lock_for(ws_name):
            while True:
                task = self._tasks.get(task_id)
                if task is None:
                    return
                # 取消（或其它终态）立刻停：不再计划、不再派活、不再交付、不往群里
                # 发任何东西——线上踩过「网页取消了，子 agent 还跑了 40 秒」
                if str(task["status"]) in ("cancelled", "completed", "failed", "rejected"):
                    logger.info("任务 %s 已是「%s」，协调器立刻停", task_id, task["status"])
                    return
                if str(task["status"]) != "queued":
                    return
                if not _models_ready(self._models):
                    logger.debug("跑任务途中模型配置没了，任务 %s 保持排队，下次巡检再说", task_id)
                    return
                action = await self._run_one_attempt(task)
                if action == "retry":
                    continue
                return

    async def _run_one_attempt(self, task: dict) -> str:
        """跑一轮：计划 → 执行 → 验收 → 通过交付 / 不通过重来或失败。

        返回 "retry"（再跑一轮） / "done"（完结，不再跑）。
        """
        tid = str(task["id"])
        gid = str(task["group_id"])
        ws_name = str(task.get("workspace") or self._workspace_name(gid))

        # transition → running + start_attempt
        try:
            self._tasks.transition(tid, "running", reason="协调器开工")
        except ValueError as e:
            logger.warning("任务 %s →running 非法：%s", tid, e)
            return "done"
        attempt_n = self._tasks.start_attempt(tid)
        attempt_id = self._tasks.current_attempt_id(tid)
        req_version = int(self._tasks.get(tid)["req_version"]) if self._tasks.get(tid) else 1

        review_text = ""
        try:
            # 最近一次历史评审意见（给下一轮计划参考）
            review_text = str(self._tasks.get(tid).get("review") or "")
            plan = await self._plan(self._tasks.get(tid), prior_review=review_text)
        except (ModelError, HostError) as e:
            self._fail_with_err(tid, attempt_id, f"主模型计划失败：{getattr(e, 'message', e)}", gid)
            return "done"
        except Exception as e:
            logger.exception("任务 %s 计划阶段异常", tid)
            self._fail_with_err(tid, attempt_id, f"计划失败：{e}", gid)
            return "done"

        # 安全网可能在计划回合把任务 paused 了：别再往下走 wait/重排
        _now = self._tasks.get(tid)
        if _now is None or str(_now.get("status") or "") not in ("running", "reviewing"):
            logger.info("任务 %s 计划后状态已变（预案里被暂停或终态），停", tid)
            return "done"

        # question → waiting_input + 群里问一句
        if plan["question"]:
            try:
                self._tasks.transition(
                    tid,
                    "waiting_input",
                    reason="缺信息",
                    question=plan["question"],
                    question_ts=clock.now(),
                )
            except ValueError as e:
                logger.warning("任务 %s →waiting_input 非法：%s", tid, e)
                return "done"
            text = plan["question"]
            requester = str(task.get("requester_name") or "").strip()
            if requester:
                text = f"@{requester} {text}"
            try:
                self._outbox.enqueue(
                    f"ask:{tid}:{attempt_n}",
                    gid,
                    "text",
                    {"text": text, "push_kind": "status"},
                    task_id=tid,
                )
            except Exception:
                logger.exception("入队提问失败")
            self._tasks.finish_attempt(
                attempt_id,
                status="waiting",
                summary=f"缺信息，等发起人回答：{plan['question']}",
            )
            self._write_tokens(tid)
            return "done"

        # 空 jobs → 视作没派活，直接判失败（不能「验收空气」）
        jobs = plan["jobs"]
        if not jobs:
            self._tasks.finish_attempt(
                attempt_id, status="failed", summary="", review="主模型没派活"
            )
            return self._handle_unpassed(
                tid, attempt_n, gid, "主模型没派活，无法开工", plan["deliver_kind"]
            )

        # 更新 criteria（如果主模型给了新的）
        self._persist_criteria(tid, plan["criteria"])

        # 执行环境：主模型选 railway → 拿一台一次性机器（拿不到回落本机并在 env/时间线写清原因）
        on_railway, railway_box = await self._setup_exec_env(tid, plan, gid)

        # 本机不能隔离跑命令、也没拿到一次性机器：不假装开工，给一句实在话就收
        # （_setup_exec_env 已把原因写进 env 字段和时间线）
        if not on_railway and railway_box is None and not self._local_can_exec():
            cap = getattr(self, "_capability", None)
            why = str(getattr(cap, "reason", "") or "这台机器不能隔离跑命令")
            msg = f"本机不能隔离跑命令（{why}），专用机器和一次性机器也都没拿到：这个任务做不了"
            logger.warning("任务 %s %s", tid, msg)
            self._fail_with_err(tid, attempt_id, msg, gid)
            return "done"

        # 执行 jobs 并发（受信号量）；结束（成功/失败/异常）一定 release 一次性机器
        try:
            reports: list[Any] = await asyncio.gather(
                *[
                    self._run_job(
                        brief=self._enrich_brief(j["brief"], tid, plan["deliver_kind"], railway_box if on_railway else False),
                        tools=self._remote_job_tools(j["tools"], railway_box) if on_railway else j["tools"],
                        gid=gid,
                        tid=tid,
                        job_idx=i + 1,
                        ws_name=ws_name,
                        job_type=str(j.get("type") or "other"),
                    )
                    for i, j in enumerate(jobs)
                ]
            )
        finally:
            await self._release_remote(railway_box)

        # 每个 job 返回后先 accept_result：False → 只记历史，结束
        if not self._tasks.accept_result(tid, attempt_id, req_version):
            self._tasks.finish_attempt(
                attempt_id,
                status="stale",
                summary="；".join(r.summary for r in reports if r and r.summary)[:500],
                evidence=[e for r in reports if r for e in (r.evidence or [])],
            )
            return "done"

        # 子 agent 都交回了，但任务可能刚被取消 / 被安全网暂停：终态和暂停都立刻停，
        # 不再验收、不交付、不往群里发（安全网恢复后 run_task 会从 queued 重新开工）
        task_now = self._tasks.get(tid)
        if task_now is not None and str(task_now["status"]) in (
            "cancelled", "completed", "failed", "rejected", "paused", "waiting_input", "shelved",
        ):
            logger.info("任务 %s 已是「%s」，不验收不交付", tid, task_now["status"])
            return "done"

        # 汇总 summary / evidence
        summary_parts = []
        evidence: list[str] = []
        for r in reports:
            if r is None:
                continue
            if r.summary:
                summary_parts.append(str(r.summary))
            evidence.extend([str(x) for x in (r.evidence or [])])
        summary = "；".join(summary_parts)[:500]

        # transition → reviewing
        try:
            self._tasks.transition(tid, "reviewing", reason="子 agent 交回")
        except ValueError as e:
            logger.warning("任务 %s →reviewing 非法：%s", tid, e)
            return "done"

        # 验收
        try:
            review = await self._review(
                self._tasks.get(tid), plan, summary, evidence, reports
            )
        except (ModelError, HostError) as e:
            self._fail_with_err(
                tid, attempt_id, f"验收失败：{getattr(e, 'message', e)}", gid
            )
            return "done"
        except Exception as e:
            logger.exception("任务 %s 验收阶段异常", tid)
            self._fail_with_err(tid, attempt_id, f"验收失败：{e}", gid)
            return "done"

        _now2 = self._tasks.get(tid)
        if _now2 is None or str(_now2.get("status") or "") not in ("running", "reviewing"):
            logger.info("任务 %s 验收后状态已变（安全网暂停或终态），不再写结果", tid)
            return "done"

        # 汇总 attempt 结果先写（无论过不过）
        self._tasks.finish_attempt(
            attempt_id,
            status="passed" if review["pass"] else "failed",
            summary=summary,
            evidence=evidence,
            artifacts=[review.get("artifact") or ""],
            review=review["review"],
        )

        if review["pass"]:
            return await self._handle_passed(
                tid, gid, ws_name, plan, review
            )
        return self._handle_unpassed(
            tid, attempt_n, gid, review["review"], plan["deliver_kind"]
        )

    # ------------------------------------------------------------------
    # 执行 jobs
    # ------------------------------------------------------------------

    async def _run_job(
        self, *, brief: str, tools: list[str], gid: str, tid: str, job_idx: int, ws_name: str,
        job_type: str = "other",
    ) -> Any:
        sem = self._semaphore_for(ws_name)
        async with sem:
            try:
                ws_path = self._env.workspace(ws_name)
            except Exception as e:
                logger.exception("拿工作区失败 %s", ws_name)
                from .workers import WorkerReport

                return WorkerReport(ok=False, summary="", error=f"拿不到工作区：{e}")
            # 调研类子任务：system 提示追加报告框架（做东西的活不加）
            system_extra = ""
            if str(job_type) == "research":
                try:
                    from .workers import RESEARCH_REPORT_FRAMEWORK

                    system_extra = RESEARCH_REPORT_FRAMEWORK
                except Exception:
                    logger.exception("取调研报告框架失败")
            try:
                return await self._workers.run(
                    brief,
                    group_id=gid,
                    tools=tools,
                    task_id=tid,
                    actor=f"子 agent #{job_idx}",
                    workspace=ws_path,
                    system_extra=system_extra,
                )
            except (ModelError, HostError) as e:
                from .workers import WorkerReport

                return WorkerReport(ok=False, summary="", error=f"子 agent 调用失败：{getattr(e, 'message', e)}")
            except Exception as e:
                logger.exception("子 agent #%s 执行异常", job_idx)
                from .workers import WorkerReport

                return WorkerReport(ok=False, summary="", error=f"子 agent 执行异常：{e}")

    # ------------------------------------------------------------------
    # 执行环境：选 railway → 拿一台一次性机器（回落本机写清原因）
    # ------------------------------------------------------------------

    async def _setup_exec_env(self, tid: str, plan: dict, gid: str) -> tuple[bool, Any]:
        """决定这轮在哪干 + 把任务 env 字段写好。返回 (跑在别的机器上?, 拿到的机器 | None)。

        机器有两种：专用机器（ssh，用户自己的 VPS / VM）和一次性 VM（railway）。
        - 选 local 且本机能隔离跑命令：就在本机（不碰机器）；
        - 选 ssh：专用机器 → 一次性 VM → 本机；
        - 选 railway：一次性 VM → 专用机器 → 本机；
        - 选 local 但本机受限（不能隔离跑命令）：专用机器 → 一次性 VM → 做不了。
        换了地方都在 env 字段 / 时间线写清原因；最后都拿不到且本机受限，返回 (False, None)，
        调用方据此判失败，不假装开工。
        """
        want = str(plan.get("env") or "local")
        local_ok = self._local_can_exec()
        if want not in ("ssh", "railway") and local_ok:
            try:
                self._tasks.set_env(tid, self._local_env_desc())
            except Exception:
                logger.exception("写任务 %s 的 env 字段失败", tid)
            return False, None
        order = ["railway", "ssh"] if want == "railway" else ["ssh", "railway"]
        labels = {"ssh": "专用机器", "railway": "一次性机器"}
        reasons: list[str] = []
        for kind in order:
            box, reason = await self._acquire_remote(kind, tid, str(plan.get("machine") or "").strip())
            if box is None:
                if reason:
                    reasons.append(f"{labels[kind]}：{reason}")
                continue
            if not local_ok and want not in ("ssh", "railway"):
                cap = getattr(self, "_capability", None)
                why = str(getattr(cap, "reason", "") or "这台机器不能隔离跑命令")
                note = f"不能隔离跑命令，改在{labels[kind]}上做：{why}"
            elif kind != want:
                note = f"{labels.get(want, '指定的机器')}拿不到，改在{labels[kind]}上做：" + "；".join(reasons)
            else:
                note = ""
            try:
                self._tasks.set_env(tid, self._remote_env_desc(box), note=note)
            except Exception:
                logger.exception("写任务 %s 的 env 字段失败", tid)
            return True, box
        why_all = "；".join(reasons)
        if local_ok:
            note = f"{labels.get(want, '指定的机器')}拿不到，改在本机做：{why_all or '现在没有可用的机器'}"
        else:
            cap = getattr(self, "_capability", None)
            why = str(getattr(cap, "reason", "") or "这台机器不能隔离跑命令")
            if why_all:
                note = f"不能隔离跑命令，别的机器也没拿到，这个活做不了：{why}；{why_all}"
            else:
                note = f"不能隔离跑命令，也没开专用机器或一次性机器，这个活做不了：{why}"
        try:
            self._tasks.set_env(tid, self._local_env_desc(), note=note)
        except Exception:
            logger.exception("写任务 %s 的 env 字段失败", tid)
        return False, None

    async def _acquire_remote(self, kind: str, tid: str, prefer: str = "") -> tuple[Any, str]:
        """申请一台机器：返回 (机器 | None, 没拿到的原因)。没开这种机器 → (None, 原因或空)。"""
        if kind == "ssh":
            if not self._ssh_available():
                return None, ""
            try:
                box = await (self._ssh.acquire(tid, prefer=prefer) if prefer else self._ssh.acquire(tid))
            except Exception:
                logger.exception("申请专用机器出错（任务 %s）", tid)
                box = None
            if box is not None:
                return box, ""
            try:
                lf = self._ssh.last_fail()
                reason = str((lf or {}).get("reason") or "")
            except Exception:
                reason = ""
            return None, reason or "都连不上或都在忙"
        if not self._railway_available():
            return None, "现在没开 railway（配置关了或环境没就位）" if getattr(self, "_railway", None) is not None else ""
        try:
            box = await self._railway.acquire(tid)
        except Exception:
            logger.exception("申请一次性机器出错（任务 %s）", tid)
            box = None
        if box is not None:
            return box, ""
        return None, await self._fetch_acquire_reason()

    async def _stopped_local_fallback(self, tid: str) -> tuple[bool, Any]:
        """本机受限时的回落（老入口，等同计划选 local 时的 _setup_exec_env）。"""
        return await self._setup_exec_env(tid, {"env": "local"}, "")

    async def _release_remote(self, box: Any) -> None:
        """结束（成功 / 失败 / 取消 / 异常）一定释放机器；自身不再抛错。"""
        if box is None:
            return
        if self._is_ssh_box(box):
            ssh = getattr(self, "_ssh", None)
            if ssh is None:
                return
            try:
                await ssh.release(box)
            except Exception:
                logger.exception("释放专用机器出错（任务跑完兜底）")
            return
        await self._release_railway(box)

    async def _release_railway(self, box: Any) -> None:
        """结束（成功 / 失败 / 取消 / 异常）一定释放一次性机器；自身不再抛错。"""
        if box is None or self._railway is None:
            return
        try:
            await self._railway.release(box)
        except Exception:
            logger.exception("释放一次性机器出错（任务跑完兜底）")

    def _enrich_brief(self, brief: str, tid: str, deliver_kind: str, on_railway: bool = False) -> str:
        out = str(brief)
        target_dir = self._artifact_dir(tid)
        out += (
            f"\n\n成品放在工作区 {target_dir}/ 下；"
        )
        if deliver_kind == "view":
            out += "展示类成品做成单页 index.html（手机能看、不依赖外部资源）。"
        elif deliver_kind == "file":
            out += "做成文件给人下载或编辑，文件名起清楚。"
        if on_railway and self._is_ssh_box(on_railway):
            out += (
                f"\n\n这轮在专用机器「{getattr(on_railway, 'name', '')}」上做（用户自己的 VPS / VM，没有时间限制）："
                "用 machine_run 在机器上跑命令（命令在这次的工作目录里执行）、"
                "machine_put_file 把工作区里你写的脚本传上去、machine_read_file 看机器上的输出"
                "（路径都写工作目录下的相对路径）。"
                "做好的成品不能留在机器上——交付只认本机工作区；"
                f"最后一定要用 machine_fetch_file 把成品拷回本机工作区 {target_dir}/ 下，拷不回来就等于没做成。"
            )
        elif on_railway:
            out += (
                "\n\n这轮在 railway.new 一次性机器上做（2 核 2G、60 分钟窗口，到点就什么都没了）："
                "用 vm_run 在机器上跑命令、vm_put_file 把工作区里你写的脚本传上去、"
                "vm_read_file 看机器上的输出。"
                "做好的成品不能留在机器上——交付只认本机工作区；"
                "最后一定要用 vm_fetch_file 把成品从机器的 /app/ 下拷回本机工作区 "
                f"{target_dir}/ 下，拷不回来就等于没做成。"
                "机器时间不多，别拖；快到期会提醒你，一提醒就马上 vm_fetch_file。"
            )
        return out

    # ------------------------------------------------------------------
    # 验收
    # ------------------------------------------------------------------

    async def _review(
        self,
        task: dict,
        plan: dict,
        summary: str,
        evidence: list[str],
        reports: list[Any],
    ) -> dict:
        """主模型验收；最多 _REVIEW_TOOL_LIMIT 轮工具调用（只读 inspect_）。"""
        tid = str(task["id"])
        gid = str(task["group_id"])
        ws_name = str(task.get("workspace") or self._workspace_name(gid))

        # 列 artifacts 目录清单放进 prompt
        listing: list[dict] = []
        try:
            listing = await self._env.list_files(ws_name, self._artifact_dir(tid), depth=3, limit=50)
            artifact_lines = [
                f"- {e['path']}{'/' if e['is_dir'] else f'（{e['size']} 字节）'}"
                for e in listing
            ]
            artifacts_text = "\n".join(artifact_lines) if artifact_lines else "（目录是空的）"
        except Exception:
            artifacts_text = "（拿目录清单失败）"

        prompt_lines = [
            "你是 MaiWork 的主模型，正在验收子 agent 交回的成品。",
            f"任务标题：{task['title']}",
            "",
            "完成标准：",
        ]
        for c in plan["criteria"]:
            prompt_lines.append(f"- {c}")
        prompt_lines.append("")
        prompt_lines.append("子 agent 的总结：")
        prompt_lines.append(summary or "（空）")
        prompt_lines.append("")
        prompt_lines.append("子 agent 给的证据：")
        if evidence:
            for e in evidence:
                prompt_lines.append(f"- {e}")
        else:
            prompt_lines.append("（没给）")
        prompt_lines.append("")
        prompt_lines.append(f"工作区 {self._artifact_dir(tid)}/ 下的成品清单：")
        prompt_lines.append(artifacts_text)

        # 引用核对（只对调研类任务做）：代码抽交付物里的 http(s) 链接，和本任务
        # fetch_page 成功过的 URL 比对；没打开过的清单作为事实喂给验收模型。
        link_check: dict | None = None
        if bool(plan.get("research")):
            try:
                link_check = await self._link_check(
                    tid=tid, ws_name=ws_name, listing=listing, summary=summary, evidence=evidence
                )
            except Exception:
                logger.exception("验收引用核对出错（任务 %s），这次跳过", tid)
                link_check = None
        if link_check and link_check["unopened"]:
            prompt_lines.append("")
            prompt_lines.append(
                "事实核对（代码查的，不是模型判断）：下面这些链接出现在交付内容里，"
                "但这个任务里没有真正打开过（fetch_page 没成功过；只在搜索结果里出现过不算打开过）："
            )
            for url in link_check["unopened_urls"]:
                prompt_lines.append(f"- {url}")
            prompt_lines.append(
                "请据此判断：只是少量、且不是关键结论的依据 → 可以 pass，但要在 review 里点出来；"
                "关键结论只靠这些没打开过的链接撑着 → pass 必须 false，并在 review 里点名要求打开核实或删掉。"
            )
        prompt_lines.append("")
        prompt_lines.append(
            "只回 JSON："
            '{"pass": true|false, "review": "中文验收意见，说清楚哪里过哪里不过",'
            ' "missing": ["还缺什么"], "artifact": "要交付的成品在工作区里的相对路径（'
            '如 artifacts/T-1/index.html；text 交付可以留空）", "note": "交付时在群里说的一句话（'
            '不点名关注成员、不暴露工具细节）"}'
        )

        prefix = self._identity_prefix(gid, with_memory=True)
        messages: list[dict] = [{"role": "user", "content": prefix + "\n".join(prompt_lines)}]
        specs = main_review_tool_specs(self._tools)
        try:
            ws_path = self._env.workspace(ws_name)
        except Exception:
            ws_path = None
        ctx = ToolContext(
            group_id=gid, task_id=tid, actor="主模型", workspace=ws_path, role="main"
        )
        review_data: dict | None = None
        for _round in range(_REVIEW_TOOL_LIMIT):
            result = await self._chat_main(
                messages,
                tools=specs or None,
                json_mode=False,
                purpose="coordinator.review",
                group_id=gid,
                task_id=tid,
            )
            tool_calls = result.tool_calls or []
            if not tool_calls:
                # 拿到最终 JSON 文本
                try:
                    parsed = json.loads(result.text)
                except (ValueError, TypeError):
                    # 再等一轮提示一下
                    messages.append({"role": "assistant", "content": result.text})
                    messages.append(
                        {
                            "role": "user",
                            "content": "请只回 JSON，字段按上面说的来；不要再调用工具。",
                        }
                    )
                    continue
                if isinstance(parsed, dict):
                    review_data = parsed
                    break
                continue
            # 执行工具调用，把结果作为 tool 消息回给模型
            await self._run_tool_calls(
                messages, tool_calls, ctx, assistant_text=result.text or ""
            )

        if review_data is None:
            raise ModelError("验收阶段主模型没给出结论")

        passed = bool(review_data.get("pass"))
        review_text = str(review_data.get("review") or "").strip() or ("通过" if passed else "不通过")
        artifact = str(review_data.get("artifact") or "").strip()
        note = str(review_data.get("note") or "").strip()
        missing = review_data.get("missing") if isinstance(review_data.get("missing"), list) else []

        # 硬性检查：pass 且非 text 时 artifact 必须真的存在、且不含符号链接（S3：
        # 发布/打包会跳过所有符号链接，交带链接的成品等于少件；线上是 root，
        # 跟着链接会把工作区外文件发出去）
        if passed and plan["deliver_kind"] != "text":
            if not artifact:
                passed = False
                review_text = "（验收 pass 但没给 artifact，视为不通过）" + review_text
            elif not self._artifact_exists(ws_name, artifact):
                passed = False
                review_text = f"（验收说 pass 但 {artifact} 不存在，视为不通过）" + review_text
            else:
                try:
                    art_path = self._env.resolve(ws_name, artifact)
                except (PermissionError, ValueError):
                    art_path = None
                problem = self._find_artifact_symlink_escape(art_path) if art_path is not None else None
                if problem:
                    passed = False
                    review_text = f"（{problem}，视为不通过）" + review_text

        # 引用核对留痕：验收意见里带一行「引用核对：N 条链接，M 条没打开过」，
        # 结构化结果存 kv["task.link_check.<任务ID>"]（前端读它做字段）。
        if link_check is not None:
            review_text = self._append_link_check_line(review_text, link_check)
            try:
                with self._store.tx() as conn:
                    self._store.kv_set(
                        conn,
                        f"task.link_check.{tid}",
                        {
                            "ts": clock.now(),
                            "attempt": int(task.get("attempts") or 0),
                            "links": int(link_check["links"]),
                            "unopened": int(link_check["unopened"]),
                            "unopened_urls": list(link_check["unopened_urls"]),
                        },
                    )
            except Exception:
                logger.exception("写引用核对记录失败（任务 %s）", tid)

        return {
            "pass": passed,
            "review": review_text,
            "artifact": artifact,
            "note": note,
            "missing": missing,
            "link_check": link_check,
        }

    # ------------------------------------------------------------------
    # 验收引用核对（调研类任务）：交付里的链接是不是这个任务真打开过
    # ------------------------------------------------------------------

    def _opened_urls(self, tid: str) -> set[str]:
        """本任务 fetch_page 成功过的 URL（规范化）；web_search 结果里的链接不算打开过。

        请求地址（tool_calls.input）和跳转后的最终地址都算打开过：交付里常引用最终的长链。
        最终地址从 fetch_page 的 output 摘要标记里解析（tools_builtin.final_url_from_summary，
        不改表结构）。
        """
        from .tools_builtin import final_url_from_summary

        try:
            rows = self._store.read().execute(
                "SELECT input, output FROM tool_calls WHERE task_id=? AND tool='fetch_page' AND ok=1",
                (str(tid),),
            ).fetchall()
        except Exception:
            logger.exception("读 fetch_page 记录失败（任务 %s）", tid)
            return set()
        opened: set[str] = set()
        for row in rows:
            raw = str(row["input"] or "").strip()
            url = raw
            if raw.startswith("{"):  # 工具的 summarize 给的是 JSON 摘要时兜底取 url
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        url = str(parsed.get("url") or "")
                except (ValueError, TypeError):
                    url = raw
            final = final_url_from_summary(str(row["output"] or ""))
            for one in (url, final):
                key = normalize_link_for_check(one)
                if key:
                    opened.add(key)
        return opened

    async def _gather_deliverable_texts(
        self, ws_name: str, listing: list[dict], summary: str, evidence: list[str]
    ) -> list[str]:
        """交付物文本：summary + evidence + 工作区里的文本成品（md/html/json…；二进制跳过）。"""
        texts: list[str] = [str(summary or ""), "\n".join(str(e) for e in (evidence or []))]
        files = 0
        for entry in listing or []:
            if not isinstance(entry, dict) or entry.get("is_dir"):
                continue
            path = str(entry.get("path") or "")
            if not path.lower().endswith(_TEXT_ARTIFACT_SUFFIXES):
                continue
            if files >= _ARTIFACT_SCAN_MAX_FILES:
                break
            files += 1
            try:
                texts.append(await self._env.read_file(ws_name, path, max_bytes=_ARTIFACT_SCAN_MAX_BYTES))
            except Exception:
                logger.debug("读成品 %s 失败，跳过", path, exc_info=True)
        return texts

    async def _link_check(
        self, *, tid: str, ws_name: str, listing: list[dict], summary: str, evidence: list[str]
    ) -> dict:
        """交付里引用的链接 vs 本任务真打开过的链接；返回 {links, unopened, unopened_urls}。"""
        links: list[str] = []
        for text in await self._gather_deliverable_texts(ws_name, listing, summary, evidence):
            links.extend(extract_http_links(text))
        opened = self._opened_urls(tid)
        unopened = [u for u in links if normalize_link_for_check(u) not in opened]
        return {
            "links": len(links),
            "unopened": len(unopened),
            "unopened_urls": unopened[:_UNOPENED_URLS_IN_PROMPT],
        }

    @staticmethod
    def _append_link_check_line(review_text: str, link_check: dict) -> str:
        line = f"引用核对：{int(link_check['links'])} 条链接，{int(link_check['unopened'])} 条没打开过。"
        if link_check["unopened_urls"]:
            line += "没打开过的：" + "、".join(str(u) for u in link_check["unopened_urls"][:5])
        base = str(review_text or "").strip()
        return f"{base}\n{line}" if base else line


    async def _run_tool_calls(
        self,
        messages: list[dict],
        tool_calls: list,
        ctx: ToolContext,
        *,
        assistant_text: str = "",
    ) -> None:
        """跑一轮工具调用：结果（截断 6000 字）当 tool 消息追加进 messages。

        主模型自己的工具调用一律走 Tools.call —— 落 tool_calls 表（actor="主模型"）。
        """
        messages.append(
            {"role": "assistant", "content": assistant_text, "tool_calls": tool_calls}
        )
        for tc in tool_calls:
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            name = str(fn.get("name") or "")
            raw_args = fn.get("arguments")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except (ValueError, TypeError):
                    args = raw_args
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}
            tr = await self._tools.call(name, args, ctx)
            content = tr.output if tr.ok else (tr.error or tr.output)
            if len(content) > 6000:
                content = content[:6000] + " …（已截断）"
            msg: dict[str, Any] = {"role": "tool", "content": content}
            if tc.get("id"):
                msg["tool_call_id"] = str(tc["id"])
            if name:
                msg["name"] = name
            messages.append(msg)

    # ------------------------------------------------------------------
    # 群空间小回合（验收通过、交付之前）
    # ------------------------------------------------------------------

    async def _groupspace_round(
        self, task_id: str, gid: str, ws_name: str, plan: dict, review: dict
    ) -> None:
        """交付前问一句主模型「这活要不要动群空间」，要动就让它调群空间工具。

        - 没有 group_space（[group_space] enabled=false / 没就位）→ 什么都不做；
        - 这个群 capabilities 全 False → 连模型都不叫（零额外开销）；
        - 只给这个群能力允许的那几个工具，最多 _GROUPSPACE_TOOL_LIMIT 轮工具调用；
        - 工具调用经 Tools.call 落库（actor="主模型"）；
        - 这一回合出任何岔子都不影响交付（尽力而为，只记日志）。
        """
        if self._group_space is None:
            return
        try:
            caps = await self._group_space.capabilities_async(gid)
        except Exception:
            logger.exception("查群空间能力失败（群 %s），这轮不折腾群空间", gid)
            return
        if not isinstance(caps, dict):
            return
        allowed = [name for name, cap in _GROUPSPACE_TOOL_CAPS if bool(caps.get(cap))]
        if not allowed:
            return  # 这个群什么都做不了：不叫模型
        specs = self._tools.specs("main", allowed)
        if not specs:
            return

        task = self._tasks.get(task_id) or {}
        prompt_lines = [
            "你是 MaiWork 的主模型。有一个 QQ 群的活刚验收通过，交付之前确认一下：",
            "这次要不要动一动本群的群空间？",
            "",
            f"任务标题：{task.get('title') or ''}",
            "群友提的原始需求（req）：",
            str(task.get("req") or "").strip() or "（空）",
            "",
            "完成标准（这活算做完了的标准）：",
        ]
        _crit = [str(c) for c in (plan.get("criteria") or [])]
        prompt_lines.extend([f"- {c}" for c in _crit] or ["（空）"])
        prompt_lines.extend(
            [
                "",
                "本群现在能用的群空间操作（只能从这里挑）：",
            ]
        )
        _tool_desc = {
            "group_files_list": "看群文件（可指定文件夹）",
            "group_file_manage": "管理群文件：删除 / 改名 / 移到文件夹 / 建文件夹（只能动机器人自己传的）",
            "group_notice_send": "发群公告（每群每天最多 1 条，会先在群里说一句预告）",
            "group_album_upload": "把工作区里的成品图传进群相册",
        }
        for name in allowed:
            prompt_lines.append(f"- {name}：{_tool_desc.get(name, '')}")
        artifact = str(review.get("artifact") or "").strip()
        if artifact:
            # 同一道交付路径闸：不在本任务成品目录里的 artifact 不写进提示词
            # （免得不小心把工作区里的内部文件当「成品」传给群空间工具）
            art_path = self._deliver_path_in_task_dir(ws_name, task_id, artifact)
            prompt_lines.append("")
            prompt_lines.append(
                f"这次交付的成品（工作区相对路径 {artifact}"
                + (f"，绝对路径 {art_path}" if art_path is not None else "")
                + "）"
            )
        prompt_lines.append("")
        prompt_lines.append(
            "只有任务明确需要时才用（比如用户要求整理群文件、发公告、传相册）；"
            "否则直接回答 {\"done\": true}。"
        )
        prompt_lines.append(
            "需要时就调用工具，做完后再回答 {\"done\": true}；不要输出别的。"
        )

        messages: list[dict] = [{"role": "user", "content": "\n".join(prompt_lines)}]
        try:
            ws_path = self._env.workspace(ws_name)
        except Exception:
            ws_path = None
        ctx = ToolContext(
            group_id=gid, task_id=task_id, actor="主模型", workspace=ws_path, role="main"
        )
        for _round in range(_GROUPSPACE_TOOL_LIMIT):
            try:
                result = await self._chat_main(
                    messages,
                    tools=specs,
                    json_mode=False,
                    purpose="coordinator.groupspace",
                    group_id=gid,
                    task_id=task_id,
                )
            except Exception:
                logger.exception("群空间小回合调模型失败（任务 %s），直接交付", task_id)
                return
            tool_calls = result.tool_calls or []
            if not tool_calls:
                try:
                    parsed = json.loads(result.text)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, dict):
                    return  # 拿到结论（{"done": true} 或别的）就收工
                messages.append({"role": "assistant", "content": result.text or ""})
                messages.append(
                    {
                        "role": "user",
                        "content": "不需要动群空间就直接回 {\"done\": true}；要动就调用上面给的某个工具。",
                    }
                )
                continue
            try:
                await self._run_tool_calls(
                    messages, tool_calls, ctx, assistant_text=result.text or ""
                )
            except Exception:
                logger.exception("群空间工具调用出错（任务 %s），直接交付", task_id)
                return

    # ------------------------------------------------------------------
    # 「记经验」小回合（验收通过、交付之前）
    # ------------------------------------------------------------------

    async def _remember_round(self, task_id: str, gid: str, ws_name: str, plan: dict, review: dict) -> None:
        """验收通过后问一句主模型「有没有值得记的经验？」，有就让它调 remember（最多 2 次）。

        - 没 identity / remember 工具没注册 / 模型没配好 → 什么都不做（零额外模型调用）；
        - 模型不回调工具、给的又不是 {"done": true} → 提醒一次；再不行就收工；
        - 这一回合出任何岔子都不影响交付（尽力而为，只记日志）。
        """
        identity = self._identity
        if identity is None:
            return
        specs = self._tools.specs("main", ["remember"])
        if not specs:
            return
        task = self._tasks.get(task_id) or {}
        prompt_lines = [
            "你是 MaiWork 的主模型。有一个 QQ 群的活刚验收通过，马上要交付。",
            "这次干活里有没有「值得记下来、以后能用」的经验？",
            "",
            f"任务标题：{task.get('title') or ''}",
            "完成标准：",
        ]
        _crit = [str(c) for c in (plan.get("criteria") or [])]
        prompt_lines.extend([f"- {c}" for c in _crit] or ["（空）"])
        prompt_lines.append("")
        prompt_lines.append(f"验收意见（节选）：{str(review.get('review') or '')[:300]}")
        prompt_lines.append("")
        prompt_lines.append(
            "规则："
            "- 值得记就调用 remember 工具：scope=group 记「这个群」的经验（喜欢这个群怎么交付、"
            "哪类事别做；最多 200 字）；scope=global 只记和具体群、具体人无关的通用经验"
            "（全局里不许写群号、QQ 号、任何人的名字）。最多调 2 次，值得记才调，没有值得记的就别调；"
            "- 调完（或不调）就回答 {\"done\": true}；不要输出别的。"
        )
        messages: list[dict] = [{"role": "user", "content": "\n".join(prompt_lines)}]
        try:
            ws_path = self._env.workspace(ws_name)
        except Exception:
            ws_path = None
        ctx = ToolContext(
            group_id=gid, task_id=task_id, actor="主模型", workspace=ws_path, role="main"
        )
        used = 0
        for _round in range(_REMEMBER_TOOL_LIMIT + 2):  # 工具配额 2 次 + 收尾/提醒各一次机会
            quota_left = _REMEMBER_TOOL_LIMIT - used
            try:
                result = await self._chat_main(
                    messages,
                    tools=specs if quota_left > 0 else None,
                    json_mode=False,
                    purpose="coordinator.remember",
                    group_id=gid,
                    task_id=task_id,
                )
            except Exception:
                logger.exception("记经验小回合调模型失败（任务 %s），直接交付", task_id)
                return
            tool_calls = [tc for tc in (result.tool_calls or []) if str((tc.get("function") or {}).get("name") or "") == "remember"]
            if not tool_calls:
                try:
                    parsed = json.loads(result.text)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, dict):
                    return  # 拿到结论（{"done": true} 或别的）就收工
                messages.append({"role": "assistant", "content": result.text or ""})
                messages.append(
                    {
                        "role": "user",
                        "content": "没有值得记的就直接回 {\"done\": true}；值得记就调用 remember。",
                    }
                )
                continue
            if quota_left <= 0:
                return  # 配额用光模型还想调：不再执行，直接收工交付
            run_calls = tool_calls[:quota_left]
            used += len(run_calls)
            try:
                await self._run_tool_calls(
                    messages, run_calls, ctx, assistant_text=result.text or ""
                )
            except Exception:
                logger.exception("记经验工具调用出错（任务 %s），直接交付", task_id)
                return

    def _persist_criteria(self, task_id: str, criteria: list[str]) -> None:
        """把主模型补全的完成标准落进任务（不通过 revise 版本来——revise 是需求变了）。"""
        try:
            task = self._tasks.get(task_id)
            if not task:
                return
            current = self._safe_json_list(task.get("criteria"))
            if current == criteria:
                return
            # transition 不让改 criteria，这里直接改库 + task_versions 各记一条新版本不合适。
            # 简单起见：用 tasks.revise 等价语义会污染版本；用 store 直接更新 criteria 字段。
            with self._store.tx() as conn:
                conn.execute(
                    "UPDATE tasks SET criteria=?, updated=? WHERE id=?",
                    (json.dumps(criteria, ensure_ascii=False), clock.now(), task_id),
                )
        except Exception:
            logger.exception("补全 criteria 落库失败")

    # ------------------------------------------------------------------
    # 结果落地
    # ------------------------------------------------------------------

    def _scrub_note(self, gid: str, note: str, task_title: str) -> str:
        """G7：交付说明要发进群，不能含关注成员名字 / 注记。
        命中 → 兜底「做好了：<任务标题>」；note 本来空 → 默认兜底话。"""
        from .privacy import scrub

        text = str(note or "").strip() or _TEXT_DELIVER_FALLBACK_NOTE
        cleaned = scrub(gid, text, self._store)
        if cleaned is None:
            title = str(task_title or "").strip() or "任务"
            return f"做好了：{title}"
        return cleaned

    async def _handle_passed(
        self, task_id: str, gid: str, ws_name: str, plan: dict, review: dict
    ) -> str:
        """通过：finish_attempt(passed) 已经做过，这里 transition → completed + 交付。"""
        # 群空间小回合：验收通过、正式交付之前（不需要就不动模型；出错不影响交付）
        await self._groupspace_round(task_id, gid, ws_name, plan, review)
        # 「记经验」小回合：群空间之后、交付之前（没 identity / 没 remember 工具 → 零额外模型调用）
        try:
            await self._remember_round(task_id, gid, ws_name, plan, review)
        except Exception:
            logger.exception("记经验小回合出错（任务 %s，不影响交付）", task_id)
        try:
            self._tasks.transition(
                task_id,
                "completed",
                reason="验收通过",
                review=review["review"],
                delivery_kind=plan["deliver_kind"],
            )
        except ValueError as e:
            logger.warning("任务 %s →completed 非法：%s", task_id, e)
            return "done"

        kind = plan["deliver_kind"]
        # G7 隐私闸：note 含关注成员信息 → 兜底「做好了：<任务标题>」
        task_title = ""
        try:
            _t = self._tasks.get(task_id)
            task_title = str((_t or {}).get("title") or "")
        except Exception:
            task_title = ""
        note = self._scrub_note(gid, review["note"] or _TEXT_DELIVER_FALLBACK_NOTE, task_title)
        if kind == "text":
            try:
                self._outbox.enqueue(
                    f"task:{task_id}:deliver:text",
                    gid,
                    "text",
                    {"text": note, "push_kind": "delivery"},
                    task_id=task_id,
                )
            except Exception:
                logger.exception("text 交付入队失败")
        else:
            artifact = review.get("artifact") or ""
            path = self._deliver_path_in_task_dir(ws_name, task_id, artifact)
            if path is None:
                return "done"
            name = Path(path).name or Path(path).parent.name or task_id
            try:
                await self._delivery.deliver_task(
                    task_id, kind=kind, path=path, name=name, note=note
                )
            except Exception:
                logger.exception("deliver_task 失败")
        self._write_tokens(task_id)
        return "done"

    def _handle_unpassed(
        self, task_id: str, attempt: int, gid: str, review: str, deliver_kind: str
    ) -> str:
        """不通过：尝试数 < 3 → 回 queued 立刻再来；否则 failed + 固定话。"""
        if attempt < _MAX_ATTEMPTS:
            try:
                self._tasks.transition(
                    task_id,
                    "queued",
                    reason=review or "验收不通过，重试",
                    review=review,
                )
                return "retry"
            except ValueError as e:
                logger.warning("任务 %s →queued 非法：%s", task_id, e)
                return "done"
        try:
            self._tasks.transition(task_id, "failed", reason=review, review=review)
        except ValueError as e:
            logger.warning("任务 %s →failed 非法：%s", task_id, e)
            return "done"
        task = self._tasks.get(task_id)
        title = str(task.get("title") if task else "") or "任务"
        short = (review or "没通过")[:60]
        try:
            self._outbox.enqueue(
                f"task:{task_id}:failed",
                gid,
                "text",
                {"text": f"「{title}」没做成：{short}", "push_kind": "status"},
                task_id=task_id,
            )
        except Exception:
            logger.exception("失败通知入队失败")
        self._write_tokens(task_id)
        return "done"

    def _fail_with_err(self, task_id: str, attempt_id: int | None, msg: str, gid: str) -> None:
        """故障路径：attempt 标 failed、任务 failed、report_error。"""
        if attempt_id is not None:
            try:
                self._tasks.finish_attempt(attempt_id, status="failed", review=msg)
            except Exception:
                logger.exception("finish_attempt(failed) 失败")
        try:
            current = self._tasks.get(task_id)
            if current and str(current["status"]) not in ("failed", "cancelled", "completed"):
                self._tasks.transition(task_id, "failed", reason=msg, review=msg)
        except Exception as e:
            logger.warning("任务 %s →failed 失败：%s", task_id, e)
        try:
            # 「模型还没配好」类的故障不往群里报（配置问题用户自己在网页看，不刷屏）
            if not _is_unconfigured_error(msg):
                _report_error(self._store, self._outbox, gid, msg, clock.now())
        except Exception:
            logger.exception("report_error 失败")
        self._write_tokens(task_id)

    def _write_tokens(self, task_id: str) -> None:
        """按 task_id 汇总 usage 表写回 tasks.tokens。"""
        try:
            row = self._store.read().execute(
                "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS t"
                " FROM usage WHERE task_id=?",
                (task_id,),
            ).fetchone()
            total = int(row["t"]) if row is not None else 0
            with self._store.tx() as conn:
                conn.execute(
                    "UPDATE tasks SET tokens=?, updated=? WHERE id=?",
                    (total, clock.now(), task_id),
                )
        except Exception:
            logger.exception("汇总 tokens 失败")

    # ------------------------------------------------------------------
    # check_goal
    # ------------------------------------------------------------------

    async def check_goal(self, goal_id: str) -> None:
        """agent 目标检查：看进展、勾完成标准、决定要不要派下级任务、汇报或推后。"""
        # 模型没配好：跳过这次检查，不报错也不推进（02 §12.1：没配好不做任何要模型的事）
        if not _models_ready(self._models):
            return
        goal = self._goals.get(goal_id)
        if goal is None or str(goal.get("state")) != "active" or str(goal.get("kind")) != "agent":
            return
        gid = str(goal["group_id"])

        # 下级任务状态
        rows = self._store.read().execute(
            "SELECT id, title, status, review FROM tasks WHERE goal_id=? ORDER BY created DESC LIMIT 5",
            (goal_id,),
        ).fetchall()
        task_lines = [f"- [{r['status']}] {r['title']}（{r['id']}）" for r in rows]

        crit = self._safe_json_list(goal.get("criteria"))
        crit_lines = [
            f"[{i}] {'✓ ' if c.get('done') else ''}{c.get('text', '')}"
            for i, c in enumerate(crit)
        ]
        last_text = str(goal.get("last_text") or "")
        # 群聊上下文（2026-10）：目标检查要考虑群里最近的新进展；第一次检查时还要靠它
        # 补出验收标准（criteria 为空 → 请模型顺手补 3–5 条，落库）。
        chat_lines = self._goal_chat_lines(gid)
        need_criteria = not crit

        prompt_lines = [
            "你是 MaiWork 的主模型，在检查一个 agent 目标的进展。",
            f"目标标题：{goal['title']}",
            f"目标内容：{str(goal.get('body') or '')}",
            f"时间要求（by）：{str(goal.get('by_text') or '')}",
            "",
            "完成标准（带索引）：",
            *(crit_lines or ["（还没定——这是第一次检查，请先根据群聊补出 3–5 条能验收的标准）"]),
        ]
        if chat_lines:
            prompt_lines.extend(
                ["", "群里最近两天在聊的（判断进展用；没有新进展就别硬说）：", *chat_lines]
            )
        prompt_lines.extend(
            [
                "",
                f"最近进展：{last_text or '（还没记过）'}",
                "",
                "下级任务状态：",
                *(task_lines or ["（还没有下级任务）"]),
                "",
                "只回 JSON："
                '{"done_criteria": [满足了的完成标准索引],'
                ' "next_check_hours": 几小时后再检查,'
                ' "progress": "一句话的新进展，没有新进展就 null",'
                ' "new_task": {"title","req","criteria":["…"]} | null,'
                ' "report": "有阶段性结果想发到群里说的一句话，没有就 null"'
                + (',' + ' "criteria": ["第一次检查补出的完成标准（3–5 条，一句话一条）"]'
                   if need_criteria else '')
                + "}",
            ]
        )
        prompt = "\n".join(prompt_lines)
        try:
            result = await self._chat_main(
                [{"role": "user", "content": prompt}],
                json_mode=True,
                purpose="coordinator.check_goal",
                group_id=gid,
            )
            data = json.loads(result.text)
        except (ModelError, HostError) as e:
            msg = getattr(e, "message", e)
            try:
                # 「模型还没配好」类的故障不往群里报，也不算出错（等配置，不算卡住）
                if not _is_unconfigured_error(msg):
                    self._check_goal_fail(goal_id, f"目标检查失败：{msg}")
                    _report_error(self._store, self._outbox, gid, f"目标检查失败：{msg}", clock.now())
            except Exception:
                pass
            return
        except (ValueError, TypeError) as e:
            logger.warning("目标 %s 检查 JSON 不合法：%s", goal_id, e)
            self._check_goal_fail(goal_id, f"检查返回不合法：{e}")
            return
        except Exception as e:
            logger.exception("目标 %s 检查异常", goal_id)
            self._check_goal_fail(goal_id, f"检查异常：{e}")
            return
        if not isinstance(data, dict):
            self._check_goal_fail(goal_id, "检查返回不是 JSON 对象")
            return
        # 成功跑完（不管有没有新进展）：报平安——写心跳、清卡住标记、出错计数归零
        try:
            self._goals.beat(goal_id, clock.now())
        except Exception:
            logger.exception("目标 %s 写心跳失败", goal_id)

        # 第一次检查：模型顺手补出的验收标准先落库（这样下面的 done_criteria 索引才对得上）
        if need_criteria:
            raw_crit = data.get("criteria")
            if isinstance(raw_crit, list):
                texts = [str(x).strip() for x in raw_crit if str(x).strip()]
                if texts:
                    try:
                        self._goals.set_criteria(goal_id, texts)
                        crit = self._safe_json_list(self._goals.get(goal_id).get("criteria"))
                    except Exception:
                        logger.exception("目标 %s 补验收标准落库失败", goal_id)

        # 勾完成标准
        done_list = data.get("done_criteria") or []
        if isinstance(done_list, list):
            for i in done_list:
                try:
                    idx = int(i)
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(crit):
                    try:
                        self._goals.set_criterion(goal_id, idx, True)
                    except (IndexError, KeyError):
                        pass

        # 记进展
        progress = data.get("progress")
        progress_s = str(progress).strip() if progress else ""
        if progress_s:
            try:
                self._goals.touch(goal_id, progress_s)
            except Exception:
                pass

        # 推后下次检查
        try:
            hours = float(data.get("next_check_hours") or 0) or 1.0
        except (TypeError, ValueError):
            hours = 1.0
        try:
            self._goals.set_next_check(goal_id, clock.now() + int(hours * 3600))
        except Exception:
            pass

        # 新建下级任务 + 立刻 run_task
        new_task = data.get("new_task")
        tid_new: str | None = None
        if isinstance(new_task, dict):
            title_t = str(new_task.get("title") or "").strip()
            req_t = str(new_task.get("req") or "").strip()
            crit_t = new_task.get("criteria") if isinstance(new_task.get("criteria"), list) else []
            crit_t = [str(x).strip() for x in crit_t if str(x).strip()]
            if title_t and req_t:
                tid_new = self._tasks.create(
                    gid,
                    title=title_t,
                    req=req_t,
                    criteria=crit_t,
                    source="goal",
                    goal_id=goal_id,
                    status="queued",
                )

        # 汇报
        report = data.get("report")
        report_s = str(report).strip() if isinstance(report, str) else ""
        if report_s:
            try:
                self._outbox.enqueue(
                    f"goal:{goal_id}:report:{int(clock.now())}",
                    gid,
                    "text",
                    {"text": report_s, "push_kind": "status"},
                )
            except Exception:
                pass

        # 全部完成标准满足 → done + 完成话
        crit_now = self._safe_json_list(self._goals.get(goal_id).get("criteria"))
        all_done = bool(crit_now) and all(bool(c.get("done")) for c in crit_now)
        if all_done:
            try:
                self._goals.done(goal_id)
                self._outbox.enqueue(
                    f"goal:{goal_id}:done",
                    gid,
                    "text",
                    {
                        "text": f"目标「{goal['title']}」完成了：{progress_s or report_s or _AGENT_DONE_WORD_HINT}",
                        "push_kind": "status",
                    },
                )
            except Exception:
                pass

        # 下级任务立刻跑（不在锁里：run_task 会自己按工作区拿锁）
        if tid_new:
            try:
                await self.run_task(tid_new)
            except Exception:
                logger.exception("跑下级任务失败 %s", tid_new)

    # ------------------------------------------------------------------
    # resume
    # ------------------------------------------------------------------

    async def resume(self, task_id: str, answer: str) -> None:
        """waiting_input / shelved 的任务收到回答 → 追加进 req → queued → run_task。

        docs/02 §7.2：任务详情时间线要能看见「收到回答：前 60 字」——
        resume 里 revise 记了版本，另外写一条 task.answer 事件 + 一行 tool_calls
        （task_id 维度的网页时间线读 tool_calls）。
        """
        task = self._tasks.get(task_id)
        if task is None:
            return
        if str(task["status"]) not in ("waiting_input", "shelved"):
            return
        answer_s = str(answer or "").strip()
        old_req = str(task.get("req") or "")
        new_req = (old_req + f"\n\n【发起人补充】{answer_s}").strip()
        current_crit = self._safe_json_list(task.get("criteria"))
        # revise 会把 running/reviewing 排回 queued；waiting_input/shelved 不在那张表里，
        # 所以 revise 之后还要手动 transition → queued
        self._tasks.revise(task_id, req=new_req, criteria=current_crit)
        self._record_answer(task_id, str(task.get("group_id") or ""), answer_s)
        task = self._tasks.get(task_id)
        if task and str(task["status"]) in ("waiting_input", "shelved"):
            try:
                self._tasks.transition(task_id, "queued", reason="发起人已补充，重新排队")
            except ValueError as e:
                logger.warning("任务 %s →queued 非法：%s", task_id, e)
                return
        await self.run_task(task_id)

    def _record_answer(self, task_id: str, group_id: str, answer: str) -> None:
        """「收到回答：前 60 字」落库——task.answer 事件 + tool_calls 行（网页任务详情时间线读它）。"""
        short = str(answer or "").strip()[:60]
        if not short:
            return
        try:
            with self._store.tx() as conn:
                self._store.event(
                    conn, "task.answer", group_id=str(group_id), entity="task",
                    entity_id=str(task_id), payload={"text": short},
                )
                conn.execute(
                    "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ms, ok, error)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        clock.now(), str(group_id), str(task_id), "主模型",
                        "收到回答", "", short, 0, 1, "",
                    ),
                )
        except Exception:
            logger.exception("记「收到回答」事件失败（任务 %s）", task_id)

    # ------------------------------------------------------------------
    # 杂项
    # ------------------------------------------------------------------

    def _check_goal_fail(self, goal_id: str, error: str) -> None:
        """目标检查出错计数（goals.fail 内部满 3 次会写 stale_reason）。"""
        try:
            self._goals.fail(goal_id, str(error or ""), clock.now())
        except Exception:
            logger.exception("目标 %s 出错计数失败", goal_id)

    def _goal_chat_lines(self, gid: str) -> list[str]:
        """目标检查用的群聊节选：本群最近 _GOAL_CHAT_H 小时，每条截 _GOAL_CHAT_TEXT 字。

        只给主模型看；读不到 / 库错误 → []（那段就不加，检查照常跑）。
        """
        from .chatlog import recent_chat

        try:
            rows = recent_chat(self._store, gid, hours=_GOAL_CHAT_H, limit=_GOAL_CHAT_N)
        except Exception:
            logger.info("目标检查读群聊失败（群 %s）", gid, exc_info=True)
            return []
        out: list[str] = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            text = str(r.get("text") or "").strip().replace("\n", " ")
            if not text:
                continue
            who = str(r.get("who") or "").strip() or "群友"
            out.append(f"- {who}：{text[:_GOAL_CHAT_TEXT]}")
        return out

    @staticmethod
    def _safe_json_list(value: Any) -> list:
        """宽容地把 JSON 串/本身变成 list；不能变就 []。"""
        try:
            out = json.loads(value) if isinstance(value, str) else value
        except (TypeError, ValueError):
            return []
        return out if isinstance(out, list) else []


__all__ = ["Coordinator"]
