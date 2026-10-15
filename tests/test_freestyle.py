"""environments/freestyle.py + tools_railway.py 在 Freestyle 上的行为（2026-10-15）。

调研依据：.trellis/tasks/10-15-viz-component-ui/research/freestyle-vm.md §3.6。
**全部注入 httpx.MockTransport**：一条真实请求都不发，也不碰真密钥（AGENTS.md 红线）。

覆盖：acquire（请求体 / 轮询 / 429 / 409 / 401 / 超时 / 无密钥不发请求）、
每日配额、同时 1 台、run（夹时限 / 退出码 / 超时）、put（小文件 / 大文件分片）、
get（stat + Range 续传 / 二进制）、release（DELETE 幂等）、到期护栏、密钥不外泄。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.environments import freestyle as fs
from CharTyr_MaiWork.maiwork.environments.freestyle import FreestyleBox, FreestyleEnv
from CharTyr_MaiWork.maiwork.store import Store

pytestmark = pytest.mark.asyncio

BJ = timezone(timedelta(hours=8))
NOW = 1_790_000_000.0
KEY = "fs-test-key-abc123"
CREATED_ISO = clock.bj(NOW).astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
VM_ID = "vm-11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clock, "now", lambda: NOW)
    # 绝不读真实环境变量里的密钥
    monkeypatch.delenv("FREESTYLE_API_KEY", raising=False)


# ----------------------------------------------------------------------
# 假 Freestyle API（按「方法 + 路径」路由，录下每次请求）
# ----------------------------------------------------------------------


class FakeAPI:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = {}

    def route(self, method: str, path: str, fn: Callable[[httpx.Request], httpx.Response]) -> "FakeAPI":
        self.routes[(method.upper(), path)] = fn
        return self

    def json_route(self, method: str, path: str, status: int, payload: Any) -> "FakeAPI":
        return self.route(method, path, lambda _r: httpx.Response(status, json=payload))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        fn = self.routes.get((request.method.upper(), request.url.path))
        if fn is None:
            return httpx.Response(404, json={"code": "NOT_FOUND", "message": f"没这条路由：{request.url.path}"})
        return fn(request)

    # 便捷断言 --------------------------------------------------------

    def of(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method.upper() == method.upper() and r.url.path == path]

    def paths(self) -> list[tuple[str, str]]:
        return [(r.method.upper(), r.url.path) for r in self.requests]


def _vm(state: str = "starting", **over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": VM_ID,
        "state": state,
        "slug": "maiwork-job-a-abcdef",
        "createdAt": CREATED_ISO,
        "resources": {"cpu": 2, "memory": 4096, "storage": 16384},
        "ttlSeconds": 3600,
        "autoDeleteSeconds": 0,
        "idleTimeoutSeconds": 600,
    }
    data.update(over)
    return data


def _settings(tmp_path: Path, cfg: dict | None = None) -> Any:
    raw = {
        "environments": {
            "vm_platform": "freestyle", "freestyle": True,
            "freestyle_api_key": KEY,
            "workspace_root": str(tmp_path / "ws"),
        }
    }
    if cfg:
        raw["environments"].update(cfg)
    s, _ = load_settings(raw)
    return s


async def _no_sleep(_seconds: float) -> None:
    return None


def _make_env(
    tmp_path: Path,
    api: FakeAPI,
    *,
    cfg: dict | None = None,
    store: Store | None = None,
    **kw: Any,
) -> tuple[FreestyleEnv, Store, Any]:
    settings = _settings(tmp_path, cfg)
    st = store or Store(tmp_path / "t.db")
    st.migrate()
    env = FreestyleEnv(
        lambda: settings,
        data_dir=tmp_path / "data",
        store=st,
        transport=httpx.MockTransport(api),
        sleep=_no_sleep,
        poll_interval_s=0.0,
        **kw,
    )
    return env, st, settings


def _acquire_api(**over: Any) -> FakeAPI:
    """能走完 acquire 的假 API：POST 建机器 → 第一次 GET starting、第二次 GET running。"""
    api = FakeAPI()
    api.json_route("POST", "/v5/vms", 201, _vm("starting", **over))
    calls = {"n": 0}

    def _get(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_vm("running" if calls["n"] > 1 else "starting"))

    api.route("GET", f"/v5/vms/{VM_ID}", _get)
    api.route("POST", f"/v5/vms/{VM_ID}/fs/mkdir", lambda _r: httpx.Response(200, json={"ok": True}))
    return api


# ----------------------------------------------------------------------
# 1. acquire 正常路径
# ----------------------------------------------------------------------


class TestAcquire:
    async def test_acquire_request_shape_and_polling(self, tmp_path: Path) -> None:
        api = _acquire_api()
        env, _st, _s = _make_env(tmp_path, api)
        box = await env.acquire("job-a")
        assert box is not None
        assert isinstance(box, FreestyleBox)
        assert box.vm_id == VM_ID
        assert box.kind == "freestyle"
        assert box.remote_root == "/root/maiwork"
        assert box.state == "running"

        posts = api.of("POST", "/v5/vms")
        assert len(posts) == 1
        req = posts[0]
        assert str(req.url) == "https://api.freestyle.sh/v5/vms"
        assert req.headers["authorization"] == f"Bearer {KEY}"
        body = json.loads(req.content)
        assert body["firewall"]["rules"][0] == {
            "action": "allow", "source": {}, "destination": {"public": True},
        }
        assert body["snapshotId"] == "freestyle/ubuntu-sm"
        assert body["autoDeleteSeconds"] == 0
        assert body["ttlSeconds"] == 60 * 60
        assert body["idleTimeoutSeconds"] == 10 * 60
        assert body["metadata"]["maiwork_job"] == "job-a"
        assert body["slug"].startswith("maiwork-job-a-")
        # 建完机器把工作根目录建出来（用文件 API，不花一次 exec）
        mkdirs = api.of("POST", f"/v5/vms/{VM_ID}/fs/mkdir")
        assert mkdirs and json.loads(mkdirs[0].content)["path"] == "/root/maiwork"

        # 轮询到 running 才返回
        assert len(api.of("GET", f"/v5/vms/{VM_ID}")) == 2
        assert box.expires_ts == pytest.approx(NOW + 3600)
        assert box.cpu == 2 and box.memory_mib == 4096
        assert env.current_box is box
        assert env.usage_today() == 1
        assert env.last_fail() is None

    async def test_acquire_slug_retries_once_on_conflict(self, tmp_path: Path) -> None:
        api = FakeAPI()
        seen: list[str] = []

        def _create(r: httpx.Request) -> httpx.Response:
            seen.append(json.loads(r.content)["slug"])
            if len(seen) == 1:
                return httpx.Response(409, json={"code": "CONFLICT", "message": "slug taken"})
            return httpx.Response(201, json=_vm("running"))

        api.route("POST", "/v5/vms", _create)
        api.route("POST", f"/v5/vms/{VM_ID}/fs/mkdir", lambda _r: httpx.Response(200, json={}))
        env, _st, _s = _make_env(tmp_path, api)
        box = await env.acquire("job-b")
        assert box is not None
        assert len(seen) == 2 and seen[0] != seen[1]

    async def test_acquire_429_gives_chinese_reason(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        api = FakeAPI()
        api.json_route("POST", "/v5/vms", 429, {"code": "RATE_LIMITED", "message": "too many vms"})
        env, _st, _s = _make_env(tmp_path, api)
        with caplog.at_level(logging.INFO):
            assert await env.acquire("job-c") is None
        lf = env.last_fail()
        assert isinstance(lf, dict)
        reason = str(lf["reason"])
        assert "配额" in reason or "上限" in reason
        # 服务商原始英文报错**不拼进给网页看的中文结论**（2026-10-15 用户定），只进日志
        assert "too many vms" not in reason
        assert "too many vms" in caplog.text
        assert env.usage_today() == 0

    async def test_acquire_401_says_key_is_wrong(self, tmp_path: Path) -> None:
        api = FakeAPI()
        api.json_route(
            "POST", "/v5/vms", 401,
            {"code": "MISSING_CREDENTIALS", "message": "Missing credentials: send `Authorization: Bearer <api key>`"},
        )
        env, _st, _s = _make_env(tmp_path, api)
        assert await env.acquire("job-d") is None
        reason = str(env.last_fail()["reason"])  # type: ignore[index]
        assert "密钥" in reason
        assert KEY not in reason

    async def test_acquire_poll_timeout(self, tmp_path: Path) -> None:
        api = FakeAPI()
        api.json_route("POST", "/v5/vms", 201, _vm("starting"))
        api.json_route("GET", f"/v5/vms/{VM_ID}", 200, _vm("starting"))
        env, _st, _s = _make_env(tmp_path, api, poll_timeout_s=0.0)
        assert await env.acquire("job-e") is None
        reason = str(env.last_fail()["reason"])  # type: ignore[index]
        assert "超时" in reason or "等" in reason

    async def test_acquire_without_key_sends_nothing(self, tmp_path: Path) -> None:
        api = FakeAPI()
        env, _st, _s = _make_env(tmp_path, api, cfg={"freestyle_api_key": ""})
        assert await env.acquire("job-f") is None
        assert api.requests == []
        assert "密钥" in str(env.last_fail()["reason"])  # type: ignore[index]

    async def test_acquire_refused_when_platform_not_selected(self, tmp_path: Path) -> None:
        api = FakeAPI()
        env, _st, _s = _make_env(tmp_path, api, cfg={"vm_platform": "railway", "freestyle": False})
        assert await env.acquire("job-g") is None
        assert api.requests == []


# ----------------------------------------------------------------------
# 2. 配额 / 同时 1 台
# ----------------------------------------------------------------------


class TestLimits:
    async def test_daily_max(self, tmp_path: Path) -> None:
        api = _acquire_api()
        env, st, _s = _make_env(tmp_path, api, cfg={"freestyle_daily_max": 2})
        for i in range(2):
            box = await env.acquire(f"job-{i}")
            assert box is not None
            await env.release(box)
        assert await env.acquire("job-3") is None
        assert "已达上限" in str(env.last_fail()["reason"])  # type: ignore[index]
        assert env.usage_today() == 2
        rec = st.kv_get(f"freestyle.day.{clock.day_key(NOW)}", None)
        assert isinstance(rec, dict) and int(rec["count"]) == 2

    async def test_only_one_box_at_a_time(self, tmp_path: Path) -> None:
        api = _acquire_api()
        env, _st, _s = _make_env(tmp_path, api)
        first = await env.acquire("job-1")
        assert first is not None
        assert await env.acquire("job-2") is None
        assert "同一时间只能用一台" in str(env.last_fail()["reason"])  # type: ignore[index]
        await env.release(first)
        assert env.current_box is None
        assert await env.acquire("job-3") is not None


# ----------------------------------------------------------------------
# 3. run
# ----------------------------------------------------------------------


class TestRun:
    async def _box(self, tmp_path: Path, **kw: Any) -> tuple[FreestyleEnv, FreestyleBox, FakeAPI]:
        api = _acquire_api()
        env, _st, _s = _make_env(tmp_path, api, **kw)
        box = await env.acquire("job-run")
        assert box is not None
        return env, box, api

    async def test_run_passes_command_and_returns_exit_code(self, tmp_path: Path) -> None:
        env, box, api = await self._box(tmp_path)
        api.json_route(
            "POST", f"/v5/vms/{VM_ID}/exec-await", 200,
            {"statusCode": 7, "stdout": "hello", "stderr": "warn"},
        )
        r = await env.run(box, "echo hello", timeout_s=60)
        assert r.exit_code == 7
        assert r.stdout == "hello" and r.stderr == "warn"
        assert r.timed_out is False
        req = api.of("POST", f"/v5/vms/{VM_ID}/exec-await")[0]
        body = json.loads(req.content)
        assert body["command"] == "echo hello"
        assert body["timeoutMs"] == 60_000
        assert body["linuxUser"] == "root"
        assert KEY not in req.content.decode("utf-8", "replace")

    async def test_run_clamps_timeout_to_platform_max(self, tmp_path: Path) -> None:
        env, box, api = await self._box(tmp_path)
        api.json_route(
            "POST", f"/v5/vms/{VM_ID}/exec-await", 200,
            {"statusCode": 0, "stdout": "", "stderr": ""},
        )
        await env.run(box, "sleep 1", timeout_s=600)
        body = json.loads(api.of("POST", f"/v5/vms/{VM_ID}/exec-await")[0].content)
        assert body["timeoutMs"] <= 280_000
        assert env.max_run_timeout_s <= 280

    async def test_run_null_status_code_means_timed_out(self, tmp_path: Path) -> None:
        env, box, api = await self._box(tmp_path)
        api.json_route(
            "POST", f"/v5/vms/{VM_ID}/exec-await", 200,
            {"statusCode": None, "stdout": "partial", "stderr": ""},
        )
        r = await env.run(box, "sleep 9999", timeout_s=30)
        assert r.timed_out is True
        assert r.exit_code != 0
        assert "超时" in r.stderr

    async def test_run_on_missing_vm_is_chinese_failure(self, tmp_path: Path) -> None:
        env, box, api = await self._box(tmp_path)
        api.json_route("POST", f"/v5/vms/{VM_ID}/exec-await", 404,
                       {"code": "NOT_FOUND", "message": "vm not found"})
        r = await env.run(box, "ls", timeout_s=10)
        assert r.exit_code != 0
        assert "找不到" in r.stderr or "没了" in r.stderr

    async def test_run_refuses_when_box_almost_expired(self, tmp_path: Path) -> None:
        env, box, _api = await self._box(tmp_path)
        box.expires_ts = clock.now() + 100
        r = await env.run(box, "ls", timeout_s=60)
        assert r.exit_code != 0
        assert "即将到期" in r.stderr


# ----------------------------------------------------------------------
# 4. put / get
# ----------------------------------------------------------------------


class TestFiles:
    async def _box(self, tmp_path: Path, **kw: Any) -> tuple[FreestyleEnv, FreestyleBox, FakeAPI]:
        api = _acquire_api()
        env, _st, _s = _make_env(tmp_path, api, **kw)
        box = await env.acquire("job-files")
        assert box is not None
        return env, box, api

    async def test_put_small_file_base64_atomic(self, tmp_path: Path) -> None:
        env, box, api = await self._box(tmp_path)
        api.json_route("PUT", f"/v5/vms/{VM_ID}/fs/write", 200, {"ok": True})
        local = tmp_path / "script.py"
        local.write_bytes(b"print('hi')\n")
        await env.put(box, local, "/root/maiwork/script.py")

        req = api.of("PUT", f"/v5/vms/{VM_ID}/fs/write")[0]
        assert req.url.params["path"] == "/root/maiwork/script.py"
        body = json.loads(req.content)
        assert body["encoding"] == "base64"
        assert base64.b64decode(body["content"]) == b"print('hi')\n"
        assert body["mode"] == 0o644
        assert body["sha256"] == hashlib.sha256(b"print('hi')\n").hexdigest()
        # 父目录先建出来（fs/write 不建目录）
        assert api.of("POST", f"/v5/vms/{VM_ID}/fs/mkdir")

    async def test_put_big_file_uses_uploads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert fs._FS_WRITE_MAX_BYTES == 8 * 1024 * 1024
        monkeypatch.setattr(fs, "_FS_WRITE_MAX_BYTES", 8)
        monkeypatch.setattr(fs, "_FS_UPLOAD_CHUNK", 8)
        env, box, api = await self._box(tmp_path)
        upload_id = "upl-1"
        api.json_route("POST", f"/v5/vms/{VM_ID}/fs/uploads", 200,
                       {"uploadId": upload_id, "path": "/root/maiwork/big.bin", "size": 20, "receivedBytes": 0})
        api.json_route("PUT", f"/v5/vms/{VM_ID}/fs/uploads/{upload_id}", 200, {"receivedBytes": 8})
        api.json_route("POST", f"/v5/vms/{VM_ID}/fs/uploads/{upload_id}/commit", 200, {"ok": True})
        local = tmp_path / "big.bin"
        local.write_bytes(bytes(range(20)))
        await env.put(box, local, "/root/maiwork/big.bin")

        assert not api.of("PUT", f"/v5/vms/{VM_ID}/fs/write")
        begin = json.loads(api.of("POST", f"/v5/vms/{VM_ID}/fs/uploads")[0].content)
        assert begin["size"] == 20 and begin["path"] == "/root/maiwork/big.bin"
        parts = api.of("PUT", f"/v5/vms/{VM_ID}/fs/uploads/{upload_id}")
        assert [int(r.url.params["offset"]) for r in parts] == [0, 8, 16]
        assert b"".join(r.content for r in parts) == bytes(range(20))
        assert api.of("POST", f"/v5/vms/{VM_ID}/fs/uploads/{upload_id}/commit")

    async def test_get_uses_stat_and_ranged_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fs, "_FS_READ_CHUNK", 4)
        env, box, api = await self._box(tmp_path)
        payload = b"0123456789A"  # 11 字节 → 4 + 4 + 3
        api.json_route("GET", f"/v5/vms/{VM_ID}/fs/stat", 200, {"size": len(payload)})

        def _read(r: httpx.Request) -> httpx.Response:
            rng = r.headers.get("Range")
            assert rng, "分块续传必须带 Range 头"
            start_s, end_s = rng.replace("bytes=", "").split("-")
            return httpx.Response(206, content=payload[int(start_s): int(end_s) + 1])

        api.route("GET", f"/v5/vms/{VM_ID}/fs/read", _read)
        local = tmp_path / "out" / "got.bin"
        await env.get(box, "/root/maiwork/got.bin", local)
        assert local.read_bytes() == payload
        assert len(api.of("GET", f"/v5/vms/{VM_ID}/fs/read")) == 3

    async def test_get_binary_bytes_survive(self, tmp_path: Path) -> None:
        env, box, api = await self._box(tmp_path)
        payload = bytes([0, 1, 2, 253, 254, 255])
        api.json_route("GET", f"/v5/vms/{VM_ID}/fs/stat", 200, {"size": len(payload)})
        api.route("GET", f"/v5/vms/{VM_ID}/fs/read", lambda _r: httpx.Response(200, content=payload))
        local = tmp_path / "bin.dat"
        await env.get(box, "/root/maiwork/bin.dat", local)
        assert local.read_bytes() == payload


# ----------------------------------------------------------------------
# 5. release
# ----------------------------------------------------------------------


class TestRelease:
    async def test_release_sends_delete_and_clears(self, tmp_path: Path) -> None:
        api = _acquire_api()
        env, st, _s = _make_env(tmp_path, api)
        box = await env.acquire("job-rel")
        assert box is not None
        api.json_route("DELETE", f"/v5/vms/{VM_ID}", 204, None)
        await env.release(box)
        assert api.of("DELETE", f"/v5/vms/{VM_ID}")
        assert env.current_box is None
        assert st.kv_get("freestyle.active", None) is None

    async def test_release_is_idempotent_on_404(self, tmp_path: Path) -> None:
        api = _acquire_api()
        env, _st, _s = _make_env(tmp_path, api)
        box = await env.acquire("job-rel2")
        assert box is not None
        api.json_route("DELETE", f"/v5/vms/{VM_ID}", 404, {"code": "NOT_FOUND", "message": "gone"})
        await env.release(box)  # 不抛
        await env.release(box)  # 再来一次也不抛
        await env.release(None)  # None 直接返回
        assert env.current_box is None

    async def test_release_swallows_server_error(self, tmp_path: Path) -> None:
        api = _acquire_api()
        env, _st, _s = _make_env(tmp_path, api)
        box = await env.acquire("job-rel3")
        assert box is not None
        api.json_route("DELETE", f"/v5/vms/{VM_ID}", 500, {"code": "INTERNAL", "message": "boom"})
        await env.release(box)  # 自身不抛（coordinator 的约定）
        assert env.current_box is None


# ----------------------------------------------------------------------
# 6. 健康检查（验密钥）
# ----------------------------------------------------------------------


class TestCheckKey:
    async def test_ok(self, tmp_path: Path) -> None:
        api = FakeAPI()
        api.json_route("GET", "/v5/vms", 200, {"vms": [], "totalCount": 1, "runningCount": 1})
        env, _st, _s = _make_env(tmp_path, api)
        ok, text = await env.check_key()
        assert ok is True
        assert "1" in text
        assert api.of("GET", "/v5/vms")[0].url.params["limit"] == "1"

    async def test_bad_key(self, tmp_path: Path) -> None:
        api = FakeAPI()
        api.json_route("GET", "/v5/vms", 401, {"code": "MISSING_CREDENTIALS", "message": "nope"})
        env, _st, _s = _make_env(tmp_path, api)
        ok, text = await env.check_key()
        assert ok is False
        assert "密钥" in text

    async def test_without_key_no_request(self, tmp_path: Path) -> None:
        api = FakeAPI()
        env, _st, _s = _make_env(tmp_path, api, cfg={"freestyle_api_key": ""})
        ok, text = await env.check_key()
        assert ok is False
        assert "密钥" in text
        assert api.requests == []


# ----------------------------------------------------------------------
# 7. 到期护栏 + 工具层（vm_* 在 freestyle box 上照样工作）
# ----------------------------------------------------------------------


async def _tool_call(tools: Any, name: str, args: dict, ctx: Any) -> Any:
    return await tools.call(name, args, ctx)


class TestVmToolsOnFreestyle:
    def _ctx(self, tmp_path: Path) -> Any:
        from CharTyr_MaiWork.maiwork.environments.local import LocalEnv
        from CharTyr_MaiWork.maiwork.tools import ToolContext

        local = LocalEnv(lambda: _settings(tmp_path))
        ws = local.workspace("g900000001")
        return ToolContext(
            group_id="900000001", task_id="T-1", actor="子 agent", role="worker", workspace=ws
        ), local

    async def test_near_expiry_appends_reminder(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.maiwork.tools import Tools
        from CharTyr_MaiWork.maiwork.tools_railway import register_vm_tools

        api = _acquire_api()
        env, st, _s = _make_env(tmp_path, api)
        box = await env.acquire("job-t")
        assert box is not None
        box.expires_ts = clock.now() + 300  # 只剩 5 分钟
        api.json_route("POST", f"/v5/vms/{VM_ID}/exec-await", 200,
                       {"statusCode": 0, "stdout": "ok", "stderr": ""})
        tools = Tools(st)
        register_vm_tools(tools, get_box=lambda: env.current_box, env=env)
        ctx, _local = self._ctx(tmp_path)
        r = await _tool_call(tools, "vm_run", {"command": "ls"}, ctx)
        assert r.ok and "到期" in r.output

    async def test_expired_box_fails_chinese(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.maiwork.tools import Tools
        from CharTyr_MaiWork.maiwork.tools_railway import register_vm_tools

        api = _acquire_api()
        env, st, _s = _make_env(tmp_path, api)
        box = await env.acquire("job-t2")
        assert box is not None
        box.expires_ts = clock.now() - 5
        tools = Tools(st)
        register_vm_tools(tools, get_box=lambda: env.current_box, env=env)
        ctx, _local = self._ctx(tmp_path)
        r = await _tool_call(tools, "vm_run", {"command": "ls"}, ctx)
        assert not r.ok and ("到点" in r.error or "到期" in r.error)

    async def test_remote_root_is_platform_specific(self, tmp_path: Path) -> None:
        """put/read/fetch 的远端根跟着 box.remote_root 走（Freestyle 不是 /app）。"""
        from CharTyr_MaiWork.maiwork.tools import Tools
        from CharTyr_MaiWork.maiwork.tools_railway import register_vm_tools

        api = _acquire_api()
        env, st, _s = _make_env(tmp_path, api)
        box = await env.acquire("job-t3")
        assert box is not None
        api.json_route("PUT", f"/v5/vms/{VM_ID}/fs/write", 200, {"ok": True})
        tools = Tools(st)
        register_vm_tools(tools, get_box=lambda: env.current_box, env=env)
        ctx, local = self._ctx(tmp_path)
        ws = ctx.workspace
        assert ws is not None
        (ws / "s.py").write_text("print(1)\n", encoding="utf-8")
        r = await _tool_call(tools, "vm_put_file", {"path": "s.py"}, ctx)
        assert r.ok, r.error
        assert r.data["remote"] == "/root/maiwork/s.py"

        # 读回：/app/ 前缀在 Freestyle 上要被拒（那是 railway 的根）
        r2 = await _tool_call(tools, "vm_read_file", {"path": "/app/x.log"}, ctx)
        assert not r2.ok


# ----------------------------------------------------------------------
# 8. 密钥不外泄
# ----------------------------------------------------------------------


class TestKeyNeverLeaks:
    async def test_no_key_in_logs(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        api = _acquire_api()
        api.json_route("POST", f"/v5/vms/{VM_ID}/exec-await", 200,
                       {"statusCode": 1, "stdout": "x", "stderr": "y"})
        env, _st, _s = _make_env(tmp_path, api)
        with caplog.at_level(logging.DEBUG):
            box = await env.acquire("job-log")
            assert box is not None
            await env.run(box, "echo x", timeout_s=10)
            await env.release(box)
        assert KEY not in caplog.text

    async def test_key_only_in_authorization_header(self, tmp_path: Path) -> None:
        api = _acquire_api()
        env, _st, _s = _make_env(tmp_path, api)
        box = await env.acquire("job-hdr")
        assert box is not None
        for req in api.requests:
            if req.headers.get("authorization"):
                assert req.headers["authorization"] == f"Bearer {KEY}"
            else:
                assert KEY not in req.content.decode("utf-8", "replace")
        # last_fail / usage 里也绝不出现密钥
        assert KEY not in json.dumps(env.last_fail() or {})


# ----------------------------------------------------------------------
# 9. 与 railway 同形的对外接口（coordinator / views 靠这个鸭子类型）
# ----------------------------------------------------------------------


class TestSameShapeAsRailway:
    async def test_public_surface(self) -> None:
        for name in ("acquire", "run", "put", "get", "release", "check_key", "close"):
            assert callable(getattr(FreestyleEnv, name, None)), name
        assert isinstance(FreestyleEnv.current_box, property)
        assert callable(FreestyleEnv.usage_today)
        assert callable(FreestyleEnv.last_fail)
