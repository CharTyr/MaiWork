"""看板「行为不变」的黄金对照（docs/33 §3）。

重构前：用播种工具种一份覆盖六类来源（交接单 / 任务 / 资讯轮 / 构想 / 目标 /
开话题 / 主模型条目）、新老记录（带 run_ref 的精确关联 + run_ref 空的时间窗
回落）的数据，把旧 board_view 的 lanes 和每个条目的 item_view 输出冻结成
期望值（now 固定）。重构后的 board 包必须逐字段一致——这是「对外行为不变」
的证据。

期望值不是手写的：是本文件里 GOLDEN 常量，由旧实现一次性生成（重生成方法见
文件尾注释）。任何字段不一致都会精确报出路径，方便判断是回归还是有意变化。

2026-10-30 有意变更（主会话批准）：详情的 item 和 lanes 里同一件逐字段一致
（附加 counts/last_step/error 除外）、link 只由实际用的钥匙决定——golden 按
新口径重生成过。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.console.board import board_view, item_view
from CharTyr_MaiWork.maiwork.store import Store

GID = "900000001"
GID2 = "123456789"
NOW = 1_800_000_000.0


class _FakeAgents:
    """岗位名单（含一个自定义岗）：board_view 用它出 kind_title / fish_seed。"""

    def profiles(self):
        return [
            {"kind": "news", "title": "资讯", "fish_seed": "seed-news"},
            {"kind": "idea", "title": "构想", "fish_seed": "seed-idea"},
            {"kind": "goal", "title": "目标", "fish_seed": "seed-goal"},
            {"kind": "c_ab12cd", "title": "调研岗", "fish_seed": "seed-c"},
        ]


class _Svc:
    """board_view / item_view 需要的最小服务对象。

    agents 用真 Agents（懒建 agent_handoffs 表，profiles 覆盖一个自定义岗名）。"""

    def __init__(self, store: Store) -> None:
        from CharTyr_MaiWork.maiwork.agents import Agents
        self.store = store
        self.agents = Agents(store, self.get_settings)
        self.agents._ensure_schema()
        # 一个自定义专岗（固定 kind 好对照）：lane 名单和 kind_title 有它一份
        if not any(p.get("kind") == "c_ab12cd" for p in self.agents.profiles()):
            from CharTyr_MaiWork.maiwork import agents as _agents_mod
            raw = store.kv_get(_agents_mod._KV_PROFILES) or {}
            raw["c_ab12cd"] = {"title": "调研岗", "instructions": "", "enabled": True,
                               "skills": None, "fish_seed": "seed-c",
                               "model": "", "effort": "", "backup": "", "escalate": ""}
            with store.tx() as conn:
                store.kv_set(conn, _agents_mod._KV_PROFILES, raw)

    def get_settings(self):
        settings, _ = load_settings(
            {
                "groups": {"serve": [{"group": f"qq:{GID}"}, {"group": f"qq:{GID2}"}]},
                "console": {"board_hours": 24},
            }
        )
        return settings


# ----------------------------------------------------------------------
# 播种：六类来源 + 新老记录，全用固定时间戳（不碰 clock.now）
# ----------------------------------------------------------------------


def _seed(store: Store) -> dict:
    """种满各表；返回 {source: [id...]} 方便枚举详情。所有 ts 都以 NOW 为基准。"""
    ids: dict[str, list] = {
        "handoff": [], "task": [], "news_batch": [], "idea": [],
        "goal": [], "topic": [], "main": [],
    }
    _Svc(store)  # 懒建的表（agent_handoffs 等）先就位，顺便注册自定义岗
    with store.tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO groups (group_id, name, created) VALUES (?,?,?)",
            (GID, "黄金群一", 1_700_000_000.0),
        )
        conn.execute(
            "INSERT OR REPLACE INTO groups (group_id, name, created) VALUES (?,?,?)",
            (GID2, "黄金群二", 1_700_000_000.0),
        )
        conn.execute(
            "INSERT OR REPLACE INTO groups (group_id, name, created) VALUES (?,?,?)",
            ("555555555", "非服务群", 1_700_000_000.0),
        )

        # ---- 交接单：running（一个卡住、一个新鲜）/ queued / 三种终态 / 非服务群 ----
        for hid, kind, status, ts, task_id, phase in [
            ("h-run-stuck", "news", "running", NOW - 700, "feeds-verify:g:1:0", "verify"),
            ("h-run", "idea", "running", NOW - 100, "", ""),
            ("h-queued", "news", "queued", NOW - 50, "", ""),
            ("h-acc", "news", "accepted", NOW - 300, "feeds-verify:g:2:0", "verify"),
            ("h-rej", "c_ab12cd", "rejected", NOW - 400, "", ""),
            ("h-fail", "goal", "failed", NOW - 500, "", ""),
            ("h-other", "news", "running", NOW - 60, "", ""),  # 群换成 555555555 下播
        ]:
            gid = "555555555" if hid == "h-other" else GID
            conn.execute(
                "INSERT INTO agent_handoffs"
                " (id, group_id, kind, task_id, phase, parent_id, status, brief,"
                "  criteria, tools, skills, summary, data, evidence, review, error, created, updated)"
                " VALUES (?,?,?,?,?,?,?,?, '[]','[]','[]','','','[]','','',?,?)",
                (hid, gid, kind, task_id, phase, "", status, f"交接单 {hid}", ts, ts),
            )
            if hid != "h-other":
                ids["handoff"].append(hid)

        # ---- 任务：running / waiting_input（不 stuck）/ feature_off 搁置 / done ----
        for tid, status, ts, extra in [
            ("T-run", "running", NOW - 900, {}),
            ("T-wait", "waiting_input", NOW - 3600, {}),
            ("T-off", "queued", NOW - 7200, {"paused_reason": '{"kind":"feature_off"}'}),
            ("T-done", "done", NOW - 400, {"finished_ts": NOW - 200, "started_ts": NOW - 390}),
            ("T-old", "done", NOW - 30 * 3600, {"finished_ts": NOW - 29 * 3600,
                                                "started_ts": NOW - 30 * 3600}),
        ]:
            cols = "id, group_id, workspace, source, request_id, goal_id, requester_id, requester_name, icon, title, req, criteria, status, env, delivery_kind, created, updated, started_ts, finished_ts"
            conn.execute(
                f"INSERT INTO tasks ({cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, GID, "ws", "", "", None, "999999999", "发起人", "package",
                 f"任务 {tid}", "需求", "[]", status, "", "", ts, ts,
                 extra.get("started_ts"), extra.get("finished_ts")),
            )
            if extra.get("paused_reason"):
                conn.execute("UPDATE tasks SET paused_reason=? WHERE id=?",
                             (extra["paused_reason"], tid))
            ids["task"].append(tid)
        conn.execute(
            "INSERT INTO attempts (task_id, n, req_version, status, started, finished)"
            " VALUES (?,?,?,?,?,?)",
            ("T-done", 1, 1, "passed", NOW - 390, NOW - 200),
        )

        # ---- 资讯批次：带 run_ref 的新行 / run_ref 空的老行 / skipped / 个人向 / 非服务群 ----
        for found, kept, skipped, note, ts, ref in [
            (10, 3, 0, "", NOW - 150, "news:900000001:1799999999000:1"),
            (8, 0, 0, "", NOW - 400, ""),                      # 老行：时间窗回落
            (12, 0, 1, "时机不对", NOW - 500, "news:g:x:2"),
            (5, 2, 0, "personal:某成员", NOW - 600, ""),       # 不上板
            (7, 1, 0, "", NOW - 700, "news:other:3"),          # 群换成 555 下播
        ]:
            gid = "555555555" if ref == "news:other:3" else GID
            cur = conn.execute(
                "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created, run_ref)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (gid, ts, found, kept, skipped, note, ts, ref),
            )
            ids["news_batch"].append(int(cur.lastrowid or 0))

        # ---- 构想：带 run_ref / 老行 / 个人向（标题遮蔽）----
        for title, target, ts, ref in [
            ("群构想甲", "", NOW - 160, "idea:900000001:1799999998000:2"),
            ("老构想乙", "", NOW - 450, ""),
            ("送给某人的礼物", "8888888", NOW - 170, ""),      # 个人向 → 「个人构想」
        ]:
            cur = conn.execute(
                "INSERT INTO ideas (group_id, title, body, basis, state, created, updated, target_user_id, run_ref)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (GID, title, "正文", "依据原话", "new", ts, ts, target, ref),
            )
            ids["idea"].append(int(cur.lastrowid or 0))

        # ---- 目标：active（新立）/ done / cancelled / 窗口外 ----
        for gid_goal, title, state, created, updated in [
            ("G-1", "盯住发布页", "active", NOW - 120, NOW - 120),
            ("G-2", "老目标完成", "done", NOW - 5000, NOW - 130),
            ("G-3", "取消了的目标", "cancelled", NOW - 4000, NOW - 140),
            ("G-4", "太老的目标", "done", NOW - 40 * 3600, NOW - 39 * 3600),
        ]:
            conn.execute(
                "INSERT INTO goals (id, group_id, kind, title, state, created, updated)"
                " VALUES (?,?,?,?,?,?,?)",
                (gid_goal, GID, "agent", title, state, created, updated),
            )
            ids["goal"].append(gid_goal)

        # ---- 开话题：带 run_ref（+同轮 judgments）/ 老行 / 拒开 ----
        jev_json = json.dumps({"ok": True, "ok_p": 0.9, "reason": "群里冷了一会儿了",
                               "fit_title": "候选话题甲"}, ensure_ascii=False)
        cur = conn.execute(
            "INSERT INTO topic_log (group_id, ts, quiet_s, usual_gap_s, jev, pick, candidate_id,"
            " opener, message_id, followup_due_ts, result, verdict, run_ref)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (GID, NOW - 180, 300.0, None, jev_json, "{}", None, "开场白", "", None, "sent",
             "good", "topic:900000001:1799999997000:3"),
        )
        ids["topic"].append(int(cur.lastrowid or 0))
        cur = conn.execute(
            "INSERT INTO topic_log (group_id, ts, quiet_s, usual_gap_s, jev, pick, candidate_id,"
            " opener, message_id, followup_due_ts, result, verdict, run_ref)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (GID, NOW - 460, 300.0, None, "{}", "{}", None, "", "", None,
             json.dumps({"rejected": "没有合适候选"}), "bad", ""),
        )
        ids["topic"].append(int(cur.lastrowid or 0))
        conn.execute(
            "INSERT INTO judgments (ts, day, purpose, group_id, state_summary, answers, ms, ok, error, verdict, run_ref)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (NOW - 181, "2027-01-14", "topic.check", GID, "{}", "{}", 30, 1, "", "",
             "topic:900000001:1799999997000:3"),
        )

        # ---- 主模型条目：running（新鲜 + 过中断时限）/ done / failed ----
        for purpose, title, status, started, finished, ref, gid in [
            ("admin_chat", "回答管理员", "done", NOW - 800, NOW - 750,
             "main:admin_chat:g:8:0", GID),
            ("intake.quick_judge", "判断 @ 是不是派活", "running", NOW - 60, 0,
             "main:intake.quick_judge:g:9:0", GID),
            ("p", "判断", "running", NOW - 7 * 3600, 0, "main:p:g:10:0", GID),  # 过时限→中断
            ("feeds.mention_judge", "复盘一轮", "failed", NOW - 900, NOW - 850,
             "main:feeds.mention_judge:g:11:0", GID),
            ("x", "别群的", "done", NOW - 200, NOW - 190, "main:x:other:12:0",
             "555555555"),
        ]:
            cur = conn.execute(
                "INSERT INTO main_runs (group_id, purpose, title, status, started, finished, ok, error, ref)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (gid, purpose, title, status, started, finished,
                 1 if status == "done" else 0, "", ref),
            )
            if gid == GID:
                ids["main"].append(int(cur.lastrowid or 0))

        # ---- tool_calls / model_calls：task_id 直连 + run_ref 直连 + 时间窗老行 ----
        def _tc(ts, gid_, task_id="", run_ref="", tool="web_search"):
            conn.execute(
                "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ms, ok, error, run_ref)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ts, gid_, task_id, "子 agent", tool, "输入", "输出", 10, 1, "", run_ref),
            )

        def _mc(ts, gid_, task_id="", run_ref="", agent="news", text="想了一下"):
            conn.execute(
                "INSERT INTO model_calls (ts, purpose, role, agent, model, group_id, task_id, attempt, ok,"
                " status, ms, prompt_tokens, completion_tokens, error, request, response,"
                " usage_src, cache_read, cache_write, run_ref)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, "p", "worker", agent, "m1", gid_, task_id, 1, 1, 200, 20, 3, 5, "",
                 "{}", json.dumps({"text": text, "reasoning": "想的过程",
                                    "tool_calls": [], "finish_reason": "stop"},
                                   ensure_ascii=False), "reported", 0, 0, run_ref),
            )

        # h-run-stuck 的过程（task_id 直连）
        _tc(NOW - 650, GID, task_id="feeds-verify:g:1:0")
        _mc(NOW - 640, GID, task_id="feeds-verify:g:1:0", text="核验中")
        # T-run / T-done 的过程
        _tc(NOW - 800, GID, task_id="T-run")
        _mc(NOW - 350, GID, task_id="T-done", agent="task", text="做完了")
        # news_batch 精确行的过程（run_ref 直连）
        _tc(NOW - 155, GID, run_ref="news:900000001:1799999999000:1")
        _mc(NOW - 152, GID, run_ref="news:900000001:1799999999000:1", text="打分")
        # news_batch 老行：窗口内同群调用（不带 task_id/run_ref）
        _mc(NOW - 410, GID, task_id="feeds-collect:g:7:7", text="老轮打分")
        # idea 精确行
        _mc(NOW - 165, GID, run_ref="idea:900000001:1799999998000:2", text="出构想")
        # topic 精确行
        _mc(NOW - 179, GID, run_ref="topic:900000001:1799999997000:3",
            agent="main", text="开场白草稿")
        # main done 行
        _mc(NOW - 780, GID, run_ref="main:admin_chat:g:8:0", agent="main",
            text="管理员回复")
        _tc(NOW - 770, GID, run_ref="main:admin_chat:g:8:0", tool="probe")
        # goal G-1 的检查（goal-check:<id> task_id）
        _mc(NOW - 100, GID, task_id="goal-check:G-1", agent="goal", text="盯一眼")

    return ids


# ----------------------------------------------------------------------
# 期望值：旧实现生成，重构后逐字段比（now 固定 → 输出完全确定）
# ----------------------------------------------------------------------


def _views(store: Store, ids: dict) -> dict:
    """跑一次 board_view + 每个条目的 item_view，整理成可比对结构。"""
    svc = _Svc(store)
    out = {"board": board_view(svc, hours=24, now=NOW), "items": {}}
    for source, source_ids in ids.items():
        for iid in source_ids:
            view = item_view(svc, source, str(iid), now=NOW)
            out["items"][f"{source}/{iid}"] = view
    return out


def _freeze(obj):
    """排序无关紧要的处不动（实现必须逐字段一致，包括顺序）——直接原样 dump。"""
    return obj


def _diff(a, b, path=""):
    """逐字段比，返回第一条差异的说明（没有 → 空串）。"""
    if type(a) is not type(b):
        return f"{path}: 类型 {type(a).__name__} → {type(b).__name__}"
    if isinstance(a, dict):
        if set(a) != set(b):
            return f"{path}: 键 {sorted(set(a) ^ set(b))} 不一致"
        for k in a:
            d = _diff(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return ""
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: 长度 {len(a)} → {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = _diff(x, y, f"{path}[{i}]")
            if d:
                return d
        return ""
    if a != b:
        return f"{path}: {a!r} → {b!r}"
    return ""


_GOLDEN_PATH = Path(__file__).with_name("fixtures") / "board_golden.json"


class TestBoardGolden:
    """lanes + 每个 item_view 逐字段对齐冻结值。"""

    @pytest.fixture(scope="class")
    @classmethod
    def seeded(cls, tmp_path_factory):
        store = Store(tmp_path_factory.mktemp("golden") / "t.db")
        store.migrate()
        ids = _seed(store)
        yield store, ids
        store.close()

    def test_golden_file_exists(self):
        assert _GOLDEN_PATH.is_file(), (
            f"缺黄金文件 {_GOLDEN_PATH}：重构前先用旧实现生成（本文件尾注释有方法）")

    def test_board_and_items_match_golden(self, seeded):
        """lanes + 每个 item_view 逐字段对齐冻结值。新版 board_view 只剩
        now/hours/stuck_after_s/lanes（第一版的 running/agents/recent/agent_fish
        已删），只比 lanes；旧字段的有无由 test_board.py 专门守。"""
        store, ids = seeded
        golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
        got = _views(store, ids)
        d = _diff(got["board"]["lanes"], golden["board"]["lanes"], "board.lanes")
        assert not d, f"board_view 不一致：{d}"
        for key, expected in golden["items"].items():
            d = _diff(got["items"].get(key), expected, f"items.{key}")
            assert not d, f"item_view 不一致：{d}"
        # 反向：新实现不该多出golden没有的条目视图（源 id 集相同是 _views 自己枚举的）
        assert set(got["items"]) == set(golden["items"])


class TestItemSameShape:
    """「同一件活只有一种形状」（2026-10-30 主会话定）：详情接口回的 item 和
    该件在 lanes 里那件逐字段一致（详情可以多 error / counts / last_step），
    stuck / elapsed_s / title / updated / finished 全部按 lanes 口径算。"""

    @pytest.fixture(scope="class")
    @classmethod
    def seeded(cls, tmp_path_factory):
        store = Store(tmp_path_factory.mktemp("shape") / "t.db")
        store.migrate()
        ids = _seed(store)
        yield store, ids
        store.close()

    def test_detail_item_matches_lanes_item(self, seeded):
        store, _ids = seeded
        svc = _Svc(store)
        board = board_view(svc, hours=168, now=NOW)
        diffs: list[str] = []
        seen: set[str] = set()
        for lane in board["lanes"]:
            for sec in ("running", "history"):
                for row in lane[sec]:
                    key = f"{row['source']}/{row['id']}"
                    seen.add(key)
                    view = item_view(svc, row["source"], str(row["id"]), now=NOW)
                    if view is None:
                        diffs.append(f"{key}: lanes 有但 item_view 是 None")
                        continue
                    detail = {k: v for k, v in view["item"].items() if k != "error"}
                    d = _diff(row, detail, f"{key}")
                    if d:
                        diffs.append(d)
                    # counts / last_step 详情也给全（和 lanes 同一份）
                    if "counts" not in view["item"]:
                        diffs.append(f"{key}: 详情缺 counts")
                    elif view["item"]["counts"] != row.get("counts"):
                        diffs.append(
                            f"{key}: counts {view['item']['counts']!r} ≠ lanes {row.get('counts')!r}")
                    if view["item"].get("last_step") != row.get("last_step"):
                        diffs.append(f"{key}: last_step 不一致")
        assert not diffs, "详情与列表不同形：\n" + "\n".join(diffs)
        assert seen, "种子库一件都没有，测试没意义"

    def test_handoff_without_task_id_reports_time_window(self, seeded):
        """link 诚实：没挂 task_id 的交接单实际按同群+时间窗查，link 必须如实报
        time_window（不再拿 link_kind 覆盖成 exact）。"""
        store, _ids = seeded
        svc = _Svc(store)
        view = item_view(svc, "handoff", "h-run", now=NOW)
        assert view is not None
        assert view["link"] == "time_window"
        view = item_view(svc, "handoff", "h-acc", now=NOW)
        assert view is not None and view["link"] == "exact"


# ----------------------------------------------------------------------
# 重新生成 GOLDEN（只能在「确认现状是对的」的时候做）：
#   source ~/.venvs/maiwork/bin/activate
#   python -c "
#   import json, pathlib, tempfile
#   from tests.test_board_golden import _seed, _views
#   from CharTyr_MaiWork.maiwork.store import Store
#   s = Store(tempfile.mkdtemp() + '/t.db'); s.migrate()
#   ids = _seed(s)
#   pathlib.Path('tests/fixtures/board_golden.json').write_text(
#       json.dumps(_views(s, ids), ensure_ascii=False, indent=2, sort_keys=True))
#   "
# ----------------------------------------------------------------------
