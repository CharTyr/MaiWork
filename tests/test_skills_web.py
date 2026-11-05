"""Regression coverage for skill management alongside the new export API."""
import pytest

from CharTyr_MaiWork.maiwork import skills_web
from CharTyr_MaiWork.maiwork.store import Store


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / 'skills.db')
    db.migrate()
    yield db
    db.close()


def test_custom_skill_roundtrip_retains_roles_tools_and_source(tmp_path, store):
    created = skills_web.create(tmp_path, store, {
        'name': 'example', 'description': 'A useful skill', 'roles': ['main', 'worker'],
        'body': '# Original\n', 'allowed_tools': ['search_web'],
    })
    assert created['source'] == 'web'
    skills_web.update(tmp_path, store, 'example', {'description': 'Revised', 'body': '# Revised\n'})
    blob = skills_web.export_zip(tmp_path, store, 'example')
    skills_web.install_zip(store, tmp_path, blob, replace=True)
    restored = skills_web.get_view(tmp_path, store, 'example')
    assert restored['roles'] == ['main', 'worker']
    assert restored['allowed_tools'] == ['search_web']
    assert restored['description'] == 'Revised' and restored['body'] == '# Revised\n'
    assert restored['source'] == 'web'


def test_export_and_reinstall_preserves_toggle_state(tmp_path, store):
    skills_web.create(tmp_path, store, {'name': 'disabled', 'body': 'body'})
    skills_web.toggle(tmp_path, store, 'disabled', False)
    blob = skills_web.export_zip(tmp_path, store, 'disabled')
    skills_web.install_zip(store, tmp_path, blob, replace=True)
    assert skills_web.get_view(tmp_path, store, 'disabled')['enabled'] is False
    skills_web.toggle(tmp_path, store, 'disabled', True)
    assert skills_web.get_view(tmp_path, store, 'disabled')['enabled'] is True
