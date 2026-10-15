"""一次性 VM 平台注册表（2026-10-15，docs/02 §9、docs/07 §11.1d / §11.1e）。

为什么有这一层：任务虚拟机原来只有一条硬编码路径（railway.new），环境名
`local` / `ssh` / `railway` 散落在 config → rules → app → coordinator → tools_* →
console/views 十几处 `if kind == ...` 里。railway 与 Freestyle 的操作集合逐字同形
（acquire / run / put / get / release / current_box / usage_today / last_fail），
所以把「一次性 VM」抽成这张很薄的表：**加第三家平台 = 加一个实现 + 一条注册**，
`ssh`（用户自有、持久、按 job 建目录）留在原样不动。

这里的每条事实都来自官方文档/OpenAPI，**没有用真密钥实测过**（2026-10-15）：
- Freestyle：https://www.freestyle.sh/docs/llms.txt 、https://api.freestyle.sh/openapi.json
- Railway：docs/09-railway-new一次性VM实测.md

预设只是「照着文档填好的默认值」：规格（cpu / 内存）由 `snapshotId` 决定，
创建时不能单独选；网页上用户都能改。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

from .. import clock

RAILWAY = "railway"
FREESTYLE = "freestyle"


@dataclass(frozen=True)
class VMSnapshot:
    """一个官方镜像 / 快照：它决定 CPU / 内存 / 磁盘（创建时不能单独选规格）。"""

    slug: str
    label: str      # 给用户看的一句话规格
    cpu: int
    memory_mib: int
    disk_gib: int


@dataclass(frozen=True)
class VMPreset:
    """一家一次性 VM 平台的定死知识。"""

    id: str                  # "railway" / "freestyle"；也当 box.kind 用
    label: str               # 显示名
    kind: str                # 同 id（box.kind 查表用）
    needs_key: bool          # 要不要用户自己的 API key
    key_url: str             # 去哪拿密钥（"" = 免密钥）
    docs_url: str            # 官方文档
    console_url: str         # 控制台
    default_snapshot: str    # 默认镜像 / 快照（"" = 平台自己没有这个概念）
    snapshots: tuple[VMSnapshot, ...]
    ttl_minutes: int         # 一台机器默认最多活多少分钟
    note: str                # 一句中文提醒（网页上收在「ⓘ」里）
    key_env: str = ""        # 也认这个环境变量
    fixed_spec: str = ""     # 规格写死、平台不回资源的那些（railway：2 核 2G）
    describe_name: str = ""  # 任务 env 字段里那半句
    plan_hint: str = ""      # 排计划提示词里给主模型的一段（「选它还是选 ssh」）

    # ------------------------------------------------------------------
    # 开关判定（配置里现在允不允许用这家）
    # ------------------------------------------------------------------

    def enabled(self, settings: Any) -> bool:
        """这家自己的开关开着（2026-10-15 用户定：两家各自开关、可同时开，不再二选一）。

        railway 看 `[environments] railway`（默认开）；freestyle 看 `[environments] freestyle`
        （默认关）。没有 freestyle 开关的老设置对象（测试替身等）按「优先用的就是它」算开。
        """
        env = getattr(settings, "environments", None)
        if env is None:
            return False
        if self.kind == RAILWAY:
            return bool(getattr(env, "railway", True))
        flag = getattr(env, self.kind, None)
        if flag is None:
            return selected_kind(settings) == self.kind
        return bool(flag)

    def has_key(self, settings: Any) -> bool:
        """这家要的密钥填了没（不要密钥的永远 True）。"""
        if not self.needs_key:
            return True
        return bool(api_key_of(self.kind, settings))

    def ready(self, settings: Any) -> bool:
        """开关开着 + 密钥齐 → 真能用。"""
        return self.enabled(settings) and self.has_key(settings)

    # ------------------------------------------------------------------

    def snapshot_spec(self, slug: str) -> VMSnapshot | None:
        """认得出的镜像 → 规格；认不出（用户自己拍的快照）→ None，不瞎猜。"""
        want = str(slug or "").strip()
        if not want:
            return None
        for snap in self.snapshots:
            if snap.slug == want:
                return snap
        return None


PRESETS: dict[str, VMPreset] = {
    RAILWAY: VMPreset(
        id=RAILWAY,
        label="Railway",
        kind=RAILWAY,
        needs_key=False,
        key_url="",
        docs_url="https://docs.railway.com/guides/ssh",
        console_url="https://railway.com",
        default_snapshot="",
        snapshots=(),
        ttl_minutes=60,
        note="免密钥、匿名；官方没有销毁命令，用完等它自己过期（约 60 分钟）。",
        fixed_spec="2 核 2G",
        describe_name="railway.new 一次性机器",
        plan_hint=(
            "railway 是一台一次性机器：60 分钟窗口、2 核 2G、用完即弃；同一时间只有 1 台、"
            "每天有限额，现在不一定拿得到。要跑不信任的第三方代码、要 root 或 docker、"
            "要一台干净系统时选它"
        ),
    ),
    FREESTYLE: VMPreset(
        id=FREESTYLE,
        label="Freestyle",
        kind=FREESTYLE,
        needs_key=True,
        key_url="https://dash.freestyle.sh",
        docs_url="https://www.freestyle.sh/docs",
        console_url="https://dash.freestyle.sh",
        default_snapshot="freestyle/ubuntu-sm",
        snapshots=(
            VMSnapshot("freestyle/busybox", "1 核 128M · 1G 盘（只有 sh，没包管理器）", 1, 128, 1),
            VMSnapshot("freestyle/ubuntu-sm", "2 核 4G · 16G 盘", 2, 4096, 16),
            VMSnapshot("freestyle/ubuntu", "4 核 8G · 32G 盘（免费档上限）", 4, 8192, 32),
            VMSnapshot("freestyle/ubuntu-lg", "8 核 16G · 64G 盘（Hobby 档起）", 8, 16384, 64),
        ),
        ttl_minutes=60,
        note="要自己的 API key（只在创建时显示一次）；用完能主动销毁。",
        key_env="FREESTYLE_API_KEY",
        describe_name="Freestyle 一次性机器",
        plan_hint=(
            "freestyle 是 Freestyle 的一次性机器：默认 2 核 4G、用完能主动销毁；同一时间只有 1 台、"
            "每天有限额，现在不一定拿得到。要跑不信任的第三方代码、要 root 或 docker、"
            "要一台干净系统时选它"
        ),
    ),
}


def get(kind: Any) -> VMPreset | None:
    """按平台名查预设；不认识 → None。"""
    return PRESETS.get(str(kind or "").strip().lower())


def kinds() -> tuple[str, ...]:
    """注册表里的平台名（顺序 = 提示词里列选项的顺序）。"""
    return tuple(PRESETS)


def selected_kind(settings: Any) -> str:
    """优先用的那家（`[environments] vm_platform`）；不认识回落 railway。

    只是「两家都开时先用谁」，不代表另一家被关掉；开没开看 `enabled_kinds`。
    """
    env = getattr(settings, "environments", None)
    raw = str(getattr(env, "vm_platform", RAILWAY) or RAILWAY).strip().lower()
    return raw if raw in PRESETS else RAILWAY


preferred_kind = selected_kind


def enabled_kinds(settings: Any) -> tuple[str, ...]:
    """开着的平台（只看开关，不看密钥），优先那家排第一，其余按注册表顺序。"""
    try:
        first = selected_kind(settings)
    except Exception:
        first = RAILWAY
    order = [first, *[k for k in PRESETS if k != first]]
    out: list[str] = []
    for k in order:
        try:
            if PRESETS[k].enabled(settings):
                out.append(k)
        except Exception:
            continue
    return tuple(out)


class VMRouter:
    """把几家一次性 VM 合成一个「环境」交给 vm_* 工具（2026-10-15，两家可同时开）。

    - 同一时间两家合计只占一台（coordinator 申请前查 `busy_kind`），所以 `current_box`
      就是「现在那台」，子 agent 的工具不用知道是哪家；
    - run / put / get 按 `box.kind` 交给对应那家（没带 kind 的老 Box 当 railway）；
    - `max_run_timeout_s`：有机器时取那家的上限，没机器时取各家里最小的（写进工具说明，宁紧勿松）。
    """

    def __init__(self, envs: dict[str, Any]) -> None:
        self._envs = {str(k): v for k, v in (envs or {}).items() if v is not None}
        self._lock: Any = None      # asyncio.Lock，第一次申请时建（绑定当时的事件循环）

    async def acquire(self, kind: str, job_id: str) -> tuple[Any, str]:
        """在某一家申请一台：(机器 | None, 占着的另一家或 "")。

        「查另一家空不空 + 申请」在同一把锁里做完——两个任务同时申请时不会两家各拿一台
        （那样 vm_* 工具就分不清是哪台了）。
        """
        import asyncio

        env = self._envs.get(str(kind))
        if env is None:
            return None, ""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            busy = self.busy_kind(exclude=str(kind))
            if busy:
                return None, busy
            return await env.acquire(job_id), ""

    def exclusive(self, kind: str) -> Any:
        """某一家的「守规矩」版：别的都照原样，只有 acquire 走上面那把锁（资讯实测用）。"""
        return _ExclusiveEnv(self, str(kind))

    @property
    def envs(self) -> dict[str, Any]:
        return dict(self._envs)

    def set_envs(self, envs: dict[str, Any]) -> None:
        """原地换掉手里的几家（网页开关临时机器后热生效；vm_* 工具手里拿的还是这个分派器）。"""
        self._envs = {str(k): v for k, v in (envs or {}).items() if v is not None}

    def busy_kind(self, *, exclude: str = "") -> str:
        """哪家现在占着一台机器（"" = 都空着）；exclude 跳过某一家。"""
        for k, e in self._envs.items():
            if k == exclude:
                continue
            try:
                if getattr(e, "current_box", None) is not None:
                    return k
            except Exception:
                continue
        return ""

    @property
    def current_box(self) -> Any:
        k = self.busy_kind()
        return getattr(self._envs[k], "current_box", None) if k else None

    def _env_of(self, box: Any) -> Any:
        kind = str(getattr(box, "kind", "") or "").strip().lower() or RAILWAY
        env = self._envs.get(kind)
        if env is None:
            raise RuntimeError(f"这台机器（{kind}）对应的平台没开")
        return env

    @staticmethod
    def _limit(env: Any) -> int:
        try:
            return int(getattr(env, "max_run_timeout_s", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def max_run_timeout_s(self) -> int:
        k = self.busy_kind()
        if k:
            return self._limit(self._envs[k])
        limits = [v for v in (self._limit(e) for e in self._envs.values()) if v > 0]
        return min(limits) if limits else 0

    async def run(self, box: Any, command: str, *, timeout_s: Any) -> Any:
        return await self._env_of(box).run(box, command, timeout_s=timeout_s)

    async def put(self, box: Any, local_path: Any, remote_path: str) -> Any:
        return await self._env_of(box).put(box, local_path, remote_path)

    async def get(self, box: Any, remote_path: str, local_path: Any) -> Any:
        return await self._env_of(box).get(box, remote_path, local_path)


class _ExclusiveEnv:
    """VMRouter.exclusive 的产物：委托给那一家，只把 acquire 换成带锁的版本。"""

    def __init__(self, router: VMRouter, kind: str) -> None:
        self._router = router
        self._kind = kind
        self._env = router.envs[kind]

    async def acquire(self, job_id: str) -> Any:
        box, _busy = await self._router.acquire(self._kind, job_id)
        return box

    # 显式委托（不用 __getattr__：仓库禁止动态属性替身，见 test_host_only）
    @property
    def current_box(self) -> Any:
        return getattr(self._env, "current_box", None)

    @property
    def max_run_timeout_s(self) -> int:
        return VMRouter._limit(self._env)

    async def release(self, box: Any) -> Any:
        return await self._env.release(box)

    async def run(self, box: Any, command: str, *, timeout_s: Any) -> Any:
        return await self._env.run(box, command, timeout_s=timeout_s)

    async def put(self, box: Any, local_path: Any, remote_path: str) -> Any:
        return await self._env.put(box, local_path, remote_path)

    async def get(self, box: Any, remote_path: str, local_path: Any) -> Any:
        return await self._env.get(box, remote_path, local_path)

    def last_fail(self) -> Any:
        fn = getattr(self._env, "last_fail", None)
        return fn() if callable(fn) else None

    def usage_today(self) -> int:
        fn = getattr(self._env, "usage_today", None)
        return int(fn()) if callable(fn) else 0


def api_key_of(kind: str, settings: Any) -> str:
    """读这家的密钥：环境变量优先，其次 config.toml（和 jev 的口径一致）。

    返回 "" 表示没有。值本身绝不打印、绝不外发、绝不进 kv。
    """
    preset = get(kind)
    if preset is None or not preset.needs_key:
        return ""
    if preset.key_env:
        env_value = os.environ.get(preset.key_env, "").strip()
        if env_value:
            return env_value
    env = getattr(settings, "environments", None)
    field = f"{preset.kind}_api_key"
    return str(getattr(env, field, "") or "").strip()


# ----------------------------------------------------------------------
# 任务 env 字段 / 提示词里的描述
# ----------------------------------------------------------------------


def _fmt_spec(cpu: int, memory_mib: int) -> str:
    """(2, 4096) → "2 核 4G"；认不出的规格 → ""。"""
    parts: list[str] = []
    if cpu > 0:
        parts.append(f"{int(cpu)} 核")
    if memory_mib > 0:
        if memory_mib % 1024 == 0:
            parts.append(f"{int(memory_mib) // 1024}G")
        else:
            parts.append(f"{int(memory_mib)}M")
    return " ".join(parts)


def describe(kind: Any, box: Any) -> str:
    """任务 env 字段那半句：「<平台> 一次性机器 · 2 核 4G · 到期 HH:MM」。

    规格优先取 box 上平台回的资源；没有就查预设里那个镜像的规格；再没有就只写平台名。
    不认识 kind（第三家还没进表）也不抛，给一句中性文案。
    """
    preset = get(kind)
    name = (preset.describe_name if preset is not None else "") or f"{str(kind or '').strip() or '未知'} 一次性机器"
    spec = preset.fixed_spec if preset is not None else ""
    if not spec:
        cpu = _int_of(getattr(box, "cpu", 0))
        mem = _int_of(getattr(box, "memory_mib", 0))
        if cpu <= 0 or mem <= 0:
            snap_slug = str(getattr(box, "snapshot", "") or "")
            if not snap_slug and preset is not None:
                snap_slug = preset.default_snapshot
            snap = preset.snapshot_spec(snap_slug) if preset is not None else None
            if snap is not None:
                cpu, mem = snap.cpu, snap.memory_mib
        spec = _fmt_spec(cpu, mem)
    out = f"{name} · {spec}" if spec else name
    try:
        expires = float(getattr(box, "expires_ts", 0) or 0)
    except (TypeError, ValueError):
        expires = 0.0
    if expires > 0:
        out += f" · 到期 {clock.bj(expires).strftime('%H:%M')}"
    return out


def _int_of(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# ----------------------------------------------------------------------
# 工厂：按平台名建实现对象（app 用它取代写死的 RailwayEnv 导入）
# ----------------------------------------------------------------------


def make_env(
    kind: str,
    get_settings: Callable[[], Any],
    data_dir: Any,
    *,
    store: Any = None,
    transport: Any = None,
) -> Any:
    """建一个一次性 VM 环境对象；不认识 / 模块没就位 → ValueError。

    - railway：environments.railway.RailwayEnv（transport 用不上，忽略）
    - freestyle：environments.freestyle.FreestyleEnv（transport = httpx.MockTransport，测试注入）
    """
    preset = get(kind)
    if preset is None:
        raise ValueError(f"没有这个一次性 VM 平台：{kind!r}")
    if preset.kind == RAILWAY:
        from .railway import RailwayEnv

        return RailwayEnv(get_settings, data_dir, store=store)
    if preset.kind == FREESTYLE:
        from .freestyle import FreestyleEnv

        return FreestyleEnv(get_settings, data_dir, store=store, transport=transport)
    raise ValueError(f"这个一次性 VM 平台还没接实现：{preset.kind!r}")  # pragma: no cover
