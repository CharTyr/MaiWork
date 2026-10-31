import json
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from test_pending_ask_recovery import store, now, _Graph, _msg_dict, _settings, GID, T0
from fakes import FakeHost, FakeModelsQueue, FakeProfiles


def bj(day): return datetime(2026, 9, day, 1, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize('day,delay,nextday', [(21, 3, 22), (24, 3, 25), (25, 3, 28), (21, 4*86400+3, 28)])
def test_r5_weekday_next_is_first_future_workday(day, delay, nextday):
    assert Goals._next_remind('weekdays', bj(day), bj(day)+delay) == bj(nextday)


def test_r5_repeat_never_asks_progress_or_consumes_renew(store, now):
    goals = Goals(store, _settings)
    gid = goals.create_member(GID, who_id='u', who_name='阿明', title='喝水', due_ts=T0, remind_ts=T0, repeat='daily')
    goals.mark(gid, 'remind', T0+3)
    assert not any(e['type'] == 'ask_progress' for e in goals.due(T0+60))
    row = goals.get(gid)
    assert row['last_ts'] is None


@pytest.mark.asyncio
async def test_r5_slow_classification_keeps_repeat_without_due(store, now):
    models = FakeModelsQueue(replies=[json.dumps({'asks': [{'i':1, 'kind':'reminder', 'title':'喝水', 'when':'2026-09-27 09:00', 'repeat':'weekdays'}]})])
    g = _Graph(store, FakeHost([]), models)
    g.asks.record_pending(GID, _msg_dict('r5', T0-7200), 'slow')
    assert await g.profiles.recover_pending_asks(GID) == 1
    row = dict(store.read().execute('SELECT * FROM goals').fetchone())
    assert row['repeat'] == 'weekdays'
    assert row['due_ts'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('uid,expected', [('u', '提醒：喝水'), ('', '@阿明 提醒：喝水')])
async def test_r5_payload_has_one_mention(store, now, uid, expected):
    goals = Goals(store, _settings)
    goals.create_member(GID, who_id=uid, who_name='阿明', title='喝水', remind_ts=T0, due_ts=T0)
    sent=[]
    app = SimpleNamespace(goals=goals, store=store, _settings=_settings(), coordinator=None,
                          outbox=SimpleNamespace(enqueue=lambda *a, **kw: sent.append(a[3])), _models_ready=lambda: False)
    await MaiWorkApp._goals_round(app, T0+3)
    assert sent[0]['text'] == expected
    if uid: assert sent[0]['at_user'] == uid


@pytest.mark.parametrize('legacy', [False, True])
def test_r5_workdays_alias_lands_as_weekdays(store, now, legacy):
    g = _Graph(store, FakeHost([]), FakeModelsQueue(replies=[]))
    g.asks.record_pending(GID, _msg_dict('alias'), 'slow')
    assert g.asks.decide(GID, 'alias', {'kind':'reminder', 'title':'喝水', 'remind_ts':T0+60, 'repeat':'workdays'})
    if legacy:
        with store.tx() as c:
            c.execute('UPDATE pending_asks SET decision=? WHERE message_id=?', (json.dumps({'kind':'reminder', 'title':'喝水', 'remind_ts':T0+60, 'repeat':'workdays'}), 'alias'))
    assert g.profiles.apply_pending_ask(GID, 'alias')
    row = dict(store.read().execute('SELECT * FROM goals').fetchone())
    assert row['repeat'] == 'weekdays'
    assert row['due_ts'] is None


def test_fake_profiles_group_space_wiring():
    p=FakeProfiles()
    marker=object()
    p.set_group_space(marker)
    assert p.group_space is marker
