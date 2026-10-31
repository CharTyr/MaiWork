"""Adversarial artifact boundary regressions (R2)."""
import pytest
from test_console import env, G1


def artifact(env, tid):
    base = env.tmp_path / "workspaces/tinker/artifacts" / tid
    base.mkdir(parents=True)
    return base


def task(env):
    return env.app.tasks.create(G1, title="t", req="r", criteria=[], source="test", status="completed")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["task_root", "inner", "input_alias", "upper_input", "nested_input"])
async def test_r2_rejects_raw_symlinks_and_input_at_every_level(env, kind):
    tid = task(env)
    base = artifact(env, tid)
    if kind == "task_root":
        outside = env.tmp_path / "private"
        outside.mkdir()
        (outside / "secret.txt").write_text("private")
        # Empty fixture directory, replaced with the malicious task-root link.
        assert base == env.tmp_path / "workspaces/tinker/artifacts" / tid
        base.rmdir()
        base.symlink_to(outside, target_is_directory=True)
        rel = "secret.txt"
    elif kind in ("inner", "input_alias"):
        folder = "input" if kind == "input_alias" else "real"
        (base / folder).mkdir()
        (base / folder / "secret.txt").write_text("private")
        (base / "alias").symlink_to(base / folder, target_is_directory=True)
        rel = "alias/secret.txt"
    else:
        rel = "Input/secret.txt" if kind == "upper_input" else "sub/INPUT/secret.txt"
        target = base / rel
        target.parent.mkdir(parents=True)
        target.write_text("private")
    token = env.app.token_of(G1)
    r = await env.client.get(f"/g/{token}/artifacts/{tid}/{rel}")
    assert r.status in (403, 404), (kind, r.status, await r.text())


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix,mime", [("css", "text/css"), ("js", "javascript"), ("mjs", "javascript"), ("png", "image/png"), ("jpg", "image/jpeg"), ("webp", "image/webp"), ("woff2", "font/woff2"), ("txt", "text/plain")])
async def test_r2_assets_have_usable_mime_cors_and_framing(env, suffix, mime):
    tid = task(env)
    base = artifact(env, tid)
    (base / f"asset.{suffix}").write_bytes(b"asset")
    token = env.app.token_of(G1)
    r = await env.client.get(f"/g/{token}/artifacts/{tid}/asset.{suffix}", headers={"Origin": "null"})
    assert r.status == 200
    assert mime in r.headers["Content-Type"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Access-Control-Allow-Origin"] == "*"
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in r.headers["Content-Security-Policy"]


@pytest.mark.asyncio
async def test_r2_html_opaque_origin_resources_and_task_workspace(env):
    tid = task(env)
    with env.app.store.tx() as conn:
        conn.execute("UPDATE tasks SET workspace='original' WHERE id=?", (tid,))
    base = env.tmp_path / "workspaces/original/artifacts" / tid
    base.mkdir(parents=True)
    (base / "index.html").write_text('<script type="module" src="./app.js"></script>')
    token = env.app.token_of(G1)
    r = await env.client.get(f"/g/{token}/artifacts/{tid}/")
    assert r.status == 200
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'unsafe-inline' data: blob: http: https:" in csp
    assert "connect-src 'none'" in csp and "allow-same-origin" not in csp
    assert "object-src 'none'" in csp and "form-action 'none'" in csp


@pytest.mark.asyncio
async def test_r2_unicode_attachment_filename_is_rfc5987(env):
    from urllib.parse import quote
    tid = task(env)
    base = artifact(env, tid)
    (base / "报告.pdf").write_bytes(b"%PDF")
    token = env.app.token_of(G1)
    r = await env.client.get(f"/g/{token}/artifacts/{tid}/报告.pdf")
    assert r.status == 200
    assert r.headers["Content-Disposition"] == "attachment; filename*=UTF-8''" + quote("报告.pdf")


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_r2_local_root_selects_passed_main_among_public_files(env, nested):
    import json
    tid = task(env)
    base = artifact(env, tid)
    rel = "site/report.html" if nested else "report.html"
    target = base / rel
    target.parent.mkdir(exist_ok=True)
    target.write_text('<link rel="stylesheet" href="./style.css"><p>chosen main</p>')
    (target.parent / "style.css").write_text("body{}")
    (base / "other.html").write_text("not the main")
    (base / "research.md").write_text("research")
    with env.app.store.tx() as conn:
        conn.execute("INSERT INTO attempts (task_id,n,req_version,status,artifacts,started) VALUES (?,1,1,'passed',?,0)", (tid, json.dumps([f"artifacts/{tid}/{rel}"])))
    token = env.app.token_of(G1)
    response = await env.client.get(f"/g/{token}/artifacts/{tid}/")
    assert response.status == 200
    assert "chosen main" in await response.text()
    if nested:
        assert response.url.path.endswith("/site/report.html")


@pytest.mark.asyncio
async def test_r2_r3_member_detail_preview_and_republish_roundtrip(env):
    from CharTyr_MaiWork.maiwork import clock
    tid = task(env)
    base = artifact(env, tid)
    (base / "index.html").write_text('<link rel="stylesheet" href="./style.css">')
    (base / "style.css").write_text("body {color: red}")
    (base / "input").mkdir()
    (base / "input/private.txt").write_text("private")
    ob = env.app.outbox
    oid = ob.enqueue(f"task:{tid}:deliver", G1, "herenow", {"note": "成品", "push_kind": "delivery"}, task_id=tid)
    ob._set(oid, status="sent", result={"url": "https://old.here.now/", "expires_ts": clock.now() - 10})
    snapshots = []
    class Publisher:
        async def publish(self, directory):
            snapshots.append(sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file()))
            return {"url": "https://fresh.here.now/", "expires_ts": clock.now() + 86400}
    ob._herenow = Publisher()
    token = env.app.token_of(G1)
    headers = {"X-MW-Group": token}
    response = await env.client.get(f"/api/tasks/{tid}", headers=headers)
    assert response.status == 200
    rec = next(x for x in (await response.json())["delivery"] if x["raw_kind"] == "herenow")
    assert rec["expired"] is True and rec["local_url"] == f"/g/{token}/artifacts/{tid}/"
    # iframe + relative assets carry neither an API header nor an administrator cookie.
    assert not env.client.session.cookie_jar
    page = await env.client.get(rec["local_url"])
    css = await env.client.get(rec["local_url"] + "style.css", headers={"Origin": "null"})
    assert page.status == css.status == 200
    assert css.content_type == "text/css" and css.headers["Access-Control-Allow-Origin"] == "*"
    republish = await env.client.post(f"/api/tasks/{tid}/republish", json={}, headers=headers)
    assert republish.status == 200
    assert (await republish.json())["url"] == "https://fresh.here.now/"
    assert snapshots == [["index.html", "style.css"]]
    refreshed = await env.client.get(f"/api/tasks/{tid}", headers=headers)
    new_rec = next(x for x in (await refreshed.json())["delivery"] if x["raw_kind"] == "herenow")
    assert new_rec["expired"] is False and new_rec["url"] == "https://fresh.here.now/"

