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
    assert "agent-memory-save" not in text, "本群提醒搬进群页「这个群」的本群规矩"
    assert "agent-profile-save" in text
    assert "/api/agents" in text and "/agents/" in text
    assert "state.agentLoadSeq" in text, "切群时必须防止旧请求覆盖当前群"
    assert 'a.kind === "task"' in text, "通用任务也应有只读交接记录"
    router = (JS / "router.js").read_text(encoding="utf-8")
    assert router.count('state.setSub === "agents"') >= 2


def test_specialist_fish_avatar_contract():
    """专岗小鱼：每个岗位用程序生成的小鱼当头像，「换一条」只改这个岗位的 fish_seed。"""
    assert (JS / "fish.js").is_file(), "缺小鱼生成模块 fish.js"
    text = (JS / "settings/agents.js").read_text(encoding="utf-8")
    assert "agentFish" in text and '"../api.js"' in text, "设置页和各页面共用一个取鱼函数"
    assert 'data-act="agent-fish-reroll"' in text
    assert 'case "agent-fish-reroll"' in text
    assert "{ fish_seed: p.fish_seed }" in text, "换一条只提交 fish_seed，不带别的岗位字段"
    api = (JS / "api.js").read_text(encoding="utf-8")
    assert 'import { fishSvg } from "./fish.js";' in api
    assert "agent:${kind}:" in api, "种子为空时按岗位名固定一条鱼"
    assert "agent_fish" in api, "群友打开的页面从群视图拿各岗位的鱼种子"


def test_specialist_fish_on_pages():
    """各岗位的小鱼放在对应页面大标题前：资讯鱼在最新一批的标题前（没资讯时在「资讯」标题前），构想 / 目标 / 任务同理。"""
    news = (JS / "pages/news.js").read_text(encoding="utf-8")
    assert "tasteInner" not in news and "feeds-pref" not in news, "口味小结、这个群想看已并进本群做法 / 本群规矩"
    view = news[news.index("export function viewNews"):]
    assert 'b ? "" : agentFish("news"' in view, "只有最新一批的标题前放资讯鱼"
    assert '<h1 class="h-page">资讯</h1>' not in view, "没资讯时「资讯」标题也带鱼"
    for page, kind, title in (("ideas", "idea", "构想"), ("goals", "goal", "目标"), ("tasks", "task", "任务")):
        text = (JS / f"pages/{page}.js").read_text(encoding="utf-8")
        assert f'agentFish("{kind}"' in text, f"{title}页标题旁放{title}鱼"
        assert f'<h1 class="h-page">{title}</h1>' not in text, f"{title}页大标题要带鱼"


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


def test_every_module_parses_as_es_module(tmp_path):
    """每个前端文件按 ES 模块语法过一遍 node --check。

    2026-10 教训：同一个文件里留了两个同名 function——普通脚本模式允许，
    ES 模块里是语法错，浏览器整页白屏；`node --check x.js` 按脚本模式查不出来，
    所以这里复制成 .mjs 再查。"""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("本机没有 node，跑不了模块语法检查")
    bad = []
    for p in _modules():
        copy = tmp_path / (p.relative_to(JS).as_posix().replace("/", "__") + ".mjs")
        copy.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
        res = subprocess.run([node, "--check", str(copy)], capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            bad.append(f"{p.relative_to(JS)}: {res.stderr.strip().splitlines()[-1] if res.stderr.strip() else '?'}")
    assert not bad, "这些前端文件按 ES 模块解析不过：\n" + "\n".join(bad)


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


def test_guide_only_batch_points_to_articles():
    """一批只收了文章（没有资讯）时，资讯栏不说「没有值得看的」，而是指向「文章」栏。"""
    news = (JS / "pages/news.js").read_text(encoding="utf-8")
    view = news[news.index("export function viewNews"):]
    assert "在「文章」里" in view
    assert 'data-act="news-tab" data-t="guides"' in view, "给一个直接去文章栏的按钮"


def test_settings_agents_nav_uses_fish_emoji():
    """设置里「专岗」的图标是 🐟（Fluent 3D，和其他图标同一套），不是机器人。"""
    idx = (JS / "settings/index.js").read_text(encoding="utf-8")
    assert '["agents", "专岗", "fish",' in idx
    util = (JS / "util.js").read_text(encoding="utf-8")
    icons = util[util.index("const ICONS"):].split("\n", 1)[0]
    assert " fish " in icons or " fish\"" in icons
    assert (STATIC / "assets/icons/fish.png").is_file()


def test_news_page_has_no_two_phase_switch():
    """新找法对所有群默认生效（2026-09-30 用户决定），网页上不再有开关。"""
    for rel in ("pages/news.js", "actions_news.js"):
        src = (JS / rel).read_text(encoding="utf-8")
        assert "two-phase" not in src and "twoPhase" not in src, rel
    assert ".two-phase" not in (STATIC / "style.css").read_text(encoding="utf-8")


def test_fish_icon_not_in_group_avatar_pool():
    """🐟 是专岗专用图标：不进群头像候选，否则加一张图会让所有群的图标整体错位。"""
    from CharTyr_MaiWork.maiwork.console import views

    views._icons_cache = None
    icons = views.group_icons()
    assert "fish" not in icons
    assert "robot" in icons and "teacup" in icons


_FISH_CUSTOM_CHECK = r"""
import { fishSvg, fishTraits, FISH_SPECIES, FISH_EYES, FISH_COLORS, FISH_COLOR_NAMES, customSeed, parseCustom } from %s;
const body = (svg) => svg.match(/class="fish-body" d="([^"]+)"/)[1];
const out = { bad: [], len: 0 };
// 1) 挑什么就画什么
for (const sp of Object.keys(FISH_SPECIES)) for (const e of Object.keys(FISH_EYES)) {
  const c = FISH_COLORS.length - 1;
  const s = customSeed({ species: sp, color: c, eyes: e, base: "abcdefghij" });
  out.len = Math.max(out.len, s.length);
  if (!/^[A-Za-z0-9_-]{1,32}$/.test(s)) out.bad.push("seed:" + s);
  const t = fishTraits("agent:news:" + s);
  if (t.species !== sp || t.eyes !== e || t.color !== FISH_COLORS[c]) out.bad.push("traits:" + s);
  const p = parseCustom("agent:news:" + s);
  if (!p || p.species !== sp || p.eyes !== e || p.color !== c || p.base !== "abcdefghij") out.bad.push("parse:" + s);
  if (/NaN|undefined|Infinity/.test(fishSvg("agent:news:" + s))) out.bad.push("svg:" + s);
}
// 2) 只换颜色：身形不动
const a = fishSvg("agent:idea:" + customSeed({ species: "shark", color: 1, eyes: "dots", base: "k9" }), { still: true });
const b = fishSvg("agent:idea:" + customSeed({ species: "shark", color: 5, eyes: "dots", base: "k9" }), { still: true });
out.colorKeepsShape = body(a) === body(b) && a !== b;
// 3) 打开定制时按原来那条鱼填好：原样保存画出来一模一样
out.identity = [];
for (const seed of ["", "ab12cd34", "x_1"]) {
  const orig = "agent:goal:" + seed;
  const t = fishTraits(orig);
  const s = customSeed({ species: t.species, color: FISH_COLORS.indexOf(t.color), eyes: t.eyes, base: seed });
  out.identity.push(fishSvg(orig, { still: true }) === fishSvg("agent:goal:" + s, { still: true }));
}
// 4) 乱写的 / 不认识的不当定制
out.junk = [parseCustom("agent:news:c-whale-1-dots-a"), parseCustom("agent:news:c-shark-99-dots-a"), parseCustom("agent:news:c-shark-1-lol-a"), parseCustom("agent:news:ab12")].every((x) => x === null);
out.names = FISH_COLOR_NAMES.length === FISH_COLORS.length && new Set(FISH_COLOR_NAMES).size === FISH_COLORS.length;
console.log(JSON.stringify(out));
"""


def test_fish_custom_seed_picks_species_color_eyes(tmp_path):
    """专岗小鱼可以定制（2026-09-30 用户要）：鱼种 / 颜色 / 眼神由管理员挑，写进 fish_seed，后端不用改。"""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("本机没有 node，跑不了小鱼定制检查")
    script = tmp_path / "custom.mjs"
    script.write_text(_FISH_CUSTOM_CHECK % json.dumps((JS / "fish.js").as_uri()), encoding="utf-8")
    res = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout.strip().splitlines()[-1])
    assert not data["bad"], data["bad"][:10]
    assert data["len"] <= 32, "定制种子要放得进 fish_seed（≤32 字符）"
    assert data["colorKeepsShape"], "只换颜色时鱼的样子不能跟着变"
    assert all(data["identity"]), "打开定制原样保存，鱼要和原来一模一样"
    assert data["junk"], "不认识的鱼种 / 颜色 / 眼神不能当成定制"
    assert data["names"], "每个颜色要有中文名（读屏和提示用）"


def test_fish_custom_picker_contract():
    """设置 → 专岗：每条鱼有「定制」，挑鱼种 / 颜色 / 眼神，有预览，只提交 fish_seed。"""
    text = (JS / "settings/agents.js").read_text(encoding="utf-8")
    for act in ("agent-fish-custom", "agent-fish-pick", "agent-fish-shape", "agent-fish-save", "agent-fish-cancel"):
        assert f'data-act="{act}"' in text and f'case "{act}"' in text, act
    assert "customSeed" in text and "aria-pressed" in text
    assert "{ fish_seed: seed }" in text, "定制保存只提交 fish_seed"
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".fish-swatch" in css and ".fish-opt" in css


def test_specialist_escalate_selector_contract():
    """任务双岗协作（docs/20 §5.3）：干活岗位能选「做不动时换用」；主模型岗没有这一项。"""
    text = (JS / "settings/agents.js").read_text(encoding="utf-8")
    assert 'id="agent-escalate"' in text
    assert "做不动时换用" in text
    assert 'modelOptions(p.escalate, "用主模型的")' in text, "没选 = 用主模型的（用户 2026-10-05 定）"
    assert 'if ($("agent-escalate")) body.escalate = $("agent-escalate").value;' in text
    # 主模型岗不显示这一项：只排计划和验收，不亲自干活
    assert '${main ? "" : `<label for="agent-escalate">' in text


def test_task_detail_shows_lane_notes():
    """任务双岗协作（docs/20 §八）：管理员版任务详情显示返工 / 换模型 / 重开的记录。"""
    text = (JS / "detail.js").read_text(encoding="utf-8")
    assert "t.lane_notes" in text
    assert "返工和换手" in text
