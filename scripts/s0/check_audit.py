"""S4-04 日志、网络与备份审计（离线可留证部分）。

证明**本仓库分发的内容**不会自行产生网络面、日志文件或数据副本，并核对配置模型里
没有密钥类字段；真实部署上的绑定、端口、Token、宿主快照与同步盘**只能在线核对**，
本脚本只列清单与步骤（编号 O-18—O-20，见 docs/03 附录 J），不对它们判 PASS。

不使用 `Harness`：本脚本不跑事件与框架，只做结构审计——导入 `astrbot_plugin_chizuru.redact`
是安全的（该模块只依赖标准库与同包的 `config`/`budget`，不加载框架、不建库、不联网）。
"""

from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import Checker  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "astrbot_plugin_chizuru"
SKIP_DIRS = {".git", ".runtime", ".cache", ".tools", ".venv", "node_modules", "__pycache__"}

# 只读结构审计：把仓库根加入 sys.path 以便导入插件包（`__init__.py` 只有文档字符串，
# 不加载框架、不建库、不联网）。
sys.path.insert(0, str(ROOT))

NETWORK_MODULES = (
    "socket",
    "socketserver",
    "http",
    "aiohttp",
    "quart",
    "uvicorn",
    "websockets",
    "tornado",
    "flask",
    "fastapi",
)
"""插件源码中不允许出现的网络/服务模块（收发只经框架既有的适配器与提供商）。"""

NETWORK_ATTRS = ("bind", "listen", "accept", "connect_ex", "sendto")
"""套接字风格的方法名；出现即说明插件自己在建连接面。"""

SECRET_FIELD_SUFFIXES = ("_key", "_token", "_secret", "_password", "_credential")
"""字段名中不允许出现的密钥类后缀（`input_token_budget` 这类额度参数不在此列）。"""

AUDIT_RECORD_FIELDS = {"category", "code", "duration_ms", "tokens", "count", "correlation"}
"""`redact.AuditRecord` 的**完整**字段集合；多一个少一个都要改这里与文档。"""

IGNORE_PATTERNS = ("*.db", "*.db-wal", "*.db-shm", "*.log", "/.runtime/")
"""`.gitignore` 必须覆盖的运行数据：库文件（含 WAL/SHM）、日志与运行目录。"""


def plugin_sources() -> list[Path]:
    return sorted(PLUGIN.rglob("*.py"))


def distributed_files() -> list[Path]:
    """仓库分发内的全部文件（运行目录、VCS 元数据与缓存不算分发内容）。"""
    return sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file() and not any(part in SKIP_DIRS for part in path.parts)
    )


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def imported_names(tree: ast.Module) -> set[str]:
    names = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    names |= {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    return {name for name in names if name}


class LoggerSites(ast.NodeVisitor):
    """记录 `self.logger.<level>(...)` 调用所在的函数名（应当只有 `_audit`）。"""

    def __init__(self) -> None:
        self.stack: list[str] = []
        self.sites: set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "logger"
        ):
            self.sites.add(self.stack[-1] if self.stack else "<module>")
        self.generic_visit(node)


def main() -> int:
    checker = Checker("S4-04", "日志、网络与备份审计（离线可留证部分）")
    sources = plugin_sources()

    # --- 网络面：插件自己不开监听、不建连接 ---
    offenders: list[str] = []
    for path in sources:
        tree = parse(path)
        hit = imported_names(tree) & set(NETWORK_MODULES)
        if hit:
            offenders.append(f"{path.name}: 导入 {sorted(hit)}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in NETWORK_ATTRS:
                    offenders.append(f"{path.name}:{node.lineno}: 调用 {node.func.attr}()")
    checker.check(
        "插件源码不导入网络/服务模块，也不调用绑定或监听",
        not offenders,
        evidence="; ".join(offenders),
    )

    # --- 本地日志面：插件不写文件、不导入 logging、不 print ---
    writers: list[str] = []
    for path in sources:
        tree = parse(path)
        if "logging" in imported_names(tree):
            writers.append(f"{path.name}: 导入 logging")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in ("open", "print"):
                writers.append(f"{path.name}:{node.lineno}: {func.id}()")
            elif isinstance(func, ast.Attribute) and func.attr in (
                "open",
                "write_text",
                "write_bytes",
            ):
                writers.append(f"{path.name}:{node.lineno}: {func.attr}()")
    checker.check(
        "插件不做文件写入、不导入 logging、不 print（日志只经框架通道）",
        not writers,
        evidence="; ".join(writers),
    )

    logger_sites = LoggerSites()
    logger_sites.visit(parse(PLUGIN / "main.py"))
    checker.check(
        "main.py 的日志调用只出现在 _audit 内（无自由文本日志点）",
        logger_sites.sites == {"_audit"},
        evidence=f"出现位置：{sorted(logger_sites.sites)}",
    )

    # --- 审计记录的字段集合固定且无自由文本 ---
    import astrbot_plugin_chizuru.config as config_module  # noqa: E402（纯标准库依赖）
    import astrbot_plugin_chizuru.redact as redact  # noqa: E402

    fields_seen = {field.name for field in fields(redact.AuditRecord)}
    checker.check(
        "审计记录的字段集合固定为闭集（无正文/密钥字段）",
        fields_seen == AUDIT_RECORD_FIELDS,
        evidence=f"实际字段：{sorted(fields_seen)}",
    )

    # --- 仓库分发内没有备份或临时副本，也没有备份脚本 ---
    files = distributed_files()
    backup_names = [
        path.relative_to(ROOT).as_posix()
        for path in files
        if path.name.endswith("~")
        or path.name.endswith(".bak")
        or path.name.endswith(".orig")
        or "backup" in path.name.lower()
    ]
    backup_tools: list[str] = []
    for path in files:
        if path == Path(__file__).resolve():
            continue  # 本脚本列出这些工具名作为检查样式，不算"备份脚本"
        # 只看可执行的脚本与配置文件：文档里出现工具名（例如附录 J 的核对步骤）
        # 是说明，不是备份自动化。
        if path.suffix not in (".py", ".sh", ".json", ".yaml", ".yml", ".toml"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for tool in ("rsync", "rclone", "tmutil"):
            if tool in text:
                backup_tools.append(f"{path.relative_to(ROOT).as_posix()}: {tool}")
    checker.check(
        "仓库分发内无备份/临时副本，也没有备份脚本或配置",
        not backup_names and not backup_tools,
        evidence="; ".join(backup_names + backup_tools),
    )

    # --- 运行数据被版本控制排除 ---
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    missing = [pattern for pattern in IGNORE_PATTERNS if pattern not in gitignore]
    leaked = [
        path.relative_to(ROOT).as_posix()
        for path in files
        if path.suffix in (".db", ".log") or path.name.endswith((".db-wal", ".db-shm"))
    ]
    checker.check(
        "运行数据（库文件、日志、运行目录）被 .gitignore 覆盖且工作区无副本",
        not missing and not leaked,
        evidence=f"缺少样式：{missing}；工作区命中：{leaked}",
    )

    # --- 配置模型与 schema 里没有密钥类字段 ---
    settings_fields = {field.name for field in fields(config_module.Settings)}
    schema_keys = set(json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8")))
    suspicious = sorted(
        name
        for name in settings_fields | schema_keys
        if name.lower().endswith(SECRET_FIELD_SUFFIXES)
    )
    checker.check(
        "配置模型与 _conf_schema.json 不含密钥类字段",
        not suspicious,
        evidence=f"命中：{suspicious}",
    )

    # 只认**字面量**里的密钥样式（`sk-` + 16 位以上字母数字）；敏感词表里的
    # 正则写法（`sk-[A-Za-z0-9]{8,}`）是检测器本身，不会命中。
    key_shape = re.compile(r"sk-[A-Za-z0-9]{16,}")
    secret_literals = [
        f"{path.name}:{node.lineno}"
        for path in sources
        for node in ast.walk(parse(path))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and key_shape.search(node.value)
    ]
    checker.check(
        "插件源码中没有任何密钥样式字面量",
        not secret_literals,
        evidence="; ".join(secret_literals),
    )

    # --- 只能在线核对的部分：列出清单与步骤，不判 PASS ---
    checker.note("以下三项**只能在线核对**（未上线时不判 PASS）：")
    checker.note(
        "  · O-18 真实日志：插件通道（astrbot.plugin.<插件名>）与框架日志文件/WebUI 缓存中"
        "不得出现群消息正文、ws_reverse_token、请求体或异常堆栈；轮转与保留期按架构 §11.1 复核"
    )
    checker.note(
        "  · O-19 网络与端口：ws_reverse_token 非空且两端一致；NapCat 与 AstrBot 优先回环绑定；"
        "WebUI（默认 6185）与 OneBot（默认 6199）不得对公网暴露；管理凭据已改默认值"
    )
    checker.note(
        "  · O-20 宿主快照与同步盘：整机备份、同步盘与 Time Machine 快照必须排除运行数据目录"
        "（架构 §11.3）——核对步骤见 docs/03 附录 J"
    )
    checker.note("框架侧人工审计清单（redact.AUDIT_CHECKLIST）逐条：")
    for item in redact.AUDIT_CHECKLIST:
        checker.note(f"  · {item.topic}｜{item.checkpoint}｜{item.expectation}")

    return checker.report()


if __name__ == "__main__":
    raise SystemExit(main())
