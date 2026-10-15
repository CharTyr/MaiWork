"""执行真实 ES 模块，验证异步刷新不会把查询失败当作操作成功。"""
import json
import shutil
import subprocess

from test_attention_optdeps_frontend import HARNESS, JS


def run(tmp_path, real, call, **cfg):
    script = HARNESS.replace(
        'const real = new Set(cfg.real);',
        '''const real = new Set(cfg.real);
const calls = [], toasts = [], timers = [];
defaults.api = async (...args) => {
  calls.push(args);
  const out = (cfg.responses || [])[calls.length - 1];
  if (out && out.error) { const e = new Error(out.error); e.status = out.status; throw e; }
  return out;
};
defaults.toast = (...args) => toasts.push(args);
defaults.ui = {};
''',
    ).replace(
        'console, Date, URLSearchParams, document:',
        '''console, Date, URLSearchParams, setTimeout: (fn) => {timers.push(fn); return timers.length;}, clearTimeout: () => {},
setInterval: () => 0, location: {hash: "#/settings/overview"}, history: {replaceState() {}}, window: {scrollTo() {}}, document:''',
    ).replace(
        'const html = fn();',
        '''await fn(...(cfg.args || []));
if (cfg.poll) { const tick = timers.pop(); if (!tick) throw new Error("没有安排轮询"); await tick(); }
const html = {state, calls, toasts, timers: timers.length};''',
    )
    f = tmp_path / "async-state.mjs"
    f.write_text(script % json.dumps({"root": str(JS), "real": real, "call": call, **cfg}), encoding="utf-8")
    node = shutil.which("node")
    assert node, "没有 Node，不能验证网页"
    r = subprocess.run([node, "--experimental-vm-modules", str(f)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])["html"]


def installing(**kw):
    return {"available": True, "groups": [{"id": "office", "installing": True, "last_install": {"state": "installing"}}], **kw}


def test_poll_failure_keeps_progress_and_does_not_announce_success(tmp_path):
    initial = installing()
    out = run(tmp_path, ["settings/optdeps.js"], "loadOptDeps", poll=True,
              state={"page": "settings", "setSub": "extensions"},
              responses=[initial, {"error": "网络中断", "status": 502}])
    assert out["toasts"] == []
    assert out["state"]["optDeps"] == initial
    assert out["state"]["optDepsErr"] == "网络中断"
    assert out["timers"] == 1


def test_poll_uninstall_failure_uses_uninstall_copy(tmp_path):
    initial = {"available": True, "groups": [{"id": "office", "uninstalling": True}]}
    failed = {"available": True, "groups": [{"id": "office", "last_install": {"state": "failed"}}]}
    out = run(tmp_path, ["settings/optdeps.js"], "loadOptDeps", poll=True,
              state={"page": "settings", "setSub": "extensions"}, responses=[initial, failed])
    assert out["toasts"] == [["卸载没成功，原因见「可选组件」", True]]


def test_probe_completion_does_not_announce_install_success(tmp_path):
    out = run(tmp_path, ["settings/optdeps.js"], "loadOptDeps", poll=True,
              state={"page": "settings", "setSub": "extensions"},
              responses=[{"checking": True}, {"checking": False}])
    assert out["toasts"] == []


def test_attention_refresh_failure_keeps_pending_items(tmp_path):
    asks = {"total": 1, "items": [{"group_id": "g1", "message_id": "m1"}]}
    out = run(tmp_path, ["settings/attention.js"], "loadAttention",
              state={"attention": {"asks": asks, "cards": {"cards": []}}},
              responses=[{"error": "请求中断", "status": 502}, {"cards": []}])
    assert out["state"]["attention"]["asks"] == asks
    assert out["state"]["attentionErr"]["asks"] == "请求中断"


def test_reenter_settings_overview_refreshes_attention_with_cached_settings(tmp_path):
    out = run(tmp_path, ["router.js", "settings/attention.js"], "enterSettings", args=["overview"],
              state={"settings": {}, "page": None}, responses=[{"total": 0, "items": []}, {"cards": []}])
    assert [c[1] for c in out["calls"]] == ["/api/asks/review", "/api/news/cards?limit=10"]


def test_poll_unknown_result_does_not_announce_install_success(tmp_path):
    out = run(tmp_path, ["settings/optdeps.js"], "loadOptDeps", poll=True,
              state={"page": "settings", "setSub": "extensions"},
              responses=[installing(), {"available": True, "groups": [{"id": "office", "state": "unknown"}]}])
    assert out["toasts"] == []
