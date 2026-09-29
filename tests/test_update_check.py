"""更新提醒：查 GitHub 上的最新版本号，只提醒不自动更新。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from CharTyr_MaiWork.maiwork import update_check as uc


def _manifest(v: str) -> bytes:
    return json.dumps({"id": "chartyr.maiwork", "version": v}).encode()


def _commits(msg: str) -> bytes:
    return json.dumps([{"commit": {"message": msg}}]).encode()


def _transport(routes: dict[str, tuple[int, bytes]], hits: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if hits is not None:
            hits.append(url)
        for key, (code, body) in routes.items():
            if key in url:
                return httpx.Response(code, content=body)
        return httpx.Response(404, content=b"")

    return httpx.MockTransport(handler)


def test_parse_version():
    assert uc.parse_version("0.4.3") == (0, 4, 3)
    assert uc.parse_version("v1.2") == (1, 2)
    assert uc.parse_version("abc") is None
    assert uc.parse_version("") is None


def test_newer_found_with_notes():
    t = _transport({
        "raw.githubusercontent.com": (200, _manifest("0.4.5")),
        "api.github.com": (200, _commits("0.4.5：资讯相关度放宽、更新提醒\n\n细节……")),
    })
    chk = uc.UpdateCheck("0.4.3", transport=t, now=lambda: 1000.0)
    st = asyncio.run(chk.refresh())
    assert st["latest"] == "0.4.5"
    assert st["current"] == "0.4.3"
    assert st["newer"] is True
    assert st["notes"] == "0.4.5：资讯相关度放宽、更新提醒"
    assert st["checked_ts"] == 1000.0
    assert st["error"] == ""


def test_same_or_older_is_not_newer():
    for v in ("0.4.3", "0.4.2"):
        t = _transport({"raw.githubusercontent.com": (200, _manifest(v))})
        st = asyncio.run(uc.UpdateCheck("0.4.3", transport=t, now=lambda: 1.0).refresh())
        assert st["newer"] is False, v


def test_notes_only_when_commit_matches_version():
    # 最新提交不是这个版本的发版说明 → 不拿它当更新说明
    t = _transport({
        "raw.githubusercontent.com": (200, _manifest("0.4.5")),
        "api.github.com": (200, _commits("docs: typo")),
    })
    st = asyncio.run(uc.UpdateCheck("0.4.3", transport=t, now=lambda: 1.0).refresh())
    assert st["newer"] is True
    assert st["notes"] == ""


def test_falls_back_to_mirror():
    t = _transport({
        "raw.githubusercontent.com": (500, b""),
        "cdn.jsdelivr.net": (200, _manifest("0.5.0")),
    })
    st = asyncio.run(uc.UpdateCheck("0.4.3", transport=t, now=lambda: 1.0).refresh())
    assert st["latest"] == "0.5.0" and st["newer"] is True


def test_all_fail_is_quiet_and_keeps_old_result():
    good = _transport({"raw.githubusercontent.com": (200, _manifest("0.4.5"))})
    chk = uc.UpdateCheck("0.4.3", transport=good, now=lambda: 1.0)
    asyncio.run(chk.refresh())
    chk._transport = _transport({})  # 全挂
    st = asyncio.run(chk.refresh())
    assert st["latest"] == "0.4.5" and st["newer"] is True
    assert st["error"]  # 记下没查到，但不清掉上次结果


def test_rejects_wrong_plugin_id():
    body = json.dumps({"id": "someone.else", "version": "9.9.9"}).encode()
    t = _transport({"raw.githubusercontent.com": (200, body), "cdn.jsdelivr.net": (200, body)})
    st = asyncio.run(uc.UpdateCheck("0.4.3", transport=t, now=lambda: 1.0).refresh())
    assert st["newer"] is False and st["latest"] == ""


def test_status_refreshes_only_when_stale():
    hits: list[str] = []
    clock = [10_000.0]
    t = _transport({"raw.githubusercontent.com": (200, _manifest("0.4.5"))}, hits)
    chk = uc.UpdateCheck("0.4.3", transport=t, now=lambda: clock[0])

    async def go():
        await chk.maybe_refresh()
        n1 = len([h for h in hits if "raw." in h])
        await chk.maybe_refresh()  # 刚查过，不再查
        n2 = len([h for h in hits if "raw." in h])
        clock[0] += uc.CHECK_INTERVAL_S + 1
        await chk.maybe_refresh()
        n3 = len([h for h in hits if "raw." in h])
        return n1, n2, n3

    assert asyncio.run(go()) == (1, 1, 2)


def test_failed_check_retries_sooner_but_not_every_call():
    hits: list[str] = []
    clock = [10_000.0]
    t = _transport({}, hits)
    chk = uc.UpdateCheck("0.4.3", transport=t, now=lambda: clock[0])

    async def go():
        await chk.maybe_refresh()
        a = len(hits)
        await chk.maybe_refresh()
        b = len(hits)
        clock[0] += uc.RETRY_AFTER_FAIL_S + 1
        await chk.maybe_refresh()
        return a, b, len(hits)

    a, b, c = asyncio.run(go())
    assert a > 0 and b == a and c > b


def test_disabled_never_fetches():
    hits: list[str] = []
    t = _transport({"raw.githubusercontent.com": (200, _manifest("0.4.5"))}, hits)
    chk = uc.UpdateCheck("0.4.3", transport=t, now=lambda: 1.0, enabled=lambda: False)
    st = asyncio.run(chk.maybe_refresh())
    assert hits == [] and st["newer"] is False and st["enabled"] is False


def test_local_version_matches_manifest():
    from pathlib import Path

    mf = Path(uc.__file__).resolve().parents[1] / "_manifest.json"
    assert uc.local_version() == json.loads(mf.read_text(encoding="utf-8"))["version"]
    assert uc.local_version(Path("/nonexistent/_manifest.json")) == ""
