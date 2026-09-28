"""按群区分的管理员（群管理员，2026-10 新增）。

背景：网页原来只有两种身份——总管理员（cookie 登录，看全部群）和群友
（X-MW-Group 链接码，只读本群）。这里加第三种 **群管理员**：用「这个群的
群管理员密码」登录，只能管这一个群的事，碰不到任何 MaiWork 全局的东西。

存法（**只进数据库，不进 config.toml**）：
- 每个群一份密码哈希：`secrets["group_admin_pw.<群号>"]`，格式与总管理员一致
  （`sha256$<salt hex>$<digest hex>`），明文不落库。
- 本群管理员名单：`kv["group_admins.<群号>"] = ["qq:123", ...]`（`config.norm_account`
  规范化，MaiBot 标准写法；只写数字的旧写法当 qq）。

红线：
- 只认**服务群**：群被移出服务名单后，它的群管理员密码匹配不到（视为陌生人）。
- 不同群不许用同一个密码；群管理员密码也不许和总管理员密码相同（否则登录时
  分不清身份）——`set_password` 会拒绝，报中文原因。
- 密码只进不出：网页接口、日志都不回显密码；指纹只用来做 cookie 签名。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets as _secrets
from typing import Any, Callable

from .config import norm_account

logger = logging.getLogger("maiwork.group_admins")

PW_PREFIX = "group_admin_pw."
ACCOUNTS_PREFIX = "group_admins."
MIN_PASSWORD_LEN = 8

# 密码重复时的统一文案（网页 400 / 分组校验都用它）
DUPLICATE_MESSAGE = "和别的群管理员密码重复了"
TOO_SHORT_MESSAGE = "群管理员密码至少 8 位"


class GroupAdmins:
    """群管理员的密码与名单（数据层）。

    - `store`：Store（真库）。
    - `get_settings`：拿 Settings 的可调用对象；用来只知道**服务群**有哪些。
    - `console_auth`：ConsoleAuth；`set_password` 拿它比对总管理员密码。
      构造后再接也认（`bind_console_auth`），避免 app 启动顺序上的循环依赖。
    """

    def __init__(
        self,
        store: Any,
        *,
        get_settings: Callable[[], Any] | None = None,
        console_auth: Any = None,
    ) -> None:
        self._store = store
        self._get_settings = get_settings
        self._auth = console_auth

    def bind_console_auth(self, auth: Any) -> None:
        self._auth = auth

    # ------------------------------------------------------------------
    # 群
    # ------------------------------------------------------------------

    def served_gids(self) -> list[str]:
        """当前服务群（配置里列的）；拿不到配置就退回「库里存过密码的群」。"""
        settings = None
        if callable(self._get_settings):
            try:
                settings = self._get_settings()
            except Exception:
                settings = None
        if settings is not None:
            try:
                return sorted(str(g) for g in settings.groups.keys())
            except Exception:
                pass
        return sorted(self._stored_gids())

    def _stored_gids(self) -> list[str]:
        """库里存过群管理员密码的群号（兜底 / 查重时用）。"""
        try:
            rows = self._store.read().execute(
                "SELECT name FROM secrets WHERE name LIKE ?", (PW_PREFIX + "%",)
            ).fetchall()
        except Exception:
            logger.debug("读群管理员密码名单失败（表还没建？）", exc_info=True)
            return []
        out = []
        for r in rows:
            try:
                name = str(r["name"])
            except Exception:
                continue
            if name.startswith(PW_PREFIX) and len(name) > len(PW_PREFIX):
                out.append(name[len(PW_PREFIX):])
        return out

    def all_password_gids(self) -> list[str]:
        """服务群 + 库里残留过密码的群（查重范围，比 served_gids 宽）。"""
        return sorted({*self.served_gids(), *self._stored_gids()})

    # ------------------------------------------------------------------
    # 密码
    # ------------------------------------------------------------------

    @staticmethod
    def _digest(password: str, salt: str) -> str:
        return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()

    def _stored_hash(self, gid: Any) -> str:
        try:
            return str(self._store.secret_get(PW_PREFIX + str(gid)) or "")
        except Exception:
            logger.debug("读群 %s 的群管理员密码失败", gid, exc_info=True)
            return ""

    @classmethod
    def _verify_hash(cls, password: str, stored: str) -> bool:
        if not stored:
            return False
        try:
            algo, salt, expect = stored.split("$", 2)
        except ValueError:
            return False
        if algo != "sha256" or not salt or not expect:
            return False
        return hmac.compare_digest(cls._digest(str(password), salt), expect)

    def has_password(self, gid: Any) -> bool:
        return bool(self._stored_hash(gid))

    def set_password(self, gid: Any, password: str) -> None:
        """设置这个群的群管理员密码；太短 / 和别的群或总管理员重复 → ValueError（中文）。"""
        gid_s = str(gid).strip()
        if not gid_s:
            raise ValueError("群号不能是空的")
        pw = str(password or "")
        if len(pw) < MIN_PASSWORD_LEN:
            raise ValueError(TOO_SHORT_MESSAGE)
        for other in self.all_password_gids():
            if other == gid_s:
                continue
            if self._verify_hash(pw, self._stored_hash(other)):
                raise ValueError(DUPLICATE_MESSAGE)
        auth = self._auth
        if auth is not None:
            try:
                if auth.verify_password(pw):
                    raise ValueError(DUPLICATE_MESSAGE)
            except ValueError:
                raise
            except Exception:
                logger.debug("和总管理员密码比对失败，跳过这一步", exc_info=True)
        salt = _secrets.token_hex(8)
        stored = f"sha256${salt}${self._digest(pw, salt)}"
        with self._store.tx() as conn:
            self._store.secret_set(conn, PW_PREFIX + gid_s, stored)

    def clear_password(self, gid: Any) -> None:
        """清掉这个群的群管理员密码（幂等）；该群已发的 cookie 立刻失效。"""
        with self._store.tx() as conn:
            self._store.secret_delete(conn, PW_PREFIX + str(gid))

    def match(self, password: str) -> str | None:
        """用密码找出是哪个群的管理员；只认服务群，没有 / 不对 → None。"""
        pw = str(password or "")
        if not pw:
            return None
        for gid in self.served_gids():
            if self._verify_hash(pw, self._stored_hash(gid)):
                return gid
        return None

    def fingerprint(self, gid: Any) -> str:
        """密码哈希的短指纹（cookie 签名用）；没设密码 → ""（cookie 一律失效）。"""
        stored = self._stored_hash(gid)
        if not stored:
            return ""
        return hashlib.sha256(f"mw-ga|{gid}|{stored}".encode("utf-8")).hexdigest()[:16]

    # ------------------------------------------------------------------
    # 名单
    # ------------------------------------------------------------------

    def accounts(self, gid: Any) -> list[str]:
        """本群管理员名单（规范化的「平台:账号」），认不出的条目丢掉。"""
        try:
            raw = self._store.kv_get(ACCOUNTS_PREFIX + str(gid), [])
        except Exception:
            logger.debug("读群 %s 的群管理员名单失败", gid, exc_info=True)
            return []
        out: list[str] = []
        for x in (raw if isinstance(raw, (list, tuple)) else []):
            n = norm_account(x)
            if n and n not in out:
                out.append(n)
        return out

    def set_accounts(self, gid: Any, accounts: Any) -> list[str]:
        """整份替换名单；认不出的账号 → ValueError（中文，网页好提示）。"""
        gid_s = str(gid).strip()
        if not gid_s:
            raise ValueError("群号不能是空的")
        if accounts is None:
            accounts = []
        if not isinstance(accounts, (list, tuple)):
            raise ValueError("名单要是数组，比如 [\"qq:123456\"]")
        out: list[str] = []
        for x in accounts:
            n = norm_account(x)
            if not n:
                raise ValueError(f"认不出这个账号「{x}」，要写成 qq:123456")
            if n not in out:
                out.append(n)
        with self._store.tx() as conn:
            self._store.kv_set(conn, ACCOUNTS_PREFIX + gid_s, out)
        return out

    def is_group_admin(self, gid: Any, user_id: Any, platform: str = "qq") -> bool:
        """这个（群，账号）是不是名单里的本群管理员。"""
        uid = str(user_id or "").strip()
        if not uid:
            return False
        target = norm_account(f"{platform or 'qq'}:{uid}")
        return bool(target) and target in self.accounts(gid)
