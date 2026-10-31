"""Docker 检查：容器探测、数据目录是否挂到容器外、引导/运行状态里的提示。"""

from __future__ import annotations

import posixpath
from types import SimpleNamespace
from typing import Any

import pytest

from CharTyr_MaiWork.maiwork import onboarding, rules
from CharTyr_MaiWork.maiwork.console import views
from CharTyr_MaiWork.maiwork.environments import container as ct


# ---------------------------------------------------------------- in_docker


def _fs(files: dict[str, str]):
    return (lambda p: p in files), (lambda p: files.get(p))


@pytest.mark.parametrize("files,expect", [
    ({"/.dockerenv": ""}, True),
    ({"/run/.containerenv": ""}, True),
    ({"/proc/1/cgroup": "0::/system.slice/docker-abc.scope\n"}, True),
    ({"/proc/1/cgroup": "12:pids:/kubepods/besteffort/pod1\n"}, True),
    ({"/proc/1/cgroup": "0::/system.slice/containerd.service\n"}, True),
    ({"/proc/1/cgroup": "0::/init.scope\n"}, False),
    ({}, False),
])
def test_in_docker(files, expect) -> None:
    exists, read = _fs(files)
    assert ct.in_docker(exists=exists, read=read) is expect


# ---------------------------------------------------------------- mountinfo

_OVERLAY_ROOT = (
    "600 500 0:50 / / rw,relatime master:1 - overlay overlay rw,lowerdir=/a,upperdir=/b\n"
    "601 600 0:51 / /proc rw - proc proc rw\n"
)
_BIND = _OVERLAY_ROOT + "650 600 8:1 /srv/mai/data /MaiMBot/data rw,relatime - ext4 /dev/sda1 rw\n"


def _reader(text):
    return lambda p: text if p == "/proc/self/mountinfo" else None


@pytest.fixture
def _posix_mountinfo_paths(monkeypatch: pytest.MonkeyPatch):
    """mountinfo 只存在于 Linux：注入的就是 Linux 挂载表，data_dir 的绝对化也按 POSIX 语义走。

    不这么做的话 Windows 上 os.path.abspath("/MaiMBot/data/...") 会变 C:\\MaiMBot\\...，
    挂载点匹配必然落空（测的是 Linux 语义，不是 Windows 没有 mountinfo 这条路径）。
    """
    monkeypatch.setattr(ct, "os", SimpleNamespace(path=posixpath))


def test_persisted_false_on_overlay_root(_posix_mountinfo_paths) -> None:
    assert ct.data_dir_persisted("/MaiMBot/data/maiwork", read=_reader(_OVERLAY_ROOT)) is False


def test_persisted_true_on_bind_mount_parent(_posix_mountinfo_paths) -> None:
    assert ct.data_dir_persisted("/MaiMBot/data/maiwork", read=_reader(_BIND)) is True


def test_persisted_false_outside_bind_mount(_posix_mountinfo_paths) -> None:
    # /MaiMBot/database 不在 /MaiMBot/data 下，仍是容器层
    assert ct.data_dir_persisted("/MaiMBot/database", read=_reader(_BIND)) is False


def test_persisted_true_on_non_overlay_root(_posix_mountinfo_paths) -> None:
    text = "1 0 8:1 / / rw - ext4 /dev/sda1 rw\n"
    assert ct.data_dir_persisted("/x/y", read=_reader(text)) is True


def test_persisted_none_when_unreadable() -> None:
    assert ct.data_dir_persisted("/x", read=lambda p: None) is None
    assert ct.data_dir_persisted("/x", read=_reader("garbage line\n")) is None


# ---------------------------------------------------------------- 引导 / 运行状态


class _Store:
    def kv_get(self, key, default=None):
        return default


def _svc(*, docker: bool, persisted=None, listen=("127.0.0.1", 18650), missing=(), railway=True) -> Any:
    settings = SimpleNamespace(
        environments=SimpleNamespace(railway=railway, railway_daily_max=2),
        console=SimpleNamespace(public_url="", listen=listen),
        groups={"1": SimpleNamespace()},
        data_dir="/MaiMBot/data/maiwork",
    )
    return SimpleNamespace(
        get_settings=lambda: settings,
        store=_Store(),
        ssh_missing_tools=lambda: list(missing),
        group_space=None,
        in_docker=lambda: docker,
        data_dir_persisted=lambda d: persisted,
    )


def _by_key(svc) -> dict[str, dict]:
    return {i["key"]: i for i in onboarding.items(svc, checks={})}


def test_items_unchanged_outside_docker() -> None:
    keys = list(_by_key(_svc(docker=False, persisted=False)))
    assert not [k for k in keys if k.startswith("docker")]


def test_docker_items_after_exec_and_texts(monkeypatch) -> None:
    keys = [i["key"] for i in onboarding.items(_svc(docker=True, persisted=False), checks={})]
    assert keys.index("docker_data") == keys.index("exec") + 1
    assert keys.index("docker_listen") == keys.index("exec") + 2
    it = _by_key(_svc(docker=True, persisted=False))
    d = it["docker_data"]
    assert d["state"] == "warn" and "/MaiMBot/data/maiwork" in d["text"] and "-v" in d["text"]
    l_ = it["docker_listen"]
    assert l_["state"] == "warn" and "0.0.0.0:18650" in l_["text"] and "127.0.0.1:18650:18650" in l_["text"]
    assert l_["sub"] == "rules" and l_["sec"] == "console"


def test_docker_data_ok_and_none() -> None:
    assert _by_key(_svc(docker=True, persisted=True))["docker_data"]["state"] == "ok"
    assert "docker_data" not in _by_key(_svc(docker=True, persisted=None))


def test_docker_listen_open_warns_public() -> None:
    l_ = _by_key(_svc(docker=True, persisted=True, listen=("0.0.0.0", 18650)))["docker_listen"]
    assert l_["state"] == "ok" and "公网" in l_["text"]


def test_docker_item_targets_valid() -> None:
    secs = {s["id"] for s in rules.CONFIG_SECTIONS}
    import re
    from pathlib import Path

    js = (Path(onboarding.__file__).parent / "console/static/js/settings/index.js").read_text(encoding="utf-8")
    subs = set(re.findall(r'\["([a-z]+)"', js.split("export const SET_SUBS = [", 1)[1].split("];", 1)[0]))
    for svc in (_svc(docker=True, persisted=False), _svc(docker=True, persisted=True, listen=("0.0.0.0", 1))):
        for i in onboarding.items(svc, checks={}):
            if i.get("sub"):
                assert i["sub"] in subs, i
            if i.get("sec"):
                assert i["sub"] == "rules" and i["sec"] in secs, i


def test_missing_ssh_in_docker_mentions_freestyle(monkeypatch) -> None:
    monkeypatch.setattr(views, "_localenv_health", lambda svc: {"state": "off", "text": "没开"})
    svc = _svc(docker=True, missing=["ssh"])
    assert "Freestyle" in views._railway_health(svc)["text"]
    assert "Freestyle" in onboarding._exec_item(svc)["text"]
    plain = _svc(docker=False, missing=["ssh"])
    assert "Freestyle" not in views._railway_health(plain)["text"]
    assert "Freestyle" not in onboarding._exec_item(plain)["text"]


def test_container_health() -> None:
    assert views._container_health(_svc(docker=False)) is None
    assert views._container_health(_svc(docker=True, persisted=False))["state"] == "warn"
    assert views._container_health(_svc(docker=True, persisted=True))["state"] == "ok"
    h = views._container_health(_svc(docker=True, persisted=None))
    assert h["state"] == "off" and h["key"] == "container"


# ---------------------------------------------------------------- 资讯卡片画图（字体 + 浏览器）


def _deps(fonts_state: str, browser_state: str):
    from types import SimpleNamespace as NS

    def g(gid, st):
        return {"id": gid, "kind": "asset", "label": gid, "state": st, "summary": f"{gid}-{st}",
                "hint": "缺 libnss3.so" if st == "broken" else ""}
    office = {"id": "office", "state": "ok", "packages": [], "tools": [{}]}
    return NS(status=lambda: {"checked_at": 1.0, "groups": [office, g("fonts", fonts_state), g("browser", browser_state)]})


def test_card_render_item_states() -> None:
    from CharTyr_MaiWork.maiwork import onboarding
    from types import SimpleNamespace as NS

    def item(f, b):
        return onboarding._card_render_item(NS(optional_deps=_deps(f, b)))

    assert item("ok", "system")["state"] == "ok"
    it = item("missing", "ok")
    assert it["state"] == "warn" and it["sub"] == "extensions" and "fonts-missing" in it["text"]
    it = item("ok", "broken")
    assert it["state"] == "warn" and "libnss3" in it["text"]
    assert item("unknown", "ok")["state"] == "wait"
    # 老版本没有这两组 → 不出这一项
    assert onboarding._card_render_item(NS(optional_deps=NS(status=lambda: {"groups": [{"id": "office"}]}))) is None


def test_card_render_item_in_items() -> None:
    from CharTyr_MaiWork.maiwork import onboarding

    svc = _svc(docker=False)
    svc.optional_deps = _deps("missing", "ok")
    keys = [i["key"] for i in onboarding.items(svc, checks={})]
    assert "card_render" in keys
