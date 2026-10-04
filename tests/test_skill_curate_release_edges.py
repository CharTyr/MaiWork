"""最终收口：受保护做法不触发整理；模型失败也保留已归档的审计记录。"""
import asyncio
import pytest
from test_skills_review_fixes import env, G, NOW, _Models, _seed_handoff
from CharTyr_MaiWork.maiwork import lessons


def test_curate_gate_counts_only_unlocked_active(env):
    store, agents = env
    for name in ("已锁甲", "已锁乙", "已锁丙", "已锁丁"):
        sid = agents.skill_add(G, "task", name=name, body="相同做法")
        agents.skill_update(G, sid, locked=True)
    models = _Models()
    asyncio.run(lessons._run_curate(store, models, agents, G, NOW, scrub=None))
    assert models.calls == []


@pytest.mark.parametrize("reply", [RuntimeError("模型失败"), "不是 JSON", {"merge": {"from": [], "into": "新甲", "body": "违规合并"}}])
def test_archive_audit_survives_curate_model_failure(env, reply):
    store, agents = env
    old = agents.skill_add(G, "task", name="老办法", body="先核对旧材料")
    for name in ("新甲", "新乙"):
        sid = agents.skill_add(G, "task", name=name, body="先核对本轮材料")
        with store.tx() as conn:
            conn.execute("UPDATE agent_skills SET created=? WHERE id=?", (NOW - 1, sid))
    with store.tx() as conn:
        conn.execute("UPDATE agent_skills SET created=? WHERE id=?", (NOW - 40 * 86400, old))
    models = _Models([reply])
    changed = asyncio.run(lessons._run_curate(store, models, agents, G, NOW, scrub=None))
    assert changed == 1 and agents.skill_get(G, old)["status"] == "archived"
    events = store.read().execute("SELECT payload FROM events WHERE kind='skills.change' AND group_id=?", (G,)).fetchall()
    assert len(events) == 1, "已落库的自动归档不能因模型失败丢失改动审计"


def test_successful_reflection_writes_real_store_audit(env):
    store, agents = env
    agents.skill_add(G, "news", body="先检查")
    _seed_handoff(store, "news")
    models = _Models([{"patch": [{"old": "先检查", "new": "先检查再核对"}]}])
    changed = asyncio.run(lessons._run_specialist_kind(store, models, agents, G, "news", NOW, scrub=None, recent_topics=None))
    assert changed == 1
    events = store.read().execute("SELECT payload FROM events WHERE kind='skills.change' AND group_id=?", (G,)).fetchall()
    assert len(events) == 1, "真实 Store.event 契约必须接得上复盘改动审计"
