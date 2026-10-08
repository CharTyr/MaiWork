"""group_push.py：每群一份「往群里发」的设置（**唯一真源**）。

它替掉了原来散在两处的三套节制（`[delivery] push_per_day` + `[topics] per_day` +
资讯卡片 / 构想提一嘴各自的每日上限），只留：

- `topics_enabled` / `news_card_enabled` / `news_card_count` / `idea_mention_enabled`：
  三种 MaiWork 自己往群里发的东西的开关（默认都沿用老行为：开话题看 `[topics] enabled`、
  卡片和提一嘴默认关）。
- `daily_max`：**一个**每日总上限，三种 kind 一起数（沿用原 `[delivery] push_per_day`
  默认 3，不加大）；0 = 不限。
- `quiet_hours`：睡觉时段（沿用原 `[delivery] quiet_hours`）。
- `news_card_since` / `idea_mention_since`：开关打开的时刻（内部字段，只有 set 里开关
  从关到开才写）；开开关之前出来的批次 / 构想不补发。

存 `kv["group_push.<群号>"]`（`KV_PREFIX`，不进 config.toml）。第一次读某个服务群时
从现有设置 seed，并把旧的 `kv["cardpush.<群号>"]` 里的开关 / 条数 / since 一起带过来、
**删掉旧键**（惰性迁移，幂等：第二次读只认 `group_push.*`）。

只认服务群：非服务群 `get_config` 直接给一份**保守默认**（三个自动群发开关全关、
额度有限、默认睡觉时段；零库读取、零写入），`set_config` 抛 ValueError——非服务群零
读取是红线，也绝不能用 legacy「话题默认开」那份放行（上游误传群号时照样不能读资料 /
调模型 / 入队）。认不出服务群（没有 `is_served` 也没有 `groups`，或判定抛异常）比照坏
配置：保守默认 + 零读零写，绝不默认开。

写：`set_config` 先逐字段校验（错就抛 ValueError，一个字段都不落库），再整份替换。
退役字段（每类每日上限那五个）**所有调用口都拒**（legacy `settings=None` 同样拒，不
静默假保存），提示去群页改。

失败关闭（fail-closed，2026-10 复审）：只有键**明确缺失**才 seed；存在但坏掉 / 是
null / 不是对象 / 读失败 → 保守默认（三个自动群发开关全关、额度有限、默认睡觉时段），
不写、也不删旧源。读路径的开关只认真 true/false（`bool("false")` 会把自动开话题
打开）。种子事务里二次读，并发 `set_config` 刚存下的新记录不会被覆盖。生产路径
拿不到 settings 时传 `UNAVAILABLE`（零读零写 + 保守默认），不要用 `None` ——
`None` 是显式 legacy 的放行口径。

视图：`view()` 给网页「往群里发」区用——今天的设置 + 真发出去几条 + 占掉多少额度 +
最近几条（结果不明的那条明确标 `uncertain`，绝不写成「已发」）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Iterable, Optional

from . import clock

logger = logging.getLogger("maiwork.group_push")

# 父会话读写这块数据只用这两个常量（别再各处硬编码字符串）
KV_PREFIX = "group_push."
LEGACY_KV_PREFIX = "cardpush."

DEFAULT_QUIET_HOURS = "23:00-08:00"
DEFAULT_DAILY_MAX = 3

# kv_get 的默认哨兵：把「键不存在」和「存在但值是 null / 解不开」区分开。
# 只有**明确缺失**才 seed；存在但读不出可信的一份 → 保守默认，不写、不删旧源
# （否则管理員在网页上关掉的开关会被旧全局种子重新打开）。
_MISSING = object()

# 生产路径「拿不到 settings」的哨兵：和显式 settings=None 的老纯数据层调用区分开。
# 拿到它就零读零写、直接给保守默认（不是老数据层的放行口径）。
UNAVAILABLE = object()


def _seed_ready(settings: Any) -> bool:
    """这轮允不允许「缺失就惰性播种」。

    - 显式 legacy（`settings is None`，老纯数据层调用）→ True（老合同照旧）。
    - 真 Settings：认派生字段 `group_controls_seed_ready`——缺省 True（老合同）；
      App 在「旧配置覆盖层没物化成功」时显式设 False → 不播种，只回保守默认。
      字段不是真 bool（坏值）→ 按 False（收紧，不打开）。
    """
    raw = getattr(settings, "group_controls_seed_ready", True)
    return raw if isinstance(raw, bool) else False

# 读失败 / 记录坏掉时的兜底：三个自动群发开关全关、额度有限、睡觉时段默认
_SAFE_DEFAULTS = {
    "topics_enabled": False,
    "news_card_enabled": False,
    "news_card_count": 3,
    "idea_mention_enabled": False,
    "daily_max": DEFAULT_DAILY_MAX,
    "quiet_hours": DEFAULT_QUIET_HOURS,
    "news_card_since": 0.0,
    "idea_mention_since": 0.0,
}
# 显式 settings=None（老纯数据层调用口）缺键时的底：沿用老行为（话题默认开）
_LEGACY_DEFAULTS = {**_SAFE_DEFAULTS, "topics_enabled": True}

# 三种「MaiWork 自己往群里发」的消息：开关 = 配置字段；每日总上限三种一起数
KIND_SWITCH = {
    "topic": "topics_enabled",
    "news_card": "news_card_enabled",
    "idea_mention": "idea_mention_enabled",
}
BOOL_FIELDS = ("topics_enabled", "news_card_enabled", "idea_mention_enabled")
INT_RANGES = {"news_card_count": (1, 3), "daily_max": (0, 24)}   # daily_max 0 = 不限
SINCE_OF_SWITCH = {"news_card_enabled": "news_card_since", "idea_mention_enabled": "idea_mention_since"}
SINCE_FIELDS = ("news_card_since", "idea_mention_since")
# 旧字段：退役了——所有调用口一律拒（含 legacy settings=None），不静默假保存
RETIRED_FIELDS = frozenset((
    "news_card_daily_max", "idea_mention_daily_max", "push_per_day", "topics_per_day", "per_day",
))
SETTABLE = frozenset(BOOL_FIELDS) | frozenset(INT_RANGES) | {"quiet_hours"}

_HHMM_RANGE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")

_STATE_CN = {
    "pending": "待发",
    "sending": "发送中",
    "sent": "已发",
    "uncertain": "不确定",
    "failed": "失败",
    "dropped": "已作废",
    "cancelled": "已取消",
}


# ----------------------------------------------------------------------
# 读
# ----------------------------------------------------------------------


def get_config(store: Any, group_id: Any, settings: Any = None) -> dict:
    """这个群的「往群里发」设置；第一次读会 seed（含旧 cardpush 惰性迁移）。

    settings 取值：
    - `None`（缺省，老调用口）：显式 legacy 纯数据层——不查服务名单，仍会落库 seed；
    - `UNAVAILABLE`：生产路径拿不到配置——**零读零写**，直接给保守默认；
    - 真 Settings：查服务名单；非服务群**保守默认**（开关全关）、零库读取、零写入。

    失败关闭（2026-10 复审）：键**明确缺失**才 seed；键存在但坏掉 / 是 null /
    不是对象 / 读失败 → 保守默认（开关全关、额度有限、默认睡觉时段），
    不写库、不删旧来源、绝不拿旧全局种子把管理员关掉的开关恢复成开。
    """
    gid = str(group_id)
    if settings is UNAVAILABLE:
        return _conservative()
    if settings is not None:
        state = _served_state(settings, gid)
        if state is None:
            # 没有服务群证据 / 判定炸了：零读零写 + 保守默认（不碰外群、不默认开）
            return _conservative(settings)
        if state is False:
            # 非服务群：保守默认（三个自动开关全关）+ 零读零写。绝不回落到 legacy
            # 「话题默认开」那份——上游哪怕误传群号，也不能读资料 / 调模型 / 入队。
            return _conservative(settings)
    key = f"{KV_PREFIX}{gid}"
    try:
        raw = store.kv_get(key, _MISSING)
    except Exception:
        logger.debug("读每群推送设置失败（群 %s），按保守默认、不重种", gid, exc_info=True)
        return _conservative(settings)
    if raw is _MISSING:
        if not _seed_ready(settings):
            # 群控归一没迁成功（App 的 group_controls_seed_ready 门关着）：缺失记录只回
            # 保守默认，绝不拿这份「没被旧覆盖盖过的有效值」当种子落库。
            logger.info("群 %s 的往群里发设置还没从旧的全局设置迁过来，缺失记录按保守默认、不播种", gid)
            return _conservative(settings)
        return _seed(store, gid, settings)
    if not isinstance(raw, dict):
        logger.warning("每群推送设置记录坏掉（群 %s），按保守默认、不重种", gid)
        return _conservative(settings)
    return _normalize(raw, settings)


def _loads(raw: Any) -> Any:
    """kv 里的原始文本 → Python 值；解不开 → None（调用方按「坏记录」处理）。"""
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _existing_config(store: Any, gid: str) -> dict | None:
    """库里已有一份**合法**的每群设置吗（缺 / 坏 / 读失败 → None）。"""
    try:
        raw = store.kv_get(f"{KV_PREFIX}{gid}", _MISSING)
    except Exception:
        logger.debug("读已有的每群推送设置失败（群 %s）", gid, exc_info=True)
        return None
    return raw if isinstance(raw, dict) else None


def _seed(store: Any, gid: str, settings: Any) -> dict:
    """首次 seed：现有设置 + 旧 cardpush（带完删源）。返回落库后的整份配置。

    事务里**二次读**兜住并发：另一个 `set_config`（或另一路启动迁移）刚存下的新
    记录、或已经坏掉的记录，一律不覆盖、不删旧键；读失败 / 写失败返回保守默认，
    绝不把「种出来的值」当结果返回（那等于没落库也当成配置生效）。
    """
    if not _seed_ready(settings):
        return _conservative(settings)
    payload, legacy_present = _seed_payload(store, gid, settings)
    key = f"{KV_PREFIX}{gid}"
    try:
        with store.tx() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            if row is not None:
                existing = _loads(row["value"])
                if isinstance(existing, dict):
                    return _normalize(existing, settings)
                logger.warning("每群推送设置记录坏掉（群 %s），不重种", gid)
                return _conservative(settings)
            store.kv_set(conn, key, payload)
            if legacy_present:
                # 惰性迁移：旧的 cardpush.<群号> 删掉，从此只有一份真源（幂等）
                store.kv_delete(conn, f"{LEGACY_KV_PREFIX}{gid}")
    except Exception:
        logger.exception("种每群推送设置失败（群 %s），按保守默认处理", gid)
        return _conservative(settings)
    logger.info("群 %s 的「往群里发」设置已归一到 group_push%s", gid,
                "（旧 cardpush 键已删）" if legacy_present else "")
    return payload


def _seed_payload(store: Any, gid: str, settings: Any) -> tuple[dict, bool]:
    """seed 的内容 + 旧来源在不在（旧来源读失败 → 不迁、不删）。"""
    cfg = _from_settings(settings)
    legacy_key = f"{LEGACY_KV_PREFIX}{gid}"
    legacy: Any = None
    try:
        legacy = store.kv_get(legacy_key, None)
    except Exception:
        logger.debug("读旧 cardpush 设置失败（群 %s），本次不迁旧源", gid, exc_info=True)
    present = isinstance(legacy, dict)
    if present:
        for key in ("news_card_enabled", "news_card_count", "idea_mention_enabled") + SINCE_FIELDS:
            if key in legacy:
                cfg[key] = legacy[key]
    return _normalize(cfg, settings), present


def conservative_defaults(settings: Any = None) -> dict:
    """保守默认（不动库、也**不读**库）：开关全关、额度有限、睡觉时段默认。

    读设置失败 / 每群记录坏掉时的兜底口。额度上限收敛到 `DEFAULT_DAILY_MAX`：
    绝不放大成 0（不限），也不会比管理员设的值更大。
    """
    return _conservative(settings)


def defaults(settings: Any = None) -> dict:
    """读不出可信配置时的兜底（不动库）：保守默认。老调用口（None）也一样保守。"""
    return _conservative(settings)


def _conservative(settings: Any = None) -> dict:
    """坏配置 / 读失败时的完整默认：三个自动群发开关都关。"""
    out = dict(_SAFE_DEFAULTS)
    if settings is not None and settings is not UNAVAILABLE:
        try:
            want = int(getattr(getattr(settings, "delivery", None), "push_per_day",
                               DEFAULT_DAILY_MAX))
        except (TypeError, ValueError):
            want = DEFAULT_DAILY_MAX
        out["daily_max"] = want if 0 < want <= DEFAULT_DAILY_MAX else DEFAULT_DAILY_MAX
    return out


def _strict_bool(raw: Any, *, default: bool) -> bool:
    """读取路径的布尔：只认真 true / false。

    坏值（字符串 "false"、1/0、None、[]…）一律取**更安全**的一侧。旧实现
    `bool(raw)` 有 `bool("false") is True`：坏一条就把「冷场自动开话题」打开。
    """
    return raw if isinstance(raw, bool) else default


def _from_settings(settings: Any) -> dict:
    """从现有 settings 推一份默认（不动库）。

    settings 缺失（None = 老数据层 / UNAVAILABLE = 读不到配置）→ 老默认；真 settings
    的开关只认真 bool，坏值取更安全的一侧（关）。
    """
    if settings is None or settings is UNAVAILABLE:
        return dict(_LEGACY_DEFAULTS)
    topics = getattr(settings, "topics", None)
    delivery = getattr(settings, "delivery", None)
    try:
        daily_max = int(getattr(delivery, "push_per_day", DEFAULT_DAILY_MAX))
    except (TypeError, ValueError):
        daily_max = DEFAULT_DAILY_MAX
    quiet = str(getattr(delivery, "quiet_hours", "") or DEFAULT_QUIET_HOURS)
    return {
        "topics_enabled": _strict_bool(getattr(topics, "enabled", None), default=False),
        "news_card_enabled": False,
        "news_card_count": 3,
        "idea_mention_enabled": False,
        "daily_max": daily_max,
        "quiet_hours": quiet,
        "news_card_since": 0.0,
        "idea_mention_since": 0.0,
    }


def _normalize(raw: dict, settings: Any) -> dict:
    """把一份存库内容整成完整的配置（缺的补默认、坏的回落默认、范围夹住）。

    三个开关只认真 bool：坏值（"false" / 1 / None / []…）按**关**处理，
    绝不因为一个坏串就把自动群发打开。
    """
    base = _from_settings(settings)
    out = dict(base)
    if isinstance(raw, dict):
        for key in list(base):
            if key in raw:
                out[key] = raw[key]
    for key in BOOL_FIELDS:
        out[key] = _strict_bool(out[key], default=False)
    for key, (lo, hi) in INT_RANGES.items():
        try:
            value = int(out[key])
        except (TypeError, ValueError):
            value = int(base[key])
        out[key] = min(hi, max(lo, value))
    out["quiet_hours"] = _clean_quiet(out.get("quiet_hours"), base["quiet_hours"])
    for key in SINCE_FIELDS:
        try:
            out[key] = float(out.get(key) or 0.0)
        except (TypeError, ValueError):
            out[key] = 0.0
    return out


def _clean_quiet(value: Any, fallback: str) -> str:
    """睡觉时段：格式对就用，不对回落（配错按 settings 的默认 / 不限制）。"""
    text = str(value or "").strip()
    return text if _valid_quiet(text) else str(fallback or "")


def _valid_quiet(text: str) -> bool:
    m = _HHMM_RANGE.match(str(text or ""))
    if not m:
        return False
    sh, sm, eh, em = (int(x) for x in m.groups())
    return sh < 24 and eh < 24 and sm < 60 and em < 60


# ----------------------------------------------------------------------
# 写
# ----------------------------------------------------------------------


def set_config(store: Any, group_id: Any, patch: dict, settings: Any = None, *,
               now: Optional[float] = None) -> dict:
    """改一个群的设置；字段 / 范围不对抛 ValueError（中文），一个字段都不落库。

    返回改完的完整配置。settings=None（老调用口）时不查服务名单。
    退役字段在任何调用口都抛 ValueError（含显式 legacy），避免网页「假保存」。
    """
    gid = str(group_id)
    if not isinstance(patch, dict) or not patch:
        raise ValueError("要给要改的字段")
    if settings is UNAVAILABLE:
        raise ValueError("现在读不到配置，改不了推送设置，请稍后再试")
    if settings is not None:
        state = _served_state(settings, gid)
        if state is None:
            raise ValueError("现在认不出这个群在不在服务列表，改不了推送设置")
        if state is False:
            raise ValueError("这个群不在服务列表里，改不了推送设置")
    if settings is not None and settings is not UNAVAILABLE and not _seed_ready(settings):
        # 群控归一没迁成功：这份「局部改」要是从保守默认拼出来落库，会永久盖住修好后的
        # 迁移结果（且混进一堆未知默认）。已有合法记录（人明确在编辑的那份）照常放行；
        # 缺失 / 坏记录 → 明确拒绝并说清原因，让管理员等迁移成功再改。
        if _existing_config(store, gid) is None:
            raise ValueError(
                "群控设置还没从旧的全局设置搬过来（启动时那次迁移没成功），"
                "现在改不了这个群的「主动发言」；稍后再试或看启动日志"
            )
    moment = clock.now() if now is None else float(now)
    cur = dict(get_config(store, gid, settings))
    for key, value in patch.items():
        if key in RETIRED_FIELDS:
            # 退役字段**所有**调用口都拒（legacy settings=None 同样拒）：静默收下
            # 就是「假保存」，网页 / 老桥会以为改成功了。一个字段都不落库。
            raise ValueError(
                f"{key} 已经退役：每类每日上限并成了一个「每日总上限」，"
                "请到群页的「主动发言」里改"
            )
        if key not in SETTABLE:
            raise ValueError(f"不认识的字段：{key}")
        if key in BOOL_FIELDS:
            if not isinstance(value, bool):
                raise ValueError(f"{key} 要是 true / false")
            if value and not cur[key]:
                since = SINCE_OF_SWITCH.get(key)
                if since:
                    cur[since] = moment
            cur[key] = value
        elif key in INT_RANGES:
            lo, hi = INT_RANGES[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
                raise ValueError(f"{key} 要是整数")
            if not lo <= int(value) <= hi:
                raise ValueError(f"{key} 要在 {lo}~{hi} 之间")
            cur[key] = int(value)
        else:  # quiet_hours
            if not isinstance(value, str) or not _valid_quiet(value):
                raise ValueError("quiet_hours 要写成 23:00-08:00 这样")
            cur["quiet_hours"] = value
    cur = _normalize(cur, settings)
    with store.tx() as conn:
        store.kv_set(conn, f"{KV_PREFIX}{gid}", cur)
    return cur


def kind_enabled(cfg: dict, push_kind: Any) -> bool:
    """这条推送的开关开着吗；不归这三个开关管的 kind（交付 / 故障 / 指令…）永远 True。

    受管的三个开关只认真 true：配置里缺这一项 / 坏值一律按**关**（宁可少发，
    不许因为坏配置把自动群发打开）。
    """
    field = KIND_SWITCH.get(str(push_kind or ""))
    if field is None:
        return True
    return _strict_bool((cfg or {}).get(field), default=False)


def switch_field(push_kind: Any) -> str:
    """kind → 开关字段名；不受开关管的 kind 返回 ""（给网页 / 日志用）。"""
    return KIND_SWITCH.get(str(push_kind or ""), "")


# ----------------------------------------------------------------------
# 额度 / 视图
# ----------------------------------------------------------------------


def _day_bounds(now: float) -> tuple[float, float]:
    t = clock.bj(float(now))
    start = t.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return start, start + 86400.0


def _payload_of(raw: Any) -> dict:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def used_today(store: Any, group_id: Any, *, now: float, exempt_kinds: Iterable[str] = ()) -> int:
    """今天占掉的推送额度：已发出的受限推送 + 结果不明（sending / uncertain）的安全保留。

    - 不算豁免 kind（error / command / admin / awaited_delivery，由调用方传进来）。
    - 不算同一件交付自动补的说明（payload.follow_up_of）：它不占第二份额度。
    - 失败的发送不留痕（没发出去就不该占额度）。
    """
    gid = str(group_id)
    day = clock.day_key(float(now))
    exempt = tuple(sorted({str(k) for k in (exempt_kinds or ()) if str(k)}))
    sql = "SELECT COUNT(*) AS c FROM pushes WHERE group_id=? AND day=?"
    params: list[Any] = [gid, day]
    if exempt:
        sql += f" AND kind NOT IN ({', '.join('?' for _ in exempt)})"
        params.extend(exempt)
    row = store.read().execute(sql, params).fetchone()
    used = int(row["c"]) if row else 0
    rows = store.read().execute(
        "SELECT payload, updated FROM outbox WHERE group_id=? AND status IN ('sending', 'uncertain')",
        (gid,),
    ).fetchall()
    for r in rows:
        if clock.day_key(float(r["updated"] or 0.0)) != day:
            continue
        payload = _payload_of(r["payload"])
        if payload.get("follow_up_of"):
            continue
        if str(payload.get("push_kind") or "") in exempt:
            continue
        used += 1
    return used


def sent_today(store: Any, group_id: Any, *, now: float) -> int:
    """今天真的发出去几条（不限 kind，含故障 / 指令回执）。"""
    start, end = _day_bounds(now)
    row = store.read().execute(
        "SELECT COUNT(*) AS c FROM outbox WHERE group_id=? AND status='sent'"
        " AND updated>=? AND updated<?",
        (str(group_id), start, end),
    ).fetchone()
    return int(row["c"]) if row else 0


def recent(store: Any, group_id: Any, *, limit: int = 5) -> list[dict]:
    """最近几条发件记录（新在前）：结果不明的那条 state=不确定，不写成「已发」。"""
    rows = store.read().execute(
        "SELECT id, key, kind, payload, status, error, updated, result FROM outbox"
        " WHERE group_id=? ORDER BY updated DESC, id DESC LIMIT ?",
        (str(group_id), int(limit)),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        payload = _payload_of(r["payload"])
        status = str(r["status"] or "")
        text = str(payload.get("text") or payload.get("note") or payload.get("title")
                   or payload.get("name") or "")
        out.append({
            "id": int(r["id"]),
            "key": str(r["key"] or ""),
            "kind": str(r["kind"] or ""),
            "push_kind": str(payload.get("push_kind") or ""),
            "status": status,
            "state": _STATE_CN.get(status, status),
            "uncertain": status == "uncertain",
            "text": text[:200],
            "ts": float(r["updated"] or 0.0),
            "error": str(r["error"] or ""),
        })
    return out


def view(store: Any, group_id: Any, settings: Any = None, *, now: Optional[float] = None) -> dict:
    """网页「往群里发」区要的数据：设置 + 今天发了几条 / 占了多少额度 + 最近几条。

    非服务群：零读取，给一份空视图（前端照常能画）。
    """
    gid = str(group_id)
    moment = clock.now() if now is None else float(now)
    cfg = get_config(store, gid, settings)
    if settings is not None and not _served(settings, gid):
        return {"config": cfg, "daily_max": int(cfg["daily_max"]), "sent_today": 0,
                "quota_used": 0, "recent": []}
    from .delivery import PUSH_EXEMPT_KINDS  # 局部导入：豁免清单只有一份（delivery）

    return {
        "config": cfg,
        "daily_max": int(cfg["daily_max"]),
        "sent_today": sent_today(store, gid, now=moment),
        "quota_used": used_today(store, gid, now=moment, exempt_kinds=PUSH_EXEMPT_KINDS),
        "recent": recent(store, gid),
    }


# ----------------------------------------------------------------------
# 内部
# ----------------------------------------------------------------------


def _served_state(settings: Any, gid: str) -> Optional[bool]:
    """认这个群在不在服务名单：True 在 / False 明确不在 / None 认不出（没证据或判定抛异常）。"""
    fn: Callable[[str], bool] | None = getattr(settings, "is_served", None)
    if callable(fn):
        try:
            return bool(fn(gid))
        except Exception:
            logger.debug("判服务群失败（%s）", gid, exc_info=True)
            return None
    groups = getattr(settings, "groups", None)
    if groups is not None:
        try:
            return str(gid) in groups
        except Exception:
            logger.debug("按 groups 判服务群失败（%s）", gid, exc_info=True)
            return None
    return None


def served(settings: Any, group_id: Any) -> bool:
    """这个群明确在服务名单里吗（只认真实证据）。

    `Topics.check` / `Pushes.can_push` 的第一道闸用它：`settings` 读不到、没有
    `is_served` / `groups`、或判定抛异常，一律 False——非服务群零读取、零 Jev、
    零模型、零发送是红线，拿不到证据时**不能**当成服务群放行。
    """
    return _served_state(settings, str(group_id)) is True


def _served(settings: Any, gid: str) -> bool:
    """明确在服务名单里才算 True；认不出 / 判定失败一律 False（保守）。"""
    return _served_state(settings, gid) is True
