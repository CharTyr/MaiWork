"""Per-group module switches and shutdown history. No host/model calls."""
from __future__ import annotations

import json
import logging
import math
from typing import Any, Optional
from . import clock

logger = logging.getLogger("maiwork.group_features")
KV_PREFIX = "group_features."
MODULES = ("news", "idea", "task")
_MISSING = object()


def _is_served(settings: Any, gid: str) -> bool:
    if settings is None:
        return True
    try:
        return bool(settings.is_served(str(gid)))
    except Exception:
        return False


def _normalize(raw: Any) -> dict:
    # Missing installation config preserves the historical all-on default.
    # Existing invalid values never silently enable work.
    missing = raw is _MISSING
    data = raw if isinstance(raw, dict) else {}
    out = {m: data.get(m, missing) if isinstance(data.get(m, missing), bool) else False for m in MODULES}
    for field in ("disabled_ts", "last_disabled_ts", "enabled_since"):
        stamps = data.get(field, {})
        out[field] = {}
        if isinstance(stamps, dict):
            for m in MODULES:
                try:
                    ts = float(stamps.get(m))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(ts) and ts > 0:
                    out[field][m] = ts
    for m, ts in out["disabled_ts"].items():
        out["last_disabled_ts"][m] = max(ts, out["last_disabled_ts"].get(m, 0))
    out["updated_ts"] = data.get("updated_ts", 0.0)
    out["updated_by"] = str(data.get("updated_by", ""))
    return out


def _read_config(conn: Any, gid: str) -> dict:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (f"{KV_PREFIX}{gid}",)).fetchone()
    if row is None:
        return _normalize(_MISSING)
    try:
        return _normalize(json.loads(row["value"]))
    except (TypeError, ValueError):
        return _normalize(None)


def get(store: Any, group_id: Any, settings: Any = None) -> dict:
    gid = str(group_id)
    if not _is_served(settings, gid):
        return _normalize(_MISSING)  # zero database access for unserved groups
    try:
        return _read_config(store.read(), gid)
    except Exception:
        logger.warning("读功能开关失败（群 %s），暂停新增工作", gid, exc_info=True)
        return _normalize(None)


def enabled(store: Any, group_id: Any, module: Any, settings: Any = None) -> bool:
    m = str(module or "")
    return m not in MODULES or bool(get(store, group_id, settings)[m])


def disabled_since(store: Any, group_id: Any, module: Any, settings: Any = None) -> Optional[float]:
    return get(store, group_id, settings)["disabled_ts"].get(str(module))


def set_config(store: Any, group_id: Any, patch: dict, settings: Any = None, *, by: str = "", now: Optional[float] = None) -> dict:
    gid = str(group_id)
    if not isinstance(patch, dict) or not patch:
        raise ValueError("要给要改的字段")
    for key, value in patch.items():
        if key not in MODULES:
            raise ValueError(f"不认识的功能：{key}")
        if not isinstance(value, bool):
            raise ValueError(f"{key} 要是 true / false")
    if not _is_served(settings, gid):
        raise ValueError("这个群不在服务列表里，改不了功能开关")
    moment = clock.now() if now is None else float(now)
    with store.tx() as conn:
        latest = _read_config(conn, gid)
        changes = [(m, value) for m, value in patch.items() if latest[m] != value]
        for m, value in changes:
            latest[m] = value
            if value:
                latest["disabled_ts"].pop(m, None)
                latest["enabled_since"][m] = moment
            else:
                latest["disabled_ts"][m] = moment
                latest["last_disabled_ts"][m] = moment
        # Same transaction as the switch: even an off/on between scheduler rounds
        # leaves all queued work held. Running/reviewing tasks remain untouched.
        if patch.get("task") is False:
            rows = conn.execute("SELECT id FROM tasks WHERE group_id=? AND status='queued' AND COALESCE(paused_reason,'') != ?", (gid, json.dumps({"kind": "feature_off"}))).fetchall()
            for row in rows:
                conn.execute("UPDATE tasks SET paused_reason=?, updated=? WHERE id=? AND status='queued'", (json.dumps({"kind": "feature_off"}), moment, row["id"]))
                store.event(conn, "task.held", group_id=gid, entity="task", entity_id=str(row["id"]), payload={"reason": "feature_off", "by": str(by)})
        if changes:
            latest["updated_ts"], latest["updated_by"] = moment, str(by or "")
            store.kv_set(conn, f"{KV_PREFIX}{gid}", latest)
            for m, value in changes:
                store.event(conn, "features.toggled", group_id=gid, entity="group", entity_id=gid, payload={"group_id": gid, "module": m, "enabled": value, "by": str(by or "")})
    return latest
