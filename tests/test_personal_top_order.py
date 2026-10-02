"""R06：个人向按分择优；返回值、reject 标记与真实写帖/落库保持一致。"""
from __future__ import annotations
import json
import pytest
from CharTyr_MaiWork.maiwork.personal import Personal

@pytest.mark.parametrize('scores,limit', [
    ([4.1,4.2,4.3,5.0,4.9],3),
    ([5.0,4.9,4.3,4.2,4.1],3),
    ([4.1,5.0,4.9,4.2],2),
    ([4.5,4.5,4.5,4.5],2),
    ([4.1,4.9],5),
    ([4.1,4.9],0),
])
def test_keep_top_marks_only_sorted_overflow(scores, limit):
    items = [{'title':str(i),'scores':{'avg':s}} for i,s in enumerate(scores)]
    expected = sorted(items, key=lambda it:-it['scores']['avg'])[:limit]
    kept = object.__new__(Personal)._keep_top(items, limit)
    assert [it['title'] for it in kept] == [it['title'] for it in expected]
    assert all('reject' not in it for it in kept)
    assert {it['title'] for it in items if 'reject' not in it} == {it['title'] for it in kept}


def test_keep_top_does_not_revive_previously_rejected_or_unscored():
    items = [
        {'title':'prior','scores':{'avg':5},'reject':('hard','不扎实')},
        {'title':'low','scores':{'avg':4.1}},
        {'title':'high','scores':{'avg':4.9}},
        {'title':'unscored'},
    ]
    kept = object.__new__(Personal)._keep_top(items,1)
    assert [it['title'] for it in kept] == ['high']
    assert items[0]['reject'] == ('hard','不扎实')
    assert 'reject' in items[1]
    assert 'reject' not in items[2]


def test_personal_public_flow_writes_and_inserts_highest_scored(tmp_path):
    from test_personal import (
        _make_personal, _time_patch, _run, _QUOTE, NOW, GID, UID,
        FakeModelsQueue, FakeWorkers, _ok_report,
    )
    titles = ['学习板资料甲','量化部署资料乙','硬件周报资料丙','系统镜像实测丁','传感器选购实测戊']
    candidates = {'items':[
        {'title':title,'url':f'https://site{i}.com/{i}','summary':f'{title}。资料完整。值得读。',
         'kind':'news','published':NOW-86400,'fetched':True,'quote':_QUOTE,'paywall':False}
        for i,title in enumerate(titles)
    ]}
    # 所有候选均过默认门槛，但最高分排在列表后面。
    scores = {'scores':[
        {'i':i,'title':titles[i],'info':5,'source':5,'relevance':5,'timeliness':5,
         'chat':score,'profile':0,'topic':f'T{i}','sensitive':False,'grounded':True,
         'junk':False,'same_as_recent':False,'why':'他在做','icon':'tools'}
        for i,score in enumerate([4.1,4.2,4.3,5.0,4.9])
    ]}
    posts = {'posts':[
        {'i':j,'title':titles[i],'body':'这份资料可以用来推进你的项目。',
         'reason':'和当前项目有关','refs':[],'audience':[],'keywords':['硬件']}
        for j,i in enumerate([2,3,4])
    ]}
    models = FakeModelsQueue(ready=True,replies=[
        json.dumps({'focus':[{'query':'FPGA','why':'在做'}],'idea':None},ensure_ascii=False),
        json.dumps(scores,ensure_ascii=False),json.dumps(posts,ensure_ascii=False),
    ])
    store,_,personal,models,*_ = _make_personal(tmp_path,models=models,workers=FakeWorkers(_ok_report(candidates)))
    with _time_patch():
        assert _run(personal.prepare_personal(GID,UID)) == 3
    rows = store.read().execute('SELECT title,rejected FROM news_items WHERE target_user_id=?',(UID,)).fetchall()
    assert {r['title'] for r in rows if not r['rejected']} == set(titles[2:])
    for _,messages,kwargs in models.calls:
        if kwargs.get('purpose') == 'personal.post':
            text = '\n'.join(str(m.get('content') or '') for m in messages)
            assert titles[0] not in text and titles[1] not in text
