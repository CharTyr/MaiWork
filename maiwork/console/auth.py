"""console 的鉴权：管理员 cookie、群友链接码、登录限流。

- 管理员密码：[console] password 非空用它（网页改密码就是写进 config.toml 的这个键，
  明文，用户明确同意）；否则首次生成 16 位随机密码，
  scrypt 哈希存 secrets.admin_password_hash（格式见 passwords.py；老的 "sha256$<salt>$<hash>"
  照样认，登录成功时自动换成 scrypt），
  明文写 <data_dir>/console_password.txt（0600），日志只打印文件路径。
  网页改密码成功后旧的自动生成哈希会被删掉（rules.save_config_patch / 迁移）。
- cookie mw_admin = "<到期时间戳>.<HMAC>"，HttpOnly、SameSite=Strict、Path=/、7 天；
  HMAC 密钥是 secrets.console_secret（没有就生成），且混入密码哈希——改了管理员密码旧 cookie 就失效。
- 群管理员 cookie（2026-10）：同一个 cookie 名，值 = "g:<群号>.<到期时间戳>.<HMAC>"，
  签名内容 = "group|<群号>|<到期>|<该群密码指纹>"——改了 / 清了这个群的群管理员密码，
  旧 cookie 立刻失效（指纹没了或不一致）。总管理员的格式保持兼容不变。
- 同一 IP 10 分钟内密码错 5 次 → 429（总管理员和群管理员共用同一套限流）。
  IP 默认是连接地址；配了 [console] trusted_proxies 时，来自这些代理的请求按
  X-Forwarded-For 里的真实访客 IP 计数（`client_ip`）。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import secrets as _secrets
import stat
from pathlib import Path
from typing import Any

from .. import clock, passwords

logger = logging.getLogger("maiwork.console.auth")

COOKIE_NAME = "mw_admin"
COOKIE_TTL = 7 * 86400
_MAX_TRIES = 5
_WINDOW = 600.0  # 10 分钟
_FAILS_PRUNE_AT = 1024  # 失败记录表超过这么多个 IP 就清一次过期的
_FAILS_MAX = 4096  # 清完还超过就淘汰最久没失败的 IP，失败记录表的硬上限


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
        if not stored or not passwords.verify(password, stored):
            return False
        if passwords.needs_upgrade(stored):
            self._upgrade_hash(stored, passwords.hash_password(password))
        return True

    async def verify_password_async(self, password: str) -> bool:
        """登录接口用：算哈希（scrypt 几十毫秒）放到线程里，不卡事件循环；读写库仍在循环线程。"""
        cfg_pw = self._config_password()
        if cfg_pw:
            return hmac.compare_digest(password.encode("utf-8"), cfg_pw.encode("utf-8"))
        stored = self._store.secret_get("admin_password_hash")
        if not stored or not await asyncio.to_thread(passwords.verify, password, stored):
            return False
        if passwords.needs_upgrade(stored):
            self._upgrade_hash(stored, await asyncio.to_thread(passwords.hash_password, password))
        return True

    def _upgrade_hash(self, old: str, new: str) -> None:
        """老 sha256 哈希登录成功后换成 scrypt（中途被改过就不动）。指纹跟着变，旧 cookie 失效一次。"""
        try:
            with self._store.tx() as conn:
                if self._store.secret_get("admin_password_hash") == old:
                    self._store.secret_set(conn, "admin_password_hash", new)
        except Exception:
            logger.warning("管理员密码哈希升级失败，下次登录再试", exc_info=True)

    def ensure_password(self, data_dir: Path) -> None:
        """有 config 密码就不用管；否则首次生成随机密码（存哈希、明文落文件）。"""
        if self._config_password():
            return
        if self._store.secret_get("admin_password_hash"):
            return
        password = _secrets.token_urlsafe(12)  # 16 个字符
        if len(password) > 16:
            password = password[:16]
        with self._store.tx() as conn:
            self._store.secret_set(conn, "admin_password_hash", passwords.hash_password(password))
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
        now = clock.now()
        fails = [t for t in (self._fails.get(ip) or []) if now - t < _WINDOW]
        if fails:
            self._fails[ip] = fails
        else:
            # 没有窗口内的失败就不留键（以前每个来登录的 IP 都会留一条空列表，只增不减）
            self._fails.pop(ip, None)
        return len(fails) >= _MAX_TRIES

    def record_login_fail(self, ip: str) -> None:
        now = clock.now()
        fails = [t for t in (self._fails.get(ip) or []) if now - t < _WINDOW]
        fails.append(now)
        self._fails[ip] = fails
        if len(self._fails) > _FAILS_PRUNE_AT:
            self._prune_fails(now)

    def _prune_fails(self, now: float) -> None:
        """错一次就再没来过的 IP 也会一直占着：表大了逐条清掉窗口外的时间点，清空了就摘掉；
        窗口内的 IP 还是太多就按最近一次失败从旧到新淘汰到上限（内存有硬上限）。"""
        for k in list(self._fails):
            ts = [t for t in self._fails[k] if now - t < _WINDOW]
            if ts:
                self._fails[k] = ts
            else:
                del self._fails[k]
        extra = len(self._fails) - _FAILS_MAX
        if extra > 0:
            for k in sorted(self._fails, key=lambda k: max(self._fails[k]))[:extra]:
                del self._fails[k]

    def record_login_ok(self, ip: str) -> None:
        self._fails.pop(ip, None)

    # ------------------------------------------------------------------
    # 同源检查
    # ------------------------------------------------------------------


def client_ip(remote: str, forwarded_for: str, trusted: Any) -> str:
    """登录限流用的访客 IP。

    直连（来源不在 `trusted` 代理名单里）→ 就是连接地址，请求头一律不信（防伪造）；
    来源是信任的代理 → 从 X-Forwarded-For 右往左找第一个不是信任代理的地址
    （最右边是离我们最近的一跳，左边的可能被访客自己伪造）；头里全是代理 / 没有头 / 坏值
    → 回落连接地址。
    """
    import ipaddress

    remote = str(remote or "")
    nets = []
    for item in trusted or ():
        try:
            nets.append(ipaddress.ip_network(str(item), strict=False))
        except ValueError:
            continue
    if not nets:
        return remote

    def _trusted(addr: str) -> bool:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        return any(ip in n for n in nets)

    if not _trusted(remote):
        return remote
    hops = [h.strip() for h in str(forwarded_for or "").split(",") if h.strip()]
    for hop in reversed(hops):
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            return remote  # 坏值：不猜，按代理本身算
        if not _trusted(hop):
            return hop
    return remote


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
