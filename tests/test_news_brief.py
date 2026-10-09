"""资讯「卡片短摘要」brief（2026-10，用户定）。

背景：发到 QQ 群的资讯卡片图上用的是 summary，而 summary 中位数 222 字，卡片上放不下。
做法：每条资讯在打分时顺带让模型写两三句 60–100 字的短摘要（2026-10-06 由 30–60 放宽） brief（直接说发生了什么、
带关键数字、不铺垫、不重复标题、不点名群友），只给群卡片用；网页照旧用 summary。

- store.py：迁移 `_m_news_brief` 给 news_items 加 `brief TEXT NOT NULL DEFAULT ''`，
  幂等（列已在就跳过），追加在 `_MIGRATIONS` 末尾。
- feeds.py：打分提示词加 `"brief"` 一项；`_norm` 读出（去空白、换行变空格；正常长度原样保留，
  超长异常整条回落——2026-10-08 巡检前是硬截 140 字，会把「免费 / 付费限定」这类条件切掉）；
  写回 item、`_zero_pack` / 两处 setdefault 兜底空串；
  隐私闸（`_scrub_item_text`）对 brief 也过一遍——不过只把 brief 置空，**不**拒整条；
  入库（accepted_items 那条 INSERT）写 brief；打分时给模型看的 summary 从 150 字放宽到 300 字。
"""

from __future__ import annotations

import json
from pathlib import Path

from CharTyr_MaiWork.maiwork import store as store_mod
from CharTyr_MaiWork.maiwork.store import _MIGRATIONS, Store

from fakes import FakeModelsQueue
from test_feeds import GID, _FOCUS_JSON, _make_feeds, _run, _TimePatch

# 关注的群友（隐私闸参照物）：note 里任何 ≥8 字连续片段出现在文字里就算命中
MEMBER_NOTE = "他最近在备考注册建筑师考试，周三晚上没空"
MEMBER_NOTE_FRAGMENT = "备考注册建筑师考试"

BRIEF_A = "新板子 10 月上市，集成 8 路收发器，售价 199 美元。"
BRIEF_B = "在 8GB 显存上跑 7B 模型，实测每秒 20 token。"


def _cols(store: Store, table: str = "news_items") -> dict:
    return {str(r["name"]): r for r in store.read().execute(f"PRAGMA table_info({table})")}


def _scores_json(entries: list[dict]) -> str:
    return json.dumps({"scores": entries}, ensure_ascii=False)


def _entry(idx: int, title: str, brief: str | None = "短摘要。", **extra) -> dict:
    """一条打分的形状（照 test_feeds._SCORES_JSON 的字段，多一个 brief）。"""
    out = {
        "i": idx,
        "title": title,
        "info": 5,
        "source": 4,
        "relevance": 5,
        "timeliness": 4,
        "chat": 4,
        "profile": 0,
        "topic": f"话题{idx}",
        "sensitive": False,
        "grounded": True,
        "junk": False,
        "junk_reason": "",
        "same_as_recent": False,
        "why": "和画像对得上",
        "icon": "tools",
    }
    if brief is not None:
        out["brief"] = brief
    out.update(extra)
    return out


def _seed_focus_member(store: Store, note: str = MEMBER_NOTE) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO focus_members (group_id, user_id, name, note, pinned, removed, updated)"
            " VALUES (?, 'ufan', '阿帆', ?, 0, 0, 0)",
            (GID, note),
        )


def _accepted_rows(store: Store) -> list:
    return store.read().execute(
        "SELECT * FROM news_items WHERE rejected=0 ORDER BY id"
    ).fetchall()


def _score_prompt(models: FakeModelsQueue) -> str:
    for _role, messages, kwargs in models.calls:
        if str(kwargs.get("purpose") or "") == "feeds.score":
            return str(messages[0]["content"])
    raise AssertionError("打分模型没被调用")


# ----------------------------------------------------------------------
# 迁移：news_items.brief
# ----------------------------------------------------------------------


class TestMigration:
    def test_migration_step_kept_in_order(self) -> None:
        """加 brief 那一步还在原来的位置（第 36 步），后面只许往后追加新步。"""
        idx = [fn.__name__ for fn in _MIGRATIONS].index("_m_news_brief")
        assert idx == 35, "第 36 步 = news_items.brief（docs/20）；新步只能往后加"

    def test_fresh_db_has_brief_column(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "t.db")
        try:
            assert store.migrate() == len(_MIGRATIONS)
            cols = _cols(store)
            assert "brief" in cols
            assert str(cols["brief"]["type"]) == "TEXT"
            assert int(cols["brief"]["notnull"]) == 1
            assert str(cols["brief"]["dflt_value"]) == "''"
        finally:
            store.close()

    def test_old_db_upgrade_adds_brief_and_rerun_is_idempotent(self, tmp_path: Path) -> None:
        """老库（库号停在加 brief 之前）升上来补列；重复跑同一步不报错、结构不变。"""
        store = Store(tmp_path / "old.db")
        try:
            steps = store_mod._MIGRATIONS.index(store_mod._m_news_brief)
            for fn in _MIGRATIONS[:steps]:
                fn(store.read())
            store.read().execute(f"PRAGMA user_version={steps}")
            assert "brief" not in _cols(store)

            assert store.migrate() == len(_MIGRATIONS)
            assert "brief" in _cols(store)

            before = _cols(store)
            store_mod._m_news_brief(store.read())
            store_mod._m_news_brief(store.read())
            assert _cols(store) == before
        finally:
            store.close()

    def test_existing_rows_read_as_empty_string(self, tmp_path: Path) -> None:
        """老行跟着补列后 brief 读作空串，数据不动。"""
        store = Store(tmp_path / "old.db")
        try:
            steps = store_mod._MIGRATIONS.index(store_mod._m_news_brief)
            for fn in _MIGRATIONS[:steps]:
                fn(store.read())
            store.read().execute(f"PRAGMA user_version={steps}")
            with store.tx() as conn:
                conn.execute(
                    "INSERT INTO news_items (batch_id, group_id, title, summary)"
                    " VALUES (1, ?, '老新闻', '老摘要')",
                    (GID,),
                )
            store.migrate()
            row = store.read().execute(
                "SELECT title, brief FROM news_items WHERE title='老新闻'").fetchone()
            assert row["title"] == "老新闻" and row["brief"] == ""
        finally:
            store.close()


# ----------------------------------------------------------------------
# 打分提示词
# ----------------------------------------------------------------------


class TestPrompt:
    def test_prompt_asks_brief_and_shows_300_chars_of_summary(self, tmp_path) -> None:
        long_summary = "摘要开头。" + "很长的正文细节，" * 40  # >300 字
        models = FakeModelsQueue(ready=True, replies=[_scores_json([_entry(0, "候选甲")])])
        store, settings, feeds, *_ = _make_feeds(tmp_path, models=models)
        cands = [{
            "title": "候选甲", "url": "https://example.com/a", "summary": long_summary,
            "kind": "news", "quote": "原文依据", "fetched": True, "paywall": False,
        }]
        with _TimePatch():
            _run(feeds._score(GID, settings, cands))
        prompt = _score_prompt(models)
        assert '"brief"' in prompt
        assert "60 到 100 个汉字" in prompt  # 2026-10-06 用户：30~60 字太少
        # summary 从 150 字放宽到 300 字：150 字那版看不到这么靠后的内容
        assert long_summary[:300] in prompt
        store.close()


# ----------------------------------------------------------------------
# 打分 → 入库
# ----------------------------------------------------------------------


def _run_prepare(tmp_path, entries: list[dict]):
    """跑一次完整备料（三候选，前两条过线），返回 (store, 打分回复)。"""
    models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, _scores_json(entries)])
    store, settings, feeds, models, workers, topics, profiles = _make_feeds(
        tmp_path, models=models)
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    return store


class TestBriefLanding:
    def test_brief_from_scores_lands_in_news_items(self, tmp_path) -> None:
        store = _run_prepare(tmp_path, [
            _entry(0, "新开源 FPGA 开发板发布", brief=BRIEF_A),
            _entry(1, "小模型本地部署教程", brief=BRIEF_B),
            _entry(2, "吃桃子的十种方法", brief="挑桃子的三个小窍门。", relevance=1, chat=1),
        ])
        try:
            briefs = {r["title"]: r["brief"] for r in _accepted_rows(store)}
            assert briefs["新开源 FPGA 开发板发布"] == BRIEF_A
            assert briefs["小模型本地部署教程"] == BRIEF_B
            # 被筛掉的条目不带 brief（网页照旧用 summary）
            rejected = store.read().execute(
                "SELECT brief FROM news_items WHERE rejected=1").fetchall()
            assert all(r["brief"] == "" for r in rejected)
        finally:
            store.close()

    def test_long_brief_kept_in_full(self, tmp_path) -> None:
        """2026-10-08 巡检改契约：正常长度的 brief 不再硬截 140（线上 #1301 176 字被切成
        「…目前只是 al」，把收费限定整句丢掉）。200 字照原样入库。"""
        long_brief = "字" * 200
        store = _run_prepare(tmp_path, [
            _entry(0, "新开源 FPGA 开发板发布", brief=long_brief),
            _entry(1, "小模型本地部署教程", brief=BRIEF_B),
            _entry(2, "吃桃子的十种方法", brief="挑桃子的三个小窍门。", relevance=1, chat=1),
        ])
        try:
            row = store.read().execute(
                "SELECT brief FROM news_items WHERE title='新开源 FPGA 开发板发布'").fetchone()
            assert row["brief"] == long_brief
            assert len(row["brief"]) == 200
        finally:
            store.close()

    def test_overlong_brief_is_blanked_for_summary_fallback(self, tmp_path) -> None:
        """超长异常（超过安全上限）不硬截也不留半截：整条置空，卡片回落已核验的 summary。"""
        from CharTyr_MaiWork.maiwork.feeds import _BRIEF_KEEP_MAX

        store = _run_prepare(tmp_path, [
            _entry(0, "新开源 FPGA 开发板发布", brief="字" * (_BRIEF_KEEP_MAX + 20)),
            _entry(1, "小模型本地部署教程", brief=BRIEF_B),
            _entry(2, "吃桃子的十种方法", brief="挑桃子的三个小窍门。", relevance=1, chat=1),
        ])
        try:
            row = store.read().execute(
                "SELECT brief, summary FROM news_items WHERE title='新开源 FPGA 开发板发布'").fetchone()
            assert row["brief"] == ""
            assert row["summary"], "回落靠的是已经核验过的 summary，它必须在"
            # 同批别条不受影响
            other = store.read().execute(
                "SELECT brief FROM news_items WHERE title='小模型本地部署教程'").fetchone()
            assert other["brief"] == BRIEF_B
        finally:
            store.close()

    def test_brief_whitespace_and_newlines_normalized(self, tmp_path) -> None:
        store = _run_prepare(tmp_path, [
            _entry(0, "新开源 FPGA 开发板发布", brief="  第一句事实。\n第二句数字。  "),
            _entry(1, "小模型本地部署教程", brief=BRIEF_B),
            _entry(2, "吃桃子的十种方法", brief="挑桃子的三个小窍门。", relevance=1, chat=1),
        ])
        try:
            row = store.read().execute(
                "SELECT brief FROM news_items WHERE title='新开源 FPGA 开发板发布'").fetchone()
            assert row["brief"] == "第一句事实。 第二句数字。"
        finally:
            store.close()

    def test_missing_brief_is_empty_string(self, tmp_path) -> None:
        store = _run_prepare(tmp_path, [
            _entry(0, "新开源 FPGA 开发板发布", brief=None),
            _entry(1, "小模型本地部署教程", brief=None),
            _entry(2, "吃桃子的十种方法", brief=None, relevance=1, chat=1),
        ])
        try:
            rows = store.read().execute("SELECT brief FROM news_items").fetchall()
            assert all(r["brief"] == "" for r in rows)
        finally:
            store.close()

    def test_privacy_failed_brief_is_blanked_but_item_kept(self, tmp_path) -> None:
        """brief 撞上关注成员私下注记：只把 brief 置空，条目照常入库（不拒整条）。"""
        bad_brief = f"{MEMBER_NOTE_FRAGMENT}，下半年考试安排已公布。"
        models = FakeModelsQueue(ready=True, replies=[
            _FOCUS_JSON,
            _scores_json([
                _entry(0, "新开源 FPGA 开发板发布", brief=bad_brief),
                _entry(1, "小模型本地部署教程", brief=BRIEF_B),
                _entry(2, "吃桃子的十种方法", brief="挑桃子的三个小窍门。", relevance=1, chat=1),
            ]),
        ])
        store, settings, feeds, models, workers, topics, profiles = _make_feeds(
            tmp_path, models=models)
        _seed_focus_member(store)
        try:
            with _TimePatch():
                _run(feeds.prepare_news(GID))
            row = store.read().execute(
                "SELECT brief, rejected, why FROM news_items WHERE title='新开源 FPGA 开发板发布'"
            ).fetchone()
            assert row["rejected"] == 0, "brief 不过隐私闸不该拒掉整条资讯"
            assert row["brief"] == ""
            assert row["why"] == "和画像对得上"
            # 同批别的条目的 brief 不受影响
            other = store.read().execute(
                "SELECT brief FROM news_items WHERE title='小模型本地部署教程'").fetchone()
            assert other["brief"] == BRIEF_B
        finally:
            store.close()

    def test_clean_brief_survives_privacy_gate(self, tmp_path) -> None:
        models = FakeModelsQueue(ready=True, replies=[
            _FOCUS_JSON,
            _scores_json([
                _entry(0, "新开源 FPGA 开发板发布", brief=BRIEF_A),
                _entry(1, "小模型本地部署教程", brief=BRIEF_B),
                _entry(2, "吃桃子的十种方法", brief="挑桃子的三个小窍门。", relevance=1, chat=1),
            ]),
        ])
        store, settings, feeds, models, workers, topics, profiles = _make_feeds(
            tmp_path, models=models)
        _seed_focus_member(store)
        try:
            with _TimePatch():
                _run(feeds.prepare_news(GID))
            row = store.read().execute(
                "SELECT brief, rejected FROM news_items WHERE title='新开源 FPGA 开发板发布'"
            ).fetchone()
            assert row["rejected"] == 0 and row["brief"] == BRIEF_A
        finally:
            store.close()
