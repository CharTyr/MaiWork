"""网页密码哈希：新存 scrypt，老 sha256 照认，登录成功自动换成 scrypt。"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from test_group_admin import G1, G1_PW, _ga, _make_settings, _make_store

from CharTyr_MaiWork.maiwork import passwords
from CharTyr_MaiWork.maiwork.console.auth import ConsoleAuth


def _legacy(password: str, salt: str = "a1b2c3d4e5f60718") -> str:
    return f"sha256${salt}${hashlib.sha256((salt + password).encode('utf-8')).hexdigest()}"


def test_hash_roundtrip_and_legacy() -> None:
    h = passwords.hash_password("密码-abc123")
    assert h.startswith("scrypt$") and "密码" not in h
    assert passwords.verify("密码-abc123", h)
    assert not passwords.verify("密码-abc124", h)
    assert not passwords.needs_upgrade(h)
    old = _legacy("密码-abc123")
    assert passwords.verify("密码-abc123", old)
    assert not passwords.verify("wrong", old)
    assert passwords.needs_upgrade(old)


@pytest.mark.parametrize("bad", ["", "plain", "sha256$", "sha256$$", "scrypt$x$8$1$aa$bb", "scrypt$16384$8$1$zz$bb", "md5$a$b"])
def test_bad_stored_values_never_match(bad: str) -> None:
    assert passwords.verify("anything", bad) is False


def test_salt_differs_each_time() -> None:
    assert passwords.hash_password("same") != passwords.hash_password("same")


def _admin_auth(tmp_path: Path):
    store = _make_store(tmp_path)
    settings = _make_settings()
    object.__setattr__(settings.console, "password", "")
    return store, ConsoleAuth(store, lambda: settings)


def test_admin_generated_password_is_scrypt(tmp_path: Path) -> None:
    store, auth = _admin_auth(tmp_path)
    auth.ensure_password(tmp_path / "data")
    stored = store.secret_get("admin_password_hash")
    assert stored.startswith("scrypt$")
    pw = (tmp_path / "data" / "console_password.txt").read_text(encoding="utf-8").strip()
    assert auth.verify_password(pw)


@pytest.mark.asyncio
async def test_admin_legacy_hash_upgrades_on_login(tmp_path: Path) -> None:
    store, auth = _admin_auth(tmp_path)
    with store.tx() as conn:
        store.secret_set(conn, "admin_password_hash", _legacy("old-generated-pw"))
    assert not await auth.verify_password_async("wrong-pw")
    assert store.secret_get("admin_password_hash").startswith("sha256$")
    assert await auth.verify_password_async("old-generated-pw")
    upgraded = store.secret_get("admin_password_hash")
    assert upgraded.startswith("scrypt$")
    assert await auth.verify_password_async("old-generated-pw")
    assert auth.verify_password("old-generated-pw")


@pytest.mark.asyncio
async def test_group_admin_legacy_hash_upgrades_on_match(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ga = _ga(store, _make_settings())
    with store.tx() as conn:
        store.secret_set(conn, f"group_admin_pw.{G1}", _legacy(G1_PW))
    old_fp = ga.fingerprint(G1)
    assert await ga.match_async("wrong-password") is None
    assert await ga.match_async(G1_PW) == G1
    assert store.secret_get(f"group_admin_pw.{G1}").startswith("scrypt$")
    assert ga.fingerprint(G1) != old_fp  # 换了哈希，旧 cookie 失效一次
    assert ga.match(G1_PW) == G1


# ----------------------------------------------------------------------
# 反向代理后的访客 IP（console.trusted_proxies）
# ----------------------------------------------------------------------

from CharTyr_MaiWork.maiwork.config import load_settings  # noqa: E402
from CharTyr_MaiWork.maiwork.console.auth import client_ip  # noqa: E402


def test_client_ip_ignores_header_without_trusted_proxy() -> None:
    assert client_ip("203.0.113.9", "1.2.3.4", ()) == "203.0.113.9"
    # 来源不是信任代理：头是伪造的也不信
    assert client_ip("203.0.113.9", "1.2.3.4", ("127.0.0.1",)) == "203.0.113.9"


def test_client_ip_behind_trusted_proxy() -> None:
    trusted = ("127.0.0.1/32", "10.0.0.0/8")
    assert client_ip("127.0.0.1", "198.51.100.7", trusted) == "198.51.100.7"
    # 访客自己塞了一个假地址在最左边：取右边第一个不是代理的
    assert client_ip("127.0.0.1", "6.6.6.6, 198.51.100.7, 10.1.2.3", trusted) == "198.51.100.7"
    # 没头 / 全是代理 / 坏值 → 回落连接地址
    assert client_ip("127.0.0.1", "", trusted) == "127.0.0.1"
    assert client_ip("127.0.0.1", "10.0.0.5", trusted) == "127.0.0.1"
    assert client_ip("127.0.0.1", "not-an-ip", trusted) == "127.0.0.1"
    assert client_ip("::1", "2001:db8::1", ("::1",)) == "2001:db8::1"


def test_trusted_proxies_config_parsing() -> None:
    s, problems = load_settings({"plugin": {"enabled": True}, "console": {"trusted_proxies": ["127.0.0.1", "10.0.0.0/8", "bad", "127.0.0.1"]}})
    assert s.console.trusted_proxies == ("127.0.0.1/32", "10.0.0.0/8")
    assert any("bad" in p for p in problems)
    s2, problems2 = load_settings({"plugin": {"enabled": True}})
    assert s2.console.trusted_proxies == () and not any("trusted_proxies" in p for p in problems2)
