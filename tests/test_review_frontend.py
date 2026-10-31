"""Execute real browser ES modules in Node; no source-string bug assertions."""
from pathlib import Path
import shutil
import subprocess
import pytest

RUNNER = Path(__file__).parent / "review_frontend" / "behavior.mjs"

@pytest.mark.parametrize("case", [
    "r3_detail", "r3_preview", "r3_urls", "r3_action", "r3_republish",
    "r11_member", "r11_admin", "r12_empty", "r12_partial", "r12_input",
    "r12_focus", "r12_go", "r12_hash", "r13_inert", "r13_back",
    "r13_reboot", "r13_preview_back", "r13_tab", "r13_motion", "r13_actions",
    "s3_republish_ui", "s4_composing_tab", "s4_desktop_preview_close",
])
def test_review_frontend_behavior(case):
    node = shutil.which("node")
    assert node, "Node is required for frontend behavior regression tests"
    result = subprocess.run(
        [node, "--experimental-vm-modules", str(RUNNER), case],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
async def test_member_topic_log_contains_only_opened_records(tmp_path):
    from types import SimpleNamespace
    from CharTyr_MaiWork.maiwork.console import views
    from unittest.mock import patch

    records = [{"id": 1, "opener": "已开启", "jev": {"ok_p": .8}, "verdict": "right"},
               {"id": 2, "opener": "", "jev": {"ok_p": .2, "detail": "诊断"}}]
    svc = SimpleNamespace(profiles=SimpleNamespace(entries=lambda gid: []),
                          get_settings=lambda: SimpleNamespace(workspace_of=lambda gid: "w"))
    from contextlib import ExitStack
    boundaries = {"_group_row": {}, "_base_summary": {"id": "1"}, "_today_counts": {},
                  "_group_quiet_hours": [], "_profile_sections": [], "_goals_view": {},
                  "_tasks_view": {}, "_upcoming": [], "_agent_fish": {},
                  "_m2_lists": ([], [], [], records, [], [])}
    with ExitStack() as stack:
        for name, value in boundaries.items():
            stack.enter_context(patch.object(views, name, return_value=value))
        stack.enter_context(patch.object(views, "_attach_idea_targets"))
        result = views.group_view(svc, "1", admin=False)
    assert [r["id"] for r in result["topic_log"]] == [1]
    assert "verdict" not in result["topic_log"][0]
