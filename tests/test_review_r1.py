import pytest
from CharTyr_MaiWork.maiwork.host import Host, _image_segments
from fakes import FakeCtx
from test_host import _maibot_db


@pytest.mark.asyncio
async def test_r1_desc_follows_id_after_sort(tmp_path):
    db = tmp_path / 'MaiBot.db'
    _maibot_db(db, [('a', 'IMAGE', '猫', 1), ('b', 'IMAGE', '狗', 1)])
    raw = [dict(message_id=mid, timestamp=ts, is_picture=True,
                raw_message=[{'type': 'image', 'hash': h}])
           for mid, ts, h in [('new', 2, 'b'), ('old', 1, 'a')]]
    msgs = await Host(FakeCtx({'message.get_by_time_in_chat': raw}), maibot_db_path=db).messages('s', 0, 3, 100)
    assert [(m.id, m.text) for m in msgs] == [('old', '[图片：猫]'), ('new', '[图片：狗]')]


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['[表情包: 无语]', '[图片：猫]', '[动画表情: 笑]'])
async def test_r1_existing_description_not_appended(tmp_path, text):
    db = tmp_path / 'MaiBot.db'
    _maibot_db(db, [('a', 'EMOJI', '无语', 0)])
    raw = [dict(message_id='1', timestamp=1, is_picture=True, processed_plain_text=text,
                raw_message=[{'type': 'emoji', 'hash': 'a'}])]
    msgs = await Host(FakeCtx({'message.get_by_time_in_chat': raw}), maibot_db_path=db).messages('s', 0, 3, 100)
    assert msgs[0].text == text


def test_r1_nested_segments_and_top_level_hash():
    assert _image_segments([{'type': 'seglist', 'data': [
        {'type': 'image', 'data': {}, 'hash': 'a'},
        {'type': 'seglist', 'data': [{'type': 'emoji', 'data': {'hash': 'b'}}]},
    ]}]) == [('a', False), ('b', True)]


@pytest.mark.asyncio
async def test_r1_repeated_hash_after_batch_limit_still_backfills(tmp_path):
    db = tmp_path / 'MaiBot.db'
    _maibot_db(db, [(str(i), 'IMAGE', str(i), 1) for i in range(100)])
    raw = [dict(message_id=str(i), timestamp=i, is_picture=True,
                raw_message=[{'type': 'image', 'hash': str(i % 100)}]) for i in range(102)]
    msgs = await Host(FakeCtx({'message.get_by_time_in_chat': raw}), maibot_db_path=db).messages('s', 0, 200, 200)
    assert msgs[100].text == '[图片：0]'
    assert msgs[101].text == '[图片：1]'
