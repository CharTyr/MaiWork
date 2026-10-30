"""统一注入 group_context(gid, kind)（docs/17 §八.2）：

- 规矩在前、做法在后；空段不输出。
- kind=main 不出做法段；kind=task 出「本群/<名字>：<description>」清单；
- 专岗出那一岗的做法 body。nullptr agents 不炸。
- 跨群不泄漏：用 G1 的注入看不到 G2 的规矩/做法。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import group_context
from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"
G2 = "123456789"


class _Settings:
    def __init__(self, served=()):
        self.served = set(served)
        self.model_list = ()

    def is_served(self, gid):
        return str(gid) in self.served


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def agents(store):
    return Agents(store, lambda: _Settings(served=(G1, G2)))


def test_both_blocks_rules_first(agents):
    agents.group_rules_set(G1, "只发中文", updated_by="admin")
    agents.skill_add(G1, "news", description="", body="资讯先看画像")
    out = group_context.group_context(agents, G1, "news")
    idx_rules = out.index("【本群规矩")
    idx_skill = out.index("【本群资讯的做法")
    assert idx_rules < idx_skill
    assert "只发中文" in out
    assert "资讯先看画像" in out
    assert "以规矩为准" in out


def test_only_rules_when_skill_empty(agents):
    agents.group_rules_set(G1, "只发中文", updated_by="admin")
    out = group_context.group_context(agents, G1, "news")
    assert "【本群规矩" in out
    assert "本群资讯的做法" not in out  # 空段不出


def test_only_skill_when_rules_empty(agents):
    agents.skill_add(G1, "news", description="", body="正文")
    out = group_context.group_context(agents, G1, "news")
    assert "【本群规矩" not in out
    assert "【本群资讯的做法" in out


def test_empty_when_both_empty(agents):
    assert group_context.group_context(agents, G1, "news") == ""


def test_main_no_skill(agents):
    agents.skill_add(G1, "news", description="", body="正文")
    # main 没 skill；这时只剩规矩
    agents.group_rules_set(G1, "别发广告", updated_by="admin")
    out = group_context.group_context(agents, G1, "main")
    assert "本群规矩" in out
    assert "做法" not in out
    # 无规矩时 main 完全不出
    agents.group_rules_set(G1, "", updated_by="admin")
    assert group_context.group_context(agents, G1, "main") == ""


def test_task_kind_lists_names_and_description(agents):
    agents.skill_add(G1, "task", name="整理报名表", description="把群里报名整理成表", body="步骤：先爬，再合成")
    agents.skill_add(G1, "task", name="周报", description="每周五", body="周报步骤")
    out = group_context.group_context(agents, G1, "task")
    assert "本群/整理报名表" in out and "本群/周报" in out
    assert "把群里报名整理成表" in out
    assert "步骤" not in out  # 主模型只给清单，正文要 read_skill 读


def test_not_leak_across_groups(agents):
    agents.group_rules_set(G1, "G1 的规矩", updated_by="admin")
    agents.group_rules_set(G2, "G2 的规矩", updated_by="admin")
    agents.skill_add(G1, "news", description="", body="G1 做法")
    agents.skill_add(G2, "news", description="", body="G2 做法")
    out1 = group_context.group_context(agents, G1, "news")
    assert "G1 的规矩" in out1 and "G1 做法" in out1
    assert "G2 的规矩" not in out1 and "G2 做法" not in out1


def test_archived_skill_not_shown(agents):
    sid = agents.skill_add(G1, "news", description="", body="已归档")
    agents.skill_update(G1, sid, status="archived")
    out = group_context.group_context(agents, G1, "news")
    assert out == "" or "已归档" not in out


def test_graceful_failures(agents):
    try:
        # 非服务群 → agents 内部 ValueError → 空串，不炸
        out = group_context.group_context(agents, "非服务群", "news")
        assert out == ""
        out = group_context.group_context(None, "非服务群", "news")
        assert out == ""
    except Exception as e:
        pytest.fail(f"不炸原则：{e}")
