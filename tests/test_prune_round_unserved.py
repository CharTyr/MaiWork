"""A3：每日 _prune_round 按时间清 chat_log(+ids) / focus_messages / bot_messages。

退出服务的群（不再出现在 serve 列表）照样按时间清——只按 ts 删、不读内容、
不按群过滤；未过期的不动；chat_log 与去重账本 chat_log_ids 保持一致。
服务群行为不变：本来就有 prune_old / tick 里的按群清理，这里是兜住「群退了之后
按群清理再也不跑」的洞。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fakes import FakeCtx

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"     # 服务群
G_OUT = "935900283"  # 已退出服务的群（线上真实的那个）


def _app(tmp_path: Path) -> MaiWorkApp:
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}"}]},
        "storage": {"data_dir": str(tmp_path / "data")},
    }
    app = MaiWorkApp(FakeCtx({}), raw, plugin_dir=Path(__file__).resolve().parents[1])
    data_dir = Path(raw["storage"]["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    app._settings = load_settings(raw)[0]  # noqa: SLF001  （_prune_round 里 get_settings 要用）
    app.store = Store(data_dir / "maiwork.db")
    app.store.migrate()
    return app


def _seed(store: Store, gid: str, now: float) -> None:
    old = now - 30 * 86400.0    # 三张表全过期
    mid = now - 10 * 86400.0    # bot_messages(7 天)过期；focus_messages / chat_log(14 天)还没
    fresh = now - 86400.0       # 全保留
    with store.tx() as conn:
        for tag, ts in (("old", old), ("mid", mid), ("fresh", fresh)):
            conn.execute(
                "INSERT INTO chat_log (text, group_id, message_id, ts, user_id, user_name)"
                " VALUES (?, ?, ?, ?, 'u1', '阿一')",
                (f"{gid}-{tag}", gid, f"{gid}-m-{tag}", ts),
            )
            conn.execute(
                "INSERT INTO chat_log_ids (group_id, message_id, ts) VALUES (?, ?, ?)",
                (gid, f"{gid}-m-{tag}", ts),
            )
            conn.execute(
                "INSERT INTO focus_messages (group_id, user_id, ts, message_id, text)"
                " VALUES (?, 'u1', ?, ?, ?)",
                (gid, ts, f"{gid}-f-{tag}", tag),
            )
            conn.execute(
                "INSERT INTO bot_messages (group_id, message_id, ts) VALUES (?, ?, ?)",
                (gid, f"{gid}-b-{tag}", ts),
            )


def test_prune_round_clears_expired_rows_for_unserved_groups_too(tmp_path: Path) -> None:
    app = _app(tmp_path)
    now = clock.now()
    _seed(app.store, G1, now)
    _seed(app.store, G_OUT, now)

    app._prune_round(now)  # noqa: SLF001

    store = app.store
    chat = {r["message_id"] for r in store.read().execute("SELECT message_id FROM chat_log")}
    ids = {r["message_id"] for r in store.read().execute("SELECT message_id FROM chat_log_ids")}
    focus = {r["message_id"] for r in store.read().execute("SELECT message_id FROM focus_messages")}
    bots = {r["message_id"] for r in store.read().execute("SELECT message_id FROM bot_messages")}

    for gid in (G1, G_OUT):
        # 30 天前：三张表全清（退出服务的群一样清）
        assert f"{gid}-m-old" not in chat
        assert f"{gid}-f-old" not in focus
        assert f"{gid}-b-old" not in bots
        # 10 天前：bot_messages（7 天）清，focus_messages / chat_log（14 天）留
        assert f"{gid}-b-mid" not in bots
        assert f"{gid}-f-mid" in focus
        assert f"{gid}-m-mid" in chat
        # 昨天：全留（未过期的不动）
        assert f"{gid}-m-fresh" in chat
        assert f"{gid}-f-fresh" in focus
        assert f"{gid}-b-fresh" in bots
    # chat_log 和去重账本一致
    assert chat == ids
