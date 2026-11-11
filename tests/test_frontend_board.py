"""网页看板（js/board.js + js/board_steps.js）：只总管理员、只看不动、停在看板时 10 秒刷新。

看板页是 2026-10-29 用户要的：实时看 MaiWork 各专岗在做什么、最近 N 小时做了什么。
2026-11-05 看板精修第三步（前端）：时间窗切换、状态筛选、空闲栏收起、卡片错误 /
便条角标、抽屉里轮次与步骤筛选、工具一行摘要、跳回条目页、最近便条抽屉。
board_steps.js 是纯函数模块（和 topiclog.js 一样不碰 DOM），这里用 node 直接测；
board.js 依赖浏览器的 matchMedia，只做源码级检查。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[1] / "maiwork" / "console" / "static" / "js"
if not JS.is_dir():  # 找不到插件目录直接报错，不许假绿
    raise RuntimeError(f"找不到前端目录：{JS}")

# 只读动作白名单：筛选 / 折叠 / 跳转 / 开抽屉，不许暂停 / 取消 / 批准之类的写操作
READONLY_ACTS = {
    "board-filter", "board-hours", "board-status", "board-reset", "board-lane-more",
    "board-item", "board-step-filter", "board-round-filter", "board-jump-end",
    "board-goto", "board-notes", "chat-ask-ref",
    "board-flow-toggle", "board-ctx-view", "board-ctx-part",
}


def _read(name: str) -> str:
    return (JS / name).read_text(encoding="utf-8")


def _node(body: str) -> subprocess.CompletedProcess:
    """用 node 直接 import board_steps.js 跑单测（它是纯函数模块；state.js 要 window.matchMedia，垫一下）。"""
    setup = (
        "globalThis.window = { matchMedia: () => ({ matches: false, addEventListener() {} }) };\n"
        f'const mod = await import("file://{(JS / "board_steps.js").as_posix()}");\n'
    )
    return subprocess.run(
        ["node", "--input-type=module", "-e", setup + body],
        capture_output=True, text=True, timeout=20,
    )


def _assert_js(body: str) -> subprocess.CompletedProcess:
    res = _node(body)
    assert res.returncode == 0, res.stderr
    return res


# ───────────── 只读红线 ─────────────


def test_board_reads_only_board_api() -> None:
    src = _read("board.js")
    calls = re.findall(r'api\(\s*"([A-Z]+)"\s*,\s*[`"]([^`"$?]+)', src)
    # 只读：列表（可带 ?hours=）+ 单件过程明细（可带 ?after=），都是 GET
    assert sorted(calls) == [("GET", "/api/board"), ("GET", "/api/board/items/")]
    assert "hours=" in src and "after=" in src
    # board_steps.js 完全不发请求
    assert "api(" not in _read("board_steps.js")


def test_board_has_no_operation_buttons() -> None:
    acts = set()
    for name in ("board.js", "board_steps.js"):
        acts |= set(re.findall(r'data-act="([a-z-]+)"', _read(name)))
    # 只许筛选和跳转到现有页面，不许暂停 / 取消 / 批准之类的动作
    assert acts <= READONLY_ACTS, acts


def test_board_is_admin_only_in_router_and_rail() -> None:
    router = _read("router.js")
    assert 'h.board && admin() ? "board"' in router
    render = _read("render.js")
    # 左栏入口只在 admin() 时出现
    assert re.search(r'admin\(\) \? `<button class="r-item" data-act="board"', render)


def test_board_polls_every_10s_only_when_visible_on_board() -> None:
    router = _read("router.js")
    m = re.search(r"setInterval\(\(\) => \{\n  if \(document\.hidden \|\| state\.page !== \"board\" \|\| !admin\(\)\) return;.*?\}, (\d+)\);", router, re.S)
    assert m, "看板定时刷新没接上"
    assert m.group(1) == "10000"


def test_board_hides_failure_state_honestly() -> None:
    src = _read("board.js")
    assert 'loading(fail, "board")' in src
    assert "连接中断" in src
    router = _read("router.js")
    assert 'what === "board"' in router


# ───────────── 页头与筛选 ─────────────


def test_board_header_toolbar_and_filters() -> None:
    src = _read("board.js")
    assert "board-bar" in src and "board-filters" in src
    # 说明收进 tip
    assert '看板${tip(' in src
    assert "实时展示各岗位正在执行与近期完成的工作，每 10 秒自动刷新，仅供查看。" in src
    # 时间窗
    assert 'data-act="board-hours"' in src and "state.boardHours" in src
    for h, label in (("6", "6 小时"), ("24", "24 小时"), ("72", "3 天"), ("168", "7 天")):
        assert f'data-h="{h}"' in src and label in src
    # 状态快筛
    assert 'data-act="board-status"' in src and "state.boardStatus" in src
    for st in ("all", "active", "failed"):
        assert f'data-st="{st}"' in src
    assert "进行中与卡住" in src and "没做成" in src
    # 群筛选照旧在
    assert 'data-act="board-filter"' in src
    # 筛选后全空 → 空态 + 重置筛选
    assert "当前筛选下没有工作" in src
    assert 'data-act="board-reset"' in src and "重置筛选" in src
    # 便条入口
    assert 'data-act="board-notes"' in src and "最近便条" in src


def test_board_lanes_show_running_history_and_fish() -> None:
    src = _read("board.js")
    assert "lane.running" in src and "lane.history" in src
    assert "进行中" in src and "历史" in src
    assert "fishSvg(`agent:${kind}:${seed}`" in src  # 和各页一样的专岗小鱼


def test_board_lane_compaction_and_card_badges() -> None:
    src = _read("board.js")
    # 空闲栏收起
    assert "lane.idle" in src or 'lane idle' in src
    # 历史超 8 条折叠
    assert 'data-act="board-lane-more"' in src and "state.boardLaneOpen" in src
    assert "展开其余" in src and "收起" in src
    # 卡片错误与交接 / 便条角标
    assert "bcard-err" in src and "x.error_brief" in src
    assert "x.handoff_count" in src and "轮交接" in src
    assert "x.note_count" in src and "便条" in src


# ───────────── 抽屉与跳转 ─────────────


def test_board_item_sheet_shows_tools_and_thinking() -> None:
    src = _read("board.js") + _read("board_steps.js")
    assert 's.type === "tool"' in src and 's.type === "think"' in src
    for word in ("输入", "输出", "思考", "工具"):
        assert word in src
    sheet = _read("sheet.js")
    assert 'kind === "board-item"' in sheet


def test_board_think_step_shows_reasoning_or_redacted_note() -> None:
    src = _read("board.js") + _read("board_steps.js")
    assert "s.reasoning" in src and "思考过程" in src
    assert "s.reasoning_redacted" in src


def test_board_drawer_error_jump_and_filters() -> None:
    src = _read("board.js") + _read("board_steps.js")
    # 错误显眼呈现
    assert "bad-box" in src and "it.error" in src
    # 跳回条目页
    assert 'data-act="board-goto"' in src and "it.jump" in src
    for label in ("去任务页查看", "去目标详情查看", "去构想页查看", "去资讯页查看", "去本群页查看"):
        assert label in src
    # 轮次总览 + 轮次筛选
    assert 'data-act="board-round-filter"' in src and "全部轮次" in src
    assert "第 ${" in src and "轮" in src
    # 步骤快筛 + 跳到最新
    assert 'data-act="board-step-filter"' in src and 'data-sf="all"' in src
    assert 'data-sf="think"' in src and 'data-sf="tool"' in src and 'data-sf="failed"' in src
    assert 'data-act="board-jump-end"' in src and "跳到最新" in src
    # 截断照实说
    assert "中间已省略部分步骤" in src


def test_board_notes_sheet_wired_in_sheet_js() -> None:
    src = _read("board.js")
    assert "export function boardNotesSheet" in src or "export const boardNotesSheet" in src
    assert "n.handoff_id" in src or "handoff_id" in src
    sheet = _read("sheet.js")
    assert 'kind === "board-notes"' in sheet and "boardNotesSheet" in sheet


def test_board_actions_delegate_to_actboard() -> None:
    acts = _read("actions.js")
    assert "if (await actBoard(a, el)) return;" in acts
    # board 分支都搬进 board.js，actions.js 不再留 case
    for a in ("board-filter", "board-item", "board-hours", "board-status"):
        assert f'case "{a}"' not in acts, a
    board = _read("board.js")
    assert "export async function actBoard" in board
    for a in ("board-filter", "board-item", "board-hours", "board-status", "board-reset",
              "board-lane-more", "board-step-filter", "board-round-filter",
              "board-jump-end", "board-goto", "board-notes"):
        assert f'case "{a}"' in board, a


def test_board_step_round_filters_cleaned_on_close() -> None:
    sheet = _read("sheet.js")
    m = re.search(r'state\.sheet === "board-item"(.{0,500})', sheet, re.S)
    assert m, "关闭看板抽屉时没有清理步骤 / 轮次筛选状态"
    body = m.group(1)
    assert "boardStepFilter" in body and "boardRound" in body


def test_board_css_exists() -> None:
    css = (JS.parent / "style.css").read_text(encoding="utf-8")
    for sel in (".board-bar", ".lane.idle", ".lane-more", ".bcard-err",
                ".bi-round", ".st-sum", ".bad-box", ".st-filters"):
        assert sel in css, sel


# ───────────── node 单测：toolSummary ─────────────


def test_tool_summary_known_tools() -> None:
    _assert_js(r'''
const { toolSummary } = mod;
const eq = (a, b) => { if (a !== b) throw new Error(JSON.stringify(a) + ' !== ' + JSON.stringify(b)); };
eq(toolSummary('web_search', JSON.stringify({q: 'Switch 2 固件'}), ''), '搜：Switch 2 固件');
eq(toolSummary('social_search', JSON.stringify({query: '本地模型', platform: 'bilibili'}), ''), '搜：本地模型（bilibili）');
eq(toolSummary('fetch_page', JSON.stringify({url: 'https://example.com/docs/a'}), ''), '读网页：example.com/docs/a');
eq(toolSummary('run_command', JSON.stringify({cmd: 'pytest -q'}), ''), '$ pytest -q');
eq(toolSummary('vm_run', JSON.stringify({command: 'ls /tmp'}), ''), '$ ls /tmp');
eq(toolSummary('read_file', JSON.stringify({path: '/x/report.md'}), ''), '文件：report.md');
eq(toolSummary('submit_result', JSON.stringify({summary: '已完成核对'}), ''), '交回：已完成核对');
eq(toolSummary('submit_result', '{}', JSON.stringify({summary: '交了'})), '交回：交了');
eq(toolSummary('other_tool', JSON.stringify({title: '短标题'}), ''), '短标题');
eq(toolSummary('other_tool', JSON.stringify({nums: [1, 2]}), ''), '');
eq(toolSummary('other_tool', '不是json', ''), '');
''')


def test_tool_summary_never_throws_and_truncates() -> None:
    _assert_js(r'''
const { toolSummary } = mod;
const ok = (f) => { try { return f(); } catch (e) { throw new Error('threw: ' + e); } };
ok(() => toolSummary('web_search', null, null));
ok(() => toolSummary('web_search', 42, {}));
ok(() => toolSummary('web_search', '{bad json', ''));
ok(() => toolSummary('fetch_page', JSON.stringify({url: 'not a url'}), ''));
if (toolSummary('web_search', JSON.stringify({q: '长'.repeat(100)}), '').length > 72) throw new Error('太长');
// 畸形 JSON / 原始字符串不抛错；已知工具拿原始字符串当退路
if (toolSummary('run_command', 'echo hi', '') !== '$ echo hi') throw new Error('原始命令');
if (toolSummary('web_search', '直接字符串', '') !== '搜：直接字符串') throw new Error('原始查询');
''')


# ───────────── node 单测：mergeSteps ─────────────


def test_merge_steps_dedups_by_uid_and_keeps_same_ts() -> None:
    _assert_js(r'''
const { mergeSteps } = mod;
const prev = { steps: [
  {uid: 's:1', ts: 1, type: 'tool', tool: 'a'},
  {uid: 's:2', ts: 1, type: 'think', text: 'x'},
  {ts: 1, type: 'status', text: '开始'}, // 无 uid：按内容键去重
] };
const next = { steps: [
  {uid: 's:2', ts: 1, type: 'think', text: 'x'},  // 重复，丢弃
  {uid: 's:3', ts: 1, type: 'tool', tool: 'b'},   // 同时戳不同 uid：不能丢
  {uid: 's:4', ts: 2, type: 'think', text: 'y'},
] };
const m = mergeSteps(prev, next);
const uids = m.steps.map((s) => s.uid);
if (JSON.stringify(uids) !== JSON.stringify(['s:1', 's:2', undefined, 's:3', 's:4']))
  throw new Error('merge 顺序错: ' + JSON.stringify(m.steps));
if (!mergeSteps(null, next) || mergeSteps({steps: []}, {truncated: true, steps: []}).truncated !== true)
  throw new Error('prev 空 / next 截断应直接返回');
''')


def test_merge_steps_finished_transition_keeps_end_status() -> None:
    _assert_js(r'''
const { mergeSteps } = mod;
const prev = { item: {finished: null}, steps: [
  {uid: 's:1', ts: 1, type: 'status', text: '进行中'},
  {uid: 's:2', ts: 2, type: 'tool', tool: 'a'},
] };
const next = { item: {finished: 9}, steps: [{uid: 's:end:9', ts: 9, type: 'status', text: '已完成'}] };
const m = mergeSteps(prev, next);
if (!m.steps.some((s) => s.uid === 's:end:9')) throw new Error('终态步骤丢了');
''')


def test_merge_steps_caps_at_500_with_omit_marker() -> None:
    _assert_js(r'''
const { mergeSteps } = mod;
const big = Array.from({length: 620}, (_, i) => ({uid: 'u' + i, ts: i, type: 'think'}));
const m = mergeSteps({steps: []}, {steps: big});
if (m.steps.length !== 500) throw new Error('长度应为 500: ' + m.steps.length);
if (m.truncated !== true) throw new Error('应标记 truncated');
if (m.steps[0].uid !== 'u0' || m.steps[499].uid !== 'u619') throw new Error('首尾应保留');
if (!m.steps.some((s) => s.type === 'status' && s.uid === 's:omit')) throw new Error('缺省略标记');
''')


# ───────────── node 单测：stepsSectionHTML ─────────────


const_D = r'''
const d = { steps: [
  {uid: 's:1', ts: 1, type: 'think', text: '想 <script>alert(1)</script>', round_id: 'r1'},
  {uid: 's:2', ts: 2, type: 'tool', tool: 'run_command', input: '{"cmd":"pytest -q"}', output: '', ok: true, round_id: 'r1'},
  {uid: 's:3', ts: 3, type: 'tool', tool: 'read_file', input: '{"path":"a.md"}', ok: false, error: '读不了', round_id: 'r2'},
] };
'''


def test_steps_section_filters_and_escapes() -> None:
    _assert_js(const_D + r'''
const { stepsSectionHTML } = mod;
const all = stepsSectionHTML(d, {type: 'all', round: '', open: {}});
if (!all.includes('st-filters') || !all.includes('data-act="board-step-filter"')) throw new Error('缺快筛');
if (!all.includes('$ pytest -q')) throw new Error('工具一行摘要没出来');
if (all.includes('<script>alert(1)</script>')) throw new Error('XSS 没转义');
if (!all.includes('&lt;script&gt;')) throw new Error('应转义 <script>');
if (!all.includes('st-sum')) throw new Error('缺 st-sum 摘要行');
const think = stepsSectionHTML(d, {type: 'think', round: '', open: {}});
if (think.includes('run_command')) throw new Error('think 筛不该有工具');
if (!think.includes('aria-pressed="true"')) throw new Error('缺 aria-pressed');
const failed = stepsSectionHTML(d, {type: 'failed', round: '', open: {}});
if (failed.includes('run_command') || !failed.includes('read_file')) throw new Error('failed 筛错');
''')


def test_steps_section_round_filtering() -> None:
    _assert_js(r'''
const { stepsSectionHTML } = mod;
const d = {
  rounds: [
    {id: 'r1', no: 1, kind: 'task', kind_title: '执行', phase: '', status: 'done', status_text: '已完成', started: 1, finished: 2, ok: true, summary: '做了'},
    {id: 'r2', no: 2, kind: 'task', kind_title: '检查', phase: '', status: 'failed', status_text: '没做成', started: 3, finished: 4, ok: false, summary: '', review: '', error: '超时'},
  ],
  steps: [
    {uid: 's:1', ts: 1, type: 'think', text: '一', round_id: 'r1'},
    {uid: 's:2', ts: 2, type: 'think', text: '二', round_id: 'r2'},
  ],
};
const all = stepsSectionHTML(d, {type: 'all', round: '', open: {}});
if (!all.includes('bi-rounds')) throw new Error('缺轮次总览');
if (!all.includes('data-act="board-round-filter"')) throw new Error('缺轮次筛选钮');
if (!all.includes('全部轮次')) throw new Error('缺「全部轮次」');
if (!all.includes('第 1 轮') || !all.includes('第 2 轮')) throw new Error('轮次编号不对');
const r2 = stepsSectionHTML(d, {type: 'all', round: 'r2', open: {}});
if (r2.includes('>一<') || !r2.includes('>二<')) throw new Error('轮次筛选不生效');
const one = stepsSectionHTML({rounds: d.rounds.slice(0, 1), steps: d.steps}, {type: 'all', round: '', open: {}});
if (one.includes('board-round-filter')) throw new Error('单轮不该出筛选钮');
''')


def test_steps_section_jump_and_truncation_note() -> None:
    _assert_js(r'''
const { stepsSectionHTML } = mod;
const steps = Array.from({length: 12}, (_, i) => ({uid: 'u' + i, ts: i, type: 'think', text: 't' + i}));
const h = stepsSectionHTML({steps}, {type: 'all', round: '', open: {}});
if (!h.includes('data-act="board-jump-end"')) throw new Error('超 8 步应出「跳到最新」');
const small = stepsSectionHTML({steps: steps.slice(0, 4)}, {type: 'all', round: '', open: {}});
if (small.includes('board-jump-end')) throw new Error('少步不该出跳转');
const t = stepsSectionHTML({steps, truncated: true, total_steps: 900}, {type: 'all', round: '', open: {}});
if (!t.includes('共 900 步，中间已省略部分步骤')) throw new Error('截断提示不对');
''')


def test_steps_section_review_fixes() -> None:
    """复审四修：uid 展开态、redacted 文案、工具行结构、status 行内文 + accepted 轮次圆点。"""
    _assert_js(r'''
const { stepsSectionHTML } = mod;
// 1) 展开态按 uid 记，轮询后下标变了照样展开
const d = { steps: [
  {uid: 'u-a', ts: 1, type: 'tool', tool: 'run_command', input: '{"cmd":"ls"}', output: 'ok'},
  {uid: 'u-b', ts: 2, type: 'think', reasoning: '先想一下'},
] };
const opened = stepsSectionHTML(d, {type: 'all', round: '', open: {'u-a': true, 'r:u-b': true}});
if (!opened.includes('data-i="u-a" open')) throw new Error('uid 展开态没生效');
if (!opened.includes('data-i="r:u-b" open')) throw new Error('思考展开态没生效');
const closed = stepsSectionHTML(d, {type: 'all', round: '', open: {}});
if (closed.includes(' open>') || /data-i="u-a" open/.test(closed)) throw new Error('不该展开');
// 旧下标键也要认（老数据没有 uid）
const legacy = stepsSectionHTML({steps: [{ts: 1, type: 'tool', tool: 'ls', input: 'x'}]}, {type: 'all', round: '', open: {0: true}});
if (!legacy.includes(' open')) throw new Error('旧下标键兼容丢了');
// 2) redacted 文案：模型加密，不是隐私设置
const rd = stepsSectionHTML({steps: [{ts: 1, type: 'think', reasoning_redacted: true}]}, {type: 'all', round: '', open: {}});
if (!rd.includes('思考过程已由模型加密，无法查看')) throw new Error('redacted 文案不对');
if (rd.includes('隐私')) throw new Error('别再说隐私设置');
if (rd.includes('st-why"') && rd.includes('details')) throw new Error('redacted 不该出 details');
// 3) 工具行：st-name + 有哪个字段出哪块，空的不出空 <pre>
const tw = stepsSectionHTML(d, {type: 'all', round: '', open: {}});
if (!tw.includes('<span class="st-badge tool">工具</span><b class="st-name">run_command</b>')) throw new Error('工具名该在 st-name');
const onlyErr = stepsSectionHTML({steps: [{ts: 1, type: 'tool', tool: 'read_file', ok: false, error: '读不了'}]}, {type: 'all', round: '', open: {}});
if (!onlyErr.includes('>错误</div><pre class="bad-t">读不了</pre>')) throw new Error('错误块不见了');
if (onlyErr.includes('>输入</div>') || onlyErr.includes('>输出</div>') || onlyErr.includes('<pre></pre>')) throw new Error('空字段出了空块');
const bare = stepsSectionHTML({steps: [{ts: 1, type: 'tool', tool: 'vm_run'}]}, {type: 'all', round: '', open: {}});
if (bare.includes('st-io')) throw new Error('三无工具不该出 details');
// reasoning 要 st-text st-why
if (!opened.includes('<pre class="st-text st-why">')) throw new Error('reasoning 缺 st-text st-why');
// 4) 状态行内文 + accepted 算 done
const st = stepsSectionHTML({steps: [{ts: 1, type: 'status', text: '排队'}]}, {type: 'all', round: '', open: {}});
if (!st.includes('<span class="st-badge">状态</span><span class="st-sub">排队</span>')) throw new Error('状态文不在行内');
if (st.includes('st-text')) throw new Error('状态文不该再出一遍');
const rd2 = stepsSectionHTML({rounds: [{id: 'r1', no: 1, kind_title: '执行', status: 'accepted', status_text: '已通过', started: 1, finished: 2}], steps: []}, {type: 'all', round: '', open: {}});
if (!rd2.includes('dot done')) throw new Error('accepted 轮次该是 done 点');
''')
