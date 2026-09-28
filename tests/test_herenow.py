"""herenow.py 单元测试：全走 httpx.MockTransport，不真的访问 here.now。

接口形状以 docs/06-宿主接口事实.md 末尾「here.now 匿名发布」一节为准：
create → PUT 预签名 → finalize；不用账号凭据；429 带 retry_after。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from CharTyr_MaiWork.herenow import HereNow, HereNowError


def _resp(payload: dict, status: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        headers=headers or {"content-type": "application/json"},
    )


def _create_payload(*, uploads: list[dict] | None = None, finalize_site_url: str = "") -> dict:
    files = uploads or [
        {
            "path": "index.html",
            "method": "PUT",
            "url": "https://up.example/upload/index.html",
            "headers": {"Content-Type": "text/html; charset=utf-8"},
        }
    ]
    return {
        "slug": "test-slug",
        "siteUrl": "https://test-slug.here.now/",
        "upload": {
            "versionId": "v-123",
            "uploads": files,
            "finalizeUrl": "https://here.now/api/v1/finalize/test-slug",
        },
        "claimToken": "tok-abc",
        "claimUrl": "https://here.now/claim/tok-abc",
        "expiresAt": "2026-10-01T12:00:00.000Z",
    }


def _mk_dir(tmp_path: Path, files: dict[str, str | bytes]) -> Path:
    """造一个发布目录：files 的 key 是相对路径（可能含子目录），value 是内容。"""
    d = tmp_path / "site"
    d.mkdir()
    for rel, content in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
    return d


pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------------------
# 正常三步
# ----------------------------------------------------------------------


async def test_publish_three_steps(tmp_path):
    """create（无 Authorization、带 x-herenow-client）→ PUT 预签名（只带返回的头）→ finalize。"""
    d = _mk_dir(tmp_path, {"index.html": "<h1>你好</h1>", "css/style.css": "body{color:red}"})
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/api/v1/publish":
            return _resp(
                _create_payload(
                    uploads=[
                        {
                            "path": "index.html",
                            "method": "PUT",
                            "url": "https://up.example/u/index.html",
                            "headers": {"Content-Type": "text/html; charset=utf-8"},
                        },
                        {
                            "path": "css/style.css",
                            "method": "PUT",
                            "url": "https://up.example/u/style.css",
                            "headers": {"Content-Type": "text/css; charset=utf-8"},
                        },
                    ]
                )
            )
        if request.url.host == "up.example":
            return httpx.Response(200)
        if "finalize" in request.url.path:
            return _resp({"siteUrl": "https://final-test-slug.here.now/"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    hn = HereNow(transport=transport)
    result = await hn.publish(d)

    # create 请求
    create_req = calls[0]
    assert create_req.method == "POST"
    assert create_req.url == "https://here.now/api/v1/publish"
    assert "authorization" not in create_req.headers  # 匿名，不带 Authorization
    assert create_req.headers["x-herenow-client"] == "maiwork/plugin"
    body = json.loads(create_req.content)
    files = {f["path"]: f for f in body["files"]}
    assert set(files) == {"index.html", "css/style.css"}  # 相对路径，用 /
    idx = files["index.html"]
    assert idx["size"] == len("<h1>你好</h1>".encode("utf-8"))
    assert idx["contentType"] == "text/html; charset=utf-8"
    assert idx["hash"] == hashlib.sha256("<h1>你好</h1>".encode("utf-8")).hexdigest()
    css = files["css/style.css"]
    assert css["contentType"] == "text/css; charset=utf-8"
    assert css["hash"] == hashlib.sha256(b"body{color:red}").hexdigest()

    # PUT：两个文件的预签名上传
    puts = [c for c in calls if c.method == "PUT"]
    assert len(puts) == 2
    for p in puts:
        assert "authorization" not in p.headers
    index_put = [p for p in puts if "index.html" in str(p.url)][0]
    # 只带 create 返回的 headers（content-type），不多带别的专有头
    assert index_put.headers["content-type"] == "text/html; charset=utf-8"
    assert index_put.content == "<h1>你好</h1>".encode("utf-8")

    # finalize
    finalize_req = [c for c in calls if "finalize" in str(c.url)][0]
    assert finalize_req.method == "POST"
    assert json.loads(finalize_req.content) == {"versionId": "v-123"}

    # 返回：url 用 finalize 的 siteUrl；claim_url / claim_token 都有
    assert result["url"] == "https://final-test-slug.here.now/"
    assert result["slug"] == "test-slug"
    assert result["claim_url"] == "https://here.now/claim/tok-abc"
    assert result["claim_token"] == "tok-abc"
    # expires_ts = expiresAt 解析出来的 epoch
    assert result["expires_ts"] > 1_700_000_000


async def test_publish_url_fallback_to_create_siteurl(tmp_path):
    """finalize 响应没有 siteUrl 时用 create 的 siteUrl。"""
    d = _mk_dir(tmp_path, {"index.html": "x"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/publish":
            return _resp(_create_payload())
        if request.url.host == "up.example":
            return httpx.Response(200)
        if "finalize" in request.url.path:
            return _resp({"ok": True})
        return httpx.Response(404)

    hn = HereNow(transport=httpx.MockTransport(handler))
    result = await hn.publish(d)
    assert result["url"] == "https://test-slug.here.now/"


async def test_publish_no_expires_at(tmp_path):
    """没有 expiresAt 时用 now+24h。"""
    d = _mk_dir(tmp_path, {"index.html": "x"})
    payload = _create_payload()
    payload.pop("expiresAt")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/publish":
            return _resp(payload)
        if request.url.host == "up.example":
            return httpx.Response(200)
        return _resp({"ok": True})

    hn = HereNow(transport=httpx.MockTransport(handler))
    before = __import__("time").time()
    result = await hn.publish(d)
    after = __import__("time").time()
    assert before + 24 * 3600 - 5 <= result["expires_ts"] <= after + 24 * 3600 + 5


async def test_publish_skips_hidden_files(tmp_path):
    """隐藏文件、._ 开头、.DS_Store 都跳过；子目录里的同样跳过。"""
    d = _mk_dir(
        tmp_path,
        {
            "index.html": "x",
            ".DS_Store": "junk",
            ".hidden": "junk",
            "._meta": "junk",
            "sub/.DS_Store": "junk",
            "sub/page.html": "y",
        },
    )
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/publish":
            body = json.loads(request.content)
            seen.extend(body["files"])
            return _resp(_create_payload(uploads=[]))
        return _resp({"ok": True})

    hn = HereNow(transport=httpx.MockTransport(handler))
    await hn.publish(d)
    paths = {f["path"] for f in seen}
    assert paths == {"index.html", "sub/page.html"}


async def test_publish_unknown_content_type(tmp_path):
    """未知扩展名 → application/octet-stream。"""
    d = _mk_dir(tmp_path, {"data.bin": b"\x01\x02"})
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/publish":
            seen.extend(json.loads(request.content)["files"])
            return _resp(_create_payload(uploads=[]))
        return _resp({"ok": True})

    hn = HereNow(transport=httpx.MockTransport(handler))
    await hn.publish(d)
    assert seen[0]["contentType"] == "application/octet-stream"


# ----------------------------------------------------------------------
# 我们自己的上限（发布前检查，不发出任何请求）
# ----------------------------------------------------------------------


async def test_publish_too_many_files(tmp_path):
    d = tmp_path / "many"
    d.mkdir()
    for i in range(2501):
        (d / f"f{i}.txt").write_text("x", encoding="utf-8")

    called = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(1)
        return _resp({})

    hn = HereNow(transport=httpx.MockTransport(handler))
    with pytest.raises(HereNowError, match="2500|文件太多|太多"):
        await hn.publish(d)
    assert not called  # 一个请求都不发


async def test_publish_file_too_large(tmp_path):
    """单文件超限（>50MB，M3 起单文件上限不得超过总量 200MB）→ 中文报错（不真造大文件，monkeypatch 扫描结果就行）。"""
    d = _mk_dir(tmp_path, {"big.bin": b"x"})

    import CharTyr_MaiWork.herenow as herenow_mod

    real_iter = herenow_mod._iter_files
    big = 251 * 1024 * 1024

    def fake_iter(directory):
        for rel, path, _size in real_iter(directory):
            yield rel, path, big  # 假装这个文件超大

    original = herenow_mod._iter_files
    herenow_mod._iter_files = fake_iter
    try:
        hn = HereNow(transport=httpx.MockTransport(lambda r: _resp({})))
        with pytest.raises(HereNowError, match="250|太大|超过"):
            await hn.publish(d)
    finally:
        herenow_mod._iter_files = original


async def test_publish_total_too_large(tmp_path):
    """总量 >200MB → 中文报错。"""
    d = _mk_dir(tmp_path, {"a.bin": b"x", "b.bin": b"y"})

    import CharTyr_MaiWork.herenow as herenow_mod

    real_iter = herenow_mod._iter_files

    def fake_iter(directory):
        for rel, path, _size in real_iter(directory):
            yield rel, path, 150 * 1024 * 1024  # 每个 150MB，总量 300MB

    original = herenow_mod._iter_files
    herenow_mod._iter_files = fake_iter
    try:
        hn = HereNow(transport=httpx.MockTransport(lambda r: _resp({})))
        with pytest.raises(HereNowError, match="200|太大|超过"):
            await hn.publish(d)
    finally:
        herenow_mod._iter_files = original


# ----------------------------------------------------------------------
# 错误处理
# ----------------------------------------------------------------------


async def test_publish_429_with_retry_after(tmp_path):
    d = _mk_dir(tmp_path, {"index.html": "x"})

    def handler(request: httpx.Request) -> httpx.Response:
        return _resp(
            {"error": "rate_limited", "message": "too many", "retry_after": 17},
            status=429,
        )

    hn = HereNow(transport=httpx.MockTransport(handler))
    with pytest.raises(HereNowError) as ei:
        await hn.publish(d)
    assert ei.value.retry_after == 17
    assert "429" in str(ei.value)


async def test_publish_missing_files(tmp_path):
    """finalize 返回 400 missingFiles → 中文「上传没到位」。"""
    d = _mk_dir(tmp_path, {"index.html": "x"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/publish":
            return _resp(_create_payload())
        if request.url.host == "up.example":
            return httpx.Response(200)
        if "finalize" in request.url.path:
            return _resp({"error": "missingFiles", "message": "files not uploaded"}, status=400)
        return httpx.Response(404)

    hn = HereNow(transport=httpx.MockTransport(handler))
    with pytest.raises(HereNowError, match="上传没到位"):
        await hn.publish(d)


async def test_publish_other_error_includes_status_and_body(tmp_path):
    """其他错误：带状态码和 error/message 字段（截 300）。"""
    d = _mk_dir(tmp_path, {"index.html": "x"})

    def handler(request: httpx.Request) -> httpx.Response:
        return _resp({"error": "broken", "message": "服务器冒烟了 " + "长" * 500}, status=500)

    hn = HereNow(transport=httpx.MockTransport(handler))
    with pytest.raises(HereNowError) as ei:
        await hn.publish(d)
    msg = str(ei.value)
    assert "500" in msg
    assert "服务器冒烟了" in msg
    assert len(msg) < 500  # 截断过


async def test_publish_upload_retry_once(tmp_path):
    """PUT 上传失败重试 1 次（预签名幂等）；两次都失败才报错。"""
    d = _mk_dir(tmp_path, {"index.html": "x"})
    put_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/publish":
            return _resp(_create_payload())
        if request.url.host == "up.example":
            put_calls["n"] += 1
            if put_calls["n"] == 1:
                return httpx.Response(500, text="boom")
            return httpx.Response(200)  # 第二次成功
        return _resp({"ok": True})

    hn = HereNow(transport=httpx.MockTransport(handler))
    result = await hn.publish(d)
    assert put_calls["n"] == 2  # 失败 1 次 + 重试 1 次
    assert result["url"]


async def test_publish_upload_retry_exhausted(tmp_path):
    """PUT 连续两次都失败 → HereNowError。"""
    d = _mk_dir(tmp_path, {"index.html": "x"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/publish":
            return _resp(_create_payload())
        if request.url.host == "up.example":
            return httpx.Response(500, text="boom")
        return _resp({"ok": True})

    hn = HereNow(transport=httpx.MockTransport(handler))
    with pytest.raises(HereNowError):
        await hn.publish(d)


async def test_publish_custom_client_header(tmp_path):
    """client_header 可配置。"""
    d = _mk_dir(tmp_path, {"index.html": "x"})
    seen_headers: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/publish":
            seen_headers.append(request.headers.get("x-herenow-client", ""))
            return _resp(_create_payload(uploads=[]))
        return _resp({"ok": True})

    hn = HereNow(transport=httpx.MockTransport(handler), client_header="maiwork/test")
    await hn.publish(d)
    assert seen_headers == ["maiwork/test"]
