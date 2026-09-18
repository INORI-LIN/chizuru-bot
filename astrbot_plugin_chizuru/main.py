"""插件装配：Star 生命周期、唯一出站路径与服务接线（S1-14）。

**本模块是唯一的框架入口与唯一的出站装配点**：出站只经 `_deliver()`，它在任何
`event.send()` 之前必须先过 `send_gate.evaluate()`。业务规则不写在这里——分类、指令
解析、授权、预算、队列、去重、脱敏全部由各自的纯逻辑模块负责，本模块只做接线与配对。

装配顺序（与架构 §4.3 一致）：

    分类 → 指令解析 → 授权 → 去重 begin → 预算 reserve → 调度 submit → 模型调用
      → 预算 settle → 发送门控 evaluate → event.send → 去重 finish

**配对规则**（决定条目与额度的去向）：

| 结果 | dedup | budget |
|---|---|---|
| 队列满 / 等待超时 / 调度器已关闭（从未开始） | `release` | `cancel` |
| 预算拒绝、提供商不可用、message_id 不可用 | `release` | 未预留 |
| 超业务期限（已开始，无结果） | `finish(COMPLETED)` | `settle(None)` 按估算 |
| 正常返回（含空回复与失败分支） | `finish(COMPLETED)` | `settle(usage)` |
| `event.send` 抛异常（发送不确定） | `finish(SEND_UNCERTAIN)` | 已结算 |

被框架取消（`CancelledError`）时条目留在 `IN_FLIGHT`，由 TTL 收敛：不确定是否已发送，
就不重放（架构 §6.1）。

**S1 的临时边界**（均记入 docs/03 §5.2，落地后回改）：

- 群开关由部署配置推导（`Settings.allows_group` 命中即 `OPEN`）：S1 没有持久化群策略
  （S2-01 才有），也没有暂停/关闭通道（S2-05 才有）。
- 去重窗口与容量是装配层常量，标注"建议参数待评审"——取值依赖仍未在线核验的 O-07。
- 群内出口只有 `千鹤 状态`（用 `health.format_report` 的既有文本）；附件提示与其它
  控制指令一律静默：文案属附录 C 待审范围，执行属 S2-05/S3-04，此刻发明文案会先于评审。
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
from typing import Callable

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, File, Image, Plain, Record, Video
from astrbot.api.star import Context, Star
from astrbot.core.platform.message_type import MessageType

from . import commands, context_assembly, health, llm, redact
from .budget import BudgetLedger, BudgetRefusal, BudgetRefused, PriceTable, Reservation, UsageKind
from .config import Settings
from .control import authorize
from .dedup import ActionKind, Claim, DedupKey, DedupStore, Outcome
from .keys import BotInstanceKey, GroupKey
from .policy import Classification, MessageFacts, classify
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


class ChizuruPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.services: _Services | None = None
        # 测试注入点：生产环境用默认实现，构造签名保持 (context, config)。
        self.clock: Callable[[], float] = time.monotonic
        self.datetime_clock: Callable[[], datetime] = datetime.now
        self.redactor_salt: bytes | None = None

    # ---- 生命周期 ----

    async def initialize(self) -> None:
        """构造服务；幂等。identity 未配置时也照常构造——拒绝发生在每个事件里。"""
        if self.services is not None:
            return
        settings = Settings.from_mapping(self.config)
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
            health=health.HealthMonitor(),
            redactor=redact.Redactor(salt=self.redactor_salt),
        )

    async def terminate(self) -> None:
        """释放调度器：拒绝新提交并唤醒等待者。本插件不持有后台任务。"""
        services = self.services
        self.services = None
        if services is not None:
            await services.scheduler.aclose()

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
        if services is None or classification is not Classification.TEXT_CANDIDATE:
            # 未装配或非文本候选：IGNORE / 空 @ / 附件都不产生任何出站（需求 §4.1）。
            if services is not None:
                self._audit(services, redact.EventCategory.IGNORED)
            return

        self._audit(services, redact.EventCategory.VALID_AT)
        intent = commands.parse(facts.direct_text)
        if intent is None:
            await self._handle_chat(event, facts, settings, services)
        elif intent.kind is commands.CommandKind.STATUS:
            await self._handle_status(event, facts, settings, services, intent)
        else:
            # 其它控制指令的执行与文案分别属 S2-05/S3-04 与附录 C 待审范围。
            self._audit(services, redact.EventCategory.IGNORED)

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
                lambda deadline: self._ask(provider, facts, settings, services, deadline),
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

        outcome = await self._deliver(
            event,
            facts,
            settings,
            services,
            group,
            answer.text,
            kind=SendKind.CHAT,
            llm_invoked=True,
        )
        services.dedup.finish(key, outcome)

    async def _ask(
        self,
        provider: object,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
        deadline: Deadline,
    ) -> llm.Answer:
        """一次聊天请求：直接调用提供商，最多重试 1 次且**共用同一个期限**。"""
        plan = context_assembly.build_chat_plan(
            user_text=facts.direct_text,
            max_retries=settings.limits.max_retries,
        )
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
        outcome = await self._deliver(
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
    ) -> Outcome:
        """发送前复核后出站。这是本插件唯一调用 `event.send` 的地方。"""
        request = SendRequest(
            kind=kind,
            source=facts,
            target=group,
            revision=None,
            llm_invoked=llm_invoked,
        )
        gate = evaluate(
            request,
            settings,
            GateFacts(group_state=self._group_state(settings, facts), current=None, previous=None),
        )
        if not gate.allowed:
            self._audit(services, redact.EventCategory.GATE_DROP, code=_drop_code(gate.drop))
            return Outcome.COMPLETED

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
            return Outcome.SEND_UNCERTAIN
        self._audit(
            services,
            redact.EventCategory.SEND_RESULT,
            duration_ms=max(0, int((self.clock() - started) * 1000)),
        )
        return Outcome.COMPLETED

    # ---- 小工具 ----

    @staticmethod
    def _group_key(facts: MessageFacts) -> GroupKey:
        return GroupKey(
            BotInstanceKey(platform_id=facts.platform_id, self_id=facts.self_id),
            facts.group_id,
        )

    @staticmethod
    def _group_state(settings: Settings, facts: MessageFacts) -> GroupState:
        """S1 的群开关来源是部署配置（临时来源，S2-01 持久化群策略落地后替换）。"""
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
