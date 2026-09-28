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
| 装配拒绝（固定规则 + 当前输入超预算）：回一次缩短提示 | `finish` | 未预留 |
| 队列满 / 等待调度超时（从未开始）：回一次忙碌提示 | `finish` | `cancel` |
| 调度器已关闭（从未开始） | `release` | `cancel` |
| 预算拒绝、提供商不可用：回一次暂不可用提示 | `finish` | 未预留 |
| 粘滞降级已置位（401/402）：回一次暂不可用提示 | `finish` | 未预留 |
| message_id 不可用 | 不进入去重 | 未预留 |
| 超业务期限（已开始，无结果）：回一次失败提示 | `finish` | `settle(None)` 按估算 |
| 正常返回（含已分类的失败与空回复：回一次固定提示） | `finish` | `settle(usage)` |
| 正常返回但类别无文案（未分类故障） | `finish(COMPLETED)` | `settle(usage)` |
| `event.send` 抛异常（发送不确定） | `finish(SEND_UNCERTAIN)` | 已结算 |

被框架取消（`CancelledError`）时条目留在 `IN_FLIGHT`，由 TTL 收敛：不确定是否已发送，
就不重放（架构 §6.1）。

**临时边界与已知限制**（均记入 docs/03 §5.2，落地后回改）：

- 群开关（`send_gate` 的 `GroupState`）**来自持久化策略**（S1 的"仅看部署配置"已在 S2-02
  替换）：CHAT 受"暂停"约束，`FIXED_NOTICE` 不受（否则暂停群里连状态查询都不可达）；
  存储缺失或读失败按未暂停处理（R21：存储不是聊天的前置条件）。暂停期聊天在调模型前
  就停下，不产生费用。聊天不要求告知，采集才要求。
- 普通群聊采集（S2-03）与 `上下文 退出/加入`（S2-04）已接线；采集准入读持久化群策略
  （**无行即关闭**）。
- 动态材料、互动历史与**长期记忆**（S2-06/S2-07/S3-08）已接入请求：材料与记忆只落
  `extra_user_content_parts` 并标记为临时，历史只落 `contexts`；三者都经同一 token 预算
  裁剪，**结构上不进 system**。预算不够时先丢群聊材料、再丢记忆行、最后丢历史。超预算时
  不调模型、不发送正文，只回一次缩短提示（S4-01，文案取自附录 C.5）。
- 记忆注入以**本轮的修订号快照先于读取**为前置（S3-09）：用了记忆的那一轮不写回共享历史
  （`memory_assisted`），因此注入的记忆不会永久混进群历史。
- 互动历史写回只在**送达成功**后进行，且写前重读群/成员修订号；写失败只记审计，
  不影响已送达的回复、不置持久降级（历史不是聊天的前置条件）。
- **清理失败跨重启保留**（S4-03，闭合 R23）：群级删除（`上下文 退出`、`上下文 清空`、
  `群上下文 关闭`）失败时把"未确认恢复"的计数写入 `storage.maintenance`，启动时读回
  计数与 `DELETION_FAILED`；一次**成功**的重跑清除该群登记，最后一条被清除时解除降级
  （架构 §8.3"恢复前不重启相关能力"）。启动裁剪的失败只计本次运行、不落登记——它在
  每次启动自动重跑，没有"待维护者复跑"的状态。无存储时只保留本次运行的计数，不假装恢复。
- 群维护者命令（S2-02/S2-05）已接线：`群上下文 开启` 逐字回复附录 C.1 全文并记录
  5 分钟待确认窗口，`群上下文 确认开启` 在窗口内同群同人时写入告知版本并开启采集
  （失败重发全文并重开窗口）；`群上下文 关闭` / `上下文 清空` / `千鹤 暂停/恢复`
  执行状态变更。
- 记忆授权与命令面（S3-03/S3-04）已接线：`记忆 开启` 逐字回复附录 C.2 说明全文并记录
  待确认窗口，`记忆 确认开启` 在窗口内同群同人时写入授权行（带说明版本）；`记忆 查看`
  先回附录 C.3 提示、确认后才列出本人记录；`记忆 状态/纠正/删除/关闭` 直接读写本人数据，
  `记忆 关闭` 与 `记忆 删除全部` 等价（先撤权、再清空）。全部经 `FIXED_NOTICE` 出站，
  因此**暂停群里的删除与关闭仍然可达**（需求 §4.4）。
- `帮助`（S1-17）逐字回复 `help_notice.HELP_NOTICE_TEXT`（附录 C.6），同样是 `FIXED_NOTICE`、
  零模型调用、不写历史、不触发抽取，暂停群里也可达。
- **401/402 粘滞门禁**（S4-01 离线残差的收口）：`AUTH_FAILED`/`BALANCE_INSUFFICIENT` 首次出现
  时置 `PROVIDER_DISABLED`/`BUDGET_EXHAUSTED`，此后聊天与抽取都不再发起付费调用，只对有效 @
  回一次暂不可用提示；维护者从 `千鹤 状态` 的降级行看到原因。**解除只有进程重启/插件重载**
  （内存标志、不持久化），`_acquire_provider` 的"取到即清除"因此删除——**行为分化**：提供商
  "后配置好"不再自动恢复，需重载（docs/03 §5.2 有记录）。
- 群内出口：`千鹤 状态`、`群上下文 开启`（含确认失败时的重发全文）、`帮助`、记忆类回执与列表、
  `@` 后的聊天回复，以及**固定提示**（附件能力、失败、空回复、忙碌、暂不可用、超预算，
  S4-01；文案逐字取自 `fixed_notice.py`，见附录 C.5）。`上下文 退出/加入` 与 S2-05 的
  其余状态变更执行但**静默无回执**。
- 存储路径来自 AstrBot 插件数据目录，**延迟建库**（不写不建文件）；解析或打开失败即
  降级为"无存储"，采集与退出/加入保持关闭，`千鹤 状态` 可见。
- 去重窗口与容量是装配层常量，标注"建议参数待评审"——取值依赖仍未在线核验的 O-07。
- 固定提示（S4-01）：失败、空回复、排队、超预算与附件能力都只对**已有的有效 @** 回一次
  简短文案，固定提示不调模型、不写互动历史、不触发抽取；`llm.follow_up` 判定"该不该说话"，
  `fixed_notice.text_for` 给出文案，未分类的故障保持静默（宁可沉默，也不猜原因）。附件
  分支只回能力提示，不解析附件（需求 §4.1）。
- 记忆抽取（S3-07/S3-10）接在**回复流程之后**、同一次事件处理内 `await`（不建游离任务，
  以保住装配测试的"无新增任务"不变量）：准入看授权位（**含说明版本比对**，S3-03）、暂停、
  总开关与预算门槛；写回在单个短事务内重核修订号与授权、按"源消息 + 动作"去重。**这条
  路径没有任何出站**，失败只记审计与计数，不影响已发送的回复。
- `limits`、`budget` 与两个记忆确认窗口在 `initialize()` 时固定（窗口取
  `memory.auth_confirm_ttl_seconds`），运行时改动需重载插件；身份、允许群、维护者映射与
  金额开关每个事件重读（配置变更即时生效）。

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

from . import (
    commands,
    context_assembly,
    fixed_notice,
    health,
    help_notice,
    history,
    llm,
    memory,
    notice,
    redact,
    storage,
)
from .budget import (
    BudgetLedger,
    BudgetRefusal,
    BudgetRefused,
    PriceTable,
    Reservation,
    TokenPrice,
    UsageKind,
)
from .config import ModelPrice, Settings
from .context_buffer import BufferShape, ContextBuffer, IngestOutcome
from .control import authorize
from .dedup import ActionKind, Claim, DedupKey, DedupStore, Outcome
from .keys import BotInstanceKey, GroupKey, MemberKey, RevisionSnapshot
from .policy import Classification, MessageFacts, classify, is_trusted_scope
from .scheduler import AdmissionRefused, Deadline, DeadlineExceeded, Refusal, Scheduler
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

DELETE_ALL_HISTORY_SECONDS = 99_999_999
"""平台消息历史的"整体删除"窗口（≈3.17 年）。

**上游把默认值 86400 的语义写反了**：`delete(..., offset_sec)` 删的是"最近 offset 秒内"
（`db/sqlite.py` 的 SQL 是 `created_at >= now - offset`），默认只清最近 24 小时。
dashboard 删除会话时用的就是这个超大窗口，本插件沿用它，并在文档里记下该事实。"""

_PAYMENT_ENTRIES: tuple[tuple[redact.ErrorCode, health.Degradation], ...] = (
    (redact.ErrorCode.AUTH_FAILED, health.Degradation.PROVIDER_DISABLED),
    (redact.ErrorCode.BALANCE_INSUFFICIENT, health.Degradation.BUDGET_EXHAUSTED),
)
"""401/402 与**粘滞降级**的对应表（S4-01 离线残差的收口，架构 §8.3）：第一个元素是
触发它的故障码，第二个是要置位的降级；顺序即 `_payment_block` 的探测顺序。置位后停用
全部付费调用（聊天与抽取），直到进程重启或插件重载——`initialize()` 会重建
`HealthMonitor`，进程内没有第二个解除入口；`千鹤 状态` 的降级行负责"通知维护侧"。"""


def _payment_reason(
    code: redact.ErrorCode | None,
) -> tuple[health.Degradation, redact.ErrorCode] | None:
    """故障码 → （要置位的粘滞降级，触发它的码）；其余一律 `None`。

    只认 401/402：429/5xx/超时都是瞬态错误，把它们做成粘滞标志会让"降级永远无法
    被清除"（`health.Degradation` 的 docstring 已警戒此例）。
    """
    for entry_code, reason in _PAYMENT_ENTRIES:
        if code is entry_code:
            return reason, entry_code
    return None


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
    notice: notice.NoticeGate
    memory_consent: memory.ConsentGate
    """`记忆 开启` 的两步确认窗口（成员级）。"""
    memory_view: memory.ConsentGate
    """`记忆 查看` 的两步确认窗口（成员级）。与授权窗口是**两个实例**：互不作废。"""


@dataclass(frozen=True)
class _ChatPreparation:
    """一次聊天的装配结果：请求形态、发起时的数据修订号快照与"是否用了长期记忆"。"""

    plan: llm.LLMRequestPlan
    revision: RevisionSnapshot | None
    memory_used: bool = False
    """本轮是否真的把记忆行放进了请求（裁剪后仍有记忆块）。**只有 True 时**该轮不写回
    共享历史（S3-08/需求 §4.3"注入的记忆不能永久混进群共享历史"）。"""


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

    async def sweep(
        self,
        platform_id: str,
        trim: Callable[[str | None], tuple[dict, ...] | None],
    ) -> tuple[int, int]:
        """启动裁剪（S2-08）：枚举本平台会话，`trim` 给出新条列表时写回。

        返回 `(改动数, 失败数)`：单个会话失败不放弃其余会话；枚举本身失败向上抛。
        `trim` 是纯策略（`history.trim_stored` 的部分应用），适配器不解释它，
        因此"只裁本插件的 `_at` 成对条目"这条规则只有一处实现。
        """
        manager = self._manager()
        conversations = await manager.get_conversations(platform_id=platform_id)
        changed = 0
        failed = 0
        for conversation in conversations or ():
            try:
                trimmed = trim(getattr(conversation, "history", None))
                if trimmed is None:
                    continue
                await manager.update_conversation(
                    conversation.user_id,
                    conversation_id=conversation.cid,
                    history=list(trimmed),
                )
                changed += 1
            except Exception:
                failed += 1
        return changed, failed


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
        self.price_table: PriceTable | None = None
        """价目表的测试注入点；生产由配置键 `model_prices` 构造（S4-01，R20）。
        注入时优先于配置——离线用例据此构造"已配置金额 + 已知价格"的组合。"""

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
                prices=self._price_table(settings),
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
            notice=notice.NoticeGate(clock=self.clock),
            # 记忆的确认窗口取配置值（`auth_confirm_ttl_seconds`，默认 300 秒）；
            # 群告知窗口保持 notice.py 的模块默认——那是 B1a 的独立参数，两者不互相借用。
            memory_consent=memory.ConsentGate(
                clock=self.clock,
                window_seconds=settings.memory.auth_confirm_ttl_seconds,
            ),
            memory_view=memory.ConsentGate(
                clock=self.clock,
                window_seconds=settings.memory.auth_confirm_ttl_seconds,
            ),
        )
        self._restore_cleanup_failures(services=self.services)
        await self._sweep_history(settings)

    async def _sweep_history(self, settings: Settings) -> None:
        """启动清理（S2-08，架构 §6.3"正常重启"）：裁掉会话存储里过期或超轮的轮次。

        三条边界：`platform_id` 为空直接跳过（上游只在它非空时过滤，空串会扫到全实例
        的会话）；取不到框架会话接口（未装配、测试替身）什么都不做；失败只记审计与
        计数，**不置粘滞降级**——过期轮次在读侧本来就不会进入请求，为一次启动抖动
        停掉整进程采集换不到隐私收益（成员主动删除的失败路径另按 §8.3 置降级）。
        """
        services = self.services
        if services is None or not settings.platform_id:
            return
        store = self._history()
        sweep = getattr(store, "sweep", None)
        if sweep is None:
            return
        if self.history_store is None and not hasattr(self.context, "conversation_manager"):
            # 框架没暴露会话接口（未装配、测试替身）：**没有可清的东西，不是失败**。
            return
        try:
            changed, failed = await sweep(settings.platform_id, self._trim_plan(settings))
        except Exception:
            self._record_sweep_failure(services)
            return
        if changed:
            self._audit(services, redact.EventCategory.CONTEXT_OP, count=changed)
        if failed:
            self._record_sweep_failure(services, count=failed)

    def _trim_plan(self, settings: Settings) -> Callable[[str | None], tuple[dict, ...] | None]:
        """把窗口参数固定成纯策略交给存储适配器；时钟在启动时刻取一次。"""
        now_epoch = self._now_epoch()
        return lambda raw: history.trim_stored(
            raw,
            max_turns=settings.context.history_max_turns,
            ttl_seconds=settings.context.history_ttl_hours * 3600,
            now_epoch=now_epoch,
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
            if classification is Classification.UNSUPPORTED_ATTACHMENT:
                # "@ 后只有附件"是唯一允许回固定提示的非文本档（需求 §4.1，S4-01）；
                # 其余非文本档（未告知群、私聊、@全体、空 @、命令）零出站零模型调用。
                await self._handle_attachment(event, facts, settings, services)
                return
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
        elif intent.kind in (
            commands.CommandKind.GROUP_NOTICE_OPEN,
            commands.CommandKind.GROUP_NOTICE_CONFIRM,
        ):
            await self._handle_group_notice(event, facts, settings, services, intent)
        elif intent.kind in (
            commands.CommandKind.GROUP_CONTEXT_CLOSE,
            commands.CommandKind.CONTEXT_CLEAR,
            commands.CommandKind.PAUSE,
            commands.CommandKind.RESUME,
        ):
            await self._handle_group_control(event, facts, settings, services, intent)
        elif intent.kind in (
            commands.CommandKind.MEMORY_ENABLE,
            commands.CommandKind.MEMORY_CONFIRM,
        ):
            await self._handle_memory_consent(event, facts, settings, services, intent)
        elif intent.kind in (
            commands.CommandKind.MEMORY_LIST,
            commands.CommandKind.MEMORY_LIST_CONFIRM,
        ):
            await self._handle_memory_view(event, facts, settings, services, intent)
        elif intent.kind in (
            commands.CommandKind.MEMORY_STATUS,
            commands.CommandKind.MEMORY_CORRECT,
            commands.CommandKind.MEMORY_DELETE,
            commands.CommandKind.MEMORY_DISABLE,
        ):
            await self._handle_memory_control(event, facts, settings, services, intent)
        elif intent.kind is commands.CommandKind.HELP:
            await self._handle_help(event, facts, settings, services, intent)
        else:
            # 防御分支：`commands.parse` 认识的 kind 已全部有归属，走到这里只可能是
            # 解析器新增了 kind 而分发漏接——按旧口径记审计，不猜行为。
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
        stored = self._memory_store(services)
        if stored is None:
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
    def _memory_store(services: _Services) -> storage.Storage | None:
        """可读写的记忆库；无存储、库已失败或已置降级时返回 ``None``（架构 §8.3）。

        三个调用点共用本判定：采集准入、记忆注入、抽取准入。"无法确认退出/删除状态时
        停止受影响的动作"因此只有一处表达。
        """
        stored = services.storage
        if stored is None or stored.database.failed:
            return None
        blocking = {
            health.Degradation.MEMORY_STORE_FAILED,
            health.Degradation.DELETION_FAILED,
        }
        if services.health.degradations() & blocking:
            return None
        return stored

    @staticmethod
    def _payment_block(
        services: _Services,
    ) -> tuple[health.Degradation, redact.ErrorCode] | None:
        """当前是否被 401/402 的粘滞降级挡住付费调用；返回（原因，审计码）或 ``None``。

        与 `_memory_store` 同一模式：集合取交集，`health` 不新增判定 API。
        命中即"停用异常提供商调用 / 暂停付费请求及抽取"（架构 §8.3）。
        """
        for code, reason in _PAYMENT_ENTRIES:
            if reason in services.health.degradations():
                return reason, code
        return None

    def _mark_payment_degraded(
        self,
        services: _Services,
        reason: health.Degradation,
        code: redact.ErrorCode,
    ) -> None:
        """置位粘滞降级；**首次**置位才写一次审计（重复置位由 `_payment_block` 拦在
        调用之前，这里再挡一次是为了防御并发事件里的重复置位刷屏）。"""
        if reason in services.health.degradations():
            return
        services.health.set_degraded(reason)
        self._audit(services, redact.EventCategory.DEGRADATION, code=code)

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

    async def _handle_attachment(
        self,
        event: AstrMessageEvent,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
    ) -> None:
        """@ 后只有附件：回一次固定的文本能力提示（需求 §4.1，S4-01）。

        不解析附件、不进缓冲、不读存储、不请求模型；去重口径与聊天一致
        （拿不到稳定 `message_id` 即不处理），提示不写互动历史、不触发抽取。
        经 `FIXED_NOTICE` 出站，因此暂停群里仍然可达。
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
        outcome = await self._deliver_fixed(
            event, facts, settings, services, group, fixed_notice.ATTACHMENT_NOTICE_TEXT
        )
        services.dedup.finish(key, outcome)

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

        if self._group_state(settings, facts, services, kind=SendKind.CHAT) is not GroupState.OPEN:
            # 本群已暂停（S2-05）：在装配、取提供商与预留之前就停下，不白花一次模型调用。
            services.dedup.release(key)
            self._audit(services, redact.EventCategory.GATE_DROP)
            return

        blocked = self._payment_block(services)
        if blocked is not None:
            # 401/402 粘滞降级（S4-01）：不再发起任何付费调用，只回一次暂不可用提示。
            # 与暂停检查同一配对规则用 `finish` 而不是 `release`——该事件已被提示答复，
            # 同 message_id 重放不重答；新事件照常受理，因此成员仍能看到提示与状态行。
            _reason, code = blocked
            self._audit(services, redact.EventCategory.GATE_DROP, code=code)
            outcome = await self._deliver_fixed(
                event, facts, settings, services, group, fixed_notice.UNAVAILABLE_NOTICE_TEXT
            )
            services.dedup.finish(key, outcome)
            return

        prepared = await self._prepare_chat(event, facts, settings, services, message_id)
        if prepared is None:
            # 固定规则 + 当前输入已超预算：不预留、不调用、不发正文，只回一次缩短提示。
            self._audit(
                services, redact.EventCategory.MODEL_CALL, code=redact.ErrorCode.REQUEST_INVALID
            )
            outcome = await self._deliver_fixed(
                event, facts, settings, services, group, fixed_notice.OVER_BUDGET_NOTICE_TEXT
            )
            services.dedup.finish(key, outcome)
            return

        provider = await self._acquire_provider(event, services, facts, message_id)
        if provider is None:
            outcome = await self._deliver_fixed(
                event, facts, settings, services, group, fixed_notice.UNAVAILABLE_NOTICE_TEXT
            )
            services.dedup.finish(key, outcome)
            return
        reservation = self._reserve(services, provider)
        if isinstance(reservation, BudgetRefusal):
            # 预算耗尽或价格未知（R20）：同样只回一次暂不可用提示，不发起调用。
            outcome = await self._deliver_fixed(
                event, facts, settings, services, group, fixed_notice.UNAVAILABLE_NOTICE_TEXT
            )
            services.dedup.finish(key, outcome)
            return

        try:
            answer = await services.scheduler.submit_chat(
                group,
                lambda deadline: self._ask(provider, prepared.plan, settings, services, deadline),
            )
        except AdmissionRefused as refused:
            # 从未开始：额度可退。排队满或等待超时对有效 @ 回一次忙碌提示；
            # 调度器已关闭（插件正在停止）不尝试出站，条目释放以便重来。
            services.budget.cancel(reservation)
            self._audit(services, redact.EventCategory.QUEUE_DEPTH, code=redact.ErrorCode.QUEUE_FULL)
            if refused.reason in (Refusal.QUEUE_FULL, Refusal.WAIT_TIMEOUT):
                outcome = await self._deliver_fixed(
                    event, facts, settings, services, group, fixed_notice.BUSY_NOTICE_TEXT
                )
                services.dedup.finish(key, outcome)
            else:
                services.dedup.release(key)
            return
        except DeadlineExceeded:
            # 已开始但无结果：缺失 usage 按预留估算入账，绝不记零；回一次失败提示。
            services.budget.settle(reservation, None)
            self._audit(
                services,
                redact.EventCategory.MODEL_CALL,
                code=redact.ErrorCode.PROVIDER_UNAVAILABLE,
            )
            outcome = await self._deliver_fixed(
                event, facts, settings, services, group, fixed_notice.FAILURE_NOTICE_TEXT
            )
            services.dedup.finish(key, outcome)
            return
        except asyncio.CancelledError:
            services.budget.settle(reservation, None)
            raise

        services.budget.settle(reservation, answer.usage)
        if answer.usage is not None:
            self._audit(services, redact.EventCategory.TOKEN_USAGE, tokens=answer.usage)
        if answer.kind is not llm.AnswerKind.TEXT:
            # S4-01：`llm.follow_up` 判定该不该说话，文案由 `fixed_notice` 给出；
            # 未分类的故障没有文案，保持静默（既不猜原因，也不把它说成可重试）。
            # 401/402 另外置**粘滞降级**：后续付费调用被 `_payment_block` 拦下，
            # 本条回复的文案维持既有映射不变。
            hit = _payment_reason(answer.code)
            if hit is not None:
                self._mark_payment_degraded(services, *hit)
            notice_text = (
                fixed_notice.text_for(answer.code)
                if llm.follow_up(answer) is llm.FollowUp.FIXED_NOTICE
                else None
            )
            if notice_text is None:
                services.dedup.finish(key, Outcome.COMPLETED)
                return
            outcome = await self._deliver_fixed(
                event, facts, settings, services, group, notice_text
            )
            services.dedup.finish(key, outcome)
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
        if history.should_record(memory_assisted=prepared.memory_used, delivered=sent):
            # 记忆辅助轮（本轮请求里真的带了记忆块）不写回共享历史（S3-08，需求 §4.3）。
            await self._record_history(event, facts, settings, services, answer.text, prepared.revision)
        services.dedup.finish(key, outcome)
        await self._extract_memory(
            facts,
            settings,
            services,
            provider=provider,
            message_id=message_id,
            revision=prepared.revision,
        )

    async def _prepare_chat(
        self,
        event: AstrMessageEvent,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
        message_id: str,
    ) -> _ChatPreparation | None:
        """装配一次聊天请求：修订快照 + 记忆块 + 动态材料 + 合格历史；超预算返回 `None`。

        **修订号快照先于一切读取**（S3-09"修订校验贯穿读"）：快照之后发生的退出、清空、
        暂停或记忆纠正都会被发送前复核发现并丢弃；快照之前发生的变更则体现在随后读到的
        记忆与材料里——两个方向都不会把已撤回的数据放进请求。

        记忆块与材料块都只进 `extra_user_content_parts` 并标记为临时，历史只进 `contexts`，
        三者都不进 system；预算不够时的裁剪顺序见 `trim_to_budget`（先丢群聊材料）。
        """
        group = self._group_key(facts)
        revision = self._revision_snapshot(services, group, facts.sender_id)
        memory_block = self._memory_block(services, group, facts.sender_id)
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
            memory_block=memory_block,
        )
        if trimmed is None:
            return None
        parts: list[object] = []
        for text in (trimmed.memory, trimmed.materials):
            if text is not None:
                # part 级临时标记（K6）：框架若把这一轮交给持久化，注入片段会被剔除。
                parts.append(TextPart(text=text).mark_as_temp())
        plan = context_assembly.build_chat_plan(
            user_text=facts.direct_text,
            max_retries=settings.limits.max_retries,
            dynamic_parts=tuple(parts),
            contexts=trimmed.contexts,
        )
        return _ChatPreparation(
            plan=plan,
            revision=revision,
            memory_used=trimmed.memory is not None,
        )

    def _memory_block(
        self,
        services: _Services,
        group: GroupKey,
        member_id: str,
    ) -> context_assembly.TextBlock | None:
        """读本人记忆并渲染成注入块；未授权、无存储或读失败一律**不注入**。

        查询键由可信事件元数据构造（群 + 当前发言者），没有任何"按 ID 查他人"的入口。
        库不可用（无存储、库失败或已降级）时**不注入**；读失败同样只退回普通聊天，
        不置降级也不阻断回复——记忆不是聊天的前置条件（S3-11，与 R21 同向）。
        """
        stored = self._memory_store(services)
        if stored is None:
            return None
        member = MemberKey(group, member_id)
        try:
            if not self._memory_authorized(stored, member):
                return None
            facts = memory.retrieve(stored.memories, member)
        except storage.StorageFailure:
            return None
        return memory.render_block(facts)

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
        """一次模型请求（聊天与抽取共用）：直接调用提供商，最多重试 1 次且**共用同一个期限**。"""
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
        """取当前提供商；任何失败都收敛为 None（绝不抛出到处理器外）。

        取不到提供商的**行为分化**（S4-01 离线残差）：`PROVIDER_DISABLED` 现在只在重启/
        重载时解除，因此"提供商后配置好"不再自动恢复——门禁在任何取提供商动作之前，
        标志置位期间这里本就不可达；保留旧的"取到即清除"只会制造第二种语义。
        """
        try:
            provider = await self.context.get_using_provider_async(event.unified_msg_origin)
        except Exception:
            provider = None
        if provider is None:
            self._mark_payment_degraded(
                services,
                health.Degradation.PROVIDER_DISABLED,
                redact.ErrorCode.UNCLASSIFIED,
            )
            self._audit(
                services,
                redact.EventCategory.MODEL_CALL,
                code=redact.ErrorCode.UNCLASSIFIED,
                parts=(facts.group_id, message_id),
            )
            return None
        return provider

    def _reserve(self, services: _Services, provider: object) -> Reservation | BudgetRefusal:
        """预留额度；被拒绝时返回原因（调用方据此回一次暂不可用提示，R20）。"""
        try:
            return services.budget.reserve(UsageKind.CHAT, self._model_name(provider))
        except BudgetRefused as refused:
            self._audit(
                services,
                redact.EventCategory.GATE_DROP,
                code=_budget_code(refused.reason),
            )
            return refused.reason

    def _price_table(self, settings: Settings) -> PriceTable:
        """价目表来源（S4-01，R20）：测试注入点优先，否则取配置的 `model_prices`。

        空映射＝价格未知：已配金额时 `reserve` 保守拒绝（群内回一次暂不可用提示，
        `千鹤 状态` 的预算行同时给出原因）。模型 ID 与提供商返回值逐字比对——
        不做大小写或别名归一，宁可拒绝也不猜用户想写哪个模型。
        """
        if self.price_table is not None:
            return self.price_table
        prices: dict[str, TokenPrice] = {
            model: TokenPrice(price.input_per_million, price.output_per_million)
            for model, price in settings.budget.model_prices.items()
        }
        return PriceTable(prices)

    @staticmethod
    def _model_name(provider: object) -> str:
        try:
            name = provider.get_model()
        except Exception:
            return UNKNOWN_MODEL
        return name if isinstance(name, str) and name else UNKNOWN_MODEL

    # ---- 记忆抽取（S3-07/S3-10） ----

    async def _extract_memory(
        self,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
        *,
        provider: object,
        message_id: str,
        revision: RevisionSnapshot | None,
    ) -> None:
        """回复流程之后运行的后台抽取（S3-07/S3-10）。

        **这条路径没有任何出站**：不调用 `_deliver`，也没有任何文案——"不产生已记住通知"
        因此是结构事实。失败只记审计与计数，绝不抛出到处理器外：已送达的聊天不受影响。

        准入的四种拒绝都是**静默且常态**的（未授权、已暂停、总开关关闭、预算未配置），
        所以不逐条写审计——它们是默认状态而非异常，逐条记录只会把日志淹掉；维护者从
        `千鹤 状态` 的预算行与配置即可看到。
        """
        if not settings.memory.extraction_enabled or not services.budget.extraction_allowed:
            # 默认关闭（总开关关闭或预算未配置）：连存储都不读。判定仍由 `memory.admit`
            # 收口，这里只是避免在"从不会抽取"的部署里每轮多读两次库。
            return
        if self._payment_block(services) is not None:
            # 401/402 粘滞降级（S4-01）：付费调用已停用，抽取不得例外。静默——标志是在
            # 置位的那一刻记的 DEGRADATION 审计，这里逐条记录只会把日志淹掉。
            return
        stored = self._memory_store(services)
        if stored is None:
            return  # 无存储或已降级 = 记忆子系统不可用（R21、S3-11），不是错误
        group = self._group_key(facts)
        member = MemberKey(group, facts.sender_id)
        admission = memory.admit(
            source_text=facts.direct_text,
            authorized=self._memory_authorized(stored, member),
            paused=self._group_paused(stored, group),
            extraction_enabled=settings.memory.extraction_enabled,
            budget_allows=services.budget.extraction_allowed,
        )
        if not admission.allowed:
            return

        plan = memory.build_request(
            source_text=admission.source_text,
            max_retries=settings.limits.max_retries,
        )
        reservation = self._reserve_extraction(services, provider)
        if reservation is None:
            return

        try:
            result = await services.scheduler.submit_extraction(
                group,
                lambda deadline: self._ask_extraction(provider, plan, settings, services, deadline),
            )
        except AdmissionRefused:
            # 从未开始：额度可退，事件可重来（与聊天路径同一配对规则）。
            services.budget.cancel(reservation)
            self._audit(services, redact.EventCategory.MEMORY_OP, code=redact.ErrorCode.QUEUE_FULL)
            return
        except DeadlineExceeded:
            services.budget.settle(reservation, None)
            self._mark_extraction_health(services, failed=True)
            self._audit(
                services,
                redact.EventCategory.MEMORY_OP,
                code=redact.ErrorCode.PROVIDER_UNAVAILABLE,
            )
            return
        except asyncio.CancelledError:
            services.budget.settle(reservation, None)
            raise

        services.budget.settle(reservation, result.usage)
        if result.usage is not None:
            self._audit(services, redact.EventCategory.TOKEN_USAGE, tokens=result.usage)
        if memory.counts_as_failure(result):
            self._mark_extraction_health(services, failed=True)
            # 401/402 的粘滞降级在抽取侧同样置位（架构 §8.3：停用提供商调用 / 暂停付费
            # 请求**及抽取**）——聊天成功后仍有并发事件打付费调用，抽取不得绕过。
            hit = _payment_reason(result.code)
            if hit is not None:
                self._mark_payment_degraded(services, *hit)
        else:
            self._mark_extraction_health(services, failed=False)
        self._write_memory(
            stored,
            services,
            settings,
            member,
            result,
            message_id=message_id,
            revision=revision,
        )

    def _write_memory(
        self,
        stored: storage.Storage,
        services: _Services,
        settings: Settings,
        member: MemberKey,
        result: memory.ExtractionResult,
        *,
        message_id: str,
        revision: RevisionSnapshot | None,
    ) -> None:
        """写回候选：**单个短事务**内重核修订号与授权、按来源去重（S3-07）。

        修订号不一致、授权已撤回、来源已处理——三种都是"什么都不写"的确定性结论：只记
        审计，**不重试、不重做**（S3-09 的机制面）。
        """
        plan = memory.WritePlan(
            member=member,
            source_message_id=message_id,
            source_action=ActionKind.MEMORY_EXTRACT.value,
            source_retention_seconds=int(DEDUP_WINDOW_SECONDS),
            limits=self._memory_limits(settings),
            candidates=memory.plan_candidates(result),
            expected_revision=revision,
        )
        try:
            with stored.database.transaction() as connection:
                outcome = memory.write_back(connection, plan, at=self._now_epoch())
        except storage.StorageFailure:
            self._audit(
                services,
                redact.EventCategory.MEMORY_OP,
                code=redact.ErrorCode.MEMORY_STORE_FAILED,
            )
            return
        if outcome.outcome is memory.WriteOutcome.WRITTEN:
            self._audit(services, redact.EventCategory.MEMORY_OP, count=outcome.written)
        elif outcome.outcome is not memory.WriteOutcome.NOTHING_TO_WRITE:
            self._audit(
                services,
                redact.EventCategory.MEMORY_OP,
                code=redact.ErrorCode.REVISION_CHANGED
                if outcome.outcome is memory.WriteOutcome.REVISION_CHANGED
                else None,
            )

    @staticmethod
    def _mark_extraction_health(services: _Services, *, failed: bool) -> None:
        """抽取的计数与可见标志（S3-11）：失败置位、成功清除。

        **标志不阻断抽取**——它是"最近一次抽取没成功"的可见标记，若拿它当门禁就永远无法
        清除（与 `PROVIDER_DISABLED` 同例：由成功来清）。阈值判断不在本模块，`health` 只计数。
        """
        if failed:
            services.health.record_extraction_failure()
            services.health.set_degraded(health.Degradation.EXTRACTION_SUSPENDED)
        else:
            services.health.record_extraction_success()
            services.health.clear_degraded(health.Degradation.EXTRACTION_SUSPENDED)

    def _reserve_extraction(self, services: _Services, provider: object) -> Reservation | None:
        """抽取的预留；被拒绝即不发起调用（与聊天同一顺序：先预留、再调度）。"""
        try:
            return services.budget.reserve(UsageKind.EXTRACTION, self._model_name(provider))
        except BudgetRefused as refused:
            self._audit(
                services,
                redact.EventCategory.MEMORY_OP,
                code=_budget_code(refused.reason),
            )
            return None

    async def _ask_extraction(
        self,
        provider: object,
        plan: llm.LLMRequestPlan,
        settings: Settings,
        services: _Services,
        deadline: Deadline,
    ) -> memory.ExtractionResult:
        """一次抽取请求：与聊天共用 `_ask`（同一期限、同一重试策略），只换解释方式。"""
        return memory.from_answer(await self._ask(provider, plan, settings, services, deadline))

    @staticmethod
    def _memory_authorized(stored: storage.Storage, member: MemberKey) -> bool:
        """读授权位**并比对说明版本**（S3-03）；读不到或版本过期一律按未授权处理。

        版本比对放在这里（消费点）而不是 `memory.pipeline.admit`：`CONSENT_VERSION` 是
        进程内不变的常量，行一旦以当前版本写入就永远匹配；旧版本的残留行只会在升级重启后
        出现，那时准入已经在调用侧被拒。后台写路径因此与本判定共用同一谓词。
        """
        try:
            return memory.authorization_is_current(stored.memories.state(member))
        except storage.StorageFailure:
            return False

    @staticmethod
    def _group_paused(stored: storage.Storage, group: GroupKey) -> bool:
        """本群是否已暂停；**读不到按已暂停处理**（架构 §8.3：不用"未知"换放行）。"""
        try:
            return stored.groups.policy(group).paused
        except storage.StorageFailure:
            return True

    @staticmethod
    def _memory_limits(settings: Settings) -> storage.MemoryLimits:
        """把配置里的条数与天数转成存储层要的参数（天数 → 秒在这里完成）。"""
        return storage.MemoryLimits(
            max_records=settings.memory.max_records_per_member,
            ttl_seconds=settings.memory.ttl_days * 24 * 3600,
        )

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
        lines = list(health.format_report(snapshot, configured=settings.identity_configured))
        lines.append(self._describe_group(services, facts))
        report = "\n".join(lines)
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

    def _describe_group(self, services: _Services, facts: MessageFacts) -> str:
        """群上下文状态行（**维护者可见新文案，待评审**）：只读，绝不建行。

        无存储或读失败时按"未告知"显示——降级原因由 `health` 的既有行解释。
        """
        stored = services.storage
        if stored is None or stored.database.failed:
            return notice.describe_policy(notice_version="", context_enabled=False, paused=False)
        try:
            policy = stored.groups.policy(self._group_key(facts))
        except storage.StorageFailure:
            return notice.describe_policy(notice_version="", context_enabled=False, paused=False)
        return notice.describe_policy(
            notice_version=policy.notice_version,
            context_enabled=policy.context_enabled,
            paused=policy.paused,
        )

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

    # ---- 控制：帮助（S1-17） ----

    async def _handle_help(
        self,
        event: AstrMessageEvent,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
        intent: commands.CommandIntent,
    ) -> None:
        """成员命令：逐字回复附录 C.6 的使用说明（S1-17）。

        零模型调用、不写互动历史、不触发抽取；经 `FIXED_NOTICE` 出站，因此暂停群里
        仍然可达（与 `千鹤 状态`、记忆类回执同例）。文案不含任何用户数据，不需要
        发送前重读修订号。
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
            # 可信范围内的成员即可（MEMBER 档）；拒绝不发提示，与既有命令同口径。
            services.dedup.release(key)
            self._audit(services, redact.EventCategory.IGNORED)
            return
        outcome = await self._deliver_fixed(
            event, facts, settings, services, group, help_notice.HELP_NOTICE_TEXT
        )
        services.dedup.finish(key, outcome)

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
                await self._clear_group_history(event, services, group)
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

    # ---- 控制：群告知两步开启（S2-02） ----

    async def _handle_group_notice(
        self,
        event: AstrMessageEvent,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
        intent: commands.CommandIntent,
    ) -> None:
        """`群上下文 开启` / `群上下文 确认开启`：两步告知（B1a），文案逐字取自附录 C.1。

        第一步只回复全文并记录待确认窗口（写入面不发生）；第二步在窗口内同群同人才写入
        告知版本并开启采集。窗口不满足时**重发全文并重开窗口**——B1a 的"要求重新发起"
        因此不需要任何新文案。
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

        if not self._notice_version_ready(settings, services):
            # 部署声明的文案版本与插件内文本不一致：不展示、不写入、不回（回执文案待审），
            # 只留审计——否则会出现"写了版本 A 却按版本 B 判定"的永不开启。
            services.dedup.finish(key, Outcome.COMPLETED)
            return

        if intent.kind is commands.CommandKind.GROUP_NOTICE_OPEN:
            outcome = await self._deliver_notice(
                event, facts, settings, services, group, actor_id=facts.sender_id
            )
            services.dedup.finish(key, outcome)
            return

        confirmed = services.notice.confirm(
            group,
            actor_id=facts.sender_id,
            version=settings.notice_version,
        )
        if confirmed is notice.ConfirmOutcome.CONFIRMED:
            outcome = self._record_notice(group, facts, settings, services, message_id)
            services.dedup.finish(key, outcome)
            return

        # 过期 / 换人 / 无待确认 / 版本不符：一律重发全文并重开窗口（语义完全相同）。
        outcome = await self._deliver_notice(
            event, facts, settings, services, group, actor_id=facts.sender_id
        )
        services.dedup.finish(key, outcome)

    def _notice_version_ready(self, settings: Settings, services: _Services) -> bool:
        """部署配置的告知版本必须与插件内文案版本一致，否则拒绝（fail-closed）。

        不一致时宁可什么都不做：写入 A 却按 B 判定会让采集永远打不开，而群内文案
        （告知全文）此时与实际采集行为不符，不能靠猜。
        """
        if settings.notice_version == notice.NOTICE_VERSION:
            return True
        self._audit(
            services,
            redact.EventCategory.CONTEXT_OP,
            code=redact.ErrorCode.REQUEST_INVALID,
        )
        return False

    def _record_notice(
        self,
        group: GroupKey,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
        message_id: str,
    ) -> Outcome:
        """写入告知版本、时间与告知人（幂等）；无存储或写失败都按"未发生"处理。"""
        stored = services.storage
        if stored is None or stored.database.failed:
            self._storage_failed(services, facts, message_id, category=redact.EventCategory.CONTEXT_OP)
            return Outcome.COMPLETED
        try:
            stored.groups.record_notice_confirmed(
                group,
                version=settings.notice_version,
                actor_id=facts.sender_id,
            )
        except storage.StorageFailure:
            self._storage_failed(services, facts, message_id, category=redact.EventCategory.CONTEXT_OP)
            return Outcome.COMPLETED
        self._audit(services, redact.EventCategory.CONTEXT_OP)
        return Outcome.COMPLETED

    async def _deliver_notice(
        self,
        event: AstrMessageEvent,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
        group: GroupKey,
        *,
        actor_id: str,
    ) -> Outcome:
        """回复告知全文；**只有送达成功才记录待确认窗口**（B1a 步骤 1）。"""
        outcome, sent = await self._deliver(
            event,
            facts,
            settings,
            services,
            group,
            notice.NOTICE_TEXT,
            kind=SendKind.FIXED_NOTICE,
            llm_invoked=False,
        )
        if sent:
            services.notice.begin(group, actor_id=actor_id, version=settings.notice_version)
        self._audit(services, redact.EventCategory.CONTEXT_OP)
        return outcome

    # ---- 控制：关闭 / 清空 / 暂停 / 恢复（S2-05） ----

    async def _handle_group_control(
        self,
        event: AstrMessageEvent,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
        intent: commands.CommandIntent,
    ) -> None:
        """`群上下文 关闭`、`上下文 清空`、`千鹤 暂停/恢复`：状态变更，**静默无回执**。

        顺序：先写状态（失败即停采）→ 再清缓冲 → 最后清受影响群历史。"暂停时保留成员
        删除通道"由结构保证：`上下文 退出` 只读成员状态，不看 `paused`。
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
        available = stored is not None and not stored.database.failed

        if intent.kind is commands.CommandKind.CONTEXT_CLEAR:
            # 清空是隐私动作：存储不可用时仍清内存与会话历史，只跳过修订号（在途失效收窄）。
            if available:
                try:
                    stored.groups.bump_revision(group)
                except storage.StorageFailure:
                    self._storage_failed(services, facts, message_id, category=redact.EventCategory.CONTEXT_OP)
            else:
                self._audit(
                    services,
                    redact.EventCategory.CONTEXT_OP,
                    parts=(facts.group_id, message_id),
                    code=redact.ErrorCode.MEMORY_STORE_FAILED,
                )
            services.context_buffer.clear_group(group)
            await self._clear_group_history(event, services, group)
            self._audit(services, redact.EventCategory.CONTEXT_OP)
            services.dedup.finish(key, Outcome.COMPLETED)
            return

        if not available:
            self._storage_failed(services, facts, message_id, category=redact.EventCategory.CONTEXT_OP)
            services.dedup.finish(key, Outcome.COMPLETED)
            return

        try:
            if intent.kind is commands.CommandKind.GROUP_CONTEXT_CLOSE:
                stored.groups.set_context_enabled(
                    group,
                    enabled=False,
                    required_notice_version=settings.notice_version,
                )
                services.context_buffer.clear_group(group)
                await self._clear_group_history(event, services, group)
            elif intent.kind is commands.CommandKind.PAUSE:
                self._set_paused(stored, group, paused=True)
                services.context_buffer.clear_group(group)
            else:  # RESUME
                self._set_paused(stored, group, paused=False)
        except storage.PolicyRefused:
            # 本群没有策略行（从未告知）或版本不匹配：不生效，按忽略记录（不发提示）。
            services.dedup.finish(key, Outcome.COMPLETED)
            self._audit(services, redact.EventCategory.IGNORED)
            return
        except storage.StorageFailure:
            self._storage_failed(services, facts, message_id, category=redact.EventCategory.CONTEXT_OP)
            services.dedup.finish(key, Outcome.COMPLETED)
            return

        self._audit(services, redact.EventCategory.CONTEXT_OP)
        services.dedup.finish(key, Outcome.COMPLETED)

    @staticmethod
    def _set_paused(stored: storage.Storage, group: GroupKey, *, paused: bool) -> None:
        """暂停 / 恢复；**没有策略行时先建行**——从未开启采集的群也必须能静音。

        `bump_revision` 建出的行是"未告知 + 未开启"，因此不会让采集意外打开。
        """
        try:
            stored.groups.set_paused(group, paused=paused)
        except storage.PolicyRefused:
            stored.groups.bump_revision(group)
            stored.groups.set_paused(group, paused=paused)

    # ---- 控制：记忆授权与命令面（S3-03/S3-04） ----

    async def _handle_memory_consent(
        self,
        event: AstrMessageEvent,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
        intent: commands.CommandIntent,
    ) -> None:
        """`记忆 开启` / `记忆 确认开启`：成员级两步授权（S3-03），文案逐字取自附录 C.2。

        第一步只回复说明全文并记录待确认窗口（写入面不发生，也不建库）；第二步在窗口内
        同群同人才写入授权行（带说明版本）。窗口不满足时**重发全文并重开窗口**——与
        `群上下文 确认开启` 的失败语义完全一致，因此不需要任何额外失败文案。
        """
        group = self._group_key(facts)
        member = MemberKey(group, facts.sender_id)
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

        if intent.kind is commands.CommandKind.MEMORY_ENABLE:
            services.dedup.finish(key, await self._show_consent(event, facts, settings, services, group, member))
            return

        confirmed = services.memory_consent.confirm(
            member,
            actor_id=facts.sender_id,
            version=memory.CONSENT_VERSION,
        )
        if confirmed is not memory.ConsentOutcome.CONFIRMED:
            services.dedup.finish(key, await self._show_consent(event, facts, settings, services, group, member))
            return

        stored = self._memory_store(services)
        if stored is None:
            services.dedup.finish(
                key, await self._reply_memory_unavailable(event, facts, settings, services, group, message_id)
            )
            return
        try:
            stored.memories.set_authorized(member, authorized=True, auth_version=memory.CONSENT_VERSION)
        except storage.StorageFailure:
            services.dedup.finish(
                key, await self._reply_memory_unavailable(event, facts, settings, services, group, message_id)
            )
            return
        services.dedup.finish(
            key, await self._reply_memory(event, facts, settings, services, group, memory.CONFIRM_SUCCESS_TEXT)
        )

    async def _handle_memory_view(
        self,
        event: AstrMessageEvent,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
        intent: commands.CommandIntent,
    ) -> None:
        """`记忆 查看` / `记忆 查看 确认`：两步查看（S3-04），先提示群内可见再列出。

        第一步**先确认授权再回提示**（未开启时不引导查看）；第二步**重新读一次**再列出——
        窗口与读取之间可能已经撤回或过期，列出陈旧内容等于绕过撤回。
        """
        group = self._group_key(facts)
        member = MemberKey(group, facts.sender_id)
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

        snapshot = self._memory_snapshot(services, member)
        if snapshot is None:
            services.dedup.finish(
                key, await self._reply_memory_unavailable(event, facts, settings, services, group, message_id)
            )
            return
        authorized, _records = snapshot
        if not authorized:
            services.dedup.finish(
                key, await self._reply_memory(event, facts, settings, services, group, memory.STATUS_CLOSED_TEXT)
            )
            return

        if intent.kind is commands.CommandKind.MEMORY_LIST:
            services.dedup.finish(key, await self._show_view_notice(event, facts, settings, services, group, member))
            return

        confirmed = services.memory_view.confirm(
            member,
            actor_id=facts.sender_id,
            version=memory.VIEW_NOTICE_VERSION,
        )
        if confirmed is not memory.ConsentOutcome.CONFIRMED:
            services.dedup.finish(key, await self._show_view_notice(event, facts, settings, services, group, member))
            return

        snapshot = self._memory_snapshot(services, member)
        if snapshot is None:
            services.dedup.finish(
                key, await self._reply_memory_unavailable(event, facts, settings, services, group, message_id)
            )
            return
        authorized, records = snapshot
        if not authorized:
            services.dedup.finish(
                key, await self._reply_memory(event, facts, settings, services, group, memory.STATUS_CLOSED_TEXT)
            )
            return
        services.dedup.finish(
            key,
            await self._reply_memory(
                event, facts, settings, services, group, memory.list_body(records), count=len(records)
            ),
        )

    async def _handle_memory_control(
        self,
        event: AstrMessageEvent,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
        intent: commands.CommandIntent,
    ) -> None:
        """`记忆 状态/纠正/删除/关闭/删除全部`：直接读写本人数据（S3-04）。

        `记忆 关闭` 与 `记忆 删除全部` 是**同一 kind**：需求 §4.4 与附录 C.2 都写作
        "均撤回授权并清除已有记忆"，因此等价实现，顺序固定为**先撤权、再清空**——反过来
        会留下"仍授权、无记录"的窗口，抽取可能立刻写回新记录。该分支**不要求当前已授权**：
        对"已撤回但事实残留"的成员，关闭与删除必须仍然可用。
        """
        group = self._group_key(facts)
        member = MemberKey(group, facts.sender_id)
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

        if intent.kind is commands.CommandKind.MEMORY_STATUS:
            snapshot = self._memory_snapshot(services, member)
            if snapshot is None:
                services.dedup.finish(
                    key, await self._reply_memory_unavailable(event, facts, settings, services, group, message_id)
                )
                return
            authorized, records = snapshot
            text = memory.status_line(
                authorized=authorized,
                count=len(records),
                limit=settings.memory.max_records_per_member,
                days=settings.memory.ttl_days,
            )
            services.dedup.finish(
                key,
                await self._reply_memory(
                    event, facts, settings, services, group, text, count=len(records) if authorized else None
                ),
            )
            return

        if intent.kind is commands.CommandKind.MEMORY_CORRECT:
            stored = self._memory_store(services)
            if stored is None:
                services.dedup.finish(
                    key, await self._reply_memory_unavailable(event, facts, settings, services, group, message_id)
                )
                return
            record_id = int(intent.record_id)
            content = memory.prepare_source(intent.argument)
            if content is None:
                services.dedup.finish(
                    key, await self._reply_memory(event, facts, settings, services, group, memory.CORRECT_REFUSED_TEXT)
                )
                return
            try:
                updated = stored.memories.correct(member, record_id, content=content)
            except ValueError:
                # 编号超出存储层上界：与"没有找到"同一回执（不区分原因）。
                updated = None
            except storage.StorageFailure:
                services.dedup.finish(
                    key, await self._reply_memory_unavailable(event, facts, settings, services, group, message_id)
                )
                return
            text = memory.record_updated(record_id) if updated is not None else memory.record_missing(record_id)
            services.dedup.finish(key, await self._reply_memory(event, facts, settings, services, group, text))
            return

        if intent.kind is commands.CommandKind.MEMORY_DELETE:
            stored = self._memory_store(services)
            if stored is None:
                services.dedup.finish(
                    key, await self._reply_memory_unavailable(event, facts, settings, services, group, message_id)
                )
                return
            record_id = int(intent.record_id)
            try:
                deleted = stored.memories.delete(member, record_id)
            except ValueError:
                deleted = False
            except storage.StorageFailure:
                services.dedup.finish(
                    key, await self._reply_memory_unavailable(event, facts, settings, services, group, message_id)
                )
                return
            text = memory.record_deleted(record_id) if deleted else memory.record_missing(record_id)
            services.dedup.finish(key, await self._reply_memory(event, facts, settings, services, group, text))
            return

        # 记忆 关闭 / 记忆 删除全部（等价）。
        stored = self._memory_store(services)
        if stored is None:
            services.dedup.finish(
                key, await self._reply_memory_unavailable(event, facts, settings, services, group, message_id)
            )
            return
        try:
            stored.memories.set_authorized(member, authorized=False, auth_version=memory.CONSENT_VERSION)
            stored.memories.clear(member)
        except storage.StorageFailure:
            # 撤权已生效但清空未完成时也如实回这一句：**不虚报已清除**（架构 §8.3）。
            services.dedup.finish(
                key, await self._reply_memory_unavailable(event, facts, settings, services, group, message_id)
            )
            return
        services.dedup.finish(
            key, await self._reply_memory(event, facts, settings, services, group, memory.DISABLE_SUCCESS_TEXT)
        )

    def _memory_snapshot(
        self,
        services: _Services,
        member: MemberKey,
    ) -> tuple[bool, tuple[storage.MemoryFact, ...]] | None:
        """(授权是否当前有效, 未过期记录)；无存储、库降级或读失败一律返回 `None`。

        "读失败"与"未授权"刻意分开：库坏了要如实说"暂时不可用"，不能显示成"你没开"
        （架构 §8.3：不以"未知"换放行）。
        """
        stored = self._memory_store(services)
        if stored is None:
            return None
        try:
            authorized = memory.authorization_is_current(stored.memories.state(member))
            records = stored.memories.facts(member) if authorized else ()
        except storage.StorageFailure:
            return None
        return authorized, records

    async def _show_consent(
        self,
        event: AstrMessageEvent,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
        group: GroupKey,
        member: MemberKey,
    ) -> Outcome:
        """回复授权说明全文；**送达成功才记录待确认窗口**（S3-03 步骤 1）。"""
        outcome, sent = await self._deliver_memory(event, facts, settings, services, group, memory.CONSENT_TEXT)
        if sent:
            services.memory_consent.begin(
                member,
                actor_id=facts.sender_id,
                version=memory.CONSENT_VERSION,
            )
        self._audit(services, redact.EventCategory.MEMORY_OP)
        return outcome

    async def _show_view_notice(
        self,
        event: AstrMessageEvent,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
        group: GroupKey,
        member: MemberKey,
    ) -> Outcome:
        """回复查看提示；**送达成功才记录待确认窗口**（S3-04 步骤 1）。"""
        outcome, sent = await self._deliver_memory(event, facts, settings, services, group, memory.VIEW_NOTICE_TEXT)
        if sent:
            services.memory_view.begin(
                member,
                actor_id=facts.sender_id,
                version=memory.VIEW_NOTICE_VERSION,
            )
        self._audit(services, redact.EventCategory.MEMORY_OP)
        return outcome

    async def _deliver_memory(
        self,
        event: AstrMessageEvent,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
        group: GroupKey,
        text: str,
    ) -> tuple[Outcome, bool]:
        """记忆类回执的统一出口：与 `千鹤 状态` 同例（固定提示、不走模型）。

        `FIXED_NOTICE` 不受暂停约束（`_group_state`），因此暂停期里成员的删除与关闭
        仍然可达——需求 §4.4"暂停时保留成员删除通道"。
        """
        return await self._deliver(
            event,
            facts,
            settings,
            services,
            group,
            text,
            kind=SendKind.FIXED_NOTICE,
            llm_invoked=False,
        )

    async def _deliver_fixed(
        self,
        event: AstrMessageEvent,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
        group: GroupKey,
        text: str,
    ) -> Outcome:
        """固定提示的统一出口（S4-01）：不调模型、不带修订号、不写历史。

        文案本身不含任何用户数据，因此不需要发送前重读修订号；`FIXED_NOTICE`
        不受暂停约束（`_group_state`），与记忆类回执同例。
        """
        outcome, _ = await self._deliver(
            event,
            facts,
            settings,
            services,
            group,
            text,
            kind=SendKind.FIXED_NOTICE,
            llm_invoked=False,
        )
        return outcome

    async def _reply_memory(
        self,
        event: AstrMessageEvent,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
        group: GroupKey,
        text: str,
        *,
        count: int | None = None,
    ) -> Outcome:
        """回一条记忆类消息并登记审计；返回去重结论（门控丢弃与送达同为 COMPLETED）。"""
        outcome, _ = await self._deliver_memory(event, facts, settings, services, group, text)
        self._audit(services, redact.EventCategory.MEMORY_OP, count=count)
        return outcome

    async def _reply_memory_unavailable(
        self,
        event: AstrMessageEvent,
        facts: MessageFacts,
        settings: Settings,
        services: _Services,
        group: GroupKey,
        message_id: str,
    ) -> Outcome:
        """库不可用或写失败：置降级 + 审计，并**如实回复"没有完成"**（架构 §8.3）。"""
        self._storage_failed(services, facts, message_id, category=redact.EventCategory.MEMORY_OP)
        return await self._reply_memory(event, facts, settings, services, group, memory.UNAVAILABLE_TEXT)

    def _storage_failed(
        self,
        services: _Services,
        facts: MessageFacts,
        message_id: str,
        *,
        category: redact.EventCategory,
    ) -> None:
        """写路径的公共降级：置持久降级 + 留审计，调用方按"未发生"处理。"""
        services.health.set_degraded(health.Degradation.MEMORY_STORE_FAILED)
        self._audit(
            services,
            category,
            parts=(facts.group_id, message_id),
            code=redact.ErrorCode.MEMORY_STORE_FAILED,
        )

    async def _clear_group_history(
        self,
        event: AstrMessageEvent,
        services: _Services,
        group: GroupKey,
    ) -> None:
        """清理该群互动历史；失败只登记降级与计数（含持久登记），不重试、不虚报。

        成功则把该群从**待维护登记**里移除：这是"已恢复"的唯一来源（S4-03/R23）。
        """
        cleaner = self.history_cleaner or self._delete_group_history
        try:
            await cleaner(event.unified_msg_origin)
        except Exception:
            services.health.set_degraded(health.Degradation.DELETION_FAILED)
            self._record_cleanup_failure(services, group)
            return
        self._clear_cleanup_failure(services, group)

    def _record_cleanup_failure(self, services: _Services, group: GroupKey, *, count: int = 1) -> None:
        """登记一次"清理未能确认完成"（S2-08 + S4-03）：内存计数、持久登记、审计。

        持久登记写失败**不虚报**：审计码改为存储失败，本次运行的计数仍然保留；
        存储的粘滞失败会同时让记忆相关能力保持关闭（`_memory_store`）。
        """
        services.health.record_cleanup_failure()
        code = redact.ErrorCode.DELETE_FAILED
        stored = services.storage
        if stored is not None and not stored.database.failed:
            try:
                stored.maintenance.record_failure(group)
            except storage.StorageFailure:
                code = redact.ErrorCode.MEMORY_STORE_FAILED
        self._audit(
            services,
            redact.EventCategory.CLEANUP_FAILURE,
            code=code,
            count=count,
        )

    def _clear_cleanup_failure(self, services: _Services, group: GroupKey) -> None:
        """一次**成功**的群级清理后撤销该群登记，并在全部清空后解除 `DELETION_FAILED`。

        计数按该群**登记在案的失败次数**扣减（不粗暴清零：启动裁剪等其他失败仍留在
        本次运行的计数里）。解除降级挂在"最后一条登记被清除"上：只要还有别的群删除
        未确认完成，降级与"记忆相关能力保持关闭"都不恢复（架构 §8.3）。无存储时不做
        任何声明——计数保持原值，不假装恢复。
        """
        stored = services.storage
        if stored is None or stored.database.failed:
            return
        try:
            pending = stored.maintenance.failures(group)
            if not pending.exists:
                return
            if not stored.maintenance.clear_group(group):
                return
            remaining_total = stored.maintenance.total()
        except storage.StorageFailure:
            return
        services.health.set_cleanup_failures(
            max(0, services.health.cleanup_failures() - pending.failures)
        )
        if remaining_total == 0:
            services.health.clear_degraded(health.Degradation.DELETION_FAILED)

    def _record_sweep_failure(self, services: _Services, *, count: int = 1) -> None:
        """启动裁剪失败：只计本次运行 + 审计，**不落持久登记**。

        启动裁剪在每次启动都会自动重跑，没有"待维护者复跑"的状态；R23 的登记面是
        群级删除（维护者能主动重跑的那些）。计数因此不跨重启保留，这是有意的。
        """
        services.health.record_cleanup_failure()
        self._audit(
            services,
            redact.EventCategory.CLEANUP_FAILURE,
            code=redact.ErrorCode.DELETE_FAILED,
            count=count,
        )

    def _restore_cleanup_failures(self, services: _Services) -> int:
        """启动时把持久登记读回计数与降级标志（S4-03 闭合 R23）。

        有未恢复的登记 → 计数与 `DELETION_FAILED` 一起恢复：记忆相关能力继续停用，
        `千鹤 状态` 继续显示"清理失败：N 次"，直到维护者重跑清理命令成功。
        无存储、库失败或读取失败 → 保持零、不置降级（R21：存储不是前置条件）。
        """
        stored = services.storage
        if stored is None or stored.database.failed:
            return 0
        try:
            total = stored.maintenance.total()
        except storage.StorageFailure:
            return 0
        if not total:
            return 0
        services.health.set_cleanup_failures(total)
        services.health.set_degraded(health.Degradation.DELETION_FAILED)
        return total

    async def _delete_group_history(self, umo: str) -> None:
        """原生历史清理（K9/R14 + S2-08）：**会话历史 + 平台消息历史**两步都做。

        平台消息历史那张表在本插件路径下本应为空（QQ 群消息经 `event.send` 不落表），
        这里是**幂等防御性清理**：只有部署曾打开内置群历史开关时才会真正删到东西。
        任何一步失败都抛给调用方，由它置降级并登记——**不虚报已删除**。
        """
        await self.context.conversation_manager.delete_conversations_by_user_id(umo)
        platform_id = umo.partition(":")[0]
        if not platform_id:
            raise ValueError("umo 缺少平台实例段，无法确认平台消息历史已清理")
        await self.context.message_history_manager.delete(
            platform_id=platform_id,
            user_id=umo,
            offset_sec=DELETE_ALL_HISTORY_SECONDS,
        )

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
            GateFacts(
                group_state=self._group_state(settings, facts, services, kind=kind),
                current=current,
                previous=None,
            ),
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

    def _group_state(
        self,
        settings: Settings,
        facts: MessageFacts,
        services: _Services,
        *,
        kind: SendKind,
    ) -> GroupState:
        """群开关：部署允许名单 ∧ 持久化策略**未暂停**（S2-02 起替换 S1 的临时来源）。

        - `CHAT`：暂停即关闭（需求 §4.4"暂停/恢复本群聊天"）；
        - `FIXED_NOTICE`：暂停**不**关闭——否则暂停群里的 `千鹤 状态` 与恢复不可达；
        - 存储缺失或读失败 → 放行：R21"存储不是聊天的前置条件"，且无法读取不等于已暂停；
        - `群上下文 关闭`（`context_enabled=0`）只停采集，**不**停聊天（需求 §4.4）。
        """
        if not settings.allows_group(facts.platform_id, facts.self_id, facts.group_id):
            return GroupState.CLOSED
        if kind is not SendKind.CHAT:
            return GroupState.OPEN
        stored = services.storage
        if stored is None or stored.database.failed:
            return GroupState.OPEN
        try:
            policy = stored.groups.policy(self._group_key(facts))
        except storage.StorageFailure:
            return GroupState.OPEN
        return GroupState.CLOSED if policy.paused else GroupState.OPEN

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
