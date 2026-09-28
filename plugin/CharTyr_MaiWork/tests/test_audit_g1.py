"""G1 回归测试：线上以 root 运行时禁止 direct 模式。

direct 模式不隔离：子 agent 直接以插件进程身份跑（线上就是 root），等于把
root shell 交给模型。load_settings 里 local_mode="direct" 且 root →
强制回落 systemd 并记中文问题。

注意：conftest 的隔离罩把所有测试默认 patch 成「非 root」（_running_as_root=False），
本文件专测这条防线——每个用例自己把它再 patch 成需要的值。
"""

from __future__ import annotations

import pytest

from CharTyr_MaiWork import config
from CharTyr_MaiWork.config import load_settings


def _raw(local_mode: str) -> dict:
    return {"environments": {"local_mode": local_mode, "workspace_root": "/tmp/x"}}


class TestDirectModeForbiddenAsRoot:
    def test_direct_as_root_falls_back_to_systemd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """root + direct → systemd + 问题清单带原因。"""
        monkeypatch.setattr(config, "_running_as_root", lambda: True)
        s, problems = load_settings(_raw("direct"))
        assert s.environments.local_mode == "systemd"
        assert any("root" in p and "direct" in p for p in problems)

    def test_direct_as_non_root_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """普通用户本地测试：direct 照旧能用，不记问题。"""
        monkeypatch.setattr(config, "_running_as_root", lambda: False)
        s, problems = load_settings(_raw("direct"))
        assert s.environments.local_mode == "direct"
        assert not any("root" in p and "direct" in p for p in problems)

    def test_systemd_unaffected_by_euid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "_running_as_root", lambda: True)
        s, problems = load_settings(_raw("systemd"))
        assert s.environments.local_mode == "systemd"
        assert not any("root" in p and "direct" in p for p in problems)
