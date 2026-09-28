"""管理员 / 免批名单按 MaiBot 的标准写法「平台:账号」（用户要求 2026-09-27）。

旧写法纯数字当作 qq 平台，照样生效；比对时按「平台 + 账号」。
"""
from __future__ import annotations

import pytest

from CharTyr_MaiWork.maiwork.config import load_settings, norm_account
from CharTyr_MaiWork.maiwork import rules


def test_norm_account() -> None:
    assert norm_account("qq:100000001") == "qq:100000001"
    assert norm_account(" QQ:100000001 ") == "qq:100000001"
    assert norm_account("100000001") == "qq:100000001"  # 旧写法
    assert norm_account("telegram:alice_01") == "telegram:alice_01"
    for bad in ("", "qq:", ":123", "qq:12 3", "abc", "q q:1"):
        assert norm_account(bad) == "", bad


def test_default_admin_is_platform_form() -> None:
    s, _ = load_settings({})
    assert s.approval.admins == ("qq:100000001",)


def test_config_lists_normalized_and_bad_reported() -> None:
    s, problems = load_settings({"approval": {"admins": ["10001", "qq:10002", "telegram:bob", "坏的"],
                                              "exempt_users": ["20001"], "exempt_groups": ["900000001", "qq:111"]}})
    assert s.approval.admins == ("qq:10001", "qq:10002", "telegram:bob")
    assert s.approval.exempt_users == ("qq:20001",)
    assert s.approval.exempt_groups == ("qq:900000001", "qq:111")
    assert any("坏的" in p for p in problems)


def test_rules_accept_platform_form() -> None:
    check = rules._FIELDS["approval"]["admins"]
    assert check(["qq:1", "2", "telegram:x"]) == ["qq:1", "qq:2", "telegram:x"]
    with pytest.raises(ValueError):
        check(["不是账号"])


def test_is_admin_by_platform(tmp_path) -> None:
    from CharTyr_MaiWork.maiwork.approvals import Approvals
    from CharTyr_MaiWork.maiwork.store import Store
    s, _ = load_settings({"approval": {"admins": ["qq:10001", "telegram:bob"]}})
    st = Store(tmp_path / "t.db"); st.migrate()
    a = Approvals(st, lambda: s, None, None)
    assert a.is_admin("10001") is True            # 不给平台 = qq
    assert a.is_admin("10001", platform="qq") is True
    assert a.is_admin("10001", platform="telegram") is False
    assert a.is_admin("bob", platform="telegram") is True
    assert a.is_admin("bob") is False
    st.close()


def test_exempt_group_and_user_by_platform(tmp_path) -> None:
    from CharTyr_MaiWork.maiwork.approvals import Approvals
    from CharTyr_MaiWork.maiwork.store import Store
    s, _ = load_settings({"approval": {"exempt_groups": ["qq:900000001"], "exempt_users": ["qq:20001"]}})
    st = Store(tmp_path / "t.db"); st.migrate()
    a = Approvals(st, lambda: s, None, None)
    assert a._is_auto("900000001", "1") is True
    assert a._is_auto("111", "20001") is True
    assert a._is_auto("111", "3") is False
    st.close()
