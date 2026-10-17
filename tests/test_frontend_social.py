"""社交平台界面的行为回归：执行真实 JS，网络和抽屉由轻量替身提供。"""
import re
import struct
import subprocess
from pathlib import Path

from CharTyr_MaiWork.maiwork import social_platforms as sp

JS = Path(__file__).resolve().parent.parent / "maiwork/console/static/js"
LOGOS = Path(__file__).resolve().parent.parent / "maiwork/console/static/assets/logos/social"


def run_social(body):
    source = (JS / "social-brand.js").read_text() + "\n" + (JS / "settings/social.js").read_text()
    source = "\n".join(line for line in source.splitlines() if not line.startswith("import "))
    source = source.replace("export ", "")
    setup = r'''
import assert from "node:assert/strict";
const listeners = {}, calls = [], timers = [];
const state = {page: "settings", setSub: "social", me: {role: "admin"}};
const esc = s => String(s ?? "").replaceAll("<", "&lt;");
const ico = () => "", tip = s => `<details>${s}</details>`;
const tipLabel = (id, text, help) => `<label for="${id}">${text}</label>${tip(help)}`;
const toast = () => {}, loading = () => "加载中…";
const openSheet = () => {}, closeSheet = () => {state.sheet = null;};
const repaints = []; const repaintSheet = opts => repaints.push(opts);
const document = {addEventListener: (name, fn) => listeners[name] = fn,
  getElementById: id => nodes[id] || null, dispatchEvent: () => {}, hidden: false};
const nodes = {};
const setTimeout = (fn, delay) => {timers.push({fn, delay}); return timers.length;};
const clearTimeout = () => {};
let reply = async (method, path) => method === "GET" ? {platforms: social.platforms || []} : {ok: true};
const api = async (method, path, data) => {calls.push({method, path, data}); return reply(method, path, data);};
'''
    result = subprocess.run(["node", "--input-type=module", "-e", setup + source + "\n{\n" + body + "\n}"], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_brand_logo_allowlist_matches_backend_platforms():
    """前端白名单落后于后端注册表时，新平台会静默退回通用图标——这里挡住这种漂移。"""
    source = (JS / "social-brand.js").read_text()
    listed = re.search(r"const known = \[([^\]]*)\]", source)
    assert listed, "social-brand.js 里应有平台白名单"
    ids = set(re.findall(r'"([a-z0-9_]+)"', listed.group(1)))
    assert ids == set(sp.PLATFORMS), f"前端白名单与后端平台不一致：{ids ^ set(sp.PLATFORMS)}"
    for pid in sorted(ids):
        png = LOGOS / f"{pid}-128.png"
        assert png.is_file(), f"缺品牌图 {png.name}"
        raw = png.read_bytes()[:24]
        assert raw[:8] == b"\x89PNG\r\n\x1a\n" and raw[12:16] == b"IHDR", f"{png.name} 不是 PNG"
        assert struct.unpack(">II", raw[16:24]) == (128, 128), f"{png.name} 不是 128×128"


def test_platform_rows_show_brand_logos_with_fallback():
    run_social(r'''
social.platforms = [
  {id: "twitter", name: "Twitter/X", enabled: true, installed: true, needs_cookie: true, cookie_set: true},
  {id: "xiaohongshu", name: "小红书", enabled: false},
  {id: "futurenet", name: "未来平台", enabled: false},
];
const rows = social.platforms.map(platformRow).join("");
assert.equal(rows.match(/class="social-logo"/g).length, 2, "已知平台用品牌 logo，未知平台回退通用图标");
assert.ok(rows.includes("/static/assets/logos/social/twitter-128.png"));
assert.ok(rows.includes("/static/assets/logos/social/xiaohongshu-128.png"));
assert.ok(!rows.includes("/social/futurenet"), "未知平台不拼 logo 路径");
const login = socialPage();
assert.match(login, /class="social-logo"[^>]*logos\/social\/twitter-128\.png/, "登录态页也用品牌 logo");
''')


def test_check_all_without_platform_id():
    run_social('''
social.platforms = [{id: "youtube", enabled: true, installed: true}];
assert.equal(await actSocial("social-check-all", {dataset: {}}), true);
assert.equal(calls.filter(c => c.path.endsWith("/check")).length, 1);
''')


def test_cookie_is_masked_and_accessible():
    run_social('''
state.cookieEdit = {id: "twitter", name: "Twitter/X", guide: "登录后复制"};
const html = socialCookieSheet();
assert.match(html, /type="password"/);
assert.match(html, /aria-describedby="cookie-help cookie-err"/);
assert.doesNotMatch(html, /<textarea|sheet-lead/);
assert.match(html, /maxlength="8000"/);
''')


def test_cookie_submit_blocks_duplicates_and_keeps_input_on_failure():
    run_social('''
state.cookieEdit = {id: "twitter"}; state.sheet = "social-cookie";
const attrs = {};
nodes["cookie-input"] = {value: "auth_token=private", setAttribute: (k,v) => attrs[k]=v};
nodes["cookie-err"] = {textContent: "", hidden: true};
const button = {disabled: false, textContent: "保存"};
const form = {id: "social-cookie-form", querySelector: () => button, setAttribute: () => {}};
let finish; reply = () => new Promise((resolve,reject) => finish = reject);
const event = {target: form, preventDefault() {}};
const first = listeners.submit(event);
await listeners.submit(event);
assert.equal(calls.length, 1);
assert.equal(button.disabled, true);
finish(new Error("连接中断")); await first;
assert.equal(nodes["cookie-input"].value, "auth_token=private");
assert.equal(button.disabled, false);
assert.equal(nodes["cookie-err"].textContent, "连接中断");
''')


def test_refresh_failure_retains_old_state_and_offers_retry():
    run_social('''
social.platforms = [{id: "youtube", name: "YouTube"}];
reply = async () => {throw new Error("连接中断");};
await loadSocial();
assert.equal(social.platforms.length, 1);
assert.match(socialPage(), /social-reload/);
assert.match(socialPage(), /连接中断/);
''')


def test_login_actions_are_inside_the_platform_configuration():
    run_social('''
social.platforms = [{id: "twitter", name: "Twitter/X", enabled: true, installed: true, needs_cookie: true, cookie_set: true},
{id: "youtube", name: "YouTube", enabled: true, installed: true}];
const html = socialPage();
assert.match(html, /social-cookie-clear/);
assert.match(html, /登录态已填/);
assert.match(html, /social-check/);
assert.match(html, /无需填写登录态/);
assert.equal((html.match(/data-act="social-cookie"/g) || []).length, 1);
''')


def test_cookie_instructions_are_visible_and_platform_specific():
    run_social('''
for (const [id, url, fields] of [
 ["twitter", "https://x.com", "auth_token"],
 ["xiaohongshu", "https://www.xiaohongshu.com", "web_session"],
 ["reddit", "https://www.reddit.com", "完整 Cookie"],
]) {
 state.cookieEdit = {id, name:id};
 const html = socialCookieSheet();
 assert.match(html, /<ol/);
 assert.ok(html.includes(url));
 assert.ok(html.includes(fields));
 assert.match(html, /Network/);
 assert.match(html, /Request Headers/);
 assert.match(html, /Cookie:/);
 assert.match(html, /检测/);
 assert.doesNotMatch(html, /<details[^>]*>[^]*id="cookie-help"/);
}
''')


def test_failed_check_is_not_shown_as_success_and_install_can_stop():
    run_social('''
social.platforms = [{id:"youtube", name:"YouTube", enabled:true, installed:true, last_check:123, last_error:"检测失败"}];
assert.match(socialPage(), /检测未通过/);
social.platforms[0].installing = true;
const html = socialPage();
assert.match(html, /data-act="social-disable"[^>]*(?<!disabled)>/);
assert.doesNotMatch(html, /data-act="social-disable"[^>]*disabled/);
''')


def test_install_poll_leaves_page_and_preserves_sheet():
    run_social('''
social.platforms = [{id:"youtube", name:"YouTube", enabled:true, installed:false}];
await actSocial("social-install", {dataset: {id: "youtube"}});
assert.ok(repaints.every(r => r && r.preserveSheet));
assert.ok(timers.length > 0);
const before = calls.length;
state.page = "chat";
await timers.at(-1).fn();
assert.equal(calls.length, before);
''')
