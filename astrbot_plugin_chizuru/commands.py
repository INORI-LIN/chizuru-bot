"""控制指令纯解析：把直接文本变成 CommandIntent。

**本模块不判断权限、不执行任何动作、不调用模型。** 它只回答"这句话是不是一条
已知指令、是哪一条、需要什么权限"。权限核验与状态变更在 ``control.py``（S1-06），
且必须基于可信事件身份，不能由模型或文本决定。

调用前提：**调用方必须已经确认真实 @**。本函数只看文本，无法区分"被 @ 后说的
记忆 删除"和"群里随口提到 记忆 删除"，所以不能在未确认触发的情况下调用它执行。

指令名称取自需求文档 §4.4；该节自述"实际解析在后续实施确定"，因此空白归一化与
参数形式由本模块固定，并在测试中钉住。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CommandKind(StrEnum):
    HELP = "help"
    CONTEXT_LEAVE = "context_leave"
    CONTEXT_JOIN = "context_join"
    CONTEXT_CLEAR = "context_clear"
    MEMORY_ENABLE = "memory_enable"
    MEMORY_CONFIRM = "memory_confirm"
    MEMORY_STATUS = "memory_status"
    MEMORY_LIST = "memory_list"
    MEMORY_CORRECT = "memory_correct"
    MEMORY_DELETE = "memory_delete"
    MEMORY_DISABLE = "memory_disable"
    GROUP_NOTICE_OPEN = "group_notice_open"
    GROUP_NOTICE_CONFIRM = "group_notice_confirm"
    GROUP_CONTEXT_CLOSE = "group_context_close"
    PAUSE = "pause"
    RESUME = "resume"
    STATUS = "status"


class Permission(StrEnum):
    """指令所需的最低权限。真实判定在 control.py，且只来自可信事件身份。"""

    MEMBER = "member"
    """群成员即可，不需要本人以外的任何授权。"""

    SELF = "self"
    """只能作用于本人的数据；不能代他人操作。"""

    MAINTAINER = "maintainer"
    """需要经配置显式授权的本群维护者；QQ 群管理员身份不自动生效。"""


@dataclass(frozen=True)
class CommandIntent:
    kind: CommandKind
    permission: Permission
    record_id: str = ""
    argument: str = ""


# 无参数的精确指令。键是空白归一化后的文本。
_EXACT: dict[str, tuple[CommandKind, Permission]] = {
    "帮助": (CommandKind.HELP, Permission.MEMBER),
    "上下文 退出": (CommandKind.CONTEXT_LEAVE, Permission.SELF),
    "上下文 加入": (CommandKind.CONTEXT_JOIN, Permission.SELF),
    "上下文 清空": (CommandKind.CONTEXT_CLEAR, Permission.MAINTAINER),
    "记忆 开启": (CommandKind.MEMORY_ENABLE, Permission.SELF),
    "记忆 确认开启": (CommandKind.MEMORY_CONFIRM, Permission.SELF),
    "记忆 状态": (CommandKind.MEMORY_STATUS, Permission.SELF),
    "记忆 查看": (CommandKind.MEMORY_LIST, Permission.SELF),
    "记忆 关闭": (CommandKind.MEMORY_DISABLE, Permission.SELF),
    "记忆 删除全部": (CommandKind.MEMORY_DISABLE, Permission.SELF),
    "群上下文 开启": (CommandKind.GROUP_NOTICE_OPEN, Permission.MAINTAINER),
    "群上下文 确认开启": (CommandKind.GROUP_NOTICE_CONFIRM, Permission.MAINTAINER),
    "群上下文 关闭": (CommandKind.GROUP_CONTEXT_CLOSE, Permission.MAINTAINER),
    "千鹤 暂停": (CommandKind.PAUSE, Permission.MAINTAINER),
    "千鹤 恢复": (CommandKind.RESUME, Permission.MAINTAINER),
    "千鹤 状态": (CommandKind.STATUS, Permission.MAINTAINER),
}

_CORRECT_PREFIX = "记忆 纠正 "
_DELETE_PREFIX = "记忆 删除 "

# 已入库的指令名清单，供测试与文档核对。
COMMAND_TEXTS: tuple[str, ...] = tuple(_EXACT) + (
    "记忆 纠正 <记录编号> <内容>",
    "记忆 删除 <记录编号>",
)


def normalize(text: str) -> str:
    """归一化空白：去首尾、把内部连续空白压成单个空格。

    这样 ``记忆   删除  3`` 与 ``记忆 删除 3`` 等价。只影响空白，不改变字符。
    """
    return " ".join(text.split())


def _is_record_id(value: str) -> bool:
    """记录编号：正整数形式，不接受 0 与前导零。

    与 QQ 号校验同口径。编号是否存在由存储层判断，解析器只负责形式。
    """
    return value.isdecimal() and not value.startswith("0")


def parse(text: str) -> CommandIntent | None:
    """解析指令；不是已知指令时返回 None（调用方按普通聊天处理）。

    返回 None 必须被当作"不能执行"，而不是"按无参数指令执行"——未知文本不得
    误判为命令。
    """
    normalized = normalize(text)
    if not normalized:
        return None

    exact = _EXACT.get(normalized)
    if exact is not None:
        kind, permission = exact
        return CommandIntent(kind, permission)

    if normalized.startswith(_CORRECT_PREFIX):
        record_id, _, content = normalized[len(_CORRECT_PREFIX) :].partition(" ")
        if _is_record_id(record_id) and content.strip():
            return CommandIntent(
                CommandKind.MEMORY_CORRECT,
                Permission.SELF,
                record_id=record_id,
                argument=content.strip(),
            )
        return None

    if normalized.startswith(_DELETE_PREFIX):
        record_id = normalized[len(_DELETE_PREFIX) :]
        if _is_record_id(record_id):
            return CommandIntent(
                CommandKind.MEMORY_DELETE,
                Permission.SELF,
                record_id=record_id,
            )
        return None

    return None


def is_command(text: str) -> bool:
    return parse(text) is not None
