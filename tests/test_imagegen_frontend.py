"""生图模型的网页侧（设置 → 模型）：用途「生图」、生图字段、只给生图模型的选择按钮。

真跑一遍 JS（node + SourceTextModule）渲染 modelsPage()，再补几条静态检查。
找不到 Node 直接失败（不许 skip 造成假绿）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "maiwork/console/static/js"
MODELS = STATIC / "settings/models.js"
AGENTS = STATIC / "settings/agents.js"

CHECK = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const CFG = %s;
const source = fs.readFileSync(CFG.path, 'utf8');
const esc = (s) => String(s ?? '').replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const state = CFG.state;
const context = vm.createContext({ console, location: { origin: 'http://local.test' }, setTimeout,
  document: { querySelectorAll: () => [], querySelector: () => null, addEventListener: () => {} } });
const stubs = {
  '../state.js': { state, $: () => null },
  '../util.js': { tip: (h) => (h ? '<details class="tip">' + h + '</details>' : ''), tipLabel: (f, t, h) => '<label for="' + f + '">' + t + '</label>' + (h ? '<details class="tip">' + h + '</details>' : ''), esc, ico: () => '', toast: () => {}, when: () => '刚刚' },
  '../api.js': { api: async () => ({}) },
  '../sheet.js': { repaintSheet: () => {} }, '../pages/news.js': { loading: () => '' },
  '../router.js': { loadSettings: async () => {} },
  './jev.js': { jevSection: () => '', loadJev: async () => {} },
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
console.log(JSON.stringify({ html: mod.namespace.modelsPage() }));
"""


def _endpoints() -> list[dict]:
    return [{"id": "e1", "name": "端点一", "protocol": "openai",
             "base_url": "https://api.test/v1", "key_set": True, "checked_at": 0,
             "header_names": [], "retries": 5, "retry_delay_s": 10,
             "max_concurrency": 2, "max_rpm": 0}]


def _models() -> list[dict]:
    return [
        {"id": "m1", "endpoint": "e1", "model": "gpt-x", "name": "聊天",
         "efforts": ["low"], "vision": False, "context_window": 128000, "max_tokens": 32768,
         "use": "chat", "image_api": "openai", "image_default_size": "", "image_max_n": 1},
        {"id": "img1", "endpoint": "e1", "model": "gpt-image-1", "name": "生图一号",
         "efforts": [], "vision": False, "context_window": 128000, "max_tokens": 32768,
         "use": "image", "image_api": "openai", "image_default_size": "1024x1024", "image_max_n": 2},
        {"id": "img2", "endpoint": "e1", "model": "Kwai-Kolors/Kolors", "name": "硅基生图",
         "efforts": [], "vision": False, "context_window": 128000, "max_tokens": 32768,
         "use": "image", "image_api": "siliconflow", "image_default_size": "", "image_max_n": 1},
    ]


def _presets() -> list[dict]:
    return [
        {"id": "openai", "label": "OpenAI", "image_api": "openai",
         "base_url": "https://api.openai.com/v1", "model": "gpt-image-1",
         "models": ["gpt-image-1"], "key_url": "https://platform.openai.com/api-keys",
         "docs_url": "https://platform.openai.com/docs", "note": "官方"},
        {"id": "ark", "label": "火山方舟", "image_api": "openai",
         "base_url": "https://ark.cn-beijing.volces.com/api/v3", "model": "doubao-seedream-4-0-250828",
         "models": ["doubao-seedream-4-0-250828"], "key_url": "https://console.volcengine.com/ark",
         "docs_url": "https://www.volcengine.com/docs/82379", "note": "有试用额度"},
    ]


def render(tmp_path: Path, *, mdl_edit=None, models=None, image_model: str = "img1") -> str:
    node = shutil.which("node")
    assert node, "没有 Node，无法验证生图模型表单"
    assert MODELS.is_file(), "设置前端文件缺失"
    state = {
        "mdl": {"endpoints": _endpoints(), "models": models if models is not None else _models(),
                "image_model": image_model, "imagegen_presets": _presets()},
        "mdlEdit": mdl_edit,
        "mdlAvail": {},
        "mdlVerification": {},
        "settings": {"models": {"ready": True}},
    }
    script = tmp_path / "imagegen.mjs"
    script.write_text(CHECK % json.dumps({"path": str(MODELS), "state": state}), encoding="utf-8")
    r = subprocess.run([node, "--experimental-vm-modules", str(script)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])["html"]


def test_image_row_marked_and_no_chat_verify_button(tmp_path: Path) -> None:
    html = render(tmp_path)
    assert "生图一号" in html
    # 用途标出来
    assert "生图" in html
    # 生图模型不给「验证」（验证是聊天口径，会白花钱）
    assert 'data-act="mdl-model-verify" data-id="img1"' not in html
    assert 'data-act="mdl-model-verify" data-id="m1"' in html
    # 当前在用的生图模型标出来，另一个给「设为生图模型」
    assert "当前生图模型" in html
    assert 'data-act="mdl-image-model" data-id="img2"' in html
    assert 'data-act="mdl-image-model" data-id="img1"' not in html


def test_image_row_shows_shape_and_limits(tmp_path: Path) -> None:
    html = render(tmp_path)
    assert "单次最多 2 张" in html
    assert "默认尺寸 1024x1024" in html
    assert "硅基流动" in html  # image_api 的中文名


def test_chat_row_keeps_old_meta(tmp_path: Path) -> None:
    html = render(tmp_path)
    assert "思考强度 低" in html
    assert "上下文 128K" in html


def test_model_form_has_use_switch_and_image_fields(tmp_path: Path) -> None:
    draft = {"id": "img3", "endpoint": "e1", "model": "", "name": "", "efforts": [],
             "vision": False, "context_window": 128000, "max_tokens": 32768,
             "use": "image", "image_api": "siliconflow", "image_default_size": "1024x768",
             "image_max_n": 3}
    html = render(tmp_path, mdl_edit={"type": "model", "isNew": True, "draft": draft})
    # 用途：两段式选择，当前在「生图」
    assert 'data-act="mdl-use" data-use="image" aria-pressed="true"' in html
    assert 'data-act="mdl-use" data-use="chat" aria-pressed="false"' in html
    assert 'id="mo-image-api"' in html
    assert 'id="mo-image-size"' in html
    assert 'id="mo-image-n"' in html
    assert "1024x768" in html
    assert 'value="siliconflow" selected' in html
    # 生图不用思考强度 / 上下文：这些字段不出现
    assert 'id="mo-ctx"' not in html
    assert 'class="mo-effort"' not in html
    # 常用生图服务一键填入
    assert 'data-act="mdl-preset"' in html
    assert "火山方舟" in html


def test_chat_form_still_has_old_fields(tmp_path: Path) -> None:
    draft = {"id": "m9", "endpoint": "e1", "model": "gpt-x", "name": "", "efforts": ["high"],
             "vision": False, "context_window": 64000, "max_tokens": 8192,
             "use": "chat", "image_api": "openai", "image_default_size": "", "image_max_n": 1}
    html = render(tmp_path, mdl_edit={"type": "model", "isNew": True, "draft": draft})
    assert 'id="mo-ctx"' in html
    assert 'class="mo-effort"' in html
    assert 'id="mo-image-api"' not in html
    assert 'data-act="mdl-use" data-use="chat" aria-pressed="true"' in html


def _code_of(src: str, head: str) -> str:
    """取一个函数体，并去掉 // 注释行（静态检查要看真代码，不能被注释糊弄过去）。"""
    body = src.split(head)[1].split("\n}")[0]
    return "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("//"))


def test_read_model_reads_image_fields() -> None:
    body = _code_of(MODELS.read_text(encoding="utf-8"), "function readModel()")
    for key in ("use", "image_api", "image_default_size", "image_max_n"):
        assert key in body, f"readModel 没读 {key}"


def test_agents_model_dropdown_filters_image_entries() -> None:
    body = _code_of(AGENTS.read_text(encoding="utf-8"), "function modelOptions(")
    assert "filter(" in body, "专岗模型下拉没有过滤逻辑"
    assert '!== "image"' in body, "专岗模型下拉没滤掉生图条目"
