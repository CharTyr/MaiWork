"""R3: republishing stays private and is reachable from /mw 领取."""
import asyncio
import json
from pathlib import Path
import pytest
from test_outbox import _sent_herenow_row, _rows, NOON, GID
from test_commands import _env
from CharTyr_MaiWork.maiwork import clock

pytestmark = pytest.mark.asyncio


async def test_r3_republish_stages_public_files_only(tmp_path, monkeypatch):
    store, _, _, ob, _, row, hn = await _sent_herenow_row(tmp_path, expires_offset=-10)
    base = tmp_path / "ws/artifacts/T-1"
    (base / "index.html").rename(base / "报告.html")
    (base / "Input").mkdir()
    (base / "Input/private.png").write_bytes(b"private")
    (base / "alias.png").symlink_to(base / "Input/private.png")
    (base / "sub").mkdir()
    (base / "sub/INPUT").mkdir()
    (base / "sub/INPUT/private.txt").write_text("private")
    seen = {}
    async def publish(directory):
        seen.update({p.relative_to(directory).as_posix(): p.read_bytes() for p in Path(directory).rglob("*") if p.is_file()})
        return {"url": "https://new.here.now", "expires_ts": NOON + 86400}
    hn.publish = publish
    monkeypatch.setattr(clock, "now", lambda: NOON)
    result = await ob.republish_if_expired("T-1")
    assert result["ok"]
    assert seen == {"index.html": b"<html>v1</html>"}
    assert (base / "报告.html").exists()


async def test_r3_republish_uses_original_main_html_with_other_public_files(tmp_path, monkeypatch):
    store, _, _, ob, _, row, hn = await _sent_herenow_row(tmp_path, expires_offset=-10)
    base = tmp_path / "ws/artifacts/T-1"
    (base / "index.html").rename(base / "report.html")
    (base / "research.md").write_text("research")
    (base / "style.css").write_text("body{}")
    payload = json.loads(row["payload"])
    payload["fallback"]["path"] = str(base / "report.html")
    with store.tx() as conn:
        conn.execute("UPDATE outbox SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
    seen = {}
    async def publish(directory):
        seen.update({p.relative_to(directory).as_posix(): p.read_bytes() for p in Path(directory).rglob("*") if p.is_file()})
        return {"url": "https://new.here.now", "expires_ts": NOON + 86400}
    hn.publish = publish
    monkeypatch.setattr(clock, "now", lambda: NOON)
    assert (await ob.republish_if_expired("T-1"))["ok"]
    assert seen == {"index.html": b"<html>v1</html>", "research.md": b"research", "style.css": b"body{}"}


async def test_r3_concurrent_force_republish_is_throttled_before_await(tmp_path, monkeypatch):
    _, _, _, ob, _, _, hn = await _sent_herenow_row(tmp_path, expires_offset=-10)
    monkeypatch.setattr(clock, "now", lambda: NOON)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def publish(directory):
        calls.append(directory)
        entered.set()
        await release.wait()
        return {"url": "https://new.here.now", "expires_ts": NOON + 86400}
    hn.publish = publish
    first = asyncio.create_task(ob.republish_if_expired("T-1", force=True))
    await entered.wait()
    second = asyncio.create_task(ob.republish_if_expired("T-1", force=True))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)
    assert len(calls) == 1
    assert [r.get("reason") for r in results] == [None, "throttled"]


async def test_r2_local_url_is_a_cookie_free_group_path(tmp_path, monkeypatch):
    store, _, _, _, delivery, _, _ = await _sent_herenow_row(tmp_path)
    with store.tx() as conn:
        conn.execute("UPDATE groups SET token='group-access' WHERE group_id=?", (GID,))
    record = next(r for r in delivery.delivery_records("T-1") if r["raw_kind"] == "herenow")
    assert record["local_url"] == "/g/group-access/artifacts/T-1/"


@pytest.mark.parametrize("success", [True, False])
async def test_r3_expired_claim_actually_republishes(tmp_path, success):
    env = _env(tmp_path, public_url="https://maiwork.example")
    calls = []
    env.outbox.claim_delivery = lambda gid, tid: "expired"
    async def republish(tid):
        calls.append(tid)
        return {"ok": success, "url": "https://fresh.here.now"} if success else {"ok": False, "reason": "throttled"}
    env.outbox.republish_if_expired = republish
    reply = await env.say("/mw 领取 T-1")
    assert calls == ["T-1"]
    if success:
        assert "https://fresh.here.now" in reply
        assert "https://maiwork.example/g/kTok1234/artifacts/T-1/" in reply
    else:
        assert "本地副本" in reply
