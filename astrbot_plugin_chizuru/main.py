from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, File, Image, Plain, Record, Video
from astrbot.api.star import Context, Star
from astrbot.core.platform.message_type import MessageType

from .config import Settings
from .policy import Classification, MessageFacts, classify

# 首版不解析媒体（需求 §1.3）：@ 后只有这些组件时，只回固定的文本能力提示。
# 合并转发（Forward/Nodes）不算附件，它整体不参与采集与解析（需求 §4.2）。
ATTACHMENT_COMPONENTS = (Image, Record, Video, File)


class ChizuruPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config

    def classify_event(self, event: AstrMessageEvent) -> Classification:
        if event.get_platform_name() != "aiocqhttp":
            return Classification.IGNORE
        chain = event.get_messages()
        # 只读取顶层组件，不递归 Reply/Nodes，也不信任拼接后的 message_str。
        facts = MessageFacts(
            platform_id=event.get_platform_id(),
            self_id=event.get_self_id(),
            group_id=event.get_group_id(),
            sender_id=event.get_sender_id(),
            is_group=event.get_message_type() == MessageType.GROUP_MESSAGE,
            mention_targets=tuple(str(part.qq) for part in chain if isinstance(part, At)),
            direct_text="".join(part.text for part in chain if isinstance(part, Plain)),
            has_attachment=any(isinstance(part, ATTACHMENT_COMPONENTS) for part in chain),
        )
        return classify(facts, Settings.from_mapping(self.config))

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def on_message(self, event: AstrMessageEvent) -> None:
        """骨架接收 OneBot 事件后静默终止；不会放行聊天或存储。"""
        if event.get_platform_name() != "aiocqhttp":
            return
        # 先停止再分类，配置/适配错误也不能放行后续模型调用。
        # 该处理器不能阻止框架更早的 WakingCheckStage 或其他插件直接发送。
        event.stop_event()
        event.clear_result()
        event.set_extra("chizuru.classification", self.classify_event(event).value)
