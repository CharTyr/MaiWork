"""共用测试设置：把插件目录的上一级放进 sys.path，测试里用 `CharTyr_MaiWork.xxx` 导入。

找不到插件目录就直接报错（不许 skip 造成假绿）。
共用假对象在 fakes.py，测试里 `from fakes import FakeCtx`。

隔离罩（autouse fixture `_isolated_host`，每个测试自动套上）——测试绝不能碰
真实 systemd / 真实密钥 / 真实 HOME。这是 2026-09 的教训：同样的代码拿到线上
Linux（root、trafilatura 已装、/root/.typesafe_key 存在）跑，一批「本机全绿」的
测试当场变红，还差点用 systemd-run 在服务器上起了真单元：
- root：config 的 G1 防线会把 local_mode=direct 强制改成 systemd（线上行为是对的，
  但测试里用 direct 的用例全被改了底盘）→ 罩子里假装不是 root；
  专测 G1 的 test_audit_g1 自己再把它 patch 成 True。
- 密钥：TYPESAFE_API_KEY / TYPESAFE_KEY_FILE 环境变量删掉，Path.home() 指到每个
  测试自己的空目录——jev 默认读 ~/.typesafe_key，不挡的话线上会读到真密钥。
- 安全网：asyncio.create_subprocess_exec 被包了一层，argv[0] 是 systemd-run /
  systemctl 直接炸（注入了假 runner 的用例不受影响——它们根本不走默认 runner）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if not (PLUGIN_DIR / "plugin.py").is_file():
    raise RuntimeError(f"找不到插件目录: {PLUGIN_DIR}")
if str(PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR.parent))
if PLUGIN_DIR.name != "CharTyr_MaiWork" and "CharTyr_MaiWork" not in sys.modules:
    # 从插件市场 / git clone 装下来的目录名不一定是 CharTyr_MaiWork：按这个包名挂上去，
    # 测试里的 `CharTyr_MaiWork.xxx` 照样能导入（插件自身全用相对导入，不受目录名影响）。
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "CharTyr_MaiWork", PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["CharTyr_MaiWork"] = _pkg
    _spec.loader.exec_module(_pkg)
TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

# 安全网拦的真实系统命令（basename，-- 不管绝对路径还是裸名都拦）
_SYSTEMD_BINARIES = ("systemd-run", "systemctl")


@pytest.fixture
def plugin_dir() -> Path:
    return PLUGIN_DIR


@pytest.fixture(autouse=True)
def _isolated_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """假宿主机隔离罩：非 root、无密钥、空 HOME、不许碰真实 systemd。每个测试自动生效。"""
    from CharTyr_MaiWork import config as _config

    # (a) 假装不是 root：G1 的 direct→systemd 强制回落不在普通测试里生效。
    #     test_audit_g1 专测这条防线，自己再 patch 成 True。
    monkeypatch.setattr(_config, "_running_as_root", lambda: False)

    # (b) 密钥与 HOME：环境变量清空；Path.home() 和 HOME 都指到空目录。
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_KEY_FILE", raising=False)
    fake_home = tmp_path / "tmp-home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("HOME", str(fake_home))

    # (c) 安全网：测试里起 systemd-run / systemctl 真实进程 → 立刻炸。
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def _no_real_systemd(*args, **kwargs):
        argv0 = Path(str(args[0])).name if args else ""
        if argv0 in _SYSTEMD_BINARIES:
            raise RuntimeError(f"测试里不许调用真实 systemd（{argv0}）：systemd 模式请注入假 runner 录 argv")
        return await real_create_subprocess_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _no_real_systemd)



@pytest.fixture(autouse=True)
def _no_model_retry_pause(monkeypatch):
    """模型重试前默认要等 10 秒（可重试错误的退避）；测试里不等——
    把 models 的模块级 _SLEEP 换成立即返回的假函数。"""
    try:
        import CharTyr_MaiWork.models as _mm

        async def _zero_sleep(seconds: float) -> None:
            return None

        monkeypatch.setattr(_mm, "_SLEEP", _zero_sleep)
    except Exception:
        pass
