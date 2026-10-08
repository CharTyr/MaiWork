"""交接包（docs/24）：后端生成 + 计数。

先写先红（2026-10-08）：`maiwork/handoff.py` 还没建、三个路由还没注册时，
这个文件整体应当失败（导入错误 / 404），再写实现让它变绿。

覆盖 docs/24 §八 的 10 条：
1. 去人（{@QQ号} / 名册名字）+ 绝不带的字段（个人构想 basis、timeline / tokens / workspace /
   requester_id / requester_name）一个都不出现；
2. 关注成员注记片段进要求文字 → 409，日志不含内容；
3. 包里不出现本群链接随机码，也不出现 console.public_url 下的群页地址；
4. 别的群群友 / 群管理员 403、没登录 401、非服务群 404；
5. 管理员拿到的包和群友逐字相同；
6. 构想 items=1,3 只列 1、3；越界按全部；
7. 长度：正常长度一字不截；超 30000 先砍资料再砍验收意见；「要做什么 / 做到什么程度算完」永不砍；
8. 画像挑选：不沾边的不选、最多 3 条、同分先 locked 再 evidence 多；
9. 计数：GET 不计数、POST 计数、10 分钟内同浏览器只算一次、群友版没有 handoff_count；
10. 各状态任务（含做完、失败、取消）都能生成。

纯函数的用例直接调 `maiwork/handoff.py`（快、准）；权限 / 计数 / 隐私闸走真服务器
（aiohttp TestServer，和 test_console.py 一样）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.app import MaiWorkApp

G1 = "900000001"
G2 = "123456789"
G3_NOTSERVED = "999999999"
ADMIN_PW = "总管理员密码-交接包-1234"
G2_PW = "群二管理员密码-交接包-abcd"
MEMBER = "20002"
PUBLIC_URL = "https://mw.example.com"

ALL_STATUSES = (
    "pending_approval", "queued", "running", "waiting_input", "shelved",
    "reviewing", "completed", "failed", "paused", "cancelled", "rejected",
)


def _free_port() -> int:
    import socket as _s

    with _s.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _raw_config(data_dir: Path) -> dict:
    return {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}, {"group": f"qq:{G2}"}]},
        "console": {"listen": f"127.0.0.1:{_free_port()}", "password": ADMIN_PW, "public_url": PUBLIC_URL},
        "models": {"base_url": "https://ep.test/v1", "api_key": "sk-x", "main": "m", "worker": "w"},
        "storage": {"data_dir": str(data_dir)},
        "approval": {"required": True, "admins": ["10001"]},
    }


# ----------------------------------------------------------------------
# 纯函数用的样本
# ----------------------------------------------------------------------

TS = 1_790_000_000.0  # 2026-09-18 前后，北京时间固定值


def idea_view(**over) -> dict:
    """群友版构想视图（feeds.ideas_view(admin=False) 的形状）。"""
    v = {
        "id": 12,
        "icon": "bulb",
        "title": "把 FPGA 板子玩起来",
        "body": "板子在抽屉里躺了半年，想拿它做个能演示的小东西。",
        "basis": "群里聊过好几回 FPGA，都说想上手但没时间。",
        "step": "",
        "effort": "",
        "state": "new",
        "requested_by": "10086",
        "task_id": None,
        "created_ts": TS,
        "feedback": {"up": 1, "down": 0},
        "feasibility": {"level": "ok", "note": "板子和工具链都现成，能直接开工。"},
        "keywords": ["FPGA", "开源硬件"],
        "items": [
            {"no": 1, "kind": "task", "title": "挑一块板子", "desc": "能跑通最小工程就行"},
            {"no": 2, "kind": "task", "title": "写一个计数器", "desc": ""},
            {"no": 3, "kind": "task", "title": "拍一段演示", "desc": "十秒以内的短视频"},
        ],
        "target_user_id": "",
    }
    v.update(over)
    return v


def task_view(**over) -> dict:
    """群友版任务详情视图（tasks.detail_view(admin=False) + server 拼的 delivery/link_check）。"""
    v = {
        "id": "T-7",
        "icon": "package",
        "title": "整理一份投票方案",
        "status": "running",
        "meta": "已跑 3 分钟 · 1 次尝试",
        "goal_id": None,
        "updated_ts": TS,
        "undelivered": False,
        "paused_reason": None,
        "requester_name": "阿柒",
        "req": "把投票发起来、统计结果、公示三天。",
        "criteria": ["能正常投票", "结果要公示"],
        "requirements": [],
        "review": "",
        "delivery": [],
        "question": None,
        "delivery_kind": "text",
        "attempts": 1,
        "started_ts": TS,
        "finished_ts": None,
        "steps": 6,
        "link_check": None,
    }
    v.update(over)
    return v


def _refs(n: int, *, opened: int = 0) -> dict:
    """造 link_check：前 opened 条是打开核实过的，剩下的只在搜索结果里见过。"""
    urls = [f"https://ref.example/{i}" for i in range(1, n + 1)]
    return {
        "links": n,
        "unopened": n - opened,
        "opened_urls": urls[:opened],
        "unopened_urls": urls[opened:],
    }


# ----------------------------------------------------------------------
# 纯函数：去人 / 字段取舍 / items / 长度 / 画像
# ----------------------------------------------------------------------


class TestDehumanizeAndFields:
    def test_idea_drops_people_and_admin_only_fields(self) -> None:
        """§八.1：{@QQ号}、名册名字、个人构想 basis、requested_by 一个都不出现。"""
        from CharTyr_MaiWork.maiwork import handoff

        view = idea_view(
            title="给{@20002}做的工具",
            body="阿帆说想要一个{@20003}能用的脚本；蓝莓山竹也提过。",
            basis="因为蓝莓山竹在做 FPGA 项目。",
            requested_by="10086",
            keywords=["FPGA"],
        )
        out = handoff.build_idea(
            view, "", [], ["阿帆", "阿帆的小号", "蓝莓山竹", "Kiriko"], TS
        )
        md = out["markdown"]
        assert "{@" not in md
        assert "20002" not in md and "20003" not in md and "10086" not in md
        for name in ("阿帆", "阿帆的小号", "蓝莓山竹", "Kiriko"):
            assert name not in md, f"名字没去掉：{name}"
        assert "某群友" in md
        assert out["filename"] == "maiwork-idea-12.md"
        assert out["chars"] == len(md)
        assert out["truncated"] is False

    def test_long_roster_name_replaced_first(self) -> None:
        """长名字优先替换：先换「蓝莓山竹」，短的「蓝莓山」才轮得到。"""
        from CharTyr_MaiWork.maiwork import handoff

        view = idea_view(body="蓝莓山竹和蓝莓山都提过。")
        md = handoff.build_idea(view, "", [], ["蓝莓山", "蓝莓山竹"], TS)["markdown"]
        assert "蓝莓山竹" not in md and "蓝莓山" not in md
        assert md.count("某群友") == 2

    def test_personal_idea_hides_basis_and_says_so(self) -> None:
        """个人向构想：不写 basis，只写一句「这是给群里一位群友的构想」。"""
        from CharTyr_MaiWork.maiwork import handoff

        view = idea_view(
            basis="他在群里说过想买板子（个人画像摘要）",
            target_user_id=MEMBER,
        )
        md = handoff.build_idea(view, "", [], [], TS)["markdown"]
        assert "个人画像摘要" not in md
        assert "这是给群里一位群友的构想" in md

    def test_task_drops_requester_and_impl_fields(self) -> None:
        """§八.1：timeline / tokens / workspace / requester_id / requester_name 不进包。"""
        from CharTyr_MaiWork.maiwork import handoff

        view = task_view(
            requester_name="阿柒",
            requester_id="20002",
            env="本机 · maiwork 用户 · 内存上限 512M",
            workspace="tinker",
            source="request",
            request_id="R-8",
            tokens=12345,
            steps=6,
            timeline=[{"ts": TS, "actor": "子 agent #1", "tool": "write_file", "input": "秘密输入", "output": "秘密输出", "ms": 3, "ok": True}],
            lane_notes=[{"ts": TS, "kind": "rework", "note": "返工过一次"}],
            req="把投票发起来；找{@20002}帮忙统计。",
        )
        md = handoff.build_task(view, [], [], TS)["markdown"]
        for bad in ("阿柒", "20002", "tinker", "R-8", "12345", "秘密输入", "秘密输出", "返工过一次", "本机 · maiwork"):
            assert bad not in md, f"不该出现的字段进包了：{bad}"
        assert "把投票发起来" in md
        assert handoff.build_task(view, [], [], TS)["filename"] == "maiwork-T-7.md"

    def test_task_filename_sanitized(self) -> None:
        from CharTyr_MaiWork.maiwork import handoff

        md = handoff.build_task(task_view(id="T/7 号"), [], [], TS)
        assert md["filename"] == "maiwork-T-7--.md"

    def test_task_status_and_progress_sections(self) -> None:
        """任务：状态中文、尝试次数、暂停原因、最近一次验收意见、提问、成品。"""
        from CharTyr_MaiWork.maiwork import handoff

        view = task_view(
            status="paused",
            attempts=2,
            paused_reason={"kind": "tokens", "limit": 100, "used": 120, "text": "用量到上限了"},
            review="上一轮结尾没写清楚。",
            question="用哪种投票工具？",
            delivery=[
                {"id": 3, "kind": "here.now", "text": "投票方案", "state": "已发", "url": "https://x.here.now/abc"},
                {"id": 4, "kind": "群文件", "text": "统计表.xlsx", "state": "已发", "url": None},
            ],
            finished_ts=None,
        )
        out = handoff.build_task(view, [], [], TS)
        md = out["markdown"]
        assert "## 已经做到哪了" in md
        assert "暂停" in md
        assert "2 次" in md
        assert "用量到上限了" in md
        assert "上一轮结尾没写清楚。" in md
        assert "用哪种投票工具？" in md
        assert "https://x.here.now/abc" in md and "统计表.xlsx" in md

    def test_every_status_renders(self) -> None:
        """§八.10：11 个状态都能生成，状态用中文。"""
        from CharTyr_MaiWork.maiwork import handoff

        for status in ALL_STATUSES:
            out = handoff.build_task(task_view(status=status), [], [], TS)
            assert out["markdown"].strip(), status
            assert "## 要做什么" in out["markdown"], status
        # 抽查几个状态的中文
        for status, cn in (("completed", "做完"), ("failed", "失败"), ("cancelled", "取消"), ("queued", "排队")):
            md = handoff.build_task(task_view(status=status), [], [], TS)["markdown"]
            assert cn in md, (status, cn)


class TestReviewFixes:
    """主会话复核补的两条（2026-10-08）。"""

    def test_progress_and_background_are_dehumanized(self) -> None:
        """提问、暂停原因、成品名、群画像条目里的人名 / {@QQ号} 也要换掉。"""
        from CharTyr_MaiWork.maiwork import handoff

        view = task_view(
            status="paused",
            paused_reason={"kind": "capability", "text": "等阿柒给账号"},
            question="{@20002} 你要用哪种投票工具？",
            delivery=[{"kind": "here.now", "text": "阿柒的投票方案", "state": "已发", "url": "https://x.here.now/a"}],
            review="阿柒说结尾不清楚",
        )
        sections = [{"category": "ongoing", "name": "在忙什么",
                     "entries": [{"id": 1, "text": "阿柒在张罗投票方案", "evidence_count": 3, "locked": False}]}]
        md = handoff.build_task(view, sections, ["阿柒"], TS)["markdown"]
        assert "阿柒" not in md and "20002" not in md and "{@" not in md
        assert "某群友在张罗投票方案" in md, "背景里的画像条目要保留、只换名字"

    def test_idea_background_dehumanized(self) -> None:
        from CharTyr_MaiWork.maiwork import handoff

        sections = [{"category": "ongoing", "name": "在忙什么",
                     "entries": [{"id": 1, "text": "阿帆在做投票小工具", "evidence_count": 1, "locked": False}]}]
        md = handoff.build_idea(idea_view(title="投票小工具"), "", sections, ["阿帆"], TS)["markdown"]
        assert "阿帆" not in md and "某群友在做投票小工具" in md

    def test_opened_urls_from_tool_calls_are_not_used(self) -> None:
        """「打开过哪些页」来自工具调用记录（只给总管理员看），不进包；只列引用核对里给群友看的那份。"""
        from CharTyr_MaiWork.maiwork import handoff

        lc = {"links": 2, "unopened": 1, "opened_urls": ["https://secret.example/opened"],
              "unopened_urls": ["https://ref.example/1"]}
        md = handoff.build_task(task_view(link_check=lc), [], [], TS)["markdown"]
        assert "https://secret.example/opened" not in md
        assert "https://ref.example/1" in md


class TestItems:
    def test_picked_items_only(self) -> None:
        """§八.6：items=1,3 只列 1、3。"""
        from CharTyr_MaiWork.maiwork import handoff

        md = handoff.build_idea(idea_view(), "1,3", [], [], TS)["markdown"]
        assert "挑一块板子" in md and "拍一段演示" in md
        assert "写一个计数器" not in md

    def test_items_all_when_not_given_or_out_of_range(self) -> None:
        from CharTyr_MaiWork.maiwork import handoff

        for arg in ("", "9,10", None, "abc", "0,-1"):
            md = handoff.build_idea(idea_view(), arg, [], [], TS)["markdown"]
            for title in ("挑一块板子", "写一个计数器", "拍一段演示"):
                assert title in md, (arg, title)

    def test_items_accepts_list(self) -> None:
        from CharTyr_MaiWork.maiwork import handoff

        md = handoff.build_idea(idea_view(), [2], [], [], TS)["markdown"]
        assert "写一个计数器" in md
        assert "挑一块板子" not in md

    def test_completion_notes(self) -> None:
        from CharTyr_MaiWork.maiwork import handoff

        md = handoff.build_idea(idea_view(), "2", [], [], TS)["markdown"]
        # 选中的第 2 项没有完成说明 → 用固定那句
        assert "按上面每个项目交出成品即可" in md


class TestLength:
    def test_normal_length_never_cut(self) -> None:
        """§八.7：要求 5000 字 + 验收意见 3000 字 + 20 条资料 → 一字不截。"""
        from CharTyr_MaiWork.maiwork import handoff

        req = "要求" * 2500          # 5000 字
        review = "验收" * 1500       # 3000 字
        view = task_view(req=req, review=review, link_check=_refs(20), status="completed")
        out = handoff.build_task(view, [], [], TS)
        md = out["markdown"]
        assert out["truncated"] is False
        assert req in md
        assert review in md
        for i in range(1, 21):
            assert f"https://ref.example/{i}" in md
        assert out["chars"] == len(md)

    def test_too_long_cuts_refs_then_review(self) -> None:
        """§八.7：超 30000 → 先参考资料留前 5 条，再把验收意见截到 500 字。"""
        from CharTyr_MaiWork.maiwork import handoff

        req = "长" * 32000
        review = "验收意见" * 900      # 3600 字
        view = task_view(req=req, review=review, link_check=_refs(20))
        out = handoff.build_task(view, [], [], TS)
        md = out["markdown"]
        assert out["truncated"] is True
        assert "https://ref.example/6" not in md     # 资料只剩前 5 条
        assert "https://ref.example/5" in md
        assert "验收意见" * 125 in md                 # 500 字正好留下
        assert "验收意见" * 126 not in md
        assert req in md                             # 要做什么一字不砍

    def test_huge_requirement_and_criteria_kept_whole(self) -> None:
        """§八.7：「要做什么」「做到什么程度算完」单独超过 30000 字也完整保留。"""
        from CharTyr_MaiWork.maiwork import handoff

        req = "要" * 31000
        crit = ["标准" * 16000]
        view = task_view(req=req, criteria=crit, review="验收" * 600, link_check=_refs(20))
        out = handoff.build_task(view, [], [], TS)
        md = out["markdown"]
        assert req in md
        assert crit[0] in md
        assert out["truncated"] is True     # 资料 / 验收意见被砍过，但两节主体完整

    def test_requirements_used_before_criteria(self) -> None:
        from CharTyr_MaiWork.maiwork import handoff

        view = task_view(
            requirements=[{"id": "R0", "text": "内容真实、不编造", "origin": "底线", "kind": "底线"},
                          {"id": "R1", "text": "把投票发起来", "origin": "原话", "kind": "按原话"}],
            criteria=["这条不该出现"],
        )
        md = handoff.build_task(view, [], [], TS)["markdown"]
        assert "把投票发起来" in md
        assert "这条不该出现" not in md


class TestProfilePick:
    def _sections(self, entries: list[dict]) -> list[dict]:
        return [{"category": "recent", "name": "最近在聊", "entries": entries}]

    def _entry(self, text: str, *, locked: bool = False, evidence: int = 0) -> dict:
        return {"id": 1, "text": text, "evidence_count": evidence, "locked": locked,
                "first_ts": TS, "last_ts": TS, "source": "model"}

    def test_related_only_and_capped_at_three(self) -> None:
        """§八.8：不沾边的不选；最多 3 条。"""
        from CharTyr_MaiWork.maiwork import handoff

        entries = [
            self._entry("群里在准备一份投票方案", evidence=1),
            self._entry("投票结果要公示三天", evidence=2),
            self._entry("大家喜欢用投票来决定事情", evidence=3),
            self._entry("投票之前先对一遍名单", evidence=4),
            self._entry("这个群喜欢半夜聊吃的", evidence=9),
            self._entry("有人在学日语", evidence=9),
        ]
        view = task_view(req="把投票发起来、统计结果、公示三天。")
        md = handoff.build_task(view, self._sections(entries), [], TS)["markdown"]
        assert "## 背景" in md
        assert "半夜聊吃的" not in md and "有人在学日语" not in md
        assert md.count("投票") >= 3
        picked = [e for e in entries if e["text"] in md]
        assert len(picked) <= 3

    def test_no_hit_no_background_section(self) -> None:
        from CharTyr_MaiWork.maiwork import handoff

        md = handoff.build_task(task_view(req="整理一份投票方案"), self._sections([self._entry("有人在学日语")]), [], TS)["markdown"]
        assert "## 背景" not in md

    def test_tie_break_locked_then_evidence(self) -> None:
        """同分先 locked，再 evidence_count 多的。"""
        from CharTyr_MaiWork.maiwork import handoff

        entries = [
            self._entry("投票统计表模板", locked=False, evidence=5),
            self._entry("投票统计表放哪里", locked=True, evidence=0),
            self._entry("投票统计表要留档", locked=False, evidence=99),
        ]
        md = handoff.build_task(task_view(req="投票统计表"), self._sections(entries), [], TS)["markdown"]
        assert "投票统计表放哪里" in md        # locked 优先
        assert "投票统计表要留档" in md        # 其次 evidence 多

    def test_idea_keywords_count(self) -> None:
        """构想打分要把 keywords 算进去。"""
        from CharTyr_MaiWork.maiwork import handoff

        entries = [self._entry("板子到了记得拍照")]
        view = idea_view(body="想做点小东西。", keywords=["板子"])
        md = handoff.build_idea(view, "", self._sections(entries), [], TS)["markdown"]
        assert "板子到了记得拍照" in md


# ----------------------------------------------------------------------
# 真服务器：权限 / 隐私闸 / 群链接码 / 一致性 / 计数
# ----------------------------------------------------------------------


class SimpleEnv:
    def __init__(self, app: MaiWorkApp, client: TestClient, tmp_path: Path) -> None:
        self.app = app
        self.client = client
        self.tmp_path = tmp_path

    async def login(self, password: str = ADMIN_PW):
        return await self.client.post("/api/login", json={"password": password})

    def member_headers(self, gid: str) -> dict:
        return {"X-MW-Group": self.app.token_of(gid)}

    def add_idea(self, gid: str = G1, **fields) -> int:
        cols = {
            "group_id": gid,
            "title": fields.pop("title", "一个构想"),
            "body": fields.pop("body", "正文"),
            "basis": fields.pop("basis", ""),
            "state": fields.pop("state", "new"),
            "items": json.dumps(fields.pop("items", []), ensure_ascii=False),
            "keywords": json.dumps(fields.pop("keywords", []), ensure_ascii=False),
            "feasibility": json.dumps(fields.pop("feasibility", {"level": "ok", "note": ""}), ensure_ascii=False),
            "target_user_id": fields.pop("target_user_id", ""),
            "task_id": fields.pop("task_id", None),
            "created": clock.now(),
            "updated": clock.now(),
        }
        assert not fields, fields
        names = ", ".join(cols)
        marks = ", ".join("?" * len(cols))
        with self.app.store.tx() as conn:
            cur = conn.execute(f"INSERT INTO ideas ({names}) VALUES ({marks})", list(cols.values()))
            return int(cur.lastrowid or 0)

    def add_task(self, gid: str = G1, *, status: str = "queued", review: str = "", **fields) -> str:
        tid = self.app.tasks.create(
            gid,
            title=fields.pop("title", "整理一份投票方案"),
            req=fields.pop("req", "把投票发起来。"),
            criteria=fields.pop("criteria", []),
            source="test",
            requester_id=fields.pop("requester_id", MEMBER),
            requester_name=fields.pop("requester_name", "阿柒"),
            status="queued",
        )
        assert not fields, fields
        if status != "queued" or review:
            with self.app.store.tx() as conn:
                conn.execute(
                    "UPDATE tasks SET status=?, review=?, attempts=1 WHERE id=?",
                    (status, review, tid),
                )
        return tid

    def add_profile(self, gid: str, text: str, *, category: str = "recent",
                    locked: bool = False, evidence: int = 0) -> None:
        profiles = self.app.profiles
        if not hasattr(profiles, "entries_map"):
            raise RuntimeError("测试用 FakeProfiles 没挂上")
        profiles.add_entry(gid, category, text)
        entry = profiles.entries_map[gid][-1]
        entry["locked"] = locked
        entry["evidence_count"] = evidence

    def add_focus_note(self, gid: str, user_id: str, note: str) -> None:
        with self.app.store.tx() as conn:
            conn.execute(
                "INSERT INTO focus_members (group_id, user_id, name, note, removed, persona)"
                " VALUES (?, ?, ?, ?, 0, '')"
                " ON CONFLICT(group_id, user_id) DO UPDATE SET note=excluded.note",
                (gid, user_id, "阿帆", note),
            )

    def set_roster(self, gid: str, user_id: str, name: str) -> None:
        from CharTyr_MaiWork.maiwork import members

        with self.app.store.tx() as conn:
            members.record(conn, gid, user_id, name, clock.now())

    def count_taken(self, kind: str, entity_id) -> int:
        row = self.app.store.read().execute(
            "SELECT COUNT(*) AS c FROM events WHERE kind='handoff.taken' AND entity=? AND entity_id=?",
            (kind, str(entity_id)),
        ).fetchone()
        return int(row["c"] or 0)

    def taken_rows(self, kind: str, entity_id) -> list[dict]:
        rows = self.app.store.read().execute(
            "SELECT payload FROM events WHERE kind='handoff.taken' AND entity=? AND entity_id=? ORDER BY id",
            (kind, str(entity_id)),
        ).fetchall()
        return [json.loads(r["payload"] or "{}") for r in rows]


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    import tomlkit

    raw = _raw_config(tmp_path / "data")
    plug_dir = tmp_path / "plug"
    plug_dir.mkdir()
    doc = tomlkit.document()
    for section, values in raw.items():
        if isinstance(values, dict):
            t = tomlkit.table()
            for k, v in values.items():
                t[k] = v
            doc[section] = t
    (plug_dir / "config.toml").write_text(tomlkit.dumps(doc), encoding="utf-8")
    ctx = FakeCtx({"config.get": "987654321"})
    app = MaiWorkApp(ctx, raw, plugin_dir=plug_dir)
    app.profiles_cls = FakeProfiles
    await app.start()
    app.group_admins.set_password(G2, G2_PW)
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    try:
        yield SimpleEnv(app=app, client=client, tmp_path=tmp_path)
    finally:
        await client.close()
        await app.stop()


class TestPermissions:
    @pytest.mark.asyncio
    async def test_anonymous_401(self, env: SimpleEnv) -> None:
        iid = env.add_idea()
        tid = env.add_task()
        for path in (f"/api/handoff/idea/{iid}", f"/api/handoff/task/{tid}"):
            r = await env.client.get(path)
            assert r.status == 401, path
            assert (await r.json())["error"] == "先登录管理员，或用群链接打开"
            r2 = await env.client.post(f"{path}/taken", json={"action": "copy"})
            assert r2.status == 401, path

    @pytest.mark.asyncio
    async def test_missing_404(self, env: SimpleEnv) -> None:
        await env.login()
        assert (await env.client.get("/api/handoff/idea/999999")).status == 404
        assert (await env.client.get("/api/handoff/task/T-999999")).status == 404
        assert (await env.client.get("/api/handoff/bogus/1")).status == 404

    @pytest.mark.asyncio
    async def test_other_group_403(self, env: SimpleEnv) -> None:
        iid = env.add_idea(G1)
        tid = env.add_task(G1)
        # 别群群友
        for path in (f"/api/handoff/idea/{iid}", f"/api/handoff/task/{tid}"):
            r = await env.client.get(path, headers=env.member_headers(G2))
            assert r.status == 403, path
            assert (await r.json())["error"] == "只能看自己群的内容"
        # 别群群管理员
        r = await env.client.post("/api/login", json={"password": G2_PW})
        assert r.status == 200 and (await r.json())["role"] == "group_admin"
        assert (await env.client.get(f"/api/handoff/idea/{iid}")).status == 403
        assert (await env.client.get(f"/api/handoff/task/{tid}")).status == 403
        assert (await env.client.post(f"/api/handoff/idea/{iid}/taken", json={"action": "copy"})).status == 403

    @pytest.mark.asyncio
    async def test_not_served_group_404(self, env: SimpleEnv) -> None:
        iid = env.add_idea(G3_NOTSERVED)
        tid = env.add_task(G3_NOTSERVED)
        await env.login()
        assert (await env.client.get(f"/api/handoff/idea/{iid}")).status == 404
        assert (await env.client.get(f"/api/handoff/task/{tid}")).status == 404
        assert (await env.client.post(f"/api/handoff/idea/{iid}/taken", json={"action": "copy"})).status == 404

    @pytest.mark.asyncio
    async def test_own_group_member_ok(self, env: SimpleEnv) -> None:
        iid = env.add_idea(G1)
        r = await env.client.get(f"/api/handoff/idea/{iid}", headers=env.member_headers(G1))
        assert r.status == 200
        assert (await r.json())["filename"] == f"maiwork-idea-{iid}.md"


class TestAdminSameAsMember:
    @pytest.mark.asyncio
    async def test_byte_identical(self, env: SimpleEnv) -> None:
        """§八.5：管理员拿到的包和群友逐字相同（都从群友版数据生成）。"""
        env.set_roster(G1, MEMBER, "阿帆")
        env.add_profile(G1, "群里最近在折腾投票", locked=True)
        iid = env.add_idea(
            G1,
            title="做个投票工具",
            body="阿帆想给群里做个投票小工具。",
            basis="群里最近在折腾投票。",
            target_user_id=MEMBER,
            items=[{"kind": "task", "title": "选工具", "desc": "能匿名投就行"}],
        )
        tid = env.add_task(G1, req="把投票发起来。", review="还没验收")
        await env.login()
        admin_idea = await (await env.client.get(f"/api/handoff/idea/{iid}")).json()
        admin_task = await (await env.client.get(f"/api/handoff/task/{tid}")).json()
        await env.client.post("/api/logout")
        member_idea = await (await env.client.get(f"/api/handoff/idea/{iid}", headers=env.member_headers(G1))).json()
        member_task = await (await env.client.get(f"/api/handoff/task/{tid}", headers=env.member_headers(G1))).json()
        assert admin_idea["markdown"] == member_idea["markdown"]
        assert admin_idea["filename"] == member_idea["filename"]
        assert admin_idea["chars"] == member_idea["chars"]
        assert admin_idea["truncated"] == member_idea["truncated"]
        assert admin_task["markdown"] == member_task["markdown"]
        # 群管理员拿到的也是同一份
        await env.client.post("/api/login", json={"password": ADMIN_PW})
        assert (await env.client.post(f"/api/handoff/idea/{iid}/taken", json={"action": "copy"})).status == 200
        await env.client.post("/api/logout")
        # 包本身不因计数而变
        again = await (await env.client.get(f"/api/handoff/idea/{iid}", headers=env.member_headers(G1))).json()
        assert again["markdown"] == member_idea["markdown"]


class TestPrivacyGate:
    @pytest.mark.asyncio
    async def test_note_fragment_409_without_content_in_log(self, env: SimpleEnv, caplog) -> None:
        """§八.2：关注成员注记片段进了要求文字 → 409，日志不含内容。"""
        note = "他最近在准备考研复试，压力挺大"
        env.add_focus_note(G1, MEMBER, note)
        tid = env.add_task(G1, req=f"帮个忙：{note}，做个复习计划表。")
        await env.login()
        with caplog.at_level(logging.DEBUG):
            r = await env.client.get(f"/api/handoff/task/{tid}")
        assert r.status == 409
        assert (await r.json())["error"] == "这份内容没法带出去"
        joined = "\n".join(rec.getMessage() for rec in caplog.records) + "\n".join(
            str(rec.args) for rec in caplog.records
        )
        assert "考研" not in joined
        assert note not in joined
        assert G1 in joined      # 日志里有群号（只记群号 + 条目编号）

    @pytest.mark.asyncio
    async def test_persona_summary_409(self, env: SimpleEnv) -> None:
        with env.app.store.tx() as conn:
            conn.execute(
                "INSERT INTO focus_members (group_id, user_id, name, note, removed, persona)"
                " VALUES (?, ?, ?, '', 0, ?)",
                (G1, MEMBER, "阿帆", json.dumps({"summary": "喜欢收集机械键盘，正在攒一把客制化"}, ensure_ascii=False)),
            )
        iid = env.add_idea(G1, body="照着他喜欢收集机械键盘，正在攒一把客制化来做个清单。")
        await env.login()
        r = await env.client.get(f"/api/handoff/idea/{iid}")
        assert r.status == 409


class TestNoGroupLink:
    @pytest.mark.asyncio
    async def test_no_token_or_group_page_url(self, env: SimpleEnv) -> None:
        """§八.3：包里不出现本群链接随机码，也不出现 console.public_url 下的群页地址。"""
        iid = env.add_idea(G1, title="做个投票工具", body="给群里做个投票工具。")
        tid = env.add_task(G1, req="把投票发起来。")
        await env.login()
        token = env.app.token_of(G1)
        assert token, "测试前提：这个群要有链接码"
        for path in (f"/api/handoff/idea/{iid}", f"/api/handoff/task/{tid}"):
            r = await env.client.get(path)
            assert r.status == 200, path
            md = (await r.json())["markdown"]
            assert token not in md
            assert f"{PUBLIC_URL}/#/{token}" not in md
            assert PUBLIC_URL not in md

    @pytest.mark.asyncio
    async def test_group_page_link_in_content_is_refused(self, env: SimpleEnv) -> None:
        """内容里真带了本群群页地址（随机码 = 整个群页的访问权）→ 409，不给半份。"""
        token = env.app.token_of(G1)
        assert token
        iid = env.add_idea(G1, body=f"照这个页面上写的做：{PUBLIC_URL}/#/{token}/news")
        await env.login()
        r = await env.client.get(f"/api/handoff/idea/{iid}")
        assert r.status == 409
        assert (await r.json())["error"] == "这份内容没法带出去"


class TestTakenCount:
    async def _post(self, env: SimpleEnv, path: str, body: dict):
        return await env.client.post(path, json=body)

    @pytest.mark.asyncio
    async def test_get_does_not_count_post_counts(self, env: SimpleEnv) -> None:
        """§八.9：GET 不计数；POST 计数。"""
        iid = env.add_idea(G1)
        await env.login()
        await env.client.get(f"/api/handoff/idea/{iid}")
        assert env.count_taken("idea", iid) == 0
        r = await self._post(env, f"/api/handoff/idea/{iid}/taken", {"action": "copy", "client": "browser-1"})
        assert r.status == 200 and (await r.json()) == {"ok": True}
        assert env.count_taken("idea", iid) == 1
        payload = env.taken_rows("idea", iid)[0]
        assert payload["action"] == "copy"
        assert payload["role"] == "admin"
        assert payload["browser"] == hashlib.sha256(b"browser-1").hexdigest()[:12]
        assert "browser-1" not in json.dumps(payload)

    @pytest.mark.asyncio
    async def test_same_browser_10min_deduped(self, env: SimpleEnv) -> None:
        iid = env.add_idea(G1)
        await env.login()
        for _ in range(3):
            r = await self._post(env, f"/api/handoff/idea/{iid}/taken", {"action": "copy", "client": "same"})
            assert r.status == 200
        assert env.count_taken("idea", iid) == 1
        r = await self._post(env, f"/api/handoff/idea/{iid}/taken", {"action": "download", "client": "other"})
        assert r.status == 200
        assert env.count_taken("idea", iid) == 2

    @pytest.mark.asyncio
    async def test_task_kind_and_bad_action(self, env: SimpleEnv) -> None:
        tid = env.add_task(G1)
        await env.login()
        r = await self._post(env, f"/api/handoff/task/{tid}/taken", {"action": "download", "client": "b1"})
        assert r.status == 200
        assert env.count_taken("task", tid) == 1
        assert env.taken_rows("task", tid)[0]["browser"] == hashlib.sha256(b"b1").hexdigest()[:12]
        bad = await self._post(env, f"/api/handoff/task/{tid}/taken", {"action": "nope", "client": "b1"})
        assert bad.status == 400
        assert env.count_taken("task", tid) == 1

    @pytest.mark.asyncio
    async def test_member_post_counts_with_member_role(self, env: SimpleEnv) -> None:
        iid = env.add_idea(G1)
        r = await env.client.post(
            f"/api/handoff/idea/{iid}/taken",
            json={"action": "copy", "client": "m-1"},
            headers=env.member_headers(G1),
        )
        assert r.status == 200
        assert env.taken_rows("idea", iid)[0]["role"] == "member"

    @pytest.mark.asyncio
    async def test_handoff_count_in_admin_views_only(self, env: SimpleEnv) -> None:
        """§八.9：管理员视图有 handoff_count，群友版没有这个字段。"""
        iid = env.add_idea(G1)
        tid = env.add_task(G1)
        await env.login()
        await self._post(env, f"/api/handoff/idea/{iid}/taken", {"action": "copy", "client": "c1"})
        await self._post(env, f"/api/handoff/idea/{iid}/taken", {"action": "download", "client": "c2"})
        await self._post(env, f"/api/handoff/task/{tid}/taken", {"action": "copy", "client": "c1"})

        view = await (await env.client.get(f"/api/groups/{G1}")).json()
        admin_idea = [it for it in view["ideas"] if int(it["id"]) == iid][0]
        assert admin_idea["handoff_count"] == 2
        task_detail = await (await env.client.get(f"/api/tasks/{tid}")).json()
        assert task_detail["handoff_count"] == 1

        # 群友版：视图和任务详情都没有这个字段
        await env.client.post("/api/logout")
        member_view = await (await env.client.get(f"/api/groups/{env.app.token_of(G1)}", headers=env.member_headers(G1))).json()
        member_idea = [it for it in member_view["ideas"] if int(it["id"]) == iid][0]
        assert "handoff_count" not in member_idea
        member_task = await (await env.client.get(f"/api/tasks/{tid}", headers=env.member_headers(G1))).json()
        assert "handoff_count" not in member_task

        # 本群群管理员拿的是 admin=True 版群视图 → 有 handoff_count；任务详情也有
        env.app.group_admins.set_password(G1, "本群管理员密码-交接包-xyz")
        r = await env.client.post("/api/login", json={"password": "本群管理员密码-交接包-xyz"})
        assert (await r.json())["role"] == "group_admin"
        ga_view = await (await env.client.get(f"/api/groups/{G1}")).json()
        ga_idea = [it for it in ga_view["ideas"] if int(it["id"]) == iid][0]
        assert ga_idea["handoff_count"] == 2
        ga_task = await (await env.client.get(f"/api/tasks/{tid}")).json()
        assert ga_task["handoff_count"] == 1
