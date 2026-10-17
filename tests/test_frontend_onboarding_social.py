"""执行社交平台引导模块；真实浏览器布局另行验收。"""
import subprocess
from pathlib import Path

JS = Path(__file__).resolve().parent.parent / "maiwork/console/static/js"


def test_social_onboarding_selection_and_partial_failure():
    source = (JS / "social-brand.js").read_text() + "\n" + (JS / "onboarding-social.js").read_text()
    source = "\n".join(line for line in source.splitlines() if not line.startswith("import ")).replace("export ", "")
    setup = '''
import assert from "node:assert/strict";
const esc = s => String(s ?? ""), ico = () => "", tip = s => `<details>${s}</details>`;
const checks = [], calls = [];
const document = {querySelectorAll: () => checks};
let fail = false;
const api = async (method, path) => {
 calls.push({method, path});
 if (method === "GET") return {platforms:[{id:"youtube",name:"YouTube",enabled:false},{id:"twitter",name:"Twitter/X",enabled:true,needs_cookie:true}]};
 if (fail && path.includes("twitter")) throw new Error("连接中断");
 return {ok:true};
};
'''
    body = '''
await loadOnbSocial();
assert.match(onbSocialPane(), /type="checkbox"/);
assert.match(onbSocialPane(), /登录态/);
assert.equal(onbSocialPane().match(/class="social-logo"/g).length, 2);
checks.push({value:"youtube", checked:true}, {value:"twitter", checked:true});
await saveOnbSocial();
assert.equal(calls.filter(c => c.method === "POST").length, 1);
assert.match(calls.at(-1).path, /youtube\\/enable$/);
checks[1].checked = false; fail = true;
await assert.rejects(saveOnbSocial(), /连接中断/);
assert.equal(calls.filter(c => c.path.includes("youtube/enable")).length, 1);
'''
    result = subprocess.run(["node", "--input-type=module", "-e", setup + source + "\n" + body], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
