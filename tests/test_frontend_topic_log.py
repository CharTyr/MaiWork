"""「群」页开话题记录（js/topiclog.js）：按冷场合并、显示真实分数和门槛、讲清楚卡在哪。

线上 2026-10-01（测试群）：主循环每 30 秒判一次，一天 343 条「忍住了没开」刷满记录页；
页面上的「把握 0.48」其实是「原因」那道选择题的把握，不是「适合开」的分数，
还出现过「不适合 · 可以开」这种自相矛盾的话。

这里用 node 真跑 topiclog.js 的纯函数（不开浏览器）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[1] / "maiwork" / "console" / "static" / "js"

_CHECK = r"""
const m = await import(%s);
const start = 1790845000;
// 旧记录：同一段冷场每 30 秒一条，没 ok_p / stretch_ts
const old = [0, 1, 2, 3].map((k) => ({
  id: 10 + k, ts: start + 1300 + k * 30, quiet_s: 1300 + k * 30, usual_gap_s: 84,
  jev: { ok: false, reason: "可以开", confidence: 0.21, detail: "" }, pick: {}, opener: "", verdict: null,
}));
// 新记录：另一段，时机过了但候选不对胃口
const fresh = [
  { id: 30, ts: start + 9000, quiet_s: 1250, usual_gap_s: 84, opener: "", pick: {}, verdict: "wrong",
    jev: { ok: true, reason: "可以开", confidence: 0.7, detail: "", ok_p: 0.66, ok_need: 0.6,
           fit_best: 0.33, fit_need: 0.5, fit_title: "战锤40K预告", stuck: "candidate", stretch_ts: start + 7750 } },
  { id: 29, ts: start + 8950, quiet_s: 1200, usual_gap_s: 84, opener: "", pick: {}, verdict: null,
    jev: { ok: false, reason: "有问题还没人回", confidence: 0.5, detail: "", ok_p: 0.43, ok_need: 0.6,
           fit_best: 0.3, fit_need: 0.5, fit_title: "x", stuck: "timing", stretch_ts: start + 7750 } },
];
const opened = { id: 40, ts: start + 20000, quiet_s: 1500, usual_gap_s: 84, opener: "话说之前大家聊的那个挑战怎么样了？",
  pick: { title: "挑战", fit: 0.7 }, verdict: null, result: { replies: 2 },
  jev: { ok: true, reason: "可以开", confidence: 0.8, detail: "", ok_p: 0.7, fit_best: 0.7, stuck: null, stretch_ts: start + 18500 } };
const log = [opened, ...fresh, ...old.slice().reverse()];
const st = m.stretches(log);
const out = {
  n: st.length,
  counts: st.map((s) => s.n),
  reps: st.map((s) => s.rep.id),
  firsts: st.map((s) => s.first.id),
  opened: st.map((s) => s.opened),
  bestOk: st.map((s) => s.bestOk),
  freshText: m.judgeText(fresh[0]),
  timingText: m.judgeText(fresh[1]),
  oldText: m.judgeText(old[0]),
  openedText: m.judgeText(opened),
  noJev: m.judgeText({ jev: null }),
  summary: m.daySummary(log, () => true),
  summaryNone: m.daySummary(log, () => false),
};
console.log(JSON.stringify(out));
"""


def _run(tmp_path: Path) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("本机没有 node，跑不了开话题记录检查")
    script = tmp_path / "check.mjs"
    script.write_text(_CHECK % json.dumps((JS / "topiclog.js").as_uri()), encoding="utf-8")
    res = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_rows_of_one_quiet_stretch_merge_into_one_card(tmp_path):
    d = _run(tmp_path)
    assert d["n"] == 3, "三段冷场：开了的一段、新记录一段（两次判断）、旧记录一段（四次判断）"
    assert d["counts"] == [1, 2, 4]
    # 每段的代表：开了的那条 / 没开就是这段最新那条（「对 / 不对」点在它上面）
    assert d["reps"] == [40, 30, 13]
    assert d["firsts"] == [40, 29, 10], "first 是这段最早的一次判断"
    assert d["opened"] == [True, False, False]
    assert d["bestOk"][1] == pytest.approx(0.66)
    assert d["bestOk"][2] is None, "旧记录没存分数，不编数字"


def test_judge_text_shows_real_score_threshold_and_where_it_got_stuck(tmp_path):
    d = _run(tmp_path)
    t = d["freshText"]
    assert "适合开 0.66（要 0.60）" in t
    assert "最合适的候选 0.33（要 0.50）" in t and "战锤40K预告" in t
    assert "卡在：候选不对胃口" in t
    assert "把握" not in t, "不再把「原因」的把握冒充成分数"
    t2 = d["timingText"]
    assert "适合开 0.43（要 0.60）" in t2 and "原因：有问题还没人回" in t2 and "卡在：时机不对" in t2
    assert "卡在" not in d["openedText"]


def test_old_records_do_not_contradict_themselves(tmp_path):
    d = _run(tmp_path)
    t = d["oldText"]
    assert "可以开" not in t, "旧记录 ok=false 却写「不适合 · 可以开」是自相矛盾"
    assert "不适合" in t and "旧记录没存分数" in t
    assert "把握" not in t
    assert d["noJev"] == "没问 Jev"


def test_day_summary_counts_stretches_and_best_scores(tmp_path):
    d = _run(tmp_path)
    s = d["summary"]
    assert "3 段冷场" in s and "开了 1 次" in s
    assert "适合开最高 0.70（要 0.60）" in s
    assert "候选最高 0.70（要 0.50）" in s
    assert d["summaryNone"] == ""


def test_group_page_uses_topiclog_module():
    g = (JS / "pages/group.js").read_text(encoding="utf-8")
    assert 'from "../topiclog.js"' in g
    assert "stretches(" in g and "daySummary(" in g
    assert "把握" not in g, "群页不再显示「原因」的把握"
