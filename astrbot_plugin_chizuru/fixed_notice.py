"""固定提示文案（S4-01 离线半部）：纯逻辑，只有常量与确定性映射。

架构 §8.3 的"群内行为"列要求故障只在**已有有效 @** 的交互里给一次简短提示，
固定提示不得再次调用模型——`send_gate.SendRequest` 在构造时就拒绝
`FIXED_NOTICE + llm_invoked=True`（S4-02 的离线用例同时钉住）。本模块只回答
"这个错误码该说哪一句"；发送、去重、修订复核与"是否静默"全部由装配层负责。

| 事项 | 本模块的处理 |
|---|---|
| 文案 | 六条，逐字取自 docs/03 附录 C.5（2026-09-23 随批次 4a 定稿）；测试用字面量副本与 SHA-256 指纹钉住 |
| 版本 | `FIXED_NOTICE_VERSION` 锚定本模块内文本；**任何改动（含标点与空白）都必须递增本值**，与 `notice.NOTICE_VERSION` 同例 |
| 映射 | `text_for` 只认 `redact.ErrorCode`；未知码返回 `None`——宁可沉默，也不把未分类的故障说成用户能理解的原因（与 `llm.follow_up` 同一口径，两者的一致性由测试交叉断言钉住） |
| 方向 | 只覆盖聊天等**前台**交互；`llm.follow_up(background=True)` 恒为 `NOTHING`，抽取链路不得调用本模块 |

**必须不做**：不导入框架/sqlite/asyncio/`llm`、不做 IO、不调模型、不发消息、
不判权限、不读配置、不拼装任何用户数据。
"""

from __future__ import annotations

from typing import Mapping

from .redact import ErrorCode

FIXED_NOTICE_VERSION = "fixed-notice-1"
"""固定提示文案版本。**文本任何改动（含标点与空白）都必须递增本值**——
测试用字面量副本与 SHA-256 指纹钉住（与 `NOTICE_VERSION` 同例）。"""

ATTACHMENT_NOTICE_TEXT = "我目前只能读文字消息，还看不了图片、语音和文件。请把想说的内容用文字发给我。"
"""@ 后只有图片/语音/文件时的能力提示（需求 §4.1）。不解析附件、不请求模型。"""

OVER_BUDGET_NOTICE_TEXT = "这条消息太长了，我一次处理不了，请缩短一些再发一次。"
"""当前消息超出输入上限时的提示（需求 §6：提示缩短，不悄悄截断关键含义）。"""

FAILURE_NOTICE_TEXT = "这次没有回复成功，请稍后再试一次。"
"""请求失败、限流重试耗尽、业务期限到点的通用提示（架构 §8.3、A16）。"""

EMPTY_REPLY_NOTICE_TEXT = "这次没能生成回复，请再发一次。"
"""模型空结果时的提示（架构 §8.3"模型空结果：聊天给固定提示"）。"""

BUSY_NOTICE_TEXT = "现在消息有点多，请稍后再发一次。"
"""排队已满或等待调度超时时的忙碌提示（架构 §8.3 的 429 行）。"""

UNAVAILABLE_NOTICE_TEXT = "现在我暂时不可用，请稍后再试，或联系本群维护者。"
"""402 余额不足、本地预算耗尽与价格未知的暂不可用提示（架构 §8.3、R20）。"""

FIXED_NOTICE_TEXTS: Mapping[ErrorCode, str] = {
    ErrorCode.REQUEST_INVALID: FAILURE_NOTICE_TEXT,
    ErrorCode.AUTH_FAILED: FAILURE_NOTICE_TEXT,
    ErrorCode.PROVIDER_UNAVAILABLE: FAILURE_NOTICE_TEXT,
    ErrorCode.RATE_LIMITED: FAILURE_NOTICE_TEXT,
    ErrorCode.EMPTY_REPLY: EMPTY_REPLY_NOTICE_TEXT,
    ErrorCode.QUEUE_FULL: BUSY_NOTICE_TEXT,
    ErrorCode.BALANCE_INSUFFICIENT: UNAVAILABLE_NOTICE_TEXT,
    ErrorCode.BUDGET_EXHAUSTED: UNAVAILABLE_NOTICE_TEXT,
}
"""错误码 → 文案。**只列 `llm` 判定为固定提示的类别**；未列出的码没有文案
（`REVISION_CHANGED`/`DELETE_FAILED`/`MEMORY_STORE_FAILED`/`UNCLASSIFIED` 等
一律静默——丢弃在途结果不是"可以重试"，记忆与删除的失败回执由各自的命令面负责）。"""


def text_for(code: ErrorCode) -> str | None:
    """取该错误码的固定提示；未知码返回 `None`（调用方据此保持静默）。"""
    if not isinstance(code, ErrorCode):
        raise ValueError("code 必须是 ErrorCode")
    return FIXED_NOTICE_TEXTS.get(code)
