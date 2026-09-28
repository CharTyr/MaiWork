"""关注点返回格式容错：有的模型不按 {"focus": [...]} 包，直接回一个关注点对象、
一个列表、或换了键名（2026-09-28 线上 step-5-preview 实测回了单个对象）。"""

from __future__ import annotations

import pytest

from CharTyr_MaiWork.maiwork.feeds import focus_items


def test_standard_shape() -> None:
    data = {"focus": [{"query": "a", "why": "x"}, {"query": "b"}], "diverse": None}
    assert [f["query"] for f in focus_items(data)] == ["a", "b"]


def test_single_bare_object_seen_on_prod() -> None:
    data = {"query": "Splatoon Raiders 刷取攻略", "why": "猛刷期"}
    items = focus_items(data)
    assert items == [{"query": "Splatoon Raiders 刷取攻略", "why": "猛刷期"}]


def test_bare_list() -> None:
    assert [f["query"] for f in focus_items([{"query": "a"}, {"query": "b"}])] == ["a", "b"]


def test_focus_is_single_object() -> None:
    assert [f["query"] for f in focus_items({"focus": {"query": "a"}})] == ["a"]


@pytest.mark.parametrize("key", ["focuses", "items", "queries", "关注点"])
def test_other_key_names(key: str) -> None:
    assert [f["query"] for f in focus_items({key: [{"query": "a"}]})] == ["a"]


def test_plain_strings_become_queries() -> None:
    assert [f["query"] for f in focus_items({"focus": ["a", " ", "b"]})] == ["a", "b"]


def test_diverse_alone_is_not_focus() -> None:
    assert focus_items({"diverse": {"query": "反方"}}) == []


def test_garbage() -> None:
    assert focus_items(None) == []
    assert focus_items("text") == []
    assert focus_items({"foo": 1}) == []
