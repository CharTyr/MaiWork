"""test_e2e.py：端到端集成测试——真 MaiWorkApp（经 plugin.py 钩子入口）+ FakeCtx 宿主
+ MockTransport 的假 OpenAI 端点 / 假 Jev / 假 here.now，把整条链路接起来测。

约束（AGENTS.md 红线）：
- 零真实网络：OpenAI、Jev、here.now 一律走 httpx.MockTransport；宿主一律 FakeCtx。
- 不 SSH、不 git、不碰线上。数据目录、工作区一律 tmp_path。
- 每个断言都落在「宿主收到的调用」或「库里状态」上，不放松红线断言。

Jev / OpenAI / here.now 的 transport 必须在 app.start() 之前注好（Jev 的
httpx.AsyncClient 在构造时就定）。经 plugin.create_plugin() 入口时，先 on_load
启一次（这次不会真发网络），再停掉、注 transport、再 start。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp
import httpx
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeProfiles, FakeCtx

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.config import MaiWorkConfig
from CharTyr_MaiWork.plugin import MaiWorkPlugin, create_plugin

BJ = timezone(timedelta(hours=8))

SERVE = "900000001"
OTHER = "111"
SESSION = "sess-e2e"
BOT_QQ = "2000000000"
ADMIN = "5500001"
USER1 = "10001"
NAME1 = "阿柒"

MODEL_KEY = "sk-e2e-secret-dontleak"
JEV_KEY = "jev-e2e-secret-dontleak"


# ---------------------------------------------------------------------------
# 时钟驾驶（monkeypatch clock.now；本文件内每个用例独立 world）
# ---------------------------------------------------------------------------


def _bj_epoch(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=BJ).timestamp()


class TravelClock:
    """装在 monkeypatch 上的时钟：set(...) 换当前时刻。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._holding = [_bj_epoch(2026, 10, 19, 14, 0)]  # 2026-10-19 周一 14:00
        monkeypatch.setattr(clock, "now", lambda: self._holding[0])

    def set(self, ts: float) -> None:
        self._holding[0] = float(ts)

    def __call__(self) -> float:
        return self._holding[0]


# ---------------------------------------------------------------------------
# 宿主应答（FakeCtx 的 responses；断言都读这里 / ctx.calls）
# ---------------------------------------------------------------------------


def hook_message(
    group_id: str,
    user_id: str,
    text: str,
    *,
    message_id: str = "m-new",
    session_id: str = SESSION,
    ts: float | None = None,
    is_at: bool = False,
    nickname: str = NAME1,
) -> dict:
    """按 docs/06 的真实键名造一条 chat.receive.after_process 的 kwargs。"""
    info: dict[str, Any] = {"user_info": {"user_id": user_id, "user_nickname": nickname}}
    if group_id:
        info["group_info"] = {"group_id": group_id, "group_name": "测试群"}
    return {
        "message": {
            "message_id": message_id,
            "timestamp": ts if ts is not None else clock.now(),
            "platform": "qq",
            "message_info": info,
            "raw_message": text,
            "is_at": is_at,
            "is_mentioned": is_at,
            "is_command": text.startswith("/"),
            "is_emoji": False,
            "is_picture": False,
            "is_notify": False,
            "session_id": session_id,
            "processed_plain_text": text,
        }
    }


class HostResponses:
    """给 FakeCtx 用的「宿主」：调用记录、应答脚本。"""

    def __init__(self) -> None:
        self.send_hybrid: list[dict] = []
        self.uploads: list[dict] = []
        self.uploads_fail: bool = False
        self.uploads_timeout: bool = False
        self.get_calls: list[dict] = []
        self.messages: list[dict] = []
        self.proactive: list[dict] = []
        self.member_role: str = "member"
        self._msg_n = 0
        self.ctx: FakeCtx | None = None

    def _next_message_id(self) -> str:
        self._msg_n += 1
        return f"9{self._msg_n:05d}"

    def config_get(self, key: str = "", **kw: Any) -> Any:
        table = {"bot.qq_account": BOT_QQ, "bot.nickname": "麦麦"}
        return table.get(key)

    def stream_by_group_id(self, **kw: Any) -> dict:
        gid = str(kw.get("group_id") or "")
        return {"session_id": SESSION if gid == SERVE else f"steam-{gid}"}

    def get_messages(self, **kw: Any) -> list:
        self.get_calls.append(dict(kw))
        return list(self.messages)

    def send_hybrid_call(self, **kw: Any) -> dict:
        mid = self._next_message_id()
        rec = dict(kw)
        rec["message_id"] = mid
        self.send_hybrid.append(rec)
        return {"sent": True, "message_id": mid}

    async def api_list(self, **kw: Any) -> Any:
        # 线上宿主 api.list 的真实形态（components.py _cap_api_list）
        res = await self.api_call(api_name="api.list")
        names = res["data"]["apis"]
        return {"success": True, "apis": [{"plugin_id": "adapter", "name": n, "version": "1"} for n in names]}

    async def api_call(self, **kw: Any) -> Any:
        name = str(kw.get("api_name") or "")
        args = dict(kw.get("args") or {})
        # 线上 SnowLuma 适配器 1.x：动作直通接口参数整包在 params 里（2026-10-10 巡检）
        if isinstance(args.get("params"), dict):
            args = dict(args["params"])
        if name == "api.list":
            # 群空间探测：e2e 世界按旧版适配器处理（只有上传和取链接），
            # 公告/相册/文件管理一律不出现
            return {
                "status": "ok", "retcode": 0,
                "data": {
                    "apis": [
                        "adapter.napcat.file.upload_group_file",
                        "adapter.napcat.file.get_group_file_url",
                        "adapter.napcat.group.get_group_info",
                        "adapter.napcat.group.get_group_member_info",
                    ]
                },
            }
        if name == "adapter.napcat.file.upload_group_file":
            if self.uploads_timeout:
                # Host._call 把 TimeoutError 包成 HostError（「调用宿主能力超时」），
                # outbox 按「超时」字样落 uncertain，绝不重传
                raise asyncio.TimeoutError("上传网关没回话")
            if self.uploads_fail:
                return {"status": "failed", "retcode": 1, "data": {}}
            self.uploads.append(dict(args))
            return {"status": "ok", "retcode": 0, "data": {"file_id": f"/f{len(self.uploads):04d}"}}
        if name == "adapter.napcat.group.get_group_info":
            return {"status": "ok", "retcode": 0, "data": {"group_name": "折腾研究所", "member_count": 5}}
        if name == "adapter.napcat.group.get_group_member_info":
            return {"status": "ok", "retcode": 0, "data": {"role": self.member_role}}
        if name == "adapter.napcat.file.get_group_file_url":
            return {"status": "ok", "retcode": 0, "data": {"url": "https://file.example/f"}}
        raise AssertionError(f"没预料的 api.call：{name}")

    def make_ctx(self) -> FakeCtx:
        self.ctx = FakeCtx(
            {
                "config.get": self.config_get,
                "chat.get_stream_by_group_id": self.stream_by_group_id,
                "message.get_by_time_in_chat": self.get_messages,
                "send.hybrid": self.send_hybrid_call,
                "api.call": self.api_call,
                "api.list": self.api_list,
            }
        )
        return self.ctx


# ---------------------------------------------------------------------------
# 假 OpenAI 端点（httpx.MockTransport）
# ---------------------------------------------------------------------------


def _assistant(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False), "tool_calls": []}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5},
        },
    )


def _raw_text(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": text, "tool_calls": []}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5},
        },
    )


def _tool_calls(calls: list[tuple[str, dict]], *, content: str = "") -> httpx.Response:
    tcs = []
    for i, (name, args) in enumerate(calls):
        tcs.append(
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
        )
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content, "tool_calls": tcs}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5},
        },
    )


class OpenAIStub:
    """按请求特征脚本化回复。

    判定顺序（看 messages 文本 + tools 名单）：
    - 提醒解析（用户消息带「解析」「提醒」）→ script.reminder_answer 或默认 {"ok": false}
    - 验收（带「验收」「子 agent」）→ script.review_answer 或默认 pass
    - 计划（带「deliver_kind」「jobs」）→ script.plan_answer 或默认 view 计划
    - tools 名单里有 submit_result → 子 agent 回合：第 1 次 write_file，第 2 次 submit_result
      （artifacts 路径从 brief 里剥出来）
    - 其余 → "好的"
    每个请求都记进 requests，断言用。
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.plan_answer: dict | None = None
        self.review_answer: dict | None = None
        self.reminder_answer: dict | None = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.requests.append({"url": str(request.url), "body": body})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": m} for m in ("fake-main", "fake-worker")]})
        if request.url.path.endswith("/chat/completions"):
            return await self._chat(body)
        return httpx.Response(404)

    @staticmethod
    def _last_text(messages: list) -> str:
        for m in reversed(messages):
            if m.get("role") in ("user", "system"):
                c = str(m.get("content") or "")
                if c:
                    return c
        return ""

    async def _chat(self, body: dict) -> httpx.Response:
        messages = list(body.get("messages") or [])
        text_all = "\n".join(str(m.get("content") or "") for m in messages)
        last = self._last_text(messages)
        # 1) 提醒解析（app.on_reminder 的固定起手式）
        if "把群友的话解析成一个提醒" in text_all:
            return _assistant(self.reminder_answer if self.reminder_answer is not None else {"ok": False})
        # 2) 验收（coordinator._review 起手式；工具回合历史里也带这句话）
        if "正在验收子 agent 交回的成品" in text_all:
            if self.review_answer is not None:
                return _assistant(self.review_answer)
            # 默认：从 prompt 的「成品清单」里剥文件路径当 artifact，pass
            # 清单行形如 "- artifacts/T-1/index.html（123 字节）"；目录行末是 "/"
            artifact = ""
            hits = re.findall(r"artifacts/[A-Za-z0-9_\-/\.]+", text_all)
            file_hits = [h.rstrip("/.") for h in hits if re.search(r"\.[A-Za-z0-9]{1,8}$", h.rstrip("/."))]
            if file_hits:
                artifact = file_hits[0]
            return _assistant({"pass": True, "review": "脚本默认通过", "artifact": artifact, "note": "做好了请查收"})
        # 3) 计划（coordinator._plan 起手式）
        if "这是一个 QQ 群派的活" in text_all:
            return _assistant(
                self.plan_answer
                if self.plan_answer is not None
                else {
                    "criteria": ["包含 NAS 方案整理结论"],
                    "deliver_kind": "view",
                    "jobs": [{"brief": "整理成一页 NAS 方案汇总", "tools": ["write_file", "list_files", "read_file"]}],
                    "question": None,
                }
            )
        # 4) 子 agent 回合
        tools = body.get("tools") or []
        names = {((t.get("function") or {}).get("name") or "") for t in tools if isinstance(t, dict)}
        if "submit_result" in names:
            return self._worker_round(body, messages)
        return _raw_text("好的")

    def _worker_round(self, body: dict, messages: list) -> httpx.Response:
        turn = sum(1 for m in messages if m.get("role") == "assistant")
        brief = ""
        for m in messages:
            if m.get("role") == "user" and "artifacts/" in str(m.get("content") or ""):
                brief = str(m["content"])
                break
        artifact_rel = "artifacts/T-1/index.html"
        m = re.search(r"artifacts/[A-Za-z0-9_\-/\.]+", brief)
        if m:
            artifact_rel = m.group(0).rstrip("/")
            if not artifact_rel.endswith((".html", ".txt", ".md", ".csv", ".zip")):
                artifact_rel += "/index.html"
        if turn == 0:
            return _tool_calls(
                [("write_file", {"path": artifact_rel, "content": "<html><head><meta charset='utf-8'></head><body>NAS 方案整理</body></html>"})]
            )
        if turn == 1:
            return _tool_calls(
                [("submit_result", {"summary": "整理完成一页汇总", "data": {"artifact": artifact_rel}, "evidence": [artifact_rel]})]
            )
        return _raw_text("（多余的轮次）")

    def count(self, hint: str) -> int:
        """body 文本里出现 hint 的请求数。"""
        return sum(1 for r in self.requests if hint in json.dumps(r["body"], ensure_ascii=False, default=str))


class JevStub:
    """假 Jev：记录 body，按 questions.keys 给脚本化答案。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.answers: dict[str, dict] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.requests.append({"url": str(request.url), "body": body})
        questions = body.get("questions") or {}
        answers: dict[str, dict] = {}
        for key, q in questions.items():
            if key in self.answers:
                answers[key] = self.answers[key]
                continue
            if q.get("type") == "noul":
                answers[key] = {"type": "noul", "noul": 0.8}
            else:
                crit = list((q.get("criteria") or {}).keys())
                label = crit[0] if crit else "none"
                answers[key] = {
                    "type": "choice",
                    "choice": label,
                    "probabilities": {c: (0.9 if c == label else 0.033) for c in crit},
                    "confidence": 0.9,
                }
        return httpx.Response(200, json={"answers": answers})

    def set_choice(self, key: str, label: str, confidence: float, criteria: list[str], prob: float = 0.9) -> None:
        n = max(1, len(criteria) - 1)
        self.answers[key] = {
            "type": "choice",
            "choice": label,
            "probabilities": {c: (prob if c == label else round((1 - prob) / n, 4)) for c in criteria},
            "confidence": confidence,
        }

    def set_noul(self, key: str, value: float) -> None:
        self.answers[key] = {"type": "noul", "noul": value}


class HereNowStub:
    """假 here.now：create / PUT / finalize 三步都记录。"""

    def __init__(self) -> None:
        self.creates: list[dict] = []
        self.puts: list[dict] = []
        self.finalizes: list[dict] = []
        self.site_url = "https://e2e.here.now/"
        self.fail: bool = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/api/v1/publish"):
            if self.fail:
                return httpx.Response(500, json={"error": "boom"})
            body = json.loads(request.content or b"{}")
            self.creates.append({"url": str(request.url), "body": body})
            uploads = []
            for f in body.get("files", []):
                uploads.append(
                    {
                        "path": f.get("path"),
                        "method": "PUT",
                        "url": f"https://upload.fake/put/{f.get('path')}",
                        "headers": {"Content-Type": f.get("contentType") or "application/octet-stream"},
                    }
                )
            return httpx.Response(
                200,
                json={
                    "slug": "e2e-slug",
                    "siteUrl": self.site_url,
                    "upload": {
                        "versionId": "v1-e2e",
                        "uploads": uploads,
                        "finalizeUrl": "http://here.now/api/v1/finalize/op-e2e",
                    },
                    "claimUrl": "https://here.now/claim/e2e",
                    "claimToken": "claim-e2e",
                    "expiresAt": "2099-01-01T00:00:00Z",
                },
            )
        if request.method == "PUT":
            self.puts.append({"url": str(request.url), "body": request.content[:120].decode("utf-8", "replace")})
            return httpx.Response(200)
        if request.method == "POST" and "/finalize" in request.url.path:
            body = json.loads(request.content or b"{}")
            self.finalizes.append({"url": str(request.url), "body": body})
            return httpx.Response(200, json={"siteUrl": self.site_url, "status": "live"})
        return httpx.Response(404)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def travel(monkeypatch: pytest.MonkeyPatch) -> TravelClock:
    return TravelClock(monkeypatch)


@pytest.fixture
def host() -> HostResponses:
    return HostResponses()


@pytest.fixture
def openai() -> OpenAIStub:
    return OpenAIStub()


@pytest.fixture
def jev() -> JevStub:
    return JevStub()


@pytest.fixture
def herenow() -> HereNowStub:
    return HereNowStub()


def make_config(tmp_path: Path, **sections: Any) -> dict:
    """生成完整宿主样式配置（服务群 qq:900000001；direct 模式；data/workspace 都在 tmp_path）。

    模型 / Jev 的地址指向假端点（MockTransport 拦截，出不了网）。
    """
    raw = MaiWorkConfig().model_dump(mode="python")
    raw["groups"]["serve"] = [{"group": f"qq:{SERVE}"}]
    raw["plugin"]["enabled"] = True
    raw["storage"]["data_dir"] = str(tmp_path / "data")
    raw["environments"]["local_mode"] = "direct"
    raw["environments"]["workspace_root"] = str(tmp_path / "workspaces")
    raw["console"]["password"] = "e2e-密码"
    raw["console"]["listen"] = "127.0.0.1:18691"
    raw["models"].update(
        {
            "base_url": "http://fake.openai/v1",
            "api_key": MODEL_KEY,
            "main": "fake-main",
            "worker": "fake-worker",
        }
    )
    raw["jev"]["key_file"] = str(tmp_path / "typesafe_key")
    raw["approval"]["admins"] = [ADMIN]
    raw["approval"]["required"] = True
    for section, values in sections.items():
        if isinstance(raw.get(section), dict) and isinstance(values, dict):
            raw[section].update(values)
        else:
            raw[section] = values
    return raw


class World:
    """一整套端到端环境：插件 + 启好的 app + 各 stub + 时钟。"""

    def __init__(
        self,
        *,
        plugin: MaiWorkPlugin,
        app: MaiWorkApp,
        host: HostResponses,
        openai: OpenAIStub,
        jev: JevStub,
        herenow: HereNowStub,
        travel: TravelClock,
        tmp_path: Path,
        config: dict,
    ) -> None:
        self.plugin = plugin
        self.app = app
        self.host = host
        self.openai = openai
        self.jev = jev
        self.herenow = herenow
        self.travel = travel
        self.tmp_path = tmp_path
        self.config = config

    async def next_message(self, *args: Any, settle: bool = True, **kwargs: Any) -> dict:
        """给插件送一条消息。

        settle=True（默认）：钩子返回后立刻手动跑一轮 run_loop_once（把信号落库、
        发车发件箱、派工等），并再给 spawn 出去的活一点调度时间——整条链确定性。
        settle=False：只看钩子本身的行为（也不跑后台轮，比如「钩子 ≤1.5 秒」类断言）。
        """
        out = await self.plugin.maiwork_intake(**hook_message(*args, **kwargs))
        if settle:
            await self.app.run_loop_once()
            await asyncio.sleep(0.05)
        return out

    async def settle(self, rounds: int = 1) -> None:
        """手动把后台循环推一轮（让 spawn 出去的活在下一轮被巡检接到）。"""
        for _ in range(rounds):
            await self.app.run_loop_once()
            await asyncio.sleep(0.05)

    async def pump(self, seconds: float = 0.15) -> None:
        await asyncio.sleep(seconds)

    async def wait_task(self, task_id: str, *, timeout: float = 15.0) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            t = self.app.tasks.get(task_id)
            if t and str(t["status"]) in ("completed", "failed", "cancelled", "shelved"):
                return t
            await asyncio.sleep(0.05)
        t = self.app.tasks.get(task_id)
        raise AssertionError(f"任务 {task_id} 没在 {timeout}s 内完结（现在 {t and t['status']}）")

    def outbox_rows(self, status: str | None = None) -> list[Any]:
        sql = "SELECT * FROM outbox"
        args: tuple = ()
        if status:
            sql += " WHERE status=?"
            args = (status,)
        return self.app.store.read().execute(sql, args).fetchall()

    def ctx_calls(self, name: str) -> list[dict]:
        ctx = self.host.ctx
        assert ctx is not None
        return [kw for n, kw in ctx.calls if n == name]

    def sent_texts(self) -> list[str]:
        """所有 send.hybrid 的 text 内容。"""
        out: list[str] = []
        for kw in self.ctx_calls("send.hybrid"):
            for seg in kw.get("segments") or []:
                if seg.get("type") == "text":
                    out.append(str(seg.get("content") or ""))
        return out

    async def web_client(self) -> TestClient:
        """开一个开向 console 的 TestClient（unsafe=True 允许跨端口带 cookie）。

        调用方记得 await client.close()。
        """
        server = TestServer(self.app.console.app)
        client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
        await client.start_server()
        return client

    async def admin_client(self) -> TestClient:
        """已用 [console] password 登录的管理员 client。"""
        client = await self.web_client()
        resp = await client.post("/api/login", json={"password": "e2e-密码"})
        assert resp.status == 200, f"管理员登录失败：{await resp.text()}"
        return client


async def _make_world(
    tmp_path: Path,
    *,
    host: HostResponses,
    openai: OpenAIStub,
    jev: JevStub,
    herenow: HereNowStub,
    travel: TravelClock,
    config_overrides: dict | None = None,
    capability_probe: Any = None,
) -> World:
    (tmp_path / "typesafe_key").write_text(JEV_KEY, encoding="utf-8")
    cfg = make_config(tmp_path, **(config_overrides or {}))
    plugin = create_plugin()
    plugin._ctx = host.make_ctx()
    plugin.set_plugin_config(cfg)
    await plugin.on_load()
    app = plugin._app
    assert app is not None, "插件 on_load 后 app 应该启动"
    # 执行方式判定注入点要在第二次 start 之前设好（start 里才判定）
    if capability_probe is not None:
        app.capability_probe = capability_probe
    # 画像跑真 Profiles 会把假模型当提炼模型使唤，端到端里用 FakeProfiles：
    # 链路断言全部落在「宿主收到的调用 / 库里状态」上，FakeProfiles 记录调用
    app.profiles_cls = FakeProfiles
    # 第一次 start 用的默认 transport（不会真发网络）；现在换上注好的，重来一次栈
    app.http_transport = openai.transport()
    app.jev_transport = jev.transport()
    app.herenow_transport = herenow.transport()
    await app.stop()
    await app.start()
    assert app.started
    # 测试里完全手动驱动 run_loop_once；后台循环挂到 1 小时，绝不自己开火（确定性）
    app.loop_interval = 3600.0
    return World(
        plugin=plugin, app=app, host=host, openai=openai, jev=jev, herenow=herenow, travel=travel, tmp_path=tmp_path, config=cfg
    )


@pytest_asyncio.fixture
async def world(
    tmp_path: Path, host: HostResponses, openai: OpenAIStub, jev: JevStub, herenow: HereNowStub, travel: TravelClock
) -> Any:
    w = await _make_world(tmp_path, host=host, openai=openai, jev=jev, herenow=herenow, travel=travel)
    try:
        yield w
    finally:
        await w.plugin.on_unload()


# ---------------------------------------------------------------------------
# 场景 4：/mw 指令（固定回复、非管理员被拒、管理员批准）
# ---------------------------------------------------------------------------


class TestMwCommands:
    @pytest.mark.asyncio
    async def test_mw_plain_replies_fixed_text(self, world: World) -> None:
        """服务群里 /mw → 回一条固定话；reply 段指向原消息。"""
        out = await world.next_message(SERVE, USER1, "/mw", message_id="mw-1", session_id=SESSION, ts=world.travel())
        assert out == {"action": "continue"}
        await world.pump(0.3)
        sends = world.ctx_calls("send.hybrid")
        rows_dbg = [(r["key"], r["status"], r["error"]) for r in world.outbox_rows()]
        assert sends, f"该有一条 send.hybrid；outbox={rows_dbg}；ctx.calls={world.host.ctx.calls}"
        last = sends[-1]
        assert last["stream_id"] == SESSION
        segments = last["segments"]
        reply_segs = [s for s in segments if s.get("type") == "reply"]
        text_segs = [s for s in segments if s.get("type") == "text"]
        assert reply_segs and reply_segs[0]["data"]["target_message_id"] == "mw-1"
        assert text_segs and "MaiWork 本群情况" in text_segs[0]["content"]
        rows = [r for r in world.outbox_rows() if "cmd:mw-1" in r["key"]]
        assert rows and rows[0]["status"] == "sent"

    @pytest.mark.asyncio
    async def test_mw_approve_denied_for_non_admin(self, world: World, travel: TravelClock) -> None:
        """非管理员 /mw 批准 R-x → 被拒的固定话，请求状态不变。"""
        world.jev.set_choice("kind", "prepare", 0.92, ["prepare", "goal", "reminder", "none"])
        await world.next_message(
            SERVE, USER1, "帮我把这周大家聊的 NAS 方案整理成一页", message_id="at-1", is_at=True, session_id=SESSION, ts=travel()
        )
        await world.pump(0.3)
        pending = world.app.approvals.pending_view(SERVE)
        assert len(pending) == 1
        rid = pending[0]["id"]
        before = len(world.ctx_calls("send.hybrid"))
        await world.next_message(SERVE, USER1, f"/mw 批准 {rid}", message_id="mw-x", session_id=SESSION, ts=travel())
        await world.pump(0.3)
        sends = world.ctx_calls("send.hybrid")
        assert len(sends) > before
        text = sends[-1]["segments"][-1]["content"]
        assert "只有 bot 管理员" in text
        assert len(world.app.approvals.pending_view(SERVE)) == 1  # 状态不变

    @pytest.mark.asyncio
    async def test_mw_approve_by_admin_lands_task_and_runs(self, world: World, travel: TravelClock) -> None:
        """管理员 /mw 批准 → 请求 approved、落成任务并开工。"""
        world.jev.set_choice("kind", "prepare", 0.92, ["prepare", "goal", "reminder", "none"])
        await world.next_message(
            SERVE, USER1, "帮我把这周大家聊的 NAS 方案整理成一页", message_id="at-2", is_at=True, session_id=SESSION, ts=travel()
        )
        rid = world.app.approvals.pending_view(SERVE)[0]["id"]
        await world.next_message(SERVE, ADMIN, f"/mw 批准 {rid}", message_id="mw-ok", session_id=SESSION, ts=travel())
        row = world.app.store.read().execute("SELECT status, task_id FROM requests WHERE id=?", (rid,)).fetchone()
        assert row["status"] == "approved"
        tid = str(row["task_id"])
        assert tid.startswith("T-")
        t = await world.wait_task(tid, timeout=15)
        assert t["status"] == "completed"
        texts = world.sent_texts()
        assert any("已批准" in x and "开工" in x for x in texts)


# ---------------------------------------------------------------------------
# 场景 1：派活全链（展示类）：钩子 Jev 判 prepare → 待批 → 网页批准 →
# coordinator 干活 → 验收 → here.now 三步 → send.hybrid 说明 → 任务详情/群友版
# ---------------------------------------------------------------------------


class TestFullChainViewDelivery:
    @pytest.mark.asyncio
    async def test_hook_returns_continue_within_1500ms(self, world: World, travel: TravelClock) -> None:
        """服务群 @：钩子返回 continue 且远快于 1.5 秒；Jev 判 prepare → 待批。"""
        world.jev.set_choice("kind", "prepare", 0.92, ["prepare", "goal", "reminder", "none"])
        start = asyncio.get_running_loop().time()
        out = await world.next_message(
            SERVE, USER1, "帮我把这周大家聊的 NAS 方案整理成一页",
            message_id="at-chain", is_at=True, session_id=SESSION, ts=travel(),
            settle=False,  # 只量钩子本身
        )
        elapsed = asyncio.get_running_loop().time() - start
        assert out == {"action": "continue"}
        assert elapsed < 1.5
        # Jev 请求带着 intake 的 question 发了一次
        assert any('{"kind"' in json.dumps(r["body"], ensure_ascii=False) or "kind" in (r["body"].get("questions") or {}) for r in world.jev.requests)

    @pytest.mark.asyncio
    async def test_full_chain_view_delivery(self, world: World, travel: TravelClock) -> None:
        world.jev.set_choice("kind", "prepare", 0.92, ["prepare", "goal", "reminder", "none"])
        await world.next_message(
            SERVE, USER1, "帮我把这周大家聊的 NAS 方案整理成一页",
            message_id="at-chain", is_at=True, session_id=SESSION, ts=travel(),
        )
        # ---- 待批出现在网页 ----
        client = await world.admin_client()
        try:
            resp = await client.get("/api/groups/900000001")
            assert resp.status == 200
            view = await resp.json()
            pending = (view.get("tasks") or {}).get("pending") or []
            assert len(pending) == 1
            rid = pending[0]["id"]
            assert "NAS" in (pending[0]["quote"] or pending[0]["title"])
            assert "Jev" in pending[0]["via"]
        finally:
            await client.close()

        # ---- 网页批准 ----
        client = await world.admin_client()
        try:
            resp = await client.post(f"/api/requests/{rid}/approve")
            assert resp.status == 200, await resp.text()
            res = await resp.json()
            tid = str(res["task_id"])
        finally:
            await client.close()

        # ---- 等干活（批准时 spawn 的后台任务）----
        t = await world.wait_task(tid, timeout=20)
        assert t["status"] == "completed"

        # ---- outbox flush（交付）----
        await world.app.outbox.flush(world.travel())
        # here.now 三步
        assert len(world.herenow.creates) == 1
        assert len(world.herenow.puts) == len(world.herenow.creates[0]["body"]["files"])
        assert len(world.herenow.finalizes) == 1
        # send.hybrid：成品说明里带 here.now 链接
        texts = world.sent_texts()
        assert any(world.herenow.site_url in x for x in texts), f"说明里没带上链接：{texts}"
        # 子 agent 真写了文件
        artifact = world.tmp_path / "workspaces" / f"g{SERVE}" / "artifacts" / tid / "index.html"
        assert artifact.is_file()

        # ---- 任务详情（管理员）----
        client = await world.admin_client()
        try:
            resp = await client.get(f"/api/tasks/{tid}")
            assert resp.status == 200
            detail = await resp.json()
        finally:
            await client.close()
        delivery = detail.get("delivery") or []
        kinds = [d.get("kind") for d in delivery]
        assert "here.now" in kinds
        assert detail.get("undelivered") is False
        herenow_items = [d for d in delivery if d.get("kind") == "here.now"]
        assert herenow_items and herenow_items[0].get("url") == world.herenow.site_url
        # 管理员看：有 env / timeline
        assert "env" in detail and "timeline" in detail

        # ---- 群友看：没有 env / timeline ----
        token = world.app.token_of(SERVE)
        client2 = await world.web_client()
        try:
            resp = await client2.get(f"/api/tasks/{tid}", headers={"X-MW-Group": token})
            assert resp.status == 200
            member_detail = await resp.json()
        finally:
            await client2.close()
        assert "env" not in member_detail
        assert "timeline" not in member_detail
        # 但群友看得到交付块（她的链接）
        assert member_detail.get("delivery"), "群友也看得到交付记录"


# ---------------------------------------------------------------------------
# 场景 3：免批（[approval] exempt_users 含发起人 → 不出现在待批，直接开工）
# ---------------------------------------------------------------------------


class TestExemptAutoApprove:
    @pytest.mark.asyncio
    async def test_exempt_user_skips_pending_and_runs(
        self, tmp_path, host, openai, jev, herenow, travel
    ) -> None:
        world = await _make_world(
            tmp_path, host=host, openai=openai, jev=jev, herenow=herenow, travel=travel,
            config_overrides={"approval": {"exempt_users": [USER1]}},
        )
        try:
            world.jev.set_choice("kind", "prepare", 0.92, ["prepare", "goal", "reminder", "none"])
            await world.next_message(
                SERVE, USER1, "帮我把这周大家聊的 NAS 方案整理成一页",
                message_id="at-ex", is_at=True, session_id=SESSION, ts=travel(),
            )
            # 不出现在待批
            assert world.app.approvals.pending_view(SERVE) == []
            # 直接落成任务
            row = world.app.store.read().execute("SELECT status, task_id FROM requests ORDER BY created DESC").fetchone()
            assert row["status"] == "approved"
            tid = str(row["task_id"])
            assert tid.startswith("T-")
            await world.settle()  # 巡检捞起来开工
            t = await world.wait_task(tid, timeout=15)
            assert t["status"] == "completed"
        finally:
            await world.plugin.on_unload()

    @pytest.mark.asyncio
    async def test_idea_want_web_approve_marks_idea_started(
        self, tmp_path, host, openai, jev, herenow, travel
    ) -> None:
        """构想「想要」→ 待批 → 网页手动批准：构想变 started 并回写 task_id（回归缺口）。"""
        world = await _make_world(
            tmp_path, host=host, openai=openai, jev=jev, herenow=herenow, travel=travel,
        )
        try:
            # 直接往库里塞一个构想 + 想法 view（模拟「构想卡片已经在了」）
            now = world.travel()
            with world.app.store.tx() as conn:
                cur = conn.execute(
                    "INSERT INTO ideas (group_id, icon, title, body, basis, step, effort, state, created, updated)"
                    " VALUES (?, 'bulb', '做个 NAS 清单页', '把大家聊的 NAS 做成单页', '群里老聊', '先列型号', '半天', 'wanted', ?, ?)",
                    (SERVE, now, now),
                )
                idea_id = int(cur.lastrowid or 0)
                rid = None
            # 「想要」→ 落成待批（要批的路径）
            res = world.app.approvals.create(
                SERVE, kind="task", title="做个 NAS 清单页", quote="把大家聊的 NAS 做成单页",
                via="来自构想", requester_id="", requester_name="阿柒（网页）", idea_id=idea_id,
            )
            with world.app.store.tx() as conn:
                conn.execute("UPDATE ideas SET state='pending' WHERE id=?", (idea_id,))
            rid = str(res["id"])

            client = await world.admin_client()
            try:
                resp = await client.post(f"/api/requests/{rid}/approve")
                assert resp.status == 200, await resp.text()
                body = await resp.json()
                tid = str(body["task_id"])
            finally:
                await client.close()

            row = world.app.store.read().execute("SELECT state, task_id FROM ideas WHERE id=?", (idea_id,)).fetchone()
            assert row["state"] == "started"
            assert str(row["task_id"]) == tid

            # 批准后的任务照样能在后台完成（网页 spawn 了 run_task）
            t = await world.wait_task(tid, timeout=15)
            assert t["status"] == "completed"
        finally:
            await world.plugin.on_unload()


# ---------------------------------------------------------------------------
# 场景 6：非服务群零接触；场景 7：睡觉时段零 Jev 零发送
# ---------------------------------------------------------------------------


class TestNonServedGroupZeroTouch:
    @pytest.mark.asyncio
    async def test_non_served_group_zero_calls(self, world: World, travel: TravelClock, caplog) -> None:
        """对 111 群发 @、/mw、普通消息：FakeCtx 零调用、假模型零请求、假 Jev 零请求、库里没这个群。"""
        before_ctx = len(world.host.ctx.calls)
        before_openai = len(world.openai.requests)
        before_jev = len(world.jev.requests)

        for kwargs in (
            dict(text="普通消息", message_id="n-1", is_at=False),
            dict(text="帮我把这周 NAS 方案整理成一页", message_id="n-2", is_at=True),
            dict(text="/mw", message_id="n-3", is_at=False),
            dict(text="/mw 批准 R-1", message_id="n-4", is_at=False),
        ):
            out = await world.next_message(OTHER, USER1, kwargs["text"], message_id=kwargs["message_id"], session_id="sess-other", ts=travel())
            assert out == {"action": "continue"}

        # 后台也跑一轮（万一走了什么路径，不该有 111 的东西）
        await world.app.run_loop_once()

        assert len(world.host.ctx.calls) == before_ctx, f"非服务群后有宿主调用：{world.host.ctx.calls[before_ctx:]}"
        assert len(world.openai.requests) == before_openai, "假模型有新请求"
        assert len(world.jev.requests) == before_jev, "假 Jev 有新请求"
        # 库里没有这个群
        row = world.app.store.read().execute("SELECT 1 FROM groups WHERE group_id=?", (OTHER,)).fetchone()
        assert row is None
        # 日志里也不该有这个群的活动（软放值得一查：至少没有 ERROR）
        errs = [r for r in caplog.records if r.levelno >= logging.ERROR and OTHER in r.getMessage()]
        assert not errs


class TestQuietHoursZeroTopic:
    @pytest.mark.asyncio
    async def test_quiet_hours_topics_check_silent(self, tmp_path, host, openai, jev, herenow, travel) -> None:
        """把时钟拨到 23:30，冷场其他条件都满足：topics.check 零 Jev、零发送。"""
        world = await _make_world(
            tmp_path, host=host, openai=openai, jev=jev, herenow=herenow, travel=travel,
        )
        try:
            # 先把群喂「安静」：候选 + 画像成形 + 距离上次消息足够久
            from CharTyr_MaiWork.maiwork import clock as _clk

            base = _bj_epoch(2026, 10, 19, 20, 0)
            with world.app.store.tx() as conn:
                conn.execute(
                    "UPDATE groups SET profile_ready_ts=?, session_id=?, last_msg_ts=? WHERE group_id=?",
                    (base - 86400, SESSION, base - 7200, SERVE),
                )
            world.app.topics.add_candidate(SERVE, kind="news", ref_id=1, title="NAS 新玩法", brief="b", ttl_h=24)
            # FakeProfiles：usual_gap 返回 120s（这个钟点平时有人）；pulse 无所谓
            profiles = world.app.profiles
            profiles.usual_gap_value = 120.0

            before_jev = len(world.jev.requests)
            before_sends = len(world.ctx_calls("send.hybrid"))
            # 23:30（睡觉时段 23:00-08:00）
            travel.set(_bj_epoch(2026, 10, 19, 23, 30))
            res = await world.app.topics.check(SERVE, world.travel())
            assert str(res).startswith("skip")
            assert len(world.jev.requests) == before_jev, "睡觉时段问 Jev 了"
            assert len(world.ctx_calls("send.hybrid")) == before_sends, "睡觉时段发消息了"
        finally:
            await world.plugin.on_unload()


# ---------------------------------------------------------------------------
# 场景 5：成员目标提醒（@ → Jev reminder → 主模型解析 → 到点 @ 提醒一次，不重复）
# ---------------------------------------------------------------------------


class TestMemberReminder:
    @pytest.mark.asyncio
    async def test_reminder_full_arc(self, world: World, travel: TravelClock) -> None:
        now = travel()
        # 用当天 20:00（醒着），躲开睡觉时段
        remind_ts = _bj_epoch(2026, 10, 19, 20, 0)
        assert remind_ts > now
        world.jev.set_choice("kind", "reminder", 0.91, ["prepare", "goal", "reminder", "none"])
        world.openai.reminder_answer = {"ok": True, "title": "交稿", "due_ts": remind_ts, "remind_ts": remind_ts}

        out = await world.next_message(
            SERVE, USER1, "明晚 8 点提醒我交稿", message_id="rem-1",
            is_at=True, session_id=SESSION, ts=travel(), settle=False,
        )
        assert out == {"action": "continue"}
        await world.settle(3)

        view = world.app.goals.view(SERVE)
        member = view.get("member") or []
        assert len(member) == 1
        goal_id = member[0]["id"]
        assert goal_id.startswith("M-")
        assert "交稿" in str(member[0]["title"])
        assert float(member[0]["remind_ts"]) == remind_ts
        assert any("记下了" in x for x in world.sent_texts()), f"回话：{world.sent_texts()}"

        travel.set(remind_ts)
        before = len(world.ctx_calls("send.hybrid"))
        # 一轮入队（goals 到期在巡检 step 3）、一轮真发（outbox flush 在 step 1）
        await world.settle(2)
        reminder_texts = [x for x in world.sent_texts() if "@阿柒 提醒：交稿" == x.strip()]
        assert len(reminder_texts) == 1, f"提醒该发一次：{world.sent_texts()}"
        assert len(world.ctx_calls("send.hybrid")) > before

        # 再跑：目标到期「问进展」也会发一条（独立功能，不算重复提醒），把它发完
        await world.settle(2)
        # 之后提醒本身绝不再发（remind_ts 已置空）
        remind_before = [x for x in world.sent_texts() if "@阿柒 提醒：交稿" == x.strip()]
        await world.settle(2)
        remind_after = [x for x in world.sent_texts() if "@阿柒 提醒：交稿" == x.strip()]
        assert len(remind_after) == len(remind_before) == 1


# ---------------------------------------------------------------------------
# 场景 8：推送上限（status/delivery 类超限推迟；command 和 error 不受限）
# ---------------------------------------------------------------------------


class TestPushCaps:
    @pytest.mark.asyncio
    async def test_daily_cap_defers_status_but_not_command_error(self, world: World, travel: TravelClock) -> None:
        # 先喂一条普通消息：让 groups.session_id 就位（text 发送要 session）
        await world.next_message(SERVE, USER1, "打个底", message_id="seed-1", session_id=SESSION, ts=travel())
        now = travel()
        pushes = world.app.pushes
        assert world.app.get_settings().delivery.push_per_day == 3  # 默认 3
        # 已有 3 条受限推送（今天，醒着的时间）。
        # 注意（S2 修复）：delivery 按 02 §6.4 不计入每日上限，这里必须用受限类型凑额度。
        pushes.record(SERVE, "status", "已有 1", now)
        pushes.record(SERVE, "topic", "已有 2", now)
        pushes.record(SERVE, "status", "已有 3", now)
        ob = world.app.outbox
        ob.enqueue("cap:status", SERVE, "text", {"text": "状态汇报一条", "push_kind": "status"})
        ob.enqueue("cap:command", SERVE, "text", {"text": "/mw 回话", "push_kind": "command"})
        ob.enqueue("cap:error", SERVE, "text", {"text": "【故障】测试错误", "push_kind": "error"})
        await ob.flush(now)
        rows = {r["key"]: r for r in world.outbox_rows()}
        # status：推迟（仍 pending、not_before 到明天、error 字段带原因）
        assert rows["cap:status"]["status"] == "pending"
        assert float(rows["cap:status"]["not_before"]) > now
        assert "推迟" in str(rows["cap:status"]["error"] or "")
        # command / error：已发
        assert rows["cap:command"]["status"] == "sent"
        assert rows["cap:error"]["status"] == "sent"
        # send.hybrid 只发了 command 和 error 两条
        assert len(world.ctx_calls("send.hybrid")) == 2
        # 拨到明天 09:00（醒了）再 flush：status 出去了
        travel.set(_bj_epoch(2026, 10, 20, 9, 0))
        await ob.flush(world.travel())
        assert [r for r in world.outbox_rows() if r["key"] == "cap:status"][0]["status"] == "sent"


# ---------------------------------------------------------------------------
# 场景 2：文件类交付（群文件先、失败回落 here.now、超时 uncertain 不重传）
# ---------------------------------------------------------------------------


async def _run_file_task(world: World, travel: TravelClock, title: str) -> str:
    """一条 deliver_kind=file 的任务跑到 completed，返回 task_id。"""
    world.openai.plan_answer = {
        "criteria": ["写一份 NAS 选购清单"],
        "deliver_kind": "file",
        "jobs": [{"brief": f"写一份关于「{title}」的清单文件", "tools": ["write_file", "list_files"]}],
        "question": None,
    }
    world.jev.set_choice("kind", "prepare", 0.9, ["prepare", "goal", "reminder", "none"])
    await world.next_message(SERVE, USER1, f"帮我整理一份 {title}", message_id=f"at-{title}", is_at=True, session_id=SESSION, ts=travel())
    rid = world.app.approvals.pending_view(SERVE)[-1]["id"]
    client = await world.admin_client()
    try:
        resp = await client.post(f"/api/requests/{rid}/approve")
        assert resp.status == 200
        tid = str((await resp.json())["task_id"])
    finally:
        await client.close()
    t = await world.wait_task(tid, timeout=20)
    assert t["status"] == "completed"
    await world.app.outbox.flush(world.travel())
    return tid


class TestFileDelivery:
    @pytest.mark.asyncio
    async def test_upload_called_once_then_note(self, world: World, travel: TravelClock) -> None:
        """首选群文件：upload_group_file 恰好一次 → 补一条说明 send.hybrid。"""
        tid = await _run_file_task(world, travel, "nas清单")
        assert len(world.host.uploads) == 1
        up = world.host.uploads[0]
        assert up["group_id"] == SERVE
        assert str(up["name"]).endswith(".html")
        # 说明 text 的 send.hybrid 发出过
        assert any("请查收" in x for x in world.sent_texts()), f"没补上说明：{world.sent_texts()}"
        # 产物没走 here.now
        assert world.herenow.creates == []
        # 再 flush 不再上传（发送完成的不重发）
        await world.app.outbox.flush(world.travel())
        await world.app.outbox.flush(world.travel())
        assert len(world.host.uploads) == 1

    @pytest.mark.asyncio
    async def test_upload_fail_falls_back_to_herenow(self, world: World, travel: TravelClock) -> None:
        """上传返回失败 → 自动回落 here.now；回落成功后有 Web 链接说明。"""
        world.host.uploads_fail = True
        tid = await _run_file_task(world, travel, "nas清单2")
        # 回落 here.now 三步走了
        assert len(world.herenow.creates) >= 1
        assert len(world.herenow.finalizes) >= 1
        texts = world.sent_texts()
        assert any(world.herenow.site_url in x for x in texts), f"没给出网页链接说明：{texts}"

    @pytest.mark.asyncio
    async def test_upload_timeout_uncertain_no_retry(self, world: World, travel: TravelClock) -> None:
        """上传超时 → uncertain，绝不自动重传（上传不幂等，docs/02 §6.5）。"""
        world.host.uploads_timeout = True
        tid = await _run_file_task(world, travel, "nas清单3")
        rows = [r for r in world.outbox_rows() if r["task_id"] == tid and r["kind"] == "file"]
        assert rows and rows[0]["status"] == "uncertain"
        # 再 flush：不重传
        await world.app.outbox.flush(world.travel())
        await world.app.outbox.flush(world.travel())
        assert len(world.host.uploads) == 0


# ---------------------------------------------------------------------------
# 场景 10：本机受限（macOS / 不是 root / 没 systemd）——不跑命令、工作区在数据目录下
# ---------------------------------------------------------------------------


def _stopped_decision(run_as: str = "maiwork") -> Any:
    from CharTyr_MaiWork.maiwork.environments import capability

    return capability.Decision(
        mode="stopped", ok=False, exec_kind="plugin", unit_user="",
        reason="这台机器不是 Linux（Windows / macOS）", hint="换 Railway",
        log_line="本机干活：不能用——这台机器不是 Linux",
    )


class TestStoppedMode:
    @pytest.mark.asyncio
    async def test_stopped_no_command_tools_root_in_data_dir(
        self, tmp_path: Path, host: HostResponses, openai: OpenAIStub, jev: JevStub,
        herenow: HereNowStub, travel: TravelClock,
    ) -> None:
        """受限：命令工具没注册、文件工具还在；工作区根=data_dir/workspaces；健康项说明白。"""
        w = await _make_world(
            tmp_path, host=host, openai=openai, jev=jev, herenow=herenow, travel=travel,
            config_overrides={"environments": {"railway": False, "workspace_root": str(tmp_path / "ws-配置了但用不上")}},
            capability_probe=_stopped_decision,
        )
        try:
            assert w.app.capability.mode == "stopped"
            for name in ("run_command", "start_process", "check_process", "stop_process"):
                assert w.app.tools.get(name, "worker") is None, name
            assert w.app.tools.get("write_file", "worker") is not None
            s = w.app.get_settings()
            assert s.environments.workspace_root == s.data_dir / "workspaces"
            # outbox 的交付闸和 LocalEnv 看到的是同一个根
            assert w.app.delivery._task_artifact_dir("T-1", SERVE) == (
                s.data_dir / "workspaces" / f"g{SERVE}" / "artifacts" / "T-1"
            ).resolve()
            from CharTyr_MaiWork.maiwork.console.views import _localenv_health

            h = _localenv_health(w.app)
            assert h["name"] == "本机" and h["state"] == "off"
            assert "没开" in h["text"] and "临时机器" in h["text"]
        finally:
            await w.plugin.on_unload()

    @pytest.mark.asyncio
    async def test_stopped_task_needing_command_fails_with_honest_message(
        self, tmp_path: Path, host: HostResponses, openai: OpenAIStub, jev: JevStub,
        herenow: HereNowStub, travel: TravelClock,
    ) -> None:
        """要跑命令的活：本机受限且没开一次性机器 → 任务判失败，群里说清「做不了」。"""
        w = await _make_world(
            tmp_path, host=host, openai=openai, jev=jev, herenow=herenow, travel=travel,
            config_overrides={"environments": {"railway": False}},
            capability_probe=_stopped_decision,
        )
        try:
            w.openai.plan_answer = {
                "criteria": ["在本机跑一条命令"],
                "deliver_kind": "text",
                "jobs": [{"brief": "跑 ls 看看目录", "tools": ["run_command"]}],
                "question": None,
            }
            w.jev.set_choice("kind", "prepare", 0.9, ["prepare", "goal", "reminder", "none"])
            await w.next_message(
                SERVE, USER1, "帮我跑个命令", message_id="at-cmd", is_at=True,
                session_id=SESSION, ts=travel(),
            )
            rid = w.app.approvals.pending_view(SERVE)[-1]["id"]
            client = await w.admin_client()
            try:
                resp = await client.post(f"/api/requests/{rid}/approve")
                assert resp.status == 200
                tid = str((await resp.json())["task_id"])
            finally:
                await client.close()
            t = await w.wait_task(tid, timeout=20)
            assert t["status"] == "failed", f"该直接判失败：{t}"
            assert "不能隔离跑命令" in str(t["review"]), t["review"]
            await w.app.outbox.flush(w.travel())
            assert any("不能隔离跑命令" in x for x in w.sent_texts()), w.sent_texts()
        finally:
            await w.plugin.on_unload()


# ---------------------------------------------------------------------------
# 场景 9：重启不重复上传（sending → uncertain，不重放）
# ---------------------------------------------------------------------------


class TestRestartNoReupload:
    @pytest.mark.asyncio
    async def test_recover_marks_sending_uncertain_no_replay(
        self, tmp_path, host, openai, jev, herenow, travel
    ) -> None:
        world = await _make_world(
            tmp_path, host=host, openai=openai, jev=jev, herenow=herenow, travel=travel,
        )
        try:
            # 塞一条 sending 的上传项（模拟上次崩在上传中）
            oid = world.app.outbox.enqueue(
                "task:T-x:deliver", SERVE, "file",
                {"path": "/tmp/x.html", "name": "x.html", "note": "说明", "push_kind": "delivery"},
                task_id="T-x",
            )
            with world.app.store.tx() as conn:
                conn.execute("UPDATE outbox SET status='sending' WHERE id=?", (oid,))
            cfg = json.loads(json.dumps(world.config))
        finally:
            await world.plugin.on_unload()

        # 同一 data_dir 新建 app（重启 recover 在 start 里执行）
        plugin2 = create_plugin()
        plugin2._ctx = host.make_ctx()
        plugin2.set_plugin_config(cfg)
        await plugin2.on_load()
        app2 = plugin2._app
        assert app2 is not None
        try:
            row = app2.store.read().execute("SELECT status, error FROM outbox WHERE id=?", (oid,)).fetchone()
            assert row["status"] == "uncertain"
            assert "中断" in str(row["error"] or "")
            uploads_before = len(host.uploads)
            await app2.outbox.flush(travel())
            assert len(host.uploads) == uploads_before, "uncertain 不该被自动重发"
        finally:
            await plugin2.on_unload()


# ---------------------------------------------------------------------------
# 场景 10：网页不泄密（假密钥不出现在任何接口响应文本 / 日志 / 群消息）
# ---------------------------------------------------------------------------


class TestNoSecretLeak:
    @pytest.mark.asyncio
    async def test_fake_keys_absent_everywhere(self, world: World, travel: TravelClock, caplog) -> None:
        world.jev.set_choice("kind", "prepare", 0.9, ["prepare", "goal", "reminder", "none"])
        await world.next_message(SERVE, USER1, "帮我把 NAS 方案整理成一页", message_id="at-leak", is_at=True, session_id=SESSION, ts=travel())
        rid = world.app.approvals.pending_view(SERVE)[0]["id"]
        client = await world.admin_client()
        texts: list[str] = []
        tid = ""
        try:
            resp = await client.post(f"/api/requests/{rid}/approve")
            assert resp.status == 200
            tid = str((await resp.json())["task_id"])
        finally:
            await client.close()
        t = await world.wait_task(tid, timeout=20)
        await world.app.outbox.flush(world.travel())

        client = await world.admin_client()
        try:
            for path in (
                "/api/groups/900000001",
                "/api/settings",
                f"/api/tasks/{tid}",
                "/api/me",
            ):
                r = await client.get(path)
                assert r.status == 200, path
                texts.append(await r.text())
        finally:
            await client.close()

        for s in (MODEL_KEY, JEV_KEY):
            for body in texts:
                assert s not in body, f"接口响应出现了密钥 {s[:8]}…"
            assert s not in caplog.text, f"日志里出现了密钥 {s[:8]}…"
            for msg in world.sent_texts():
                assert s not in msg, f"群消息里出现了密钥 {s[:8]}…"
        # 设置接口：只给 key_set 布尔值、不回密钥本体
        settings_body = json.loads(texts[1])
        models = settings_body.get("models") or {}
        assert models.get("key_set") is True
        assert "api_key" not in models
