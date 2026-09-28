"""首次安装引导的状态（网页 /api/onboarding）。

只记一件事：管理员走完或跳过了引导没有（kv["onboarding"]）。
要不要弹引导 = 没走过也没跳过，且模型还没配好——已经在用的老安装不打扰。
checks 给引导页显示「哪几步已经配过」，全部从现有设置里读，不另存。
"""

from __future__ import annotations

from typing import Any

from . import clock

KV_KEY = "onboarding"
ACTIONS = ("done", "skip", "reset")


def _checks(svc: Any) -> dict[str, bool]:
    settings = svc.get_settings()
    out = {"models": False, "groups": False, "jev": False, "search": False, "admins": False}
    try:
        out["models"] = bool(svc.models.settings().ready())
    except Exception:
        out["models"] = False
    if settings is not None:
        out["groups"] = bool(getattr(settings, "groups", None))
        out["admins"] = bool(tuple(getattr(settings.approval, "admins", ()) or ()))
    # 搜索（2026-10）：有可用绑定就算配好（绑定存数据库 kv["extensions.search"]）
    try:
        search = getattr(svc, "search", None)
        out["search"] = bool(search is not None and search.available())
    except Exception:
        out["search"] = False
    try:
        from .jev import _resolve_key

        out["jev"] = bool(_resolve_key(settings))
    except Exception:
        out["jev"] = False
    return out


def view(svc: Any) -> dict[str, Any]:
    rec = svc.store.kv_get(KV_KEY, None) or {}
    state = str(rec.get("state") or "")
    checks = _checks(svc)
    return {
        "show": state == "" and not checks["models"],
        "state": state,
        "ts": float(rec.get("ts") or 0),
        "checks": checks,
    }


def act(svc: Any, action: str) -> dict[str, Any]:
    if action not in ACTIONS:
        raise ValueError("action 只能是 done / skip / reset")
    value: Any = {"state": "done" if action == "done" else "skipped", "ts": clock.now()}
    if action == "reset":
        value = {}
    with svc.store.tx() as conn:
        svc.store.kv_set(conn, KV_KEY, value)
    return view(svc)
