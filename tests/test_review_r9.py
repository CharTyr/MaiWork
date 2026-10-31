import json
import pytest
from CharTyr_MaiWork.maiwork.host import Msg
from CharTyr_MaiWork.maiwork import channel_dedup
from test_topics import _make_topics, _followup_log, _seed_candidate, _QUIET_CFG, GID, SID, NOW
from fakes import FakeHost


@pytest.mark.asyncio
async def test_r9_empty_opener_id_never_matches_empty_reply(tmp_path):
    host = FakeHost(msgs=[Msg(id='m',ts=NOW+30,user_id='u',user_name='u',text='中午吃什么',is_bot=False,is_at=False,is_picture=False,reply_to='')], session_id=SID)
    store, settings, topics, *_ = _make_topics(tmp_path, host=host, cfg=_QUIET_CFG)
    tid=_followup_log(store, ts=NOW, opener_mid='')
    await topics.follow_up(GID, NOW+700)
    result=json.loads(store.read().execute('SELECT result FROM topic_log WHERE id=?',(tid,)).fetchone()['result'])
    assert result['replies']==1
    assert result['on_topic']==0


def test_r9_keywords_preferred_and_generic_title_not_topics(tmp_path):
    store, settings, topics, *_ = _make_topics(tmp_path)
    from test_news_feedback import _item
    iid = _item(store, gid=GID, keywords=['steamdeck'])
    cid=_seed_candidate(store, check_now=NOW, title='一个可以推荐的新功能', kind='news', ref_id=iid)
    tid=_followup_log(store, ts=NOW)
    with store.tx() as c:
        c.execute('UPDATE topic_log SET candidate_id=? WHERE id=?',(cid,tid))
    words=topics._topic_words(tid)
    assert 'steamdeck' in words
    assert not topics._hits_topic('一个可以做的东西',words)


@pytest.mark.parametrize('channel,fn,payload,ref', [
    ('news_card', channel_dedup.card_used_item_ids, {'item_ids':[77]},77),
    ('topic', channel_dedup.topic_used_news_ids, {'candidate_kind':'news','candidate_ref_id':88},88),
])
def test_r9_sending_outbox_is_reserved(tmp_path,channel,fn,payload,ref):
    store, *_ = _make_topics(tmp_path)
    with store.tx() as c:
        c.execute('INSERT INTO outbox (key,group_id,kind,payload,status,created,updated) VALUES (?,?,\'text\',?,\'sending\',?,?)',(channel+':999',GID,json.dumps(payload),NOW,NOW))
    assert ref in fn(store,GID,NOW)


def test_r9_dedup_uses_real_send_time(tmp_path):
    store, settings, topics, *_ = _make_topics(tmp_path)
    cid=_seed_candidate(store, check_now=NOW-13*3600,title='开源掌机',kind='news',ref_id=77)
    tid=_followup_log(store, ts=NOW-13*3600, due_ts=NOW-3600+600)
    with store.tx() as c: c.execute('UPDATE topic_log SET candidate_id=? WHERE id=?',(cid,tid))
    assert 77 in channel_dedup.topic_used_news_ids(store,GID,NOW)
