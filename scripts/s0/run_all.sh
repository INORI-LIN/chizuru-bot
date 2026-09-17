#!/usr/bin/env bash
# S0 技术可行性（离线部分）核验汇总入口。
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

for name in check_env check_outbound check_gating check_collect check_temp check_history; do
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
printf '注意：S0-07、S0-09 及 S0-02/03/04/05 的在线部分仍未验证，S0 阶段门未通过，不得进入 S1。\n'
