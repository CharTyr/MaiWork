"""config_file.py：config.toml 的「定位、读-改-写、备份、权限」测试。

- 用临时插件目录 + 临时 config.toml（注释保留靠 tomlkit）；
- 类型映射：groups.serve → [[groups.serve]]、environments.ssh → [[environments.ssh]]、
  console.listen → "IP:端口" 字符串、列表 → 数组、其余标量；
- reset = 删键回默认；
- 备份写到数据目录 config-backups/，最多留 10 份，0600；
- config.toml 写完保持 0600；
- 写失败不留半截（文件内容不变）；
- 不在插件目录留下任何新文件（目录清单前后一致，除 config.toml 内容本身）。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from CharTyr_MaiWork import config_file


def _plugin_dir(tmp_path: Path, *, with_config: bool = True) -> Path:
    d = tmp_path / "plug"
    d.mkdir(parents=True, exist_ok=True)
    if with_config:
        (d / "config.toml").write_text(
            "# 顶部注释：不许丢\n"
            "[plugin]\n"
            "enabled = true  # 行内注释：不许丢\n"
            "\n"
            "[topics]\n"
            "# per_day 的注释\n"
            "per_day = 2\n"
            "\n"
            "[groups]\n"
            "# [[groups.serve]]\n"
            "# group = \"qq:1\"\n"
            "\n"
            "[console]\n"
            "listen = \"127.0.0.1:18650\"\n",
            encoding="utf-8",
        )
    return d


class TestLocate:
    def test_find_config(self, tmp_path: Path) -> None:
        d = _plugin_dir(tmp_path)
        assert config_file.config_path(d) == d / "config.toml"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        d = _plugin_dir(tmp_path, with_config=False)
        with pytest.raises(config_file.ConfigFileError, match="找不到"):
            config_file.read_text(d)


class TestWrite:
    def test_write_scalar_preserves_comments(self, tmp_path: Path) -> None:
        d = _plugin_dir(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        new_text = config_file.write_fields(
            d, data_dir, {"topics.per_day": 5, "topics.enabled": False, "delivery.quiet_hours": "22:00-07:00"}
        )
        assert "# 顶部注释：不许丢" in new_text
        assert "enabled = true  # 行内注释：不许丢" in new_text
        assert "# per_day 的注释" in new_text
        assert "per_day = 5" in new_text
        assert "enabled = false" in new_text
        assert 'quiet_hours = "22:00-07:00"' in new_text
        # 文件真的写了
        on_disk = (d / "config.toml").read_text(encoding="utf-8")
        assert on_disk == new_text

    def test_serve_groups_array_of_tables(self, tmp_path: Path) -> None:
        d = _plugin_dir(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        text = config_file.write_fields(
            d,
            data_dir,
            {"groups.serve": [
                {"group": "qq:900000001", "workspace": "tinker"},
                {"group": "qq:10086", "workspace": ""},
            ]},
        )
        assert "[[groups.serve]]" in text
        assert 'group = "qq:900000001"' in text
        assert 'workspace = "tinker"' in text
        assert 'group = "qq:10086"' in text
        # 空 workspace 不写这一行
        assert text.count("workspace =") == 1
        # 可被解析回同一结构
        import tomlkit

        parsed = tomlkit.parse(text)
        serve = parsed["groups"]["serve"]
        assert len(serve) == 2
        assert serve[1]["group"] == "qq:10086"
        assert "workspace" not in serve[1]

    def test_ssh_list_array_of_tables(self, tmp_path: Path) -> None:
        d = _plugin_dir(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        text = config_file.write_fields(
            d,
            data_dir,
            {"environments.ssh": [{"name": "vps1", "host": "maiwork@1.2.3.4:22", "note": "小机器"}]},
        )
        assert "[[environments.ssh]]" in text
        assert 'host = "maiwork@1.2.3.4:22"' in text
        import tomlkit

        parsed = tomlkit.parse(text)
        assert parsed["environments"]["ssh"][0]["note"] == "小机器"

    def test_lists_become_arrays(self, tmp_path: Path) -> None:
        d = _plugin_dir(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        text = config_file.write_fields(
            d, data_dir, {"feeds.news_slots": ["08:30", "20:00"], "approval.admins": ["qq:10001"]}
        )
        import tomlkit

        parsed = tomlkit.parse(text)
        assert list(parsed["feeds"]["news_slots"]) == ["08:30", "20:00"]
        assert list(parsed["approval"]["admins"]) == ["qq:10001"]

    def test_reset_deletes_key(self, tmp_path: Path) -> None:
        d = _plugin_dir(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        text = config_file.write_fields(d, data_dir, {"topics.per_day": 7})
        assert "per_day = 7" in text
        text2 = config_file.delete_fields(d, data_dir, ["topics.per_day"])
        assert "per_day = " not in text2
        # 注释留着
        assert "# per_day 的注释" in text2

    def test_mode_0600(self, tmp_path: Path) -> None:
        d = _plugin_dir(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        config_file.write_fields(d, data_dir, {"topics.per_day": 5})
        mode = stat.S_IMODE(os.stat(d / "config.toml").st_mode)
        assert mode == 0o600

    def test_backup_written_and_pruned(self, tmp_path: Path) -> None:
        d = _plugin_dir(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        for i in range(12):
            config_file.write_fields(d, data_dir, {"topics.per_day": i})
        backups = sorted((data_dir / "config-backups").glob("config.toml-*"))
        assert len(backups) == 10
        for b in backups:
            assert stat.S_IMODE(os.stat(b).st_mode) == 0o600

    def test_no_temp_files_in_plugin_dir(self, tmp_path: Path) -> None:
        d = _plugin_dir(tmp_path)
        before = sorted(p.name for p in d.iterdir())
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        config_file.write_fields(d, data_dir, {"topics.per_day": 9})
        config_file.delete_fields(d, data_dir, ["topics.per_day"])
        after = sorted(p.name for p in d.iterdir())
        assert before == after

    def test_reread_before_write(self, tmp_path: Path) -> None:
        """宿主可能在两次写之间改文件：每次写都重新读最新内容，不丢别人的改动。"""
        d = _plugin_dir(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        config_file.write_fields(d, data_dir, {"topics.per_day": 5})
        # 模拟宿主/用户手工改了文件
        p = d / "config.toml"
        p.write_text(p.read_text(encoding="utf-8").replace("per_day = 5", "per_day = 6"), encoding="utf-8")
        text = config_file.write_fields(d, data_dir, {"topics.min_gap_hours": 4})
        assert "per_day = 6" in text
        assert "min_gap_hours = 4" in text

    def test_write_failure_leaves_file_intact(self, tmp_path: Path) -> None:
        """备份目录建不了 → 整个失败，config.toml 内容不变。"""
        d = _plugin_dir(tmp_path)
        # data_dir 是个「文件」→ 建 config-backups 目录必然失败
        blocker = tmp_path / "data"
        blocker.write_text("我不是目录", encoding="utf-8")
        original = (d / "config.toml").read_text(encoding="utf-8")
        with pytest.raises(config_file.ConfigFileError):
            config_file.write_fields(d, blocker, {"topics.per_day": 5})
        assert (d / "config.toml").read_text(encoding="utf-8") == original

    def test_new_section_created_when_missing(self, tmp_path: Path) -> None:
        d = _plugin_dir(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        text = config_file.write_fields(d, data_dir, {"usage.alert_daily_tokens": 12345})
        import tomlkit

        parsed = tomlkit.parse(text)
        assert parsed["usage"]["alert_daily_tokens"] == 12345
