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
    # rules 的旧 _FIELDS 层 2026-10 删了；现在字段走 CONFIG_BY_KEY + _validate_generic。
    from CharTyr_MaiWork.maiwork.rules import CONFIG_BY_KEY, _validate_generic

    assert _validate_generic(CONFIG_BY_KEY["approval.admins"], ["qq:1", "2", "telegram:x"]) == ["qq:1", "qq:2", "telegram:x"]
    with pytest.raises(ValueError):
        _validate_generic(CONFIG_BY_KEY["approval.admins"], ["不是账号"])


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
    """免批按**每群一份**（group_approval，0.8.0）：得先有真实的 served 证据。

    全局那几行只是新群第一次的迁移种子；settings 里没有任何服务群时按 fail-closed
    处理（不放行）——所以这里用带 served 列表的 raw 配置，第一次读某个群时惰性种一份。
    """
    from CharTyr_MaiWork.maiwork.approvals import Approvals
    from CharTyr_MaiWork.maiwork.store import Store

    g1 = "900000001"
    g2 = "111222333"
    raw = {
        "groups": {"serve": [{"group": f"qq:{g1}"}, {"group": f"qq:{g2}"}]},
        "approval": {
            "required": True,
            "exempt_groups": [f"qq:{g1}"],
            "exempt_users": ["qq:20001", "telegram:77777"],
        },
    }
    s, problems = load_settings(raw)
    assert not problems, problems
    st = Store(tmp_path / "t.db"); st.migrate()
    a = Approvals(st, lambda: s, None, None)
    # 整群免批：全局 exempt_groups 含本群 → 种进这一群（这个群谁都不用批）
    assert a._is_auto(g1, "1") is True
    # 另一群没整群免批，只认免批的人：平台对得上（qq）才算
    assert a._is_auto(g2, "20001") is True
    # 平台隔离：telegram 的免批人不在本群平台，照样要批；陌生人也一样
    assert a._is_auto(g2, "77777") is False
    assert a._is_auto(g2, "3") is False
    # 非服务群 / 没有服务群证据：fail-closed（不放行）
    assert a._is_auto("111", "20001") is False
    st.close()
