"""names.py 测试：QQ 群名乱码清洗（线上实测样例来自 QQ 特殊标记错误解码）。

规则（只针对群名进视图/库的地方，一处实现到处复用）：
- 去掉 Unicode 控制字符（Cc / Cf，保留正常 emoji 和 ⚠️ 这种符号）；
- 去掉 <$…> 这种「长度 ≤8 且内部含控制字符或 ÿ/Ā 这类乱码」的片段；
- 合并空白、strip；清洗后为空 → 「群 <群号>」。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.names import clean_group_name
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"


class TestCleanGroupName:
    def test_live_sample(self):
        """线上实测：<$�\x0e>FUCK超级地球（尼尼孩孩Major冠军⚠️）<$�\x0e> → 中间正常部分。"""
        raw = "<$ÿĀD\x0e>FUCK超级地球（尼尼孩孩Major冠军⚠️）<$ÿĀD\x0e>"
        out = clean_group_name(raw, G1)
        assert out == "FUCK超级地球（尼尼孩孩Major冠军⚠️）"

    def test_empty_and_garbage_only_falls_back(self):
        assert clean_group_name("", G1) == f"群 {G1}"
        assert clean_group_name("   ", G1) == f"群 {G1}"
        assert clean_group_name("<$ÿĀD\x0e>", G1) == f"群 {G1}"
        assert clean_group_name("\x00\x01\x02", G1) == f"群 {G1}"
        assert clean_group_name(None, G1) == f"群 {G1}"

    def test_control_chars_removed_emoji_kept(self):
        # 零宽空格（Cf）和软连接符要没，🚀 和中文要留下
        out = clean_group_name("技术​交流🚀群­", G1)
        assert out == "技术交流🚀群"
        assert "​" not in out
        assert "­" not in out

    def test_normal_angle_brackets_kept(self):
        """<折腾> 这种正常尖括号不含乱码/控制符 → 留下。"""
        out = clean_group_name("<折腾>研究所", G1)
        assert out == "<折腾>研究所"

    def test_dollar_segment_without_garbage_kept(self):
        # <$ABC> 长度 6 ≤8 但没有控制符/乱码 → 留下
        out = clean_group_name("<$ABC>俱乐部", G1)
        assert out == "<$ABC>俱乐部"

    def test_long_dollar_segment_kept(self):
        # 长度 >8 的 <$...> 不算那个 QQ 特殊标记，留下（只去掉短而脏的）
        out = clean_group_name("<$0123456789>长标记", G1)
        assert "<$0123456789>" in out

    def test_whitespace_collapsed(self):
        out = clean_group_name("  折腾  研究\t 所  \n", G1)
        assert out == "折腾 研究 所"

    def test_group_id_default(self):
        # 群号缺省时回通用名
        assert clean_group_name("\x00", "") == "群"

    def test_chunk_with_control_short_removed(self):
        # <$ab\x0c> 含控制字符且 ≤8：整个片段去掉
        out = clean_group_name("前<$ab\x0c>后", G1)
        assert out == "前后"


# ----------------------------------------------------------------------
# 进库就洗（profile._refresh_group_info 一次，视图直接用库里干净值）
# ----------------------------------------------------------------------


class TestStoreWash:
    @pytest.mark.asyncio
    async def test_refresh_group_info_washes(self, tmp_path: Path):
        """host 给的群名带乱码：写进 groups 表的必须是清洗后的。"""
        from fakes import FakeHost

        from CharTyr_MaiWork.maiwork import clock
        from CharTyr_MaiWork.maiwork.profile import Profiles

        store = Store(tmp_path / "t.db")
        store.migrate()
        with store.tx() as conn:
            conn.execute("INSERT INTO groups (group_id, name) VALUES (?, '')", (G1,))

        class _Models:
            def settings(self):
                class _S:
                    def ready(self) -> bool:
                        return False

                return _S()

        raw_name = "<$ÿĀD\x0e>FUCK超级地球（尼尼孩孩Major冠军⚠️）<$ÿĀD\x0e>"
        host = FakeHost(info={"group_name": raw_name, "member_count": 100})
        profiles = Profiles(store, host, _Models(), lambda: None)
        await profiles._refresh_group_info(G1, clock.now())
        row = store.read().execute("SELECT name FROM groups WHERE group_id=?", (G1,)).fetchone()
        assert row["name"] == "FUCK超级地球（尼尼孩孩Major冠军⚠️）"
        store.close()
