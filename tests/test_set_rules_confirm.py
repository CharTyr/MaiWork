"""set_rules 的确认门改成「白名单」口径（2026-10-12）。

安全背景：管理员在网页上和主模型对话时，主模型能调 set_rules 改 config.toml 并立刻
热生效。主模型读到的群消息 / 网页正文 / 工具输出里可能藏注入指令，所以除了明确
「无安全影响的调参项」（资讯 / 画像 / 评测的节奏与数量），其余字段——包括将来新增
的——一律先写小票等管理员点头；绝不能让它把运行账号换成 root、把服务地址改到陌生
服务器（密钥随请求发出去）、把密钥文件指向任意文件、加一台陌生 SSH 机器、改控制台
监听地址。

夹具沿用 test_tools_admin.py 那一套（真 Store + 临时插件目录里的真 config.toml）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import clock, rules
from test_tools_admin import G1, _Svc, _bind_chat, _config_field, _config_raw, _ctx, _pending_rows

NEED_CONFIRM = "已请求管理员确认"

# 任务定下的白名单：只有这些「纯调参」键可以不经确认直接写
WHITELIST = frozenset(
    {
        "feeds.news_slots",
        "feeds.news_jitter_minutes",
        "feeds.max_items",
        "feeds.lookback_days",
        "feeds.web_min_avg",
        "feeds.pool_min_avg",
        "feeds.guides",
        "feeds.collect_minutes",
        "feeds.viz_per_day",
        "topics.candidate_ttl_hours",
        "profile.batch_messages",
        "profile.max_interval_hours",
        "profile.backfill_days",
        "profile.backfill_max_messages",
        "profile.weekly_day",
        "profile.read_interval_minutes",
        "jev.timeout_ms",
        "quick_judge.keyword_filter",
    }
)

# 每个白名单键配一个合法新值（和 config 默认值不同，才能看出真写进去了）
WHITELIST_VALUES: dict[str, object] = {
    "feeds.news_slots": ["07:45", "18:20"],
    "feeds.news_jitter_minutes": 25,
    "feeds.max_items": 12,
    "feeds.lookback_days": 10,
    "feeds.web_min_avg": 2.5,
    "feeds.pool_min_avg": 3.5,
    "feeds.guides": False,
    "feeds.collect_minutes": 20,
    "feeds.viz_per_day": 2,
    "topics.candidate_ttl_hours": 20,
    "profile.batch_messages": 60,
    "profile.max_interval_hours": 4,
    "profile.backfill_days": 5,
    "profile.backfill_max_messages": 1200,
    "profile.weekly_day": 2,
    "profile.read_interval_minutes": 15,
    "jev.timeout_ms": 900,
    "quick_judge.keyword_filter": True,
}


@pytest.fixture
def svc(tmp_path: Path):
    """和 test_tools_admin.py 同一套夹具写法（真 Store + 临时插件目录里的 config.toml）。"""
    from CharTyr_MaiWork.maiwork.tools_admin import _APPROVED_CTX, _CHAT_CTX, register_admin_tools

    _CHAT_CTX.set((0, 0))
    _APPROVED_CTX.set(None)
    s = _Svc(tmp_path, workspace_root=tmp_path / "ws")
    now = clock.now()
    with s.store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, workspace, created) VALUES (?, ?, ?)"
            " ON CONFLICT(group_id) DO NOTHING",
            (G1, "tinker", now),
        )
    s.admin_pending = register_admin_tools(s.tools, s)
    return s


def test_whitelist_is_the_agreed_set() -> None:
    """白名单就是任务定下的这 18 个纯调参键：多一个 / 少一个都要有人重新想过。"""
    from CharTyr_MaiWork.maiwork.tools_admin import _SET_RULES_NO_CONFIRM

    assert _SET_RULES_NO_CONFIRM == WHITELIST


def test_whitelist_keys_are_real_writable_fields() -> None:
    """白名单里的键必须在 rules 里真实存在、且不是只读 / 不是归每群管的（写错了会静默变严）。"""
    from CharTyr_MaiWork.maiwork.tools_admin import _SET_RULES_NO_CONFIRM

    for key in sorted(_SET_RULES_NO_CONFIRM):
        spec = rules.CONFIG_BY_KEY.get(key)
        assert spec is not None, key
        assert not spec.get("readonly"), key
        assert not rules.group_managed_hint(key), key


def test_sensitive_keys_are_not_whitelisted() -> None:
    """安全边界类的键一个都不许留在白名单里。"""
    from CharTyr_MaiWork.maiwork.tools_admin import _SET_RULES_NO_CONFIRM

    for key in (
        "environments.run_as",
        "environments.ssh",
        "environments.local_mode",
        "jev.api_url",
        "jev.key_file",
        "console.listen",
        "console.public_url",
        "approval.admins",
        "approval.required",
        "topics.min_gap_hours",
    ):
        assert key not in _SET_RULES_NO_CONFIRM, key


class TestWhitelistWritesDirectly:
    """白名单项=无安全影响的调参：不写小票，直接写文件并热生效。"""

    @pytest.fixture(autouse=True)
    def _chat(self, svc: _Svc) -> None:
        _bind_chat(svc)

    @pytest.mark.asyncio
    async def test_feeds_max_items_needs_no_confirm(self, svc: _Svc) -> None:
        r = await svc.tools.call("rules_manage", {"action": "set", "patch": {"feeds": {"max_items": 12}}}, _ctx())
        assert r.ok, r.error
        assert NEED_CONFIRM not in r.output
        assert _pending_rows(svc.store) == []
        assert _config_field(svc, "feeds", "max_items") == 12
        assert svc.get_settings().feeds.max_items == 12  # 立刻热生效

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", sorted(WHITELIST))
    async def test_every_whitelist_key_needs_no_confirm(self, svc: _Svc, key: str) -> None:
        section, field = key.split(".", 1)
        value = WHITELIST_VALUES[key]
        r = await svc.tools.call("rules_manage", {"action": "set", "patch": {section: {field: value}}}, _ctx())
        assert r.ok, (key, r.error)
        assert NEED_CONFIRM not in r.output, key
        assert _pending_rows(svc.store) == [], key
        assert _config_field(svc, section, field, "没写过") == value, key


class TestNonWhitelistNeedsConfirm:
    """白名单之外的一切（含将来新增的字段）先写小票；同意前 config.toml 一个字都不动。"""

    @pytest.fixture(autouse=True)
    def _chat(self, svc: _Svc) -> None:
        _bind_chat(svc)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "patch,label",
        (
            ({"environments": {"run_as": "worker"}}, "运行账号"),
            ({"jev": {"api_url": "https://evil.example/v1"}}, "服务地址"),
            ({"environments": {"ssh": [{"name": "陌生机器", "host": "user@10.0.0.9:22", "note": ""}]}}, "自有机器"),
            ({"console": {"listen": "0.0.0.0:19387"}}, "监听地址"),
            ({"console": {"public_url": "https://evil.example"}}, "公开网址"),
            ({"jev": {"key_file": "/home/char/other.key"}}, "密钥文件"),
            ({"usage": {"alert_daily_tokens": 100}}, "每日用量提醒线"),  # 将来新增的字段也要确认
        ),
    )
    async def test_sensitive_patch_queues_ticket(self, svc: _Svc, patch: dict, label: str) -> None:
        before = _config_raw(svc)
        r = await svc.tools.call("rules_manage", {"action": "set", "patch": patch}, _ctx())
        assert r.ok, (patch, r.error)
        assert NEED_CONFIRM in r.output, patch
        rows = _pending_rows(svc.store)
        assert len(rows) == 1 and rows[0]["tool"] == "rules_manage", patch
        assert label in rows[0]["summary"], (label, rows[0]["summary"])
        assert "安全" in rows[0]["summary"], rows[0]["summary"]  # 摘要里点明涉及安全设置
        assert _config_raw(svc) == before, patch  # 没同意前不写文件

    @pytest.mark.asyncio
    async def test_run_as_applies_only_after_approval(self, svc: _Svc) -> None:
        """小票不是死路：管理员点头后这次改动照旧落地。"""
        r = await svc.tools.call("rules_manage", {"action": "set", "patch": {"environments": {"run_as": "worker"}}}, _ctx())
        assert r.ok, r.error
        pid = int(_pending_rows(svc.store)[0]["id"])
        assert _config_field(svc, "environments", "run_as", "没写过") == "没写过"
        out = await svc.admin_pending.execute(pid, True)
        assert out.ok, out.error
        assert _config_field(svc, "environments", "run_as") == "worker"


class TestMixedPatch:
    """白名单项和安全项混在一张 patch 里：整张一起等确认，摘要列出全部字段。"""

    @pytest.fixture(autouse=True)
    def _chat(self, svc: _Svc) -> None:
        _bind_chat(svc)

    @pytest.mark.asyncio
    async def test_mixed_patch_confirms_and_summarizes_all(self, svc: _Svc) -> None:
        patch = {"feeds": {"max_items": 12}, "environments": {"run_as": "worker"}}
        r = await svc.tools.call("rules_manage", {"action": "set", "patch": patch}, _ctx())
        assert r.ok, r.error
        assert NEED_CONFIRM in r.output
        rows = _pending_rows(svc.store)
        assert len(rows) == 1
        summary = rows[0]["summary"]
        assert "每批条数 → 12" in summary      # 白名单项也列出来（点头前看到的就是真要改的）
        assert "运行账号 → worker" in summary
        assert "environments." not in summary and "feeds." not in summary
        # 整张 patch 一起等确认：白名单项也不许先偷偷写进去
        assert _config_field(svc, "feeds", "max_items", "没写过") == "没写过"
        assert _config_field(svc, "environments", "run_as", "没写过") == "没写过"
