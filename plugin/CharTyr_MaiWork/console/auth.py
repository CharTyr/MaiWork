"""console 的鉴权：管理员 cookie、群友链接码、登录限流。

- 管理员密码：[console] password 非空用它（网页改密码就是写进 config.toml 的这个键，
  明文，用户明确同意）；否则首次生成 16 位随机密码，
  sha256(salt + 密码) 存 secrets.admin_password_hash（格式 "sha256$<salt hex>$<hash hex>"），
  明文写 <data_dir>/console_password.txt（0600），日志只打印文件路径。
  网页改密码成功后旧的自动生成哈希会被删掉（rules.save_config_patch / 迁移）。
- cookie mw_admin = "<到期时间戳>.<HMAC>"，HttpOnly、SameSite=Strict、Path=/、7 天；
  HMAC 密钥是 secrets.console_secret（没有就生成），且混入密码哈希——改了管理员密码旧 cookie 就失效。
- 群管理员 cookie（2026-10）：同一个 cookie 名，值 = "g:<群号>.<到期时间戳>.<HMAC>"，
  签名内容 = "group|<群号>|<到期>|<该群密码指纹>"——改了 / 清了这个群的群管理员密码，
  旧 cookie 立刻失效（指纹没了或不一致）。总管理员的格式保持兼容不变。
- 同一 IP 10 分钟内密码错 5 次 → 429（总管理员和群管理员共用同一套限流）。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets as _secrets
import stat
from pathlib import Path
from typing import Any

from .. import clock

logger = logging.getLogger("maiwork.console.auth")

COOKIE_NAME = "mw_admin"
COOKIE_TTL = 7 * 86400
_MAX_TRIES = 5
_WINDOW = 600.0  # 10 分钟


class ConsoleAuth:
    def __init__(self, store: Any, get_settings: Any) -> None:
        self._store = store
        self._get_settings = get_settings
        self._fails: dict[str, list[float]] = {}
        # GroupAdmins：群管理员密码指纹要从它拿（ConsoleServer 组装时接上）
        self._group_admins: Any = None

    # ------------------------------------------------------------------
    # 密钥与密码
    # ------------------------------------------------------------------

    def _console_secret(self) -> bytes:
        secret = self._store.secret_get("console_secret")
        if not secret:
            secret = _secrets.token_hex(32)
            with self._store.tx() as conn:
                self._store.secret_set(conn, "console_secret", secret)
        return secret.encode("utf-8")

    def _config_password(self) -> str:
        """config.toml [console] password（网页改密码写的也是它）；没有 → ""。"""
        settings = self._get_settings()
        return str(settings.console.password or "") if settings is not None else ""

    def _password_fingerprint(self) -> str:
        """当前密码的指纹：config 密码 → 它的哈希；自动生成的 → 存的哈希。"""
        cfg_pw = self._config_password()
        if cfg_pw:
            return "cfg:" + hashlib.sha256(("cfg:" + cfg_pw).encode("utf-8")).hexdigest()
        return "gen:" + self._store.secret_get("admin_password_hash")

    def verify_password(self, password: str) -> bool:
        cfg_pw = self._config_password()
        if cfg_pw:
            return hmac.compare_digest(password.encode("utf-8"), cfg_pw.encode("utf-8"))
        stored = self._store.secret_get("admin_password_hash")
        if not stored:
            return False
        try:
            _algo, salt, expect = stored.split("$", 2)
        except ValueError:
            return False
        actual = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
        return hmac.compare_digest(actual, expect)

    def ensure_password(self, data_dir: Path) -> None:
        """有 config 密码就不用管；否则首次生成随机密码（存哈希、明文落文件）。"""
        if self._config_password():
            return
        if self._store.secret_get("admin_password_hash"):
            return
        password = _secrets.token_urlsafe(12)  # 16 个字符
        if len(password) > 16:
            password = password[:16]
        salt = _secrets.token_hex(8)
        digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
        with self._store.tx() as conn:
            self._store.secret_set(conn, "admin_password_hash", f"sha256${salt}${digest}")
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(data_dir, stat.S_IMODE(0o700))
        except OSError:
            pass
        pw_file = data_dir / "console_password.txt"
        pw_file.write_text(password + "\n", encoding="utf-8")
        try:
            os.chmod(pw_file, stat.S_IMODE(0o600))
        except OSError:
            pass
        logger.warning("管理员密码已生成并写到 %s（只写这一次，日志里不会出现密码本身）", pw_file)

    # ------------------------------------------------------------------
    # cookie
    # ------------------------------------------------------------------

    def make_cookie(self) -> tuple[str, int]:
        """返回总管理员的 (cookie 值, max_age)。"""
        expire = int(clock.now()) + COOKIE_TTL
        sig = self._sign(str(expire))
        return f"{expire}.{sig}", COOKIE_TTL

    def check_cookie(self, value: str) -> bool:
        if not value or "." not in value:
            return False
        expire_s, sig = value.rsplit(".", 1)
        if not expire_s.isdigit():
            return False
        if not hmac.compare_digest(sig, self._sign(expire_s)):
            return False
        return int(expire_s) > int(clock.now())

    def _sign(self, payload: str) -> str:
        # 混入密码指纹：改了管理员密码（config）旧 cookie 立刻失效
        msg = f"admin|{payload}|{self._password_fingerprint()}".encode("utf-8")
        return hmac.new(self._console_secret(), msg, hashlib.sha256).hexdigest()

    # ------------------------------------------------------------------
    # 群管理员 cookie（g:<群号>.<到期>.<HMAC>）
    # ------------------------------------------------------------------

    def bind_group_admins(self, group_admins: Any) -> None:
        """接上 GroupAdmins（指纹来源）。"""
        self._group_admins = group_admins

    def _group_fingerprint(self, gid: str) -> str:
        ga = self._group_admins
        if ga is None:
            return ""
        try:
            return str(ga.fingerprint(gid) or "")
        except Exception:
            logger.debug("读群 %s 的群管理员密码指纹失败", gid, exc_info=True)
            return ""

    def _sign_group(self, gid: str, expire_s: str, fingerprint: str) -> str:
        msg = f"group|{gid}|{expire_s}|{fingerprint}".encode("utf-8")
        return hmac.new(self._console_secret(), msg, hashlib.sha256).hexdigest()

    def make_group_cookie(self, gid: str) -> tuple[str, int]:
        """返回群管理员的 (cookie 值, max_age)；该群没设密码时返回 ("", 0)。"""
        gid_s = str(gid)
        fingerprint = self._group_fingerprint(gid_s)
        if not fingerprint:
            return "", 0
        expire = int(clock.now()) + COOKIE_TTL
        sig = self._sign_group(gid_s, str(expire), fingerprint)
        return f"g:{gid_s}.{expire}.{sig}", COOKIE_TTL

    def check_group_cookie(self, value: str) -> str | None:
        """群管理员 cookie → 群号；签名不对 / 过期 / 密码改了或清了 → None。"""
        if not value or not value.startswith("g:") or value.count(".") != 2:
            return None
        head, expire_s, sig = value.split(".", 2)
        gid = head[2:]
        if not gid or not expire_s.isdigit():
            return None
        fingerprint = self._group_fingerprint(gid)
        if not fingerprint:
            return None
        expect = self._sign_group(gid, expire_s, fingerprint)
        if not hmac.compare_digest(sig, expect):
            return None
        return gid if int(expire_s) > int(clock.now()) else None

    # ------------------------------------------------------------------
    # 登录限流（同一 IP 10 分钟错 5 次 → 429）
    # ------------------------------------------------------------------

    def login_blocked(self, ip: str) -> bool:
        fails = self._fails.get(ip) or []
        now = clock.now()
        fails = [t for t in fails if now - t < _WINDOW]
        self._fails[ip] = fails
        return len(fails) >= _MAX_TRIES

    def record_login_fail(self, ip: str) -> None:
        now = clock.now()
        fails = [t for t in (self._fails.get(ip) or []) if now - t < _WINDOW]
        fails.append(now)
        self._fails[ip] = fails

    def record_login_ok(self, ip: str) -> None:
        self._fails.pop(ip, None)

    # ------------------------------------------------------------------
    # 同源检查
    # ------------------------------------------------------------------


def _norm_host_port(hostport: str, scheme: str) -> str:
    """把 host[:port] 归一成 "主机:端口"；没端口按 http=80 / https=443 补。"""
    hostport = hostport.strip().lower()
    default = "443" if scheme == "https" else "80"
    if hostport.startswith("["):  # IPv6，形如 [::1]:8080
        if "]:" in hostport:
            name, _, port = hostport.partition("]:")
            return f"{name}]:{port}"
        return f"{hostport}:{default}"
    if ":" in hostport:
        return hostport
    return f"{hostport}:{default}"


def same_origin(origin: str, host: str) -> bool:
    """非 GET 请求：Origin 的 host:port 必须等于请求 Host。

    Origin 形如 "https://example.com:443"，可能不带显式端口
    （这时按 http=80 / https=443 算），Host 也一样归一。
    """
    origin = str(origin or "").strip()
    host = str(host or "").strip()
    if not origin or not host:
        return False
    scheme = ""
    rest = origin
    for s in ("https://", "http://"):
        if rest.lower().startswith(s):
            scheme = s[0:-3]  # "https" / "http"
            rest = rest[len(s):]
            break
    if not scheme:
        return False
    rest = rest.split("/", 1)[0]
    if not rest:
        return False
    return _norm_host_port(rest, scheme) == _norm_host_port(host, scheme)
