"""identity.py 专岗 SOUL / AGENTS（阶段 3）测试。

覆盖面：
- 存储路径：``identity/agents/<kind>/{SOUL.md, AGENTS.md}``（与全局 SOUL/AGENTS/MEMORY/群记忆共存，互不干扰）。
- 首次启动迁移：旧全局 SOUL/AGENTS 拷给 main；其余内建专岗 SOUL 从 MaiBot 同步、
  AGENTS = 岗位预设 + （旧 instructions 非空则追加在「## 原职责」小节）；幂等（第二次不再覆盖）。
- MaiBot 没人格 → SOUL 空（不报错）。
- agent_read / agent_write / agent_sync_soul / agent_reset_agents 的形状与限额；
  未知 kind 一律 KeyError（HTTP 404）。
- 每类 kind 各自独立：写 main 的不影响 news 的。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.identity import Identity
from CharTyr_MaiWork.maiwork.store import Store

GID = "111"
GID_OTHER = "222"


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _settings(cfg: dict | None = None) -> Any:
    raw = cfg or {}
    raw.setdefault("groups", {"serve": [{"group": f"qq:{GID}"}, {"group": f"qq:{GID_OTHER}"}]})
    settings, _ = load_settings(raw)
    return settings


class FakeHost:
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = dict(config)

    async def config(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.migrate()
    return s


def _make(tmp_path: Path, store: Store, *, host: Any = None, cfg: dict | None = None) -> Identity:
    settings = _settings(cfg)
    return Identity(tmp_path / "data", store, lambda: settings, host=host)


# ----------------------------------------------------------------------
# 首次启动：每个内建专岗都有自己的 SOUL / AGENTS
# ----------------------------------------------------------------------


def test_first_start_creates_per_builtin_agent_docs(tmp_path: Path, store: Store) -> None:
    """首次启动：identity/agents/{main,news,idea,goal,task}/SOUL.md + AGENTS.md 都就位。"""
    host = FakeHost({"bot.nickname": "小麻", "personality.personality": "慢热但靠谱"})
    identity = _make(tmp_path, store, host=host)
    _run(identity.ensure_started())

    root = tmp_path / "data" / "identity" / "agents"
    for kind in ("main", "news", "idea", "goal", "task"):
        assert (root / kind / "SOUL.md").is_file(), kind
        assert (root / kind / "AGENTS.md").is_file(), kind
    # main 的 SOUL 应来自 MaiBot（有 host.personality）
    main_soul = (root / "main" / "SOUL.md").read_text(encoding="utf-8")
    assert "慢热但靠谱" in main_soul
    # 其余内建专岗的 SOUL 同样从 MaiBot 同步
    news_soul = (root / "news" / "SOUL.md").read_text(encoding="utf-8")
    assert "慢热但靠谱" in news_soul
    # main 的 AGENTS.md 是 main 预设（包含「新建专岗」提示小节）
    main_agents = (root / "main" / "AGENTS.md").read_text(encoding="utf-8")
    assert "新建专岗" in main_agents or "派给哪个专岗" in main_agents
    # task 的 AGENTS.md 是 task 预设
    task_agents = (root / "task" / "AGENTS.md").read_text(encoding="utf-8")
    assert "通用执行" in task_agents or "群友派的活" in task_agents


def test_first_start_migration_moves_global_to_main(tmp_path: Path, store: Store) -> None:
    """若线上已有旧的全局 SOUL.md / AGENTS.md：内容拷给 main，旧文件保留（不丢数据）。"""
    identity = _make(tmp_path, store)
    root = tmp_path / "data" / "identity"
    root.mkdir(parents=True, exist_ok=True)
    (root / "SOUL.md").write_text("我是旧全局 SOUL，应该归 main。", encoding="utf-8")
    (root / "AGENTS.md").write_text("旧全局规矩：先想再做。", encoding="utf-8")
    _run(identity.ensure_started())

    main_dir = root / "agents" / "main"
    assert "我是旧全局 SOUL" in (main_dir / "SOUL.md").read_text(encoding="utf-8")
    assert "旧全局规矩" in (main_dir / "AGENTS.md").read_text(encoding="utf-8")
    # 旧文件仍在（不删除，不丢数据）
    assert (root / "SOUL.md").is_file()
    assert (root / "AGENTS.md").is_file()


def test_first_start_no_persona_leaves_soul_empty(tmp_path: Path, store: Store) -> None:
    """MaiBot 完全没人格配置：内建专岗 SOUL.md 就位但内容为空（不报错）。"""
    identity = _make(tmp_path, store, host=FakeHost({}))
    _run(identity.ensure_started())
    for kind in ("news", "idea", "goal", "task"):
        soul = identity.agent_read(kind, "soul")["text"]
        assert soul == "" or soul.strip() == ""


def test_migration_is_idempotent(tmp_path: Path, store: Store) -> None:
    """迁移只跑一次：管理员改过 main 的 AGENTS 后再次 ensure_started，不会被预设覆盖。"""
    identity = _make(tmp_path, store)
    _run(identity.ensure_started())
    # 管理员手动改 main 的 AGENTS
    identity.agent_write("main", "agents", "我改过 main 的 AGENTS")
    # 再跑 ensure_started（热重启）
    _run(identity.ensure_started())
    # 仍是管理员改过的版本，没被重置
    assert identity.agent_read("main", "agents")["text"] == "我改过 main 的 AGENTS"


# ----------------------------------------------------------------------
# 读写 / 限额 / 404
# ----------------------------------------------------------------------


def test_agent_read_write_roundtrip_and_isolation(tmp_path: Path, store: Store) -> None:
    """每个 kind 的 SOUL/AGENTS 各自独立：改 main 的不影响 news 的。"""
    identity = _make(tmp_path, store)
    _run(identity.ensure_started())
    identity.agent_write("main", "soul", "main 专属人格")
    identity.agent_write("news", "soul", "news 专属人格")
    identity.agent_write("main", "agents", "main 专属规矩")
    assert identity.agent_read("main", "soul")["text"] == "main 专属人格"
    assert identity.agent_read("news", "soul")["text"] == "news 专属人格"
    assert identity.agent_read("main", "agents")["text"] == "main 专属规矩"
    # news 的 AGENTS 没被动
    assert "main 专属规矩" not in identity.agent_read("news", "agents")["text"]


def test_agent_docs_shape(tmp_path: Path, store: Store) -> None:
    """agent_read_all(kind) 的形状就是前端要的那份：soul/agents/limits 三键。"""
    identity = _make(tmp_path, store)
    _run(identity.ensure_started())
    out = identity.agent_read_all("news")
    assert set(out.keys()) >= {"soul", "agents", "limits"}
    assert isinstance(out["soul"], dict) and "text" in out["soul"] and "updated_ts" in out["soul"]
    assert "synced_from_maibot" in out["soul"]
    assert isinstance(out["agents"], dict) and "text" in out["agents"] and "updated_ts" in out["agents"]
    assert out["limits"] == {"soul": 16384, "agents": 16384}


def test_agent_write_limit_enforced(tmp_path: Path, store: Store) -> None:
    identity = _make(tmp_path, store)
    _run(identity.ensure_started())
    with pytest.raises(ValueError):
        identity.agent_write("news", "soul", "长" * 20000)  # 超 16KB
    with pytest.raises(ValueError):
        identity.agent_write("goal", "agents", "规" * 20000)


def test_agent_unknown_kind_raises_key_error(tmp_path: Path, store: Store) -> None:
    identity = _make(tmp_path, store)
    _run(identity.ensure_started())
    for bad in ("ghost", "c_x", "c_", "MAIN", "", "../evil", "c_xxxx"):
        with pytest.raises(KeyError):
            identity.agent_read_all(bad)
        with pytest.raises(KeyError):
            identity.agent_write(bad, "soul", "x")


# ----------------------------------------------------------------------
# 从 MaiBot 同步 / 恢复默认
# ----------------------------------------------------------------------


def test_agent_sync_soul_saves_bak(tmp_path: Path, store: Store) -> None:
    """点「从 MaiBot 同步」覆盖前把旧版存 identity/agents/<kind>/SOUL.md.bak。"""
    host = FakeHost({"bot.nickname": "小麻", "personality.personality": "新版本人格"})
    identity = _make(tmp_path, store, host=host)
    _run(identity.ensure_started())
    # 管理员先改成自己的版本
    identity.agent_write("news", "soul", "管理员自己写的 news 人格")
    out = _run(identity.agent_sync_soul("news"))
    assert out["synced_from_maibot"] is True
    assert "新版本人格" in out["text"]
    bak = (tmp_path / "data" / "identity" / "agents" / "news" / "SOUL.md.bak")
    assert "管理员自己写的" in bak.read_text(encoding="utf-8")


def test_agent_reset_agents_writes_preset(tmp_path: Path, store: Store) -> None:
    """恢复默认：AGENTS.md 变回 agent_presets/<kind>.md 的内容（不含其他东西）。"""
    identity = _make(tmp_path, store)
    _run(identity.ensure_started())
    # 管理员改坏它
    identity.agent_write("idea", "agents", "我随手改的")
    out = identity.agent_reset_agents("idea")
    preset = (Path(__file__).resolve().parents[1] / "maiwork" / "agent_presets" / "idea.md").read_text(encoding="utf-8")
    assert out["text"].strip() == preset.strip()


# ----------------------------------------------------------------------
# 注入（agent_prompt_block）：主模型用 main 的，子 agent 用它自己 kind 的
# ----------------------------------------------------------------------


def test_agent_prompt_block_per_kind(tmp_path: Path, store: Store) -> None:
    """每个 kind 注入自己的 SOUL/AGENTS；空文件不出块；分块标题固定。"""
    identity = _make(tmp_path, store)
    _run(identity.ensure_started())
    identity.agent_write("main", "soul", "主模型的人格")
    identity.agent_write("news", "soul", "资讯岗的人格")
    identity.agent_write("news", "agents", "资讯岗的规矩")

    soul_main = identity.agent_prompt_block("main", "soul")
    assert soul_main.startswith("## MaiWork 的身份\n") and "主模型的人格" in soul_main
    soul_news = identity.agent_prompt_block("news", "soul")
    assert "资讯岗的人格" in soul_news and "主模型的人格" not in soul_news
    agents_news = identity.agent_prompt_block("news", "agents")
    assert agents_news.startswith("## 做事规矩\n") and "资讯岗的规矩" in agents_news
    # task 的 AGENTS 是预设，内容不是 main 的
    agents_task = identity.agent_prompt_block("task", "agents")
    assert "主模型的人格" not in agents_task


def test_agent_prompt_block_empty_when_doc_empty(tmp_path: Path, store: Store) -> None:
    """SOUL 留空时（MaiBot 没人格）agent_prompt_block("soul") 出空串。"""
    identity = _make(tmp_path, store, host=FakeHost({}))
    _run(identity.ensure_started())
    # 把 idea 的 SOUL 清空
    identity.agent_write("idea", "soul", "")
    assert identity.agent_prompt_block("idea", "soul") == ""
