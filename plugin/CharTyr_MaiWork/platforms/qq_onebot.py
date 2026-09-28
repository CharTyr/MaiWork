"""platforms/qq_onebot.py（QQ / NapCat-OneBot 平台档案）：群空间 GroupSpace。

设计依据 docs/02-设计.md §10、docs/06-宿主接口事实.md「QQ 适配器与群空间接口」：

- 线上适配器可能还是旧版（0.8.5，44 个 API，没有公告/相册/文件管理）：
  **启动时探测**——`probe()` 调 host.list_apis()（api.list）拿可用接口集合缓存
  （启动时一次 + 每 6 小时刷新），再按群用 host.group_member_role(group_id, bot_qq)
  拿机器人身份（owner/admin/member，缓存 1 小时）。两样都满足的操作才开放。
- 防手滑：删除 / 改名 / 移动只动机器人自己上传的文件——outbox 上传成功时登记进
  group_files_owned 表（register_owned），动手前校验 file_id 在表里，否则拒绝。
- 发公告：先在群里说一句固定话「我要发一条群公告：<前 30 字>」（由调用方传
  announce 回调，app 接的是 outbox.enqueue，push_kind="status"），再调适配器；
  每群每天最多 [group_space] notice_per_day 条（默认 1）。
- 非服务群：零调用（连身份都不查）。
- api.call 的参数名不确定的地方都写成模块级可配置常量并标〔待实测〕。
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from .. import clock
from ..host import HostError
from ..store import Store

logger = logging.getLogger("maiwork.groupspace")

# ----------------------------------------------------------------------
# 可调常量
# ----------------------------------------------------------------------

PROBE_TTL_S = 6 * 3600      # api 名单缓存 6 小时
ROLE_TTL_S = 3600           # 机器人群身份缓存 1 小时
NOTICE_ANNOUNCE_PREFIX = "我要发一条群公告："  # 发公告前的固定预告话
NOTICE_PREVIEW_LEN = 30     # 预告里带公告内容前多少字

CAPABILITY_KEYS = (
    "files_list",
    "files_manage",
    "notice_read",
    "notice_send",
    "album_list",
    "album_upload",
)

# 适配器 api 名（文档 docs/06 末尾名单；旧版没有这些 → 能力全关）
API_GET_ROOT_FILES = "adapter.napcat.file.get_group_root_files"
API_GET_FILES_BY_FOLDER = "adapter.napcat.file.get_group_files_by_folder"
API_GET_FILE_SYSTEM_INFO = "adapter.napcat.file.get_group_file_system_info"
API_CREATE_FOLDER = "adapter.napcat.file.create_group_file_folder"
API_DELETE_FILE = "adapter.napcat.file.delete_group_file"
API_DELETE_FOLDER = "adapter.napcat.file.delete_group_folder"
API_RENAME_FILE = "adapter.napcat.file.rename_group_file"
API_MOVE_FILE = "adapter.napcat.file.move_group_file"
API_GET_NOTICES = "adapter.napcat.group.get_group_notice"
API_SEND_NOTICE = "adapter.napcat.group.send_group_notice"
API_DELETE_NOTICE = "adapter.napcat.group.delete_group_notice"
API_ALBUM_LIST = "adapter.napcat.file.get_qun_album_list"
API_ALBUM_MEDIA_LIST = "adapter.napcat.file.get_group_album_media_list"
API_ALBUM_UPLOAD = "adapter.napcat.file.upload_image_to_qun_album"
API_ALBUM_DEL_MEDIA = "adapter.napcat.file.del_group_album_media"

# 〔待实测〕api.call 的 args 参数名：按 NapCat/OneBot 通行写法（snake_case，
# 群号/文件 ID/文件夹 ID 都是字符串，文件夹用 folder_id、根目录省略或给 "/"）。
# 升级到 v1.0.1 后在测试群逐个核对，不对就只改这些常量。
ARG_GROUP_ID = "group_id"
ARG_FILE_ID = "file_id"
ARG_FOLDER_ID = "folder_id"
ARG_FOLDER = "folder"       # 部分 NapCat 版本列目录用 "folder" 而不是 "folder_id"〔待实测〕
ARG_NAME = "name"
ARG_CONTENT = "content"
ARG_ALBUM_ID = "album_id"
ARG_PATH = "path"
ARG_IMAGE = "image"         # upload_image_to_qun_album 的图片参数名也可能是 "file"〔待实测〕

_ROLE_ADMIN_SET = ("owner", "admin")


class GroupSpace:
    """QQ 群空间：群文件管理、群公告、群相册。接口/身份两样都满足才开放。"""

    def __init__(self, host: Any, store: Store, get_settings: Callable[[], Any]) -> None:
        self._host = host
        self._store = store
        self._get_settings = get_settings
        self._apis: set[str] = set()
        self._probed: bool = False
        self._probe_ts: float = 0.0
        self._roles: dict[str, tuple[float, str]] = {}  # group_id -> (ts, role)

    # ------------------------------------------------------------------
    # 探测
    # ------------------------------------------------------------------

    async def probe(self, *, now: float | None = None) -> None:
        """刷新适配器接口名单。失败只记日志不抛（不影响启动），旧缓存保留；
        失败也记探测时间（6 小时内不重试，免得适配器/宿主还没好的时候每轮循环都打）。"""
        ts = clock.now() if now is None else float(now)
        if self._probed and ts - self._probe_ts < PROBE_TTL_S:
            return
        try:
            apis = await self._host.list_apis()
        except Exception as e:
            logger.warning("群空间探测适配器接口失败（%s），这次按旧缓存/全关处理", type(e).__name__)
            self._probe_ts = ts  # 连失败也算一次探测：下次 6 小时后再试
            return
        self._apis = {str(a).strip() for a in apis if str(a or "").strip()}
        self._probed = True
        self._probe_ts = ts

    async def _role(self, group_id: str, now: float) -> str:
        """机器人在该群的身份（owner/admin/member/""），缓存 1 小时。"""
        gid = str(group_id)
        hit = self._roles.get(gid)
        if hit is not None and now - hit[0] < ROLE_TTL_S:
            return hit[1]
        try:
            bot_qq = await self._host.bot_qq()
            role = str(await self._host.group_member_role(gid, bot_qq) or "").strip()
        except Exception:
            role = ""
        self._roles[gid] = (now, role)
        return role

    async def refresh_role(self, group_id: str) -> str:
        """有事件循环版本的身份查询（写缓存）。网页 GroupView 在拿缓存前先调一次。"""
        gid = str(group_id)
        try:
            settings = self._get_settings()
            if settings is None or not settings.is_served(gid):
                return ""
        except Exception:
            return ""
        return await self._role(gid, clock.now())

    # ------------------------------------------------------------------
    # 能力
    # ------------------------------------------------------------------

    def _caps_with(self, group_id: str, role: str) -> dict[str, bool]:
        gid = str(group_id)
        apis = self._apis
        admin = role in _ROLE_ADMIN_SET
        return {
            "files_list": API_GET_ROOT_FILES in apis and API_GET_FILES_BY_FOLDER in apis,
            "files_manage": admin and all(
                a in apis
                for a in (API_DELETE_FILE, API_RENAME_FILE, API_MOVE_FILE, API_CREATE_FOLDER)
            ),
            "notice_read": API_GET_NOTICES in apis,
            "notice_send": admin and API_SEND_NOTICE in apis,
            "album_list": API_ALBUM_LIST in apis,
            "album_upload": admin and API_ALBUM_UPLOAD in apis,
        }

    def capabilities(self, group_id: str, *, now: float | None = None) -> dict[str, bool]:
        """这个群现在能做什么（同步版：只读接口名单 + 身份缓存）。

        接口不在名单里 → False；缓存里没有身份（还没 refresh_role 过 / 过期）→
        admin 系 False（保守）。非服务群：全 False 且零调用。
        想要准确的身份请用 `await capabilities_async(...)`。
        """
        gid = str(group_id)
        try:
            settings = self._get_settings()
            if settings is None or not settings.is_served(gid):
                return {k: False for k in CAPABILITY_KEYS}
        except Exception:
            return {k: False for k in CAPABILITY_KEYS}
        role = self._role_cached(gid)
        return self._caps_with(gid, role)

    async def capabilities_async(self, group_id: str, *, now: float | None = None) -> dict[str, bool]:
        """异步版：身份缓存过期/没有时就地查（group_member_role，缓存 1 小时）。"""
        gid = str(group_id)
        try:
            settings = self._get_settings()
            if settings is None or not settings.is_served(gid):
                return {k: False for k in CAPABILITY_KEYS}
        except Exception:
            return {k: False for k in CAPABILITY_KEYS}
        role = await self._role(gid, clock.now() if now is None else float(now))
        return self._caps_with(gid, role)

    def _role_cached(self, group_id: str) -> str:
        hit = self._roles.get(str(group_id))
        return hit[1] if hit is not None else ""

    def role_of(self, group_id: str) -> str:
        """缓存里的身份（没有就 ""）。网页 GroupView 用。"""
        return self._role_cached(group_id)
        return hit[1] if hit is not None else ""

    def adapter_open(self) -> bool:
        """适配器是不是新版（开放了群空间接口）。健康状态用。"""
        return API_GET_ROOT_FILES in self._apis and API_SEND_NOTICE in self._apis

    @property
    def probed(self) -> bool:
        return self._probed

    def served_groups(self) -> list[str]:
        try:
            settings = self._get_settings()
            return [str(g) for g in (getattr(settings, "groups", {}) or {}).keys()] if settings else []
        except Exception:
            return []

    # ------------------------------------------------------------------
    # 内部：服务群 + 能力闸
    # ------------------------------------------------------------------

    def _check_served(self, group_id: str) -> None:
        gid = str(group_id)
        try:
            settings = self._get_settings()
            ok = settings is not None and settings.is_served(gid)
        except Exception:
            ok = False
        if not ok:
            raise PermissionError(f"MaiWork 不服务这个群：{gid}，不做任何操作")

    def _enabled(self) -> bool:
        try:
            settings = self._get_settings()
            gs = getattr(settings, "group_space", None) if settings is not None else None
            return bool(getattr(gs, "enabled", True)) if gs is not None else True
        except Exception:
            return True

    async def _require(self, group_id: str, capability: str) -> None:
        """服务群 + 接口 + 身份三道闸；不过关抛 PermissionError（中文原因）。"""
        self._check_served(group_id)
        if not self._enabled():
            raise PermissionError("群空间功能关着（[group_space] enabled=false）")
        gid = str(group_id)
        # 身份要现查的（admin 系能力）；先看接口在不在，再查身份——不服务的连查都不查
        apis = self._apis
        needs_admin = capability in ("files_manage", "notice_send", "album_upload")
        if capability == "files_list":
            if not (API_GET_ROOT_FILES in apis and API_GET_FILES_BY_FOLDER in apis):
                raise PermissionError("这个群现在做不了：适配器没开放群文件列表接口（QQ 适配器是旧版，升级到 v1.0.1 后自动开放）")
        elif capability == "files_manage":
            if not all(a in apis for a in (API_DELETE_FILE, API_RENAME_FILE, API_MOVE_FILE, API_CREATE_FOLDER)):
                raise PermissionError("这个群现在做不了：适配器没开放群文件管理接口（升级到 v1.0.1 后自动开放）")
        elif capability == "notice_read":
            if API_GET_NOTICES not in apis:
                raise PermissionError("这个群现在做不了：适配器没开放群公告接口（升级到 v1.0.1 后自动开放）")
        elif capability == "notice_send":
            if API_SEND_NOTICE not in apis:
                raise PermissionError("这个群现在做不了：适配器没开放发群公告接口（升级到 v1.0.1 后自动开放）")
        elif capability == "album_list":
            if API_ALBUM_LIST not in apis:
                raise PermissionError("这个群现在做不了：适配器没开放群相册接口（升级到 v1.0.1 后自动开放）")
        elif capability == "album_upload":
            if API_ALBUM_UPLOAD not in apis:
                raise PermissionError("这个群现在做不了：适配器没开放传群相册接口（升级到 v1.0.1 后自动开放）")
        if needs_admin:
            role = await self._role(gid, clock.now())
            if role not in _ROLE_ADMIN_SET:
                raise PermissionError("这个群现在做不了：机器人不是群主或管理员")

    def _require_own(self, group_id: str, file_id: str) -> None:
        """防手滑：file_id 必须在 group_files_owned 表里（机器人自己传的）。"""
        row = self._store.read().execute(
            "SELECT 1 FROM group_files_owned WHERE group_id=? AND file_id=? LIMIT 1",
            (str(group_id), str(file_id)),
        ).fetchone()
        if row is None:
            raise PermissionError("只能动我自己传的文件：这个文件不是我传的，不动")

    # ------------------------------------------------------------------
    # 自有文件登记
    # ------------------------------------------------------------------

    def register_owned(
        self,
        group_id: str,
        file_id: str,
        name: str,
        task_id: str | None = None,
        *,
        now: float | None = None,
    ) -> None:
        """outbox 上传群文件成功时调：登记进 group_files_owned。

        只登记配置里的服务群（非服务群零写入）。任何异常只记日志——登记失败
        不该把发送流程搞挂（最坏后果是这个文件之后不能删/改名/移动）。
        """
        gid = str(group_id)
        try:
            settings = self._get_settings()
            if settings is None or not settings.is_served(gid):
                return
        except Exception:
            return
        fid = str(file_id or "").strip()
        if not fid:
            return
        ts = clock.now() if now is None else float(now)
        try:
            with self._store.tx() as conn:
                conn.execute(
                    "INSERT INTO group_files_owned (group_id, file_id, name, uploaded_ts, task_id)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(group_id, file_id) DO UPDATE SET"
                    " name=excluded.name, uploaded_ts=excluded.uploaded_ts, task_id=excluded.task_id",
                    (gid, fid, str(name or ""), ts, str(task_id) if task_id is not None else None),
                )
                self._store.event(
                    conn, "group_space.file_registered", group_id=gid,
                    entity="group_file", entity_id=fid,
                    payload={"name": str(name or ""), "task_id": str(task_id or "")},
                )
        except Exception:
            logger.exception("群文件登记失败（群 %s）", gid)

    # ------------------------------------------------------------------
    # 群文件
    # ------------------------------------------------------------------

    async def list_files(self, group_id: str, folder_id: str | None = None) -> list[dict]:
        """列群文件：folder_id 空 → 根目录；否则列那个文件夹。"""
        await self._require(group_id, "files_list")
        gid = str(group_id)
        out: list[dict] = []
        if folder_id:
            data = await self._host.call_adapter(
                API_GET_FILES_BY_FOLDER,
                {ARG_GROUP_ID: gid, ARG_FOLDER_ID: str(folder_id)},  # 〔待实测〕folder 参数名
            )
            out.extend(self._normalize_file_listing(data, gid))
        else:
            data = await self._host.call_adapter(API_GET_ROOT_FILES, {ARG_GROUP_ID: gid})
            out.extend(self._normalize_file_listing(data, gid))
        return out

    @staticmethod
    def _normalize_file_listing(data: Any, gid: str) -> list[dict]:
        """get_group_root_files / get_group_files_by_folder 的 data → 统一条目列表。

        NapCat 返回 {"files": [...], "folders": [...]}，条目标 type=file/folder；
        字段名按通行写法取，拿不到就空。〔待实测〕字段细节。
        """
        if not isinstance(data, dict):
            return []
        out: list[dict] = []
        for f in data.get("files") or []:
            if not isinstance(f, dict):
                continue
            out.append(
                {
                    "type": "file",
                    "file_id": str(f.get("file_id") or ""),
                    "name": str(f.get("file_name") or f.get("name") or ""),
                    "size": int(f.get("file_size") or f.get("size") or 0),
                    "uploader": str(f.get("uploader") or f.get("uploader_id") or ""),
                }
            )
        for f in data.get("folders") or []:
            if not isinstance(f, dict):
                continue
            out.append(
                {
                    "type": "folder",
                    "folder_id": str(f.get("folder_id") or ""),
                    "name": str(f.get("folder_name") or f.get("name") or ""),
                }
            )
        return out

    async def create_folder(self, group_id: str, name: str, folder_id: str | None = None) -> None:
        await self._require(group_id, "files_manage")
        args: dict[str, Any] = {ARG_GROUP_ID: str(group_id), ARG_NAME: str(name)}
        if folder_id:
            args[ARG_FOLDER_ID] = str(folder_id)  # 〔待实测〕父文件夹参数名
        await self._host.call_adapter(API_CREATE_FOLDER, args)
        self._write_event("group_space.folder_created", group_id, name)

    async def delete_file(self, group_id: str, file_id: str) -> None:
        await self._require(group_id, "files_manage")
        self._require_own(group_id, file_id)
        await self._host.call_adapter(
            API_DELETE_FILE, {ARG_GROUP_ID: str(group_id), ARG_FILE_ID: str(file_id)}
        )
        with self._store.tx() as conn:
            conn.execute(
                "DELETE FROM group_files_owned WHERE group_id=? AND file_id=?",
                (str(group_id), str(file_id)),
            )
        self._write_event("group_space.file_deleted", group_id, file_id)

    async def rename_file(self, group_id: str, file_id: str, name: str) -> None:
        await self._require(group_id, "files_manage")
        self._require_own(group_id, file_id)
        await self._host.call_adapter(
            API_RENAME_FILE,
            {ARG_GROUP_ID: str(group_id), ARG_FILE_ID: str(file_id), ARG_NAME: str(name)},
        )
        with self._store.tx() as conn:
            conn.execute(
                "UPDATE group_files_owned SET name=? WHERE group_id=? AND file_id=?",
                (str(name), str(group_id), str(file_id)),
            )
        self._write_event("group_space.file_renamed", group_id, file_id, {"name": str(name)})

    async def move_file(self, group_id: str, file_id: str, folder_id: str) -> None:
        await self._require(group_id, "files_manage")
        self._require_own(group_id, file_id)
        await self._host.call_adapter(
            API_MOVE_FILE,
            {ARG_GROUP_ID: str(group_id), ARG_FILE_ID: str(file_id), ARG_FOLDER_ID: str(folder_id)},
        )
        self._write_event("group_space.file_moved", group_id, file_id, {"folder_id": str(folder_id)})

    async def delete_folder(self, group_id: str, folder_id: str) -> None:
        await self._require(group_id, "files_manage")
        await self._host.call_adapter(
            API_DELETE_FOLDER, {ARG_GROUP_ID: str(group_id), ARG_FOLDER_ID: str(folder_id)}
        )
        self._write_event("group_space.folder_deleted", group_id, folder_id)

    # ------------------------------------------------------------------
    # 群公告
    # ------------------------------------------------------------------

    async def get_notices(self, group_id: str) -> list[dict]:
        await self._require(group_id, "notice_read")
        data = await self._host.call_adapter(API_GET_NOTICES, {ARG_GROUP_ID: str(group_id)})
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            items = data.get("notices") or data.get("list") or []
            if isinstance(items, list):
                return [x for x in items if isinstance(x, dict)]
        return []

    def _notice_count_today(self, group_id: str, now: float) -> int:
        """今天（北京时间）发过几条公告。按 runs 里每行的 day_key 现比——
        events 表没存 day 列，直接按时间窗可能把前一天晚上的算进来。"""
        day = clock.day_key(float(now))
        rows = self._store.read().execute(
            "SELECT ts FROM events WHERE kind='group_space.notice_sent' AND group_id=?"
            " AND ts>=?",
            (str(group_id), float(now) - 86400 * 2),
        ).fetchall()
        return sum(1 for r in rows if clock.day_key(float(r["ts"])) == day)

    def _notice_limit(self) -> int:
        try:
            settings = self._get_settings()
            gs = getattr(settings, "group_space", None) if settings is not None else None
            return max(1, int(getattr(gs, "notice_per_day", 1))) if gs is not None else 1
        except Exception:
            return 1

    async def send_notice(
        self,
        group_id: str,
        content: str,
        *,
        announce: Callable[[str, str], Awaitable[None]] | None = None,
        now: float | None = None,
    ) -> None:
        """发公告：先在群里说一句预告（announce 回调，app 接 outbox，push_kind=status），
        再调适配器。每群每天最多 [group_space] notice_per_day 条。
        """
        await self._require(group_id, "notice_send")
        gid = str(group_id)
        text = str(content or "").strip()
        if not text:
            raise ValueError("公告内容不能为空")
        ts = clock.now() if now is None else float(now)
        if self._notice_count_today(gid, ts) >= self._notice_limit():
            raise PermissionError("今天已经发过公告了，明天再发（每群每天最多 1 条）")
        # 先预告：固定的「我要发一条群公告：<前 30 字>」
        if announce is not None:
            preview = text[:NOTICE_PREVIEW_LEN]
            await announce(gid, f"{NOTICE_ANNOUNCE_PREFIX}{preview}")
        await self._host.call_adapter(
            API_SEND_NOTICE, {ARG_GROUP_ID: gid, ARG_CONTENT: text}
        )
        self._write_event("group_space.notice_sent", gid, text[:80], ts=ts)

    # ------------------------------------------------------------------
    # 群相册
    # ------------------------------------------------------------------

    async def list_albums(self, group_id: str) -> list[dict]:
        await self._require(group_id, "album_list")
        data = await self._host.call_adapter(API_ALBUM_LIST, {ARG_GROUP_ID: str(group_id)})
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            items = data.get("albums") or data.get("album_list") or data.get("list") or []
            if isinstance(items, list):
                return [x for x in items if isinstance(x, dict)]
        return []

    async def upload_to_album(self, group_id: str, album_id: str, path: str) -> None:
        await self._require(group_id, "album_upload")
        await self._host.call_adapter(
            API_ALBUM_UPLOAD,
            {
                ARG_GROUP_ID: str(group_id),
                ARG_ALBUM_ID: str(album_id),
                ARG_IMAGE: str(path),  # 〔待实测〕图片参数名可能是 "file"
            },
        )
        self._write_event("group_space.album_uploaded", group_id, str(album_id))

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------

    def _write_event(
        self,
        kind: str,
        group_id: str,
        entity_id: str,
        payload: dict | None = None,
        *,
        ts: float | None = None,
    ) -> None:
        """所有写操作记事件。失败只记日志，不把已完成的主操作搞挂。

        ts 缺省 clock.now()；公告这类「按事件数节制」的调用要把当时的 ts 传进来，
        每日上限才数得准。
        """
        when = clock.now() if ts is None else float(ts)
        try:
            if ts is None:
                with self._store.tx() as conn:
                    self._store.event(
                        conn, kind, group_id=str(group_id),
                        entity="group_space", entity_id=str(entity_id)[:120],
                        payload=payload,
                    )
            else:
                import json as _json

                with self._store.tx() as conn:
                    conn.execute(
                        "INSERT INTO events (ts, kind, group_id, entity, entity_id, payload, v)"
                        " VALUES (?, ?, ?, 'group_space', ?, ?, 1)",
                        (
                            when, str(kind), str(group_id), str(entity_id)[:120],
                            _json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                        ),
                    )
        except Exception:
            logger.exception("群空间事件写入失败（%s）", kind)
