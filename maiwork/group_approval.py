"""每群一个「谁能批准本群的活（含免批）」（docs/18 §五，0.8.0 归一）。

背景：以前「谁能批」散成三套——全局 `approval.admins`（config.toml）、每群
`kv["group_admins.<群号>"]`（群管理员名单，group_admins.py）、全局免批
（`approval.exempt_users` / `exempt_groups` / `required`）。三套各写各的，网页改了
一处另一处不生效。docs/18 §五 归一成**一个群一份**：

    kv["group_approval.<群号>"] = {
        "approvers":    ["qq:123", ...],   # 谁能批本群的活（群管理员名单也吃这一份）
        "exempt_users": ["qq:456", ...],   # 本群免批的人（平台按本群平台归一）
        "exempt_group": false,             # 本群整群免批
        "required":     true,              # 本群要不要批准（false = 全免批）
    }

迁移（惰性、幂等、只在**服务群**上做）：
- 第一次访问某个群时，若还没有这份记录，就从旧来源种一次：
  `approvers` = 全局 `approval.admins` ∪ 旧 `kv["group_admins.<群号>"]`（归一、去重保序）；
  `exempt_users` = 全局 `approval.exempt_users` 里属于本群平台的；
  `exempt_group` = 全局 `exempt_groups` 是否含「本群平台:群号」；`required` 照抄全局。
- 种完在**同一个事务**里删掉旧 `kv["group_admins.<群号>"]`（密码 `secrets` 一个字不动）。
- 种下以后**只认这一份**：不再和全局名单动态并集（删掉的批准人不能从全局绕回来）。
  旧全局字段暂时留着当「迁移种子父」，不是第二个运行来源。

数据层：不调模型、不调宿主、不发消息。写操作整份替换（先校验、后写，一次事务）。

失败关闭（fail-closed，2026-10 复审）：配了 `get_settings` 却拿不到配置、判服务群抛异常、
settings 里没有任何「服务群」证据、读库失败、已存记录坏掉——这些情况一律按**默认**处理
（要批、无批准人、无免批），并且**不读不写**；绝不在异常时把权限放宽或从旧全局名单重种
回已经被本群撤掉的管理员。只有**根本没配 getter** 的显式 legacy 纯数据层用法才保留旧放行行为。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Callable

from .config import norm_account

logger = logging.getLogger("maiwork.group_approval")

KV_PREFIX = "group_approval."
LEGACY_ACCOUNTS_PREFIX = "group_admins."

FIELDS: tuple[str, ...] = ("approvers", "exempt_users", "exempt_group", "required")


def _norm_accounts(values: Any) -> tuple[str, ...]:
    """账号列表归一成「平台:账号」，去重保序；认不出的丢掉（读取路径要宽容）。"""
    out: list[str] = []
    for x in (values if isinstance(values, (list, tuple)) else []):
        n = norm_account(x)
        if n and n not in out:
            out.append(n)
    return tuple(out)


@dataclass(frozen=True)
class GroupApproval:
    """一个群的批准名单（含免批）。"""

    approvers: tuple[str, ...] = ()
    exempt_users: tuple[str, ...] = ()
    exempt_group: bool = False
    required: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "approvers": list(self.approvers),
            "exempt_users": list(self.exempt_users),
            "exempt_group": bool(self.exempt_group),
            "required": bool(self.required),
        }


_DEFAULT = GroupApproval()

# kv_get 的默认哨兵：把「键不存在」和「存在但值是 null / 解不开」区分开。
_MISSING = object()


def _seed_ready(settings: Any) -> bool:
    """这轮允不允许「缺失就惰性播种」。

    - 显式 legacy（`settings is None`，老纯数据层调用）→ True（老合同照旧）。
    - 真 Settings：认派生字段 `group_controls_seed_ready`——缺省 True（老合同）；
      App 在「旧配置覆盖层没物化成功」时显式设 False → 不播种，只回安全默认。
      字段不是真 bool（坏值）→ 按 False（收紧，不放开）。
    """
    raw = getattr(settings, "group_controls_seed_ready", True)
    return raw if isinstance(raw, bool) else False


def _strict_bool(raw: Any, *, default: bool) -> bool:
    """读取路径的布尔：只认真 true / false。

    坏值（字符串 "false"、1/0、None…）一律取**更安全**的一侧——免批 → False、要批 → True。
    旧实现 `bool(raw)` 会有 `bool("false") is True`：`exempt_group` 字段一损坏就整群免批。
    """
    return raw if isinstance(raw, bool) else default


def _parse_stored(raw: Any) -> GroupApproval | None:
    """库里读到的记录 → GroupApproval；不是对象 → None（调用方按默认拒绝，绝不重种）。"""
    if not isinstance(raw, Mapping):
        return None
    return GroupApproval(
        approvers=_norm_accounts(raw.get("approvers")),
        exempt_users=_norm_accounts(raw.get("exempt_users")),
        exempt_group=_strict_bool(raw.get("exempt_group", False), default=False),
        required=_strict_bool(raw.get("required", True), default=True),
    )


class GroupApprovals:
    """每群批准名单的读写（真来源：kv["group_approval.<群号>"]）。

    - `store`：Store。
    - `get_settings`：拿 Settings 的可调用对象；用来认平台、认服务群、取旧全局种子。
      **没配**（None）时 = 显式 legacy 纯数据层用法：视为服务群、平台按 qq（老测试兼容）。
      **配了**却拿不到配置，或 settings 里没有任何服务群证据 → 一律拒绝（fail-closed）。
    """

    def __init__(self, store: Any, *, get_settings: Callable[[], Any] | None = None) -> None:
        self._store = store
        self._get_settings = get_settings

    # ------------------------------------------------------------------
    # 上下文
    # ------------------------------------------------------------------

    def _has_getter(self) -> bool:
        """有没有显式配置的 getter；**只有 None** 才是「legacy 纯数据层」放行路径。

        传了任何别的东西（哪怕是个不可调用的对象 = 接线错误）都算「配过了」：一律按
        拿不到配置处理（fail-closed），绝不悄悄退回 legacy 的宽松放行。
        """
        return self._get_settings is not None

    def _settings(self) -> Any:
        if not self._has_getter():
            return None
        getter = self._get_settings
        if not callable(getter):
            logger.warning("get_settings 不是可调用的（配置不合法），每群批准名单按拒绝处理")
            return None
        try:
            return getter()
        except Exception:
            logger.debug("读配置失败（每群批准名单按拒绝处理）", exc_info=True)
            return None

    def _seed_allowed(self) -> bool:
        """缺失记录时能不能惰性播种（配了 getter 却拿不到配置 → 一律不播）。"""
        if not self._has_getter():
            return True
        settings = self._settings()
        return settings is not None and _seed_ready(settings)

    def _approval(self) -> Any:
        settings = self._settings()
        return getattr(settings, "approval", None) if settings is not None else None

    def platform_of(self, gid: Any) -> str:
        """本群平台（qq / telegram …）。

        - 没配 getter（legacy 纯数据层）/ settings 没有 `platform_of`：按 qq（老行为）。
        - `platform_of` 抛异常：返回 ""（**认不出平台，不猜成 qq**）——免批按平台过滤一律不通过。
        """
        if not self._has_getter():
            return "qq"
        settings = self._settings()
        fn = getattr(settings, "platform_of", None)
        if not callable(fn):
            return "qq"
        try:
            return str(fn(str(gid)) or "qq") or "qq"
        except Exception:
            logger.debug("认本群平台失败（%s），按未知平台处理", gid, exc_info=True)
            return ""

    def is_served(self, gid: Any) -> bool:
        """只服务配置里列出的群；拿不到服务群证据就拒绝（fail-closed）。

        - 没配 getter（显式 legacy 纯数据层）：放行。
        - 配了 getter 但调用抛异常 / 返回 None：拒绝，**零 SQL**（生产读配置失败时不许读写陌生群）。
        - settings 有 `is_served`：照它判；它抛异常也拒绝。
        - settings 没有 `is_served`、但有 `groups` 映射：按映射成员判定。
        - 两者都没有：没有任何服务群证据 → 拒绝。
        """
        if not self._has_getter():
            return True
        settings = self._settings()
        if settings is None:
            return False
        fn = getattr(settings, "is_served", None)
        if callable(fn):
            try:
                return bool(fn(str(gid)))
            except Exception:
                logger.debug("判服务群失败（%s），按不服务处理", gid, exc_info=True)
                return False
        groups = getattr(settings, "groups", None)
        if groups is not None:
            try:
                return str(gid) in groups
            except Exception:
                logger.debug("按 groups 判服务群失败（%s），按不服务处理", gid, exc_info=True)
                return False
        logger.debug("配置里没有服务群证据（%s），按不服务处理", gid)
        return False

    # ------------------------------------------------------------------
    # 读（含惰性迁移种子）
    # ------------------------------------------------------------------

    def get(self, gid: Any) -> GroupApproval:
        """本群记录；还没有 → 从旧来源种一次（幂等）。

        非服务群 / 拿不到配置 / 读失败：不读不写，按默认（要批、无批准人、无免批）。
        键存在但记录坏掉（不是对象、是 null）：也按默认拒绝，**不重种**——重种会把
        旧全局名单里已经被本群撤掉的批准人放回来。
        """
        gid_s = str(gid or "").strip()
        if not gid_s or not self.is_served(gid_s):
            return _DEFAULT
        key = KV_PREFIX + gid_s
        try:
            raw = self._store.kv_get(key, _MISSING)
        except Exception:
            logger.debug("读每群批准名单失败（%s），按默认处理", gid_s, exc_info=True)
            return _DEFAULT
        if raw is _MISSING:
            if not self._seed_allowed():
                # 群控归一没迁成功（App 的 group_controls_seed_ready 门关着）：缺失记录
                # 只回安全默认，绝不拿这份「没被旧覆盖盖过的有效值」当种子落库。
                logger.info(
                    "群 %s 的批准名单还没从旧的全局设置迁过来，缺失记录按默认处理、不播种", gid_s
                )
                return _DEFAULT
            return self._seed(gid_s, key)
        rec = _parse_stored(raw)
        if rec is None:
            logger.warning("每群批准名单记录坏掉（群 %s），按默认处理、不重种", gid_s)
            return _DEFAULT
        return rec

    def view(self, gid: Any) -> dict[str, Any]:
        """给网页的形态：{approvers, exempt_users, exempt_group, required}（精确四字段）。"""
        return self.get(gid).as_dict()

    def accounts(self, gid: Any) -> list[str]:
        """本群批准人（旧「群管理员名单」也读这一份）。"""
        return list(self.get(gid).approvers)

    def is_approver(self, gid: Any, user_id: Any, platform: str = "qq") -> bool:
        uid = str(user_id or "").strip()
        if not uid:
            return False
        target = norm_account(f"{platform or 'qq'}:{uid}")
        return bool(target) and target in self.get(gid).approvers

    def is_exempt_user(self, gid: Any, user_id: Any, platform: str | None = None) -> bool:
        uid = str(user_id or "").strip()
        if not uid:
            return False
        plat = platform or self.platform_of(gid)
        target = norm_account(f"{plat}:{uid}")
        return bool(target) and target in self.get(gid).exempt_users

    def is_exempt(self, gid: Any, requester_id: Any, platform: str | None = None) -> bool:
        """这个人在这个群派活要不要批（True = 免批直接落地）。"""
        rec = self.get(gid)
        if not rec.required:
            return True
        if rec.exempt_group:
            return True
        return self.is_exempt_user(gid, requester_id, platform)

    # ------------------------------------------------------------------
    # 写（整份替换；校验在前、写在后）
    # ------------------------------------------------------------------

    @staticmethod
    def parse_payload(body: Any) -> GroupApproval:
        """校验一份完整 payload（PUT 全量字段）。任何问题抛 ValueError（中文，网页好提示）。

        规则：必须是对象；四个字段一个不能少、也不许多；账号要认得出；两个布尔要真是布尔。
        **先校验完再写**——校验不过时一个字段都不落库。
        """
        if not isinstance(body, Mapping):
            raise ValueError("请求体要是对象")
        keys = {str(k) for k in body.keys()}
        missing = [f for f in FIELDS if f not in keys]
        if missing:
            raise ValueError("缺少字段：" + "、".join(missing))
        unknown = sorted(keys - set(FIELDS))
        if unknown:
            raise ValueError("不认识的字段：" + "、".join(unknown))
        for f in FIELDS:
            if f in ("exempt_group", "required") and not isinstance(body[f], bool):
                raise ValueError(f"「{f}」要是 true / false")
            if f in ("approvers", "exempt_users") and not isinstance(body[f], (list, tuple)):
                raise ValueError(f"「{f}」要是数组，比如 [\"qq:123456\"]")
        approvers = GroupApprovals._require_accounts(body["approvers"], "批准人")
        exempt_users = GroupApprovals._require_accounts(body["exempt_users"], "免批的人")
        return GroupApproval(
            approvers=approvers,
            exempt_users=exempt_users,
            exempt_group=bool(body["exempt_group"]),
            required=bool(body["required"]),
        )

    @staticmethod
    def _require_accounts(values: Iterable[Any], field_zh: str) -> tuple[str, ...]:
        raw = list(values) if isinstance(values, (list, tuple)) else []
        out: list[str] = []
        for x in raw:
            n = norm_account(x)
            if not n:
                raise ValueError(f"{field_zh}里的「{x}」认不出，要写成 qq:123456")
            if n not in out:
                out.append(n)
        return tuple(out)

    def save(self, gid: Any, rec: GroupApproval) -> GroupApproval:
        """整份写入（事务内一条 upsert）；非服务群拒绝。"""
        gid_s = str(gid or "").strip()
        if not gid_s:
            raise ValueError("群号不能是空的")
        if not self.is_served(gid_s):
            raise ValueError("没有这个群（只支持服务群）")
        if not isinstance(rec, GroupApproval):
            raise ValueError("记录形态不对")
        # 落库一律规范形态（账号归一、布尔化），不信任调用方给的原样值
        rec = GroupApproval(
            approvers=_norm_accounts(rec.approvers),
            exempt_users=_norm_accounts(rec.exempt_users),
            exempt_group=bool(rec.exempt_group),
            required=bool(rec.required),
        )
        with self._store.tx() as conn:
            self._store.kv_set(conn, KV_PREFIX + gid_s, rec.as_dict())
        return rec

    def set(self, gid: Any, body: Any) -> GroupApproval:
        """校验 + 写入（PUT 用）：先 `parse_payload` 全量校验，再落库。"""
        return self.save(gid, self.parse_payload(body))

    def set_accounts(self, gid: Any, accounts: Any) -> list[str]:
        """旧「群管理员名单」兼容写：只换 approvers，其余字段原样保留，返回新名单。"""
        gid_s = str(gid or "").strip()
        if not gid_s:
            raise ValueError("群号不能是空的")
        if accounts is None:
            accounts = []
        if not isinstance(accounts, (list, tuple)):
            raise ValueError("名单要是数组，比如 [\"qq:123456\"]")
        approvers = self._require_accounts(accounts, "名单")
        current = self.get(gid_s)
        self.save(gid_s, GroupApproval(
            approvers=approvers,
            exempt_users=current.exempt_users,
            exempt_group=current.exempt_group,
            required=current.required,
        ))
        return list(approvers)

    # ------------------------------------------------------------------
    # 迁移种子
    # ------------------------------------------------------------------

    def _seed(self, gid: str, key: str) -> GroupApproval:
        """从旧来源种一次（幂等；同一事务里写新记录 + 删旧按群名单键）。

        失败关闭：事务里二次读失败、发现已存记录坏掉、写失败，都**不覆盖、不删旧键**，
        返回默认（要批、无批准人）；绝不把「从旧全局种出来的名单」当结果返回——那等于
        没落库也放行。门关着（群控归一没迁成功）时也一律不种。
        """
        if not self._seed_allowed():
            return _DEFAULT
        payload = self._seed_payload(gid)
        try:
            with self._store.tx() as conn:
                row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
                if row is not None:
                    existing = _parse_stored(_loads(row["value"]))
                    if existing is not None:
                        return existing
                    logger.warning("每群批准名单记录坏掉（群 %s），不重种", gid)
                    return _DEFAULT
                self._store.kv_set(conn, key, payload.as_dict())
                self._store.kv_delete(conn, LEGACY_ACCOUNTS_PREFIX + gid)
        except Exception:
            logger.exception("种每群批准名单失败（群 %s），按默认处理", gid)
            return _DEFAULT
        logger.info("群 %s 的批准名单已归一到 group_approval（旧按群名单键已删）", gid)
        return payload

    def _seed_payload(self, gid: str) -> GroupApproval:
        approval = self._approval()
        plat = self.platform_of(gid)
        global_admins = getattr(approval, "admins", ()) if approval is not None else ()
        approvers = list(_norm_accounts(global_admins))
        for n in self._legacy_accounts(gid):
            if n not in approvers:
                approvers.append(n)
        # 平台认不出来（""）时：免批一律不种（宁可不免，也不许猜成 qq 放宽）
        exempt_users = tuple(
            n for n in _norm_accounts(getattr(approval, "exempt_users", ()) if approval is not None else ())
            if plat and n.split(":", 1)[0] == str(plat).lower()
        )
        gkey = norm_account(f"{plat}:{gid}") if plat else ""
        exempt_group = bool(gkey) and any(
            norm_account(g) == gkey
            for g in (getattr(approval, "exempt_groups", ()) if approval is not None else ())
        )
        required = bool(getattr(approval, "required", True)) if approval is not None else True
        return GroupApproval(
            approvers=tuple(approvers),
            exempt_users=exempt_users,
            exempt_group=exempt_group,
            required=required,
        )

    def _legacy_accounts(self, gid: str) -> list[str]:
        """旧的 kv["group_admins.<群号>"]（读失败 / 没有 → []）。"""
        try:
            raw = self._store.kv_get(LEGACY_ACCOUNTS_PREFIX + gid, [])
        except Exception:
            logger.debug("读旧按群名单失败（群 %s）", gid, exc_info=True)
            return []
        return list(_norm_accounts(raw))


def _loads(raw: Any) -> Any:
    import json

    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None
