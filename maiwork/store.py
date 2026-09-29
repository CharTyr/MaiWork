"""SQLite 存储：连接、迁移、事务、事件、kv、密钥。

- 单写入者：进程内一把 threading.RLock，事务用 BEGIN IMMEDIATE。
- 事件和状态更新在同一事务里提交。
- 同步 API（库很小，单次操作要短，调用在事件循环里直接执行）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import clock

# M1 表结构（字段以这里为准，要点见 docs/07-代码接口.md §4）
_M1_SQL = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS secrets (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    group_id TEXT NOT NULL DEFAULT '',
    entity TEXT NOT NULL DEFAULT '',
    entity_id TEXT NOT NULL DEFAULT '',
    payload TEXT,
    v INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS groups (
    group_id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    member_count INTEGER NOT NULL DEFAULT 0,
    token TEXT UNIQUE,
    created REAL NOT NULL DEFAULT 0,
    last_msg_ts REAL NOT NULL DEFAULT 0,
    cursor_ts REAL NOT NULL DEFAULT 0,
    cursor_ids TEXT NOT NULL DEFAULT '[]',
    read_since REAL NOT NULL DEFAULT 0,
    profile_ready_ts REAL NOT NULL DEFAULT 0,
    last_refresh_ts REAL NOT NULL DEFAULT 0,
    last_weekly_ts REAL NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS profile_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    category TEXT NOT NULL,
    text TEXT NOT NULL,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    evidence TEXT NOT NULL DEFAULT '[]',
    first_ts REAL NOT NULL DEFAULT 0,
    last_ts REAL NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0,
    locked INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    updated REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS member_activity (
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (group_id, user_id, day)
);
CREATE TABLE IF NOT EXISTS activity_bins (
    group_id TEXT NOT NULL,
    bin_ts REAL NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, bin_ts)
);
CREATE TABLE IF NOT EXISTS focus_members (
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    reasons TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0,
    removed INTEGER NOT NULL DEFAULT 0,
    updated REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, user_id)
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    day TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT '',
    group_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 1,
    ms INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
"""


def _m1(conn: sqlite3.Connection) -> None:
    conn.executescript(_M1_SQL)


# profile.py 第一部分（群活跃统计）新增：bot_messages / member_interactions 两张表，
# groups 加 pending_count（攒批计数）和 info_ts（群信息最后更新时间）两列。
_M_PROFILE_SQL = """
CREATE TABLE IF NOT EXISTS bot_messages (
    group_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (group_id, message_id)
);
CREATE TABLE IF NOT EXISTS member_interactions (
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, user_id, day)
);
"""


def _m_profile(conn: sqlite3.Connection) -> None:
    conn.executescript(_M_PROFILE_SQL)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(groups)")}
    if "pending_count" not in cols:
        conn.execute(
            "ALTER TABLE groups ADD COLUMN pending_count INTEGER NOT NULL DEFAULT 0"
        )
    if "info_ts" not in cols:
        conn.execute("ALTER TABLE groups ADD COLUMN info_ts REAL NOT NULL DEFAULT 0")


# M2：资讯、构想、开话题、可提起清单、Jev 判断、工具调用（docs/07 §10.8）
_M2_SQL = """
CREATE TABLE IF NOT EXISTS judgments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    day TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT '',
    group_id TEXT NOT NULL DEFAULT '',
    state_summary TEXT NOT NULL DEFAULT '',
    answers TEXT NOT NULL DEFAULT '{}',
    ms INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 1,
    error TEXT NOT NULL DEFAULT '',
    verdict TEXT
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    group_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    tool TEXT NOT NULL,
    input TEXT NOT NULL DEFAULT '',
    output TEXT NOT NULL DEFAULT '',
    ms INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 1,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_task ON tool_calls(task_id, ts);
CREATE TABLE IF NOT EXISTS news_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    slot_ts REAL NOT NULL,
    found INTEGER NOT NULL DEFAULT 0,
    kept INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS news_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    group_id TEXT NOT NULL,
    icon TEXT NOT NULL DEFAULT 'newspaper',
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    why TEXT NOT NULL DEFAULT '',
    sources TEXT NOT NULL DEFAULT '[]',
    url_key TEXT NOT NULL DEFAULT '',
    published_ts REAL,
    score REAL NOT NULL DEFAULT 0,
    status_kind TEXT NOT NULL DEFAULT 'new',
    status_at REAL,
    replies INTEGER NOT NULL DEFAULT 0,
    expires_ts REAL,
    up INTEGER NOT NULL DEFAULT 0,
    down INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_news_items_group ON news_items(group_id, created);
CREATE TABLE IF NOT EXISTS ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    icon TEXT NOT NULL DEFAULT 'bulb',
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    basis TEXT NOT NULL DEFAULT '',
    step TEXT NOT NULL DEFAULT '',
    effort TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'new',
    requested_by TEXT,
    task_id TEXT,
    up INTEGER NOT NULL DEFAULT 0,
    down INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL DEFAULT 0,
    updated REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS topic_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref_id INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL,
    brief TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '',
    expires_ts REAL NOT NULL,
    used_ts REAL,
    created REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS topic_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    ts REAL NOT NULL,
    quiet_s REAL NOT NULL DEFAULT 0,
    usual_gap_s REAL,
    jev TEXT,
    pick TEXT,
    candidate_id INTEGER,
    opener TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    followup_due_ts REAL,
    result TEXT,
    verdict TEXT
);
CREATE TABLE IF NOT EXISTS mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    key TEXT NOT NULL,
    text TEXT NOT NULL,
    expires_ts REAL NOT NULL,
    turns_left INTEGER NOT NULL DEFAULT 5,
    created REAL NOT NULL DEFAULT 0,
    UNIQUE (group_id, key)
);
CREATE TABLE IF NOT EXISTS pushes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    ts REAL NOT NULL,
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT ''
);
"""


def _m2(conn: sqlite3.Connection) -> None:
    conn.executescript(_M2_SQL)


# M3：待批请求、任务与尝试、目标、发件箱、故障报错去重（docs/07 §11）
_M3_SQL = """
CREATE TABLE IF NOT EXISTS seq (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'task',
    title TEXT NOT NULL,
    quote TEXT NOT NULL DEFAULT '',
    via TEXT NOT NULL DEFAULT '',
    icon TEXT NOT NULL DEFAULT 'magnifier',
    requester_id TEXT NOT NULL DEFAULT '',
    requester_name TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    idea_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    decided_by TEXT NOT NULL DEFAULT '',
    decided_ts REAL,
    reminded_ts REAL,
    task_id TEXT,
    goal_id TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    workspace TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    request_id TEXT,
    goal_id TEXT,
    requester_id TEXT NOT NULL DEFAULT '',
    requester_name TEXT NOT NULL DEFAULT '',
    icon TEXT NOT NULL DEFAULT 'package',
    title TEXT NOT NULL,
    req TEXT NOT NULL DEFAULT '',
    req_version INTEGER NOT NULL DEFAULT 1,
    criteria TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    env TEXT NOT NULL DEFAULT '',
    review TEXT NOT NULL DEFAULT '',
    question TEXT,
    question_ts REAL,
    question_msg_id TEXT,
    delivery_kind TEXT NOT NULL DEFAULT '',
    delivery TEXT NOT NULL DEFAULT '[]',
    undelivered INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    started_ts REAL,
    finished_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_group ON tasks(group_id, updated);
CREATE TABLE IF NOT EXISTS task_versions (
    task_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    req TEXT NOT NULL,
    criteria TEXT NOT NULL DEFAULT '[]',
    ts REAL NOT NULL,
    PRIMARY KEY (task_id, version)
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    n INTEGER NOT NULL,
    req_version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    summary TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '[]',
    artifacts TEXT NOT NULL DEFAULT '[]',
    review TEXT NOT NULL DEFAULT '',
    started REAL NOT NULL,
    finished REAL
);
CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    icon TEXT NOT NULL DEFAULT 'bullseye',
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    who_id TEXT NOT NULL DEFAULT '',
    who_name TEXT NOT NULL DEFAULT '',
    by_text TEXT NOT NULL DEFAULT '',
    criteria TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'active',
    due_ts REAL,
    remind_ts REAL,
    repeat TEXT,
    until_ts REAL,
    next_check_ts REAL,
    last_ts REAL,
    last_text TEXT NOT NULL DEFAULT '',
    task_id TEXT,
    request_id TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    group_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    result TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    task_id TEXT,
    not_before REAL NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS error_reports (
    group_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (group_id, fingerprint)
);
"""


def _m3(conn: sqlite3.Connection) -> None:
    conn.executescript(_M3_SQL)


# 关注成员个人画像（persona.py；docs/07 §8.2）：focus_messages 存「关注成员说过的
# 原话」做素材（只管理员可见，privacy.scrub 的片段来源之一）；focus_members 加
# persona（JSON）/ persona_ts（最后刷新时间）两列。
_M_PERSONA_SQL = """
CREATE TABLE IF NOT EXISTS focus_messages (
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    ts REAL NOT NULL,
    message_id TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (group_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_focus_messages_user ON focus_messages(group_id, user_id, ts);
"""


def _m_persona(conn: sqlite3.Connection) -> None:
    conn.executescript(_M_PERSONA_SQL)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(focus_members)")}
    if "persona" not in cols:
        conn.execute("ALTER TABLE focus_members ADD COLUMN persona TEXT NOT NULL DEFAULT ''")
    if "persona_ts" not in cols:
        conn.execute("ALTER TABLE focus_members ADD COLUMN persona_ts REAL")


def next_id(conn: sqlite3.Connection, prefix: str) -> str:
    """在调用方事务里取下一个编号，如 next_id(conn, "T") -> "T-1"。"""
    conn.execute("INSERT OR IGNORE INTO seq(name, value) VALUES (?, 0)", (prefix,))
    conn.execute("UPDATE seq SET value = value + 1 WHERE name=?", (prefix,))
    n = conn.execute("SELECT value FROM seq WHERE name=?", (prefix,)).fetchone()[0]
    return f"{prefix}-{n}"


# 资讯「质量标准」（docs/02-设计.md §4.1，2026-09-27 与用户定）：news_items 加
# kind（news/guide）、scores（五项+avg 的 JSON）、topic、sensitive、profile_ref、
# rejected / reject_gate('hard'|'web') / reject_reason——被筛掉的也入库，
# 管理员在网页「被筛掉的」一栏能看到卡在哪一道、为什么、各多少分。
def _m_quality(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(news_items)")}

    def _add(name: str, ddl: str) -> None:
        if name not in cols:
            conn.execute(f"ALTER TABLE news_items ADD COLUMN {ddl}")

    _add("kind", "kind TEXT NOT NULL DEFAULT 'news'")
    _add("scores", "scores TEXT NOT NULL DEFAULT ''")
    _add("topic", "topic TEXT NOT NULL DEFAULT ''")
    _add("sensitive", "sensitive INTEGER NOT NULL DEFAULT 0")
    _add("profile_ref", "profile_ref TEXT NOT NULL DEFAULT ''")
    _add("rejected", "rejected INTEGER NOT NULL DEFAULT 0")
    _add("reject_gate", "reject_gate TEXT")
    _add("reject_reason", "reject_reason TEXT")


# 「有人味」的资讯（docs/02-设计.md §4.1「写法」，2026-09-27 与用户定）：
# - news_items：body（按 MaiBot 口吻写、可带内联链接的正文）、reason（我发这条的原因）、
#   refs（群里什么时候聊过：[{ts, who, user_id, text, message_id}]，user_id 用来查当前名）、
#   audience（谁可能需要：[{"user_id","name"}]，认人靠 user_id；老行是 [名字]）、
#   image_url（原文封面图）、keywords（给 MaiBot 接话题用的关键词）、verify（railway.new 实测结果 JSON）、
#   chat_votes（群友点「想在群里聊」的次数）、angle（'diverse' = 刻意放进来的不同角度）
# - ideas：feasibility（可行性 JSON）、keywords
# - chat_log：服务群最近 14 天发言的只读副本（全文检索，trigram 支持中文），
#   用来找「什么时候聊过」和让 MaiBot 在群友聊起时接上对应内容
# - goals：heartbeat_ts / stale_reason（后台盯着的事定期报平安，卡住要标出来）
def _m_humane(conn: sqlite3.Connection) -> None:
    def _cols(table: str) -> set[str]:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    news = _cols("news_items")
    for name, ddl in (
        ("body", "body TEXT NOT NULL DEFAULT ''"),
        ("reason", "reason TEXT NOT NULL DEFAULT ''"),
        ("refs", "refs TEXT NOT NULL DEFAULT '[]'"),
        ("audience", "audience TEXT NOT NULL DEFAULT '[]'"),
        ("image_url", "image_url TEXT NOT NULL DEFAULT ''"),
        ("keywords", "keywords TEXT NOT NULL DEFAULT '[]'"),
        ("verify", "verify TEXT NOT NULL DEFAULT ''"),
        ("chat_votes", "chat_votes INTEGER NOT NULL DEFAULT 0"),
        ("angle", "angle TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in news:
            conn.execute(f"ALTER TABLE news_items ADD COLUMN {ddl}")
    ideas = _cols("ideas")
    for name, ddl in (
        ("feasibility", "feasibility TEXT NOT NULL DEFAULT ''"),
        ("keywords", "keywords TEXT NOT NULL DEFAULT '[]'"),
    ):
        if name not in ideas:
            conn.execute(f"ALTER TABLE ideas ADD COLUMN {ddl}")
    goals = _cols("goals")
    for name, ddl in (
        ("heartbeat_ts", "heartbeat_ts REAL"),
        ("stale_reason", "stale_reason TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in goals:
            conn.execute(f"ALTER TABLE goals ADD COLUMN {ddl}")
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS chat_log USING fts5("
        "text, group_id UNINDEXED, message_id UNINDEXED, ts UNINDEXED, "
        "user_id UNINDEXED, user_name UNINDEXED, tokenize='trigram')"
    )


# 关注成员的个人向产出（docs/02-设计.md §3.2；personal.py）：
# - news_items.target_user_id：非空 = 这条是「个人向资讯」，只给本人 + 管理员看，
#   绝不出现在群资讯视图 / 群友视图 / 话题候选池 / TopicMatcher 候选里（各处查询按
#   「target_user_id 为空」过滤）。
# - ideas.target_user_id：同上的「个人向构想」（0–1 条 / 轮）。
def _m_personal(conn: sqlite3.Connection) -> None:
    def _cols(table: str) -> set[str]:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    news = _cols("news_items")
    if "target_user_id" not in news:
        conn.execute(
            "ALTER TABLE news_items ADD COLUMN target_user_id TEXT NOT NULL DEFAULT ''"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_news_items_target"
            " ON news_items(group_id, target_user_id, created)"
        )
    ideas = _cols("ideas")
    if "target_user_id" not in ideas:
        conn.execute(
            "ALTER TABLE ideas ADD COLUMN target_user_id TEXT NOT NULL DEFAULT ''"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ideas_target"
            " ON ideas(group_id, target_user_id, created)"
        )


# 群空间「自有文件登记」（docs/02-设计.md §10，platforms/qq_onebot.py）：
# 防手滑——删除/改名/移动只动机器人自己上传的文件；outbox 上传成功时写一行。
def _m_group_space(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS group_files_owned (
        group_id TEXT NOT NULL,
        file_id TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        uploaded_ts REAL NOT NULL DEFAULT 0,
        task_id TEXT,
        PRIMARY KEY (group_id, file_id)
    );
    """)


# @ 慢路径落地（问题 B 修复，见 docs/02 §3.1/§5.1）：Jev 判不了的 @ 消息不再只攒
# 内存 + intake.slow 事件，写进 pending_asks；主模型读群提炼时把这批消息标出来请它判
# （asks），判过的标 handled。同一群同一条消息只留一行（后到的覆盖原因）。
def _m_pending_asks(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS pending_asks (
        group_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        user_id TEXT NOT NULL DEFAULT '',
        user_name TEXT NOT NULL DEFAULT '',
        text TEXT NOT NULL DEFAULT '',
        ts REAL NOT NULL DEFAULT 0,
        reason TEXT NOT NULL DEFAULT '',
        handled INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (group_id, message_id)
    );
    """)


# 最近模型请求日志（models.py 每次 chat 尝试写一行；管理员在网页 /api/logs/* 看，2026-10 新增）：
# - error 已遮密钥、截 1000；request/response 是 JSON（messages 内容截 4000、整份 request 截 80KB、
#   response.text 截 20000）；**密钥绝不入库**（不存 headers，只有 messages/tools/json_mode）。
# - status：HTTP 状态码；网络错误为 0。attempt：第几次尝试（从 1 起，重试递增）。
def _m_model_calls(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS model_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        purpose TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        group_id TEXT NOT NULL DEFAULT '',
        task_id TEXT NOT NULL DEFAULT '',
        attempt INTEGER NOT NULL DEFAULT 1,
        ok INTEGER NOT NULL DEFAULT 0,
        status INTEGER NOT NULL DEFAULT 0,
        ms INTEGER NOT NULL DEFAULT 0,
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '',
        request TEXT NOT NULL DEFAULT '{}',
        response TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_model_calls_ts ON model_calls(ts);
    CREATE INDEX IF NOT EXISTS idx_model_calls_group ON model_calls(group_id, ts);
    """)


# 管理员在网页上直接和 MaiWork 主模型对话（docs/02-设计.md「管理员对话」）：
# - admin_chats：一段对话（可聚焦某个服务群；archived=1 不出现在列表）；
# - admin_chat_msgs：消息流，role 取 user|assistant|tool|system_note；
#   assistant 的 tool_calls 存 JSON，tool 消息的 meta 存 {"ok","tool","label"}（前端直接显示 label）；
# - admin_chat_pending：要管理员点「确认/拒绝」的动作（发群消息、群空间写、删除类、高危规则）——
#   工具当时不执行，只写这张表；管理员点头后 admin_chat.confirm 调 tools_admin.PendingGate.execute 真正执行并回写 status/result。
def _m_admin_chat(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS admin_chats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL DEFAULT '',
        group_id TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL DEFAULT 0,
        updated REAL NOT NULL DEFAULT 0,
        archived INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_admin_chats_updated ON admin_chats(archived, updated);
    CREATE TABLE IF NOT EXISTS admin_chat_msgs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        ts REAL NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL DEFAULT '',
        tool_calls TEXT NOT NULL DEFAULT '',
        tool_call_id TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL DEFAULT '',
        meta TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_admin_chat_msgs_chat ON admin_chat_msgs(chat_id, id);
    CREATE TABLE IF NOT EXISTS admin_chat_pending (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        msg_id INTEGER NOT NULL DEFAULT 0,
        tool TEXT NOT NULL DEFAULT '',
        args TEXT NOT NULL DEFAULT '{}',
        summary TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        result TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL DEFAULT 0,
        decided REAL
    );
    CREATE INDEX IF NOT EXISTS idx_admin_chat_pending_chat ON admin_chat_pending(chat_id, status);
    """)


# 关注成员的平台资料缓存（card=群名片 / nickname=QQ 昵称，docs/06 的
# adapter.napcat.group.get_group_member_info）：focus_members 加两列 + 缓存时间。
# 网页视图只读这两列；6 小时过期，过期的由后台在 focus() 重算时顺手刷新。
def _m_focus_names(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(focus_members)")}
    if "card" not in cols:
        conn.execute("ALTER TABLE focus_members ADD COLUMN card TEXT NOT NULL DEFAULT ''")
    if "nickname" not in cols:
        conn.execute("ALTER TABLE focus_members ADD COLUMN nickname TEXT NOT NULL DEFAULT ''")
    if "profile_ts" not in cols:
        conn.execute("ALTER TABLE focus_members ADD COLUMN profile_ts REAL NOT NULL DEFAULT 0")


# 构想「包含的项目」+ 派活请求的来源与选中项目（2026-10）：
# - ideas.items：这条构想包含哪些项目，JSON `[{"kind":"task"|"goal","title","desc"}]`（最多 5 个）；
#   老的构想默认 `[]`（读取时按「没有项目」走老逻辑，不报错）；step/effort 两列留着不删，
#   只为读得动老数据，新构想不再生成。
# - requests.item_nos：从构想转来的请求里，群友点名要做的项目序号 JSON（`[]` = 全部项目）。
# - requests.source：请求来源标记（`""` = 群友 @ / 网页发起，`"maiwork"` = MaiWork 主动提议）。
def _m_idea_items(conn: sqlite3.Connection) -> None:
    def _cols(table: str) -> set[str]:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    ideas = _cols("ideas")
    if "items" not in ideas:
        conn.execute("ALTER TABLE ideas ADD COLUMN items TEXT NOT NULL DEFAULT '[]'")
    requests = _cols("requests")
    if "item_nos" not in requests:
        conn.execute("ALTER TABLE requests ADD COLUMN item_nos TEXT NOT NULL DEFAULT '[]'")
    if "source" not in requests:
        conn.execute("ALTER TABLE requests ADD COLUMN source TEXT NOT NULL DEFAULT ''")


# 派活「自动审核」（auto_review.py；docs/02 §5.2，2026-10）：requests 加两列——
# - force_manual：这条请求是不是**永远**要管理员批准（MaiWork 主动提的目标走这条；
#   自动审核一律绕过它）；
# - auto_reason：自动审核通过时主模型给的一句话理由（批准人是「MaiWork 自动审核」，
#   记在 decided_by 里）；人批 / 免批的行是空串。
def _m_auto_review(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(requests)")}
    if "force_manual" not in cols:
        conn.execute("ALTER TABLE requests ADD COLUMN force_manual INTEGER NOT NULL DEFAULT 0")
    if "auto_reason" not in cols:
        conn.execute("ALTER TABLE requests ADD COLUMN auto_reason TEXT NOT NULL DEFAULT ''")


# 一条请求落地出的**全部**任务（2026-10）：构想按项目拆成多个任务时，requests.task_id 只
# 记得住第一个，导致 auto_info_by_task 只给第一个任务批准人 / 自动审核理由。
# - requests.task_ids：JSON 数组 `["T-1","T-2"]`；老数据默认 `[]`（读取时回落 requests.task_id）。
def _m_landed_task_ids(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(requests)")}
    if "task_ids" not in cols:
        conn.execute("ALTER TABLE requests ADD COLUMN task_ids TEXT NOT NULL DEFAULT '[]'")


# 任务安全网（0.4.0，[tasks] token_limit / run_seconds）：tasks 加 paused_reason
# （JSON：{"kind": "tokens"|"time", "limit": N, "used": M}，null = 不是安全网停的）。
# 手动暂停没有它；网页 tasks.list / 详情都带，前端据它显示「自动暂停原因」。
def _m_task_nets(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    if "paused_reason" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN paused_reason TEXT")


# 群空间「自有文件夹登记」（插件中心审核整改 2026-10，platforms/qq_onebot.py）：
# 防手滑第二层——delete_folder 只能删机器人自己建、且里面只有机器人自己传的文件 /
# 自己建的子文件夹的文件夹。create_folder 成功后登记一行；拿不到 folder_id 时
# 只记日志不登记（最坏后果：这个文件夹以后不能删）。
def _m_group_folders(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS group_folders_owned (
        group_id TEXT NOT NULL,
        folder_id TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        created_ts REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (group_id, folder_id)
    );
    """)


# 成员名册（members.py）：按（群, 平台 id）记最新显示名；上线时从 member_activity 补一遍
def _m_members(conn: sqlite3.Connection) -> None:
    from . import members

    conn.executescript(members.SCHEMA_SQL)
    members.seed_from_activity(conn)


# 名册跟 QQ 对名字（members.refresh_from_host）：members 加 checked_ts（上次问 QQ 的时间）
def _m_member_checked(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(members)")}
    if "checked_ts" not in cols:
        conn.execute("ALTER TABLE members ADD COLUMN checked_ts REAL NOT NULL DEFAULT 0")


# 资讯评价（news_rating.py，2026-09-29）：取代「想在群里聊」气泡
def _m_news_ratings(conn: sqlite3.Connection) -> None:
    from . import news_rating

    conn.executescript(news_rating.SCHEMA_SQL)


# 资讯图解（news_viz.py，2026-09-29）
def _m_news_viz(conn: sqlite3.Connection) -> None:
    from . import news_viz

    conn.executescript(news_viz.SCHEMA_SQL)


# 资讯卡片 / 构想提一嘴（card_push.py）：每批资讯至多一张卡片（batch_id 唯一），
# 每个构想至多提一次（idea_id 唯一）；status pending/sending/sent/failed/uncertain/dropped
def _m_card_push(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS news_cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id TEXT NOT NULL,
        batch_id INTEGER NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'pending',
        item_ids TEXT NOT NULL DEFAULT '[]',
        created REAL NOT NULL DEFAULT 0,
        due_ts REAL NOT NULL DEFAULT 0,
        sent_ts REAL,
        attempts INTEGER NOT NULL DEFAULT 0,
        mode TEXT NOT NULL DEFAULT '',
        message_id TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_news_cards_group ON news_cards(group_id, status);
    CREATE TABLE IF NOT EXISTS idea_mentions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id TEXT NOT NULL,
        idea_id INTEGER NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'pending',
        text TEXT NOT NULL DEFAULT '',
        at_user TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL DEFAULT 0,
        due_ts REAL NOT NULL DEFAULT 0,
        sent_ts REAL,
        attempts INTEGER NOT NULL DEFAULT 0,
        message_id TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_idea_mentions_group ON idea_mentions(group_id, status);
    """)


# 迁移是有序列表，每步一个函数；新阶段只能往后加，不改旧的
_MIGRATIONS = [_m1, _m_profile, _m2, _m3, _m_persona, _m_quality, _m_humane, _m_personal, _m_group_space, _m_pending_asks, _m_model_calls, _m_admin_chat, _m_focus_names, _m_idea_items, _m_auto_review, _m_landed_task_ids, _m_task_nets, _m_group_folders, _m_members, _m_card_push, _m_member_checked, _m_news_ratings, _m_news_viz]


class Store:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._chmod(self._path.parent, 0o700)
        self._conn = sqlite3.connect(str(self._path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._chmod(self._path, 0o600)
        self._lock = threading.RLock()

    @staticmethod
    def _chmod(p: Path, mode: int) -> None:
        try:
            os.chmod(p, stat.S_IMODE(mode))
        except OSError:
            pass  # exFAT 等不支持权限的文件系统上 chmod 可能失败，不报错

    def migrate(self) -> int:
        """把库迁移到最新版本。幂等；返回当前版本（PRAGMA user_version）。

        注意：迁移里 PRAGMA user_version 会隐式提交，所以这里不用 tx()，
        直接顺序执行（同步 API、进程内单写入者语义不变）。
        """
        v = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        for i, fn in enumerate(_MIGRATIONS):
            if v < i + 1:
                fn(self._conn)
                self._conn.execute(f"PRAGMA user_version={i + 1}")
                v = i + 1
        return v

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """写事务：BEGIN IMMEDIATE … COMMIT / 出错 ROLLBACK。进程内一把锁 = 单写入者。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def read(self) -> sqlite3.Connection:
        """只读查询用（同一连接，row_factory = sqlite3.Row）。autocommit 模式，BEGIN/COMMIT 由 tx() 管。"""
        return self._conn

    def event(
        self,
        conn: sqlite3.Connection,
        kind: str,
        *,
        group_id: str = "",
        entity: str = "",
        entity_id: str = "",
        payload: dict | None = None,
    ) -> int:
        """在调用方的事务里写一条事件；payload 存 JSON，带 v=1。返回事件 id。"""
        cur = conn.execute(
            "INSERT INTO events (ts, kind, group_id, entity, entity_id, payload, v)"
            " VALUES (?, ?, ?, ?, ?, ?, 1)",
            (
                clock.now(),
                str(kind),
                str(group_id or ""),
                str(entity or ""),
                str(entity_id or ""),
                json.dumps(payload, ensure_ascii=False) if payload is not None else None,
            ),
        )
        return int(cur.lastrowid or 0)

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (ValueError, TypeError):
            return default

    def kv_set(self, conn: sqlite3.Connection, key: str, value: Any) -> None:
        conn.execute(
            "INSERT INTO kv (key, value, updated) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (key, json.dumps(value, ensure_ascii=False), clock.now()),
        )

    def secret_get(self, name: str) -> str:
        row = self._conn.execute("SELECT value FROM secrets WHERE name=?", (name,)).fetchone()
        return str(row["value"]) if row is not None else ""

    def secret_set(self, conn: sqlite3.Connection, name: str, value: str) -> None:
        conn.execute(
            "INSERT INTO secrets (name, value, updated) VALUES (?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (name, str(value), clock.now()),
        )

    def secret_delete(self, conn: sqlite3.Connection, name: str) -> None:
        """删掉一条密钥（不存在也幂等）——「清掉网页存的、回落 config.toml 值」用。"""
        conn.execute("DELETE FROM secrets WHERE name=?", (str(name),))

    # ------------------------------------------------------------------
    # M8：数据不无限增长（design 12.2：大日志保留 30 天）
    # ------------------------------------------------------------------

    _PRUNE_KEEP_S = 30 * 86400
    # model_calls（最近模型请求日志）：留 3 天 / 最多 2000 行，哪个先到按哪个清
    _MODEL_CALLS_KEEP_S = 3 * 86400
    _MODEL_CALLS_MAX_ROWS = 2000

    def prune(self, now: float) -> dict[str, int]:
        """清掉过期数据：events / tool_calls / usage / judgments 保留 30 天，
        mentions 过期行物理删除，model_calls 留 3 天 / 最多 2000 行（哪个先到按哪个）。
        返回各表删了多少条。幂等，app 每天跑一次。
        """
        cutoff = float(now) - self._PRUNE_KEEP_S
        counts: dict[str, int] = {}
        with self.tx() as conn:
            for table, col in (
                ("events", "ts"),
                ("tool_calls", "ts"),
                ("usage", "ts"),
                ("judgments", "ts"),
            ):
                cur = conn.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))
                counts[table] = int(cur.rowcount or 0)
            cur = conn.execute("DELETE FROM mentions WHERE expires_ts <= ?", (float(now),))
            counts["mentions"] = int(cur.rowcount or 0)
            counts["model_calls"] = self._prune_model_calls_tx(conn, now)
        return counts

    def _prune_model_calls_tx(self, conn: sqlite3.Connection, now: float) -> int:
        """在调用方事务里按「3 天 + 2000 行上限」清 model_calls；返回删掉的行数。

        models.py 每写 200 行也会调一次（不能等每天一次的 prune 就把撑爆的日志库清掉）。
        """
        cutoff = float(now) - self._MODEL_CALLS_KEEP_S
        cur = conn.execute("DELETE FROM model_calls WHERE ts < ?", (cutoff,))
        removed = int(cur.rowcount or 0)
        cur = conn.execute(
            "DELETE FROM model_calls WHERE id < ("
            "  SELECT MIN(id) FROM (SELECT id FROM model_calls ORDER BY id DESC LIMIT ?)"
            ")",
            (self._MODEL_CALLS_MAX_ROWS,),
        )
        removed += int(cur.rowcount or 0)
        return removed
