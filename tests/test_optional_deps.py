"""可选依赖（`maiwork/optional_deps.py`）单元测试。

用户 2026-10-15 定：办公文档三库（python-docx / openpyxl / python-pptx）以「可选安装」
方式提供，**不加进 `_manifest.json` 的必装依赖**。所以 MaiWork 要能自己把这三库装到
「子 agent 跑命令时真的 import 得到」的地方，同时**不碰 MaiBot 宿主环境**（线上红线）。

这份测试全部用假执行环境（`_FakeEnv`）和假 pip runner（`_runner`）：
- **绝不真的联网 pip install**；
- 绝不真起 systemd / 真装包。

覆盖：白名单拒绝任意包名、安装状态机（installing → ok / failed）、同一时间只装一次、
探测结果（每个包的 installed / version、soffice 只检测不装）、超时与错误摘要、
pip 进程的最小环境（不继承插件进程的密钥）、卸载只删本组文件。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import types
from pathlib import Path
from typing import Any

import pytest

from CharTyr_MaiWork.maiwork import optional_deps as od
from CharTyr_MaiWork.maiwork.store import Store

MARK = "MAIWORK_DEPS_JSON="


# ----------------------------------------------------------------------
# 假对象
# ----------------------------------------------------------------------


class _Settings:
    """最小 settings：只要 environments.workspace_root。"""

    def __init__(self, workspace_root: Path) -> None:
        self.environments = types.SimpleNamespace(workspace_root=str(workspace_root))
        self.data_dir = Path(workspace_root).parent / "data"


class _FakeEnv:
    """假本机执行环境：只实现 optional_deps 用到的 `run()`。

    真实现（environments/local.py）会把 PYTHONPATH 注进最小环境里，
    这里不模拟——那份接线由 test_optional_deps_env.py 单独测。
    """

    def __init__(self, stdout: str = "", *, exit_code: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.exit_code = exit_code
        self.stderr = stderr
        self.calls: list[dict[str, Any]] = []

    async def run(self, name: str, command: str, *, timeout_s: int | None = None, memory: str | None = None, cwd: str = ""):
        self.calls.append({"name": name, "command": command, "timeout_s": timeout_s})
        return types.SimpleNamespace(
            exit_code=self.exit_code, stdout=self.stdout, stderr=self.stderr,
            ms=1, timed_out=False, oom=False,
        )


def _probe_stdout(
    packages: dict[str, dict[str, Any]] | None = None,
    *,
    python: str = "/usr/bin/python3",
    version: str = "3.12.3",
    soffice: str = "",
) -> str:
    """造一份探测输出（和真探测同一格式）。"""
    data = {"python": python, "version": version, "packages": dict(packages or {})}
    lines = [MARK + json.dumps(data)]
    lines.append(soffice or "MAIWORK_NO_SOFFICE")
    return "\n".join(lines) + "\n"


def _installed() -> dict[str, dict[str, Any]]:
    return {
        "python-docx": {"installed": True, "version": "1.1.2"},
        "openpyxl": {"installed": True, "version": "3.1.5"},
        "python-pptx": {"installed": True, "version": "0.6.23"},
    }


def _missing(*, error: str = "ModuleNotFoundError") -> dict[str, dict[str, Any]]:
    return {
        "python-docx": {"installed": False, "error": error},
        "openpyxl": {"installed": False, "error": error},
        "python-pptx": {"installed": False, "error": error},
    }


def _runner(code: int = 0, out: str = "", err: str = ""):
    """假 pip runner：录下 argv / env / timeout，返回预定结果。"""
    calls: list[dict[str, Any]] = []

    async def runner(argv, *, env, timeout):
        calls.append({"argv": list(argv), "env": dict(env), "timeout": timeout})
        return code, out, err

    return runner, calls


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "db.sqlite3")
    s.migrate()
    yield s
    s.close()


def _deps(store, tmp_path: Path, *, env=None, runner=None, workspace_root: Path | None = None) -> od.OptionalDeps:
    """workspace_root 用嵌套目录：pylibs 会落在 tmp_path/pylibs（不会串到别的用例）。"""
    root = workspace_root or (tmp_path / "ws")
    return od.OptionalDeps(store, lambda: _Settings(root), env=env, runner=runner)


# ----------------------------------------------------------------------
# 目录与白名单
# ----------------------------------------------------------------------


def test_pylibs_dir_is_sibling_of_workspace_root(tmp_path: Path) -> None:
    """装在 workspace_root 的**兄弟目录**：workspace_root 自己被 systemd 换成空 tmpfs。"""
    assert od.pylibs_dir(_Settings(tmp_path / "ws")) == tmp_path / "pylibs"
    assert od.pylibs_dir(_Settings(tmp_path / "a" / "b" / "ws")) == tmp_path / "a" / "b" / "pylibs"
    # 没 environments 的 settings（老 stub）→ None，不猜
    assert od.pylibs_dir(types.SimpleNamespace()) is None
    assert od.pylibs_dir(None) is None


def test_office_group_is_the_three_libs() -> None:
    assert "office" in od.GROUPS
    names = [p.name for p in od.GROUPS["office"].packages]
    assert names == ["python-docx", "openpyxl", "python-pptx"]
    assert [p.module for p in od.GROUPS["office"].packages] == ["docx", "openpyxl", "pptx"]
    # 每个包都写清楚版本下限和一句中文用途
    for p in od.GROUPS["office"].packages:
        assert p.min_version and p.note


def test_package_specs_only_whitelist() -> None:
    assert od.package_specs("office") == ["python-docx>=1.1.0", "openpyxl>=3.1.0", "python-pptx>=0.6.23"]
    # 只装白名单里的包名：任意包名 / 带 shell 花样的名字一律拒
    for bad in ("requests", "python-docx; rm -rf /", "../../etc/passwd", "", "python-docx "):
        with pytest.raises(ValueError):
            od.package_specs("office", [bad])
    with pytest.raises(KeyError):
        od.package_specs("nope")


def test_pip_argv_targets_our_dir() -> None:
    argv = od.pip_argv("/usr/bin/python3", Path("/home/maiwork/pylibs"), ["python-docx>=1.1.0"])
    assert argv[:4] == ["/usr/bin/python3", "-m", "pip", "install"]
    assert "--target" in argv and "/home/maiwork/pylibs" in argv
    assert "--upgrade" in argv
    assert argv[-1] == "python-docx>=1.1.0"


# ----------------------------------------------------------------------
# 探测
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_unknown_before_any_check(store, tmp_path: Path) -> None:
    deps = _deps(store, tmp_path, env=_FakeEnv(_probe_stdout(_missing())))
    view = deps.status()
    g = view["groups"][0]
    assert g["state"] == "unknown"
    assert view["checked_at"] == 0
    assert [p["installed"] for p in g["packages"]] == [None, None, None]


@pytest.mark.asyncio
async def test_refresh_reports_installed_versions_and_soffice(store, tmp_path: Path) -> None:
    env = _FakeEnv(_probe_stdout(_installed(), soffice="/usr/bin/soffice"))
    deps = _deps(store, tmp_path, env=env)
    view = await deps.refresh()
    g = view["groups"][0]
    assert view["available"] is True
    assert view["python"] == "/usr/bin/python3"
    assert view["python_version"] == "3.12.3"
    assert view["checked_at"] > 0
    assert g["state"] == "ok" and g["installed_count"] == 3 and g["total"] == 3
    assert g["packages"][0]["version"] == "1.1.2"
    assert g["tools"][0]["name"] == "soffice" and g["tools"][0]["found"] is True
    assert g["tools"][0]["path"] == "/usr/bin/soffice"
    # 探测命令里三个 import 名都要出现（探测和清单不许脱节）
    cmd = env.calls[0]["command"]
    for mod in ("docx", "openpyxl", "pptx"):
        assert mod in cmd
    assert env.calls[0]["timeout_s"] and env.calls[0]["timeout_s"] > 0


@pytest.mark.asyncio
async def test_refresh_partial_and_missing(store, tmp_path: Path) -> None:
    part = _missing()
    part["openpyxl"] = {"installed": True, "version": "3.1.5"}
    deps = _deps(store, tmp_path, env=_FakeEnv(_probe_stdout(part)))
    assert (await deps.refresh())["groups"][0]["state"] == "partial"
    deps2 = _deps(store, tmp_path, env=_FakeEnv(_probe_stdout(_missing())))
    assert (await deps2.refresh())["groups"][0]["state"] == "missing"
    assert deps2.status()["groups"][0]["tools"][0]["found"] is False


@pytest.mark.asyncio
async def test_refresh_without_python_is_honest(store, tmp_path: Path) -> None:
    env = _FakeEnv("bash: python3: command not found\n", exit_code=127)
    deps = _deps(store, tmp_path, env=env)
    view = await deps.refresh()
    assert view["groups"][0]["state"] == "missing"
    assert view["check_error"]
    assert view["python"] == ""


@pytest.mark.asyncio
async def test_refresh_without_local_env_says_why(store, tmp_path: Path) -> None:
    deps = _deps(store, tmp_path, env=None)
    view = await deps.refresh()
    assert view["available"] is False
    assert "本机执行" in view["note"]
    assert view["groups"][0]["state"] == "unknown"


@pytest.mark.asyncio
async def test_check_result_survives_restart(store, tmp_path: Path) -> None:
    """探测结果落 kv：插件重启后网页照样看得到上次结果。"""
    deps = _deps(store, tmp_path, env=_FakeEnv(_probe_stdout(_installed())))
    await deps.refresh()
    again = _deps(store, tmp_path, env=None)
    view = again.status()
    assert view["groups"][0]["state"] == "ok"
    assert view["python"] == "/usr/bin/python3"


# ----------------------------------------------------------------------
# 安装
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_success_uses_probed_python_and_our_dir(store, tmp_path: Path) -> None:
    env = _FakeEnv(_probe_stdout(_installed()))
    runner, calls = _runner(0, "Successfully installed python-docx-1.1.2 openpyxl-3.1.5 python-pptx-0.6.23")
    deps = _deps(store, tmp_path, env=env, runner=runner)
    deps.start_install("office", by="网页管理员")
    assert deps.status()["groups"][0]["state"] == "installing"
    await deps.wait()
    assert len(calls) == 1
    argv = calls[0]["argv"]
    # 用「子 agent 命令实际用的那个 python3」（探测出来的绝对路径）→ ABI 一致
    assert argv[0] == "/usr/bin/python3"
    assert "--target" in argv and str(tmp_path / "pylibs") in argv
    assert "python-docx>=1.1.0" in argv
    assert calls[0]["timeout"] == od.INSTALL_TIMEOUT_S
    g = deps.status()["groups"][0]
    assert g["state"] == "ok"
    assert g["last_install"]["state"] == "ok"
    assert g["last_install"]["by"] == "网页管理员"
    assert g["last_install"]["at"] > 0
    assert "Successfully installed" in g["last_install"]["log"]


@pytest.mark.asyncio
async def test_install_failure_records_error_summary(store, tmp_path: Path) -> None:
    env = _FakeEnv(_probe_stdout(_missing()))
    runner, _ = _runner(1, "", "WARNING: retrying\nERROR: Could not find a version that satisfies the requirement python-docx")
    deps = _deps(store, tmp_path, env=env, runner=runner)
    deps.start_install("office")
    await deps.wait()
    g = deps.status()["groups"][0]
    assert g["state"] == "missing"
    assert g["last_install"]["state"] == "failed"
    assert "Could not find a version" in g["last_install"]["error"]
    assert len(g["last_install"]["error"]) <= od.MAX_ERROR_CHARS


@pytest.mark.asyncio
async def test_install_ok_but_not_importable_is_failed(store, tmp_path: Path) -> None:
    """pip 说装好了、但子 agent 那边 import 不到（比如目录它读不到）→ 如实记失败。"""
    env = _FakeEnv(_probe_stdout(_missing()))
    runner, _ = _runner(0, "Successfully installed python-docx-1.1.2")
    deps = _deps(store, tmp_path, env=env, runner=runner)
    deps.start_install("office")
    await deps.wait()
    g = deps.status()["groups"][0]
    assert g["last_install"]["state"] == "failed"
    assert "import" in g["last_install"]["error"] or "读不到" in g["last_install"]["error"]


@pytest.mark.asyncio
async def test_install_timeout_marks_failed(store, tmp_path: Path) -> None:
    async def runner(argv, *, env, timeout):
        raise asyncio.TimeoutError()

    deps = _deps(store, tmp_path, env=_FakeEnv(_probe_stdout(_missing())), runner=runner)
    deps.start_install("office")
    await deps.wait()
    g = deps.status()["groups"][0]
    assert g["last_install"]["state"] == "failed"
    assert "超时" in g["last_install"]["error"]


@pytest.mark.asyncio
async def test_install_concurrent_only_one(store, tmp_path: Path) -> None:
    """同一时间只装一次：第二次直接 Busy，pip 只被调用一次。"""
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[list[str]] = []

    async def runner(argv, *, env, timeout):
        calls.append(list(argv))
        started.set()
        await release.wait()
        return 0, "", ""

    deps = _deps(store, tmp_path, env=_FakeEnv(_probe_stdout(_installed())), runner=runner)
    deps.start_install("office")
    await started.wait()
    assert deps.status()["groups"][0]["state"] == "installing"
    with pytest.raises(od.Busy):
        deps.start_install("office")
    release.set()
    await deps.wait()
    assert len(calls) == 1
    assert deps.status()["groups"][0]["state"] == "ok"


@pytest.mark.asyncio
async def test_stale_installing_after_restart_is_interrupted(store, tmp_path: Path) -> None:
    """装到一半插件重启了：不许假装还在装（网页会永远转圈），如实说中断了。"""
    deps = _deps(store, tmp_path, env=_FakeEnv(_probe_stdout(_missing())))
    deps._write_kv(f"{od.KV_PREFIX}office", {
        "state": "installing", "at": 1.0, "by": "网页管理员", "error": "", "log": "",
    })
    again = _deps(store, tmp_path, env=_FakeEnv(_probe_stdout(_missing())))
    await again.refresh()
    g = again.status()["groups"][0]
    assert g["installing"] is False
    assert g["last_install"]["state"] == "interrupted"
    # 文案通用（装和卸都可能落到这条 state）：不说「装到一半」
    assert "重新加载" in g["last_install"]["error"] and "重新操作" in g["last_install"]["error"]


@pytest.mark.asyncio
async def test_install_unknown_group(store, tmp_path: Path) -> None:
    deps = _deps(store, tmp_path, env=_FakeEnv(_probe_stdout(_missing())))
    with pytest.raises(KeyError):
        deps.start_install("nope")


@pytest.mark.asyncio
async def test_install_without_local_env_is_unavailable(store, tmp_path: Path) -> None:
    deps = _deps(store, tmp_path, env=None)
    with pytest.raises(od.Unavailable):
        deps.start_install("office")


@pytest.mark.asyncio
async def test_install_env_has_no_host_secrets(store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "绝密值不许进子进程")
    monkeypatch.setenv("MAIWORK_TEST_SECRET", "也不许")
    runner, calls = _runner(0, "ok")
    deps = _deps(store, tmp_path, env=_FakeEnv(_probe_stdout(_installed())), runner=runner)
    deps.start_install("office")
    await deps.wait()
    env = calls[0]["env"]
    # SystemRoot / windir 只在 Windows 上带（PR #5）；其余白名单项每个平台都要有
    windows_only = {"SystemRoot", "windir"}
    assert set(env) <= set(od.PIP_ENV_KEYS)
    assert set(od.PIP_ENV_KEYS) - windows_only <= set(env)
    assert (set(env) & windows_only) == set(od.windows_system_env())
    assert "TYPESAFE_API_KEY" not in env and "MAIWORK_TEST_SECRET" not in env
    assert env["PATH"] == od.PIP_PATH
    assert env["HOME"] == str(tmp_path)  # pylibs 的上一级，不是宿主 HOME


@pytest.mark.asyncio
async def test_install_retries_with_break_system_packages(store, tmp_path: Path) -> None:
    """PEP 668 externally-managed：第一次失败就带 --break-system-packages 再来一次。"""
    calls: list[list[str]] = []

    async def runner(argv, *, env, timeout):
        calls.append(list(argv))
        if len(calls) == 1:
            return 1, "", "error: externally-managed-environment\n"
        return 0, "Successfully installed", ""

    deps = _deps(store, tmp_path, env=_FakeEnv(_probe_stdout(_installed())), runner=runner)
    deps.start_install("office")
    await deps.wait()
    assert len(calls) == 2
    assert "--break-system-packages" in calls[1]
    assert deps.status()["groups"][0]["last_install"]["state"] == "ok"


@pytest.mark.asyncio
async def test_install_writes_kv_state(store, tmp_path: Path) -> None:
    runner, _ = _runner(0, "ok")
    deps = _deps(store, tmp_path, env=_FakeEnv(_probe_stdout(_installed())), runner=runner)
    deps.start_install("office")
    assert store.kv_get(f"{od.KV_PREFIX}office")["state"] == "installing"
    await deps.wait()
    rec = store.kv_get(f"{od.KV_PREFIX}office")
    assert rec["state"] == "ok" and rec["at"] > 0


# ----------------------------------------------------------------------
# 卸载
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uninstall_removes_only_this_group(store, tmp_path: Path) -> None:
    libs = tmp_path / "pylibs"
    (libs / "docx").mkdir(parents=True)
    (libs / "docx" / "__init__.py").write_text("x")
    (libs / "python_docx-1.1.2.dist-info").mkdir()
    (libs / "openpyxl").mkdir()
    (libs / "openpyxl-3.1.5.dist-info").mkdir()
    (libs / "pptx").mkdir()
    (libs / "python_pptx-0.6.23.dist-info").mkdir()
    (libs / "lxml").mkdir()  # 依赖装的、不属于本组：留着
    (libs / "lxml-5.2.1.dist-info").mkdir()

    runner, _ = _runner(0, "ok")
    deps = _deps(store, tmp_path, env=_FakeEnv(_probe_stdout(_missing())), runner=runner)
    deps.start_uninstall("office")
    await deps.wait()
    assert not (libs / "docx").exists()
    assert not (libs / "python_docx-1.1.2.dist-info").exists()
    assert not (libs / "openpyxl").exists()
    assert not (libs / "pptx").exists()
    assert (libs / "lxml").exists() and (libs / "lxml-5.2.1.dist-info").exists()
    assert deps.status()["groups"][0]["state"] == "missing"
    with pytest.raises(KeyError):
        deps.start_uninstall("nope")


# ----------------------------------------------------------------------
# 配置热更（rebind）：旧环境 / 旧目录的探测结果一个字都不能接着用
# ----------------------------------------------------------------------


def _deps_movable(store, root: Path, *, env=None, capability=None):
    """工作区根可以中途换掉的 OptionalDeps（模拟网页改了 workspace_root 后热更）。"""
    box = {"root": root}
    deps = od.OptionalDeps(
        store, lambda: _Settings(box["root"]), env=env, capability=capability
    )
    return deps, box


def _cap(mode: str = "fixed", ok: bool = True):
    return types.SimpleNamespace(mode=mode, ok=ok)


@pytest.mark.asyncio
async def test_rebind_drops_stale_probe_when_dir_changes(store, tmp_path: Path) -> None:
    """换了工作区根 = 换了安装目录：旧目录那份探测结果（内存缓存 + kv）都不许再用。

    以前 rebind 只清 `_python_path`，`_cache` 留着，kv 也照读——网页上会看到
    「新目录的路径 + 旧目录的探测结果」，最长 5 分钟（CHECK_TTL_S）都是错的。
    """
    deps, box = _deps_movable(store, tmp_path / "a" / "ws", env=_FakeEnv(_probe_stdout(_installed())), capability=_cap())
    await deps.refresh()
    assert deps.status()["groups"][0]["state"] == "ok"

    box["root"] = tmp_path / "b" / "ws"      # 新目录（pylibs 也跟着换）：装的东西在别处
    deps.rebind(env=_FakeEnv(_probe_stdout(_missing())), capability=_cap())
    view = deps.status()
    assert view["checked_at"] == 0           # 旧探测结果不再冒充「现在的状态」
    assert view["python"] == ""
    assert view["groups"][0]["state"] == "unknown"
    assert [p["installed"] for p in view["groups"][0]["packages"]] == [None, None, None]

    await deps.refresh()                     # 新环境探一次，才是新目录的真状态
    assert deps.status()["groups"][0]["state"] == "missing"


@pytest.mark.asyncio
async def test_rebind_drops_stale_probe_even_with_same_env_object(store, tmp_path: Path) -> None:
    """app.update_config 的真实调用形状：env 对象**不换**（LocalEnv 每次读 settings），只换配置。

    所以判断「换了环境」不能靠对象身份，得靠指纹（安装目录 / 判定 / 本地模式 / 运行账号）。
    """
    same_env = _FakeEnv(_probe_stdout(_installed()))
    deps, box = _deps_movable(store, tmp_path / "a" / "ws", env=same_env, capability=_cap())
    await deps.refresh()
    assert deps.status()["groups"][0]["state"] == "ok"

    box["root"] = tmp_path / "b" / "ws"       # 只换配置：env 还是同一个对象
    deps.rebind(env=same_env, capability=_cap())
    view = deps.status()
    assert view["checked_at"] == 0
    assert view["groups"][0]["state"] == "unknown"


@pytest.mark.asyncio
async def test_rebind_keeps_cache_when_environment_unchanged(store, tmp_path: Path) -> None:
    """同一个目录、同一个判定：只是重接了一个新对象（每次保存配置都会走一趟）→ 不白探一次。

    这条是**故意**的行为：指纹（安装目录 + 判定 + 本地模式 + 运行账号）没变时，
    上一次的探测结果依然成立；否则管理员每存一次设置，可选组件页就要同步重探一次。
    """
    env = _FakeEnv(_probe_stdout(_installed()))
    deps, _box = _deps_movable(store, tmp_path / "ws", env=env, capability=_cap())
    await deps.refresh()
    assert len(env.calls) == 1

    deps.rebind(env=_FakeEnv(_probe_stdout(_missing())), capability=_cap())
    view = deps.status()
    assert view["checked_at"] > 0
    assert view["groups"][0]["state"] == "ok"
    assert len(env.calls) == 1                # 没有为了「重接」再探一次


@pytest.mark.asyncio
async def test_inflight_probe_is_discarded_after_rebind(store, tmp_path: Path) -> None:
    """探测跑到一半环境换了：这次结果回来必须丢掉，不许覆盖新环境的状态 / kv。"""
    gate = asyncio.Event()

    class _SlowEnv(_FakeEnv):
        async def run(self, name, command, *, timeout_s=None, memory=None, cwd=""):
            self.calls.append({"name": name, "command": command, "timeout_s": timeout_s})
            await gate.wait()
            return types.SimpleNamespace(
                exit_code=0, stdout=self.stdout, stderr="", ms=1, timed_out=False, oom=False
            )

    old = _SlowEnv(_probe_stdout(_installed()))
    deps, box = _deps_movable(store, tmp_path / "ws", env=old, capability=_cap())
    task = asyncio.create_task(deps.refresh())
    for _ in range(5):
        await asyncio.sleep(0)               # 让 refresh 进到旧环境那次探测里
    assert old.calls, "旧环境那次探测得真跑起来，否则这条测试是空的"

    box["root"] = tmp_path / "ws2"
    deps.rebind(env=_FakeEnv(_probe_stdout(_missing())), capability=_cap())
    gate.set()
    await task

    assert store.kv_get(od.CHECK_KEY) is None          # 旧结果没落 kv
    view = deps.status()
    assert view["checked_at"] == 0
    assert view["groups"][0]["state"] == "unknown"


# ----------------------------------------------------------------------
# 卸载的前置闸 / 权限
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uninstall_without_local_env_is_refused_and_keeps_last_check(store, tmp_path: Path) -> None:
    """本机执行没就位时「卸」要当场拒掉：以前它会拿「没环境」的错误桩覆盖上次那份好探测结果。"""
    libs = tmp_path / "pylibs"
    (libs / "docx").mkdir(parents=True)
    with_env = _deps(store, tmp_path, env=_FakeEnv(_probe_stdout(_installed())))
    await with_env.refresh()
    before = store.kv_get(od.CHECK_KEY)
    assert isinstance(before, dict) and before.get("python")

    deps = _deps(store, tmp_path, env=None)            # 这台机器不能隔离跑命令 / 模块没接上
    with pytest.raises(od.Unavailable):
        deps.start_uninstall("office")
    await deps.wait()

    assert store.kv_get(od.CHECK_KEY) == before        # 上次那份好结果一个字没动
    assert (libs / "docx").exists()                    # 这次活根本没起，文件也不该少
    assert store.kv_get(f"{od.KV_PREFIX}office") is None
    g = deps.status()["groups"][0]
    assert g["uninstalling"] is False and g["installing"] is False


def test_relax_perms_opens_pylibs_root_dir(tmp_path: Path) -> None:
    """pip 由插件进程（root）跑：umask 紧的时候连 pylibs 根目录都是 0700，子 agent 进不去。

    根目录本身也必须放开（os.walk 只遍历它的子项，以前漏的就是它）。
    """
    root = tmp_path / "pylibs"
    (root / "docx").mkdir(parents=True)
    (root / "docx" / "__init__.py").write_text("x", encoding="utf-8")
    (root / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(root, 0o700)
    os.chmod(root / "docx", 0o700)
    os.chmod(root / "docx" / "__init__.py", 0o600)
    os.chmod(root / "run.sh", 0o700)

    od._relax_perms(root)

    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    assert stat.S_IMODE((root / "docx").stat().st_mode) == 0o755
    assert stat.S_IMODE((root / "docx" / "__init__.py").stat().st_mode) == 0o644
    assert stat.S_IMODE((root / "run.sh").stat().st_mode) == 0o755   # 可执行位保持


class _GatedEnv(_FakeEnv):
    """run() 卡在闸门上不返回：用来在「探测跑到一半」时做热更（rebind）。"""

    def __init__(self, stdout: str, gate: asyncio.Event) -> None:
        super().__init__(stdout)
        self._gate = gate

    async def run(self, name, command, *, timeout_s=None, memory=None, cwd=""):
        self.calls.append({"name": name, "command": command, "timeout_s": timeout_s})
        await self._gate.wait()
        return types.SimpleNamespace(
            exit_code=self.exit_code, stdout=self.stdout, stderr=self.stderr,
            ms=1, timed_out=False, oom=False,
        )


@pytest.mark.asyncio
async def test_python_probe_reprobes_current_env_after_rebind(store, tmp_path: Path) -> None:
    """探测解释器跑到一半环境换了：不许把旧环境的 python 路径缓存给新环境用。

    旧行为会 `_commit_check` 丢弃结果、却照样 `self._python_path = info["python"]`——
    新环境拿旧解释器去 pip，装到别的 ABI 目录里，装完 import 不到。
    """
    gate = asyncio.Event()
    old = _GatedEnv(_probe_stdout(_installed(), python="/usr/bin/python3.11"), gate)
    deps, box = _deps_movable(store, tmp_path / "a" / "ws", env=old, capability=_cap())
    task = asyncio.create_task(deps._python())
    for _ in range(5):
        await asyncio.sleep(0)
    assert old.calls, "旧环境那次探测得真跑起来，否则这条测试是空的"

    box["root"] = tmp_path / "b" / "ws"
    deps.rebind(env=_FakeEnv(_probe_stdout(_installed(), python="/usr/bin/python3.12")), capability=_cap())
    gate.set()
    got = await task

    assert got == "/usr/bin/python3.12"                     # 按当前环境重探，不是旧的 3.11
    assert deps._python_path == "/usr/bin/python3.12"
    assert store.kv_get(od.CHECK_KEY)["python"] == "/usr/bin/python3.12"


def test_remove_group_files_counts_only_really_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """rmtree(ignore_errors=True) 会吞掉失败：删不掉的不许算「删掉了」。"""
    libs = tmp_path / "pylibs"
    (libs / "docx").mkdir(parents=True)
    (libs / "openpyxl").mkdir()
    (libs / "lxml").mkdir()                 # 别的组的依赖：不碰
    real = shutil.rmtree

    def fake(path, *a, **kw):
        if Path(path).name == "docx":
            return                          # 假装删了：其实还在
        return real(path, *a, **kw)

    monkeypatch.setattr(od.shutil, "rmtree", fake)
    assert od._remove_group_files(libs, od.GROUPS["office"]) == 1
    assert od._group_leftovers(libs, od.GROUPS["office"]) == ["docx"]
    assert (libs / "lxml").exists()


@pytest.mark.asyncio
async def test_uninstall_reports_leftovers_as_failed(store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """卸不干净要如实报失败（以前 rmtree 吞错、照样记 ok）。"""
    libs = tmp_path / "pylibs"
    (libs / "docx").mkdir(parents=True)
    monkeypatch.setattr(od.shutil, "rmtree", lambda *a, **kw: None)
    deps = _deps(store, tmp_path, env=_FakeEnv(_probe_stdout(_installed())))
    deps.start_uninstall("office")
    await deps.wait()
    g = deps.status()["groups"][0]
    assert g["last_install"]["state"] == "failed"
    assert "docx" in g["last_install"]["error"]
    assert "仍存在" in g["last_install"]["log"]
    assert (libs / "docx").exists()


@pytest.mark.asyncio
async def test_uninstall_with_probe_error_is_not_ok(store, tmp_path: Path) -> None:
    """文件已删除、但卸载后探测没跑起来：状态无法确认，不许记 ok。"""
    (tmp_path / "pylibs" / "docx").mkdir(parents=True)
    deps = _deps(store, tmp_path, env=_FakeEnv("", exit_code=1))   # 没有探测结果那一行
    deps.start_uninstall("office")
    await deps.wait()
    g = deps.status()["groups"][0]
    assert g["last_install"]["state"] == "failed"
    assert "无法确认" in g["last_install"]["error"]


@pytest.mark.asyncio
async def test_uninstall_result_discarded_when_env_changes_midway(store, tmp_path: Path) -> None:
    """卸的过程中环境换了：这次结果不许落缓存 / kv，也不许记 ok。"""
    gate = asyncio.Event()
    old = _GatedEnv(_probe_stdout(_missing()), gate)
    deps, box = _deps_movable(store, tmp_path / "a" / "ws", env=old, capability=_cap())
    (tmp_path / "a" / "pylibs" / "docx").mkdir(parents=True)
    deps.start_uninstall("office")
    for _ in range(5):
        await asyncio.sleep(0)
    assert old.calls, "旧环境那次探测得真跑起来，否则这条测试是空的"

    box["root"] = tmp_path / "b" / "ws"
    deps.rebind(env=_FakeEnv(_probe_stdout(_missing())), capability=_cap())
    gate.set()
    await deps.wait()

    assert store.kv_get(od.CHECK_KEY) is None
    g = deps.status()["groups"][0]
    assert g["last_install"]["state"] == "failed"
    assert "无法确认" in g["last_install"]["error"]


@pytest.mark.asyncio
async def test_dynamic_mode_install_refused_but_uninstall_allowed(store, tmp_path: Path) -> None:
    """dynamic 模式（系统临时账号隔离）装不了：说清原因，别让管理员反复白装。

    docs/06 实测：dynamic 下 `/var/lib/private/` 在单元的命名空间里只挂了它自己的工作区
    → `<工作区根>/../pylibs` 子 agent 根本看不到，装了也 import 不到。
    但「卸」不该被这条挡住（能跑命令就能删、能重新探测）。
    """
    libs = tmp_path / "pylibs"
    (libs / "docx").mkdir(parents=True)
    deps, _box = _deps_movable(
        store, tmp_path / "ws", env=_FakeEnv(_probe_stdout(_missing())), capability=_cap("dynamic")
    )
    view = deps.status()
    assert view["available"] is False
    assert "没有固定运行账号" in view["note"]
    with pytest.raises(od.Unavailable):
        deps.start_install("office")

    deps.start_uninstall("office")
    await deps.wait()
    assert deps.status()["groups"][0]["last_install"]["state"] == "ok"
    assert not (libs / "docx").exists()
