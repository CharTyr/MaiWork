"""按群区分的管理员（群管理员，2026-10 新增）。

背景：网页原来只有两种身份——总管理员（cookie 登录，看全部群）和群友
（X-MW-Group 链接码，只读本群）。这里加第三种 **群管理员**：用「这个群的
群管理员密码」登录，只能管这一个群的事，碰不到任何 MaiWork 全局的东西。

存法（**只进数据库，不进 config.toml**）：
- 每个群一份密码哈希：`secrets["group_admin_pw.<群号>"]`，格式与总管理员一致
  （`sha256$<salt hex>$<digest hex>`），明文不落库。**密码这一份没动**。
- 本群批准人名单（0.8.0 归一，docs/18 §五）：不再单独存 `kv["group_admins.<群号>"]`，
  而是和「谁能批本群的活（含免批）」共用**同一份** `kv["group_approval.<群号>"]` 的
  `approvers`（group_approval.py）。`accounts` / `set_accounts` / `is_group_admin`
  都转发到那一份，读写只有一个来源；旧按群名单键在首次访问时并入并删掉。

红线：
- 只认**服务群**：群被移出服务名单后，它的群管理员密码匹配不到（视为陌生人）。
- 失败关闭（fail-closed，2026-10 复审）：配了 `get_settings` 的真 runtime 拿不到**任何**
  服务群证据（getter 抛异常 / 返回 None / 传了不可调用的东西；settings 没有 `groups`
  映射枚举不出服务群）时，服务群名单一律**空**、**零 SQL**——绝不退回「库里存过密码的
  群」。`has_password` / `match` / `fingerprint` 都先过这道闸门：不读非服务群的密码、
  不认旧密码、不给没有配置证据的群发 / 认 cookie。只有**根本没传 getter** 的显式 legacy
  纯数据层用法（老测试 / 老嵌入代码，兼容声明）才保留「库里存过密码的群」兜底。
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

from .group_approval import GroupApprovals

logger = logging.getLogger("maiwork.group_admins")

PW_PREFIX = "group_admin_pw."
# 旧按群名单键：0.8.0 起只作为迁移来源，不再是运行来源（见 group_approval.py）
ACCOUNTS_PREFIX = "group_admins."
MIN_PASSWORD_LEN = 8

# 密码重复时的统一文案（网页 400 / 分组校验都用它）
DUPLICATE_MESSAGE = "和别的群管理员密码重复了"
TOO_SHORT_MESSAGE = "群管理员密码至少 8 位"


def _no_settings() -> None:
    """「配了 getter、但配置不合法」的替身：让 approvals 也按失败关闭处理。"""
    return None


class GroupAdmins:
    """群管理员的密码与名单（数据层）。

    - `store`：Store（真库）。
    - `get_settings`：拿 Settings 的可调用对象；用来只知道**服务群**有哪些。
      **没传**（None）= 显式 legacy 纯数据层用法（老测试 / 老嵌入代码兼容声明）；
      **配了**却拿不到配置 → 一律拒绝（fail-closed），绝不退回 legacy 的宽松兜底。
    - `console_auth`：ConsoleAuth；`set_password` 拿它比对总管理员密码。
      构造后再接也认（`bind_console_auth`），避免 app 启动顺序上的循环依赖。
    """

    def __init__(
        self,
        store: Any,
        *,
        get_settings: Callable[[], Any] | None = None,
        console_auth: Any = None,
        approvals: GroupApprovals | None = None,
    ) -> None:
        self._store = store
        self._get_settings = get_settings
        self._auth = console_auth
        # 每群批准人名单：与「谁能批本群的活（含免批）」同一份来源。
        # 传了不可调用的东西 = 配置不合法：给 approvals 一个「配了、但拿不到配置」的
        # getter（不是 None），让它也走失败关闭路径，而不是悄悄退回 legacy 的宽松放行。
        if approvals is None:
            settings_getter = get_settings
            if settings_getter is not None and not callable(settings_getter):
                settings_getter = _no_settings
            approvals = GroupApprovals(store, get_settings=settings_getter)
        self.approvals = approvals

    def bind_console_auth(self, auth: Any) -> None:
        self._auth = auth

    # ------------------------------------------------------------------
    # 群
    # ------------------------------------------------------------------

    def served_gids(self) -> list[str]:
        """当前服务群名单（配置里列的）。

        失败关闭（2026-10 复审）：配了 getter 的真 runtime 拿不到**任何**服务群证据时，
        返回**空名单**且**零 SQL**——绝不退回 `_stored_gids()`（「库里存过密码的群」）：

        - getter 抛异常 / 返回 None；
        - `get_settings` 是不可调用的东西（配置不合法）；
        - settings 没有 `groups` 映射——只有 `is_served` 也只够判单个群、**枚举不出**
          服务群名单，这里按安全拒绝处理（记一行 debug 说明），不许退化成扫 `secrets`。

        `settings.groups` 是合法映射时按 key 返回。只有**根本没传 getter** 的显式
        legacy 纯数据层用法才退回「库里存过密码的群」（老兼容）。
        """
        if not self._runtime():
            return sorted(self._stored_gids())
        settings = self._settings()
        if settings is None:
            return []
        try:
            groups = getattr(settings, "groups", None)
        except Exception:
            logger.debug("读 settings.groups 失败，列不出服务群；按空名单处理、不扫库", exc_info=True)
            return []
        if groups is None:
            logger.debug("配置里没有 groups 映射，列不出服务群；按空名单处理、不扫库兜底")
            return []
        try:
            return sorted(str(g) for g in groups.keys())
        except Exception:
            logger.debug("读服务群名单失败，按空名单处理", exc_info=True)
            return []

    def _runtime(self) -> bool:
        """是不是「配了 getter 的真 runtime」。

        **只有根本没传**（`get_settings is None`）才算法定的 legacy 纯数据层用法。
        传了任何别的东西（哪怕是不可调用的对象）都算配置过了：配置不合法一律按拿不到
        处理，绝不悄悄退回 legacy 的宽松路径。
        """
        return self._get_settings is not None

    def _settings(self) -> Any:
        """取当前配置；配了 getter 却取不到（不是可调用的 / 抛异常 / 返回 None）→ None。"""
        if not self._runtime():
            return None
        getter = self._get_settings
        if not callable(getter):
            logger.warning("get_settings 不是可调用的（配置不合法），群管理员按拿不到配置处理")
            return None
        try:
            return getter()
        except Exception:
            logger.debug("读配置失败（群管理员按拿不到服务群证据处理）", exc_info=True)
            return None

    def _served(self, gid: Any) -> bool:
        """「这个群当前是服务群」有没有证据——唯一证据来源是 `served_gids()`。"""
        gid_s = str(gid or "").strip()
        return bool(gid_s) and gid_s in self.served_gids()

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
        """这个群现在算不算「有群管理员密码」。

        非服务群 / 拿不到服务群证据 → False，而且**不读**这个群的密码（失败关闭）。
        当前服务群上行为照旧。
        """
        if not self._served(gid):
            return False
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
        """用密码找出是哪个群的管理员；只认**当前服务群名单**，没有 / 不对 → None。

        名单拿不到任何证据时是空名单（见 `served_gids`），所以一个已保存的密码也匹配不到
        任何群——绝不会因为「配置读不出来」就拿库里存过的密码放行。
        """
        pw = str(password or "")
        if not pw:
            return None
        for gid in self.served_gids():
            if self._verify_hash(pw, self._stored_hash(gid)):
                return gid
        return None

    def fingerprint(self, gid: Any) -> str:
        """密码哈希的短指纹（cookie 签名用）。

        非服务群 / 拿不到服务群证据 → ""（旧 cookie 一律失效、也不发新 cookie），并且
        **不读**这个群的密码；当前服务群的指纹算法一个字没动。
        """
        if not self._served(gid):
            return ""
        stored = self._stored_hash(gid)
        if not stored:
            return ""
        return hashlib.sha256(f"mw-ga|{gid}|{stored}".encode("utf-8")).hexdigest()[:16]

    # ------------------------------------------------------------------
    # 名单（0.8.0：转发到 group_approval 那一份，不再自己写名单）
    # ------------------------------------------------------------------

    def accounts(self, gid: Any) -> list[str]:
        """本群批准人名单（规范化的「平台:账号」）——和每群批准名单同一来源。"""
        return list(self.approvals.accounts(gid))

    def set_accounts(self, gid: Any, accounts: Any) -> list[str]:
        """整份替换本群批准人；认不出的账号 → ValueError（中文，网页好提示）。

        只换 `approvers`，本群免批设置（exempt_users / exempt_group / required）原样保留。
        """
        return list(self.approvals.set_accounts(gid, accounts))

    def is_group_admin(self, gid: Any, user_id: Any, platform: str = "qq") -> bool:
        """这个（群，账号）是不是本群批准人名单里的人。"""
        uid = str(user_id or "").strip()
        if not uid:
            return False
        return bool(self.approvals.is_approver(gid, uid, platform=platform))
