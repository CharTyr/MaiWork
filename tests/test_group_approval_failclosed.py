"""每群批准名单的**失败关闭**（fail-closed）契约（2026-10 复审）。

背景：`group_approval.py` 是「谁能批本群的活 / 谁免批」的唯一真来源。复审发现四处
失败开放（fail-open），任何一种都会让权限在异常时**放宽**，必须在异常时**收紧**：

1. `get_settings` 是 callable、但调用抛异常 / 返回 None 时，旧实现把 settings 当 None，
   `is_served` 找不到方法就返回 True → 生产里读配置一失败，MaiWork 就会去读写
   **配置里根本没列出的陌生群**（红线：非服务群零读取）。修：配了 getter 时
   拿不到 settings、判服务群抛异常、或 settings 没有任何服务群证据 → 一律拒绝，且**零 SQL**。
   只有**根本没配 getter**的显式 legacy 纯数据层用法才保留放行。
2. `kv_get` 抛异常时旧实现 rec=None → 走种子迁移，可能从**旧全局名单**重新种回
   已经被本群撤掉的管理员 / 免批。修：读失败不种、不重写，直接按默认（要批、无批准人、无免批）。
3. 库里已存的记录坏掉（不是对象、字段是坏串）时旧实现 `bool("false") is True`
   → `exempt_group` 字段一损坏就整群免批。修：读取路径只认真 bool，坏值取**更安全**一侧
   （免批 → False，要批 → True）。
4. 迁移种子事务里的**二次读**失败 / 坏记录，旧实现会覆盖并删旧键。修：同样不种、不写。

这些用例都先写先红：旧实现下应当失败，改完变绿。服务群移除、平台不同都不改变结论。
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.group_approval import GroupApproval, GroupApprovals
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"
G2 = "123456789"
QQ_ADMIN = "10001"
LEGACY_ADMIN = "30003"


# ======================================================================
# 脚手架
# ======================================================================


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "failclosed.db")
    store.migrate()
    return store


class SpyStore:
    """包一层真 Store，数「有没有碰库」并按需注入读失败。"""

    def __init__(self, inner: Store) -> None:
        self._inner = inner
        self.kv_get_calls: list[str] = []
        self.kv_set_calls: list[str] = []
        self.kv_delete_calls: list[str] = []
        self.tx_count = 0
        self.kv_get_error: Exception | None = None
        self.tx_read_error = False
        self.kv_set_error: Exception | None = None

    def kv_get(self, key: str, default: Any = None) -> Any:
        self.kv_get_calls.append(str(key))
        if self.kv_get_error is not None:
            raise self.kv_get_error
        return self._inner.kv_get(key, default)

    def kv_set(self, conn: Any, key: str, value: Any) -> None:
        self.kv_set_calls.append(str(key))
        if self.kv_set_error is not None:
            raise self.kv_set_error
        return self._inner.kv_set(conn, key, value)

    def kv_delete(self, conn: Any, key: str) -> None:
        self.kv_delete_calls.append(str(key))
        return self._inner.kv_delete(conn, key)

    @contextmanager
    def tx(self):
        self.tx_count += 1
        with self._inner.tx() as conn:
            yield _TxConn(conn) if self.tx_read_error else conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _TxConn:
    """事务内的连接代理：让「SELECT value FROM kv」这一句炸掉（模拟事务里二次读失败）。"""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, sql: str, *args: Any) -> Any:
        if "FROM kv" in sql:
            raise RuntimeError("事务里二次读失败")
        return self._conn.execute(sql, *args)


def _settings(serve=("qq:" + G1,)):
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": g} for g in serve]},
        "approval": {"required": True, "admins": ["qq:" + QQ_ADMIN]},
    }
    settings, problems = load_settings(raw)
    assert not problems, problems
    return settings


class _BareSettings:
    """只有 approval 的老式假配置：既没有 is_served 也没有 groups。"""

    def __init__(self) -> None:
        self.approval = SimpleNamespace(
            required=True, admins=("qq:" + QQ_ADMIN,), exempt_users=(), exempt_groups=()
        )


class _GroupsOnlySettings:
    """有 groups 映射、但没有 is_served。"""

    def __init__(self, groups: dict[str, Any]) -> None:
        self.groups = groups
        self.approval = SimpleNamespace(
            required=True, admins=("qq:" + QQ_ADMIN,), exempt_users=(), exempt_groups=()
        )


class _RaisingServed:
    def __init__(self) -> None:
        self.approval = SimpleNamespace(
            required=True, admins=("qq:" + QQ_ADMIN,), exempt_users=(), exempt_groups=()
        )

    def is_served(self, gid: str) -> bool:
        raise RuntimeError("判服务群炸了")


class _RaisingPlatform:
    def __init__(self) -> None:
        self.approval = SimpleNamespace(
            required=True, admins=("qq:" + QQ_ADMIN,), exempt_users=(), exempt_groups=()
        )

    def is_served(self, gid: str) -> bool:
        return True

    def platform_of(self, gid: str) -> str:
        raise RuntimeError("认平台炸了")


DENY = GroupApproval()


def _put_raw(store: Store, key: str, value: Any) -> None:
    with store.tx() as conn:
        store.kv_set(conn, key, value)


def _put_text(store: Store, key: str, text: str) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO kv (key, value, updated) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, text, 0.0),
        )


def _raw_text(store: Store, key: str) -> Any:
    row = store.read().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return None if row is None else row["value"]


# ======================================================================
# 1. 配置拿不到 / 判服务群异常 → 失败关闭、零 SQL
# ======================================================================


class TestFailClosedSettings:
    def test_configured_getter_exception_denies_with_zero_sql(self, tmp_path: Path) -> None:
        store = SpyStore(_store(tmp_path))

        def boom() -> Any:
            raise RuntimeError("读配置炸了")

        gv = GroupApprovals(store, get_settings=boom)
        assert gv.get(G1) == DENY
        assert store.kv_get_calls == [], "拿不到配置时不许读库"
        assert store.tx_count == 0, "拿不到配置时不许开事务"
        assert store.kv_set_calls == [] and store.kv_delete_calls == []
        assert store.kv_get("group_approval." + G1, None) is None

    def test_configured_getter_returning_none_denies_with_zero_sql(self, tmp_path: Path) -> None:
        store = SpyStore(_store(tmp_path))
        gv = GroupApprovals(store, get_settings=lambda: None)
        assert gv.get(G1) == DENY
        assert store.kv_get_calls == []
        assert store.tx_count == 0
        assert store.kv_get("group_approval." + G1, None) is None

    def test_noncallable_getter_is_not_legacy_permissive(self, tmp_path: Path) -> None:
        """直接构造 helper 传不可调用的东西：算「配过了但不合法」→ 拒绝，绝不退回 legacy 放行。"""
        for bad in (object(), "随便一个字符串", 12345):
            store = SpyStore(_store(tmp_path))
            gv = GroupApprovals(store, get_settings=bad)
            assert gv.is_served(G1) is False, bad
            assert gv.get(G1) == DENY, bad
            assert store.kv_get_calls == [], bad        # 配置不合法时不许读库
            assert store.tx_count == 0, bad             # 配置不合法时不许开事务
            assert store.kv_set_calls == [] and store.kv_delete_calls == [], bad
            assert store.kv_get("group_approval." + G1, None) is None, bad

    def test_served_check_exception_denies_with_zero_sql(self, tmp_path: Path) -> None:
        store = SpyStore(_store(tmp_path))
        gv = GroupApprovals(store, get_settings=lambda: _RaisingServed())
        assert gv.get(G1) == DENY
        assert gv.is_served(G1) is False
        assert store.kv_get_calls == []
        assert store.tx_count == 0

    def test_settings_without_any_served_evidence_denies(self, tmp_path: Path) -> None:
        store = SpyStore(_store(tmp_path))
        gv = GroupApprovals(store, get_settings=lambda: _BareSettings())
        assert gv.is_served(G1) is False
        assert gv.get(G1) == DENY
        assert store.kv_get_calls == []
        assert store.tx_count == 0

    def test_groups_mapping_used_when_is_served_missing(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        gv = GroupApprovals(store, get_settings=lambda: _GroupsOnlySettings({G1: object()}))
        assert gv.is_served(G1) is True
        assert gv.is_served(G2) is False
        _put_raw(store, "group_approval." + G1, {
            "approvers": ["qq:9"], "exempt_users": [], "exempt_group": False, "required": True,
        })
        assert gv.get(G1).approvers == ("qq:9",)
        assert gv.get(G2) == DENY

    def test_unserved_group_denies_with_zero_sql(self, tmp_path: Path) -> None:
        store = SpyStore(_store(tmp_path))
        gv = GroupApprovals(store, get_settings=lambda: _settings())
        assert gv.get(G2) == DENY
        assert store.kv_get_calls == []
        assert store.tx_count == 0

    def test_group_removed_from_serve_denies_even_with_record(self, tmp_path: Path) -> None:
        inner = _store(tmp_path)
        store = SpyStore(inner)
        _put_raw(inner, "group_approval." + G2, {
            "approvers": ["qq:9"], "exempt_users": [], "exempt_group": True, "required": False,
        })
        gv = GroupApprovals(store, get_settings=lambda: _settings(serve=("qq:" + G1,)))
        assert gv.get(G2) == DENY
        assert gv.is_exempt(G2, "9") is False
        assert store.kv_get_calls == []

    def test_save_on_broken_settings_writes_nothing(self, tmp_path: Path) -> None:
        store = SpyStore(_store(tmp_path))

        def boom() -> Any:
            raise RuntimeError("读配置炸了")

        gv = GroupApprovals(store, get_settings=boom)
        try:
            gv.save(G1, GroupApproval(approvers=("qq:9",)))
        except ValueError:
            pass
        else:  # pragma: no cover - 旧实现会在这里悄悄写库
            raise AssertionError("拿不到配置时 save 必须拒绝")
        assert store.tx_count == 0
        assert store.kv_set_calls == []

    def test_platform_of_exception_does_not_guess_exemption(self, tmp_path: Path) -> None:
        """认不出平台时不许猜成 qq 去种全局免批人（批准人账号自带平台，照种无害）。"""
        store = _store(tmp_path)
        settings = _RaisingPlatform()
        settings.approval = SimpleNamespace(
            required=True,
            admins=("qq:" + QQ_ADMIN,),
            exempt_users=("qq:" + QQ_ADMIN,),
            exempt_groups=("qq:" + G1,),
        )
        gv = GroupApprovals(store, get_settings=lambda: settings)
        rec = gv.get(G1)
        assert rec.required is True
        assert rec.exempt_users == () and rec.exempt_group is False
        assert gv.is_exempt_user(G1, QQ_ADMIN) is False
        assert gv.is_exempt(G1, QQ_ADMIN) is False
        assert rec.approvers == ("qq:" + QQ_ADMIN,)

    def test_no_getter_legacy_mode_still_permissive(self, tmp_path: Path) -> None:
        """显式 legacy 纯数据层用法（根本不配 getter）保留旧的放行行为。"""
        store = _store(tmp_path)
        gv = GroupApprovals(store)  # 没有 getter
        assert gv.is_served("999999") is True
        rec = gv.get(G1)
        assert rec == GroupApproval()  # 没有全局来源可种 → 默认要批、无批准人
        assert store.kv_get("group_approval." + G1, None) is not None


# ======================================================================
# 2. 读记录的异常 / 坏数据 → 不种、不重写
# ======================================================================


class TestFailClosedStoredRecord:
    def test_kv_read_error_does_not_seed_from_global(self, tmp_path: Path) -> None:
        inner = _store(tmp_path)
        _put_raw(inner, "group_admins." + G1, [LEGACY_ADMIN])
        store = SpyStore(inner)
        store.kv_get_error = RuntimeError("kv 读炸了")
        gv = GroupApprovals(store, get_settings=lambda: _settings())
        assert gv.get(G1) == DENY
        assert store.kv_set_calls == [], "读失败不许种新记录"
        assert store.kv_delete_calls == [], "读失败不许删旧键"
        assert inner.kv_get("group_approval." + G1, None) is None
        assert inner.kv_get("group_admins." + G1, None) == [LEGACY_ADMIN]

    def test_kv_read_error_does_not_rewrite_existing_record(self, tmp_path: Path) -> None:
        inner = _store(tmp_path)
        _put_raw(inner, "group_approval." + G1, {
            "approvers": ["qq:9"], "exempt_users": [], "exempt_group": False, "required": True,
        })
        before = _raw_text(inner, "group_approval." + G1)
        store = SpyStore(inner)
        store.kv_get_error = RuntimeError("kv 读炸了")
        gv = GroupApprovals(store, get_settings=lambda: _settings())
        assert gv.get(G1) == DENY
        assert store.kv_set_calls == [] and store.kv_delete_calls == []
        assert _raw_text(inner, "group_approval." + G1) == before

    def test_corrupt_bool_strings_are_not_exempt(self, tmp_path: Path) -> None:
        inner = _store(tmp_path)
        _put_raw(inner, "group_approval." + G1, {
            "approvers": ["qq:9"], "exempt_users": [],
            "exempt_group": "false", "required": "true",
        })
        before = _raw_text(inner, "group_approval." + G1)
        gv = GroupApprovals(inner, get_settings=lambda: _settings())
        rec = gv.get(G1)
        assert rec.exempt_group is False, "坏串 'false' 不许被当成 truthy 整群免批"
        assert rec.required is True
        assert rec.approvers == ("qq:9",)
        assert gv.is_exempt(G1, "99999") is False
        assert _raw_text(inner, "group_approval." + G1) == before, "坏字段只收紧、不回写"

    def test_truthy_corrupt_bools_take_the_safe_side(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _put_raw(store, "group_approval." + G1, {
            "approvers": [], "exempt_users": [], "exempt_group": 1, "required": 0,
        })
        gv = GroupApprovals(store, get_settings=lambda: _settings())
        rec = gv.get(G1)
        assert rec.exempt_group is False  # 坏值不放开免批
        assert rec.required is True       # 坏值不放开「不用批」
        assert gv.is_exempt(G1, "99999") is False

    def test_non_mapping_record_is_not_seeded(self, tmp_path: Path) -> None:
        inner = _store(tmp_path)
        _put_raw(inner, "group_admins." + G1, [LEGACY_ADMIN])
        for bad in ("nope", [1, 2], 123):
            _put_raw(inner, "group_approval." + G1, bad)
            store = SpyStore(inner)
            gv = GroupApprovals(store, get_settings=lambda: _settings())
            assert gv.get(G1) == DENY, bad
            assert store.kv_set_calls == [], bad
            assert store.kv_delete_calls == [], bad
            assert inner.kv_get("group_approval." + G1, None) == bad
            assert inner.kv_get("group_admins." + G1, None) == [LEGACY_ADMIN]

    def test_null_record_is_not_seeded(self, tmp_path: Path) -> None:
        store = SpyStore(_store(tmp_path))
        _put_text(store, "group_approval." + G1, "null")
        gv = GroupApprovals(store, get_settings=lambda: _settings())
        assert gv.get(G1) == DENY
        assert store.kv_set_calls == []

    def test_missing_key_still_seeds_once(self, tmp_path: Path) -> None:
        """没配坏、没读坏时，首次访问照旧惰性迁移（不要矫枉过正）。"""
        inner = _store(tmp_path)
        _put_raw(inner, "group_admins." + G1, [LEGACY_ADMIN])
        gv = GroupApprovals(inner, get_settings=lambda: _settings())
        rec = gv.get(G1)
        assert rec.approvers == ("qq:" + QQ_ADMIN, "qq:" + LEGACY_ADMIN)
        assert inner.kv_get("group_admins." + G1, None) is None

    def test_existing_valid_record_is_returned_untouched(self, tmp_path: Path) -> None:
        inner = _store(tmp_path)
        _put_raw(inner, "group_approval." + G1, {
            "approvers": ["qq:9"], "exempt_users": ["qq:8"],
            "exempt_group": False, "required": True,
        })
        before = _raw_text(inner, "group_approval." + G1)
        store = SpyStore(inner)
        gv = GroupApprovals(store, get_settings=lambda: _settings())
        assert gv.get(G1).approvers == ("qq:9",)
        assert store.kv_set_calls == [] and store.kv_delete_calls == []
        assert _raw_text(inner, "group_approval." + G1) == before


# ======================================================================
# 3. 迁移种子事务：二次读失败 / 坏记录 / 写失败 → 不覆盖、不删旧键
# ======================================================================


class TestFailClosedSeedTx:
    def test_seed_tx_corrupt_json_row_is_not_overwritten(self, tmp_path: Path) -> None:
        """kv_get 对坏 JSON 会返回默认（看着像「没有」）；事务里二次读必须兜住。"""
        inner = _store(tmp_path)
        _put_text(inner, "group_approval." + G1, "{这不是 json")
        _put_raw(inner, "group_admins." + G1, [LEGACY_ADMIN])
        before = _raw_text(inner, "group_approval." + G1)
        store = SpyStore(inner)
        gv = GroupApprovals(store, get_settings=lambda: _settings())
        assert gv.get(G1) == DENY
        assert store.kv_set_calls == [], "坏记录不许被覆盖重种"
        assert store.kv_delete_calls == [], "坏记录不许删旧按群名单"
        assert _raw_text(inner, "group_approval." + G1) == before
        assert inner.kv_get("group_admins." + G1, None) == [LEGACY_ADMIN]

    def test_seed_tx_second_read_failure_denies_without_write(self, tmp_path: Path) -> None:
        inner = _store(tmp_path)
        _put_raw(inner, "group_admins." + G1, [LEGACY_ADMIN])
        store = SpyStore(inner)
        store.tx_read_error = True
        gv = GroupApprovals(store, get_settings=lambda: _settings())
        assert gv.get(G1) == DENY
        assert store.kv_set_calls == [] and store.kv_delete_calls == []
        assert inner.kv_get("group_approval." + G1, None) is None
        assert inner.kv_get("group_admins." + G1, None) == [LEGACY_ADMIN]

    def test_seed_write_failure_does_not_return_global_admins(self, tmp_path: Path) -> None:
        """写种子失败时旧实现把「从全局种出来的名单」当结果返回——相当于没落库也放行。"""
        inner = _store(tmp_path)
        store = SpyStore(inner)
        store.kv_set_error = RuntimeError("写炸了")
        gv = GroupApprovals(store, get_settings=lambda: _settings())
        assert gv.get(G1) == DENY
        assert inner.kv_get("group_approval." + G1, None) is None
