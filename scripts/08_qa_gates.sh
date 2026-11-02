#!/usr/bin/env bash
# ===========================================================================
# 08 · 生产闸门（payload/qa_gates.py 的薄封装）
#
# 真正的实现在 payload/qa_gates.py —— 它跟 02_patch_upstream.sh 一起被拷进上游
# 代码目录，跟着代码走（因为它要 import 上游的 utils.event_labels 才能算出
# 真实标签分布，脱离上游代码没法工作）。
#
# 这个 wrapper 做三件事：
#   1. 自动找到 payload 里的 qa_gates.py（不要求你在仓库根目录）
#   2. 用上游代码目录当 PYTHONPATH，让 import utils.* 能成功
#   3. 把结果同时写屏幕 + <batch>/qa_gates.json（供 10_report.py 消费）
#
# 用法：
#   bash scripts/08_qa_gates.sh ~/dvla-gen/a_slip
#   bash scripts/08_qa_gates.sh ~/dvla-gen/a_slip --profile a_slip
#   bash scripts/08_qa_gates.sh --root ~/dvla-gen --all     # 逐个子目录全跑
#
# 退出码：0 = PASS / 1 = 有产出但闸门不过 / 2 = 零产出（qa_gates 原样透传）
# ===========================================================================
set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
QA="$HERE/payload/qa_gates.py"
ROOT="${ROOT:-$HOME/dynamicvla-demo}"
PY="${VLA_SIM_PY:-python3}"

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLD=$'\033[1m'; RST=$'\033[0m'
ok()   { printf '%s✓%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s!%s %s\n' "$YLW" "$RST" "$*"; }
die()  { printf '%s✗%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }

[[ -f "$QA" ]] || die "找不到 $QA"
command -v "$PY" >/dev/null 2>&1 || die "找不到 python：$PY（用 VLA_SIM_PY 指定）"

# ---- 参数分流：wrapper 自己的 --root/--all 之外，全部原样透传给 qa_gates ----
BASE_ARG=""
ALL=0
PASS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --root) ROOT="${2:-}"; shift 2 ;;
        --all)  ALL=1; shift ;;
        *)      PASS+=("$1"); shift ;;
    esac
done
[[ ${#PASS[@]} -gt 0 ]] && BASE_ARG="${PASS[0]}"

if [[ -z "$BASE_ARG" ]]; then
    die "缺 batch 目录。用法：bash scripts/08_qa_gates.sh <batch_dir> [--profile a_slip]"
fi
# 允许 ~ 展开
BASE_ARG="${BASE_ARG/#\~/$HOME}"

# 上游代码目录进 PYTHONPATH —— qa_gates 要 import utils.event_labels
# 上游 layout 兼容两种：<root>/dynamic-vla（工位机）与 <root>/official（DSW）
PP=""
for c in "$ROOT/dynamic-vla" "$ROOT/official" "$ROOT"; do
    [[ -d "$c/utils" ]] && PP="$c:${PP}"
done
export PYTHONPATH="${PP}${PYTHONPATH:-}"

printf '%s========================================%s\n' "$BLD" "$RST"
printf '%s  08 · 生产闸门%s\n' "$BLD" "$RST"
printf '%s========================================%s\n' "$BLD" "$RST"
printf 'gate    : %s\n' "$QA"
printf 'python  : %s\n' "$PY"
printf 'batch   : %s\n' "$BASE_ARG"
printf 'code    : %s\n' "${PP:-（未找到含 utils/ 的代码目录）}"

run_one() {
    local dir="$1"; shift
    [[ -d "$dir" ]] || { warn "跳过（不存在）：$dir"; return 0; }
    "$PY" "$QA" "$dir" "$@"
    local rc=$?
    # 落盘 JSON 供 10_report.py 汇总（这是它判断「闸门全绿」的唯一依据）
    if [[ $rc -ne 2 ]]; then
        "$PY" "$QA" "$dir" "$@" --json > "$dir/qa_gates.json" 2>/dev/null \
            && ok "已落盘 $dir/qa_gates.json" \
            || warn "未能落盘 JSON（$dir）"
    fi
    return $rc
}

if [[ $ALL -eq 1 ]]; then
    printf '\n'
    mapfile -t DIRS < <(find "$BASE_ARG" -maxdepth 3 -type d \
        \( -name 'raw' -o -name 'lerobot_home' -o -name 'h5' \) -prune -o \
        -maxdepth 2 -type d -print 2>/dev/null | tail -n +2 | sort -u)
    [[ ${#DIRS[@]} -eq 0 ]] && die "在 $BASE_ARG 下没找到批次子目录"
    RC_ALL=0
    for d in "${DIRS[@]}"; do
        printf '\n--- %s ---\n' "$d"
        run_one "$d" "${PASS[@]:1}" || RC_ALL=$?
    done
    exit $RC_ALL
fi

printf '\n'
run_one "$BASE_ARG" "${PASS[@]:1}"
exit $?