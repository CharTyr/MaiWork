"""设置 → 资讯来源：每个群一行「近 14 天入选里来自优质来源的占几条」
（docs/10 §九 第二步剩下三项之 3，2026-10-05 用户选「设置页一行」）。

口径：近 14 天、上了网页（rejected=0）、群向（个人向不算）；三类互斥——
订阅 RSS 带来的（src_provider="rss:…"）先算 RSS；其余按来源名在不在本群优质来源名单（网页上显示的那份，
已去掉移出 / 屏蔽的）算「优质来源」，剩下的算「其他」。「一手」代码判断不准，按已有名单算。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import source_stats
from CharTyr_MaiWork.maiwork.store import Store

NOW = 1_790_000_000.0
GID = "111"
SOURCES = Path(__file__).resolve().parents[1] / "maiwork/console/static/js/settings/sources.js"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _item(store, url, *, avg=4.2, rejected=0, days_ago=1.0, provider="", target="", gid=GID):
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO news_items (batch_id, group_id, title, url_key, sources, scores, rejected, created,"
            " src_provider, target_user_id) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (gid, url, url.split("://", 1)[-1], json.dumps([{"url": url, "site": url.split("/")[2]}]),
             json.dumps({"avg": avg}), rejected, NOW - days_ago * 86400, provider, target),
        )


def test_share_counts_trusted_rss_and_other(store):
    for i in range(3):
        _item(store, f"https://good.example/{i}")              # 3 条高分 → good.example 上名单
    _item(store, "https://www.good.example/x", avg=3.0)        # 名单里的站，低分也算「来自优质来源」
    _item(store, "https://feed.example/1", provider="rss:r1")  # RSS 带来的
    _item(store, "https://good.example/r", provider="rss:r2")  # 名单里的站但经 RSS 来 → 算 RSS
    _item(store, "https://other.example/1", avg=3.5)
    _item(store, "https://other.example/2", avg=4.5)           # 只有 1 条高分，不上名单
    # 不算的：没上网页 / 太旧 / 个人向 / 别的群
    _item(store, "https://good.example/no", rejected=1)
    _item(store, "https://good.example/old", days_ago=20)
    _item(store, "https://good.example/me", target="u1")
    _item(store, "https://good.example/g2", gid="222")
    sh = source_stats.source_share(store, GID, NOW)
    assert sh == {"days": 14, "total": 8, "trusted": 4, "rss": 2, "other": 2}


def test_share_respects_removed_and_blocked(store):
    for i in range(3):
        _item(store, f"https://good.example/{i}")
    source_stats.set_removed(store, GID, "good.example", True)
    assert source_stats.source_share(store, GID, NOW)["trusted"] == 0
    source_stats.set_removed(store, GID, "good.example", False)
    assert source_stats.source_share(store, GID, NOW, blocked=["good.example"])["trusted"] == 0


def test_share_in_view_and_never_raises(store):
    _item(store, "https://x.example/1")
    v = source_stats.view(store, GID, NOW)
    assert v["share"]["total"] == 1 and v["share"]["other"] == 1

    class Broken:
        def read(self):
            raise RuntimeError("坏了")

        def kv_get(self, *a, **k):
            raise RuntimeError("坏了")

    assert source_stats.source_share(Broken(), GID, NOW) == {"days": 14, "total": 0, "trusted": 0, "rss": 0, "other": 0}


CHECK = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const CFG = %s;
const source = fs.readFileSync(CFG.path, 'utf8');
const esc = (s) => String(s ?? '').replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const context = vm.createContext({ console });
const state = { trusted: { g1: CFG.trusted }, rssAuto: { g1: {} } };
const stubs = {
  '../util.js': { dayWord: () => '今天', esc, ico: () => '', now: () => 0 },
  '../api.js': { api: () => new Promise(() => {}), mq: (s) => esc(s) },
  '../state.js': { state },
  './models.js': { feedsSettings: () => '' },
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
console.log(JSON.stringify({ html: mod.namespace.sourcesPage({ groups: [{ id: 'g1', name: '单机群' }], feeds: { rss: {} } }) }));
"""


def _render(tmp_path, trusted):
    node = shutil.which("node")
    assert node, "没有 Node，无法验证资讯来源页"
    script = tmp_path / "share.mjs"
    script.write_text(CHECK % json.dumps({"path": str(SOURCES), "trusted": trusted}), encoding="utf-8")
    r = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])["html"]


def test_frontend_share_line(tmp_path):
    html = _render(tmp_path, {"trusted": [{"domain": "good.example", "high": 3, "up": 0}], "removed": [],
                              "share": {"days": 14, "total": 8, "trusted": 4, "rss": 2, "other": 2}})
    assert "近 14 天入选 8 条" in html
    assert "来自优质来源 4 条（50%）" in html
    assert "订阅 RSS 带来 2 条" in html and "其他 2 条" in html
    assert 'class="fine trusted-share"' in html


def test_frontend_share_line_hidden_when_empty(tmp_path):
    html = _render(tmp_path, {"trusted": [], "removed": [], "share": {"days": 14, "total": 0, "trusted": 0, "rss": 0, "other": 0}})
    assert "trusted-share" not in html
    html = _render(tmp_path, {"trusted": [], "removed": []})  # 老接口没有 share：不报错、不显示
    assert "trusted-share" not in html
