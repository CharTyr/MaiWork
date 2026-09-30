"""旧 [models] → [[endpoints]] + [[model_list]] + 专岗选择的一次性迁移（改版 1a）。

- 触发：config.toml 的 [models] base_url 非空、且还没有 [[endpoints]]。幂等。
- 搬法：旧端点 → [[endpoints]] 第一条「默认端点」（openai 协议，带着 api_key / 重试 / 限流）；
  main / main_backup / worker / worker_backup 四个槽去重后 → [[model_list]]
  （上下文 / 最大输出沿用旧全局值）；专岗「主模型」给主 + 备，「资讯/构想/目标/任务」
  给子 + 备；最后把旧 [models]本周从文件里删掉。
- 安全：写文件前先备份（config_file 的老机制）；密钥绝不进日志；
  岗位 kv 先写、文件后写——文件写砸时旧 [models] 还在，旧四槽兜底仍然能干活。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import config_file
from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.migrations import migrate_models_config_to_endpoints
from CharTyr_MaiWork.maiwork.store import Store

SECRET = "sk-迁移-见不得光"


def _plugin_dir(tmp_path: Path, models_text: str = "", extra: str = "") -> Path:
    d = tmp_path / "plug"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.toml").write_text(
        "# 注释留着\n"
        "[plugin]\n"
        "enabled = true\n"
        + (f"\n{models_text}\n" if models_text else "")
        + (f"\n{extra}\n" if extra else ""),
        encoding="utf-8",
    )
    return d


FULL_MODELS = """[models]
base_url = "https://old.test/v1"
api_key = "KEY"
main = "m-main"
main_backup = "m-bak"
worker = "m-worker"
worker_backup = "m-wbak"
retries = 3
retry_delay_s = 20
max_concurrency = 4
max_rpm = 60
context_window = 64000
max_tokens = 8192
""".replace("KEY", SECRET)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "data" / "m.db")
    s.migrate()
    yield s
    s.close()


class TestNormalMigration:
    def test_full_brick_migrates(self, tmp_path: Path, store: Store) -> None:
        plug = _plugin_dir(tmp_path, FULL_MODELS)
        did = migrate_models_config_to_endpoints(store, plug, tmp_path / "data")
        assert did is True
        settings, problems = load_settings(_toml_of(plug))
        assert problems == []
        assert len(settings.endpoints) == 1
        ep = settings.endpoints[0]
        assert ep.id == "default" and ep.name == "默认端点" and ep.protocol == "openai"
        assert ep.base_url == "https://old.test/v1" and ep.api_key == SECRET
        assert (ep.retries, ep.retry_delay_s, ep.max_concurrency, ep.max_rpm) == (3, 20, 4, 60)
        assert [m.model for m in settings.model_list] == ["m-main", "m-bak", "m-worker", "m-wbak"]
        assert [m.id for m in settings.model_list] == ["m1", "m2", "m3", "m4"]
        for m in settings.model_list:
            assert m.endpoint == "default"
            assert m.context_window == 64000 and m.max_tokens == 8192
            assert m.efforts == () and m.vision is False
        # 旧 [models] 从文件里删掉
        text = (plug / "config.toml").read_text(encoding="utf-8")
        assert "[models]" not in text
        assert "# 注释留着" in text
        assert 'm-main' in text
        # 岗位选择：主模型 = 主 + 备；资讯/构想/目标/任务 = 子 + 备
        agents = Agents(store, lambda: settings)
        p = {x["kind"]: x for x in agents.profiles()}
        assert p["main"]["model"] == "m1" and p["main"]["backup"] == "m2"
        for kind in ("news", "idea", "goal", "task"):
            assert p[kind]["model"] == "m3"
            assert p[kind]["backup"] == "m4"

    def test_empty_endpoint_lists_still_migrate(self, tmp_path: Path, store: Store) -> None:
        """文件里有空的 endpoints = [] / model_list = []（比如按配置 schema 补齐了默认值）
        不算「迁过」：照样搬，搬完空列表换成真条目。2026-10 冒烟测出来的。"""
        plug = _plugin_dir(tmp_path, FULL_MODELS)
        text = (plug / "config.toml").read_text(encoding="utf-8")
        (plug / "config.toml").write_text(
            text.replace("[plugin]", "endpoints = []\nmodel_list = []\n\n[plugin]", 1), encoding="utf-8"
        )
        assert migrate_models_config_to_endpoints(store, plug, tmp_path / "data") is True
        settings, problems = load_settings(_toml_of(plug))
        assert problems == []
        assert [e.id for e in settings.endpoints] == ["default"]
        assert [m.model for m in settings.model_list] == ["m-main", "m-bak", "m-worker", "m-wbak"]
        assert "[models]" not in (plug / "config.toml").read_text(encoding="utf-8")
        # 再跑一遍不动
        assert migrate_models_config_to_endpoints(store, plug, tmp_path / "data") is False

    def test_backup_written_before_change(self, tmp_path: Path, store: Store) -> None:
        plug = _plugin_dir(tmp_path, FULL_MODELS)
        old_text = (plug / "config.toml").read_text(encoding="utf-8")
        migrate_models_config_to_endpoints(store, plug, tmp_path / "data")
        backups = sorted((tmp_path / "data" / "config-backups").glob("config.toml-*"))
        assert backups, "写文件前必须有备份"
        assert backups[-1].read_text(encoding="utf-8") == old_text
        assert SECRET in backups[-1].read_text(encoding="utf-8")  # 备份 = 旧文件原文

    def test_api_key_never_logged(self, tmp_path: Path, store: Store, caplog) -> None:
        plug = _plugin_dir(tmp_path, FULL_MODELS)
        with caplog.at_level(logging.DEBUG):
            migrate_models_config_to_endpoints(store, plug, tmp_path / "data")
        whole = "\n".join(r.getMessage() for r in caplog.records)
        assert SECRET not in whole

    def test_existing_kv_fields_preserved(self, tmp_path: Path, store: Store) -> None:
        plug = _plugin_dir(tmp_path, FULL_MODELS)
        with store.tx() as conn:
            store.kv_set(conn, "agents.profiles", {"news": {"fish_seed": "koi-news", "custom": 1}})
        migrate_models_config_to_endpoints(store, plug, tmp_path / "data")
        raw = store.kv_get("agents.profiles", {})
        assert raw["news"]["fish_seed"] == "koi-news"
        assert raw["news"]["custom"] == 1
        assert raw["news"]["model"] == "m3"


class TestIdempotentAndNoop:
    def test_second_run_is_noop(self, tmp_path: Path, store: Store) -> None:
        plug = _plugin_dir(tmp_path, FULL_MODELS)
        assert migrate_models_config_to_endpoints(store, plug, tmp_path / "data") is True
        text_after = (plug / "config.toml").read_text(encoding="utf-8")
        assert migrate_models_config_to_endpoints(store, plug, tmp_path / "data") is False
        assert (plug / "config.toml").read_text(encoding="utf-8") == text_after

    def test_empty_old_config_creates_nothing(self, tmp_path: Path, store: Store) -> None:
        empty = '[models]\nbase_url = ""\nmain = ""\n'
        plug = _plugin_dir(tmp_path, empty)
        assert migrate_models_config_to_endpoints(store, plug, tmp_path / "data") is False
        text = (plug / "config.toml").read_text(encoding="utf-8")
        assert "[endpoints]" not in text
        assert store.kv_get("agents.profiles") in (None, {})

    def test_no_models_section_at_all(self, tmp_path: Path, store: Store) -> None:
        plug = _plugin_dir(tmp_path, "")
        assert migrate_models_config_to_endpoints(store, plug, tmp_path / "data") is False

    def test_already_migrated_is_noop(self, tmp_path: Path, store: Store) -> None:
        extra = FULL_MODELS + """
[[endpoints]]
id = "default"
protocol = "openai"
base_url = "https://new.test/v1"
api_key = "sk-new"
"""
        plug = _plugin_dir(tmp_path, extra)
        text_before = (plug / "config.toml").read_text(encoding="utf-8")
        assert migrate_models_config_to_endpoints(store, plug, tmp_path / "data") is False
        assert (plug / "config.toml").read_text(encoding="utf-8") == text_before


class TestDedupeAndPartial:
    def test_duplicate_model_names_deduped(self, tmp_path: Path, store: Store) -> None:
        dup = FULL_MODELS.replace('main_backup = "m-bak"', 'main_backup = "m-main"').replace(
            'worker_backup = "m-wbak"', 'worker_backup = "m-worker"'
        )
        plug = _plugin_dir(tmp_path, dup)
        migrate_models_config_to_endpoints(store, plug, tmp_path / "data")
        settings, _ = load_settings(_toml_of(plug))
        assert [m.model for m in settings.model_list] == ["m-main", "m-worker"]
        agents = Agents(store, lambda: settings)
        p = {x["kind"]: x for x in agents.profiles()}
        assert p["main"]["model"] == "m1"
        # 备用和首选同一条目 → 迁移不写 backup（备用不许 = 首选）
        assert p["main"]["backup"] == ""
        assert p["task"]["model"] == "m2"
        assert p["task"]["backup"] == ""

    def test_same_name_main_and_worker_share_entry(self, tmp_path: Path, store: Store) -> None:
        dup = FULL_MODELS.replace('worker = "m-worker"', 'worker = "m-main"').replace(
            'main_backup = "m-bak"', 'main_backup = ""'
        ).replace('worker_backup = "m-wbak"', 'worker_backup = ""')
        plug = _plugin_dir(tmp_path, dup)
        migrate_models_config_to_endpoints(store, plug, tmp_path / "data")
        settings, _ = load_settings(_toml_of(plug))
        assert [m.model for m in settings.model_list] == ["m-main"]
        agents = Agents(store, lambda: settings)
        p = {x["kind"]: x for x in agents.profiles()}
        assert p["main"]["model"] == "m1" and p["task"]["model"] == "m1"

    def test_only_main_no_worker(self, tmp_path: Path, store: Store) -> None:
        only_main = FULL_MODELS.replace('worker = "m-worker"', 'worker = ""').replace(
            'worker_backup = "m-wbak"', 'worker_backup = ""'
        )
        plug = _plugin_dir(tmp_path, only_main)
        migrate_models_config_to_endpoints(store, plug, tmp_path / "data")
        settings, _ = load_settings(_toml_of(plug))
        assert [m.model for m in settings.model_list] == ["m-main", "m-bak"]
        agents = Agents(store, lambda: settings)
        p = {x["kind"]: x for x in agents.profiles()}
        assert p["main"]["model"] == "m1"
        assert p["task"]["model"] == ""


class TestBrokenFile:
    def test_broken_toml_no_crash(self, tmp_path: Path, store: Store) -> None:
        plug = _plugin_dir(tmp_path, "")
        (plug / "config.toml").write_text("这是坏 toml = [", encoding="utf-8")
        assert migrate_models_config_to_endpoints(store, plug, tmp_path / "data") is False


def _toml_of(plug: Path) -> dict:
    import tomlkit

    return dict(tomlkit.parse((plug / "config.toml").read_text(encoding="utf-8")))
