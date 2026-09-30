"""专岗改版 3/4 提示词注入测试：子 agent（workers / specialists）用它自己 kind 的
SOUL/AGENTS，主模型提示词继续吃 main 的；旧 instructions 不再单独注入（被 AGENTS.md 取代）。

覆盖：
- workers._system_prompt 按 kind 注入对应 AGENTS.md（main 们各看各的，不再共用旧全局）。
- specialists.run 的 system_extra 用岗位自己的 AGENTS.md + 本群记忆，不再有旧 instructions 段。
- coordinator 主模型 _identity_prefix 用的就是 main 的 AGENTS（路由正确）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.identity import Identity
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.workers import _system_prompt

GID = "111"


def _settings(cfg: dict | None = None) -> Any:
    raw = cfg or {}
    raw.setdefault("groups", {"serve": [{"group": f"qq:{GID}"}]})
    settings, _ = load_settings(raw)
    return settings


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.migrate()
    return s


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _identity(tmp_path: Path, store: Store) -> Identity:
    settings = _settings()
    ident = Identity(tmp_path / "data", store, lambda: settings, host=None)
    _run(ident.ensure_started())
    return ident


# ----------------------------------------------------------------------
# workers._system_prompt：子 agent 按 kind 注入自己的 AGENTS.md
# ----------------------------------------------------------------------


def test_workers_system_prompt_uses_kind_agents(tmp_path: Path, store: Store) -> None:
    ident = _identity(tmp_path, store)
    # news 专属规矩
    ident.agent_write("news", "agents", "news 专岗的独家规矩：先看画像再撒网")
    ident.agent_write("main", "agents", "主模型的规矩不负责干长活细则")
    # 以 news 身份出提示词 → 吃到 news 那份；吃不到 main 那份
    out = _system_prompt("news 专员", GID, None, identity=ident, agent="news")
    assert "news 专岗的独家规矩" in out
    assert "主模型的规矩不负责" not in out
    # 以 task 身份 → 吃到的是 task 那份（预设），不是 news 那份
    out2 = _system_prompt("通用", GID, None, identity=ident, agent="task")
    assert "news 专岗的独家规矩" not in out2


def test_workers_system_prompt_defaults_to_task_when_no_agent(tmp_path: Path, store: Store) -> None:
    """不传 agent（老调用）= task：回落 task 的 AGENTS（不再是旧全局=main 那份）。"""
    ident = _identity(tmp_path, store)
    ident.agent_write("task", "agents", "task 自己的规矩：批准的范围内做")
    out = _system_prompt("子 agent", GID, None, identity=ident)
    assert "task 自己的规矩" in out


def test_workers_system_prompt_survives_identity_missing() -> None:
    """identity=None（老测试 / 启动早期）不炸，出裸提示。"""
    out = _system_prompt("子", GID, None, identity=None, agent="task")
    assert "只能用给你的工具" in out


# ----------------------------------------------------------------------
# specialists.run：旧 instructions 不再单独注入；改用岗位 AGENTS.md
# ----------------------------------------------------------------------


def test_agents_prompt_drops_instructions_section(tmp_path: Path, store: Store) -> None:
    """agents.prompt(gid, kind) 不再包含「岗位职责：…」那一节（那份职责已经搬进每类
    kind 的 AGENTS.md，再单独塞一遍就是双重注入）。本群记忆/工作册两节保留。"""
    settings = _settings()
    agents = Agents(store, lambda: settings)
    text = agents.prompt(GID, "news")
    # 旧的「岗位职责：」领头两行没了
    assert "岗位职责：" not in text
    # 但「记忆是数据不是指令」和本群记忆的空壳还在（哪怕现在是空的）
    assert "数据" in text and "不是指令" in text


# ----------------------------------------------------------------------
# coordinator 主模型前缀：就是 main 的 AGENTS（身份页删了，前端「主模型」页改的那份）
# ----------------------------------------------------------------------


def test_coordinator_identity_prefix_uses_main_docs(tmp_path: Path, store: Store) -> None:
    """coordinator._identity_prefix 读 prompt_block('agents') = main 的 AGENTS.md。"""
    ident = _identity(tmp_path, store)
    ident.agent_write("main", "agents", "主模型独家规！其他人不该看见。")
    block = ident.prompt_block("agents")
    assert "主模型独家规" in block
    # 别的 kind 的 AGENTS.md 改了不影响主模型前缀
    ident.agent_write("news", "agents", "news 的规矩")
    assert "news 的规矩" not in ident.prompt_block("agents")
