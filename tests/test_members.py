"""members.py：成员名册（按平台 id 认人，名字只用来显示、跟着改名更新）。"""

from __future__ import annotations

from CharTyr_MaiWork.maiwork import members
from CharTyr_MaiWork.maiwork.store import Store

G = "900000001"


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "m.db")
    s.migrate()
    return s


def test_record_and_name_of_latest_wins(tmp_path) -> None:
    s = _store(tmp_path)
    with s.tx() as conn:
        members.record(conn, G, "111", "iBad Bro", 100.0)
        members.record(conn, G, "111", "胡萝卜花之王", 200.0)
    assert members.name_of(s, G, "111") == "胡萝卜花之王"


def test_older_message_does_not_overwrite_newer_name(tmp_path) -> None:
    """补整理会重读旧消息：旧名字不能盖掉新名字。"""
    s = _store(tmp_path)
    with s.tx() as conn:
        members.record(conn, G, "111", "新名字", 200.0)
        members.record(conn, G, "111", "旧名字", 100.0)
    assert members.name_of(s, G, "111") == "新名字"


def test_never_uses_platform_id_as_name(tmp_path) -> None:
    s = _store(tmp_path)
    with s.tx() as conn:
        members.record(conn, G, "111", "111", 100.0)  # 名字回落成了 QQ 号
        members.record(conn, G, "222", "", 100.0)
    assert members.name_of(s, G, "111") == ""
    assert members.name_of(s, G, "222", fallback="222") == ""
    assert members.name_of(s, G, "333", fallback="老快照") == "老快照"


def test_per_group(tmp_path) -> None:
    s = _store(tmp_path)
    with s.tx() as conn:
        members.record(conn, G, "111", "A群的名片", 100.0)
        members.record(conn, "999", "111", "B群的名片", 100.0)
    assert members.name_of(s, G, "111") == "A群的名片"
    assert members.name_of(s, "999", "111") == "B群的名片"
    assert members.names_of(s, G, ["111", "404"]) == {"111": "A群的名片"}


def test_render_tokens_to_current_names(tmp_path) -> None:
    s = _store(tmp_path)
    with s.tx() as conn:
        members.record(conn, G, "100000001", "胡萝卜花之王", 100.0)
    text = f"{members.token('100000001')}的NS2底座发烫；{members.token('123456789')}也说烫"
    out = members.render(s, G, text)
    assert out == f"胡萝卜花之王的NS2底座发烫；{members.UNKNOWN}也说烫"
    assert "100000001" not in out and "123456789" not in out
    assert members.render(s, G, "没有人名") == "没有人名"


def test_legend_for_model_prompt(tmp_path) -> None:
    s = _store(tmp_path)
    with s.tx() as conn:
        members.record(conn, G, "111", "肉肉", 100.0)
    legend = members.legend(s, G, ["x {@111} y", "{@111} {@222}"])
    assert "{@111}=肉肉" in legend
    assert "{@222}" in legend  # 不认识的也列出来，标「不认识」
    assert members.legend(s, G, ["没有人"]) == ""


def test_migration_seeds_from_member_activity(tmp_path) -> None:
    """老库：从 member_activity 取每人最近一天的名字，名册一上线就有名字。"""
    import sqlite3

    path = tmp_path / "old.db"
    s = Store(path)
    s.migrate()
    with s.tx() as conn:
        conn.execute("DELETE FROM members")
        conn.execute(
            "INSERT INTO member_activity (group_id, user_id, day, count, name) VALUES"
            " (?, '111', '2026-09-27', 3, '旧名'), (?, '111', '2026-09-28', 1, '新名'),"
            " (?, '222', '2026-09-28', 1, '222')",
            (G, G, G),
        )
    with s.tx() as conn:
        members.seed_from_activity(conn)
    assert members.name_of(s, G, "111") == "新名"
    assert members.name_of(s, G, "222") == ""


def test_tokenize_bare_ids_written_by_model(tmp_path) -> None:
    """模型没按要求写 {@id}、直接写了 QQ 号：本群认识的 id 一律转成 {@id}，名字括号去重。"""
    s = _store(tmp_path)
    with s.tx() as conn:
        members.record(conn, G, "100000001", "胡萝卜花之王", 100.0)
        conn.execute(
            "INSERT INTO member_activity (group_id, user_id, day, count, name)"
            " VALUES (?, '100000001', '2026-09-20', 1, 'iBad Bro')",
            (G,),
        )
    f = lambda t: members.tokenize_ids(s, G, t)  # noqa: E731
    assert f("iBad Bro (100000001) 底座发烫") == "{@100000001} 底座发烫"
    assert f("胡萝卜花之王(QQ 100000001)说烫") == "{@100000001}说烫"
    assert f("{@100000001}（QQ 100000001）说烫") == "{@100000001}说烫"
    assert f("QQ100000001 说烫") == "{@100000001} 说烫"
    assert f("价格 199 元，编号 12345678") == "价格 199 元，编号 12345678"  # 不认识的数字不动
    assert members.render(s, G, f("iBad Bro (100000001) 底座发烫")) == "胡萝卜花之王 底座发烫"
