#!/usr/bin/env bash
# S0 技术可行性（离线部分）核验汇总入口。S1 的离线核验脚本（check_single_call）也在
# 这里执行：它钉住的是框架约束，与 S0 的核验同源。
#
# 只跑离线可核验的部分；S0-07（DeepSeek 凭据）与 S0-09（NapCat/QQ）及一切
# 在线验证不在本脚本范围内，S0 阶段门在本脚本全绿后仍然未通过。
#
# 用法：bash scripts/s0/run_all.sh
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 1

export UV_CACHE_DIR="$ROOT/.cache/uv"
export ASTRBOT_BUILD_DASHBOARD=0

run() {
    env -u VIRTUAL_ENV uv run --project "$ROOT/.runtime/astrbot" --no-sync --offline \
        python -B "$@"
}

failed=()

for name in check_env check_outbound check_gating check_collect check_temp check_history check_single_call; do
    printf '\n========== %s ==========\n' "$name"
    if ! run "$ROOT/scripts/s0/$name.py"; then
        failed+=("$name")
    fi
done

printf '\n========== tests/（既有回归） ==========\n'
if ! run -m unittest discover -s "$ROOT/tests"; then
    failed+=("tests")
fi

printf '\n========== 汇总 ==========\n'
if [ ${#failed[@]} -gt 0 ]; then
    printf 'FAILED: %s\n' "${failed[*]}"
    exit 1
fi

printf 'S0 离线核验全部通过。\n'
printf '注意：S0-07、S0-09 及 S0-02/03/04/05 的在线部分仍未验证；S0 阶段门于 2026-09-17 被维护者决定有意跳过（不是通过）。\n'
printf 'S1：S1-01—S1-15 的离线部分已完成（含装配与离线回归）；S1-10 的在线部分与 S1-16 阶段门仍阻塞，详见 docs/03 第 5.2 节与附录 D.3。\n'
printf 'S2：S2-01—S2-07 的离线部分已完成（存储与群策略、环形缓冲、成员退出/加入、告知两步开启与维护者命令、动态材料注入、互动历史限制）；S2-08/S2-09 待做，S2-02/S2-05 的前置已由维护者确认（值只进运行时配置），S2-10 阶段门未通过。详见 docs/03 第 5.3、5.2（实施判断）与 9.5 节。\n'
