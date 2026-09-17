from dataclasses import dataclass, field
from enum import StrEnum

from .config import Settings, is_qq_id


class Classification(StrEnum):
    IGNORE = "ignore"
    EMPTY_OR_UNSUPPORTED = "empty_or_unsupported"
    TEXT_CANDIDATE = "text_candidate"


@dataclass(frozen=True)
class MessageFacts:
    platform_id: str
    self_id: str
    group_id: str
    sender_id: str
    is_group: bool
    mention_targets: tuple[str, ...] = ()
    direct_text: str = field(default="", repr=False)

    @property
    def member_key(self) -> tuple[str, str, str, str]:
        return self.platform_id, self.self_id, self.group_id, self.sender_id


def classify(message: MessageFacts, settings: Settings) -> Classification:
    """只分类本轮直接输入；候选不代表获准发送、采集或调用模型。"""
    if (
        not settings.platform_id
        or not settings.self_id
        or not settings.allowed_group_ids
        or not message.is_group
        or message.platform_id != settings.platform_id
        or message.self_id != settings.self_id
        or message.group_id not in settings.allowed_group_ids
        or not is_qq_id(message.sender_id)
        or message.sender_id == message.self_id
    ):
        return Classification.IGNORE
    if "all" in message.mention_targets:
        return Classification.IGNORE
    if message.self_id not in message.mention_targets:
        return Classification.IGNORE
    if not message.direct_text.strip():
        return Classification.EMPTY_OR_UNSUPPORTED
    return Classification.TEXT_CANDIDATE
