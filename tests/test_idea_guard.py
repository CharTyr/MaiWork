"""idea_guard：个人向构想 / 提一嘴的「堆积闸 + 打扰闸」+ personal._insert_idea 的生成闸。

用户 2026-10 定（个人向构想不占群面构想的位子：feeds._unhandled_idea_count 只数
`target_user_id=''`），所以个人向构想会无限堆积、也会反复追着同一个人提。这里定两条减法：

- 生成那一刻（personal._insert_idea）：同一人同时只留 1 条没处理的个人向构想；每群
  7 天新鲜期里最多 3 条；7 天以前的旧行不再占位子（绝不永久锁死）；只读，不改任何老行
  （在等批准 pending 的尤其不碰）。
- 提一嘴（card_push.IdeaMention.scan）：同一人 3 天内只提一次——sent / uncertain，以及
  还没落地的 pending / queued / sending 全算；每群 7 天新鲜期里最多 3 条个人提一嘴在途。

「沉默不是差评」：整套只做减法，不写负面经验 / 不降权。
"""

from __future__ import annotations

import json

import pytest

from CharTyr_MaiWork.maiwork import feeds, idea_guard
from CharTyr_MaiWork.maiwork.store import Store

import test_personal as tp

GID = "111"
OTHER = "222"
UID = "10001"
UID2 = "10002"
UID3 = "10003"
UID4 = "10004"
NOW = 1_800_000_000.0


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "g.db")
    store.migrate()
    with store.tx() as conn:
        for g in (GID, OTHER):
            conn.execute(
                "INSERT INTO groups (group_id, session_id, name, token) VALUES (?, ?, '测试群', ?)",
                (g, f"sess-{g}", f"tok{g}"),
            )
    return store


def _idea(store, uid: str, *, created: float = NOW, state: str = "new", gid: str = GID,
          title: str = "我可以帮你理一份清单", task_id: str | None = None) -> int:
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO ideas (group_id, title, body, basis, state, created, updated,"
            " target_user_id, task_id) VALUES (?, ?, '', '', ?, ?, ?, ?, ?)",
            (gid, title, state, created, created, uid, task_id),
        )
        return int(cur.lastrowid)


def _mention(store, *, idea_id: int, uid: str, status: str = "pending", created: float = NOW,
             sent_ts: float | None = None, gid: str = GID) -> int:
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO idea_mentions (group_id, idea_id, status, at_user, created, due_ts, sent_ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (gid, int(idea_id), status, uid, created, created, sent_ts),
        )
        return int(cur.lastrowid)


def _chat(store, uid: str, text: str, *, ts: float = NOW, gid: str = GID, mid: str = "m1") -> str:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO chat_log (text, group_id, message_id, ts, user_id, user_name)"
            " VALUES (?, ?, ?, ?, ?, '群友')",
            (text, gid, mid, float(ts), uid),
        )
    return mid


# ----------------------------------------------------------------------
# 1. 常量对齐 + 生成闸（个人向构想）
# ----------------------------------------------------------------------


def test_unhandled_states_match_feeds() -> None:
    """「没处理」的口径和群向构想那套一致（新想法 / 想要 / 等批准）。"""
    assert idea_guard.UNHANDLED_IDEA_STATES == feeds._IDEAS_UNHANDLED_STATES


def test_idea_block_one_unhandled_per_member(tmp_path) -> None:
    store = _store(tmp_path)
    _idea(store, UID, state="new")
    assert "一条" in idea_guard.personal_idea_block(store, GID, UID, NOW)
    # wanted / pending 也算没处理
    store2 = _store(tmp_path / "b")
    _idea(store2, UID, state="pending")
    assert idea_guard.personal_idea_block(store2, GID, UID, NOW)
    # 已经开工（有 task_id）的不占位子
    store3 = _store(tmp_path / "c")
    _idea(store3, UID, state="started", task_id="T1")
    assert idea_guard.personal_idea_block(store3, GID, UID, NOW) == ""


def test_idea_block_group_cap_same_group_only(tmp_path) -> None:
    store = _store(tmp_path)
    for i, uid in enumerate((UID, UID2, UID3)):
        _idea(store, uid, created=NOW - 3600, title=f"我可以帮你做第 {i} 份")
    assert "3 条" in idea_guard.personal_idea_block(store, GID, UID4, NOW)
    # 别的群不占这个群的位子
    assert idea_guard.personal_idea_block(store, OTHER, UID4, NOW) == ""
    # 群向构想（target_user_id 为空）不算个人向的位子
    store2 = _store(tmp_path / "b")
    for i in range(5):
        _idea(store2, "", created=NOW - 3600, title=f"群向 {i}")
    assert idea_guard.personal_idea_block(store2, GID, UID, NOW) == ""


def test_idea_block_old_rows_do_not_lock_and_are_untouched(tmp_path) -> None:
    """7 天以前的旧行不占位子（绝不永久锁死），而且一行都不改。"""
    store = _store(tmp_path)
    old = [_idea(store, uid, created=NOW - 8 * 86400.0, state="new")
           for uid in (UID, UID2, UID3)]
    assert idea_guard.personal_idea_block(store, GID, UID4, NOW) == ""
    assert idea_guard.personal_idea_block(store, GID, UID, NOW) == ""
    rows = store.read().execute("SELECT id, state, updated FROM ideas ORDER BY id").fetchall()
    assert [int(r["id"]) for r in rows] == old
    assert all(str(r["state"]) == "new" for r in rows)


def test_idea_block_pending_approval_untouched(tmp_path) -> None:
    """在等批准的 pending 老构想：只读，不改状态腾位子。"""
    store = _store(tmp_path)
    _idea(store, UID, created=NOW - 8 * 86400.0, state="pending")
    assert idea_guard.personal_idea_block(store, GID, UID2, NOW) == ""
    row = store.read().execute("SELECT state FROM ideas").fetchone()
    assert str(row["state"]) == "pending"


# ----------------------------------------------------------------------
# 2. 打扰闸（提一嘴）
# ----------------------------------------------------------------------


@pytest.mark.parametrize("status", ["pending", "queued", "sending", "sent", "uncertain"])
def test_mention_block_cooldown_covers_all_in_flight_and_sent(tmp_path, status: str) -> None:
    """3 天冷却把「还没落地的」和「已提 / 不确定」一起算。"""
    store = _store(tmp_path)
    sent_ts = NOW - 3600 if status in ("sent", "uncertain") else None
    _mention(store, idea_id=1, uid=UID, status=status, created=NOW - 3600, sent_ts=sent_ts)
    assert idea_guard.personal_mention_block(store, GID, UID, NOW) != ""


def test_mention_block_cooldown_uses_created_when_sent_ts_missing(tmp_path) -> None:
    """老行的 sent_ts 可能是空的：按 created 算冷却，绝不当成「没提过」。"""
    store = _store(tmp_path)
    _mention(store, idea_id=1, uid=UID, status="sent", created=NOW - 3600, sent_ts=None)
    assert "3 天" in idea_guard.personal_mention_block(store, GID, UID, NOW)


def test_mention_block_after_cooldown_allows(tmp_path) -> None:
    store = _store(tmp_path)
    _mention(store, idea_id=1, uid=UID, status="sent", created=NOW - 4 * 86400.0,
             sent_ts=NOW - 4 * 86400.0)
    assert idea_guard.personal_mention_block(store, GID, UID, NOW) == ""


def test_mention_block_open_row_within_horizon_even_past_cooldown(tmp_path) -> None:
    """还没落地的那条只要还在 7 天新鲜期里就不重复建（哪怕已经过了 3 天）。"""
    store = _store(tmp_path)
    _mention(store, idea_id=1, uid=UID, status="pending", created=NOW - 5 * 86400.0)
    assert "没落地" in idea_guard.personal_mention_block(store, GID, UID, NOW)


def test_mention_block_group_cap_and_old_rows_do_not_lock(tmp_path) -> None:
    store = _store(tmp_path)
    for i, uid in enumerate((UID, UID2, UID3)):
        _mention(store, idea_id=i + 1, uid=uid, status="pending", created=NOW - 3600)
    assert "3 条" in idea_guard.personal_mention_block(store, GID, UID4, NOW)
    store2 = _store(tmp_path / "b")
    for i, uid in enumerate((UID, UID2, UID3)):
        _mention(store2, idea_id=i + 1, uid=uid, status="pending", created=NOW - 8 * 86400.0)
    assert idea_guard.personal_mention_block(store2, GID, UID4, NOW) == ""


def test_mention_block_group_scoped(tmp_path) -> None:
    store = _store(tmp_path)
    _mention(store, idea_id=1, uid=UID, status="sent", created=NOW - 3600, sent_ts=NOW - 3600)
    assert idea_guard.personal_mention_block(store, GID, UID, NOW)
    assert idea_guard.personal_mention_block(store, OTHER, UID, NOW) == ""


# ----------------------------------------------------------------------
# 3. 聊天依据（只认本人 + 本群 + 新鲜窗口）
# ----------------------------------------------------------------------


def test_target_chat_since_only_exact_author_and_group(tmp_path) -> None:
    store = _store(tmp_path)
    _chat(store, UID, "本人的话", ts=NOW - 10, mid="mine")
    _chat(store, UID2, "别人的话", ts=NOW - 9, mid="other")
    _chat(store, UID, "别的群的话", ts=NOW - 8, gid=OTHER, mid="elsewhere")
    _chat(store, UID, "太老的话", ts=NOW - 100, mid="old")
    rows = idea_guard.target_chat_since(store, GID, UID, NOW - 50, NOW)
    assert [r["message_id"] for r in rows] == ["mine"]
    assert rows[0]["text"] == "本人的话"


def test_target_chat_since_is_bounded_and_newest_first(tmp_path) -> None:
    store = _store(tmp_path)
    for i in range(30):
        _chat(store, UID, f"第 {i} 句", ts=NOW - 30 + i, mid=f"m{i}")
    rows = idea_guard.target_chat_since(store, GID, UID, NOW - 1000, NOW)
    assert len(rows) == idea_guard.PERSONAL_CHAT_LIMIT
    assert rows[0]["message_id"] == "m29"          # 新的在前
    assert rows[-1]["message_id"] == f"m{30 - idea_guard.PERSONAL_CHAT_LIMIT}"


def test_material_fingerprint_counts_only_his_own_material(tmp_path) -> None:
    """材料指纹只看他本人 / 给他的话和活：别人的话、别的群的都不算。"""
    store = _store(tmp_path)
    _chat(store, UID, "他说的", ts=NOW - 10, mid="mine")
    _chat(store, UID2, "别人说的", ts=NOW - 5, mid="other")
    _chat(store, UID, "别的群的", ts=NOW - 3, gid=OTHER, mid="elsewhere")
    fp = idea_guard.material_fingerprint(store, GID, UID)
    assert fp is not None and fp["chat_max_rowid"] > 0
    fp2 = idea_guard.material_fingerprint(store, GID, UID2)
    assert fp2 is not None and fp2["chat_max_rowid"] > fp["chat_max_rowid"]


def test_material_grew_detects_new_chat_task_and_idea(tmp_path) -> None:
    """材料「变多」要认出来：新聊天行（哪怕时间戳更早）、新活、新构想。"""
    store = _store(tmp_path)
    _chat(store, UID, "旧的", ts=NOW - 10, mid="m1")
    base = idea_guard.material_fingerprint(store, GID, UID)
    assert base is not None
    assert idea_guard.material_grew(base, base) is False
    _chat(store, UID, "补读的", ts=NOW - 20, mid="m2")          # 时间戳更早，也是「变多」
    grown = idea_guard.material_fingerprint(store, GID, UID)
    assert idea_guard.material_grew(base, grown) is True
    assert idea_guard.material_grew(grown, base) is False       # 变少不算「多了」
    # 新任务 / 新构想
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tasks (id, group_id, workspace, requester_id, title, status, created,"
            " updated) VALUES ('T1', ?, 'w', ?, '活', 'queued', ?, ?)", (GID, UID, NOW, NOW))
    assert idea_guard.material_grew(base, idea_guard.material_fingerprint(store, GID, UID)) is True
    _idea(store, UID)
    assert idea_guard.material_grew(base, idea_guard.material_fingerprint(store, GID, UID)) is True
    # 形状不对 / 拿不到 → 失败关闭
    assert idea_guard.material_grew(None, base) is True
    assert idea_guard.material_grew(base, {}) is True
    assert idea_guard.material_grew({}, base) is True


def test_evidence_ok_requires_target_and_fresh_window(tmp_path) -> None:

    store = _store(tmp_path)
    _chat(store, UID, "帮我弄一下", ts=NOW - 10, mid="mine")
    _chat(store, UID2, "别人说的", ts=NOW - 9, mid="other")
    _chat(store, UID, "太老", ts=NOW - 100, mid="old")
    assert idea_guard.evidence_ok(store, GID, UID, [{"message_id": "mine"}],
                                  anchor=NOW - 50, now=NOW) is True
    assert idea_guard.evidence_ok(store, GID, UID, [{"message_id": "other"}],
                                  anchor=NOW - 50, now=NOW) is False
    assert idea_guard.evidence_ok(store, GID, UID, [{"message_id": "old"}],
                                  anchor=NOW - 50, now=NOW) is False
    assert idea_guard.evidence_ok(store, GID, UID,
                                  [{"message_id": "mine"}, {"message_id": "other"}],
                                  anchor=NOW - 50, now=NOW) is False
    assert idea_guard.evidence_ok(store, GID, UID, [], anchor=NOW - 50, now=NOW) is True


def test_last_sent_and_mentioned_since(tmp_path) -> None:
    store = _store(tmp_path)
    _mention(store, idea_id=1, uid=UID, status="sent", created=NOW - 100, sent_ts=NOW - 90)
    _mention(store, idea_id=2, uid=UID, status="uncertain", created=NOW - 80, sent_ts=NOW - 70)
    _mention(store, idea_id=3, uid=UID, status="pending", created=NOW - 60)
    assert idea_guard.last_sent_mention_ts(store, GID, UID) == NOW - 70
    assert idea_guard.mentioned_since(store, GID, UID, NOW - 75) is True
    assert idea_guard.mentioned_since(store, GID, UID, NOW - 60) is False
    assert idea_guard.last_sent_mention_ts(store, GID, UID2) == 0.0


# ----------------------------------------------------------------------
# 4. 生成那一刻的闸（personal._insert_idea，走真 prepare_personal）
# ----------------------------------------------------------------------


def _idea_reply(title: str) -> str:
    return json.dumps({
        "focus": [{"query": "FPGA", "why": "在做"}],
        "idea": {"title": title, "body": "我可以帮你把这板子的例程整理成一页",
                 "step": "先列出要跑的例程", "effort": "半天",
                 "feasibility": tp._FEAS_OK},
    }, ensure_ascii=False)


def _prep_models(models_cls, title: str):
    return models_cls(ready=True, replies=[
        _idea_reply(title), tp._personal_scores_json(),
        json.dumps({"posts": []}, ensure_ascii=False),
    ])


def _seed_unhandled_idea(store, uid: str, *, created: float, title: str = "我可以帮你旧点子") -> int:
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO ideas (group_id, title, body, basis, state, created, updated, target_user_id)"
            " VALUES (?, ?, '旧点子', '', 'new', ?, ?, ?)",
            (tp.GID, title, created, created, uid),
        )
        return int(cur.lastrowid)


def test_generation_gate_blocks_second_unhandled_idea_for_same_member(tmp_path) -> None:
    store, settings, personal, models, workers, topics, _ = tp._make_personal(tmp_path)
    _seed_unhandled_idea(store, tp.UID, created=tp.NOW - 3600)
    personal._models = _prep_models(type(models), "我可以帮你把新的例程跑一遍")
    with tp._time_patch():
        tp._run(personal.prepare_personal(tp.GID, tp.UID))
    rows = store.read().execute(
        "SELECT title FROM ideas WHERE target_user_id=? ORDER BY id", (tp.UID,)
    ).fetchall()
    assert [str(r["title"]) for r in rows] == ["我可以帮你旧点子"]


def test_generation_gate_allows_new_idea_after_old_one_ages_out(tmp_path) -> None:
    """7 天以前的旧行不再锁死：新的个人向构照常出。"""
    store, settings, personal, models, workers, topics, _ = tp._make_personal(tmp_path)
    _seed_unhandled_idea(store, tp.UID, created=tp.NOW - 8 * 86400.0)
    personal._models = _prep_models(type(models), "我可以帮你把新的例程跑一遍")
    with tp._time_patch():
        tp._run(personal.prepare_personal(tp.GID, tp.UID))
    rows = store.read().execute(
        "SELECT title FROM ideas WHERE target_user_id=? ORDER BY id", (tp.UID,)
    ).fetchall()
    assert [str(r["title"]) for r in rows] == ["我可以帮你旧点子", "我可以帮你把新的例程跑一遍"]


def test_generation_gate_group_cap_is_narrow(tmp_path) -> None:
    """每群 7 天最多 3 条个人向构想；到顶只挡构想，个人向资讯照出。"""
    store, settings, personal, models, workers, topics, _ = tp._make_personal(tmp_path)
    for i, uid in enumerate(("90001", "90002", "90003")):
        _seed_unhandled_idea(store, uid, created=tp.NOW - 3600, title=f"我可以帮你第 {i} 份")
    personal._models = _prep_models(type(models), "我可以帮你把新的例程跑一遍")
    with tp._time_patch():
        got = tp._run(personal.prepare_personal(tp.GID, tp.UID))
    assert got > 0     # 个人向资讯照出
    assert store.read().execute(
        "SELECT COUNT(*) c FROM ideas WHERE target_user_id=?", (tp.UID,)
    ).fetchone()["c"] == 0
