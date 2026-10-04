"""每群三份迁移的「部分清理失败」幂等性（真 Store + 真 Agents，只注入一次失败）。

背景（复审发现的真坑）：

`_migrate_one_group` 用 `cur.updated_by == "migrate"` 判「上次是我们拼的，可以继续补段」。
于是当规则正文已经拼好、但**旧来源清理失败**（`kv_delete` 抛错 / `mem.bak` 写不进去）时，
下一次启动会把**同一段 legacy 内容再拼一遍**——反复几次直到 3000 字上限，之后报「超长
不覆盖」，源永远删不掉；`_gc_backup_memory_file` 还会用新内容**覆写**最早那份原件备份，
把最初的原文件弄丢。

本文件锁死的契约（全部用真 Store + 真 Agents + 真文件）：

1. 已经**完整**迁进正文的规范块不再重复 append；清理失败只是「源留着」，下次接着清。
2. 真正**新出现**的 legacy 块只 append 一次（第二次不再补）；去重按**完整 block 精确匹配**，
   不用标题 marker 粗判（正文里只有标题、没有那段内容时，新段照补）。
3. `mem.bak` 保**最初原件**、绝不覆写；原文件又出现新内容 → 另存唯一后缀档
   （`<名>.1.md` …），先写成功备份、再清原文件；备份写失败 → 原文件一个字不动。
4. 正文超长（写不下）/ 正文被管理员手改成非 migrate 来源 → **源一律保留**，绝不擅删。
5. 非服务群零读取零写入；`agent_memory_notes` 表绝不 DROP（其他群的行照常在）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from CharTyr_MaiWork.maiwork import migrations
from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"
G2 = "123456789"

PREF = "找中文的，别太长"
PREF_PART = f"【管理员写的资讯偏好】\n{PREF}"
NOTES_FRAG = "资讯要加个「顺手一提」"
MEM_A = "- 2026-10-01 这个群爱看开发内幕（手动写）\n"
MEM_B = "- 2026-10-05 管理员又手写了一段新的工作记忆\n"


class _Settings:
    """最小 settings 形态：只有服务群名单 + data_dir（migrations 只读这几样）。"""

    def __init__(self, data_dir: Path, served=(G1,)) -> None:
        self._data_dir = Path(data_dir)
        self.groups = {str(g): object() for g in served}
        self.model_list = ()

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    def is_served(self, gid: Any) -> bool:
        return str(gid) in self.groups


class FlakyStore:
    """包一层真 Store：对指定 key 的 `kv_delete` 失败**一次**，其余全走真库。"""

    def __init__(self, inner: Store, *, fail_delete_key: str | None = None) -> None:
        self._inner = inner
        self.fail_delete_key = fail_delete_key
        self.delete_calls: list[str] = []
        self.failed = False

    def kv_delete(self, conn: Any, key: str) -> None:
        self.delete_calls.append(str(key))
        if not self.failed and self.fail_delete_key is not None and str(key) == self.fail_delete_key:
            self.failed = True
            raise RuntimeError("kv_delete 失败（测试注入一次）")
        return self._inner.kv_delete(conn, key)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "t.db")
    store.migrate()
    return store


def _seed(store: Store, settings: _Settings, gid: str = G1, *, mem: str | None = MEM_A) -> None:
    """塞全套旧来源：feeds.pref kv + agent_memory_notes 两岗 + identity/memory/<gid>.md。"""
    agents = Agents(store, lambda: settings)
    agents._ensure_schema()
    with store.tx() as conn:
        store.kv_set(conn, f"feeds.pref.{gid}", PREF)
        conn.execute(
            "INSERT INTO agent_memory_notes (group_id, kind, notes, updated) VALUES (?, ?, ?, 1.0)",
            (gid, "news", NOTES_FRAG),
        )
        conn.execute(
            "INSERT INTO agent_memory_notes (group_id, kind, notes, updated) VALUES (?, ?, ?, 1.0)",
            (gid, "idea", "构想别发太早"),
        )
    mem_dir = Path(settings.data_dir) / "identity" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    if mem is not None:
        (mem_dir / f"{gid}.md").write_text(mem, encoding="utf-8")


def _mig(store: Store, settings: _Settings) -> dict:
    return migrations.migrate_group_context_to_rules_and_skills(
        store, settings, None, lambda g: str(settings.data_dir)
    )


def _body(store: Store, settings: _Settings, gid: str = G1) -> str:
    return str(Agents(store, lambda: settings).group_rules_get(gid)["body"])


def _mem(settings: _Settings, gid: str = G1) -> Path:
    return Path(settings.data_dir) / "identity" / "memory" / f"{gid}.md"


def _bak(settings: _Settings, name: str) -> Path:
    return Path(settings.data_dir) / "identity" / "mem.bak" / name


# ======================================================================
# 1. 清理失败 → 不重复 append，恢复后往下清；正文只一份
# ======================================================================


class TestPartialCleanupIdempotent:
    def test_kv_delete_failure_then_recovery_appends_once(self, tmp_path: Path) -> None:
        inner = _store(tmp_path)
        settings = _Settings(tmp_path)
        _seed(inner, settings)
        flaky = FlakyStore(inner, fail_delete_key=f"feeds.pref.{G1}")

        _mig(flaky, settings)
        body1 = _body(inner, settings)
        assert body1.count(NOTES_FRAG) == 1, body1
        assert body1.count(PREF) == 1, body1
        assert body1.count("这个群爱看开发内幕") == 1, body1
        assert inner.kv_get(f"feeds.pref.{G1}") is not None, "清理失败：源要留着下次再清"
        assert flaky.failed is True

        # 恢复（不再注入失败）后再跑两次：不重复拼、源这次真清掉
        _mig(inner, settings)
        body2 = _body(inner, settings)
        _mig(inner, settings)
        body3 = _body(inner, settings)
        assert body2 == body1, "已完整迁入的规范块被重复 append 了"
        assert body3 == body1
        assert body3.count(NOTES_FRAG) == 1 and body3.count(PREF) == 1
        assert body3.count("这个群爱看开发内幕") == 1
        assert inner.kv_get(f"feeds.pref.{G1}") is None

    def test_mem_backup_failure_then_recovery_appends_once(self, tmp_path: Path) -> None:
        inner = _store(tmp_path)
        settings = _Settings(tmp_path)
        _seed(inner, settings)
        bak_dir = Path(settings.data_dir) / "identity" / "mem.bak"
        bak_dir.write_text("占位：让备份目录建不出来（模拟 mem.bak 写失败）", encoding="utf-8")

        _mig(inner, settings)
        body1 = _body(inner, settings)
        assert body1.count("这个群爱看开发内幕") == 1, body1
        assert _mem(settings).read_text(encoding="utf-8") == MEM_A, "备份失败时不许清原文件"

        # 恢复：目录能建了，再跑两次
        bak_dir.unlink()
        _mig(inner, settings)
        body2 = _body(inner, settings)
        assert _mem(settings).read_text(encoding="utf-8") == "", "备份成功后原文件清空"
        assert _bak(settings, f"{G1}.md").read_text(encoding="utf-8") == MEM_A
        _mig(inner, settings)
        body3 = _body(inner, settings)
        assert body2 == body1 and body3 == body1, "备份恢复后不该再补同一段"
        # 已经是空壳文件了：再跑一次顺手删掉（内容早备份过），但正文不再补
        assert not _mem(settings).exists() or _mem(settings).read_text(encoding="utf-8") == ""

    def test_exact_block_match_not_marker_only(self, tmp_path: Path) -> None:
        """正文里只有标题 marker、没有那段内容 → 真正新的整段照补一次（不按 marker 粗判丢段）。"""
        inner = _store(tmp_path)
        settings = _Settings(tmp_path)
        agents = Agents(inner, lambda: settings)
        agents._ensure_schema()
        agents.group_rules_set(G1, "【管理员写的资讯偏好】\n旧的另一套", updated_by="migrate")
        with inner.tx() as conn:
            inner.kv_set(conn, f"feeds.pref.{G1}", "新的偏好内容")
        _mig(inner, settings)
        body = _body(inner, settings)
        assert body.count("新的偏好内容") == 1, body
        assert "旧的另一套" in body
        _mig(inner, settings)
        assert _body(inner, settings).count("新的偏好内容") == 1


# ======================================================================
# 2. 备份保最初原件、不覆写；新内容另存唯一后缀档
# ======================================================================


class TestMemoryBackupPreserved:
    def test_second_round_new_memory_gets_suffix_and_keeps_original(self, tmp_path: Path) -> None:
        inner = _store(tmp_path)
        settings = _Settings(tmp_path)
        _seed(inner, settings, mem=MEM_A)

        _mig(inner, settings)
        base = _bak(settings, f"{G1}.md")
        assert base.read_text(encoding="utf-8") == MEM_A
        assert _mem(settings).read_text(encoding="utf-8") == ""

        # 管理员又手写了一段新记忆：第二次迁移要另存后缀档，绝不覆写最初原件
        _mem(settings).write_text(MEM_B, encoding="utf-8")
        _mig(inner, settings)
        assert base.read_text(encoding="utf-8") == MEM_A, "最初原件备份被覆写了"
        suffix = _bak(settings, f"{G1}.1.md")
        assert suffix.read_text(encoding="utf-8") == MEM_B
        assert _mem(settings).read_text(encoding="utf-8") == ""
        body = _body(inner, settings)
        assert body.count("管理员又手写了一段新的工作记忆") == 1

        # 幂等：再跑一次不新增第三份备份、正文也不再补
        _mig(inner, settings)
        assert base.read_text(encoding="utf-8") == MEM_A
        assert not _bak(settings, f"{G1}.2.md").exists()
        assert _body(inner, settings) == body


# ======================================================================
# 3. 超长 / 手改权威 / 非服务群：源一律保留
# ======================================================================


class TestSourcesPreservedWhenNotMerged:
    def test_overlong_body_keeps_source(self, tmp_path: Path) -> None:
        inner = _store(tmp_path)
        settings = _Settings(tmp_path)
        agents = Agents(inner, lambda: settings)
        agents._ensure_schema()
        long_body = "长" * 2995
        agents.group_rules_set(G1, long_body, updated_by="migrate")
        with inner.tx() as conn:
            inner.kv_set(conn, f"feeds.pref.{G1}", PREF)
        _mig(inner, settings)
        assert _body(inner, settings) == long_body, "超长不许覆盖"
        assert inner.kv_get(f"feeds.pref.{G1}") is not None, "没拼进去 → 源必须保留"

    def test_admin_body_keeps_source(self, tmp_path: Path) -> None:
        inner = _store(tmp_path)
        settings = _Settings(tmp_path)
        agents = Agents(inner, lambda: settings)
        agents._ensure_schema()
        agents.group_rules_set(G1, "只发搞笑视频", updated_by="admin")
        with inner.tx() as conn:
            inner.kv_set(conn, f"feeds.pref.{G1}", "不该拼进来")
        _mig(inner, settings)
        assert _body(inner, settings) == "只发搞笑视频"
        assert inner.kv_get(f"feeds.pref.{G1}") == "不该拼进来"

    def test_unserved_group_not_touched(self, tmp_path: Path) -> None:
        inner = _store(tmp_path)
        settings = _Settings(tmp_path, served=(G1,))
        _seed(inner, settings, gid=G1)
        _seed(inner, settings, gid=G2)
        _mig(inner, settings)
        # G2 不在服务群：kv / notes / memory 文件一个字没动
        assert inner.kv_get(f"feeds.pref.{G2}") == PREF
        rows = inner.read().execute(
            "SELECT COUNT(*) AS c FROM agent_memory_notes WHERE group_id=?", (G2,)
        ).fetchone()
        assert int(rows["c"]) == 2
        assert _mem(settings, G2).read_text(encoding="utf-8") == MEM_A


# ======================================================================
# 4. notes 表绝不 DROP；其他群的行照常
# ======================================================================


class TestNotesTableSurvives:
    def test_table_kept_and_other_groups_rows_intact(self, tmp_path: Path) -> None:
        inner = _store(tmp_path)
        settings = _Settings(tmp_path, served=(G1,))
        _seed(inner, settings, gid=G1)
        _seed(inner, settings, gid=G2)
        _mig(inner, settings)
        exists = inner.read().execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_memory_notes'"
        ).fetchone()
        assert exists is not None, "不该 DROP notes 表"
        mine = inner.read().execute(
            "SELECT COUNT(*) AS c FROM agent_memory_notes WHERE group_id=?", (G1,)
        ).fetchone()
        other = inner.read().execute(
            "SELECT COUNT(*) AS c FROM agent_memory_notes WHERE group_id=?", (G2,)
        ).fetchone()
        assert int(mine["c"]) == 0, "本群已迁完的清掉"
        assert int(other["c"]) == 2, "非服务群的行不许被误删"
