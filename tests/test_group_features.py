"""按群功能开关（group_features）：资讯 / 构想 / 任务三个模块的独立启停。

产品决定（用户已确认，见 .trellis/tasks/10-18-independent-feature-switches/prd.md）：
- 三个开关按群分别设置，只影响当前群；第一版不做全局总开关。
- 关闭任务连带暂停该群的目标检查 / 提醒 / 目标派生任务。
- 关闭时正在执行的任务允许完成并交付；停止接新活、不启动排队任务。
- 重开后积压任务由管理员手动恢复，不自动继续。

这一份只测**数据模块**的契约：
1. 默认全开：KV 缺失时三个功能都视为开启，且**不写回种子**（避免非服务群误写）。
2. 明确存的 false 永远生效：损坏 / 缺位补默认时绝不把 false 覆盖回 true。
3. 非服务群：set_config 拒绝；读返回全开默认值，零读写。
4. 从开到关记录 disabled_ts（每模块独立）；从关到开清除；同值重复写不重记。
5. 写时原子校验：不认识的字段 / 非布尔值整笔拒绝，一个字段都不落库。
6. 每次变更写事件 features.toggled（哪个群、哪个模块、开还是关、谁改的）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import group_features
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.store import Store

GID = "900000001"
OTHER = "555000"


def _make(tmp_path: Path, *, groups=(GID,)):
    root = tmp_path / "gf"
    root.mkdir(parents=True, exist_ok=True)
    store = Store(root / "t.db")
    store.migrate()
    merged = {
        "environments": {"workspace_root": str(root)},
        "groups": {"serve": [{"group": f"qq:{g}"} for g in groups]},
    }
    settings, _ = load_settings(merged)
    with store.tx() as conn:
        for g in set(groups) | {OTHER}:
            conn.execute(
                "INSERT INTO groups (group_id, session_id, name, token) VALUES (?, ?, '测试群', ?)",
                (g, f"sess-{g}", f"tok{g}"),
            )
    return store, settings


def _kv_raw(store: Store, gid: str):
    row = store.read().execute(
        "SELECT value FROM kv WHERE key=?", (f"group_features.{gid}",)
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["value"])


def _events(store: Store, kind: str) -> list[dict]:
    rows = store.read().execute(
        "SELECT payload FROM events WHERE kind=? ORDER BY id", (kind,)
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


# ---------------------------------------------------------------------------
# 默认与读取
# ---------------------------------------------------------------------------


def test_missing_kv_defaults_all_on_and_does_not_seed(tmp_path):
    store, settings = _make(tmp_path)
    cfg = group_features.get(store, GID, settings)
    assert cfg["news"] is True and cfg["idea"] is True and cfg["task"] is True
    assert group_features.enabled(store, GID, "news", settings) is True
    assert group_features.enabled(store, GID, "idea", settings) is True
    assert group_features.enabled(store, GID, "task", settings) is True
    # 读取不写种子
    assert _kv_raw(store, GID) is None


def test_stored_false_is_never_overridden_by_defaults(tmp_path):
    store, settings = _make(tmp_path)
    group_features.set_config(store, GID, {"news": False}, settings, by="管理员")
    # 手工删除 idea/task 位：缺失位暂停新增，保留有效的 news=false
    raw = _kv_raw(store, GID)
    assert raw is not None
    raw.pop("idea", None)
    raw.pop("task", None)
    with store.tx() as conn:
        store.kv_set(conn, f"group_features.{GID}", raw)
    cfg = group_features.get(store, GID, settings)
    assert cfg["news"] is False
    assert cfg["idea"] is False and cfg["task"] is False


def test_corrupt_kv_value_falls_back_without_reseeding_false(tmp_path):
    store, settings = _make(tmp_path)
    group_features.set_config(store, GID, {"task": False}, settings, by="管理员")
    # 把值整成坏串：读取按全关兜底，但库里那份坏记录不被偷偷重写
    with store.tx() as conn:
        conn.execute(
            "UPDATE kv SET value=? WHERE key=?", ("{not json", f"group_features.{GID}")
        )
    cfg = group_features.get(store, GID, settings)
    assert cfg["news"] is False and cfg["idea"] is False and cfg["task"] is False
    # 坏记录保留原样（不自动重种成全开的新记录覆盖管理员的意图）
    assert store.read().execute(
        "SELECT value FROM kv WHERE key=?", (f"group_features.{GID}",)
    ).fetchone()["value"] == "{not json"


def test_bool_fields_only_accept_real_bool(tmp_path):
    store, settings = _make(tmp_path)
    # 读取路径：字符串 "false"、数字、None 等坏值一律按关闭处理
    with store.tx() as conn:
        store.kv_set(conn, f"group_features.{GID}", {"news": "false", "idea": 1, "task": None})
    cfg = group_features.get(store, GID, settings)
    assert cfg["news"] is False and cfg["idea"] is False and cfg["task"] is False


# ---------------------------------------------------------------------------
# 服务群边界
# ---------------------------------------------------------------------------


def test_unserved_group_read_defaults_write_rejected(tmp_path):
    store, settings = _make(tmp_path)
    cfg = group_features.get(store, "999999", settings)
    assert cfg["news"] is True and cfg["idea"] is True and cfg["task"] is True
    with pytest.raises(ValueError):
        group_features.set_config(store, "999999", {"news": False}, settings, by="管理员")
    assert _kv_raw(store, "999999") is None


def test_per_group_isolation(tmp_path):
    store, settings = _make(tmp_path, groups=(GID, OTHER))
    group_features.set_config(store, GID, {"idea": False}, settings, by="管理员")
    assert group_features.enabled(store, GID, "idea", settings) is False
    assert group_features.enabled(store, OTHER, "idea", settings) is True
    assert group_features.enabled(store, OTHER, "news", settings) is True


# ---------------------------------------------------------------------------
# 写入校验与 disabled_ts
# ---------------------------------------------------------------------------


def test_unknown_field_and_non_bool_rejected_atomically(tmp_path):
    store, settings = _make(tmp_path)
    with pytest.raises(ValueError):
        group_features.set_config(store, GID, {"news": False, "nope": True}, settings, by="管理员")
    with pytest.raises(ValueError):
        group_features.set_config(store, GID, {"news": 0}, settings, by="管理员")
    with pytest.raises(ValueError):
        group_features.set_config(store, GID, {}, settings, by="管理员")
    # 整笔拒绝：一个字段都没落库
    assert _kv_raw(store, GID) is None


def test_disabled_ts_recorded_on_off_and_cleared_on_on(tmp_path):
    store, settings = _make(tmp_path)
    t1 = 1_700_000_000.0
    group_features.set_config(store, GID, {"news": False}, settings, by="管理员", now=t1)
    raw = _kv_raw(store, GID)
    assert raw is not None and raw["news"] is False
    assert raw["disabled_ts"]["news"] == t1

    # 同值重复写不重记
    t2 = t1 + 100
    group_features.set_config(store, GID, {"news": False}, settings, by="管理员", now=t2)
    assert _kv_raw(store, GID)["disabled_ts"]["news"] == t1

    # 重新开启清除该模块的 disabled_ts
    t3 = t2 + 100
    group_features.set_config(store, GID, {"news": True}, settings, by="管理员", now=t3)
    raw = _kv_raw(store, GID)
    assert raw["news"] is True
    assert "news" not in raw.get("disabled_ts", {})


def test_disabled_since_helper(tmp_path):
    store, settings = _make(tmp_path)
    assert group_features.disabled_since(store, GID, "task", settings) is None
    t1 = 1_700_000_000.0
    group_features.set_config(store, GID, {"task": False}, settings, by="管理员", now=t1)
    assert group_features.disabled_since(store, GID, "task", settings) == t1
    # 别的模块不受牵连
    assert group_features.disabled_since(store, GID, "news", settings) is None


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------


def test_toggle_writes_event_with_module_and_actor(tmp_path):
    store, settings = _make(tmp_path)
    group_features.set_config(store, GID, {"task": False}, settings, by="管理员")
    group_features.set_config(store, GID, {"task": True}, settings, by="群管理员（网页）")
    events = _events(store, "features.toggled")
    assert len(events) == 2
    assert events[0]["module"] == "task" and events[0]["enabled"] is False
    assert events[0]["by"] == "管理员" and events[0]["group_id"] == GID
    assert events[1]["module"] == "task" and events[1]["enabled"] is True


def test_same_value_write_does_not_emit_event(tmp_path):
    store, settings = _make(tmp_path)
    group_features.set_config(store, GID, {"news": True}, settings, by="管理员")
    assert _events(store, "features.toggled") == []
