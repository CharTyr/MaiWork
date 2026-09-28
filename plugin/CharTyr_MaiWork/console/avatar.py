"""console 的头像服务：bot 头像（自定义 / MaiBot 平台头像 / 默认）和关注成员头像。

Bot 头像来源优先级（GET /api/avatar/bot，不用登录，登录页也要显示）：
1. 管理员自定义：上传的图片存 <数据目录>/console/avatar_bot.<ext>；自定义网址
   存 kv["avatar.bot_url"]（POST /api/settings/avatar 两种 JSON 形态）。
2. MaiBot 的 QQ 头像：QQ 号走 host.bot_qq()（bot.qq_account），图片地址
   https://q1.qlogo.cn/g?b=qq&nk=<号>&s=640，服务端下载后磁盘缓存 24 小时
   （<数据目录>/console/avatar_cache/bot_qq.<ext>）；下载失败用旧缓存，旧缓存
   也没有才往下走。
3. 其他平台：host.config("bot.platforms") 形如 ["tg:123", ...]。这些平台
   （Telegram / Discord / …）**没有无需密钥的公开头像地址**——Telegram 要
   Bot API token 调 getUserProfilePhotos，Discord 要 bot token——拿不到 token
   就没法代理，只能跳过（代码注释在此，新增平台时再评）。
4. 自带默认图 /static/assets/bot.jpg（不经过本模块，服务端给 404 让前端回落）。

群头像（GET /api/avatar/g/<token>）：
- token = HMAC-SHA256(console_secret, "avatar-g|<群号>") 前 16 位十六进制；只认服务群。
- 管理员能看所有服务群；群友（member）只能看自己那个群（别人群 403）。
- QQ 群头像地址 https://p.qlogo.cn/gh/<群号>/<群号>/640，磁盘缓存 24 小时
  （avatar_cache/g_<token>.<ext>）；非 qq 平台 / 找不到 / 下载失败且没缓存 → 404
  （前端回落 emoji 群图标）。
- 群对象的 avatar 字段 = /api/avatar/g/<token>（非 QQ 形态的群号给空串）。

关注成员头像（GET /api/avatar/m/<token>，要管理员）：
- token = HMAC-SHA256(secrets.console_secret, "avatar-m|<群号>:<用户QQ>") 前 16 位
  十六进制；服务端在**所有服务群**的 focus_members 里反查（不显式展示 QQ 号，
  QQ 号不进 URL、不进日志）。
- 查到后经 qlogo（nk=<用户QQ>&s=160）代理 + 磁盘缓存 24 小时
  （avatar_cache/m_<token>.<ext>）；找不到人 / 下载失败且没有缓存 → 404
  （前端回落首字母圆）。

通用规则：
- Content-Type 按文件头魔数判定（png/jpeg/gif/webp），不信扩展名；上传也只收这四种。
- 出站请求用 httpx（注入点 transport，测试给 MockTransport 不碰真实网络），
  超时 8 秒，响应体上限 2MB，只认 2xx。
- 头像版本号（?v=）= kv["avatar.version"]：自定义上传/换网址/删除、平台头像
  缓存更新时递增，网页端靠它避开浏览器旧缓存。
- QQ 号不写进响应头、不写进日志（日志只记类型和大小）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
from pathlib import Path
from typing import Any

from .. import clock

logger = logging.getLogger("maiwork.console.avatar")

MAX_BYTES = 2 * 1024 * 1024          # 头像字节上限（上传和出站下载同限）
FETCH_TIMEOUT_S = 8.0                # 出站超时（含连接 + 读）
CACHE_TTL_S = 24 * 3600              # 平台头像磁盘缓存 24 小时
MEMBER_INFO_TTL_S = 6 * 3600         # 群名片 / QQ 昵称缓存 6 小时（task 2，这里只放常量）

_QLOGO_BOT = "https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"
_QLOGO_MEMBER = "https://q1.qlogo.cn/g?b=qq&nk={qq}&s=160"
_QLOGO_GROUP = "https://p.qlogo.cn/gh/{gid}/{gid}/640"   # QQ 群头像（task 1）

_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_MIME_TO_EXT = {v: k for k, v in _EXT_TO_MIME.items() if k != ".jpeg"}


def sniff_image(data: bytes) -> str | None:
    """按文件头魔数判图片类型 → MIME；不认识 → None。"""
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def sniff_ext(data: bytes) -> str | None:
    mime = sniff_image(data)
    return _MIME_TO_EXT.get(mime or "")


def ext_to_mime(path: Path) -> str:
    """磁盘缓存文件 → Content-Type：先试内容魔数，再按扩展名，兜底 octet-stream。"""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        head = b""
    mime = sniff_image(head)
    if mime:
        return mime
    return _EXT_TO_MIME.get(path.suffix.lower(), "application/octet-stream")


class AvatarService:
    """头像读写与缓存。store 没就位（没启动）时所有读回落「默认图」。"""

    def __init__(self, store: Any, data_dir: Any, host: Any = None, *, transport: Any = None) -> None:
        self._store = store
        self._dir = Path(data_dir) / "console"
        self._host = host
        self._transport = transport

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------

    @property
    def _cache_dir(self) -> Path:
        return self._dir / "avatar_cache"

    def _kv_get(self, key: str, default: Any = None) -> Any:
        store = self._store
        if store is None:
            return default
        try:
            return store.kv_get(key, default)
        except Exception:
            return default

    def _kv_set(self, key: str, value: Any) -> None:
        if self._store is None:
            return
        with self._store.tx() as conn:
            self._store.kv_set(conn, key, value)

    def _secret(self) -> bytes:
        """console 共享密钥（auth.py 同一个 console_secret）：没有就生成。"""
        import secrets as _secrets

        store = self._store
        if store is None:
            return b"maiwork-avatar-no-store"
        secret = store.secret_get("console_secret")
        if not secret:
            secret = _secrets.token_hex(32)
            with store.tx() as conn:
                store.secret_set(conn, "console_secret", secret)
        return secret.encode("utf-8")

    # ------------------------------------------------------------------
    # 版本号（?v=）：自定义变化 / 平台缓存更新时 +1
    # ------------------------------------------------------------------

    def version(self) -> int:
        try:
            return int(self._kv_get("avatar.version", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _bump_version(self) -> None:
        self._kv_set("avatar.version", self.version() + 1)

    # ------------------------------------------------------------------
    # 自定义头像（上传文件 / 外部网址）
    # ------------------------------------------------------------------

    def _custom_file(self) -> Path | None:
        """已上传的自定义头像文件；不存在 → None。"""
        try:
            for p in self._dir.glob("avatar_bot.*"):
                if p.suffix.lower() in _EXT_TO_MIME and p.is_file():
                    return p
        except OSError:
            return None
        return None

    def custom_url(self) -> str:
        return str(self._kv_get("avatar.bot_url", "") or "").strip()

    def set_custom_upload(self, data: bytes) -> None:
        """存上传的自定义头像（调用方已验大小和魔数）；换扩展名时删旧文件。"""
        ext = sniff_ext(data)
        if ext is None:
            raise ValueError("只收 png / jpeg / webp / gif 图片")
        self._dir.mkdir(parents=True, exist_ok=True)
        self._remove_custom_files()
        (self._dir / f"avatar_bot{ext}").write_bytes(bytes(data))
        self._kv_set("avatar.bot_url", "")
        self._bump_version()

    def set_custom_url(self, url: str) -> None:
        url = str(url or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("自定义头像网址要以 http:// 或 https:// 开头")
        if len(url) > 2000:
            raise ValueError("网址太长了")
        self._remove_custom_files()
        self._kv_set("avatar.bot_url", url)
        self._bump_version()

    def clear_custom(self) -> None:
        self._remove_custom_files()
        self._kv_set("avatar.bot_url", "")
        self._bump_version()

    def _remove_custom_files(self) -> None:
        try:
            for p in self._dir.glob("avatar_bot.*"):
                if p.suffix.lower() in _EXT_TO_MIME:
                    try:
                        p.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

    # ------------------------------------------------------------------
    # MaiBot 的 QQ（task 1；不写日志、不进响应头）
    # ------------------------------------------------------------------

    async def _bot_qq(self) -> str:
        host = self._host
        if host is None:
            return ""
        try:
            qq = await host.bot_qq()
        except Exception:
            return ""
        qq = str(qq or "").strip()
        return qq if qq.isdigit() else ""

    async def _bot_platform(self) -> str:
        """bot.platform（线上是 "qq"）；拿不到就按 qq 处理（线上事实）。"""
        host = self._host
        if host is None:
            return ""
        try:
            val = await host.config("bot.platform")
        except Exception:
            return ""
        return str(val or "").strip().lower()

    async def _platform_accounts(self) -> list[str]:
        """bot.platforms 形如 ["tg:123", ...]：没有其他平台 → []。

        注意：Telegram / Discord 等平台**没有无需密钥的公开头像地址**（都要
        bot token 调各自 API），本版对它们一律跳过——这里只留了解析，真接
        平台头像时再来补「该平台怎么不用密钥拿头像」。
        """
        host = self._host
        if host is None:
            return []
        try:
            val = await host.config("bot.platforms")
        except Exception:
            return []
        out: list[str] = []
        if isinstance(val, (list, tuple)):
            out = [str(x).strip() for x in val if str(x).strip()]
        elif isinstance(val, str) and val.strip():
            out = [val.strip()]
        return out

    # ------------------------------------------------------------------
    # 出站下载（httpx，超时 8 秒、上限 2MB、只认 2xx）
    # ------------------------------------------------------------------

    async def _fetch_image(self, url: str) -> tuple[bytes, str] | None:
        """下载图片 → (字节, MIME)；失败一律 None（不抛、不记 URL 进日志）。"""
        try:
            import httpx
        except ImportError:  # pragma: no cover - httpx 是硬依赖
            return None
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(FETCH_TIMEOUT_S),
                follow_redirects=True,
                max_redirects=3,
            ) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code < 200 or resp.status_code >= 300:
                        return None
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes(65536):
                        total += len(chunk)
                        if total > MAX_BYTES:
                            return None
                        chunks.append(chunk)
            data = b"".join(chunks)
        except Exception as e:
            logger.info("头像下载没成功（%s）", type(e).__name__)
            return None
        mime = sniff_image(data)
        if mime is None:
            return None
        return data, mime

    def _cache_paths(self, stem: str) -> list[Path]:
        try:
            return [
                p for p in self._cache_dir.glob(f"{stem}.*")
                if p.suffix.lower() in _EXT_TO_MIME and p.is_file()
            ]
        except OSError:
            return []

    def _read_cache(self, stem: str, ttl_s: float) -> Path | None:
        """没过期（mtime + ttl > now）的缓存文件；没有 → None。"""
        now = clock.now()
        for p in self._cache_paths(stem):
            try:
                st = p.stat()
                if st.st_mtime + ttl_s > now and st.st_size > 0:
                    return p
            except OSError:
                continue
        return None

    def _stale_cache(self, stem: str) -> Path | None:
        """过期的缓存文件也拿出来（下载失败时的回落）。"""
        for p in self._cache_paths(stem):
            try:
                if p.stat().st_size > 0:
                    return p
            except OSError:
                continue
        return None

    def _write_cache(self, stem: str, data: bytes, mime: str) -> Path | None:
        """写缓存（同 stem 的旧文件删掉）；mtime 用文件自己的，不篡改。"""
        ext = _MIME_TO_EXT.get(mime)
        if ext is None:
            return None
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            for p in self._cache_paths(stem):
                try:
                    p.unlink()
                except OSError:
                    pass
            target = self._cache_dir / f"{stem}{ext}"
            target.write_bytes(bytes(data))
            return target
        except OSError:
            return None

    # ------------------------------------------------------------------
    # bot 头像（GET /api/avatar/bot）
    # ------------------------------------------------------------------

    def bot_avatar_url(self) -> str:
        return f"/api/avatar/bot?v={self.version()}"

    def settings_state(self, *, bot_qq_known: bool = False) -> dict:
        """GET /api/settings/avatar 的结构。bot_qq_known 由服务端先问过 host 再传进来
        （同步函数里没法 await；没问到就按 False，url 照给 /api/avatar/bot?v=N——
        它自己会回落）。"""
        custom_file = self._custom_file()
        custom_url = self.custom_url()
        if custom_file is not None:
            source, kind = "custom", "upload"
        elif custom_url:
            source, kind = "custom", "url"
        elif bot_qq_known:
            source, kind = "qq", ""
        else:
            source, kind = "default", ""
        if source == "custom" and kind == "url":
            url = custom_url
        else:
            url = self.bot_avatar_url()
        return {
            "source": source,
            "platform": "qq" if source == "qq" else "",
            "url": url,
            "custom_kind": kind,
            "custom_url": custom_url if kind == "url" else "",
        }

    async def resolve_bot(self) -> tuple[str, Any]:
        """→ (kind, payload)。kind: "path"（本地文件）/ "redirect"（外部网址）/ "bytes" / "none"。

        payload：kind=path 时是 Path；kind=redirect 时是 URL 字符串；kind=bytes 时是
        (data, mime)。QQ 号不出现在返回值里。
        """
        # 1. 自定义
        custom_file = self._custom_file()
        if custom_file is not None:
            return "path", custom_file
        custom_url = self.custom_url()
        if custom_url:
            return "redirect", custom_url
        # 2. MaiBot 的 QQ 头像（platform=qq 且 qq_account 有值）
        qq = ""
        platform = await self._bot_platform()
        if platform in ("", "qq"):
            qq = await self._bot_qq()
        if qq:
            stem = "bot_qq"
            fresh = self._read_cache(stem, CACHE_TTL_S)
            if fresh is not None:
                return "path", fresh
            got = await self._fetch_image(_QLOGO_BOT.format(qq=qq))
            if got is not None:
                data, mime = got
                path = self._write_cache(stem, data, mime)
                self._bump_version()  # 平台头像缓存更新了
                if path is not None:
                    return "path", path
                # 写盘失败：直接用内存里的字节给（不落缓存）
                return "bytes", (data, mime)
            stale = self._stale_cache(stem)
            if stale is not None:
                return "path", stale
            return "none", None
        # 3. 其他平台：没有无需密钥的公开头像地址，跳过（见 _platform_accounts 注释）
        await self._platform_accounts()
        # 4. 默认
        return "none", None

    # ------------------------------------------------------------------
    # 关注成员头像（GET /api/avatar/m/<token>）
    # ------------------------------------------------------------------

    def member_token(self, group_id: str, user_id: str) -> str:
        """HMAC-SHA256(console_secret, "avatar-m|<群号>:<用户QQ>") 前 16 位十六进制。"""
        msg = f"avatar-m|{group_id}:{user_id}".encode("utf-8")
        return hmac.new(self._secret(), msg, hashlib.sha256).hexdigest()[:16]

    def member_avatar_path(self, token: str) -> str:
        return f"/api/avatar/m/{token}"

    def find_member_by_token(self, token: str, group_ids: list[str]) -> tuple[str, str] | None:
        """在所有服务群的 focus_members 里反查 token → (group_id, user_id)；没有 → None。"""
        token = str(token or "").strip().lower()
        if not token or len(token) > 64 or self._store is None:
            return None
        try:
            rows = self._store.read().execute(
                "SELECT group_id, user_id FROM focus_members WHERE removed=0"
            ).fetchall()
        except Exception:
            return None
        served = {str(g) for g in group_ids}
        for r in rows:
            gid, uid = str(r["group_id"]), str(r["user_id"])
            if served and gid not in served:
                continue
            if hmac.compare_digest(self.member_token(gid, uid), token):
                return gid, uid
        return None

    async def resolve_member(self, token: str, group_ids: list[str]) -> tuple[str, Any]:
        """→ ("path", Path) / ("bytes", (data, mime)) / ("none", None)。找不到人 / 失败 → none。"""
        found = self.find_member_by_token(token, group_ids)
        if found is None:
            return "none", None
        _gid, uid = found
        if not uid.isdigit():
            return "none", None
        stem = f"m_{token}"
        fresh = self._read_cache(stem, CACHE_TTL_S)
        if fresh is not None:
            return "path", fresh
        got = await self._fetch_image(_QLOGO_MEMBER.format(qq=uid))
        if got is not None:
            data, mime = got
            path = self._write_cache(stem, data, mime)
            if path is not None:
                return "path", path
            return "bytes", (data, mime)
        stale = self._stale_cache(stem)
        if stale is not None:
            return "path", stale
        return "none", None


    # ------------------------------------------------------------------
    # 群头像（GET /api/avatar/g/<token>）
    # ------------------------------------------------------------------

    def group_token(self, group_id: str) -> str:
        """HMAC-SHA256(console_secret, "avatar-g|<群号>") 前 16 位十六进制。"""
        msg = f"avatar-g|{group_id}".encode("utf-8")
        return hmac.new(self._secret(), msg, hashlib.sha256).hexdigest()[:16]

    def group_avatar_path(self, group_id: str) -> str:
        """群对象的 avatar 字段；非 QQ 形态的群号（非纯数字）→ 空串。"""
        gid = str(group_id or "").strip()
        if not gid.isdigit():
            return ""
        return f"/api/avatar/g/{self.group_token(gid)}"

    def find_group_by_token(self, token: str, group_ids: list[str]) -> str | None:
        """在服务群里反查 token → 群号；没有 → None。"""
        token = str(token or "").strip().lower()
        if not token or len(token) > 64 or self._store is None:
            return None
        for gid in group_ids:
            gid_s = str(gid)
            if hmac.compare_digest(self.group_token(gid_s), token):
                return gid_s
        return None

    async def resolve_group(self, token: str, group_ids: list[str]) -> tuple[str, Any]:
        """→ ("path", Path) / ("bytes", (data, mime)) / ("none", None)；只认服务群 + qq。"""
        gid = self.find_group_by_token(token, group_ids)
        if gid is None or not gid.isdigit():
            return "none", None
        platform = await self._bot_platform()
        if platform not in ("", "qq"):
            return "none", None
        stem = f"g_{str(token or '').strip().lower()}"
        fresh = self._read_cache(stem, CACHE_TTL_S)
        if fresh is not None:
            return "path", fresh
        got = await self._fetch_image(_QLOGO_GROUP.format(gid=gid))
        if got is not None:
            data, mime = got
            path = self._write_cache(stem, data, mime)
            if path is not None:
                return "path", path
            return "bytes", (data, mime)
        stale = self._stale_cache(stem)
        if stale is not None:
            return "path", stale
        return "none", None


# ----------------------------------------------------------------------
# POST 请求体的两种形态解析（server.py 用）
# ----------------------------------------------------------------------


def parse_avatar_post(body: Any) -> tuple[str, Any]:
    """{"url": ...} → ("url", 网址)；{"data": base64, "mime": ...} → ("upload", 字节)。

    不合法抛 ValueError（中文）。上传做：base64 解码、≤2MB、魔数校验（mime 字段
    只当参考，以魔数为准）。
    """
    if not isinstance(body, dict):
        raise ValueError("请求体不是 JSON")
    if body.get("url") is not None:
        url = str(body.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("自定义头像网址要以 http:// 或 https:// 开头")
        return "url", url
    if body.get("data") is not None:
        raw_b64 = str(body.get("data") or "")
        try:
            data = base64.b64decode(raw_b64, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("data 不是合法的 base64") from None
        if not data:
            raise ValueError("图片是空的")
        if len(data) > MAX_BYTES:
            raise ValueError("图片超过 2MB 上限")
        if sniff_image(data) is None:
            raise ValueError("只收 png / jpeg / webp / gif 图片（按文件内容判定）")
        return "upload", data
    raise ValueError('请求体要给 {"url": "..."} 或 {"data": "<base64>", "mime": "image/..."}')


def service_of(svc: Any) -> "AvatarService | None":
    """svc 上的头像服务：已有就复用；没有就按 app 启动时的参数建一次并挂回 svc.avatar。

    建不起来（没 store / 没 settings）→ None，调用方各自回落（bot 图 / 没有 avatar 字段）。
    """
    av = getattr(svc, "avatar", None)
    if isinstance(av, AvatarService):
        return av
    try:
        settings = svc.get_settings()
        av = AvatarService(
            svc.store,
            settings.data_dir,
            getattr(svc, "host", None),
            transport=getattr(svc, "avatar_transport", None),
        )
    except Exception:
        return None
    try:
        svc.avatar = av
    except Exception:
        pass
    return av
