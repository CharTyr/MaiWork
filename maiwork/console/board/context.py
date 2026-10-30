"""一次看板请求共用的 Ctx：只读连接、服务群集合、岗位名 / 小鱼种子、群名缓存、now。

适配器（sources/*.py）只通过 Ctx 拿这些共享信息，自己不重复开连接、重复
算服务群；Ctx 也负责把 rows 变成网页看到的字段（group_name / elapsed_s /
counts / last_step / title 截断）。
"""

from __future__ import annotations

import logging
from typing import Any

from ... import clock
from ...names import clean_group_name
from .. import views

logger = logging.getLogger("maiwork.console.board")

STUCK_AFTER_S = 600          # 在跑的东西超过这么久没动静算「卡住」
DEFAULT_HOURS = 24
MAX_HOURS = 168              # 一周
TITLE_MAX = 80               # title / 摘要最多多少字
_FETCH_LIMIT = 4000          # 每类来源最多取多少行（防病态库扫全表）

# lane 顺序：main → news → idea → goal → task → topic → 自定义 c_*
_LANE_ORDER = ("main", "news", "idea", "goal", "task", "topic")


def normalize_hours(raw: Any, default: int = DEFAULT_HOURS) -> int:
    """hours 参数：认不出（空 / 不是整数）用默认；大了夹到 168，小了夹到 1。"""
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        n = int(default or DEFAULT_HOURS)
    return max(1, min(MAX_HOURS, n))


def _clip(text: Any, n: int = TITLE_MAX) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


class Ctx:
    """一次请求的现场。构造失败一律降级成「空」，绝不 500（同 usage_history）。"""

    def __init__(self, svc: Any, *, now: float | None = None) -> None:
        self.svc = svc
        self.now = float(now) if now is not None else clock.now()
        self._conn: Any = None
        self._served: set[str] = set()
        self.titles: dict[str, str] = {}
        self.fish: dict[str, str] = {}
        self._group_name_cache: dict[str, str] = {}
        self._build()

    # ---------- 现场 ----------

    def _build(self) -> None:
        try:
            self._conn = self.svc.store.read() if self.svc is not None else None
        except Exception:
            logger.exception("看板拿不到库连接")
            self._conn = None
        try:
            self._served = set(views._served_group_ids(self.svc))
        except Exception:
            logger.exception("看板拿不到服务群集合")
            self._served = set()
        self.titles = self._agent_titles()
        self.fish = self._agent_fish()

    def _agent_titles(self) -> dict[str, str]:
        """岗位 kind → 中文岗位名：优先 agents.profiles()（含自定义），拿不到就用内建默认。"""
        titles: dict[str, str] = {
            "main": "主模型",
            "news": "资讯",
            "idea": "构想",
            "goal": "目标",
            "task": "通用任务",
        }
        agents = getattr(self.svc, "agents", None)
        try:
            profiles = agents.profiles() if agents is not None else []
        except Exception:
            profiles = []
        try:
            for p in profiles or []:
                kind = str(p.get("kind") or "")
                title = str(p.get("title") or "").strip()
                if kind and title:
                    titles[kind] = title
        except Exception:
            pass
        return titles

    def _agent_fish(self) -> dict[str, str]:
        """各专岗小鱼种子（kind → fish_seed，空串 = 按岗位名的默认小鱼）；网页画头像用。"""
        out = {k: "" for k in self.titles}
        agents = getattr(self.svc, "agents", None)
        try:
            for p in (agents.profiles() if agents is not None else []) or []:
                kind = str(p.get("kind") or "")
                seed = p.get("fish_seed")
                if kind:
                    out[kind] = seed if isinstance(seed, str) else ""
        except Exception:
            pass
        return out

    # ---------- lane 名单 ----------

    def lane_kinds(self) -> list[str]:
        """lane 顺序：main → news → idea → goal → task → topic → 自定义 c_*（含 topic）。"""
        titles = {"topic": "开话题", **self.titles}
        out = [k for k in _LANE_ORDER]
        for k in titles:
            if k not in out:
                out.append(k)
        return out

    def lane_title(self, kind: str) -> str:
        if kind == "topic":
            return "开话题"
        return self.titles.get(kind, kind)

    # ---------- 查询 / 行处理 ----------

    @property
    def conn(self) -> Any:
        return self._conn

    @property
    def served(self) -> set[str]:
        return self._served

    def fetch(self, sql: str, args: tuple = ()) -> list[Any]:
        """查一把；表还没建等任何异常 → 空列表，绝不 500。"""
        if self._conn is None:
            return []
        try:
            return list(self._conn.execute(sql, args).fetchall())
        except Exception as e:
            logger.warning("看板查询失败（当空算）：%s", e)
            return []

    def fetch_one(self, sql: str, args: tuple) -> Any:
        rows = self.fetch(sql, args)
        return rows[0] if rows else None

    def group_name(self, group_id: str) -> str:
        """群名（带缓存）；拿不到 → clean_group_name('', gid) 兜底。"""
        gid = str(group_id or "")
        if gid in self._group_name_cache:
            return self._group_name_cache[gid]
        try:
            row = views._group_row(self.svc, gid)
            name = row.get("name") if isinstance(row, dict) else None
        except Exception:
            name = None
        out = clean_group_name(name or "", gid)
        self._group_name_cache[gid] = out
        return out

    # ---------- Item → 对外字段 ----------

    def finish_item(self, item: dict[str, Any]) -> dict[str, Any]:
        """给 Item 补上对外字段：去内部字段（lane / task_id / _run_ref /
        _trace_key / error / trace / _extra_think），加 group_name /
        elapsed_s / title 截断 / counts / last_step 占位。"""
        out = dict(item)
        for k in ("lane", "task_id", "_run_ref", "_trace_key",
                  "trace", "_extra_think", "_finished_ts"):
            out.pop(k, None)
        out["title"] = _clip(out.get("title") or "")
        gid = str(out.get("group_id") or "")
        # 不属于任何群（不聚焦的管理员对话）：留空，别兜成占位的「群」
        out["group_name"] = self.group_name(gid) if gid else ""
        if "elapsed_s" not in out:
            # 结束了 = 用时（finished − started）；没结束 = 已做多久（now − started）
            end = _f(out.get("finished")) or self.now
            out["elapsed_s"] = max(0, int(end - _f(out.get("started"))))
        return out


def _paused_kind(raw: Any) -> str:
    """tasks.paused_reason（JSON 串）→ kind；认不出 → 空串。"""
    import json

    try:
        data = json.loads(raw) if isinstance(raw, str) and raw.strip() else None
    except Exception:
        data = None
    return str(data.get("kind") or "") if isinstance(data, dict) else ""
