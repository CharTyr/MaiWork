"""设置 → 资讯来源 → RSS：自动订阅的源标「自动」+ 理由 + 试用期 + 命中率；每群可展开看自动订阅记录 / 来源地图
（docs/10 §九 第二步，2026-10-05 用户拍板：网页标「自动」并写原因、一键删掉、删了不再推荐）。"""
import json
import shutil
import subprocess
from pathlib import Path

SOURCES = Path(__file__).resolve().parents[1] / "maiwork/console/static/js/settings/sources.js"
ACTIONS = Path(__file__).resolve().parents[1] / "maiwork/console/static/js/actions.js"
NOW = 1_790_000_000
CHECK = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const CFG = %s;
const source = fs.readFileSync(CFG.path, 'utf8');
const esc = (s) => String(s ?? '').replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const context = vm.createContext({ console });
const state = { trusted: { g1: { trusted: [], removed: [] } }, rssAuto: CFG.auto };
const stubs = {
  '../util.js': { dayWord: (ts) => (ts > CFG.now ? '后天' : '今天'), esc, ico: () => '', now: () => CFG.now },
  '../api.js': { api: () => new Promise(() => {}), mq: (s) => esc(s) },
  '../state.js': { state },
  './models.js': { feedsSettings: () => '<p>BLOCKED</p>' },
};
const modules = new Map();
async function link(spec) {
  if (!modules.has(spec)) {
    const exports = stubs[spec];
    if (!exports) throw new Error('unexpected import: ' + spec);
    const mod = new vm.SyntheticModule(Object.keys(exports), function () {
      for (const [k, v] of Object.entries(exports)) this.setExport(k, v);
    }, { context });
    modules.set(spec, mod); await mod.link(link);
  }
  return modules.get(spec);
}
const mod = new vm.SourceTextModule(source, { context });
await mod.link(link); await mod.evaluate();
console.log(JSON.stringify({ html: mod.namespace.sourcesPage(CFG.s) }));
"""


def render(tmp_path, feeds, auto):
    node = shutil.which("node")
    assert node, "没有 Node，无法验证资讯来源页"
    assert SOURCES.is_file()
    s = {"groups": [{"id": "g1", "name": "单机群"}], "feeds": {"rss": {"g1": feeds}}}
    script = tmp_path / "sources.mjs"
    script.write_text(CHECK % json.dumps({"path": str(SOURCES), "s": s, "auto": auto, "now": NOW}), encoding="utf-8")
    r = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])["html"]


AUTO_FEED = {"id": "rA", "url": "https://www.gcores.com/rss", "title": "机核", "enabled": True,
             "added_ts": NOW - 86400, "last_ok_ts": NOW - 3600, "last_error": "", "auto": True,
             "origin": "trusted", "reason": "近 30 天有 5 条 4 分以上<b>", "label": "gcores.com",
             "trial_until": NOW + 2 * 86400}
MANUAL_FEED = {"id": "rM", "url": "https://ok.example.com/feed", "title": "好博客", "enabled": True,
               "added_ts": NOW - 86400, "last_ok_ts": 0, "last_error": "", "auto": False, "origin": "",
               "reason": "", "label": "", "trial_until": 0}
AUTO_VIEW = {"g1": {
    "auto_log": [
        {"ts": NOW - 60, "action": "subscribed", "label": "gcores.com", "url": "u", "origin": "trusted", "reason": "门槛够了"},
        {"ts": NOW - 120, "action": "unsubscribed", "label": "slow.example.com", "url": "u2", "origin": "map", "reason": "试用期结束，一条都没被选进资讯"},
        {"ts": NOW - 180, "action": "rejected", "label": "bad.example.com", "url": "u3", "origin": "push", "reason": "管理员删掉了"},
        {"ts": NOW - 240, "action": "skipped", "label": "busy.example.com", "url": "", "origin": "map", "reason": "体检没过：取 RSS 失败（HTTP 429）"},
        {"ts": NOW - 300, "action": "rejected", "label": "old.example.com", "url": "", "origin": "push", "reason": "体检没过：取 RSS 失败（HTTP 429）"},
    ],
    "source_map": {"ts": NOW - 3600, "sources": [
        {"name": "机核", "url": "https://www.gcores.com/", "why": "中文游戏媒体一手", "label": "gcores.com",
         "feed_url": "https://www.gcores.com/rss", "status": "verified", "reason": "", "origin": "model"},
        {"name": "某聚合站", "url": "https://agg.example.com/", "why": "w", "label": "agg.example.com",
         "feed_url": "", "status": "rejected", "reason": "找不到订阅地址", "origin": "model"},
        {"name": "found.example.com", "url": "https://found.example.com/", "why": "找来源搜索「独立游戏 blog」搜到",
         "label": "found.example.com", "feed_url": "https://found.example.com/feed", "status": "verified",
         "reason": "", "origin": "search"},
    ]},
    "auto": {"quota": {"trusted": {"used": 1, "max": 3}, "map": {"used": 0, "max": 3}, "total": {"used": 2, "max": 20}},
             "push": [], "rule": "近 30 天 ≥4 条 4 分以上", "origins": {"trusted": "门槛", "map": "来源地图", "push": "固定清单"},
             "hits": {"rA": {"cand": 6, "kept": 2, "days": 30}}},
}}


def test_auto_feed_tagged_with_reason_trial_and_hits(tmp_path):
    html = render(tmp_path, [AUTO_FEED, MANUAL_FEED], AUTO_VIEW)
    auto_row = html[html.index("机核"):html.index("好博客")]
    assert "自动" in auto_row
    assert "近 30 天有 5 条 4 分以上&lt;b&gt;" in auto_row and "<b>" not in auto_row
    assert "试用到后天" in auto_row
    assert "近 30 天给了 6 条，进资讯 2 条" in auto_row
    # 删掉自动源要说清楚「以后不再推荐」
    assert 'data-auto="1"' in auto_row
    manual_row = html[html.index("好博客"):]
    manual_row = manual_row[:manual_row.index("</form>")]
    assert "rss-auto-tag" not in manual_row and 'data-auto="1"' not in manual_row


def test_auto_log_and_source_map_in_collapsed_details(tmp_path):
    html = render(tmp_path, [AUTO_FEED], AUTO_VIEW)
    assert "<details" in html and "rss-auto" in html
    assert "门槛来的 1/3" in html and "来源地图 0/3" in html
    assert "订上 gcores.com" in html
    assert "退订 slow.example.com" in html and "试用期结束，一条都没被选进资讯" in html
    assert "不再推荐 bad.example.com" in html
    # 体检没过 = 这次没订上；0.9.0 上线首轮的老记录动作写的是 rejected，也按原因认出来
    assert "没订上 busy.example.com" in html
    assert "没订上 old.example.com" in html and "不再推荐 old.example.com" not in html
    assert "中文游戏媒体一手" in html
    assert "agg.example.com" in html and "找不到订阅地址" in html
    assert "近 30 天 ≥4 条 4 分以上" in html
    # 每周「找来源」搜索搜到的网站在来源地图里标出来（模型列的不标）
    assert html.count("ra-found") == 1
    assert '搜到的</span>' in html and "找来源搜索「独立游戏 blog」搜到" in html


def test_no_auto_details_before_data_loads(tmp_path):
    html = render(tmp_path, [MANUAL_FEED], {})
    assert "好博客" in html
    assert "rss-auto-tag" not in html


def test_delete_auto_feed_confirm_says_not_recommended_again_and_refreshes_auto_view():
    js = ACTIONS.read_text(encoding="utf-8")
    block = js[js.index('case "rss-del"'):js.index('case "news-run"')]
    assert "不再自动推荐" in block
    assert "rssAuto" in block
