"""插件装配：Star 生命周期、唯一出站路径与服务接线（S1-14）。

**本模块是唯一的框架入口与唯一的出站装配点**：出站只经 `_deliver()`，它在任何
`event.send()` 之前必须先过 `send_gate.evaluate()`。业务规则不写在这里——分类、指令
解析、授权、预算、队列、去重、脱敏全部由各自的纯逻辑模块负责，本模块只做接线与配对。

装配顺序（与架构 §4.3 一致）：

    分类 → 指令解析 → 授权 → 去重 begin → 装配（材料/历史/修订快照）→ 预算 reserve
      → 调度 submit → 模型调用 → 预算 settle → 发送门控 evaluate → event.send
      → 写回合格历史 → 去重 finish

**配对规则**（决定条目与额度的去向）：

| 结果 | dedup | budget |
|---|---|---|
| 装配拒绝（固定规则 + 当前输入超预算） | `release` | 未预留 |
| 队列满 / 等待超时 / 调度器已关闭（从未开始） | `release` | `cancel` |
| 预算拒绝、提供商不可用、message_id 不可用 | `release` | 未预留 |
| 超业务期限（已开始，无结果） | `finish(COMPLETED)` | `settle(None)` 按估算 |
| 正常返回（含空回复与失败分支） | `finish(COMPLETED)` | `settle(usage)` |
| `event.send` 抛异常（发送不确定） | `finish(SEND_UNCERTAIN)` | 已结算 |

被框架取消（`CancelledError`）时条目留在 `IN_FLIGHT`，由 TTL 收敛：不确定是否已发送，
就不重放（架构 §6.1）。

**临时边界与已知限制**（均记入 docs/03 §5.2，落地后回改）：

- 群开关（`send_gate` 的 `GroupState`）仍由部署配置推导：持久化群策略已在 S2-01 落地，
  但**替换延后到 S2-02**——S2-02 之前没有任何合法途径写出"已告知 + 开启"，提前替换只会
  让聊天静默停摆。聊天不要求告知，采集才要求。
- 普通群聊采集（S2-03）与 `上下文 退出/加入`（S2-04）已接线；采集准入读持久化群策略
  （**无行即关闭**），因此 S2-02/S2-05 落地前生产环境实际不采集、暂停/关闭命令也不存在。
- 动态材料与互动历史（S2-06/S2-07）已接入请求：材料只落 `extra_user_content_parts` 并
  标记为临时，历史只落 `contexts`；两者都经同一 token 预算裁剪，**结构上不进 system**。
  超预算时不调模型、不发送、静默（超长提示文案属附录 C 待审范围）。
- 互动历史写回只在**送达成功**后进行，且写前重读群/成员修订号；写失败只记审计，
  不影响已送达的回复、不置持久降级（历史不是聊天的前置条件）。
- 存储路径来自 AstrBot 插件数据目录，**延迟建库**（不写不建文件）；解析或打开失败即
  降级为"无存储"，采集与退出/加入保持关闭，`千鹤 状态` 可见。
- 去重窗口与容量是装配层常量，标注"建议参数待评审"——取值依赖仍未在线核验的 O-07。
- 群内出口只有 `千鹤 状态`（用 `health.format_report` 的既有文本）；`上下文 退出/加入`
  执行但**静默无回执**，其余控制指令不执行：回执文案属附录 C 待审范围，执行属
  S2-05/S3-04，此刻发明文案会先于评审。
- 聊天失败不发任何群内提示：同样的理由（架构 §8.3 的失败文案属 S4-01）。
- `limits` 与 `budget` 在 `initialize()` 时固定，运行时改动需重载插件；身份、允许群、
  维护者映射与金额开关每个事件重读（配置变更即时生效）。

**收口三件套**：`stop_event()` + `clear_result()` + `should_call_llm(True)`。前两者沿用
骨架姿态，第三个才是抑制框架默认 LLM 链路的那一个（K5：`call_llm` 初值为 `False`，
第二路径的门槛是 `not event.call_llm`）。框架侧仍须 `provider_settings.enable=false`。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Sequence

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import (
    At,
    File,
    Forward,
    Image,
    Nodes,
    Plain,
    Record,
    Reply,
    Video,
)
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart
from astrbot.core.platform.message_type import MessageType

from . import commands, context_assembly, health, history, llm, redact, storage
from .budget import BudgetLedger, BudgetRefusal, BudgetRefused, PriceTable, Reservation, UsageKind
from .config import Settings
from .context_buffer import BufferShape, ContextBuffer, IngestOutcome
from .control import authorize
from .dedup import ActionKind, Claim, DedupKey, DedupStore, Outcome
from .keys import BotInstanceKey, GroupKey, MemberKey, RevisionSnapshot
from .policy import Classification, MessageFacts, classify, is_trusted_scope
from .scheduler import AdmissionRefused, Deadline, DeadlineExceeded, Scheduler
from .send_gate import (
    DropReason,
    GateFacts,
    GroupState,
    SendKind,
    SendRequest,
    evaluate,
)

# 首版不解析媒体（需求 §1.3）：@ 后只有这些组件时，只回固定的文本能力提示。
# 合并转发（Forward/Nodes）不算附件，它整体不参与采集与解析（需求 §4.2）。
ATTACHMENT_COMPONENTS = (Image, Record, Video, File)

CLASSIFICATION_EXTRA = "chizuru.classification"

DEDUP_WINDOW_SECONDS = 600.0
"""去重窗口（秒）。**建议参数待评审**：取值依赖 O-07（message_id 稳定性与重连回放
窗口，S0-08），在线结论出来前它只是与群缓冲 TTL 同量级的假设值。"""

DEDUP_CAPACITY = 2048
"""去重容量上界（条）。**建议参数待评审**：条目只含标识与状态，不含正文。"""

UNKNOWN_MODEL = "unknown"
"""提供商未暴露模型名时的预算预留键；价目表为空时不参与定价。"""


@dataclass
class _Services:
    """跨事件存活的服务集合；由 `initialize()` 构造、`terminate()` 释放。"""

    dedup: DedupStore
    budget: BudgetLedger
    scheduler: Scheduler
    health: health.HealthMonitor
    redactor: redact.Redactor
    storage: storage.Storage | None
    context_buffer: ContextBuffer


@dataclass(frozen=True)
class _ChatPreparation:
    """一次聊天的装配结果：请求形态与发起时的数据修订号快照。"""

    plan: llm.LLMRequestPlan
    revision: RevisionSnapshot | None


class _ConversationHistory:
    """把 AstrBot 会话存储适配成 `history` 模块要的两件事：读原文、写条列表。

    不做业务判断（筛选取严、轮数上限、私有键剥离都在 `history.py`），也不做补救：
    任何失败由调用方降级为"无历史"。**懒解析** `context` 属性，构造与 `initialize()`
    都不触碰框架上下文。
    """

    def __init__(self, context: Context) -> None:
        self._context = context

    def _manager(self) -> object:
        return self._context.conversation_manager

    async def load(self, umo: str) -> str | None:
        """读会话存储的 JSON 原文；没有会话就不是"空历史"而是"没有历史"。"""
        manager = self._manager()
        conversation_id = await manager.get_curr_conversation_id(umo)
        if not conversation_id:
            return None
        conversation = await manager.get_conversation(umo, conversation_id)
        raw = getattr(conversation, "history", None)
        return raw if isinstance(raw, str) else None

    async def save(self, umo: str, entries: Sequence[Mapping[str, object]]) -> None:
        """整体写回（含裁剪结果）；没有会话时先建一个再写。"""
        manager = self._manager()
        conversation_id = await manager.get_curr_conversation_id(umo)
        if not conversation_id:
            conversation_id = await manager.new_conversation(umo)
        await manager.update_conversation(umo, conversation_id=conversation_id, history=list(entries))


class ChizuruPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.services: _Services | None = None
        # 测试注入点：生产环境用默认实现，构造签名保持 (context, config)。
        self.clock: Callable[[], float] = time.monotonic
        self.datetime_clock: Callable[[], datetime] = datetime.now
        self.redactor_salt: bytes | None = None
        self.storage_path: Path | None = None
        self.history_cleaner: Callable[[str], Awaitable[None]] | None = None
        self.history_store: object | None = None
        """互动历史存储（S2-07）的测试注入点；默认用 `_ConversationHistory(context)`。"""

    # ---- 生命周期 ----

    async def initialize(self) -> None:
        """构造服务；幂等。identity 未配置时也照常构造——拒绝发生在每个事件里。"""
        if self.services is not None:
            return
        settings = Settings.from_mapping(self.config)
        monitor = health.HealthMonitor()
        self.services = _Services(
            dedup=DedupStore(
                window_seconds=DEDUP_WINDOW_SECONDS,
                capacity=DEDUP_CAPACITY,
                clock=self.clock,
            ),
            budget=BudgetLedger(
                settings.budget,
                prices=PriceTable(),
                clock=self.datetime_clock,
            ),
            scheduler=Scheduler(settings.limits, monotonic=self.clock),
            health=monitor,
            redactor=redact.Redactor(salt=self.redactor_salt),
            storage=self._open_storage(monitor),
            context_buffer=ContextBuffer(
                max_messages=settings.context.buffer_max_messages,
                ttl_seconds=settings.context.buffer_ttl_seconds,
                clock=self.clock,
            ),
        )

    async def terminate(self) -> None:
        """释放存储与调度器：拒绝新提交并唤醒等待者。本插件不持有后台任务。"""
        services = self.services
        self.services = None
        if services is not None:
            if services.storage is not None:
                services.storage.close()
            await services.scheduler.aclose()

    # ---- 存储 ----

    def _open_storage(self, monitor: health.HealthMonitor) -> storage.Storage | None:
        """解析数据目录并打开存储；任何失败都降级为"无存储"，绝不抛出。

        无存储 = 不采集、不可退出/加入（架构 §8.3 的"保持关闭"），并在 `千鹤 状态`
        中可见；聊天不依赖存储，仍然可用。
        """
        path = self._resolve_storage_path()
        if path is None:
            monitor.set_degraded(health.Degradation.MEMORY_STORE_FAILED)
            return None
        try:
            return storage.open_storage(path, clock=self._now_epoch)
        except storage.StorageFailure:
            monitor.set_degraded(health.Degradation.MEMORY_STORE_FAILED)
            return None

    def _resolve_storage_path(self) -> Path | None:
        """存储路径：测试用 `self.storage_path` 注入，生产取 AstrBot 插件数据目录。

        `StarTools.get_data_dir()` 按调用栈识别插件，因此必须在本模块内调用；
        它只创建数据目录，不创建库文件（建库是首次写入的事）。
        """
        if self.storage_path is not None:
            return self.storage_path
        try:
            return StarTools.get_data_dir() / storage.DB_FILE_NAME
        except Exception:
            return None

    def _now_epoch(self) -> int:
        return int(self.datetime_clock().timestamp())

    # ---- 事实提取 ----

    def classify_event(self, event: AstrMessageEvent) -> Classification:
        if event.get_platform_name() != "aiocqhttp":
            return Classification.IGNORE
        return classify(self._extract_facts(event), Settings.from_mapping(self.config))

    def _extract_facts(self, event: AstrMessageEvent) -> MessageFacts:
        chain = event.get_messages()
        # 只读取顶层组件，不递归 Reply/Nodes，也不信任拼接后的 message_str。
        return MessageFacts(
            platform_id=event.get_platform_id(),
            self_id=event.get_self_id(),
            group_id=event.get_group_id(),
            sender_id=event.get_sender_id(),
            is_group=event.get_message_type() == MessageType.GROUP_MESSAGE,
            mention_targets=tuple(str(part.qq) for part in chain if isinstance(part, At)),
            direct_text="".join(part.text for part in chain if isinstance(part, Plain)),
            has_attachment=any(isinstance(part, ATTACHMENT_COMPONENTS) for part in chain),
        )

    # ---- 唯一入口 ----

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def on_message(self, event: AstrMessageEvent) -> None:
        """先收口再分类，然后按触发形态分流；出站只经 `_deliver()`。"""
        if event.get_platform_name() != "aiocqhttp":
            return
        # 该处理器不能阻止框架更早的 WakingCheckStage 或保留内置插件直接发送（K1/K2/R15）。
        event.stop_event()
        event.clear_result()
        event.should_call_llm(True)

        settings = Settings.from_mapping(self.config)
        facts = self._extract_facts(event)
        classification = classify(facts, settings)
        event.set_extra(CLASSIFICATION_EXTRA, classification.value)

        services = self.services
        if services is None:
            return
        if classification is not Classification.TEXT_CANDIDATE:
            # 非文本候选只可能进普通群聊缓冲；未告知群、私聊、@全体、附件与命令
            # 一律在 `_maybe_collect` 内被拒绝（需求 §4.1/§4.2），零出站零模型调用。
            outcome = self._maybe_collect(event, facts, settings, services)
            if outcome is not IngestOutcome.STORED:
                self._audit(services, redact.EventCategory.IGNORED)
            return

        self._audit(services, redact.EventCategory.VALID_AT)
        intent = commands.parse(facts.direct_text)
        if intent is None:
            await self._handle_chat(event, facts, settings, services)
        elif intent.kind is commands.CommandKind.STATUS:
            await self._handle_status(event, facts, settings, services, intent)
        elif intent.kind in (
            commands.CommandKind.CONTEXT_LEAVE,
            commands.CommandKind.CONTEXT_JOIN,
        ):
            await self._handle_context_switch(event, facts, settings, services, intent)
        else:
            # 其它控制指令的执行与文案分别属 S2-05/S3-04 与附录 C 待审范围。
            self._audit(services, redact.EventCategory.IGNORED)

    # ---- 普通群聊采集（S2-03） ----

    def _maybe_collect(
        self,
        event: AstrMessageEvent,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
    ) -> IngestOutcome | None:
        """尝试把一条普通群聊写入内存缓冲；不产生任何出站或模型调用。

        准入只来自可信事实与持久化状态：策略仓储**没有行就是关闭**，因此"未告知
        不采集"不依赖任何默认放行（S2-02 落地前后同样成立）。群开关的临时来源
        （部署配置）不参与采集判定——采集要求告知，聊天不要求。
        """
        stored = services.storage
        if stored is None or stored.database.failed:
            return None
        if services.health.degradations() & {
            health.Degradation.MEMORY_STORE_FAILED,
            health.Degradation.DELETION_FAILED,
        }:
            # 无法确认退出/删除状态时停止采集（架构 §8.3）。
            return None
        if not is_trusted_scope(facts, settings):
            return None
        shape = self._buffer_shape(event.get_messages())
        if shape is not BufferShape.TEXT_ONLY:
            return None
        message_id = self._message_id(event)
        if message_id is None:
            return None

        group = self._group_key(facts)
        member = MemberKey(group, facts.sender_id)
        try:
            policy = stored.groups.policy(group)
            if not policy.is_collection_open(required_notice_version=settings.notice_version):
                return None
            if stored.members.state(member).opted_out:
                return None
        except storage.StorageFailure:
            services.health.set_degraded(health.Degradation.MEMORY_STORE_FAILED)
            return None
        return services.context_buffer.ingest(
            group=group,
            member=member,
            message_id=message_id,
            text=facts.direct_text,
            shape=shape,
            nickname=self._sender_nickname(event),
        )

    @staticmethod
    def _sender_nickname(event: AstrMessageEvent) -> str:
        """事件里的昵称，**只用于展示**：不进键、不参与任何判定（需求 §5.3、架构 §2.2）。"""
        sender = getattr(event.message_obj, "sender", None)
        value = getattr(sender, "nickname", "")
        return value if isinstance(value, str) else ""

    @staticmethod
    def _buffer_shape(chain: Sequence[object]) -> BufferShape:
        """把顶层消息组件映射为缓冲形状；只有纯文本可以进入缓冲。

        只读顶层、不递归：引用与合并转发内的内容不参与判定，也不需要解析。
        """
        if any(isinstance(part, Reply) for part in chain):
            return BufferShape.HAS_QUOTE
        if any(isinstance(part, (Forward, Nodes)) for part in chain):
            return BufferShape.HAS_FORWARD
        if any(isinstance(part, At) for part in chain):
            return BufferShape.HAS_MENTION
        if any(isinstance(part, ATTACHMENT_COMPONENTS) for part in chain):
            return BufferShape.HAS_ATTACHMENT
        if chain and all(isinstance(part, Plain) for part in chain):
            return BufferShape.TEXT_ONLY
        return BufferShape.OTHER

    # ---- 聊天 ----

    async def _handle_chat(
        self,
        event: AstrMessageEvent,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
    ) -> None:
        group = self._group_key(facts)
        message_id = self._message_id(event)
        if message_id is None:
            # 去重是回复的前置：拿不到稳定 message_id 就不处理（R10，fail-closed）。
            self._audit(services, redact.EventCategory.IGNORED)
            return
        key = DedupKey(group=group, message_id=message_id, action=ActionKind.CHAT_REPLY)
        if services.dedup.begin(key) is not Claim.FIRST:
            self._audit(services, redact.EventCategory.IGNORED)
            return

        prepared = await self._prepare_chat(event, facts, settings, services, message_id)
        if prepared is None:
            # 固定规则 + 当前输入已超预算：不预留、不调用、不发送（提示文案属附录 C 待审范围）。
            services.dedup.release(key)
            self._audit(services, redact.EventCategory.MODEL_CALL, code=redact.ErrorCode.REQUEST_INVALID)
            return

        provider = await self._acquire_provider(event, services, facts, message_id)
        if provider is None:
            services.dedup.release(key)
            return
        reservation = self._reserve(services, provider)
        if reservation is None:
            services.dedup.release(key)
            return

        try:
            answer = await services.scheduler.submit_chat(
                group,
                lambda deadline: self._ask(provider, prepared.plan, settings, services, deadline),
            )
        except AdmissionRefused:
            # 从未开始：额度可退，事件可重来（重连回放不该被永久判为已处理）。
            services.budget.cancel(reservation)
            services.dedup.release(key)
            self._audit(services, redact.EventCategory.QUEUE_DEPTH, code=redact.ErrorCode.QUEUE_FULL)
            return
        except DeadlineExceeded:
            # 已开始但无结果：缺失 usage 按预留估算入账，绝不记零。
            services.budget.settle(reservation, None)
            services.dedup.finish(key, Outcome.COMPLETED)
            self._audit(
                services,
                redact.EventCategory.MODEL_CALL,
                code=redact.ErrorCode.PROVIDER_UNAVAILABLE,
            )
            return
        except asyncio.CancelledError:
            services.budget.settle(reservation, None)
            raise

        services.budget.settle(reservation, answer.usage)
        if answer.usage is not None:
            self._audit(services, redact.EventCategory.TOKEN_USAGE, tokens=answer.usage)
        if answer.kind is not llm.AnswerKind.TEXT:
            # S1 不发失败或空回复提示：文案属 S4-01 与附录 C 待审范围。
            services.dedup.finish(key, Outcome.COMPLETED)
            return

        outcome, sent = await self._deliver(
            event,
            facts,
            settings,
            services,
            group,
            answer.text,
            kind=SendKind.CHAT,
            llm_invoked=True,
            revision=prepared.revision,
        )
        if history.should_record(memory_assisted=False, delivered=sent):
            # `memory_assisted` 本批恒 False；S3-08 起为"本轮检索到长期记忆"。
            await self._record_history(event, facts, settings, services, answer.text, prepared.revision)
        services.dedup.finish(key, outcome)

    async def _prepare_chat(
        self,
        event: AstrMessageEvent,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
        message_id: str,
    ) -> _ChatPreparation | None:
        """装配一次聊天请求：动态材料 + 合格历史 + 修订号快照；超预算返回 `None`。

        材料来自内存缓冲（同一事件已按 message_id 排除），历史来自会话存储并已按
        轮数/TTL 筛选；两者都经同一 token 预算裁剪（架构 §5.2）。**材料只进
        `extra_user_content_parts` 并标记为临时，历史只进 `contexts`，都不进 system。**
        """
        group = self._group_key(facts)
        materials = context_assembly.render_materials(
            services.context_buffer.entries(group),
            exclude_message_id=message_id,
            current_member_id=facts.sender_id,
            current_nickname=self._sender_nickname(event),
            now_monotonic=self.clock(),
            now_wall=self.datetime_clock(),
        )
        trimmed = context_assembly.trim_to_budget(
            system_prompt=context_assembly.system_prompt(),
            user_text=facts.direct_text,
            materials=materials,
            history_turns=await self._load_history(event, settings, services),
            budget=settings.budget.input_token_budget,
        )
        if trimmed is None:
            return None
        parts: tuple[object, ...] = ()
        if trimmed.materials is not None:
            # part 级临时标记（K6）：框架若把这一轮交给持久化，注入片段会被剔除。
            parts = (TextPart(text=trimmed.materials).mark_as_temp(),)
        plan = context_assembly.build_chat_plan(
            user_text=facts.direct_text,
            max_retries=settings.limits.max_retries,
            dynamic_parts=parts,
            contexts=trimmed.contexts,
        )
        return _ChatPreparation(plan=plan, revision=self._revision_snapshot(services, group, facts.sender_id))

    async def _load_history(
        self,
        event: AstrMessageEvent,
        settings: Settings,
        services: _Services,
    ) -> tuple[history.Turn, ...]:
        """读会话存储并筛出有效期内的最近轮次；**任何失败都降级为"无历史"**。

        历史只影响多轮体验，不是聊天的前置条件：读失败不置持久降级、不影响出站。
        """
        store = self._history()
        try:
            raw = await store.load(event.unified_msg_origin)
        except Exception:
            self._audit(services, redact.EventCategory.CONTEXT_OP, code=redact.ErrorCode.UNCLASSIFIED)
            return ()
        return history.select(
            history.parse(raw),
            max_turns=settings.context.history_max_turns,
            ttl_seconds=settings.context.history_ttl_hours * 3600,
            now_epoch=self._now_epoch(),
        )

    async def _record_history(
        self,
        event: AstrMessageEvent,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
        answer_text: str,
        revision: RevisionSnapshot | None,
    ) -> None:
        """把已送达的一轮写回会话存储（S2-07）：**写前重读修订号，退出/清空后不复活**。

        只写普通问答：控制命令与记忆辅助轮根本不进这个函数（架构 §4.4、需求 §4.3）。
        任何失败只记审计——已发送的回复不受影响，历史也不是聊天的前置条件。
        """
        if revision is not None and self._revision_snapshot(services, self._group_key(facts), facts.sender_id) != revision:
            return
        store = self._history()
        try:
            raw = await store.load(event.unified_msg_origin)
            turns = history.parse(raw) + (
                history.make_turn(
                    user_text=facts.direct_text,
                    assistant_text=answer_text,
                    at_epoch=self._now_epoch(),
                ),
            )
            qualified = history.select(
                turns,
                max_turns=settings.context.history_max_turns,
                ttl_seconds=settings.context.history_ttl_hours * 3600,
                now_epoch=self._now_epoch(),
            )
            await store.save(event.unified_msg_origin, history.storage_entries(qualified))
        except Exception:
            self._audit(services, redact.EventCategory.CONTEXT_OP, code=redact.ErrorCode.UNCLASSIFIED)
            return
        self._audit(services, redact.EventCategory.CONTEXT_OP)

    def _history(self) -> object:
        """互动历史存储：测试注入 `history_store`，生产用框架会话存储（懒解析 context）。"""
        if self.history_store is not None:
            return self.history_store
        return _ConversationHistory(self.context)

    def _revision_snapshot(
        self,
        services: _Services,
        group: GroupKey,
        member_id: str,
    ) -> RevisionSnapshot | None:
        """读群与成员修订号；无存储或读失败返回 `None`（无法比对 ≠ 已变更）。"""
        stored = services.storage
        if stored is None or stored.database.failed:
            return None
        try:
            return RevisionSnapshot(
                group_revision=stored.groups.policy(group).revision,
                member_revision=stored.members.state(MemberKey(group, member_id)).revision,
            )
        except storage.StorageFailure:
            return None

    async def _ask(
        self,
        provider: object,
        plan: llm.LLMRequestPlan,
        settings: Settings,
        services: _Services,
        deadline: Deadline,
    ) -> llm.Answer:
        """一次聊天请求：直接调用提供商，最多重试 1 次且**共用同一个期限**。"""
        policy = llm.RetryPolicy.from_limits(settings.limits)
        attempt = 0
        while True:
            attempt += 1
            started = self.clock()
            try:
                response = await provider.text_chat(**plan.call_kwargs())
                answer = llm.interpret(llm.ResponseFacts.from_response(response))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # 失败分支不携带任何模型文本，只留错误码。
                answer = llm.Answer(
                    kind=llm.AnswerKind.FAILED,
                    code=llm.classify_exception(error),
                )
            self._audit(
                services,
                redact.EventCategory.MODEL_CALL,
                code=answer.code,
                duration_ms=max(0, int((self.clock() - started) * 1000)),
            )
            decision = policy.decide(
                answer,
                attempt=attempt,
                deadline=deadline,
                now=self.clock(),
            )
            if not decision.retry:
                return answer

    async def _acquire_provider(
        self,
        event: AstrMessageEvent,
        services: _Services,
        facts: MessageFacts,
        message_id: str,
    ) -> object | None:
        """取当前提供商；任何失败都收敛为 None（绝不抛出到处理器外）。"""
        try:
            provider = await self.context.get_using_provider_async(event.unified_msg_origin)
        except Exception:
            provider = None
        if provider is None:
            services.health.set_degraded(health.Degradation.PROVIDER_DISABLED)
            self._audit(
                services,
                redact.EventCategory.MODEL_CALL,
                code=redact.ErrorCode.UNCLASSIFIED,
                parts=(facts.group_id, message_id),
            )
            return None
        services.health.clear_degraded(health.Degradation.PROVIDER_DISABLED)
        return provider

    def _reserve(self, services: _Services, provider: object) -> Reservation | None:
        try:
            return services.budget.reserve(UsageKind.CHAT, self._model_name(provider))
        except BudgetRefused as refused:
            self._audit(
                services,
                redact.EventCategory.GATE_DROP,
                code=_budget_code(refused.reason),
            )
            return None

    @staticmethod
    def _model_name(provider: object) -> str:
        try:
            name = provider.get_model()
        except Exception:
            return UNKNOWN_MODEL
        return name if isinstance(name, str) and name else UNKNOWN_MODEL

    # ---- 控制：千鹤 状态 ----

    async def _handle_status(
        self,
        event: AstrMessageEvent,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
        intent: commands.CommandIntent,
    ) -> None:
        """维护者状态查询：全链路只读，且在任何模型工作之前完成。"""
        group = self._group_key(facts)
        message_id = self._message_id(event)
        if message_id is None:
            self._audit(services, redact.EventCategory.IGNORED)
            return
        key = DedupKey(group=group, message_id=message_id, action=ActionKind.CHAT_REPLY)
        if services.dedup.begin(key) is not Claim.FIRST:
            self._audit(services, redact.EventCategory.IGNORED)
            return
        if not authorize(intent, facts, settings).allowed:
            # 权限不足不发提示（附录 D.2 no_permission_reply=false 的插件侧对应）。
            services.dedup.release(key)
            self._audit(services, redact.EventCategory.IGNORED)
            return

        self._observe_platform(services, settings)
        snapshot = services.health.snapshot(
            budget=services.budget.snapshot(),
            scheduler=services.scheduler.stats(),
        )
        report = "\n".join(
            health.format_report(snapshot, configured=settings.identity_configured)
        )
        outcome, _ = await self._deliver(
            event,
            facts,
            settings,
            services,
            group,
            report,
            kind=SendKind.FIXED_NOTICE,
            llm_invoked=False,
        )
        services.dedup.finish(key, outcome)

    def _observe_platform(self, services: _Services, settings: Settings) -> None:
        """按 `health` 的提取契约读平台状态：只读 `status` 与错误条数。"""
        try:
            instance = self.context.get_platform_inst(settings.platform_id)
        except Exception:
            instance = None
        if instance is None:
            services.health.observe_platform(instance_present=False)
            return
        try:
            status = instance.status
            error_count = len(instance.errors)
        except Exception:
            status, error_count = None, 0
        services.health.observe_platform(
            instance_present=True,
            status=status,
            error_count=error_count,
        )
        self._audit(services, redact.EventCategory.PLATFORM_STATE, count=error_count)

    # ---- 控制：上下文 退出 / 加入（S2-04） ----

    async def _handle_context_switch(
        self,
        event: AstrMessageEvent,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
        intent: commands.CommandIntent,
    ) -> None:
        """成员本人的上下文退出/加入：确定性状态变更，**本批静默**（回执文案待审）。

        顺序：先持久化 → 再清本人缓冲 → 最后清受影响群历史。即使历史清理失败，
        采集也已经停止（fail-closed），且不虚报已删除（架构 §8.3）。
        """
        group = self._group_key(facts)
        message_id = self._message_id(event)
        if message_id is None:
            self._audit(services, redact.EventCategory.IGNORED)
            return
        key = DedupKey(group=group, message_id=message_id, action=ActionKind.CHAT_REPLY)
        if services.dedup.begin(key) is not Claim.FIRST:
            self._audit(services, redact.EventCategory.IGNORED)
            return
        if not authorize(intent, facts, settings).allowed:
            services.dedup.release(key)
            self._audit(services, redact.EventCategory.IGNORED)
            return

        stored = services.storage
        if stored is None or stored.database.failed:
            # 无法持久化就什么都不做：不虚报成功，相关能力保持关闭。
            services.health.set_degraded(health.Degradation.MEMORY_STORE_FAILED)
            self._audit(
                services,
                redact.EventCategory.CONTEXT_OP,
                parts=(facts.group_id, message_id),
                code=redact.ErrorCode.MEMORY_STORE_FAILED,
            )
            services.dedup.finish(key, Outcome.COMPLETED)
            return

        member = MemberKey(group, facts.sender_id)
        try:
            if intent.kind is commands.CommandKind.CONTEXT_LEAVE:
                stored.members.opt_out(member)
                services.context_buffer.clear_member(member)
                await self._clear_group_history(event, services)
            else:
                stored.members.opt_in(
                    member,
                    required_notice_version=settings.notice_version,
                )
        except storage.PolicyRefused:
            # 本群未告知/未开启/已暂停：加入不生效，按忽略记录（不发提示）。
            services.dedup.finish(key, Outcome.COMPLETED)
            self._audit(services, redact.EventCategory.IGNORED)
            return
        except storage.StorageFailure:
            services.health.set_degraded(health.Degradation.MEMORY_STORE_FAILED)
            self._audit(
                services,
                redact.EventCategory.CONTEXT_OP,
                parts=(facts.group_id, message_id),
                code=redact.ErrorCode.MEMORY_STORE_FAILED,
            )
            services.dedup.finish(key, Outcome.COMPLETED)
            return

        self._audit(services, redact.EventCategory.CONTEXT_OP)
        services.dedup.finish(key, Outcome.COMPLETED)

    async def _clear_group_history(self, event: AstrMessageEvent, services: _Services) -> None:
        """清理该群互动历史；失败只登记降级，不重试、不虚报（重试属 S2-08）。"""
        cleaner = self.history_cleaner or self._delete_group_history
        try:
            await cleaner(event.unified_msg_origin)
        except Exception:
            services.health.set_degraded(health.Degradation.DELETION_FAILED)
            self._audit(
                services,
                redact.EventCategory.CLEANUP_FAILURE,
                code=redact.ErrorCode.DELETE_FAILED,
            )

    async def _delete_group_history(self, umo: str) -> None:
        """原生会话删除（K9）：群共享会话下按 umo 删除即整群互动历史（R14）。"""
        await self.context.conversation_manager.delete_conversations_by_user_id(umo)

    # ---- 唯一出站路径 ----

    async def _deliver(
        self,
        event: AstrMessageEvent,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
        group: GroupKey,
        text: str,
        *,
        kind: SendKind,
        llm_invoked: bool,
        revision: RevisionSnapshot | None = None,
    ) -> tuple[Outcome, bool]:
        """发送前复核后出站。这是本插件唯一调用 `event.send` 的地方。

        返回 ``(去重结论, 是否已送出)``：门控丢弃与发送成功在去重里同为 ``COMPLETED``，
        但只有真的送出才允许写回互动历史。`revision` 是发起工作时的修订号快照：非 `None`
        时在发送前重读一次，退出/暂停/授权变更发生后丢弃在途结果（架构 §6.2）。
        """
        request = SendRequest(
            kind=kind,
            source=facts,
            target=group,
            revision=revision,
            llm_invoked=llm_invoked,
        )
        current = None
        if revision is not None:
            current = self._revision_snapshot(services, group, facts.sender_id)
        gate = evaluate(
            request,
            settings,
            GateFacts(group_state=self._group_state(settings, facts), current=current, previous=None),
        )
        if not gate.allowed:
            self._audit(services, redact.EventCategory.GATE_DROP, code=_drop_code(gate.drop))
            return Outcome.COMPLETED, False

        started = self.clock()
        try:
            await event.send(MessageChain([Plain(text)]))
        except Exception:
            # 上游不返回 message_id、协议端没有回执：异常是唯一的不确定信号。
            self._audit(
                services,
                redact.EventCategory.SEND_RESULT,
                duration_ms=max(0, int((self.clock() - started) * 1000)),
            )
            return Outcome.SEND_UNCERTAIN, False
        self._audit(
            services,
            redact.EventCategory.SEND_RESULT,
            duration_ms=max(0, int((self.clock() - started) * 1000)),
        )
        return Outcome.COMPLETED, True

    # ---- 小工具 ----

    @staticmethod
    def _group_key(facts: MessageFacts) -> GroupKey:
        return GroupKey(
            BotInstanceKey(platform_id=facts.platform_id, self_id=facts.self_id),
            facts.group_id,
        )

    @staticmethod
    def _group_state(settings: Settings, facts: MessageFacts) -> GroupState:
        """群开关：S1 的临时来源是部署配置；替换为持久化策略**延后到 S2-02**。

        聊天不要求告知、采集才要求：采集准入在 `_maybe_collect` 读策略仓储
        （无行即关闭）。S2-02 之前没有写入"已告知 + 开启"的合法路径，此刻替换
        只会让聊天静默停摆，不带来任何采集能力。
        """
        if settings.allows_group(facts.platform_id, facts.self_id, facts.group_id):
            return GroupState.OPEN
        return GroupState.CLOSED

    @staticmethod
    def _message_id(event: AstrMessageEvent) -> str | None:
        value = getattr(event.message_obj, "message_id", None)
        if isinstance(value, str) and value.strip() and value == value.strip():
            return value
        return None

    def _audit(
        self,
        services: _Services,
        category: redact.EventCategory,
        *,
        parts: tuple[str, ...] = (),
        code: redact.ErrorCode | None = None,
        duration_ms: int | None = None,
        tokens: object | None = None,
        count: int | None = None,
    ) -> None:
        """记录一条类别化审计；无自由文本字段，正文与密钥在类型上写不进来。"""
        record = redact.AuditRecord(
            category=category,
            code=code,
            duration_ms=duration_ms,
            tokens=tokens,
            count=count,
            correlation=services.redactor.correlation(*parts) if parts else None,
        )
        message = redact.format_record(record)
        if category is redact.EventCategory.IGNORED:
            self.logger.debug(message)
        else:
            self.logger.info(message)


def _budget_code(reason: BudgetRefusal) -> redact.ErrorCode:
    if reason in (BudgetRefusal.LIMIT_EXHAUSTED, BudgetRefusal.EXTRACTION_DISABLED):
        return redact.ErrorCode.BUDGET_EXHAUSTED
    return redact.ErrorCode.UNCLASSIFIED


def _drop_code(drop: DropReason | None) -> redact.ErrorCode | None:
    if drop is DropReason.REVISION_CHANGED:
        return redact.ErrorCode.REVISION_CHANGED
    return None
