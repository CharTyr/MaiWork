"""Optional social discovery CLIs, installed in MaiWork-owned per-platform venvs.

See task research evidence for upstream contracts and verification limits. A venv
and private HOME isolate dependencies/configuration; they are NOT an OS sandbox.
Results are leads: callers still apply original-page verification and privacy gates.
Never return/log raw CLI diagnostics or put credentials in argv or kv state.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
import html
import json
import logging
import os
from pathlib import Path
import re
import shlex
import signal
import sys
import tempfile
import time
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote_plus, urlsplit
import weakref

logger = logging.getLogger("maiwork.social_platforms")
KV_KEY = "social.platforms"
COOKIE_PREFIX = "social.cookie."
COOKIE_MAX = 8000
QUERY_MAX = 500
SNIPPET_MAX = 300
SEARCH_TIMEOUT_S = 45
CHECK_TIMEOUT_S = 30
INSTALL_TIMEOUT_S = 180
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_PARALLEL = 3
_FIXED_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"


class SocialPlatformError(RuntimeError):
    """A fixed, credential-safe administrator-facing message."""


def _twitter_cmd(query, cookie, limit):
    return ["twitter", "search", query, "-t", "Latest", "--max", str(limit), "--json"]


def _xhs_cmd(query, cookie, limit):
    return ["xhs", "search", query, "--json"]


def _bili_cmd(query, cookie, limit):
    return ["bili", "search", query, "--type", "video", "--max", str(limit), "--json"]


def _youtube_cmd(query, cookie, limit):
    return ["yt-dlp", "--ignore-config", f"ytsearch{limit}:{query}", "--flat-playlist", "--dump-json", "--no-warnings"]


def _reddit_cmd(query, cookie, limit):
    return ["rdt", "search", query, "-n", str(limit), "--json"]


def _items(value: Any) -> list[dict]:
    if isinstance(value, list):
        if any(not isinstance(item, dict) for item in value):
            raise SocialPlatformError("平台返回的数据格式无法识别")
        return value
    if not isinstance(value, dict):
        raise SocialPlatformError("平台返回的数据格式无法识别")
    if value.get("ok") is False or value.get("success") is False or value.get("error"):
        raise SocialPlatformError("平台搜索未成功，请检测连接或更新登录状态")
    # "children"：rdt-cli 真实信封 data.data.children（Reddit Listing，2026-10-16 实测）
    for key in ("data", "items", "results", "tweets", "notes", "videos", "posts", "entries", "children"):
        if key in value:
            return _items(value[key])
    if any(key in value for key in ("title", "text", "note_card", "bvid", "permalink", "webpage_url")):
        return [value]
    raise SocialPlatformError("平台返回的数据格式无法识别")


def _iter_items(stdout: str) -> list[dict]:
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        # yt-dlp --dump-json emits one JSON object per line.
        try:
            lines = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        except (ValueError, TypeError):
            raise SocialPlatformError("平台返回的数据格式无法识别") from None
        if not lines:
            raise SocialPlatformError("平台未返回可识别的数据")
        rows = []
        for line in lines:
            rows.extend(_items(line))
        return rows
    return _items(payload)


def _first(item, keys):
    return next((item[k] for k in keys if item.get(k) is not None and item[k] != ""), None)


def _to_epoch(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    try:
        if re.fullmatch(r"\d{8}", text):
            return datetime.strptime(text, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()
        stamp = float(text)
        if stamp > 1e12:
            stamp /= 1000
        return stamp if 0 < stamp < 1e11 else None
    except (ValueError, OverflowError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()
        except (ValueError, OverflowError, OSError):
            return None


def _redact_text(text: str, redact=()) -> str:
    for secret in redact:
        text = text.replace(secret, "***")
    return text


def _plain(value: Any, limit: int, *, redact=()) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    text = html.unescape(re.sub(r"<[^>]*>", "", _redact_text(str(value), redact)))
    return _redact_text(text, redact).strip()[:limit]


def _url(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    try:
        p = urlsplit(value)
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
            return ""
    except ValueError:
        return ""
    return value


def _platform_url(pid: str, value: Any) -> str:
    value = _url(value)
    if not value:
        return ""
    p = urlsplit(value)
    host = p.hostname.lower()
    try:
        if p.port not in (None, 80, 443):
            return ""
    except ValueError:
        return ""
    if pid == "twitter" and host in ("x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"):
        if re.fullmatch(r"/(?:[A-Za-z0-9_]{1,15}|i/web)/status/[0-9]+/?", p.path):
            return "https://x.com" + p.path.rstrip("/")
    elif pid == "youtube" and host in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"):
        video = p.path[1:] if host == "youtu.be" else parse_qs(p.query).get("v", [""])[0] if p.path == "/watch" else ""
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", video):
            return "https://www.youtube.com/watch?v=" + video
    elif pid == "bilibili" and host in ("www.bilibili.com", "bilibili.com", "m.bilibili.com"):
        if re.fullmatch(r"/video/(?:BV[A-Za-z0-9]{10}|av[0-9]+)/?", p.path):
            return "https://www.bilibili.com" + p.path.rstrip("/")
    elif pid == "reddit" and host in ("www.reddit.com", "reddit.com", "old.reddit.com", "new.reddit.com"):
        if re.fullmatch(r"/r/[A-Za-z0-9_]+/comments/[a-z0-9]+(?:/[A-Za-z0-9_~-]*)*/?", p.path):
            return "https://www.reddit.com" + p.path
    elif pid == "xiaohongshu" and host in ("www.xiaohongshu.com", "xiaohongshu.com"):
        if re.fullmatch(r"/(?:explore|discovery/item)/[a-fA-F0-9]{24}/?", p.path):
            # xsec_token can be necessary for opening the original note.
            return "https://www.xiaohongshu.com" + p.path + ("?" + p.query if p.query else "")
    return ""


def _parse(pid: str, stdout: str, limit: int, *, redact=()) -> list[dict]:
    out = []
    seen = set()
    items = _iter_items(stdout)
    for original in items:
        item = dict(original)
        # Reddit Listing 的 {"kind":"t3","data":{帖子字段}} 包裹：纯包裹才解开（2026-10-16 实测）
        wrapped = item.get("data")
        if isinstance(wrapped, dict) and set(item) <= {"kind", "data"}:
            item = dict(wrapped)
        card = item.get("note_card") or item.get("noteCard")
        if isinstance(card, dict):
            item.update(card)
        url = _first(item, ("webpage_url", "url", "link", "arcurl"))
        if pid == "twitter" and item.get("id"):
            author = item.get("author")
            handle = author.get("screenName") if isinstance(author, dict) else None
            if handle:
                url = f"https://x.com/{quote(str(handle), safe='')}/status/{quote(str(item['id']), safe='')}"
        elif pid == "bilibili" and item.get("bvid"):
            url = f"https://www.bilibili.com/video/{quote(str(item['bvid']), safe='')}"
        elif pid == "youtube":
            if not _url(url) and item.get("id"):
                url = f"https://www.youtube.com/watch?v={quote(str(item['id']), safe='')}"
        elif pid == "reddit" and item.get("permalink"):
            url = str(item["permalink"])
            if url.startswith("/") and not url.startswith("//"):
                url = "https://www.reddit.com" + url
        elif pid == "xiaohongshu" and not _url(url) and item.get("id"):
            url = f"https://www.xiaohongshu.com/explore/{quote(str(item['id']), safe='')}"
        url = _platform_url(pid, html.unescape(str(url or "")))
        decoded_url = unquote_plus(url)
        # Reject credential-bearing links instead of making broken masked URLs.
        # Nested percent escapes are ambiguous; never return them with secrets.
        if any(secret in url or secret in decoded_url for secret in redact) or (
            redact and unquote_plus(decoded_url) != decoded_url
        ):
            continue
        identity = url.rsplit("/", 1)[-1] if pid == "twitter" else url
        if not url or identity in seen:
            continue
        body = _plain(_first(item, ("snippet", "description", "desc", "selftext", "text", "content", "summary")), SNIPPET_MAX, redact=redact)
        title = _plain(_first(item, ("title", "display_title", "displayTitle", "name")), 300, redact=redact) or body[:60]
        if not title:
            continue
        published = _to_epoch(_first(item, ("published", "published_at", "createdAtISO", "created_at", "upload_date", "pubdate", "timestamp", "created_utc", "time", "date")))
        out.append({"title": title, "url": url, "snippet": body, "published": published})
        seen.add(identity)
        if len(out) >= max(1, int(limit)):
            break
    if items and not out:
        raise SocialPlatformError("平台返回了条目，但没有可识别的内容链接，请检查工具版本")
    return out


@dataclass(frozen=True)
class Platform:
    id: str
    name: str
    cli_name: str
    package: str
    needs_cookie: bool
    search_cmd: Callable
    parse_output: Callable
    cookie_guide: str = ""

    @property
    def supports_cookie(self) -> bool:
        return self.needs_cookie


PLATFORMS = {
    "twitter": Platform("twitter", "Twitter/X", "twitter", "twitter-cli>=0.8.5,<0.9", True, _twitter_cmd, partial(_parse, "twitter"), "在浏览器登录 X，复制请求头 Cookie；必须包含 auth_token 和 ct0。不要分享给其他人。"),
    "xiaohongshu": Platform("xiaohongshu", "小红书", "xhs", "xiaohongshu-cli", True, _xhs_cmd, partial(_parse, "xiaohongshu"), "在浏览器登录小红书，复制请求头中的完整 Cookie。建议包含 a1 和 web_session；是否有效以检测结果为准。"),
    "bilibili": Platform("bilibili", "B站", "bili", "bilibili-cli", False, _bili_cmd, partial(_parse, "bilibili")),
    "youtube": Platform("youtube", "YouTube", "yt-dlp", "yt-dlp[default]", False, _youtube_cmd, partial(_parse, "youtube")),
    "reddit": Platform("reddit", "Reddit", "rdt", "git+https://github.com/public-clis/rdt-cli.git@5e4fb3720d5c174e976cd425ccc3b879d52cac66", True, _reddit_cmd, partial(_parse, "reddit"), "在浏览器登录 Reddit，复制请求头中的完整 Cookie。是否有效以检测结果为准。"),
}


def _platform(platform_id: Any) -> Platform:
    p = PLATFORMS.get(str(platform_id or ""))
    if not p:
        raise SocialPlatformError("不支持这个社交平台")
    return p


def cmd_line(argv: list) -> str:
    """For public argument display only. Credentials never enter search argv."""
    return shlex.join([str(a) for a in argv])


def managed_root(store: Any, platform_id: str) -> Path:
    p = _platform(platform_id)
    return Path(store.path).absolute().parent / "social-platforms" / p.id


def _private_dir(path: Path) -> None:
    if path.is_symlink():
        raise SocialPlatformError("依赖目录异常，请管理员检查")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _prepare_root(store, p):
    root = managed_root(store, p.id)
    _private_dir(root.parent)
    _private_dir(root)
    if (root / "venv").is_symlink():
        raise SocialPlatformError("依赖目录异常，请管理员检查")
    return root


def _cli(store, p) -> str:
    root = managed_root(store, p.id)
    exe = root / "venv" / "bin" / p.cli_name
    # Ignore legacy global-PATH/kv paths. Only an actual managed executable counts.
    try:
        if root.is_symlink() or root.parent.is_symlink() or (root / "venv").is_symlink():
            return ""
        if exe.is_file() and os.access(exe, os.X_OK) and exe.resolve().is_relative_to(root.resolve()):
            return str(exe)
    except OSError:
        pass
    return ""


_DEFAULT = {"enabled": False, "cli_path": "", "installed_at": 0, "last_check": 0, "last_error": ""}


def _states(store):
    raw = store.kv_get(KV_KEY, {})
    return raw if isinstance(raw, dict) else {}


def _state_of(store, pid):
    raw = _states(store).get(pid)
    return {**_DEFAULT, **(raw if isinstance(raw, dict) else {})}


def _patch(store, pid, **patch):
    # Read and merge INSIDE the same transaction, never retain async snapshots.
    with store.tx() as conn:
        states = _states(store)
        raw = states.get(pid)
        states[pid] = {**_DEFAULT, **(raw if isinstance(raw, dict) else {}), **patch}
        store.kv_set(conn, KV_KEY, states)


_INSTALLING: set[tuple[str, str]] = set()
_CONTEXTS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _key(store, pid):
    return (str(Path(store.path).absolute()), pid)


def install_in_progress(platform_id: Any, store: Any = None) -> bool:
    pid = str(platform_id or "")
    return _key(store, pid) in _INSTALLING if store is not None else any(k[1] == pid for k in _INSTALLING)


def _context(store, pid):
    loop = asyncio.get_running_loop()
    if loop not in _CONTEXTS:
        _CONTEXTS[loop] = (asyncio.Semaphore(MAX_PARALLEL), {})
    sem, locks = _CONTEXTS[loop]
    key = _key(store, pid)
    if key not in locks:
        locks[key] = asyncio.Lock()
    return sem, locks[key]


def _view(store, p):
    st = _state_of(store, p.id)
    cli = _cli(store, p)
    return {"id": p.id, "name": p.name, "cli_name": p.cli_name, "needs_cookie": p.needs_cookie,
            "supports_cookie": p.supports_cookie, "cookie_guide": p.cookie_guide,
            "enabled": bool(st["enabled"]), "installed": bool(cli), "installing": install_in_progress(p.id, store),
            "cli_path": cli, "installed_at": st["installed_at"], "last_check": st["last_check"],
            "cookie_set": cookie_status(store, p.id), "last_error": st["last_error"]}


def list_platforms(store):
    return [_view(store, p) for p in PLATFORMS.values()]


def enable_platform(store, platform_id):
    p = _platform(platform_id)
    _patch(store, p.id, enabled=True)
    return _view(store, p)


def disable_platform(store, platform_id):
    p = _platform(platform_id)
    _patch(store, p.id, enabled=False)
    return _view(store, p)


def _cookies(value: Any, p: Platform) -> dict[str, str]:
    if not isinstance(value, str) or not value.strip() or len(value) > COOKIE_MAX or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise SocialPlatformError("Cookie 为空、过长或含换行，请重新复制请求头中的 Cookie")
    cookies = {}
    for part in value.split(";"):
        if not part.strip():
            continue
        key, sep, val = part.strip().partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z0-9_!#$%&'*+.^`|~-]+", key) or not val.strip():
            raise SocialPlatformError("Cookie 格式不正确，请复制完整的 Cookie 请求头内容")
        cookies[key] = val.strip()
    if not cookies:
        raise SocialPlatformError("Cookie 是空的")
    if p.id == "twitter" and not all(cookies.get(k) for k in ("auth_token", "ct0")):
        raise SocialPlatformError("Twitter/X Cookie 必须同时包含 auth_token 和 ct0")
    return cookies


def set_cookie(store, platform_id, cookie):
    p = _platform(platform_id)
    if not p.supports_cookie:
        raise SocialPlatformError("这个平台使用公开搜索，无需填写 Cookie")
    _cookies(cookie, p)
    with store.tx() as conn:
        store.secret_set(conn, COOKIE_PREFIX + p.id, cookie.strip())


def clear_cookie(store, platform_id):
    p = _platform(platform_id)
    with store.tx() as conn:
        store.secret_delete(conn, COOKIE_PREFIX + p.id)


def cookie_status(store, platform_id):
    p = _platform(platform_id)
    return bool(store.secret_get(COOKIE_PREFIX + p.id).strip())


def _child_env(home: Path, root: Path) -> dict[str, str]:
    # Deliberately excludes host secrets, PYTHONPATH, browser/config locations,
    # proxy credentials and arbitrary subprocess configuration.
    return {"PATH": str(root / "venv" / "bin") + ":" + _FIXED_PATH,
            "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"), "XDG_DATA_HOME": str(home / ".local/share"),
            "TMPDIR": str(home), "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1", "PIP_NO_CACHE_DIR": "1"}


def _credential_file(path: Path, data: dict) -> None:
    _private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream)


def _credentials(home, p, cookies, env):
    if p.id == "twitter":
        env["TWITTER_AUTH_TOKEN"] = cookies["auth_token"]
        env["TWITTER_CT0"] = cookies["ct0"]
    elif p.id == "xiaohongshu":
        _credential_file(home / ".xiaohongshu-cli" / "cookies.json", {**cookies, "saved_at": time.time()})
    elif p.id == "reddit":
        _private_dir(home / ".config")
        _credential_file(home / ".config" / "rdt-cli" / "credential.json", {
            "cookies": cookies, "source": "maiwork", "username": None, "modhash": None,
            "saved_at": time.time(), "last_verified_at": None})


def _kill_group(proc):
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        elif proc.returncode is None:
            proc.kill()
    except ProcessLookupError:
        pass


async def _run_cli(argv: list, *, timeout: float, env: dict | None = None, cwd: Path | None = None) -> tuple[int, str, str]:
    """Bound both streams together; kill and reap on timeout/cancel/output cap."""
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *map(str, argv), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=env or {"PATH": _FIXED_PATH, "LANG": "C.UTF-8"}, cwd=cwd,
        start_new_session=(os.name == "posix")))
    try:
        proc = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        # Spawn can finish after cancellation; it must not leave an orphan.
        proc = await spawn
        _kill_group(proc)
        await proc.wait()
        raise
    total = 0
    async def drain(stream):
        nonlocal total
        chunks = []
        while True:
            chunk = await stream.read(16384)
            if not chunk:
                return b"".join(chunks).decode("utf-8", errors="replace")
            total += len(chunk)
            if total > MAX_OUTPUT_BYTES:
                raise SocialPlatformError("平台命令输出过大，已终止")
            chunks.append(chunk)
    readers = [asyncio.create_task(drain(proc.stdout)), asyncio.create_task(drain(proc.stderr))]
    async def finish():
        out, err = await asyncio.gather(*readers)
        code = await proc.wait()
        return code, out, err
    try:
        return await asyncio.wait_for(finish(), timeout=timeout)
    finally:
        _kill_group(proc)
        for task in readers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        await proc.wait()


async def install_platform(store, platform_id):
    p = _platform(platform_id)
    key = _key(store, p.id)
    if key in _INSTALLING:
        return {**_view(store, p), "ok": False, "error": "正在安装中，请稍后查看"}
    _INSTALLING.add(key)
    error = ""
    try:
        _patch(store, p.id, last_error="")
        async def install():
            sem, lock = _context(store, p.id)
            async with lock, sem:
                root = _prepare_root(store, p)
                venv = root / "venv"
                with tempfile.TemporaryDirectory(prefix="install-", dir=root) as name:
                    home = Path(name)
                    env = _child_env(home, root)
                    if not (venv / "bin/python").is_file():
                        code, _, _ = await _run_cli([sys.executable, "-m", "venv", str(venv)], timeout=INSTALL_TIMEOUT_S, env=env, cwd=home)
                        if code:
                            raise SocialPlatformError("安装失败：无法创建独立 Python 环境，请检查 venv 支持")
                    code, _, _ = await _run_cli([str(venv / "bin/python"), "-m", "pip", "--isolated", "install", "--upgrade", p.package], timeout=INSTALL_TIMEOUT_S, env=env, cwd=home)
                    if code:
                        raise SocialPlatformError("安装失败，请检查网络、Python 版本或包源后重试")
                cli = _cli(store, p)
                if not cli:
                    raise SocialPlatformError("安装失败：未找到独立环境内的可执行文件")
                _patch(store, p.id, cli_path=cli, installed_at=time.time(), last_error="")
        # Queue time is part of the budget, not an unbounded prelude to it.
        await asyncio.wait_for(install(), INSTALL_TIMEOUT_S)
    except asyncio.CancelledError:
        _patch(store, p.id, last_error="安装已取消，可稍后重试")
        raise
    except asyncio.TimeoutError:
        error = "安装超时，请检查网络后重试"
    except SocialPlatformError as exc:
        error = str(exc)
    except Exception:
        error = "安装失败：独立环境不可用，请管理员检查"
        logger.warning("社交平台 %s 安装失败", p.id)
    finally:
        _INSTALLING.discard(key)
    if error:
        _patch(store, p.id, last_error=error)
    return {**_view(store, p), "ok": not bool(error), "error": error}


async def _search(store, p, query, limit, *, check=False):
    # wait_for cancels and awaits its child, including queued lock/semaphore waits
    # and credential/process cleanup if execution has already started.
    try:
        return await asyncio.wait_for(
            _inner_search(store, p, query, limit, check=check),
            CHECK_TIMEOUT_S if check else SEARCH_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise SocialPlatformError("平台搜索超时，请稍后重试") from None


async def _inner_search(store, p, query, limit, *, check=False):
    q = str(query or "").strip()
    if not q or q.startswith("-") or len(q) > QUERY_MAX or any(ord(c) < 32 or ord(c) == 127 for c in q):
        raise SocialPlatformError("搜索词为空、过长、以减号开头或包含控制字符")
    try:
        n = max(1, min(int(limit), 50))
    except (TypeError, ValueError, OverflowError):
        raise SocialPlatformError("搜索条数必须是 1 到 50 之间的整数") from None
    sem, lock = _context(store, p.id)
    try:
        async with lock, sem:
            if not check and not _state_of(store, p.id)["enabled"]:
                raise SocialPlatformError("这个平台还未启用，请到设置中启用")
            cli = _cli(store, p)
            if not cli:
                raise SocialPlatformError("命令行工具还没装好，请在社交平台设置中安装")
            cookies = {}
            if p.needs_cookie:
                value = store.secret_get(COOKIE_PREFIX + p.id)
                if not value:
                    raise SocialPlatformError("还没填写 Cookie，请到登录态设置中填写")
                cookies = _cookies(value, p)
            root = _prepare_root(store, p)
            with tempfile.TemporaryDirectory(prefix="search-", dir=root) as name:
                home = Path(name)
                env = _child_env(home, root)
                _credentials(home, p, cookies, env)
                argv = p.search_cmd(q, "", n)
                argv[0] = cli
                code, out, _ = await _run_cli(argv, timeout=CHECK_TIMEOUT_S if check else SEARCH_TIMEOUT_S, env=env, cwd=home)
                if code:
                    raise SocialPlatformError("平台搜索失败，请检查连接或更新登录状态")
                # Decode first: cookie values may match JSON keys, booleans or
                # escaped strings. Redact decoded fields before truncation.
                # Short cookie flags ("1", "true") are not usable credentials.
                redact = sorted({v for v in cookies.values() if len(v) >= 8}, key=len, reverse=True)
                rows = p.parse_output(out, n, redact=redact)
                for row in rows:
                    row["platform"] = p.id
                return rows
    except asyncio.TimeoutError:
        raise SocialPlatformError("平台搜索超时，请稍后重试") from None
    except SocialPlatformError:
        raise
    except Exception:
        raise SocialPlatformError("平台搜索无法执行，请管理员检查独立环境") from None


async def search(store, platform_id, query, *, limit=10):
    return await _search(store, _platform(platform_id), query, limit)


async def check_platform(store, platform_id):
    p = _platform(platform_id)
    error = ""
    try:
        await _search(store, p, "python", 1, check=True)
    except SocialPlatformError as exc:
        error = str(exc)
    _patch(store, p.id, last_check=time.time(), last_error=error)
    return {**_view(store, p), "ok": not bool(error), "error": error, "version": ""}


# ----------------------------------------------------------------------
# Twitter 网页备选渠道（2026-10-16，PRD .trellis/tasks/10-16-twitter-web-channel）
# ----------------------------------------------------------------------

_SNOWFLAKE_EPOCH_MS = 1288834974657  # Twitter 纪元（2010-11-04T01:42:54.657Z）
_TWITTER_SITES = ("x.com", "twitter.com")
TWITTER_CLI_TIMEOUT_S = 20
TWITTER_WEB_TIMEOUT_S = 20
# 网页渠道结果的 provider 标记：和 CLI 路（feeds 里标 social:twitter）区分开，可溯源到引擎
WEB_PROVIDER_PREFIX = "social:twitter-web"


def _snowflake_published(url: str) -> float | None:
    """从合法 Twitter 状态链接的 Snowflake ID 推 UTC 发布时间（epoch 秒）；推不出 → None。

    url 须已过 _platform_url("twitter", ...) 校验（路径是 /status/<纯数字>）。
    ID 不在 64 位内、推出时间早于 Twitter 纪元或晚于明天 → None（绝不伪造）。
    """
    m = re.search(r"/status/(\d+)", urlsplit(url).path)
    if not m:
        return None
    try:
        snowflake = int(m.group(1))
    except (ValueError, OverflowError):
        return None
    if not 0 < snowflake < 1 << 63:
        return None
    epoch_s = ((snowflake >> 22) + _SNOWFLAKE_EPOCH_MS) / 1000
    if not _SNOWFLAKE_EPOCH_MS / 1000 <= epoch_s <= time.time() + 86400:
        return None
    return epoch_s


async def search_twitter_web(query: str, *, web_search_fn: Callable, limit: int = 10) -> list[dict]:
    """Twitter web discovery using existing search services, without Twitter cookies.

    Search x.com and twitter.com under one HTTP/deadline budget; merge by status
    ID across domains and handles. Keep successful sites/providers when others
    fail. Missing dates may come from Snowflake metadata, never page evidence.
    web_search_fn is normally the bound Search.search_broad method. Candidates
    carry provider provenance and published_source in addition to CLI fields.
    """
    q = str(query or "").strip()
    if not q or q.startswith("-") or len(q) > QUERY_MAX or any(ord(c) < 32 or ord(c) == 127 for c in q):
        raise SocialPlatformError("搜索词为空、过长、以减号开头或包含控制字符")
    try:
        n = max(1, min(int(limit), 50))
    except (TypeError, ValueError, OverflowError):
        raise SocialPlatformError("搜索条数必须是 1 到 50 之间的整数") from None
    from .mcp_client import RequestBudget, request_budget, use_request_budget
    from .search import Search

    budget = request_budget.get() or RequestBudget(timeout_s=TWITTER_WEB_TIMEOUT_S)
    budget.deadline = min(budget.deadline, asyncio.get_running_loop().time() + TWITTER_WEB_TIMEOUT_S)
    owns_deadline = getattr(web_search_fn, "__func__", None) is Search.search_broad
    async def one_site(site):
        # Search budgets each provider, then collects completed results. A second
        # timer at the same deadline would cancel that collection and lose them.
        if owns_deadline:
            return await web_search_fn(q, site=site, limit=n)
        async with asyncio.timeout_at(budget.deadline):
            return await web_search_fn(q, site=site, limit=n)

    with use_request_budget(budget):
        results = await asyncio.gather(
            *(one_site(site) for site in _TWITTER_SITES), return_exceptions=True,
        )
    out: list[dict] = []
    seen: set[str] = set()
    failures = 0
    for site, rows in zip(_TWITTER_SITES, results):
        if isinstance(rows, asyncio.CancelledError):
            raise rows
        if isinstance(rows, BaseException):
            failures += 1
            logger.info("Twitter 网页渠道 %s 站点这次没搜成，用另一个站点的结果", site)
            continue
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            url = _platform_url("twitter", item.get("url"))
            identity = url.rsplit("/", 1)[-1]
            if not url or identity in seen:
                continue
            snippet = _plain(item.get("snippet"), SNIPPET_MAX)
            title = _plain(item.get("title"), 300) or snippet[:60]
            if not title:
                continue
            published = _to_epoch(item.get("published"))
            if published is None:
                published = _snowflake_published(url)
            engine = _plain(item.get("provider"), 100)
            seen.add(identity)
            out.append({"title": title, "url": url, "snippet": snippet, "published": published,
                        "published_source": "provider" if _to_epoch(item.get("published")) is not None else "snowflake" if published is not None else "",
                        "platform": "twitter",
                        "provider": f"{WEB_PROVIDER_PREFIX}:{engine}" if engine else WEB_PROVIDER_PREFIX})
            if len(out) >= n:
                break
        if len(out) >= n:
            break
    if failures == len(_TWITTER_SITES):
        raise SocialPlatformError("平台搜索暂不可用，请稍后重试")
    return out


async def search_all(store, query, *, platforms=None, limit=10, errors=None, web_search_fn=None) -> dict[str, list[dict]]:
    """Search enabled platforms; return candidates and optionally safe errors.

    Twitter uses CLI first when web_search_fn is supplied. Error, timeout or
    empty CLI output reserves time for the bounded web channel. Without that
    callback behavior remains CLI-only. Parent cancellation always propagates.
    """
    if platforms is None:
        ids = [p.id for p in PLATFORMS.values() if _state_of(store, p.id)["enabled"]]
    else:
        candidates = [platforms] if isinstance(platforms, str) else platforms
        ids = list(dict.fromkeys(str(pid) for pid in candidates if str(pid) in PLATFORMS))

    async def _one(pid: str) -> list[dict]:
        if pid != "twitter" or web_search_fn is None or not _state_of(store, pid)["enabled"]:
            return await search(store, pid, query, limit=limit)
        try:
            # Reserve time for the anonymous channel when CLI hangs. The outer
            # deadline includes CLI locks, cancellation cleanup and web queues.
            async with asyncio.timeout(SEARCH_TIMEOUT_S):
                try:
                    rows = await asyncio.wait_for(
                        search(store, pid, query, limit=limit), timeout=TWITTER_CLI_TIMEOUT_S,
                    )
                    if rows:
                        return rows
                except Exception:
                    pass
                return await search_twitter_web(query, web_search_fn=web_search_fn, limit=limit)
        except TimeoutError:
            raise SocialPlatformError("平台搜索超时，请稍后重试") from None

    results = await asyncio.gather(*(_one(pid) for pid in ids), return_exceptions=True)
    out = {}
    for pid, result in zip(ids, results):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            if errors is not None:
                errors[pid] = str(result) if isinstance(result, SocialPlatformError) else "平台搜索暂不可用"
            logger.info("社交平台 %s 这次搜索失败", pid)
        else:
            out[pid] = result
    return out
