from CharTyr_MaiWork.maiwork.admin_chat import AdminChat
from CharTyr_MaiWork.maiwork.goals import Goals
from test_admin_chat import _Svc, G1, G2, SECRET


def test_goal_context_is_current_and_bound_to_group(tmp_path):
    svc = _Svc(tmp_path)
    chat = AdminChat(svc)
    goals = Goals(svc.store, svc.get_settings)
    gid = goals.create_agent(G1, title="追踪版本发布", body="从实际发布页核实", criteria=["已核实来源"], by_text="管理员")
    cid = chat.create(group_id=G1)["id"]
    goals.set_admin_chat(gid, cid)
    row = chat._chat_or_raise(cid)
    prompt = chat._system_prompt(row)
    assert gid in prompt and "追踪版本发布" in prompt and "已核实来源" in prompt
    with svc.store.tx() as conn:
        conn.execute("UPDATE goals SET stale_reason=?, body=? WHERE id=?", ("缺少网页读取能力", SECRET, gid))
    prompt = chat._system_prompt(row)
    assert "缺少网页读取能力" in prompt
    assert SECRET not in prompt
    other = {**row, "group_id": G2}
    assert "追踪版本发布" not in chat._system_prompt(other)
    svc.store.close()
