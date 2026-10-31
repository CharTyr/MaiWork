import pytest
from test_delivery import _make_mentions, _seed_group, _kwargs_last_message, _injected_text, GID


@pytest.mark.parametrize('tail', ['@MaiBot 帮我做个表', '[图片] 看看这个', '[回复了卡片] 这个是什么意思'])
def test_r7_latest_unclosed_message_is_target_speaker(tmp_path, tail):
    store, settings, m = _make_mentions(tmp_path)
    _seed_group(store)
    m.add_ephemeral(GID, '本轮卡片备忘', key='card-reply:m2', ttl_s=90, turns=2, target_user_id='u2')
    m.note_msg_user(GID, 'm1', 'u1')
    m.note_msg_user(GID, 'm2', 'u2')
    kw = _kwargs_last_message(msg_id='m1', text='历史发言')
    kw['items'].append({'item_type':'UserMessageItem', 'parts': [
        {'type':'text', 'text': '<message msg_id="m2" is_self_message="false">'+tail},
        {'type':'at', 'target':'bot'},
    ]})
    assert '本轮卡片备忘' in _injected_text(m.inject(kw))


@pytest.mark.parametrize('recent,visible', [('看看T-12做好没', True), ('看看T-120做好没', False), ('at-12完成', False)])
def test_r7_chinese_task_number_boundary(tmp_path, recent, visible):
    store, settings, m = _make_mentions(tmp_path)
    _seed_group(store)
    m.add(GID, '你的T-12已有进展', key='request:t', ttl_s=3600, turns=3, target_user_id='owner')
    kw = _kwargs_last_message(msg_id='m', text=recent)
    m.note_msg_user(GID, 'm', 'other')
    assert ('你的T-12已有进展' in _injected_text(m.inject(kw))) is visible
