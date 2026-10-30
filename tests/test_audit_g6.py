"""G6 回归测试：共享工作区时群画像文件名带群号，不再互相覆盖。

- profile.py 的画像同步文件名 PROFILE.md → PROFILE-<群号>.md（每个群一份，
  共享工作区的多个群各自写各自的，子 agent 能看到所有这些画像——设计允许共享）；
- load_settings 检测到「多个服务群同一个 workspace」时，问题清单里提示一句
  「共享工作区的群会互相看到群画像」。
"""

from __future__ import annotations

from pathlib import Path

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.profile import Profiles
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"
G2 = "123456789"


class TestProfileMdFilename:
    def _profiles(self, tmp_path: Path, serve: list[dict]) -> tuple[Profiles, Store]:
        store = Store(tmp_path / "t.db")
        store.migrate()
        settings, _ = load_settings(
            {
                "groups": {"serve": serve},
                "environments": {"workspace_root": str(tmp_path / "ws")},
            }
        )
        ws = tmp_path / "ws" / "shared"
        ws.mkdir(parents=True)
        return Profiles(store, host=None, models=None, get_settings=lambda: settings), store

    def test_each_group_writes_its_own_file(self, tmp_path: Path) -> None:
        profiles, store = self._profiles(
            tmp_path, [{"group": f"qq:{G1}", "workspace": "shared"}, {"group": f"qq:{G2}", "workspace": "shared"}]
        )
        for gid, text in ((G1, "一群的画像"), (G2, "二群的画像")):
            with store.tx() as conn:
                conn.execute(
                    "INSERT INTO profile_entries (group_id, category, text, first_ts, last_ts, updated)"
                    " VALUES (?, 'interest', ?, 0, 0, 0)",
                    (gid, text),
                )
            profiles._write_profile_md(gid)
        folder = tmp_path / "ws" / "shared"
        f1 = folder / f"PROFILE-{G1}.md"
        f2 = folder / f"PROFILE-{G2}.md"
        assert f1.is_file() and "一群的画像" in f1.read_text(encoding="utf-8")
        assert f2.is_file() and "二群的画像" in f2.read_text(encoding="utf-8")
        # 不再写共享的 PROFILE.md（那会被互相覆盖）
        assert not (folder / "PROFILE.md").exists()


class TestSharedWorkspaceProblem:
    def test_shared_workspace_adds_problem(self) -> None:
        _, problems = load_settings(
            {
                "groups": {
                    "serve": [
                        {"group": f"qq:{G1}", "workspace": "shared"},
                        {"group": f"qq:{G2}", "workspace": "shared"},
                    ]
                }
            }
        )
        assert any("共享工作区" in p and "群画像" in p for p in problems)

    def test_distinct_workspaces_no_problem(self) -> None:
        _, problems = load_settings(
            {
                "groups": {
                    "serve": [
                        {"group": f"qq:{G1}", "workspace": "wa"},
                        {"group": f"qq:{G2}", "workspace": "wb"},
                    ]
                }
            }
        )
        assert not any("共享工作区" in p for p in problems)
