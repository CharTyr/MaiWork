"""R6: real Coordinator artifact paths produce a safe, readable handoff excerpt."""
import json
import pytest
from test_console import env, G1
from test_review_artifacts import task, artifact


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "full"])
async def test_r6_handoff_reads_real_saved_artifact_paths(env, prefix):
    tid = task(env)
    base = artifact(env, tid)
    (base / "report.md").write_text("这是交付成品正文，不能漏掉。")
    rel = f"artifacts/{tid}/report.md" if prefix else "report.md"
    with env.app.store.tx() as conn:
        conn.execute("INSERT INTO attempts (task_id,n,req_version,status,artifacts,started) VALUES (?,1,1,'passed',?,0)", (tid, json.dumps([rel])))
    r = await env.client.get(f"/api/handoff/task/{tid}", headers={"X-MW-Group": env.app.token_of(G1)})
    assert r.status == 200, await r.text()
    md = (await r.json())["markdown"]
    assert "## 已有成品摘要" in md and "这是交付成品正文" in md


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["binary", "input", "inner_link", "root_link"])
async def test_r6_handoff_excludes_binary_input_and_links(env, kind):
    tid = task(env)
    base = artifact(env, tid)
    rel = "report.png" if kind == "binary" else "report.md"
    if kind == "input":
        rel = "Input/report.md"
        (base / "Input").mkdir()
    target = base / rel
    target.write_bytes(b"PRIVATE-EXCERPT\xff\xfe")
    if kind == "inner_link":
        (base / "alias.md").symlink_to(target)
        rel = "alias.md"
    if kind == "root_link":
        outside = base.with_name(tid + "-private")
        base.rename(outside)
        base.symlink_to(outside, target_is_directory=True)
    with env.app.store.tx() as conn:
        conn.execute("INSERT INTO attempts (task_id,n,req_version,status,artifacts,started) VALUES (?,1,1,'passed',?,0)", (tid, json.dumps([rel])))
    r = await env.client.get(f"/api/handoff/task/{tid}", headers={"X-MW-Group": env.app.token_of(G1)})
    assert r.status == 200
    md = (await r.json())["markdown"]
    assert "PRIVATE-EXCERPT" not in md
    assert "## 已有成品摘要" not in md


@pytest.mark.asyncio
async def test_r3_admin_republish_cannot_publish_unserved_group(env, monkeypatch):
    from types import SimpleNamespace
    tid = task(env)
    await env.login()
    calls = []
    async def publish(*args, **kwargs):
        calls.append(args)
        return {"ok": True, "url": "https://private.example"}
    env.app.outbox = SimpleNamespace(republish_if_expired=publish)
    monkeypatch.setattr(type(env.app.get_settings()), "is_served", lambda self, gid: False)
    r = await env.client.post(f"/api/tasks/{tid}/republish", json={})
    assert r.status == 404
    assert calls == []
