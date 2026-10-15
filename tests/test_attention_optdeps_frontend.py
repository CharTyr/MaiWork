"""真实 JS（2026-10-15 线上巡检后）：设置概况「待处理」+ 工具页「可选组件」。

- 待复核 @ 请求按群汇总，给「处理」；发送失败的资讯卡片列出来，给「重发」；
- 两样都没有 → 整块不显示（不画空标题）；
- 可选组件：没装给「安装」、装好给「卸载」、装着显示进度不给按钮；上次失败写原因；
  「中断」不显示成正在装；读取失败写原因 + 重试；后端没这个模块（null 且无错）整块不显示；
- 所有文字转义。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_goal_fit_frontend import CHECK, JS

HARNESS = CHECK.replace(
    "const view = cfg.view || {};",
    "const view = cfg.view || {}; Object.assign(state, cfg.state || {});",
).replace(
    "const html = cfg.call === 'detailHTML' ? fn() : fn({id: 'g1'}, view);",
    "const html = fn();",
)


def render(tmp_path, real, call, **cfg):
    node = shutil.which("node")
    assert node, "没有 Node，不能验证网页"
    f = tmp_path / "attn.mjs"
    f.write_text(HARNESS % json.dumps({"root": str(JS), "real": real, "call": call, **cfg}), encoding="utf-8")
    r = subprocess.run([node, "--experimental-vm-modules", str(f)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])["html"]


ATT = ["settings/attention.js"]
OD = ["settings/optdeps.js"]


def test_attention_lists_review_asks_and_failed_cards(tmp_path):
    html = render(tmp_path, ATT, "attentionSection", state={"attention": {
        "asks": {"total": 2, "items": [
            {"group_id": "111", "group_name": "<测试群>", "wait_text": "17 天", "message_id": "a"},
            {"group_id": "111", "group_name": "<测试群>", "wait_text": "3 天", "message_id": "b"},
        ]},
        "cards": {"cards": [
            {"id": 99, "group_id": "111", "group_name": "测试群", "status": "failed", "created": 1760000000,
             "titles": ["<b>头条</b>"], "error": "send.hybrid 发送失败：超时", "fallback": {"status": "sent"}},
        ]},
    }})
    assert "待处理" in html
    assert "2 条 @ 请求" in html and "17 天" in html and 'data-act="att-asks"' in html and 'data-g="111"' in html
    assert "&lt;测试群&gt;" in html and "<测试群>" not in html
    assert 'data-act="att-resend"' in html and 'data-id="99"' in html and "发送失败" in html
    assert "&lt;b&gt;头条" in html and "已改发文字版" in html and "超时" in html


@pytest.mark.parametrize(("fallback", "expected"), [
    ({}, ""),
    ({"status": "queued"}, "文字版等待发送"),
    ({"status": "sent"}, "已改发文字版"),
    ({"status": "failed"}, "文字版发送失败"),
    ({"status": "uncertain"}, "文字版发送结果未知"),
    ({"status": "dropped"}, "文字版已取消"),
    ({"status": "no_items"}, "无可发送的文字内容"),
])
def test_attention_reports_actual_text_fallback_status(tmp_path, fallback, expected):
    html = render(tmp_path, ATT, "attentionSection", state={"attention": {"cards": {"cards": [
        {"id": 1, "status": "failed", "fallback": fallback},
    ]}}})
    if expected:
        assert expected in html
    if fallback.get("status") != "sent":
        assert "已改发文字版" not in html


def test_attention_uses_untruncated_group_counts(tmp_path):
    html = render(tmp_path, ATT, "attentionSection", state={"attention": {"asks": {
        "total": 101, "group_counts": {"g1": 101},
        "items": [{"group_id": "g1", "group_name": "测试群", "wait_text": "两天"}],
    }}})
    assert "101 条 @ 请求" in html


def test_attention_load_error_is_visible_and_escaped(tmp_path):
    html = render(tmp_path, ATT, "attentionSection", state={"attention": {}, "attentionErr": {"asks": "<网络> 中断"}})
    assert "&lt;网络&gt; 中断" in html and 'data-act="att-reload"' in html


def test_attention_hidden_when_nothing(tmp_path):
    html = render(tmp_path, ATT, "attentionSection", state={"attention": {"asks": {"total": 0, "items": []}, "cards": {"cards": []}}})
    assert html.strip() == ""
    html = render(tmp_path, ATT, "attentionSection", state={"attention": {"asks": None, "cards": None}})
    assert html.strip() == ""


def _od(**g):
    base = {"id": "office", "label": "办公文档（Word / Excel / PPT）", "state": "missing", "installing": False,
            "uninstalling": False, "installed_count": 0, "total": 3,
            "packages": [{"name": "python-docx", "installed": False}, {"name": "openpyxl", "installed": None}],
            "tools": [{"name": "soffice", "found": False, "note": "转 PDF 需要 LibreOffice"}], "last_install": {}}
    base.update(g)
    return {"available": True, "groups": [base]}


def test_optdeps_missing_offers_install(tmp_path):
    html = render(tmp_path, OD, "optDepsSection", state={"optDeps": _od()})
    assert "可选组件" in html and "办公文档" in html
    assert 'data-act="od-install"' in html and 'data-act="od-uninstall"' not in html
    assert "未安装" in html and "python-docx 未安装" in html and "openpyxl 未检查" in html
    assert "LibreOffice" in html


def test_optdeps_ok_offers_uninstall_and_versions(tmp_path):
    html = render(tmp_path, OD, "optDepsSection", state={"optDeps": _od(
        state="ok", installed_count=3,
        packages=[{"name": "python-docx", "installed": True, "version": "1.1.2"}])})
    assert 'data-act="od-uninstall"' in html and 'data-act="od-install"' not in html
    assert "python-docx 1.1.2" in html and "已安装" in html


def test_optdeps_installing_shows_progress_no_buttons(tmp_path):
    html = render(tmp_path, OD, "optDepsSection", state={"optDeps": _od(state="installing", installing=True)})
    assert "正在安装" in html and 'data-act="od-install"' not in html and 'data-act="od-uninstall"' not in html


def test_optdeps_failed_and_interrupted(tmp_path):
    html = render(tmp_path, OD, "optDepsSection", state={"optDeps": _od(last_install={"state": "failed", "error": "<pip> 网络不通"})})
    assert "上次操作失败" in html and "&lt;pip&gt; 网络不通" in html and "重新安装" in html
    html = render(tmp_path, OD, "optDepsSection", state={"optDeps": _od(last_install={"state": "interrupted"})})
    assert "中断" in html and "正在安装" not in html


def test_optdeps_load_error_and_absent(tmp_path):
    html = render(tmp_path, OD, "optDepsSection", state={"optDeps": None, "optDepsErr": "请求失败（500）"})
    assert "请求失败（500）" in html and 'data-act="od-reload"' in html
    html = render(tmp_path, OD, "optDepsSection", state={"optDeps": None, "optDepsErr": ""})
    assert html.strip() == ""
    html = render(tmp_path, OD, "optDepsSection", state={"optDeps": _od(state="installing", installing=True), "optDepsErr": "<网络> 中断"})
    assert "正在安装" in html and "刷新失败：&lt;网络&gt; 中断" in html
    assert 'data-act="od-reload"' in html


def test_wired_into_pages_and_actions():
    ext = (JS / "settings/ext.js").read_text()
    assert "optDepsSection()" in ext and "await loadOptDeps()" in ext
    idx = (JS / "settings/index.js").read_text()
    assert "attentionSection()" in idx
    assert "await loadAttention()" in (JS / "router.js").read_text()
    act = (JS / "actions.js").read_text()
    assert "actOptDeps(a, el)" in act and "actAttention(a, el)" in act
    od = (JS / "settings/optdeps.js").read_text()
    assert "confirm(" in od.split('case "od-uninstall"')[1] or "confirm(" in od.split("od-uninstall")[-1]
    onb = (JS / "onboarding.js").read_text()
    assert "开箱可用" not in onb and "「设置 → 工具」安装办公文档组件" in onb


def test_usage_page_shows_imagegen(tmp_path):
    hist = {"days": [{"day": "2026-10-15", "main": 1, "worker": 1}], "totals": {},
            "imagegen": {"configured": True, "model": "<画图>", "today": {"calls": 2, "images": 3},
                         "week": {"images": 9, "failed": 1}, "total": {"calls": 5, "images": 12},
                         "last_failure": {"ts": 1760000000, "error": "<额度> 用完"}}}
    html = render(tmp_path, ["settings/usage.js"], "usagePage", state={"usage": {"days": 30, "sel": "", "hist": hist}})
    assert "生图" in html and "&lt;画图&gt;" in html and "12" in html and "&lt;额度&gt; 用完" in html
    hist["imagegen"] = {"configured": False, "total": {"calls": 0}}
    html = render(tmp_path, ["settings/usage.js"], "usagePage", state={"usage": {"days": 30, "sel": "", "hist": hist}})
    assert "生图" not in html


def _mismatch(tmp_path, ep_url):
    import json as _j
    f = tmp_path / "m7.mjs"
    src = HARNESS % _j.dumps({"root": str(JS), "real": ["settings/models.js"], "call": "presetMismatch",
                               "state": {"mdl": {"endpoints": [{"id": "e1", "name": "<默认>", "base_url": ep_url}]}}})
    src = src.replace("const html = fn();", "const html = fn({endpoint: 'e1', _preset: {label: '硅基流动', base_url: 'https://api.siliconflow.cn/v1/', key_url: 'https://cloud.siliconflow.cn'}});")
    f.write_text(src, encoding="utf-8")
    r = subprocess.run([shutil.which("node"), "--experimental-vm-modules", str(f)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])["html"]


def test_imagegen_preset_warns_when_endpoint_url_differs(tmp_path):
    """M7：填入常用生图服务时，端点地址对不上要在表单里明说（模型挂端点下，只填模型名会打到别家）。"""
    html = _mismatch(tmp_path, "https://api.openai.com/v1")
    assert "不一致" in html and "新建一个端点" in html and "&lt;默认&gt;" in html
    assert _mismatch(tmp_path, "https://API.siliconflow.cn/v1").strip() == ""
