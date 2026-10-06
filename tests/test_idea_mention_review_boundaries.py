"""主会话独立验收：首次也要证据；旧队列不得绕过复核。"""
import json
import pytest
import test_idea_mention as t
from CharTyr_MaiWork.maiwork.outbox import Outbox

pytestmark = pytest.mark.asyncio

@pytest.mark.parametrize("reply", [t._guard(), t._guard(need=True, evidence=["invented"])])
async def test_first_personal_mention_requires_real_need(tmp_path, reply):
    store, host, models, pushes, im, ob = t._make(tmp_path, replies=[reply])
    t._enable(store)
    t._idea(store, target=t.UID)
    im.scan(t.GID, t.NOON + 10)
    await im.flush(t.GID, t.NOON + 10)
    await ob.flush(t.NOON + 11)
    assert not host.texts, "首次提起也不能把没证据的画像猜想发出去"
    assert not t._write_calls(models)

async def test_personal_queue_without_producer_guard_never_sends(tmp_path):
    store, host, models, pushes, im, ob = t._make(tmp_path)
    t._enable(store)
    iid = t._idea(store, target=t.UID)
    im.scan(t.GID, t.NOON + 10)
    mid = int(t._rows(store)[0]["id"])
    # 模拟旧队列重启，IdeaMention 构造/挂接失败，但 Outbox 仍在。
    unguarded = Outbox(store, host, pushes, im._mentions, im._get_settings)
    unguarded.enqueue(f"idea_mention:{mid}", t.GID, "text", {
        "text": "旧的提议", "at_user": t.UID, "push_kind": "idea_mention",
        "expires_ts": t.NOON + 3600,
    })
    await unguarded.flush(t.NOON + 11)
    assert not host.texts, "复核组件没挂上时不能把旧个人提议放出去"


async def test_queue_invalidated_by_new_chat_even_if_old_evidence_exists(tmp_path):
    store, host, models, pushes, im, ob = t._make(tmp_path)
    t._enable(store)
    t._idea(store, target=t.UID)
    t._chat(store, "那个追更表还没弄好，能帮我做吗", ts=t.NOON - 10, mid="need-1")
    models.replies = [t._guard(need=True, evidence=["need-1"]), json.dumps({"text": "追更表要不要我来弄？"})]
    im.scan(t.GID, t.NOON + 10)
    await im.flush(t.GID, t.NOON + 10)
    assert t._boxes(store), "正例复核应当入队"
    t._chat(store, "追更表我自己弄好了，不用帮了", ts=t.NOON + 20, mid="solved-1")
    before = len(models.calls)
    await ob.flush(t.NOON + 30)
    assert not host.texts, "原引用还在不能代表需求没变"
    assert len(models.calls) == before, "发件箱的锁里不能进行模型复核"


async def test_partial_invented_evidence_is_not_silently_filtered(tmp_path):
    store, host, models, pushes, im, ob = t._make(tmp_path)
    t._enable(store)
    t._idea(store, target=t.UID)
    t._chat(store, "那个追更表能帮我做吗", ts=t.NOON - 10, mid="need-1")
    models.replies = [t._guard(need=True, evidence=["need-1", "invented"])]
    im.scan(t.GID, t.NOON + 10)
    await im.flush(t.GID, t.NOON + 10)
    await ob.flush(t.NOON + 11)
    assert not host.texts, "复核引用须全部可信，不能悄悄忽略不存在的引用"


async def _valid_queue(tmp_path):
    store, host, models, pushes, im, ob = t._make(tmp_path)
    t._enable(store)
    t._idea(store, target=t.UID)
    t._chat(store, "那个追更表还没做，能帮忙吗", ts=t.NOON - 10, mid="need-1")
    models.replies = [t._guard(need=True, evidence=["need-1"]), json.dumps({"text": "追更表要不要我来弄？"})]
    im.scan(t.GID, t.NOON + 10)
    await im.flush(t.GID, t.NOON + 10)
    assert t._boxes(store)
    return store, host, models, pushes, im, ob


async def test_restart_with_other_hook_does_not_bypass_personal_guard(tmp_path):
    store, host, models, pushes, im, ob = await _valid_queue(tmp_path)
    unguarded = Outbox(store, host, pushes, im._mentions, im._get_settings)
    unguarded.add_preflight_hook(lambda info: None)  # 例如冷场开场白的检查，不能冒充个人建议检查
    with store.tx() as c:
        c.execute("UPDATE focus_members SET removed=1 WHERE group_id=? AND user_id=?", (t.GID,t.UID))
    await unguarded.flush(t.NOON + 11)
    assert not host.texts


async def test_task_created_while_queued_invalidates_review(tmp_path):
    store, host, models, pushes, im, ob = await _valid_queue(tmp_path)
    with store.tx() as c:
        c.execute("INSERT INTO tasks (id,group_id,workspace,requester_id,title,status,created,updated) VALUES ('T-review',?,'test',?,'番剧追更表','completed',?,?)", (t.GID,t.UID,t.NOON+15,t.NOON+15))
    await ob.flush(t.NOON+20)
    assert not host.texts, "已有任务完成，不能用任务落库前的复核继续邀约"


async def test_late_ingested_chat_invalidates_old_review(tmp_path):
    store, host, models, pushes, im, ob = await _valid_queue(tmp_path)
    # profile.tick 补读：发言时间早于复核，实际在复核后才入库。
    t._chat(store, "追更表我已经做完了不用再做", ts=t.NOON + 5, mid="late-solved")
    await ob.flush(t.NOON + 20)
    assert not host.texts, "材料增加要使旧复核失效，不能只比较消息时间是否晚于checked_ts"


async def test_web_task_link_invalidates_review_without_requester(tmp_path):
    store, host, models, pushes, im, ob = await _valid_queue(tmp_path)
    with store.tx() as c:
        c.execute("INSERT INTO tasks (id,group_id,workspace,requester_id,title,status,created,updated) VALUES ('T-web',?,'test','','番剧追更表','queued',?,?)", (t.GID,t.NOON+15,t.NOON+15))
        c.execute("UPDATE ideas SET task_id='T-web' WHERE group_id=?", (t.GID,))
    await ob.flush(t.NOON+20)
    assert not host.texts, "网页建任务未填requester也必须停掉旧提议"


async def test_replaced_chat_with_reused_rowid_invalidates_review(tmp_path):
    store, host, models, pushes, im, ob = t._make(tmp_path)
    t._enable(store)
    t._idea(store, target=t.UID)
    t._chat(store, "追更表还没弄好，能帮忙吗", ts=t.NOON-10, mid="need-1")
    t._chat(store, "我去喝水", ts=t.NOON, mid="filler")
    models.replies = [t._guard(need=True,evidence=["need-1"]), json.dumps({"text":"追更表要不要我来弄？"})]
    im.scan(t.GID,t.NOON+10)
    await im.flush(t.GID,t.NOON+10)
    before=store.read().execute("SELECT max(rowid) FROM chat_log").fetchone()[0]
    with store.tx() as c:
        c.execute("DELETE FROM chat_log WHERE message_id='filler'")
    t._chat(store,"追更表已经搞定，不用了",ts=t.NOON+5,mid="solved")
    assert store.read().execute("SELECT max(rowid) FROM chat_log").fetchone()[0] == before
    await ob.flush(t.NOON+20)
    assert not host.texts, "规模不变也可能发生材料替换"


async def test_member_removed_during_review_stops_before_writing(tmp_path):
    store,host,models,pushes,im,ob = t._make(tmp_path)
    t._enable(store);t._idea(store,target=t.UID)
    t._chat(store,"追更表可以帮我做吗",ts=t.NOON-10,mid="need-1")
    models.replies=[t._guard(need=True,evidence=["need-1"])]
    original=models.chat
    async def changing_chat(*a,**kw):
        result=await original(*a,**kw)
        if kw.get("purpose")=="card_push.idea_guard":
            with store.tx() as c:
                c.execute("UPDATE focus_members SET removed=1 WHERE group_id=? AND user_id=?",(t.GID,t.UID))
        return result
    models.chat=changing_chat
    im.scan(t.GID,t.NOON+10)
    await im.flush(t.GID,t.NOON+10)
    assert not t._write_calls(models), "复核等候期间取消关注后不得继续写话"


async def test_existing_pending_row_cannot_bypass_three_day_cooldown(tmp_path):
    store,host,models,pushes,im,ob = await _valid_queue(tmp_path)
    with store.tx() as c:
        c.execute("INSERT INTO idea_mentions (group_id,idea_id,status,at_user,created,due_ts,sent_ts) VALUES (?,999,'sent',?,?,?,?)",(t.GID,t.UID,t.NOON-86400,t.NOON-86400,t.NOON-86400))
    await ob.flush(t.NOON+20)
    assert not host.texts, "发送前也须落实三天冷却，而不只scan时检查"
