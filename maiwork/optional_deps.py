"""可选依赖：办公文档三库（python-docx / openpyxl / python-pptx）按需安装。

用户 2026-10-15 定：这三库以「可选安装」方式提供，**不加进 `_manifest.json` 的必装依赖**
（不想为了少数派活让每个安装都多背三个库）；管理员在网页上一键装。

## 装到哪、为什么

子 agent 跑本机命令时用的是**宿主系统 PATH 上的 python3**，不是 MaiBot 自己的 venv：

- `environments/local.py` 的 `_minimal_env()` 只给最小环境
  （`PATH=/usr/local/bin:/usr/bin:/bin:...`、`HOME=工作区`、`LANG`），**绝不继承插件进程
  的环境**（防密钥漏给子进程）。所以子 agent 的 `python3` 是系统 python3，
  往 MaiBot 的 venv 里装东西对它一点用没有，而且**改宿主环境是线上红线**。
- 所以装到 MaiWork 自己的目录 `<workspace_root>/../pylibs`
  （默认 `/home/maiwork/pylibs`），再用 `PYTHONPATH` 让子 agent 的 `python3` 找到它。
  为什么是 `workspace_root` 的**兄弟**而不是它下面：`workspace_root`
  （`/home/maiwork/workspaces`）在 systemd 单元里被 `TemporaryFileSystem` 换成空 tmpfs、
  只 `BindPaths` 回本工作区，放里面子 agent 看不见；兄弟目录 `/home/maiwork/pylibs`
  在 `ProtectHome=read-only` 下可见可读（属主是 `maiwork`），也不在 `/root`
  （0700，`maiwork` 读不到——MaiBot 的数据目录在那里）。宿主进程不受影响。

## 怎么装

用「子 agent 命令实际用的那个 python3」（探测出来的**绝对路径**，保证 ABI 一致）跑
`<python> -m pip install --target <dir> --upgrade <白名单包>`：

- 只装白名单里的包名（`package_specs` 严格核对，不接受任意包名 / 带 shell 花样的名字）；
- 超时 `INSTALL_TIMEOUT_S`、输出截断、pip 进程只给最小环境（不继承插件进程的密钥）；
- 同一时间只装一次（`Busy`）；
- 状态写 kv：`optional_deps.<组>` = installing / ok / failed + 时间 + 错误摘要；
  探测结果写 `optional_deps.check`（重启后网页照样看得到上次结果）；
- **装完必须重新探测**：pip 说成功、但子 agent 那边 import 不到（比如目录它读不到）
  → 记 failed，不许报成功。
- LibreOffice（`soffice`）**只检测不安装**（系统级软件，MaiWork 不替用户装）。

## 一次装 / 一次探测用哪个 python

`python3 -c '<探测程序>'` 走的还是 LocalEnv 的最小 PATH，所以打印出来的
`sys.executable` 就是子 agent 真会用的那个解释器；安装就拿它去 pip。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import clock

logger = logging.getLogger("maiwork.optional_deps")

__all__ = [
    "GROUPS", "Group", "Package", "OptionalDeps", "Busy", "Unavailable",
    "pylibs_dir", "package_specs", "pip_argv", "pip_env", "probe_command",
    "KV_PREFIX", "INSTALL_TIMEOUT_S", "MAX_ERROR_CHARS", "PIP_PATH", "PIP_ENV_KEYS",
]

PYLIBS_DIRNAME = "pylibs"
KV_PREFIX = "optional_deps."
CHECK_KEY = KV_PREFIX + "check"
INSTALL_TIMEOUT_S = 300          # 装依赖最长等 5 分钟
PROBE_TIMEOUT_S = 60             # 探测（查库 + 查 soffice）最长等 1 分钟
CHECK_TTL_S = 300.0              # 探测结果的保鲜期：5 分钟内不重复探测
MAX_ERROR_CHARS = 300            # 错误摘要最多留多少字
MAX_LOG_CHARS = 4000             # pip 输出最多留多少字（末尾）
_PROBE_MARK = "MAIWORK_DEPS_JSON="
_NO_SOFFICE_MARK = "MAIWORK_NO_SOFFICE"
_PROBE_WORKSPACE = "_deps"       # 探测用的一次性工作区名（[A-Za-z0-9_-]{1,64}）

# pip 进程的最小环境：绝不继承插件进程的环境（密钥不落子进程）。
PIP_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"
PIP_ENV_KEYS = (
    "PATH", "HOME", "LANG",
    "PIP_DISABLE_PIP_VERSION_CHECK", "PIP_NO_INPUT", "PIP_NO_CACHE_DIR", "PYTHONNOUSERSITE",
)

# 只检测、不安装的外部命令（系统级软件，MaiWork 不替用户装）。
_TOOL_CHECKS: dict[str, dict[str, Any]] = {
    "soffice": {
        "bins": ("soffice", "libreoffice"),
        "note": "LibreOffice：转 PDF、重算 Excel 公式需要它；属系统级软件，MaiWork 不代为安装（未安装时按 skill 中的替代做法处理）",
    },
}


# ----------------------------------------------------------------------
# 清单
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Package:
    """一个可选包：pip 名、import 名、版本下限、一句中文用途。"""

    name: str
    module: str
    min_version: str
    note: str


@dataclass(frozen=True)
class Group:
    """一组可选依赖（现在只有「办公文档」一组）。"""

    id: str
    label: str
    purpose: str
    packages: tuple[Package, ...]
    tools: tuple[str, ...] = ()


GROUPS: dict[str, Group] = {
    "office": Group(
        id="office",
        label="办公文档（Word / Excel / PPT）",
        purpose=(
            "子 agent 要交 .docx / .xlsx / .pptx 时用这三个库生成；"
            "转 PDF、重算 Excel 公式还要系统里有 LibreOffice。不装也能干活："
            "可以改交 Markdown / CSV / HTML。"
        ),
        packages=(
            Package("python-docx", "docx", "1.1.0", "读写 Word（.docx）"),
            Package("openpyxl", "openpyxl", "3.1.0", "读写 Excel（.xlsx）"),
            Package("python-pptx", "pptx", "0.6.23", "读写 PPT（.pptx）"),
        ),
        tools=("soffice",),
    ),
}

GROUP_IDS: tuple[str, ...] = tuple(GROUPS)


# ----------------------------------------------------------------------
# 目录 / 命令行拼装（纯函数，方便单测）
# ----------------------------------------------------------------------


def pylibs_dir(settings: Any) -> Path | None:
    """可选依赖装在哪个目录：`<workspace_root>/../pylibs`；读不到配置返回 None。

    **不创建目录**（只有真装的时候才建）：没装过的机器上这个路径就是不存在，
    LocalEnv 也不会往最小环境里塞一个空的 PYTHONPATH。
    """
    env = getattr(settings, "environments", None)
    root = getattr(env, "workspace_root", None) if env is not None else None
    text = str(root or "").strip()
    if not text:
        return None
    return Path(text).parent / PYLIBS_DIRNAME


def _group(group_id: str) -> Group:
    key = str(group_id or "")
    if key not in GROUPS:
        raise KeyError(f"没有叫「{key}」的可选组件（有的是：{'、'.join(GROUP_IDS)}）")
    return GROUPS[key]


def package_specs(group_id: str, names: Iterable[str] | None = None) -> list[str]:
    """组内包名 → pip 规格（`名字>=下限`）。

    `names` 缺省 = 整组；给了就**逐个严格核对白名单**：不在清单里的名字（包括空串、
    带空格、带 `;` / `/` 这类花样的）一律 ValueError——绝不接受任意包名。
    """
    group = _group(group_id)
    allow = {p.name: p for p in group.packages}
    if names is None:
        return [f"{p.name}>={p.min_version}" for p in group.packages]
    out: list[str] = []
    for raw in names:
        name = str(raw if raw is not None else "")
        if name not in allow:
            raise ValueError(f"「{name}」不在可选依赖白名单里（只允许：{'、'.join(allow)}）")
        out.append(f"{name}>={allow[name].min_version}")
    return out


def pip_argv(python: str, target: Path, specs: Sequence[str]) -> list[str]:
    """拼 pip 命令：装到我们自己的目录，不碰系统环境。"""
    return [
        str(python), "-m", "pip", "install",
        "--target", str(target),
        "--upgrade",
        "--no-input",
        "--disable-pip-version-check",
        *[str(s) for s in specs],
    ]


def pip_env(target: Path) -> dict[str, str]:
    """pip 进程的最小环境：最小 PATH、HOME 指到目录上一级（别去写 /root）、禁缓存。"""
    return {
        "PATH": PIP_PATH,
        "HOME": str(Path(target).parent),
        "LANG": "C.UTF-8",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "PIP_NO_CACHE_DIR": "1",
        "PYTHONNOUSERSITE": "1",
    }


def _probe_program() -> str:
    """探测程序（从 GROUPS 生成，清单和探测不许脱节）。

    注意：整段塞进 `python3 -c '<程序>'`，所以**程序里一个单引号都不能有**。
    """
    pairs: list[str] = []
    for group in GROUPS.values():
        for pkg in group.packages:
            item = '("%s","%s")' % (pkg.module, pkg.name)
            if item not in pairs:
                pairs.append(item)
    return (
        "import json,sys,importlib\n"
        'out={"python":sys.executable,"version":sys.version.split()[0],"packages":{}}\n'
        "for mod,name in (" + ",".join(pairs) + ",):\n"
        "    try:\n"
        "        m=importlib.import_module(mod)\n"
        '        out["packages"][name]={"installed":True,"version":str(getattr(m,"__version__","") or "")}\n'
        "    except Exception as e:\n"
        '        out["packages"][name]={"installed":False,"error":type(e).__name__}\n'
        'print("' + _PROBE_MARK + '"+json.dumps(out))\n'
    )


def probe_command() -> str:
    """一条命令查完：每个包的 import 情况 + 这台机器有没有 soffice。

    走的是子 agent 同一条路（LocalEnv 的最小 PATH + 我们注入的 PYTHONPATH），
    所以探测结果就是子 agent 的实际情况。
    """
    return (
        "python3 -c '" + _probe_program() + "'\n"
        "command -v soffice || command -v libreoffice || echo " + _NO_SOFFICE_MARK + "\n"
    )


def _decode(data: Any) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data or "")


def _tail_text(text: str, limit: int = MAX_LOG_CHARS) -> str:
    body = str(text or "").strip()
    return body if len(body) <= limit else body[-limit:]


def _error_summary(text: str) -> str:
    """错误摘要：优先 pip 那行 `ERROR: ...`，否则最后一行非空；截断。"""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    pick = ""
    for line in lines:
        if line.startswith("ERROR") or "error:" in line.lower():
            pick = line
    if not pick:
        pick = lines[-1] if lines else ""
    return (pick[:MAX_ERROR_CHARS] or "安装依赖失败（安装程序未给出更多信息）")


def _pkg_installed(check: Mapping[str, Any], name: str) -> bool:
    got = (check.get("packages") or {}).get(name)
    return bool(isinstance(got, dict) and got.get("installed"))


def _parse_probe(stdout: str, *, exit_code: int = 0) -> dict[str, Any]:
    """解析探测输出 → {python, version, packages, soffice, error}。"""
    info: dict[str, Any] = {
        "python": "", "version": "", "packages": {},
        "soffice": {"found": False, "path": ""}, "error": "",
    }
    for raw in str(stdout or "").splitlines():
        line = raw.strip()
        if line.startswith(_PROBE_MARK):
            try:
                data = json.loads(line[len(_PROBE_MARK):])
            except ValueError:
                continue
            if isinstance(data, dict):
                info["python"] = str(data.get("python") or "")
                info["version"] = str(data.get("version") or "")
                pkgs = data.get("packages")
                if isinstance(pkgs, dict):
                    info["packages"] = pkgs
        elif not info["soffice"]["found"] and line.startswith("/"):
            base = line.rsplit("/", 1)[-1]
            if base in ("soffice", "libreoffice"):
                info["soffice"] = {"found": True, "path": line}
    if not info["python"]:
        info["error"] = (
            "探测命令未能启动（本机 PATH 中没有 python3）"
            if int(exit_code or 0) != 0
            else "探测输出无法解析（未取得结果）"
        )
    return info


def _relax_perms(root: Path) -> None:
    """装完把权限放宽到别的账号也能读。

    子 agent 是另一个用户（默认 `maiwork`），插件是 root 在装——root 的 umask 要是很紧
    （077），装出来的文件它读不到。**根目录本身也要放开**（`os.walk` 只遍历它的子项，
    根目录 0700 的话子 agent 连进都进不去），目录 755、可执行文件保持可执行、其余 644。
    """
    _chmod(root, 0o755)
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            _chmod(Path(dirpath) / name, 0o755)
        for name in filenames:
            path = Path(dirpath) / name
            try:
                mode = path.stat().st_mode
            except OSError:
                continue
            _chmod(path, 0o755 if mode & 0o100 else 0o644)


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _dist_stem(name: str) -> str:
    """`python_docx-1.1.2.dist-info` → `python_docx`。"""
    return str(name).split("-", 1)[0].strip().lower()


def _group_names(group: Group) -> set[str]:
    """本组在 pylibs 目录里的顶层名字（模块目录 / 单文件模块 / 自己的 dist-info 前缀）。"""
    want: set[str] = set()
    for pkg in group.packages:
        want.add(pkg.module)
        want.add(pkg.module + ".py")
        want.add(pkg.name.lower().replace("-", "_"))
    return want


def _is_group_entry(name: str, want: set[str]) -> bool:
    """这个顶层条目是不是本组的（dist-info / egg-info 按包名前缀认）。"""
    if name.endswith(".dist-info") or name.endswith(".egg-info"):
        return _dist_stem(name) in want
    return name in want


def _group_leftovers(root: Path, group: Group) -> list[str]:
    """本组**还留在目录里**的顶层条目名（真没删掉的）。

    卸载要如实报结果：`shutil.rmtree(ignore_errors=True)` 会吞掉失败，删不掉还记成功
    的话，网页上会说「卸干净了」，实际上三库还在。所以删完再列一遍，有残留就报失败。
    """
    base = Path(root)
    if not base.is_dir():
        return []
    want = _group_names(group)
    out: list[str] = []
    for child in sorted(base.iterdir()):
        if child.is_symlink():      # 符号链接不删也不报（不是本组装进去的东西）
            continue
        if _is_group_entry(child.name, want):
            out.append(child.name)
    return out


def _remove_group_files(root: Path, group: Group) -> int:
    """删掉本组装进去的东西（模块目录 / 单文件模块 / 自己的 dist-info），返回**真删掉**的条数。

    只删目录里、名字对得上的顶层条目；依赖顺带装的别的包（lxml 之类）留着
    ——它们不碍事，也不属于这一组。绝不跟随符号链接、绝不碰目录外的东西。
    删不掉的（权限等）不计数——调用方用 `_group_leftovers()` 看残留、如实报失败。
    """
    base = Path(root)
    if not base.is_dir():
        return 0
    want = _group_names(group)
    removed = 0
    for child in sorted(base.iterdir()):
        if not _is_group_entry(child.name, want):
            continue
        if child.is_symlink():
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
        except OSError:
            logger.warning("卸载时删不掉 %s（权限？）", child, exc_info=True)
        if child.is_symlink() or child.exists():
            continue                # 还在（rmtree 吞了错、或部分删除）→ 不算删掉
        removed += 1
    return removed


async def _default_pip_runner(argv: Sequence[str], *, env: Mapping[str, str], timeout: float) -> tuple[int, str, str]:
    """默认 pip runner：直接开子进程（插件进程是 root，写得进自己的目录）。

    `environments/local.py` 的 runner 跑在加固过的单元里（那个目录只读），装不了东西，
    所以这里不用它——安装只碰 MaiWork 自己的目录。
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=dict(env),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        raise
    code = proc.returncode if proc.returncode is not None else -1
    return int(code), _decode(out_b), _decode(err_b)


class Busy(RuntimeError):
    """这一组正在装 / 正在卸，别重复起。"""


class Unavailable(RuntimeError):
    """本机执行环境没就位 / 没 python3 / 没地方装。"""


# ----------------------------------------------------------------------
# 主体
# ----------------------------------------------------------------------


@dataclass
class _Install:
    state: str = "failed"
    at: float = 0.0
    by: str = ""
    error: str = ""
    log: str = ""
    packages: list[str] = field(default_factory=list)


class OptionalDeps:
    """可选依赖的装 / 卸 / 探测；状态落 kv，探测走本机执行环境（可注入假对象）。"""

    def __init__(
        self,
        store: Any,
        get_settings: Callable[[], Any],
        *,
        env: Any = None,
        capability: Any = None,
        runner: Any = None,
        timeout_s: int = INSTALL_TIMEOUT_S,
        probe_timeout_s: int = PROBE_TIMEOUT_S,
        assets: Any = None,
    ) -> None:
        # 画图组件（render_assets.RenderAssets：中文字体 / 画图浏览器）。给 MaiWork 自己画图用、
        # 不走子 agent，所以本机执行没开也能装；网页上和办公文档并列（groups 里 kind="asset"）。
        self._assets = assets
        self._store = store
        self._get_settings = get_settings
        self._env = env
        self._capability = capability
        self._runner = runner
        self._timeout_s = int(timeout_s)
        self._probe_timeout_s = int(probe_timeout_s)
        self._cache: dict[str, Any] | None = None
        self._python_path = ""
        self._checking = False
        # 执行环境「第几代」：rebind 换环境时 +1。正在跑的探测 / 装卸带着出发时的号回来，
        # 号对不上就丢弃结果——绝不拿旧环境的结果覆盖新环境的状态。
        self._epoch = 0
        # 组 → "install" / "uninstall"（正在跑的活）；同一时间每组只跑一个
        self._running: dict[str, str] = {}
        self._tasks: set[asyncio.Task] = set()

    # ---------------- 小工具 ----------------

    def rebind(self, *, env: Any, capability: Any) -> None:
        """配置热更新后重接本机执行环境 / 执行能力判定（工作区根变了目录也跟着变）。

        探测结果只对**一个执行环境**有效（装在哪、谁能读，取决于工作区根 / 判定 / 本地模式 /
        运行账号）。所以重接时：`_cache` 清掉、正在跑的那次探测作废（epoch +1），
        下次 `_check()` 按新指纹重认 kv——指纹没变（同一目录同一判定）就照旧认回来，
        不白探一次（管理员每存一次设置都会走到这里）。
        """
        self._env = env
        self._capability = capability
        self._python_path = ""
        self._epoch += 1
        self._cache = None      # 下次 _check() 按新指纹重认（含 kv 那份）

    def _env_key(self) -> str:
        """这次探测结果属于哪个执行环境：装到哪 + 判定 + 本地模式 + 运行账号。

        指纹变了 = 子 agent 那边 import 得到什么可能整个变了，旧结果一个字都不能用；
        指纹没变 = 上次的探测依然成立（不因为「重接了一次对象」就重探）。

        **不看**「现在能不能跑命令」：那说的是能不能重新探一次，不是盘上的东西变了没有
        ——本机执行临时没就位（`env` 没接上 / 判定不可用）时，上次那份结果照旧有用。
        """
        senv = getattr(self._get_settings(), "environments", None)
        target = self.dir()
        cap = self._capability
        return "|".join((
            str(target) if target is not None else "",
            str(getattr(cap, "mode", "") or ""),
            str(getattr(senv, "local_mode", "") or ""),
            str(getattr(senv, "run_as", "") or ""),
        ))

    def _commit_check(self, info: Mapping[str, Any], *, epoch: int) -> dict[str, Any] | None:
        """把一次探测结果落进缓存 + kv；这次探测出发后环境换过（epoch 变了）就丢弃。

        丢弃时**什么都不写**：既不动内存缓存，也不动 kv——新环境的状态要等它自己的
        那次探测说了算（否则就是拿旧目录的结果冒充新目录）。
        """
        if int(epoch) != self._epoch:
            logger.debug("这次探测期间执行环境换了，结果丢弃（不写缓存 / 不写 kv）")
            return None
        rec = {**dict(info), "at": clock.now(), "env": self._env_key()}
        self._cache = rec
        self._write_kv(CHECK_KEY, rec)
        return rec

    def dir(self) -> Path | None:
        try:
            return pylibs_dir(self._get_settings())
        except Exception:
            logger.debug("读可选依赖目录出错", exc_info=True)
            return None

    def _kv_get(self, key: str, default: Any = None) -> Any:
        store = self._store
        if store is None:
            return default
        try:
            return store.kv_get(key, default)
        except Exception:
            logger.warning("读 kv %s 出错", key, exc_info=True)
            return default

    def _write_kv(self, key: str, value: Any) -> None:
        store = self._store
        if store is None:
            return
        try:
            with store.tx() as conn:
                store.kv_set(conn, key, value)
        except Exception:
            logger.warning("写 kv %s 出错", key, exc_info=True)

    def _check(self) -> dict[str, Any]:
        """上次探测结果（进程内缓存 → kv）。

        **换过执行环境的记录不认**：kv 里那条带着写它时的环境指纹，指纹跟现在对不上
        （工作区根 / 判定 / 本地模式 / 运行账号变过）就当没有——不然网页上会出现
        「新目录的路径 + 旧目录的探测结果」。插件重启后是同一个环境 → 照旧看得见上次结果。
        """
        if self._cache is not None:
            return self._cache
        rec = self._kv_get(CHECK_KEY, None)
        if isinstance(rec, dict) and str(rec.get("env") or "") == self._env_key():
            self._cache = rec
        else:
            self._cache = {}
        return self._cache

    def _last_install(self, group_id: str) -> dict[str, Any]:
        rec = self._kv_get(KV_PREFIX + str(group_id), None)
        return rec if isinstance(rec, dict) else {}

    def _run_note(self) -> str:
        """能不能在本机跑命令 / 探测（不能就装、卸都做不了）。"""
        if self._env is None:
            return "本机执行环境不可用（这台机器无法隔离运行命令），无法安装可选依赖"
        cap = self._capability
        if cap is not None and not bool(getattr(cap, "ok", True)):
            why = str(getattr(cap, "reason", "") or "本机执行未启用")
            return f"本机执行未启用（{why}），无法安装可选依赖"
        return ""

    def _unavailable_note(self) -> str:
        """**装**这件事现在能不能做（页面上的 `available` / `note` 就是这个）。

        dynamic 模式（系统里没有固定运行账号、改用 systemd 临时用户）另外挡一道：
        那种模式下 `/var/lib/private/` 在单元的命名空间里只挂了它自己的工作区
        （docs/06 实测：群 A 的沙箱看不到群 B 的工作区），所以装在
        `<工作区根>/../pylibs` 的库子 agent 根本看不到——装了也 import 不到，
        会一直「装完→import 不到→失败」。宁可这里说清，也不让管理员反复白装。
        （卸载不受这条限制：能跑命令就能删、能重新探测。）
        """
        note = self._run_note()
        if note:
            return note
        cap = self._capability
        if str(getattr(cap, "mode", "") or "") == "dynamic":
            return (
                "当前运行模式使用系统临时账号隔离，没有固定运行账号："
                "子 agent 读不到 MaiWork 自己的库目录，可选组件装了也无法生效，"
                "因此暂不提供安装。需要办公文档时，请先建立专用运行账号"
                "（例如 maiwork），在「设置 → 全部设置 → 运行环境 → 运行账号」填好后返回安装。"
            )
        return ""

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait(self) -> None:
        """等后台的装 / 卸跑完（接口和测试用）。"""
        if self._assets is not None:
            await self._assets.wait()
        for _ in range(100):
            pending = [t for t in self._tasks if not t.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    # ---------------- 探测 ----------------

    async def _probe(self) -> dict[str, Any]:
        env = self._env
        if env is None:
            return {
                "python": "", "version": "", "packages": {},
                "soffice": {"found": False, "path": ""}, "error": self._unavailable_note(),
            }
        res = await env.run(_PROBE_WORKSPACE, probe_command(), timeout_s=self._probe_timeout_s)
        return _parse_probe(
            str(getattr(res, "stdout", "") or ""),
            exit_code=int(getattr(res, "exit_code", 0) or 0),
        )

    async def refresh(self) -> dict[str, Any]:
        """现在探测一次并落 kv，返回最新状态。"""
        if self._assets is not None:
            try:
                await self._assets.refresh()
            except Exception:
                logger.warning("探测画图组件出错", exc_info=True)
        if self._env is None:
            self._cache = {}
            return self.status()
        self._checking = True
        epoch = self._epoch
        try:
            try:
                info = await self._probe()
            except Exception as exc:
                logger.warning("探测可选依赖出错", exc_info=True)
                info = {
                    "python": "", "version": "", "packages": {},
                    "soffice": {"found": False, "path": ""},
                    "error": f"探测失败：{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS],
                }
            self._commit_check(info, epoch=epoch)
        finally:
            self._checking = False
        return self.status()

    def checked(self) -> bool:
        office = float(self._check().get("at") or 0.0) > 0
        if self._assets is None:
            return office
        # 本机执行没开时办公文档那组探测不了（永远「没探测过」），别因此每次 GET 都重起一次浏览器探测
        return (office or self._env is None) and bool(self._assets.checked())

    def maybe_refresh(self, *, max_age: float = CHECK_TTL_S) -> bool:
        """缓存过期就后台刷一次（不阻塞请求）；没事件循环 / 不可用时什么都不做。"""
        if self._assets is not None:
            try:
                self._assets.maybe_refresh(max_age=max_age)
            except Exception:
                logger.debug("后台刷新画图组件出错", exc_info=True)
        if self._env is None or self._checking:
            return False
        at = float(self._check().get("at") or 0.0)
        if at > 0 and clock.now() - at < max_age:
            return False
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._spawn(self.refresh())
        return True

    async def _python(self) -> str:
        """子 agent 命令实际用的那个 python3 的绝对路径（探测得来）。

        `_check()` 已经把「换了执行环境」的旧记录挡掉了，所以这里拿到的 python 一定是
        现在这个环境里探测出来的。真要新探一次时也带 epoch：探测期间环境换了（rebind）
        就**重探一次当前环境**，两趟都不成（配置一直在动）才回 `Unavailable`——
        绝不把旧环境的解释器路径缓存下来给新环境用（那会让 pip 装到另一个解释器的
        ABI 目录里去）。
        """
        if self._python_path:
            return self._python_path
        found = str(self._check().get("python") or "")
        if found:
            self._python_path = found
            return found
        for _ in range(2):
            epoch = self._epoch
            info = await self._probe()
            if self._commit_check(info, epoch=epoch) is None:
                continue            # 探测期间环境换了：这次结果丢掉，按当前环境重探
            self._python_path = str(info.get("python") or "")
            return self._python_path
        raise Unavailable(
            "执行环境在探测期间发生变化（工作区或判定刚修改过），本次未安装；配置稳定后请重新操作"
        )

    # ---------------- 状态 ----------------

    def status(self) -> dict[str, Any]:
        """给网页 / 管理员工具看的只读快照（同步，只读缓存，不探测）。"""
        check = self._check()
        at = float(check.get("at") or 0.0)
        note = self._unavailable_note()
        target = self.dir()
        groups: list[dict[str, Any]] = []
        for gid, group in GROUPS.items():
            pkgs: list[dict[str, Any]] = []
            installed_n = 0
            for pkg in group.packages:
                got = (check.get("packages") or {}).get(pkg.name) if at > 0 else None
                installed = bool(got.get("installed")) if isinstance(got, dict) else None
                if installed:
                    installed_n += 1
                pkgs.append({
                    "name": pkg.name,
                    "module": pkg.module,
                    "min_version": pkg.min_version,
                    "note": pkg.note,
                    "spec": f"{pkg.name}>={pkg.min_version}",
                    "installed": installed,
                    "version": str((got or {}).get("version") or "") if isinstance(got, dict) else "",
                    "error": str((got or {}).get("error") or "") if isinstance(got, dict) else "",
                })
            running = self._running.get(gid, "")
            if running == "install":
                state = "installing"
            elif running == "uninstall":
                state = "uninstalling"
            elif at <= 0:
                state = "unknown"
            elif installed_n >= len(group.packages):
                state = "ok"
            elif installed_n:
                state = "partial"
            else:
                state = "missing"
            tools: list[dict[str, Any]] = []
            for tool in group.tools:
                spec = _TOOL_CHECKS.get(tool) or {}
                found = bool((check.get("soffice") or {}).get("found")) if tool == "soffice" else False
                path = str((check.get("soffice") or {}).get("path") or "") if tool == "soffice" else ""
                tools.append({
                    "name": tool,
                    "found": found,
                    "path": path,
                    "note": str(spec.get("note") or ""),
                    "installable": False,   # 只检测不装
                })
            last = self._last_install(gid)
            last_state = str(last.get("state") or "")
            last_error = str(last.get("error") or "")
            if last_state in ("installing", "uninstalling") and not running:
                # 装 / 卸跑到一半插件重启或热重载了：那个进程没了，这次活不会再有结果。
                # 不假装还在装（否则网页上永远转圈），如实说「中断了，再点一次」。
                # 文案保持通用（2026-10-15 巡检）：这条 state 装和卸都可能落到，
                # 别写成「装到一半」——管理员可能是点卸载时被打断的。
                last_state = "interrupted"
                last_error = "上次操作因插件重新加载而中断，请重新操作"
            groups.append({
                "id": gid,
                "kind": "pip",
                "available": not note,
                "note": note,
                "label": group.label,
                "purpose": group.purpose,
                "state": state,
                "installing": running == "install",
                "uninstalling": running == "uninstall",
                "installed_count": installed_n,
                "total": len(group.packages),
                "packages": pkgs,
                "tools": tools,
                "last_install": {
                    "state": last_state,
                    "at": float(last.get("at") or 0.0),
                    "by": str(last.get("by") or ""),
                    "error": last_error,
                    "log": str(last.get("log") or ""),
                },
                "error": last_error if last_state in ("failed", "interrupted") else "",
            })
        if self._assets is not None:
            try:
                groups.extend(self._assets.status_groups())
            except Exception:
                logger.warning("读画图组件状态出错", exc_info=True)
        return {
            "available": not note,
            "note": note,
            "dir": str(target) if target is not None else "",
            "python": str(check.get("python") or ""),
            "python_version": str(check.get("version") or ""),
            "checked_at": at,
            "checking": bool(self._checking) or bool(self._assets is not None and self._assets.busy()),
            "check_error": str(check.get("error") or ""),
            "groups": groups,
        }

    # ---------------- 装 / 卸 ----------------

    def start_install(self, group_id: str, *, by: str = "网页管理员") -> dict[str, Any]:
        """后台装这一组，立刻返回状态。已在装 → Busy；本机执行没就位 → Unavailable。"""
        if self._assets is not None and str(group_id) in self._assets.group_ids:
            self._assets.start_install(str(group_id), by=str(by))
            return self.status()
        group = _group(group_id)
        note = self._unavailable_note()
        if note:
            raise Unavailable(note)
        if group.id in self._running:
            raise Busy(f"「{group.label}」正在装，等它跑完")
        specs = package_specs(group.id)   # 白名单（不接受任意包名）
        self._running[group.id] = "install"
        self._write_kv(KV_PREFIX + group.id, {
            "state": "installing", "at": clock.now(), "by": str(by),
            "error": "", "log": "", "packages": [p.name for p in group.packages],
        })
        self._spawn(self._install(group, specs, by=str(by)))
        return self.status()

    def start_uninstall(self, group_id: str, *, by: str = "网页管理员") -> dict[str, Any]:
        """后台卸载这一组（删目录里本组自己的文件），立刻返回状态。

        本机执行没就位时和装一样当场拒（`Unavailable`）：卸完要重新探测一次才知道现在的
        状态，没有执行环境就没法探——以前会拿「没环境」的错误桩写进 kv，把上次那份好
        探测结果覆盖掉（网页上变成「三库全没了」）。
        走的是 `_run_note()`（只看能不能跑命令），**不**吃 dynamic 模式那条安装限制：
        那种机器上照样要能把这组文件删掉。
        """
        if self._assets is not None and str(group_id) in self._assets.group_ids:
            self._assets.start_uninstall(str(group_id), by=str(by))
            return self.status()
        group = _group(group_id)
        note = self._run_note()
        if note:
            raise Unavailable(note)
        if group.id in self._running:
            raise Busy(f"「{group.label}」正在装 / 卸，等它跑完")
        self._running[group.id] = "uninstall"
        self._write_kv(KV_PREFIX + group.id, {
            "state": "uninstalling", "at": clock.now(), "by": str(by),
            "error": "", "log": "", "packages": [p.name for p in group.packages],
        })
        self._spawn(self._uninstall(group, by=str(by)))
        return self.status()

    async def _pip(self, python: str, target: Path, specs: Sequence[str]) -> tuple[int, str, str]:
        runner = self._runner or _default_pip_runner
        argv = pip_argv(python, target, specs)
        env = pip_env(target)
        code, out, err = await runner(argv, env=env, timeout=self._timeout_s)
        code, out, err = int(code), _decode(out), _decode(err)
        if code != 0 and "externally-managed" in f"{out}\n{err}":
            # PEP 668：系统 python 不让装。我们装的是自己的目录，不是系统环境，
            # 带上这个开关再来一次（pip ≥ 23 才有；老 pip 第一次就已经报别的错了）。
            retry = [*argv[:4], "--break-system-packages", *argv[4:]]
            code2, out2, err2 = await runner(retry, env=env, timeout=self._timeout_s)
            code, out, err = int(code2), out + _decode(out2), err + _decode(err2)
        return code, out, err

    async def _install(self, group: Group, specs: Sequence[str], *, by: str) -> None:
        rec = _Install(state="failed", by=by, packages=[p.name for p in group.packages])
        epoch = self._epoch        # 安装过程中环境变了（工作区根改了）→ 本次结果无效
        try:
            target = self.dir()
            if target is None:
                raise Unavailable("找不到安装目录（未读到配置中的工作区根目录）")
            target.mkdir(parents=True, exist_ok=True)
            python = await self._python()
            if not python:
                raise Unavailable("本机 PATH 中没有 python3，无法安装（请先安装 python3）")
            code, out, err = await self._pip(python, target, specs)
            log = _tail_text(out + "\n" + err)
            rec.log = log
            if code != 0:
                rec.state = "failed"
                rec.error = _error_summary(err or out)
            else:
                _relax_perms(target)
                # 安装程序说成功不算数：真在那个 python 下 import 到才算装好
                info = await self._probe()
                fresh = self._commit_check(info, epoch=epoch)
                if fresh is None:
                    rec.state = "failed"
                    rec.error = (
                        "安装过程中执行环境或工作区目录发生变化，本次结果无效；"
                        "配置稳定后请重新安装"
                    )[:MAX_ERROR_CHARS]
                else:
                    missing = [p.name for p in group.packages if not _pkg_installed(fresh, p.name)]
                    if missing:
                        rec.state = "failed"
                        rec.error = (
                            "已安装，但子 agent 无法导入：" + "、".join(missing)
                            + "（可能读不到安装目录，请检查运行账号的读权限）"
                        )[:MAX_ERROR_CHARS]
                    else:
                        rec.state = "ok"
                        rec.error = ""
        except asyncio.TimeoutError:
            rec.state = "failed"
            rec.error = f"安装依赖超时（超过 {self._timeout_s} 秒）"
        except Exception as exc:
            logger.warning("装可选依赖「%s」出错", group.id, exc_info=True)
            rec.state = "failed"
            rec.error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
        finally:
            rec.at = clock.now()
            self._running.pop(group.id, None)
            self._write_kv(KV_PREFIX + group.id, {
                "state": rec.state, "at": rec.at, "by": rec.by,
                "error": rec.error, "log": rec.log, "packages": rec.packages,
            })

    async def _uninstall(self, group: Group, *, by: str) -> None:
        rec = _Install(state="failed", by=by, packages=[p.name for p in group.packages])
        epoch = self._epoch
        try:
            target = self.dir()
            removed = _remove_group_files(target, group) if target is not None else 0
            left = _group_leftovers(target, group) if target is not None else []
            rec.log = f"已删除 {removed} 个文件或目录"
            if left:
                rec.log += f"；仍存在：{'、'.join(left)}"
            info = await self._probe()
            fresh = self._commit_check(info, epoch=epoch)
            # 卸载的结论必须来自「真删干净了 + 真重新探测过」：
            # - 环境在卸的过程中换了 → 这次不作数；
            # - 探测没跑起来 / 输出看不懂 → 现在的状态没法确认；
            # - 白名单条目还留在目录里 → 就是没卸干净（以前 rmtree 吞错还记 ok）。
            if fresh is None:
                rec.state = "failed"
                rec.error = (
                    "卸载过程中执行环境或工作区目录发生变化，本次结果无法确认；"
                    "配置稳定后请重新操作"
                )[:MAX_ERROR_CHARS]
            elif str(fresh.get("error") or ""):
                rec.state = "failed"
                rec.error = (
                    f"文件已删除，但卸载后无法确认状态（{fresh.get('error')}）"
                )[:MAX_ERROR_CHARS]
            elif left:
                rec.state = "failed"
                rec.error = (
                    "部分文件未删除：" + "、".join(left)
                    + "（通常是权限问题，请查看服务端日志）"
                )[:MAX_ERROR_CHARS]
            else:
                rec.state = "ok"
                rec.error = ""
        except Exception as exc:
            logger.warning("卸载可选依赖「%s」出错", group.id, exc_info=True)
            rec.state = "failed"
            rec.error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
        finally:
            rec.at = clock.now()
            self._running.pop(group.id, None)
            self._write_kv(KV_PREFIX + group.id, {
                "state": rec.state, "at": rec.at, "by": rec.by,
                "error": rec.error, "log": rec.log, "packages": rec.packages,
            })
