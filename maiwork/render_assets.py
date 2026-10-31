"""画图组件：中文字体 + 画图浏览器（Chromium）一键装（2026-10-25）。

## 为什么要有

Docker 版 MaiBot 的精简镜像常常**没有中文字体、没有 Chromium**：资讯卡片（news_card.py，
用 MaiBot 环境里的 playwright + Chromium 截图）会出方块字，或者浏览器起不来直接改发文字。
MaiWork 是插件，不能改镜像、不能 apt 装系统包（可能也不是 root），所以把这两样**下载到
MaiWork 自己的目录**（和 `pylibs` 同级，即 `<workspace_root>/..`）：

- `fonts/`：一款开源中文字体（思源黑体 / Noto Sans SC Regular，SIL OFL 1.1）+ 一份
  `fonts.conf`（Linux 上让 Chromium 的 fontconfig 也能找到它，图解页也不出方块字）；
- `ms-playwright/`：`<python> -m playwright install chromium` 下载的浏览器
  （`PLAYWRIGHT_BROWSERS_PATH` 指到这里）；
- `hostlibs/`：MaiBot 环境 import 不到 playwright 时，先 `pip install --target` 装到这里
  （**不放进 pylibs**：pylibs 是给子 agent 的系统 python3 用的，playwright 依赖的 greenlet
  是编译过的扩展，跟 MaiBot 的 python 版本绑定，不该混进去）。

和 `optional_deps.py` 的区别：那边装的东西给**子 agent**用（走本机执行环境探测）；
这里装的东西给 **MaiWork 自己（宿主进程）画图**用，探测直接在宿主进程 / 宿主 python 上做，
所以「本机执行没开」也能装。网页上照样放在「设置 → 工具 → 可选组件」，接口形状同一个
（`OptionalDeps.status()["groups"]` 里多两组，`kind="asset"`）。

## 安全取法

- 字体下载地址**写死在代码里**并**钉 sha256**；下载后校验，不对就删掉报错；主地址失败用备用镜像。
- httpx：`trust_env=False`（不继承代理环境变量）、只认 https、不跟跳转、总超时、大小上限。
- 子进程（pip / playwright install / 启动探测）只给最小环境（不继承插件进程的密钥）、
  超时、输出截断；**绝不**跑 `install-deps` / `--with-deps`（那要 root 装系统包）。
- 浏览器下载好了但系统缺库：MaiWork 修不了，网页上用大白话说清楚。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import clock

logger = logging.getLogger("maiwork.render_assets")

__all__ = [
    "FONT", "FontAsset", "FONT_FAMILY", "FONT_URL", "AssetError", "RenderAssets", "GROUP_IDS",
    "assets_root", "download_font", "font_state", "system_cjk_fonts", "chromium_install_argv",
    "chromium_env", "parse_missing_libs", "explain_launch_error", "find_chromium",
    "local_font_path", "local_chromium", "fontconfig_file", "set_root_getter", "PROBE_MARK",
]

KV_PREFIX = "optional_deps."
CHECK_KEY = KV_PREFIX + "assets_check"
GROUP_IDS: tuple[str, ...] = ("fonts", "browser")
FONTS_DIRNAME = "fonts"
BROWSERS_DIRNAME = "ms-playwright"
HOSTLIBS_DIRNAME = "hostlibs"
FONTCONF_NAME = "fonts.conf"
DOWNLOAD_TIMEOUT_S = 180.0
INSTALL_TIMEOUT_S = 900          # 浏览器约 150MB，国内网络慢，给 15 分钟
PIP_TIMEOUT_S = 300
PROBE_TIMEOUT_S = 90
CHECK_TTL_S = 300.0
MAX_ERROR_CHARS = 300
MAX_LOG_CHARS = 4000
PROBE_MARK = "MAIWORK_RENDER_JSON="

# 卡片 HTML 里字体的「虚拟地址」：news_card 的路由把它拦下来，直接回本地字体文件
# （卡片页不许联网、file:// 在 set_content 的页面里也加载不了）。.invalid 保证永远解析不到。
FONT_FAMILY = "MaiWork CJK"
FONT_URL = "https://maiwork-font.invalid/NotoSansSC-Regular.otf"


@dataclass(frozen=True)
class FontAsset:
    label: str
    filename: str
    size: int
    sha256: str
    urls: tuple[str, ...]
    license: str


# 2026-10-25 联网核对：两个地址内容一致（8,331,336 字节，sha256 如下）。
# 钉在 noto-cjk 的发布标签 Sans2.004 上（不跟 main 分支走，内容不会悄悄变）。
FONT = FontAsset(
    label="思源黑体 Noto Sans SC Regular",
    filename="NotoSansSC-Regular.otf",
    size=8331336,
    sha256="faa6c9df652116dde789d351359f3d7e5d2285a2b2a1f04a2d7244df706d5ea9",
    urls=(
        "https://raw.githubusercontent.com/notofonts/noto-cjk/Sans2.004/Sans/SubsetOTF/SC/NotoSansSC-Regular.otf",
        "https://cdn.jsdelivr.net/gh/notofonts/noto-cjk@Sans2.004/Sans/SubsetOTF/SC/NotoSansSC-Regular.otf",
    ),
    license="SIL Open Font License 1.1（OFL，可免费商用、可随软件分发）",
)

_MIN_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"


class AssetError(RuntimeError):
    """下载 / 安装失败（消息是给管理员看的中文）。"""


# ----------------------------------------------------------------------
# 目录
# ----------------------------------------------------------------------


def assets_root(settings: Any) -> Path | None:
    """MaiWork 自己的目录：`<workspace_root>/..`（和 pylibs 同级）；读不到配置返回 None。"""
    env = getattr(settings, "environments", None)
    root = getattr(env, "workspace_root", None) if env is not None else None
    text = str(root or "").strip()
    return Path(text).parent if text else None


_ROOT_GETTER: Callable[[], Path | None] | None = None


def set_root_getter(fn: Callable[[], Path | None] | None) -> None:
    """news_card 画图时从这里找字体 / 浏览器（RenderAssets 建起来时登记）。"""
    global _ROOT_GETTER
    _ROOT_GETTER = fn


def _root() -> Path | None:
    fn = _ROOT_GETTER
    if fn is None:
        return None
    try:
        return fn()
    except Exception:
        return None


def _font_file(fonts_dir: Path, font: FontAsset = FONT) -> Path | None:
    path = Path(fonts_dir) / font.filename
    try:
        if path.is_file() and path.stat().st_size == font.size:
            return path
    except OSError:
        pass
    return None


def local_font_path() -> Path | None:
    """本机已装好的那款字体文件（大小对得上才算）；没有 → None。"""
    root = _root()
    return _font_file(root / FONTS_DIRNAME) if root is not None else None


def fontconfig_file() -> Path | None:
    root = _root()
    if root is None:
        return None
    path = root / FONTS_DIRNAME / FONTCONF_NAME
    return path if path.is_file() and local_font_path() is not None else None


# 各平台 Playwright 浏览器目录里可执行文件的位置（无头版优先：更小、更省内存）
_EXE_PATTERNS = (
    "chromium_headless_shell-*/chrome-linux/headless_shell",
    "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
    "chromium-*/chrome-linux/chrome",
    "chromium-*/chrome-linux64/chrome",
    "chromium_headless_shell-*/chrome-mac*/headless_shell",
    "chromium_headless_shell-*/chrome-headless-shell-mac*/chrome-headless-shell",
    "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
    # Windows：新布局（chrome-headless-shell-win64）与旧布局（chrome-win）都认
    "chromium_headless_shell-*/chrome-headless-shell-win*/chrome-headless-shell.exe",
    "chromium_headless_shell-*/chrome-win*/headless_shell.exe",
    "chromium-*/chrome-win*/chrome.exe",
)


def _rev(path: Path) -> int:
    for part in path.parts:
        m = re.match(r"chromium(?:_headless_shell)?-(\d+)$", part)
        if m:
            return int(m.group(1))
    return 0


_OS_NAME = os.name  # 测试可注入；改 os.name 会让 Path() 变 WindowsPath 当场炸


def _is_exe(path: Path) -> bool:
    """可执行文件判定：Windows 的 .exe 不需要 POSIX 执行位（文件系统根本没有），
    只要求是个文件；POSIX 仍要求 os.X_OK——没执行位的脚本不该被当成浏览器。"""
    if _OS_NAME == "nt":
        return path.is_file()
    return path.is_file() and os.access(path, os.X_OK)


def find_chromium(browsers_dir: Path) -> Path | None:
    """`ms-playwright` 目录里下载好的 Chromium 可执行文件（版本号最大的那个）。"""
    base = Path(browsers_dir)
    if not base.is_dir():
        return None
    for pattern in _EXE_PATTERNS:
        hits = [p for p in base.glob(pattern) if _is_exe(p)]
        if hits:
            return max(hits, key=_rev)
    return None


def local_chromium() -> Path | None:
    root = _root()
    return find_chromium(root / BROWSERS_DIRNAME) if root is not None else None


def hostlibs_path() -> Path | None:
    root = _root()
    if root is None:
        return None
    path = root / HOSTLIBS_DIRNAME
    return path if path.is_dir() else None


def ensure_playwright_importable() -> bool:
    """MaiBot 环境里没有 playwright、但我们装过一份到 hostlibs → 加到 sys.path **末尾**
    （优先级最低，不顶掉宿主自己的任何包）。返回是否加了。"""
    path = hostlibs_path()
    if path is None or not (path / "playwright").is_dir():
        return False
    text = str(path)
    if text not in sys.path:
        sys.path.append(text)
    return True


# ----------------------------------------------------------------------
# 字体
# ----------------------------------------------------------------------


def _fontconf_text(fonts_dir: Path) -> str:
    d = str(Path(fonts_dir))
    return (
        '<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n<fontconfig>\n'
        '  <include ignore_missing="yes">/etc/fonts/fonts.conf</include>\n'
        f"  <dir>{d}</dir>\n  <cachedir>{d}/.fccache</cachedir>\n</fontconfig>\n"
    )


async def download_font(
    dest_dir: Path,
    *,
    font: FontAsset = FONT,
    transport: Any = None,
    timeout_s: float = DOWNLOAD_TIMEOUT_S,
) -> Path:
    """按顺序试每个地址，下载到 `dest_dir/<文件名>`；sha256 / 大小不对就删掉换下一个，全失败抛 AssetError。"""
    import httpx

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    final = dest / font.filename
    part = dest / (font.filename + ".part")
    cap = int(font.size) + 1024
    errors: list[str] = []
    for url in font.urls:
        if not url.startswith("https://"):
            errors.append(f"{url}：只允许 https")
            continue
        try:
            digest = hashlib.sha256()
            total = 0
            async with asyncio.timeout(timeout_s):
                async with httpx.AsyncClient(
                    transport=transport, trust_env=False, timeout=30.0, follow_redirects=False,
                    headers={"User-Agent": "MaiWork/1.0 (+font-download)"},
                ) as client:
                    async with client.stream("GET", url) as resp:
                        if resp.status_code != 200:
                            raise AssetError(f"HTTP {resp.status_code}")
                        with open(part, "wb") as fh:
                            async for chunk in resp.aiter_bytes():
                                total += len(chunk)
                                if total > cap:
                                    raise AssetError("文件比预期大，已停止下载")
                                digest.update(chunk)
                                fh.write(chunk)
            if digest.hexdigest() != font.sha256 or total != font.size:
                raise AssetError("文件校验不通过（sha256 不一致，可能被篡改或下载不完整），已删除")
            os.replace(part, final)
            os.chmod(final, 0o644)
            return final
        except Exception as exc:  # noqa: BLE001  换下一个地址
            why = str(exc) if isinstance(exc, AssetError) else f"{type(exc).__name__}: {exc}"
            errors.append(f"{url.split('/')[2]}：{why}"[:160])
            logger.info("字体下载失败（%s）：%s", url, why)
        finally:
            try:
                part.unlink()
            except OSError:
                pass
    raise AssetError("字体下载失败——" + "；".join(errors))


_CJK_NAME = re.compile(
    r"(?i)(cjk|source ?han|sourcehan|noto ?s[ae]n?s? ?(sc|tc|hk|jp|kr)\b|notosans(sc|tc|hk|jp|kr)|"
    r"notoserif(sc|tc|jp|kr)|wqy|wenquanyi|droid ?sans ?fallback|droidsansfallback|simhei|simsun|"
    r"msyh|yahei|pingfang|hiragino ?sans ?gb|heiti|songti|ukai|uming|arphic|harmonyos ?sans ?sc|"
    r"思源|黑体|宋体)"
)
_FONT_SUFFIX = (".ttf", ".otf", ".ttc", ".otc")


def _system_font_dirs(os_name: str, *, env: Mapping[str, str], home: Path) -> list[Path]:
    r"""按平台给「系统字体目录」清单（**惰性**：导入时不展开 `Path.home()` / 环境变量）。

    - POSIX（Linux）：fontconfig 常见目录 + 用户字体目录；
    - macOS：`/System/Library/Fonts`（STHeiti / Songti / Hiragino 都在这）、
      Supplemental、`/Library/Fonts`、用户 `~/Library/Fonts`；
    - Windows：`%SystemRoot%\Fonts`、`%LOCALAPPDATA%\Microsoft\Windows\Fonts`
      （用户级字体装在后者）；环境变量读不到就跳过，不编造路径。
    """
    if os_name == "nt":
        out: list[Path] = []
        root = str(env.get("SystemRoot") or env.get("WINDIR") or "").strip()
        if root:
            out.append(Path(root) / "Fonts")
        local = str(env.get("LOCALAPPDATA") or "").strip()
        if local:
            out.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
        return out
    if os_name == "macos":
        return [
            Path("/System/Library/Fonts"),
            Path("/System/Library/Fonts/Supplemental"),
            Path("/Library/Fonts"),
            home / "Library" / "Fonts",
        ]
    return [
        Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
        home / ".fonts", home / ".local" / "share" / "fonts",
    ]


def _fc_list(argv: Sequence[str]) -> str:
    from .optional_deps import windows_system_path

    res = subprocess.run(
        list(argv), capture_output=True, text=True, timeout=10,
        env={"PATH": windows_system_path() or _MIN_PATH, "LANG": "C.UTF-8"},
    )
    return res.stdout if res.returncode == 0 else ""


def system_cjk_fonts(
    *,
    fc_runner: Callable[[Sequence[str]], str] | None = None,
    dirs: Iterable[Path] | None = None,
    limit: int = 5,
) -> list[str]:
    """系统里已有的中文字体（`fc-list :lang=zh family`，没有 fc-list 就扫常见字体目录）。

    探测不到就当没有（返回 []），从不抛。
    """
    found: list[str] = []
    try:
        out = (fc_runner or _fc_list)(["fc-list", ":lang=zh", "family"])
    except Exception:
        out = ""
    for line in str(out or "").splitlines():
        name = line.split(",", 1)[0].strip()
        if name and name not in found:
            found.append(name)
    if found:
        return found[:limit]
    if dirs is None:
        os_name = "nt" if _OS_NAME == "nt" else ("macos" if sys.platform == "darwin" else "posix")
        dirs = _system_font_dirs(os_name, env=os.environ, home=Path.home())
    seen = 0
    for base in dirs:
        try:
            if not Path(base).is_dir():
                continue
            for dirpath, _dirs, files in os.walk(base):
                for fn in files:
                    seen += 1
                    if seen > 20000:
                        return found[:limit]
                    if fn.lower().endswith(_FONT_SUFFIX) and _CJK_NAME.search(fn) and fn not in found:
                        found.append(fn)
                        if len(found) >= limit:
                            return found
        except Exception:
            continue
    return found[:limit]


def font_state(fonts_dir: Path | None, *, system: Sequence[str], font: FontAsset = FONT) -> dict[str, Any]:
    """三态：ok（MaiWork 自己装了）/ system（系统已有中文字体）/ missing。"""
    path = _font_file(fonts_dir, font) if fonts_dir is not None else None
    if path is not None:
        return {"state": "ok", "path": str(path), "system": list(system)}
    if system:
        return {"state": "system", "path": "", "system": list(system)}
    return {"state": "missing", "path": "", "system": []}


# ----------------------------------------------------------------------
# Chromium
# ----------------------------------------------------------------------


def chromium_install_argv(python: str) -> list[str]:
    """下载 Chromium：**不带** `--with-deps`（那会去 apt 装系统包、要 root）。"""
    return [str(python), "-m", "playwright", "install", "chromium"]


def chromium_env(browsers_dir: Path, *, hostlibs: Path | None) -> dict[str, str]:
    """playwright 子进程的最小环境：浏览器装到 / 从 MaiWork 自己的目录找；不继承插件进程的环境。

    Windows 另补 SystemRoot / windir：少了这两项，子进程里的 `import playwright`
    会因为 Winsock 初始化失败报 WinError 10106，被误判成「没装 playwright」。
    Windows 的 PATH 改用 `windows_system_path()`：`/usr/local/bin:...` 在那边是无效路径。
    """
    from .optional_deps import windows_system_env, windows_system_path

    env = {
        "PATH": windows_system_path() or _MIN_PATH,
        "HOME": str(Path(browsers_dir).parent),
        "LANG": "C.UTF-8",
        "PLAYWRIGHT_BROWSERS_PATH": str(browsers_dir),
        "PYTHONNOUSERSITE": "1",
        **windows_system_env(),
    }
    if hostlibs is not None:
        env["PYTHONPATH"] = str(hostlibs)
    return env


_SO_RE = re.compile(r"\blib[A-Za-z0-9_+\-.]*?\.so(?:\.\d+)*")
_MISSING_MARKS = ("Host system is missing dependencies", "error while loading shared libraries")


def parse_missing_libs(text: str, *, limit: int = 8) -> list[str]:
    """从 Playwright / 动态链接器的报错里挑出缺的 .so 名字（只在确实是缺库报错时）。"""
    body = str(text or "")
    if not any(m in body for m in _MISSING_MARKS):
        return []
    out: list[str] = []
    for m in _SO_RE.finditer(body):
        name = m.group(0)
        if name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return out


def explain_launch_error(text: str) -> str:
    """启动失败 → 给管理员看的大白话。"""
    libs = parse_missing_libs(text)
    if libs or any(m in str(text or "") for m in _MISSING_MARKS):
        shown = "、".join(libs[:5]) + (" 等" if len(libs) > 5 else "")
        return (
            "浏览器下载好了，但这个系统缺少它需要的系统库"
            + (f"（{shown}）" if shown else "")
            + "，MaiWork 没法自己装。需要在 MaiBot 容器里用 root 运行 "
            "`playwright install-deps chromium`，或换带浏览器的镜像；在那之前资讯卡片会改发文字。"
        )
    tail = " ".join(str(text or "").split())[-200:]
    return f"浏览器启动失败：{tail or '原因未知'}；在那之前资讯卡片会改发文字。"


# 启动探测：import playwright → 真起一次无头 Chromium、打开 about:blank、关掉。
# 整段以 `-c` 交给宿主 python；结果一行 JSON。
_PROBE_SCRIPT = (
    "import json,os,sys\n"
    "out={}\n"
    "try:\n"
    "    from playwright.sync_api import sync_playwright\n"
    "    out['playwright']=True\n"
    "except Exception as e:\n"
    "    out['playwright']=False\n"
    "    out['error']=type(e).__name__+': '+str(e)[:300]\n"
    "if out['playwright'] and os.environ.get('MW_LAUNCH')=='1':\n"
    "    try:\n"
    "        with sync_playwright() as p:\n"
    "            kw={'headless':True,'args':['--no-sandbox','--disable-dev-shm-usage']}\n"
    "            if os.environ.get('MW_EXE'): kw['executable_path']=os.environ['MW_EXE']\n"
    "            b=p.chromium.launch(**kw)\n"
    "            pg=b.new_page(); pg.goto('about:blank'); b.close()\n"
    "        out['launch']='ok'\n"
    "    except Exception as e:\n"
    "        out['launch']='failed'\n"
    "        out['launch_error']=str(e)[-3000:]\n"
    "print('" + PROBE_MARK + "'+json.dumps(out))\n"
)


def _decode(data: Any) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data or "")


def _tail(text: str, limit: int = MAX_LOG_CHARS) -> str:
    body = str(text or "").strip()
    return body if len(body) <= limit else body[-limit:]


def _parse_probe(stdout: str) -> dict[str, Any]:
    for raw in reversed(str(stdout or "").splitlines()):
        line = raw.strip()
        if line.startswith(PROBE_MARK):
            try:
                data = json.loads(line[len(PROBE_MARK):])
            except ValueError:
                break
            if isinstance(data, dict):
                return data
    return {"playwright": False, "error": "探测输出无法解析"}


async def _default_runner(argv: Sequence[str], *, env: Mapping[str, str], timeout: float) -> tuple[int, str, str]:
    from .optional_deps import _default_pip_runner

    return await _default_pip_runner(argv, env=env, timeout=timeout)


# ----------------------------------------------------------------------
# 主体
# ----------------------------------------------------------------------


_LABELS = {
    "fonts": "中文字体（资讯卡片用）",
    "browser": "画图浏览器（Chromium，资讯卡片用）",
}
_PURPOSES = {
    "fonts": (
        f"资讯卡片里的中文要有中文字体才不会变成方块。下载{FONT.label}（约 8 MB，{FONT.license}）"
        "到 MaiWork 自己的目录，不改动系统。"
    ),
    "browser": (
        "资讯卡片是用无头浏览器截图画出来的。MaiBot 环境里没有浏览器时，可以在这里下载一份 Chromium"
        "（约 150 MB）到 MaiWork 自己的目录；不装的话资讯卡片会改发文字。"
    ),
}


class RenderAssets:
    """字体 / 浏览器的装、卸、探测；状态落 kv（和 optional_deps 同一前缀，网页同一处展示）。"""

    group_ids = GROUP_IDS

    def __init__(
        self,
        store: Any,
        get_settings: Callable[[], Any],
        *,
        runner: Any = None,
        transport: Any = None,
        python: str | None = None,
        font: FontAsset = FONT,
        system_fonts: Callable[[], list[str]] | None = None,
        register: bool = True,
    ) -> None:
        self._store = store
        self._get_settings = get_settings
        self._runner = runner or _default_runner
        self._transport = transport
        self._python = str(python or sys.executable)
        self._font = font
        self._system_fonts = system_fonts or system_cjk_fonts
        self._cache: dict[str, Any] | None = None
        self._checking = False
        self._running: dict[str, str] = {}
        self._tasks: set[asyncio.Task] = set()
        if register:
            set_root_getter(self.root)

    # ---------------- 目录 / kv ----------------

    def root(self) -> Path | None:
        try:
            return assets_root(self._get_settings())
        except Exception:
            return None

    def _dirs(self) -> tuple[Path, Path, Path]:
        root = self.root()
        if root is None:
            raise AssetError("找不到 MaiWork 自己的目录（配置里没有工作区根目录）")
        return root / FONTS_DIRNAME, root / BROWSERS_DIRNAME, root / HOSTLIBS_DIRNAME

    def _kv_get(self, key: str, default: Any = None) -> Any:
        if self._store is None:
            return default
        try:
            return self._store.kv_get(key, default)
        except Exception:
            return default

    def _kv_set(self, key: str, value: Any) -> None:
        if self._store is None:
            return
        try:
            with self._store.tx() as conn:
                self._store.kv_set(conn, key, value)
        except Exception:
            logger.warning("写 kv %s 出错", key, exc_info=True)

    def _check(self) -> dict[str, Any]:
        if self._cache is None:
            rec = self._kv_get(CHECK_KEY, None)
            self._cache = rec if isinstance(rec, dict) else {}
        return self._cache

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait(self) -> None:
        for _ in range(100):
            pending = [t for t in self._tasks if not t.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    def busy(self) -> bool:
        return bool(self._running) or self._checking

    # ---------------- 探测 ----------------

    async def _probe_browser(self) -> dict[str, Any]:
        _fonts, browsers, hostlibs = self._dirs()
        exe = find_chromium(browsers)
        env = chromium_env(browsers, hostlibs=hostlibs if hostlibs.is_dir() else None)
        env["MW_LAUNCH"] = "1"
        if exe is not None:
            env["MW_EXE"] = str(exe)
        code, out, err = await self._runner(
            [self._python, "-c", _PROBE_SCRIPT], env=env, timeout=PROBE_TIMEOUT_S,
        )
        info = _parse_probe(_decode(out))
        info["local"] = str(exe) if exe is not None else ""
        if not info.get("playwright") and not info.get("error"):
            info["error"] = _tail(_decode(err), MAX_ERROR_CHARS)
        return info

    async def refresh(self) -> None:
        if self._checking:
            return
        self._checking = True
        try:
            rec: dict[str, Any] = {"at": clock.now()}
            try:
                fonts, _b, _h = self._dirs()
            except AssetError as exc:
                rec["error"] = str(exc)
                fonts = None
            try:
                system = await asyncio.to_thread(self._system_fonts)
            except Exception:
                system = []
            rec["fonts"] = font_state(fonts, system=system, font=self._font)
            try:
                rec["browser"] = await self._probe_browser()
            except asyncio.TimeoutError:
                rec["browser"] = {"playwright": True, "launch": "failed", "launch_error": "启动探测超时"}
            except Exception as exc:  # noqa: BLE001
                rec["browser"] = {"playwright": False, "error": f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]}
            self._cache = rec
            self._kv_set(CHECK_KEY, rec)
        finally:
            self._checking = False

    def checked(self) -> bool:
        return float(self._check().get("at") or 0.0) > 0

    def maybe_refresh(self, *, max_age: float = CHECK_TTL_S) -> bool:
        if self._checking or self._running:
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

    # ---------------- 状态 ----------------

    def _last(self, gid: str) -> dict[str, Any]:
        rec = self._kv_get(KV_PREFIX + gid, None)
        last = rec if isinstance(rec, dict) else {}
        state = str(last.get("state") or "")
        error = str(last.get("error") or "")
        if state in ("installing", "uninstalling") and gid not in self._running:
            state, error = "interrupted", "上次操作因插件重新加载而中断，请重新操作"
        return {
            "state": state, "at": float(last.get("at") or 0.0), "by": str(last.get("by") or ""),
            "error": error, "log": str(last.get("log") or ""),
        }

    def _font_view(self, check: Mapping[str, Any]) -> tuple[str, str, str]:
        """(state, 一句话, hint)。本地文件现查（便宜），系统字体用上次探测结果。"""
        try:
            fonts, _b, _h = self._dirs()
        except AssetError:
            fonts = None
        system = list(((check.get("fonts") or {}).get("system")) or [])
        st = font_state(fonts, system=system, font=self._font)
        if st["state"] == "ok":
            return "ok", f"已安装（{self._font.label}），资讯卡片会用它显示中文", ""
        if not check.get("at"):
            return "unknown", "未检查", ""
        if st["state"] == "system":
            return "system", "系统已有中文字体（" + "、".join(system[:3]) + "），一般不用再装", ""
        return "missing", "未安装，系统里也没找到中文字体：资讯卡片里的中文可能显示成方块", ""

    def _browser_view(self, check: Mapping[str, Any]) -> tuple[str, str, str]:
        try:
            _f, browsers, _h = self._dirs()
            exe = find_chromium(browsers)
        except AssetError:
            exe = None
        info = check.get("browser") if isinstance(check.get("browser"), dict) else {}
        if not check.get("at"):
            return ("broken" if exe else "unknown"), ("已下载，尚未检查能否启动" if exe else "未检查"), ""
        if not info.get("playwright"):
            return "missing", "MaiBot 环境里没有 playwright；安装时会先把它装到 MaiWork 自己的目录，再下载浏览器", ""
        launch = str(info.get("launch") or "")
        local = bool(info.get("local"))
        if launch == "ok" and local:
            return "ok", "已下载到 MaiWork 自己的目录，启动检查通过", ""
        if launch == "ok":
            return "system", "MaiBot 环境里已有可用的浏览器，一般不用再下载", ""
        if local:
            return "broken", "已下载，但启动失败", explain_launch_error(str(info.get("launch_error") or ""))
        return "missing", "未安装：没有可用的浏览器，资讯卡片会改发文字", ""

    def status_groups(self) -> list[dict[str, Any]]:
        check = self._check()
        out: list[dict[str, Any]] = []
        for gid in GROUP_IDS:
            state, summary, hint = (self._font_view if gid == "fonts" else self._browser_view)(check)
            running = self._running.get(gid, "")
            if running == "install":
                state, summary = "installing", "正在安装…"
            elif running == "uninstall":
                state, summary = "uninstalling", "正在卸载…"
            last = self._last(gid)
            out.append({
                "id": gid,
                "kind": "asset",
                "label": _LABELS[gid],
                "purpose": _PURPOSES[gid],
                "state": state,
                "summary": summary,
                "hint": hint,
                "available": self.root() is not None,
                "note": "" if self.root() is not None else "找不到 MaiWork 自己的目录（配置里没有工作区根目录）",
                "installing": running == "install",
                "uninstalling": running == "uninstall",
                "installed_count": 1 if state == "ok" else 0,
                "total": 1,
                "packages": [],
                "tools": [],
                "last_install": last,
                "error": last["error"] if last["state"] in ("failed", "interrupted") else "",
            })
        return out

    # ---------------- 装 / 卸 ----------------

    def _begin(self, gid: str, action: str, by: str) -> None:
        from .optional_deps import Busy, Unavailable

        if gid not in GROUP_IDS:
            raise KeyError(gid)
        if self.root() is None:
            raise Unavailable("找不到 MaiWork 自己的目录（配置里没有工作区根目录）")
        if gid in self._running:
            raise Busy(f"「{_LABELS[gid]}」正在装 / 卸，等它跑完")
        self._running[gid] = action
        self._kv_set(KV_PREFIX + gid, {
            "state": action + "ing" if action == "install" else "uninstalling",
            "at": clock.now(), "by": by, "error": "", "log": "",
        })

    def start_install(self, gid: str, *, by: str = "网页管理员") -> None:
        self._begin(gid, "install", str(by))
        self._spawn(self._run(gid, "install", str(by)))

    def start_uninstall(self, gid: str, *, by: str = "网页管理员") -> None:
        self._begin(gid, "uninstall", str(by))
        self._spawn(self._run(gid, "uninstall", str(by)))

    async def _run(self, gid: str, action: str, by: str) -> None:
        rec = {"state": "failed", "at": 0.0, "by": by, "error": "", "log": ""}
        try:
            fn = {
                ("fonts", "install"): self._install_font,
                ("fonts", "uninstall"): self._uninstall_font,
                ("browser", "install"): self._install_browser,
                ("browser", "uninstall"): self._uninstall_browser,
            }[(gid, action)]
            rec["log"] = await fn()
            rec["state"] = "ok"
        except asyncio.TimeoutError:
            rec["error"] = "超时（网络太慢？可以稍后重试）"
        except Exception as exc:  # noqa: BLE001
            logger.warning("画图组件「%s」%s出错", gid, action, exc_info=True)
            msg = str(exc) if isinstance(exc, AssetError) else f"{type(exc).__name__}: {exc}"
            rec["error"] = msg[:MAX_ERROR_CHARS * 2]
        finally:
            rec["at"] = clock.now()
            self._running.pop(gid, None)
            self._kv_set(KV_PREFIX + gid, rec)

    async def _install_font(self) -> str:
        fonts, _b, _h = self._dirs()
        path = await download_font(fonts, font=self._font, transport=self._transport)
        (fonts / FONTCONF_NAME).write_text(_fontconf_text(fonts), encoding="utf-8")
        for p in (fonts, fonts / FONTCONF_NAME):
            try:
                os.chmod(p, 0o755 if p.is_dir() else 0o644)
            except OSError:
                pass
        await self.refresh()
        return f"已下载并校验：{path}"

    async def _uninstall_font(self) -> str:
        fonts, _b, _h = self._dirs()
        for name in (self._font.filename, FONTCONF_NAME):
            try:
                (fonts / name).unlink()
            except FileNotFoundError:
                pass
        shutil.rmtree(fonts / ".fccache", ignore_errors=True)
        if _font_file(fonts, self._font) is not None:
            raise AssetError("字体文件没删掉（权限？）")
        await self.refresh()
        return "已删除字体文件"

    async def _pip_playwright(self, hostlibs: Path) -> str:
        from .optional_deps import pip_argv, pip_env

        hostlibs.mkdir(parents=True, exist_ok=True)
        argv = pip_argv(self._python, hostlibs, ["playwright"])
        env = pip_env(hostlibs)
        code, out, err = await self._runner(argv, env=env, timeout=PIP_TIMEOUT_S)
        out, err = _decode(out), _decode(err)
        if int(code) != 0 and "externally-managed" in out + err:
            argv = [*argv[:4], "--break-system-packages", *argv[4:]]
            code, out2, err2 = await self._runner(argv, env=env, timeout=PIP_TIMEOUT_S)
            out, err = out + _decode(out2), err + _decode(err2)
        if int(code) != 0:
            raise AssetError("安装 playwright 失败：" + _tail(err or out, MAX_ERROR_CHARS))
        return _tail(out + "\n" + err, 1000)

    async def _install_browser(self) -> str:
        _f, browsers, hostlibs = self._dirs()
        log = ""
        await self.refresh()
        if not (self._check().get("browser") or {}).get("playwright"):
            log += await self._pip_playwright(hostlibs) + "\n"
        browsers.mkdir(parents=True, exist_ok=True)
        env = chromium_env(browsers, hostlibs=hostlibs if hostlibs.is_dir() else None)
        code, out, err = await self._runner(
            chromium_install_argv(self._python), env=env, timeout=INSTALL_TIMEOUT_S,
        )
        log += _tail(_decode(out) + "\n" + _decode(err))
        if int(code) != 0:
            raise AssetError("下载浏览器失败：" + _tail(_decode(err) or _decode(out), MAX_ERROR_CHARS))
        from .optional_deps import _relax_perms

        _relax_perms(browsers)
        await self.refresh()
        info = self._check().get("browser") or {}
        if not info.get("local"):
            raise AssetError("下载命令说成功了，但在 MaiWork 目录里没找到浏览器")
        if info.get("launch") != "ok":
            raise AssetError(explain_launch_error(str(info.get("launch_error") or info.get("error") or "")))
        return _tail(log)

    async def _uninstall_browser(self) -> str:
        _f, browsers, _h = self._dirs()
        if browsers.is_symlink():
            raise AssetError("浏览器目录是符号链接，不动它")
        shutil.rmtree(browsers, ignore_errors=True)
        if browsers.exists():
            raise AssetError("浏览器目录没删干净（权限？）")
        await self.refresh()
        return "已删除浏览器目录"
