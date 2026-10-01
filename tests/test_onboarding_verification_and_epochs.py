"""R01/R07：明确失败不能称可用，引导轮次与终态不被旧请求覆盖。"""
from __future__ import annotations
from types import SimpleNamespace
import pytest
from CharTyr_MaiWork.maiwork import onboarding
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.store import Store

class ModelView:
    def __init__(self): self.records = {}
    def settings(self):
        return SimpleNamespace(ready=lambda:True,main='same-model',main_label='主模型',worker='worker-model',worker_label='任务模型')
    def verification_for(self, kind='main'):
        return self.records.get(kind)

@pytest.fixture
def svc(tmp_path):
    settings, problems = load_settings({'plugin':{'enabled':True},'storage':{'data_dir':str(tmp_path)},
                                      'groups':{'serve':[{'group':'qq:123456789'}]},'approval':{'admins':[]}})
    assert not problems
    store = Store(tmp_path/'db.sqlite3'); store.migrate()
    obj = SimpleNamespace(store=store,models=ModelView(),get_settings=lambda:settings,search=None)
    yield obj
    store.close()

@pytest.mark.parametrize('kind', ['main','task'])
def test_matching_known_failure_is_not_usable_or_done(svc,kind):
    svc.models.records[kind] = {'ok':False,'tools_ok':False,'error':'验证返回401'}
    info = onboarding.view(svc)
    assert info['checks']['models'] is False
    assert info['usable'] is False and info['missing'] == ['models']
    item = next(x for x in info['items'] if x['key']=='models')
    assert item['state'] == 'off' and '401' in item['text']
    done = onboarding.act(svc,'done')
    assert done['state'] == 'later' and done['usable'] is False


def test_unknown_old_install_and_optional_tool_warning_still_allowed(svc):
    info = onboarding.view(svc)
    assert info['usable'] is True and info['show'] is False
    svc.models.records['main'] = {'ok':True,'tools_ok':False,'note':'工具未走通'}
    info = onboarding.act(svc,'done')
    assert info['usable'] is True and info['state'] == 'done'
    assert next(x for x in info['items'] if x['key']=='models')['state'] == 'warn'


def test_name_only_old_record_never_claims_verified(svc):
    with svc.store.tx() as conn:
        svc.store.kv_set(conn,'models.verified.old',{'model':'same-model','ok':True,'tools_ok':True})
    item = next(x for x in onboarding.view(svc)['items'] if x['key']=='models')
    assert item['state'] == 'warn' and ('未验证' in item['text'] or '没验证' in item['text'])

@pytest.mark.parametrize('finish,expected', [('done','done'),('skip','skipped')])
def test_late_progress_cannot_overwrite_terminal(svc,finish,expected):
    run = onboarding.act(svc,'start',step='models')
    ended = onboarding.act(svc,finish,run_id=run['run_id'],sequence=2)
    assert ended['state'] == expected
    late = onboarding.act(svc,'progress',step='done',run_id=run['run_id'],sequence=1)
    assert late['state'] == expected and late['show'] is False
    assert late['ts'] == ended['ts']


def test_saved_later_stays_closed_until_explicit_start(svc):
    svc.models.records['main'] = {'ok':False,'error':'401'}
    run = onboarding.act(svc,'start',step='models')
    ended = onboarding.act(svc,'done',run_id=run['run_id'],sequence=2)
    assert ended['state'] == 'later'
    late = onboarding.act(svc,'progress',step='groups',run_id=run['run_id'],sequence=1)
    assert late['state'] == 'later' and late['show'] is False
    restarted = onboarding.act(svc,'start',step='models')
    assert restarted['state'] == 'in_progress' and restarted['show'] is True
    assert restarted['run_id'] != run['run_id']


def test_new_round_rejects_old_progress_and_completion(svc):
    old = onboarding.act(svc,'start',step='models')
    new = onboarding.act(svc,'start',step='groups')
    for action in ('progress','done','skip'):
        result = onboarding.act(svc,action,step='models',run_id=old['run_id'],sequence=99)
        assert result['run_id'] == new['run_id']
        assert result['state'] == 'in_progress' and result['step'] == 'groups'


def test_out_of_order_progress_does_not_move_step_back(svc):
    run = onboarding.act(svc,'start',step='models')
    newer = onboarding.act(svc,'progress',step='search',run_id=run['run_id'],sequence=2)
    older = onboarding.act(svc,'progress',step='groups',run_id=run['run_id'],sequence=1)
    assert older['step'] == newer['step'] == 'search'
    assert older['sequence'] == 2


def test_legacy_progress_cannot_reopen_legacy_terminal(svc):
    ended = onboarding.act(svc,'done')
    late = onboarding.act(svc,'progress',step='models')
    assert late['state'] == ended['state'] == 'done'
    assert late['show'] is False


def test_missing_run_id_cannot_mutate_new_round(svc):
    run = onboarding.act(svc,'start',step='groups')
    unchanged = onboarding.act(svc,'progress',step='models')
    assert unchanged['run_id'] == run['run_id'] and unchanged['step'] == 'groups'
