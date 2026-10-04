"""群管理员（group_admins.py）的**失败关闭**契约（2026-10 复审）。

背景（复审发现的一处失败开放）：`GroupAdmins.served_gids()` 原来在「配了
`get_settings`、但拿不到配置」时退回 `_stored_gids()`——也就是「库里存过群管理员密码的
所有群」。对真 runtime 来说这是 fail-open：

- `match()` 拿这份兜底名单比对密码 → 配置没了 / 读不出来时，**已保存的**群管理员密码
  照样匹配成功；
- `fingerprint()` 完全不看服务群 → 群被移出服务名单、或配置根本读不出来时，
  已发出的群管理员 cookie 仍然验签通过（`ConsoleAuth.check_group_cookie` 用的就是它）；
- 红线「非服务群零读取」也被破坏：拿不到配置时反而去扫 `secrets` 表里所有存过密码的群。

修法（两种用法分开，不混语义）：

- **根本没传 getter**（`get_settings=None`，显式 legacy 纯数据层，老测试 / 老嵌入用法）：
  保持旧兼容——退回「库里存过密码的群」。
- **配了 getter 的真 runtime**：拿不到任何服务群证据就返回**空名单**，并且**零 SQL**
  （不查 `_stored_gids`、不猜、不扫 `secrets`）：
  - getter 抛异常 / 返回 None；settings 没有 `groups` 映射（只有 `is_served` 也只够判
    单个群、**枚举不出**服务群，不许退化成扫库）→ 空名单 + 一行 debug 说明；
  - `get_settings` 传了**不可调用的东西**（任意 obj）→ 严格判「配置不合法」→ 同样拒绝；
  - `settings.groups` 是合法映射 → 按 key 返回服务群。
- `has_password()` / `match()` / `fingerprint()` 三个（读密码的）入口都先过同一道
  「当前服务群名单」闸门：非服务群 / 拿不到证据 → 不读这个群的密码、不认、不签 cookie，
  也**不发**新 cookie。**当前服务群**上的行为（比对 / 指纹 / 哈希格式 / 最少 8 位 /
  不回显）一个字不动。

这些用例先写先红：旧实现下应当失败，改完变绿。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.console.auth import ConsoleAuth
from CharTyr_MaiWork.maiwork.group_admins import GroupAdmins
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"
G2 = "123456789"
G3 = "555000111"
ADMIN_PW = "总管理员密码-ga-failclosed-1234"
PW1 = "群一管理员密码-failclosed"
PW2 = "群二管理员密码-failclosed"
PW3 = "群三管理员密码-failclosed"


# ======================================================================
# 脚手架
# ======================================================================


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "ga_failclosed.db")
    store.migrate()
    return store


class SpyStore:
    """包一层真 Store，数「读表 / 读密码」各几次；行为全走真库。"""

    def __init__(self, inner: Store) -> None:
        self._inner = inner
        self.read_calls = 0
        self.secret_get_keys: list[str] = []

    def read(self) -> Any:
        self.read_calls += 1
        return self._inner.read()

    def secret_get(self, key: str) -> Any:
        self.secret_get_keys.append(str(key))
        return self._inner.secret_get(key)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _settings(*gids: str) -> Any:
    """真 Settings（load_settings）——服务群就是这几个。"""
    settings, problems = load_settings(
        {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{g}"} for g in gids]},
            "console": {"password": ADMIN_PW},
            "approval": {"admins": []},
        }
    )
    assert not problems, problems
    return settings


def _seed_password(store: Store, gid: str, pw: str) -> None:
    """用 legacy（根本不传 getter）写法把密码落进真库，模拟「配置撤掉前就存过的密码」。"""
    GroupAdmins(store).set_password(gid, pw)


def _boom() -> Any:
    """生产 `MaiWorkApp.get_settings` 拿不到配置时的形状（assert / 抛异常）。"""
    raise AssertionError("_settings is not None")


class _GroupsOnlySettings:
    """只有 groups 映射、没有 is_served 的最小配置形态（合法的枚举来源）。"""

    def __init__(self, groups: dict[str, Any]) -> None:
        self.groups = groups


class _ServedOnlySettings:
    """只有 is_served、没有 groups：能判单个群，但**枚举不出**服务群名单。"""

    def is_served(self, gid: Any) -> bool:
        return str(gid) == G1


# ======================================================================
# 1. 拿不到服务群证据 → 空名单、零 SQL、零密码读取
# ======================================================================


class TestFailClosedAudience:
    def test_getter_exception_is_empty_audience_with_zero_sql(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_password(store, G1, PW1)          # 库里存过密码
        spy = SpyStore(store)
        ga = GroupAdmins(spy, get_settings=_boom)

        assert ga.served_gids() == []
        assert ga.match(PW1) is None, "配置读不出来时，已保存的密码不许匹配成功"
        assert ga.has_password(G1) is False
        assert ga.fingerprint(G1) == ""
        assert spy.read_calls == 0, "拿不到配置时不许扫 secrets 表找「存过密码的群」"
        assert spy.secret_get_keys == [], "拿不到配置时不许读已保存的密码"

    def test_getter_returning_none_grants_no_group_admin_trust(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_password(store, G1, PW1)
        spy = SpyStore(store)
        ga = GroupAdmins(spy, get_settings=lambda: None)

        assert ga.served_gids() == []
        assert ga.match(PW1) is None
        assert ga.has_password(G1) is False
        assert ga.fingerprint(G1) == ""
        assert spy.read_calls == 0
        assert spy.secret_get_keys == []

    @pytest.mark.parametrize("bad", ["随便一个字符串", 12345, object()], ids=["str", "int", "obj"])
    def test_noncallable_getter_is_configured_invalid(self, tmp_path: Path, bad: Any) -> None:
        """传了不可调用的东西 = 配过了但配置不合法 → 严格拒，不许悄悄退回 legacy 宽松路径。"""
        store = _store(tmp_path)
        _seed_password(store, G1, PW1)
        spy = SpyStore(store)
        ga = GroupAdmins(spy, get_settings=bad)

        assert ga.served_gids() == []
        assert ga.match(PW1) is None
        assert ga.fingerprint(G1) == ""
        assert spy.read_calls == 0 and spy.secret_get_keys == []

    def test_real_settings_object_passed_as_getter_is_configured_invalid(self, tmp_path: Path) -> None:
        """误把 Settings 本体当 getter 传（常见的接线错误）也要按「配置不合法」拒。"""
        store = _store(tmp_path)
        _seed_password(store, G1, PW1)
        spy = SpyStore(store)
        ga = GroupAdmins(spy, get_settings=_settings(G1, G2))

        assert ga.served_gids() == []
        assert ga.fingerprint(G1) == ""
        assert spy.read_calls == 0 and spy.secret_get_keys == []

    def test_settings_without_groups_cannot_enumerate_and_denies(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """只有 is_served、没有 groups：判得出单个群，但列不出名单 → 安全拒绝（必要说明），
        绝不许扫库兜底。"""
        store = _store(tmp_path)
        _seed_password(store, G1, PW1)
        spy = SpyStore(store)
        ga = GroupAdmins(spy, get_settings=lambda: _ServedOnlySettings())

        with caplog.at_level(logging.DEBUG, logger="maiwork.group_admins"):
            assert ga.served_gids() == []
        assert any("groups" in r.getMessage() for r in caplog.records), "拒绝要留下说明"
        assert ga.fingerprint(G1) == ""
        assert spy.read_calls == 0 and spy.secret_get_keys == []

    def test_noncallable_getter_also_denies_group_approval(self, tmp_path: Path) -> None:
        """同一道「配置不合法」判据也要落到每群批准名单上（approvals 吃同一个 getter）。

        传不可调用的东西时，approvals 不许被当成「根本没配 getter」的 legacy 放行路径。
        """
        store = _store(tmp_path)
        ga = GroupAdmins(store, get_settings=object())

        assert ga.served_gids() == []
        assert ga.accounts(G1) == []
        assert ga.is_group_admin(G1, "30003") is False
        with pytest.raises(ValueError):
            ga.set_accounts(G1, ["30003"])
        assert store.kv_get("group_approval." + G1, None) is None, "拒绝时一个字段都不落库"

    def test_groups_mapping_is_the_audience_and_never_scans_store(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_password(store, G3, PW3)          # 库里存过、但配置的 groups 里没有
        spy = SpyStore(store)
        ga = GroupAdmins(spy, get_settings=lambda: _GroupsOnlySettings({G1: object(), G2: object()}))

        assert ga.served_gids() == sorted([G1, G2])
        assert spy.read_calls == 0 and spy.secret_get_keys == [], "列服务群只读配置，不扫库"
        assert ga.match(PW3) is None
        assert ga.has_password(G3) is False
        assert ga.fingerprint(G3) == ""
        # match 只比对**服务群**的密码；非服务群（G3）的密码一次都不许读
        assert all(G3 not in key for key in spy.secret_get_keys), spy.secret_get_keys

    def test_real_settings_audience_excludes_stored_but_unserved_gid(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_password(store, G3, PW3)
        spy = SpyStore(store)
        ga = GroupAdmins(spy, get_settings=lambda: _settings(G1, G2))

        assert ga.served_gids() == sorted([G1, G2])
        assert spy.read_calls == 0
        assert ga.fingerprint(G3) == ""

    def test_no_getter_legacy_still_falls_back_to_stored_gids(self, tmp_path: Path) -> None:
        """显式 legacy（根本没传 getter）保留老兼容：退回库里存过密码的群。"""
        store = _store(tmp_path)
        _seed_password(store, G1, PW1)
        _seed_password(store, G2, PW2)
        ga = GroupAdmins(store)                 # 没有 getter

        assert ga.served_gids() == sorted([G1, G2])
        assert ga.match(PW1) == G1
        assert ga.match(PW2) == G2
        assert ga.has_password(G2) is True
        assert ga.fingerprint(G1) != ""


# ======================================================================
# 2. 已保存的密码：非服务群不认、不签；服务群照旧
# ======================================================================


class TestFailClosedStoredPassword:
    def test_unserved_stored_password_never_matched_or_signed(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_password(store, G1, PW1)
        _seed_password(store, G2, PW2)          # 配置里只有 G1
        spy = SpyStore(store)
        ga = GroupAdmins(spy, get_settings=lambda: _settings(G1))

        assert ga.match(PW1) == G1, "服务群的密码照旧能匹配"
        assert ga.match(PW2) is None, "已被移出服务名单的群，旧密码不许再匹配"
        assert ga.fingerprint(G2) == "", "非服务群指纹为空 → 旧 cookie 一律失效"
        assert ga.has_password(G2) is False
        assert ga.fingerprint(G1)
        assert all(key == "group_admin_pw." + G1 for key in spy.secret_get_keys), spy.secret_get_keys

    def test_served_group_match_fingerprint_and_hash_format_unchanged(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        ga = GroupAdmins(store, get_settings=lambda: _settings(G1, G2))
        ga.set_password(G1, PW1)
        ga.set_password(G2, PW2)

        assert ga.served_gids() == sorted([G1, G2])
        assert ga.match(PW1) == G1 and ga.match(PW2) == G2
        assert ga.match("完全不对的密码-zzzz") is None
        assert ga.has_password(G1) is True
        fp = ga.fingerprint(G1)
        assert fp and len(fp) == 16
        # 哈希格式 / 最少 8 位 / 不回显：一个字没动
        raw = store.secret_get("group_admin_pw." + G1)
        assert raw.startswith("sha256$") and raw.count("$") == 2
        assert PW1 not in raw
        with pytest.raises(ValueError):
            ga.set_password(G1, "1234567")


# ======================================================================
# 3. 群管理员 cookie：有配置证据才有效；拿不到证据就不认、也不发
# ======================================================================


class TestGroupCookieFailClosed:
    def test_served_group_cookie_round_trip_unchanged(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        settings = _settings(G1)
        ga = GroupAdmins(store, get_settings=lambda: settings)
        ga.set_password(G1, PW1)
        auth = ConsoleAuth(store, lambda: settings)
        auth.bind_group_admins(ga)

        value, max_age = auth.make_group_cookie(G1)
        assert value.startswith(f"g:{G1}.") and max_age > 0
        assert auth.check_group_cookie(value) == G1

    @pytest.mark.parametrize(
        "broken", [_boom, (lambda: None)], ids=["getter-raises", "getter-returns-none"]
    )
    def test_cookie_dies_when_config_cannot_be_read(self, tmp_path: Path, broken: Any) -> None:
        store = _store(tmp_path)
        good = _settings(G1)
        ga = GroupAdmins(store, get_settings=lambda: good)
        ga.set_password(G1, PW1)
        auth = ConsoleAuth(store, lambda: good)
        auth.bind_group_admins(ga)
        cookie, _ = auth.make_group_cookie(G1)
        assert auth.check_group_cookie(cookie) == G1, "有配置证据时旧 cookie 照旧有效"

        broken_ga = GroupAdmins(store, get_settings=broken)
        broken_auth = ConsoleAuth(store, broken)
        broken_auth.bind_group_admins(broken_ga)
        assert broken_ga.fingerprint(G1) == ""
        assert broken_auth.check_group_cookie(cookie) is None, "拿不到配置证据不许再认这个 cookie"
        assert broken_auth.make_group_cookie(G1) == ("", 0), "拿不到配置证据不许发新 cookie"

    def test_cookie_dies_when_group_removed_from_config(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        both = _settings(G1, G2)
        ga = GroupAdmins(store, get_settings=lambda: both)
        ga.set_password(G2, PW2)
        auth = ConsoleAuth(store, lambda: both)
        auth.bind_group_admins(ga)
        cookie, _ = auth.make_group_cookie(G2)
        assert auth.check_group_cookie(cookie) == G2

        only_g1 = _settings(G1)
        ga2 = GroupAdmins(store, get_settings=lambda: only_g1)
        auth2 = ConsoleAuth(store, lambda: only_g1)
        auth2.bind_group_admins(ga2)
        assert ga2.fingerprint(G2) == ""
        assert auth2.check_group_cookie(cookie) is None
        assert auth2.make_group_cookie(G2) == ("", 0)


# ======================================================================
# 4. 真 app 接线：生产 getter（app.get_settings）拿不到配置时的行为
# ======================================================================


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _raw_config(data_dir: Path) -> dict:
    return {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}, {"group": f"qq:{G2}"}]},
        "console": {"listen": f"127.0.0.1:{_free_port()}", "password": ADMIN_PW, "public_url": ""},
        "models": {"base_url": "https://ep.test/v1", "api_key": "sk-x", "main": "m", "worker": "w"},
        "storage": {"data_dir": str(data_dir)},
        "approval": {"required": True, "admins": ["10001"]},
    }


@pytest_asyncio.fixture
async def app_env(tmp_path: Path):
    import tomlkit

    raw = _raw_config(tmp_path / "data")
    plug_dir = tmp_path / "plug"
    plug_dir.mkdir()
    doc = tomlkit.document()
    for section, values in raw.items():
        if isinstance(values, dict):
            table = tomlkit.table()
            for k, v in values.items():
                table[k] = v
            doc[section] = table
    (plug_dir / "config.toml").write_text(tomlkit.dumps(doc), encoding="utf-8")
    app = MaiWorkApp(FakeCtx({"config.get": "987654321"}), raw, plugin_dir=plug_dir)
    app.profiles_cls = FakeProfiles
    await app.start()
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    try:
        yield app, client
    finally:
        await client.close()
        await app.stop()


class TestRealAppWiring:
    @pytest.mark.asyncio
    async def test_ga_cookie_ignored_while_settings_unavailable(self, app_env) -> None:
        app, client = app_env
        app.group_admins.set_password(G1, PW1)
        assert (await client.post("/api/login", json={"password": PW1})).status == 200
        me = await (await client.get("/api/me")).json()
        assert me["role"] == "group_admin" and me["group"] == G1

        keep = app._settings
        app._settings = None                    # 真 runtime 的 getter 现在拿不到配置了
        try:
            assert app.group_admins.served_gids() == []
            assert app.group_admins.match(PW1) is None
            assert app.group_admins.fingerprint(G1) == ""
            r = await client.get("/api/me")
            assert r.status == 200, r.status
            assert (await r.json())["role"] == "none", "配置读不出来时群管理员 cookie 必须失效"
        finally:
            app._settings = keep
        # 配置回来了：服务群名单 / 密码 / 旧 cookie 照旧（指纹算法没动）
        assert app.group_admins.served_gids() == sorted([G1, G2])
        assert app.group_admins.match(PW1) == G1
        assert (await (await client.get("/api/me")).json())["role"] == "group_admin"
