import pytest
from test_goal_fit_frontend import render, WORK, JS


@pytest.mark.parametrize('role', ['admin', 'group_admin'])
def test_review_card_visible_to_admin_escaped_and_not_empty(tmp_path, role):
    html = render(tmp_path, WORK, 'viewTasks', role=role, view={'tasks': {'review': [
        {'message_id': 'old', 'text': '<script>bad</script>', 'who': '<成员>', 'ts': 10,
         'why': '超过 48 小时仍未办成', 'attempts': 2}
    ]}})
    assert '待复核' in html and '暂无任务' not in html
    assert '&lt;script&gt;' in html and '<script>' not in html
    assert '转为任务请求' in html and '转为目标请求' in html and '忽略' in html
    assert 'data-act="ask-review"' in html and 'data-gid="g1"' in html
    assert '仍需审批' in html


def test_member_cannot_render_review_even_if_bad_server_payload(tmp_path):
    html = render(tmp_path, WORK, 'viewTasks', role='member', view={'tasks': {'review': [
        {'message_id': 'old', 'text': '私人原话', 'who': '成员', 'why': '失败'}
    ]}})
    assert '私人原话' not in html and '待复核' not in html


def test_review_load_failure_is_not_empty_state(tmp_path):
    html = render(tmp_path, WORK, 'viewTasks', role='admin', view={'tasks': {'review_error': '暂时无法读取'}})
    assert '暂时无法读取' in html and '暂无任务' not in html


def test_review_action_group_guard_and_confirmation():
    src = (JS / 'actions.js').read_text()
    assert 'case "ask-review"' in src
    segment = src.split('case "ask-review"')[1].split('case "req"')[0]
    assert 'state.g' in segment and 'el.dataset.gid' in segment
    assert 'confirm(' in segment and 'encodeURIComponent' in segment
    assert 'pending-asks' in segment
