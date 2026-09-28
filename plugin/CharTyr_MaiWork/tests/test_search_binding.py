"""search_binding.py 测试：联网搜索绑定（kv["extensions.search"]）的读写、状态、候选清单。

绑定只存数据库，不进 config.toml。搜索只能走「扩展」里指定的一个 MCP 的某个工具。
密钥一律假值，不碰真实网络。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.config import load_settings
from CharTyr_MaiWork.search_binding import (
    KV_SEARCH,
    clear_binding,
    get_binding,
    guess_tool_role,
    save_binding,
    search_view,
    set_binding,
    status_of,
)
from CharTyr_MaiWork.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _settings_with_mcp(**mcp_over):
    entry = {
        "name": "tavily",
        "url": "https://mcp.example/mcp",
        "enabled": True,
    }
    entry.update(mcp_over)
    raw = {"extensions": {"mcp": [entry]}}
    s, problems = load_settings(raw)
    assert problems == []
    return s


class TestBindingRoundTrip:
    def test_empty_when_nothing(self, store: Store) -> None:
        assert get_binding(store) is None

    def test_set_and_get(self, store: Store) -> None:
        set_binding(store, {"mcp": "tavily", "tool": "tavily-search", "extract_tool": "tavily-extract"})
        assert get_binding(store) == {"mcp": "tavily", "tool": "tavily-search", "extract_tool": "tavily-extract"}

    def test_extract_tool_optional(self, store: Store) -> None:
        set_binding(store, {"mcp": "tavily", "tool": "tavily-search"})
        b = get_binding(store)
        assert b == {"mcp": "tavily", "tool": "tavily-search", "extract_tool": ""}

    def test_garbage_in_kv_is_none(self, store: Store) -> None:
        with store.tx() as conn:
            store.kv_set(conn, KV_SEARCH, {"mcp": 123})
        assert get_binding(store) is None
        with store.tx() as conn:
            store.kv_set(conn, KV_SEARCH, "不是表")
        assert get_binding(store) is None

    def test_clear(self, store: Store) -> None:
        set_binding(store, {"mcp": "tavily", "tool": "tavily-search", "extract_tool": ""})
        clear_binding(store)
        assert get_binding(store) is None
        # 幂等
        clear_binding(store)
        assert get_binding(store) is None

    def test_clear_binding_for_extension(self, store: Store) -> None:
        set_binding(store, {"mcp": "tavily", "tool": "s", "extract_tool": "e"})
        clear_binding(store, mcp="别的扩展")
        assert get_binding(store) is not None  # 别的扩展不动
        clear_binding(store, mcp="tavily")
        assert get_binding(store) is None


class TestValidation:
    def test_save_binding_validates(self, store: Store) -> None:
        settings = _settings_with_mcp()

        def spec_of(mcp: str, tool: str):
            if mcp == "tavily" and tool in ("tavily-search", "tavily-extract"):
                return {"name": tool, "inputSchema": {"type": "object", "properties": {}}}
            return None

        binding = save_binding(store, settings, {"mcp": "tavily", "tool": "tavily-search", "extract_tool": "tavily-extract"}, tool_spec_of=spec_of)
        assert binding["mcp"] == "tavily"
        assert binding["tool"] == "tavily-search"
        assert binding["extract_tool"] == "tavily-extract"

    def test_save_binding_unknown_extension(self, store: Store) -> None:
        settings = _settings_with_mcp()
        with pytest.raises(ValueError, match="没有这个扩展"):
            save_binding(store, settings, {"mcp": "没有这家", "tool": "s"}, tool_spec_of=lambda m, t: None)

    def test_save_binding_unknown_tool(self, store: Store) -> None:
        settings = _settings_with_mcp()
        with pytest.raises(ValueError, match="没有这个工具"):
            save_binding(
                store, settings, {"mcp": "tavily", "tool": "没有的工具"},
                tool_spec_of=lambda m, t: {"name": t} if t == "tavily-search" else None,
            )

    def test_save_binding_same_tool_rejected(self, store: Store) -> None:
        settings = _settings_with_mcp()
        with pytest.raises(ValueError, match="同一个工具"):
            save_binding(
                store, settings, {"mcp": "tavily", "tool": "s", "extract_tool": "s"},
                tool_spec_of=lambda m, t: {"name": t},
            )


class TestStatus:
    def test_no_binding(self, store: Store) -> None:
        settings = _settings_with_mcp()
        ok, text = status_of(store, settings, lambda name: None)
        assert ok is False
        assert "还没指定联网搜索" in text
        assert "扩展" in text

    def test_binding_extension_gone(self, store: Store) -> None:
        settings = _settings_with_mcp()
        set_binding(store, {"mcp": "被删的", "tool": "s", "extract_tool": ""})
        ok, text = status_of(store, settings, lambda name: None)
        assert ok is False
        assert "不在了" in text

    def test_binding_extension_disabled(self, store: Store) -> None:
        settings = _settings_with_mcp(enabled=False)
        set_binding(store, {"mcp": "tavily", "tool": "s", "extract_tool": ""})
        ok, text = status_of(store, settings, lambda name: None)
        assert ok is False
        assert "没启用" in text

    def test_binding_tool_missing(self, store: Store) -> None:
        settings = _settings_with_mcp()
        set_binding(store, {"mcp": "tavily", "tool": "s", "extract_tool": ""})

        class _Runtime:
            client = object()  # 连上了但工具清单里没有 s

            def tools_remote(self):
                return {}

        ok, text = status_of(store, settings, lambda name: _Runtime())
        assert ok is False
        assert "没有这个工具" in text

    def test_binding_ok(self, store: Store) -> None:
        settings = _settings_with_mcp()
        set_binding(store, {"mcp": "tavily", "tool": "tavily-search", "extract_tool": "tavily-extract"})

        class _Runtime:
            client = object()

            def tools_remote(self):
                return {"tavily-search": {}, "tavily-extract": {}}

        ok, text = status_of(store, settings, lambda name: _Runtime())
        assert ok is True
        assert "tavily" in text and "tavily-search" in text

    def test_binding_ext_not_connected(self, store: Store) -> None:
        """扩展还没连上（client None）：绑定无效，状态提示扩展没连上。"""
        settings = _settings_with_mcp()
        set_binding(store, {"mcp": "tavily", "tool": "tavily-search", "extract_tool": ""})

        class _Runtime:
            client = None

            def tools_remote(self):
                return {}

        ok, text = status_of(store, settings, lambda name: _Runtime())
        assert ok is False
        assert "没连上" in text


class TestGuess:
    @pytest.mark.parametrize(
        ("name", "desc", "want"),
        [
            ("tavily-search", "", "search"),
            ("you-search", "", "search"),
            ("web_search", "search the web", "search"),
            ("search", "", "search"),
            ("google_search", "", "search"),
            ("tavily-extract", "", "extract"),
            ("you-contents", "fetch page contents", "extract"),
            ("extract", "", "extract"),
            ("crawl", "", "extract"),
            ("reader", "读取网页正文", "extract"),
            ("echo", "回显", ""),
            ("create_issue", "新建 issue", ""),
        ],
    )
    def test_guess_tool_role(self, name: str, desc: str, want: str) -> None:
        assert guess_tool_role(name, desc) == want


class TestSearchView:
    def test_view_shape(self, store: Store) -> None:
        settings = _settings_with_mcp()

        class _Runtime:
            client = object()

            def tools_remote(self):
                return {
                    "tavily-search": {"name": "tavily-search", "description": "搜索网页"},
                    "tavily-extract": {"name": "tavily-extract", "description": "抽正文"},
                }

        view = search_view(store, settings, lambda name: _Runtime())
        assert view["binding"] is None
        assert view["status"]["ok"] is False
        cand = view["candidates"]
        assert len(cand) == 1
        assert cand[0]["mcp"] == "tavily"
        tools = {t["name"]: t for t in cand[0]["tools"]}
        assert tools["tavily-search"]["guess"] == "search"
        assert tools["tavily-extract"]["guess"] == "extract"
        assert "description" in tools["tavily-search"]

    def test_view_with_binding(self, store: Store) -> None:
        settings = _settings_with_mcp()
        set_binding(store, {"mcp": "tavily", "tool": "tavily-search", "extract_tool": "tavily-extract"})

        class _Runtime:
            client = object()

            def tools_remote(self):
                return {"tavily-search": {"name": "tavily-search"}, "tavily-extract": {"name": "tavily-extract"}}

        view = search_view(store, settings, lambda name: _Runtime())
        assert view["binding"] == {"mcp": "tavily", "tool": "tavily-search", "extract_tool": "tavily-extract"}
        assert view["status"]["ok"] is True
        assert "tavily-search" in view["status"]["text"]
