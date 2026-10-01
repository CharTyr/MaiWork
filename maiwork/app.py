"""app.py：MaiWorkApp——把各模块接起来；enabled 开关；后台循环；热更新。

- start()：load_settings → Store → Host → Models → Profiles → ensure_groups →
  控制台 → 后台循环。enabled=False 时什么都不起（不开库、不开端口、不跑循环）。
- stop()：取消并等待后台循环 → 关控制台 → 关模型客户端 → 关库。
- update_config(raw)：关→开 整app重启；开→关 全收；开着改配置 更新 settings，
  console.listen 变了就重启控制台。
- on_message(kwargs)：转给 intake.handle；未启动 / 任何异常都返回 {"action": "continue"}。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import secrets
from pathlib import Path
from typing import Any, Callable

from . import clock, members
from .config import Settings, load_settings
from .host import Host
from .intake import Intake, Signals
from .models import Models
from .store import Store

logger = logging.getLogger("maiwork.app")

# 群链接码字符集：去掉容易看混的 0/O、1/l/I
_TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"

_CONTINUE: dict[str, Any] = {"action": "continue"}


def _import_profiles_cls():
    """profile.py 由另一条线同时开发；没写好时不至于拖垮整个 app。"""
    try:
        from .profile import Profiles

        return Profiles
    except Exception as e:  # pragma: no cover - profile.py 还没就位时的兜底
        logger.warning("profile.py 还没就位（%s），群画像功能这次跳过", type(e).__name__)
        return None


def _import_m2_class(module: str, attr: str) -> Any:
    """M2 模块（feeds/scheduler 由另一条线同时开发）；没写好时返回 None，不拖垮 app。"""
    try:
        mod = __import__(f"{__package__}.{module}", fromlist=[attr])
        return getattr(mod, attr)
    except Exception as e:  # pragma: no cover - 模块还没就位时的兜底
        logger.info("%s.%s 还没就位（%s），这块功能这次跳过", module, attr, type(e).__name__)
        return None


class MaiWorkApp:
    def __init__(self, ctx: Any, raw_config: Any, plugin_dir: Path | str) -> None:
        self._ctx = ctx
        self._raw_config = raw_config
        self.plugin_dir = Path(plugin_dir)
        # 测试注入点（默认取真实现）
        self.profiles_cls: Any = _import_profiles_cls()
        self.profiles_factory: Callable[..., Any] | None = None
        self.feeds_cls: Any = _import_m2_class("feeds", "Feeds")
        self.scheduler_cls: Any = _import_m2_class("scheduler", "Scheduler")
        self.feeds_factory: Callable[..., Any] | None = None
        self.scheduler_factory: Callable[..., Any] | None = None
        self.coordinator_cls: Any = _import_m2_class("coordinator", "Coordinator")
        self.coordinator_factory: Callable[..., Any] | None = None
        self.personas_factory: Callable[..., Any] | None = None
        self.personal_factory: Callable[..., Any] | None = None
        self.env_factory: Callable[..., Any] | None = None
        self.railway_factory: Callable[..., Any] | None = None
        self.loop_interval: float = 30.0
        # 测试注入点（可选，start 前设置）：各外部 HTTP 客户端的 httpx transport。
        # 缺省 None = 真实网络；测试注入 httpx.MockTransport 后不碰任何真实网络（AGENTS.md 红线）。
        self.http_transport: Any = None      # models.py 的 OpenAI 客户端
        self.jev_transport: Any = None       # jev.py 的 Jev 客户端
        self.herenow_transport: Any = None   # herenow.py 的 HereNow 客户端
        self.extensions_transport: Any = None  # extensions.py 的 MCP 扩展客户端
        self.rss_transport: Any = None         # rss.py 的 RSS 客户端（MockTransport 用）
        self.avatar_transport: Any = None      # console/avatar.py 的头像出站下载（MockTransport 用）

        self._settings: Settings | None = None
        self.problems: list[str] = []

        self.store: Store | None = None
        self.host: Host | None = None
        self.models: Models | None = None
        self.profiles: Any = None
        self.signals = Signals()
        self._intake: Intake | None = None
        self.console: Any = None  # console.server.ConsoleServer
        # 管理员对话（tools_admin.PendingGate / admin_chat.AdminChat）
        self.admin_pending: Any = None
        self.admin_chat: Any = None
        # M2 模块（docs/07 §10）
        self.jev: Any = None
        self.search: Any = None
        self.reader: Any = None  # JinaReader（reader.py）：打开网页的首选路
        self.tools: Any = None
        self.workers: Any = None
        self.mentions: Any = None
        self.pushes: Any = None
        self.topics: Any = None
        self.feeds: Any = None
        self.scheduler: Any = None
        self.goal_proposer: Any = None
        # M3 模块（docs/07 §11）
        self.tasks: Any = None
        self.goals: Any = None
        self.approvals: Any = None
        # 派活自动审核（auto_review.py）：低风险轻活由主模型判断后自动批
        self.auto_review: Any = None
        self.env: Any = None
        # 本机执行能力判定（environments/capability.py）：启动算一次、记一行中文日志；
        # fixed=固定用户隔离 / dynamic=自动分配用户隔离 / stopped=本机不能隔离跑命令。
        self.capability: Any = None
        # 判定后的实际工作区根（None = 还没判定，get_settings 不套修正）
        self._ws_root: Path | None = None
        # 测试注入点：替换探测（默认走 capability.probe 真探测）
        self.capability_probe: Callable[[str], Any] | None = None
        # 一次性 VM 执行环境（environments/railway.py；railway=false / 模块没就位 → None）
        self.railway: Any = None
        # 专用 SSH 机器（environments/ssh.py；启动就建，没配机器也先生成 key 给网页展示公钥）
        self.ssh: Any = None
        self.ssh_factory: Any = None  # 测试注入点
        self._ssh_check_ts = 0.0
        self._ssh_check_sig: Any = None
        self.outbox: Any = None
        self.card_push: Any = None  # 资讯卡片（card_push.py）
        self.idea_mention: Any = None  # 构想提一嘴（card_push.py）
        self.news_viz: Any = None  # 资讯图解（news_viz.py）
        self.update_check: Any = None  # 更新提醒（update_check.py；只提醒不自动更新）
        self.delivery: Any = None
        self.herenow: Any = None
        self.coordinator: Any = None
        self.commands: Any = None
        # 关注成员个人画像（persona.py；没就位就 None，网页 focus 里 persona 全 null）
        self.personas: Any = None
        # 关注成员的个人向产出（personal.py；没就位就 None，focus[].personal 为空、路由 503）
        self.personal: Any = None
        # skill + MCP 扩展（docs/02 §10；skills.py 只读数据目录、extensions.py 连 MCP；不挂 MaiBot planner）
        self.skills: Any = None
        self.extensions: Any = None
        # 专岗（agents.py / specialists.py；没就位 → None，各注入点走老路，server 503）
        self.agents: Any = None
        self.specialists: Any = None
        # 测试注入点：专岗组件构造工厂（默认 _make_agents/_make_specialists 的懒加载）
        self.agents_factory: Callable[..., Any] | None = None
        self.specialists_factory: Callable[..., Any] | None = None
        # 群空间（docs/02 §10；platforms/qq_onebot.py；[group_space] enabled=false → None）
        self.group_space: Any = None
        # 身份与工作记忆（identity.py；start 时建，模块出错 → None，注入点自动跳过）
        self.identity: Any = None
        # 头像服务（console/avatar.py：bot 头像 + 关注成员头像的缓存与签发）；
        # 控制台启动时建（见 _start_stack 第 6 步），没启动 / 没建好 → None，views 回落默认图
        self.avatar: Any = None

        self._task: asyncio.Task | None = None
        self._started = False
        self._listen: tuple[str, int] | None = None
        # 长活（资讯备料 / 构想）：同一群同一种同时只跑一个，不阻塞后台循环
        self._bg_jobs: set[asyncio.Task] = set()
        self._running_jobs: set[tuple[str, str]] = set()
        # 名册跟 QQ 对名字：每群上次派工时间（30 分钟一轮）
        self._names_last: dict[str, float] = {}
        # M3 任务派工：同一任务同一时刻只跑一个 coordinator.run_task
        self._running_tasks: set[str] = set()
        # 提问回答恢复（docs/02 §7.2）：intake 钩子只查这张内存表——
        # 「群 → [(task_id, question_msg_id, requester_id)]」，30 秒一刷，状态变了主动失效
        self._answer_cache: dict[str, list] = {}
        self._answer_cache_ts: float = 0.0
        # MaiBot planner 钩子只查这张服务群内存映射；启动时从服务群的库行恢复，
        # 消息到来时同步更新，不受后台循环 signals.take() 消耗影响。
        self._session_to_served_group: dict[str, str] = {}
        self._ambiguous_sessions: set[str] = set()
        # 网页规则覆盖（rules.py：rules.override）缓存：
        # (基础 Settings 对象 id, kv["rules.override"], 合并后的有效 Settings)
        self._effective_cache: tuple[int, Any, Settings] | None = None

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def started(self) -> bool:
        return self._started

    @property
    def settings(self) -> Settings | None:
        return self._settings

    @property
    def intake(self) -> Intake | None:
        return self._intake

    def _effective_settings(self) -> Settings:
        """有效配置（config.toml + 网页规则覆盖），**不**套执行方式判定的工作区根修正。

        给 _workspace_root_for 用：它要读用户配置的根再决定实际根，套了修正会自循环。
        """
        assert self._settings is not None
        base = self._settings
        if self.store is None:
            return base
        from . import rules as _rules

        override = _rules.read_override(self.store)
        cache = self._effective_cache
        if (
            cache is not None
            and cache[0] == id(base)
            and cache[1] == override
        ):
            return cache[2]
        merged = _rules.effective_settings(base, override)
        self._effective_cache = (id(base), override, merged)
        return merged

    def get_settings(self) -> Settings:
        """各模块用这个方法拿**有效**配置（config.toml + 网页规则覆盖 + 实际工作区根）。

        Settings 的唯一出口：网页存的 kv["rules.override"] 在这里做一次合并
        （rules.effective_settings，新对象一层缓存，override 变了才重建），
        所有模块读到的都是合并后的值——topics 开关关掉下一轮立刻停靠的就是这里。
        没开库（没启动 / enabled=false）给 config 原值。
        （2026-10：「全部配置」网页改的直接写 config.toml，走 update_config 热更新
        到 base Settings，不再有 kv["config.override"] 这一层。）
        再套一层「执行方式判定后的工作区根」（启动时算出，见 _detect_local_capability）：
        dynamic 落在 /var/lib/private/maiwork/workspaces、受限落在数据目录下的 workspaces/。
        这样 LocalEnv 读写文件、outbox 校验交付路径、coordinator 算成品目录看到的是同一个根。
        Settings 里两处都要换：environments.workspace_root（LocalEnv 读）和顶层
        workspace_root（outbox 的交付闸 / 暂存目录读）。
        """
        s = self._effective_settings()
        root = self._ws_root
        if root is None:
            return s
        try:
            env = s.environments
            ws = Path(root)
            if Path(env.workspace_root) == ws and Path(s.workspace_root) == ws:
                return s
            return dataclasses.replace(
                s,
                workspace_root=ws,
                environments=dataclasses.replace(env, workspace_root=ws),
            )
        except Exception:
            return s

    def base_settings(self) -> Settings:
        """config.toml 的原始配置（不含网页规则覆盖）；只在「和文件值比 / 展示 defaults」时用。"""
        assert self._settings is not None
        return self._settings

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        settings, problems = load_settings(self._raw_config)
        self._settings = settings
        self.problems = problems
        if problems:
            for p in problems:
                logger.warning("配置问题：%s", p)
        if not settings.enabled:
            logger.info("MaiWork 未启用（config 里 enabled=false），什么都不起")
            return
        try:
            await self._start_stack(settings)
        except Exception:
            logger.exception("MaiWork 启动出错，已尽量回收到未启动状态")
            await self._stop_stack()
            return
        self._started = True

    async def _start_stack(self, settings: Settings) -> None:
        # 1. 库
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(settings.data_dir / "maiwork.db")
        self.store.migrate()
        # 1.1 一次性迁移（幂等）：数据库里的旧网页配置覆盖层 → config.toml。
        # 迁移会改 config.toml 内容；宿主文件监控随后发的 on_config_update 和
        # 下面重读的文件是同一份，update_config 里有幂等比较。
        try:
            from . import migrations as _mig

            # [search] 段（老配置）→ MCP 扩展 + kv 搜索绑定（2026-10；幂等，日志不打密钥）。
            # 跑在 Extensions 连接之前没关系：绑定只存「扩展名 + 工具名」，不必当场验证工具
            # 在不在——扩展连上后 search.available() 现查现验。
            try:
                if _mig.migrate_search_config_to_extension(self.store, self.plugin_dir, settings.data_dir):
                    logger.info("老的 [search] 配置已迁成 MCP 扩展 + 搜索绑定")
            except Exception:
                logger.exception("搜索配置迁移（[search] → 扩展绑定）出错，按现状继续启动")
            moved = _mig.migrate_db_config_to_file(self.store, self.plugin_dir, settings.data_dir)
            # 1.2 一次性迁移（幂等）：旧 [models] → [[endpoints]] + [[model_list]] + 专岗选择。
            # 热应用走同一套：文件变了下面重读一份，宿主文件监控随后发的 on_config_update
            # 幂等跳过；Models.settings() 缓存键含 Settings 对象 id，换了 Settings 自动重算。
            try:
                if _mig.migrate_models_config_to_endpoints(self.store, self.plugin_dir, settings.data_dir):
                    moved = True
                    logger.info("旧 [models] 模型配置已迁成端点 + 模型库 + 专岗选择")
            except Exception:
                logger.exception("模型配置迁移（[models] → 端点/模型库）出错，按现状继续启动")
            if moved:
                import tomlkit as _tk

                raw_config = dict(_tk.parse((self.plugin_dir / "config.toml").read_text(encoding="utf-8")))
                new_settings, problems = load_settings(raw_config)
                self._raw_config = raw_config
                self._settings = new_settings
                self.problems = problems
                settings = new_settings
                for prob in problems:
                    logger.warning("配置问题：%s", prob)
        except Exception:
            logger.exception("配置迁移（数据库 → config.toml）出错，按现有配置继续启动")
        # 2. 宿主（预热 bot_qq，失败不致命）
        self.host = Host(self._ctx)
        # 群号 → 平台（qq / telegram）：Host 里不传 platform 的调用按配置自动认
        self.host.set_platform_resolver(lambda gid: self.get_settings().platform_of(gid))
        self.host.set_session_group_resolver(self._group_for_session_cached)
        try:
            await self.host.bot_qq()
        except Exception:
            logger.warning("预热 bot_qq 失败（宿主还没好？），之后用时再取", exc_info=True)
        try:
            await self.host._bot_accounts()  # 预热 bot.platforms（Telegram 等的机器人账号）
        except Exception:
            logger.warning("预热 bot.platforms 失败，之后用时再取", exc_info=True)
        try:
            nickname = await self.host.config("bot.nickname")
            self.host.bot_name_cache = str(nickname) if nickname else "MaiBot"
        except Exception:
            self.host.bot_name_cache = "MaiBot"
        # 3. 模型 / 画像
        self.models = Models(
            self.store, self.get_settings, transport=self.http_transport,
            config_writer=self._write_config_and_apply,
        )
        self.profiles = self._make_profiles()
        # 关注成员个人画像模块（persona.py；模块没就位还不至于拖垮别的）
        self.personas = self._make_personas()
        # 3.5 M2 模块（docs/07 §10）：Jev / 工具 / 子 agent / 可提起清单 / 推送 / 开话题 / 资讯构想 / 排程
        from .delivery import Mentions, Pushes
        from .jev import Jev
        from .search import Search
        from .tools import Tools
        from .tools_builtin import register_builtin
        from .topics import Topics
        from .workers import Workers

        # 身份与工作记忆（identity.py）：失败不拖垮启动（模块出错就 None，各注入点自动跳过）
        self.identity = self._make_identity()
        if self.identity is not None:
            try:
                await self.identity.ensure_started()  # 首次启动自动生成 SOUL / AGENTS 默认模板
            except Exception:
                logger.exception("身份文件首次建立出错，本轮身份注入跳过")

        self.jev = Jev(self.store, self.get_settings, transport=self.jev_transport)
        # 搜索（2026-10）：内置服务全删，只走「扩展」里绑定的那个 MCP——Search 现读
        # kv["extensions.search"] 绑定 + Extensions 的运行状态；扩展在 _start_extensions 才连上，
        # 这里先建好壳子（available() 现查，不缓存）。
        self.search = Search(self.store, lambda: self.extensions)
        # 打开网页先用 Jina Reader，有问题马上换抓正文工具，再不行直接打开（reader.py / tools_builtin.py）
        from .reader import JinaReader

        self.reader = JinaReader(self.get_settings)
        # M5：工具落库摘要统一遮密钥——已知密钥（模型 / 搜索）由这个回调给
        self.tools = Tools(self.store, get_known_secrets=self._known_secrets)
        try:
            register_builtin(self.tools, search=self.search, profiles=self.profiles, reader=self.reader)
        except Exception:
            logger.exception("注册内置工具出错，子 agent 这次没有工具用")
        # remember 工具（roles={"main"}；身份与工作记忆，identity.py）
        if self.identity is not None:
            try:
                from .identity import register_remember_tool

                register_remember_tool(self.tools, self.identity)
            except Exception:
                logger.exception("注册 remember 工具出错，主模型这次没有记忆工具用")
        self.workers = Workers(self.models, self.tools, identity=self.identity, tasks=self.tasks, get_settings=self.get_settings)
        self.mentions = Mentions(self.store, self.get_settings)
        self.pushes = Pushes(self.store, self.get_settings)
        self.topics = Topics(
            self.store, self.host, self.models, self.jev, self.profiles,
            self.mentions, self.pushes, self.get_settings, self.signals,
            identity=self.identity,
        )
        # 3.55 一次性 VM（railway.new 资讯实测，docs/07 §11.1b）：railway=true 才建 +
        # 注册 vm 工具；Feeds 的 verify_runner 从这里拿（railway=false → None）
        self.railway = self._make_railway()
        self.feeds = self._make_feeds()
        self.scheduler = self._make_scheduler()
        # 关注成员的个人向产出（personal.py；要在 topics / workers / search 就位之后建）
        self.personal = self._make_personal()
        # 3.57 skill + MCP 扩展（docs/02 §10：MaiWork 自己加载，不挂 MaiBot planner）——
        # 要在 Workers 建好、工具表就位之后做；skill/MCP 工具注册进 self.tools。
        await self._start_extensions()
        # 3.6 M3 模块（docs/07 §11）：目标 / 任务 / 批准 / 本机执行环境 / 发件箱 / 交付 / 协调 / 指令
        from .approvals import Approvals
        from .goals import Goals
        from .outbox import Delivery, Outbox
        from .tasks import Tasks
        from .tools_exec import register_exec_tools

        self.tasks = Tasks(self.store, self.get_settings, self.tools)
        # 冷启动时上次进程的 running/reviewing 已无人接管；先暂停并保留产物，
        # 不把可能有外部副作用的任务当作 queued 盲目重做。
        interrupted = self.tasks.interrupt_orphaned()
        if interrupted:
            logger.warning("恢复时发现 %d 个执行中断的任务，已暂停待核对", interrupted)
        self.goals = Goals(self.store, self.get_settings)
        self.approvals = Approvals(self.store, self.get_settings, self.tasks, self.goals)
        # 主动提目标（GoalProposer；[goals] propose 默认开）：到点由后台循环调一次
        self.goal_proposer = self._make_goal_proposer()
        # 派活自动审核（auto_review.py）：Approvals 每落一条待批请求就回调一次，
        # 这里把判断 spawn 到后台（调模型是慢活，绝不卡住收消息钩子）
        self.auto_review = self._make_auto_review()
        self._wire_auto_review()
        # 执行方式自动判定：fixed（固定用户隔离）/ dynamic（自动分配用户隔离）/ stopped（受限）
        self._detect_local_capability()
        self.env = self._make_env()
        try:
            register_exec_tools(
                self.tools, env=self.env, host=self.host,
                get_settings=self.get_settings, session_of=self._session_of_group,
            )
            # 「受限」时不给子 agent 跑命令的工具（文件读写工具保留）
            self._drop_command_tools_if_stopped(self.tools)
        except Exception:
            logger.exception("注册执行工具出错，子 agent 这次用不了文件 / 命令 / 聊天历史")
        # vm_fetch_file 需要本机 LocalEnv（此刻才建好）；register_vm_tools 逐个幂等，
        # 只补还没注册的（vm_run/put/read 已在 _make_railway 注册过就跳过这一遍）
        if self.railway is not None and self.env is not None:
            try:
                from .tools_railway import register_vm_tools as _register_vm_tools2

                _register_vm_tools2(
                    self.tools,
                    get_box=lambda: getattr(self.railway, "current_box", None),
                    env=self.railway,
                    local_env=self.env,
                )
            except Exception:
                logger.exception("注册 vm_fetch_file 出错，railway 派活这次少个拷回工具")
        # 专用 SSH 机器：建环境 + 注册 machine_* 工具（要在本机 LocalEnv 建好之后，拷回成品用它）
        self.ssh = self._make_ssh()
        self.herenow = self._make_herenow()
        self.outbox = Outbox(
            self.store, self.host, self.pushes, self.mentions, self.get_settings,
            herenow=self.herenow,
        )
        # 提问回执（docs/02 §7.2）：ask:{task_id}:{attempt} 发出 → 回写 question_msg_id
        try:
            self.outbox.set_ask_hook(self._on_ask_sent)
        except Exception:
            logger.exception("挂提问回执 hook 出错")
        # 主模型读群发现请求（docs/02 §3.1/§5.1）：profiles 建得比这三样早，这里补接线
        try:
            self.profiles.set_request_deps(
                approvals=self.approvals, goals=self.goals, outbox=self.outbox
            )
        except Exception:
            logger.exception("profiles 派活接线出错，读群发现的请求这次不落地")
        # 3.65 群空间（docs/02 §10）：[group_space] enabled=true 才建；启动探测失败不影响启动。
        self.group_space = self._make_group_space()
        if self.group_space is not None:
            # 防手滑登记：outbox 群文件传成功 → group_files_owned
            try:
                self.outbox.set_group_file_hook(self.group_space.register_owned)
            except Exception:
                logger.exception("挂群文件登记 hook 出错")
            try:
                await self.group_space.probe()
            except Exception:
                logger.exception("群空间启动探测出错（不影响启动，能力先全关）")
            try:
                from .tools_groupspace import register_groupspace_tools

                register_groupspace_tools(
                    self.tools, self.group_space, announce=self._groupspace_announce,
                    get_settings=self.get_settings,
                )
            except Exception:
                logger.exception("注册群空间工具出错，主模型这次没有群空间工具用")
        # 插件重启：发送中断的标不确定，不重放（docs/02 §6.5）
        recovered = self.outbox.recover()
        if recovered:
            logger.info("发件箱恢复：%d 条「发送中」标成不确定，不自动重发", recovered)
        # 资讯卡片 / 构想提一嘴（每群开关默认关；card_push.py）
        try:
            from .card_push import CardPush, IdeaMention

            self.card_push = CardPush(
                self.store, self.host, self.pushes, self.mentions, self.get_settings
            )
            self.idea_mention = IdeaMention(
                self.store, self.host, self.models, self.pushes, self.mentions, self.get_settings,
                identity=self.identity,  # 读 SOUL：提一嘴要按人设说话（voice.py）
            )
            n = self.card_push.recover() + self.idea_mention.recover()
            if n:
                logger.info("资讯卡片 / 构想提一嘴恢复：%d 条「发送中」标成不确定，不自动重发", n)
        except Exception:
            logger.exception("资讯卡片 / 构想提一嘴模块没建起来，这次不发")
            self.card_push = None
            self.idea_mention = None
        # 资讯图解（没配图、数据多的资讯画一张小图；news_viz.py）
        try:
            from .news_viz import NewsViz

            self.news_viz = NewsViz(self.store, self.models, self.workers, self.tools, self.get_settings)
        except Exception:
            logger.exception("资讯图解模块没建起来，这次不做图解")
            self.news_viz = None
        # 更新提醒：管理员开网页时顺手查 GitHub 最新版本（update_check.py）
        try:
            from .update_check import UpdateCheck, local_version

            self.update_check = UpdateCheck(
                local_version(),
                enabled=lambda: bool(self.get_settings().console.update_check),
            )
        except Exception:
            logger.exception("更新提醒模块没建起来，这次不查新版")
            self.update_check = None
        self.delivery = Delivery(self.store, self.outbox, self.tasks)
        self.coordinator = self._make_coordinator()
        # 按群的管理员（group_admins.py）：密码哈希 / 本群管理员名单只进数据库，
        # 不进 config.toml。/mw 批准 要用它，所以建在 commands 之前。
        try:
            from .group_admins import GroupAdmins

            self.group_admins = GroupAdmins(self.store, get_settings=self.get_settings)
        except Exception:
            logger.exception("建群管理员存储出错，群管理员这次不可用")
            self.group_admins = None
        self.commands = self._make_commands()
        # 3.66 专岗（agents.py + specialists.py）：要在 Workers / Tools / Skills / 扩展全部
        # 就位之后建（Specialists 要它们），在 Feeds/GoalProposer/Coordinator 全部就位之后挂
        # （它们都吃 `_specialists` 注入点）。没就位 → None，网页 API（server.py）503、
        # 业务走老路并记一行日志——绝不静默换成「通才 worker」。
        self.agents = self._make_agents()
        # 上个进程没收尾的交接单（验收都在同一进程里紧接着做，进程换了就没人收了）→ cancelled；
        # 和上面 interrupt_orphaned 收任务 / 尝试是同一个道理（外部审查 2026-10-02）
        if self.agents is not None:
            try:
                n = self.agents.cancel_unsettled(why="插件重启，上一轮没人接手")
                if n:
                    logger.info("收掉上次没收尾的交接单 %d 张", n)
            except Exception:
                logger.exception("启动时收残留交接单出错，继续")
        # 2026-10 模型改版 1a：模型路由读岗位 profile（model/backup）；挂上即生效（清缓存）
        if self.agents is not None and self.models is not None:
            try:
                self.models.set_agents(self.agents)
            except Exception:
                logger.exception("给 Models 挂专岗 Agents 出错，模型路由这次按旧 [models] 四槽走")
        self.specialists = self._make_specialists()
        self._wire_specialists()
        # 3.7 回收上次配置里删掉、库里残留的群数据（就地标记，不删）
        try:
            self._reconcile_unserved()
        except Exception:
            logger.exception("回收非服务群残留出错，继续")
        # 4. 收消息
        self._intake = self._make_intake()
        # 5. 每个服务群建行 + token
        self._ensure_groups()
        self._refresh_served_sessions()
        # 5.5 管理员对话：只给 admin 角色的工具 + 待确认门闸 + 对话循环（失败不影响其他功能）
        try:
            from .tools_admin import register_admin_tools

            self.admin_pending = register_admin_tools(self.tools, self) if self.tools is not None else None
        except Exception:
            logger.exception("注册管理员对话工具出错，管理员对话这次不可用")
            self.admin_pending = None
        try:
            from .admin_chat import AdminChat

            self.admin_chat = AdminChat(self)
        except Exception:
            logger.exception("建管理员对话出错，管理员对话这次不可用")
            self.admin_chat = None
        # 6. 控制台
        from .console.server import AUTH_KEY, ConsoleServer

        # 头像服务先建（views._bot_info 在第一个请求来时就要用它拼 /api/avatar/bot?v=N）
        try:
            from .console.avatar import AvatarService

            self.avatar = AvatarService(
                self.store, settings.data_dir, self.host, transport=self.avatar_transport
            )
        except Exception:
            logger.exception("建头像服务出错，bot 头像这次用默认图")
            self.avatar = None
        self.console = ConsoleServer(self)
        # 没有 config 密码时先确保自动生成的密码就位（存哈希 + 写文件）
        self.console.app[AUTH_KEY].ensure_password(settings.data_dir)
        host, port = settings.console.listen
        await self.console.start(host, port)
        self._listen = settings.console.listen
        self._console_auth_ensured = True
        # 7. 后台循环
        self._task = asyncio.create_task(self._loop(), name="maiwork-loop")
        logger.info("MaiWork 已启动：%d 个服务群，数据目录 %s", len(settings.groups), settings.data_dir)

    async def stop(self) -> None:
        await self._stop_stack()
        self._settings = None
        self.problems = []
        self._started = False

    # ------------------------------------------------------------------
    # 群空间（docs/02 §10；platforms/qq_onebot.py）
    # ------------------------------------------------------------------

    def _make_group_space(self) -> Any:
        """建 GroupSpace；[group_space] enabled=false / 建不起来 → None（网页健康里能看到）。"""
        try:
            settings = self.get_settings()
            gs_cfg = getattr(settings, "group_space", None)
            if gs_cfg is not None and not bool(getattr(gs_cfg, "enabled", True)):
                return None
        except Exception:
            pass
        try:
            from .platforms.qq_onebot import GroupSpace

            return GroupSpace(self.host, self.store, self.get_settings)
        except Exception:
            logger.exception("建 GroupSpace 出错，群空间这次跳过")
            return None

    async def _groupspace_announce(self, group_id: str, text: str) -> None:
        """群空间发公告前的固定预告话：走 outbox（push_kind=status，受推送节制）。"""
        if self.outbox is None:
            return
        try:
            # 公告预告本身不带任务；key 按群+内容去重
            import hashlib as _hl

            digest = _hl.sha1(f"{group_id}|{text}".encode("utf-8")).hexdigest()[:12]
            self.outbox.enqueue(
                f"groupspace-notice-pre:{group_id}:{digest}",
                str(group_id),
                "text",
                {"text": str(text), "push_kind": "status"},
                task_id=None,
            )
        except Exception:
            logger.exception("群公告预告入队出错（群 %s）", group_id)

    async def _stop_stack(self) -> None:
        # 先停后台循环（它会 spawn 长活），再收长活；顺序反了收拾长活期间
        # 循环又 spawn 新的，永远收不完
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("后台循环退出时出错")
        # 再收长活（资讯备料 / 构想）的后台任务
        bg, self._bg_jobs = self._bg_jobs, set()
        self._running_jobs.clear()
        for t in bg:
            t.cancel()
        for t in bg:
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("长活任务退出时出错")
            # cancel 后 callback（_on_long_job_done）操作的是被换掉的旧集合；
            # 这里同步从新集合也删掉，外面读才干净
            self._bg_jobs.discard(t)
            self._running_jobs.discard(self._job_key_of(t))
        # 所有协程收妥后再标暂停；重载失败或进程意外退出时，启动处也会补做。
        if self.tasks is not None:
            try:
                interrupted = self.tasks.interrupt_orphaned(reason="插件停用或热重载中断，保留产物；核对后手动继续")
                if interrupted:
                    logger.warning("停机时暂停 %d 个执行中断的任务", interrupted)
            except Exception:
                logger.exception("停机时回收执行中任务失败；下次启动会再补")
        # 管理员对话的在跑回合先收掉（它会调模型和工具）
        chat, self.admin_chat = self.admin_chat, None
        if chat is not None:
            try:
                await chat.close()
            except Exception:
                logger.exception("关闭管理员对话出错，改为直接取消")
                try:
                    chat.cancel_all()
                except Exception:
                    logger.exception("取消管理员对话回合出错")
        self.admin_pending = None
        if self.console is not None:
            try:
                await self.console.stop()
            except Exception:
                logger.exception("关闭网页出错")
            self.console = None
        self.avatar = None
        if self.jev is not None:
            try:
                await self.jev.close()
            except Exception:
                logger.exception("关闭 Jev 客户端出错")
            self.jev = None
        if self.models is not None:
            try:
                await self.models.close()
            except Exception:
                logger.exception("关闭模型客户端出错")
            self.models = None
        if self.env is not None:
            # M4：direct 模式的后台子进程 / 日志句柄 / kill 定时器要收掉（热重载不留孤儿）
            try:
                close = getattr(self.env, "close", None)
                if callable(close):
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
            except Exception:
                logger.exception("收本机执行环境出错")
        if self.search is not None:
            # tavily_mcp 的 MCP 客户端里缓存着会话和 httpx 连接；热重载/停用时要收掉
            try:
                close = getattr(self.search, "close", None)
                if callable(close):
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
            except Exception:
                logger.exception("关闭搜索客户端出错")
        if self.railway is not None:
            # 一次性 VM：手上那台放掉（本地删 key 目录 + 清 kv 标记；远端只能等官方回收）
            try:
                box = getattr(self.railway, "current_box", None)
                if box is not None:
                    result = self.railway.release(box)
                    if asyncio.iscoroutine(result):
                        await result
            except Exception:
                logger.exception("释放一次性 VM 出错")
            try:
                close = getattr(self.railway, "close", None)
                if callable(close):
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
            except Exception:
                logger.exception("收一次性 VM 环境出错")
            self.railway = None
        self.ssh = None
        if self.extensions is not None:
            # MCP 扩展缓存着会话和 httpx 连接；热重载/停用时要收掉
            try:
                close = getattr(self.extensions, "close", None)
                if callable(close):
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
            except Exception:
                logger.exception("收 MCP 扩展出错")
            self.extensions = None
        self.skills = None
        self.identity = None
        self.specialists = None
        self.agents = None
        self.search = None
        self.tools = None
        self.workers = None
        self.mentions = None
        self.pushes = None
        self.topics = None
        self.feeds = None
        self.scheduler = None
        self.tasks = None
        self.goals = None
        self.approvals = None
        self.auto_review = None
        self.goal_proposer = None
        self.env = None
        self.railway = None
        self.outbox = None
        self.card_push = None
        self.idea_mention = None
        self.delivery = None
        self.herenow = None
        self.coordinator = None
        self.commands = None
        self.group_admins = None
        self.personal = None
        self._running_tasks.clear()
        self.profiles = None
        self.host = None
        self._intake = None
        if self.store is not None:
            try:
                self.store.close()
            except Exception:
                logger.exception("关闭数据库出错")
            self.store = None
        self._listen = None
        self._session_to_served_group.clear()
        self._ambiguous_sessions.clear()
        self.signals.take()  # 清掉残留信号
        self._started = False

    async def update_config(self, raw_config: Any) -> None:
        new_settings, problems = load_settings(raw_config)
        if (
            self._started
            and self._settings is not None
            and new_settings == self._settings
            and list(problems) == list(self.problems)
        ):
            # 同一份内容（网页写文件后本进程已应用、宿主文件监控随后再发一次）：
            # 幂等跳过，不重复刷日志刷状态
            return
        self.problems = problems
        if not self._started:
            # 没启动（包括从未启动、或启动失败）：新配置 enabled → 直接按它 start
            self._raw_config = raw_config
            self._settings = new_settings
            if new_settings.enabled:
                await self.start()
            return
        if not new_settings.enabled:
            # 开着 → 关掉：停循环、关控制台、关模型客户端、关库
            logger.info("MaiWork 被关闭，正在收起")
            self._raw_config = raw_config
            await self.stop()
            return
        # 开着改配置：更新 settings；console.listen 变了就重启控制台
        old_settings = self._settings
        self._raw_config = raw_config
        self._settings = new_settings
        for p in problems:
            logger.warning("配置问题：%s", p)
        # 执行方式跟着配置变（run_as / workspace_root）：重判一次，工作区根跟着换
        # （判定结果变了会记一行中文日志提示要重启才完全生效）
        try:
            self._detect_local_capability()
        except Exception:
            logger.exception("配置更新后重判本机执行能力出错")
        self._drop_command_tools_if_stopped(self.tools)
        self._ensure_groups()
        self._refresh_served_sessions()
        # 配置里删掉的群：残留就地标记（发件 cancelled / 任务 cancelled / 目标 cancelled / 待批 expired）
        try:
            self._reconcile_unserved()
        except Exception:
            logger.exception("配置更新后回收非服务群残留出错")
        # 不再服务的群的收消息信号也清掉（planner 备忘钩子凭信号认群）
        self._drop_unserved_signals()
        if new_settings.console.listen != self._listen and self.console is not None:
            host, port = new_settings.console.listen
            await self.console.stop()
            ok = await self.console.start(host, port)
            self._listen = new_settings.console.listen if ok else None
        # config 密码被删掉（回默认）时，自动生成的密码要补回来（启动时生成过、后来
        # 改密码时被清掉的情况）；有 config 密码时 ensure_password 自己什么都不做
        if self.console is not None and new_settings.console.listen == self._listen:
            try:
                from .console.server import AUTH_KEY as _AUTH

                self.console.app[_AUTH].ensure_password(new_settings.data_dir)
            except Exception:
                logger.exception("补自动生成的管理员密码出错")
        # 群空间开关热生效（以前标 applies=reload；宿主只发 on_config_update 不重载插件，
        # 所以在这里做）：开→关 摘掉组件；关→开 现建现探测
        try:
            await self._reconcile_group_space(old_settings, new_settings)
        except Exception:
            logger.exception("群空间热更新出错")
        logger.info("MaiWork 配置已更新")

    async def _reconcile_group_space(self, old_settings: Settings | None, new_settings: Settings) -> None:
        old_on = bool(getattr(getattr(old_settings, "group_space", None), "enabled", True)) if old_settings else True
        new_on = bool(getattr(new_settings.group_space, "enabled", True))
        if old_on == new_on:
            return
        if not new_on:
            # 关掉：摘掉组件（tools 里的群空间工具按 self.group_space 现状注册，
            # 已注册的调用会拿到 None 组件自然失败，下轮 update 不再探测）
            self.group_space = None
            logger.info("群空间已关闭（不再探测；群文件 / 公告 / 相册不可用）")
            return
        # 打开：现建现探测（失败不拖垮配置更新，能力先全关）
        self.group_space = self._make_group_space()
        if self.group_space is not None:
            try:
                if self.outbox is not None:
                    self.outbox.set_group_file_hook(self.group_space.register_owned)
            except Exception:
                logger.exception("挂群文件登记 hook 出错（热更新）")
            try:
                await self.group_space.probe()
            except Exception:
                logger.exception("群空间探测出错（热更新）")
            try:
                from .tools_groupspace import register_groupspace_tools

                if self.tools is not None:
                    register_groupspace_tools(
                        self.tools, self.group_space, announce=self._groupspace_announce,
                        get_settings=self.get_settings,
                    )
            except Exception:
                logger.exception("注册群空间工具出错（热更新）")
            logger.info("群空间已开启（完成探测）")

    async def apply_config_text(self, text: str) -> None:
        """网页写 config.toml 后立刻在本进程应用（不等宿主的文件监控）。

        走和 on_config_update 完全一样的路径：文本 → dict → update_config。
        宿主随后发的 on_config_update 是同一份内容，update_config 里有幂等比较，
        不会重复刷日志刷状态。
        """
        import tomlkit

        try:
            data = dict(tomlkit.parse(text))
        except Exception:
            logger.exception("刚写进 config.toml 的内容解析失败（文件坏了？）")
            return
        await self.update_config(data)

    def config_file_ops(self) -> tuple[Path, Path]:
        """(插件目录, 数据目录)：给 rules.save_config_patch / reset_config_field 定位 config.toml。"""
        assert self._settings is not None
        return self.plugin_dir, Path(self._settings.data_dir)

    def _write_config_and_apply(self, flat: dict[str, Any]) -> Any:
        """Models.save 的写配置钩子：同步写文件，应用排成后台任务。

        Models.save 是同步接口（网页路由是唯一生产调用点，保存后会自己
        await svc.apply_config_text 一次保证返回已生效；这里排的后台任务
        和宿主的文件监控补的 on_config_update 都是同一份内容，幂等）。
        """
        from . import config_file

        plugin_dir, data_dir = self.config_file_ops()
        text = config_file.write_fields(plugin_dir, data_dir, flat)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.apply_config_text(text))
        except RuntimeError:
            # 没有运行中的事件循环（测试里同步调用）：应用交给宿主的文件监控
            pass
        return text

    # ------------------------------------------------------------------
    # 收消息
    # ------------------------------------------------------------------

    async def on_message(self, kwargs: dict) -> dict:
        """钩子本体；async（@ 时钩子里等 Jev ≤ timeout_ms）；永远返回 {"action": "continue"}。"""
        try:
            if not self._started or self._intake is None:
                return dict(_CONTINUE)
            return await self._intake.handle(kwargs)
        except Exception:
            logger.exception("收消息钩子出错，已吞掉")
            return dict(_CONTINUE)

    # ------------------------------------------------------------------
    # planner 备忘注入（maisaka.planner.before_request）
    # ------------------------------------------------------------------

    def on_planner_before_request(self, kwargs: dict) -> dict:
        """planner 钩子本体。返回 {"action":"continue"} 或 {"action":"continue","modified_kwargs":…}。

        写法出处：reference/maibot_sdk-2.8.1/maibot_sdk/components.py HookHandler
        docstring 里的 BLOCKING 钩子示例（回写用 modified_kwargs，整体替换 kwargs）。
        非服务群零读取（连库都不碰）；任何异常一律 continue。
        """
        try:
            if not self._started or self.mentions is None:
                return dict(_CONTINUE)
            session_id = ""
            gid = ""
            if isinstance(kwargs, dict):
                session_id = str(kwargs.get("session_id") or "").strip()
            if session_id:
                # 非服务群：只查内存里的信号/持久会话映射，不读库、不调宿主；
                # G4：信号可能是热更新删群前的残留，查到群后必须用 is_served 复核
                gid = self._group_for_session_cached(session_id)
                if not gid:
                    return dict(_CONTINUE)
                if self._settings is None or not self._settings.is_served(gid):
                    return dict(_CONTINUE)
            modified = self.mentions.inject(kwargs, group_id=gid)
            if isinstance(modified, dict):
                return {"action": "continue", "modified_kwargs": modified}
            return dict(_CONTINUE)
        except Exception:
            logger.exception("planner 备忘钩子出错，已吞掉")
            return dict(_CONTINUE)

    def _group_for_session_cached(self, session_id: str) -> str:
        """session_id → 服务群号；钩子中只查内存，不查未知群的数据库行。

        signals 会被后台循环消费；持久映射只在启动时读服务群的库行、或
        记录服务群新消息时更新，所以重启和信号消耗之后备忘仍然能注入。
        """
        if session_id in self._ambiguous_sessions:
            return ""  # 同一个会话号指向多个服务群，宁可不注入
        try:
            signals = getattr(self.signals, "_map", None)
            if isinstance(signals, dict):
                matches = {str(gid) for gid, sig in signals.items()
                           if str(getattr(sig, "session_id", "") or "") == session_id}
                cached = self._session_to_served_group.get(session_id)
                if len(matches) > 1 or (matches and cached and cached not in matches):
                    self._ambiguous_sessions.add(session_id)
                    self._session_to_served_group.pop(session_id, None)
                    return ""
                if matches:
                    return next(iter(matches))
        except Exception:
            pass
        return self._session_to_served_group.get(session_id, "")

    def _refresh_served_sessions(self) -> None:
        """只从配置里的服务群加载会话号；hook 对未知会话不碰数据库。"""
        self._session_to_served_group = {}
        self._ambiguous_sessions.clear()
        if self.store is None or self._settings is None:
            return
        for gid in self._settings.groups:
            try:
                row = self.store.read().execute(
                    "SELECT session_id FROM groups WHERE group_id=?", (str(gid),)
                ).fetchone()
                if row is not None and row["session_id"]:
                    sid = str(row["session_id"])
                    if sid in self._ambiguous_sessions:
                        continue
                    previous = self._session_to_served_group.get(sid)
                    if previous is not None and previous != str(gid):
                        self._session_to_served_group.pop(sid, None)
                        self._ambiguous_sessions.add(sid)
                    else:
                        self._session_to_served_group[sid] = str(gid)
            except Exception:
                logger.exception("恢复服务群会话号失败（群 %s）", gid)

    def _remember_session_row(self, gid: str, session_id: str, last_ts: float) -> None:
        """把 session_id / last_msg_ts 记进 groups 表（planner 钩子的回落来源）。"""
        if self.store is None or not session_id or self._settings is None or not self._settings.is_served(str(gid)):
            return
        try:
            with self.store.tx() as conn:
                conn.execute(
                    "UPDATE groups SET session_id=?, last_msg_ts=MAX(COALESCE(last_msg_ts,0), ?) WHERE group_id=?",
                    (str(session_id), float(last_ts), str(gid)),
                )
            sid = str(session_id)
            previous = self._session_to_served_group.get(sid)
            self._session_to_served_group = {
                old_sid: group for old_sid, group in self._session_to_served_group.items()
                if group != str(gid)
            }
            if previous is not None and previous != str(gid):
                self._ambiguous_sessions.add(sid)
                self._session_to_served_group.pop(sid, None)
            elif sid not in self._ambiguous_sessions:
                self._session_to_served_group[sid] = str(gid)
        except Exception:
            logger.debug("记 groups.session_id 失败（群 %s）", gid, exc_info=True)

    # ------------------------------------------------------------------
    # 服务群建行
    # ------------------------------------------------------------------

    def _ensure_groups(self) -> None:
        """为每个服务群建 groups 行、生成 8 位链接码（幂等）。"""
        if self.store is None or self._settings is None:
            return
        now = _now()
        with self.store.tx() as conn:
            for gid, gsetting in self._settings.groups.items():
                row = conn.execute("SELECT token, workspace FROM groups WHERE group_id=?", (gid,)).fetchone()
                if row is None:
                    token = self._new_group_token(conn)
                    conn.execute(
                        "INSERT INTO groups (group_id, workspace, token, created) VALUES (?, ?, ?, ?)",
                        (gid, gsetting.workspace, token, now),
                    )
                    self.store.event(conn, "group.added", group_id=gid, payload={"workspace": gsetting.workspace})
                else:
                    if not row["token"]:
                        conn.execute(
                            "UPDATE groups SET token=? WHERE group_id=?",
                            (self._new_group_token(conn), gid),
                        )
                    if row["workspace"] != gsetting.workspace:
                        conn.execute(
                            "UPDATE groups SET workspace=? WHERE group_id=?",
                            (gsetting.workspace, gid),
                        )

    def _new_group_token(self, conn: Any) -> str:
        for _ in range(20):
            token = "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(8))
            row = conn.execute("SELECT 1 FROM groups WHERE token=?", (token,)).fetchone()
            if row is None:
                return token
        raise RuntimeError("生成群链接码失败（撞了 20 次）")

    # ------------------------------------------------------------------
    # 非服务群残留回收（不删数据）
    # ------------------------------------------------------------------

    def _drop_unserved_signals(self) -> None:
        """清掉不再服务的群的内存信号（planner 备忘钩子凭它认群，不能让它认出非服务群）。"""
        if self._settings is None:
            return
        try:
            signals = getattr(self.signals, "_map", None)
            if not isinstance(signals, dict):
                return
            for gid in list(signals.keys()):
                if not self._settings.is_served(str(gid)):
                    signals.pop(gid, None)
        except Exception:
            logger.exception("清非服务群信号出错")


    def _reconcile_unserved(self) -> None:
        """配置里不再服务的群：库里残留就地标记（不删数据）。

        - pending 发件 → cancelled（error 写「这个群已不在服务列表」）；
        - 所有非终态任务（含 running / reviewing / paused）→ cancelled，停当前执行；
        - active 目标 → cancelled；
        - pending 待批 → expired。
        启动和热更新都跑一遍；非服务群的零派工/零发送由它 + 各巡检入口的
        is_served 兜底（_tasks_round / _goals_round / _approval_round / outbox.flush）
        双保险。
        """
        if self.store is None or self._settings is None:
            return
        settings = self._settings
        reason = "这个群已不在服务列表"
        now = _now()
        # 1) 发件箱 pending → cancelled
        if self.outbox is not None:
            try:
                rows = self.store.read().execute(
                    "SELECT DISTINCT group_id FROM outbox WHERE status='pending'"
                ).fetchall()
                for r in rows:
                    gid = str(r["group_id"])
                    if settings.is_served(gid):
                        continue
                    try:
                        n = self.outbox.cancel_group_pending(gid, reason=reason)
                        if n:
                            logger.info("群 %s 已不在服务列表：%d 条待发件标 cancelled", gid, n)
                    except Exception:
                        logger.exception("回收发件箱失败（群 %s）", gid)
            except Exception:
                logger.exception("回收发件箱查询失败")
        # 2) 非终态任务全部取消；running/reviewing 的子 agent 协程也要停。
        if self.tasks is not None:
            try:
                rows = self.store.read().execute(
                    "SELECT id, group_id FROM tasks WHERE status IN "
                    "('pending_approval', 'queued', 'running', 'reviewing', "
                    "'waiting_input', 'shelved', 'paused')"
                ).fetchall()
                for r in rows:
                    gid = str(r["group_id"])
                    if settings.is_served(gid):
                        continue
                    try:
                        self.tasks.transition(str(r["id"]), "cancelled", reason=reason)
                        self.cancel_task_run(str(r["id"]))
                        logger.info("群 %s 已不在服务列表：任务 %s 标 cancelled", gid, r["id"])
                    except Exception:
                        logger.exception("回收任务失败（%s）", r["id"])
            except Exception:
                logger.exception("回收任务查询失败")
        # 3) 目标 active → cancelled
        if self.goals is not None:
            try:
                rows = self.store.read().execute(
                    "SELECT id, group_id FROM goals WHERE state='active'"
                ).fetchall()
                for r in rows:
                    gid = str(r["group_id"])
                    if settings.is_served(gid):
                        continue
                    try:
                        self.goals.cancel(str(r["id"]))
                        logger.info("群 %s 已不在服务列表：目标 %s 标 cancelled", gid, r["id"])
                    except Exception:
                        logger.exception("回收目标失败（%s）", r["id"])
            except Exception:
                logger.exception("回收目标查询失败")
        # 4) 待批 pending → expired
        try:
            with self.store.tx() as conn:
                rows = conn.execute(
                    "SELECT id, group_id FROM requests WHERE status='pending'"
                ).fetchall()
                for r in rows:
                    gid = str(r["group_id"])
                    if settings.is_served(gid):
                        continue
                    conn.execute(
                        "UPDATE requests SET status='expired', updated=? WHERE id=?",
                        (now, str(r["id"])),
                    )
                    self.store.event(
                        conn, "request.expired", group_id=gid,
                        entity="request", entity_id=str(r["id"]),
                        payload={"reason": reason},
                    )
        except Exception:
            logger.exception("回收待批请求失败")

    def token_of(self, group_id: str) -> str:
        if self.store is None:
            return ""
        row = self.store.read().execute("SELECT token FROM groups WHERE group_id=?", (group_id,)).fetchone()
        return str(row["token"]) if row is not None and row["token"] else ""

    def stale_goals_count(self, group_id: str) -> int:
        """这个群「卡住」的 agent 目标数（网页总览用；「模型没配好，暂停检查」不算卡住）。

        字段来源：goals.view 的 agent 项里的 "stale"；卡住原因在 "stale_reason"，
        最近心跳在 "heartbeat_ts"（没报过平安是 None）。
        """
        if self.goals is None:
            return 0
        try:
            view = self.goals.view(str(group_id))
        except Exception:
            logger.exception("数卡住目标出错（群 %s）", group_id)
            return 0
        agent = view.get("agent", []) if isinstance(view, dict) else []
        return sum(1 for g in agent if isinstance(g, dict) and g.get("stale"))

    def reset_group_token(self, group_id: str) -> str:
        """重置群链接码；旧链接即刻失效。返回新 token（群行不存在返回 ""）。"""
        if self.store is None:
            return ""
        with self.store.tx() as conn:
            row = conn.execute("SELECT group_id FROM groups WHERE group_id=?", (group_id,)).fetchone()
            if row is None:
                return ""
            token = self._new_group_token(conn)
            conn.execute("UPDATE groups SET token=? WHERE group_id=?", (token, group_id))
            self.store.event(conn, "group.token_reset", group_id=group_id)
        return token

    # ------------------------------------------------------------------
    # 后台循环
    # ------------------------------------------------------------------

    def _make_profiles(self) -> Any:
        factory = self.profiles_factory
        cls = factory if factory is not None else self.profiles_cls
        if cls is None:
            return _NullProfiles()
        return cls(self.store, self.host, self.models, self.get_settings)

    def _make_personas(self) -> Any:
        """persona.py 新模块；没就位 / 构造失败就 None，个人画像这轮不做。"""
        factory = getattr(self, "personas_factory", None)
        cls = factory if factory is not None else _import_m2_class("persona", "Personas")
        if cls is None:
            return None
        try:
            return cls(self.store, self.host, self.models, self.get_settings)
        except Exception:
            logger.exception("建 Personas 出错，关注成员个人画像这次跳过")
            return None

    def _make_personal(self) -> Any:
        """personal.py（关注成员个人向产出）；没就位 / 构造失败就 None，这个功能跳过。"""
        factory = getattr(self, "personal_factory", None)
        cls = factory if factory is not None else _import_m2_class("personal", "Personal")
        if cls is None:
            return None
        try:
            return cls(
                self.store, self.models, self.workers, self.profiles, self.topics,
                self.get_settings, search=self.search, host=self.host, identity=self.identity,
            )
        except Exception:
            logger.exception("建 Personal 出错，关注成员个人向产出这次跳过")
            return None

    def _make_feeds(self) -> Any:
        """feeds.py 由另一条线同时开发；模块没就位就 None（路由 503、视图空列表）。

        on_start 回调：构想「做这个」→ 落成任务并开工（M3 接线，见 _on_idea_started）。
        """
        factory = self.feeds_factory
        cls = factory if factory is not None else self.feeds_cls
        if cls is None:
            return None
        try:
            return cls(
                self.store, self.models, self.workers, self.profiles, self.topics,
                self.get_settings, search=self.search, host=self.host, on_start=self._on_idea_started,
                verify_runner=self._make_verify_runner(), identity=self.identity,
                rss_transport=self.rss_transport,
            )
        except Exception:
            logger.exception("建 Feeds 出错，资讯/构想这次跳过")
            return None

    def _make_goal_proposer(self) -> Any:
        """GoalProposer（主动提目标）；模块没就位 / 构造失败就 None，这块功能跳过。"""
        try:
            from .goal_proposal import GoalProposer

            return GoalProposer(
                self.store, self.models, self.goals, self.approvals, self.get_settings,
                profiles=self.profiles, identity=self.identity,
            )
        except Exception:
            logger.exception("建 GoalProposer 出错，主动提目标这次跳过")
            return None

    def _make_agents(self) -> Any:
        """Agents（agents.py，A 负责）；模块没就位 / 构造失败 → None（server 503、专岗全停）。"""
        try:
            factory = getattr(self, "agents_factory", None)
            if factory is not None:
                return factory(self.store, self.get_settings)
        except Exception:
            logger.exception("用 agents_factory 建 Agents 出错")
            return None
        try:
            from .agents import Agents

            return Agents(self.store, self.get_settings)
        except Exception:
            logger.info("专岗 Agents 还没就位（模块未提供或构造出错），专岗功能这次跳过")
            return None

    def _make_specialists(self) -> Any:
        """Specialists（specialists.py，B 负责）；agents 没就位也不能建 → None。"""
        agents = self.agents
        if agents is None or self.workers is None:
            return None
        try:
            factory = getattr(self, "specialists_factory", None)
            if factory is not None:
                return factory(agents, self.workers, self.skills)
        except Exception:
            logger.exception("用 specialists_factory 建 Specialists 出错")
            return None
        try:
            from .specialists import Specialists

            return Specialists(agents, self.workers, self.skills)
        except Exception:
            logger.info("专岗 Specialists 还没就位（模块未提供或构造出错），专岗功能这次跳过")
            return None

    def _wire_specialists(self) -> None:
        """把同一份 Specialists 挂到使用它的三个管线（feeds / goal_proposer / coordinator）。

        没就位 → 三处保持 None（老路）；就位 → 生产强制使用专岗，不静默换通才。
        """
        specialists = self.specialists
        if specialists is None:
            return
        wired = []
        try:
            if self.feeds is not None:
                self.feeds._specialists = specialists  # noqa: SLF001
                wired.append("feeds")
        except Exception:
            logger.exception("给 feeds 挂 specialists 出错")
        try:
            if self.goal_proposer is not None:
                self.goal_proposer._specialists = specialists  # noqa: SLF001
                wired.append("goal_proposer")
        except Exception:
            logger.exception("给 goal_proposer 挂 specialists 出错")
        try:
            if self.coordinator is not None:
                self.coordinator._specialists = specialists  # noqa: SLF001
                wired.append("coordinator")
        except Exception:
            logger.exception("给 coordinator 挂 specialists 出错")
        if wired:
            logger.info("专岗已接线（%s）", ",".join(wired))

    def _make_auto_review(self) -> Any:
        """自动审核（auto_review.py）；模块没就位 / 构造失败就 None，这块功能跳过。

        开工入口复用 self.spawn_run_task（`/mw 批准` 和网页批准也是这条）。
        """
        try:
            from .auto_review import AutoReviewer

            return AutoReviewer(
                self.store, self.models, self.approvals, self.get_settings,
                run_task_starter=self.spawn_run_task,
            )
        except Exception:
            logger.exception("建 AutoReviewer 出错，派活自动审核这次跳过")
            return None

    def _wire_auto_review(self) -> None:
        """把「刚记下一条待批请求」的回调挂到 Approvals 上。

        回调只登记（spawn 后台协程），所以收消息钩子永远不会被审核卡住。
        """
        reviewer = self.auto_review
        approvals = self.approvals
        if reviewer is None or approvals is None:
            return
        try:
            approvals.set_review_hook(self._spawn_auto_review)
        except Exception:
            logger.exception("挂自动审核回调出错，派活自动审核这次不生效")

    def _spawn_auto_review(self, request_id: str, group_id: str) -> None:
        """Approvals.create 落了 pending 之后的回调：后台审一次（失败只记日志）。"""
        reviewer = self.auto_review
        if reviewer is None:
            return
        rid = str(request_id or "")
        if not rid:
            return
        self._spawn_bg(reviewer.review(rid, group_id=str(group_id or "")), name=f"maiwork-review-{rid}")

    def _make_identity(self) -> Any:
        """identity.py（身份与工作记忆）；没就位 / 构造失败就 None，各注入点自动跳过。"""
        try:
            from .identity import Identity

            return Identity(self.get_settings().data_dir, self.store, self.get_settings, host=self.host)
        except Exception:
            logger.exception("建 Identity 出错，身份/工作记忆这次跳过")
            return None

    def note_useless_feedback(self, group_id: str, item_id: int) -> None:
        """console 的「没用」反馈钩子：有 Identity 就检查「累计 3 次 → 记进本群记忆」。"""
        identity = self.identity
        if identity is None:
            return
        try:
            identity.note_useless_feedback(str(group_id), int(item_id))
        except Exception:
            logger.exception("反馈自动记出错（群 %s 条 %s，不影响反馈本身）", group_id, item_id)

    def _make_verify_runner(self) -> Any:
        """资讯实测的注入闭包（Feeds 的 verify_runner 契约：async (items, picks, gid, settings)）。

        没建起 RailwayEnv（railway=false / 模块没就位）→ None，Feeds 整段跳过实测；
        有 → 真跑 feeds.run_railway_verify（它自己 acquire / 逐条派子 agent / release）。
        """
        env = self.railway
        if env is None:
            return None

        async def _runner(items: list, picks: list, gid: str, settings: Any) -> None:
            from .feeds import run_railway_verify

            await run_railway_verify(
                items, picks, gid, settings,
                workers=self.workers, tools=self.tools, env=env,
            )

        return _runner

    def _make_scheduler(self) -> Any:
        """scheduler.py 由另一条线同时开发；模块没就位就 None。"""
        factory = self.scheduler_factory
        cls = factory if factory is not None else self.scheduler_cls
        if cls is None:
            return None
        try:
            return cls(self.store, self.get_settings)
        except Exception:
            logger.exception("建 Scheduler 出错，排程这次跳过")
            return None

    # ------------------------------------------------------------------
    # M3 接线（docs/07 §11）
    # ------------------------------------------------------------------

    def _detect_local_capability(self) -> Any:
        """启动时判定一次本机执行能力（记一行中文日志；配置改了下次重判）。

        探测真跑 environments/capability.probe；测试用 capability_probe 注入假判定。
        """
        from .environments import capability as _cap

        settings = self.get_settings()
        run_as = str(getattr(getattr(settings, "environments", None), "run_as", "maiwork") or "maiwork")
        probe = self.capability_probe or _cap.probe
        try:
            dec = probe(run_as)
        except Exception:
            logger.exception("本机执行能力探测失败，按「受限」处理")
            dec = _cap.Decision(
                mode="stopped", ok=False, exec_kind="plugin", unit_user="",
                reason="探测失败", hint="本机跑命令的活这次用不了",
                log_line="本机干活：不能用——探测本机执行能力失败",
            )
        old = self.capability
        self.capability = dec
        try:
            self._ws_root = self._workspace_root_for(dec)
        except Exception:
            logger.exception("算实际工作区根出错，按配置里的来")
            self._ws_root = None
        logger.info("%s", dec.log_line)
        if old is not None and getattr(old, "mode", "") != dec.mode:
            logger.info(
                "本机干活：执行方式从「%s」变成「%s」——起子 agent 的方式要重启插件才完全生效",
                getattr(old, "mode", ""), dec.mode,
            )
        return dec

    def _workspace_root_for(self, dec: Any) -> Path:
        """按判定结果定工作区根（受限→数据目录下；dynamic→/var/lib/private；fixed→配置）。

        读的是**没套修正**的有效配置（_effective_settings），套了会自循环。
        """
        from .environments import capability as _cap

        settings = self._effective_settings()
        cfg_root = Path(getattr(getattr(settings, "environments", None), "workspace_root", "") or "")
        data_dir = Path(getattr(settings, "data_dir", "") or ".")
        return _cap.resolve_workspace_root(cfg_root, dec, data_dir=data_dir)

    def _drop_command_tools_if_stopped(self, tools: Any) -> None:
        """「受限」（本机不能隔离跑命令）→ 摘掉跑命令类工具，文件工具保留。

        工具一开始按全量注册；判定结果出来后再摘。配置热更新时本函数幂等
        （unregister 对不存在的名字返回 False），所以每次启动/热更新都安全调用。
        """
        cap = self.capability
        if cap is None or getattr(cap, "ok", False):
            return
        for name in ("run_command", "start_process", "check_process", "stop_process"):
            try:
                tools.unregister(name)
            except Exception:
                logger.exception("摘工具 %s 失败", name)

    def _make_env(self) -> Any:
        """本机执行环境；模块没就位 / 建不起来就 None（网页健康里能看到）。

        先判定执行能力（_detect_local_capability 已跑过，结果在 self.capability）：
        - fixed/dynamic：LocalEnv(判定) —— 工作区根由 get_settings 统一换成实际根；
        - stopped：照样建 LocalEnv（工作区在数据目录下，子 agent 读写文件要用），
          但跑命令类工具随后被 _drop_command_tools_if_stopped 摘掉。
        """
        factory = self.env_factory
        cls = factory if factory is not None else _import_m2_class("environments.local", "LocalEnv")
        if cls is None:
            return None
        try:
            try:
                return cls(self.get_settings, capability=self.capability)
            except TypeError:
                return cls(self.get_settings)
        except Exception:
            logger.exception("建本机执行环境出错，派活这次跳过")
            return None

    def _make_railway(self) -> Any:
        """一次性 VM 执行环境（docs/07 §11.1b）：railway=true 才建 + 注册 vm 工具。

        - [environments] railway=false / environments 节都没有 / 模块没就位 → None；
        - 建起来就把 vm_run / vm_put_file / vm_read_file 注册进 tools（worker 角色），
          get_box 现读「当前占着的那台」（RailwayEnv.current_box，同时只许 1 台）——
          实测流程 acquire 之后就能看到机器，release 之后自动变 None。
        """
        settings = self.get_settings()
        env_cfg = getattr(settings, "environments", None)
        if env_cfg is None or not bool(getattr(env_cfg, "railway", True)):
            return None
        factory = self.railway_factory
        cls = factory if factory is not None else _import_m2_class("environments.railway", "RailwayEnv")
        if cls is None:
            return None
        try:
            env = cls(self.get_settings, settings.data_dir, store=self.store)
        except Exception:
            logger.exception("建 RailwayEnv 出错，一次性 VM 实测这次跳过")
            return None
        try:
            from .tools_railway import register_vm_tools

            register_vm_tools(self.tools, get_box=lambda: getattr(env, "current_box", None), env=env)
        except Exception:
            logger.exception("注册 vm 工具出错，一次性 VM 实测这次跳过")
            return None
        return env

    def _make_ssh(self) -> Any:
        """专用 SSH 机器执行环境 + machine_* 工具；建不起来 → None（派活就只剩本机 / 一次性 VM）。"""
        cls = self.ssh_factory if self.ssh_factory is not None else _import_m2_class("environments.ssh", "SshEnv")
        if cls is None:
            return None
        try:
            env = cls(self.get_settings, self.get_settings().data_dir)
        except Exception:
            logger.exception("建专用机器环境出错，这次不用专用机器")
            return None
        try:
            from .tools_ssh import register_machine_tools

            register_machine_tools(
                self.tools, get_box=lambda tid: env.box_for(tid), env=env, local_env=self.env,
            )
        except Exception:
            logger.exception("注册 machine 工具出错，这次不用专用机器")
            return None
        return env

    def _maybe_check_ssh(self, now: float) -> None:
        """每 30 分钟（或机器名单变了）在后台把专用机器连一遍，结果给网页「运行状态」。"""
        ssh = self.ssh
        if ssh is None:
            return
        try:
            sig = tuple((m.get("name"), m.get("host")) for m in ssh.machines())
        except Exception:
            sig = None
        if sig == self._ssh_check_sig and now - self._ssh_check_ts < 1800:
            return
        self._ssh_check_sig = sig
        self._ssh_check_ts = now
        self._spawn_bg(ssh.check_all(), name="maiwork-ssh-check")

    def _make_herenow(self) -> Any:
        module_cls = _import_m2_class("herenow", "HereNow")
        if module_cls is None:
            return None
        try:
            return module_cls(transport=self.herenow_transport)
        except Exception:
            logger.exception("建 HereNow 出错，网页链接交付这次跳过")
            return None

    def _make_coordinator(self) -> Any:
        """coordinator.py 由另一位同事同时写；模块/构造失败就 None（任务留在 queued，网页可见）。"""
        factory = self.coordinator_factory
        cls = factory if factory is not None else self.coordinator_cls
        if cls is None:
            return None
        try:
            return cls(
                self.store, self.models, self.workers, self.tools, self.tasks,
                self.goals, self.delivery, self.outbox, self.env, self.profiles,
                self.get_settings, host=self.host, railway=self.railway,
                group_space=self.group_space, identity=self.identity,
                capability=self.capability, ssh=self.ssh,
            )
        except TypeError:
            # 老的 / 测试替身 Coordinator 不认 ssh 参数：不带它再建一次
            try:
                return cls(
                    self.store, self.models, self.workers, self.tools, self.tasks,
                    self.goals, self.delivery, self.outbox, self.env, self.profiles,
                    self.get_settings, host=self.host, railway=self.railway,
                    group_space=self.group_space, identity=self.identity,
                    capability=self.capability,
                )
            except Exception:
                logger.exception("建 Coordinator 出错，任务执行这次跳过")
                return None
        except Exception:
            logger.exception("建 Coordinator 出错，任务执行这次跳过")
            return None

    def _make_commands(self) -> Any:
        try:
            from .commands import Commands

            return Commands(
                self.store, self.approvals, self.tasks, self.goals, self.outbox,
                self.host, self.get_settings,
                coordinator=self.coordinator,
                run_task_starter=self.spawn_run_task,
                run_task_stopper=self.cancel_task_run,
                group_admins=self.group_admins,
            )
        except Exception:
            logger.exception("建 Commands 出错，/mw 指令这次跳过")
            return None

    def _make_intake(self) -> Intake:
        return Intake(
            self.get_settings,
            self.signals,
            jev=self.jev,
            approvals=self.approvals,
            mentions=self.mentions,
            commands=self.commands,
            on_reminder=self.on_reminder,
            bot_qq=self._bot_qq_now,
            bot_account=lambda plat: self.host.cached_bot_account(plat) if self.host else "",
            spawn=self._spawn_bg,
            store=self.store,
            on_answer=self._resume_from_answer,
            waiting_tasks=self._waiting_answer_map,
        )

    # ------------------------------------------------------------------
    # 提问的回答恢复（docs/02 §7.2）
    # ------------------------------------------------------------------

    def _on_ask_sent(self, key: str, group_id: str, task_id: Any, message_id: str) -> None:
        """outbox 把 ask:{task_id}:{attempt} 的提问发出去了 → 回写任务的 question_msg_id。

        只回写 waiting_input / shelved 的任务（别的状态的提问说明早就move on了）；
        写完让回答缓存立刻失效（question_msg_id 变了，下一轮 intake 必须看得到）。
        """
        store = self.store
        if store is None or not message_id:
            return
        tid = ""
        # key 形如 ask:{task_id}:{attempt}；task_id 为空时从 key 里拆
        if task_id:
            tid = str(task_id)
        else:
            parts = str(key or "").split(":")
            if len(parts) >= 3 and parts[0] == "ask":
                tid = parts[1]
        if not tid:
            return
        try:
            with store.tx() as conn:
                cur = conn.execute(
                    "UPDATE tasks SET question_msg_id=?, updated=?"
                    " WHERE id=? AND status IN ('waiting_input', 'shelved')",
                    (str(message_id), clock.now(), tid),
                )
                if int(cur.rowcount or 0) > 0:
                    store.event(
                        conn, "task.question_sent", group_id=str(group_id), entity="task",
                        entity_id=tid, payload={"message_id": str(message_id)},
                    )
        except Exception:
            logger.exception("回写 question_msg_id 失败（任务 %s）", tid)
            return
        self._invalidate_answer_cache()

    def _invalidate_answer_cache(self) -> None:
        """等待任务缓存立刻作废——下一次 intake 用之前重建。"""
        self._answer_cache = {}
        self._answer_cache_ts = 0.0

    def _waiting_answer_map(self, group_id: str) -> list:
        """intake 用的「群 → [(task_id, question_msg_id, requester_id)]」缓存快照。

        钩子里不做重查询：30 秒一刷；问题刚发出 / 刚被回答时会主动失效。
        返回浅拷贝，intake 随便改不影响缓存。
        """
        gid = str(group_id or "")
        if not gid:
            return []
        now = clock.now()
        if self._answer_cache_ts <= 0.0 or now - self._answer_cache_ts > 30.0:
            if self.store is not None:
                try:
                    rows = self.store.read().execute(
                        "SELECT id, group_id, question_msg_id, requester_id"
                        " FROM tasks WHERE status IN ('waiting_input', 'shelved')"
                    ).fetchall()
                    cache: dict[str, list] = {}
                    for r in rows:
                        cache.setdefault(str(r["group_id"]), []).append(
                            (
                                str(r["id"]),
                                str(r["question_msg_id"] or ""),
                                str(r["requester_id"] or ""),
                            )
                        )
                    self._answer_cache = cache
                    self._answer_cache_ts = now
                except Exception:
                    logger.exception("刷新等待任务缓存出错")
                    self._answer_cache = {}
                    self._answer_cache_ts = now
        return list(self._answer_cache.get(gid, []))

    async def _resume_from_answer(self, group_id: str, task_id: str, text: str) -> None:
        """intake 认出「这是对提问的回答」→ 后台协程里跑 coordinator.resume。"""
        coord = self.coordinator
        if coord is None:
            return
        # 只对服务群的等待任务生效（intake 已按服务群过滤，这里再兜一层）
        try:
            if self._settings is not None and not self._settings.is_served(str(group_id)):
                return
        except Exception:
            pass
        try:
            await coord.resume(str(task_id), str(text))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("回答恢复任务出错（群 %s，任务 %s）", group_id, task_id)

    def _known_secrets(self) -> list[str]:
        """目前已知会被写进运行文本的密钥：模型 + 搜索 + MCP 扩展 headers 的值。每次现读（热更新后跟着变）。"""
        out: list[str] = []
        try:
            # 网页加的 MCP 扩展头值（secrets 表 mcp.<扩展名>.<头名>）：只进不出，并进遮罩
            if self.store is not None:
                rows = self.store.read().execute("SELECT value FROM secrets WHERE name LIKE 'mcp.%'").fetchall()
                for row in rows:
                    v_s = str(row["value"] or "")
                    if v_s:
                        out.append(v_s)
        except Exception:
            pass
        try:
            if self.search is not None:
                for v in self.search.known_secrets():
                    if v and v not in out:
                        out.append(v)
            if self._settings is not None:
                for v in (
                    getattr(self._settings.models, "api_key", ""),
                    getattr(self._settings.jev, "api_key", ""),
                    self._settings.console.password,
                ):
                    v_s = str(v or "")
                    if v_s:
                        out.append(v_s)
                # 2026-10 模型改版：所有 [[endpoints]] 的 api_key 也进遮罩（绝不进日志）
                for ep in (getattr(self._settings, "endpoints", ()) or ()):
                    v_s = str(getattr(ep, "api_key", "") or "")
                    if v_s and v_s not in out:
                        out.append(v_s)
                # [[extensions.mcp]] headers 的值：密钥只进不出，摘要统一遮罩
                mcp_entries = getattr(getattr(self._settings, "extensions", None), "mcp", ()) or ()
                for entry in mcp_entries:
                    for v in dict(getattr(entry, "headers", {}) or {}).values():
                        v_s = str(v or "")
                        if v_s:
                            out.append(v_s)
        except Exception:
            pass
        return out

    # ------------------------------------------------------------------
    # skill + MCP 扩展（docs/02 §10：MaiWork 自己加载，不挂 MaiBot planner）
    # ------------------------------------------------------------------

    async def _start_extensions(self) -> None:
        """skill 目录 + MCP 扩展：在 Workers / 工具表就位之后调用。

        - skills：Skills(data_dir) 只读视图；给 Workers 一个 skills_hint_fn（system 提示的
          「名字：一句描述」清单，最多 20 条）；注册 list_skills / read_skill 工具。
        - extensions：连 [[extensions.mcp]]（initialize → tools/list → 工具注册成
          mcp_<name>_<工具名>）；连不上不影响启动，健康状态里能看到。
        任何一步出错都不拖垮 app。
        """
        settings = self._settings
        if settings is None or self.tools is None:
            return
        try:
            from .skills import Skills
            from .skills_tools import register_skill_tools

            # 接 store/settings 后，Skills 会跟着 kv / MCP enabled 变化自动带走/回来 skill
            self.skills = Skills(settings.data_dir, store=self.store, settings=self.get_settings)
            try:
                register_skill_tools(self.tools, self.skills)
            except Exception:
                logger.exception("注册 skill 工具出错，子 agent 这次用不了 skill")
            # Workers 的默认 skill 提示（调用方 run(skills_hint=…) 可覆盖）；
            # Workers 没法全局设置时用构造参数 skills_hint_fn
            if self.workers is not None and getattr(self.workers, "_skills_hint_fn", None) is None:
                self.workers._skills_hint_fn = lambda: self.skills.hint() if self.skills is not None else ""  # noqa: SLF001
            # feeds 的搜索服务 skill（brief 注入）吃同一套开关
            try:
                if self.feeds is not None:
                    self.feeds._skills = self.skills  # noqa: SLF001
            except Exception:
                logger.exception("给 feeds 接 skills 出错，搜索 skill 提示这次照带")
        except Exception:
            logger.exception("skill 扩展就位出错，这次没有 skill 用")
            self.skills = None
        try:
            from .extensions import Extensions

            self.extensions = Extensions(self.get_settings, transport=self.extensions_transport, store=self.store)
            await self.extensions.start(self.tools)
        except Exception:
            logger.exception("MCP 扩展连接出错，这次没有扩展工具用（插件其他功能照常）")

    async def reload_mcp(self, name: str) -> dict | None:
        """网页管理员 reload 一个 MCP 扩展：None = 没配这个名字。"""
        if self.extensions is None or self.tools is None:
            return None
        return await self.extensions.reload(str(name or ""), self.tools)

    async def remove_mcp(self, name: str) -> bool:
        """网页删除一个 MCP 扩展：摘工具、关连接（extensions.remove）。没运转 → False。"""
        if self.extensions is None or self.tools is None:
            return False
        return await self.extensions.remove(str(name or ""), self.tools)

    def _models_ready(self) -> bool:
        """模型是否配好（settings.ready）。没配好时不做任何要模型的事（02 §12.1）。"""
        try:
            return bool(self.models is not None and self.models.settings().ready())
        except Exception:
            return False

    def _bot_qq_now(self) -> str:
        """intake 拿机器人 QQ：优先 Host 预热缓存，没有就现问一次（失败返回 ""）。"""
        host = self.host
        if host is None:
            return ""
        try:
            cached = str(getattr(host, "_bot_qq", "") or "")
            if cached:
                return cached
        except Exception:
            pass
        return ""

    def _session_of_group(self, group_id: str) -> str:
        """执行工具的 read_chat_history 用：groups 表里的 session_id。读不到就 ""。"""
        if self.store is None:
            return ""
        try:
            row = self.store.read().execute(
                "SELECT session_id FROM groups WHERE group_id=?", (str(group_id),)
            ).fetchone()
            return str(row["session_id"]) if row is not None and row["session_id"] else ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # M3 后台任务派工
    # ------------------------------------------------------------------

    def spawn_run_task(self, task_id: str) -> None:
        """开工一个任务（同一任务同一时刻只跑一个）。"""
        tid = str(task_id or "")
        if not tid or self.coordinator is None:
            return
        if tid in self._running_tasks:
            logger.debug("任务 %s 已在跑，不重复开工", tid)
            return
        self._running_tasks.add(tid)
        self._spawn_bg(self._run_task_guarded(tid), name=f"maiwork-task-{tid}")

    def cancel_task_run(self, task_id: str) -> bool:
        """任务被取消的统一入口：把正在跑的子 agent（maiwork-task-<tid>）立刻停掉。

        所有取消路径（网页 op=cancel、管理员工具 cancel_task、/mw 取消）在
        Tasks.transition(→cancelled) 之后都调这里；停不到也不影响状态已变——
        coordinator / workers 每步开头还会再查一次状态兜底。返回有没有真的 cancel 到。
        """
        tid = str(task_id or "")
        if not tid:
            return False
        stopped = False
        for task in list(self._bg_jobs):
            try:
                if task.get_name() == f"maiwork-task-{tid}" and not task.done():
                    task.cancel()
                    stopped = True
            except Exception:
                logger.exception("停任务 %s 的后台协程出错", tid)
        self._running_tasks.discard(tid)
        # 被停掉的协程不会再来验收：它名下没收尾的交接单收成 cancelled（尝试由
        # Tasks.transition(→cancelled) 同一事务里作废）——外部审查 2026-10-02
        if self.agents is not None:
            try:
                self.agents.cancel_unsettled(task_id=tid, why="任务已取消")
            except Exception:
                logger.exception("收任务 %s 的交接单出错", tid)
        return stopped

    async def _run_task_guarded(self, task_id: str) -> None:
        tid = str(task_id)
        try:
            if self.coordinator is not None:
                await self.coordinator.run_task(tid)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("任务 %s 执行出错", tid)
        finally:
            self._running_tasks.discard(tid)

    def _spawn_bg(self, awaitable: Any, *, name: str = "maiwork-bg") -> None:
        """通用后台派工（intake / 指令 / 网页用）：登记在 _bg_jobs，stop 时一起收。"""
        if awaitable is None:
            return
        if not (asyncio.iscoroutine(awaitable) or isinstance(awaitable, asyncio.Future)):
            return
        try:
            task = asyncio.ensure_future(awaitable, loop=asyncio.get_running_loop())
        except RuntimeError:
            # 没有运行中的事件循环：关掉协程防告警
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            return
        except Exception:
            logger.exception("后台派工出错（%s）", name)
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            return
        try:
            task.set_name(name)
        except Exception:
            pass
        self._bg_jobs.add(task)
        task.add_done_callback(self._on_bg_done)

    def _on_bg_done(self, task: asyncio.Task) -> None:
        self._bg_jobs.discard(task)
        if not task.cancelled():
            try:
                exc = task.exception()
            except Exception:
                exc = None
            if exc is not None:
                logger.exception("后台任务 %s 未捕获异常", task.get_name(), exc_info=exc)

    # ------------------------------------------------------------------
    # 构想 → 活（on_start 回调 + 网页「想要这个」）
    # ------------------------------------------------------------------

    def _on_idea_started(self, view: dict) -> None:
        """构想「做这个」：管理员直接批准 → 落成任务 / agent 目标并开工。

        2026-10：构想带 items 时按项目逐个落（kind=goal 的要 models 就位才建 agent 目标；
        没有 goals 模块时整条按老逻辑建一个任务）。网页「直接开工」可以只勾选部分项目
        （view["item_nos"]，1 起序号；越界 / 非法 / 一个都对不上 → 按全部处理，口径复用
        approvals.picked_idea_items）。feeds.idea_action 同步调用；任何异常只记日志，
        不影响构想状态变更。
        """
        try:
            if self.tasks is None or self.store is None or not isinstance(view, dict):
                return
            idea_id = int(view.get("id") or 0)
            if idea_id <= 0:
                return
            gid = self._group_of_idea(idea_id)
            if not gid:
                logger.warning("构想 #%s 找不到群，没落成任务", idea_id)
                return
            title = str(view.get("title") or "构想")
            requester = str(view.get("requested_by") or "管理员")
            items = self._picked_idea_items(view)
            task_ids: list[str] = []
            if items and self.goals is not None:
                ctx = f"来自构想 #{idea_id}《{title}》：{str(view.get('body') or '').strip()}".strip("：")
                for it in items:
                    body = self._idea_item_req(it, ctx)
                    if str(it.get("kind")) == "goal":
                        self.goals.create_agent(
                            gid,
                            title=str(it["title"]),
                            body=body,
                            criteria=[],
                            by_text=f"{requester} 直接开工 · 来自构想",
                            icon=str(view.get("icon") or "bullseye"),
                        )
                    else:
                        task_ids.append(
                            self.tasks.create(
                                gid,
                                title=str(it["title"]),
                                req=body,
                                criteria=[],
                                source="idea",
                                requester_id="",
                                requester_name=requester,
                                icon=str(view.get("icon") or "package"),
                                status="queued",
                            )
                        )
            else:
                task_ids.append(
                    self.tasks.create(
                        gid,
                        title=title,
                        req=self._idea_request_text(view),
                        criteria=[],
                        source="idea",
                        requester_id="",
                        requester_name=requester,
                        icon=str(view.get("icon") or "package"),
                        status="queued",
                    )
                )
            task_id = task_ids[0] if task_ids else None
            if task_id is not None:
                with self.store.tx() as conn:
                    conn.execute(
                        "UPDATE ideas SET task_id=? WHERE id=?", (str(task_id), int(idea_id))
                    )
            logger.info("构想 #%s「做这个」落成 %s 个任务（群 %s）", idea_id, len(task_ids), gid)
            for one in task_ids:
                self.spawn_run_task(one)
        except Exception:
            logger.exception("构想「做这个」落地任务出错（view=%s）", view)

    @staticmethod
    def _picked_idea_items(view: dict) -> list[dict]:
        """构想 view 里这次要落的项目：view["item_nos"] 勾选的（1 起序号）。

        口径复用 approvals.picked_idea_items：空 / 非法序号 / 一个都对不上 → 全部项目
        （写错序号不该变成什么都不做）；没有 items 的老构想 → []，外层按老逻辑建一个。
        """
        from .approvals import Approvals, _parse_item_nos

        return Approvals._picked_idea_items(view, _parse_item_nos(view.get("item_nos")))

    @staticmethod
    def _idea_item_req(item: dict, ctx: str) -> str:
        desc = str(item.get("desc") or "").strip()
        return f"{desc}\n\n{ctx}".strip() if desc else ctx

    def on_idea_want(self, view: dict, group_id: str) -> None:
        """构想「想要这个」（网页 want 路由在 idea_action 之后调）：落成待批请求。

        免批的直接落地（approved + 任务 queued）→ 构想卡标 started、回写 task_id、开工；
        要批的构想卡标 pending（等批准），批准时由 Approvals 落成任务。
        """
        try:
            if self.approvals is None or self.store is None or not isinstance(view, dict):
                return
            idea_id = int(view.get("id") or 0)
            gid = str(group_id or "")
            if idea_id <= 0 or not gid:
                return
            res = self.approvals.create(
                gid,
                kind="task",
                title=str(view.get("title") or "构想"),
                quote=self._idea_request_text(view),
                via="来自构想",
                requester_id="",
                requester_name=str(view.get("requested_by") or "群友（网页）"),
                idea_id=idea_id,
                icon=str(view.get("icon") or "magnifier"),
                source="idea",
            )
            if not isinstance(res, dict):
                return
            tid = str(res.get("task_id") or "")
            if tid:
                with self.store.tx() as conn:
                    conn.execute(
                        "UPDATE ideas SET state='started', task_id=? WHERE id=? AND state IN ('new', 'wanted')",
                        (tid, int(idea_id)),
                    )
                self.spawn_run_task(tid)
                logger.info("构想 #%s 免批落地任务 %s（群 %s）", idea_id, tid, gid)
            elif str(res.get("status") or "") == "pending":
                with self.store.tx() as conn:
                    conn.execute(
                        "UPDATE ideas SET state='pending' WHERE id=? AND state IN ('new', 'wanted')",
                        (int(idea_id),),
                    )
        except Exception:
            logger.exception("构想「想要这个」落成待批出错（view=%s）", view)

    @staticmethod
    def _idea_request_text(view: dict) -> str:
        body = str(view.get("body") or "")
        basis = str(view.get("basis") or "")
        step = str(view.get("step") or "")
        parts = [body]
        if basis:
            parts.append(f"依据：{basis}")
        if step:
            parts.append(f"第一步：{step}")
        items = view.get("items")
        if isinstance(items, list) and items:
            lines = ["包含的项目（批准后逐个开工）："]
            for i, it in enumerate(items):
                if not isinstance(it, dict):
                    continue
                no = it.get("no") or (i + 1)
                kind_zh = "目标" if str(it.get("kind") or "") == "goal" else "任务"
                desc = str(it.get("desc") or "").strip()
                title = str(it.get("title") or "").strip()
                lines.append(f"{no}. （{kind_zh}）{title}" + (f"：{desc}" if desc else ""))
            if len(lines) > 1:
                parts.append("\n".join(lines))
        return "\n".join(p for p in parts if p).strip() or str(view.get("title") or "构想")

    def _group_of_idea(self, idea_id: int) -> str:
        if self.store is None:
            return ""
        try:
            row = self.store.read().execute(
                "SELECT group_id FROM ideas WHERE id=?", (int(idea_id),)
            ).fetchone()
            return str(row["group_id"]) if row is not None else ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # @ 设提醒的慢路径：主模型解析时间 → 成员目标 → 群里回一句
    # ------------------------------------------------------------------

    async def on_reminder(self, group_id: str, msg: dict) -> None:
        gid = str(group_id)
        try:
            if self.models is None or self.goals is None or self.outbox is None:
                return
            if not isinstance(msg, dict):
                return
            user_name = str(msg.get("user_name") or msg.get("user_id") or "")
            text = str(msg.get("text") or "")
            message_id = str(msg.get("message_id") or "")
            now = _now()
            prompt = (
                "把群友的话解析成一个提醒，只输出 JSON，不带任何其他文字。\n"
                f"现在是北京时间 {clock.bj(now):%Y-%m-%d %H:%M}（epoch 秒 {now:.0f}）。\n"
                f"群友「{user_name}」说：{text[:300]}\n"
                '输出格式：{"ok": true, "title": "一句话提醒内容（不超过 20 字）", '
                '"due_ts": 到期的 epoch 秒, "remind_ts": 提醒的 epoch 秒}；\n'
                "remind_ts 默认等于 due_ts。解析不出时间、或时间早于现在 → 只输出 {\"ok\": false}。"
            )
            result = await self.models.chat(
                agent="main",
                messages=[{"role": "user", "content": prompt}],
                json_mode=True, purpose="reminder_parse", group_id=gid, timeout=60,
                # 提醒解析在 @ 消息的处理链上直接 await：不能按默认 5 次 ×10 秒重试把
                # 消息处理卡几分钟，失败这轮就算了（群友可以再发一次）
                retries=1,
            )
            data = _parse_reminder_json(str(getattr(result, "text", "") or ""), now)
            if data is None:
                logger.info("提醒没解析出来（群 %s，原话：%s）", gid, text[:60])
                return
            goal_id = self.goals.create_member(
                gid,
                who_id=str(msg.get("user_id") or ""),
                who_name=user_name,
                title=str(data["title"]),
                due_ts=float(data["due_ts"]),
                remind_ts=float(data["remind_ts"]),
            )
            due_txt = clock.bj(float(data["due_ts"])).strftime("%Y-%m-%d %H:%M")
            self.outbox.enqueue(
                f"reminder:{goal_id}",
                gid,
                "text",
                {
                    "text": f"{user_name}，记下了，{due_txt} 提醒你：{data['title']}",
                    "reply_to": message_id,
                    "push_kind": "status",
                },
            )
            if self.mentions is not None:
                try:
                    self.mentions.add(
                        gid,
                        f"{user_name} 刚才定了个提醒（{due_txt}：{data['title']}），有人问起可以说已经记下了",
                        key=f"reminder-mention:{goal_id}",
                        ttl_s=30 * 60,
                    )
                except Exception:
                    logger.exception("提醒备忘写入失败（群 %s）", gid)
            logger.info("提醒已建：%s（群 %s，%s 提醒）", goal_id, gid, due_txt)
        except Exception:
            logger.exception("解析提醒出错（群 %s）", gid)


    async def run_loop_once(self) -> None:
        """后台循环的一轮（提出来方便测试）。

        M2：在画像之后，每个服务群依次 topics.check / follow_up / 排程。
        资讯备料和构想是长活（可能几分钟），spawn 成独立后台任务，
        同一群同一种同时只跑一个；不阻塞这一轮。
        """
        if not self._started or self.profiles is None or self._settings is None:
            return
        now = _now()
        try:
            self._maybe_check_ssh(now)
        except Exception:
            logger.exception("检查专用机器出错")
        # 1) 收消息信号写进 profiles
        try:
            signals = self.signals.take()
        except Exception:
            signals = {}
        for gid, sig in signals.items():
            try:
                self.profiles.remember_session(gid, sig.session_id, sig.last_ts)
            except Exception:
                logger.exception("remember_session 出错（群 %s）", gid)
            # 同时把 session_id 记进 groups 表：planner 钩子在重启后、
            # 收得见信号之前的空窗期要靠它认群
            self._remember_session_row(gid, sig.session_id, sig.last_ts)
        # 2) 每个服务群串行处理；单个群出错不影响别的
        for gid in list(self._settings.groups.keys()):
            try:
                # tick 只做「读消息 + 统计」（快）：refresh=False 不在循环里等主模型；
                # 有信号最多 60 秒读一次宿主，没信号按 read_interval_minutes 节流
                tick_result = await self.profiles.tick(
                    gid, refresh=False, has_signal=gid in signals
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("群画像 tick 出错（群 %s）", gid)
            else:
                # 提炼 / 每周整理是慢活（主模型，最长几分钟）：丢给长任务机制后台跑，
                # 同群同种同时只跑一个，不挡住这一轮（发件箱、提醒、开话题照常）
                try:
                    if getattr(tick_result, "needs_refresh", False):
                        self._spawn_long_job(gid, "profile", self.profiles.refresh)
                except Exception:
                    logger.exception("画像提炼后台派工出错（群 %s）", gid)
            try:
                self._persona_round(gid, now)
            except Exception:
                logger.exception("个人画像巡检出错（群 %s）", gid)
            try:
                self._personal_round(gid, now)
            except Exception:
                logger.exception("个人向产出巡检出错（群 %s）", gid)
            try:
                await self._topics_round(gid, now)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("开话题巡检出错（群 %s）", gid)
            try:
                await self._schedule_round(gid, now, signals.get(gid))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("排程巡检出错（群 %s）", gid)
            try:
                await self._card_push_round(gid, now)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("资讯卡片 / 构想提一嘴巡检出错（群 %s）", gid)
            try:
                self._member_names_round(gid, now)
            except Exception:
                logger.exception("名册对名字巡检出错（群 %s）", gid)
            try:
                self._viz_round(gid, now)
            except Exception:
                logger.exception("资讯图解巡检出错（群 %s）", gid)
        # 3) M3 巡检：发件箱 / 批准提醒与过期 / 目标到期 / 排队任务派工
        try:
            await self._m3_round(now)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("M3 巡检这一轮出错，继续")
        # 4) 用量提醒（docs/02 §7.2）：超阈值只记一条给网页看，不往群里发、不暂停
        try:
            self._usage_alert_round(now)
        except Exception:
            logger.exception("用量提醒巡检出错")
        # 5) M8：数据不无限增长——每天清一次 30 天前的大日志 / 过期备忘
        try:
            self._prune_round(now)
        except Exception:
            logger.exception("每日数据清理出错")

    def _usage_alert_round(self, now: float) -> None:
        """用量提醒一轮：超线只写 kv（网页设置页的 usage.alerts 读它）。

        不往群里发、不进发件箱、不暂停任何任务（docs/02 §7.2）。
        """
        if self.store is None:
            return
        from . import usage_alerts

        for alert in usage_alerts.check(self.store, self.get_settings, now):
            logger.info(
                "用量提醒（群 %s，%s）：%s",
                alert.get("group_id"),
                alert.get("kind"),
                alert.get("text"),
            )

    def _prune_round(self, now: float) -> None:
        """每天一次 store.prune（按北京日去重；启动后第一轮就跑）。"""
        if self.store is None:
            return
        today = clock.day_key(now)
        if self.store.kv_get("prune.last_day") == today:
            return
        counts = self.store.prune(now)
        with self.store.tx() as conn:
            self.store.kv_set(conn, "prune.last_day", today)
        if any(counts.values()):
            logger.info("每日数据清理：%s", counts)

    # ------------------------------------------------------------------
    # M3 后台巡检（每轮一次；各项互不影响、异常记日志）
    # ------------------------------------------------------------------

    async def _m3_round(self, now: float) -> None:
        # 1) 发件箱
        if self.outbox is not None:
            try:
                await self.outbox.flush(now)
            except Exception:
                logger.exception("发件箱 flush 出错")
        # 1.5) 群空间探测刷新（6 小时缓存到期才真调 api.list；probe 自己有 TTL）
        if self.group_space is not None:
            try:
                await self.group_space.probe(now=now)
            except Exception:
                logger.exception("群空间探测刷新出错")
        # 2) 批准：过期 + 24 小时提醒
        if self.approvals is not None:
            try:
                self._approval_round(now)
            except Exception:
                logger.exception("批准巡检出错")
        # 3) 目标到期
        if self.goals is not None:
            try:
                await self._goals_round(now)
            except Exception:
                logger.exception("目标巡检出错")
        # 4) 排队任务派工 + waiting_input 照看
        try:
            self._tasks_round(now)
        except Exception:
            logger.exception("任务巡检出错")

    def _approval_round(self, now: float) -> None:
        approvals = self.approvals
        assert approvals is not None
        try:
            expired = approvals.expire(now)
        except Exception:
            expired = []
            logger.exception("待批过期清理出错")
        if expired:
            logger.info("待批请求过期 %d 件：%s", len(expired), ",".join(expired[:5]))
        # 24 小时提醒（可配 [approval] remind=false 关）
        remind_on = True
        try:
            remind_on = bool(getattr(self._settings.approval, "remind", True)) if self._settings is not None else True
        except Exception:
            remind_on = True
        if not remind_on or self.outbox is None:
            return
        due = approvals.due_reminders(now)
        if not due:
            return
        by_group: dict[str, list[dict]] = {}
        for r in due:
            gid0 = str(r.get("group_id") or "")
            # 非服务群：不发提醒（零发送红线）；待批本身由回收逻辑标 expired
            if self._settings is not None and not self._settings.is_served(gid0):
                continue
            by_group.setdefault(gid0, []).append(r)
        for gid, items in by_group.items():
            titles = "、".join(f"「{str(r.get('title') or '')[:16]}」" for r in items[:3])
            text = f"有 {len(items)} 件事等管理员批准：{titles}。在群里发 /mw 批准 ID，或到网页上处理"
            self.outbox.enqueue(
                f"approval-remind:{gid}:{clock.day_key(now)}",
                gid,
                "text",
                {"text": text, "push_kind": "status"},
            )
            for r in items:
                try:
                    approvals.mark_reminded(str(r["id"]), now)
                except Exception:
                    logger.exception("批准提醒标记失败（%s）", r.get("id"))

    async def _goals_round(self, now: float) -> None:
        goals = self.goals
        assert goals is not None
        settings = self._settings
        events = goals.due(now)
        for ev in events:
            try:
                etype = str(ev.get("type") or "")
                goal = ev.get("goal") if isinstance(ev.get("goal"), dict) else {}
                goal_id = str(goal.get("id") or "")
                if not goal_id:
                    continue
                row = goals.get(goal_id) or {}
                gid = str(row.get("group_id") or "")
                # 非服务群：零调用、零发送（回收逻辑已标 cancelled，这里再兜底）
                if settings is not None and not settings.is_served(gid):
                    continue
                if etype == "check":
                    # 先推进检查点，再开工；coordinator 没就位就等它有就位的那轮。
                    # 模型没配好：跳过（不推进、不报错），下次巡检自然再轮到这个检查点。
                    if self.coordinator is not None and self._models_ready():
                        goals.mark(goal_id, "check", now)
                        self._spawn_bg(self.coordinator.check_goal(goal_id), name=f"maiwork-check-{goal_id}")
                    continue
                if not gid or self.outbox is None:
                    continue
                # @ 的人用名册当前名（按 who_id 查），查不到回落目标里的名字快照
                who = members.name_of(
                    self.store, gid, goal.get("who_id"), fallback=goal.get("who_name") or goal.get("who")
                )
                title = str(goal.get("title") or "")
                if etype == "remind":
                    self.outbox.enqueue(
                        f"goal-remind:{goal_id}:{clock.day_key(now)}",
                        gid,
                        "text",
                        {"text": f"@{who} 提醒：{title}", "push_kind": "reminder"},
                    )
                    goals.mark(goal_id, "remind", now)
                elif etype == "ask_progress":
                    self.outbox.enqueue(
                        f"goal-ask:{goal_id}",
                        gid,
                        "text",
                        {"text": f"@{who} 「{title}」到期了，进展怎么样？", "push_kind": "status"},
                    )
                    goals.mark(goal_id, "ask_progress", now)
                elif etype == "renew":
                    self.outbox.enqueue(
                        f"goal-renew:{goal_id}",
                        gid,
                        "text",
                        {
                            "text": f"「{title}」这个每天的提醒 30 天到了，还要继续的话，"
                            f"让 {who or '当事人'} 在网页上重新设一个，或者群里告诉我",
                            "push_kind": "status",
                        },
                    )
                    goals.mark(goal_id, "renew", now)
                else:
                    logger.info("不认识的目标到期类型：%s（%s）", etype, goal_id)
            except Exception:
                logger.exception("处理目标到期出错：%s", ev.get("goal", {}).get("id") if isinstance(ev, dict) else ev)
        # 报平安巡视（docs/02 §4.3）：超时没心跳的 agent 目标标出来；
        # 模型没配好的标「暂停检查」，不算卡住
        try:
            goals.refresh_stale(now, models_ready=self._models_ready())
        except Exception:
            logger.exception("目标卡住巡视出错")

    def _tasks_round(self, now: float) -> None:
        if self.store is None or self._settings is None:
            return
        rows = self.store.read().execute(
            "SELECT id, group_id, status, title, question, question_ts FROM tasks"
            " WHERE status IN ('queued', 'waiting_input')"
        ).fetchall()
        models_ready = self._models_ready()
        served = self._settings.is_served
        for row in rows:
            if not served(str(row["group_id"])):
                continue  # 非服务群：零派工（回收逻辑已标 cancelled，这里再兜底）
            tid = str(row["id"])
            status = str(row["status"])
            if status == "queued":
                # 模型没配好不派工：任务保持排队（不报错、不开尝试，02 §12.1）
                if models_ready:
                    self.spawn_run_task(tid)
                continue
            # waiting_input：超 6 小时提醒一次、超 24 小时 → shelved
            qts = float(row["question_ts"] or 0.0)
            if qts <= 0.0:
                continue
            age = now - qts
            if age >= 24 * 3600:
                try:
                    if self.tasks is not None:
                        self.tasks.transition(
                            tid, "shelved", reason="等回答超过 24 小时，先搁置；有人回复那条提问就恢复"
                        )
                        # 状态变了：等待任务缓存立刻失效（shelved 也要能被回复恢复）
                        self._invalidate_answer_cache()
                except Exception:
                    logger.exception("waiting_input → shelved 出错（任务 %s）", tid)
            elif age >= 6 * 3600 and self.outbox is not None:
                question = str(row["question"] or "")[:60]
                title = str(row["title"] or "")[:20]
                self.outbox.enqueue(
                    f"task-wait-remind:{tid}",
                    str(row["group_id"]),
                    "text",
                    {"text": f"「{title}」还在等回答：{question}（回复那条提问就行）", "push_kind": "status"},
                )


    def _persona_round(self, gid: str, now: float) -> None:
        """一个群的个人画像巡检：到期的关注成员每轮最多 spawn 一个 refresh（后台任务，
        不阻塞本轮；同一群同一时刻只跑一个）。"""
        personas = self.personas
        if personas is None:
            return
        key = (str(gid), "persona")
        if key in self._running_jobs:
            return
        try:
            settings = self._settings
            if settings is not None and not settings.focus.personal_profile:
                return
            due = personas.due(str(gid), now) or []
        except Exception:
            logger.exception("个人画像 due 巡检出错（群 %s）", gid)
            return
        if not due:
            return
        uid = str(due[0])

        async def _runner() -> None:
            try:
                await personas.refresh(str(gid), uid)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("个人画像 refresh 出错（群 %s %s）", gid, uid[:8] if uid else uid)

        self._running_jobs.add(key)
        task = asyncio.create_task(_runner(), name=f"maiwork-job-persona-{gid}")
        self._bg_jobs.add(task)
        task.add_done_callback(self._on_long_job_done)

    def _personal_round(self, gid: str, now: float) -> None:
        """一个群的个人向产出巡检：每轮最多给 1 个到期的人 spawn 一次 prepare_personal
        （后台任务、同群同一时刻只跑一个；due 里含 9:00–22:00 时间窗与今天去重判断）。"""
        personal = self.personal
        if personal is None:
            return
        key = (str(gid), "personal")
        if key in self._running_jobs:
            return
        try:
            due = personal.due(str(gid), now) or []
        except Exception:
            logger.exception("个人向 due 巡检出错（群 %s）", gid)
            return
        if not due:
            return
        uid = str(due[0])

        async def _runner() -> None:
            try:
                await personal.prepare_personal(str(gid), uid)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("个人向 prepare 出错（群 %s %s）", gid, uid[:8] if uid else uid)
            finally:
                # 成功失败都算「这个人今天做过了」，避免隔 30 秒又来一轮
                try:
                    personal.mark_done(str(gid), uid, clock.now())
                except Exception:
                    logger.exception("个人向 mark_done 出错（群 %s）", gid)

        self._running_jobs.add(key)
        task = asyncio.create_task(_runner(), name=f"maiwork-job-personal-{gid}")
        self._bg_jobs.add(task)
        task.add_done_callback(self._on_long_job_done)

    async def _topics_round(self, gid: str, now: float) -> None:
        """一个群的冷场开话题巡检：先 check（到点开话题），再 follow_up（到点记效果）。

        开话题开关关掉要立刻停（网页规则覆盖，rules.py）：这里读合并后的有效值，
        关掉起下一轮就不开话题（开关在 kv 里，get_settings 合并层拾得到）。
        """
        if self.topics is None:
            return
        if not self._topics_effective_on():
            return
        result = await self.topics.check(gid, now)
        if not str(result).startswith("skip:"):
            logger.info("开话题巡检（群 %s）：%s", gid, result)
        await self.topics.follow_up(gid, now)

    async def _schedule_round(self, gid: str, now: float, sig: Any) -> None:
        """一个群的排程巡检：scheduler.due 到点的事 → spawn 长活。"""
        if self.scheduler is None:
            return
        last_msg_ts = float(getattr(sig, "last_ts", 0.0) or 0.0) if sig is not None else 0.0
        if last_msg_ts <= 0.0:
            last_msg_ts = self.signals.last_ts(gid)
        if last_msg_ts <= 0.0 and self.store is not None:
            row = self.store.read().execute(
                "SELECT last_msg_ts FROM groups WHERE group_id=?", (gid,)
            ).fetchone()
            if row is not None:
                last_msg_ts = float(row["last_msg_ts"] or 0.0)
        due = self.scheduler.due(gid, now, last_msg_ts=last_msg_ts)
        # 反馈 + 口味（feedback_jobs）：每群每小时最多一轮，群画像成形后才跑；不往群里发东西
        try:
            from . import feedback_jobs

            if self.store is not None and self.models is not None and feedback_jobs.due(gid, now):
                row = self.store.read().execute(
                    "SELECT profile_ready_ts FROM groups WHERE group_id=?", (gid,)
                ).fetchone()
                if row is not None and float(row["profile_ready_ts"] or 0) > 0:
                    self._spawn_long_job(gid, "feedback", self._feedback_round)
        except Exception:
            logger.exception("排反馈 / 口味这一轮出错（群 %s）", gid)
        for job in due or []:
            if job == "news":
                if self.feeds is None:
                    continue
                if (gid, "news_manual") in self._running_jobs:
                    continue  # 手动的那批还在跑，这次先不开（下一轮再看）
                self._spawn_long_job(gid, "news", self.feeds.prepare_news)
            elif job == "idea":
                if self.feeds is None:
                    continue
                if (gid, "idea_manual") in self._running_jobs:
                    continue  # 手动点的那个还在跑，这次先不开（下一轮再看）
                self._spawn_long_job(gid, "idea", self.feeds.make_idea)
            elif job == "goal":
                if self.goal_proposer is None:
                    continue
                self._spawn_long_job(gid, "goal", self._propose_goal_round)

    async def _feedback_round(self, gid: str) -> None:
        from . import feedback_jobs
        from .privacy import scrub

        out = await feedback_jobs.run(
            self.store, self.models, gid, _now(), profiles=self.profiles,
            scrub=lambda g, text: scrub(g, text, self.store),
        )
        if out.get("mentions") or out.get("taste"):
            logger.info("反馈 / 口味（群 %s）：%s", gid, out)

    async def _card_push_round(self, gid: str, now: float) -> None:
        """一个群的资讯卡片 / 构想提一嘴：建待发行 + 投递（开关、节制都在模块里）。

        卡片要下配图 + 起浏览器画图（几秒到几十秒），提一嘴要调模型写话：有到点的才丢成
        后台长活（同群同种同时只跑一个），不卡主循环。
        """
        cp = self.card_push
        if cp is not None:
            cp.scan(gid, now)
            if cp.has_due(gid, now):
                self._spawn_long_job(gid, "newscard", cp.flush)
        im = self.idea_mention
        if im is not None:
            im.scan(gid, now)
            if im.has_due(gid, now) and self._models_ready():
                self._spawn_long_job(gid, "ideamention", im.flush)

    def _viz_round(self, gid: str, now: float) -> None:
        """资讯图解：有到期的活（开着、没到当天上限、有没处理过的候选）才派后台长活 kind=viz。"""
        nv = self.news_viz
        if nv is None or (str(gid), "viz") in self._running_jobs:
            return
        if not self._models_ready():
            return
        if nv.has_work(str(gid), now):
            self._spawn_long_job(str(gid), "viz", nv.run)

    _NAMES_EVERY_S = 30 * 60.0

    def _member_names_round(self, gid: str, now: float) -> None:
        """名册跟 QQ 对名字（members.refresh_from_host）：每群 30 分钟最多派一轮后台长活。

        只在服务群的循环里调用；问多少人、多久问一次由 members 模块管。
        """
        store, host = self.store, self.host
        if store is None or host is None:
            return
        gid = str(gid)
        last = self._names_last.get(gid)
        if last is not None and now - last < self._NAMES_EVERY_S:
            return
        self._names_last[gid] = now

        async def _refresh(g: str) -> None:
            await members.refresh_from_host(store, host, g, now)

        self._spawn_long_job(gid, "names", _refresh)

    async def _propose_goal_round(self, gid: str) -> None:
        """主动提目标一轮：开关 / 非服务群 / 每日上限都在 GoalProposer 里兜住。"""
        proposer = self.goal_proposer
        if proposer is None:
            return
        await proposer.propose(gid)

    def _topics_effective_on(self) -> bool:
        """有效设置里开话题开没开（网页规则覆盖后）。"""
        try:
            return bool(self.get_settings().topics.enabled)
        except Exception:
            return False

    def run_news_now(self, gid: str) -> dict:
        """管理员在网页上点「现在就备一批」。后台跑，不等结果。

        不记 scheduler.done（kind 用 news_manual）：否则最近的时段会被当成已做过而跳过；
        和定时备料互斥——同一群已经有一批在跑就不再开。

        开工前先过 `feeds.news_precheck`（画像成形 / news 专岗启停 / 模型就绪）：不通过就
        明确回 {"started": False, "reason": 中文原因}，不再出现「回了开始、后台立刻静默退出」。
        通过则给一个运行编号 run_id，并往 kv 写一条运行记录（running → done / skipped / failed），
        网页用 GET /api/groups/{gid}/news/run 读它（A11）。
        """
        gid = str(gid)
        settings = self._settings
        if settings is None or self.feeds is None or not settings.is_served(gid):
            return {"started": False, "reason": "这个群现在备不了料（不是服务群，或资讯模块没开）"}
        precheck = getattr(self.feeds, "news_precheck", None)
        if callable(precheck):
            try:
                why = str(precheck(gid) or "")
            except Exception:
                logger.exception("备料开工前提检查出错（群 %s）", gid)
                why = "开工前提检查出错，这次先不备料（详细原因看日志）"
            if why:
                return {"started": False, "reason": why}
        if not self._models_ready():
            return {"started": False, "reason": "模型还没配好：去网页 设置 → 模型 里配好一个能用的模型再来"}
        if (gid, "news") in self._running_jobs or (gid, self._MANUAL_NEWS_KIND) in self._running_jobs:
            return {"started": False, "reason": "这个群已经在备料了，等这一批出来"}
        run_id = secrets.token_hex(6)
        started = _now()
        self._write_manual_news_record(
            gid,
            {
                "run_id": run_id,
                "state": "running",
                "started_ts": started,
                "ended_ts": 0.0,
                "reason": "",
                "items": 0,
            },
        )

        async def _run_manual(g: str) -> int:
            try:
                items = int(await self.feeds.prepare_news(g) or 0)
            except asyncio.CancelledError:
                self._finish_manual_news(g, run_id, "failed", "这批备料跑到一半就中断了（服务停了或重启了）", 0)
                raise
            except Exception as e:
                logger.exception("手动备料长活出错（群 %s）", g)
                self._finish_manual_news(g, run_id, "failed", f"备料出错了：{e}", 0)
                return 0
            if items > 0:
                self._finish_manual_news(g, run_id, "done", self._manual_news_done_reason(g, started, items), items)
            else:
                note = self._latest_news_batch_note(g, started)
                self._finish_manual_news(g, run_id, "skipped", note or "这一轮没有可发的资讯（没有留下具体原因）", 0)
            return items

        self._spawn_long_job(gid, self._MANUAL_NEWS_KIND, _run_manual)
        return {"started": True, "reason": "", "run_id": run_id}

    # ---- 手动备料的运行记录（A11） ------------------------------------------------

    _MANUAL_NEWS_KIND = "news_manual"

    @staticmethod
    def _manual_news_key(gid: str) -> str:
        return f"news.manual_run.{gid}"

    def _manual_news_record(self, gid: str) -> dict | None:
        """读这条手动备料的运行记录；没写过 / 结构坏了 → None。"""
        store = self.store
        if store is None:
            return None
        try:
            got = store.kv_get(self._manual_news_key(gid))
        except Exception:
            logger.exception("读手动备料运行记录出错（群 %s）", gid)
            return None
        return got if isinstance(got, dict) else None

    def _write_manual_news_record(self, gid: str, rec: dict) -> None:
        store = self.store
        if store is None:
            return
        try:
            with store.tx() as conn:
                store.kv_set(conn, self._manual_news_key(gid), rec)
        except Exception:
            logger.exception("写手动备料运行记录出错（群 %s）", gid)

    def _finish_manual_news(self, gid: str, run_id: str, state: str, reason: str, items: int = 0) -> None:
        """给这轮手动备料落终态；新一轮已经开了（run_id 对不上）就不覆盖。"""
        rec = self._manual_news_record(gid)
        if rec is None or str(rec.get("run_id") or "") != str(run_id):
            return
        out = dict(rec)
        out["state"] = str(state)
        out["ended_ts"] = _now()
        out["reason"] = str(reason or "")
        out["items"] = int(items or 0)
        self._write_manual_news_record(gid, out)

    def _latest_news_batch_note(self, gid: str, since: float) -> str:
        """本轮之后最新一条 news_batches 的 note——跳过原因就写在 note 字段。

        （`feeds._skipped_batch` / `_insert_batch_and_items` 都写 `note`。）
        """
        store = self.store
        if store is None:
            return ""
        try:
            row = store.read().execute(
                "SELECT note FROM news_batches WHERE group_id=? AND created>=? ORDER BY id DESC LIMIT 1",
                (str(gid), float(since)),
            ).fetchone()
        except Exception:
            logger.exception("读最近一批资讯的备注出错（群 %s）", gid)
            return ""
        return str(row["note"] or "") if row is not None else ""

    def _manual_news_done_reason(self, gid: str, since: float, items: int) -> str:
        """done 的中文说明：只出了文章（kind=guide）没出资讯也算 done，但要说明白。"""
        store = self.store
        if store is None or items <= 0:
            return f"备料完成：这一轮入选 {items} 条"
        try:
            row = store.read().execute(
                "SELECT id FROM news_batches WHERE group_id=? AND created>=? ORDER BY id DESC LIMIT 1",
                (str(gid), float(since)),
            ).fetchone()
            if row is not None:
                news_n = int(
                    store.read().execute(
                        "SELECT COUNT(*) c FROM news_items WHERE batch_id=? AND rejected=0 AND kind='news'",
                        (int(row["id"]),),
                    ).fetchone()["c"]
                )
                if news_n == 0:
                    return f"这轮只出了文章（好文）{items} 篇、没有新资讯——不算失败"
        except Exception:
            logger.exception("看这轮出的是资讯还是文章出错（群 %s）", gid)
        return f"备料完成：这一轮入选 {items} 条"

    def news_manual_run_status(self, gid: str) -> dict:
        """这条手动备料的运行记录（GET /api/groups/{gid}/news/run）。

        - 没写过：state="none"（网页显示「还没手动备过料」）；
        - 记录还写着 running，但后台已经没有这个长活（服务器重启 / 服务停过）→ 当场改成
          failed「中断了」并落库，不让网页永远显示「进行中」。
        """
        gid = str(gid)
        rec = self._manual_news_record(gid)
        if rec is None:
            return {"run_id": "", "state": "none", "started_ts": 0.0, "ended_ts": 0.0, "reason": "", "items": 0}
        out = {
            "run_id": str(rec.get("run_id") or ""),
            "state": str(rec.get("state") or "none"),
            "started_ts": float(rec.get("started_ts") or 0.0),
            "ended_ts": float(rec.get("ended_ts") or 0.0),
            "reason": str(rec.get("reason") or ""),
            "items": int(rec.get("items") or 0),
        }
        if out["state"] == "running" and (gid, self._MANUAL_NEWS_KIND) not in self._running_jobs:
            out["state"] = "failed"
            out["ended_ts"] = out["ended_ts"] or _now()
            out["reason"] = "上一次手动备料跑到一半就中断了（服务器重启或服务停了），这批没跑完"
            self._write_manual_news_record(gid, out)
        return out

    def make_idea_now(self, gid: str) -> dict:
        """管理员（网页 / 管理员对话）点「现在就想一个构想」。后台跑，不等结果。

        和 run_news_now 对称：同群互斥（定时出的 idea 或手动这批还在跑就不再开，
        连点八次也只起一个）；kind 用 idea_manual，跑完不记 scheduler.done。
        """
        gid = str(gid)
        settings = self._settings
        if settings is None or self.feeds is None or not settings.is_served(gid):
            return {"started": False, "reason": "这个群现在出不了构想（不是服务群，或构想模块没开）"}
        if not self._models_ready():
            return {"started": False, "reason": "模型还没配好"}
        if (gid, "idea") in self._running_jobs or (gid, "idea_manual") in self._running_jobs:
            return {"started": False, "reason": "这个群已经在想构想了，等它想完"}
        self._spawn_long_job(gid, "idea_manual", self.feeds.make_idea)
        return {"started": True, "reason": ""}

    def refresh_profile_now(self, gid: str) -> dict:
        """管理员（管理员对话 / 网页）点「现在重新整理这个群的画像」。后台跑，不等结果。

        和 run_news_now / make_idea_now 对称：同群互斥（定时提炼或手动这次还在跑
        就不再开，连调多次也只起一个）；kind 用 profile_manual；走的就是平时画像刷新的
        同一个入口 profiles.refresh(force=True)。不对外发任何东西，不用管理员再确认。
        """
        gid = str(gid)
        settings = self._settings
        if settings is None or self.profiles is None or not settings.is_served(gid):
            return {"started": False, "reason": "这个群现在整理不了画像（不是服务群，或画像模块没开）"}
        if not self._models_ready():
            return {"started": False, "reason": "模型还没配好"}
        if (gid, "profile") in self._running_jobs or (gid, "profile_manual") in self._running_jobs:
            return {"started": False, "reason": "这个群已经在整理画像了，等它整理完"}
        refresh = getattr(self.profiles, "refresh", None)
        if not callable(refresh):
            return {"started": False, "reason": "这个 MaiWork 的画像模块没有刷新入口"}

        async def _force_refresh(g: str) -> None:
            await refresh(g, force=True)

        self._spawn_long_job(gid, "profile_manual", _force_refresh)
        return {"started": True, "reason": ""}

    def _spawn_long_job(self, gid: str, kind: str, fn: Callable[..., Any]) -> None:
        """资讯备料 / 构想 / 画像提炼：可能跑几分钟，独立后台任务；同一群同一种同时只跑一个。"""
        key = (str(gid), str(kind))
        if key in self._running_jobs:
            logger.debug("群 %s 的 %s 已在跑，这次不重复开", gid, kind)
            return

        async def _runner() -> None:
            try:
                await fn(gid)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("群 %s 的 %s 长活出错了", gid, kind)
            finally:
                # scheduler 管的事（news/idea/goal）不管成功失败都算做过，错过不补（docs/07 §10.5）；
                # 画像提炼（profile）不在 scheduler 的词表里，不汇报
                if kind in ("news", "idea", "goal"):
                    try:
                        if self.scheduler is not None:
                            self.scheduler.done(gid, kind, _now())
                    except Exception:
                        logger.exception("scheduler.done 出错（群 %s %s）", gid, kind)

        self._running_jobs.add(key)
        task = asyncio.create_task(_runner(), name=f"maiwork-job-{kind}-{gid}")
        self._bg_jobs.add(task)
        task.add_done_callback(self._on_long_job_done)

    @staticmethod
    def _job_key_of(task: asyncio.Task) -> tuple[str, str]:
        """task 名 maiwork-job-news-900000001 → (gid, kind) 剥回来。"""
        name = task.get_name()
        if name.startswith("maiwork-job-"):
            rest = name[len("maiwork-job-"):]
            kind, _, gid = rest.partition("-")
            return (gid, kind)
        return ("", "")

    def _on_long_job_done(self, task: asyncio.Task) -> None:
        # M9：task.exception() 本身可能抛（CancelledError / InvalidStateError），先判 cancelled 再用 try 兜
        self._bg_jobs.discard(task)
        gid, kind = self._job_key_of(task)
        if gid:
            self._running_jobs.discard((gid, kind))
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except Exception:
            exc = None
        if exc is not None:
            logger.exception("长活任务 %s 未捕获异常", task.get_name(), exc_info=exc)

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_loop_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("后台循环这一轮出错，继续")
            await asyncio.sleep(self.loop_interval)


def _now() -> float:
    return clock.now()


def _parse_reminder_json(text: str, now: float) -> dict | None:
    """解析主模型的提醒 JSON。返回 {"title", "due_ts", "remind_ts"} 或 None。

    - 宽容提取：找第一个 { 到最后一个 }；json_mode 坏掉也能试。
    - 必须是 {"ok": true, title 非空, due_ts 数字}；due_ts < now-60 当无效（解析到过去）。
    - remind_ts 缺省 = due_ts。
    """
    s = str(text or "").strip()
    if not s:
        return None
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        data = json.loads(s[i : j + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    title = str(data.get("title") or "").strip()
    if not title:
        return None
    try:
        due_ts = float(data.get("due_ts"))
    except (TypeError, ValueError):
        return None
    if due_ts < float(now) - 60:
        return None
    try:
        remind_ts = float(data.get("remind_ts")) if data.get("remind_ts") is not None else due_ts
    except (TypeError, ValueError):
        remind_ts = due_ts
    return {"title": title[:40], "due_ts": due_ts, "remind_ts": remind_ts}


class _NullProfiles:
    """profile.py 还没就位时的替身：什么都不做。"""

    async def tick(
        self,
        group_id: str,
        *,
        force: bool = False,
        refresh: bool = True,
        has_signal: "bool | None" = None,
    ) -> dict:
        return {"group_id": group_id, "skipped": True, "needs_refresh": False}

    async def refresh(self, group_id: str, *, force: bool = False) -> bool:
        return False

    def set_request_deps(self, *, approvals: Any = None, goals: Any = None, outbox: Any = None) -> None:
        return None

    def remember_session(self, group_id: str, session_id: str, last_msg_ts: float) -> None:
        return None

    def entries(self, group_id: str) -> list:
        return []

    def add_entry(self, group_id: str, category: str, text: str) -> int:
        raise KeyError("群画像功能还没就位")

    def edit_entry(self, entry_id: int, *, text: str | None = None, locked: bool | None = None) -> None:
        raise KeyError("群画像功能还没就位")

    def delete_entry(self, entry_id: int) -> None:
        raise KeyError("群画像功能还没就位")

    def focus(self, group_id: str) -> list:
        return []

    def set_focus(self, group_id: str, user_id: str, action: str) -> None:
        raise KeyError("群画像功能还没就位")

    def pulse(self, group_id: str, *, end: float, hours: int = 24) -> list:
        return [0] * (hours * 4)

    def usual_gap(self, group_id: str, ts: float) -> None:
        return None
