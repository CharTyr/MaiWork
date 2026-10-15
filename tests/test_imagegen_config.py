"""生图模型条目（[[model_list]] 的 use / image_api 等字段）配置层测试。

先写测试再写实现。要点：
- 老配置（没有新字段）读出来一字不变：use="chat"；
- use="image" 的条目能被读出来，也能写回文件（config_file 字段白名单不能漏）；
- 不认识的值宽松回落 + 记中文问题（不丢整条），网页保存路径把问题当 400；
- use="image" 的条目**不能**当聊天模型：解析候选链时跳过、专岗选择拒绝。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.config_file import write_aot_section
from CharTyr_MaiWork.maiwork.models import Models
from CharTyr_MaiWork.maiwork.store import Store

EP = {"id": "default", "name": "默认", "protocol": "openai",
      "base_url": "https://api.test/v1", "api_key": "sk-test-key"}


def _settings(model_list, problems_out=None):
    settings, problems = load_settings(
        {"endpoints": [dict(EP)], "model_list": model_list}
    )
    if problems_out is not None:
        problems_out.extend(problems)
    return settings


def _entry(settings, mid):
    return next(m for m in settings.model_list if m.id == mid)


class TestBackwardCompat:
    def test_old_entry_defaults_to_chat(self) -> None:
        s = _settings([{"id": "m1", "endpoint": "default", "model": "gpt-x"}])
        m = _entry(s, "m1")
        assert m.use == "chat"
        assert m.image_api == "openai"
        assert m.image_default_size == ""
        assert m.image_max_n == 1

    def test_chat_entry_keeps_old_fields(self) -> None:
        s = _settings([{"id": "m1", "endpoint": "default", "model": "gpt-x",
                        "efforts": ["low"], "vision": True,
                        "context_window": 64000, "max_tokens": 4096}])
        m = _entry(s, "m1")
        assert (m.efforts, m.vision, m.context_window, m.max_tokens) == (("low",), True, 64000, 4096)


class TestImageEntry:
    def test_image_entry_parsed(self) -> None:
        s = _settings([{"id": "img1", "endpoint": "default", "model": "gpt-image-1",
                        "name": "生图", "use": "image", "image_api": "siliconflow",
                        "image_default_size": "1024x1024", "image_max_n": 4}])
        m = _entry(s, "img1")
        assert m.use == "image"
        assert m.image_api == "siliconflow"
        assert m.image_default_size == "1024x1024"
        assert m.image_max_n == 4

    def test_bad_use_falls_back_with_problem_keeps_entry(self) -> None:
        problems: list[str] = []
        s = _settings([{"id": "m1", "endpoint": "default", "model": "x", "use": "画图"}], problems)
        assert _entry(s, "m1").use == "chat"
        assert any("use" in p for p in problems), problems

    def test_bad_image_api_falls_back_with_problem(self) -> None:
        problems: list[str] = []
        s = _settings([{"id": "img1", "endpoint": "default", "model": "x",
                        "use": "image", "image_api": "gemini"}], problems)
        assert _entry(s, "img1").image_api == "openai"
        assert any("image_api" in p and "gemini" in p for p in problems), problems

    def test_image_max_n_out_of_range_clamped_with_problem(self) -> None:
        problems: list[str] = []
        s = _settings([{"id": "img1", "endpoint": "default", "model": "x",
                        "use": "image", "image_max_n": 99}], problems)
        assert _entry(s, "img1").image_max_n == 1
        assert any("image_max_n" in p for p in problems), problems

    def test_image_default_size_too_long_dropped(self) -> None:
        problems: list[str] = []
        s = _settings([{"id": "img1", "endpoint": "default", "model": "x",
                        "use": "image", "image_default_size": "x" * 200}], problems)
        assert _entry(s, "img1").image_default_size == ""
        assert any("image_default_size" in p for p in problems), problems

    def test_use_uppercase_tolerated(self) -> None:
        s = _settings([{"id": "img1", "endpoint": "default", "model": "x", "use": "IMAGE"}])
        assert _entry(s, "img1").use == "image"


class TestConfigFileRoundtrip:
    def test_write_and_read_back_image_fields(self, tmp_path: Path) -> None:
        plug = tmp_path / "plug"
        plug.mkdir()
        (plug / "config.toml").write_text("", encoding="utf-8")
        data = tmp_path / "data"
        data.mkdir()
        entries = [
            {"id": "img1", "endpoint": "default", "model": "gpt-image-1", "name": "生图",
             "use": "image", "image_api": "openai", "image_default_size": "1024x1024",
             "image_max_n": 2},
            {"id": "m1", "endpoint": "default", "model": "gpt-x"},
        ]
        write_aot_section(plug, data, "model_list", entries)
        text = (plug / "config.toml").read_text(encoding="utf-8")
        assert 'use = "image"' in text
        assert 'image_api = "openai"' in text
        assert "image_max_n = 2" in text

        raw = {
            "endpoints": [dict(EP)],
            "model_list": entries,
        }
        s, _ = load_settings(raw)
        m = _entry(s, "img1")
        assert (m.use, m.image_api, m.image_default_size, m.image_max_n) == (
            "image", "openai", "1024x1024", 2,
        )


class TestImageEntryIsNotAChatModel:
    def test_chat_candidate_resolution_skips_image_entry(self, tmp_path: Path) -> None:
        s = _settings([
            {"id": "img1", "endpoint": "default", "model": "gpt-image-1", "use": "image"},
            {"id": "m1", "endpoint": "default", "model": "gpt-x"},
        ])
        store = Store(tmp_path / "db.sqlite3")
        store.migrate()
        try:
            models = Models(store, lambda: s)
            cands = models._entries_to_candidates(("img1",), s.endpoints, s.model_list)
            assert cands == []
            ok = models._entries_to_candidates(("m1",), s.endpoints, s.model_list)
            assert [c.service_model for c in ok] == ["gpt-x"]
        finally:
            store.close()

    def test_agents_profile_rejects_image_entry(self, tmp_path: Path) -> None:
        s = _settings([
            {"id": "img1", "endpoint": "default", "model": "gpt-image-1", "use": "image"},
            {"id": "m1", "endpoint": "default", "model": "gpt-x"},
        ])
        store = Store(tmp_path / "db.sqlite3")
        store.migrate()
        try:
            a = Agents(store, lambda: s)
            with pytest.raises(ValueError) as e:
                a.update_profile("main", {"model": "img1"})
            assert "模型" in str(e.value)
            # 正常的聊天条目照样能选
            p = a.update_profile("main", {"model": "m1"})
            assert p["model"] == "m1"
        finally:
            store.close()

    def test_read_path_clears_stale_image_selection(self, tmp_path: Path) -> None:
        """先选了聊天条目，后来这条被改成生图：读路径按「没选」处理（不会拿生图模型聊天）。"""
        s = _settings([{"id": "m1", "endpoint": "default", "model": "gpt-x"}])
        store = Store(tmp_path / "db.sqlite3")
        store.migrate()
        try:
            a = Agents(store, lambda: s)
            a.update_profile("main", {"model": "m1"})
            assert a.profile("main")["model"] == "m1"
            s2 = _settings([{"id": "m1", "endpoint": "default", "model": "gpt-image-1", "use": "image"}])
            a2 = Agents(store, lambda: s2)
            assert a2.profile("main")["model"] == ""
        finally:
            store.close()

    def test_quick_judge_candidate_skips_image_entry(self, tmp_path: Path) -> None:
        s = _settings([{"id": "img1", "endpoint": "default", "model": "gpt-image-1", "use": "image"}])
        store = Store(tmp_path / "db.sqlite3")
        store.migrate()
        try:
            models = Models(store, lambda: s)
            assert models.candidates_for_entry("img1") == []
        finally:
            store.close()
