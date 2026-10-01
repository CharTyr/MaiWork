"""console 的 HTTP 层：aiohttp 路由、鉴权、静态文件、ConsoleServer。

路由和返回结构严格按 docs/07-代码接口.md §9.1/§9.2/§9.3（M1 部分），
M2/M3 的路由先注册、统一 501。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

from .. import clock
from . import usage_history
from . import views
from ..group_admins import GroupAdmins
from .auth import COOKIE_NAME, ConsoleAuth, same_origin

logger = logging.getLogger("maiwork.console.server")

_STATIC_DIR = Path(__file__).resolve().parent / "static"

# 首页里的 js/main.js / style.css 和 import map 里的每个前端模块都带内容版本号（?v=前 12 位 sha256）：部署新版后浏览器必定拿新文件，
# 不会用启发式缓存里的旧版。按 (mtime, size) 缓存，文件没变不重算。
_ASSET_VER_CACHE: dict[str, tuple[tuple[float, int], str]] = {}


def _asset_version(name: str) -> str:
    import hashlib

    path = _STATIC_DIR / name
    try:
        st = path.stat()
    except OSError:
        return ""
    key = (st.st_mtime, st.st_size)
    hit = _ASSET_VER_CACHE.get(name)
    if hit and hit[0] == key:
        return hit[1]
    ver = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    _ASSET_VER_CACHE[name] = (key, ver)
    return ver


def _js_modules() -> list[str]:
    """static/js 下的所有前端模块（相对 static/ 的路径，稳定排序；跳过 macOS 的 ._ 文件）。"""
    js_dir = _STATIC_DIR / "js"
    if not js_dir.is_dir():
        return []
    return sorted(
        p.relative_to(_STATIC_DIR).as_posix() for p in js_dir.rglob("*.js") if not p.name.startswith("._")
    )


def _index_html() -> str:
    """首页：入口脚本和样式带版本号；入口 import 的其余模块经 import map 也带上版本号。
    这样前端不用编译、拆成多个文件，部署后浏览器也不会新旧模块混用。"""
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for name in ("js/main.js", "style.css"):
        ver = _asset_version(name)
        if ver:
            html = html.replace(f'"/static/{name}"', f'"/static/{name}?v={ver}"')
    imports = {}
    for name in _js_modules():
        ver = _asset_version(name)
        if ver:
            imports[f"/static/{name}"] = f"/static/{name}?v={ver}"
    at = html.find('<script type="module"')
    if imports and at != -1:
        tag = '<script type="importmap">' + json.dumps({"imports": imports}, indent=1) + "</script>\n  "
        html = html[:at] + tag + html[at:]
    return html


_TOKEN_CHARS = frozenset("ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789")

Handler = Callable[[web.Request], Awaitable[web.Response]]
AUTH_KEY = web.AppKey("auth", object)  # ConsoleAuth；放 app[AUTH_KEY]，避免魔术字符串


@dataclass
class Identity:
    role: str  # "admin" | "group_admin" | "member" | "none"
    group_id: str | None = None


# 群管理员碰个人画像 / 个人向资讯时的统一文案（只读）
_PERSONAL_READONLY = "群管理员只能看，不能改个人画像"


def _err(status: int, text: str) -> web.Response:
    return web.json_response({"error": text}, status=status)


def _looks_like_token(ref: str) -> bool:
    return bool(ref) and not ref.isdigit() and all(c in _TOKEN_CHARS for c in ref)


class ConsoleServer:
    """MaiWork 的网页服务。端口被占用 → 记错误日志、不抛（插件照常运行）。"""

    def __init__(self, svc: Any) -> None:
        self._svc = svc
        self._bg_tasks: set[Any] = set()  # 后台小任务（更新检查）的引用，防被回收
        self.app: web.Application = self._build_app()
        self._runner: web.AppRunner | None = None
        self.port: int | None = None

    async def start(self, host: str, port: int) -> bool:
        if self._runner is not None:
            return True
        runner = web.AppRunner(self.app)
        try:
            await runner.setup()
            await web.TCPSite(runner, host, port).start()
        except OSError as e:
            logger.error("网页端口 %s:%s 起不来（可能被占用）：%s。网页这次不开了，插件其他功能照常。", host, port, e)
            try:
                await runner.cleanup()
            except Exception:
                pass
            return False
        self._runner = runner
        self.port = port
        logger.info("MaiWork 网页已开在 http://%s:%s", host, port)
        return True

    async def stop(self) -> None:
        runner, self._runner = self._runner, None
        port, self.port = self.port, None
        if runner is not None:
            try:
                await asyncio.wait_for(runner.cleanup(), timeout=5)
            except Exception:
                logger.exception("网页关闭时出错")
            # cleanup() 关服务端连接是 fire-and-forget，监听端口真正释放有毫秒级延迟；
            # 等它真的连不上再返回，调用方立刻重 bind 不会撞到
            if port is not None:
                await self._wait_port_free(port)

    @staticmethod
    async def _wait_port_free(port: int, timeout: float = 3.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
            except OSError:
                return  # 连不上 = 端口空了
            await asyncio.sleep(0.05)

    # ------------------------------------------------------------------
    # 鉴权
    # ------------------------------------------------------------------

    def _identify(self, request: web.Request) -> Identity:
        auth: ConsoleAuth = request.app[AUTH_KEY]
        cookie = request.cookies.get(COOKIE_NAME, "")
        if cookie:
            if cookie.startswith("g:"):
                # 群管理员 cookie：g:<群号>.<到期>.<签名>；群不再是服务群 → 视为 none
                gid = auth.check_group_cookie(cookie)
                if gid is not None and self._resolve_ref(gid) is not None:
                    return Identity(role="group_admin", group_id=gid)
            elif auth.check_cookie(cookie):
                return Identity(role="admin")
        token = request.headers.get("X-MW-Group", "").strip()
        if token:
            gid = views.group_id_by_token(self._svc, token)
            if gid is not None:
                return Identity(role="member", group_id=gid)
        return Identity(role="none")

    def _require_admin(self, request: web.Request) -> web.Response | None:
        """只给总管理员；群管理员碰全局的东西 → 403，群友 403、匿名 401。"""
        ident = self._identify(request)
        if ident.role == "admin":
            return None
        if ident.role == "group_admin":
            return _err(403, "群管理员只能管本群的事")
        if ident.role == "member":
            return _err(403, "这里只有管理员能进")
        return _err(401, "先登录管理员")

    @staticmethod
    def _require_group_admin_ident(ident: Identity, gid: Any) -> web.Response | None:
        """按群的管理动作：总管理员随便；群管理员只在本群；群友 / 匿名拒绝。"""
        if ident.role == "admin":
            return None
        if ident.role == "group_admin":
            if str(ident.group_id or "") == str(gid or ""):
                return None
            return _err(403, "群管理员只能管本群的事")
        if ident.role == "member":
            return _err(403, "这里只有管理员能进")
        return _err(401, "先登录管理员，或用群链接打开")

    def _require_group_admin(self, request: web.Request, gid: Any) -> web.Response | None:
        return self._require_group_admin_ident(self._identify(request), gid)

    @staticmethod
    def _origin_guard(request: web.Request) -> web.Response | None:
        """非 GET 且有 Origin 头：Origin 的 host:port 必须等于请求 Host。"""
        if request.method == "GET":
            return None
        origin = request.headers.get("Origin")
        if origin is None:
            return None
        host = request.headers.get("Host", "")
        if not same_origin(origin, host):
            logger.warning("拒了一次跨源 %s %s", request.method, request.path)
            return _err(403, "跨源请求被拒绝")
        return None

    @staticmethod
    def _redeliver_failed(svc: Any, task_id: str) -> dict:
        """redeliver：把该任务 failed / uncertain 的 outbox 项 retry。

        M1：kind=file 且 status=uncertain 的项跳过（群文件上传不幂等，可能其实
        已经传上去了）——返回里带 warning 让网页提示「群文件可能已发出，请先到群里确认」。
        返回 {"retried": int, "skipped": int, "warning": str}。
        """
        outbox = getattr(svc, "outbox", None)
        store = getattr(svc, "store", None)
        result = {"retried": 0, "skipped": 0, "warning": ""}
        if outbox is None or store is None:
            return result
        try:
            rows = store.read().execute(
                "SELECT id, kind, status FROM outbox WHERE task_id=? AND status IN ('failed', 'uncertain')",
                (str(task_id),),
            ).fetchall()
        except Exception:
            logger.exception("redeliver 查询失败（任务 %s）", task_id)
            return result
        for r in rows:
            if str(r["kind"]) == "file" and str(r["status"]) == "uncertain":
                result["skipped"] += 1
                continue
            try:
                outbox.retry(int(r["id"]))
                result["retried"] += 1
            except ValueError:
                pass
        if result["skipped"]:
            result["warning"] = "有的群文件发送超时被跳过了：群文件可能已发出，请先到群里确认，确认没发出来再让管理员强制重发"
        return result

    def _write(self, handler: Handler) -> Handler:
        """非 GET 处理器的同源守卫包装。"""

        async def wrapped(request: web.Request) -> web.Response:
            guard = self._origin_guard(request)
            if guard is not None:
                return guard
            return await handler(request)

        return wrapped

    def _resolve_ref(self, ref: str) -> str | None:
        """群引用（群号或链接码）→ 群号；只认配置里的服务群。"""
        svc = self._svc
        settings = svc.get_settings()
        if settings is not None and settings.is_served(ref):
            return ref
        return views.group_id_by_token(svc, ref)

    # ------------------------------------------------------------------
    # 组装
    # ------------------------------------------------------------------

    def _build_app(self) -> web.Application:
        svc = self._svc
        app = web.Application(middlewares=(self._errors_mw,))
        auth = ConsoleAuth(svc.store, svc.get_settings)
        # 群管理员：指纹签 cookie（ConsoleAuth ← GroupAdmins），密码比对总管理员（反向）
        group_admins = getattr(svc, "group_admins", None)
        if group_admins is None:
            try:
                group_admins = GroupAdmins(svc.store, get_settings=svc.get_settings)
            except Exception:
                logger.exception("建群管理员存储出错，群管理员这次不可用")
                group_admins = None
        if group_admins is not None:
            try:
                group_admins.bind_console_auth(auth)
            except Exception:
                logger.exception("群管理员接总管理员密码比对上出错")
            try:
                auth.bind_group_admins(group_admins)
            except Exception:
                logger.exception("总管理员接群管理员密码指纹上出错")
        app[AUTH_KEY] = auth

        def get(path: str):
            def deco(fn: Handler) -> Handler:
                app.router.add_get(path, fn)
                return fn

            return deco

        def post(path: str):
            def deco(fn: Handler) -> Handler:
                app.router.add_post(path, self._write(fn))
                return fn

            return deco

        async def _json_body(request: web.Request) -> dict[str, Any] | None:
            try:
                body = await request.json()
            except Exception:
                return None
            return body if isinstance(body, dict) else None

        # ---------- 身份 ----------

        @get("/api/me")
        async def _me(request: web.Request) -> web.Response:
            ident = self._identify(request)
            group = None
            if ident.role in ("member", "group_admin"):
                group = ident.group_id
            elif ident.role == "admin":
                # 管理员带链接码打开时也能定位到那个群（还能顺便核对 admin 看群号路径不冲突）
                token = request.headers.get("X-MW-Group", "").strip()
                if token:
                    group = views.group_id_by_token(svc, token)
            return web.json_response(
                {
                    "role": ident.role,
                    "group": group,
                    "bot": views._bot_info(svc),
                    "now": clock.now(),
                }
            )

        # ---------- 更新提醒（只给总管理员；只提醒不自动更新，update_check.py） ----------

        @get("/api/update")
        async def _update_status(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            chk = getattr(svc, "update_check", None)
            if chk is None:
                return web.json_response({"enabled": False, "newer": False})
            # 到点了就后台查一次（不让这个请求等 GitHub）；这次先回缓存，下次轮询就能看到
            try:
                task = asyncio.ensure_future(chk.maybe_refresh())
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)
            except Exception:
                logger.exception("起更新检查出错")
            st = dict(chk.status())
            settings = svc.get_settings()
            st["maibot_webui_url"] = getattr(getattr(settings, "console", None), "maibot_webui_url", "") or ""
            return web.json_response(st)

        @post("/api/login")
        async def _login(request: web.Request) -> web.Response:
            auth: ConsoleAuth = request.app[AUTH_KEY]
            ip = request.remote or ""
            if auth.login_blocked(ip):
                return _err(429, "错太多次了，过 10 分钟再试")
            body = await _json_body(request)
            password = str((body or {}).get("password") or "")
            if password and auth.verify_password(password):
                # 总管理员：cookie 格式和以前一样
                auth.record_login_ok(ip)
                value, max_age = auth.make_cookie()
                resp = web.json_response({"ok": True, "role": "admin"})
                resp.set_cookie(COOKIE_NAME, value, max_age=max_age, httponly=True, samesite="Strict", path="/")
                return resp
            gid_hit = None
            if password:
                ga = getattr(svc, "group_admins", None)
                if ga is not None:
                    try:
                        gid_hit = ga.match(password)
                    except Exception:
                        logger.exception("群管理员密码匹配出错")
                        gid_hit = None
            if gid_hit:
                value, max_age = auth.make_group_cookie(gid_hit)
                if not value:
                    auth.record_login_fail(ip)
                    return _err(401, "密码不对，再试一次")
                auth.record_login_ok(ip)
                logger.info("群 %s 的群管理员从网页登录了（IP %s）", gid_hit, ip)
                resp = web.json_response({"ok": True, "role": "group_admin", "group": gid_hit})
                resp.set_cookie(COOKIE_NAME, value, max_age=max_age, httponly=True, samesite="Strict", path="/")
                return resp
            auth.record_login_fail(ip)
            logger.info("网页登录失败一次（IP %s）", ip)
            return _err(401, "密码不对，再试一次")

        @post("/api/logout")
        async def _logout(request: web.Request) -> web.Response:
            resp = web.json_response({"ok": True})
            resp.del_cookie(COOKIE_NAME, path="/")
            return resp

        # ---------- 群 ----------

        @get("/api/groups")
        async def _groups(request: web.Request) -> web.Response:
            ident = self._identify(request)
            if ident.role == "admin":
                return web.json_response(views.list_summaries(svc, admin=True))
            if ident.role == "group_admin":
                # 群管理员只看到自己那一个群（管理员版视图：带链接码，他能重置本群链接）
                return web.json_response(
                    views.list_summaries(svc, admin=True, only_group_id=ident.group_id)
                )
            if ident.role == "member":
                return web.json_response(views.list_summaries(svc, admin=False, only_group_id=ident.group_id))
            return _err(401, "先登录管理员，或用群链接打开")

        @get("/api/groups/{ref}")
        async def _group_view(request: web.Request) -> web.Response:
            ident = self._identify(request)
            ref = request.match_info["ref"]
            if ident.role == "admin":
                gid = self._resolve_ref(ref)
                if gid is None:
                    return _err(404, "没有这个群")
                return web.json_response(views.group_view(svc, gid, admin=True))
            if ident.role == "group_admin":
                # 只限本群；给管理员版视图（能管这个群的事）
                gid = self._resolve_ref(ref)
                if gid is None:
                    return _err(404, "没有这个群")
                if str(gid) != str(ident.group_id or ""):
                    return _err(403, "群管理员只能管本群的事")
                return web.json_response(views.group_view(svc, gid, admin=True))
            if ident.role == "member":
                if not _looks_like_token(ref):
                    return _err(403, "群友要用自己群的链接打开")
                token_gid = views.group_id_by_token(svc, ref)
                if token_gid is None:
                    return _err(404, "这个链接打不开了")
                if token_gid != ident.group_id:
                    return _err(403, "只能看自己群的内容")
                return web.json_response(views.group_view(svc, token_gid, admin=False))
            return _err(401, "先登录管理员，或用群链接打开")

        # ---------- 画像 / 关注成员（管理员） ----------

        @post("/api/groups/{gid}/profile")
        async def _profile_add(request: web.Request) -> web.Response:
            gid = self._resolve_ref(request.match_info["gid"])
            if gid is None:
                return _err(404, "没有这个群")
            forbid = self._require_group_admin(request, gid)
            if forbid is not None:
                return forbid
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            category = str(body.get("category") or "").strip()
            text = str(body.get("text") or "").strip()
            if category not in views.CATEGORY_KEYS:
                return _err(400, "类别不对，只支持五类画像")
            if not text:
                return _err(400, "内容不能是空的")
            entry_id = svc.profiles.add_entry(gid, category, text)
            return web.json_response({"ok": True, "id": int(entry_id)})

        def _profile_group(entry_id: int) -> str | None:
            """这条画像条目属于哪个群（鉴权用；遍历服务群，不碰别的模块内部）。"""
            settings = svc.get_settings()
            gids = list(settings.groups.keys()) if settings is not None else []
            for gid in gids:
                try:
                    entries = svc.profiles.entries(gid) or []
                except Exception:
                    continue
                for e in entries:
                    if not isinstance(e, dict):
                        continue
                    if int(e.get("id") or 0) == int(entry_id) and not e.get("deleted"):
                        return str(gid)
            return None

        async def _profile_edit(request: web.Request) -> web.Response:
            ident = self._identify(request)
            if ident.role not in ("admin", "group_admin"):
                forbid = self._require_admin(request)
                return forbid
            try:
                entry_id = int(request.match_info["entry_id"])
            except (ValueError, TypeError):
                return _err(404, "这条画像不存在")
            gid = _profile_group(entry_id)
            if gid is None:
                return _err(404, "这条画像不存在")
            forbid = self._require_group_admin_ident(ident, gid)
            if forbid is not None:
                return forbid
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            kwargs: dict[str, Any] = {}
            if body.get("text") is not None:
                kwargs["text"] = str(body["text"])
            if body.get("locked") is not None:
                kwargs["locked"] = bool(body["locked"])
            try:
                svc.profiles.edit_entry(entry_id, **kwargs)
            except (KeyError, ValueError):
                return _err(404, "这条画像不存在")
            return web.json_response({"ok": True})

        async def _profile_delete(request: web.Request) -> web.Response:
            ident = self._identify(request)
            if ident.role not in ("admin", "group_admin"):
                forbid = self._require_admin(request)
                return forbid
            try:
                entry_id = int(request.match_info["entry_id"])
            except (ValueError, TypeError):
                return _err(404, "这条画像不存在")
            gid = _profile_group(entry_id)
            if gid is None:
                return _err(404, "这条画像不存在")
            forbid = self._require_group_admin_ident(ident, gid)
            if forbid is not None:
                return forbid
            try:
                svc.profiles.delete_entry(entry_id)
            except (KeyError, ValueError):
                return _err(404, "这条画像不存在")
            return web.json_response({"ok": True})

        app.router.add_route("PATCH", "/api/profile/{entry_id}", self._write(_profile_edit))
        app.router.add_route("DELETE", "/api/profile/{entry_id}", self._write(_profile_delete))

        @post("/api/groups/{gid}/focus")
        async def _focus(request: web.Request) -> web.Response:
            gid = self._resolve_ref(request.match_info["gid"])
            if gid is None:
                return _err(404, "没有这个群")
            forbid = self._require_group_admin(request, gid)
            if forbid is not None:
                return forbid
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            user_id = str(body.get("user_id") or "").strip()
            action = str(body.get("action") or "").strip()
            if not user_id.isdigit():
                return _err(400, "QQ 号应该是纯数字")
            if action not in ("add", "remove", "auto"):
                return _err(400, "action 只支持 add / remove / auto")
            if action == "remove" and self._identify(request).role != "admin":
                return _err(403, "移除关注会删掉个人画像，只有总管理员能做")
            svc.profiles.set_focus(gid, user_id, action)
            return web.json_response({"ok": True})

        @post("/api/groups/{gid}/token")
        async def _token_reset(request: web.Request) -> web.Response:
            gid = self._resolve_ref(request.match_info["gid"])
            if gid is None:
                return _err(404, "没有这个群")
            forbid = self._require_group_admin(request, gid)
            if forbid is not None:
                return forbid
            new_token = svc.reset_group_token(gid)
            if not new_token:
                return _err(404, "这个群还没有链接码")
            logger.info("群 %s 的链接码已重置，旧链接即刻失效", gid)
            return web.json_response({"token": new_token})

        # ---------- 群管理员（按群的管理员；设置 / 查看只给总管理员） ----------

        def _group_admin_ready() -> tuple[Any, web.Response | None]:
            ga = getattr(svc, "group_admins", None)
            if ga is None:
                return None, _err(503, "群管理员还没开")
            return ga, None

        def _group_admin_view(gid: str) -> dict[str, Any]:
            ga = getattr(svc, "group_admins", None)
            if ga is None:
                return {"password_set": False, "accounts": []}
            return {
                "password_set": bool(ga.has_password(gid)),
                "accounts": list(ga.accounts(gid)),
            }

        @get("/api/groups/{gid}/group-admin")
        async def _group_admin_get(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            _ga, not_ready = _group_admin_ready()
            if not_ready is not None:
                return not_ready
            gid = self._resolve_ref(request.match_info["gid"])
            if gid is None:
                return _err(404, "没有这个群")
            return web.json_response(_group_admin_view(gid))

        async def _group_admin_put(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            ga, not_ready = _group_admin_ready()
            if not_ready is not None:
                return not_ready
            gid = self._resolve_ref(request.match_info["gid"])
            if gid is None:
                return _err(404, "没有这个群")
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            password = str(body.get("password") or "")
            has_accounts = "accounts" in body and body.get("accounts") is not None
            accounts_v = body.get("accounts") if has_accounts else None
            if has_accounts and not isinstance(accounts_v, list):
                return _err(400, "名单要是数组，比如 [\"qq:123456\"]")
            if password:
                try:
                    ga.set_password(gid, password)
                except ValueError as e:
                    return _err(400, str(e))
                # 日志只说「改了」，不记密码本身
                logger.info("群 %s 的群管理员密码已更新", gid)
            if has_accounts:
                try:
                    accounts = ga.set_accounts(gid, accounts_v)
                except ValueError as e:
                    return _err(400, str(e))
                logger.info("群 %s 的群管理员名单改成 %d 人", gid, len(accounts))
            return web.json_response(_group_admin_view(gid))

        async def _group_admin_delete_password(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            ga, not_ready = _group_admin_ready()
            if not_ready is not None:
                return not_ready
            gid = self._resolve_ref(request.match_info["gid"])
            if gid is None:
                return _err(404, "没有这个群")
            ga.clear_password(gid)
            logger.info("群 %s 的群管理员密码已清掉，该群旧的登录状态立刻失效", gid)
            return web.json_response(_group_admin_view(gid))

        app.router.add_route("PUT", "/api/groups/{gid}/group-admin", self._write(_group_admin_put))
        app.router.add_route(
            "DELETE", "/api/groups/{gid}/group-admin/password", self._write(_group_admin_delete_password)
        )

        # ---------- 设置（管理员） ----------

        @get("/api/settings")
        async def _settings(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            return web.json_response(views.settings_view(svc))

        # ---------- 端点 + 模型库（管理员；2026-10 模型改版 1a；替代旧 /api/settings/models*） ----------
        # 只进不出：任何响应都不含 api_key（key_set 布尔代替）；写操作立刻落 config.toml 并热应用。

        from .. import config as _cfg
        from .. import config_file as _cf

        def _endpoint_public(ep: Any) -> dict[str, Any]:
            d: dict[str, Any] = {
                "id": str(ep.id), "name": str(ep.name), "protocol": str(ep.protocol),
                "base_url": str(ep.base_url), "key_set": bool(str(getattr(ep, "api_key", "") or "")),
                "retries": int(ep.retries), "retry_delay_s": int(ep.retry_delay_s),
                "max_concurrency": int(ep.max_concurrency), "max_rpm": int(ep.max_rpm),
            }
            checked = svc.store.kv_get(f"endpoints.checked.{ep.id}") if svc.store is not None else None
            if isinstance(checked, dict):
                try:
                    d["checked_at"] = float(checked.get("checked_at") or 0.0)
                except (TypeError, ValueError):
                    d["checked_at"] = 0.0
                raw = checked.get("available")
                d["available"] = [str(x) for x in raw] if isinstance(raw, list) else []
            return d

        def _model_public(entry: Any) -> dict[str, Any]:
            return {
                "id": str(entry.id), "endpoint": str(entry.endpoint), "model": str(entry.model),
                "name": str(entry.name), "efforts": [str(x) for x in (entry.efforts or ())],
                "vision": bool(entry.vision),
                "context_window": int(entry.context_window), "max_tokens": int(entry.max_tokens),
            }

        def _endpoints_view() -> dict[str, Any]:
            settings = svc.get_settings()
            return {
                "endpoints": [_endpoint_public(ep) for ep in (getattr(settings, "endpoints", ()) or ())],
                "models": [_model_public(m) for m in (getattr(settings, "model_list", ()) or ())],
            }

        async def _apply_config_after_file_write(new_text: str) -> None:
            """写完文件立刻在本进程应用；失败只记日志（宿主文件监控会补一次）。"""
            try:
                await svc.apply_config_text(new_text)
            except Exception:
                logger.exception("写后应用出错（文件已写，宿主文件监控会补一次）")

        @get("/api/settings/endpoints")
        async def _endpoints_get(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            return web.json_response(_endpoints_view())

        def _validate_endpoint_payload(
            body: dict[str, Any], path_id: str, existing: dict[str, Any] | None,
        ) -> dict[str, Any]:
            """把网页 body 合出一份端点字典；域名格式错误直接抛 ValueError(中文)。
            api_key：空串/没给 = 保持 old 值（新建=空），非空串 = 覆盖。"""
            raw_patch: dict[str, Any] = {}
            for key in ("name", "protocol", "base_url"):
                if key in body:
                    raw_patch[key] = body[key]
            for key in ("retries", "retry_delay_s", "max_concurrency", "max_rpm"):
                if key in body and body[key] is not None:
                    raw_patch[key] = body[key]
            cand: dict[str, Any] = dict(existing or {})
            cand.update(raw_patch)
            cand["id"] = path_id  # id 以路由为准（body.id 一致才放行，不然误导）
            if "id" in body and str(body.get("id") or "").strip() != path_id:
                raise ValueError("地址里的端点 id 和 body.id 不一致")
            # api_key：只进；空串/没给 = 不改
            if "api_key" in body:
                key_v = body.get("api_key")
                if key_v is not None and str(key_v).strip():
                    cand["api_key"] = str(key_v)
            elif "api_key" not in cand:
                cand["api_key"] = ""
            problems: list[str] = []
            parsed = _cfg._parse_endpoints([cand], problems)
            if problems:
                raise ValueError(problems[0])
            if not parsed:
                raise ValueError("端点参数不合法")
            e = parsed[0]
            return {
                "id": e.id, "name": e.name, "protocol": e.protocol, "base_url": e.base_url,
                "api_key": e.api_key, "retries": e.retries, "retry_delay_s": e.retry_delay_s,
                "max_concurrency": e.max_concurrency, "max_rpm": e.max_rpm,
            }

        async def _endpoints_save(
            request: web.Request, *, create_or_update: bool,
        ) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            settings = svc.get_settings()
            entries = [dict(
                id=str(ep.id), name=str(ep.name), protocol=str(ep.protocol), base_url=str(ep.base_url),
                api_key=str(getattr(ep, "api_key", "") or ""), retries=int(ep.retries),
                retry_delay_s=int(ep.retry_delay_s), max_concurrency=int(ep.max_concurrency),
                max_rpm=int(ep.max_rpm),
            ) for ep in (getattr(settings, "endpoints", ()) or ())]
            path_id = str(request.match_info["id"]).strip()
            old = next((e for e in entries if e["id"] == path_id), None)
            if create_or_update and old is None:
                # 新建再走一遍 id 规则（路径 id 本身就得合法，不然 table 会跳过）
                problems: list[str] = []
                _cfg._parse_endpoints([{"id": path_id, "base_url": "https://x.test"}], problems)
                if problems:
                    return _err(400, problems[0])
            if not create_or_update and old is None:
                return _err(404, "没有这个端点（只能改已存在的）")
            try:
                cand = _validate_endpoint_payload(body, path_id, old)
            except ValueError as e:
                return _err(400, str(e))
            # 新建不许和已有 id 撞
            if create_or_update and old is None and any(e["id"] == cand["id"] for e in entries):
                return _err(400, f"端点 id「{cand['id']}」已经存在")
            entries = [cand if e["id"] == cand["id"] else e for e in entries] if old else entries + [cand]
            try:
                new_text = _cf.write_aot_section(
                    svc.config_file_ops()[0], svc.config_file_ops()[1], "endpoints", entries
                )
            except _cf.ConfigFileError as e:
                return _err(500, str(e))
            await _apply_config_after_file_write(new_text)
            logger.info("端点「%s」已保存（key_set=%s）", cand["id"], bool(cand.get("api_key")))
            return web.json_response(_endpoints_view())

        async def _endpoint_put(request: web.Request) -> web.Response:
            return await _endpoints_save(request, create_or_update=True)

        async def _endpoint_delete(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            settings = svc.get_settings()
            path_id = str(request.match_info["id"]).strip()
            entries = [dict(
                id=str(ep.id), name=str(ep.name), protocol=str(ep.protocol), base_url=str(ep.base_url),
                api_key=str(getattr(ep, "api_key", "") or ""), retries=int(ep.retries),
                retry_delay_s=int(ep.retry_delay_s), max_concurrency=int(ep.max_concurrency),
                max_rpm=int(ep.max_rpm),
            ) for ep in (getattr(settings, "endpoints", ()) or ())]
            if not any(e["id"] == path_id for e in entries):
                return _err(404, "没有这个端点")
            users = [m for m in (getattr(settings, "model_list", ()) or ()) if str(getattr(m, "endpoint", "")) == path_id]
            if users:
                return _err(400, f"模型库里还有 {len(users)} 条模型挂在这个端点上，先把它们删掉或换到别的端点")
            entries = [e for e in entries if e["id"] != path_id]
            try:
                new_text = _cf.write_aot_section(
                    svc.config_file_ops()[0], svc.config_file_ops()[1], "endpoints", entries
                )
            except _cf.ConfigFileError as e:
                return _err(500, str(e))
            await _apply_config_after_file_write(new_text)
            try:
                mod = getattr(svc, "store", None)
                if mod is not None:
                    with mod.tx() as conn:
                        conn.execute("DELETE FROM kv WHERE key=?", (f"endpoints.checked.{path_id}",))
            except Exception:
                pass
            logger.info("端点「%s」已删除", path_id)
            return web.json_response(_endpoints_view())

        app.router.add_route("PUT", "/api/settings/endpoints/{id}", self._write(_endpoint_put))
        app.router.add_route("DELETE", "/api/settings/endpoints/{id}", self._write(_endpoint_delete))

        @post("/api/settings/endpoints/{id}/test")
        async def _endpoint_test(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            path_id = str(request.match_info["id"]).strip()
            settings = svc.get_settings()
            ep = next((e for e in (getattr(settings, "endpoints", ()) or ()) if str(getattr(e, "id", "")) == path_id), None)
            if ep is None and not str(body.get("base_url") or "").strip():
                return _err(404, "没有这个端点（测未存的值请在 body 里给 base_url）")
            # base_url：body 优先，存了的端点回落；protocol 同理
            base_url = str(body.get("base_url") or "").strip() or (str(getattr(ep, "base_url", "") or "") if ep else "")
            protocol = str(body.get("protocol") or "").strip() or (str(getattr(ep, "protocol", "openai") or "openai") if ep else "openai")
            if protocol not in ("openai", "anthropic", "responses"):
                return _err(400, "协议只认 openai / anthropic / responses")
            if not base_url.startswith(("http://", "https://")):
                return _err(400, "端点地址要以 http:// 或 https:// 开头")
            # api_key：body 非空为准，否则用这个端点的存稿（密钥不出接口）
            api_key = str(body.get("api_key") or "")
            if not api_key and ep is not None:
                api_key = svc.models.endpoint_key(path_id)
            try:
                available = await svc.models.list_models(base_url, api_key, protocol=protocol)
            except Exception as e:
                message = str(e) or "连接失败"
                logger.info("端点「%s」测试失败：%s", path_id, message[:120])
                return web.json_response({"ok": False, "models": [], "error": message})
            # 存「测试连接」结果（按端点；不是配置）
            if ep is not None:
                self._save_endpoint_checked(path_id, base_url, available, protocol)
            return web.json_response({"ok": True, "models": available})

        # ---------- 模型库（[[model_list]]） ----------

        def _validate_model_payload(
            body: dict[str, Any], path_id: str, existing: dict[str, Any] | None, endpoints: tuple,
        ) -> dict[str, Any]:
            raw_patch: dict[str, Any] = {}
            for key in ("endpoint", "model", "name"):
                if key in body:
                    raw_patch[key] = body[key]
            if "efforts" in body and body["efforts"] is not None:
                raw_patch["efforts"] = body["efforts"]
            if "vision" in body and body["vision"] is not None:
                raw_patch["vision"] = body["vision"]
            for key in ("context_window", "max_tokens"):
                if key in body and body[key] is not None:
                    raw_patch[key] = body[key]
            cand: dict[str, Any] = dict(existing or {})
            cand.update(raw_patch)
            cand["id"] = path_id
            if "id" in body and str(body.get("id") or "").strip() != path_id:
                raise ValueError("地址里的 id 和 body.id 不一致")
            problems: list[str] = []
            parsed = _cfg._parse_model_list([cand], endpoints, problems)
            if problems:
                raise ValueError(problems[0])
            if not parsed:
                raise ValueError("模型条目参数不合法")
            m = parsed[0]
            return {
                "id": m.id, "endpoint": m.endpoint, "model": m.model, "name": m.name,
                "efforts": list(m.efforts or ()), "vision": bool(m.vision),
                "context_window": int(m.context_window), "max_tokens": int(m.max_tokens),
            }

        async def _model_put(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            settings = svc.get_settings()
            endpoints = tuple(getattr(settings, "endpoints", ()) or ())
            entries = [_model_public(m) for m in (getattr(settings, "model_list", ()) or ())]
            path_id = str(request.match_info["id"]).strip()
            old = next((m for m in entries if m["id"] == path_id), None)
            if old is None:
                problems: list[str] = []
                _cfg._parse_model_list([{"id": path_id, "endpoint": "__x__", "model": "y"}], endpoints, problems)
                if problems and "不合法" in problems[0]:
                    return _err(400, problems[0])
            try:
                cand = _validate_model_payload(body, path_id, old, endpoints)
            except ValueError as e:
                return _err(400, str(e))
            if old is None and any(m["id"] == cand["id"] for m in entries):
                return _err(400, f"模型条目 id「{cand['id']}」已经存在")
            entries = [cand if m["id"] == cand["id"] else m for m in entries] if old else entries + [cand]
            try:
                new_text = _cf.write_aot_section(
                    svc.config_file_ops()[0], svc.config_file_ops()[1], "model_list", entries
                )
            except _cf.ConfigFileError as e:
                return _err(500, str(e))
            await _apply_config_after_file_write(new_text)
            logger.info("模型条目「%s」已保存（服务端模型名 %s）", cand["id"], cand.get("model", ""))
            return web.json_response(_endpoints_view())

        async def _model_delete(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            settings = svc.get_settings()
            entries = [_model_public(m) for m in (getattr(settings, "model_list", ()) or ())]
            path_id = str(request.match_info["id"]).strip()
            if not any(m["id"] == path_id for m in entries):
                return _err(404, "没有这个模型条目")
            # 岗位在用 → 拒（消息点名岗位，管理员知道先去哪改）
            users: list[str] = []
            agents_mod = getattr(svc, "agents", None)
            if agents_mod is not None:
                try:
                    for p in agents_mod.profiles():
                        if str(p.get("model") or "") == path_id or str(p.get("backup") or "") == path_id:
                            users.append(str(p.get("title") or p.get("kind") or "?"))
                except Exception:
                    pass
            if users:
                return _err(400, f"还有专岗在用这个模型（{'、'.join(users)}），先到「专岗」页改掉再删")
            entries = [m for m in entries if m["id"] != path_id]
            try:
                new_text = _cf.write_aot_section(
                    svc.config_file_ops()[0], svc.config_file_ops()[1], "model_list", entries
                )
            except _cf.ConfigFileError as e:
                return _err(500, str(e))
            await _apply_config_after_file_write(new_text)
            logger.info("模型条目「%s」已删除", path_id)
            return web.json_response(_endpoints_view())

        app.router.add_route("PUT", "/api/settings/model-list/{id}", self._write(_model_put))
        app.router.add_route("DELETE", "/api/settings/model-list/{id}", self._write(_model_delete))

        async def _model_verify(request: web.Request) -> web.Response:
            """验证所选模型（docs/13 A03）：一次短回答 + 一次无副作用工具往返，结果存
            kv["models.verified.<id>"] 给引导完成页 / 设置页显示。会真的花一点点 token。"""
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            path_id = str(request.match_info["id"]).strip()
            try:
                result = await svc.models.verify_entry(path_id)
            except Exception as e:  # 兜底：验证本身出意外也要如实说
                result = {"ok": False, "chat_ok": False, "tools_ok": False, "error": str(e) or "验证出错",
                          "note": "", "suggested_max_tokens": 0, "calls": 0}
            settings = svc.get_settings()
            entry = next((m for m in (getattr(settings, "model_list", ()) or ()) if str(getattr(m, "id", "")) == path_id), None)
            if entry is not None:
                try:
                    with svc.store.tx() as conn:
                        svc.store.kv_set(conn, f"models.verified.{path_id}", {
                            "model": str(getattr(entry, "model", "")), "endpoint": str(getattr(entry, "endpoint", "")),
                            "ok": bool(result.get("ok")), "tools_ok": bool(result.get("tools_ok")),
                            "note": str(result.get("note") or "")[:300], "error": str(result.get("error") or "")[:300],
                            "ts": clock.now(),
                        })
                except Exception:
                    logger.debug("验证结果没存下", exc_info=True)
            return web.json_response(result)

        app.router.add_route("POST", "/api/settings/model-list/{id}/verify", self._write(_model_verify))

        # ---------- 规则（网页可改的设置；存 kv["rules.override"]，不写 config.toml） ----------

        def _rules_view() -> Any:
            from .. import rules as _rules

            base = svc.base_settings() if callable(getattr(svc, "base_settings", None)) else svc.get_settings()
            return _rules.rules_view(base, svc.store, effective=svc.get_settings())

        @get("/api/settings/rules")
        async def _rules_get(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            return web.json_response(_rules_view())

        async def _rules_put(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            from .. import rules as _rules

            base = svc.base_settings() if callable(getattr(svc, "base_settings", None)) else svc.get_settings()
            try:
                _rules.save_patch(svc.store, body, base=base)
            except ValueError as e:
                return _err(400, str(e))
            return web.json_response(_rules_view())

        app.router.add_route("PUT", "/api/settings/rules", self._write(_rules_put))

        @post("/api/settings/rules/reset")
        async def _rules_reset(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            from .. import rules as _rules

            try:
                _rules.reset_field(svc.store, body.get("field"))
            except ValueError as e:
                return _err(400, str(e))
            return web.json_response(_rules_view())

        # ---------- 通用设置（管理员；直写 config.toml，数据库不再存覆盖层） ----------

        def _config_view() -> Any:
            from .. import rules as _rules

            base = svc.base_settings() if callable(getattr(svc, "base_settings", None)) else svc.get_settings()
            return _rules.config_view(base, svc.store, effective=svc.get_settings())

        @get("/api/settings/config")
        async def _config_get(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            return web.json_response(_config_view())

        async def _config_put(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            from .. import config_file as _cf
            from .. import rules as _rules

            base = svc.base_settings() if callable(getattr(svc, "base_settings", None)) else svc.get_settings()
            try:
                plugin_dir, _data_dir = svc.config_file_ops()
            except Exception:
                plugin_dir = None
            try:
                changed = _rules.save_config_patch(svc.store, body, base=base, plugin_dir=plugin_dir)
            except ValueError as e:
                return _err(400, str(e))
            except _cf.ConfigFileError as e:
                return _err(500, str(e))
            except Exception:
                logger.exception("保存配置出错")
                return _err(500, "服务器出错了")
            if changed:
                # 写完文件立刻在本进程应用（不等宿主文件监控；宿主随后发的是同一份，幂等）
                try:
                    text = _cf.read_text(svc.config_file_ops()[0])
                    await svc.apply_config_text(text)
                except Exception:
                    logger.exception("配置写后应用出错（文件已写，宿主文件监控会补一次）")
            return web.json_response(_config_view())

        app.router.add_route("PUT", "/api/settings/config", self._write(_config_put))

        @post("/api/settings/config/reset")
        async def _config_reset(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            from .. import config_file as _cf
            from .. import rules as _rules

            base = svc.base_settings() if callable(getattr(svc, "base_settings", None)) else svc.get_settings()
            try:
                plugin_dir, _data_dir = svc.config_file_ops()
            except Exception:
                plugin_dir = None
            try:
                _rules.reset_config_field(svc.store, body.get("field"), base=base, plugin_dir=plugin_dir)
            except ValueError as e:
                return _err(400, str(e))
            except _cf.ConfigFileError as e:
                return _err(500, str(e))
            try:
                text = _cf.read_text(svc.config_file_ops()[0])
                await svc.apply_config_text(text)
            except Exception:
                logger.exception("配置写后应用出错（文件已写，宿主文件监控会补一次）")
            return web.json_response(_config_view())

        # ---------- 首次安装引导（管理员） ----------

        @get("/api/onboarding")
        async def _onboarding_get(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            from .. import onboarding as _onb

            return web.json_response(_onb.view(svc))

        async def _onboarding_post(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            from .. import onboarding as _onb

            try:
                return web.json_response(
                    _onb.act(svc, str(body.get("action") or ""), str(body.get("step") or ""))
                )
            except ValueError as e:
                return _err(400, str(e))

        app.router.add_route("POST", "/api/onboarding", self._write(_onboarding_post))

        # ---------- 最近模型/工具请求日志（管理员，docs/07 §9.4） ----------

        from .. import models as _models_mod
        import json as _json_lib

        def _limit_param(request: web.Request) -> int:
            try:
                n = int(str(request.query.get("limit") or "50"))
            except (TypeError, ValueError):
                n = 50
            return max(1, min(200, n))

        def _before_param(request: web.Request) -> int:
            try:
                return max(0, int(str(request.query.get("before_id") or "0")))
            except (TypeError, ValueError):
                return 0

        def _failed_where(request: web.Request) -> tuple[str, list]:
            """failed=0|1 → ok 过滤；SQLite 里 ok 存成整数，外部给的是「失败与否」。"""
            failed = str(request.query.get("failed") or "").strip()
            if failed == "1":
                return "ok = 0", []
            if failed == "0":
                return "ok = 1", []
            return "", []

        def _safe_loads(text: Any) -> dict:
            """model_calls 的 request/response 可能截在 80KB 上沿断了（不合法 JSON），
            解析不了就把字符串藏进 _raw，保证详情接口永远有结构。"""
            if not isinstance(text, str) or not text:
                return {}
            try:
                data = _json_lib.loads(text)
            except (ValueError, TypeError):
                return {"_raw": text}
            return data if isinstance(data, dict) else {"_raw": text}

        def _group_names() -> dict[str, str]:
            names: dict[str, str] = {}
            try:
                for row in svc.store.read().execute("SELECT group_id, name FROM groups").fetchall():
                    names[str(row["group_id"])] = str(row["name"] or "")
            except Exception:
                pass
            from ..names import clean_group_name

            return {gid: clean_group_name(n, gid) for gid, n in names.items()}

        def _secret_list() -> list[str]:
            secrets = []
            try:
                for row in svc.store.read().execute("SELECT value FROM secrets").fetchall():
                    v = str(row["value"] or "")
                    if v and len(v) <= 4096:
                        secrets.append(v)
            except Exception:
                pass
            try:
                settings = svc.get_settings()
                if settings is not None:
                    v = str(getattr(settings.models, "api_key", "") or "")
                    if v:
                        secrets.append(v)
            except Exception:
                pass
            return secrets

        _LIST_TEXT_MAX = 300
        _DETAIL_TEXT_MAX = 20000

        @get("/api/logs/model-calls")
        async def _logs_model_calls(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            limit = _limit_param(request)
            before_id = _before_param(request)
            clauses: list[str] = []
            args: list = []
            failed_sql, failed_args = _failed_where(request)
            if failed_sql:
                clauses.append(failed_sql)
                args.extend(failed_args)
            purpose = str(request.query.get("purpose") or "").strip()
            if purpose:
                clauses.append("purpose = ?")
                args.append(purpose)
            group = str(request.query.get("group") or "").strip()
            if group:
                clauses.append("mc.group_id = ?")
                args.append(group)
            if before_id > 0:
                clauses.append("id < ?")
                args.append(before_id)
            where = "WHERE " + " AND ".join(clauses) if clauses else ""
            try:
                rows = svc.store.read().execute(
                    f"SELECT mc.* FROM model_calls mc {where} ORDER BY id DESC LIMIT ?",
                    tuple(args) + (limit + 1,),
                ).fetchall()
            except Exception:
                # 表还没建（旧数据目录）→ 空列表，绝不 500
                rows = []
            names = _group_names()
            items: list[dict] = []
            for r in rows[:limit]:
                d = dict(r)
                gid = str(d.get("group_id") or "")
                items.append(
                    {
                        "id": int(d["id"]),
                        "ts": float(d["ts"]),
                        "purpose": str(d.get("purpose") or ""),
                        "purpose_name": views.purpose_name(d.get("purpose")),
                        "role": str(d.get("role") or ""),
                        "agent": str(d.get("agent") or ""),
                        "model": str(d.get("model") or ""),
                        "group_id": gid,
                        "group_name": names.get(gid, "") if gid else "",
                        "task_id": str(d.get("task_id") or ""),
                        "attempt": int(d.get("attempt") or 1),
                        "ok": bool(d.get("ok")),
                        "status": int(d.get("status") or 0),
                        "ms": int(d.get("ms") or 0),
                        "prompt_tokens": int(d.get("prompt_tokens") or 0),
                        "completion_tokens": int(d.get("completion_tokens") or 0),
                        "error": _redact_secret(str(d.get("error") or ""), _secret_list()),
                    }
                )
            next_before_id = int(rows[limit - 1]["id"]) if len(rows) > limit else None
            return web.json_response({"items": items, "next_before_id": next_before_id})

        def _redact_secret(text: str, secrets: list[str]) -> str:
            return _models_mod._redact_full(text, secrets)

        @get("/api/logs/model-calls/{id}")
        async def _logs_model_call_detail(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            try:
                entry_id = int(str(request.match_info["id"]))
            except (ValueError, TypeError):
                return _err(404, "这条日志不存在")
            try:
                row = svc.store.read().execute(
                    "SELECT * FROM model_calls WHERE id=?", (entry_id,)
                ).fetchone()
            except Exception:
                row = None
            if row is None:
                return _err(404, "这条日志不存在")
            d = dict(row)
            secrets = _secret_list()
            gid = str(d.get("group_id") or "")
            names = _group_names()
            req_json = _safe_loads(d.get("request"))
            resp_json = _safe_loads(d.get("response"))
            return web.json_response(
                {
                    "id": int(d["id"]),
                    "ts": float(d["ts"]),
                    "purpose": str(d.get("purpose") or ""),
                    "purpose_name": views.purpose_name(d.get("purpose")),
                    "role": str(d.get("role") or ""),
                    "agent": str(d.get("agent") or ""),
                    "model": str(d.get("model") or ""),
                    "group_id": gid,
                    "group_name": names.get(gid, "") if gid else "",
                    "task_id": str(d.get("task_id") or ""),
                    "attempt": int(d.get("attempt") or 1),
                    "ok": bool(d.get("ok")),
                    "status": int(d.get("status") or 0),
                    "ms": int(d.get("ms") or 0),
                    "prompt_tokens": int(d.get("prompt_tokens") or 0),
                    "completion_tokens": int(d.get("completion_tokens") or 0),
                    "error": _redact_secret(str(d.get("error") or ""), secrets),
                    "request": _redact_json(req_json, secrets),
                    "response": _redact_json(resp_json, secrets),
                }
            )

        def _redact_json(value: Any, secrets: list[str]) -> Any:
            """对日志里已存的 JSON 再走一遍遮罩（库里的旧行可能没遮过）。"""
            if isinstance(value, str):
                return _models_mod._redact_full(value, secrets)
            if isinstance(value, list):
                return [_redact_json(x, secrets) for x in value]
            if isinstance(value, dict):
                return {str(k): _redact_json(v, secrets) for k, v in value.items()}
            return value

        @get("/api/logs/tool-calls")
        async def _logs_tool_calls(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            limit = _limit_param(request)
            before_id = _before_param(request)
            clauses: list[str] = []
            args: list = []
            failed_sql, failed_args = _failed_where(request)
            if failed_sql:
                clauses.append(failed_sql)
                args.extend(failed_args)
            if before_id > 0:
                clauses.append("id < ?")
                args.append(before_id)
            where = "WHERE " + " AND ".join(clauses) if clauses else ""
            try:
                rows = svc.store.read().execute(
                    f"SELECT * FROM tool_calls {where} ORDER BY id DESC LIMIT ?",
                    tuple(args) + (limit + 1,),
                ).fetchall()
            except Exception:
                rows = []
            names = _group_names()
            items = []
            for r in rows[:limit]:
                d = dict(r)
                gid = str(d.get("group_id") or "")
                items.append(
                    {
                        "id": int(d["id"]),
                        "ts": float(d["ts"]),
                        "group_id": gid,
                        "group_name": names.get(gid, "") if gid else "",
                        "task_id": str(d.get("task_id") or ""),
                        "actor": str(d.get("actor") or ""),
                        "tool": str(d.get("tool") or ""),
                        "ok": bool(d.get("ok")),
                        "ms": int(d.get("ms") or 0),
                        "input": str(d.get("input") or "")[:_LIST_TEXT_MAX],
                        "output": str(d.get("output") or "")[:_LIST_TEXT_MAX],
                        "error": str(d.get("error") or ""),
                    }
                )
            next_before_id = int(rows[limit - 1]["id"]) if len(rows) > limit else None
            return web.json_response({"items": items, "next_before_id": next_before_id})

        @get("/api/logs/tool-calls/{id}")
        async def _logs_tool_call_detail(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            try:
                entry_id = int(str(request.match_info["id"]))
            except (ValueError, TypeError):
                return _err(404, "这条日志不存在")
            try:
                row = svc.store.read().execute(
                    "SELECT * FROM tool_calls WHERE id=?", (entry_id,)
                ).fetchone()
            except Exception:
                row = None
            if row is None:
                return _err(404, "这条日志不存在")
            d = dict(row)
            gid = str(d.get("group_id") or "")
            names = _group_names()
            return web.json_response(
                {
                    "id": int(d["id"]),
                    "ts": float(d["ts"]),
                    "group_id": gid,
                    "group_name": names.get(gid, "") if gid else "",
                    "task_id": str(d.get("task_id") or ""),
                    "actor": str(d.get("actor") or ""),
                    "tool": str(d.get("tool") or ""),
                    "ok": bool(d.get("ok")),
                    "ms": int(d.get("ms") or 0),
                    "input": str(d.get("input") or "")[:_DETAIL_TEXT_MAX],
                    "output": str(d.get("output") or "")[:_DETAIL_TEXT_MAX],
                    "error": str(d.get("error") or ""),
                }
            )

        @get("/api/logs/summary")
        async def _logs_summary(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            day_start = clock.bj(clock.now()).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            r0 = svc.store.read().execute(
                "SELECT COUNT(*) AS calls,"
                " COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END),0) AS failed,"
                " COALESCE(SUM(CASE WHEN attempt>1 THEN 1 ELSE 0 END),0) AS retried,"
                " COALESCE(SUM(prompt_tokens + completion_tokens),0) AS tokens"
                " FROM model_calls WHERE ts >= ?",
                (day_start,),
            ).fetchone()
            today = {
                "calls": int(r0["calls"]) if r0 else 0,
                "failed": int(r0["failed"]) if r0 else 0,
                "retried": int(r0["retried"]) if r0 else 0,
                "tokens": int(r0["tokens"]) if r0 else 0,
            }
            last_failure = None
            try:
                rf = svc.store.read().execute(
                    "SELECT ts, purpose, model, error FROM model_calls WHERE ok=0"
                    " ORDER BY ts DESC LIMIT 1"
                ).fetchone()
            except Exception:
                rf = None
            if rf is not None:
                last_failure = {
                    "ts": float(rf["ts"]),
                    "purpose": str(rf["purpose"] or ""),
                    "purpose_name": views.purpose_name(rf["purpose"]),
                    "model": str(rf["model"] or ""),
                    "error": _redact_secret(str(rf["error"] or ""), _secret_list()),
                }
            by_purpose: list[dict] = []
            try:
                rp_rows = svc.store.read().execute(
                    "SELECT purpose, COUNT(*) AS calls,"
                    " COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END),0) AS failed,"
                    " COALESCE(AVG(ms),0) AS avg_ms"
                    " FROM model_calls WHERE ts >= ? GROUP BY purpose",
                    (day_start,),
                ).fetchall()
                for r in rp_rows:
                    by_purpose.append(
                        {
                            "purpose": str(r["purpose"] or ""),
                            "purpose_name": views.purpose_name(r["purpose"]),
                            "calls": int(r["calls"]),
                            "failed": int(r["failed"]),
                            "avg_ms": float(r["avg_ms"]),
                        }
                    )
            except Exception:
                by_purpose = []
            by_purpose.sort(key=lambda b: -b["calls"])
            return web.json_response(
                {"today": today, "last_failure": last_failure, "by_purpose": by_purpose}
            )

        @get("/api/usage/history")
        async def _usage_history(request: web.Request) -> web.Response:
            """用量历史（管理员）：?days=N（1..30，默认 7）或 ?date=YYYY-MM-DD（优先）。

            结构见 docs/07 §9.5；拼装全在 console/usage_history.py，这里只接线。
            """
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            try:
                data = usage_history.history_view(
                    svc,
                    days=request.query.get("days"),
                    date=request.query.get("date"),
                )
            except ValueError as e:  # date 格式不对
                return _err(400, str(e))
            return web.json_response(data)

        # ---------- M2：资讯 / 构想 / 开话题（docs/07 §9.2 末段） ----------

        async def _ident_for_group_action(request: web.Request, *, need_admin: bool) -> tuple[Identity | None, web.Response | None]:
            """M2 写操作的统一身份闸。
            need_admin=True → 只管理员；False → 管理员或群友都行（群友只能动本群的条目）。
            """
            ident = self._identify(request)
            if ident.role == "admin":
                return ident, None
            if need_admin:
                if ident.role == "group_admin":
                    return ident, None  # 能不能动这条由调用方按条目所属群再判
                if ident.role == "member":
                    return None, _err(403, "这里只有管理员能进")
                return None, _err(401, "先登录管理员")
            if ident.role in ("member", "group_admin"):
                return ident, None
            return None, _err(401, "先登录管理员，或用群链接打开")

        def _wrong_group(ident: Identity, gid: str) -> bool:
            """群友 / 群管理员只能动本群的条目。"""
            return ident.role in ("member", "group_admin") and str(ident.group_id or "") != str(gid)

        def _personal_deny(ident: Identity, kind: str, item_id: int) -> web.Response | None:
            """个人向内容（关注成员的）：群友当不存在（404），群管理员只读（403）。"""
            if not _personal_item(kind, item_id):
                return None
            if ident.role == "group_admin":
                return _err(403, _PERSONAL_READONLY)
            if ident.role == "member" and kind == "news":
                return _err(404, "这条资讯不存在")
            return None

        def _group_of(kind: str, item_id: int) -> str | None:
            """从库里查条目属于哪个群（鉴权用，不经过 feeds/topics）。"""
            if svc.store is None:
                return None
            table = {"news": "news_items", "ideas": "ideas", "topics": "topic_log"}.get(kind)
            if table is None:
                return None
            try:
                row = svc.store.read().execute(f"SELECT group_id FROM {table} WHERE id=?", (int(item_id),)).fetchone()
            except Exception as e:
                # 表还没建（M2 迁移没跑过）→ 按「没这条」处理，绝不 500
                logger.debug("%s 查询失败（%s）", table, type(e).__name__)
                return None
            return str(row["group_id"]) if row is not None else None

        def _personal_item(kind: str, item_id: int) -> bool:
            """这条是不是「给某个关注成员的」个人向内容（只给管理员看，群友按不存在处理）。"""
            if svc.store is None:
                return False
            table = {"news": "news_items", "ideas": "ideas"}.get(kind)
            if table is None:
                return False
            try:
                row = svc.store.read().execute(
                    f"SELECT * FROM {table} WHERE id=?", (int(item_id),)
                ).fetchone()
                if row is None or "target_user_id" not in row.keys():
                    return False  # 老库没有这一列 = 没有个人向内容
                return bool(str(row["target_user_id"] or ""))
            except Exception:
                return False

        def _m2_ready(module: Any) -> web.Response | None:
            if module is None:
                return _err(503, "这个功能还没开")
            return None

        async def _news_feedback(request: web.Request) -> web.Response:
            ident, deny = await _ident_for_group_action(request, need_admin=False)
            if deny is not None:
                return deny
            not_ready = _m2_ready(svc.feeds)
            if not_ready is not None:
                return not_ready
            try:
                item_id = int(request.match_info["id"])
            except (ValueError, TypeError):
                return _err(400, "id 要是数字")
            gid = _group_of("news", item_id)
            if gid is None:
                return _err(404, "这条资讯不存在")
            if _wrong_group(ident, gid):
                return _err(403, "只能管自己群的内容")
            personal_deny = _personal_deny(ident, "news", item_id)
            if personal_deny is not None:
                return personal_deny
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            try:
                out = svc.feeds.feedback("news", item_id, body.get("value"), body.get("prev"))
            except KeyError:
                return _err(404, "这条资讯不存在")
            # 身份与工作记忆：被标「没用」累计 3 次的来源/话题 → 自动记进本群记忆（不调模型）
            try:
                if body.get("value") == "down":
                    hook = getattr(svc, "note_useless_feedback", None)
                    if callable(hook):
                        hook(gid, item_id)
            except Exception:
                logger.exception("反馈自动记 hook 出错（群 %s 条 %s，不影响反馈）", gid, item_id)
            return web.json_response(out)

        async def _ideas_feedback(request: web.Request) -> web.Response:
            ident, deny = await _ident_for_group_action(request, need_admin=False)
            if deny is not None:
                return deny
            not_ready = _m2_ready(svc.feeds)
            if not_ready is not None:
                return not_ready
            try:
                item_id = int(request.match_info["id"])
            except (ValueError, TypeError):
                return _err(400, "id 要是数字")
            gid = _group_of("ideas", item_id)
            if gid is None:
                return _err(404, "这条构想不存在")
            if _wrong_group(ident, gid):
                return _err(403, "只能管自己群的内容")
            personal_deny = _personal_deny(ident, "ideas", item_id)
            if personal_deny is not None:
                return personal_deny
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            try:
                out = svc.feeds.feedback("ideas", item_id, body.get("value"), body.get("prev"))
            except KeyError:
                return _err(404, "这条构想不存在")
            return web.json_response(out)

        def _idea_action(op: str) -> Handler:
            async def _handler(request: web.Request) -> web.Response:
                need_admin = op in ("do", "dismiss")
                ident, deny = await _ident_for_group_action(request, need_admin=need_admin)
                if deny is not None:
                    return deny
                not_ready = _m2_ready(svc.feeds)
                if not_ready is not None:
                    return not_ready
                try:
                    item_id = int(request.match_info["id"])
                except (ValueError, TypeError):
                    return _err(400, "id 要是数字")
                gid = _group_of("ideas", item_id)
                if gid is None:
                    return _err(404, "这条构想不存在")
                if _wrong_group(ident, gid):
                    return _err(403, "只能管自己群的内容")
                personal_deny = _personal_deny(ident, "ideas", item_id)
                if personal_deny is not None:
                    return personal_deny
                if ident.role == "admin":
                    by = "管理员"
                elif ident.role == "group_admin":
                    by = "群管理员（网页）"
                else:
                    by = "群友（网页）"
                # 「直接开工」可以只做勾选的项目：body {"items": [1,3]}（构想项目序号，1 起）；
                # 不带 / 空 / 非法 = 全部（口径和 approvals.parse_idea_wanted 一致）。
                picked: Any = None
                if op == "do":
                    body = await _json_body(request)
                    if isinstance(body, dict):
                        picked = body.get("items")
                try:
                    out = svc.feeds.idea_action(item_id, op, by=by, item_nos=picked)
                except KeyError:
                    return _err(404, "这条构想不存在")
                except ValueError as e:
                    return _err(400, str(e))
                # 「想要这个」→ 待批请求：在路由这里接（feeds 不感知批准；do 走 app 的 on_start 回调）
                if op == "want":
                    hook = getattr(svc, "on_idea_want", None)
                    if callable(hook):
                        try:
                            hook(dict(out), gid)
                        except Exception:
                            logger.exception("构想「想要这个」接线出错（构想 %s）", item_id)
                    # want 之后状态可能变成 pending / started：回一次新的 view 给前端
                    try:
                        for item in svc.feeds.ideas_view(gid) or []:
                            if isinstance(item, dict) and int(item.get("id") or 0) == item_id:
                                out = item
                                break
                    except Exception:
                        pass
                return web.json_response(out)

            return _handler

        async def _topics_verdict(request: web.Request) -> web.Response:
            not_ready = _m2_ready(svc.topics)
            if not_ready is not None:
                return not_ready
            try:
                item_id = int(request.match_info["id"])
            except (ValueError, TypeError):
                return _err(400, "id 要是数字")
            topic_gid = _group_of("topics", item_id)
            if topic_gid is None:
                return _err(404, "这条开话题记录不存在")
            forbid = self._require_group_admin(request, topic_gid)
            if forbid is not None:
                return forbid
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            value = body.get("value")
            if value not in ("right", "wrong", None):
                return _err(400, 'value 只支持 "right" / "wrong" / null')
            svc.topics.verdict(item_id, value)
            return web.json_response({"ok": True})

        app.router.add_post("/api/news/{id}/feedback", self._write(_news_feedback))
        app.router.add_post("/api/ideas/{id}/feedback", self._write(_ideas_feedback))
        app.router.add_post("/api/ideas/{id}/want", self._write(_idea_action("want")))
        app.router.add_post("/api/ideas/{id}/do", self._write(_idea_action("do")))
        app.router.add_post("/api/ideas/{id}/dismiss", self._write(_idea_action("dismiss")))
        app.router.add_post("/api/topics/{id}/verdict", self._write(_topics_verdict))

        async def _feeds_domains(request: web.Request) -> web.Response:
            """管理员加减来源屏蔽名单（2026-09-27 质量标准 §4.1）。

            请求体 {domain, blocked: true|false}；域名规范化（小写、去 www.、
            只允许合法域名字符）后写 kv["feeds.blocked_domains"]——kv 存「当时生效名单」
            的全量（首次改动前 = 配置；改过一次之后以网页为准，见 feeds.blocked_domains_effective）。
            返回 {"blocked_domains": [...]}（生效名单，稳定排序）。
            """
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            from ..config import normalize_domain
            from ..feeds import blocked_domains_effective

            domain = normalize_domain(body.get("domain"))
            if not domain:
                return _err(400, "域名不合法（只认字母、数字、横线、点，如 example.com）")
            blocked_v = body.get("blocked")
            if not isinstance(blocked_v, bool):
                return _err(400, "blocked 要是 true / false")
            settings = svc.get_settings()
            config_blocked: tuple = tuple(getattr(settings.feeds, "blocked_domains", ()) or ()) if settings is not None else ()
            merged: set[str] = set(blocked_domains_effective(svc.store, config_blocked))
            if blocked_v:
                merged.add(domain)
            else:
                merged.discard(domain)
            out_list = sorted(merged)
            with svc.store.tx() as conn:
                svc.store.kv_set(conn, "feeds.blocked_domains", out_list)
            return web.json_response({"blocked_domains": out_list})

        app.router.add_post("/api/feeds/domains", self._write(_feeds_domains))

        # ---------- 扩展：MCP（docs/02 §10；只管理员；不回显 headers） ----------

        async def _mcp_reload(request: web.Request) -> web.Response:
            """POST /api/extensions/mcp/{name}/reload：重连并刷新工具。"""
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            name = request.match_info["name"]
            reloader = getattr(svc, "reload_mcp", None)
            if not callable(reloader):
                return _err(503, "MCP 扩展还没开")
            try:
                result = await reloader(name)
            except Exception:
                logger.exception("MCP 扩展 %s reload 出错", name)
                return _err(500, "reload 出错了")
            if result is None:
                return _err(404, "没有叫这个名字的 MCP 扩展")
            return web.json_response(
                {"ok": bool(result.get("ok")), "tools": int(result.get("tools") or 0), "error": str(result.get("error") or "")}
            )

        app.router.add_post("/api/extensions/mcp/{name}/reload", self._write(_mcp_reload))

        # ---------- 扩展网页管理（docs/02 §10、docs/07 §10.9；只管理员；密钥只进不出） ----------
        #
        # 存法：kv["extensions.mcp"]（不含头值）+ secrets["mcp.<名>.<头名>"]；config 来源的
        # 开关存 kv["extensions.mcp.disabled"] 名单。网页加的存数据库，不写回 config.toml。

        def _ext_ready() -> web.Response | None:
            if svc.extensions is None:
                return _err(503, "MCP 扩展还没开")
            return None

        def _mcp_item(name: str) -> dict | None:
            """合并视图里的单项（响应结构同 GET /api/extensions 的 mcp[i]）。"""
            from .. import extensions_web

            for item in extensions_web.list_items(svc.get_settings(), svc.store, svc.extensions):
                if item.get("name") == name:
                    return item
            return None

        @get("/api/extensions")
        async def _extensions_list(request: web.Request) -> web.Response:
            """合并后的 MCP + skill 清单（结构见 docs/07 §10.9；头值绝不回显）。"""
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            from .. import extensions_web, skills_web

            try:
                mcp = extensions_web.list_items(svc.get_settings(), svc.store, svc.extensions)
            except Exception:
                logger.exception("拼 MCP 扩展清单出错")
                mcp = []
            try:
                skills = skills_web.list_view(svc.get_settings().data_dir, svc.store, svc.get_settings())
            except Exception:
                logger.exception("拼 skill 清单出错")
                skills = []
            return web.json_response({"mcp": mcp, "skills": skills})

        @post("/api/extensions/mcp")
        async def _mcp_create(request: web.Request) -> web.Response:
            """新增网页 MCP 扩展：校验（同 config 规则）→ 落库 → 立即连接一次，返回单项。"""
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            not_ready = _ext_ready()
            if not_ready is not None:
                return not_ready
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            from .. import extensions_web

            try:
                entry = extensions_web.create(svc.store, svc.get_settings(), body)
            except ValueError as e:
                return _err(400, str(e))
            except FileExistsError:
                return _err(409, f"已经有叫「{str(body.get('name') or '')}」的 MCP 扩展了（重名）")
            name = str(entry["name"])
            try:
                await svc.reload_mcp(name)  # 立即连接一次
            except Exception:
                logger.exception("新增 MCP 扩展 %s 后首次连接出错", name)
            item = _mcp_item(name)
            if item is None:
                return _err(500, "保存后拼视图出错了")
            return web.json_response(item)

        async def _mcp_update(request: web.Request) -> web.Response:
            """修改网页 MCP 扩展；config 来源 → 409（只能开关不能改地址）；不存在 → 404。

            headers 里值为空字符串 = 不改这个头；remove_headers:[名字] 删头。改完立即重连。
            """
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            not_ready = _ext_ready()
            if not_ready is not None:
                return not_ready
            from .. import extensions_web

            name = str(request.match_info["name"])
            source = extensions_web.source_of(svc.get_settings(), svc.store, name)
            if source is None:
                return _err(404, "没有叫这个名字的 MCP 扩展")
            if source != "web":
                return _err(409, "这个扩展来自配置文件，只能开关不能改——请改 config.toml")
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            try:
                extensions_web.update(svc.store, name, body)
            except ValueError as e:
                return _err(400, str(e))
            except KeyError:
                return _err(404, "没有叫这个名字的 MCP 扩展")
            try:
                await svc.reload_mcp(name)  # 立即重连刷新工具
            except Exception:
                logger.exception("修改 MCP 扩展 %s 后重连出错", name)
            item = _mcp_item(name)
            if item is None:
                return _err(500, "保存后拼视图出错了")
            return web.json_response(item)

        async def _mcp_delete(request: web.Request) -> web.Response:
            """删除网页 MCP 扩展（只允许 source=web）：摘工具、关连接、清 kv 和头值。"""
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            not_ready = _ext_ready()
            if not_ready is not None:
                return not_ready
            from .. import extensions_web

            name = str(request.match_info["name"])
            source = extensions_web.source_of(svc.get_settings(), svc.store, name)
            if source is None:
                return _err(404, "没有叫这个名字的 MCP 扩展")
            if source != "web":
                return _err(409, "这个扩展来自配置文件，网页上只能开关，不能删除")
            try:
                extensions_web.delete(svc.store, name)
            except KeyError:
                return _err(404, "没有叫这个名字的 MCP 扩展")
            try:
                await svc.remove_mcp(name)  # 立即摘工具、关连接
            except Exception:
                logger.exception("删除 MCP 扩展 %s 后摘工具出错", name)
            try:
                from .. import search_binding

                search_binding.clear_binding(svc.store, mcp=name)  # 搜索绑定绑着它 → 跟着清掉
            except Exception:
                logger.exception("清 MCP 扩展 %s 的搜索绑定出错", name)
            return web.json_response({"ok": True})

        @post("/api/extensions/mcp/{name}/toggle")
        async def _mcp_toggle(request: web.Request) -> web.Response:
            """开关（config 来源也能开关）：开/关后立即摘工具或重连注册。"""
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            not_ready = _ext_ready()
            if not_ready is not None:
                return not_ready
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            enabled_v = body.get("enabled")
            if not isinstance(enabled_v, bool):
                return _err(400, "enabled 要是 true / false")
            from .. import extensions_web

            name = str(request.match_info["name"])
            try:
                extensions_web.toggle(svc.store, svc.get_settings(), name, enabled_v)
            except KeyError:
                return _err(404, "没有叫这个名字的 MCP 扩展")
            except ValueError as e:
                return _err(409, str(e))
            try:
                await svc.reload_mcp(name)  # 关掉：旧工具已摘；开：重连注册
            except Exception:
                logger.exception("开关 MCP 扩展 %s 后重连出错", name)
            item = _mcp_item(name)
            if item is None:
                return _err(500, "保存后拼视图出错了")
            return web.json_response(item)

        @post("/api/extensions/mcp/test")
        async def _mcp_test(request: web.Request) -> web.Response:
            """只试连（initialize + tools/list），不保存。headers 为空时传 name 可用已存的。"""
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            from .. import extensions_web

            url = str(body.get("url") or "").strip()
            if not url.startswith("https://"):
                return _err(400, "MCP 端点地址必须 https:// 开头（密钥走这个请求，http 会泄露）")
            headers_raw = body.get("headers")
            headers: dict[str, str] = {}
            if headers_raw is not None:
                if not isinstance(headers_raw, dict):
                    return _err(400, "headers 要是 {名字: 值} 的表")
                for k, v in headers_raw.items():
                    k_s, v_s = str(k or "").strip(), str(v or "")
                    if k_s and v_s:
                        headers[k_s] = v_s
            name_s = str(body.get("name") or "").strip()
            if name_s:
                stored = extensions_web.stored_headers_for(svc.store, svc.get_settings(), name_s)
                for k, v in stored.items():  # 已存的垫底，页面传的覆盖
                    headers.setdefault(k, v)
            try:
                timeout_s = max(1, int(body.get("timeout_s") or 20))
            except (TypeError, ValueError):
                timeout_s = 20
            from ..mcp_client import MCPError, McpSessionClient

            client = McpSessionClient(url, "", timeout_s=timeout_s, transport=getattr(svc, "extensions_transport", None), headers=headers)
            try:
                tools = await client.list_tools()
            except MCPError as e:
                return web.json_response({"ok": False, "tools": [], "error": str(e)})
            except Exception as e:
                logger.warning("试连 MCP 出意外错：%s", type(e).__name__, exc_info=True)
                return web.json_response({"ok": False, "tools": [], "error": f"连接出错：{type(e).__name__}"})
            finally:
                try:
                    await client.aclose()
                except Exception:
                    pass
            names = [str(t.get("name") or "").strip() for t in tools if str(t.get("name") or "").strip()]
            return web.json_response({"ok": True, "tools": names[:50], "error": ""})

        # ---------- skill 网页管理 ----------

        def _skill_view_or_404(name: str) -> tuple[dict | None, web.Response | None]:
            from .. import skills_web

            view = skills_web.get_view(svc.get_settings().data_dir, svc.store, name, svc.get_settings())
            if view is None:
                return None, _err(404, "没有这个 skill")
            return view, None

        # ---------- 联网搜索绑定（2026-10；docs/07 §10.3；只管理员） ----------
        # 搜索只走「扩展」里指定的一个 MCP 的某个工具；绑定存 kv["extensions.search"]。

        def _tool_spec_of(mcp: str, tool: str) -> dict | None:
            getter = getattr(svc.extensions, "tool_spec", None) if svc.extensions is not None else None
            return getter(mcp, tool) if callable(getter) else None

        def _runtime_of(name: str):
            return svc.extensions.runtime_of(name) if svc.extensions is not None else None

        @get("/api/extensions/search")
        async def _search_binding_get(request: web.Request) -> web.Response:
            """{binding, status: {ok, text}, candidates: [{mcp, tools: [{name, description, guess}]}]}。"""
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            from .. import search_binding

            try:
                return web.json_response(search_binding.search_view(svc.store, svc.get_settings(), _runtime_of))
            except Exception:
                logger.exception("拼搜索绑定视图出错")
                return _err(500, "读搜索绑定出错了")

        async def _search_binding_put(request: web.Request) -> web.Response:
            """绑定：{"mcp","tool","extract_tool"} → 校验扩展存在、工具存在 → 保存 → 返回同 GET。"""
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            from .. import search_binding

            try:
                search_binding.save_binding(svc.store, svc.get_settings(), body, tool_spec_of=_tool_spec_of)
            except ValueError as e:
                return _err(400, str(e))
            except Exception:
                logger.exception("保存搜索绑定出错")
                return _err(500, "保存搜索绑定出错了")
            return web.json_response(search_binding.search_view(svc.store, svc.get_settings(), _runtime_of))

        async def _search_binding_delete(request: web.Request) -> web.Response:
            """解绑（幂等）。"""
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            from .. import search_binding

            search_binding.clear_binding(svc.store)
            return web.json_response(search_binding.search_view(svc.store, svc.get_settings(), _runtime_of))

        app.router.add_route("PUT", "/api/extensions/search", self._write(_search_binding_put))
        app.router.add_route("DELETE", "/api/extensions/search", self._write(_search_binding_delete))

        # ---------- 预设搜索服务（search_presets / search_presets_web；只管理员；密钥只进不出） ----------

        def _presets_payload() -> dict:
            from .. import search_presets_web

            return {"presets": search_presets_web.presets_view(svc.get_settings(), svc.store, _runtime_of)}

        @get("/api/extensions/presets")
        async def _presets_get(request: web.Request) -> web.Response:
            """六家预设搜索服务 + 各自状态：{presets: [{id, label, free, free_note, key_page_url, docs_url,
            logo, entry, source, enabled, ok, key_set}]}。"""
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            try:
                return web.json_response(_presets_payload())
            except Exception:
                logger.exception("拼预设搜索服务清单出错")
                return _err(500, "读预设搜索服务出错了")

        async def _preset_activate(request: web.Request) -> web.Response:
            """POST /api/extensions/presets/{id}：打开这家 / 换密钥（{key}）/ 改回免密钥（{clear_key: true}）。
            还没有搜索绑定时顺手设成主搜索。返回 {presets, name, bound, label}。"""
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            not_ready = _ext_ready()
            if not_ready is not None:
                return not_ready
            body = await _json_body(request)
            if body is None:
                body = {}
            if not isinstance(body, dict):
                return _err(400, "请求体要写成 {\"key\": 可选密钥, \"clear_key\": 可选}")
            from .. import search_presets_web
            from ..search_presets import PRESETS

            pid = str(request.match_info["id"])
            key = body.get("key")
            if key is not None and not isinstance(key, str):
                return _err(400, "key 要是字符串")
            try:
                name, bound = search_presets_web.activate(
                    svc.store, svc.get_settings(), pid, key=key, clear_key=bool(body.get("clear_key"))
                )
            except KeyError:
                return _err(404, "没有这个预设搜索服务")
            except ValueError as e:
                return _err(400, str(e))
            try:
                await svc.reload_mcp(name)  # 立即连接一次（换了地址 / 密钥也要重连）
            except Exception:
                logger.exception("打开预设搜索服务 %s 后连接出错", pid)
            out = _presets_payload()
            out.update({"name": name, "bound": bound, "label": PRESETS[pid].label})
            return web.json_response(out)

        async def _presets_setup(request: web.Request) -> web.Response:
            """POST /api/extensions/presets-setup：首次引导一次配好 {items: [{id, key?}]}；
            第一家当主搜索，其余当备用。返回 {presets, search}。"""
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            not_ready = _ext_ready()
            if not_ready is not None:
                return not_ready
            body = await _json_body(request)
            if not isinstance(body, dict):
                return _err(400, "请求体要写成 {\"items\": [{\"id\": 预设, \"key\": 可选密钥}]}")
            from .. import search_binding, search_presets_web

            try:
                names = search_presets_web.setup(svc.store, svc.get_settings(), body.get("items"))
            except ValueError as e:
                return _err(400, str(e))
            for name in names:
                try:
                    await svc.reload_mcp(name)
                except Exception:
                    logger.exception("引导打开预设搜索服务 %s 后连接出错", name)
            out = _presets_payload()
            out["search"] = search_binding.search_view(svc.store, svc.get_settings(), _runtime_of)
            return web.json_response(out)

        app.router.add_post("/api/extensions/presets/{id}", self._write(_preset_activate))
        app.router.add_post("/api/extensions/presets-setup", self._write(_presets_setup))

        @get("/api/extensions/skills/{name}")
        async def _skill_get(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            view, not_found = _skill_view_or_404(str(request.match_info["name"]))
            if not_found is not None:
                return not_found
            return web.json_response(view)

        @post("/api/extensions/skills")
        async def _skill_create(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            from .. import skills_web

            try:
                view = skills_web.create(svc.get_settings().data_dir, svc.store, body)
            except ValueError as e:
                return _err(400, str(e))
            except FileExistsError:
                return _err(409, f"已经有叫「{str(body.get('name') or '')}」的 skill 了（重名）")
            return web.json_response(view)

        async def _skill_update(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            from .. import skills_web

            name = str(request.match_info["name"])
            try:
                view = skills_web.update(svc.get_settings().data_dir, svc.store, name, body)
            except KeyError:
                return _err(404, "没有这个 skill")
            except PermissionError as e:
                return _err(409, str(e))
            except ValueError as e:
                return _err(400, str(e))
            return web.json_response(view)

        async def _skill_delete(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            from .. import skills_web

            name = str(request.match_info["name"])
            try:
                skills_web.delete(svc.get_settings().data_dir, svc.store, name)
            except KeyError:
                return _err(404, "没有这个 skill")
            except PermissionError as e:
                return _err(409, str(e))
            except ValueError as e:
                return _err(400, str(e))
            return web.json_response({"ok": True})

        # ---------- skill 开关（POST /api/extensions/skills/{name}/toggle） ----------

        async def _skill_toggle(request: web.Request) -> web.Response:
            """开关 skill（任何来源，包括 builtin）——只存 kv["extensions.skills.disabled"] 名单。

            search-<preset> 的 effective 态除了这个手动开关，还看「对应搜索服务开没开」
            （至少一条 preset 认得出的 MCP enabled）；这条只改手动开关。
            """
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            enabled_v = body.get("enabled")
            if not isinstance(enabled_v, bool):
                return _err(400, "enabled 要是 true / false")
            from .. import skills_web

            name = str(request.match_info["name"])
            try:
                skills_web.toggle(svc.get_settings().data_dir, svc.store, name, enabled_v)
            except KeyError:
                return _err(404, "没有这个 skill")
            except ValueError as e:
                return _err(400, str(e))
            view, not_found = _skill_view_or_404(name)
            if not_found is not None:
                return not_found
            return web.json_response(view)

        # ---------- skill zip 上传（POST /api/extensions/skills/upload） ----------
        #
        # 请求体两种都支持：
        # 1) 原始字节：Content-Type: application/zip（或 application/octet-stream），文件名走
        #    X-Filename 头（前端 encodeURIComponent 过）；
        # 2) multipart：字段名 file（带 filename）。
        # ?replace=1 同名替换；只管理员；过同源检查（POST）。

        async def _skill_upload(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            from .. import skills_web

            filename_raw = str(request.headers.get("X-Filename") or "").strip()
            body_bytes: bytes | None = None
            # 1) 优先试 raw body（application/zip / octet-stream）
            ct = str(request.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ct in ("application/zip", "application/octet-stream", "application/x-zip-compressed"):
                body_bytes = await request.read()
            else:
                # 2) multipart
                try:
                    data = await request.post()
                    field = data.get("file") if data is not None else None
                    if field is not None and hasattr(field, "file"):
                        body_bytes = field.file.read()
                        filename_raw = str(getattr(field, "filename", "") or filename_raw)
                except Exception:
                    body_bytes = None
            if not body_bytes:
                return _err(400, "请求体不是 zip（Content-Type: application/zip 或 multipart 字段 file）")
            replace = str(request.query.get("replace") or "").strip() in ("1", "true", "yes")
            try:
                view = skills_web.install_zip(
                    svc.store, svc.get_settings().data_dir, body_bytes,
                    filename=filename_raw, replace=replace,
                )
            except FileExistsError as e:
                return _err(409, str(e))
            except ValueError as e:
                return _err(400, str(e))
            # 装完通知面板刷一下 list（Skills 每次 start 时枚举，不用额外重载——
            # 网页的管理看的是 skills_web / skills，只要目录变了，下次 skills.list 就是新的）
            return web.json_response(view)

        app.router.add_post("/api/extensions/skills/upload", self._write(_skill_upload))
        app.router.add_post("/api/extensions/skills/{name}/toggle", self._write(_skill_toggle))

        app.router.add_route("PUT", "/api/extensions/mcp/{name}", self._write(_mcp_update))
        app.router.add_route("DELETE", "/api/extensions/mcp/{name}", self._write(_mcp_delete))
        app.router.add_route("PUT", "/api/extensions/skills/{name}", self._write(_skill_update))
        app.router.add_route("DELETE", "/api/extensions/skills/{name}", self._write(_skill_delete))

        async def _feeds_pref_get(request: web.Request) -> web.Response:
            """资讯偏好：GET 对所有人可见（含群友 / 本群群管理员，只读）。"""
            ident = self._identify(request)
            if ident.role not in ("admin", "member", "group_admin"):
                return _err(401, "先登录管理员，或用群链接打开")
            resolved = self._resolve_ref(request.match_info["gid"])
            if resolved is None:
                return _err(404, "没有这个群")
            if _wrong_group(ident, resolved):
                return _err(403, "只能看自己群的内容")
            not_ready = _m2_ready(svc.feeds)
            if not_ready is not None:
                return not_ready
            return web.json_response({"text": svc.feeds.pref(resolved)})

        async def _feeds_pref_put(request: web.Request) -> web.Response:
            """资讯偏好：PUT 管理员或本群群管理员。"""
            resolved = self._resolve_ref(request.match_info["gid"])
            if resolved is None:
                return _err(404, "没有这个群")
            forbid = self._require_group_admin(request, resolved)
            if forbid is not None:
                return forbid
            not_ready = _m2_ready(svc.feeds)
            if not_ready is not None:
                return not_ready
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            text = svc.feeds.set_pref(resolved, str(body.get("text") or ""))
            return web.json_response({"text": text})

        # ---------- 口味小结 / 优质来源（taste.py / source_stats.py；管理员或本群群管理员） ----------

        async def _taste_get(request: web.Request) -> web.Response:
            resolved = self._resolve_ref(request.match_info["gid"])
            if resolved is None:
                return _err(404, "没有这个群")
            forbid = self._require_group_admin(request, resolved)
            if forbid is not None:
                return forbid
            from .. import taste

            return web.json_response(taste.view(svc.store, resolved))

        async def _taste_put(request: web.Request) -> web.Response:
            """管理员手改口味小结（空 = 清掉，恢复自动）；7 天内自动总结不覆盖。"""
            resolved = self._resolve_ref(request.match_info["gid"])
            if resolved is None:
                return _err(404, "没有这个群")
            forbid = self._require_group_admin(request, resolved)
            if forbid is not None:
                return forbid
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            from .. import taste

            text = str(body.get("text") or "")
            scrubbed = svc.feeds._scrub_item_text(resolved, text) if (svc.feeds is not None and text) else text
            if text and scrubbed is None:
                return _err(400, "这段话里有关注成员的个人信息，口味小结只写群整体的喜好")
            return web.json_response(taste.set_manual(svc.store, resolved, text, clock.now()))

        def _blocked_for(gid: str) -> list[str]:
            try:
                settings = svc.get_settings()
                from ..feeds import blocked_domains_effective

                return blocked_domains_effective(svc.store, tuple(getattr(settings.feeds, "blocked_domains", ()) or ()))
            except Exception:
                return []

        async def _trusted_get(request: web.Request) -> web.Response:
            resolved = self._resolve_ref(request.match_info["gid"])
            if resolved is None:
                return _err(404, "没有这个群")
            forbid = self._require_group_admin(request, resolved)
            if forbid is not None:
                return forbid
            from .. import source_stats

            return web.json_response(source_stats.view(svc.store, resolved, clock.now(), blocked=_blocked_for(resolved)))

        async def _trusted_post(request: web.Request) -> web.Response:
            """{domain, removed: true|false}：把某个域名移出 / 放回本群优质来源。"""
            resolved = self._resolve_ref(request.match_info["gid"])
            if resolved is None:
                return _err(404, "没有这个群")
            forbid = self._require_group_admin(request, resolved)
            if forbid is not None:
                return forbid
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            from .. import source_stats

            try:
                source_stats.set_removed(svc.store, resolved, str(body.get("domain") or ""), bool(body.get("removed")))
            except ValueError as e:
                return _err(400, str(e))
            return web.json_response(source_stats.view(svc.store, resolved, clock.now(), blocked=_blocked_for(resolved)))

        async def _go(request: web.Request) -> web.Response:
            """GET /go/{item_id}?c=浏览器标识：网页上点开资讯原文——记一次点击（news_feedback），
            再 302 到库里存的原链接（不接受外来链接，防开放跳转）。只能点自己看得到的群的条目。"""
            ident = self._identify(request)
            if ident.role == "none":
                # 群友是用群链接看的：链接跳转带不了请求头，群链接码放在 g 参数里
                tok_gid = views.group_id_by_token(svc, request.query.get("g", ""))
                if tok_gid is not None:
                    ident = Identity(role="member", group_id=tok_gid)
            if ident.role not in ("admin", "member", "group_admin"):
                return _err(401, "先登录管理员，或用群链接打开")
            try:
                item_id = int(request.match_info["id"])
            except (ValueError, TypeError):
                return _err(404, "这条资讯不存在")
            gid = _group_of("news", item_id)
            if gid is None:
                return _err(404, "这条资讯不存在")
            if _wrong_group(ident, gid):
                return _err(403, "只能看自己群的内容")
            personal_deny = _personal_deny(ident, "news", item_id)
            if personal_deny is not None:
                return personal_deny
            from .. import news_feedback

            url = news_feedback.click(svc.store, gid, item_id, client=request.query.get("c", ""), now=clock.now())
            if not url:
                return _err(404, "这条资讯不存在")
            raise web.HTTPFound(url)

        app.router.add_get("/go/{id}", _go)
        app.router.add_get("/api/groups/{gid}/taste", _taste_get)
        app.router.add_route("PUT", "/api/groups/{gid}/taste", self._write(_taste_put))
        app.router.add_get("/api/groups/{gid}/trusted-sources", _trusted_get)
        app.router.add_post("/api/groups/{gid}/trusted-sources", self._write(_trusted_post))

        def _card_push_view(gid: str) -> dict:
            from .. import card_push as _cp

            return _cp.web_view(svc, gid)

        async def _card_push_get(request: web.Request) -> web.Response:
            """资讯卡片 / 构想提一嘴的每群开关 + 今天发了几次 + 最近几条记录（管理员 / 本群群管理员）。"""
            resolved = self._resolve_ref(request.match_info["gid"])
            if resolved is None:
                ident = self._identify(request)
                return _err(404 if ident.role == "admin" else 403, "没有这个群")
            forbid = self._require_group_admin(request, resolved)
            if forbid is not None:
                return forbid
            return web.json_response(_card_push_view(resolved))

        async def _card_push_put(request: web.Request) -> web.Response:
            """改开关 / 条数 / 每日上限：{"news_card_enabled": true, "news_card_count": 2, ...}。"""
            resolved = self._resolve_ref(request.match_info["gid"])
            if resolved is None:
                return _err(404, "没有这个群")
            forbid = self._require_group_admin(request, resolved)
            if forbid is not None:
                return forbid
            body = await _json_body(request)
            if not isinstance(body, dict):
                return _err(400, "请求体不是 JSON")
            from .. import card_push as _cp

            try:
                _cp.set_config(svc.store, resolved, body)
            except ValueError as e:
                return _err(400, str(e))
            logger.info("群 %s 的卡片 / 提一嘴设置改成：%s", resolved, sorted(body))
            return web.json_response(_card_push_view(resolved))

        async def _news_run(request: web.Request) -> web.Response:
            """管理员 / 本群群管理员「现在就备一批」：后台开跑，立刻返回 {"started", "reason", "run_id"}。

            开工前提（画像成形 / news 专岗启停 / 模型就绪）不过 → 409 中文原因，不会再「回了
            开始却立刻静默退出」；跑完的状态读 GET 同路径（A11）。
            """
            resolved = self._resolve_ref(request.match_info["gid"])
            if resolved is None:
                return _err(404, "没有这个群")
            forbid = self._require_group_admin(request, resolved)
            if forbid is not None:
                return forbid
            fn = getattr(svc, "run_news_now", None)
            if fn is None:
                return _err(503, "资讯模块没开")
            out = fn(resolved)
            if not out.get("started"):
                return _err(409, str(out.get("reason") or "现在开不了"))
            return web.json_response(out)

        async def _news_run_get(request: web.Request) -> web.Response:
            """这次手动备料跑到哪了（运行记录）：管理员 / 本群群管理员，和 POST 同权限。

            `{"run_id","state":"none|running|done|skipped|failed","started_ts","ended_ts",
            "reason","items"}`；服务器重启后残留的 running 会被报成 failed「中断了」。
            """
            resolved = self._resolve_ref(request.match_info["gid"])
            if resolved is None:
                return _err(404, "没有这个群")
            forbid = self._require_group_admin(request, resolved)
            if forbid is not None:
                return forbid
            fn = getattr(svc, "news_manual_run_status", None)
            if fn is None:
                return _err(503, "资讯模块没开")
            try:
                out = fn(resolved)
            except Exception:
                logger.exception("读手动备料运行记录出错（群 %s）", resolved)
                return _err(500, "服务器出错了")
            return web.json_response(out)

        async def _ideas_run(request: web.Request) -> web.Response:
            """管理员 / 本群群管理员「现在出一个构想」：后台开跑，立刻返回 {"started", "reason"}。"""
            resolved = self._resolve_ref(request.match_info["gid"])
            if resolved is None:
                return _err(404, "没有这个群")
            forbid = self._require_group_admin(request, resolved)
            if forbid is not None:
                return forbid
            fn = getattr(svc, "make_idea_now", None)
            if fn is None:
                return _err(503, "构想模块没开")
            out = fn(resolved)
            if not out.get("started"):
                return _err(409, str(out.get("reason") or "现在开不了"))
            return web.json_response(out)

        app.router.add_get("/api/groups/{gid}/feeds-pref", _feeds_pref_get)
        app.router.add_get("/api/groups/{gid}/card-push", _card_push_get)
        app.router.add_route("PUT", "/api/groups/{gid}/card-push", self._write(_card_push_put))
        app.router.add_post("/api/groups/{gid}/ideas/run", self._write(_ideas_run))
        app.router.add_route("PUT", "/api/groups/{gid}/feeds-pref", self._write(_feeds_pref_put))
        app.router.add_post("/api/groups/{gid}/news/run", self._write(_news_run))
        app.router.add_get("/api/groups/{gid}/news/run", _news_run_get)

        async def _news_rate(request: web.Request) -> web.Response:
            """资讯评价（news_rating）：群友（本群）或管理员给一条资讯挑理由 + 可选一句话。

            body: {"client": 浏览器随机标识, "reasons": ["old"|"useless"|"low"|"offtopic"|"dup"|"wrong"…],
            "note": 一句话}；同一浏览器再评 = 改评价，理由和一句话都空 = 撤回。
            个人向资讯（给某位关注成员的）群友不能评（和反馈一样）。"""
            ident, deny = await _ident_for_group_action(request, need_admin=False)
            if deny is not None:
                return deny
            not_ready = _m2_ready(svc.feeds)
            if not_ready is not None:
                return not_ready
            try:
                item_id = int(request.match_info["id"])
            except (ValueError, TypeError):
                return _err(400, "id 要是数字")
            gid = _group_of("news", item_id)
            if gid is None:
                return _err(404, "这条资讯不存在")
            if _wrong_group(ident, gid):
                return _err(403, "只能管自己群的内容")
            personal_deny = _personal_deny(ident, "news", item_id)
            if personal_deny is not None:
                return personal_deny
            try:
                body = await request.json()
            except Exception:
                return _err(400, "要 JSON")
            if not isinstance(body, dict):
                return _err(400, "要 JSON 对象")
            from .. import news_rating

            try:
                out = news_rating.rate(
                    svc.store, gid, item_id,
                    client=body.get("client"), reasons=body.get("reasons") or [],
                    note=body.get("note") or "", now=clock.now(),
                )
            except KeyError:
                return _err(404, "这条资讯不存在")
            except ValueError as e:
                return _err(400, str(e))
            return web.json_response(out)

        app.router.add_post("/api/news/{id}/rate", self._write(_news_rate))

        async def _news_viz(request: web.Request) -> web.Response:
            """资讯图解（news_viz）：核对过的图解整页（带 CSP，前端放进沙箱 iframe）。
            本群群友 / 群管理员 / 总管理员都能看；个人向资讯不做图解。"""
            ident, deny = await _ident_for_group_action(request, need_admin=False)
            if deny is not None:
                return deny
            try:
                item_id = int(request.match_info["id"])
            except (ValueError, TypeError):
                return _err(400, "id 要是数字")
            gid = _group_of("news", item_id)
            if gid is None:
                return _err(404, "这条资讯不存在")
            if _wrong_group(ident, gid):
                return _err(403, "只能看自己群的内容")
            from .. import news_viz

            doc = news_viz.html_for(svc.store, gid, item_id) if svc.store is not None else None
            if doc is None:
                return _err(404, "这条资讯没有图解")
            return web.json_response({"html": doc}, headers={"Cache-Control": "no-store"})

        app.router.add_get("/api/news/{id}/viz", _news_viz)

        async def _news_mention_to_member(request: web.Request) -> web.Response:
            """管理员在网页点「在群里提给他」：往本群可提起清单加一句（ttl 6 小时）。

            只加备忘，不直接发群消息；文字不含画像细节（模块里过 privacy.scrub）。
            只管理员（群友 403、匿名 401）。返回 {"ok": true}。
            """
            forbid = self._require_admin(request)
            if forbid is not None:
                if self._identify(request).role == "group_admin":
                    return _err(403, _PERSONAL_READONLY)
                return forbid
            personal = getattr(svc, "personal", None)
            mentions = getattr(svc, "mentions", None)
            if personal is None or mentions is None:
                return _err(503, "这个功能还没开")
            try:
                item_id = int(request.match_info["id"])
            except (ValueError, TypeError):
                return _err(400, "id 要是数字")
            gid = _group_of("news", item_id)
            if gid is None:
                return _err(404, "这条资讯不存在")
            try:
                out = personal.mention_to_member(gid, item_id, mentions=mentions)
            except KeyError:
                return _err(404, "这条不是个人向资讯")
            return web.json_response(out)

        app.router.add_post(
            "/api/news/{id}/mention-to-member", self._write(_news_mention_to_member)
        )

        # ---------- RSS 资讯源（rss.py；每群 ≤20；写接口过同源检查） ----------

        def _rss_resolve_gid(request: web.Request) -> tuple[str | None, web.Response | None]:
            gid = self._resolve_ref(str(request.match_info["gid"]))
            if gid is None:
                return None, _err(404, "没有这个群")
            return gid, None

        def _rss_guard(request: web.Request, gid: str) -> web.Response | None:
            """RSS 增删：管理员或本群群管理员。"""
            return self._require_group_admin(request, gid)

        @get("/api/groups/{gid}/rss")
        async def _rss_list(request: web.Request) -> web.Response:
            gid, deny = _rss_resolve_gid(request)
            if deny is not None:
                return deny
            forbid = _rss_guard(request, gid)
            if forbid is not None:
                return forbid
            from .. import rss as _rss

            return web.json_response({"rss": _rss.list_feeds(svc.store, gid)})

        @post("/api/groups/{gid}/rss")
        async def _rss_add(request: web.Request) -> web.Response:
            gid, deny = _rss_resolve_gid(request)
            if deny is not None:
                return deny
            forbid = _rss_guard(request, gid)
            if forbid is not None:
                return forbid
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            url = str(body.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                return _err(400, "RSS 源地址必须以 http:// 或 https:// 开头")
            from .. import rss as _rss

            # 先试取一次，成功才保存（title 用 feed 的；取不到 / 解析不了 → 400 中文原因）
            settings = svc.get_settings()
            lookback = int(getattr(getattr(settings, "feeds", None), "lookback_days", 14) or 14)
            out = await _rss.fetch_feed_source(
                url,
                transport=getattr(svc, "rss_transport", None),
                lookback_days=lookback,
                now=clock.now(),
                limit=1,
            )
            if out.get("error"):
                return _err(400, f"先试取失败：{out['error']}")
            try:
                entry = _rss.add_feed(
                    svc.store, gid, url=url, title=str(out.get("title") or ""),
                    feed_id="", now=clock.now(),
                )
            except _rss.RssError as e:
                return _err(400, str(e))
            return web.json_response(
                {"id": entry["id"], "url": entry["url"], "title": entry["title"], "items_count": len(out.get("items") or [])}
            )

        async def _rss_delete(request: web.Request) -> web.Response:
            gid, deny = _rss_resolve_gid(request)
            if deny is not None:
                return deny
            forbid = _rss_guard(request, gid)
            if forbid is not None:
                return forbid
            from .. import rss as _rss

            removed = _rss.remove_feed(svc.store, gid, str(request.match_info["id"]))
            if removed is None:
                return _err(404, "没有这个 RSS 源")
            return web.json_response({"ok": True})

        @post("/api/groups/{gid}/rss/{id}/toggle")
        async def _rss_toggle(request: web.Request) -> web.Response:
            gid, deny = _rss_resolve_gid(request)
            if deny is not None:
                return deny
            forbid = _rss_guard(request, gid)
            if forbid is not None:
                return forbid
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            enabled_v = body.get("enabled")
            if not isinstance(enabled_v, bool):
                return _err(400, "enabled 要是 true / false")
            from .. import rss as _rss

            try:
                entry = _rss.toggle_feed(svc.store, gid, str(request.match_info["id"]), enabled=enabled_v)
            except _rss.RssError as e:
                return _err(404, str(e))
            return web.json_response(entry)

        app.router.add_route("DELETE", "/api/groups/{gid}/rss/{id}", self._write(_rss_delete))

        # ---------- M3：批准 / 任务 / 目标（docs/07 §9.2 M3 段、§9.3 任务详情） ----------

        def _m3_ready(module: Any) -> web.Response | None:
            if module is None:
                return _err(503, "这个功能还没开")
            return None

        def _merge_delivery(detail: dict, tid: str) -> None:
            """任务详情合并 delivery.delivery_records 和 undelivered（模块没开就不动）。"""
            delivery = getattr(svc, "delivery", None)
            if delivery is None:
                return
            try:
                detail["delivery"] = delivery.delivery_records(tid)
                detail["undelivered"] = bool(delivery.undelivered(tid))
            except Exception:
                logger.exception("拼任务交付记录失败（%s）", tid)

        def _merge_auto_review(detail: dict, tid: str) -> None:
            """任务详情合并批准信息：approved_by（批准人）+ auto_reason（自动审核的一句话理由）。

            自动审核通过的任务，前端照着 auto_reason 显示「自动审核通过：<理由>」；
            人批的任务 auto_reason 是空串；免批直接落地的任务两个字段都不给。
            （群友也看得到——这条活本来是当着他的面派的，说明是谁批的没有隐私问题。）
            """
            appr = getattr(svc, "approvals", None)
            if appr is None or not isinstance(detail, dict):
                return
            try:
                info = appr.auto_info_by_task([str(tid)]).get(str(tid))
            except Exception:
                logger.exception("拼任务批准信息失败（%s）", tid)
                return
            if info:
                detail.update(info)

        def _merge_link_check(detail: dict, tid: str) -> None:
            """任务详情合并验收引用核对：coordinator 存的 kv["task.link_check.<任务ID>"]。

            给 `{links, unopened, unopened_urls}`；没做过 / 老任务 → null（前端据此不画这块）。
            群友版、管理员版都带（只是「引用了几个链接、几个没打开核实过」）。
            """
            store = getattr(svc, "store", None)
            if store is None or not isinstance(detail, dict):
                return
            saved = None
            try:
                saved = store.kv_get(f"task.link_check.{tid}")
            except Exception:
                logger.exception("读引用核对记录失败（%s）", tid)
            if isinstance(saved, dict):
                def _num(key: str) -> int:
                    try:
                        return int(saved.get(key) or 0)
                    except (TypeError, ValueError):
                        return 0

                urls = saved.get("unopened_urls")
                detail["link_check"] = {
                    "links": _num("links"),
                    "unopened": _num("unopened"),
                    "unopened_urls": [str(u) for u in urls] if isinstance(urls, list) else [],
                }
            else:
                detail["link_check"] = None

        def _start_approved_task(res: Any) -> None:
            data = res if isinstance(res, dict) else {}
            tids = [str(t) for t in (data.get("task_ids") or []) if str(t)]
            tid = str(data.get("task_id") or "")
            if tid and tid not in tids:
                tids.insert(0, tid)
            if not tids:
                return
            starter = getattr(svc, "spawn_run_task", None)
            if not callable(starter):
                return
            for one in tids:
                try:
                    starter(one)
                except Exception:
                    logger.exception("批准后 spawn run_task 出错（%s）", one)

        def _stop_running_task(svc_obj: Any, tid: str) -> None:
            """取消后把正在跑的子 agent 停掉（统一入口在 app.cancel_task_run）。"""
            stopper = getattr(svc_obj, "cancel_task_run", None)
            if callable(stopper):
                try:
                    stopper(tid)
                except Exception:
                    logger.exception("停任务 %s 的后台协程出错", tid)

        @get("/api/tasks/{id}")
        async def _task_detail(request: web.Request) -> web.Response:
            ident = self._identify(request)
            if ident.role not in ("admin", "member", "group_admin"):
                return _err(401, "先登录管理员，或用群链接打开")
            not_ready = _m3_ready(getattr(svc, "tasks", None))
            if not_ready is not None:
                return not_ready
            tid = request.match_info["id"]
            try:
                row = svc.tasks.get(tid)
            except Exception:
                row = None
            if row is None:
                return _err(404, "找不到这个任务")
            # 群友 / 群管理员只能看本群
            if _wrong_group(ident, str(row.get("group_id") or "")):
                return _err(403, "只能看自己群的内容")
            try:
                detail = svc.tasks.detail_view(tid, admin=(ident.role == "admin"))
            except KeyError:
                return _err(404, "找不到这个任务")
            _merge_delivery(detail, tid)
            _merge_auto_review(detail, tid)
            _merge_link_check(detail, tid)
            if ident.role != "admin":
                # detail_view(admin=False) 已经不给了，这里再断言一次
                # （红线：群友看不到 env / timeline / tokens / workspace / source / request_id / requester_id）
                for key in ("env", "timeline", "tokens", "workspace", "source", "request_id", "requester_id"):
                    detail.pop(key, None)
            return web.json_response(detail)

        def _request_decide(op: str) -> Handler:
            async def _handler(request: web.Request) -> web.Response:
                ident = self._identify(request)
                if ident.role not in ("admin", "group_admin"):
                    forbid = self._require_admin(request)
                    return forbid
                not_ready = _m3_ready(getattr(svc, "approvals", None))
                if not_ready is not None:
                    return not_ready
                rid = request.match_info["id"]
                if ident.role == "group_admin":
                    # 请求参数是 R-xx：先查出它属于哪个群，再判本群群管理员能不能动
                    try:
                        req_gid = svc.approvals.group_of(rid)
                    except Exception:
                        logger.exception("查请求归属出错（%s）", rid)
                        req_gid = None
                    if req_gid is None:
                        return _err(404, "找不到这个请求")
                    forbid = self._require_group_admin_ident(ident, req_gid)
                    if forbid is not None:
                        return forbid
                by = "网页管理员" if ident.role == "admin" else "群管理员（网页）"
                try:
                    if op == "approve":
                        res = svc.approvals.approve(rid, by=by)
                    else:
                        res = svc.approvals.reject(rid, by=by)
                except KeyError:
                    return _err(404, "找不到这个请求")
                except ValueError as e:
                    return _err(409, str(e))
                if op == "approve":
                    # 批准后任务已落地 → 后台开工（同一任务不并发由 app 保证）
                    _start_approved_task(res)
                return web.json_response(res)

            return _handler

        def _task_op_detail(tid: str) -> dict:
            detail = svc.tasks.detail_view(tid, admin=True)
            _merge_delivery(detail, tid)
            return detail

        async def _task_op(request: web.Request, op: str) -> web.Response:
            ident = self._identify(request)
            if ident.role not in ("admin", "group_admin"):
                forbid = self._require_admin(request)
                return forbid
            not_ready = _m3_ready(getattr(svc, "tasks", None))
            if not_ready is not None:
                return not_ready
            tid = request.match_info["id"]
            try:
                row = svc.tasks.get(tid)
            except Exception:
                row = None
            if row is None:
                return _err(404, "找不到这个任务")
            forbid = self._require_group_admin_ident(ident, str(row.get("group_id") or ""))
            if forbid is not None:
                return forbid
            status = str(row.get("status") or "")
            redeliver_warning = ""
            try:
                if op == "pause":
                    svc.tasks.transition(tid, "paused", reason="网页暂停")
                elif op == "resume":
                    if status not in ("paused", "shelved"):
                        return _err(409, f"任务现在是「{status}」，不用恢复")
                    svc.tasks.transition(tid, "queued", reason="网页恢复")
                    # 恢复不立刻 spawn：后台循环到点会把 queued 捞起来（同一任务不并发）
                elif op == "cancel":
                    svc.tasks.transition(tid, "cancelled", reason="网页取消")
                    _stop_running_task(svc, tid)
                elif op == "retry":
                    if status != "failed":
                        return _err(409, "只有失败的任务能重试")
                    svc.tasks.transition(tid, "queued", reason="网页重试")
                    _start_approved_task({"task_id": tid})
                elif op == "redeliver":
                    if not svc.get_settings().is_served(str(row.get("group_id") or "")):
                        return _err(409, "这个群已不在服务列表，不能重发")
                    redeliver_result = self._redeliver_failed(svc, tid)
                    if status == "completed" and getattr(svc, "delivery", None) is not None:
                        # 入队前就失败的任务没有 failed 行可 retry；由交付层重新核验成品，
                        # 只补首条成品记录，不重传已经入队或结果不明的文件。
                        await svc.delivery.reenqueue_missing(tid, svc.env)
                    redeliver_warning = str(redeliver_result.get("warning") or "")
                else:
                    return _err(404, "没有这个操作")
            except KeyError:
                return _err(404, "找不到这个任务")
            except ValueError as e:
                return _err(409, str(e))
            try:
                detail = _task_op_detail(tid)
                if redeliver_warning:
                    detail["redeliver_warning"] = redeliver_warning  # M1：网页提示先去群里确认
                return web.json_response(detail)
            except KeyError:
                return _err(404, "找不到这个任务")

        async def _goal_op(request: web.Request, op: str) -> web.Response:
            ident = self._identify(request)
            if ident.role not in ("admin", "group_admin"):
                forbid = self._require_admin(request)
                return forbid
            not_ready = _m3_ready(getattr(svc, "goals", None))
            if not_ready is not None:
                return not_ready
            gid_param = request.match_info["id"]
            try:
                goal_row = svc.goals.get(gid_param)
            except Exception:
                goal_row = None
            if goal_row is None:
                return _err(404, "找不到这个目标")
            forbid = self._require_group_admin_ident(ident, str(goal_row.get("group_id") or ""))
            if forbid is not None:
                return forbid
            try:
                if op == "pause":
                    svc.goals.pause(gid_param)
                elif op == "resume":
                    svc.goals.resume(gid_param)
                elif op == "cancel":
                    svc.goals.cancel(gid_param)
                else:
                    return _err(404, "没有这个操作")
            except KeyError:
                return _err(404, "找不到这个目标")
            except ValueError as e:
                return _err(409, str(e))
            return web.json_response({"ok": True})

        app.router.add_post("/api/requests/{id}/approve", self._write(_request_decide("approve")))
        app.router.add_post("/api/requests/{id}/reject", self._write(_request_decide("reject")))
        for _op in ("pause", "resume", "cancel", "retry", "redeliver"):
            app.router.add_post(
                f"/api/tasks/{{id}}/{_op}",
                self._write(lambda req, op=_op: _task_op(req, op)),
            )
        for _op in ("pause", "resume", "cancel"):
            app.router.add_post(
                f"/api/goals/{{id}}/{_op}",
                self._write(lambda req, op=_op: _goal_op(req, op)),
            )

        # ---------- 身份与工作记忆（identity.py；只管理员；docs/02「身份与工作记忆」） ----------

        def _identity_ready() -> Any:
            ident = getattr(svc, "identity", None)
            if ident is None:
                return None, _err(503, "身份与工作记忆还没开")
            return ident, None

        @get("/api/identity")
        async def _identity_get(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            ident, not_ready = _identity_ready()
            if not_ready is not None:
                return not_ready
            limits = ident.limits
            return web.json_response(
                {
                    "soul": ident.read("soul"),
                    "agents": ident.read("agents"),
                    "memory": ident.read("memory"),
                    "group_memory": ident.group_memory_map(),
                    "limits": dict(limits),
                }
            )

        async def _identity_put(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            ident, not_ready = _identity_ready()
            if not_ready is not None:
                return not_ready
            kind = request.match_info["kind"]
            if kind not in ("soul", "agents", "memory"):
                return _err(404, "没有这个身份文件（只支持 soul / agents / memory）")
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            try:
                out = ident.write(kind, str(body.get("text") or ""))
            except ValueError as e:
                return _err(400, str(e))
            return web.json_response(out)

        app.router.add_route("PUT", "/api/identity/{kind}", self._write(_identity_put))

        @get("/api/identity/group-memory/{gid}")
        async def _identity_group_memory_get(request: web.Request) -> web.Response:
            """本群工作记忆（读）：总管理员或本群群管理员。"""
            gid = self._resolve_ref(str(request.match_info["gid"]))
            if gid is None:
                return _err(404, "没有这个群（只支持服务群）")
            forbid = self._require_group_admin(request, gid)
            if forbid is not None:
                return forbid
            ident, not_ready = _identity_ready()
            if not_ready is not None:
                return not_ready
            try:
                return web.json_response(ident.group_read(gid))
            except KeyError:
                return _err(404, "没有这个群（只支持服务群）")

        async def _identity_group_memory_put(request: web.Request) -> web.Response:
            gid = self._resolve_ref(str(request.match_info["gid"]))
            if gid is None:
                return _err(404, "没有这个群（只支持服务群）")
            forbid = self._require_group_admin(request, gid)
            if forbid is not None:
                return forbid
            ident, not_ready = _identity_ready()
            if not_ready is not None:
                return not_ready
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            try:
                out = ident.group_write(gid, str(body.get("text") or ""))
            except KeyError:
                return _err(404, "没有这个群（只支持服务群）")
            except ValueError as e:
                return _err(400, str(e))
            return web.json_response(out)

        app.router.add_route(
            "PUT", "/api/identity/group-memory/{gid}", self._write(_identity_group_memory_put)
        )

        async def _identity_soul_sync(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            ident, not_ready = _identity_ready()
            if not_ready is not None:
                return not_ready
            out = await ident.sync_soul_from_maibot()
            changed = bool(out.pop("preview_changed", False))
            return web.json_response({"soul": out, "preview_changed": changed})

        app.router.add_post("/api/identity/soul/sync", self._write(_identity_soul_sync))

        # ---------- 专岗 SOUL / AGENTS（专岗改版 3/4：identity.agents.<kind>） ----------
        # 路由（前端 settings/agents.js 已按这套写）：
        #   GET  /api/agents/{kind}/docs                    → {"soul":{text,updated_ts,synced_from_maibot},
        #                                                     "agents":{text,updated_ts},
        #                                                     "limits":{"soul":16384,"agents":16384}}
        #   PUT  /api/agents/{kind}/docs/soul   {"text"}    → 同 GET.soul 单项
        #   PUT  /api/agents/{kind}/docs/agents {"text"}    → 同 GET.agents 单项
        #   POST /api/agents/{kind}/docs/soul/sync          → 从 MaiBot 重同步（旧版存 .bak）
        #   POST /api/agents/{kind}/docs/agents/reset       → 换回 agent_presets 岗位预设
        # 都只总管理员；kind 不认识 404；超 16KB 400；svc.identity 缺位 503。

        @get("/api/agents/{kind}/docs")
        async def _agent_docs_get(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            ident, not_ready = _identity_ready()
            if not_ready is not None:
                return not_ready
            try:
                return web.json_response(ident.agent_read_all(str(request.match_info["kind"])))
            except KeyError as e:
                return _err(404, str(e.args[0] if e.args else "没有这个专岗"))

        async def _agent_docs_put(request: web.Request, which: str) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            ident, not_ready = _identity_ready()
            if not_ready is not None:
                return not_ready
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            try:
                out = ident.agent_write(
                    str(request.match_info["kind"]), which, str(body.get("text") or "")
                )
            except KeyError as e:
                return _err(404, str(e.args[0] if e.args else "没有这个专岗"))
            except ValueError as e:
                return _err(400, str(e))
            return web.json_response(out)

        async def _agent_docs_soul_put(request: web.Request) -> web.Response:
            return await _agent_docs_put(request, "soul")

        async def _agent_docs_agents_put(request: web.Request) -> web.Response:
            return await _agent_docs_put(request, "agents")

        app.router.add_route("PUT", "/api/agents/{kind}/docs/soul", self._write(_agent_docs_soul_put))
        app.router.add_route("PUT", "/api/agents/{kind}/docs/agents", self._write(_agent_docs_agents_put))

        async def _agent_docs_soul_sync(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            ident, not_ready = _identity_ready()
            if not_ready is not None:
                return not_ready
            try:
                out = await ident.agent_sync_soul(str(request.match_info["kind"]))
            except KeyError as e:
                return _err(404, str(e.args[0] if e.args else "没有这个专岗"))
            out = dict(out)
            out.pop("preview_changed", None)  # 这一个按前端约定不回 preview_changed
            return web.json_response(out)

        app.router.add_post("/api/agents/{kind}/docs/soul/sync", self._write(_agent_docs_soul_sync))

        async def _agent_docs_agents_reset(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            ident, not_ready = _identity_ready()
            if not_ready is not None:
                return not_ready
            try:
                out = ident.agent_reset_agents(str(request.match_info["kind"]))
            except KeyError as e:
                return _err(404, str(e.args[0] if e.args else "没有这个专岗"))
            except ValueError as e:
                return _err(400, str(e))
            return web.json_response(out)

        app.router.add_post("/api/agents/{kind}/docs/agents/reset", self._write(_agent_docs_agents_reset))

        # ---------- 专岗（agents.py；契约 /tmp/maiwork-specialists-contract.md A 部分） ----------
        #
        # 路由（结构严格按契约 §24-29）：
        #   GET /api/agents                          → {"profiles": [news/idea/goal/task]}（只总管理员）
        #   PUT /api/agents/{kind}                   → profile（只总管理员；同源 guard 在 _write）
        #   GET /api/groups/{gid}/agents             → {"group_id", "agents": [{kind,title,notes,learned,recent_handoffs}]}
        #                                             （总管理员或本群 group_admin；成员 403 / 匿名 401 / 非服务群 404）
        #   PUT /api/groups/{gid}/agents/{kind}/memory {notes} → memory（同上权限；task 拒 400）
        #   GET /api/groups/{gid}/agents/handoffs?kind=... → {"items": [...]}（同上权限）
        # svc.agents（Agents 实例）没就位 → 503；字段严格：未知键 / 坏类型 / 超长 → 400。

        def _agents_ready() -> tuple[Any, web.Response | None]:
            mod = getattr(svc, "agents", None)
            if mod is None:
                return None, _err(503, "这个功能还没开")
            return mod, None

        @get("/api/agents")
        async def _agents_profiles(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            mod, not_ready = _agents_ready()
            if not_ready is not None:
                return not_ready
            return web.json_response({"profiles": mod.profiles()})

        async def _agents_profile_put(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            mod, not_ready = _agents_ready()
            if not_ready is not None:
                return not_ready
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            try:
                profile = mod.update_profile(str(request.match_info["kind"]), body)
            except ValueError as e:
                return _err(400, str(e) or "这个值改不了")
            except Exception:
                logger.exception("改岗位配置出错")
                return _err(500, "服务器出错了")
            return web.json_response(profile)

        app.router.add_route("PUT", "/api/agents/{kind}", self._write(_agents_profile_put))

        # 自定义专岗（专岗改版 4/4）：POST 建、DELETE 删。
        #   POST   /api/agents {title}        → 200 profile（含 kind=c_<6 位>）；title 必填超长 400
        #   DELETE /api/agents/{kind}         → 200 {"ok":true,"kind":<被删的>}
        #                                       内建 400「内置岗位不能删」；不存在 404；
        #                                       有未结交接单 409
        # 都只总管理员；svc.identity 缺位也能建/删 kv（文档目录跳过，警告写 log）。

        async def _agents_custom_post(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            mod, not_ready = _agents_ready()
            if not_ready is not None:
                return not_ready
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            try:
                profile = mod.create_custom(str(body.get("title") or ""))
            except ValueError as e:
                return _err(400, str(e))
            # 建 kv 成功后落 identity 文档（SOUL 从 MaiBot 同步或空 + AGENTS=custom 预设
            # + main AGENTS.md 自动加一行 stub）。identity 缺位 → 只警告，kv 那份还在
            # （下轮启动/修复 identity 再补建也来得及——kind 已经有了）。
            ident = getattr(svc, "identity", None)
            if ident is None:
                logger.warning("identity 没就位：自定义专岗 %s 只建了 kv，文档目录跳过", profile.get("kind"))
            else:
                try:
                    await ident.agent_init_custom(str(profile["kind"]), str(profile["title"]))
                except Exception:
                    logger.exception("自定义专岗 %s 建文档目录出错（kv 那份在）", profile.get("kind"))
            return web.json_response(profile)

        app.router.add_post("/api/agents", self._write(_agents_custom_post))

        async def _agents_custom_delete(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            mod, not_ready = _agents_ready()
            if not_ready is not None:
                return not_ready
            kind = str(request.match_info["kind"])
            try:
                deleted = mod.delete_custom(kind)
            except ValueError as e:
                msg = str(e)
                if "内置" in msg or "不能删" in msg:
                    return _err(400, msg)
                if "交接" in msg or "进行中" in msg:
                    return _err(409, msg)
                return _err(404, msg or "没有这个专岗")
            # kv 删了；文档目录挪到 .trash（挪不动也只是警告，kv 那份已经删了）
            ident = getattr(svc, "identity", None)
            if ident is None:
                logger.warning("identity 没就位：删专岗 %s 只清了 kv，文档目录留着", deleted)
            else:
                try:
                    ident.agent_trash_custom(deleted)
                except Exception:
                    logger.exception("删专岗 %s 挪文档到 .trash 出错（kv 已删）", deleted)
            return web.json_response({"ok": True, "kind": deleted})

        app.router.add_delete("/api/agents/{kind}", self._write(_agents_custom_delete))

        def _agents_group_view(mod: Any, gid: str) -> dict[str, Any]:
            """本群岗位快照：内建四种 + 全部自定义专岗（task 只读交接记录——notes='' / learned=[]）。"""
            agents_out: list[dict[str, Any]] = []
            profiles = {p["kind"]: p for p in mod.profiles()}
            # 内建固定顺序在前；自定义按 kind 字典序在后（profiles() 也是这个顺序）
            all_kinds = ["news", "idea", "goal", "task"] + [
                k for k in mod._all_kinds() if k not in ("main", "news", "idea", "goal", "task")
            ]
            for kind in all_kinds:
                p = profiles.get(kind) or {"title": kind}
                if kind == "task":
                    mem = {"notes": "", "learned": []}
                else:
                    mem = mod.memory(gid, kind)
                agents_out.append(
                    {
                        "kind": kind,
                        "title": str(p.get("title") or kind),
                        "enabled": bool(p.get("enabled", True)),
                        "notes": mem["notes"],
                        "learned": mem["learned"],
                        "recent_handoffs": mod.handoffs(gid, kind=kind, limit=5),
                    }
                )
            return {"group_id": gid, "agents": agents_out}

        @get("/api/groups/{gid}/agents")
        async def _group_agents(request: web.Request) -> web.Response:
            gid = self._resolve_ref(str(request.match_info["gid"]))
            if gid is None or not svc.get_settings().is_served(gid):
                return _err(404, "没有这个群（只支持服务群）")
            forbid = self._require_group_admin(request, gid)
            if forbid is not None:
                return forbid
            mod, not_ready = _agents_ready()
            if not_ready is not None:
                return not_ready
            try:
                return web.json_response(_agents_group_view(mod, gid))
            except ValueError as e:
                return _err(404, str(e))
            except Exception:
                logger.exception("读本群岗位快照出错")
                return _err(500, "服务器出错了")

        async def _group_agents_memory_put(request: web.Request) -> web.Response:
            gid = self._resolve_ref(str(request.match_info["gid"]))
            if gid is None or not svc.get_settings().is_served(gid):
                return _err(404, "没有这个群（只支持服务群）")
            forbid = self._require_group_admin(request, gid)
            if forbid is not None:
                return forbid
            mod, not_ready = _agents_ready()
            if not_ready is not None:
                return not_ready
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            notes = body.get("notes")
            if not isinstance(notes, str):
                return _err(400, "notes 要是字符串")
            kind = str(request.match_info["kind"])
            try:
                mem = mod.set_notes(gid, kind, notes)
            except ValueError as e:
                return _err(400, str(e) or "这个值改不了")
            except Exception:
                logger.exception("写岗位工作册出错")
                return _err(500, "服务器出错了")
            return web.json_response(mem)

        app.router.add_route(
            "PUT", "/api/groups/{gid}/agents/{kind}/memory", self._write(_group_agents_memory_put)
        )

        @get("/api/groups/{gid}/agents/handoffs")
        async def _group_agents_handoffs(request: web.Request) -> web.Response:
            gid = self._resolve_ref(str(request.match_info["gid"]))
            if gid is None or not svc.get_settings().is_served(gid):
                return _err(404, "没有这个群（只支持服务群）")
            forbid = self._require_group_admin(request, gid)
            if forbid is not None:
                return forbid
            mod, not_ready = _agents_ready()
            if not_ready is not None:
                return not_ready
            kind = str(request.rel_url.query.get("kind") or "").strip() or None
            try:
                items = mod.handoffs(gid, kind=kind, limit=20)
            except ValueError as e:
                return _err(404, str(e))
            except Exception:
                logger.exception("读本群交接单出错")
                return _err(500, "服务器出错了")
            return web.json_response({"items": items})

        # ---------- 和 MaiWork 聊：管理员对话（只管理员；配套 static/js/chat.js 的对话页） ----------
        #
        # 返回结构按 static/js/chat.js 的「和 MaiWork 聊」页：
        #   GET    /api/chat                     → {"chats": [...]}
        #   POST   /api/chat                     → 新对话（单个 chat）
        #   PATCH  /api/chat/{id}                → 改标题 / 聚焦群 / 归档（单个 chat）
        #   GET    /api/chat/{id}?after=<msg id> → {"chat", "running", "messages", "pending"}
        #   POST   /api/chat/{id}/messages       → 202 {"accepted": true, "user_message_id": n}
        #   POST   /api/chat/pending/{pid}       → {"status", "result"}
        # 模块没开 503；对话 / 确认项不存在 404；发消息 / 确认时上一句还在跑 409、小票已处理过 409；
        # 空文本、非法 id 400。
        # admin_chat 的方法同步异步都认（跑出来是 awaitable 就 await）；「找不到 / 群不合法」
        # 一律 ValueError（ChatBusy 是它的子类），server.py 不硬依赖 admin_chat 模块，按类名认忙。
        # 带对话内容的响应出网页前整份过一遍密钥遮罩（消息和工具结果里有可能抄到密钥）；
        # 发消息的 202 只回 id，不带内容。

        async def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
            out = fn(*args, **kwargs)
            if inspect.isawaitable(out):
                out = await out
            return out

        def _is_busy_error(e: BaseException) -> bool:
            return type(e).__name__ == "ChatBusy"

        def _chat_ready() -> tuple[Any, web.Response | None]:
            mod = getattr(svc, "admin_chat", None)
            if mod is None:
                return None, _err(503, "这个功能还没开")
            return mod, None

        def _id_param(raw: Any, what: str) -> tuple[int, web.Response | None]:
            try:
                n = int(str(raw))
            except (TypeError, ValueError):
                return 0, _err(400, f"{what} id 要是数字")
            if n <= 0:
                return 0, _err(400, f"{what} id 要是数字")
            return n, None

        def _chat_exists(mod: Any, chat_id: int) -> bool:
            """出错后分「对话不存在（404）」还是「内容不合规（400）」：detail 能读出来就是后者。"""
            try:
                return isinstance(mod.detail(chat_id, after=0), dict)
            except Exception:
                return False

        def _running_of(mod: Any, chat_id: Any, explicit: Any = None) -> bool:
            """running 优先用 admin_chat 给的；没给就问 is_busy（前端靠它决定轮询和禁发送）。"""
            if explicit is not None:
                return bool(explicit)
            busy = getattr(mod, "is_busy", None)
            if not callable(busy):
                return False
            try:
                return bool(busy(chat_id))
            except Exception:
                logger.exception("查对话忙不忙出错（%s）", chat_id)
                return False

        def _chat_json(payload: Any) -> web.Response:
            try:
                payload = _redact_json(payload, _secret_list())
            except Exception:
                logger.exception("对话响应遮罩失败")
            return web.json_response(payload)

        def _pending_status(out: dict, approve: bool) -> str:
            """前端认 status：done / failed（拒绝不算 failed，别弹成红色报错）。"""
            status = out.get("status")
            if isinstance(status, str) and status:
                return status  # admin_chat 已经给标准状态就用它的
            if not approve:
                return "rejected"
            return "done" if out.get("ok") else "failed"

        def _pending_result(out: dict) -> str:
            for key in ("result", "error", "output"):
                value = out.get(key)
                if value:
                    return value if isinstance(value, str) else str(value)
            return ""

        @get("/api/chat")
        async def _chat_list(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            mod, not_ready = _chat_ready()
            if not_ready is not None:
                return not_ready
            chats = list(await _call(mod.list_chats) or [])
            if callable(getattr(mod, "is_busy", None)):
                # 侧栏的「正在回答」小圆点：admin_chat 没在列表里带 running 就补上
                chats = [
                    c if not isinstance(c, dict) or "running" in c else {**c, "running": _running_of(mod, c.get("id"))}
                    for c in chats
                ]
            return _chat_json({"chats": chats})

        @post("/api/chat")
        async def _chat_create(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            mod, not_ready = _chat_ready()
            if not_ready is not None:
                return not_ready
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            gid = str(body.get("group_id") or "").strip()
            if gid:
                resolved = self._resolve_ref(gid)  # 只认服务群（群号或链接码）
                if resolved is None:
                    return _err(404, "没有这个群")
                gid = resolved
            try:
                chat = await _call(mod.create, group_id=gid)
            except (KeyError, ValueError):
                return _err(404, "没有这个群")
            if not isinstance(chat, dict):
                return _err(500, "新对话没建起来")
            return _chat_json(chat)

        async def _chat_update(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            mod, not_ready = _chat_ready()
            if not_ready is not None:
                return not_ready
            chat_id, bad_id = _id_param(request.match_info["id"], "对话")
            if bad_id is not None:
                return bad_id
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            kwargs: dict[str, Any] = {}
            if body.get("title") is not None:
                title = str(body["title"]).strip()
                if not title:
                    return _err(400, "标题不能是空的")
                kwargs["title"] = title
            if body.get("group_id") is not None:
                raw_gid = str(body["group_id"] or "").strip()
                if raw_gid:
                    resolved = self._resolve_ref(raw_gid)
                    if resolved is None:
                        return _err(404, "没有这个群")
                    kwargs["group_id"] = resolved
                else:
                    kwargs["group_id"] = ""  # 空 = 不限群
            if body.get("archived") is not None:
                if not isinstance(body["archived"], bool):
                    return _err(400, "archived 要是 true / false")
                kwargs["archived"] = body["archived"]
            if not kwargs:
                return _err(400, "没有要改的字段")
            try:
                chat = await _call(mod.update, chat_id, **kwargs)
            except (KeyError, ValueError) as e:
                if _is_busy_error(e):
                    return _err(409, str(e))
                # 对话在、只是要改的值不合规（标题太长等）→ 400；对话不在 → 404
                return _err(400, str(e) or "这个值改不了") if _chat_exists(mod, chat_id) else _err(404, "找不到这个对话")
            if not isinstance(chat, dict):
                return _err(404, "找不到这个对话")
            return _chat_json(chat)

        @get("/api/chat/{id}")
        async def _chat_detail(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            mod, not_ready = _chat_ready()
            if not_ready is not None:
                return not_ready
            chat_id, bad_id = _id_param(request.match_info["id"], "对话")
            if bad_id is not None:
                return bad_id
            try:
                after = max(0, int(str(request.query.get("after") or "0")))
            except (TypeError, ValueError):
                after = 0
            try:
                out = await _call(mod.detail, chat_id, after=after)
            except (KeyError, ValueError):
                return _err(404, "找不到这个对话")
            if not isinstance(out, dict):
                return _err(404, "找不到这个对话")
            chat = out.get("chat")
            if not isinstance(chat, dict):
                # admin_chat 把 chat 字段直接铺在顶层也认；四个键一定齐，前端不用判 undefined
                chat = {k: v for k, v in out.items() if k not in ("messages", "pending", "running")}
            return _chat_json(
                {
                    "chat": chat,
                    "running": _running_of(mod, chat_id, out.get("running")),
                    "messages": list(out.get("messages") or []),
                    "pending": list(out.get("pending") or []),
                }
            )

        @post("/api/chat/{id}/messages")
        async def _chat_send(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            mod, not_ready = _chat_ready()
            if not_ready is not None:
                return not_ready
            chat_id, bad_id = _id_param(request.match_info["id"], "对话")
            if bad_id is not None:
                return bad_id
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            text = str(body.get("text") or "").strip()
            if not text:
                return _err(400, "说的话不能是空的")
            try:
                mid = await _call(mod.send, chat_id, text)
            except (KeyError, ValueError) as e:
                # 忙是 ChatBusy（ValueError 子类）→ 409；对话在、只是话不合规（太长）→ 400；否则 404
                if _is_busy_error(e):
                    return _err(409, str(e) or "上一句还在处理，等一下")
                if _chat_exists(mod, chat_id):
                    return _err(400, str(e) or "这句话发不了")
                return _err(404, "找不到这个对话")
            if mid is None:
                return _err(404, "找不到这个对话")
            if isinstance(mid, dict):
                mid = mid.get("user_message_id", mid.get("id", 0))
            try:
                user_message_id = int(mid)
            except (TypeError, ValueError):
                user_message_id = 0
            return web.json_response({"accepted": True, "user_message_id": user_message_id}, status=202)

        async def _chat_pending(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            mod, not_ready = _chat_ready()
            if not_ready is not None:
                return not_ready
            pending_id, bad_id = _id_param(request.match_info["pid"], "确认项")
            if bad_id is not None:
                return bad_id
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            approve = body.get("approve")
            if not isinstance(approve, bool):
                return _err(400, "approve 要是 true / false")
            try:
                out = await _call(mod.confirm, pending_id, approve)
            except (KeyError, ValueError) as e:
                if _is_busy_error(e):
                    return _err(409, str(e) or "上一句还在处理，等一下")
                if type(e).__name__ == "PendingDecided":
                    return _err(409, str(e) or "这条待确认动作已经处理过了")
                return _err(404, "这个确认项不存在")
            if not isinstance(out, dict):
                return _err(404, "这个确认项不存在")
            return _chat_json({"status": _pending_status(out, approve), "result": _pending_result(out)})


        @post("/api/chat/{id}/compact")
        async def _chat_compact(request: web.Request) -> web.Response:
            """手动整理摘要（0.4.0）：忙时 409「等一下」，其余按 404/400 分岔。"""
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            mod, not_ready = _chat_ready()
            if not_ready is not None:
                return not_ready
            chat_id, bad_id = _id_param(request.match_info["id"], "对话")
            if bad_id is not None:
                return bad_id
            try:
                out = await _call(mod.compact, chat_id)
            except (KeyError, ValueError) as e:
                if _is_busy_error(e):
                    return _err(409, str(e) or "上一句还在处理，等一下再整理")
                if _chat_exists(mod, chat_id):
                    return _err(400, str(e) or "现在整理不了")
                return _err(404, "找不到这个对话")
            if not isinstance(out, dict):
                return _err(404, "找不到这个对话")
            summary = out.get("summary") if isinstance(out.get("summary"), dict) else out
            return _chat_json({"summary": summary, "chat_id": chat_id})

        # pending 路由先落：路径段数和 /api/chat/{id} 不同，顺序只为了让「静态段优先」一眼可见
        app.router.add_post("/api/chat/pending/{pid}", self._write(_chat_pending))
        app.router.add_post("/api/chat/{id}/compact", self._write(_chat_compact))
        app.router.add_route("PATCH", "/api/chat/{id}", self._write(_chat_update))

        # ---------- 头像（console/avatar.py；docs/02「网页控制台 · 头像」） ----------
        #
        # GET /api/avatar/bot 不用登录（登录页也要显示）；自定义网址 → 302 到该网址，
        # 本地文件 → FileResponse（Content-Type 按魔数），没有 → 404（前端回落默认图）。
        # GET /api/avatar/m/<token> 要管理员：token 反查关注成员的 QQ 后走 qlogo 代理
        # （QQ 号不进 URL / 响应头 / 日志）。
        # GET /api/avatar/g/<token> 要身份：token = HMAC("avatar-g|<群号>")，只认服务群；
        # 管理员可看全部服务群，群友只能看自己群（别人群 403），非 qq 平台 / 查不到 → 404。
        # 管理接口 /api/settings/avatar：GET 给 {source, platform, url, custom_kind,
        # custom_url}；POST 收 {"url"} 或 {"data","mime"}（base64，≤2MB，按魔数收
        # png/jpeg/webp/gif）；DELETE 清自定义回自动。写操作过 _write 同源守卫。

        def _avatar_svc() -> Any:
            from .avatar import service_of

            av = service_of(svc)
            if av is None:
                logger.warning("头像服务没就位（store / settings 缺），这次没有头像")
            return av

        async def _avatar_state(av: Any) -> dict:
            """settings_state 需要知道 bot_qq 有没有值（异步）；失败按没有算。"""
            try:
                qq = await av._bot_qq()  # noqa: SLF001 —— 同源模块的探针，不落日志
            except Exception:
                qq = ""
            return av.settings_state(bot_qq_known=bool(qq))

        @get("/api/avatar/bot")
        async def _avatar_bot(request: web.Request) -> web.Response:
            av = _avatar_svc()
            if av is None:
                return _err(404, "没有头像")
            kind, payload = await av.resolve_bot()
            if kind == "redirect":
                raise web.HTTPFound(str(payload))
            if kind == "bytes":
                data, mime = payload
                return web.Response(body=data, content_type=mime, headers={"Cache-Control": "no-cache"})
            if kind == "path":
                from .avatar import ext_to_mime

                return web.FileResponse(
                    payload,
                    headers={"Cache-Control": "no-cache", "Content-Type": ext_to_mime(payload)},
                )
            return _err(404, "没有头像")

        @get("/api/avatar/m/{token}")
        async def _avatar_member(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            av = _avatar_svc()
            if av is None:
                return _err(404, "没有这个头像")
            settings = svc.get_settings()
            group_ids = list(settings.groups.keys()) if settings is not None else []
            kind, payload = await av.resolve_member(str(request.match_info["token"]), group_ids)
            if kind == "bytes":
                data, mime = payload
                return web.Response(body=data, content_type=mime, headers={"Cache-Control": "no-cache"})
            if kind == "path":
                from .avatar import ext_to_mime

                return web.FileResponse(
                    payload,
                    headers={"Cache-Control": "no-cache", "Content-Type": ext_to_mime(payload)},
                )
            return _err(404, "没有这个头像")

        @get("/api/avatar/g/{token}")
        async def _avatar_group(request: web.Request) -> web.Response:
            ident = self._identify(request)
            if ident.role == "none":
                return _err(401, "先登录管理员，或用群链接打开")
            av = _avatar_svc()
            if av is None:
                return _err(404, "没有这个头像")
            settings = svc.get_settings()
            group_ids = list(settings.groups.keys()) if settings is not None else []
            token = str(request.match_info["token"])
            gid = av.find_group_by_token(token, group_ids)
            if gid is None:
                return _err(404, "没有这个头像")
            if ident.role in ("member", "group_admin") and str(ident.group_id or "") != str(gid):
                return _err(403, "只能看自己群的头像")
            kind, payload = await av.resolve_group(token, group_ids)
            if kind == "bytes":
                data, mime = payload
                return web.Response(body=data, content_type=mime, headers={"Cache-Control": "no-cache"})
            if kind == "path":
                from .avatar import ext_to_mime

                return web.FileResponse(
                    payload,
                    headers={"Cache-Control": "no-cache", "Content-Type": ext_to_mime(payload)},
                )
            return _err(404, "没有这个头像")

        @get("/api/settings/avatar")
        async def _avatar_settings_get(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            av = _avatar_svc()
            if av is None:
                return _err(503, "头像服务没就位")
            return web.json_response(await _avatar_state(av))

        async def _avatar_settings_post(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            av = _avatar_svc()
            if av is None:
                return _err(503, "头像服务没就位")
            body = await _json_body(request)
            if body is None:
                return _err(400, "请求体不是 JSON")
            from .avatar import parse_avatar_post

            try:
                kind, payload = parse_avatar_post(body)
                if kind == "url":
                    av.set_custom_url(str(payload))
                else:
                    av.set_custom_upload(payload)
            except ValueError as e:
                return _err(400, str(e))
            logger.info("bot 头像已由管理员更换（%s）", "自定义网址" if kind == "url" else "上传图片")
            return web.json_response(await _avatar_state(av))

        async def _avatar_settings_delete(request: web.Request) -> web.Response:
            forbid = self._require_admin(request)
            if forbid is not None:
                return forbid
            av = _avatar_svc()
            if av is None:
                return _err(503, "头像服务没就位")
            av.clear_custom()
            logger.info("bot 头像已恢复为自动（平台头像 / 默认图）")
            return web.json_response(await _avatar_state(av))

        app.router.add_post("/api/settings/avatar", self._write(_avatar_settings_post))
        app.router.add_route("DELETE", "/api/settings/avatar", self._write(_avatar_settings_delete))

        # ---------- 静态 ----------

        @get("/")
        async def _index(request: web.Request) -> web.Response:
            return web.Response(
                text=_index_html(), content_type="text/html", charset="utf-8",
                headers={"Cache-Control": "no-cache"},
            )

        @get("/g/{token}")
        async def _group_link(request: web.Request) -> web.Response:
            raise web.HTTPFound(f"/#/{request.match_info['token']}/news")

        app.router.add_static("/static/", _STATIC_DIR, show_index=False)

        # 未知 /api 路由 → JSON 404（放在最后兜底）
        async def _api_404(request: web.Request) -> web.Response:
            return _err(404, "没有这个接口")

        app.router.add_route("*", "/api/{tail:.*}", _api_404)
        return app

    # ------------------------------------------------------------------
    # 中间件
    # ------------------------------------------------------------------

    @web.middleware
    async def _errors_mw(self, request: web.Request, handler: Handler) -> web.Response:
        try:
            resp = await handler(request)
        except web.HTTPException:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("网页请求未捕获异常：%s %s", request.method, request.path)
            resp = _err(500, "服务器出错了")
        try:
            resp.headers.setdefault("X-Content-Type-Options", "nosniff")
            resp.headers.setdefault("Referrer-Policy", "no-referrer")
            resp.headers.setdefault("X-Frame-Options", "DENY")
        except Exception:
            pass
        return resp

    # ------------------------------------------------------------------
    # 模型测试成功后的回写（旧 kv["models.checked"]；新老并存，新写读看 kv["endpoints.checked.<id>"]）
    # ------------------------------------------------------------------


    def _save_endpoint_checked(self, endpoint_id: str, base_url: str, available: list[str], protocol: str = "openai") -> None:
        """测试连接结果（不是配置）存 kv["endpoints.checked.<id>"]；只记 key_set 与否，密钥绝不进。"""
        checked_at = clock.now()
        try:
            with self._svc.store.tx() as conn:
                self._svc.store.kv_set(conn, f"endpoints.checked.{endpoint_id}", {
                    "base_url": base_url.rstrip("/"),
                    "available": list(available),
                    "checked_at": checked_at,
                    "protocol": str(protocol or "openai"),
                })
        except Exception:
            logger.exception("回写端点测试记录失败")


    def _save_checked(self, base_url: str, available: list[str]) -> None:
        """把 available / checked_at 存回 kv["models.checked"]（测试连接结果，不是配置）。

        模型设置本体在 config.toml 的 [models]（网页保存也写它）；这份回写只在
        端点一致时并进来显示（models.settings() 里对齐）。
        """
        svc = self._svc
        checked_at = clock.now()
        try:
            with svc.store.tx() as conn:
                svc.store.kv_set(conn, "models.checked", {"base_url": base_url.rstrip("/"), "available": list(available), "checked_at": checked_at})
        except Exception:
            logger.exception("回写模型测试记录失败")
        # 内存里的缓存也顺手对齐（settings() 缓存键含 kv 值，其实下次自己就会重算）
        try:
            ms = svc.models.settings()
            if ms.base_url == base_url.rstrip("/"):
                ms.available = list(available)
                ms.checked_at = checked_at
        except Exception:
            pass


def create_app(svc: Any) -> web.Application:
    """拼出 aiohttp Application；svc 是 app.py 里的服务对象。"""
    return ConsoleServer(svc).app
