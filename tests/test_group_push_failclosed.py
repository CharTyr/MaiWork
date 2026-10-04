"""每群「往群里发」设置的**失败关闭**（fail-closed）契约（2026-10 复审）。

`group_push.py` 是「开话题 / 资讯卡片 / 构想提一嘴 / 每日总上限 / 睡觉时段」的
唯一真源（kv `group_push.<群号>`）。复审发现四处**失败开放**，任何一种都会让
「往群里自动发」在异常时**被打开或恢复**，必须在异常时收紧：

1. `_normalize` 用 `bool(raw)` 归一三个开关：`bool("false") is True`，一条坏串
   就把「冷场自动开话题」从关打开。修：读取路径只认真 bool，坏值（"false"、1、
   None、[]）一律取**更安全**的一侧（关）。
2. `get_config` 只要 `kv_get` 不是 dict 就去 seed：键**存在但坏掉 / 是 null / 不是
   对象**时，会拿 settings 里那份旧全局种子（`topics.enabled` 默认 True）把管理员
   在网页上关掉的配置**重新打开**，还会把旧 `cardpush.<群号>` 删掉。修：只有
   **明确缺失**才 seed；存在但读不出可信的一份 → 保守默认，不写、不删旧源。
3. `_seed` 在事务里**不二次读**：并发的另一个 `set_config`（或另一路启动迁移）刚
   存下的新配置会被 seed 覆盖回去。修：事务里二次读，已有记录就返回它、不覆盖、
   不删旧键；读失败 / 写失败一律返回**保守默认**（绝不把「种出来的值」当结果返回，
   那等于没落库也当成配置生效）。
4. 生产路径读设置失败时，`Pushes._config` / `CardPush._cfg` / `IdeaMention._cfg`
   原来传 `settings=None`，正好落进「legacy 纯数据层」分支 → 照样 seed、照样按
   `topics.enabled=True` 兜底。修：生产路径一律真 settings，拿不到就传哨兵
   `UNAVAILABLE` → 零读零写 + 保守默认（开关全关、额度有限、默认睡觉时段）。

另外：退役字段（每类每日上限那五个）原来「收下但静默无效」，网页会以为保存成功。
现在**所有调用口**（含显式 legacy `settings=None` 的老壳 / 老桥）都明确拒绝并提示去群页，
一个字段都不落库——不留任何「假保存」。

这些用例先写先红。服务群移除、平台不同、settings 为 None 都不改变结论。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from CharTyr_MaiWork.maiwork import group_push
from CharTyr_MaiWork.maiwork.card_push import CardPush, IdeaMention
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
from CharTyr_MaiWork.maiwork.outbox import Outbox
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.topics import Topics

GID = "900000001"
OTHER = "123456789"
KEY = group_push.KV_PREFIX + GID
LEGACY = f"{group_push.LEGACY_KV_PREFIX}{GID}"

BJ = timezone(timedelta(hours=8))
NOON = datetime(2026, 10, 15, 12, 0, tzinfo=BJ).timestamp()


# ======================================================================
# 脚手架
# ======================================================================


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "failclosed.db")
    store.migrate()
    return store


class SpyStore:
    """包一层真 Store：数「有没有碰库」并按需注入读 / 写失败。"""

    def __init__(self, inner: Store) -> None:
        self._inner = inner
        self.kv_get_calls: list[str] = []
        self.kv_set_calls: list[str] = []
        self.kv_delete_calls: list[str] = []
        self.tx_count = 0
        self.kv_get_error: Exception | None = None
        self.tx_read_error = False
        self.kv_set_error: Exception | None = None

    def kv_get(self, key: str, default: Any = None) -> Any:
        self.kv_get_calls.append(str(key))
        if self.kv_get_error is not None:
            raise self.kv_get_error
        return self._inner.kv_get(key, default)

    def kv_set(self, conn: Any, key: str, value: Any) -> None:
        self.kv_set_calls.append(str(key))
        if self.kv_set_error is not None:
            raise self.kv_set_error
        return self._inner.kv_set(conn, key, value)

    def kv_delete(self, conn: Any, key: str) -> None:
        self.kv_delete_calls.append(str(key))
        return self._inner.kv_delete(conn, key)

    @contextmanager
    def tx(self):
        self.tx_count += 1
        with self._inner.tx() as conn:
            yield _TxConn(conn) if self.tx_read_error else conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _TxConn:
    """事务内连接代理：让「SELECT value FROM kv」那一句炸掉（模拟二次读失败）。"""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, sql: str, *args: Any) -> Any:
        if "FROM kv" in sql:
            raise RuntimeError("事务里二次读失败")
        return self._conn.execute(sql, *args)


class LyingStore(SpyStore):
    """外层 kv_get 假装「没有这条」，但库里其实已经有并发刚存下的新记录。"""

    def __init__(self, inner: Store, key: str) -> None:
        super().__init__(inner)
        self._key = str(key)

    def kv_get(self, key: str, default: Any = None) -> Any:
        self.kv_get_calls.append(str(key))
        if str(key) == self._key:
            return default
        return self._inner.kv_get(key, default)


def _settings(*, serve=(GID,), cfg: dict | None = None):
    merged: dict = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{g}"} for g in serve]},
    }
    if cfg:
        merged.update(cfg)
    settings, problems = load_settings(merged)
    assert not problems, problems
    return settings


def _put_raw(store: Store, key: str, value: Any) -> None:
    with store.tx() as conn:
        store.kv_set(conn, key, value)


def _put_text(store: Store, key: str, text: str) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO kv (key, value, updated) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, text, 0.0),
        )


def _raw_text(store: Store, key: str) -> Any:
    row = store.read().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return None if row is None else row["value"]


def _boom_settings() -> Any:
    raise RuntimeError("读配置炸了")


CONSERVATIVE = {
    "topics_enabled": False,
    "news_card_enabled": False,
    "idea_mention_enabled": False,
    "daily_max": group_push.DEFAULT_DAILY_MAX,
    "quiet_hours": group_push.DEFAULT_QUIET_HOURS,
}


def _assert_conservative(cfg: dict) -> None:
    for key, want in CONSERVATIVE.items():
        assert cfg[key] == want, (key, cfg[key], want)


# ======================================================================
# 1. 读路径：只有明确缺失才 seed；存在但坏 / 读失败 → 保守、不写
# ======================================================================


class TestReadFailClosed:
    def test_missing_key_still_seeds_from_settings_once(self, tmp_path: Path) -> None:
        """没坏、没读错时照旧惰性 seed（不要矫枉过正），原值 / 总上限 3 不动。"""
        inner = _store(tmp_path)
        settings = _settings(cfg={"topics": {"enabled": True},
                                  "delivery": {"push_per_day": 3, "quiet_hours": "22:00-07:30"}})
        cfg = group_push.get_config(inner, GID, settings)
        assert cfg["topics_enabled"] is True
        assert cfg["daily_max"] == 3
        assert cfg["quiet_hours"] == "22:00-07:30"
        raw = inner.kv_get(KEY, None)
        assert isinstance(raw, dict) and raw["daily_max"] == 3

    def test_corrupt_json_record_not_seeded_and_keeps_legacy(self, tmp_path: Path) -> None:
        inner = _store(tmp_path)
        _put_text(inner, KEY, "{这不是 json")
        _put_raw(inner, LEGACY, {"news_card_enabled": True, "news_card_count": 2})
        before = _raw_text(inner, KEY)
        store = SpyStore(inner)
        cfg = group_push.get_config(store, GID, _settings())
        _assert_conservative(cfg)
        assert store.kv_set_calls == [], "坏记录不许被 seed 覆盖"
        assert store.kv_delete_calls == [], "坏记录不许删旧来源"
        assert _raw_text(inner, KEY) == before
        assert inner.kv_get(LEGACY, None) == {"news_card_enabled": True, "news_card_count": 2}

    @pytest.mark.parametrize("bad", [[1, 2], 123, "nope", None])
    def test_non_mapping_records_not_seeded(self, tmp_path: Path, bad: Any) -> None:
        """存在但根本不是对象（含 null）：按保守默认处理，绝不重种回旧全局开关。"""
        inner = _store(tmp_path)
        _put_raw(inner, KEY, bad)
        _put_raw(inner, LEGACY, {"news_card_enabled": True})
        store = SpyStore(inner)
        cfg = group_push.get_config(store, GID, _settings())
        _assert_conservative(cfg)
        assert store.kv_set_calls == []
        assert store.kv_delete_calls == []
        assert inner.kv_get(KEY, None) == bad
        assert inner.kv_get(LEGACY, None) == {"news_card_enabled": True}

    def test_kv_read_error_conservative_and_zero_write(self, tmp_path: Path) -> None:
        inner = _store(tmp_path)
        _put_raw(inner, LEGACY, {"news_card_enabled": True})
        store = SpyStore(inner)
        store.kv_get_error = RuntimeError("kv 读炸了")
        cfg = group_push.get_config(store, GID, _settings())
        _assert_conservative(cfg)
        assert store.tx_count == 0, "读失败不许开事务"
        assert store.kv_set_calls == [] and store.kv_delete_calls == []
        assert inner.kv_get(KEY, None) is None
        assert inner.kv_get(LEGACY, None) == {"news_card_enabled": True}

    def test_corrupt_string_bool_does_not_open_topics(self, tmp_path: Path) -> None:
        """核心回归：坏串 "false" 绝不能被 bool() 当成真把自动开话题打开。"""
        inner = _store(tmp_path)
        _put_raw(inner, KEY, {
            "topics_enabled": "false", "news_card_enabled": "0",
            "idea_mention_enabled": "no", "news_card_count": 2,
            "daily_max": 3, "quiet_hours": "23:00-08:00",
        })
        before = _raw_text(inner, KEY)
        cfg = group_push.get_config(inner, GID, _settings())
        assert cfg["topics_enabled"] is False
        assert cfg["news_card_enabled"] is False
        assert cfg["idea_mention_enabled"] is False
        assert group_push.kind_enabled(cfg, "topic") is False
        assert cfg["news_card_count"] == 2
        assert _raw_text(inner, KEY) == before, "坏字段只收紧、不回写"

    @pytest.mark.parametrize("bad", [1, 0, "true", [], {}, 1.5])
    def test_corrupt_truthy_bools_take_safe_side(self, tmp_path: Path, bad: Any) -> None:
        inner = _store(tmp_path)
        _put_raw(inner, KEY, {
            "topics_enabled": bad, "news_card_enabled": bad, "idea_mention_enabled": bad,
            "news_card_count": 3, "daily_max": 3, "quiet_hours": "23:00-08:00",
        })
        cfg = group_push.get_config(inner, GID, _settings())
        assert cfg["topics_enabled"] is False
        assert cfg["news_card_enabled"] is False
        assert cfg["idea_mention_enabled"] is False

    def test_corrupt_record_caps_quota_and_defaults_quiet(self, tmp_path: Path) -> None:
        """坏记录兜底：额度有限（不放大到不限，也不超过管理员设的值）+ 默认睡觉时段。"""
        inner = _store(tmp_path)
        _put_text(inner, KEY, "坏掉的内容")
        # 设置里是「不限」+ 自定义睡觉时段：坏记录不许继承成不限
        cfg = group_push.get_config(
            inner, GID,
            _settings(cfg={"delivery": {"push_per_day": 0, "quiet_hours": "00:00-07:00"}}),
        )
        assert cfg["daily_max"] == group_push.DEFAULT_DAILY_MAX
        assert cfg["quiet_hours"] == group_push.DEFAULT_QUIET_HOURS
        # 管理员设得更小 → 不放大，按更小的来
        cfg2 = group_push.get_config(
            inner, GID, _settings(cfg={"delivery": {"push_per_day": 2}})
        )
        assert cfg2["daily_max"] == 2

    def test_corrupt_record_conservative_cfg_stops_pushes(self, tmp_path: Path) -> None:
        """坏记录 + Pushes：三种自制开关都关着，绝不会「以为开着」放行。"""
        inner = _store(tmp_path)
        _put_raw(inner, KEY, {"topics_enabled": "false", "daily_max": 3,
                              "quiet_hours": "00:00-00:00"})
        settings = _settings()
        pushes = Pushes(inner, lambda: settings)
        for kind in ("topic", "news_card", "idea_mention"):
            ok, why = pushes.can_push(GID, kind, NOON)
            assert ok is False, kind
            assert why == "开关已关", kind


# ======================================================================
# 2. 种子事务：二次读 / 坏记录 / 写失败 → 不覆盖、不删旧键、不当结果
# ======================================================================


class TestSeedTxFailClosed:
    def test_seed_tx_second_read_keeps_concurrent_new_save(self, tmp_path: Path) -> None:
        """并发竞态：外层看着像「缺失」，库里其实已有刚存下的新配置 → 二次读认出它。"""
        inner = _store(tmp_path)
        new = {
            "topics_enabled": False, "news_card_enabled": True, "idea_mention_enabled": False,
            "news_card_count": 1, "daily_max": 7, "quiet_hours": "21:00-09:00",
            "news_card_since": 111.0, "idea_mention_since": 0.0,
        }
        _put_raw(inner, KEY, new)
        _put_raw(inner, LEGACY, {"news_card_enabled": True})
        before = _raw_text(inner, KEY)
        store = LyingStore(inner, KEY)
        cfg = group_push.get_config(store, GID, _settings())
        assert cfg["daily_max"] == 7, "并发刚存的新配置不许被 seed 覆盖"
        assert cfg["news_card_enabled"] is True
        assert cfg["news_card_since"] == 111.0
        assert store.kv_set_calls == []
        assert store.kv_delete_calls == [], "没种就别删旧来源"
        assert _raw_text(inner, KEY) == before
        assert inner.kv_get(LEGACY, None) is not None

    def test_seed_tx_corrupt_row_is_not_overwritten(self, tmp_path: Path) -> None:
        inner = _store(tmp_path)
        _put_text(inner, KEY, "{坏 json")
        _put_raw(inner, LEGACY, {"news_card_enabled": True})
        before = _raw_text(inner, KEY)
        store = SpyStore(inner)
        cfg = group_push.get_config(store, GID, _settings())
        _assert_conservative(cfg)
        assert store.kv_set_calls == [], "坏记录不许被覆盖重种"
        assert store.kv_delete_calls == []
        assert _raw_text(inner, KEY) == before
        assert inner.kv_get(LEGACY, None) is not None

    def test_seed_tx_second_read_failure_denies_without_write(self, tmp_path: Path) -> None:
        inner = _store(tmp_path)
        store = SpyStore(inner)
        store.tx_read_error = True
        cfg = group_push.get_config(store, GID, _settings())
        _assert_conservative(cfg)
        assert store.kv_set_calls == [] and store.kv_delete_calls == []
        assert inner.kv_get(KEY, None) is None

    def test_seed_write_failure_returns_conservative_not_seed_payload(self, tmp_path: Path) -> None:
        """写失败时旧实现把「种出来的值」当结果返回——等于没落库也当成配置生效（旧设置默认开）。"""
        inner = _store(tmp_path)
        store = SpyStore(inner)
        store.kv_set_error = RuntimeError("写炸了")
        cfg = group_push.get_config(store, GID, _settings(cfg={"topics": {"enabled": True}}))
        _assert_conservative(cfg)
        assert inner.kv_get(KEY, None) is None


# ======================================================================
# 3. 退役字段：生产路径明确拒绝；显式 legacy 才容忍
# ======================================================================


class TestRetiredFields:
    def test_retired_field_rejected_on_production_path(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        settings = _settings()
        before = dict(group_push.get_config(store, GID, settings))
        with pytest.raises(ValueError) as ei:
            group_push.set_config(store, GID, {"news_card_daily_max": 9}, settings)
        msg = str(ei.value)
        assert "退役" in msg and ("群页" in msg or "往群里发" in msg)
        assert group_push.get_config(store, GID, settings) == before

    @pytest.mark.parametrize("field", sorted(group_push.RETIRED_FIELDS))
    def test_every_retired_field_is_rejected(self, tmp_path: Path, field: str) -> None:
        store = _store(tmp_path)
        settings = _settings()
        with pytest.raises(ValueError):
            group_push.set_config(store, GID, {field: 1}, settings)

    def test_mixed_patch_with_retired_is_atomic(self, tmp_path: Path) -> None:
        """一份 patch 里混了退役字段 → 整份拒绝，连合法字段也不落库（不假保存一半）。"""
        store = _store(tmp_path)
        settings = _settings()
        before = dict(group_push.get_config(store, GID, settings))
        with pytest.raises(ValueError):
            group_push.set_config(
                store, GID, {"news_card_enabled": True, "push_per_day": 9}, settings
            )
        assert group_push.get_config(store, GID, settings) == before
        assert group_push.get_config(store, GID, settings)["news_card_enabled"] is False

    def test_unknown_field_rejected_on_both_paths(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(ValueError):
            group_push.set_config(store, GID, {"nope": 1}, _settings())
        with pytest.raises(ValueError):
            group_push.set_config(store, GID, {"nope": 1})

    def test_retired_field_rejected_on_explicit_legacy_path_too(self, tmp_path: Path) -> None:
        """显式 settings=None 的老调用口（薄壳 / 老桥）也拒：不留假保存。"""
        store = _store(tmp_path)
        group_push.set_config(store, GID, {"daily_max": 4})
        with pytest.raises(ValueError):
            group_push.set_config(store, GID, {"news_card_daily_max": 9})
        # 退役字段没进库、合法字段照旧
        assert "news_card_daily_max" not in group_push.get_config(store, GID)
        assert group_push.get_config(store, GID)["daily_max"] == 4


# ======================================================================
# 4. 生产者 / Pushes：读设置失败 → 零 send 零 seed，兜底不再默认开
# ======================================================================


class RecordingHost:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def send_text(self, session_id, text, *, reply_to="", at_user="", at_name=""):
        self.texts.append(text)
        return SimpleNamespace(sent=True, message_id="m1")

    async def send_image(self, session_id, png, *, text=""):
        self.texts.append(text)
        return SimpleNamespace(sent=True, message_id="i1")


def _kv_none(store: Store) -> bool:
    return store.kv_get(KEY, None) is None


def test_pushes_unreadable_settings_never_seeds(tmp_path: Path) -> None:
    store = SpyStore(_store(tmp_path))
    pushes = Pushes(store, _boom_settings)
    assert pushes.can_push(GID, "topic", NOON)[0] is False
    assert pushes.can_push(GID, "delivery", NOON)[0] is False
    assert pushes.can_push(GID, "error", NOON) == (True, "")   # 故障回执照旧豁免
    assert store.kv_get_calls == [], "读配置失败不许读每群设置"
    assert store.tx_count == 0, "读配置失败不许种"
    assert store.kv_get(KEY, None) is None


@pytest.mark.asyncio
async def test_outbox_flush_unreadable_settings_zero_send_zero_seed(tmp_path: Path) -> None:
    inner = _store(tmp_path)
    with inner.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, session_id, name, token) VALUES (?, ?, '测试群', ?)",
            (GID, "sess-1", "tok1"),
        )
    settings = _settings()
    host = RecordingHost()
    pushes = Pushes(inner, _boom_settings)
    mentions = Mentions(inner, lambda: settings)
    ob = Outbox(inner, host, pushes, mentions, _boom_settings)
    ob.enqueue("k:topic", GID, "text", {"text": "开场白", "push_kind": "topic"})
    ob.enqueue("k:deliver", GID, "text", {"text": "交付", "push_kind": "delivery"})

    await ob.flush(NOON)                                  # 生产调用形状：不传 allowed_groups
    assert host.texts == []
    assert _kv_none(inner), "生产路径读配置失败不许 seed"

    await ob.flush(NOON, allowed_groups={GID})            # 显式给群也不许绕过去
    assert host.texts == [], "读不到设置时一个都不发"
    assert _kv_none(inner), "显式给群也不许 seed"
    rows = inner.read().execute("SELECT key, status, error FROM outbox ORDER BY id").fetchall()
    assert [r["status"] for r in rows] == ["pending", "pending"], "只是推迟，不作废"
    assert all("读不到设置" in str(r["error"]) for r in rows)


@pytest.mark.asyncio
async def test_topics_check_unreadable_settings_skips_without_seed(tmp_path: Path) -> None:
    inner = _store(tmp_path)
    topics = Topics(inner, None, None, None, None, None, None, _boom_settings, None)
    out = await topics.check(GID, NOON)
    assert out.startswith("skip:"), out
    assert _kv_none(inner)


@pytest.mark.asyncio
async def test_card_push_and_idea_mention_cfg_unreadable_is_conservative(tmp_path: Path) -> None:
    inner = _store(tmp_path)
    settings = _settings()
    pushes = Pushes(inner, _boom_settings)
    mentions = Mentions(inner, lambda: settings)
    cp = CardPush(inner, None, pushes, mentions, _boom_settings)
    im = IdeaMention(inner, None, None, pushes, mentions, _boom_settings)
    for cfg in (cp._cfg(GID), im._cfg(GID)):
        _assert_conservative(cfg)
    assert cp.scan(GID, NOON + 60) == 0
    assert _kv_none(inner)


# ======================================================================
# 5. 兼容：settings=None 纯数据层 / 正常种子 / 旧 cardpush 迁移
# ======================================================================


class TestCompat:
    def test_legacy_settings_none_still_reads_and_writes(self, tmp_path: Path) -> None:
        inner = _store(tmp_path)
        cfg = group_push.set_config(inner, GID, {"daily_max": 2, "news_card_enabled": True})
        assert cfg["daily_max"] == 2 and cfg["news_card_enabled"] is True
        assert group_push.get_config(inner, GID)["daily_max"] == 2
        raw = inner.kv_get(KEY, None)
        assert isinstance(raw, dict) and raw["daily_max"] == 2

    def test_normal_settings_seed_keeps_topics_enabled_and_total_3(self, tmp_path: Path) -> None:
        """不擅改旧正常迁移：topics.enabled 原值照抄、总上限仍是 3。"""
        inner = _store(tmp_path)
        settings = _settings(cfg={"topics": {"enabled": True}})
        cfg = group_push.get_config(inner, GID, settings)
        assert cfg["topics_enabled"] is True
        assert cfg["daily_max"] == 3
        assert group_push.kind_enabled(cfg, "topic") is True

    def test_legacy_cardpush_bad_bool_not_opened_on_migration(self, tmp_path: Path) -> None:
        """旧 cardpush 里的坏布尔也要按安全侧归一，不能借迁移把开关打开。"""
        inner = _store(tmp_path)
        _put_raw(inner, LEGACY, {
            "news_card_enabled": "false", "idea_mention_enabled": 1,
            "news_card_count": 2,
        })
        cfg = group_push.get_config(inner, GID, _settings())
        assert cfg["news_card_enabled"] is False
        assert cfg["idea_mention_enabled"] is False
        assert cfg["news_card_count"] == 2
        assert cfg["daily_max"] == 3
        assert inner.kv_get(LEGACY, None) is None, "种好才删旧源（幂等迁移）"

    def test_served_check_failure_is_conservative_zero_sql(self, tmp_path: Path) -> None:
        """认服务群炸了 → 比照坏配置：保守默认、零 SQL（不碰外群、不默认开）。"""

        class RaisingServed:
            def __init__(self) -> None:
                self.groups = {GID: object()}
                # 旧实现会拿这份「看着能开」的设置当默认值用（话题默认开、额度不限）
                self.topics = SimpleNamespace(enabled=True)
                self.delivery = SimpleNamespace(push_per_day=0, quiet_hours="00:00-07:00")

            def is_served(self, gid: str) -> bool:
                raise RuntimeError("判服务群炸了")

        store = SpyStore(_store(tmp_path))
        cfg = group_push.get_config(store, GID, RaisingServed())
        _assert_conservative(cfg)
        assert store.kv_get_calls == []
        assert store.tx_count == 0

    def test_settings_without_served_evidence_is_conservative(self, tmp_path: Path) -> None:
        """没有任何服务群证据（既没 is_served 也没 groups）→ 保守默认、零读写。"""

        class Bare:
            topics = SimpleNamespace(enabled=True)
            delivery = SimpleNamespace(push_per_day=0, quiet_hours="00:00-07:00")

        store = SpyStore(_store(tmp_path))
        cfg = group_push.get_config(store, GID, Bare())
        _assert_conservative(cfg)
        assert store.kv_get_calls == [] and store.tx_count == 0

    def test_unserved_group_zero_read_zero_write(self, tmp_path: Path) -> None:
        store = SpyStore(_store(tmp_path))
        settings = _settings(serve=(GID,))
        cfg = group_push.get_config(store, OTHER, settings)
        # 非服务群：三个自动群发开关都关（不是 legacy「话题默认开」），零读写
        _assert_conservative(cfg)
        assert store.kv_get_calls == [], "非服务群零读取"
        assert store.tx_count == 0
        with pytest.raises(ValueError):
            group_push.set_config(store, OTHER, {"daily_max": 1}, settings)
        assert store.tx_count == 0
