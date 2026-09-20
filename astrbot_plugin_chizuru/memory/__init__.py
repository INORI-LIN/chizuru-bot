"""授权长期记忆的候选层（S3-05 起）：类型、候选校验、抽取请求与解析、准入与写回。

包内模块与"必须不做"：

| 模块 | 职责 | 必须不做 |
|---|---|---|
| ``types.py`` | 类别白名单与候选校验（长度、敏感、形状） | 不调模型、不做 IO、不决定归属、不做检索 |
| ``extract.py`` | 抽取请求成形与结构化解析（独立提示，S3-06） | 不含千鹤人格与群历史、不发送、不写库、不递归触发聊天 |
| ``pipeline.py`` | 准入判定与写回（S3-07/S3-10） | 不调模型、不发送、不预留额度、不做调度、所有拒绝都静默 |
| ``retrieve.py`` | 检索键的隔离与注入块渲染（S3-08） | 不接受成员 ID 参数、不做语义排序、不改写内容、不导入框架 |

本包**不导入框架**，也不做任何网络调用：提供商调用与预算结算由 ``main`` 的装配层完成
（与 ``llm.py`` 同例）。归属、授权与保存范围始终来自可信事件元数据，模型只建议内容。
"""

from __future__ import annotations

from .extract import (
    EXTRACT_VERSION,
    EXTRACTION_RULES,
    ExtractionResult,
    ExtractionStatus,
    build_request,
    extract,
    from_answer,
    parse_candidates,
)
from .pipeline import (
    Admission,
    Refusal,
    WriteOutcome,
    WritePlan,
    WriteResult,
    admit,
    counts_as_failure,
    plan_candidates,
    write_back,
)
from .retrieve import (
    CATEGORY_LABELS,
    LINE_PREFIX,
    MEMORY_BLOCK_TITLE,
    MEMORY_BLOCK_VERSION,
    render_block,
    render_lines,
    retrieve,
)
from .types import (
    MAX_SOURCE_LENGTH,
    Category,
    Candidate,
    build_candidate,
    prepare_source,
)

__all__ = [
    "CATEGORY_LABELS",
    "EXTRACT_VERSION",
    "EXTRACTION_RULES",
    "LINE_PREFIX",
    "MAX_SOURCE_LENGTH",
    "MEMORY_BLOCK_TITLE",
    "MEMORY_BLOCK_VERSION",
    "Admission",
    "Category",
    "Candidate",
    "ExtractionResult",
    "ExtractionStatus",
    "Refusal",
    "WriteOutcome",
    "WritePlan",
    "WriteResult",
    "admit",
    "build_candidate",
    "build_request",
    "counts_as_failure",
    "extract",
    "from_answer",
    "parse_candidates",
    "plan_candidates",
    "prepare_source",
    "render_block",
    "render_lines",
    "retrieve",
    "write_back",
]
