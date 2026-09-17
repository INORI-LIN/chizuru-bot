"""S0-01 环境、锁与无隐式 pip 基线复核。

不使用 _harness：本脚本需要 subprocess 调 uv 与 unittest，而 _harness 的守卫
会阻止子进程（那是为框架级核验准备的）。
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / ".runtime" / "astrbot"
PLUGIN = ROOT / "astrbot_plugin_chizuru"

EXPECTED_UPSTREAM_COMMIT = "ab42c0d9b726d82ad0f9563e04c53a4460c00d61"
EXPECTED_PYPROJECT_SHA256 = "6d1adff107d35eb6f2b05da0a109cefde40928d312449f468d3b43e1ab3edae4"

# 架构文档 §14.2 记录的三个仍可能走 pip 的上游入口；它们当前不应被本插件触达。
PIP_ENTRY_POINTS = (
    "astrbot/core/star/star_manager.py",
    "astrbot/core/utils/pip_installer.py",
    "astrbot/dashboard/services/update_service.py",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def main() -> int:
    failures: list[str] = []
    passed = 0

    def check(name: str, condition: bool, evidence: str = "") -> None:
        nonlocal passed
        if condition:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}" + (f"  <- {evidence}" if evidence else ""))

    print("[S0-01] 环境、锁与无隐式 pip 基线复核")

    # 1. 受控锁与工作副本逐字节一致
    baseline = ROOT / "environment" / "uv.lock"
    working = SOURCE / "uv.lock"
    same = baseline.read_bytes() == working.read_bytes()
    check("受控锁与工作副本逐字节一致", same, evidence=f"{sha256(baseline)[:16]} vs {sha256(working)[:16]}")

    # 2. 上游提交未漂移
    head = run(["git", "-C", str(SOURCE), "rev-parse", "HEAD"])
    actual_commit = head.stdout.strip()
    check(
        f"上游提交为 {EXPECTED_UPSTREAM_COMMIT[:12]}",
        actual_commit == EXPECTED_UPSTREAM_COMMIT,
        evidence=f"实际 {actual_commit or head.stderr.strip()}",
    )

    # 3. 上游工作树无已跟踪文件被修改
    dirty = run(["git", "-C", str(SOURCE), "status", "--porcelain", "--untracked-files=no"])
    check("上游工作树无已跟踪文件修改", not dirty.stdout.strip(), evidence=dirty.stdout.strip()[:200])

    # 4. 上游 pyproject.toml 未被改动
    actual_sha = sha256(SOURCE / "pyproject.toml")
    check(
        "上游 pyproject.toml 摘要未变",
        actual_sha == EXPECTED_PYPROJECT_SHA256,
        evidence=actual_sha,
    )

    # 5. 锁与声明一致
    lock_check = run(
        [
            "uv", "lock", "--check", "--offline",
            "--project", str(SOURCE),
            "--python", sys.executable,
            "--no-python-downloads",
        ],
        cwd=str(ROOT),
    )
    check(
        "uv lock --check --offline 通过",
        lock_check.returncode == 0,
        evidence=(lock_check.stderr or lock_check.stdout).strip()[-200:],
    )

    # 6. 插件不声明第三方运行依赖（依赖必须进受控锁集合）
    check("插件不含 requirements.txt", not (PLUGIN / "requirements.txt").exists())

    # 7. 插件源码不导入 pip
    pip_hits = []
    for path in sorted(PLUGIN.rglob("*.py")):
        if re.search(r"^\s*(import|from)\s+pip\b", path.read_text(), re.MULTILINE):
            pip_hits.append(str(path.relative_to(ROOT)))
    check("插件源码未导入 pip", not pip_hits, evidence=str(pip_hits))

    # 8. pip 入口在上游仍然存在（入口存在 ≠ 被调用；此处固化架构 §14.2 的记录）
    missing = [p for p in PIP_ENTRY_POINTS if not (SOURCE / p).exists()]
    check("架构 §14.2 记录的三个 pip 入口仍可定位", not missing, evidence=str(missing))

    # 9. 插件元数据固定依赖的 AstrBot 版本与实际安装一致
    metadata_text = (PLUGIN / "metadata.yaml").read_text()
    check(
        "metadata.yaml 声明 AstrBot 4.28.1",
        "==4.28.1" in metadata_text,
        evidence=metadata_text.strip().replace("\n", " ")[:120],
    )
    installed = version("astrbot")
    check("实际安装 astrbot 版本为 4.28.1", installed == "4.28.1", evidence=installed)

    total = passed + len(failures)
    if failures:
        print(f"S0-01: {passed}/{total} PASS，失败项：{', '.join(failures)}")
        return 1
    print(f"S0-01: {passed}/{total} PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
