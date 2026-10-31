from types import SimpleNamespace
import pytest
from CharTyr_MaiWork.maiwork.intake import Intake, _IdeaMentionIndex
from test_idea_mention_reply import _make, _seed_idea_and_mention, _reply_msg, _drain, _FakeMentions, _FakeJev, GID, NOW
from test_news_feedback import _item, _card


@pytest.mark.asyncio
async def test_r8_app_send_result_registers_card_and_idea_immediately(tmp_path):
    from test_app import _app
    app = _app(tmp_path)
    await app.start()
    try:
        iid = _item(app.store,gid=GID)
        _card(app.store,[iid],mid='9002',gid=GID)
        cid = app.store.read().execute('SELECT id FROM news_cards WHERE message_id=?',('9002',)).fetchone()['id']
        idea_id = _seed_idea_and_mention(app.store, mention_mid='sent-idea')
        mention_id = app.store.read().execute('SELECT id FROM idea_mentions WHERE idea_id=?',(idea_id,)).fetchone()['id']
        for key, mid in [('news_card:'+str(cid),'9002'), ('idea_mention:'+str(mention_id),'sent-idea')]:
            oid=app.outbox.enqueue(key,GID,'text',{'text':'送达'})
            row=app.store.read().execute('SELECT * FROM outbox WHERE id=?',(oid,)).fetchone()
            app.outbox._fire_result(row,{'text':'送达'},outcome='sent',result={'message_id':mid},now=NOW)
        assert app.intake._card_index.cached_entries(GID,'9002')
        assert app.intake._idea_mention_index.lookup(None,GID,'sent-idea',NOW)['idea_id']==idea_id
    finally: await app.stop()


@pytest.mark.asyncio
async def test_r8_background_refresh_only_loads_recent_ideas(tmp_path):
    intake,store,spawned=_make(tmp_path)
    _seed_idea_and_mention(store,mention_mid='fresh')
    _seed_idea_and_mention(store,mention_mid='old')
    with store.tx() as c: c.execute('UPDATE idea_mentions SET sent_ts=? WHERE message_id=?',(NOW-31*86400,'old'))
    await intake.refresh_reply_indexes(GID,NOW)
    assert intake._idea_mention_index.lookup(None,GID,'fresh',NOW)
    assert intake._idea_mention_index.lookup(None,GID,'old',NOW) is None


@pytest.mark.asyncio
async def test_r8_card_memo_available_before_background_write(tmp_path):
    mentions=_FakeMentions()
    intake,store,spawned=_make(tmp_path, mentions=mentions)
    iid=_item(store,gid=GID)
    _card(store,[iid],mid='9001',gid=GID)
    intake._card_index.entries_for(store,GID,'9001',NOW)
    await intake.handle(_reply_msg(text='这是什么',reply_to='9001',is_at=False))
    try:
        assert any(e['key']=='card-reply:r-1' for e in mentions.ephemeral)
    finally: await _drain(spawned)


@pytest.mark.asyncio
async def test_r8_explicit_idea_reply_precedes_session_and_old_question(tmp_path):
    intake,store,spawned=_make(tmp_path)
    iid=_seed_idea_and_mention(store)
    calls=[]
    intake._session_gate=SimpleNamespace(intake=lambda *a: calls.append('gate') or True)
    intake._waiting_tasks=lambda gid:[('T-old','q-old','20002')]
    async def answer(*a): calls.append('answer')
    intake._on_answer=answer
    if hasattr(intake._idea_mention_index,'refresh'): intake._idea_mention_index.refresh(store,GID,NOW)
    await intake.handle(_reply_msg(text='做',reply_to='im-1'))
    await _drain(spawned)
    assert calls==[]
    assert intake._approvals.created[0][1]['idea_id']==iid


def test_r8_lookup_is_memory_only():
    calls=[]
    class NoSql:
        def read(self):
            calls.append('read')
            raise AssertionError('hook cannot read SQL')
    assert _IdeaMentionIndex().lookup(NoSql(),GID,'unknown',NOW) is None
    assert calls==[]


@pytest.mark.parametrize('text,wanted', [('整理近 2 天的',None),('做第 2 个',[2]),('1、３',[1,3]),('构想#12，要做：1、3',[1,3])])
def test_r8_numbers_are_only_explicit_item_choices(text,wanted):
    assert Intake._idea_reply_items(text)==wanted


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['好家伙','谢谢分享'])
async def test_r8_social_phrase_goes_to_jev_not_automatic_task(tmp_path,text):
    jev=_FakeJev({'verdict':('chat',1,1)})
    intake,store,spawned=_make(tmp_path,jev=jev)
    hit={'idea_id':_seed_idea_and_mention(store),'title':'想法'}
    await intake._handle_idea_mention_reply(intake._get_settings(),GID,'u','人','m',text,hit)
    assert not intake._approvals.created
    assert jev.calls


@pytest.mark.asyncio
async def test_r8_leading_mention_is_stripped_before_short_yes(tmp_path):
    jev=_FakeJev({'verdict':('chat',1,1)})
    intake,store,spawned=_make(tmp_path,jev=jev)
    hit={'idea_id':_seed_idea_and_mention(store),'title':'想法'}
    await intake._handle_idea_mention_reply(intake._get_settings(),GID,'u','人','m','@MaiBot 做第 2 个',hit)
    assert intake._approvals.created[0][1]['items']==[2]
    assert jev.calls==[]


@pytest.mark.asyncio
async def test_r8_unrelated_explicit_quote_never_answers_old_question(tmp_path):
    intake,store,spawned=_make(tmp_path)
    intake._waiting_tasks=lambda gid:[('T-old','q-old','20002')]
    answers=[]
    async def answer(*args): answers.append(args)
    intake._on_answer=answer
    msg=_reply_msg(text='是的',reply_to='different')
    intake._maybe_answer(GID,'20002','m','是的',msg['message'])
    await _drain(spawned)
    assert answers==[]
