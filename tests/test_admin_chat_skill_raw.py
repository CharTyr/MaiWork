"""Explicit skill selection preserves the original document supplied by its author."""
import pytest
from CharTyr_MaiWork.maiwork.admin_chat import AdminChat
from test_admin_chat import _Svc


@pytest.mark.asyncio
async def test_selected_skill_keeps_full_frontmatter(tmp_path):
    svc = _Svc(tmp_path)
    skill_dir = svc.get_settings().data_dir / 'skills' / 'raw'
    skill_dir.mkdir(parents=True)
    text = '---\nname: raw\ndescription: example\nroles: worker\ncustom-field: retain-this-field\n---\n\nOriginal body\n'
    (skill_dir / 'SKILL.md').write_text(text)
    chat = AdminChat(svc)
    try:
        cid = chat.create()['id']
        await chat.send(cid, 'work', skill='raw')
        assert await chat.wait_idle(cid)
        assert text in svc.models.calls[-1][1][-1]['content']
    finally:
        await chat.close()
        svc.store.close()
