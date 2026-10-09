"""「模型压根没评过」的淘汰不许变成内容黑名单（2026-10-08 复核发现的交互）。

背景：整轮打分失败（线上端点 451 内容拦截）后，修正版会把幸存者逐条落库留痕
（reject_gate="score"、reject_reason="打分没做完（模型超时/出错），这轮没评上"），
批次审计照留。但 `_recent_rejected_keys` 只放过「名额 / 配额」类理由，别的 rejected=1
一概当「内容被拒过」进 lookback_days 去重名单——于是这批**从没被评过**的好文，
后面几轮会被 `_merge_rss_candidates` / `_prefilter`（「这条最近几轮已经筛掉过（以前拒过）」）
和 `_dup_url_key_check` 当成内容否决永久挡掉，最长 14 天。

修法（精确判定，不一刀切跳过 score 闸）：
- 新增 `_rejection_has_no_content_verdict(gate, reason)`：只认 `gate == "score"` +
  写死的那两句「没评上」话术（`_SCORE_FAILED_REASON` / `_SCORE_MISSING_REASON`）；
  别的门、别的话术（将来 score 闸若出现真正的内容判定）一律 False，照旧按内容类处理。
- `_recent_rejected_keys` 里把它和名额类一起放过；**留痕本身照旧**（行还在、批次 note 还在），
  也不会因此重发、不会多调模型、不换通道绕 451。
"""

from __future__ import annotations

from CharTyr_MaiWork.maiwork.feeds import (
    _SCORE_FAILED_REASON,
    _SCORE_MISSING_REASON,
    _normalize_url,
    _rejection_has_no_content_verdict,
    _rejection_is_quota,
)
from CharTyr_MaiWork.maiwork.models import ModelError

from fakes import FakeModelsQueue
from test_feeds_freshness import GID, _FOCUS_JSON, _make_feeds, _ok_report, _run
from test_feeds_quality import NOW, _TimePatch
from test_feeds_two_phase import _candidate

# 别群（群隔离：别群的拒绝不许影响本群）
OTHER_GID = "222"
_451 = '端点返回 451：{"type":"censorship_blocked","message":"blocked by content policy"}'

_CONTENT_REASON = "摘要在原文找不到依据（不扎实）"
_QUOTA_REASON = "文章这轮已经留了 2 篇，宁缺毋滥，留分高的"


# ----------------------------------------------------------------------
# 规则函数：没有内容判定 vs 内容类拒绝
# ----------------------------------------------------------------------


def test_rule_recognizes_unscored_score_rejections() -> None:
    """两句「没评上」话术（score 闸）→ 算「没有内容判定」。"""
    assert _rejection_has_no_content_verdict("score", _SCORE_FAILED_REASON) is True
    assert _rejection_has_no_content_verdict("score", _SCORE_MISSING_REASON) is True


def test_rule_does_not_let_content_or_unknown_reasons_off() -> None:
    """内容类真拒绝照旧挡；score 闸下的**未知话术**也照旧挡（不是一刀切跳过 score 闸）。"""
    blocked = [
        ("hard", _CONTENT_REASON),
        ("hard", "原文没打开过/打不开"),
        ("hard", "原文要登录/付费"),
        ("hard", "垃圾：标题党/软文/营销号/纯情绪"),
        ("hard", "旧闻：9 天前发的"),
        ("hard", "和最近出过的重复（同一个链接）"),
        ("web", "相关度 2.5 < 3.0，过不了上网页这道"),
        ("web", "平均分 2.1 < 3.0，过不了上网页这道"),
        # 将来 score 闸若出现真正的内容判定：认不出 → 仍然当内容类（宁多挡不误放）
        ("score", "五项分都太低，不上"),
        ("score", ""),
        ("", _SCORE_FAILED_REASON),   # gate 不是 score：不放行
        (None, None),
    ]
    for gate, reason in blocked:
        assert _rejection_has_no_content_verdict(gate, reason) is False, (gate, reason)
    # 名额类仍由原来那个函数管，两个判定互不吞并
    assert _rejection_is_quota("web", _QUOTA_REASON) is True
    assert _rejection_has_no_content_verdict("web", _QUOTA_REASON) is False


# ----------------------------------------------------------------------
# 小工具：往库里埋一条 lookback 窗口内、rejected=1 的行
# ----------------------------------------------------------------------


def _seed_rejected(store, url: str, *, reason: str, gate: str = "hard",
                   title: str = "被拒过的", when: float = NOW - 86400,
                   gid: str = GID) -> None:
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
            " VALUES (?, ?, 1, 0, 1, '', ?)",
            (gid, when, when),
        )
        conn.execute(
            "INSERT INTO news_items (batch_id, group_id, icon, title, summary, why, sources,"
            " url_key, published_ts, score, status_kind, status_at, replies, expires_ts,"
            " up, down, created, kind, scores, topic, sensitive, profile_ref, rejected,"
            " reject_gate, reject_reason, angle, image_url, bridge)"
            " VALUES (?, ?, 'newspaper', ?, 's', '', '[]', ?, ?, 4, 'new', NULL, 0,"
            " NULL, 0, 0, ?, 'news', '', '', 0, '', 1, ?, ?, '', '', '')",
            (int(cur.lastrowid), gid, title, _normalize_url(url), when - 3600,
             when, gate, reason),
        )


# ----------------------------------------------------------------------
# 去重名单：没评过的放行，内容被拒的照挡；按群隔离
# ----------------------------------------------------------------------


def test_recent_rejected_keys_excludes_unscored_keeps_content(tmp_path) -> None:
    store, settings, feeds, *_ = _make_feeds(tmp_path)
    with _TimePatch():
        _seed_rejected(store, "https://a.example/unscored", reason=_SCORE_FAILED_REASON, gate="score")
        _seed_rejected(store, "https://a.example/unscored2", reason=_SCORE_MISSING_REASON, gate="score")
        _seed_rejected(store, "https://a.example/bad-content", reason=_CONTENT_REASON)
        keys = feeds._recent_rejected_keys(GID, settings)
    assert keys == {_normalize_url("https://a.example/bad-content")}


def test_recent_rejected_keys_is_per_group(tmp_path) -> None:
    """别群的内容否决不许影响本群：名单只查本群的行。"""
    store, settings, feeds, *_ = _make_feeds(tmp_path)
    url = "https://a.example/shared"
    with _TimePatch():
        _seed_rejected(store, url, reason=_CONTENT_REASON, gid=OTHER_GID)
        assert feeds._recent_rejected_keys(GID, settings) == set()
        assert feeds._recent_rejected_keys(OTHER_GID, settings) == {_normalize_url(url)}


def test_prefilter_keeps_unscored_but_drops_content_rejected(tmp_path) -> None:
    store, settings, feeds, *_ = _make_feeds(tmp_path)
    with _TimePatch():
        _seed_rejected(store, "https://v.example/never-scored", reason=_SCORE_FAILED_REASON, gate="score")
        _seed_rejected(store, "https://v.example/dead", reason=_CONTENT_REASON)
        kept, drops, _ = feeds._prefilter(GID, settings, [
            _candidate("https://v.example/never-scored", title="没评上过的又来一轮", published=NOW - 100),
            _candidate("https://v.example/dead", title="真被内容拒过的", published=NOW - 100),
        ])
    assert [c["url"] for c in kept] == ["https://v.example/never-scored"]
    assert len(drops) == 1 and "dead" in drops[0][0]


def test_dup_url_key_check_ignores_unscored_but_blocks_content(tmp_path) -> None:
    """核验换链接后的即时判重（同一个集合）：没评过的不挡，内容被拒的照挡。"""
    store, settings, feeds, *_ = _make_feeds(tmp_path)
    with _TimePatch():
        _seed_rejected(store, "https://d.example/unscored", reason=_SCORE_FAILED_REASON, gate="score")
        _seed_rejected(store, "https://d.example/dead", reason=_CONTENT_REASON)
        check = feeds._dup_url_key_check(GID, settings)
    assert check(_normalize_url("https://d.example/unscored")) is False
    assert check(_normalize_url("https://d.example/dead")) is True


# ----------------------------------------------------------------------
# 全链路：整轮打分失败留痕之后，同一批链接下一轮还要能正常进候选
# ----------------------------------------------------------------------


def _one_candidate_round_with_451(tmp_path) -> tuple:
    """真的跑一轮 prepare_news：一条候选、打分被 451 拦死 → 留痕、0 收录。"""
    from test_feeds_freshness import FakeWorkers

    item = {"title": "某条从来没被评过的资讯", "url": "https://site0.example.com/a",
            "summary": "摘要：两三句话讲清楚这件事。", "kind": "news",
            "published": 1_790_000_000.0 - 3600, "fetched": True, "quote": "原文依据",
            "paywall": False}
    models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, ModelError(_451)])
    store, settings, feeds, models, _workers, _topics, _profiles = _make_feeds(
        tmp_path, models=models, workers=FakeWorkers(_ok_report({"items": [item]})))
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
    return store, settings, feeds, item, got, models


def test_failure_round_keeps_audit_but_not_a_content_blacklist(tmp_path) -> None:
    """留痕行还在（可审计），但它不算内容否决：下一轮粗筛/即时判重不许误拒这批链接。"""
    store, settings, feeds, item, got, models = _one_candidate_round_with_451(tmp_path)
    assert got == 0

    # ① 留痕与批次审计都在
    rows = store.read().execute(
        "SELECT url_key, reject_gate, reject_reason FROM news_items WHERE group_id=?", (GID,)
    ).fetchall()
    assert rows and rows[0]["reject_gate"] == "score"
    assert "打分没做完" in rows[0]["reject_reason"]
    batch = store.read().execute(
        "SELECT found, kept, skipped, note FROM news_batches WHERE group_id=?", (GID,)
    ).fetchone()
    assert (batch["found"], batch["kept"], batch["skipped"]) == (1, 0, 1)
    assert "451" in batch["note"]

    # ② 这条没评上的链接不进「内容被拒过」名单
    with _TimePatch():
        assert _normalize_url(item["url"]) not in feeds._recent_rejected_keys(GID, settings)

        # ③ 下一轮粗筛正常收进来，理由不是「以前拒过」
        kept, drops, _ = feeds._prefilter(GID, settings, [
            _candidate(item["url"], title="同一篇好文下一轮又搜到", published=NOW - 100),
        ])
        assert [c["url"] for c in kept] == [item["url"]], f"被误当成内容否决：{drops}"

        # ④ 核验换链接后的即时判重也不挡
        assert feeds._dup_url_key_check(GID, settings)(_normalize_url(item["url"])) is False

    # ⑤ 这一轮没有多花的调用（只有定关注点 / 挑 / 打分那次失败），也没有绕 451 的二次尝试
    assert [str(kw.get("purpose") or "") for _r, _m, kw in models.calls] == [
        "feeds.focus", "feeds.pick", "feeds.score",
    ]


def test_rss_merge_lets_unscored_back_in(tmp_path) -> None:
    """RSS 并池走同一份名单：没评过的链接下一轮照常并进来。"""
    store, settings, feeds, *_ = _make_feeds(tmp_path)
    url = "https://rss.example/never-scored"
    with _TimePatch():
        _seed_rejected(store, url, reason=_SCORE_FAILED_REASON, gate="score")
        rss_items = [{
            "title": "没评上过的文章", "url": url, "summary": "s", "kind": "news",
            "published_ts": NOW - 3600, "fetched": True, "quote": "q", "paywall": False,
            "image_url": "", "url_key": _normalize_url(url), "site": "rss.example",
            "from_rss": True, "_rss_feed": "https://rss.example/feed",
        }]
        candidates: list = []
        merged = feeds._merge_rss_candidates(GID, settings, candidates, rss_items)
    assert merged == 1 and [c["url"] for c in candidates] == [url]
