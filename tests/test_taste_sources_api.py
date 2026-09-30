"""接口：口味小结 / 优质来源（docs/10 第七节第 6、7 步）。只给管理员或本群群管理员。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_app import G1
from test_extensions_web import _login, _make


@pytest.mark.asyncio
async def test_taste_api(tmp_path: Path):
    app, client = await _make(tmp_path)
    try:
        r = await client.get(f"/api/groups/{G1}/taste")
        assert r.status in (401, 403)
        await _login(client)
        r = await client.get(f"/api/groups/{G1}/taste")
        assert r.status == 200 and (await r.json())["text"] == ""
        r = await client.put(f"/api/groups/{G1}/taste", json={"text": "爱看开发内幕"})
        assert r.status == 200
        body = await r.json()
        assert body["text"] == "爱看开发内幕" and body["manual"] is True
        r = await client.get("/api/groups/999/taste")
        assert r.status == 404
    finally:
        await client.close()
        await app.stop()


@pytest.mark.asyncio
async def test_trusted_sources_api(tmp_path: Path):
    app, client = await _make(tmp_path)
    try:
        await _login(client)
        from CharTyr_MaiWork.maiwork import clock

        now = clock.now()
        with app.store.tx() as conn:
            for i in range(2):
                conn.execute(
                    "INSERT INTO news_items (batch_id, group_id, title, url_key, sources, scores, rejected, created)"
                    " VALUES (1, ?, ?, ?, ?, ?, 0, ?)",
                    (G1, f"t{i}", f"gcores.com/{i}", json.dumps([{"site": "gcores.com"}]), json.dumps({"avg": 4.5}), now - 3600),
                )
        r = await client.get(f"/api/groups/{G1}/trusted-sources")
        assert r.status == 200
        v = await r.json()
        assert [t["domain"] for t in v["trusted"]] == ["gcores.com"]
        r = await client.post(f"/api/groups/{G1}/trusted-sources", json={"domain": "gcores.com", "removed": True})
        assert r.status == 200
        v = await r.json()
        assert v["trusted"] == [] and v["removed"] == ["gcores.com"]
        r = await client.post(f"/api/groups/{G1}/trusted-sources", json={"domain": "", "removed": True})
        assert r.status == 400
    finally:
        await client.close()
        await app.stop()


@pytest.mark.asyncio
async def test_go_click_redirects_and_records(tmp_path: Path):
    app, client = await _make(tmp_path)
    try:
        from CharTyr_MaiWork.maiwork import clock

        with app.store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO news_items (batch_id, group_id, title, url_key, sources, rejected, created)"
                " VALUES (1, ?, 't', 'a.example/x', ?, 0, ?)",
                (G1, json.dumps([{"url": "https://a.example/x?y=1", "site": "a.example"}]), clock.now()),
            )
            iid = int(cur.lastrowid)
        r = await client.get(f"/go/{iid}", allow_redirects=False)
        assert r.status in (401, 403)
        from CharTyr_MaiWork.maiwork.console import views

        tok = views.token_of(app, G1) if hasattr(app, "get_settings") else ""
        if tok:
            r = await client.get(f"/go/{iid}?g={tok}&c=member-1", allow_redirects=False)
            assert r.status == 302
            r = await client.get(f"/go/{iid}?g=wrong-token", allow_redirects=False)
            assert r.status == 401
        await _login(client)
        r = await client.get(f"/go/{iid}?c=browser-1&u=https://evil.example", allow_redirects=False)
        assert r.status == 302 and r.headers["Location"] == "https://a.example/x?y=1"
        n = app.store.read().execute("SELECT COUNT(*) FROM news_feedback WHERE kind='click'").fetchone()[0]
        assert n == (2 if tok else 1)  # 群友那次 + 管理员这次，各记一次
        r = await client.get("/go/999999", allow_redirects=False)
        assert r.status == 404
    finally:
        await client.close()
        await app.stop()
