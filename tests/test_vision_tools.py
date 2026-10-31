"""看图 / 视觉能力（2026-10-31 任务）：多模态协议适配 + 视觉候选链 + 看图工具。

Step 1（本文件前半）：内部统一 OpenAI 风格 `image_url` 图片块，发请求时按协议翻译——
- OpenAI /chat/completions：图片块原样透传；
- Responses /responses：`image_url` → `input_image`；
- Anthropic /v1/messages：`image_url` 的 data URI → `image` + base64 source；
- model_calls 日志绝不落原始 base64（占位符 + 长度）；
- compaction 的图片块固定 1600 tokens（1MB 图不会被算成百万 token）；
- Models.vision_candidates / has_vision：先取岗位候选链里 vision=True 的，
  再补模型库里标了 vision 的聊天条目（按 entry_id 去重，use=image 不算）。

Step 2（本文件后半）：`read_image`（worker）/ `inspect_image`（main）两个看图工具——
魔数校验（不认扩展名）、10MB 上限、复用路径越界 / 成品目录隔离闸；没配视觉模型时
返回中文错（不调模型）。`read_file` / `inspect_file` 读到 PNG/JPEG/GIF/WebP 魔数
直接报错引导改用看图工具。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from CharTyr_MaiWork.maiwork.compaction import estimate_message_tokens
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.environments.local import LocalEnv
from CharTyr_MaiWork.maiwork.models import (
    Models,
    _build_anthropic_request,
    _build_openai_request,
    _build_responses_request,
)
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import ToolContext, Tools
from CharTyr_MaiWork.maiwork.tools_exec import register_exec_tools

from fakes import FakeHost

SECRET = "visionsecret123"
PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).decode()
JPEG_B64 = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 64).decode()
DATA_PNG_URL = f"data:image/png;base64,{PNG_B64}"
DATA_JPEG_URL = f"data:image/jpeg;base64,{JPEG_B64}"

_IMG_MSG = {
    "role": "user",
    "content": [
        {"type": "text", "text": "这张图里写了什么"},
        {"type": "image_url", "image_url": {"url": DATA_PNG_URL}},
    ],
}


def _settings(settings_dict: dict):
    settings, problems = load_settings(settings_dict)
    assert problems == [], problems
    return settings


class _FakeAgents:
    """Models 只调 agents.profile(kind)：小假对象（同 test_models_protocols 的写法）。"""

    def __init__(self, mapping: dict | None = None) -> None:
        self._m = mapping or {}

    def profile(self, kind: str) -> dict:
        d = {"kind": kind, "title": kind, "model": "", "effort": "", "backup": "",
             "enabled": True, "skills": None, "tools": None}
        d.update(self._m.get(kind, {}))
        return dict(d)


# ----------------------------------------------------------------------
# Step 1：三协议图片块序列化
# ----------------------------------------------------------------------


class TestProtocolImageBlocks:
    def test_openai_preserves_image_url_block(self) -> None:
        body = _build_openai_request(
            "m", [_IMG_MSG], tools=None, json_mode=False, max_tokens=64,
        )
        user = [m for m in body["messages"] if m.get("role") == "user"][0]
        assert user["content"] == _IMG_MSG["content"]
        assert user["content"][1]["image_url"]["url"] == DATA_PNG_URL

    def test_responses_maps_image_url_to_input_image(self) -> None:
        body = _build_responses_request(
            "m", [_IMG_MSG], tools=None, json_mode=False, max_tokens=64,
        )
        items = [i for i in body["input"] if i.get("type") == "message" and i.get("role") == "user"]
        assert len(items) == 1
        parts = items[0]["content"]
        assert parts[0] == {"type": "input_text", "text": "这张图里写了什么"}
        assert parts[1] == {"type": "input_image", "image_url": DATA_PNG_URL}

    def test_responses_keeps_non_data_url(self) -> None:
        msg = {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://x.test/a.png"}},
        ]}
        body = _build_responses_request("m", [msg], tools=None, json_mode=False, max_tokens=64)
        parts = body["input"][0]["content"]
        assert parts == [{"type": "input_image", "image_url": "https://x.test/a.png"}]

    def test_anthropic_maps_data_uri_to_base64_image(self) -> None:
        body = _build_anthropic_request(
            "claude-x", [_IMG_MSG], tools=None, json_mode=False, max_tokens=64,
        )
        user = body["messages"][0]
        assert user["role"] == "user"
        blocks = user["content"]
        assert blocks[0] == {"type": "text", "text": "这张图里写了什么"}
        assert blocks[1] == {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64},
        }

    def test_anthropic_maps_second_mime(self) -> None:
        msg = {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": DATA_JPEG_URL}},
        ]}
        body = _build_anthropic_request("m", [msg], tools=None, json_mode=False, max_tokens=64)
        block = body["messages"][0]["content"][0]
        assert block["type"] == "image"
        assert block["source"] == {"type": "base64", "media_type": "image/jpeg", "data": JPEG_B64}

    def test_anthropic_http_url_image(self) -> None:
        msg = {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://x.test/a.png"}},
        ]}
        body = _build_anthropic_request("m", [msg], tools=None, json_mode=False, max_tokens=64)
        block = body["messages"][0]["content"][0]
        assert block["type"] == "image" and block["source"]["type"] == "url"

    def test_openai_json_mode_keeps_image_blocks(self) -> None:
        body = _build_openai_request(
            "m", [_IMG_MSG], tools=None, json_mode=True, max_tokens=64,
        )
        user = [m for m in body["messages"] if m.get("role") == "user"][0]
        assert user["content"][1]["type"] == "image_url"


# ----------------------------------------------------------------------
# Step 1：日志脱敏（base64 绝不入库）
# ----------------------------------------------------------------------


class TestLogRedaction:
    def test_log_request_omits_base64_payload(self) -> None:
        payload = Models._build_log_request([_IMG_MSG], None, False, [])
        assert PNG_B64 not in payload
        assert "data:image/png;base64" not in payload or "已省略" in payload
        data = json.loads(payload)
        user = data["messages"][0]
        assert user["role"] == "user"
        # content 落库是 JSON 字符串：里面图片块仍在（标了类型），URL 不是原始 data URI
        blocks = json.loads(user["content"])
        assert blocks[0]["type"] == "text"
        block = blocks[1]
        assert block["type"] == "image_url"
        assert PNG_B64 not in json.dumps(block, ensure_ascii=False)
        assert "已省略" in str(block["image_url"]["url"])

    def test_log_request_http_url_image_not_raw(self) -> None:
        long_url = "https://x.test/" + "a" * 9000 + ".png"
        msg = {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": long_url}},
        ]}
        payload = Models._build_log_request([msg], None, False, [])
        assert long_url not in payload

    def test_log_request_text_still_logged(self) -> None:
        payload = Models._build_log_request([_IMG_MSG], None, False, [])
        assert "这张图里写了什么" in payload


# ----------------------------------------------------------------------
# Step 1：图片块的 token 估算
# ----------------------------------------------------------------------


class TestImageTokenEstimate:
    def test_image_block_fixed_1600(self) -> None:
        msg = {"role": "user", "content": [
            {"type": "text", "text": "看看"},
            {"type": "image_url", "image_url": {"url": DATA_PNG_URL}},
        ]}
        assert estimate_message_tokens(msg) == 1600 + estimate_message_tokens(
            {"role": "user", "content": "看看"}
        )

    def test_1mb_image_not_million_tokens(self) -> None:
        big = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * (1024 * 1024)).decode()
        msg = {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{big}"}},
        ]}
        assert estimate_message_tokens(msg) == 1600

    def test_input_image_and_anthropic_blocks_counted(self) -> None:
        msg = {"role": "user", "content": [
            {"type": "input_image", "image_url": DATA_PNG_URL},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64}},
        ]}
        assert estimate_message_tokens(msg) == 3200

    def test_string_content_unchanged(self) -> None:
        assert estimate_message_tokens({"role": "user", "content": "abc"}) > 0


# ----------------------------------------------------------------------
# Step 1：vision_candidates / has_vision
# ----------------------------------------------------------------------


def _vision_cfg(*, task_vision: bool = False) -> dict:
    """task 链上一条普通模型；库里另有一条 vision 聊天模型和一条 use=image 生图模型。"""
    return {
        "endpoints": [
            {"id": "e1", "protocol": "openai", "base_url": "https://a.test/v1",
             "api_key": SECRET, "retries": 0},
        ],
        "model_list": [
            {"id": "wk", "endpoint": "e1", "model": "plain-worker", "vision": task_vision},
            {"id": "vis", "endpoint": "e1", "model": "vision-model", "vision": True},
            {"id": "vis2", "endpoint": "e1", "model": "vision-model-2", "vision": True},
            {"id": "gen", "endpoint": "e1", "model": "gen-image", "vision": True, "use": "image"},
        ],
    }


def _vision_cfg_no_vision() -> dict:
    cfg = _vision_cfg()
    for e in cfg["model_list"]:
        e["vision"] = False
    return cfg


def _models(tmp_path, cfg: dict, agents=None) -> Models:
    store = Store(tmp_path / "t.db")
    store.migrate()
    settings = _settings(cfg)
    return Models(
        store,
        lambda: settings,
        agents=agents if agents is not None else _FakeAgents({"task": {"model": "wk"}, "main": {"model": "wk"}}),
    )


class TestVisionCandidates:
    def test_scans_configured_vision_chat_entries(self, tmp_path) -> None:
        """task 链是非视觉模型 → 兜底补模型库里标了 vision 的聊天条目（按 id 去重，生图条目不算）。"""
        models = _models(tmp_path, _vision_cfg())
        try:
            cands = models.vision_candidates("task")
            ids = [c.entry_id for c in cands]
            assert "vis" in ids and "vis2" in ids
            assert "gen" not in ids        # use=image 不是聊天模型
            assert "wk" not in ids         # 没标 vision 的不能进来
            assert len(ids) == len(set(ids))
            assert all(c.vision for c in cands)
            assert models.has_vision("task") is True
        finally:
            store_close = getattr(models, "_store").close
            store_close()

    def test_prefers_chain_vision_candidate_first(self, tmp_path) -> None:
        """岗位链上首选就是视觉模型 → 它排最前，库里的视觉条目只补漏、不重复。"""
        cfg = _vision_cfg(task_vision=True)
        models = _models(tmp_path, cfg)
        try:
            cands = models.vision_candidates("task")
            assert cands[0].entry_id == "wk"  # 链上的视觉模型优先
            ids = [c.entry_id for c in cands]
            assert len(ids) == len(set(ids))
        finally:
            models._store.close()

    def test_no_vision_anywhere(self, tmp_path) -> None:
        models = _models(tmp_path, _vision_cfg_no_vision())
        try:
            assert models.vision_candidates("task") == []
            assert models.has_vision("task") is False
        finally:
            models._store.close()

    def test_vision_entry_with_broken_endpoint_skipped(self, tmp_path) -> None:
        """vision 条目挂的端点缺 api_key → 解析不出候选，不算「能跑」的视觉模型。"""
        cfg = _vision_cfg()
        cfg["endpoints"].append({"id": "e2", "protocol": "openai",
                                 "base_url": "https://b.test/v1", "api_key": ""})
        cfg["model_list"].append(
            {"id": "vis3", "endpoint": "e2", "model": "v3", "vision": True}
        )
        models = _models(tmp_path, cfg)
        try:
            ids = [c.entry_id for c in models.vision_candidates("task")]
            assert "vis3" not in ids
            assert "vis" in ids
        finally:
            models._store.close()


# ----------------------------------------------------------------------
# Step 2：read_image / inspect_image + 文本工具图片拦截
# ----------------------------------------------------------------------

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 64
_WEBP_BYTES = b"RIFF" + b"\x10\x00\x00\x00" + b"WEBP" + b"\x00" * 32
_GIF_BYTES = b"GIF89a" + b"\x00" * 32


@pytest.fixture
def exec_env(tmp_path):
    settings = _settings({
        "environments": {"local_mode": "direct", "workspace_root": str(tmp_path / "ws-root")},
    })
    env = LocalEnv(lambda: settings)
    return settings, env


class _VisionModels:
    """看图工具要的 models 桩：记录 vision_candidates/has_vision/chat 调用。"""

    def __init__(self, *, has_vision: bool = True, reply: str = "图上是一只猫") -> None:
        self._has = has_vision
        self._reply = reply
        self.chat_calls: list[dict] = []
        self.vision_kind_calls: list[str] = []

    def vision_candidates(self, kind: str = "task") -> list:
        self.vision_kind_calls.append(str(kind))
        return [object()] if self._has else []

    def has_vision(self, kind: str = "task") -> bool:
        return self._has

    async def chat(self, role=None, messages=None, **kwargs):
        self.chat_calls.append({"role": role, "messages": messages, "kwargs": kwargs})
        return type("ChatResult", (), {"text": self._reply, "finish_reason": "stop"})()


def _make_tools(store: Store, *, env, settings, models=None) -> Tools:
    tools = Tools(store)
    register_exec_tools(
        tools, env=env, host=FakeHost(), get_settings=lambda: settings,
        session_of=lambda gid: "sess-1", models=models,
    )
    return tools


def _wctx(ws_path: Path, **over) -> ToolContext:
    base = dict(group_id="g1", task_id="T-1", actor="子 agent #1", role="worker", workspace=ws_path)
    base.update(over)
    return ToolContext(**base)


def _mctx(ws_path: Path, **over) -> ToolContext:
    base = dict(group_id="g1", task_id="T-1", actor="主模型", role="main", workspace=ws_path)
    base.update(over)
    return ToolContext(**base)


@pytest.mark.asyncio
class TestReadImageTool:
    async def test_read_image_calls_vision_model(self, exec_env, tmp_path) -> None:
        settings, env = exec_env
        store = Store(tmp_path / "t.db")
        store.migrate()
        models = _VisionModels()
        tools = _make_tools(store, env=env, settings=settings, models=models)
        ws = env.workspace("ws-1")
        (ws / "pic.png").write_bytes(_PNG_BYTES)
        r = await tools.call("read_image", {"path": "pic.png", "question": "图上是什么"}, _wctx(ws))
        assert r.ok
        assert "猫" in str(r.output)
        # 消息里带了图片块（内部 OpenAI 风格 data URI）
        call = models.chat_calls[0]
        content = call["messages"][-1]["content"]
        assert isinstance(content, list)
        img = [b for b in content if isinstance(b, dict) and b.get("type") == "image_url"]
        assert img and str(img[0]["image_url"]["url"]).startswith("data:image/png;base64,")
        # 走视觉候选链
        assert call["kwargs"].get("_candidates")

    async def test_read_image_missing_file(self, exec_env, tmp_path) -> None:
        settings, env = exec_env
        store = Store(tmp_path / "t.db")
        store.migrate()
        tools = _make_tools(store, env=env, settings=settings, models=_VisionModels())
        ws = env.workspace("ws-1")
        r = await tools.call("read_image", {"path": "nope.png"}, _wctx(ws))
        assert not r.ok

    async def test_read_image_rejects_non_image(self, exec_env, tmp_path) -> None:
        settings, env = exec_env
        store = Store(tmp_path / "t.db")
        store.migrate()
        tools = _make_tools(store, env=env, settings=settings, models=_VisionModels())
        ws = env.workspace("ws-1")
        (ws / "a.txt").write_text("纯文本不是图", encoding="utf-8")
        r = await tools.call("read_image", {"path": "a.txt"}, _wctx(ws))
        assert not r.ok and "图" in str(r.error or r.output)

    async def test_read_image_rejects_extension_spoof(self, exec_env, tmp_path) -> None:
        """扩展名 .png 但内容不是图 → 照样拒（按魔数认，不认文件名）。"""
        settings, env = exec_env
        store = Store(tmp_path / "t.db")
        store.migrate()
        tools = _make_tools(store, env=env, settings=settings, models=_VisionModels())
        ws = env.workspace("ws-1")
        (ws / "fake.png").write_bytes(b"MZ\x90\x00" + b"\x00" * 100)
        r = await tools.call("read_image", {"path": "fake.png"}, _wctx(ws))
        assert not r.ok

    async def test_read_image_oversize_rejected(self, exec_env, tmp_path) -> None:
        settings, env = exec_env
        store = Store(tmp_path / "t.db")
        store.migrate()
        tools = _make_tools(store, env=env, settings=settings, models=_VisionModels())
        ws = env.workspace("ws-1")
        (ws / "big.png").write_bytes(_PNG_BYTES + b"\x00" * (10 * 1024 * 1024))
        r = await tools.call("read_image", {"path": "big.png"}, _wctx(ws))
        assert not r.ok

    async def test_read_image_no_vision_model(self, exec_env, tmp_path) -> None:
        """没配任何视觉模型 → 中文错，不调模型。"""
        settings, env = exec_env
        store = Store(tmp_path / "t.db")
        store.migrate()
        models = _VisionModels(has_vision=False)
        tools = _make_tools(store, env=env, settings=settings, models=models)
        ws = env.workspace("ws-1")
        (ws / "pic.png").write_bytes(_PNG_BYTES)
        r = await tools.call("read_image", {"path": "pic.png"}, _wctx(ws))
        assert not r.ok
        assert models.chat_calls == []

    async def test_read_image_path_traversal_rejected(self, exec_env, tmp_path) -> None:
        settings, env = exec_env
        store = Store(tmp_path / "t.db")
        store.migrate()
        tools = _make_tools(store, env=env, settings=settings, models=_VisionModels())
        ws = env.workspace("ws-1")
        r = await tools.call("read_image", {"path": "../../etc/passwd"}, _wctx(ws))
        assert not r.ok

    async def test_read_image_cross_task_artifact_rejected(self, exec_env, tmp_path) -> None:
        """artifact_scope 限了本任务目录：读别个任务的图片被拒。"""
        settings, env = exec_env
        store = Store(tmp_path / "t.db")
        store.migrate()
        tools = _make_tools(store, env=env, settings=settings, models=_VisionModels())
        ws = env.workspace("ws-1")
        other = ws / "artifacts" / "T-9"
        other.mkdir(parents=True)
        (other / "secret.png").write_bytes(_PNG_BYTES)
        ctx = _wctx(ws, artifact_scope=("artifacts/T-1",))
        r = await tools.call("read_image", {"path": "artifacts/T-9/secret.png"}, ctx)
        assert not r.ok

    async def test_read_image_worker_only(self, exec_env, tmp_path) -> None:
        settings, env = exec_env
        store = Store(tmp_path / "t.db")
        store.migrate()
        tools = _make_tools(store, env=env, settings=settings, models=_VisionModels())
        ws = env.workspace("ws-1")
        (ws / "pic.png").write_bytes(_PNG_BYTES)
        r = await tools.call("read_image", {"path": "pic.png"}, _mctx(ws))
        assert not r.ok  # main 调不到 worker 的 read_image

    async def test_inspect_image_main_only(self, exec_env, tmp_path) -> None:
        settings, env = exec_env
        store = Store(tmp_path / "t.db")
        store.migrate()
        models = _VisionModels(reply="网页验收：符合")
        tools = _make_tools(store, env=env, settings=settings, models=models)
        ws = env.workspace("ws-1")
        (ws / "pic.png").write_bytes(_PNG_BYTES)
        r = await tools.call("inspect_image", {"path": "pic.png", "question": "符合要求吗"}, _mctx(ws))
        assert r.ok and "符合" in str(r.output)
        r2 = await tools.call("inspect_image", {"path": "pic.png"}, _wctx(ws))
        assert not r2.ok  # worker 调不到 main 的 inspect_image

    async def test_jpeg_gif_webp_accepted(self, exec_env, tmp_path) -> None:
        settings, env = exec_env
        store = Store(tmp_path / "t.db")
        store.migrate()
        models = _VisionModels()
        tools = _make_tools(store, env=env, settings=settings, models=models)
        ws = env.workspace("ws-1")
        for name, raw in (("a.jpg", _JPEG_BYTES), ("b.gif", _GIF_BYTES), ("c.webp", _WEBP_BYTES)):
            (ws / name).write_bytes(raw)
            r = await tools.call("read_image", {"path": name}, _wctx(ws))
            assert r.ok, name

    async def test_read_image_without_models_registered(self, exec_env, tmp_path) -> None:
        """老调用方不传 models → 工具没注册（不破坏旧行为；调了就是「不认识」）。"""
        settings, env = exec_env
        store = Store(tmp_path / "t.db")
        store.migrate()
        tools = Tools(store)
        register_exec_tools(
            tools, env=env, host=FakeHost(), get_settings=lambda: settings,
            session_of=lambda gid: "sess-1",
        )
        ws = env.workspace("ws-1")
        (ws / "pic.png").write_bytes(_PNG_BYTES)
        r = await tools.call("read_image", {"path": "pic.png"}, _wctx(ws))
        assert not r.ok


@pytest.mark.asyncio
class TestBinaryImageInterception:
    """read_file / inspect_file 读到图片魔数 → 直接报错引导改用看图工具（不许出乱码）。"""

    async def test_read_file_intercepts_png(self, exec_env, tmp_path) -> None:
        settings, env = exec_env
        store = Store(tmp_path / "t.db")
        store.migrate()
        tools = _make_tools(store, env=env, settings=settings)
        ws = env.workspace("ws-1")
        (ws / "pic.png").write_bytes(_PNG_BYTES)
        r = await tools.call("read_file", {"path": "pic.png"}, _wctx(ws))
        assert not r.ok
        assert "read_image" in str(r.error or r.output)

    async def test_read_file_intercepts_disguised_name(self, exec_env, tmp_path) -> None:
        settings, env = exec_env
        store = Store(tmp_path / "t.db")
        store.migrate()
        tools = _make_tools(store, env=env, settings=settings)
        ws = env.workspace("ws-1")
        (ws / "notes.txt").write_bytes(_JPEG_BYTES)
        r = await tools.call("read_file", {"path": "notes.txt"}, _wctx(ws))
        assert not r.ok and "read_image" in str(r.error or r.output)

    async def test_inspect_file_intercepts_webp(self, exec_env, tmp_path) -> None:
        settings, env = exec_env
        store = Store(tmp_path / "t.db")
        store.migrate()
        tools = _make_tools(store, env=env, settings=settings)
        ws = env.workspace("ws-1")
        (ws / "shot.webp").write_bytes(_WEBP_BYTES)
        r = await tools.call("inspect_file", {"path": "shot.webp"}, _mctx(ws))
        assert not r.ok and "inspect_image" in str(r.error or r.output)

    async def test_read_file_still_reads_text(self, exec_env, tmp_path) -> None:
        settings, env = exec_env
        store = Store(tmp_path / "t.db")
        store.migrate()
        tools = _make_tools(store, env=env, settings=settings)
        ws = env.workspace("ws-1")
        (ws / "a.txt").write_text("正常文本", encoding="utf-8")
        r = await tools.call("read_file", {"path": "a.txt"}, _wctx(ws))
        assert r.ok and "正常文本" in str(r.output)

    async def test_read_file_gif_intercepted(self, exec_env, tmp_path) -> None:
        settings, env = exec_env
        store = Store(tmp_path / "t.db")
        store.migrate()
        tools = _make_tools(store, env=env, settings=settings)
        ws = env.workspace("ws-1")
        (ws / "g.gif").write_bytes(_GIF_BYTES)
        r = await tools.call("read_file", {"path": "g.gif"}, _wctx(ws))
        assert not r.ok and "read_image" in str(r.error or r.output)
