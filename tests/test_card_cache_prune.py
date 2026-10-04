"""卡片图缓存 GC（card_push.prune_card_cache）：只删自己那一份、早就落地的图。

锁死（父会话复审确认的边界）：

- 只碰 `<workspace_root>/.web/.push/card-<id>.png`；
- 发件箱里必须有一条 `key=news_card:<id>`、`kind='image'`、`status ∈ sent/failed/dropped`
  且 `updated <= now - 7 天` 的记录；
- pending / sending / uncertain 绝不删（结果没定，图还得留着重发）；
- 符号链接、解析到目录外的、名字 / 编号对不上的、没有对应记录的、artifact 里的一律不碰；
- 单张出问题只记日志，不拖垮整轮。

用真 Store + 真文件系统；workspace_root = tmp_path，不碰仓库里任何东西。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import card_push
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.store import Store

BJ = timezone(timedelta(hours=8))
GID = "900000001"
NOW = datetime(2026, 10, 15, 12, tzinfo=BJ).timestamp()
WEEK = 7 * 86400.0
OLD = NOW - WEEK - 60.0     # 早就过了保留期
RECENT = NOW - WEEK + 60.0  # 还没到

PNG = b"\x89PNG\r\n\x1a\n" + b"cache-bytes"


def _make(tmp_path):
    store = Store(tmp_path / "t.db")
    store.migrate()
    settings, problems = load_settings(
        {"environments": {"workspace_root": str(tmp_path)}}
    )
    assert not problems, problems
    push_dir = tmp_path / ".web" / ".push"
    push_dir.mkdir(parents=True, exist_ok=True)
    return store, settings, push_dir


def _png_file(push_dir: Path, cid) -> Path:
    p = push_dir / f"card-{cid}.png"
    p.write_bytes(PNG)
    return p


def _box(store: Store, key: str, *, status: str, updated: float, kind: str = "image") -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO outbox (key, group_id, kind, payload, status, attempts, result, error,"
            " not_before, created, updated) VALUES (?, ?, ?, ?, ?, 1, '{}', '', 0, ?, ?)",
            (key, GID, kind, json.dumps({"push_kind": "news_card"}), status, updated, updated),
        )


def _prune(store, settings, *, now: float = NOW) -> int:
    return card_push.prune_card_cache(store, lambda: settings, now)


def test_prune_only_removes_old_settled_own_cache(tmp_path):
    store, settings, push_dir = _make(tmp_path)
    # 该删的三张：sent / failed / dropped + 早就过了 7 天
    for cid, status in ((1, "sent"), (2, "failed"), (3, "dropped")):
        _png_file(push_dir, cid)
        _box(store, f"news_card:{cid}", status=status, updated=OLD)
    # 结果没定：绝不删
    for cid, status in ((4, "pending"), (5, "sending"), (6, "uncertain")):
        _png_file(push_dir, cid)
        _box(store, f"news_card:{cid}", status=status, updated=OLD)
    # 已发但还在保留期：不删
    _png_file(push_dir, 7)
    _box(store, "news_card:7", status="sent", updated=RECENT)
    # 不是 image 类型：不删
    _png_file(push_dir, 8)
    _box(store, "news_card:8", status="sent", updated=OLD, kind="text")
    # key 编号不是数字：不删
    _png_file(push_dir, 9)
    _box(store, "news_card:9x", status="sent", updated=OLD)

    removed = _prune(store, settings)

    assert removed == 3
    assert not (push_dir / "card-1.png").exists()
    assert not (push_dir / "card-2.png").exists()
    assert not (push_dir / "card-3.png").exists()
    for cid in (4, 5, 6, 7, 8, 9):
        assert (push_dir / f"card-{cid}.png").exists(), cid


def test_prune_skips_orphans_other_names_and_artifacts(tmp_path):
    store, settings, push_dir = _make(tmp_path)
    # 有图没记录（孤儿）：不碰
    _png_file(push_dir, 20)
    # 别人的文件：不碰
    other = push_dir / "notes.txt"
    other.write_text("别删我", encoding="utf-8")
    # 名字对不上编号：不碰
    weird = push_dir / "card-21-extra.png"
    weird.write_bytes(PNG)
    # 任务成品目录里的同名文件：不碰
    art = tmp_path / "ws" / "artifacts" / "T-1"
    art.mkdir(parents=True, exist_ok=True)
    art_png = art / "card-22.png"
    art_png.write_bytes(PNG)
    _box(store, "news_card:22", status="sent", updated=OLD)

    removed = _prune(store, settings)

    assert removed == 0
    assert (push_dir / "card-20.png").exists()
    assert other.exists()
    assert weird.exists()
    assert art_png.exists()


def test_prune_refuses_symlink_and_leaves_target(tmp_path):
    store, settings, push_dir = _make(tmp_path)
    _png_file(push_dir, 30)
    _box(store, "news_card:30", status="sent", updated=OLD)
    # 目录外的真文件 + 指过去的符号链接
    secret = tmp_path / "secret.txt"
    secret.write_text("绝不能删", encoding="utf-8")
    link = push_dir / "card-31.png"
    os.symlink(secret, link)
    _box(store, "news_card:31", status="sent", updated=OLD)

    removed = _prune(store, settings)

    assert removed == 1
    assert not (push_dir / "card-30.png").exists()
    assert secret.exists() and secret.read_text(encoding="utf-8") == "绝不能删"
    assert os.path.islink(link)


def test_prune_survives_one_bad_entry(tmp_path):
    """中间夹一张「同名目录」（不是普通文件）不该拖垮整轮。"""
    store, settings, push_dir = _make(tmp_path)
    _png_file(push_dir, 40)
    _box(store, "news_card:40", status="sent", updated=OLD)
    bad = push_dir / "card-41.png"
    bad.mkdir()
    _box(store, "news_card:41", status="sent", updated=OLD)
    _png_file(push_dir, 42)
    _box(store, "news_card:42", status="sent", updated=OLD)

    removed = _prune(store, settings)

    assert removed == 2
    assert not (push_dir / "card-40.png").exists()
    assert not (push_dir / "card-42.png").exists()
    assert bad.is_dir()


def test_prune_no_records_is_noop(tmp_path):
    store, settings, push_dir = _make(tmp_path)
    _png_file(push_dir, 50)
    assert _prune(store, settings) == 0
    assert (push_dir / "card-50.png").exists()
