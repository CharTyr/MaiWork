"""网页前端（console/static/js）的结构检查：不用编译，浏览器直接加载原生 ES 模块。

这里不跑浏览器，只做静态检查，挡住拆文件时最容易出的错：
- import 的文件不存在 / 引了对方没 export 的名字（浏览器里整页白屏）；
- 某个模块没人引用（它注册的事件就不会生效）；
- 单个文件又长回几千行。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "maiwork" / "console" / "static"
JS = STATIC / "js"
MAX_LINES = 900

_IMPORT = re.compile(r'^import\s+(?:\{([^}]*)\}\s+from\s+)?"([^"]+)";', re.M)
_EXPORT = re.compile(r"^export\s+(?:async\s+)?(?:function\*?|const|let|class)\s+([A-Za-z_$][\w$]*)", re.M)


def _modules() -> list[Path]:
    if not JS.is_dir():
        raise AssertionError(f"找不到前端模块目录 {JS}")
    return sorted(p for p in JS.rglob("*.js") if not p.name.startswith("._"))


def test_index_loads_only_the_entry_module() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '<script type="module" src="/static/js/main.js"></script>' in html
    assert "app.js" not in html


def test_imports_resolve_and_names_are_exported() -> None:
    mods = _modules()
    exports = {p: set(_EXPORT.findall(p.read_text(encoding="utf-8"))) for p in mods}
    reached: set[Path] = set()
    for p in mods:
        text = p.read_text(encoding="utf-8")
        for names, spec in _IMPORT.findall(text):
            assert spec.startswith("./") or spec.startswith("../"), f"{p.name}: 只用相对路径 import（{spec}）"
            target = (p.parent / spec).resolve()
            assert target.exists(), f"{p.relative_to(JS)} import 的 {spec} 不存在"
            reached.add(target)
            for n in [x.strip() for x in names.split(",") if x.strip()]:
                assert n in exports[target], f"{p.relative_to(JS)} 引了 {spec} 里没 export 的 {n}"
    orphans = [p.relative_to(JS).as_posix() for p in mods if p.name != "main.js" and p.resolve() not in reached]
    assert not orphans, f"这些模块没人 import，不会被加载：{orphans}"


def test_every_module_is_short() -> None:
    long = {}
    for p in _modules():
        n = p.read_text(encoding="utf-8").count("\n")
        if n > MAX_LINES:
            long[p.relative_to(JS).as_posix()] = n
    assert not long, f"这些文件太长了，拆一拆（上限 {MAX_LINES} 行）：{long}"


def test_no_nul_separator_in_frontend() -> None:
    """HTML 属性值里的 NUL（\\u0000）会被浏览器换成 U+FFFD：拿它当 option 值的分隔符，
    读回来就拆不开（线上踩过：联网搜索选了工具却保存报「要给出 mcp 和 tool」，
    抓正文下拉也只剩「不用」）。前端一律不用 NUL 当分隔符。"""
    bad = [p.relative_to(JS).as_posix() for p in _modules() if "\\u0000" in p.read_text(encoding="utf-8")]
    assert not bad, f"这些文件用了 \\u0000 当分隔符：{bad}"


def test_skill_activation_controls():
    ext = (JS / "settings/ext.js").read_text(encoding="utf-8")
    actions = (JS / "actions_news.js").read_text(encoding="utf-8")
    assert 'data-act="skill-toggle"' in ext
    assert 'k.disabled_reason' in ext
    assert 'k.manual_enabled' in ext
    assert 'case "skill-toggle"' in actions
    assert '/toggle`' in actions and '/api/extensions/skills/' in actions


def test_specialist_admin_interface_contract():
    page = JS / "settings/agents.js"
    assert page.is_file(), "专岗管理页未实现"
    text = page.read_text(encoding="utf-8")
    assert "export function agentsPage" in text
    assert "export async function loadAgents" in text
    assert "export async function actAgents" in text
    assert "agent-memory-save" in text
    assert "agent-profile-save" in text
    assert "/api/agents" in text and "/agents/" in text
    assert "state.agentLoadSeq" in text, "切群时必须防止旧请求覆盖当前群"
    assert "gid !== state.agentGroup" in text, "旧群按钮不能保存当前群的提醒"
    assert 'a.kind === "task"' in text, "通用任务也应有只读交接记录"
    router = (JS / "router.js").read_text(encoding="utf-8")
    assert router.count('state.setSub === "agents"') >= 2


def test_specialist_fish_avatar_contract():
    """专岗小鱼：每个岗位用程序生成的小鱼当头像，「换一条」只改这个岗位的 fish_seed。"""
    assert (JS / "fish.js").is_file(), "缺小鱼生成模块 fish.js"
    text = (JS / "settings/agents.js").read_text(encoding="utf-8")
    assert 'import { fishSvg } from "../fish.js";' in text
    assert 'data-act="agent-fish-reroll"' in text
    assert 'case "agent-fish-reroll"' in text
    assert "{ fish_seed: p.fish_seed }" in text, "换一条只提交 fish_seed，不带别的岗位字段"
    assert "agent:${kind}:" in text, "种子为空时按岗位名固定一条鱼"


_FISH_CHECK = r"""
import { fishSvg, fishTraits, FISH_SPECIES, FISH_EYES, swimPose, swimGeom } from %s;
// 游动：像真鱼——鱼头几乎不动、尾巴摆得最大；时快时慢；同一时刻姿态固定；still 关掉
const swimOf = (seed, o = {}) => { const m = fishSvg(seed, o).match(/data-fish-id="(\d+)"/); return m && swimGeom(m[1]); };
const bend = { ok: true, bad: [] };
for (const sp of Object.keys(FISH_SPECIES)) {
  const g = swimOf("swim-" + sp, { species: sp });
  if (!g) { bend.ok = false; bend.bad.push(sp + ":无几何"); continue; }
  const head = g.ts.indexOf(Math.min(...g.ts)), tail = g.ts.indexOf(Math.max(...g.ts));
  let hMax = 0, tMax = 0, fast = 0, slow = 1e9;
  for (let t = 0; t < 12; t += 0.05) {
    const p = swimPose(g, t), nums = p.d.match(/-?[\d.]+/g).map(Number);
    if (nums.some((v) => !Number.isFinite(v)) || /NaN/.test(p.body + p.head)) { bend.ok = false; bend.bad.push(sp + ":坏数字"); break; }
    const pt = (i) => i === 0 ? [nums[0], nums[1]] : [nums[2 + (i - 1) * 6 + 4], nums[2 + (i - 1) * 6 + 5]];
    const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);
    hMax = Math.max(hMax, dist(pt(head), g.pts[head])); tMax = Math.max(tMax, dist(pt(tail), g.pts[tail]));
  }
  if (!(tMax > 4 * hMax && tMax > 1.5)) { bend.ok = false; bend.bad.push(`${sp}:头${hMax.toFixed(2)} 尾${tMax.toFixed(2)}`); }
}
const g0 = swimOf("agent:news:");
const out = { swim: !!g0 && !fishSvg("a", { still: true }).includes("fish-swim") && !/<rect|<ellipse class="fish-shadow"/.test(fishSvg("a"))
    && swimPose(g0, 3.3).d === swimPose(g0, 3.3).d && swimPose(g0, 3.3).d !== swimPose(g0, 3.5).d,
  bend,
  same: fishSvg("agent:news:") === fishSvg("agent:news:"), differ: 0, bad: [], species: new Set(), eyes: new Set() };
for (let i = 0; i < 400; i++) {
  const seed = "s" + i;
  const svg = fishSvg(seed);
  if (/NaN|undefined|Infinity/.test(svg) || !svg.includes("<path")) out.bad.push(seed);
  if (svg !== fishSvg("t" + i)) out.differ++;
  const t = fishTraits(seed);
  out.species.add(t.species); out.eyes.add(t.eyes);
}
for (const sp of Object.keys(FISH_SPECIES)) for (const e of Object.keys(FISH_EYES)) {
  const svg = fishSvg("x", { species: sp, eyes: e });
  if (/NaN|undefined|Infinity/.test(svg)) out.bad.push(sp + "/" + e);
}
console.log(JSON.stringify({ ...out, species: out.species.size, eyes: out.eyes.size,
  nSpecies: Object.keys(FISH_SPECIES).length, nEyes: Object.keys(FISH_EYES).length }));
"""


def test_fish_generator_is_deterministic_and_well_formed(tmp_path):
    """同一个种子永远画出同一条鱼；换种子会变；每种鱼 × 每种眼神都画得出来、没有坏数字。"""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("本机没有 node，跑不了小鱼生成检查")
    script = tmp_path / "check.mjs"
    script.write_text(_FISH_CHECK % json.dumps((JS / "fish.js").as_uri()), encoding="utf-8")
    res = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout.strip().splitlines()[-1])
    assert data["same"], "同一个种子两次画出来不一样"
    assert data["swim"], "小鱼要默认会游（姿态随时间变、同一时刻固定、still 可关），而且透明底、不画底框、不带影子（用户 2026-09-30 去掉了影子）"
    assert data["bend"]["ok"], f"游动要像真鱼：鱼头几乎不动、尾巴摆得最大：{data['bend']['bad']}"
    assert data["differ"] >= 390, f"换种子几乎没变化：{data['differ']}/400"
    assert not data["bad"], f"这些种子画出了坏图：{data['bad'][:10]}"
    assert data["species"] == data["nSpecies"], "随机 400 条没覆盖到所有鱼种"
    assert data["eyes"] == data["nEyes"], "随机 400 条没覆盖到所有眼神"
