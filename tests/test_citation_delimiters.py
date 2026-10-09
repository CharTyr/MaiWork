"""T-10: 来源清单的全角开括号不能成为 URL 的一部分。"""
import pytest
from CharTyr_MaiWork.maiwork.coordinator import extract_http_links, normalize_link_for_check


@pytest.mark.parametrize("left,right", [("（", "）"), ("【", "】"), ("《", "》"), ("「", "」"), ("『", "』"), ("“", "”"), ("‘", "’")])
def test_source_annotation_is_not_part_of_url(left, right):
    url = "https://ref.example/report.html"
    text = f"16. {url}{left}AFP 全文{right}"
    assert extract_http_links(text) == [url]


def test_annotated_and_plain_url_are_one_citation():
    url = "https://ref.example/report.html"
    assert extract_http_links(f"{url}（AFP）\n[{url}]({url})") == [url]


def test_chinese_path_and_encoded_parentheses_remain_intact():
    urls = ["https://ref.example/报道?q=核实&edition=zh", "https://ref.example/report%28v2%29.html"]
    assert extract_http_links("\n".join(urls)) == urls
    assert normalize_link_for_check(urls[0]) != normalize_link_for_check(urls[1])
