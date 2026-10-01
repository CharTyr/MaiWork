"""A11 回归：手动备料「现在就备一批」的前提拒绝 + 运行记录（run_id / 真实终态）。

审查 docs/13-0.7.0整体审查.md A11：手动入口没核对画像是否成形和专岗启停，
接口回了 200/started=true，后台却立刻静默退出（模型请求 0、批次 0）。

这里覆盖：
- 画像没成形 / news 岗停用 / 模型没配好 → POST 409 + 准确中文原因，后台零调用、零模型请求；
- 接受的手动备料 → 返回 run_id，跑完能从 GET /api/groups/{gid}/news/run 读到
  running / done / skipped / failed 四种真实状态（只出了文章也算 done，不算失败）；
- 非服务群的 GET / POST 都拒绝；
- 服务器重启后残留的 running 记录不会再永远显示「进行中」。

夹具参考 tests/test_console.py（真 app + 真 console 起 aiohttp 测试服务器）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.tools import ToolContext

G1 = "900000001"
G2 = "123456789"
G_NOT_SERVED = "999888777"
PASSWORD = "测试密码-非常显眼-不要出现在日志里"


def _free_port() -> int:
    """挑一个本机空闲端口：测试别依赖固定端口空着。"""
    import socket as _s

    with _s.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _raw_config(data_dir: Path, *, models: bool = True, **over) -> dict:
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}"}, {"group": f"qq:{G2}"}]},
        "console": {"listen": f"127.0.0.1:{_free_port()}", "password": PASSWORD, "public_url": ""},
        "storage": {"data_dir": str(data_dir)},
    }
    if models:
        raw["models"] = {
            "base_url": "http://127.0.0.1:9/v1",
            "api_key": "test-key",
            "main": "m1",
            "worker": "w1",
        }
    for section, values in over.items():
        raw.setdefault(section, {}).update(values)
    return raw


class Env:
    def __init__(self, app: MaiWorkApp, client: TestClient) -> None:
        self.app = app
        self.client = client

    async def login(self, password: str = PASSWORD):
        return await self.client.post("/api/login", json={"password": password})

    async def post_run(self, gid: str = G1):
        return await self.client.post(f"/api/groups/{gid}/news/run", json={})

    async def get_run(self, gid: str = G1):
        return await self.client.get(f"/api/groups/{gid}/news/run")

    def set_profile_ready(self, gid: str = G1, ts: float | None = None) -> None:
        with self.app.store.tx() as conn:
            conn.execute(
                "UPDATE groups SET profile_ready_ts=? WHERE group_id=?",
                (float(ts if ts is not None else clock.now()), str(gid)),
            )

    def set_manual_run(self, payload: dict, gid: str = G1) -> None:
        with self.app.store.tx() as conn:
            self.app.store.kv_set(conn, f"news.manual_run.{gid}", payload)

    def manual_run(self, gid: str = G1) -> dict | None:
        got = self.app.store.kv_get(f"news.manual_run.{gid}")
        return got if isinstance(got, dict) else None

    def model_calls(self) -> int:
        return int(
            self.app.store.read().execute("SELECT COUNT(*) c FROM model_calls").fetchone()["c"]
        )

    async def drain(self) -> None:
        """等这轮后台长活跑完（等真任务，不靠墙钟猜）。"""
        for _ in range(200):
            running = [t for t in list(self.app._bg_jobs) if not t.done()]
            if not running:
                return
            await asyncio.gather(*running, return_exceptions=True)


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    """真 app + 真 console（模型配好、画像默认没成形）。"""
    app = MaiWorkApp(
        FakeCtx({"config.get": "987654321"}),
        _raw_config(tmp_path / "data"),
        plugin_dir=Path(__file__).resolve().parents[1],
    )
    app.profiles_cls = FakeProfiles
    await app.start()
    assert app.feeds is not None
    assert app.models.settings().ready(), "夹具坏了：模型该是配好的"
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    try:
        yield Env(app, client)
    finally:
        await client.close()
        await app.stop()


@pytest_asyncio.fixture
async def env_no_models(tmp_path: Path):
    """真 app + 真 console，但没配模型（模型没配好只能拒绝）。"""
    app = MaiWorkApp(
        FakeCtx({}),
        _raw_config(tmp_path / "data", models=False),
        plugin_dir=Path(__file__).resolve().parents[1],
    )
    app.profiles_cls = FakeProfiles
    await app.start()
    assert not app.models.settings().ready(), "夹具坏了：模型该是没配的"
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    try:
        yield Env(app, client)
    finally:
        await client.close()
        await app.stop()


# ----------------------------------------------------------------------
# 前提拒绝：不是「承诺了开始又静默退出」
# ----------------------------------------------------------------------


class TestPreconditions:
    @pytest.mark.asyncio
    async def test_fresh_profile_rejected_and_no_model_requests(self, env: Env) -> None:
        """画像没成形：POST 409 + 说清怎么办；后台一次都没跑、零模型请求、零批次。"""
        calls: list[str] = []

        async def _fake_prepare(gid: str) -> int:
            calls.append(str(gid))
            return 0

        env.app.feeds.prepare_news = _fake_prepare  # type: ignore[method-assign]
        await env.login()
        r = await env.post_run()
        assert r.status == 409, await r.text()
        body = await r.json()
        reason = body["error"]
        assert "画像" in reason or "熟悉" in reason, reason
        assert calls == []
        assert env.model_calls() == 0
        assert env.manual_run() is None, "被拒的这次不该留运行记录"
        batches = env.app.store.read().execute("SELECT COUNT(*) c FROM news_batches").fetchone()["c"]
        assert batches == 0

    @pytest.mark.asyncio
    async def test_fresh_profile_writes_one_skipped_batch(self, env: Env) -> None:
        """真调 feeds.prepare_news（不用接口）：画像没成形也要写一条 skipped 批次，不能静默。"""
        env.set_profile_ready(G1, ts=0.0)  # 明确没成形
        kept = await env.app.feeds.prepare_news(G1)
        assert kept == 0
        row = env.app.store.read().execute("SELECT * FROM news_batches").fetchone()
        assert row is not None, "画像没成形也要留下一条跳过记录"
        assert row["skipped"] == 1
        assert "画像" in str(row["note"]) or "熟悉" in str(row["note"])

    @pytest.mark.asyncio
    async def test_news_role_disabled_rejected(self, env: Env) -> None:
        """资讯专岗（news）停用：拒绝并说清去哪儿打开。"""
        env.set_profile_ready()
        env.app.agents.update_profile("news", {"enabled": False})
        await env.login()
        r = await env.post_run()
        assert r.status == 409, await r.text()
        reason = (await r.json())["error"]
        assert "news" in reason or "专岗" in reason, reason
        assert env.model_calls() == 0

    @pytest.mark.asyncio
    async def test_models_not_ready_rejected(self, env_no_models: Env) -> None:
        """模型没配好：拒绝并说清去哪儿配。"""
        env_no_models.set_profile_ready()
        await env_no_models.login()
        r = await env_no_models.post_run()
        assert r.status == 409, await r.text()
        reason = (await r.json())["error"]
        assert "模型" in reason, reason
        assert env_no_models.model_calls() == 0

    @pytest.mark.asyncio
    async def test_admin_tool_returns_accurate_reason(self, env: Env) -> None:
        """管理员对话工具 run_news_now 也要回准确原因，不是「已经让它备料了」。"""
        ctx = ToolContext(group_id=G1, actor="bot 管理员", role="admin")
        out = await env.app.tools.call("run_news_now", {"group_id": G1}, ctx)
        assert not out.ok
        assert "画像" in out.error or "熟悉" in out.error, out.error


# ----------------------------------------------------------------------
# 接受后的运行记录：running / done / skipped / failed
# ----------------------------------------------------------------------


class TestRunRecord:
    @pytest.mark.asyncio
    async def test_running_then_done_readable_from_get(self, env: Env) -> None:
        gate = asyncio.Event()

        async def _fake_prepare(gid: str) -> int:
            await gate.wait()
            return 2

        env.app.feeds.prepare_news = _fake_prepare  # type: ignore[method-assign]
        env.set_profile_ready()
        await env.login()
        r = await env.post_run()
        assert r.status == 200, await r.text()
        body = await r.json()
        assert body["started"] is True
        run_id = body["run_id"]
        assert run_id

        running = await (await env.get_run()).json()
        assert running["state"] == "running"
        assert running["run_id"] == run_id
        assert running["started_ts"] > 0 and not running["ended_ts"]

        gate.set()
        await env.drain()
        done = await (await env.get_run()).json()
        assert done["state"] == "done"
        assert done["run_id"] == run_id
        assert done["items"] == 2
        assert done["ended_ts"] >= done["started_ts"]
        assert done["reason"]

    @pytest.mark.asyncio
    async def test_skipped_reads_latest_batch_note(self, env: Env) -> None:
        """返回 0：跳过，原因取本轮之后最新一条 news_batches 的 note。"""

        async def _fake_prepare(gid: str) -> int:
            env.app.feeds._skipped_batch(gid, "撒网没搜出能用的候选")
            return 0

        env.app.feeds.prepare_news = _fake_prepare  # type: ignore[method-assign]
        env.set_profile_ready()
        await env.login()
        r = await env.post_run()
        assert r.status == 200, await r.text()
        run_id = (await r.json())["run_id"]
        await env.drain()
        got = await (await env.get_run()).json()
        assert got["state"] == "skipped"
        assert got["run_id"] == run_id
        assert "撒网" in got["reason"], got["reason"]
        assert got["items"] == 0

    @pytest.mark.asyncio
    async def test_exception_is_failed(self, env: Env) -> None:
        async def _fake_prepare(gid: str) -> int:
            raise RuntimeError("网络断了")

        env.app.feeds.prepare_news = _fake_prepare  # type: ignore[method-assign]
        env.set_profile_ready()
        await env.login()
        r = await env.post_run()
        assert r.status == 200, await r.text()
        await env.drain()
        got = await (await env.get_run()).json()
        assert got["state"] == "failed"
        assert "网络断了" in got["reason"], got["reason"]

    @pytest.mark.asyncio
    async def test_articles_only_is_done_not_failed(self, env: Env) -> None:
        """只出了文章（kind=guide）、没出资讯：算 done，并在原因里说明。"""

        async def _fake_prepare(gid: str) -> int:
            store = env.app.store
            now = clock.now()
            with store.tx() as conn:
                cur = conn.execute(
                    "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                    " VALUES (?, ?, 3, 1, 0, '', ?)",
                    (gid, now, now),
                )
                bid = int(cur.lastrowid or 0)
                conn.execute(
                    "INSERT INTO news_items (batch_id, group_id, icon, title, summary, why, sources,"
                    " url_key, score, status_kind, created, kind, rejected)"
                    " VALUES (?, ?, 'books', '一篇讲 SQLite WAL 的长文', '摘要', '群里在聊存储',"
                    " '[]', 'example.com/a', 4.2, 'new', ?, 'guide', 0)",
                    (bid, gid, now),
                )
            return 1

        env.app.feeds.prepare_news = _fake_prepare  # type: ignore[method-assign]
        env.set_profile_ready()
        await env.login()
        r = await env.post_run()
        assert r.status == 200, await r.text()
        await env.drain()
        got = await (await env.get_run()).json()
        assert got["state"] == "done"
        assert got["items"] == 1
        assert "文章" in got["reason"], got["reason"]

    @pytest.mark.asyncio
    async def test_no_record_yet_is_none_state(self, env: Env) -> None:
        await env.login()
        r = await env.get_run()
        assert r.status == 200, await r.text()
        got = await r.json()
        assert got["state"] == "none"
        assert not got["run_id"]


# ----------------------------------------------------------------------
# 鉴权 / 服务群隔离 / 重启后的残留 running
# ----------------------------------------------------------------------


class TestGuards:
    @pytest.mark.asyncio
    async def test_non_served_group_get_and_post_rejected(self, env: Env) -> None:
        await env.login()
        assert (await env.post_run(G_NOT_SERVED)).status == 404
        assert (await env.get_run(G_NOT_SERVED)).status == 404

    @pytest.mark.asyncio
    async def test_anonymous_rejected(self, env: Env) -> None:
        assert (await env.post_run()).status == 401
        assert (await env.get_run()).status == 401

    @pytest.mark.asyncio
    async def test_stale_running_record_reports_interrupted(self, env: Env) -> None:
        """服务器重启后残留的 running：没有对应后台任务就报 failed「中断了」。"""
        env.set_manual_run(
            {
                "run_id": "old-run-1",
                "state": "running",
                "started_ts": clock.now() - 3600,
                "ended_ts": 0.0,
                "reason": "",
                "items": 0,
            }
        )
        assert (G1, "news_manual") not in env.app._running_jobs
        await env.login()
        got = await (await env.get_run()).json()
        assert got["state"] == "failed", got
        assert "中断了" in got["reason"], got["reason"]
        # 落库也要改过来，别每次 GET 都靠现算
        stored = env.manual_run()
        assert stored is not None and stored["state"] == "failed"

    @pytest.mark.asyncio
    async def test_running_record_with_live_job_stays_running(self, env: Env) -> None:
        """真在跑的这批不能被误判成「中断了」。"""
        gate = asyncio.Event()

        async def _fake_prepare(gid: str) -> int:
            await gate.wait()
            return 1

        env.app.feeds.prepare_news = _fake_prepare  # type: ignore[method-assign]
        env.set_profile_ready()
        await env.login()
        r = await env.post_run()
        assert r.status == 200
        got = await (await env.get_run()).json()
        assert got["state"] == "running"
        gate.set()
        await env.drain()
