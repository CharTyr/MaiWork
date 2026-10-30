"""名册跟 QQ 对名字（members.refresh_from_host）：问 QQ 群名片 / 昵称，补上久不说话、没说过话的人。"""

from __future__ import annotations

import asyncio

import pytest

from CharTyr_MaiWork.maiwork import members
from CharTyr_MaiWork.maiwork.store import Store

G = "900000001"
NOW = 1_800_000_000.0
DAY = 86400.0
H = 3600.0


class FakeHost:
    def __init__(self, answers: dict | None = None, boom: set | None = None) -> None:
        self.answers = answers or {}
        self.boom = boom or set()
        self.asked: list[tuple[str, str]] = []

    async def group_member_card(self, group_id: str, user_id: str) -> dict:
        self.asked.append((group_id, user_id))
        if user_id in self.boom:
            raise RuntimeError("接口炸了")
        return dict(self.answers.get(user_id, {}))


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "m.db")
    s.migrate()
    return s


def _run(store, host, now=NOW, limit=20) -> int:
    return asyncio.run(members.refresh_from_host(store, host, G, now, limit=limit))


def _checked(store, uid) -> float:
    r = store.read().execute(
        "SELECT checked_ts FROM members WHERE group_id=? AND user_id=?", (G, uid)
    ).fetchone()
    return float(r["checked_ts"]) if r else -1.0


def test_stale_roster_name_refreshed_card_first(tmp_path) -> None:
    s = _store(tmp_path)
    with s.tx() as conn:
        members.record(conn, G, "111", "旧名字", NOW - 3 * DAY)
        members.record(conn, G, "222", "旧名字二", NOW - 3 * DAY)
    host = FakeHost({"111": {"card": "新群名片", "nickname": "昵称一"},
                     "222": {"card": "", "nickname": "只有昵称"}})
    got = _run(s, host)
    assert got == 2
    assert members.name_of(s, G, "111") == "新群名片"
    assert members.name_of(s, G, "222") == "只有昵称"
    assert _checked(s, "111") == NOW


def test_recently_checked_not_asked_again(tmp_path) -> None:
    s = _store(tmp_path)
    with s.tx() as conn:
        members.record(conn, G, "111", "名字", NOW - 3 * DAY)
    host = FakeHost({"111": {"card": "名片"}})
    _run(s, host)
    host2 = FakeHost({"111": {"card": "又改了"}})
    _run(s, host2, now=NOW + 23 * H)
    assert host2.asked == []
    _run(s, host2, now=NOW + 25 * H)
    assert host2.asked == [(G, "111")]


def test_empty_answer_keeps_name_but_marks_checked(tmp_path) -> None:
    s = _store(tmp_path)
    with s.tx() as conn:
        members.record(conn, G, "111", "原名", NOW - 3 * DAY)
    host = FakeHost({})
    assert _run(s, host) == 0
    assert members.name_of(s, G, "111") == "原名"
    assert _checked(s, "111") == NOW
    host2 = FakeHost({})
    _run(s, host2, now=NOW + H)
    assert host2.asked == []


def test_referenced_but_unknown_ids_get_names(tmp_path) -> None:
    s = _store(tmp_path)
    with s.tx() as conn:
        conn.execute(
            "INSERT INTO ideas (group_id, title, created, updated, target_user_id) VALUES (?, '点子', 0, 0, '333')",
            (G,),
        )
        conn.execute(
            "INSERT INTO goals (group_id, kind, title, who_id, created, updated) VALUES (?, 'member', '交稿', '444', 0, 0)",
            (G,),
        )
    host = FakeHost({"333": {"card": "没说过话的人"}, "444": {"nickname": "目标主人"}})
    _run(s, host)
    assert members.name_of(s, G, "333") == "没说过话的人"
    assert members.name_of(s, G, "444") == "目标主人"


def test_focus_member_columns_written_and_not_cleared(tmp_path) -> None:
    s = _store(tmp_path)
    with s.tx() as conn:
        conn.execute("INSERT INTO focus_members (group_id, user_id, name) VALUES (?, '111', '存量')", (G,))
        conn.execute(
            "INSERT INTO focus_members (group_id, user_id, name, card, nickname, profile_ts)"
            " VALUES (?, '222', '存量二', '老名片', '老昵称', 0)",
            (G,),
        )
    host = FakeHost({"111": {"card": "名片", "nickname": "昵称"}})
    _run(s, host)
    rows = {r["user_id"]: r for r in s.read().execute(
        "SELECT user_id, card, nickname, profile_ts FROM focus_members WHERE group_id=?", (G,))}
    assert (rows["111"]["card"], rows["111"]["nickname"], rows["111"]["profile_ts"]) == ("名片", "昵称", NOW)
    assert (rows["222"]["card"], rows["222"]["nickname"], rows["222"]["profile_ts"]) == ("老名片", "老昵称", NOW)
    # 关注成员 6 小时就重查
    host2 = FakeHost({})
    _run(s, host2, now=NOW + 7 * H)
    assert sorted(u for _, u in host2.asked) == ["111", "222"]


def test_limit_and_focus_first(tmp_path) -> None:
    s = _store(tmp_path)
    with s.tx() as conn:
        for i in range(5):
            members.record(conn, G, f"10{i}", f"人{i}", NOW - 3 * DAY)
        conn.execute("INSERT INTO focus_members (group_id, user_id, name) VALUES (?, '999', '关注的')", (G,))
    host = FakeHost({})
    _run(s, host, limit=3)
    assert len(host.asked) == 3
    assert host.asked[0] == (G, "999")


def test_skip_non_numeric_and_id_as_name(tmp_path) -> None:
    s = _store(tmp_path)
    with s.tx() as conn:
        members.record(conn, G, "tg_abc", "电报的人", NOW - 3 * DAY)
        members.record(conn, G, "111", "原名", NOW - 3 * DAY)
    host = FakeHost({"111": {"card": "111", "nickname": ""}})
    _run(s, host)
    assert host.asked == [(G, "111")]
    assert members.name_of(s, G, "111") == "原名"


def test_exception_skips_one_person(tmp_path) -> None:
    s = _store(tmp_path)
    with s.tx() as conn:
        members.record(conn, G, "111", "甲", NOW - 3 * DAY)
        members.record(conn, G, "222", "乙", NOW - 3 * DAY)
    host = FakeHost({"222": {"card": "乙的新名片"}}, boom={"111"})
    assert _run(s, host) == 1
    assert members.name_of(s, G, "111") == "甲"
    assert members.name_of(s, G, "222") == "乙的新名片"


def test_message_ordering_after_refresh(tmp_path) -> None:
    s = _store(tmp_path)
    with s.tx() as conn:
        members.record(conn, G, "111", "旧", NOW - 3 * DAY)
    _run(s, FakeHost({"111": {"card": "QQ上的"}}))
    with s.tx() as conn:
        members.record(conn, G, "111", "更早的消息", NOW - 10)
    assert members.name_of(s, G, "111") == "QQ上的"
    with s.tx() as conn:
        members.record(conn, G, "111", "之后改的名", NOW + 10)
    assert members.name_of(s, G, "111") == "之后改的名"


def test_no_host_is_noop(tmp_path) -> None:
    s = _store(tmp_path)
    with s.tx() as conn:
        members.record(conn, G, "111", "甲", NOW - 3 * DAY)
    assert _run(s, None) == 0
    assert _checked(s, "111") == 0.0


# ----------------------------------------------------------------------
# app 接线：每群 30 分钟最多派一轮（后台长活，不卡主循环）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_app_round_throttled_per_group(tmp_path) -> None:
    from pathlib import Path

    from CharTyr_MaiWork.maiwork.app import MaiWorkApp

    app = MaiWorkApp(ctx=None, raw_config={}, plugin_dir=Path(__file__).resolve().parents[1])
    s = _store(tmp_path)
    with s.tx() as conn:
        members.record(conn, G, "111", "旧名", NOW - 3 * DAY)
    app.store = s
    host = FakeHost({"111": {"card": "新名片"}})
    app.host = host

    app._member_names_round(G, NOW)
    for _ in range(50):
        if (G, "names") not in app._running_jobs:
            break
        await asyncio.sleep(0.02)
    assert members.name_of(s, G, "111") == "新名片"
    # 29 分钟后不再派；31 分钟后才派（这次没人到期，但会派）
    with s.tx() as conn:
        conn.execute("UPDATE members SET checked_ts=0")
    app._member_names_round(G, NOW + 29 * 60)
    await asyncio.sleep(0.05)
    assert len(host.asked) == 1
    app._member_names_round(G, NOW + 31 * 60)
    for _ in range(50):
        if len(host.asked) == 2:
            break
        await asyncio.sleep(0.02)
    assert len(host.asked) == 2


def test_app_round_skips_without_host(tmp_path) -> None:
    from pathlib import Path

    from CharTyr_MaiWork.maiwork.app import MaiWorkApp

    app = MaiWorkApp(ctx=None, raw_config={}, plugin_dir=Path(__file__).resolve().parents[1])
    app.store = _store(tmp_path)
    app.host = None
    app._member_names_round(G, NOW)  # 不抛、不派
    assert (G, "names") not in app._running_jobs
