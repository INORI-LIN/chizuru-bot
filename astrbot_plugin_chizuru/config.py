from collections.abc import Mapping
from dataclasses import dataclass


def is_qq_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.isascii()
        and value.isdecimal()
        and not value.startswith("0")
    )


@dataclass(frozen=True)
class Settings:
    platform_id: str = ""
    self_id: str = ""
    allowed_group_ids: frozenset[str] = frozenset()

    @classmethod
    def from_mapping(cls, config: Mapping[str, object]) -> "Settings":
        platform_id = config.get("platform_id", "")
        self_id = config.get("self_id", "")
        group_ids = config.get("allowed_group_ids", [])
        # 配置边界失败时整体关闭，不能保留一部分无意放行的配置。
        if (
            not isinstance(platform_id, str)
            or not platform_id.strip()
            or platform_id != platform_id.strip()
            or not is_qq_id(self_id)
            or not isinstance(group_ids, list)
            or not all(is_qq_id(group_id) for group_id in group_ids)
        ):
            return cls()
        return cls(platform_id, self_id, frozenset(group_ids))
