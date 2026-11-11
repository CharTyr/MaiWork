"""backport_routes.py：设置审计 + 常驻笔记的 HTTP 接口（注册进 ConsoleServer 的 app）。

由 server._build_app 里那段 try/ImportError 挂上（另一路接线）；单测里也可以直接
``register(server, server.app, svc)``——会自动把 /api 兜底 404 通配路由挪到我们
这些具体路由后面（aiohttp 按注册顺序匹配，通配路由先注册会遮蔽后注册的具体路由）。

- GET  /api/settings/changes            （总管理员）全局审计列表
- POST /api/settings/changes/{id}/revert（总管理员）一键撤销
- GET  /api/groups/{gid}/changes        （总管理员 / 本群管理员）本群审计列表
- POST /api/groups/{gid}/changes/{id}/revert（同上；群管理员不能碰 group_approval）
- GET  /api/groups/{gid}/agent-notes          本群常驻笔记列表
- PUT  /api/groups/{gid}/agent-notes/{kind}   写本岗常驻笔记（content / locked）
- GET  /api/groups/{gid}/agent-notes/{kind}/versions  版本列表
- POST /api/groups/{gid}/agent-notes/{kind}/rollback  回滚到某版
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from .. import settings_audit

logger = logging.getLogger("maiwork.console.backport_routes")


def _err(status: int, text: str) -> web.Response:
    return web.json_response({"error": text}, status=status)


def _catch_all_pop(app: web.Application) -> list[tuple[Any, Any]]:
    """把已注册的 /api 通配兜底路由从路由表里摘出来（存 handler，稍后重新加回末尾）。

    register 可能在 catch-all 之后才跑（测试里手动 register）：aiohttp 按注册顺序
    匹配，通配先注册会把后注册的具体路由全遮蔽——先摘下来，加完我们的路由再放回去。
    """
    removed: list[tuple[Any, Any]] = []
    resources = getattr(app.router, "_resources", None)  # noqa: SLF001 路由表重排只能靠它
    if not isinstance(resources, list):
        return removed
    for res in list(resources):
        try:
            routes = list(res)
        except TypeError:
            continue
        for rt in routes:
            canonical = str(getattr(res, "canonical", "") or "")
            if str(getattr(rt, "method", "") or "") == "*" and canonical.startswith("/api"):
                resources.remove(res)
                removed.append((canonical, getattr(rt, "handler", None)))
                break
    return removed


def _catch_all_readd(app: web.Application, removed: list[tuple[Any, Any]]) -> None:
    for _canonical, handler in removed:
        if handler is not None:
            app.router.add_route("*", "/api/{tail:.*}", handler)


def _agents_of(svc: Any) -> Any:
    """专岗数据层：svc.agents；没有就经 svc.specialists / 现建（测试最小 svc 用）。"""
    ag = getattr(svc, "agents", None)
    if ag is None:
        sp = getattr(svc, "specialists", None)
        ag = getattr(sp, "agents", None) if sp is not None else None
    if ag is None:
        try:
            from ..agents import Agents

            getter = getattr(svc, "get_settings", None)
            ag = Agents(svc.store, getter if callable(getter) else (lambda: None))
        except Exception:
            ag = None
    return ag


def _map_revert(exc: BaseException) -> web.Response:
    if isinstance(exc, KeyError):
        return _err(404, str(exc) or "未找到该修改记录")
    if isinstance(exc, PermissionError):
        return _err(403, str(exc) or "没有权限撤销这条修改")
    if isinstance(exc, settings_audit.RevertConflictError):
        return web.json_response(
            {"error": str(exc), "conflict": True, "current": exc.current, "expected": exc.expected},
            status=409,
        )
    if isinstance(exc, ValueError):
        return _err(400, str(exc) or "这条修改不能撤销")
    raise exc


def register(server: Any, app: web.Application, svc: Any) -> None:
    """注册审计 / 常驻笔记路由；server = ConsoleServer（用它的鉴权 / 同源守卫）。"""
    store = getattr(svc, "store", None)
    removed = _catch_all_pop(app)
    try:
        async def _json_body(request: web.Request) -> dict[str, Any] | None:
            try:
                body = await request.json()
            except Exception:
                return None
            return body if isinstance(body, dict) else None

        # ---------- 全局审计（总管理员） ----------

        async def list_changes(request: web.Request) -> web.Response:
            deny = server._require_admin(request)
            if deny is not None:
                return deny
            try:
                limit = int(request.query.get("limit") or 50)
            except (TypeError, ValueError):
                limit = 50
            try:
                before_id = int(request.query.get("before_id") or 0)
            except (TypeError, ValueError):
                before_id = 0
            rows, has_more = settings_audit.list_changes(
                store,
                actor=str(request.query.get("actor") or "").strip(),
                scope=str(request.query.get("scope") or "").strip(),
                limit=limit,
                before_id=before_id,
            )
            return web.json_response({"changes": rows, "has_more": has_more})

        async def revert_change(request: web.Request) -> web.Response:
            deny = server._require_admin(request)
            if deny is not None:
                return deny
            try:
                cid = int(request.match_info.get("id") or 0)
            except (TypeError, ValueError):
                cid = 0
            body = await _json_body(request) or {}
            try:
                out = settings_audit.revert_change(
                    store, svc, cid, actor="admin_web", force=bool(body.get("force")),
                )
            except (KeyError, PermissionError, ValueError, settings_audit.RevertConflictError) as e:
                return _map_revert(e)
            return web.json_response(out)

        # ---------- 本群审计（总管理员 / 本群管理员） ----------

        async def list_group_changes(request: web.Request) -> web.Response:
            gid = str(request.match_info.get("gid") or "")
            deny = server._require_group_admin(request, gid)
            if deny is not None:
                return deny
            try:
                limit = int(request.query.get("limit") or 30)
            except (TypeError, ValueError):
                limit = 30
            try:
                before_id = int(request.query.get("before_id") or 0)
            except (TypeError, ValueError):
                before_id = 0
            rows, has_more = settings_audit.list_changes(
                store, scope=f"group:{gid}", limit=limit, before_id=before_id,
            )
            return web.json_response({"changes": rows, "has_more": has_more})

        async def revert_group_change(request: web.Request) -> web.Response:
            gid = str(request.match_info.get("gid") or "")
            deny = server._require_group_admin(request, gid)
            if deny is not None:
                return deny
            ident = server._identify(request)
            try:
                cid = int(request.match_info.get("id") or 0)
            except (TypeError, ValueError):
                cid = 0
            body = await _json_body(request) or {}
            try:
                out = settings_audit.revert_change(
                    store, svc, cid,
                    actor=("group_admin" if ident.role == "group_admin" else "admin_web"),
                    actor_detail=f"group:{gid}",
                    allow_scope=f"group:{gid}",
                    is_group_admin_only=(ident.role == "group_admin"),
                    force=bool(body.get("force")),
                )
            except (KeyError, PermissionError, ValueError, settings_audit.RevertConflictError) as e:
                return _map_revert(e)
            return web.json_response(out)

        # ---------- 常驻笔记 ----------

        async def list_agent_notes(request: web.Request) -> web.Response:
            gid = str(request.match_info.get("gid") or "")
            deny = server._require_group_admin(request, gid)
            if deny is not None:
                return deny
            ag = _agents_of(svc)
            if ag is None:
                return _err(500, "专岗数据层没就位")
            try:
                notes = ag.list_index_notes(gid)
            except ValueError as e:
                return _err(404, str(e))
            return web.json_response({"notes": notes})

        async def put_agent_note(request: web.Request) -> web.Response:
            gid = str(request.match_info.get("gid") or "")
            deny = server._require_group_admin(request, gid)
            if deny is not None:
                return deny
            ident = server._identify(request)
            ag = _agents_of(svc)
            if ag is None:
                return _err(500, "专岗数据层没就位")
            kind = str(request.match_info.get("kind") or "")
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体要是 JSON 对象")
            content = body["content"] if "content" in body else None
            locked = body["locked"] if "locked" in body else None
            actor = "group_admin" if ident.role == "group_admin" else "admin_web"
            try:
                with settings_audit.audit_context(actor, actor_detail=f"group:{gid}"):
                    note = ag.set_index_note(
                        gid, kind,
                        content=content,
                        locked=locked,
                        source=actor,
                    )
            except ValueError as e:
                return _err(400, str(e))
            except Exception:
                logger.exception("写常驻笔记出错（群 %s 岗 %s）", gid, kind)
                return _err(500, "写常驻笔记失败")
            return web.json_response({"ok": True, "note": note})

        async def get_agent_note_versions(request: web.Request) -> web.Response:
            gid = str(request.match_info.get("gid") or "")
            deny = server._require_group_admin(request, gid)
            if deny is not None:
                return deny
            ag = _agents_of(svc)
            if ag is None:
                return _err(500, "专岗数据层没就位")
            kind = str(request.match_info.get("kind") or "")
            try:
                versions = ag.index_note_versions(gid, kind)
            except ValueError as e:
                return _err(404, str(e))
            return web.json_response({"versions": versions})

        async def rollback_agent_note(request: web.Request) -> web.Response:
            gid = str(request.match_info.get("gid") or "")
            deny = server._require_group_admin(request, gid)
            if deny is not None:
                return deny
            ident = server._identify(request)
            ag = _agents_of(svc)
            if ag is None:
                return _err(500, "专岗数据层没就位")
            kind = str(request.match_info.get("kind") or "")
            body = await _json_body(request) or {}
            if "version" not in body:
                return _err(400, "要给 version")
            actor = "group_admin" if ident.role == "group_admin" else "admin_web"
            try:
                with settings_audit.audit_context(actor, actor_detail=f"group:{gid}"):
                    note = ag.rollback_index_note(gid, kind, body.get("version"), source=actor)
            except KeyError as e:
                return _err(404, str(e) or "没有这个版本")
            except ValueError as e:
                return _err(400, str(e))
            return web.json_response({"ok": True, "note": note})

        # ---------- 注册 ----------
        app.router.add_get("/api/settings/changes", list_changes)
        app.router.add_post("/api/settings/changes/{id}/revert", server._write(revert_change))
        app.router.add_get("/api/groups/{gid}/changes", list_group_changes)
        app.router.add_post("/api/groups/{gid}/changes/{id}/revert", server._write(revert_group_change))
        app.router.add_get("/api/groups/{gid}/agent-notes", list_agent_notes)
        app.router.add_put("/api/groups/{gid}/agent-notes/{kind}", server._write(put_agent_note))
        app.router.add_get("/api/groups/{gid}/agent-notes/{kind}/versions", get_agent_note_versions)
        app.router.add_post("/api/groups/{gid}/agent-notes/{kind}/rollback", server._write(rollback_agent_note))
    finally:
        _catch_all_readd(app, removed)
