"""generate_image 工具（tools_imagegen.py）测试：路径 / 范围 / SSRF / 大小 / 记账。

全部用 httpx.MockTransport 打桩，**不发真网络请求**，**不打印密钥**。
先写测试再写实现。
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest

from CharTyr_MaiWork.maiwork import imagegen as imagegen_mod
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.environments.local import LocalEnv
from CharTyr_MaiWork.maiwork.specialists import _SPECIALIST_DEFAULT_TOOLS
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import ToolContext, Tools
from CharTyr_MaiWork.maiwork.tools_imagegen import register_image_tools

KEY = "sk-imagegen-DO-NOT-LEAK-9876"  # 真实密钥是 ASCII；这里也只放 ASCII（HTTP 头不能塞中文）
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 128
PUBLIC_DNS = lambda host: ["93.184.216.34"]


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


IMG_ENTRY = {"id": "img1", "endpoint": "default", "model": "gpt-image-1", "name": "生图",
             "use": "image", "image_api": "openai", "image_max_n": 2}
CHAT_ENTRY = {"id": "m1", "endpoint": "default", "model": "gpt-x"}


def _settings(model_list, *, api_key: str = KEY, root: Path):
    ep = {"id": "default", "name": "默认", "protocol": "openai",
          "base_url": "https://api.test/v1", "api_key": api_key}
    raw: dict = {
        "endpoints": [ep], "model_list": model_list,
        "environments": {"local_mode": "direct", "workspace_root": str(root / "ws-root")},
    }
    settings, problems = load_settings(raw)
    assert not problems, problems
    return settings


class _Fake:
    """假 HTTP：POST 返回固定生图结果，GET 返回固定图片字节；记录每一条请求。"""

    def __init__(self, post: httpx.Response | None = None, get: httpx.Response | None = None) -> None:
        self.post = post if post is not None else httpx.Response(200, json={"data": [{"b64_json": _b64(PNG)}]})
        self.get = get if get is not None else httpx.Response(
            200, content=PNG, headers={"content-type": "image/png"}
        )
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return self.post if request.method == "POST" else self.get

    def posts(self) -> list[httpx.Request]:
        return [r for r in self.seen if r.method == "POST"]

    def gets(self) -> list[httpx.Request]:
        return [r for r in self.seen if r.method == "GET"]


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _make(store: Store, root: Path, model_list, fake, *, resolver=None, api_key: str = KEY):
    settings = _settings(model_list, api_key=api_key, root=root)
    env = LocalEnv(lambda: settings)
    tools = Tools(store)
    register_image_tools(
        tools, get_settings=lambda: settings, env=env, store=store,
        http_transport=httpx.MockTransport(fake), resolver=resolver or PUBLIC_DNS,
    )
    return tools, env, settings


def _ctx(env: LocalEnv, **over) -> ToolContext:
    base = dict(group_id="g1", task_id="T-1", actor="子 agent #1", role="worker",
                workspace=env.workspace("ws-1"), artifact_scope=("artifacts/T-1",))
    base.update(over)
    return ToolContext(**base)


def _calls(store: Store, tool: str) -> list[dict]:
    rows = store.read().execute(
        "SELECT tool, ok, error, input, output FROM tool_calls WHERE tool=? ORDER BY id", (tool,)
    ).fetchall()
    return [dict(r) for r in rows]


class TestNotConfigured:
    @pytest.mark.asyncio
    async def test_no_image_model_configured(self, store, tmp_path) -> None:
        fake = _Fake()
        tools, env, _ = _make(store, tmp_path, [CHAT_ENTRY], fake)
        r = await tools.call("generate_image", {"prompt": "画只猫"}, _ctx(env))
        assert not r.ok
        assert "生图模型" in r.error
        assert fake.seen == []  # 没配就不发请求

    @pytest.mark.asyncio
    async def test_image_entry_without_key_reports_not_configured(self, store, tmp_path) -> None:
        fake = _Fake()
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake, api_key="")
        r = await tools.call("generate_image", {"prompt": "画只猫"}, _ctx(env))
        assert not r.ok and "生图模型" in r.error
        assert fake.seen == []

    @pytest.mark.asyncio
    async def test_empty_prompt_rejected(self, store, tmp_path) -> None:
        fake = _Fake()
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)
        r = await tools.call("generate_image", {"prompt": "  "}, _ctx(env))
        assert not r.ok and "prompt" in r.error
        assert fake.seen == []

    @pytest.mark.asyncio
    async def test_no_workspace_rejected(self, store, tmp_path) -> None:
        fake = _Fake()
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)
        r = await tools.call("generate_image", {"prompt": "画只猫"}, _ctx(env, workspace=None))
        assert not r.ok
        assert fake.seen == []


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_b64_saved_under_task_images(self, store, tmp_path) -> None:
        fake = _Fake()
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)
        ctx = _ctx(env)
        r = await tools.call("generate_image", {"prompt": "一只红熊猫", "filename": "hero"}, ctx)
        assert r.ok, r.error
        rel = r.data["files"][0]
        assert rel == "artifacts/T-1/images/hero.png"
        assert env.resolve("ws-1", rel).read_bytes() == PNG
        assert r.data["count"] == 1
        assert r.data["model"] == "gpt-image-1"
        assert r.data["elapsed_ms"] >= 0
        assert rel in r.output
        # 请求形状：POST 到 /images/generations，密钥只在头里
        req = fake.posts()[0]
        assert str(req.url) == "https://api.test/v1/images/generations"
        assert req.headers["authorization"] == f"Bearer {KEY}"
        body = req.content.decode()
        assert "一只红熊猫" in body and KEY not in body

    @pytest.mark.asyncio
    async def test_url_form_downloads_immediately(self, store, tmp_path) -> None:
        fake = _Fake(post=httpx.Response(200, json={"data": [{"url": "https://img.test/a.jpg"}]}),
                     get=httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}))
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)
        r = await tools.call("generate_image", {"prompt": "风景"}, _ctx(env))
        assert r.ok, r.error
        rel = r.data["files"][0]
        assert rel.endswith(".jpg"), rel
        assert env.resolve("ws-1", rel).read_bytes() == JPEG
        assert [str(g.url) for g in fake.gets()] == ["https://img.test/a.jpg"]

    @pytest.mark.asyncio
    async def test_default_size_from_entry_used(self, store, tmp_path) -> None:
        entry = dict(IMG_ENTRY, image_default_size="1024x1536")
        fake = _Fake()
        tools, env, _ = _make(store, tmp_path, [entry], fake)
        await tools.call("generate_image", {"prompt": "画只猫"}, _ctx(env))
        assert "1024x1536" in fake.posts()[0].content.decode()

    @pytest.mark.asyncio
    async def test_explicit_size_wins(self, store, tmp_path) -> None:
        entry = dict(IMG_ENTRY, image_default_size="1024x1536")
        fake = _Fake()
        tools, env, _ = _make(store, tmp_path, [entry], fake)
        await tools.call("generate_image", {"prompt": "画只猫", "size": "512x512"}, _ctx(env))
        body = fake.posts()[0].content.decode()
        assert "512x512" in body and "1024x1536" not in body

    @pytest.mark.asyncio
    async def test_siliconflow_entry_uses_its_shape(self, store, tmp_path) -> None:
        entry = dict(IMG_ENTRY, image_api="siliconflow", model="Kwai-Kolors/Kolors")
        fake = _Fake(post=httpx.Response(200, json={"images": [{"url": "https://sf.test/a.png"}]}))
        tools, env, _ = _make(store, tmp_path, [entry], fake)
        r = await tools.call("generate_image", {"prompt": "海岛", "size": "1024x768"}, _ctx(env))
        assert r.ok, r.error
        body = fake.posts()[0].content.decode()
        assert "image_size" in body and "batch_size" in body
        assert env.resolve("ws-1", r.data["files"][0]).read_bytes() == PNG

    @pytest.mark.asyncio
    async def test_duplicate_filename_gets_v2(self, store, tmp_path) -> None:
        fake = _Fake()
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)
        ctx = _ctx(env)
        r1 = await tools.call("generate_image", {"prompt": "猫", "filename": "hero.png"}, ctx)
        r2 = await tools.call("generate_image", {"prompt": "猫", "filename": "hero.png"}, ctx)
        assert r1.ok and r2.ok
        assert r1.data["files"][0] == "artifacts/T-1/images/hero.png"
        assert r2.data["files"][0] == "artifacts/T-1/images/hero-v2.png"

    @pytest.mark.asyncio
    async def test_n_more_than_one_writes_each(self, store, tmp_path) -> None:
        fake = _Fake(post=httpx.Response(200, json={"data": [
            {"b64_json": _b64(PNG)}, {"b64_json": _b64(JPEG)},
        ]}))
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)
        r = await tools.call("generate_image", {"prompt": "两只猫", "n": 2}, _ctx(env))
        assert r.ok, r.error
        assert r.data["count"] == 2
        assert sorted(Path(f).suffix for f in r.data["files"]) == [".jpg", ".png"]
        for rel in r.data["files"]:
            assert env.resolve("ws-1", rel).is_file()


class TestPathAndScopeGuards:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", [
        "../../etc/passwd.png", "/etc/passwd.png", "..\\..\\x.png",
        "a/../../b.png", "sub/dir/hero.png", ".hidden", "..", "x" * 200,
    ])
    async def test_bad_filename_rejected(self, store, tmp_path, name) -> None:
        fake = _Fake()
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)
        r = await tools.call("generate_image", {"prompt": "猫", "filename": name}, _ctx(env))
        assert not r.ok, name
        assert fake.seen == []

    @pytest.mark.asyncio
    async def test_odd_chars_sanitized(self, store, tmp_path) -> None:
        fake = _Fake()
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)
        r = await tools.call("generate_image", {"prompt": "猫", "filename": "hero image!.png"}, _ctx(env))
        assert r.ok, r.error
        assert r.data["files"][0] == "artifacts/T-1/images/hero_image.png"

    @pytest.mark.asyncio
    async def test_artifact_scope_outside_rejected(self, store, tmp_path) -> None:
        fake = _Fake()
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)
        r = await tools.call("generate_image", {"prompt": "猫"}, _ctx(env, artifact_scope=("artifacts/T-9",)))
        assert not r.ok
        assert "artifacts/T-9" in r.error
        assert fake.seen == []

    @pytest.mark.asyncio
    async def test_no_task_id_rejected(self, store, tmp_path) -> None:
        fake = _Fake()
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)
        r = await tools.call("generate_image", {"prompt": "猫"}, _ctx(env, task_id=""))
        assert not r.ok
        assert fake.seen == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tid", ["../../x", "a/b", "T 1"])
    async def test_bad_task_id_rejected(self, store, tmp_path, tid) -> None:
        fake = _Fake()
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)
        r = await tools.call("generate_image", {"prompt": "猫"}, _ctx(env, task_id=tid))
        assert not r.ok
        assert fake.seen == []


class TestSsrFGuards:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", [
        "https://127.0.0.1/x.png",
        "https://169.254.169.254/latest/meta-data/x.png",
        "https://localhost/x.png",
        "http://127.0.0.1:8080/x.png",
        "file:///etc/passwd",
    ])
    async def test_internal_image_url_rejected(self, store, tmp_path, url) -> None:
        fake = _Fake(post=httpx.Response(200, json={"data": [{"url": url}]}))
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)
        r = await tools.call("generate_image", {"prompt": "猫"}, _ctx(env))
        assert not r.ok, url
        assert fake.gets() == []  # 内网地址一个请求都不发
        assert "内网" in r.error or "http" in r.error

    @pytest.mark.asyncio
    async def test_private_dns_name_rejected(self, store, tmp_path) -> None:
        fake = _Fake(post=httpx.Response(200, json={"data": [{"url": "https://img.internal.test/x.png"}]}))
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake, resolver=lambda host: ["10.1.2.3"])
        r = await tools.call("generate_image", {"prompt": "猫"}, _ctx(env))
        assert not r.ok
        assert fake.gets() == []

    @pytest.mark.asyncio
    async def test_redirect_to_internal_rejected(self, store, tmp_path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json={"data": [{"url": "https://img.test/a.png"}]})
            return httpx.Response(302, headers={"location": "https://127.0.0.1/evil.png"})

        settings = _settings([IMG_ENTRY], root=tmp_path)
        env = LocalEnv(lambda: settings)
        tools = Tools(store)
        register_image_tools(tools, get_settings=lambda: settings, env=env, store=store,
                             http_transport=httpx.MockTransport(handler), resolver=PUBLIC_DNS)
        r = await tools.call("generate_image", {"prompt": "猫"}, _ctx(env))
        assert not r.ok
        assert "内网" in r.error


class TestLimitsAndErrors:
    @pytest.mark.asyncio
    async def test_n_over_entry_limit_rejected(self, store, tmp_path) -> None:
        fake = _Fake()
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)  # image_max_n = 2
        r = await tools.call("generate_image", {"prompt": "猫", "n": 3}, _ctx(env))
        assert not r.ok
        assert "2" in r.error
        assert fake.seen == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [0, -1, "x"])
    async def test_n_not_positive_rejected(self, store, tmp_path, bad) -> None:
        fake = _Fake()
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)
        r = await tools.call("generate_image", {"prompt": "猫", "n": bad}, _ctx(env))
        assert not r.ok, bad
        assert fake.seen == []

    @pytest.mark.asyncio
    async def test_image_too_large_rejected(self, store, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(imagegen_mod, "MAX_IMAGE_BYTES", 16)
        fake = _Fake(get=httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"x" * 200))
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)
        r = await tools.call("generate_image", {"prompt": "猫", "filename": "big"}, _ctx(env))
        assert not r.ok
        assert "太大" in r.error

    @pytest.mark.asyncio
    async def test_b64_too_large_rejected(self, store, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(imagegen_mod, "MAX_IMAGE_BYTES", 16)
        fake = _Fake(post=httpx.Response(200, json={
            "data": [{"b64_json": _b64(b"\x89PNG\r\n\x1a\n" + b"x" * 200)}]
        }))
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)
        r = await tools.call("generate_image", {"prompt": "猫"}, _ctx(env))
        assert not r.ok
        assert "太大" in r.error

    @pytest.mark.asyncio
    async def test_non_image_payload_rejected(self, store, tmp_path) -> None:
        fake = _Fake(post=httpx.Response(200, json={"data": [{"b64_json": _b64(b"<html>no</html>")}]}))
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)
        r = await tools.call("generate_image", {"prompt": "猫"}, _ctx(env))
        assert not r.ok
        assert "图片" in r.error

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403, 429, 500])
    async def test_http_error_is_chinese_and_key_free(self, store, tmp_path, status) -> None:
        fake = _Fake(post=httpx.Response(status, text=f"bad key {KEY}"))
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)
        r = await tools.call("generate_image", {"prompt": "猫"}, _ctx(env))
        assert not r.ok
        assert KEY not in r.error
        assert str(status) in r.error
        rows = _calls(store, "generate_image")
        assert rows and rows[-1]["ok"] == 0
        assert KEY not in (rows[-1]["error"] or "")

    @pytest.mark.asyncio
    async def test_network_error_is_chinese(self, store, tmp_path) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        settings = _settings([IMG_ENTRY], root=tmp_path)
        env = LocalEnv(lambda: settings)
        tools = Tools(store)
        register_image_tools(tools, get_settings=lambda: settings, env=env, store=store,
                             http_transport=httpx.MockTransport(boom), resolver=PUBLIC_DNS)
        r = await tools.call("generate_image", {"prompt": "猫"}, _ctx(env))
        assert not r.ok
        assert r.error and "失败" in r.error

    @pytest.mark.asyncio
    async def test_unknown_image_api_from_stale_config(self, store, tmp_path) -> None:
        """配置被手改成不认识的值：明确报错，别拿 openai 形状硬发。"""
        fake = _Fake()
        settings = _settings([IMG_ENTRY], root=tmp_path)
        object.__setattr__(settings.model_list[0], "image_api", "gemini")
        env = LocalEnv(lambda: settings)
        tools = Tools(store)
        register_image_tools(tools, get_settings=lambda: settings, env=env, store=store,
                             http_transport=httpx.MockTransport(fake), resolver=PUBLIC_DNS)
        r = await tools.call("generate_image", {"prompt": "猫"}, _ctx(env))
        assert not r.ok
        assert fake.seen == []


class TestAccounting:
    @pytest.mark.asyncio
    async def test_counter_counts_success_and_failure(self, store, tmp_path) -> None:
        fake = _Fake(post=httpx.Response(200, json={"data": [
            {"b64_json": _b64(PNG)}, {"b64_json": _b64(JPEG)},
        ]}))
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)
        ctx = _ctx(env)
        await tools.call("generate_image", {"prompt": "猫", "n": 2}, ctx)
        fake.post = httpx.Response(500, text="boom")
        await tools.call("generate_image", {"prompt": "猫"}, ctx)
        stat = store.kv_get("imagegen.calls")
        assert isinstance(stat, dict), stat
        assert stat["total"] == 2
        assert stat["ok"] == 1
        assert stat["failed"] == 1
        assert stat["images"] == 2
        assert stat["last_ok"] is False

    @pytest.mark.asyncio
    async def test_tool_calls_row_has_no_secret_or_temp_url(self, store, tmp_path) -> None:
        fake = _Fake(post=httpx.Response(200, json={
            "data": [{"url": "https://img.test/tmp-secret-token.png"}]
        }))
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)
        r = await tools.call("generate_image", {"prompt": "猫", "filename": "hero"}, _ctx(env))
        assert r.ok
        rows = _calls(store, "generate_image")
        blob = " ".join(str(x) for row in rows for x in row.values())
        assert KEY not in blob
        assert "tmp-secret-token" not in blob


class TestRegistration:
    def test_registered_for_worker_only(self, store, tmp_path) -> None:
        fake = _Fake()
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)
        assert tools.get("generate_image", "worker") is not None
        assert tools.get("generate_image", "main") is None

    def test_worker_job_tool_names_includes_it(self, store, tmp_path) -> None:
        from CharTyr_MaiWork.maiwork.coordinator import worker_job_tool_names

        fake = _Fake()
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)
        assert "generate_image" in worker_job_tool_names(tools)

    def test_news_idea_goal_whitelist_excludes_it(self) -> None:
        """只读调研岗不给生图（有意收紧）：要配图得显式改白名单。"""
        for kind in ("news", "idea", "goal"):
            wl = _SPECIALIST_DEFAULT_TOOLS[kind]
            assert wl is None or "generate_image" not in wl

    @pytest.mark.asyncio
    async def test_hard_allowlist_blocks_it_for_readonly_specialist(self, store, tmp_path) -> None:
        fake = _Fake()
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)
        ctx = _ctx(env, allowed_tools=_SPECIALIST_DEFAULT_TOOLS["news"], agent_type="news")
        r = await tools.call("generate_image", {"prompt": "猫"}, ctx)
        assert not r.ok
        assert "名单" in r.error
        assert fake.seen == []

    def test_task_specialist_not_restricted(self) -> None:
        assert _SPECIALIST_DEFAULT_TOOLS["task"] is None

    def test_tool_schema_names(self, store, tmp_path) -> None:
        fake = _Fake()
        tools, env, _ = _make(store, tmp_path, [IMG_ENTRY], fake)
        spec = tools.specs("worker", ["generate_image"])[0]["function"]
        assert set(spec["parameters"]["properties"]) == {"prompt", "size", "n", "filename"}
        assert spec["parameters"]["required"] == ["prompt"]
        assert spec["description"]
