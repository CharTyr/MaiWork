"""search_presets_web.py 测试：预设搜索服务在网页上的打开 / 填密钥 / 引导一次配好。

- 预设打开 = 在「扩展」里新增一个网页 MCP 条目（名字默认是预设 id），头值只进 secrets；
- 已有条目按地址认成预设（线上老的 keenable、You 不用重配）；
- 免费的可以不填密钥；要密钥的（TinyFish）不填就拒；
- 还没有搜索绑定时，打开的第一家自动成为主搜索；
- 引导一次配好：勾上的第一家当主搜索，其余当备用。
密钥一律假值，不碰网络。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import extensions_web, search_presets_web
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.search_binding import get_binding, set_binding
from CharTyr_MaiWork.maiwork.store import Store

FAKE_KEY = "keen_FAKE-Zz9Qw8Er7Ty6"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def settings():
    s, _ = load_settings({})
    return s


def _entry(settings, store, name):
    for e in extensions_web.merged_entries(settings, store):
        if e.name == name:
            return e
    return None


def _view(settings, store):
    return {p["id"]: p for p in search_presets_web.presets_view(settings, store, lambda _n: None)}


def test_view_lists_six_presets_in_order_with_logo(settings, store):
    view = search_presets_web.presets_view(settings, store, lambda _n: None)
    assert [p["id"] for p in view] == ["keenable", "tavily", "exa", "you", "firecrawl", "tinyfish"]
    k = view[0]
    assert k["logo"] == "/static/assets/logos/keenable.png"
    assert k["free"] is True and k["entry"] is None and k["enabled"] is False and k["key_set"] is False
    assert k["key_page_url"].startswith("https://")
    assert view[-1]["free"] is False


def test_activate_free_creates_entry_without_key_and_binds_main(settings, store):
    name, bound = search_presets_web.activate(store, settings, "keenable")
    assert name == "keenable" and bound is True
    e = _entry(settings, store, "keenable")
    assert e is not None and e.enabled and e.url.startswith("https://api.keenable.ai/mcp")
    b = get_binding(store)
    assert b["mcp"] == "keenable" and b["tool"] == "search_web_pages"
    assert b["extract_mcp"] == "keenable" and b["extract_tool"] == "fetch_page_content"
    v = _view(settings, store)["keenable"]
    assert v["entry"] == "keenable" and v["enabled"] is True and v["key_set"] is False


def test_activate_second_does_not_steal_main_binding(settings, store):
    search_presets_web.activate(store, settings, "keenable")
    _name, bound = search_presets_web.activate(store, settings, "exa")
    assert bound is False
    assert get_binding(store)["mcp"] == "keenable"


def test_activate_with_key_stores_secret_only_and_marks_key_set(settings, store):
    search_presets_web.activate(store, settings, "keenable", key=FAKE_KEY)
    headers = extensions_web.stored_headers_for(store, settings, "keenable")
    assert headers.get("X-API-Key") == FAKE_KEY
    raw_kv = str(store.kv_get(extensions_web.KV_MCP))
    assert FAKE_KEY not in raw_kv
    view = search_presets_web.presets_view(settings, store, lambda _n: None)
    assert FAKE_KEY not in str(view)
    assert _view(settings, store)["keenable"]["key_set"] is True


def test_tavily_free_uses_keyless_header_then_key_replaces_it(settings, store):
    search_presets_web.activate(store, settings, "tavily")
    h = extensions_web.stored_headers_for(store, settings, "tavily")
    assert h.get("X-Tavily-Access-Mode") == "keyless"
    search_presets_web.activate(store, settings, "tavily", key="tvly-FAKE")
    h = extensions_web.stored_headers_for(store, settings, "tavily")
    assert h.get("Authorization") == "Bearer tvly-FAKE"
    assert "X-Tavily-Access-Mode" not in h
    # 改回免密钥
    search_presets_web.activate(store, settings, "tavily", clear_key=True)
    h = extensions_web.stored_headers_for(store, settings, "tavily")
    assert "Authorization" not in h and h.get("X-Tavily-Access-Mode") == "keyless"
    assert _view(settings, store)["tavily"]["key_set"] is False


def test_firecrawl_key_switches_url(settings, store):
    search_presets_web.activate(store, settings, "firecrawl")
    assert _entry(settings, store, "firecrawl").url == "https://mcp.firecrawl.dev/mcp"
    search_presets_web.activate(store, settings, "firecrawl", key="fc-FAKE")
    assert _entry(settings, store, "firecrawl").url == "https://mcp.firecrawl.dev/v2/mcp"


def test_tinyfish_without_key_rejected(settings, store):
    with pytest.raises(ValueError) as ei:
        search_presets_web.activate(store, settings, "tinyfish")
    assert "agent.tinyfish.ai" in str(ei.value)
    assert _entry(settings, store, "tinyfish") is None
    search_presets_web.activate(store, settings, "tinyfish", key="sk-tinyfish-FAKE")
    assert _entry(settings, store, "tinyfish").enabled


def test_clear_key_on_non_free_rejected(settings, store):
    search_presets_web.activate(store, settings, "tinyfish", key="sk-tinyfish-FAKE")
    with pytest.raises(ValueError):
        search_presets_web.activate(store, settings, "tinyfish", clear_key=True)


def test_unknown_preset_rejected(settings, store):
    with pytest.raises(KeyError):
        search_presets_web.activate(store, settings, "google")


def test_existing_entry_recognized_by_url_not_duplicated(settings, store):
    # 线上老条目：名字叫 You、地址 api.you.com、带 Authorization 头
    extensions_web.create(store, settings, {
        "name": "You", "url": "https://api.you.com/mcp", "headers": {"Authorization": "Bearer ydc-FAKE"},
        "roles": ["main", "worker"], "enabled": True, "tools": [], "timeout_s": 30,
    })
    v = _view(settings, store)["you"]
    assert v["entry"] == "You" and v["key_set"] is True
    name, _ = search_presets_web.activate(store, settings, "you")
    assert name == "You"
    assert [e.name for e in extensions_web.merged_entries(settings, store)] == ["You"]


def test_existing_disabled_entry_gets_enabled(settings, store):
    extensions_web.create(store, settings, {
        "name": "kn", "url": "https://api.keenable.ai/mcp", "headers": {},
        "roles": ["worker"], "enabled": False, "tools": [], "timeout_s": 30,
    })
    name, _ = search_presets_web.activate(store, settings, "keenable")
    assert name == "kn" and _entry(settings, store, "kn").enabled


def test_name_clash_with_unrelated_entry_gets_suffix(settings, store):
    extensions_web.create(store, settings, {
        "name": "exa", "url": "https://other.example/mcp", "headers": {},
        "roles": ["worker"], "enabled": True, "tools": [], "timeout_s": 30,
    })
    name, _ = search_presets_web.activate(store, settings, "exa")
    assert name != "exa" and name.startswith("exa")
    assert _entry(settings, store, name).url.startswith("https://mcp.exa.ai/mcp")


def test_setup_first_is_main_rest_fallback(settings, store):
    out = search_presets_web.setup(store, settings, [{"id": "exa"}, {"id": "keenable", "key": FAKE_KEY}, {"id": "you"}])
    assert out == ["exa", "keenable", "you"]
    b = get_binding(store)
    assert b["mcp"] == "exa" and b["tool"] == "web_search_advanced_exa"
    assert b.get("fallback") == ["keenable", "you"]


def test_setup_replaces_existing_main(settings, store):
    set_binding(store, {"mcp": "old", "tool": "t"})
    search_presets_web.setup(store, settings, [{"id": "keenable"}])
    assert get_binding(store)["mcp"] == "keenable"


def test_setup_validates_all_before_writing(settings, store):
    with pytest.raises(ValueError):
        search_presets_web.setup(store, settings, [{"id": "keenable"}, {"id": "tinyfish"}])
    assert _entry(settings, store, "keenable") is None
    assert get_binding(store) is None


def test_setup_empty_rejected(settings, store):
    with pytest.raises(ValueError):
        search_presets_web.setup(store, settings, [])


# ----------------------------------------------------------------------
# 接口：GET /api/extensions/presets、POST /api/extensions/presets/{id}、POST /api/extensions/presets-setup
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_presets_flow(tmp_path: Path):
    from test_extensions_web import _login, _make

    app, client = await _make(tmp_path)
    try:
        r = await client.get("/api/extensions/presets")
        assert r.status == 401  # 没登录
        await _login(client)
        r = await client.get("/api/extensions/presets")
        assert r.status == 200
        body = await r.json()
        assert [p["id"] for p in body["presets"]][:2] == ["keenable", "tavily"]

        r = await client.post("/api/extensions/presets/tinyfish", json={})
        assert r.status == 400
        r = await client.post("/api/extensions/presets/nope", json={})
        assert r.status == 404

        r = await client.post("/api/extensions/presets/keenable", json={"key": FAKE_KEY})
        assert r.status == 200
        text = await r.text()
        assert FAKE_KEY not in text
        out = await r.json()
        assert out["name"] == "keenable" and out["bound"] is True
        k = next(p for p in out["presets"] if p["id"] == "keenable")
        assert k["enabled"] and k["key_set"]
        r = await client.get("/api/extensions")
        assert FAKE_KEY not in await r.text()

        r = await client.post("/api/extensions/presets-setup", json={"items": [{"id": "exa"}, {"id": "keenable"}]})
        assert r.status == 200
        out = await r.json()
        assert out["search"]["binding"]["mcp"] == "exa"
        assert out["search"]["binding"].get("fallback") == ["keenable"]
        r = await client.post("/api/extensions/presets-setup", json={"items": []})
        assert r.status == 400
    finally:
        await client.close()
        await app.stop()
