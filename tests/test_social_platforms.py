"""Upstream-shaped fixtures. No dependency installed or platform contacted."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import sys
import pytest
from CharTyr_MaiWork.maiwork import social_platforms as sp
from CharTyr_MaiWork.maiwork.store import Store

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "db.sqlite3")
    s.migrate()
    yield s
    s.close()

def ready(store, pid):
    exe = sp.managed_root(store, pid) / "venv" / "bin" / sp.PLATFORMS[pid].cli_name
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o700)
    sp.enable_platform(store, pid)
    return exe

def fake_runner(monkeypatch, output="[]", code=0, stderr=""):
    calls = []
    async def run(argv, *, timeout, env=None, cwd=None):
        calls.append((argv, timeout, dict(env or {}), cwd))
        return code, output, stderr
    monkeypatch.setattr(sp, "_run_cli", run)
    return calls

def test_registry_and_status(store):
    rows = {r["id"]: r for r in sp.list_platforms(store)}
    assert list(rows) == ["twitter", "xiaohongshu", "bilibili", "youtube", "reddit"]
    assert sp.PLATFORMS["bilibili"].package == "bilibili-cli"
    assert sp.PLATFORMS["xiaohongshu"].package == "xiaohongshu-cli"
    assert sp.PLATFORMS["twitter"].cli_name == "twitter"
    assert all(not r["installed"] and not r["installing"] for r in rows.values())
    assert rows["twitter"]["supports_cookie"] is True
    assert rows["youtube"]["supports_cookie"] is False
    assert all("pipx" not in str(p) for p in sp.PLATFORMS.values())

@pytest.mark.parametrize("pid,expected", [
    ("twitter", ["twitter", "search", "topic", "-t", "Latest", "--max", "3", "--json"]),
    ("bilibili", ["bili", "search", "topic", "--type", "video", "--max", "3", "--json"]),
    ("reddit", ["rdt", "search", "topic", "-n", "3", "--json"]),
    ("xiaohongshu", ["xhs", "search", "topic", "--json"]),
])
def test_documented_commands(pid, expected):
    assert sp.PLATFORMS[pid].search_cmd("topic", "never-in-argv", 3) == expected

def test_youtube_ignores_global_configuration():
    argv = sp.PLATFORMS["youtube"].search_cmd("topic", "", 3)
    assert "--ignore-config" in argv and "ytsearch3:topic" in argv

def test_cookie_write_only_and_validation(store):
    cookie = "auth_token=fixture-auth; ct0=fixture-csrf"
    sp.set_cookie(store, "twitter", cookie)
    assert sp.cookie_status(store, "twitter")
    assert store.secret_get("social.cookie.twitter") == cookie
    assert "fixture-auth" not in json.dumps(sp.list_platforms(store))
    assert "fixture-auth" not in json.dumps(store.kv_get(sp.KV_KEY))
    for invalid in ("auth_token=one", "ct0=one", "a=b\nct0=x", " "):
        with pytest.raises(sp.SocialPlatformError):
            sp.set_cookie(store, "twitter", invalid)
    assert store.secret_get("social.cookie.twitter") == cookie
    sp.clear_cookie(store, "twitter")
    assert not sp.cookie_status(store, "twitter")

@pytest.mark.parametrize("pid", ["youtube", "bilibili"])
def test_unsupported_cookie_rejected(store, pid):
    with pytest.raises(sp.SocialPlatformError):
        sp.set_cookie(store, pid, "a1=fake; web_session=fake")

def test_normalize_upstream_envelopes():
    fixtures = {
        "twitter": {"ok": True, "schema_version": "1", "data": [{"id": "42", "text": "A tweet", "author": {"screenName": "alice"}, "createdAtISO": "2026-01-02T00:00:00Z"}]},
        "bilibili": {"data": {"items": [{"title": '<em class="keyword">A</em> video', "bvid": "BV1xx411c7mD", "description": "video description", "pubdate": 1760000000}]}},
        "reddit": {"data": [{"title": "A post", "permalink": "/r/test/comments/42/post/", "url": "https://external.example/story", "selftext": "discussion", "created_utc": 1760000000}]},
        "xiaohongshu": {"data": {"items": [{"id": "67fa123456789abcdef01234", "note_card": {"display_title": "A note", "desc": "note body"}}]}},
        "youtube": {"id": "abc123XYZ_-", "url": "abc123XYZ_-", "title": "A video", "upload_date": "20260102"},
    }
    expected = {"twitter": "https://x.com/alice/status/42", "bilibili": "https://www.bilibili.com/video/BV1xx411c7mD", "reddit": "https://www.reddit.com/r/test/comments/42/post/", "xiaohongshu": "https://www.xiaohongshu.com/explore/67fa123456789abcdef01234", "youtube": "https://www.youtube.com/watch?v=abc123XYZ_-"}
    for pid, data in fixtures.items():
        rows = sp.PLATFORMS[pid].parse_output(json.dumps(data), 2)
        assert len(rows) == 1, pid
        assert rows[0]["url"] == expected[pid]
        assert rows[0]["title"] and "<em" not in rows[0]["title"]
        if pid != "xiaohongshu":
            assert rows[0]["published"] > 0

def test_parser_rejects_invalid_schema_and_errors():
    for text in ("garbage", '{"ok":false,"error":"secret-value"}', '{"unknown":true}'):
        with pytest.raises(sp.SocialPlatformError) as err:
            sp.PLATFORMS["twitter"].parse_output(text, 3)
        assert "secret-value" not in str(err.value)
    assert sp.PLATFORMS["twitter"].parse_output('{"ok":true,"data":[]}', 3) == []

def test_normalization_bounds_and_urls():
    rows = [{"title": "X" * 5000, "url": "javascript:alert(1)"}, {"title": "Y" * 5000, "url": "https://www.bilibili.com/video/BV1xx411c7mD", "description": "Z" * 9000}]
    parsed = sp.PLATFORMS["bilibili"].parse_output(json.dumps(rows), 1)
    assert len(parsed) == 1 and parsed[0]["url"].startswith("https:")
    assert len(parsed[0]["title"]) <= 300 and len(parsed[0]["snippet"]) <= 300

@pytest.mark.asyncio
async def test_twitter_credentials_only_in_env(store, monkeypatch):
    ready(store, "twitter")
    sp.set_cookie(store, "twitter", "auth_token=fixture-auth; ct0=fixture-csrf")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")
    calls = fake_runner(monkeypatch, '{"ok":true,"data":[]}')
    assert await sp.search(store, "twitter", 'a "; $(echo no)', limit=2) == []
    argv, timeout, env, cwd = calls[0]
    assert 'a "; $(echo no)' in argv
    assert "fixture-auth" not in str(argv)
    assert env["TWITTER_AUTH_TOKEN"] == "fixture-auth" and env["TWITTER_CT0"] == "fixture-csrf"
    assert "UNRELATED_SECRET" not in env
    assert env["HOME"] != os.environ.get("HOME")
    assert not Path(env["HOME"]).exists()
    assert timeout == sp.SEARCH_TIMEOUT_S

@pytest.mark.asyncio
async def test_missing_cookie_does_not_start_process(store, monkeypatch):
    ready(store, "twitter")
    calls = fake_runner(monkeypatch)
    with pytest.raises(sp.SocialPlatformError, match="Cookie"):
        await sp.search(store, "twitter", "topic")
    assert not calls
    result = await sp.check_platform(store, "twitter")
    assert not result["ok"] and "Cookie" in result["error"]
    assert not calls

@pytest.mark.asyncio
@pytest.mark.parametrize("pid", ["xiaohongshu", "reddit"])
async def test_private_home_credentials(store, monkeypatch, pid):
    ready(store, pid)
    cookie = "a1=fixture-a1; web_session=fixture-session" if pid == "xiaohongshu" else "reddit_session=fixture-session"
    sp.set_cookie(store, pid, cookie)
    homes = []
    async def run(argv, *, timeout, env=None, cwd=None):
        home = Path(env["HOME"])
        homes.append(home)
        target = home / (".xiaohongshu-cli/cookies.json" if pid == "xiaohongshu" else ".config/rdt-cli/credential.json")
        data = json.loads(target.read_text())
        assert target.stat().st_mode & 0o777 == 0o600
        assert home.stat().st_mode & 0o777 == 0o700
        assert isinstance(data["saved_at"], float)
        if pid == "reddit":
            assert data["cookies"]["reddit_session"] == "fixture-session"
            assert data["last_verified_at"] is None and data["source"] == "maiwork"
        else:
            assert data["web_session"] == "fixture-session" and "cookies" not in data
        assert "fixture-session" not in str(argv)
        return 0, "[]", ""
    monkeypatch.setattr(sp, "_run_cli", run)
    assert await sp.search(store, pid, "topic") == []
    assert homes and not homes[0].exists()

@pytest.mark.asyncio
async def test_search_and_check_safe_errors(store, monkeypatch):
    ready(store, "bilibili")
    calls = fake_runner(monkeypatch, output='{"data":{"items":[]}}')
    result = await sp.check_platform(store, "bilibili")
    assert result["ok"] is True
    assert "search" in calls[0][0] and "--version" not in calls[0][0]
    fake_runner(monkeypatch, output="Cookie: split-secret", code=1, stderr="auth_token=secret 403")
    result = await sp.check_platform(store, "bilibili")
    assert result["ok"] is False
    assert "secret" not in json.dumps(result)
    assert "secret" not in json.dumps(store.kv_get(sp.KV_KEY))

@pytest.mark.asyncio
async def test_install_managed_venv_and_disable_during_install(store, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def run(argv, *, timeout, env=None, cwd=None):
        calls.append(argv)
        if "venv" in argv:
            started.set()
            await release.wait()
        if "pip" in argv:
            exe = sp.managed_root(store, "bilibili") / "venv" / "bin" / "bili"
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_text("fixture")
            exe.chmod(0o700)
        return 0, "ok", ""
    monkeypatch.setattr(sp, "_run_cli", run)
    sp.enable_platform(store, "bilibili")
    task = asyncio.create_task(sp.install_platform(store, "bilibili"))
    await started.wait()
    try:
        assert next(r for r in sp.list_platforms(store) if r["id"] == "bilibili")["installing"]
        duplicate = await sp.install_platform(store, "bilibili")
        assert not duplicate["ok"]
        sp.disable_platform(store, "bilibili")
    finally:
        release.set()
    result = await task
    assert result["ok"] and not result["enabled"] and not result["installing"]
    assert calls[0][:3] == [sys.executable, "-m", "venv"]
    pip = next(c for c in calls if "pip" in c)
    assert pip[0].startswith(str(store.path.parent))
    assert "bilibili-cli" in pip and "--isolated" in pip
    assert all("pipx" not in c for c in calls)

@pytest.mark.asyncio
async def test_install_cancel_clears_state_preserves_enable(store, monkeypatch):
    started = asyncio.Event()
    async def run(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(sp, "_run_cli", run)
    sp.enable_platform(store, "youtube")
    task = asyncio.create_task(sp.install_platform(store, "youtube"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = next(r for r in sp.list_platforms(store) if r["id"] == "youtube")
    assert row["enabled"] and not row["installing"] and row["last_error"]

@pytest.mark.asyncio
async def test_stale_executable_path_not_installed(store):
    with store.tx() as conn:
        store.kv_set(conn, sp.KV_KEY, {"bilibili": {"cli_path": "/definitely/missing/bili", "enabled": True}})
    row = next(r for r in sp.list_platforms(store) if r["id"] == "bilibili")
    assert not row["installed"]
    result = await sp.check_platform(store, "bilibili")
    assert not result["ok"]

@pytest.mark.asyncio
async def test_search_all_dict_errors_dedup(store, monkeypatch):
    ready(store, "bilibili")
    sp.enable_platform(store, "twitter")
    calls = fake_runner(monkeypatch, '{"data":[]}')
    errors = {}
    result = await sp.search_all(store, "topic", platforms=["bilibili", "bilibili", "twitter", "unknown"], errors=errors)
    assert result == {"bilibili": []}
    assert set(errors) == {"twitter"}
    assert len(calls) == 1

@pytest.mark.asyncio
async def test_same_platform_searches_serialized(store, monkeypatch):
    ready(store, "bilibili")
    active = maximum = 0
    async def run(*args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
        return 0, "[]", ""
    monkeypatch.setattr(sp, "_run_cli", run)
    await asyncio.gather(*(sp.search(store, "bilibili", "topic") for _ in range(6)))
    assert maximum == 1

@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "cancel", "output"])
async def test_subprocess_terminated_and_reaped(monkeypatch, tmp_path, mode):
    created = []
    original = asyncio.create_subprocess_exec
    async def spawn(*args, **kwargs):
        proc = await original(*args, **kwargs)
        created.append(proc)
        return proc
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    if mode == "output":
        monkeypatch.setattr(sp, "MAX_OUTPUT_BYTES", 1024)
        script = "import os; os.write(1,b'x'*65536); import time; time.sleep(30)"
    else:
        script = "import time; time.sleep(30)"
    task = asyncio.create_task(sp._run_cli([sys.executable, "-c", script], timeout=0.1 if mode == "timeout" else 5, env={"PATH": "/usr/bin:/bin"}, cwd=tmp_path))
    if mode == "cancel":
        await asyncio.sleep(0.05)
        task.cancel()
    exc = {"timeout": asyncio.TimeoutError, "cancel": asyncio.CancelledError, "output": sp.SocialPlatformError}[mode]
    with pytest.raises(exc):
        await task
    assert created and created[0].returncode is not None


@pytest.mark.asyncio
async def test_success_output_cannot_echo_escaped_credentials(store, monkeypatch):
    ready(store, "twitter")
    sp.set_cookie(store, "twitter", "auth_token=fixture-secret; ct0=fixture-csrf")
    # JSON escapes must not bypass redaction after decoding.
    output = '{"ok":true,"data":[{"id":"42","author":{"screenName":"alice"},"text":"\\u0066ixture-secret fixture-csrf"}]}'
    fake_runner(monkeypatch, output)
    rows = await sp.search(store, "twitter", "topic")
    assert rows and "fixture-secret" not in json.dumps(rows)
    assert "fixture-csrf" not in json.dumps(rows)


@pytest.mark.asyncio
async def test_total_search_concurrency_bounded(store, monkeypatch):
    for pid in sp.PLATFORMS:
        ready(store, pid)
        if sp.PLATFORMS[pid].needs_cookie:
            cookie = "auth_token=fixture-auth; ct0=fixture-csrf" if pid == "twitter" else "session=fixture-session"
            sp.set_cookie(store, pid, cookie)
    active = maximum = 0
    async def run(*args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return 0, "[]", ""
    monkeypatch.setattr(sp, "_run_cli", run)
    result = await sp.search_all(store, "topic")
    assert len(result) == 5 and maximum == sp.MAX_PARALLEL


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "exit", "exception"])
async def test_install_failure_is_safe_and_retriable(store, monkeypatch, failure):
    async def run(*args, **kwargs):
        if failure == "timeout":
            raise asyncio.TimeoutError()
        if failure == "exception":
            raise OSError("auth_token=fixture-secret")
        return 1, "fixture-secret", "ct0=fixture-secret"
    monkeypatch.setattr(sp, "_run_cli", run)
    result = await sp.install_platform(store, "youtube")
    assert not result["ok"] and not result["installing"]
    assert "fixture-secret" not in json.dumps(result)
    assert "fixture-secret" not in json.dumps(store.kv_get(sp.KV_KEY))
    again = await sp.install_platform(store, "youtube")
    assert not again["ok"] and not again["installing"]


def test_unknown_platform_and_cookie_controls(store):
    for func in (sp.enable_platform, sp.disable_platform, sp.cookie_status, sp.clear_cookie):
        with pytest.raises(sp.SocialPlatformError):
            func(store, "unknown")
    for cookie in ("x=ok\x00bad", "bad format", "x=", "x=" + "a" * sp.COOKIE_MAX):
        with pytest.raises(sp.SocialPlatformError):
            sp.set_cookie(store, "reddit", cookie)


@pytest.mark.asyncio
async def test_external_symlink_executable_is_rejected(store, tmp_path):
    exe = ready(store, "youtube")
    outside = tmp_path / "outside-executable"
    outside.write_text("#!/bin/sh\nexit 0\n")
    outside.chmod(0o700)
    # Verify the intended managed fixture path before replacement.
    assert exe == sp.managed_root(store, "youtube") / "venv/bin/yt-dlp"
    exe.unlink()
    exe.symlink_to(outside)
    row = next(r for r in sp.list_platforms(store) if r["id"] == "youtube")
    assert not row["installed"]
    with pytest.raises(sp.SocialPlatformError, match="装"):
        await sp.search(store, "youtube", "topic")


@pytest.mark.parametrize("pid,url", [
    ("twitter", "https://evil.example/alice/status/42"),
    ("twitter", "https://x.com/alice/status/not-a-number"),
    ("twitter", "https://x.com.evil.example/alice/status/42"),
    ("youtube", "https://www.youtube.com/watch?v=short"),
    ("youtube", "https://evil.example/watch?v=abc123XYZ_-"),
    ("reddit", "https://www.reddit.com/login"),
    ("bilibili", "https://www.bilibili.com/login"),
    ("xiaohongshu", "https://www.xiaohongshu.com/login"),
])
def test_noncanonical_platform_urls_rejected(pid, url):
    with pytest.raises(sp.SocialPlatformError):
        sp.PLATFORMS[pid].parse_output(json.dumps([{"title": "bad URL", "url": url}]), 3)


def test_unknown_nonempty_schema_is_not_empty_search():
    with pytest.raises(sp.SocialPlatformError):
        sp.PLATFORMS["bilibili"].parse_output('{"data":{"items":[{"unexpected_title":"unknown"}]}}', 3)


@pytest.mark.asyncio
async def test_cli_option_injection_rejected(store, monkeypatch):
    ready(store, "bilibili")
    calls = fake_runner(monkeypatch)
    with pytest.raises(sp.SocialPlatformError):
        await sp.search(store, "bilibili", "--output=/tmp/no")
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["search", "check", "install"])
@pytest.mark.parametrize("gate", ["lock", "semaphore"])
async def test_total_deadline_includes_queue(store, monkeypatch, operation, gate):
    ready(store, "bilibili")
    calls = fake_runner(monkeypatch)
    monkeypatch.setattr(sp, "SEARCH_TIMEOUT_S", 0.01)
    monkeypatch.setattr(sp, "CHECK_TIMEOUT_S", 0.01)
    monkeypatch.setattr(sp, "INSTALL_TIMEOUT_S", 0.01)
    sem, lock = sp._context(store, "bilibili")
    resource, count = (lock, 1) if gate == "lock" else (sem, sp.MAX_PARALLEL)
    for _ in range(count):
        await resource.acquire()
    try:
        if operation == "search":
            with pytest.raises(sp.SocialPlatformError, match="超时"):
                await asyncio.wait_for(sp.search(store, "bilibili", "topic"), 0.3)
        else:
            fn = sp.check_platform if operation == "check" else sp.install_platform
            result = await asyncio.wait_for(fn(store, "bilibili"), 0.3)
            assert not result["ok"] and "超时" in result["error"]
            assert not result["installing"]
        assert not calls
    finally:
        for _ in range(count):
            resource.release()
    # A timed-out waiter must not later start a tool or retain the lock/permit.
    monkeypatch.setattr(sp, "SEARCH_TIMEOUT_S", 1)
    assert await sp.search(store, "bilibili", "topic") == []
    assert len(calls) == 1


# ----------------------------------------------------------------------
# Twitter 网页备选渠道（2026-10-16）：CLI 挂了/没装时走网页搜索（匿名、无凭据）
# ----------------------------------------------------------------------

def web_search_fn(results_by_site=None, error=None, error_by_site=None):
    """假网页搜索 fn(query, *, site, limit) -> list[dict]；录调用。"""
    calls = []

    async def fn(query, *, site, limit):
        calls.append((query, site, limit))
        if error_by_site and site in error_by_site:
            raise error_by_site[site]
        if error is not None:
            raise error
        return list((results_by_site or {}).get(site, []))

    fn.calls = calls
    return fn


# 一个真实量级的 Snowflake ID（推出 2025-10 的时间，落在合理窗口内）
_SNOW_ID = "1979000000000000000"
_SNOW_TS = ((int(_SNOW_ID) >> 22) + 1288834974657) / 1000


def _web_item(url, *, title="推文", snippet="内容", published=None, provider="keenable"):
    return {"title": title, "url": url, "snippet": snippet, "published": published, "provider": provider}


@pytest.mark.asyncio
async def test_twitter_web_normalizes_and_validates_urls():
    fn = web_search_fn({
        "x.com": [
            _web_item(f"https://x.com/alice/status/{_SNOW_ID}", title="X 站的"),
            _web_item("https://x.com/explore", title="非状态路径"),            # 拒：非状态链接
            _web_item("https://evil.example/alice/status/42", title="外站"),   # 拒：别的域名
            _web_item("https://x.com/alice/status/notnum", title="坏 id"),     # 拒：id 非数字
        ],
        "twitter.com": [_web_item(f"https://twitter.com/bob/status/{int(_SNOW_ID) + 1}", title="旧域名的")],
    })
    rows = await sp.search_twitter_web("话题", web_search_fn=fn, limit=10)
    assert [r["url"] for r in rows] == [
        f"https://x.com/alice/status/{_SNOW_ID}",
        f"https://x.com/bob/status/{int(_SNOW_ID) + 1}",  # twitter.com 归一化成 x.com
    ]
    assert all(r["platform"] == "twitter" for r in rows)
    assert all(r["provider"].startswith("social:twitter-web") for r in rows)
    # 两个站点变体都问了（site 限定由 fn 负责）
    assert {c[1] for c in fn.calls} == {"x.com", "twitter.com"}


@pytest.mark.asyncio
async def test_twitter_web_snowflake_published_and_bad_id_none():
    fn = web_search_fn({
        "x.com": [
            _web_item(f"https://x.com/alice/status/{_SNOW_ID}", published=None),
            _web_item("https://x.com/carol/status/99999999999999999999999999", title="超大 id"),
        ],
        "twitter.com": [],
    })
    rows = await sp.search_twitter_web("话题", web_search_fn=fn, limit=10)
    assert len(rows) == 2
    assert rows[0]["published"] == pytest.approx(_SNOW_TS, abs=1)  # Snowflake 推出
    assert rows[1]["published"] is None                            # 推不出就 None，不伪造


@pytest.mark.asyncio
async def test_twitter_web_keeps_engine_published_over_snowflake():
    fn = web_search_fn({"x.com": [_web_item(f"https://x.com/alice/status/{_SNOW_ID}", published=1760000000.0)],
                        "twitter.com": []})
    rows = await sp.search_twitter_web("话题", web_search_fn=fn, limit=10)
    assert rows[0]["published"] == 1760000000.0  # 引擎给了就不推导


@pytest.mark.asyncio
async def test_twitter_web_dedupes_across_site_variants():
    fn = web_search_fn({
        "x.com": [_web_item(f"https://x.com/alice/status/{_SNOW_ID}")],
        "twitter.com": [_web_item(f"https://twitter.com/alice/status/{_SNOW_ID}", title="同一条")],
    })
    rows = await sp.search_twitter_web("话题", web_search_fn=fn, limit=10)
    assert len(rows) == 1 and rows[0]["title"] == "推文"


@pytest.mark.asyncio
async def test_twitter_web_snippet_truncated_and_query_validated():
    fn = web_search_fn({"x.com": [_web_item(f"https://x.com/a/status/{_SNOW_ID}", snippet="长" * 900, title="<b>加粗</b>标题")],
                        "twitter.com": []})
    rows = await sp.search_twitter_web("话题", web_search_fn=fn, limit=10)
    assert len(rows[0]["snippet"]) <= sp.SNIPPET_MAX
    assert "<b>" not in rows[0]["title"]
    for bad in ("", "  ", "-byd", "x" * (sp.QUERY_MAX + 1), "a\nb"):
        with pytest.raises(sp.SocialPlatformError):
            await sp.search_twitter_web(bad, web_search_fn=fn, limit=10)


@pytest.mark.asyncio
async def test_twitter_web_both_sites_fail_raises_safe_error():
    fn = web_search_fn(error=RuntimeError("auth_token=secret-value"))
    with pytest.raises(sp.SocialPlatformError) as err:
        await sp.search_twitter_web("话题", web_search_fn=fn, limit=10)
    assert "secret-value" not in str(err.value)


@pytest.mark.asyncio
async def test_twitter_web_one_site_failing_still_returns_other():
    fn = web_search_fn(
        {"twitter.com": [_web_item(f"https://twitter.com/a/status/{_SNOW_ID}")]},
        error_by_site={"x.com": RuntimeError("boom")},
    )
    rows = await sp.search_twitter_web("话题", web_search_fn=fn, limit=10)
    assert [r["url"] for r in rows] == [f"https://x.com/a/status/{_SNOW_ID}"]


@pytest.mark.asyncio
async def test_search_all_web_fallback_only_when_cli_fails(store, monkeypatch):
    """CLI 成功时绝不调网页渠道。"""
    ready(store, "twitter")
    sp.set_cookie(store, "twitter", "auth_token=fixture-auth; ct0=fixture-csrf")
    fake_runner(monkeypatch, '{"ok":true,"data":[{"id":"%s","author":{"screenName":"alice"},"text":"CLI 的"}]}' % _SNOW_ID)
    fn = web_search_fn(error=AssertionError("CLI 成了就不该走网页"))
    result = await sp.search_all(store, "topic", platforms=["twitter"], web_search_fn=fn)
    assert result["twitter"] and result["twitter"][0]["url"].endswith(f"/status/{_SNOW_ID}")
    assert not fn.calls


@pytest.mark.asyncio
async def test_search_all_web_fallback_when_cli_missing(store):
    """平台启用但 CLI 没装 → 走网页渠道；结果结构与 CLI 路一致（含 platform）。"""
    sp.enable_platform(store, "twitter")
    fn = web_search_fn({"x.com": [_web_item(f"https://x.com/a/status/{_SNOW_ID}")], "twitter.com": []})
    errors = {}
    result = await sp.search_all(store, "topic", platforms=["twitter"], errors=errors, web_search_fn=fn)
    assert not errors
    rows = result["twitter"]
    assert rows[0]["platform"] == "twitter" and rows[0]["url"].endswith(f"/status/{_SNOW_ID}")
    assert rows[0]["provider"].startswith("social:twitter-web")
    assert fn.calls


@pytest.mark.asyncio
async def test_search_all_web_fallback_requires_enabled(store):
    """没启用的平台不走网页渠道（显式点名也一样），错误照旧进 errors。"""
    fn = web_search_fn({"x.com": [_web_item(f"https://x.com/a/status/{_SNOW_ID}")]})
    errors = {}
    result = await sp.search_all(store, "topic", platforms=["twitter"], errors=errors, web_search_fn=fn)
    assert "twitter" not in result and "twitter" in errors
    assert not fn.calls


@pytest.mark.asyncio
async def test_search_all_web_failure_isolated_from_other_platforms(store, monkeypatch):
    """twitter CLI 没装 + 网页渠道也挂 → twitter 进 errors，不影响 bilibili。"""
    ready(store, "bilibili")
    sp.enable_platform(store, "twitter")
    fake_runner(monkeypatch, '{"data":[]}')
    fn = web_search_fn(error=RuntimeError("cookie-secret-detail"))
    errors = {}
    result = await sp.search_all(store, "topic", errors=errors, web_search_fn=fn)
    assert result == {"bilibili": []}
    assert set(errors) == {"twitter"}
    assert "cookie-secret-detail" not in errors["twitter"]


@pytest.mark.asyncio
async def test_web_status_identity_dedup_across_handles():
    fn = web_search_fn({
        "x.com": [_web_item(f"https://x.com/alice/status/{_SNOW_ID}")],
        "twitter.com": [_web_item(f"https://twitter.com/i/web/status/{_SNOW_ID}")],
    })
    assert len(await sp.search_twitter_web("topic", web_search_fn=fn)) == 1


@pytest.mark.asyncio
async def test_twitter_cli_timeout_leaves_room_for_web(store, monkeypatch):
    sp.enable_platform(store, "twitter")
    stopped = asyncio.Event()
    async def slow_cli(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    monkeypatch.setattr(sp, "search", slow_cli)
    monkeypatch.setattr(sp, "TWITTER_CLI_TIMEOUT_S", 0.02, raising=False)
    fn = web_search_fn({"x.com": [_web_item(f"https://x.com/a/status/{_SNOW_ID}")]})
    result = await asyncio.wait_for(sp.search_all(store, "topic", web_search_fn=fn), 0.2)
    assert result["twitter"] and stopped.is_set()


@pytest.mark.asyncio
async def test_twitter_empty_cli_tries_web(store, monkeypatch):
    ready(store, "twitter")
    sp.set_cookie(store, "twitter", "auth_token=fixture-auth; ct0=fixture-csrf")
    fake_runner(monkeypatch, '[]')
    fn = web_search_fn({"x.com": [_web_item(f"https://x.com/a/status/{_SNOW_ID}")]})
    result = await sp.search_all(store, "topic", web_search_fn=fn)
    assert result["twitter"] and len(fn.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('token', ['fixture-long-token', 'fixture%2Dlong%2Dtoken', 'fixture%252Dlong%252Dtoken'])
async def test_encoded_cookie_in_url_is_rejected(store, monkeypatch, token):
    ready(store, "xiaohongshu")
    sp.set_cookie(store, "xiaohongshu", "web_session=fixture-long-token")
    fake_runner(monkeypatch, json.dumps([{
        'title': 'fixture note',
        'url': 'https://www.xiaohongshu.com/explore/65abcdef0123456789abcdef?xsec_token=' + token,
    }]))
    with pytest.raises(sp.SocialPlatformError) as error:
        await sp.search(store, 'xiaohongshu', 'topic')
    assert 'fixture' not in str(error.value) and token not in str(error.value)


@pytest.mark.asyncio
async def test_twitter_web_deadline_keeps_completed_site(monkeypatch):
    stopped = asyncio.Event()
    async def fn(query, *, site, limit):
        if site == "x.com":
            return [_web_item(f"https://x.com/a/status/{_SNOW_ID}")]
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    monkeypatch.setattr(sp, "TWITTER_WEB_TIMEOUT_S", 0.02, raising=False)
    rows = await asyncio.wait_for(sp.search_twitter_web("topic", web_search_fn=fn), 0.2)
    assert rows and stopped.is_set()


@pytest.mark.asyncio
async def test_search_all_without_web_fn_unchanged(store):
    """不注入 web_search_fn：CLI 没装直接进 errors，行为和以前完全一致。"""
    sp.enable_platform(store, "twitter")
    errors = {}
    result = await sp.search_all(store, "topic", platforms=["twitter"], errors=errors)
    assert "twitter" not in result
    assert "装好" in errors["twitter"]


# ----------------------------------------------------------------------
# 2026-10-16 实测修复：rdt-cli 真实信封 + 凭据脱敏短值误伤
# ----------------------------------------------------------------------

# rdt-cli 真实返回信封（已脱敏）：data 里再包一层 {"kind":"Listing","data":{"children":[...]}}，
# 每个 child 是 {"kind":"t3","data":{帖子字段}}。
_RDT_REAL_ENVELOPE = {
    "ok": True, "schema_version": "1",
    "data": {"kind": "Listing", "data": {
        "children": [
            {"kind": "t3", "data": {
                "title": "一个真实帖子", "selftext": "帖子正文讨论",
                "permalink": "/r/test/comments/42/post/", "created_utc": 1760000000,
                "subreddit": "test"}},
        ],
        "after": "t3_x", "dist": 1, "modhash": "sanitized"}},
}


def test_reddit_real_listing_envelope_parses():
    rows = sp.PLATFORMS["reddit"].parse_output(json.dumps(_RDT_REAL_ENVELOPE), 3)
    assert len(rows) == 1
    assert rows[0]["url"] == "https://www.reddit.com/r/test/comments/42/post/"
    assert rows[0]["title"] == "一个真实帖子"
    assert rows[0]["snippet"] == "帖子正文讨论"
    assert rows[0]["published"] == 1760000000


@pytest.mark.asyncio
async def test_short_cookie_values_do_not_mangle_output(store, monkeypatch):
    """rdt 真实 Cookie 里有 "1"/"true" 这类短值：脱敏不许把正常输出里的同文字符串毁掉；
    长 token 照旧遮掉。"""
    ready(store, "reddit")
    sp.set_cookie(store, "reddit", "d=1; over=true; reddit_session=fixture-long-token")
    fake_runner(monkeypatch, json.dumps({"data": [{
        "title": "第 1 次讨论 true 含义", "permalink": "/r/test/comments/42/post/",
        "selftext": "fixture-long-token 出现了，还有 1 个 true 故事", "created_utc": 1760000000}]}))
    rows = await sp.search(store, "reddit", "topic")
    assert rows and "fixture-long-token" not in json.dumps(rows)
    assert "第 1 次讨论 true 含义" == rows[0]["title"]           # 短值不许误伤标题
    assert "还有 1 个 true 故事" in rows[0]["snippet"]           # 短值不许误伤正文
    assert "***" in rows[0]["snippet"]                            # 长 token 被遮


@pytest.mark.asyncio
async def test_cookie_matching_schema_key_does_not_remove_date(store, monkeypatch):
    ready(store, "reddit")
    sp.set_cookie(store, "reddit", "reddit_session=created_utc")
    fake_runner(monkeypatch, json.dumps(_RDT_REAL_ENVELOPE))
    rows = await sp.search(store, "reddit", "topic")
    assert rows[0]["published"] == 1760000000


@pytest.mark.asyncio
async def test_json_escaped_cookie_redacted_after_decoding(store, monkeypatch):
    ready(store, "reddit")
    token = 'fixture-token"quoted'
    sp.set_cookie(store, "reddit", 'reddit_session=' + token)
    fake_runner(monkeypatch, json.dumps({"data": [{
        "title": "safe", "permalink": "/r/test/comments/42/post/",
        "selftext": token}]}))
    rows = await sp.search(store, "reddit", "topic")
    assert rows[0]["snippet"] == "***"


@pytest.mark.asyncio
async def test_html_encoded_cookie_is_redacted_after_normalization(store, monkeypatch):
    ready(store, "reddit")
    sp.set_cookie(store, "reddit", "reddit_session=fixture-long-token")
    fake_runner(monkeypatch, json.dumps({"data": [{
        "title": "safe", "permalink": "/r/test/comments/42/post/",
        "selftext": "fixture&#45;long&#45;token"}]}))
    rows = await sp.search(store, "reddit", "topic")
    assert rows[0]["snippet"] == "***"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["search", "check", "install"])
async def test_queued_cancellation_leaves_no_waiter(store, monkeypatch, operation):
    ready(store, "bilibili")
    calls = fake_runner(monkeypatch)
    _, lock = sp._context(store, "bilibili")
    await lock.acquire()
    fn = {"search": lambda: sp.search(store, "bilibili", "topic"),
          "check": lambda: sp.check_platform(store, "bilibili"),
          "install": lambda: sp.install_platform(store, "bilibili")}[operation]
    task = asyncio.create_task(fn())
    try:
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not calls and not sp.install_in_progress("bilibili", store)
    finally:
        lock.release()
    assert await sp.search(store, "bilibili", "topic") == []
    assert len(calls) == 1

@pytest.mark.asyncio
async def test_install_uses_windows_venv_layout(store, monkeypatch):
    # Windows venvs put executables in venv\Scripts\<name>.exe, not venv/bin/<name>.
    monkeypatch.setattr(sp, "_VENV_BIN", "Scripts")
    monkeypatch.setattr(sp, "_EXE_SUFFIX", ".exe")
    calls = []
    async def run(argv, *, timeout, env=None, cwd=None):
        calls.append((argv, env))
        if "pip" in argv:
            exe = sp.managed_root(store, "bilibili") / "venv" / "Scripts" / "bili.exe"
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_text("fixture")
            exe.chmod(0o700)
        return 0, "ok", ""
    monkeypatch.setattr(sp, "_run_cli", run)
    result = await sp.install_platform(store, "bilibili")
    venv = sp.managed_root(store, "bilibili") / "venv"
    assert result["ok"], result
    pip, env = next(c for c in calls if "pip" in c[0])
    assert pip[0] == str(venv / "Scripts" / "python.exe")
    assert env["PATH"].split(os.pathsep)[0] == str(venv / "Scripts")
    assert result["cli_path"] == str(venv / "Scripts" / "bili.exe")

def test_posix_venv_layout_ignores_windows_paths(store):
    exe = sp.managed_root(store, "youtube") / "venv" / "Scripts" / "yt-dlp.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("fixture")
    exe.chmod(0o700)
    if os.name != "nt":
        assert sp._cli(store, sp.PLATFORMS["youtube"]) == ""
