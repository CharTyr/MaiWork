"""search_binding.py 测试：联网搜索绑定（kv["extensions.search"]）的读写、状态、候选清单。

绑定只存数据库，不进 config.toml。搜索只能走「扩展」里指定的一个 MCP 的某个工具。
密钥一律假值，不碰真实网络。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.search_binding import (
    KV_SEARCH,
    clear_binding,
    get_binding,
    guess_tool_role,
    save_binding,
    search_role_of,
    search_view,
    set_binding,
    status_of,
)
from CharTyr_MaiWork.maiwork.store import Store


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
        assert get_binding(store) == {"mcp": "tavily", "tool": "tavily-search", "extract_mcp": "tavily", "extract_tool": "tavily-extract"}

    def test_extract_tool_optional(self, store: Store) -> None:
        set_binding(store, {"mcp": "tavily", "tool": "tavily-search"})
        b = get_binding(store)
        assert b == {"mcp": "tavily", "tool": "tavily-search", "extract_mcp": "", "extract_tool": ""}

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
        assert view["binding"] == {"mcp": "tavily", "tool": "tavily-search", "extract_mcp": "tavily", "extract_tool": "tavily-extract"}
        assert view["status"]["ok"] is True
        assert "tavily-search" in view["status"]["text"]


# ----------------------------------------------------------------------
# 搜索和抓正文分别用两个不同的 MCP（用户要求：比如搜索用 keenable、抓正文用 You）
# ----------------------------------------------------------------------


def _settings_two(**enabled):
    raw = {"extensions": {"mcp": [
        {"name": "keenable", "url": "https://k.example/mcp", "enabled": enabled.get("keenable", True)},
        {"name": "You", "url": "https://y.example/mcp", "enabled": enabled.get("You", True)},
    ]}}
    s, problems = load_settings(raw)
    assert problems == []
    return s


class _RT:
    def __init__(self, tools, connected=True):
        self._tools = tools
        self.client = object() if connected else None

    def tools_remote(self):
        return {t: {} for t in self._tools}


_RUNTIMES = {
    "keenable": _RT(["search_web_pages", "fetch_page_content"]),
    "You": _RT(["you-search", "you-contents"]),
}


def _spec_of(mcp: str, tool: str):
    rt = _RUNTIMES.get(mcp)
    return {"name": tool} if rt is not None and tool in rt._tools else None


class TestTwoMcps:
    def test_old_record_extract_follows_search_mcp(self, store: Store) -> None:
        """老绑定（没有 extract_mcp）：抓正文当成和搜索同一个扩展，行为不变。"""
        with store.tx() as conn:
            store.kv_set(conn, KV_SEARCH, {"mcp": "You", "tool": "you-search", "extract_tool": "you-contents"})
        assert get_binding(store) == {"mcp": "You", "tool": "you-search", "extract_mcp": "You", "extract_tool": "you-contents"}

    def test_save_search_and_extract_from_different_mcps(self, store: Store) -> None:
        b = save_binding(
            store, _settings_two(),
            {"mcp": "keenable", "tool": "search_web_pages", "extract_mcp": "You", "extract_tool": "you-contents"},
            tool_spec_of=_spec_of,
        )
        assert b == {"mcp": "keenable", "tool": "search_web_pages", "extract_mcp": "You", "extract_tool": "you-contents"}
        assert get_binding(store) == b

    def test_extract_mcp_defaults_to_search_mcp(self, store: Store) -> None:
        b = save_binding(
            store, _settings_two(),
            {"mcp": "keenable", "tool": "search_web_pages", "extract_tool": "fetch_page_content"},
            tool_spec_of=_spec_of,
        )
        assert b["extract_mcp"] == "keenable"

    def test_extract_tool_checked_against_its_own_mcp(self, store: Store) -> None:
        with pytest.raises(ValueError, match="没有这个工具"):
            save_binding(
                store, _settings_two(),
                {"mcp": "keenable", "tool": "search_web_pages", "extract_mcp": "You", "extract_tool": "fetch_page_content"},
                tool_spec_of=_spec_of,
            )

    def test_unknown_extract_mcp(self, store: Store) -> None:
        with pytest.raises(ValueError, match="没有这个扩展"):
            save_binding(
                store, _settings_two(),
                {"mcp": "keenable", "tool": "search_web_pages", "extract_mcp": "没有这家", "extract_tool": "x"},
                tool_spec_of=_spec_of,
            )

    def test_same_tool_name_ok_when_mcps_differ(self, store: Store) -> None:
        def spec(mcp, tool):
            return {"name": tool}

        b = save_binding(
            store, _settings_two(),
            {"mcp": "keenable", "tool": "search", "extract_mcp": "You", "extract_tool": "search"},
            tool_spec_of=spec,
        )
        assert b["extract_mcp"] == "You"

    def test_status_ok_and_names_both(self, store: Store) -> None:
        set_binding(store, {"mcp": "keenable", "tool": "search_web_pages", "extract_mcp": "You", "extract_tool": "you-contents"})
        ok, text = status_of(store, _settings_two(), _RUNTIMES.get)
        assert ok is True
        assert "keenable" in text and "search_web_pages" in text
        assert "You" in text and "you-contents" in text

    def test_extract_mcp_broken_does_not_break_search(self, store: Store) -> None:
        """抓正文那家关了：搜索照样能用，状态里说明抓正文用不了。"""
        set_binding(store, {"mcp": "keenable", "tool": "search_web_pages", "extract_mcp": "You", "extract_tool": "you-contents"})
        ok, text = status_of(store, _settings_two(You=False), _RUNTIMES.get)
        assert ok is True
        assert "抓正文" in text and "没启用" in text

    def test_removing_extract_mcp_keeps_search(self, store: Store) -> None:
        set_binding(store, {"mcp": "keenable", "tool": "search_web_pages", "extract_mcp": "You", "extract_tool": "you-contents"})
        assert clear_binding(store, mcp="You") is True
        assert get_binding(store) == {"mcp": "keenable", "tool": "search_web_pages", "extract_mcp": "", "extract_tool": ""}

    def test_removing_search_mcp_clears_all(self, store: Store) -> None:
        set_binding(store, {"mcp": "keenable", "tool": "search_web_pages", "extract_mcp": "You", "extract_tool": "you-contents"})
        clear_binding(store, mcp="keenable")
        assert get_binding(store) is None

    def test_role_badges(self, store: Store) -> None:
        set_binding(store, {"mcp": "keenable", "tool": "search_web_pages", "extract_mcp": "You", "extract_tool": "you-contents"})
        assert search_role_of(store, "keenable") == "search"
        assert search_role_of(store, "You") == "extract"
        assert search_role_of(store, "别的") == ""
