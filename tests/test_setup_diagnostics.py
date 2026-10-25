"""开箱自检：缺 SSH 客户端按系统给修法；引导清单里的执行 / 群文件 / 交付三项。

目标是让 Windows / macOS / Docker 用户一眼看出「哪里没通、怎么修」。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

from CharTyr_MaiWork.maiwork import onboarding
from CharTyr_MaiWork.maiwork.console import views
from CharTyr_MaiWork.maiwork.environments import openssh_client as oc


# ---------------------------------------------------------------- missing_tools


def test_missing_tools_uses_minimal_path_and_lists_missing() -> None:
    seen: list[Any] = []

    def which(name, path=None):
        seen.append(path)
        return None if name in ("ssh", "scp") else f"/usr/bin/{name}"

    env = {"PATH": "/x/bin", "LANG": "C.UTF-8"}
    assert oc.missing_tools(which=which, env=env) == ["ssh", "scp"]
    assert seen and all(p == "/x/bin" for p in seen)


def test_missing_tools_none_missing() -> None:
    assert oc.missing_tools(which=lambda n, path=None: "/bin/" + n, env={"PATH": "/bin"}) == []


# ---------------------------------------------------------------- install_hint


def test_hint_windows_mentions_optional_feature_powershell_and_git() -> None:
    h = oc.install_hint(["ssh"], platform="nt", in_docker=False)
    assert "可选功能" in h and "OpenSSH 客户端" in h
    assert "Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0" in h
    assert "Git" in h and "System32" in h
    assert "重启 MaiBot" in h


def test_hint_docker() -> None:
    h = oc.install_hint(["ssh-keygen"], platform="posix", in_docker=True)
    assert "容器" in h and "apt-get install -y openssh-client" in h


def test_hint_macos_and_linux() -> None:
    mac = oc.install_hint(["ssh"], platform="darwin", in_docker=False)
    assert "macOS" in mac and "PATH" in mac
    linux = oc.install_hint(["ssh"], platform="linux", in_docker=False)
    assert "openssh-client" in linux and "容器" not in linux


def test_hint_windows_env_broken() -> None:
    h = oc.install_hint(["ssh"], platform="nt", in_docker=False, env={"PATH": ""})
    assert "SystemRoot" in h


def test_hint_empty_when_nothing_missing() -> None:
    assert oc.install_hint([], platform="nt") == ""


# ---------------------------------------------------------------- 健康项 / 引导清单的假 svc


class _Store:
    def __init__(self, kv: dict | None = None) -> None:
        self.kv = dict(kv or {})

    def kv_get(self, key, default=None):
        return self.kv.get(key, default)


def _svc(*, railway=True, missing=(), kv=None, public_url="", herenow=None, group_space=None,
         groups=("1",)) -> Any:
    console = SimpleNamespace(public_url=public_url)
    if herenow is not None:
        console.herenow_share = herenow
    settings = SimpleNamespace(
        environments=SimpleNamespace(railway=railway, railway_daily_max=2),
        console=console,
        groups={g: SimpleNamespace() for g in groups},
    )
    return SimpleNamespace(
        get_settings=lambda: settings,
        store=_Store(kv),
        ssh_missing_tools=lambda: list(missing),
        group_space=group_space,
        in_docker=lambda: False,  # 不去真探测跑测试的机器（在容器里跑测试也要稳定）
    )


def test_railway_health_warns_when_ssh_missing() -> None:
    h = views._railway_health(_svc(missing=["ssh", "scp"]))
    assert h["state"] == "warn"
    assert "缺少 SSH 客户端" in h["text"] and "ssh" in h["text"] and "scp" in h["text"]
    assert "ssh-keygen" not in h["text"]
    assert h["text"].endswith(oc.install_hint(["ssh", "scp"], env=oc.minimal_env()))


def test_railway_health_ok_when_ssh_present() -> None:
    h = views._railway_health(_svc(missing=[]))
    assert h["state"] == "ok"


def test_railway_health_off_skips_ssh_check() -> None:
    h = views._railway_health(_svc(railway=False, missing=["ssh"]))
    assert h["state"] == "off"


def test_railway_health_default_probe_is_cached(monkeypatch) -> None:
    calls = []

    def fake():
        calls.append(1)
        return ["ssh"]

    monkeypatch.setattr(oc, "missing_tools", lambda *a, **k: fake())
    monkeypatch.setattr(views, "_SSH_MISSING_CACHE", None, raising=False)
    svc = _svc()
    del svc.ssh_missing_tools
    assert views._railway_health(svc)["state"] == "warn"
    assert views._railway_health(svc)["state"] == "warn"
    assert len(calls) == 1


# ---------------------------------------------------------------- exec


def test_exec_item_names_missing_ssh(monkeypatch) -> None:
    monkeypatch.setattr(views, "_localenv_health", lambda svc: {"state": "off", "text": "没开"})
    it = onboarding._exec_item(_svc(missing=["ssh-keygen"]))
    assert it["state"] == "warn" and it["sub"] == "rules"
    assert "SSH" in it["text"] and "ssh-keygen" in it["text"]


# ---------------------------------------------------------------- groupfile


def test_groupfile_ok_when_adapter_open() -> None:
    it = onboarding._groupfile_item(_svc(group_space=SimpleNamespace(adapter_open=lambda: True)))
    assert it["key"] == "groupfile" and it["title"] == "群文件"
    assert it["state"] == "ok" and "传文件" in it["text"]


def test_groupfile_warn_when_adapter_closed() -> None:
    it = onboarding._groupfile_item(_svc(group_space=SimpleNamespace(adapter_open=lambda: False)))
    assert it["state"] == "warn"
    assert "网页链接" in it["text"] and "SnowLuma" in it["text"]
    assert it["sub"] == "overview"


def test_groupfile_recent_failure_shown() -> None:
    now = time.time()
    kv = {"outbox.file_last_fail": {"ts": now - 60, "reason": "群文件空间满了", "kind": "full"},
          "outbox.file_last_ok": {"ts": now - 3600}}
    it = onboarding._groupfile_item(_svc(kv=kv, group_space=SimpleNamespace(adapter_open=lambda: True)))
    assert it["state"] == "warn"
    assert "最近一次群文件没发出去：群文件空间满了" in it["text"]


def test_groupfile_old_or_superseded_failure_ignored() -> None:
    now = time.time()
    gs = SimpleNamespace(adapter_open=lambda: True)
    old = {"outbox.file_last_fail": {"ts": now - 90000, "reason": "x", "kind": "k"}}
    assert onboarding._groupfile_item(_svc(kv=old, group_space=gs))["state"] == "ok"
    later_ok = {"outbox.file_last_fail": {"ts": now - 600, "reason": "x", "kind": "k"},
                "outbox.file_last_ok": {"ts": now - 60}}
    assert onboarding._groupfile_item(_svc(kv=later_ok, group_space=gs))["state"] == "ok"


def test_items_put_groupfile_next_to_delivery(monkeypatch) -> None:
    keys = [i["key"] for i in onboarding.items(_svc(), checks={})]
    assert "groupfile" in keys
    assert abs(keys.index("groupfile") - keys.index("delivery")) == 1


# ---------------------------------------------------------------- delivery


def test_delivery_public_url_ok() -> None:
    it = onboarding._delivery_item(_svc(public_url="https://mw.example.com/"))
    assert it["state"] == "ok" and "https://mw.example.com" in it["text"]


@pytest.mark.parametrize("herenow", [None, True])
def test_delivery_herenow_default_ok(herenow) -> None:
    it = onboarding._delivery_item(_svc(herenow=herenow))
    assert it["state"] == "ok"
    assert "here.now" in it["text"] and "24 小时" in it["text"] and "公开网址" in it["text"]


def test_delivery_nothing_warns() -> None:
    it = onboarding._delivery_item(_svc(herenow=False))
    assert it["state"] == "warn" and it["sub"] == "rules"
    assert "群友拿不到链接" in it["text"] and "here.now" in it["text"]


# ---------------------------------------------------------------- 「前往设置」跳转目标


def _set_subs() -> set[str]:
    """网页设置真有的子页（settings/index.js 的 SET_SUBS）。"""
    import re
    from pathlib import Path

    js = (Path(onboarding.__file__).parent / "console/static/js/settings/index.js").read_text(encoding="utf-8")
    block = js.split("export const SET_SUBS = [", 1)[1].split("];", 1)[0]
    return set(re.findall(r'\["([a-z]+)"', block))


def test_item_sub_targets_exist_in_web_settings(monkeypatch) -> None:
    """清单项的 sub 必须是网页里真有的设置子页，否则「前往设置」会跳错地方。"""
    subs = _set_subs()
    assert "rules" in subs and "overview" in subs
    monkeypatch.setattr(views, "_localenv_health", lambda svc: {"state": "off", "text": "没开"})
    samples = [
        onboarding._exec_item(_svc(missing=["ssh"])),
        onboarding._exec_item(_svc()),
        onboarding._groupfile_item(_svc(group_space=SimpleNamespace(adapter_open=lambda: False))),
        onboarding._delivery_item(_svc(herenow=False)),
        *onboarding.items(_svc(), checks={}),
    ]
    for it in samples:
        if it.get("sub"):
            assert it["sub"] in subs, it


def test_item_sec_targets_exist_in_all_settings(monkeypatch) -> None:
    """跳到「全部设置」的项要带 sec（直接打开那一栏），且必须是真有的栏目。"""
    from CharTyr_MaiWork.maiwork import rules

    secs = {s["id"] for s in rules.CONFIG_SECTIONS}
    monkeypatch.setattr(views, "_localenv_health", lambda svc: {"state": "off", "text": "没开"})
    assert onboarding._exec_item(_svc(missing=["ssh"]))["sec"] == "environments"
    assert onboarding._delivery_item(_svc(herenow=False))["sec"] == "console"
    for it in onboarding.items(_svc(), checks={}):
        if it.get("sec"):
            assert it["sub"] == "rules" and it["sec"] in secs, it
