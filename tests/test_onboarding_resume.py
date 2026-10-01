"""引导能中断续填、准确完成、给出能力清单（docs/13 A04 / A08 / G01 / G02，2026-10）。

- A04：开始引导后（progress 记下当前步），哪怕模型已经配好，刷新也接着显示，回到记下的那一步；
  不能靠「模型已配」推断整个安装完成。老安装（没有记录且模型配好）仍不打扰。
- A08：缺必要条件（模型 / 服务群）时点完成 → 只记「先存下，稍后继续」（later），不记 done，
  usable=False 并列出缺什么、下一步去哪。
- G01/G02：items 清单逐项说明模型 / 群 / 搜索 / 执行 / 交付 / 第一件小事到了哪一步。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_onboarding_web import _login, _start  # noqa: F401  (复用同一套启动夹具)

pytestmark = pytest.mark.asyncio

READY_MODELS = {"base_url": "https://ep.test/v1", "api_key": "sk-t", "main": "m", "worker": "w"}


async def _post(client, **body):
    if body.get("action") in ("progress", "done", "skip") and "run_id" not in body:
        current = await (await client.get("/api/onboarding")).json()
        if current.get("run_id"):
            body.update(run_id=current["run_id"], sequence=current["sequence"] + 1)
    r = await client.post("/api/onboarding", json=body)
    return r.status, await r.json()


async def test_progress_keeps_showing_after_models_ready(tmp_path: Path) -> None:
    app, client = await _start(tmp_path, READY_MODELS)
    try:
        await _login(client)
        # 老安装：没有记录 + 模型配好 → 不打扰
        d = await (await client.get("/api/onboarding")).json()
        assert d["show"] is False
        # 管理员重新引导，走到「群」这步保存
        await _post(client, action="reset")
        st, d = await _post(client, action="progress", step="groups")
        assert st == 200 and d["state"] == "in_progress" and d["step"] == "groups"
        d = await (await client.get("/api/onboarding")).json()
        assert d["show"] is True and d["step"] == "groups"
    finally:
        await client.close(); await app.stop()


async def test_fresh_progress_then_resume_step(tmp_path: Path) -> None:
    app, client = await _start(tmp_path, {})
    try:
        await _login(client)
        await _post(client, action="progress", step="search")
        d = await (await client.get("/api/onboarding")).json()
        assert d["show"] is True and d["step"] == "search"
    finally:
        await client.close(); await app.stop()


async def test_bad_step_rejected(tmp_path: Path) -> None:
    app, client = await _start(tmp_path, {})
    try:
        await _login(client)
        st, _ = await _post(client, action="progress", step="nope")
        assert st == 400
    finally:
        await client.close(); await app.stop()


async def test_done_without_models_is_saved_not_usable(tmp_path: Path) -> None:
    app, client = await _start(tmp_path, {})
    try:
        await _login(client)
        st, d = await _post(client, action="done")
        assert st == 200
        assert d["state"] == "later"
        assert d["usable"] is False
        assert "models" in d["missing"]
        assert d["next_step"] == "models"
        assert d["show"] is False  # 用户说了稍后，不硬弹；设置页另有入口
    finally:
        await client.close(); await app.stop()


async def test_done_with_everything_required_is_done(tmp_path: Path) -> None:
    app, client = await _start(tmp_path, READY_MODELS)
    try:
        await _login(client)
        st, d = await _post(client, action="done")
        assert d["state"] == "done" and d["usable"] is True and d["missing"] == []
    finally:
        await client.close(); await app.stop()


async def test_items_checklist(tmp_path: Path) -> None:
    app, client = await _start(tmp_path, READY_MODELS)
    try:
        await _login(client)
        d = await (await client.get("/api/onboarding")).json()
        keys = [i["key"] for i in d["items"]]
        for k in ("models", "groups", "search", "exec", "delivery", "first"):
            assert k in keys, keys
        by = {i["key"]: i for i in d["items"]}
        for i in d["items"]:
            assert i["state"] in ("ok", "warn", "off", "wait")
            assert i["title"] and i["text"]
        # 模型没验证过：不说「已验证」，如实标注
        assert "未验证" in by["models"]["text"] or "没验证" in by["models"]["text"]
        # 没配公网地址：说清楚只有管理员本机能打开，群友拿不到链接
        assert by["delivery"]["state"] == "warn"
        assert "群友" in by["delivery"]["text"]
        # 搜索没配：说清楚哪些还能用
        assert by["search"]["state"] in ("warn", "off")
        # 新群画像还没成形：第一件小事在等
        assert by["first"]["state"] == "wait"
    finally:
        await client.close(); await app.stop()


async def test_name_only_legacy_record_is_unverified(tmp_path: Path) -> None:
    app, client = await _start(tmp_path, READY_MODELS)
    try:
        await _login(client)
        s = app.models.settings()
        # 旧按名字记录没有配置签名：只说未验证，不猜它属于当前候选或当前端点。
        with app.store.tx() as conn:
            app.store.kv_set(conn, "models.verified.x", {"model": s.main, "ok": True, "tools_ok": True, "ts": 1.0})
        d = await (await client.get("/api/onboarding")).json()
        by = {i["key"]: i for i in d["items"]}
        assert by["models"]["state"] == "warn"
        assert "未验证" in by["models"]["text"]
    finally:
        await client.close(); await app.stop()


async def test_verify_route_records_result(tmp_path: Path) -> None:
    import aiohttp
    import httpx
    from aiohttp.test_utils import TestClient, TestServer
    from fakes import FakeCtx, FakeProfiles
    from test_endpoints_api import _raw_config, _write_plugin_dir

    from CharTyr_MaiWork.maiwork.app import MaiWorkApp

    raw = _raw_config(tmp_path / "data")
    _write_plugin_dir(tmp_path / "plug", raw)
    app = MaiWorkApp(FakeCtx({"config.get": "987654321"}), raw, plugin_dir=tmp_path / "plug")
    app.profiles_cls = FakeProfiles
    await app.start()
    client = TestClient(TestServer(app.console.app), cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    try:
        from test_endpoints_api import PASSWORD as EP_PW
        assert (await client.post("/api/login", json={"password": EP_PW})).status == 200
        r = await client.put("/api/settings/endpoints/default", json={"base_url": "https://ep.test/v1", "api_key": "sk-t"})
        assert r.status == 200, await r.text()
        r = await client.put("/api/settings/model-list/m1", json={"endpoint": "default", "model": "gpt-v"})
        assert r.status == 200, await r.text()
        await client.put("/api/agents/main", json={"model": "m1"})
        app.models._transport = httpx.MockTransport(lambda req: httpx.Response(200, json={
            "id": "x", "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))
        app.models._client = None
        r = await client.post("/api/settings/model-list/m1/verify", json={})
        assert r.status == 200
        d = await r.json()
        assert d["ok"] is True and d["tools_ok"] is False  # 假端点不调工具 → 如实给警告
        d = await (await client.get("/api/onboarding")).json()
        by = {i["key"]: i for i in d["items"]}
        assert by["models"]["state"] == "warn" and "已验证能回答" in by["models"]["text"]
    finally:
        await client.close(); await app.stop()
