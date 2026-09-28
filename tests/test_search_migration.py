"""一次性迁移：config.toml 的 [search] 段 → 一个 MCP 扩展 + 搜索绑定（启动时跑，幂等）。

- tavily_mcp / tavily（有 key）：URL 相同（忽略 query 里的 key 参数、大小写、末尾斜杠）的
  现有网页扩展 → 复用；没有 → 新建名为 "tavily" 的扩展（密钥进 secrets，不落日志）。
- exa（有 key）→ 建/复用 https://mcp.exa.ai/mcp；you → https://api.you.com/mcp。
- 设绑定（工具列表拿不到按惯例 tavily-search / tavily-extract 先存名字，不必当场验证）；
  最后从 config.toml 删掉整个 [search] 段；再跑什么都不动。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from CharTyr_MaiWork import config_file
from CharTyr_MaiWork.migrations import migrate_search_config_to_extension
from CharTyr_MaiWork.search_binding import get_binding
from CharTyr_MaiWork.store import Store

TAVILY_MCP_URL = "https://mcp.third-party.example/mcp"


def _plugin_dir(tmp_path: Path, search_section: str) -> Path:
    d = tmp_path / "plug"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.toml").write_text(
        "# 注释别丢\n"
        "[plugin]\n"
        "enabled = true\n"
        "\n"
        "[topics]\n"
        "per_day = 2\n"
        + search_section,
        encoding="utf-8",
    )
    return d


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "data" / "m.db")
    s.migrate()
    yield s
    s.close()


def _add_web_ext(store: Store, name: str, url: str, headers: dict[str, str] | None = None) -> None:
    from CharTyr_MaiWork import extensions_web

    entries = extensions_web._web_raw_entries(store)
    entries.append({
        "name": name, "url": url, "enabled": True, "tools": [], "roles": ["worker"],
        "timeout_s": 20, "header_names": sorted((headers or {}).keys()),
    })
    with store.tx() as conn:
        store.kv_set(conn, extensions_web.KV_MCP, entries)
        for h, v in (headers or {}).items():
            store.secret_set(conn, extensions_web.secret_name(name, h), v)


class TestTavilyMcp:
    def test_creates_extension_and_binding(self, tmp_path: Path, store: Store) -> None:
        plug = _plugin_dir(tmp_path, (
            "\n[search]\n"
            'provider = "tavily_mcp"\n'
            'api_key = "tvly-迁移密钥-显眼"\n'
            f'mcp_url = "{TAVILY_MCP_URL}"\n'
            "timeout_s = 20\n"
        ))
        migrated = migrate_search_config_to_extension(store, plug, tmp_path / "data")
        assert migrated is True
        text = (plug / "config.toml").read_text(encoding="utf-8")
        assert "[search]" not in text
        assert "tvly-迁移密钥-显眼" not in text  # 密钥绝不留文件里
        assert "# 注释别丢" in text
        # 新建了 tavily 扩展（网页来源），密钥进了 secrets
        from CharTyr_MaiWork import extensions_web

        entries = extensions_web._web_raw_entries(store)
        assert len(entries) == 1
        assert entries[0]["name"] == "tavily"
        assert entries[0]["url"] == TAVILY_MCP_URL
        assert entries[0]["header_names"] == ["Authorization"]
        assert store.secret_get("mcp.tavily.Authorization") == "Bearer tvly-迁移密钥-显眼"
        # 绑定按惯例（拿不到工具清单时的默认值）
        binding = get_binding(store)
        assert binding == {"mcp": "tavily", "tool": "tavily-search", "extract_tool": "tavily-extract"}

    def test_reuses_existing_extension_same_url(self, tmp_path: Path, store: Store) -> None:
        """数据库里已有同 URL 的 tavily 扩展（URL 带 ?key= 参数、大小写、末尾斜杠差异都算同一个）→ 复用不新建。"""
        _add_web_ext(store, "tavily", TAVILY_MCP_URL.upper() + "?key=旧的密钥参数", {"Authorization": "Bearer 旧的密钥"})
        plug = _plugin_dir(tmp_path, (
            "\n[search]\n"
            'provider = "tavily_mcp"\n'
            'api_key = "tvly-迁移密钥-显眼"\n'
            f'mcp_url = "{TAVILY_MCP_URL}/"\n'
        ))
        migrated = migrate_search_config_to_extension(store, plug, tmp_path / "data")
        assert migrated is True
        from CharTyr_MaiWork import extensions_web

        entries = extensions_web._web_raw_entries(store)
        assert len(entries) == 1  # 没有新建
        assert entries[0]["name"] == "tavily"
        # 复用不改它已有的密钥
        assert store.secret_get("mcp.tavily.Authorization") == "Bearer 旧的密钥"
        binding = get_binding(store)
        assert binding is not None and binding["mcp"] == "tavily"

    def test_existing_extension_different_name_reused(self, tmp_path: Path, store: Store) -> None:
        """同 URL 但叫别的名字（如用户自己加的 "search1"）→ 复用那个名字，不再建 tavily。"""
        _add_web_ext(store, "search1", TAVILY_MCP_URL, {"Authorization": "Bearer 已有"})
        plug = _plugin_dir(tmp_path, (
            "\n[search]\n"
            'provider = "tavily_mcp"\n'
            'api_key = "tvly-迁移密钥-显眼"\n'
            f'mcp_url = "{TAVILY_MCP_URL}"\n'
        ))
        assert migrate_search_config_to_extension(store, plug, tmp_path / "data") is True
        binding = get_binding(store)
        assert binding["mcp"] == "search1"
        from CharTyr_MaiWork import extensions_web

        assert len(extensions_web._web_raw_entries(store)) == 1

    def test_tool_names_from_live_list(self, tmp_path: Path, store: Store) -> None:
        """能拿到工具清单时：绑定选名字含 search 的那个 + 含 extract/contents 的那个。"""
        plug = _plugin_dir(tmp_path, (
            "\n[search]\n"
            'provider = "tavily_mcp"\n'
            'api_key = "k"\n'
            f'mcp_url = "{TAVILY_MCP_URL}"\n'
        ))
        tools = [
            {"name": "tavily_search", "description": "搜索"},
            {"name": "tavily_extract", "description": "抽正文"},
        ]
        migrate_search_config_to_extension(store, plug, tmp_path / "data", list_tools=lambda name: tools)
        binding = get_binding(store)
        assert binding["tool"] == "tavily_search"
        assert binding["extract_tool"] == "tavily_extract"

    def test_idempotent(self, tmp_path: Path, store: Store) -> None:
        plug = _plugin_dir(tmp_path, (
            "\n[search]\n"
            'provider = "tavily_mcp"\n'
            'api_key = "tvly-迁移密钥-显眼"\n'
            f'mcp_url = "{TAVILY_MCP_URL}"\n'
        ))
        assert migrate_search_config_to_extension(store, plug, tmp_path / "data") is True
        text1 = (plug / "config.toml").read_text(encoding="utf-8")
        assert migrate_search_config_to_extension(store, plug, tmp_path / "data") is False
        assert (plug / "config.toml").read_text(encoding="utf-8") == text1

    def test_no_secret_in_logs(self, tmp_path: Path, store: Store, caplog) -> None:
        plug = _plugin_dir(tmp_path, (
            "\n[search]\n"
            'provider = "tavily_mcp"\n'
            'api_key = "tvly-绝不许-进日志"\n'
            f'mcp_url = "{TAVILY_MCP_URL}"\n'
        ))
        with caplog.at_level(logging.INFO):
            migrate_search_config_to_extension(store, plug, tmp_path / "data")
        whole = "\n".join(r.getMessage() for r in caplog.records)
        assert "tvly-绝不许-进日志" not in whole


class TestOtherProviders:
    def test_tavily_direct_uses_official_mcp(self, tmp_path: Path, store: Store) -> None:
        plug = _plugin_dir(tmp_path, (
            "\n[search]\n"
            'provider = "tavily"\n'
            'api_key = "tvly-直连密钥"\n'
        ))
        assert migrate_search_config_to_extension(store, plug, tmp_path / "data") is True
        from CharTyr_MaiWork import extensions_web

        entries = extensions_web._web_raw_entries(store)
        assert entries[0]["name"] == "tavily"
        assert "tavily" in entries[0]["url"]
        assert entries[0]["url"].startswith("https://")
        assert store.secret_get("mcp.tavily.Authorization") == "Bearer tvly-直连密钥"
        assert get_binding(store)["mcp"] == "tavily"

    def test_exa(self, tmp_path: Path, store: Store) -> None:
        plug = _plugin_dir(tmp_path, (
            "\n[search]\n"
            'provider = "exa"\n'
            'api_key = "exa-密钥"\n'
        ))
        assert migrate_search_config_to_extension(store, plug, tmp_path / "data") is True
        from CharTyr_MaiWork import extensions_web

        entries = extensions_web._web_raw_entries(store)
        assert entries[0]["name"] == "exa"
        assert entries[0]["url"] == "https://mcp.exa.ai/mcp"
        assert store.secret_get("mcp.exa.Authorization") == "Bearer exa-密钥"
        assert get_binding(store)["mcp"] == "exa"
        assert "[search]" not in (plug / "config.toml").read_text(encoding="utf-8")

    def test_you(self, tmp_path: Path, store: Store) -> None:
        plug = _plugin_dir(tmp_path, (
            "\n[search]\n"
            'provider = "you"\n'
            'api_key = "you-密钥"\n'
        ))
        assert migrate_search_config_to_extension(store, plug, tmp_path / "data") is True
        from CharTyr_MaiWork import extensions_web

        entries = extensions_web._web_raw_entries(store)
        assert entries[0]["name"] == "you"
        assert entries[0]["url"] == "https://api.you.com/mcp"
        assert store.secret_get("mcp.you.Authorization") == "Bearer you-密钥"

    def test_you_no_key_free_profile(self, tmp_path: Path, store: Store) -> None:
        """you 没密钥（免费档）→ URL 带 ?profile=free，不设认证头。"""
        plug = _plugin_dir(tmp_path, (
            "\n[search]\n"
            'provider = "you"\n'
        ))
        assert migrate_search_config_to_extension(store, plug, tmp_path / "data") is True
        from CharTyr_MaiWork import extensions_web

        entries = extensions_web._web_raw_entries(store)
        assert entries[0]["url"] == "https://api.you.com/mcp?profile=free"
        assert entries[0]["header_names"] == []
        assert get_binding(store)["mcp"] == "you"


class TestNoop:
    def test_no_search_section(self, tmp_path: Path, store: Store) -> None:
        plug = _plugin_dir(tmp_path, "")
        assert migrate_search_config_to_extension(store, plug, tmp_path / "data") is False
        assert get_binding(store) is None

    def test_empty_provider(self, tmp_path: Path, store: Store) -> None:
        """[search] 在但 provider 空（等于没配过）→ 不建扩展，但这段死配置清掉。"""
        plug = _plugin_dir(tmp_path, (
            "\n[search]\n"
            'provider = ""\n'
            'api_key = ""\n'
        ))
        assert migrate_search_config_to_extension(store, plug, tmp_path / "data") is False
        assert "[search]" not in (plug / "config.toml").read_text(encoding="utf-8")
        assert get_binding(store) is None

    def test_existing_binding_not_overwritten(self, tmp_path: Path, store: Store) -> None:
        """已经有搜索绑定了（管理员手动配过）→ 不覆盖，但 [search] 段照样清掉。"""
        from CharTyr_MaiWork.search_binding import set_binding

        _add_web_ext(store, "mine", "https://mcp.mine.example/mcp")
        set_binding(store, {"mcp": "mine", "tool": "mine-search", "extract_tool": ""})
        plug = _plugin_dir(tmp_path, (
            "\n[search]\n"
            'provider = "tavily_mcp"\n'
            'api_key = "tvly-迁移密钥-显眼"\n'
            f'mcp_url = "{TAVILY_MCP_URL}"\n'
        ))
        assert migrate_search_config_to_extension(store, plug, tmp_path / "data") is True
        binding = get_binding(store)
        assert binding["mcp"] == "mine"  # 手动配的绑定不动
        assert "[search]" not in (plug / "config.toml").read_text(encoding="utf-8")
