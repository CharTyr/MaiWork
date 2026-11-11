"""settings_audit.py：设置修改审计 + 一键撤销。

- 审计表 settings_changes：谁在什么时候改了哪个设置、改了前/后各是什么；
  scope="global" 全局设置，scope="group:<群号>" 每群设置。
- 发起者用 ContextVar 传（audit_context）：网页路由、管理员对话工具、群管理员路由、
  auto_sources 不用改函数签名就能标注谁改的。
- 密钥只进不出（红线）：api_key / secret / password / token / cookie / headers / auth /
  credential 这类键的值一律落 "[REDACTED]"，且该记录不可撤销（revertible=0）——
  旧凭据绝不能被「撤销」复活，也绝不落库。
- revert_change：查记录 → 权限/可撤销校验 → 冲突检测（这条之后又有人改过 → 409）→
  回写 before → 标记 reverted。回写本身也在审计里留一条 actor="revert" 的新记录。
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from . import clock

logger = logging.getLogger("maiwork.settings_audit")

# ----------------------------------------------------------------------
# 表结构
# ----------------------------------------------------------------------

_DDL = (
    """CREATE TABLE IF NOT EXISTS settings_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor TEXT NOT NULL DEFAULT 'admin_web',
    actor_detail TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT 'global',
    target_key TEXT NOT NULL,
    summary TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    revertible INTEGER NOT NULL DEFAULT 1,
    reverted_at REAL,
    reverted_by_id INTEGER
)""",
    "CREATE INDEX IF NOT EXISTS idx_settings_changes_ts ON settings_changes(ts DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_settings_changes_scope ON settings_changes(scope, ts DESC, id DESC)",
)


def ensure_schema(conn_or_store: Any) -> bool:
    """建审计表（幂等）。给 Store 走 tx()；给裸连接直接执行（调用方管事务）。
    认不出的对象 → False（纯单测没有库时调用方照样能跑）。"""
    obj = conn_or_store
    if obj is None:
        return False
    tx = getattr(obj, "tx", None)
    if callable(tx):
        try:
            with tx() as conn:
                for stmt in _DDL:
                    conn.execute(stmt)
            return True
        except Exception:
            logger.debug("建 settings_changes 表失败", exc_info=True)
            return False
    execute = getattr(obj, "execute", None)
    if callable(execute):
        try:
            for stmt in _DDL:
                execute(stmt)
            return True
        except Exception:
            logger.debug("建 settings_changes 表失败", exc_info=True)
            return False
    return False


# ----------------------------------------------------------------------
# 发起者上下文（改函数签名免了：谁触发的修改靠 ContextVar 标）
# ----------------------------------------------------------------------

_actor_var: ContextVar[str] = ContextVar("maiwork_audit_actor", default="admin_web")
_actor_detail_var: ContextVar[str] = ContextVar("maiwork_audit_actor_detail", default="")
_reason_var: ContextVar[str] = ContextVar("maiwork_audit_reason", default="")

ACTORS = ("admin_web", "group_admin", "admin_chat", "auto_source", "revert")


@contextmanager
def audit_context(actor: str, actor_detail: str = "", reason: str = "") -> Iterator[None]:
    """在这段代码里发生的设置修改记成 ``actor`` 发起的。

    actor 取值："admin_web"（默认）、"group_admin"、"admin_chat"、"auto_source"、"revert"。
    """
    a = str(actor or "admin_web")
    if a not in ACTORS:
        a = "admin_web"
    t1 = _actor_var.set(a)
    t2 = _actor_detail_var.set(str(actor_detail or "")[:120])
    t3 = _reason_var.set(str(reason or "")[:300])
    try:
        yield
    finally:
        _reason_var.reset(t3)
        _actor_detail_var.reset(t2)
        _actor_var.reset(t1)


def current_actor() -> tuple[str, str, str]:
    """当前审计上下文（actor, actor_detail, reason）；没进 audit_context 就是默认 admin_web。"""
    return _actor_var.get(), _actor_detail_var.get(), _reason_var.get()


# ----------------------------------------------------------------------
# 密钥只进不出
# ----------------------------------------------------------------------

REDACTED = "[REDACTED]"

_SECRET_PARTS = (
    "api_key", "apikey", "secret", "password", "passwd", "token", "cookie",
    "header", "auth", "credential", "private_key", "ssh_key", "passphrase",
)


def is_secret_key(name: Any) -> bool:
    """字段名像不像密钥（大小写 / 横线不敏感；api_key / headers / auth …都拦）。"""
    low = str(name or "").strip().lower().replace("-", "_")
    if not low:
        return False
    return any(p in low for p in _SECRET_PARTS)


def _contains_secret(value: Any) -> bool:
    """这份值里有没有密钥字段（递归看 dict 键名）。"""
    if isinstance(value, dict):
        for k, v in value.items():
            if is_secret_key(k):
                return True
            if _contains_secret(v):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(v) for v in value)
    return False


def _redact(value: Any, *, key_hint: Any = "") -> Any:
    """递归脱敏：密钥字段的值一律换 "[REDACTED]"。dict 键名本身照留（名字不敏感）。"""
    if is_secret_key(key_hint):
        return REDACTED
    if isinstance(value, dict):
        return {str(k): _redact(v, key_hint=k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


def _dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value), ensure_ascii=False)


# ----------------------------------------------------------------------
# 记录
# ----------------------------------------------------------------------


def _store_of(store: Any) -> Any:
    return store if store is not None and (hasattr(store, "tx") or hasattr(store, "execute")) else None


def record(
    store: Any,
    *,
    scope: str = "global",
    target_key: str,
    summary: str,
    before: Any = None,
    after: Any = None,
    actor: str | None = None,
    actor_detail: str | None = None,
    reason: str | None = None,
    revertible: bool = True,
) -> int | None:
    """写一条设置修改记录；返回行 id。store 没有 / 建表失败 → None（不挡业务）。

    - before/after 过密钥脱敏（dict 键名命中密钥模式 → "[REDACTED]"）；
    - 载荷含密钥字段（或 target_key 本身像密钥）→ 强制 revertible=0；
    - 规范化后 before == after 且不是密钥轮换 → 跳过（不写无效噪音）。
    """
    st = _store_of(store)
    if st is None:
        return None
    key = str(target_key or "").strip()
    if not key:
        return None
    before_r = _redact({} if before is None else before)
    after_r = _redact({} if after is None else after)
    if key.startswith("rss_source:"):  # RSS 条目只比对外形状，内部计数不比
        before_r = _rss_entry_norm(before_r)
        after_r = _rss_entry_norm(after_r)
    secret_payload = (
        _contains_secret(before) or _contains_secret(after) or is_secret_key(key)
    )
    rev = bool(revertible) and not secret_payload
    before_json = _dumps(before_r)
    after_json = _dumps(after_r)
    if rev and before_json == after_json:
        return None  # 无效写入不记账
    a, d, r = current_actor()
    actor_s = str(actor if actor is not None else a)
    if actor_s not in ACTORS:
        actor_s = "admin_web"
    detail_s = str(actor_detail if actor_detail is not None else d)[:120]
    reason_s = str(reason if reason is not None else r)[:300]
    try:
        if not ensure_schema(st):
            return None
        with st.tx() as conn:
            cur = conn.execute(
                "INSERT INTO settings_changes (ts, actor, actor_detail, scope, target_key,"
                " summary, reason, before_json, after_json, revertible)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    float(clock.now()), actor_s, detail_s, str(scope or "global"),
                    key, str(summary or "")[:200], reason_s,
                    before_json, after_json, 1 if rev else 0,
                ),
            )
            return int(cur.lastrowid or 0) or None
    except Exception:
        logger.debug("设置修改记账失败（%s）", key, exc_info=True)
        return None


def _parse_json(raw: Any) -> Any:
    try:
        return json.loads(str(raw or ""))
    except (TypeError, ValueError):
        return None


def row_to_dict(row: Any) -> dict[str, Any]:
    """settings_changes 一行 → 对外 dict（before/after 已解析成对象）。"""
    return {
        "id": int(row["id"]),
        "ts": float(row["ts"] or 0.0),
        "actor": str(row["actor"] or ""),
        "actor_detail": str(row["actor_detail"] or ""),
        "scope": str(row["scope"] or ""),
        "target_key": str(row["target_key"] or ""),
        "summary": str(row["summary"] or ""),
        "reason": str(row["reason"] or ""),
        "before_json": _parse_json(row["before_json"]),
        "after_json": _parse_json(row["after_json"]),
        "revertible": bool(int(row["revertible"] or 0)),
        "reverted_at": (float(row["reverted_at"]) if row["reverted_at"] is not None else None),
        "reverted_by_id": (int(row["reverted_by_id"]) if row["reverted_by_id"] is not None else None),
    }


def get_change(store: Any, change_id: Any) -> dict[str, Any] | None:
    st = _store_of(store)
    if st is None or not ensure_schema(st):
        return None
    try:
        row = st.read().execute(
            "SELECT * FROM settings_changes WHERE id=?", (int(change_id),)
        ).fetchone()
    except Exception:
        return None
    return row_to_dict(row) if row is not None else None


def list_changes(
    store: Any,
    *,
    actor: str = "",
    scope: str = "",
    limit: int = 50,
    before_id: int = 0,
) -> tuple[list[dict[str, Any]], bool]:
    """最新在前；before_id>0 时只看比它旧的（分页）。返回 (rows, has_more)。"""
    st = _store_of(store)
    if st is None or not ensure_schema(st):
        return [], False
    try:
        n = max(1, min(int(limit or 50), 200))
    except (TypeError, ValueError):
        n = 50
    where: list[str] = []
    params: list[Any] = []
    if str(actor or "").strip():
        where.append("actor=?")
        params.append(str(actor).strip())
    if str(scope or "").strip():
        where.append("scope=?")
        params.append(str(scope).strip())
    try:
        bid = int(before_id or 0)
    except (TypeError, ValueError):
        bid = 0
    if bid > 0:
        where.append("id<?")
        params.append(bid)
    sql = "SELECT * FROM settings_changes"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    params.append(n + 1)
    rows = st.read().execute(sql, tuple(params)).fetchall()
    has_more = len(rows) > n
    return [row_to_dict(r) for r in rows[:n]], has_more


# ----------------------------------------------------------------------
# 撤销
# ----------------------------------------------------------------------


class RevertConflictError(Exception):
    """这条记录之后该设置又被改过：撤销会盖掉新值，必须 force 才做。"""

    def __init__(self, message: str, *, current: Any = None, expected: Any = None) -> None:
        super().__init__(message)
        self.current = current
        self.expected = expected


def _scope_gid(scope: str) -> str:
    s = str(scope or "")
    return s[len("group:"):] if s.startswith("group:") else ""


def _agents_of(store: Any, svc: Any) -> Any:
    ag = getattr(svc, "agents", None)
    if ag is None:
        sp = getattr(svc, "specialists", None)
        ag = getattr(sp, "agents", None) if sp is not None else None
    if ag is None:
        try:
            from .agents import Agents

            getter = getattr(svc, "get_settings", None)
            ag = Agents(store, getter if callable(getter) else (lambda: None))
        except Exception:
            ag = None
    return ag


def _settings_of(svc: Any) -> Any:
    getter = getattr(svc, "get_settings", None)
    try:
        return getter() if callable(getter) else None
    except Exception:
        return None


def _project(live: Any, shape: Any) -> Any:
    """把活值剪成记录时的形状（patch 形记录只看改过的那些键）。"""
    if isinstance(shape, dict) and isinstance(live, dict):
        return {k: live.get(k) for k in shape.keys()}
    return live


def _rss_entry_norm(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict) or not (str(entry.get("id") or "") or str(entry.get("url") or "")):
        return {}
    return {
        "id": str(entry.get("id") or ""),
        "url": str(entry.get("url") or ""),
        "title": str(entry.get("title") or ""),
        "enabled": bool(entry.get("enabled")),
        "auto": bool(entry.get("auto")),
        "label": str(entry.get("label") or ""),
        "origin": str(entry.get("origin") or ""),
        "reason": str(entry.get("reason") or ""),
        "trial_until": float(entry.get("trial_until") or 0.0),
    }


def _rss_find(store: Any, gid: str, key: str) -> dict[str, Any] | None:
    from . import rss

    for e in rss.list_feeds(store, gid):
        if str(e.get("url") or "") == key or str(e.get("id") or "") == key:
            return e
    return None


def _mcp_entry(store: Any, name: str) -> dict[str, Any]:
    from . import extensions_web

    for e in extensions_web._web_raw_entries(store):  # noqa: SLF001 同包读数据层
        if str(e.get("name") or "") == name:
            return dict(e)
    return {}


def _live_value(store: Any, svc: Any, scope: str, target_key: str, shape: Any) -> Any:
    """读 (scope, target_key) 现在的值，剪成和记录同形状；读不出 → shape 的空对应。"""
    gid = _scope_gid(scope)
    if target_key == "group_push":
        from . import group_push

        return _project(group_push.get_config(store, gid, _settings_of(svc)), shape)
    if target_key == "group_features":
        from . import group_features

        return _project(group_features.get(store, gid, _settings_of(svc)), shape)
    if target_key == "group_approval":
        from .group_approval import GroupApprovals

        ga = getattr(svc, "group_approvals", None)
        if ga is None:
            getter = getattr(svc, "get_settings", None)
            ga = GroupApprovals(store, get_settings=getter if callable(getter) else None)
        return _project(ga.get(gid).as_dict(), shape)
    if target_key == "group_rules":
        ag = _agents_of(store, svc)
        if ag is None:
            return _project({}, shape)
        return _project({"body": str(ag.group_rules_get(gid).get("body") or "")}, shape)
    if target_key == "search_binding":
        from . import search_binding

        return search_binding.get_binding(store) or {}
    if target_key.startswith("rss_source:"):
        key = target_key.split(":", 1)[1]
        return _rss_entry_norm(_rss_find(store, gid, key))
    if target_key.startswith("agent_index_note:"):
        kind = target_key.split(":", 1)[1]
        ag = _agents_of(store, svc)
        if ag is None:
            return _project({}, shape)
        note = ag.get_index_note(gid, kind)
        return _project({"content": note.get("content"), "locked": note.get("locked")}, shape)
    if target_key.startswith("agent_profile:"):
        kind = target_key.split(":", 1)[1]
        ag = _agents_of(store, svc)
        if ag is None:
            return _project({}, shape)
        try:
            return _project(ag.profile(kind), shape)
        except Exception:
            return _project({}, shape)
    if target_key.startswith("mcp:"):
        return _project(_mcp_entry(store, target_key.split(":", 1)[1]), shape)
    if target_key.startswith("config:"):
        from . import rules

        field = target_key.split(":", 1)[1]
        base = _settings_of(svc)
        section, _, fname = field.partition(".")
        live = rules._flat_base_value(base, section, fname) if base is not None else None  # noqa: SLF001
        return _project({field: live}, shape)
    if target_key.startswith("identity:"):
        ident = getattr(svc, "identity", None)
        kind = target_key.split(":", 1)[1]
        if ident is None:
            return _project({}, shape)
        return _project({"text": str(ident.read(kind).get("text") or "")}, shape)
    if target_key.startswith("agent_doc:"):
        ident = getattr(svc, "identity", None)
        _, _, rest = target_key.partition(":")
        kind, _, which = rest.partition(":")
        if ident is None:
            return _project({}, shape)
        return _project({"text": str(ident.agent_read(kind, which).get("text") or "")}, shape)
    # endpoint:* / model:* / agent_custom:* / memory:*：无读取器
    return None


def _apply_value(store: Any, svc: Any, scope: str, target_key: str, before: Any) -> bool:
    """把 before 回写进对应的领域写口。返回有没有真正写了。不支持的 → ValueError。"""
    gid = _scope_gid(scope)
    if target_key == "group_push":
        from . import group_push

        if not isinstance(before, dict) or not before:
            raise ValueError("这条记录没有可回写的旧值")
        group_push.set_config(store, gid, dict(before), _settings_of(svc))
        return True
    if target_key == "group_features":
        from . import group_features

        if not isinstance(before, dict) or not before:
            raise ValueError("这条记录没有可回写的旧值")
        group_features.set_config(store, gid, dict(before), _settings_of(svc), by="revert")
        return True
    if target_key == "group_approval":
        from .group_approval import GroupApprovals

        if not isinstance(before, dict) or not before:
            raise ValueError("这条记录没有可回写的旧值")
        ga = getattr(svc, "group_approvals", None)
        if ga is None:
            getter = getattr(svc, "get_settings", None)
            ga = GroupApprovals(store, get_settings=getter if callable(getter) else None)
        ga.set(gid, dict(before))
        return True
    if target_key == "group_rules":
        ag = _agents_of(store, svc)
        if ag is None or not isinstance(before, dict) or "body" not in before:
            raise ValueError("这条记录没有对应的撤销方式")
        ag.group_rules_set(gid, str(before.get("body") or ""), updated_by="revert")
        return True
    if target_key == "search_binding":
        from . import search_binding

        if isinstance(before, dict) and before.get("mcp") and before.get("tool"):
            search_binding.set_binding(store, before)
        else:
            search_binding.clear_binding(store)
        return True
    if target_key.startswith("rss_source:"):
        from . import rss

        key = target_key.split(":", 1)[1]
        live = _rss_find(store, gid, key)
        if not isinstance(before, dict) or not before:
            # 旧值是「没有这个源」→ 撤销 = 把现在这条删掉
            if live is not None:
                rss.remove_feed(store, gid, str(live.get("id") or ""))
            return True
        url = str(before.get("url") or key)
        if live is not None:
            rss.remove_feed(store, gid, str(live.get("id") or ""))
        rss.add_feed(
            store, gid, url=url, title=str(before.get("title") or ""),
            feed_id=str(before.get("id") or ""),
            auto=bool(before.get("auto")), origin=str(before.get("origin") or ""),
            reason=str(before.get("reason") or ""), label=str(before.get("label") or ""),
            trial_until=float(before.get("trial_until") or 0.0),
        )
        return True
    if target_key.startswith("agent_index_note:"):
        kind = target_key.split(":", 1)[1]
        ag = _agents_of(store, svc)
        if ag is None or not isinstance(before, dict):
            raise ValueError("这条记录没有对应的撤销方式")
        ag.set_index_note(
            gid, kind,
            content=str(before.get("content") or ""),
            locked=bool(before.get("locked")) if "locked" in before else None,
            source="revert",
        )
        return True
    if target_key.startswith("agent_profile:"):
        kind = target_key.split(":", 1)[1]
        ag = _agents_of(store, svc)
        if ag is None or not isinstance(before, dict) or not before:
            raise ValueError("这条记录没有可回写的旧值")
        ag.update_profile(kind, dict(before))
        return True
    if target_key.startswith("mcp:"):
        from . import extensions_web

        name = target_key.split(":", 1)[1]
        live = _mcp_entry(store, name)
        if not isinstance(before, dict) or not before:
            if live:
                extensions_web.delete(store, name)
            return True
        body = {k: v for k, v in before.items() if k != "name"}
        if live:
            if "enabled" in before and "url" not in before and "tools" not in before:
                extensions_web.update(store, name, {"enabled": bool(before["enabled"])})
            else:
                extensions_web.update(store, name, body)
        else:
            if "url" not in before:
                raise ValueError("这条记录没有对应的撤销方式")
            extensions_web.create(store, _settings_of(svc), {**body, "name": name})
        return True
    if target_key.startswith("config:"):
        from . import rules

        field = target_key.split(":", 1)[1]
        if not isinstance(before, dict) or field not in before:
            raise ValueError("这条记录没有可回写的旧值")
        base = _settings_of(svc)
        if base is None:
            raise ValueError("读不到当前配置，不能撤销")
        rules.save_config_patch(store, {field: before[field]}, base=base)
        return True
    if target_key.startswith("identity:"):
        ident = getattr(svc, "identity", None)
        kind = target_key.split(":", 1)[1]
        if ident is None or not isinstance(before, dict):
            raise ValueError("这条记录没有对应的撤销方式")
        ident.write(kind, str(before.get("text") or ""))
        return True
    if target_key.startswith("agent_doc:"):
        ident = getattr(svc, "identity", None)
        _, _, rest = target_key.partition(":")
        kind, _, which = rest.partition(":")
        if ident is None or not isinstance(before, dict):
            raise ValueError("这条记录没有对应的撤销方式")
        ident.agent_write(kind, which, str(before.get("text") or ""))
        return True
    raise ValueError("这条记录没有对应的撤销方式")


_LOCKOUT_KEYS = ("config:models.main", "config:models.worker", "config:groups.serve",
                 "model_chain", "served_groups")


def _would_lockout(target_key: str, before: Any, after: Any) -> bool:
    """撤销会把主模型链 / 服务群清空 → 拦（管理员网页 / 主模型会没人干活）。"""
    if str(target_key or "") not in _LOCKOUT_KEYS:
        return False

    def _empty(v: Any) -> bool:
        if isinstance(v, dict):
            return all(_empty(x) for x in v.values())
        if isinstance(v, (list, tuple)):
            return len(v) == 0
        return v in (None, "", 0, False)

    return _empty(before) and not _empty(after)


def revert_change(
    store: Any,
    svc: Any,
    change_id: int,
    *,
    actor: str,
    actor_detail: str = "",
    allow_scope: str | None = None,
    is_group_admin_only: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """一键撤销设置修改。

    - KeyError → 404（未找到该修改记录）；PermissionError → 403；
      ValueError → 400（不可撤销 / 已撤销 / 撤销后会锁死 / 没有撤销方式）；
      RevertConflictError → 409（这条之后又有人改过，force=True 才放行）。
    """
    st = _store_of(store)
    if st is None or not ensure_schema(st):
        raise KeyError("未找到该修改记录")
    row = st.read().execute(
        "SELECT * FROM settings_changes WHERE id=?", (int(change_id),)
    ).fetchone()
    if row is None:
        raise KeyError("未找到该修改记录")
    rec = row_to_dict(row)

    if allow_scope is not None and rec["scope"] != str(allow_scope):
        raise PermissionError("只能撤销本群的设置修改")
    if is_group_admin_only and rec["target_key"] == "group_approval":
        raise PermissionError("派活审批策略仅总管理员可修改或撤销")
    if not rec["revertible"]:
        raise ValueError("该项涉及密钥或一次性凭据，不支持撤销")
    if rec["reverted_at"] is not None:
        raise ValueError("该修改已撤销")

    before = rec["before_json"]
    after = rec["after_json"]
    if _would_lockout(rec["target_key"], before, after):
        raise ValueError("撤销后主模型将为空，已拦截")

    live = _live_value(st, svc, rec["scope"], rec["target_key"], after)
    if live is not None and after is not None and _dumps(live) != _dumps(after) and not force:
        raise RevertConflictError(
            "该设置在此记录之后又被修改过，确认仍要恢复到当时的值吗？",
            current=live, expected=after,
        )

    with audit_context("revert", actor_detail=actor_detail or str(actor or ""),
                       reason=f"撤销修改 #{int(change_id)}"):
        _apply_value(st, svc, rec["scope"], rec["target_key"], before)

    # 回写走领域写口时一般自己已经记了一条 actor=revert；没记的手工补一条。
    new_row = st.read().execute(
        "SELECT * FROM settings_changes ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if new_row is not None and str(new_row["actor"] or "") == "revert" \
            and str(new_row["target_key"] or "") == rec["target_key"] \
            and str(new_row["scope"] or "") == rec["scope"]:
        new_id = int(new_row["id"])
    else:
        new_id = record(
            st, scope=rec["scope"], target_key=rec["target_key"],
            summary=f"撤销修改 #{int(change_id)}", before=after, after=before,
            actor="revert", actor_detail=actor_detail or str(actor or ""),
            reason=f"撤销修改 #{int(change_id)}",
        ) or 0
    now = float(clock.now())
    with st.tx() as conn:
        conn.execute(
            "UPDATE settings_changes SET reverted_at=?, reverted_by_id=? WHERE id=?",
            (now, int(new_id or 0), int(change_id)),
        )
    out = get_change(st, new_id) if new_id else None
    return {"ok": True, "reverted_id": int(change_id), "change": out}
