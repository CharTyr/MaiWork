"""MaiBot planner 载荷里一条消息可能拆成好几个 text part（线上 2026-10-02 实录）：

- 带图 / 表情的消息：第一个 part 只有 `<message …>\\n` 前缀，正文在后面的 part；
- @ 人的消息：第一个 part 是「(@了你)」，真正的话在后面几个 part。
以前只看带前缀的那个 part，线上两天 7007 条群友消息里有 1712 条正文全被丢掉。
修：同一个 item 里的 text part 拼起来算一条；表情包标签不参与匹配。
"""

from __future__ import annotations

from CharTyr_MaiWork.maiwork.delivery import TopicMatcher
from CharTyr_MaiWork.maiwork import chat_feed


def _item(*texts: str, self_msg: bool = False) -> dict:
    attrs = 'msg_id="1" time="22:59:14" user="甲"' + (' is_self_message="true"' if self_msg else "")
    parts = [{"type": "text", "text": f"<message {attrs}>\n" + texts[0]}]
    parts += [{"type": "text", "text": t} for t in texts[1:]]
    return {"item_type": "UserMessageItem", "meta": {}, "parts": parts}


def _bodies(*items: dict) -> tuple[list[str], list[str]]:
    tm = TopicMatcher.__new__(TopicMatcher)
    return tm.chat_bodies({"items": list(items)})


def test_text_in_later_parts_is_kept() -> None:
    users, _ = _bodies(_item("", "[表情包: 无语]", "暗潮老兵这版本太强了"))
    assert len(users) == 1 and "暗潮老兵这版本太强了" in users[0]


def test_at_message_keeps_the_words() -> None:
    users, _ = _bodies(_item("(@了你)", "@某人", "皇牌空战8 联机好玩吗"))
    assert "皇牌空战8 联机好玩吗" in users[0]


def test_self_flag_follows_the_item() -> None:
    users, selfs = _bodies(_item("", "我也想玩", self_msg=True), _item("好"))
    assert selfs == ["我也想玩"] and users == ["好"]


def test_single_part_unchanged() -> None:
    users, _ = _bodies(_item("就一句话"))
    assert users == ["就一句话"]


def test_emoji_tags_do_not_match() -> None:
    assert "无语" not in chat_feed.clean("[表情包: 无语,震惊] 真的吗")
    assert "真的吗" in chat_feed.clean("[表情包: 无语,震惊] 真的吗")


def test_image_descriptions_do_not_match() -> None:
    t = chat_feed.clean("[图片：Switch 主界面截图，星之卡比 探索发现] 早知道下午就撕了")
    assert "switch" not in t and "探索" not in t and "早知道下午就撕了" in t
    # 截断的长描述（没有右括号）也抹掉
    assert "超级地球" not in chat_feed.clean("[图片：新闻聚合页面截图，超级地球……")
