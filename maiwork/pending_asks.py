"""待判 @ 请求（pending_asks）的状态机：先记、再判、再落地，每一步都落库可恢复。

2026-10-13 生产巡检：9 条 @ 请求在 pending_asks 里搁了 3~15 天——判断结论只在内存里，
落地失败 / 画像游标走过去就再没人管。这里把整条链拆成持久的几步：

- record_pending：收到一条要判的 @（只记服务群）。同一条消息再来只更新文字，
  **不改** 首次时间（ts / first_seen）、不把终态重新打开。
- decide：记下判断结论（decision，JSON）。prepare / goal / reminder → decided（待落地）；
  明确 none → 终态；reminder 没有可用时间 → 结论先存着、仍是 pending（等补时间）。
- fail：落地 / 分类失败，按指数退避排下次（attempts / next_try_ts / last_error）。
- complete：只有真落地了才算完成（request / reminder 必须带真 id；screened 是被相似拒绝筛掉）。

handled 列跟 state 同步：终态（completed / none / screened / review）=1，其余 =0——老读者
（intake.slow_queue、画像提示里的标记）照旧只看 handled。超 48 小时还没落地的标 review
（review=1、handled=1）留在表里给人看，不删。所有写都是短事务；给了 conn 就并进调用方事务。

2026-10-15 线上实测：9 条 review 最老的等了 17 天没人处理，日志只有一句 warning。补两件事：
- `archive_expired`：等了超过 `REVIEW_EXPIRED_S`（14 天）还没人处理的标 `expired` 终态
  （保留记录、不再自动重试、记一条中文 info 日志），挂在后台巡检里挨着 `mark_stale` 跑；
- `review_wait_s`：这条待复核等了多久（first_seen 起算）的唯一定义，汇总视图与网页接口共用。

2026-10-15 复核（发版前）：`record_pending` 走 Store 的共享连接（`busy_timeout=5000`），
另一个写入者拿着写锁时会把**收消息钩子**拖满 5 秒（本机实测 5.36s；同步代码没有 await
点，`intake._HOOK_BUDGET_MS` 拦不住），宿主按 1500ms 计超时、连续 3 次熔断 60 秒；而且
写失败 = 这条 @ 丢了。改法分两层，**任何一层都不许丢请求**：

- `record_pending_bounded`：自开一条**独立短连接**、最多等 `timeout_s`（钩子给 150ms）
  写锁，拿不到就返回 None——钩子绝不跟着 Store 的 5 秒 busy_timeout 走；
- **独立磁盘 spool**（`_PendingSpool`，`<库文件同级>/pending_spool/`，0700 目录 /
  0600 文件 / 先写 .tmp 再 os.replace 原子落盘 / 同名幂等 / 限长载荷）：拿不到写锁的那条
  立刻写进 spool——**请求此时已经持久了**（不依赖主库写锁）。后台任务用独立连接
  （`asyncio.to_thread`，绝不跨线程共用 Store 连接）把它写进库，**写进库才删 spool 文件**，
  失败退避重试、永不丢弃；进程被杀 / 热重载后 spool 还在，下一次恢复照常落库。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, unquote

from . import clock

logger = logging.getLogger("maiwork.pending_asks")

KINDS = ("prepare", "goal", "reminder", "none")
REPEATS = ("daily", "weekly", "weekdays")
OPEN_STATES = ("pending", "decided")
TERMINAL_STATES = ("completed", "none", "screened", "review", "expired")
STALE_S = 48 * 3600
# 待复核搁多久没人管就归档成过期（14 天）。模块常量，改这里就够，不动数据库（库号仍 41）。
REVIEW_EXPIRED_S = 14 * 24 * 3600
_BACKOFF_BASE_S = 60.0
_BACKOFF_MAX_S = 6 * 3600.0
_TEXT_MAX = 500
_TITLE_MAX = 30
_ERROR_MAX = 300
# 磁盘 spool（写锁被占时钩子的落点）：目录名（在库文件同级）、单条载荷上限
SPOOL_DIRNAME = "pending_spool"
_SPOOL_NAME_MAX = 100
_SPOOL_REASON_MAX = 200
_SPOOL_TEXT_MAX = _TEXT_MAX
# 巡检（mark_stale，每群 ~5 分钟一次）顺带补写 spool 时一轮最多写几条（有界，不等写锁）
_SPOOL_PATROL_LIMIT = 8
# 补落库一轮的墙钟上限（秒）：写锁被占时一轮只等一次锁，绝不变成几分钟的后台循环
_SPOOL_ROUND_BUDGET_S = 2.0


# 记一条待判 @ 的 upsert（`record_pending` 与钩子那条独立短连接共用一份 SQL，
# 免得两处口径漂移）：冲突时只在还没判（pending）时刷新文字和原因；时间、状态、
# 结论、first_seen 一概不动。
_UPSERT_PENDING_SQL = (
    "INSERT INTO pending_asks (group_id, message_id, user_id, user_name, text, ts, reason,"
    " handled, state, first_seen, updated)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'pending', ?, ?)"
    " ON CONFLICT(group_id, message_id) DO UPDATE SET"
    " user_id=CASE WHEN excluded.user_id!='' THEN excluded.user_id ELSE pending_asks.user_id END,"
    " user_name=CASE WHEN excluded.user_name!='' THEN excluded.user_name ELSE pending_asks.user_name END,"
    " text=CASE WHEN pending_asks.state='pending' AND excluded.text!='' THEN excluded.text"
    " ELSE pending_asks.text END,"
    " reason=CASE WHEN pending_asks.state='pending' THEN excluded.reason ELSE pending_asks.reason END,"
    " updated=excluded.updated"
)


def _upsert_pending(
    conn: Any, gid: str, mid: str, user_id: str, user_name: str, text: str,
    ts: float, reason: str, now: float,
) -> None:
    conn.execute(
        _UPSERT_PENDING_SQL, (gid, mid, user_id, user_name, text, ts, str(reason or ""), ts, now)
    )


def review_wait_s(row: Any, now: float) -> float:
    """这条待复核等了多久（秒）：从 first_seen 起算，缺了退回收到时间 ts，都没有算 0。

    汇总视图和网页接口共用这一处定义，免得两边各算一套。
    """
    d = dict(row) if row is not None else {}
    try:
        seen = float(d.get("first_seen") or 0) or float(d.get("ts") or 0)
    except (TypeError, ValueError):
        seen = 0.0
    if seen <= 0:
        return 0.0
    return max(0.0, float(now) - seen)


class _PendingSpool:
    """待判 @ 的**磁盘兜底**：写锁被占时，钩子把这条 @ 落在这里（不依赖主库写锁）。

    为什么要有它：钩子里落库用独立短连接 + 有界等锁（150ms），拿不到锁时如果只在内存里
    排队，进程被杀 / 热重载就**丢请求**（线上实测：写锁被占时旧代码直接抛
    `database is locked`，这条 @ 就没了）。所以这里给一条真正持久的兜底：

    - 目录 `<库文件同级>/pending_spool/`（0700），一条一个文件
      `<URL 转义的群标识>-<sha1(群号\\0消息号)>.json`（0600）：**群标识放在文件名里**，
      所以「这个群还在不在服务名单」能**不打开文件**就判出来（红线：非服务群零读取）。
      群标识走 `quote(..., safe="")`，不含 `/`，不会拼出路径穿越；
    - 载荷限长（text ≤ 500 / reason ≤ 200 / user_name ≤ 100），写的是**纯数据**，不含密钥；
      载荷里的 `group_id` 必须和文件名里的群标识一致（对不上就跳过并 warning，防串群）；
    - 先写 `<名字>.tmp` 再 `os.replace` **原子落盘**；同名幂等（同一条 @ 重复写只留一份）；
    - 后台把它写进库，**写进库之后才删文件**；写不进就留着，下一次恢复接着写——永不丢弃。

    读取口径（`list(served=...)`）：先按文件名的群标识判服务名单，**不是服务群的文件一律
    不打开**；符号链接跳过；认不出名字的老格式（纯 sha1）不强行读开，只记一次 warning 留着
    （这个 spool 格式还没发布过，不需要兼容）。非服务群的老条目按**文件时间**做 14 天归档
    （`prune_unserved`），也不读内容。

    读写都不碰 Store 的连接，可以安全地在 `asyncio.to_thread` 的线程里跑。
    """

    def __init__(self, store: Any) -> None:
        self._dir: Optional[Path] = None
        path = getattr(store, "path", None)
        if path is not None:
            try:
                self._dir = Path(path).parent / SPOOL_DIRNAME
            except Exception:
                self._dir = None
        self._warned: set[str] = set()      # 认不出的文件名只 warning 一次（别每轮刷）

    @property
    def dir(self) -> Optional[Path]:
        return self._dir

    def _ensure_dir(self) -> Optional[Path]:
        d = self._dir
        if d is None:
            return None
        try:
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
        except OSError:
            logger.exception("待判 @ spool 目录建不起来：%s", d)
            return None
        return d

    @staticmethod
    def _name(gid: str, mid: str) -> str:
        """文件名 = `<URL 转义的群标识>-<sha1(群号\\0消息号)>.json`。

        群标识只做 URL 转义（不含 `/`、`\\`），加 `-` 再拼 hash，**绝不会拼出路径穿越**。
        """
        tag = quote(str(gid), safe="")
        digest = hashlib.sha1(f"{gid}\0{mid}".encode("utf-8", "replace")).hexdigest()
        return f"{tag}-{digest}.json"

    @staticmethod
    def _gid_of_name(name: str) -> Optional[str]:
        """只从文件名里取群标识（**不读文件内容**）；不是我们写的形状 → None。"""
        text = str(name)
        if not text.endswith(".json") or "/" in text or "\\" in text or text in (".", ".."):
            return None
        stem = text[:-5]
        tag, sep, digest = stem.rpartition("-")
        if not sep or not tag or len(digest) != 40:
            return None
        try:
            return unquote(tag)
        except Exception:
            return None

    def _files(self) -> list[Path]:
        """目录里我们格式的 `.json` 文件（符号链接、目录、别的后缀一律不碰）。"""
        d = self._dir
        if d is None or not d.is_dir():
            return []
        try:
            items = list(d.iterdir())
        except OSError:
            logger.exception("读待判 @ spool 目录失败：%s", d)
            return []
        out: list[Path] = []
        for p in items:
            try:
                if p.is_symlink() or not p.is_file() or p.suffix != ".json":
                    continue
            except OSError:
                continue
            out.append(p)
        return sorted(out)

    def _load(self, path: Path) -> Optional[dict]:
        """读一条载荷（**只有已经确认服务群的文件才会走到这里**）。"""
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            logger.warning("待判 @ spool 里有读不出来的文件（留着不动）：%s", path.name)
            return None
        if not isinstance(data, dict) or not data.get("message_id"):
            logger.warning("待判 @ spool 文件形状不对（留着不动）：%s", path.name)
            return None
        return data

    def _warn_unknown(self, path: Path) -> None:
        if path.name in self._warned:
            return
        self._warned.add(path.name)
        logger.warning(
            "待判 @ spool 里有认不出名字的文件（老格式？不读内容、留着不动）：%s", path.name
        )

    def write(self, gid: str, msg: Any, reason: str) -> bool:
        """把一条待判 @ 原子写进 spool（同名幂等）。成功 = 这条请求已经持久了。"""
        d = self._ensure_dir()
        if d is None:
            return False
        mid = str(PendingAsks._field(msg, "message_id", "id") or "")
        if not mid:
            return False
        payload = {
            "v": 1,
            "group_id": str(gid),
            "message_id": mid,
            "user_id": str(PendingAsks._field(msg, "user_id") or "")[:_SPOOL_NAME_MAX],
            "user_name": str(PendingAsks._field(msg, "user_name") or "")[:_SPOOL_NAME_MAX],
            "text": str(PendingAsks._field(msg, "text") or "")[:_SPOOL_TEXT_MAX],
            "reason": str(reason or "")[:_SPOOL_REASON_MAX],
            "ts": clock.now(),
        }
        target = d / self._name(str(gid), mid)
        tmp = target.with_suffix(".tmp")
        try:
            data = json.dumps(payload, ensure_ascii=False)
            # 先写临时文件再 replace：崩在中间也不会留下半条（原子落盘）
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except OSError:
            logger.exception("待判 @ 写 spool 失败（群 %s 消息 %s）", gid, mid)
            try:
                tmp.unlink()
            except OSError:
                pass
            return False
        return True

    def list(self, *, served: Callable[[str], bool] | None = None) -> list[dict]:
        """spool 里**当前服务群**的条目（`served` 给了就按文件名里的群标识先过滤）。

        **非服务群的文件一律不打开**（红线：非服务群零读取）——服务名单只从文件名读，
        不碰载荷。符号链接跳过；认不出名字的老格式只 warning 一次、留着不动。
        载荷里的 `group_id` 和文件名里的群标识对不上 → 跳过并 warning（防串群 / 被改写）。
        """
        out: list[dict] = []
        for p in self._files():
            gid = self._gid_of_name(p.name)
            if gid is None:
                self._warn_unknown(p)
                continue
            if served is not None and not served(gid):
                continue                      # 非服务群：连我们自己的载荷都不读
            data = self._load(p)
            if data is None:
                continue
            if str(data.get("group_id") or "") != gid:
                logger.warning(
                    "待判 @ spool 文件里的群号和文件名对不上（%s），跳过不处理", p.name
                )
                continue
            out.append(data)
        return out

    def prune_unserved(self, served: Callable[[str], bool] | None, now: float, max_age_s: float) -> int:
        """非服务群的老条目按**文件时间**做 14 天归档（不读内容）。

        认得出名字、但群已不在服务名单：`now - mtime > max_age_s` 就删掉（记 info，照现有
        归档口径）；没到就留着等它回到服务名单。认不出名字的老格式一律只 warning、不动。
        返回删了几条。
        """
        dropped = 0
        for p in self._files():
            gid = self._gid_of_name(p.name)
            if gid is None:
                self._warn_unknown(p)
                continue
            if served is not None and served(gid):
                continue
            try:
                age = float(now) - float(p.stat().st_mtime)
            except OSError:
                continue
            if age <= float(max_age_s):
                continue
            try:
                p.unlink()
            except OSError:
                logger.warning("删待判 @ spool 老条目失败（群 %s）：%s", gid, p.name)
                continue
            dropped += 1
            logger.info(
                "待判 @ spool 里这条的群已不在服务名单且超过 %d 天，按归档口径丢掉（群 %s）",
                int(float(max_age_s) // 86400), gid,
            )
        return dropped

    def count(self) -> int:
        """我们格式的条目数（认不出名字的老格式不算——那种没法处理）。"""
        return sum(1 for p in self._files() if self._gid_of_name(p.name) is not None)

    def drop(self, gid: str, mid: str) -> None:
        """这条已经写进库了：删掉 spool 文件（幂等）。"""
        d = self._dir
        if d is None:
            return
        try:
            (d / self._name(str(gid), str(mid))).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("删待判 @ spool 文件失败（群 %s 消息 %s），下次落库还会再写一遍", gid, mid)


class PendingAsks:
    def __init__(self, store: Any, get_settings: Callable[[], Any]) -> None:
        self._store = store
        self._get_settings = get_settings
        self._spool = _PendingSpool(store)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _served(self, gid: str) -> bool:
        try:
            return bool(self._get_settings().is_served(str(gid)))
        except Exception:
            return False

    def _tx(self, conn: Any):
        return self._store.tx() if conn is None else nullcontext(conn)

    @staticmethod
    def _row(conn: Any, gid: str, mid: str) -> Any:
        return conn.execute(
            "SELECT * FROM pending_asks WHERE group_id=? AND message_id=?", (gid, mid)
        ).fetchone()

    @staticmethod
    def _field(msg: Any, *names: str) -> Any:
        for n in names:
            v = msg.get(n) if isinstance(msg, dict) else getattr(msg, n, None)
            if v not in (None, ""):
                return v
        return None

    # ------------------------------------------------------------------
    # 记
    # ------------------------------------------------------------------

    @staticmethod
    def _fields_of(msg: Any, now: float) -> tuple[str, str, str, str, float]:
        """从消息里取 (message_id, user_id, user_name, text, ts)；ts 缺了用 now。"""
        mid = str(PendingAsks._field(msg, "message_id", "id") or "")
        try:
            ts = float(PendingAsks._field(msg, "timestamp", "ts", "message_ts") or 0) or now
        except (TypeError, ValueError):
            ts = now
        user_id = str(PendingAsks._field(msg, "user_id") or "")
        user_name = str(PendingAsks._field(msg, "user_name") or "")
        text = str(PendingAsks._field(msg, "text") or "")[:_TEXT_MAX]
        return mid, user_id, user_name, text, ts

    def record_pending(self, gid: str, msg: Any, reason: str, *, conn: Any = None) -> Optional[dict]:
        """记一条待判 @（msg 可以是 dict 或 host.Msg）。非服务群 / 没消息号 → None。"""
        gid = str(gid or "")
        if not gid or not self._served(gid):
            return None
        now = clock.now()
        mid, user_id, user_name, text, ts = self._fields_of(msg, now)
        if not mid:
            return None
        with self._tx(conn) as c:
            _upsert_pending(c, gid, mid, user_id, user_name, text, ts, str(reason or ""), now)
            row = self._row(c, gid, mid)
        return dict(row) if row is not None else None

    @contextmanager
    def isolated_write(self, timeout_s: float):
        """**独立短连接 + 有界等锁**的写事务；拿不到写锁就 `yield None`。

        为什么要有它：`self._tx()` 用的是 Store 的共享连接（`busy_timeout=5000`）。别的
        写入者拿着写锁时，`BEGIN IMMEDIATE` 会**把共享的事件循环拖满 5 秒**——收消息钩子
        和后台判词都在这个循环上（2026-10-15 复核，本机实测钩子被拖 5.36s，宿主按
        1500ms 计超时、连续 3 次熔断 60 秒，期间所有群的钩子都被跳过）。

        这条连接**只在 with 块里创建、使用、关闭**（可以在 `asyncio.to_thread` 的线程里
        跑，绝不跨线程共用 Store 的连接）；`timeout_s<=0` = 只试一次不等待。
        没有库文件（极简构造 / 假 store）/ 连不上 / 拿不到写锁 → `yield None`（不抛）。
        **提交语义**：body 或 COMMIT 失败一律 ROLLBACK 并把异常抛回调用方（调用方据此
        返回 None，绝不能把「没提交成功」当成写成功）。
        """
        path = getattr(self._store, "path", None)
        if path is None or not Path(path).exists():
            yield None
            return
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(str(path), timeout=max(0.0, float(timeout_s)), isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={max(1, int(float(timeout_s) * 1000))}")
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as e:
            # 连不上 / 拿不到写锁：安全关掉，再按「没写成功」交给调用方——**必须 yield**，
            # 否则 with 会抛「generator didn't yield」（那是实现泄漏，不是契约）
            logger.debug("独立写事务开不起来（%.2fs 内没拿到写锁？）：%s", float(timeout_s), e)
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            yield None
            return
        # BEGIN 成功之后：body 出错或 **COMMIT 失败**都必须 ROLLBACK 并抛回调用方。
        # COMMIT 绝不能吞：`record_pending_bounded` 在提交前就把 row 读出来了，吞掉提交错误
        # 会让调用方以为写成功（spool 文件会被删掉）——那是真丢请求。
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def record_pending_bounded(
        self, gid: str, msg: Any, reason: str, *, timeout_s: float = 0.15
    ) -> Optional[dict]:
        """**钩子专用**的落库：独立短连接 + 最多等 `timeout_s` 秒写锁（见 `isolated_write`）。

        拿不到写锁 / 出错一律返回 None，由调用方落到磁盘 spool 再由后台补落库。
        """
        gid = str(gid or "")
        if not gid or not self._served(gid):
            return None
        now = clock.now()
        mid, user_id, user_name, text, ts = self._fields_of(msg, now)
        if not mid:
            return None
        if getattr(self._store, "path", None) is None:
            # 极简构造 / 假 store（没有库文件）：退回共享连接那条路（短事务，同步）
            return self.record_pending(gid, msg, reason)
        row: Any = None
        try:
            with self.isolated_write(timeout_s) as conn:
                if conn is None:
                    return None
                _upsert_pending(conn, gid, mid, user_id, user_name, text, ts, str(reason or ""), now)
                row = self._row(conn, gid, mid)
        except Exception:
            logger.exception("待判 @ 有界落库出错（群 %s 消息 %s）", gid, mid)
            return None
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # 磁盘 spool（写锁被占时的持久兜底；见 _PendingSpool）
    # ------------------------------------------------------------------

    def spool_write(self, gid: str, msg: Any, reason: str) -> bool:
        """把一条没能立刻落库的待判 @ 原子写进磁盘 spool。成功 = 这条请求已经持久。"""
        return self._spool.write(gid, msg, reason)

    def spool_count(self) -> int:
        """spool 里还有几条（只看文件数，不解析）。"""
        return self._spool.count()

    def flush_spool_once(
        self, *, timeout_s: float = 1.0, now: float | None = None, limit: int = 200,
        budget_s: float | None = None,
    ) -> tuple[list[tuple[str, str]], int]:
        """把 spool 里**当前服务群**的待判 @ 写进库；**写进库才删 spool 文件**。

        返回 `(落地的 [(群号, 消息号)], 还剩几条要写)`。当前不在服务名单里的条目**连载荷都
        不读**（服务名单按文件名里的群标识先判，红线：非服务群零读取 / 零写）：搁过
        `REVIEW_EXPIRED_S`（14 天）的按现有归档口径丢掉（记 info），没到的留着等它回到
        服务名单 / 下次巡检。

        **整轮有界**（2026-10-15 收尾）：`limit` 限条数；第一条写不进（写锁被占）就**结束
        这一轮**（剩下的都算「还剩」，原样留在 spool 里）；另加整轮墙钟上限
        `budget_s`（默认 `_SPOOL_ROUND_BUDGET_S` 秒）。写锁被占时一轮只等一次锁，不会
        变成「200 条 × 1 秒」的几分钟后台事务循环；调用方退避后再来——**永不丢弃**。
        """
        t = clock.now() if now is None else float(now)
        # 非服务群 / 认不出名字的条目：**不读载荷**，按文件时间做 14 天归档
        try:
            self._spool.prune_unserved(self._served, t, REVIEW_EXPIRED_S)
        except Exception:
            logger.exception("清理待判 @ spool 里非服务群的老条目出错，下次再来")
        # 只有当前服务群的条目会被读出来（服务名单在打开文件之前就判完了）；
        # 读完再挡一次：配置可能在两步之间被改掉（TOCTOU），非服务群一律不写
        servable: list[dict] = []
        for e in self._spool.list(served=self._served):
            gid = str(e.get("group_id") or "")
            if not gid or not e.get("message_id"):
                continue
            if not self._served(gid):
                continue
            servable.append(e)
        landed: list[tuple[str, str]] = []
        waiting = 0
        budget = _SPOOL_ROUND_BUDGET_S if budget_s is None else float(budget_s)
        # 整轮上限用**单调钟**：业务 clock 可被冻结 / 系统钟回拨，都不该延长或缩短这一轮
        started = time.monotonic()
        for i, e in enumerate(servable):
            gid, mid = str(e.get("group_id") or ""), str(e.get("message_id") or "")
            if i >= max(1, int(limit)) or (time.monotonic() - started) >= budget:
                waiting += len(servable) - i     # 这一轮不写了，剩下的原样留着
                break
            try:
                row = self.record_pending_bounded(gid, e, str(e.get("reason") or ""), timeout_s=timeout_s)
            except Exception:
                logger.exception("待判 @ 补落库出错（群 %s 消息 %s）", gid, mid)
                row = None
            if row is None:
                # 写不进（多半是写锁被占）：这一轮到此为止，剩下的留着退避后再来
                waiting += len(servable) - i
                break
            self._spool.drop(gid, mid)
            landed.append((gid, mid))
        return landed, waiting

    # ------------------------------------------------------------------
    # 判
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(decision: Any) -> tuple[Optional[dict], bool]:
        """→ (要存的结论, 是否完整可落地)。认不出的 kind → (None, False)。"""
        if not isinstance(decision, dict):
            return None, False
        kind = str(decision.get("kind") or "").strip().lower()
        if kind not in KINDS:
            return None, False
        out: dict = {"kind": kind}
        for k in ("title", "via", "why"):
            v = str(decision.get(k) or "").strip()
            if v:
                out[k] = v[:_TITLE_MAX] if k == "title" else v[:200]
        if kind != "reminder":
            return out, True
        from .profile import _parse_bj_when  # profile 顶层导入本模块，这里局部导入防循环

        remind = decision.get("remind_ts")
        try:
            remind_ts = float(remind) if remind is not None and not isinstance(remind, bool) else None
        except (TypeError, ValueError):
            remind_ts = None
        when = str(decision.get("when") or "").strip()
        if remind_ts is None and when:
            remind_ts = _parse_bj_when(when)
        if when:
            out["when"] = when
        due = decision.get("due_ts")
        try:
            due_ts = float(due) if due is not None and not isinstance(due, bool) else None
        except (TypeError, ValueError):
            due_ts = None
        repeat = str(decision.get("repeat") or "").strip().lower()
        if repeat == "workdays":
            repeat = "weekdays"
        out["repeat"] = repeat if repeat in REPEATS else None
        if remind_ts is None or remind_ts <= 0:
            return out, False
        out["remind_ts"] = remind_ts
        out["due_ts"] = due_ts if due_ts is not None else remind_ts
        return out, True

    def _decide_on(self, c: Any, gid: str, mid: str, norm: dict, complete: bool, now: float) -> bool:
        """在给定连接/事务里记结论（`decide` 与 `decide_bounded` 共用一份口径）。"""
        row = self._row(c, gid, mid)
        if row is None or str(row["state"]) not in OPEN_STATES:
            return False
        if str(row["state"]) == "decided":
            return True
        payload = json.dumps(norm, ensure_ascii=False)
        if not complete:
            c.execute(
                "UPDATE pending_asks SET decision=?, updated=? WHERE group_id=? AND message_id=?",
                (payload, now, gid, mid),
            )
            return False
        state, handled = ("none", 1) if norm["kind"] == "none" else ("decided", 0)
        c.execute(
            "UPDATE pending_asks SET decision=?, state=?, handled=?, next_try_ts=0, updated=?"
            " WHERE group_id=? AND message_id=?",
            (payload, state, handled, now, gid, mid),
        )
        return True

    def decide(self, gid: str, mid: str, decision: dict, *, conn: Any = None) -> bool:
        """记判断结论。返回 True = 结论完整、已进 decided（none 直接终态）。

        行不存在 / 非服务群 / 已是终态 → False（终态不重新打开）。已 decided 的不改主意（返回 True）。
        reminder 缺可用时间：结论先存（state 仍 pending），返回 False。
        """
        gid, mid = str(gid or ""), str(mid or "")
        if not gid or not mid or not self._served(gid):
            return False
        norm, complete = self._normalize(decision)
        if norm is None:
            return False
        now = clock.now()
        with self._tx(conn) as c:
            return self._decide_on(c, gid, mid, norm, complete, now)

    def decide_bounded(
        self, gid: str, mid: str, decision: dict, *, timeout_s: float = 0.2
    ) -> Optional[bool]:
        """**intake 判词链专用**：独立短连接 + 有界等锁记结论（见 `isolated_write`）。

        返回 True/False 同 `decide`；**None = 没写成功**（写锁被占 / 库错）——调用方据此
        别接着建请求，留给独立重试路径（保证一条 @ 只判一次）。
        """
        gid, mid = str(gid or ""), str(mid or "")
        if not gid or not mid or not self._served(gid):
            return False
        norm, complete = self._normalize(decision)
        if norm is None:
            return False
        if getattr(self._store, "path", None) is None:
            return self.decide(gid, mid, decision)
        now = clock.now()
        try:
            with self.isolated_write(timeout_s) as conn:
                if conn is None:
                    return None
                return self._decide_on(conn, gid, mid, norm, complete, now)
        except Exception:
            logger.exception("记 @ 的判断结论出错（群 %s 消息 %s）", gid, mid)
            return None

    # ------------------------------------------------------------------
    # 失败 / 完成
    # ------------------------------------------------------------------

    def _fail_on(self, c: Any, gid: str, mid: str, error: Any, now: float) -> bool:
        row = self._row(c, gid, mid)
        if row is None or str(row["state"]) not in OPEN_STATES:
            return False
        attempts = int(row["attempts"] or 0) + 1
        delay = min(_BACKOFF_MAX_S, _BACKOFF_BASE_S * (2 ** (attempts - 1)))
        c.execute(
            "UPDATE pending_asks SET attempts=?, next_try_ts=?, last_error=?, updated=?"
            " WHERE group_id=? AND message_id=?",
            (attempts, now + delay, str(error or "")[:_ERROR_MAX], now, gid, mid),
        )
        return True

    def fail(self, gid: str, mid: str, error: Any, *, conn: Any = None) -> bool:
        """落地（decided）或分类（pending）失败：按指数退避排下次。终态不动。"""
        gid, mid = str(gid or ""), str(mid or "")
        now = clock.now()
        with self._tx(conn) as c:
            return self._fail_on(c, gid, mid, error, now)

    def fail_bounded(self, gid: str, mid: str, error: Any, *, timeout_s: float = 0.2) -> Optional[bool]:
        """**intake 判词链专用**：独立短连接 + 有界等锁记失败退避；None = 没写成功。"""
        gid, mid = str(gid or ""), str(mid or "")
        if getattr(self._store, "path", None) is None:
            return self.fail(gid, mid, error)
        now = clock.now()
        try:
            with self.isolated_write(timeout_s) as conn:
                if conn is None:
                    return None
                return self._fail_on(conn, gid, mid, error, now)
        except Exception:
            logger.exception("记 @ 落地失败出错（群 %s 消息 %s）", gid, mid)
            return None

    def _complete_on(self, c: Any, gid: str, mid: str, kind: str, rid: str, state: str, now: float) -> bool:
        row = self._row(c, gid, mid)
        if row is None:
            return False
        cur = str(row["state"])
        if cur in ("completed", "screened"):
            return cur == state and str(row["result_id"] or "") == rid
        if cur in ("none", "expired"):
            # 明确闲聊 / 已归档过期：都不再复活（迟到的成功也不改历史）
            return False
        # pending / decided / review（复核期间迟到的成功也如实记上）
        c.execute(
            "UPDATE pending_asks SET state=?, handled=1, result_kind=?, result_id=?, last_error='',"
            " updated=? WHERE group_id=? AND message_id=?",
            (state, kind, rid, now, gid, mid),
        )
        return True

    def complete(
        self, gid: str, mid: str, *, result_kind: str = "request", result_id: str = "", conn: Any = None
    ) -> bool:
        """真落地了才调。request / reminder 必须带真 id；screened 允许空 id。已完成同结果 → True（幂等）。"""
        gid, mid = str(gid or ""), str(mid or "")
        kind = str(result_kind or "").strip()
        rid = str(result_id or "").strip()
        if kind not in ("request", "reminder", "screened"):
            return False
        if kind != "screened" and not rid:
            return False
        state = "screened" if kind == "screened" else "completed"
        now = clock.now()
        with self._tx(conn) as c:
            return self._complete_on(c, gid, mid, kind, rid, state, now)

    def complete_bounded(
        self, gid: str, mid: str, *, result_kind: str = "request", result_id: str = "",
        timeout_s: float = 0.2,
    ) -> Optional[bool]:
        """**intake 判词链专用**：独立短连接 + 有界等锁关行；None = 没写成功。

        None 时调用方**不要**当失败：请求已经建出来了（`approvals.create` 按来源消息去重），
        行留给独立重试路径补关，再点一次也复用同一条请求。
        """
        gid, mid = str(gid or ""), str(mid or "")
        kind = str(result_kind or "").strip()
        rid = str(result_id or "").strip()
        if kind not in ("request", "reminder", "screened"):
            return False
        if kind != "screened" and not rid:
            return False
        state = "screened" if kind == "screened" else "completed"
        if getattr(self._store, "path", None) is None:
            return self.complete(gid, mid, result_kind=kind, result_id=rid)
        now = clock.now()
        try:
            with self.isolated_write(timeout_s) as conn:
                if conn is None:
                    return None
                return self._complete_on(conn, gid, mid, kind, rid, state, now)
        except Exception:
            logger.exception("关待判 @ 出错（群 %s 消息 %s）", gid, mid)
            return None

    # ------------------------------------------------------------------
    # 查 / 过期
    # ------------------------------------------------------------------

    @staticmethod
    def view(row: Any) -> dict:
        d = dict(row)
        try:
            d["decision"] = json.loads(d.get("decision") or "") if d.get("decision") else None
        except (TypeError, ValueError):
            d["decision"] = None
        return d

    def get(self, gid: str, mid: str) -> Optional[dict]:
        row = self._row(self._store.read(), str(gid), str(mid))
        return self.view(row) if row is not None else None

    def due(self, gid: str, *, now: float | None = None, limit: int = 8) -> list[dict]:
        """判了还没落地、退避时间已到的（最老的先）。"""
        t = clock.now() if now is None else float(now)
        rows = self._store.read().execute(
            "SELECT * FROM pending_asks WHERE group_id=? AND state='decided' AND next_try_ts<=?"
            " ORDER BY first_seen ASC, message_id ASC LIMIT ?",
            (str(gid), t, max(1, int(limit))),
        ).fetchall()
        return [self.view(r) for r in rows]

    def undecided(self, gid: str, *, before: float, now: float | None = None, limit: int = 8) -> list[dict]:
        """还没判、首次出现早于 before、退避时间已到的（交给独立分类）。"""
        t = clock.now() if now is None else float(now)
        rows = self._store.read().execute(
            "SELECT * FROM pending_asks WHERE group_id=? AND state='pending' AND (CASE WHEN first_seen>0 THEN first_seen ELSE ts END)<=?"
            " AND next_try_ts<=? ORDER BY first_seen ASC, message_id ASC LIMIT ?",
            (str(gid), float(before), t, max(1, int(limit))),
        ).fetchall()
        return [self.view(r) for r in rows]

    def review_view(self, gid: str, *, limit: int = 100) -> list[dict]:
        """只读本服务群的待复核，最老的先；管理员身份由 HTTP 层校验。"""
        if not self._served(gid):
            return []
        rows = self._store.read().execute(
            "SELECT * FROM pending_asks WHERE group_id=? AND state='review'"
            " ORDER BY first_seen ASC, message_id ASC LIMIT ?", (str(gid), min(100, max(1, int(limit)))),
        ).fetchall()
        return [self.view(r) for r in rows]

    def review_count(self, gid: str) -> int:
        """本服务群待复核的**真值条数**（列表最多列 100 条，计数不受它影响）。"""
        if not self._served(gid):
            return 0
        row = self._store.read().execute(
            "SELECT COUNT(*) AS c FROM pending_asks WHERE group_id=? AND state='review'", (str(gid),)
        ).fetchone()
        return int(row["c"] or 0) if row is not None else 0

    def review_action(self, gid: str, mid: str, action: str, *, by: str,
                      approvals: Any = None, kind: str = "task") -> dict:
        """人工复核，不调用模型/宿主。转请求始终强制待批；失败保留原行。

        create 按来源消息去重；若建请求后关行失败，再点击可复用同一请求。
        全程同步、无 await，网页与后台重试不会在两步中间交错。
        """
        gid, mid = str(gid), str(mid)
        if not self._served(gid):
            raise KeyError("没有这个群")
        if action not in ("request", "ignore") or (action == "request" and kind not in ("task", "goal")):
            raise ValueError("只能转为任务 / 目标请求，或忽略")
        row = self.get(gid, mid)
        if row is None:
            raise KeyError("找不到这条待复核")
        if action == "request" and row["state"] == "completed" and row["result_kind"] == "request":
            return {"ok": True, "request_id": row["result_id"]}
        if action == "ignore" and row["state"] == "none" and row["result_kind"] == "ignored":
            return {"ok": True}
        if row["state"] != "review":
            raise ValueError("这条已处理，请刷新页面")
        rid = ""
        if action == "request":
            if approvals is None:
                raise RuntimeError("批准模块还没准备好")
            decision = row.get("decision") or {}
            res = approvals.create(
                gid, kind=kind, title=str(decision.get("title") or row["text"] or "群里的请求")[:30],
                quote=str(row["text"]), via="管理员从待复核转入", requester_id=str(row["user_id"]),
                requester_name=str(row["user_name"]), message_id=mid, force_manual=True, screen=False,
            )
            rid = str(res.get("id") or "")
            if not rid:
                raise RuntimeError("没有拿到请求编号，请稍后再试")
        with self._store.tx() as conn:
            cur = self._row(conn, gid, mid)
            if cur is None or cur["state"] != "review":
                raise ValueError("这条已处理，请刷新页面")
            conn.execute(
                "UPDATE pending_asks SET state=?, handled=1, review=0, result_kind=?, result_id=?,"
                " updated=? WHERE group_id=? AND message_id=?",
                ("completed" if rid else "none", "request" if rid else "ignored", rid, clock.now(), gid, mid),
            )
            self._store.event(conn, "ask.review", group_id=gid,
                              payload={"message_id": mid, "action": action, "by": by, "request_id": rid})
        return {"ok": True, "request_id": rid} if rid else {"ok": True}

    def mark_stale(self, gid: str, *, now: float | None = None, conn: Any = None) -> int:
        """超 48 小时还没落地的：标 review 留着（handled=1，不再自动重试），不删。

        顺带当「没消息时的 spool 兜底」：这个入口由后台巡检每群 ~5 分钟调一次
        （`profile.recover_pending_asks`，跑在**共享事件循环的那个线程**上，不是新线程），
        顺手把 spool 里当前服务群的待判 @ 补写进库。`timeout_s=0` = **不等写锁**（只试
        一次，`busy_timeout` 1ms），最多 `_SPOOL_PATROL_LIMIT` 条，所以这里不会卡住事件
        循环；真正会等锁的重试在 `intake._drain_pending_writes` 的 worker 线程里。
        给了 conn（在调用方事务里）就不碰 spool——那是别的事务里的调用，不能另开连接。
        """
        if conn is None and self.spool_count() > 0:
            try:
                self.flush_spool_once(timeout_s=0.0, limit=_SPOOL_PATROL_LIMIT)
            except Exception:
                logger.exception("巡检补写待判 @ spool 出错（群 %s），下次再来", gid)
        t = clock.now() if now is None else float(now)
        with self._tx(conn) as c:
            cur = c.execute(
                "UPDATE pending_asks SET state='review', review=1, handled=1, updated=?"
                " WHERE group_id=? AND state IN ('pending', 'decided')"
                " AND (CASE WHEN first_seen>0 THEN first_seen ELSE ts END)<?",
                (t, str(gid), t - STALE_S),
            )
            n = int(cur.rowcount or 0)
        if n:
            logger.warning("群 %s 有 %d 条 @ 请求超 48 小时没落地，标为待复核（保留）", gid, n)
        return n

    def archive_expired(self, gid: str, *, now: float | None = None, conn: Any = None) -> int:
        """待复核搁了超过 REVIEW_EXPIRED_S（14 天）还没人处理的：标 expired 归档。

        只动 review 行（pending / decided 由 mark_stale 管，终态一概不碰）；记录保留、不删，
        只是不再自动重试、也不再进待办计数。返回归档了几条。
        """
        if not self._served(str(gid)):
            return 0
        t = clock.now() if now is None else float(now)
        with self._tx(conn) as c:
            cur = c.execute(
                "UPDATE pending_asks SET state='expired', handled=1, review=0, updated=?"
                " WHERE group_id=? AND state='review'"
                " AND (CASE WHEN first_seen>0 THEN first_seen ELSE ts END)<?",
                (t, str(gid), t - REVIEW_EXPIRED_S),
            )
            n = int(cur.rowcount or 0)
        if n:
            logger.info(
                "群 %s 有 %d 条待复核 @ 请求超过 %d 天没人处理，标为已过期（记录保留，不再自动重试）",
                gid, n, int(REVIEW_EXPIRED_S // 86400),
            )
        return n
