"""纯触发分类：只回答"这次事件是否构成一次有效的 @ 交互"。

**本模块刻意不判断以下三件事**（2026-09-17 的分工决定，见 docs/03 §5.2）：

- **采集资格**（是否可进群缓冲）：那是采集规则，依赖告知、群开关与成员退出，
  属 S2；需求 §4.1 的原文也是"符合采集规则时只进短期缓冲"。
- **指令与聊天的分流**：判断一句话是不是控制指令需要指令语法知识，归
  ``commands.py``（S1-05）。放在这里会让两个模块各持一份指令表。
- **权限、发送与模型调用**：这些一律由可信事件元数据与确定性代码决定。

分类结果**只表示分流去向，不表示获准发送、采集或调用模型**。
"""

from dataclasses import dataclass, field
from enum import StrEnum

from .config import Settings, is_qq_id


class Classification(StrEnum):
    IGNORE = "ignore"
    """完全忽略：不聊天、不采集、不发提示。"""

    EMPTY_OR_UNSUPPORTED = "empty_or_unsupported"
    """有效 @ 但没有可用文本（空 @ 或仅空白）：忽略，且不进入等待续聊模式。"""

    UNSUPPORTED_ATTACHMENT = "unsupported_attachment"
    """有效 @ 但只有附件：可回复固定的文本能力提示，不解析附件、不请求模型。"""

    TEXT_CANDIDATE = "text_candidate"
    """有效 @ 且带非空直接文本：交由上游按指令/聊天分流。"""


@dataclass(frozen=True)
class MessageFacts:
    """一次事件的触发相关事实，全部来自可信事件元数据与顶层消息组件。"""

    platform_id: str
    self_id: str
    group_id: str
    sender_id: str
    is_group: bool
    mention_targets: tuple[str, ...] = ()
    direct_text: str = field(default="", repr=False)
    has_attachment: bool = False

    @property
    def member_key(self) -> tuple[str, str, str, str]:
        return self.platform_id, self.self_id, self.group_id, self.sender_id


def is_trusted_scope(message: MessageFacts, settings: Settings) -> bool:
    """事件是否落在"可信身份 + 允许范围"内。

    这是触发分类与权限判定共用的唯一谓词：``control.authorize`` 复用同一份，
    避免两处各写一套判断而漂移。安全判定不应依赖调用方已做过前置校验。
    """
    return bool(
        settings.identity_configured
        and message.is_group
        and settings.allows_group(
            message.platform_id,
            message.self_id,
            message.group_id,
        )
        and is_qq_id(message.sender_id)
        and message.sender_id != message.self_id
    )


def classify(message: MessageFacts, settings: Settings) -> Classification:
    """判定触发形态。候选不代表获准发送、采集或调用模型。"""
    if not is_trusted_scope(message, settings):
        return Classification.IGNORE
    # 含 @全体 的事件整体忽略，即使同时 @ 了机器人（需求 §4.1）。
    if "all" in message.mention_targets:
        return Classification.IGNORE
    # 只认指向当前机器人 self_id 的真实顶层 At 段；引用内的历史 At 不在其中。
    if message.self_id not in message.mention_targets:
        return Classification.IGNORE
    if message.direct_text.strip():
        return Classification.TEXT_CANDIDATE
    if message.has_attachment:
        return Classification.UNSUPPORTED_ATTACHMENT
    return Classification.EMPTY_OR_UNSUPPORTED
