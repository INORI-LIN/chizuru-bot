"""记忆授权说明、群内回执与成员级两步确认窗口（S3-03/S3-04）：纯逻辑，不做 IO、不判权限。

**交付机制**（架构 §4.4）：成员 @ 机器人发「记忆 开启」→ 机器人**只回复** `CONSENT_TEXT`
全文并记录一个待确认窗口；同一成员在窗口内、同群内发「记忆 确认开启」才写入授权行。
「记忆 查看」同型两步：先回 `VIEW_NOTICE_TEXT`（提示这是群内可见操作），确认后才列出记录。

| 事项 | 本模块的处理 |
|---|---|
| 文案 | `CONSENT_TEXT`/`VIEW_NOTICE_TEXT` **逐字**取自 docs/03 附录 C.2/C.3（2026-09-23 定稿），只去掉代码块末尾的换行；测试用字面量副本与 SHA-256 指纹钉住 |
| 版本 | `CONSENT_VERSION` 是**落盘版本**（写进 `memory_state.auth_version`，读取时比对：不一致一律按未授权）；`VIEW_NOTICE_VERSION` 只锚定窗口参数 |
| 窗口 | 每成员**单槽**：重新发起即覆盖刷新（换人即作废前一次）；`now - requested_at < window` 才有效——**恰好到期算过期**（取严） |
| 失败 | `confirm()` **无论成败都消费**窗口；失败原因只用于审计与测试，对群内行为完全一致（重发全文并重开窗口） |
| 回执 | 群内回执文本集中在本模块；记录号必须显示（`记忆 删除/纠正 <编号>` 的入参就是它） |

**必须不做**：不导入框架/sqlite/asyncio、不做 IO、不调模型、不发消息、不判权限、
不读配置（窗口与版本由调用方传入）、不保存正文或昵称、不决定归属。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Sequence

from ..context_assembly import sanitize_material_text
from ..keys import MemberKey
from ..storage.memories import MemoryFact, MemoryState
from .retrieve import LINE_PREFIX, category_label

CONSENT_VERSION = "consent-1"
"""授权说明版本。**文本任何改动（含标点与空白）都必须递增本值**——测试用字面量钉住
版本与文本指纹；递增会使已授权成员在下一次读取时回到未授权（见 docs/03 R27），
正确动作是重新告知与确认，而不是自动迁移。"""

CONSENT_TEXT = """【长期记忆授权说明】

是否允许我在本群记住一些关于你的事情？默认是关闭的，需要你确认后
才会开启。

· 会怎么用
  只从你在本群 @ 我时亲口说出的内容中提取，不会从普通群聊、别人的
  发言、引用内容或我自己的回复中提取。提取到的事实只用于我在本群回
  复你时让对话更连贯。提取在后台完成，我不会在群里主动说"我记住了"。

· 会保存什么
  你的称呼偏好、回复长短偏好、一般兴趣、非敏感的活动偏好。
  不保存密码、口令、电话、住址、证件、财务或健康信息，也不保存别人
  的隐私、关系推断，或你没说过的标签。

· 保存在哪里、存多久
  保存在本机数据库，最多 20 条，90 天后失效，到期即停止使用并清除。
  提取和回复时，必要内容会发送给 DeepSeek 处理。

· 谁能看到
  只有你本人可以查看、纠正或删除自己的记录，别人问起我不会说。
  但请注意：你在本群查看记录时，内容会发在群里，其他成员可能看到。

· 怎么撤回
  随时 @ 我发送「记忆 关闭」或「记忆 删除全部」，即可撤回授权并清除
  已保存的记录。撤回后需要重新确认才能再次开启。

· 只对开启后的内容生效
  确认开启后，我只处理此后产生的内容，不会追溯开启之前的聊天。

确认开启请回复「记忆 确认开启」，5 分钟内有效。"""

VIEW_NOTICE_VERSION = "memory-view-1"
"""查看提示版本；只锚定窗口的 `version` 参数，不落盘。"""

VIEW_NOTICE_TEXT = """【提示】接下来我会把你在本群的记忆记录列在这里。
这是群内消息，其他群成员可以看到。

继续请回复「记忆 查看 确认」。"""

CONSENT_WINDOW_SECONDS = 300
"""确认窗口（秒）的模块默认值；**生产由 `settings.memory.auth_confirm_ttl_seconds` 覆盖**
（与需求 §6 的 5 分钟一致）。"""

PENDING_CAPACITY = 256
"""待确认窗口的成员数上界。**建议参数待评审**：条目只含成员键、成员 ID、版本与时间。"""

STATUS_CLOSED_TEXT = "长期记忆：未开启。@ 我发送「记忆 开启」可查看授权说明。"
"""`记忆 状态` 的未开启行；行格式对齐 `notice.describe_policy`。"""

CONFIRM_SUCCESS_TEXT = "长期记忆已开启。@ 我发送「记忆 状态」可查看，发送「记忆 关闭」可撤回授权并清除已保存的记录。"
"""`记忆 确认开启` 成功的群内回执（与群告知的"成功静默"刻意不同：隐私动作需要可确认）。"""

CORRECT_REFUSED_TEXT = "这类内容不能保存，请换一种说法。"
"""纠正内容未通过敏感/长度判定时的回执（对应需求 §4.4"不符合允许类型的内容不写入"）。"""

DISABLE_SUCCESS_TEXT = "长期记忆已关闭，已保存的记录已清除。重新开启需要再次 @ 我发送「记忆 开启」并确认。"
"""`记忆 关闭` / `记忆 删除全部` 的回执；措辞取自附录 C.2"撤回授权并清除已保存的记录"。"""

UNAVAILABLE_TEXT = "长期记忆暂时不可用，这次操作没有完成。可以稍后再试，或联系本群维护者。"
"""无存储、库失败或写失败时的回执（架构 §8.3：删除失败必须"明确未完成"，不得虚报）。"""

LIST_TITLE = "【你在本群的记忆记录】"
"""列表标题，取自附录 C.3"你在本群的记忆记录"。"""

LIST_HINT_TEXT = "要删除或纠正某条记录，@ 我发送「记忆 删除 <编号>」或「记忆 纠正 <编号> <新内容>」。"
"""列表末行；记录号没有使用说明就没有意义。"""

LIST_EMPTY_TEXT = "当前没有记录。"
"""空列表正文（不附操作提示：没有可删除的对象）。"""


class ConsentOutcome(StrEnum):
    """一次确认尝试的结论；除 ``CONFIRMED`` 外都是"没有生效"。"""

    CONFIRMED = "confirmed"
    NO_PENDING = "no_pending"
    EXPIRED = "expired"
    ACTOR_MISMATCH = "actor_mismatch"
    VERSION_MISMATCH = "version_mismatch"


@dataclass(frozen=True)
class PendingConsent:
    """一次"已展示说明、等待确认"的记录（成员键即表键，不重复存放）。"""

    actor_id: str
    version: str
    requested_at: float


def _is_plain_text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


class ConsentGate:
    """每成员一个待确认窗口的内存表；所有方法同步，只在事件循环内使用。

    与 `notice.NoticeGate` 同型（单槽、取严、无论成败都消费、有界、惰性淘汰、不持有
    后台任务），区别只在键是 `MemberKey`——授权与查看的隔离范围是"当前机器人 + 当前群
    + 本人"，与群级告知不是一回事。重启会丢弃窗口，成员重新发起一次即可。
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        window_seconds: float = CONSENT_WINDOW_SECONDS,
        capacity: int = PENDING_CAPACITY,
    ) -> None:
        if not isinstance(window_seconds, (int, float)) or isinstance(window_seconds, bool) or window_seconds <= 0:
            raise ValueError("window_seconds 必须是正数")
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
            raise ValueError("capacity 必须是正整数")
        if not callable(clock):
            raise ValueError("clock 必须是可调用对象")
        self._clock = clock
        self._window = float(window_seconds)
        self._capacity = capacity
        self._pending: dict[MemberKey, PendingConsent] = {}
        self._evicted = 0

    def begin(self, member: MemberKey, *, actor_id: str, version: str) -> None:
        """记录（或刷新）这位成员的待确认窗口；同一成员重复发起只是刷新时间。"""
        if not isinstance(member, MemberKey):
            raise ValueError("member 必须是 MemberKey")
        if not _is_plain_text(actor_id):
            raise ValueError("actor_id 必须是非空且无首尾空白的文本")
        if not _is_plain_text(version):
            raise ValueError("version 必须是非空且无首尾空白的文本")
        now = self._clock()
        self._drop_expired(now)
        self._pending.pop(member, None)
        self._pending[member] = PendingConsent(actor_id=actor_id, version=version, requested_at=now)
        while len(self._pending) > self._capacity:
            oldest = next(iter(self._pending), None)
            if oldest is None:
                break
            self._pending.pop(oldest, None)
            self._evicted += 1

    def confirm(self, member: MemberKey, *, actor_id: str, version: str) -> ConsentOutcome:
        """核对一次确认；**无论成败都消费窗口**（失败后由调用方重发全文并重开窗口）。

        顺序：先过期、再换人、最后版本——三者对群内行为一致，区分只为审计可读。
        """
        if not isinstance(member, MemberKey):
            raise ValueError("member 必须是 MemberKey")
        now = self._clock()
        entry = self._pending.pop(member, None)
        if entry is None:
            return ConsentOutcome.NO_PENDING
        if now - entry.requested_at >= self._window:
            return ConsentOutcome.EXPIRED
        if entry.actor_id != actor_id:
            return ConsentOutcome.ACTOR_MISMATCH
        if entry.version != version:
            return ConsentOutcome.VERSION_MISMATCH
        return ConsentOutcome.CONFIRMED

    def pending(self, member: MemberKey) -> PendingConsent | None:
        """当前待确认记录（只读；过期即视为没有）。"""
        entry = self._pending.get(member)
        if entry is None:
            return None
        if self._clock() - entry.requested_at >= self._window:
            self._pending.pop(member, None)
            return None
        return entry

    def stats(self) -> tuple[int, int]:
        """(当前成员数, 因容量淘汰的累计条数)；读取本身也会淘汰过期项。"""
        self._drop_expired(self._clock())
        return len(self._pending), self._evicted

    def _drop_expired(self, now: float) -> None:
        for member in [key for key, entry in self._pending.items() if now - entry.requested_at >= self._window]:
            self._pending.pop(member, None)


def authorization_is_current(state: MemoryState, *, required_version: str = CONSENT_VERSION) -> bool:
    """授权是否**当前有效**：授权位为真 **且** 落盘版本等于当前说明版本。

    版本不一致一律按未授权处理（与 R22 的群告知同向的 fail-closed）：说明文案改了就必须
    重新告知与确认，而不是让旧同意继续生效。
    """
    if not isinstance(state, MemoryState):
        raise ValueError("state 必须是 MemoryState")
    if not _is_plain_text(required_version):
        raise ValueError("required_version 必须是非空且无首尾空白的文本")
    return bool(state.authorized and state.auth_version == required_version)


def status_line(*, authorized: bool, count: int, limit: int, days: int) -> str:
    """`记忆 状态` 的正文；数字与句式取自附录 C.2"最多 20 条，90 天后失效"。"""
    if not authorized:
        return STATUS_CLOSED_TEXT
    return f"长期记忆：已开启；记录 {count} 条（最多 {limit} 条），{days} 天后失效。"


def render_records(facts: Sequence[MemoryFact]) -> tuple[str, ...]:
    """渲染可展示的记录行：``· 记录号：类别名：内容``。

    与注入块同规则：类别不在白名单、或内容折叠后为空的整行跳过；内容经
    `sanitize_material_text` 折成单行，因此**记忆内容无法伪造出新的列表行**。
    """
    rows: list[str] = []
    for fact in facts:
        label = category_label(fact.category)
        if label is None:
            continue
        content = sanitize_material_text(fact.content)
        if not content:
            continue
        rows.append(f"{LINE_PREFIX}{fact.record_id}：{label}：{content}")
    return tuple(rows)


def list_body(facts: Sequence[MemoryFact]) -> str:
    """`记忆 查看 确认` 的完整正文：标题 + 记录行 + 操作提示（空列表则只给标题与说明）。"""
    lines = render_records(facts)
    if not lines:
        return f"{LIST_TITLE}\n\n{LIST_EMPTY_TEXT}"
    return "\n".join([LIST_TITLE, "", *lines, "", LIST_HINT_TEXT])


def record_updated(record_id: int) -> str:
    """`记忆 纠正` 成功的回执。"""
    return f"已更新记录 {record_id}。"


def record_deleted(record_id: int) -> str:
    """`记忆 删除` 成功的回执。"""
    return f"已删除记录 {record_id}。"


def record_missing(record_id: int) -> str:
    """记录不存在、已过期或编号超界时的回执（不区分原因，避免暴露他人数据是否存在）。"""
    return f"没有找到记录 {record_id}。@ 我发送「记忆 查看」可查看当前记录。"
