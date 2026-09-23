"""低敏事实的类别白名单与候选校验（S3-05）。

四条边界，全部来自需求 §4.3 与架构 §4.5，不是本模块自创的规则：

- **只认四种类别**。需求 §4.3 的"可保存"表逐项对应：本人希望的称呼、回复长短偏好、
  一般兴趣、非敏感活动偏好。类别值是 ASCII 标识（`address` / `reply_length` /
  `interest` / `activity`）；**面向群成员的展示名不在这里**，它逐字取自需求原文并放在
  ``retrieve.CATEGORY_LABELS``（S3-04 的群内列表与注入块共用那一份）。
- **内容必须是通过敏感过滤的短文本**。复用 ``context_buffer`` 的规则与长度上限：
  同一套"明显敏感"定义，不另立一份（否则两处会漂移）。
- **不猜**。需要推测、含糊或超出白名单的一律放弃（"宁可少记"，不以置信度代替授权）。
- **归属不在本模块**。成员归属由可信事件元数据构造的 ``MemberKey`` 决定，
  模型给出的任何身份字段都不被读取——因此"模型无权替换归属"是结构事实。

候选校验对**模型输出**一律不抛异常：不合格就返回 ``None``（放弃），而不是让一个畸形
字段把失败路径再炸一次。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ..context_buffer import MAX_TEXT_LENGTH, is_sensitive

MAX_SOURCE_LENGTH = MAX_TEXT_LENGTH
"""抽取源文本的长度上限（字符）。沿用缓冲区的 ``MAX_TEXT_LENGTH``（**建议参数待评审**），
不另立数字：两者面向的是同一类"成员在当前群说的一句话"。"""


class Category(StrEnum):
    """可保存的类别，逐项对应需求 §4.3 的"可保存"表。"""

    ADDRESS = "address"
    """本人希望的称呼。"""

    REPLY_LENGTH = "reply_length"
    """回复长短偏好。"""

    INTEREST = "interest"
    """一般兴趣。"""

    ACTIVITY = "activity"
    """非敏感活动偏好。"""


@dataclass(frozen=True)
class Candidate:
    """一条待写入的候选事实。``content`` 不进 ``repr``（与 ``MessageFacts.direct_text``、
    ``BufferEntry.text`` 同例）：正文只应在受控路径里被使用，不随对象一起被打印。"""

    category: Category
    content: str = field(repr=False)

    def to_pair(self) -> tuple[str, str]:
        """转成存储层写入使用的（类别值，内容）二元组。"""
        return (self.category.value, self.content)


def prepare_source(text: object) -> str | None:
    """把成员的原话整理成可送入抽取的源文本；不可用时返回 ``None``。

    源文本**只应来自本人当前这条 @ 消息的顶层文本**（不递归引用、不含缓冲与机器人
    输出）——那由调用方保证；本函数只做"太长、空、明显敏感"三道判定。
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped or len(stripped) > MAX_SOURCE_LENGTH:
        return None
    if is_sensitive(stripped):
        return None
    return stripped


def build_candidate(category: object, content: object) -> Candidate | None:
    """把一对（类别，内容）变成候选；任何不合格一律返回 ``None``。

    类别**逐字匹配**、不做大小写或近义词容错：容错会让白名单退化成模糊匹配，
    而"模型建议什么就存什么"正是要避免的。
    """
    if not isinstance(category, str) or not isinstance(content, str):
        return None
    try:
        parsed = Category(category.strip())
    except ValueError:
        return None
    text = content.strip()
    if not text or len(text) > MAX_TEXT_LENGTH:
        return None
    if is_sensitive(text):
        return None
    return Candidate(category=parsed, content=text)
