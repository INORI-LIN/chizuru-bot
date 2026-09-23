"""记忆检索与注入块渲染（S3-08）。

三条边界：

- **只取当前提问者在当前群**：查询键由调用方从可信事件元数据构造的 ``MemberKey``
  （机器人实例 + 群 + 成员）决定，本模块**不接受任何成员 ID 参数**——"不响应'列出某成员
  记忆'"因此是结构事实：没有可以传入他人 ID 的入口。
- **只渲染，不改写**：内容在候选校验时已经过长度与敏感过滤；这里只把换行/控制字符折成
  空格（复用群聊材料的同一函数），保证**结构上无法新起一行**去伪造块里的其他行。
- **注入块是新的模型可见文本**：标题与类别名**逐字取自需求 §4.3 的"可保存"表**，按待评审
  处理（``MEMORY_BLOCK_VERSION`` + 测试指纹钉住，与 ``NOTICE_TEXT``/``EXTRACTION_RULES``
  同例）。

检索本身不做语义排序：需求 §1.3 明确首版不引入向量库，"相关"由"本人 + 本群 + 未过期"
界定，条数由存储层的每成员上限与 8192 裁剪共同约束——不新增数字。
"""

from __future__ import annotations

from typing import Mapping, Sequence

from ..context_assembly import TextBlock, sanitize_material_text
from ..keys import MemberKey
from ..storage.memories import MemoryFact, MemoryStore
from .types import Category

MEMORY_BLOCK_VERSION = "memory-block-1"
"""注入块文本版本。**标题或类别名任何改动（含标点与空白）都必须递增本值**——
测试用字面量副本与 SHA-256 指纹钉住（与 ``notice.NOTICE_VERSION`` 同例）。"""

MEMORY_BLOCK_TITLE = "【本人记忆·临时材料】"
"""注入块标题，与群聊材料块 ``【近期群聊·临时材料】`` 同构。**属待评审文本。**"""

LINE_PREFIX = "· "
"""行前缀；与附录 C 文案的 ``·`` 项目符一致。"""

CATEGORY_LABELS: Mapping[Category, str] = {
    Category.ADDRESS: "本人希望的称呼",
    Category.REPLY_LENGTH: "回复长短偏好",
    Category.INTEREST: "一般兴趣",
    Category.ACTIVITY: "非敏感活动偏好",
}
"""类别名**逐字取自需求 §4.3 的"可保存"表**，不新增措辞；群内列表（``consent.render_records``）
与注入块（``render_lines``）共用同一份取值。"""


def retrieve(store: MemoryStore, member: MemberKey) -> tuple[MemoryFact, ...]:
    """取这位成员在本群的**未过期**记录（已按记录号升序）。

    过期过滤与排序都在存储层完成（读侧不续期）；隔离由 ``member`` 这个完整键保证——
    调用方只能从可信事件元数据构造它。
    """
    if not isinstance(store, MemoryStore):
        raise ValueError("store 必须是 MemoryStore")
    if not isinstance(member, MemberKey):
        raise ValueError("member 必须是 MemberKey")
    return store.facts(member)


def render_lines(facts: Sequence[MemoryFact]) -> tuple[str, ...]:
    """渲染记忆行：``· 类别名：内容``。

    类别不在白名单内、或内容折叠后为空的行**直接跳过**：注入的每一行都必须能对上需求
    §4.3 的"可保存"表。
    """
    lines: list[str] = []
    for fact in facts:
        label = category_label(fact.category)
        if label is None:
            continue
        content = sanitize_material_text(fact.content)
        if not content:
            continue
        lines.append(f"{LINE_PREFIX}{label}：{content}")
    return tuple(lines)


def render_block(facts: Sequence[MemoryFact]) -> TextBlock | None:
    """渲染注入块；没有任何可用行时返回 ``None``（**不注入空块**）。"""
    lines = render_lines(facts)
    if not lines:
        return None
    return TextBlock(title=MEMORY_BLOCK_TITLE, lines=lines)


def category_label(value: object) -> str | None:
    """类别的展示名；不在白名单内返回 ``None``。

    群内列表（``consent.render_records``）与注入块（``render_lines``）共用同一份取值，
    保证两处不会各持一份类别名。
    """
    try:
        category = Category(value)
    except ValueError:
        return None
    return CATEGORY_LABELS.get(category)
