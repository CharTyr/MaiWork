"""M5 / M7 回归测试。

M5：tools._persist 的最终 input/output 摘要统一再过一次密钥遮罩（Bearer、sk-、
api_key=、以及通过 get_known_secrets 回调传进来的已知密钥）。工具的 summarize 是
开发者随手写的，可能把密钥原样写进库——入口统一兜一道。

M7：feeds 备料前的「搜索自检」只看绑定状态（kv["extensions.search"] + 扩展运行状态），
不真发请求探活。真 Search 用 available() 判；注入了 search() 的假搜索照旧走
search()（兼容既有测试桩）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

from CharTyr_MaiWork.config import load_settings
from CharTyr_MaiWork.feeds import Feeds
from CharTyr_MaiWork.search import Search, SearchUnavailable
from CharTyr_MaiWork.store import Store
from CharTyr_MaiWork.tools import Tools, ToolContext, ToolResult

GID = "900000001"


class FakeSearch:
    """注入了 search() 的假搜索：照旧被调一次（兼容行为）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def search(self, query: str, **kw) -> list:
        self.calls.append(query)
        return []


class TestM7SearchCheckBindingOnly:
    def _feeds(self, store: Store, search_obj) -> Feeds:
        settings, _ = load_settings({"groups": {"serve": [{"group": f"qq:{GID}"}]}})
        return Feeds(store, models=None, workers=None, profiles=None, topics=None, get_settings=lambda: settings, search=search_obj)

    @pytest.mark.asyncio
    async def test_real_search_bound_no_network(self, tmp_path: Path) -> None:
        """绑好了：自检只看绑定状态，一次网络请求都不发（client 里埋了断言雷）。"""
        from CharTyr_MaiWork.search_binding import set_binding

        store = Store(tmp_path / "t.db")
        store.migrate()
        set_binding(store, {"mcp": "tavily", "tool": "tavily-search", "extract_tool": ""})

        class _Client:
            async def call_tool(self, name, arguments):  # 真发请求就炸
                raise AssertionError("自检不许发真实请求")

        class _Runtime:
            client = _Client()

            def tools_remote(self):
                return {"tavily-search": {"name": "tavily-search"}}

        class _Extensions:
            _settings, _ = load_settings({"extensions": {"mcp": [{"name": "tavily", "url": "https://mcp.example/mcp"}]}})
            _get_settings = lambda self: _Extensions._settings

            def runtime_of(self, name):
                return _Runtime()

        search_obj = Search(store, lambda: _Extensions())
        feeds = self._feeds(store, search_obj)
        await feeds._ensure_search()  # 不炸 = 没发请求

    @pytest.mark.asyncio
    async def test_real_search_unbound_unavailable(self, tmp_path: Path) -> None:
        """没绑定：SearchUnavailable（中文提示去 设置 → 扩展），同样不发请求。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        search_obj = Search(store, lambda: None)
        feeds = self._feeds(store, search_obj)
        with pytest.raises(SearchUnavailable) as e:
            await feeds._ensure_search()
        assert "扩展" in str(e.value)

    @pytest.mark.asyncio
    async def test_fake_search_still_probed(self, tmp_path: Path) -> None:
        """注入了 search() 的假搜索（测试桩）：照旧用 search() 自检一次（兼容）。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        fake = FakeSearch()
        feeds = self._feeds(store, fake)
        await feeds._ensure_search()
        assert fake.calls  # 被调过


class TestM5PersistMasking:
    def _tools(self, tmp_path: Path, known: list[str]) -> tuple[Tools, Store]:
        store = Store(tmp_path / "t.db")
        store.migrate()
        return Tools(store, get_known_secrets=lambda: known), store

    async def test_summarize_output_masked(self, tmp_path: Path) -> None:
        """工具的 summarize 把密钥直接写进了 output 摘要 → 落库前被遮掉。"""
        tools, store = self._tools(tmp_path, [])
        from CharTyr_MaiWork.tools import Tool

        async def handler(ctx, args):
            return ToolResult(ok=True, output="done")

        tools.register(Tool(
            name="leaky", description="d", parameters={"type": "object", "properties": {}},
            roles=frozenset({"worker"}), handler=handler,
            summarize=lambda a, r: ("输入", "用 Bearer abcdef1234567890 调的 api_key=supersecret123"),
        ))
        ctx = ToolContext(group_id=GID, role="worker")
        await tools.call("leaky", {}, ctx)
        row = store.read().execute("SELECT input, output FROM tool_calls").fetchone()
        assert "abcdef1234567890" not in row["output"]
        assert "supersecret123" not in row["output"]
        assert "Bearer ***" in row["output"]

    async def test_sk_pattern_masked(self, tmp_path: Path) -> None:
        tools, store = self._tools(tmp_path, [])
        from CharTyr_MaiWork.tools import Tool

        async def handler(ctx, args):
            return ToolResult(ok=True, output=f"token: sk-abcdefghijklmnop")

        tools.register(Tool(
            name="skleak", description="d", parameters={"type": "object", "properties": {}},
            roles=frozenset({"worker"}), handler=handler,
        ))
        ctx = ToolContext(group_id=GID, role="worker")
        await tools.call("skleak", {}, ctx)
        row = store.read().execute("SELECT output FROM tool_calls").fetchone()
        assert "sk-abcdefghijklmnop" not in row["output"]

    async def test_known_secrets_masked(self, tmp_path: Path) -> None:
        """get_known_secrets 回调给的密钥（比如模型密钥）出现在摘要里也要遮。"""
        tools, store = self._tools(tmp_path, ["my-model-key-987654"])
        from CharTyr_MaiWork.tools import Tool

        async def handler(ctx, args):
            return ToolResult(ok=True, output="端点鉴权失败 my-model-key-987654 无效")

        tools.register(Tool(
            name="knownleak", description="d", parameters={"type": "object", "properties": {}},
            roles=frozenset({"worker"}), handler=handler,
        ))
        ctx = ToolContext(group_id=GID, role="worker")
        await tools.call("knownleak", {}, ctx)
        row = store.read().execute("SELECT output FROM tool_calls").fetchone()
        assert "my-model-key-987654" not in row["output"]

    async def test_default_no_callback_still_works(self, tmp_path: Path) -> None:
        """不传回调（老构造）照常工作，其他遮罩规则还在。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        tools = Tools(store)
        from CharTyr_MaiWork.tools import Tool

        async def handler(ctx, args):
            return ToolResult(ok=True, output="用 Bearer xyztoken123456 调的")

        tools.register(Tool(
            name="ok", description="d", parameters={"type": "object", "properties": {}},
            roles=frozenset({"worker"}), handler=handler,
        ))
        ctx = ToolContext(group_id=GID, role="worker")
        result = await tools.call("ok", {}, ctx)
        assert result.ok
        row = store.read().execute("SELECT output FROM tool_calls").fetchone()
        assert "xyztoken123456" not in row["output"]
