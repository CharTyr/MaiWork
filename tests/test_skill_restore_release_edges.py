"""0.8.0 收口：本群做法「回退到某一版」的事务边界（docs/17 §七.2 / §216 的回退契约）。

复核发现（`Agents.skill_restore_version`）：
- 先在**事务外**读 `skill_get`（当前行）和目标版本行，事务里直接拿这份**旧快照**：
  1. 存「回退前那一版」用的是旧快照的 body / description → 两个管理员并发（或异步间隙里
     管理员改了正文）时，**实际被回退掉的那一版丢了**，版本轨迹里记的是更早的旧快照，
     事后按版本找不回真正被回退掉的正文；
  2. 目标版本行也是事务外读的 → 这一版在间隙里被裁掉（每份只留 20 版）/ 这份 skill 被删，
     照样往下写：UPDATE 影响 0 行也不查 rowcount → 留下一版**孤儿 rollback 版本行**
     （skill 都没了），正文白改。
- 事务内不重读、不查 rowcount、UPDATE 的 WHERE 也不带 group_id / kind。

竞态注入点：`_inject_after_version_read` 包装 `Store.read()`，在**目标版本行读到之后、
回退写事务之前**执行一次真实的并发写（这正是缺陷窗口：事务外读完 → 事务开始）。

要求（不新增 schema，文档里的 `source` / `note` 契约照旧）：
- 管理员回退是**正常操作**：锁定的 / 归档的照样能回退（不许收紧成拒绝正常 rollback），
  回退也不改 locked / status；
- 事务内按**真正当前行**存「回退前那一版」；目标版本也以事务内读到的为准；
- 目标版本必须属于本群本 skill（同群同 skill）；非服务群在第一条 SQL 之前就拒（零 SQL）；
- 目标版本已被裁掉 / 这份 skill 已被删 → KeyError、零写入、不留任何版本行。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
if str(PLUGIN.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN.parent))

from test_skills_review_fixes import (  # noqa: E402
    G,
    G2,
    env,  # noqa: F401  (fixture)
    _Models,  # noqa: F401  (保住共用测试装置，防被误当作死代码删掉)
    _seed_handoff,  # noqa: F401
    _state,
)


def _vrows(store, sid: int) -> list[dict]:
    rows = store.read().execute(
        "SELECT id, body, description, source, note FROM agent_skill_versions"
        " WHERE skill_id=? ORDER BY ts DESC, id DESC",
        (int(sid),),
    ).fetchall()
    return [dict(r) for r in rows]


def _vcount(store, sid: int) -> int:
    row = store.read().execute(
        "SELECT COUNT(*) AS c FROM agent_skill_versions WHERE skill_id=?", (int(sid),)
    ).fetchone()
    return int(row["c"])


def _inject_after_version_read(store, monkeypatch, mutator):
    """在「目标版本行读到之后、回退写事务之前」插一次真实并发写。

    缺陷窗口是「事务外读完当前行 + 目标版本行 → 事务才写」。这里包一层 `Store.read()`：
    只在读到 `agent_skill_versions` 的那条 SELECT 的 `fetchone()` 之后触发一次 mutator
    （mutator 自己走 `store.tx()`，此时没有开着的事务，等于另一路管理员已提交）。
    """
    fired = {"done": False}

    class _Cur:
        def __init__(self, cur):
            self._cur = cur

        def fetchone(self):
            row = self._cur.fetchone()
            if not fired["done"]:
                fired["done"] = True
                mutator()
            return row

        def __getattr__(self, name):
            return getattr(self._cur, name)

    class _Conn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            cur = self._conn.execute(sql, params)
            text = str(sql).lstrip()
            if text.upper().startswith("SELECT") and "FROM agent_skill_versions" in text:
                return _Cur(cur)
            return cur

        def __getattr__(self, name):
            return getattr(self._conn, name)

    real_read = store.read
    monkeypatch.setattr(store, "read", lambda: _Conn(real_read()))
    return fired


def _initial_vid(agents, sid: int, *, kind: str = "news", gid: str = G) -> int:
    rows = agents.skill_versions(gid, kind, sid)
    mig = [v for v in rows if v["source"] == "migrate"]
    assert mig, "初始版本必须可溯源（skill_add 写 source=migrate）"
    return int(mig[0]["id"])


# ----------------------------------------------------------------------
# 1) 事务内按「真正当前行」存回退前那一版（版本轨迹可溯源）
# ----------------------------------------------------------------------


def test_restore_saves_the_body_right_before_rollback(env, monkeypatch):
    """两个管理员在间隙里改正文：版本轨迹必须留下**实际被回退掉的那一版**。"""
    store, agents = env
    sid = agents.skill_add(G, "news", description="", body="初版", source="migrate", note="迁移")
    agents.skill_update(G, sid, body="第二版", source="admin", note="管理员改")
    vid = _initial_vid(agents, sid)

    fired = _inject_after_version_read(
        store, monkeypatch,
        # 回退读完旧快照之后、写事务之前，另一路管理员又改了正文
        lambda: agents.skill_update(G, sid, body="管理员间隙里写的第三版",
                                    source="admin", note="间隙"),
    )
    row = agents.skill_restore_version(G, "news", sid, vid)
    assert fired["done"] is True
    assert row["body"] == "初版"

    bodies = [v["body"] for v in _vrows(store, sid)]
    assert "管理员间隙里写的第三版" in bodies, (
        "回退前那一版（间隙里管理员刚写的正文）必须留在版本轨迹里，不能记成旧快照"
    )
    latest = _vrows(store, sid)[0]
    assert latest["source"] == "rollback"
    assert latest["body"] == "管理员间隙里写的第三版"
    assert "初版" in bodies  # 回退不销毁历史


def test_restore_keeps_initial_version_traceable(env):
    """正常回退：初版可溯源、回退前那版可溯源、正文回到目标版本。"""
    store, agents = env
    sid = agents.skill_add(G, "news", description="d0", body="初版", source="migrate", note="迁移")
    agents.skill_update(G, sid, body="第二版", source="admin", note="管理员改")
    vid = _initial_vid(agents, sid)
    agents.skill_restore_version(G, "news", sid, vid)
    assert agents.skill_get(G, sid)["body"] == "初版"
    sources = {v["source"] for v in _vrows(store, sid)}
    assert {"migrate", "rollback"} <= sources
    assert any(v["source"] == "rollback" and v["body"] == "第二版" for v in _vrows(store, sid))


def test_rollback_source_and_note_recorded_no_new_schema(env):
    """文档里的 `source='rollback'` + 一句 note 有效；版本表不新增列。"""
    store, agents = env
    sid = agents.skill_add(G, "news", description="d0", body="初版", source="migrate", note="迁移")
    agents.skill_update(G, sid, body="第二版", source="admin")
    vid = _initial_vid(agents, sid)
    agents.skill_restore_version(G, "news", sid, vid)
    latest = _vrows(store, sid)[0]
    assert latest["source"] == "rollback"
    assert latest["note"].strip(), "回退版本要留一句说明（网页历史里显示改了什么）"
    cols = {r["name"] for r in store.read().execute("PRAGMA table_info(agent_skill_versions)")}
    assert cols == {"id", "skill_id", "body", "description", "source", "note", "ts"}


# ----------------------------------------------------------------------
# 2) 目标版本被裁掉 / 这份 skill 被删：零写入、不留版本行
# ----------------------------------------------------------------------


def test_restore_pruned_target_version_is_zero_write(env, monkeypatch):
    """目标版本在间隙里被裁掉（20 版上限）→ 整次拒绝，不留 rollback 版本行。"""
    store, agents = env
    sid = agents.skill_add(G, "news", description="", body="初版", source="migrate", note="迁移")
    agents.skill_update(G, sid, body="第二版", source="admin")
    vid = _initial_vid(agents, sid)
    before = _vcount(store, sid)

    def prune():
        with store.tx() as conn:
            conn.execute("DELETE FROM agent_skill_versions WHERE id=?", (int(vid),))

    _inject_after_version_read(store, monkeypatch, prune)
    with pytest.raises(KeyError):
        agents.skill_restore_version(G, "news", sid, vid)
    assert agents.skill_get(G, sid)["body"] == "第二版"       # 正文没被白改
    assert _vcount(store, sid) == before - 1                  # 只剩被裁前就有的那版
    assert not any(v["source"] == "rollback" for v in _vrows(store, sid))


def test_restore_deleted_skill_leaves_no_orphan_version(env, monkeypatch):
    """这份 skill 在间隙里被删掉 → 不回写、不留孤儿版本行。"""
    store, agents = env
    sid = agents.skill_add(G, "news", description="", body="初版", source="migrate", note="迁移")
    agents.skill_update(G, sid, body="第二版", source="admin")
    vid = _initial_vid(agents, sid)

    _inject_after_version_read(store, monkeypatch,
                               lambda: agents.skill_delete(G, "news", sid))
    with pytest.raises(KeyError):
        agents.skill_restore_version(G, "news", sid, vid)
    assert _vcount(store, sid) == 0, "skill 都没了，不能留孤儿 rollback 版本行"


# ----------------------------------------------------------------------
# 3) 管理员回退是正常操作：锁定 / 归档的照样回退，且不改 locked / status
# ----------------------------------------------------------------------


def test_restore_is_allowed_on_locked_and_archived(env):
    store, agents = env
    sid = agents.skill_add(G, "news", description="", body="初版")
    agents.skill_update(G, sid, body="第二版")
    vid = int(_vrows(store, sid)[0]["id"])          # 最新那版正文 = 初版
    assert _vrows(store, sid)[0]["body"] == "初版"

    agents.skill_update(G, sid, locked=True)
    row = agents.skill_restore_version(G, "news", sid, vid)
    assert row["body"] == "初版"
    assert row["locked"] is True, "回退不许顺手把管理员的锁定解掉"

    agents.skill_update(G, sid, status="archived")
    vid2 = [v["id"] for v in _vrows(store, sid) if v["body"] == "第二版"][0]
    row = agents.skill_restore_version(G, "news", sid, vid2)
    assert row["body"] == "第二版"
    assert row["status"] == "archived", "回退不许顺手把归档的恢复成 active"
    assert row["locked"] is True


# ----------------------------------------------------------------------
# 4) 目标版本：同群同 skill；非服务群在任何 SQL 之前就拒
# ----------------------------------------------------------------------


def test_stranger_group_refused_before_any_sql(env, monkeypatch):
    store, agents = env
    sid = agents.skill_add(G, "news", description="", body="初版")
    vid = int(_vrows(store, sid)[0]["id"])
    calls = {"n": 0}
    orig_read = store.read

    def counting_read():
        calls["n"] += 1
        return orig_read()

    monkeypatch.setattr(store, "read", counting_read)
    with pytest.raises(ValueError):
        agents.skill_restore_version("999999", "news", sid, vid)
    assert calls["n"] == 0, "非服务群必须在任何 SQL 之前拒绝（零读取）"


def test_version_of_other_skill_same_group_is_zero_write(env):
    store, agents = env
    a = agents.skill_add(G, "news", description="", body="资讯初版")
    b = agents.skill_add(G, "idea", description="", body="构想初版")
    vid_b = int(_vrows(store, b)[0]["id"])
    before_a = _vcount(store, a)
    with pytest.raises(KeyError):
        agents.skill_restore_version(G, "news", a, vid_b)
    assert agents.skill_get(G, a)["body"] == "资讯初版"
    assert _vcount(store, a) == before_a
    assert agents.skill_get(G, b)["body"] == "构想初版"


def test_version_of_other_group_is_zero_write(env):
    store, agents = env
    a = agents.skill_add(G, "news", description="", body="A群初版")
    other = agents.skill_add(G2, "news", description="", body="B群初版")
    vid_other = int(_vrows(store, other)[0]["id"])
    before_a = _vcount(store, a)
    with pytest.raises(KeyError):
        agents.skill_restore_version(G, "news", a, vid_other)
    assert agents.skill_get(G, a)["body"] == "A群初版"
    assert _vcount(store, a) == before_a
    assert agents.skill_get(G2, other)["body"] == "B群初版"


def test_restore_wrong_kind_is_zero_write(env):
    store, agents = env
    sid = agents.skill_add(G, "news", description="", body="资讯初版")
    vid = int(_vrows(store, sid)[0]["id"])
    before = _vcount(store, sid)
    with pytest.raises(KeyError):
        agents.skill_restore_version(G, "idea", sid, vid)
    assert agents.skill_get(G, sid)["body"] == "资讯初版"
    assert _vcount(store, sid) == before


def test_restore_does_not_touch_lessons_state(env):
    """回退只动 skill / 版本，不写 lessons 状态 kv，也不动 updated。"""
    store, agents = env
    sid = agents.skill_add(G, "news", description="", body="初版")
    agents.skill_update(G, sid, body="第二版")
    vid = int(_vrows(store, sid)[0]["id"])
    agents.skill_restore_version(G, "news", sid, vid)
    assert _state(store, "news") == {}
    assert agents.skill_get(G, sid)["updated"] > 0
