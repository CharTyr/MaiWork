"""skill 开关（启用/停用）测试——后端行为与 API（docs/02 §10、docs/07 §10.9）。

策略口径：
- 存法：kv["extensions.skills.disabled"] = [名字]（手动停用名单）；通才 skill 默认启用。
- 通才 skill（web/手动放的）：effective = 不在手动停用名单里。
- search-<preset>（六家搜索服务内置 skill，如 search-keenable/search-tavily）：
  effective = 没手动停用 AND 至少一条被该预设认得出的 MCP 扩展条目 enabled。
  认 url 按 search_presets.preset_of_url 的主机名。
  关闭对应搜索服务（它名下全部 preset-recognized MCP 条目都 disabled）→ 该 search
  skill 从 list/read/hint 里消失；重新开启（任一匹配条目 enabled）→ 自动回来（除非
  还被手动停用）。
- 「effective 生效态」跟着 store/settings 现查，不许用开 app 时的旧快照（没有 stale cache）。

read/list 口径（Skills 运行时，给模型/子 agent）：
- 停用的 skill：list() 不含、hint() 不含、read() → None、read_file（references 等附属）→ None、
  roles() → None（read_skill 工具因此回「没有叫这个名字的 skill」）。
- 管理员网页 GET 详情（skills_web.get_view）仍然能读到停用的（管理要能看）。

news-standard（资讯标准，内置、自动注入资讯流水线）：停用不能让流水线的自动质量
标准没了（那是 policy 必须保住）；但模型自己 read/list 时遵守手动开关。news_standard.py
是程序侧按环节直读文件，不走 Skills——本条专门验证「手动开关只管模型侧 skill 工具，
不动程序注入」。

API：POST /api/extensions/skills/{name}/toggle {enabled: bool}，只管理员 + 写同源守卫；
enabled 非 bool / 缺字段 / 名字不存在 → 400/404；GET /api/extensions 的 skills 项与
GET /api/extensions/skills/{name} 详情都带 enabled / manual_enabled / disabled_reason
（disabled_reason 为中文，服务未开启时是「对应的搜索服务未开启」）。

只有「内置的搜索 skill 名」（search-<preset>，preset 存在）才走服务闸；
用户上传一个同名 search-foo 会被内置挡住（builtin 优先），但那是 builtin 的影子
规则，不影响「通才 web skill 走默认启用」。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import extensions_web, skills_web
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.skills import Skills
from CharTyr_MaiWork.maiwork.skills_tools import register_skill_tools
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import ToolContext, Tools
from test_console import PASSWORD
from test_extensions import _McpServer, _tool_spec
from test_extensions_web import _kv, _login, _make

G1 = "900000001"

# keenable 免密钥端点；preset_of_url 的主机名认得出
KEENABLE_URL = "https://api.keenable.ai/mcp"
TAVILY_URL = "https://mcp.tavily.com/mcp/"


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------


def _settings(mcp: list[dict] | None = None):
    raw: dict = {}
    if mcp is not None:
        raw["extensions"] = {"mcp": mcp}
    s, problems = load_settings(raw)
    assert problems == [], problems
    return s


def _store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.migrate()
    return s


def _make_web_skill(data_dir: Path, name: str, body: str = "随便写点正文。\n") -> None:
    """手动在数据目录放一个 skill（source=file）。"""
    d = data_dir / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 通才 skill {name}\nroles: worker\n---\n\n{body}",
        encoding="utf-8",
    )


def _make_web_skill_with_ref(data_dir: Path, name: str) -> None:
    _make_web_skill(data_dir, name)
    refs = data_dir / "skills" / name / "references"
    refs.mkdir()
    (refs / "notes.md").write_text("附属参考文件。\n", encoding="utf-8")


def _skill_list_names(sk: Skills) -> list[str]:
    return [str(i.get("name") or "") for i in sk.list()]


# ----------------------------------------------------------------------
# 1. 通才 skill：停用 / 恢复 / 持久化（Skills 运行时 read/list/hint/references）
# ----------------------------------------------------------------------


class TestGeneralSkillToggle:
    def test_default_enabled(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _make_web_skill(tmp_path, "alpha")
        sk = Skills(tmp_path, store=store, settings=_settings())
        assert "alpha" in _skill_list_names(sk)
        assert sk.read("alpha") is not None
        assert sk.roles("alpha") == ["worker"]
        assert "alpha" in sk.hint("worker")

    def test_disable_hides_from_read_and_list(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _make_web_skill(tmp_path, "alpha")
        sk = Skills(tmp_path, store=store, settings=_settings())
        skills_web.toggle(tmp_path, store, "alpha", False)
        assert "alpha" not in _skill_list_names(sk)
        assert "alpha" not in sk.hint("worker")
        assert sk.read("alpha") is None
        assert sk.roles("alpha") is None
        assert sk.is_effectively_active("alpha") is False

    def test_disable_blocks_reference_files(self, tmp_path: Path) -> None:
        """停用后 skill 目录里的 references 等附属文件也读不到。"""
        store = _store(tmp_path)
        _make_web_skill_with_ref(tmp_path, "alpha")
        sk = Skills(tmp_path, store=store, settings=_settings())
        assert sk.read_file("alpha", "references/notes.md") is not None
        skills_web.toggle(tmp_path, store, "alpha", False)
        assert sk.read_file("alpha", "references/notes.md") is None
        assert sk.read("alpha") is None

    def test_reenable_restores(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _make_web_skill(tmp_path, "alpha")
        sk = Skills(tmp_path, store=store, settings=_settings())
        skills_web.toggle(tmp_path, store, "alpha", False)
        assert sk.is_effectively_active("alpha") is False
        skills_web.toggle(tmp_path, store, "alpha", True)
        assert sk.is_effectively_active("alpha") is True
        assert sk.read("alpha") is not None
        assert "alpha" in _skill_list_names(sk)

    def test_toggle_persists_in_kv(self, tmp_path: Path) -> None:
        """写成 kv["extensions.skills.disabled"]；重读（另一个 store 实例）仍然有效。"""
        store = _store(tmp_path)
        _make_web_skill(tmp_path, "alpha")
        skills_web.toggle(tmp_path, store, "alpha", False)
        assert "alpha" in store.kv_get("extensions.skills.disabled")
        store.close()
        store2 = Store(tmp_path / "t.db")
        store2.migrate()
        try:
            assert "alpha" in store2.kv_get("extensions.skills.disabled")
        finally:
            store2.close()

    def test_unknown_name_rejected(self, tmp_path: Path) -> None:
        """toggle 不存在的名字 → KeyError（接口层 404）。"""
        store = _store(tmp_path)
        with pytest.raises(KeyError):
            skills_web.toggle(tmp_path, store, "no-such-skill", False)


# ----------------------------------------------------------------------
# 2. search-<preset> skill：服务开/关、多匹配、手动停用压倒自动态
# ----------------------------------------------------------------------


class TestSearchPresetSkillPolicy:
    def _skills(self, tmp_path: Path, store: Store, mcp: list[dict]) -> Skills:
        settings = _settings(mcp)
        return Skills(tmp_path, store=store, settings=settings)

    def test_default_disabled_when_no_matching_mcp(self, tmp_path: Path) -> None:
        """没有对应 MCP 条目时，search-keenable 默认 effective=False。"""
        store = _store(tmp_path)
        sk = self._skills(tmp_path, store, [])
        assert "search-keenable" not in _skill_list_names(sk)
        assert sk.read("search-keenable") is None
        assert sk.is_effectively_active("search-keenable") is False

    def test_service_on_activates(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        mcp = [{"name": "keen", "url": KEENABLE_URL, "enabled": True}]
        sk = self._skills(tmp_path, store, mcp)
        assert "search-keenable" in _skill_list_names(sk)
        assert sk.is_effectively_active("search-keenable") is True

    def test_service_off_hides(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        mcp = [{"name": "keen", "url": KEENABLE_URL, "enabled": True}]
        sk = self._skills(tmp_path, store, mcp)
        assert "search-keenable" in _skill_list_names(sk)
        extensions_web.toggle(store, _settings(mcp), "keen", False)
        assert "search-keenable" not in _skill_list_names(sk)
        assert sk.is_effectively_active("search-keenable") is False
        assert sk.read("search-keenable") is None
        extensions_web.toggle(store, _settings(mcp), "keen", True)
        assert sk.is_effectively_active("search-keenable") is True
        assert "search-keenable" in _skill_list_names(sk)

    def test_multiple_matching_any_enabled(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        mcp = [
            {"name": "keen-a", "url": KEENABLE_URL, "enabled": True},
            {"name": "keen-b", "url": KEENABLE_URL, "enabled": True},
        ]
        settings = _settings(mcp)
        sk = self._skills(tmp_path, store, mcp)
        assert sk.is_effectively_active("search-keenable") is True
        extensions_web.toggle(store, settings, "keen-a", False)
        assert sk.is_effectively_active("search-keenable") is True
        extensions_web.toggle(store, settings, "keen-b", False)
        assert sk.is_effectively_active("search-keenable") is False
        extensions_web.toggle(store, settings, "keen-a", True)
        assert sk.is_effectively_active("search-keenable") is True

    def test_manual_disable_overrides_service_on(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        mcp = [{"name": "keen", "url": KEENABLE_URL, "enabled": True}]
        sk = self._skills(tmp_path, store, mcp)
        assert sk.is_effectively_active("search-keenable") is True
        skills_web.toggle(tmp_path, store, "search-keenable", False)
        assert sk.is_effectively_active("search-keenable") is False
        assert "search-keenable" not in _skill_list_names(sk)
        skills_web.toggle(tmp_path, store, "search-keenable", True)
        assert sk.is_effectively_active("search-keenable") is True

    def test_disabled_reason_service_off(self, tmp_path: Path) -> None:
        """disabled_reason：服务未开启是中文「对应的搜索服务未开启」；手动是手动理由。"""
        store = _store(tmp_path)
        sk = self._skills(tmp_path, store, [])
        st = sk.status_of("search-keenable")
        assert st["enabled"] is False
        assert st["manual_enabled"] is True
        assert st["disabled_reason"] == "对应的搜索服务未开启"
        skills_web.toggle(tmp_path, store, "search-keenable", False)
        st = sk.status_of("search-keenable")
        assert st["enabled"] is False
        assert st["manual_enabled"] is False
        assert "手动" in st["disabled_reason"]

    def test_other_search_skill_unaffected(self, tmp_path: Path) -> None:
        """多个 search skill 同时：开哪一家跟哪一个，别的不动。"""
        store = _store(tmp_path)
        settings = _settings(
            [
                {"name": "keen", "url": KEENABLE_URL, "enabled": True},
                {"name": "tvly", "url": TAVILY_URL, "enabled": False},
            ]
        )
        sk = Skills(tmp_path, store=store, settings=settings)
        assert sk.is_effectively_active("search-keenable") is True
        assert sk.is_effectively_active("search-tavily") is False

    def test_uploaded_same_name_does_not_shadow_builtin(self, tmp_path: Path) -> None:
        """数据目录同名的 search-keenable：内置优先（这条是 builtin 影子规则的回归）。"""
        store = _store(tmp_path)
        _make_web_skill(tmp_path, "search-keenable")
        settings = _settings([])
        sk = Skills(tmp_path, store=store, settings=settings)
        assert sk.is_effectively_active("search-keenable") is False
        settings = _settings([{"name": "keen", "url": KEENABLE_URL, "enabled": True}])
        sk = Skills(tmp_path, store=store, settings=settings)
        assert sk.is_effectively_active("search-keenable") is True


# ----------------------------------------------------------------------
# 3. 资讯标准（news-standard）：手动开关只挡模型侧，不挡程序自动注入
# ----------------------------------------------------------------------


class TestNewsStandardProtection:
    def test_news_standard_automatic_ignores_disabled(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        settings = _settings([{"name": "keen", "url": KEENABLE_URL, "enabled": True}])
        sk = Skills(tmp_path, store=store, settings=settings)
        assert sk.is_effectively_active("news-standard") is True
        skills_web.toggle(tmp_path, store, "news-standard", False)
        assert sk.is_effectively_active("news-standard") is False
        assert sk.read("news-standard") is None
        # 程序注入仍然活得好好的：news_standard.py 直读文件，不走 Skills
        from CharTyr_MaiWork.maiwork import news_standard

        text = news_standard.section("criteria", "资讯")
        assert text, "news_standard 自动注入不能被 skill 手动停开关影响"
        assert "资讯" in text

    def test_news_standard_get_detail_allows_admin(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        skills_web.toggle(tmp_path, store, "news-standard", False)
        view = skills_web.get_view(tmp_path, store, "news-standard", _settings())
        assert view is not None
        assert view["enabled"] is False
        assert view["manual_enabled"] is False


# ----------------------------------------------------------------------
# 4. read_skill / list_skills 工具：模型侧遵守开关
# ----------------------------------------------------------------------


class TestSkillsToolsObeySwitch:
    @staticmethod
    def _make_ctx(role: str = "worker") -> ToolContext:
        return ToolContext(group_id=G1, role=role)

    @pytest.mark.asyncio
    async def test_read_skill_blocked_when_disabled(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _make_web_skill(tmp_path, "alpha")
        tools = Tools(store)
        sk = Skills(tmp_path, store=store, settings=_settings())
        register_skill_tools(tools, sk)
        ctx = self._make_ctx("worker")
        res = await tools.call("read_skill", {"name": "alpha"}, ctx)
        assert res.ok, res.error
        skills_web.toggle(tmp_path, store, "alpha", False)
        res = await tools.call("read_skill", {"name": "alpha"}, ctx)
        assert not res.ok
        assert "没有" in (res.error or "")
        res = await tools.call("list_skills", {}, ctx)
        assert res.ok
        assert "alpha" not in (res.output or "")

    @pytest.mark.asyncio
    async def test_read_skill_blocked_reference_file_when_disabled(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _make_web_skill_with_ref(tmp_path, "alpha")
        tools = Tools(store)
        sk = Skills(tmp_path, store=store, settings=_settings())
        register_skill_tools(tools, sk)
        ctx = self._make_ctx("worker")
        res = await tools.call("read_skill", {"name": "alpha", "file": "references/notes.md"}, ctx)
        assert res.ok
        skills_web.toggle(tmp_path, store, "alpha", False)
        res = await tools.call("read_skill", {"name": "alpha", "file": "references/notes.md"}, ctx)
        assert not res.ok
        assert "没有" in (res.error or "")


# ----------------------------------------------------------------------
# 5. 网页 list_view / get_view 的三个字段
# ----------------------------------------------------------------------


class TestWebViewHasSwitchFields:
    def test_list_view_general_default_enabled(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _make_web_skill(tmp_path, "alpha")
        view = skills_web.list_view(tmp_path, store, _settings())
        alpha = next(s for s in view if s["name"] == "alpha")
        assert alpha["enabled"] is True
        assert alpha["manual_enabled"] is True
        assert alpha["disabled_reason"] == ""

    def test_list_view_manual_disabled_fields(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _make_web_skill(tmp_path, "alpha")
        skills_web.toggle(tmp_path, store, "alpha", False)
        view = skills_web.list_view(tmp_path, store, _settings())
        alpha = next(s for s in view if s["name"] == "alpha")
        assert alpha["enabled"] is False
        assert alpha["manual_enabled"] is False
        assert "手动" in alpha["disabled_reason"]

    def test_list_view_search_skill_service_off(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        view = skills_web.list_view(tmp_path, store, _settings())
        keen = next(s for s in view if s["name"] == "search-keenable")
        assert keen["enabled"] is False
        assert keen["manual_enabled"] is True
        assert keen["disabled_reason"] == "对应的搜索服务未开启"
        news = next(s for s in view if s["name"] == "news-standard")
        assert news["enabled"] is True
        assert news["manual_enabled"] is True
        assert news["disabled_reason"] == ""

    def test_list_view_search_skill_service_on(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        settings = _settings([{"name": "keen", "url": KEENABLE_URL, "enabled": True}])
        view = skills_web.list_view(tmp_path, store, settings)
        keen = next(s for s in view if s["name"] == "search-keenable")
        assert keen["enabled"] is True
        assert keen["manual_enabled"] is True
        assert keen["disabled_reason"] == ""

    def test_get_view_has_fields(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _make_web_skill(tmp_path, "alpha")
        view = skills_web.get_view(tmp_path, store, "alpha", _settings())
        assert view["enabled"] is True
        assert view["manual_enabled"] is True
        assert view["disabled_reason"] == ""
        skills_web.toggle(tmp_path, store, "alpha", False)
        view = skills_web.get_view(tmp_path, store, "alpha", _settings())
        assert view["enabled"] is False
        assert view["manual_enabled"] is False


# ----------------------------------------------------------------------
# 6. API：POST /api/extensions/skills/{name}/toggle
# ----------------------------------------------------------------------


class TestSkillsToggleApi:
    @pytest.mark.asyncio
    async def test_anonymous_401(self, tmp_path: Path) -> None:
        app, client = await _make(tmp_path)
        try:
            r = await client.post(
                "/api/extensions/skills/search-keenable/toggle",
                json={"enabled": False},
            )
            assert r.status in (401, 403)
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_group_member_403(self, tmp_path: Path) -> None:
        app, client = await _make(tmp_path)
        try:
            token = app.token_of(G1)
            r = await client.post(
                "/api/extensions/skills/alpha/toggle",
                json={"enabled": False},
                headers={"X-MW-Group": token},
            )
            assert r.status == 403
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_origin_guard(self, tmp_path: Path) -> None:
        app, client = await _make(tmp_path)
        try:
            await _login(client)
            r = await client.post(
                "/api/extensions/skills/search-keenable/toggle",
                json={"enabled": False},
                headers={"Origin": "https://evil.example"},
            )
            assert r.status == 403
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_toggle_and_persist(self, tmp_path: Path) -> None:
        server = _McpServer([_tool_spec("search")])
        app, client = await _make(
            tmp_path,
            server=server,
            config_mcp=[{"name": "keen", "url": KEENABLE_URL, "enabled": True}],
        )
        try:
            await _login(client)
            r = await client.post(
                "/api/extensions/skills/search-keenable/toggle",
                json={"enabled": False},
            )
            assert r.status == 200, await r.text()
            assert "search-keenable" in _kv(app, "extensions.skills.disabled")
            r = await client.get("/api/extensions/skills/search-keenable")
            assert r.status == 200
            data = await r.json()
            assert data["enabled"] is False
            assert data["manual_enabled"] is False
            r = await client.get("/api/extensions")
            assert r.status == 200
            data = await r.json()
            keen = next(s for s in data["skills"] if s["name"] == "search-keenable")
            assert keen["enabled"] is False
            assert keen["manual_enabled"] is False
            assert keen["disabled_reason"] != ""
            r = await client.post(
                "/api/extensions/skills/search-keenable/toggle",
                json={"enabled": True},
            )
            assert r.status == 200
            assert not _kv(app, "extensions.skills.disabled")
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_toggle_rejects_nonboolean(self, tmp_path: Path) -> None:
        app, client = await _make(tmp_path)
        try:
            await _login(client)
            for bad in ("true", 1, 0, "false", None):
                r = await client.post(
                    "/api/extensions/skills/alpha/toggle",
                    json={"enabled": bad},
                )
                assert r.status in (400, 404), f"enabled={bad!r} -> {r.status}"
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_toggle_missing_enabled_field(self, tmp_path: Path) -> None:
        app, client = await _make(tmp_path)
        try:
            await _login(client)
            r = await client.post("/api/extensions/skills/alpha/toggle", json={})
            assert r.status == 400
            r = await client.post("/api/extensions/skills/alpha/toggle", json={"foo": 1})
            assert r.status == 400
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_toggle_unknown_skill_404(self, tmp_path: Path) -> None:
        app, client = await _make(tmp_path)
        try:
            await _login(client)
            r = await client.post(
                "/api/extensions/skills/no-such-skill/toggle",
                json={"enabled": False},
            )
            assert r.status == 404
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_toggle_bad_name_in_url_rejected(self, tmp_path: Path) -> None:
        app, client = await _make(tmp_path)
        try:
            await _login(client)
            r = await client.post(
                "/api/extensions/skills/bad%2Fname/toggle",
                json={"enabled": False},
            )
            assert r.status in (400, 404)
        finally:
            await client.close()
            await app.stop()


# ----------------------------------------------------------------------
# 7. feeds 注入策略：跟 Skills 同一套（只验证策略入口，不构造重 Feeds）
# ----------------------------------------------------------------------


class TestFeedsProviderSectionPolicy:
    def test_provider_on_off_path_identical_to_skills(self, tmp_path: Path) -> None:
        """feeds 的 provider 开关最后查的和 Skills 是同一个 effective 值。"""
        store = _store(tmp_path)
        settings = _settings([{"name": "keen", "url": KEENABLE_URL, "enabled": True}])
        sk = Skills(tmp_path, store=store, settings=settings)
        assert sk.is_effectively_active("search-keenable") is True
        extensions_web.toggle(store, settings, "keen", False)
        sk = Skills(tmp_path, store=store, settings=settings)
        assert sk.is_effectively_active("search-keenable") is False


class TestFeedsProviderSectionInjection:
    """直接构造轻 Feeds，调 _provider_skill_section，验证它跟 Skills 同一个 effective 值。

    特别要求：没接 _skills（裸 Feeds 构造）也不能绕开手动/服务闸；判定出错要 fail-closed。
    """

    def _make_feeds(self, tmp_path: Path, mcp: list[dict]):
        from CharTyr_MaiWork.maiwork import search_binding
        from CharTyr_MaiWork.maiwork.config import load_settings
        from CharTyr_MaiWork.maiwork.feeds import Feeds

        data_dir = tmp_path / "data"
        store = _store(tmp_path)
        raw: dict = {"storage": {"data_dir": str(data_dir)}}
        if mcp is not None:
            raw["extensions"] = {"mcp": mcp}
        settings, problems = load_settings(raw)
        assert problems == []
        search_binding.set_binding(store, {"mcp": "keen", "tool": "search_web_pages"})
        feeds = Feeds(store, None, None, None, None, lambda: settings)
        return store, settings, feeds

    def test_injection_shown_when_preset_on_and_active(self, tmp_path: Path) -> None:
        _, settings, feeds = self._make_feeds(tmp_path, [{"name": "keen", "url": KEENABLE_URL, "enabled": True}])
        text = feeds._provider_skill_section(settings)
        assert text, "服务开着 + skill 激活时要注入"
        assert "Keenable" in text
        assert len(text) > 50

    def test_injection_suppressed_when_service_off(self, tmp_path: Path) -> None:
        store, settings, feeds = self._make_feeds(tmp_path, [{"name": "keen", "url": KEENABLE_URL, "enabled": True}])
        assert feeds._provider_skill_section(settings)
        from CharTyr_MaiWork.maiwork import extensions_web

        extensions_web.toggle(store, settings, "keen", False)
        text = feeds._provider_skill_section(settings)
        assert text == "", f"服务关了不该注入，得到了：{text[:80]!r}"

    def test_injection_suppressed_when_manually_disabled(self, tmp_path: Path) -> None:
        store, settings, feeds = self._make_feeds(tmp_path, [{"name": "keen", "url": KEENABLE_URL, "enabled": True}])
        assert feeds._provider_skill_section(settings)
        skills_web.toggle(tmp_path / "data", store, "search-keenable", False)
        text = feeds._provider_skill_section(settings)
        assert text == "", f"手动停了不该注入，得到了：{text[:80]!r}"

    def test_injection_restores_service_reopen(self, tmp_path: Path) -> None:
        store, settings, feeds = self._make_feeds(tmp_path, [{"name": "keen", "url": KEENABLE_URL, "enabled": True}])
        from CharTyr_MaiWork.maiwork import extensions_web

        extensions_web.toggle(store, settings, "keen", False)
        assert feeds._provider_skill_section(settings) == ""
        extensions_web.toggle(store, settings, "keen", True)
        assert feeds._provider_skill_section(settings), "重开服务后要能注入"

    def test_injection_manual_disable_survives_service_reopen(self, tmp_path: Path) -> None:
        """手动停了：服务重开也不会自动回来；恢复手动开关才行。"""
        store, settings, feeds = self._make_feeds(tmp_path, [{"name": "keen", "url": KEENABLE_URL, "enabled": True}])
        from CharTyr_MaiWork.maiwork import extensions_web

        skills_web.toggle(tmp_path / "data", store, "search-keenable", False)
        extensions_web.toggle(store, settings, "keen", False)
        assert feeds._provider_skill_section(settings) == ""
        extensions_web.toggle(store, settings, "keen", True)
        assert feeds._provider_skill_section(settings) == "", "手动还停时服务重开也不该注入"
        skills_web.toggle(tmp_path / "data", store, "search-keenable", True)
        assert feeds._provider_skill_section(settings), "手动恢复后要能注入"

    def test_injection_suppressed_bare_feeds_no_skills_attr(self, tmp_path: Path) -> None:
        """裸构造（没 app 起 _skills）：走 fallback 构造 Skills，手动停同样拦得住。"""
        store, settings, feeds = self._make_feeds(tmp_path, [{"name": "keen", "url": KEENABLE_URL, "enabled": True}])
        # 确认 _skills 没接（保持 None）
        assert getattr(feeds, "_skills", None) is None
        skills_web.toggle(tmp_path / "data", store, "search-keenable", False)
        text = feeds._provider_skill_section(settings)
        assert text == "", f"裸构造下手动停也不该注入，得到了：{text[:80]!r}"

    def test_injection_fail_closed_on_bad_store(self, tmp_path: Path) -> None:
        """判定路径抛错 → fail-closed（按不住 skill，宁岂不出用法），但不能漏给模型。"""
        store, settings, feeds = self._make_feeds(tmp_path, [{"name": "keen", "url": KEENABLE_URL, "enabled": True}])
        assert feeds._provider_skill_section(settings)

        # 弄坏 store.kv_get：让 手动停 名单读不到 → 按保守策略当停（不泄露）
        original_kv_get = store.kv_get

        def broken_kv_get(key):
            if key == "extensions.skills.disabled":
                raise RuntimeError("injected kv failure")
            return original_kv_get(key)

        store.kv_get = broken_kv_get  # type: ignore[method-assign]
        try:
            # fail-closed：读不到手动停名单就当他已经停了（保守，宁岂不出也不泄露用法给模型）
            text = feeds._provider_skill_section(settings)
            assert text == "", (
                "离线故障下不该注入用法给子 agent；实际得到：" + (text[:80] if text else "<空>")
            )
        finally:
            store.kv_get = original_kv_get  # type: ignore[method-assign]
        # 恢复后系统照旧正常工作（没被假故障破坏状态）
        assert feeds._provider_skill_section(settings)

    def test_injection_blocked_when_bound_disabled_but_other_same_preset_enabled(self, tmp_path: Path) -> None:
        """闸的硬要求：绑定的那条本身必须 enabled。别的同预设 enabled 不救——
        绑定被关 = 搜索不走它，官方用法也不该塞给子 agent。"""
        from CharTyr_MaiWork.maiwork import extensions_web

        store, settings, feeds = self._make_feeds(
            tmp_path,
            [
                {"name": "keen", "url": KEENABLE_URL, "enabled": True},
                {"name": "keen-other", "url": KEENABLE_URL, "enabled": True},
            ],
        )
        assert feeds._provider_skill_section(settings)
        # 关掉被绑定的那条；keen-other 还开着（同预设仍然有人 enabled）
        extensions_web.toggle(store, settings, "keen", False)
        text = feeds._provider_skill_section(settings)
        assert text == "", f"绑定这条被关，不该因另一条同预设而注入，得到了：{text[:80]!r}"
        # 把绑定的重开（keen-other 此时还开）→ 恢复注入
        extensions_web.toggle(store, settings, "keen", True)
        assert feeds._provider_skill_section(settings)

    def test_disabled_bound_entry_not_used_when_another_is_enabled(self, tmp_path: Path) -> None:
        store, settings, feeds = self._make_feeds(tmp_path, [
            {"name": "keen", "url": KEENABLE_URL, "enabled": False},
            {"name": "other", "url": KEENABLE_URL, "enabled": True},
        ])
        assert Skills(tmp_path, store=store, settings=settings).is_effectively_active("search-keenable")
        assert feeds._provider_skill_section(settings) == ""

    def test_injection_with_missing_data_dir_still_checks_switch(self, tmp_path: Path) -> None:
        from types import SimpleNamespace
        store, settings, feeds = self._make_feeds(tmp_path, [{"name": "keen", "url": KEENABLE_URL, "enabled": True}])
        skills_web.toggle(tmp_path / "data", store, "search-keenable", False)
        no_dir = SimpleNamespace(extensions=settings.extensions)
        assert feeds._provider_skill_section(no_dir) == ""
