"""被拒过的链接别再回来挨筛（2026-09-30 线上实测后调整）。

线上实录：轮询 RSS 源（触乐 / 游研社 / 机核，src_query 为空）的同几篇文章，
每一轮都被重新并进来、重新打分、再被同一道理由拒一次
（例：「触乐怪话：游戏排队心理学」在批次 48、49、52 连续被拒）。
`_stored_url_keys` 只看 rejected=0，被拒过的链接没人记得。

修法：
- lookback 窗口内、这个群**因内容被拒过**的 url_key（rejected=1），后面几轮
  直接跳过（不再并进来、不再打分）——在 RSS 并候选池（_merge_rss_candidates）
  和两阶段粗筛（_prefilter）两处都拦；
- 例外：纯名额 / 配额类淘汰（只怪这轮没位子，不怪内容——「文章这轮已经留了 N 篇」、
  「和这轮另一条…是同一件事，留分高的」这类）**不**挡后面的轮次；
- 判定走一个小的显式规则函数 _rejection_is_quota(gate, reason)，只靠
  reject_reason 的固定话术区分（写死的生产话术），不再散在多处。
"""

from __future__ import annotations

from test_feeds_quality import GID, NOW, _TimePatch
from test_feeds_two_phase import _candidate, _ready_two_phase_feeds

from CharTyr_MaiWork.maiwork.feeds import _rejection_is_quota, _normalize_url


# ----------------------------------------------------------------------
# 规则函数：纯名额 / 配额类淘汰 vs 内容类淘汰
# ----------------------------------------------------------------------


def test_quota_rule_recognizes_round_quota_rejections() -> None:
    """「这轮没位子」的理由 → 不挡后面的轮次（下轮碰到同一链接照样能再评）。"""
    quota_reasons = [
        "文章这轮已经留了 2 篇，宁缺毋滥，留分高的",
        "同一件事的后续这轮已经留了一条，留分高的",
        "和这轮另一条「某某标题」是同一件事，留分高的",
        "同一个话题这轮已经留了两条，留分高的",
        "同个来源这轮已经留了三条，留分高的",
        "争议话题这轮已有一条，留分高的",
        "不同角度的这轮已经留了两条，留分高的",
        "超出本轮上限（最多 10 条）",
        "相关度 2.5 < 3.0，这轮拓展名额（1 个）已经用完",
        "「游戏排队」最近三天已经发了 3 条，换换别的",
    ]
    for reason in quota_reasons:
        assert _rejection_is_quota("web", reason) is True, reason
    # 和 gate 无关（万一将来 hard 这道也出现名额类话术，同样放过）
    assert _rejection_is_quota("hard", "和这轮另一条「某」是同一件事，留分高的") is True


def test_quota_rule_does_not_let_content_rejections_off() -> None:
    """内容类淘汰（打不开 / 垃圾 / 不扎实 / 旧闻 / 相关度不够 / 政府稿……）→ 挡后面的轮次。"""
    content_reasons = [
        ("hard", "原文没打开过/打不开"),
        ("hard", "原文要登录/付费"),
        ("hard", "来源在屏蔽名单里"),
        ("hard", "这个来源被标没用太多次"),
        ("hard", "和最近出过的重复（同一个链接）"),
        ("hard", "和最近出过的是同一件事（重复）"),
        ("hard", "摘要在原文找不到依据（不扎实）"),
        ("hard", "垃圾：标题党/软文/营销号/纯情绪"),
        ("hard", "文章没有发布时间，宁缺毋滥不收"),
        ("hard", "文章太旧：200 天前发的"),
        ("hard", "旧闻：9 天前发的"),
        ("hard", "政府通讯稿，和群无关"),
        ("web", "相关度 2.5 < 3.0，过不了上网页这道"),
        ("web", "平均分 2.1 < 3.0，过不了上网页这道"),
        ("web", "不够新（新鲜度 2.0）"),
        ("web", "群里已经聊过这件事（群友大概已经知道），不上"),
        ("web", "相关度 2.5 < 4.0（文章从严），不上"),
        ("web", "信息量 3.0 不够（文章从严），不上"),
    ]
    for gate, reason in content_reasons:
        assert _rejection_is_quota(gate, reason) is False, (gate, reason)
    # 空 / 怪值一律按「内容类」处理（宁多挡不误放）
    assert _rejection_is_quota("", "") is False
    assert _rejection_is_quota("web", "") is False
    assert _rejection_is_quota(None, None) is False


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------


def _seed_rejected(store, url: str, *, reason: str, gate: str = "hard",
                   title: str = "被拒过的", when: float = NOW - 86400) -> None:
    """给这个群埋一条 lookback 窗口内、rejected=1 的资讯行。"""
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
            " VALUES (?, ?, 1, 0, 1, '', ?)",
            (GID, when, when),
        )
        conn.execute(
            "INSERT INTO news_items (batch_id, group_id, icon, title, summary, why, sources,"
            " url_key, published_ts, score, status_kind, status_at, replies, expires_ts,"
            " up, down, created, kind, scores, topic, sensitive, profile_ref, rejected,"
            " reject_gate, reject_reason, angle, image_url, bridge)"
            " VALUES (?, ?, 'newspaper', ?, 's', '', '[]', ?, ?, 4, 'new', NULL, 0,"
            " NULL, 0, 0, ?, 'news', '', '', 0, '', 1, ?, ?, '', '', '')",
            (int(cur.lastrowid), GID, title, _normalize_url(url), when - 3600,
             when, gate, reason),
        )


# ----------------------------------------------------------------------
# RSS 并候选池：被拒过的链接别再并进来；名额类淘汰不挡
# ----------------------------------------------------------------------


def test_rss_merge_skips_recently_content_rejected(tmp_path) -> None:
    """lookback 内内容类被拒的 url_key：这轮不再并进来（不重打分、不重拒一次）。"""
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    with _TimePatch():
        _seed_rejected(store, "https://chuapp.example/queue-psy", title="触乐怪话：游戏排队心理学",
                       reason="摘要在原文找不到依据（不扎实）")
        rss_items = [
            {
                "title": "触乐怪话：游戏排队心理学", "url": "https://chuapp.example/queue-psy",
                "summary": "s", "kind": "news", "published_ts": NOW - 3600,
                "fetched": True, "quote": "q", "paywall": False, "image_url": "",
                "url_key": _normalize_url("https://chuapp.example/queue-psy"),
                "site": "chuapp.example", "from_rss": True, "_rss_feed": "https://chuapp.example/feed",
            },
            {
                "title": "一篇完全新鲜的文章", "url": "https://chuapp.example/fresh",
                "summary": "s", "kind": "news", "published_ts": NOW - 3600,
                "fetched": True, "quote": "q", "paywall": False, "image_url": "",
                "url_key": _normalize_url("https://chuapp.example/fresh"),
                "site": "chuapp.example", "from_rss": True, "_rss_feed": "https://chuapp.example/feed",
            },
        ]
        candidates: list = []
        merged = feeds._merge_rss_candidates(GID, settings, candidates, rss_items)
    assert merged == 1
    assert [c["url"] for c in candidates] == ["https://chuapp.example/fresh"]


def test_rss_merge_lets_quota_rejected_back_in(tmp_path) -> None:
    """名额类淘汰（「文章这轮已经留了 2 篇…留分高的」）：不挡，下轮还能进来再评。"""
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    with _TimePatch():
        _seed_rejected(store, "https://gcores.example/deep-dive", title="机核深度长文",
                       gate="web", reason="文章这轮已经留了 2 篇，宁缺毋滥，留分高的")
        rss_items = [
            {
                "title": "机核深度长文", "url": "https://gcores.example/deep-dive",
                "summary": "s", "kind": "guide", "published_ts": NOW - 3600,
                "fetched": True, "quote": "q", "paywall": False, "image_url": "",
                "url_key": _normalize_url("https://gcores.example/deep-dive"),
                "site": "gcores.example", "from_rss": True, "_rss_feed": "https://gcores.example/feed",
            },
        ]
        candidates: list = []
        merged = feeds._merge_rss_candidates(GID, settings, candidates, rss_items)
    assert merged == 1
    assert candidates and candidates[0]["url"] == "https://gcores.example/deep-dive"


def test_rss_merge_ignores_rejections_outside_lookback(tmp_path) -> None:
    """超出 lookback 窗口的旧拒绝不挡：许久以前的被拒链接算新候选。"""
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    with _TimePatch():
        _seed_rejected(store, "https://chuapp.example/ancient", title="上古一条",
                       reason="摘要在原文找不到依据（不扎实）",
                       when=NOW - 30 * 86400)
        rss_items = [
            {
                "title": "上古一条", "url": "https://chuapp.example/ancient",
                "summary": "s", "kind": "news", "published_ts": NOW - 3600,
                "fetched": True, "quote": "q", "paywall": False, "image_url": "",
                "url_key": _normalize_url("https://chuapp.example/ancient"),
                "site": "chuapp.example", "from_rss": True, "_rss_feed": "https://chuapp.example/th",
            },
        ]
        candidates: list = []
        merged = feeds._merge_rss_candidates(GID, settings, candidates, rss_items)
    assert merged == 1


# ----------------------------------------------------------------------
# 两阶段粗筛：被拒过的链接直接丢（带清楚的漏斗理由），名额类不挡
# ----------------------------------------------------------------------


def test_prefilter_drops_recently_content_rejected(tmp_path) -> None:
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    with _TimePatch():
        _seed_rejected(store, "https://v.example/dead-thing", title="陈年跑分",
                       reason="垃圾：标题党/软文/营销号/纯情绪")
        kept, drops, _ = feeds._prefilter(GID, settings, [
            _candidate("https://v.example/dead-thing", title="陈年跑分（重发）", published=NOW - 100),
            _candidate("https://v.example/good-one", title="新出炉的冷门好文", published=NOW - 100),
        ])
    assert [c["url"] for c in kept] == ["https://v.example/good-one"]
    # 漏斗理由要说清楚：不是第一次筛，是「以前筛掉过」
    assert len(drops) == 1
    assert "筛掉" in drops[0][1] or "拒过" in drops[0][1] or "之前" in drops[0][1]


def test_prefilter_keeps_quota_rejected(tmp_path) -> None:
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    with _TimePatch():
        _seed_rejected(store, "https://v.example/back-again", title="上次名额不够",
                       gate="web", reason="和这轮另一条「另一篇」是同一件事，留分高的")
        kept, drops, _ = feeds._prefilter(GID, settings, [
            _candidate("https://v.example/back-again", title="上次名额不够的又来了", published=NOW - 100),
        ])
    assert [c["url"] for c in kept] == ["https://v.example/back-again"]
    assert drops == []


# ----------------------------------------------------------------------
# 旧的已入库（rejected=0）去重不受影响（回归：别因为新规则把原来的入口搞松了）
# ----------------------------------------------------------------------


def test_stored_url_keys_still_block_published_dupes(tmp_path) -> None:
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    with _TimePatch():
        # rejected=0（上过网页）的同链接照旧挡
        with store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                " VALUES (?, ?, 1, 1, 0, '', ?)",
                (GID, NOW - 86400, NOW - 86400),
            )
            conn.execute(
                "INSERT INTO news_items (batch_id, group_id, icon, title, summary, why, sources,"
                " url_key, published_ts, score, status_kind, status_at, replies, expires_ts,"
                " up, down, created, kind, scores, topic, sensitive, profile_ref, rejected,"
                " reject_gate, reject_reason, angle, image_url, bridge)"
                " VALUES (?, ?, 'newspaper', '上过网页的', 's', '', '[]', ?, ?, 4, 'pool', NULL, 0,"
                " NULL, 0, 0, ?, 'news', '', '', 0, '', 0, NULL, NULL, '', '', '')",
                (int(cur.lastrowid), GID, _normalize_url("https://seen.example/p"), NOW - 86400, NOW - 86400),
            )
        rss_items = [
            {
                "title": "上过网页的", "url": "https://seen.example/p",
                "summary": "s", "kind": "news", "published_ts": NOW - 3600,
                "fetched": True, "quote": "q", "paywall": False, "image_url": "",
                "url_key": _normalize_url("https://seen.example/p"),
                "site": "seen.example", "from_rss": True, "_rss_feed": "https://seen.example/feed",
            },
        ]
        assert feeds._merge_rss_candidates(GID, settings, [], rss_items) == 0
