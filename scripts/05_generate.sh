#!/usr/bin/env bash
# ===========================================================================
# 05 · 按 profile 生成一批
#
# 用法：
#   bash scripts/05_generate.sh --profile profiles/a_slip.env --n 20 --seed 9000
#   bash scripts/05_generate.sh --profile profiles/b_miss.env --n 4 --tag calib_b
#   bash scripts/05_generate.sh --profile profiles/a_slip.env --n 20 --dry-run
#
# 行为：
#   1. source profile（参数全显式，不依赖任何默认值）
#   2. 落 run_config.json 快照（G5 闸门据此判可复现）
#   3. 生成（tmux 后台 + _DONE 哨兵，断线不中断）
#   4. 自动跑 qa_gates.py
#
# --dry-run：只生成不过闸门，用于小批校准产出率
# ===========================================================================
set -uo pipefail

PROFILE=""
N=4
SEED=9000
TAG=""
DRY_RUN=0
ROOT="${ROOT:-$HOME/dynamicvla-demo}"
OUT_ROOT="${OUT_ROOT:-$HOME/dvla-gen}"
USE_TMUX=1
NO_TMUX=0

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLD=$'\033[1m'; RST=$'\033[0m'
ok()   { printf '%s✓%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s!%s %s\n' "$YLW" "$RST" "$*"; }
die()  { printf '%s✗%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }
log()  { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile) PROFILE="${2:-}"; shift 2 ;;
        --n)       N="${2:-4}";      shift 2 ;;
        --seed)    SEED="${2:-9000}"; shift 2 ;;
        --tag)     TAG="${2:-}";     shift 2 ;;
        --root)    ROOT="${2:-}";    shift 2 ;;
        --out)     OUT_ROOT="${2:-}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --no-tmux) NO_TMUX=1; USE_TMUX=0; shift ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "未知参数：$1" ;;
    esac
done

[[ -n "$PROFILE" ]] || die "必须指定 --profile"
[[ -f "$PROFILE" ]] || die "profile 不存在：$PROFILE"

# ---- 载入 profile -----------------------------------------------------------
# shellcheck disable=SC1090
source "$PROFILE"
[[ -n "${PROFILE_NAME:-}" ]] || die "profile 未定义 PROFILE_NAME"

TAG="${TAG:-${PROFILE_NAME}_$(date +%m%d_%H%M%S)}"
BASE="$OUT_ROOT/$TAG"
RAW="$BASE/raw"
CODE="$ROOT/dynamic-vla"
GEN="$CODE/simulations/gen_recovery.py"
PY="${VLA_SIM_PY:-$HOME/miniconda3/envs/dvla-isaac45-blackwell/bin/python}"

[[ -f "$GEN" ]] || die "找不到 $GEN（先跑 02_patch_upstream.sh）"

printf '%s========================================%s\n' "$BLD" "$RST"
printf '%s  05 · 生成 · %s（%s 类）%s\n' "$BLD" "$PROFILE_NAME" "${PROFILE_CLASS:-?}" "$RST"
printf '%s========================================%s\n' "$BLD" "$RST"
printf 'tag     : %s\n' "$TAG"
printf 'try     : %d 次（期望产出 ≈ %s 条）\n' "$N" \
       "$(awk "BEGIN{printf \"%.0f\", $N * ${EXPECT_YIELD:-0.4}}")"
printf 'seed    : %d\n' "$SEED"
printf 'out     : $RAW\n' 2>/dev/null || printf 'out     : %s\n' "$RAW"

# ---- 关键参数回显（让人一眼能看出配错没）------------------------------------
printf '\n%s注入参数%s\n' "$BLD" "$RST"
for k in REC_SLIP REC_SLIP_MODE REC_SLIP_FRICTION REC_SLIP_MASS_SCALE \
         REC_GRASP_CHECK REC_MISS_OFFSET REC_DISTURB_AT REC_DISTURB_VEL \
         REC_SETTLE_MAX_TIME REC_MAX_ATTEMPTS; do
    printf '  %-24s = %s\n' "$k" "${!k:-<未设置>}"
done

# ---- 危险组合自检 -----------------------------------------------------------
DANGER=0
if [[ "${PROFILE_CLASS}" == "B" ]]; then
    if [[ "${REC_SLIP:-1}" != "0" ]]; then
        warn "B 类必须 REC_SLIP=0。当前 ${REC_SLIP}：MISS_OFFSET 只加 _grasp_misses 不动 attempt，"
        warn "  重抓后 attempt 仍为 0 → 必定再滑脱一次 → 轨迹拉长到 1000 帧，30 s 内 0% 产出"
        DANGER=1
    fi
    if [[ "${GEN_EXTRA_FLAGS:-}" != *"--keep_imperfect"* ]]; then
        die "B 类必须 --keep_imperfect。纯抓空不含 slip，--require_slip 会整条丢掉 → 产出 0"
    fi
fi
if [[ "${PROFILE_CLASS}" == "A" ]]; then
    if [[ "${REC_SLIP_MODE:-}" != "friction" ]]; then
        warn "A 类必须 REC_SLIP_MODE=friction。当前 '${REC_SLIP_MODE:-}'：open 模式下夹爪张开，"
        warn "  滑脱对训练 100% 不可见（标签只有 lost，学不到「以为还握着」）"
        DANGER=1
    fi
    if [[ "${GEN_EXTRA_FLAGS:-}" != *"--require_slip"* ]]; then
        warn "A 类建议 --require_slip，否则会混入没滑脱的脏样本"
    fi
fi
if [[ "${PROFILE_CLASS}" == "C" || "${PROFILE_CLASS}" == "D" ]]; then
    if awk "BEGIN{exit !(${REC_SETTLE_MAX_TIME:-3} < 5)}" 2>/dev/null; then
        warn "C/D 类建议 REC_SETTLE_MAX_TIME ≥ 5（当前 ${REC_SETTLE_MAX_TIME}）。"
        warn "  扰动/重放后物体滚很久，3 s 会全部判 GIVE_UP"
    fi
fi
[[ $DANGER -eq 1 ]] && die "参数组合有已知的零产出现象，先改 profile"

# ---- 落配置快照 -------------------------------------------------------------
mkdir -p "$RAW"
rm -f "$RAW/_DONE"

python3 - "$BASE" "$TAG" "$N" "$SEED" "$PROFILE" <<'PY'
import json, os, sys
base, tag, n, seed, prof = sys.argv[1:6]
keys = [k for k in os.environ if k.startswith("REC_")]
cfg = {k: os.environ[k] for k in sorted(keys)}
cfg.update({
    "tag": tag,
    "profile_file": os.path.abspath(prof),
    "profile_name": os.environ.get("PROFILE_NAME"),
    "profile_class": os.environ.get("PROFILE_CLASS"),
    "n_attempts": int(n),
    "seed": int(seed),
    "gen_task": os.environ.get("GEN_TASK"),
    "gen_robot": os.environ.get("GEN_ROBOT"),
    "gen_episode_length": os.environ.get("GEN_EPISODE_LENGTH"),
    "gen_extra_flags": os.environ.get("GEN_EXTRA_FLAGS"),
    "expect_yield": os.environ.get("EXPECT_YIELD"),
})
with open(os.path.join(base, "run_config.json"), "w", encoding="utf-8") as fp:
    json.dump(cfg, fp, indent=2, ensure_ascii=False)
print("  配置快照 -> %s/run_config.json（%d 个 REC_* 参数）" % (base, len(keys)))
PY

# ---- 组装命令 ---------------------------------------------------------------
EP_LEN="${GEN_EPISODE_LENGTH:-30}"
EXTRA="${GEN_EXTRA_FLAGS:-}"
CMD="cd $(printf %q "$CODE/simulations") && \
REC_SLIP=${REC_SLIP:-0} REC_SLIP_MODE=${REC_SLIP_MODE:-open} \
REC_SLIP_FRICTION=${REC_SLIP_FRICTION:-0.02} REC_SLIP_MASS_SCALE=${REC_SLIP_MASS_SCALE:-1.0} \
REC_SLIP_LIFT_HEIGHT=${REC_SLIP_LIFT_HEIGHT:-0.06} REC_SLIP_FRICTION_TIME=${REC_SLIP_FRICTION_TIME:-1.2} \
REC_SLIP_SLIDE_DIST=${REC_SLIP_SLIDE_DIST:-0.015} REC_SLIP_STEP_MAX=${REC_SLIP_STEP_MAX:-20} \
REC_SETTLE_MIN_TIME=${REC_SETTLE_MIN_TIME:-0.4} REC_SETTLE_MAX_TIME=${REC_SETTLE_MAX_TIME:-3.0} \
REC_MAX_ATTEMPTS=${REC_MAX_ATTEMPTS:-2} \
REC_GRASP_JITTER_XY=${REC_GRASP_JITTER_XY:-0} REC_GRASP_JITTER_YAW=${REC_GRASP_JITTER_YAW:-0} \
REC_GRASP_CHECK=${REC_GRASP_CHECK:-0} REC_GRASP_CHECK_DELAY=${REC_GRASP_CHECK_DELAY:-10} \
REC_GRASP_MIN_LIFT=${REC_GRASP_MIN_LIFT:-0.005} REC_GRASP_MISS_DIST=${REC_GRASP_MISS_DIST:-0.03} \
REC_MAX_MISSES=${REC_MAX_MISSES:-2} REC_MISS_OFFSET=${REC_MISS_OFFSET:-0} \
REC_DISTURB_AT=${REC_DISTURB_AT:--1} REC_DISTURB_VEL=${REC_DISTURB_VEL:-0} \
REC_DISTURB_AXIS=${REC_DISTURB_AXIS:-x} REC_DISTURB_RANGE=${REC_DISTURB_RANGE:-0.15} \
$(printf %q "$PY") -u $(printf %q "$GEN") \
  --task ${GEN_TASK:-place} --robot ${GEN_ROBOT:-franka} \
  -n $N --seed $SEED --save --enable_cameras --headless \
  $EXTRA --episode_length $EP_LEN -o $(printf %q "$RAW")"

printf '\n%s命令%s\n' "$BLD" "$RST"
printf '%s\n\n' "$CMD" | sed 's/ && /\n  /; s/--/\n    --/g' | head -20

# ---- 执行 -------------------------------------------------------------------
T0=$(date +%s)
if [[ $NO_TMUX -eq 1 ]]; then
    log "前台运行（--no-tmux）"
    bash -c "$CMD" 2>&1 | tee "$BASE/gen.log"
    RC=${PIPESTATUS[0]}
else
    SESS="dvla-$TAG"
    if tmux has-session -t "$SESS" 2>/dev/null; then
        die "tmux 会话 $SESS 已存在"
    fi
    log "tmux 会话：$SESS（断线不中断；./isaac-run.sh log 或 tmux attach -t $SESS）"
    tmux new-session -d -s "$SESS" "bash -c $(printf %q "$CMD") 2>&1 | tee $(printf %q "$BASE/gen.log"); echo DONE_$? >> $(printf %q "$BASE/gen.log")"

    # 等 _DONE 或进程退出
    while true; do
        [[ -f "$RAW/_DONE" ]] && break
        if ! tmux has-session -t "$SESS" 2>/dev/null; then
            sleep 3
            [[ -f "$RAW/_DONE" ]] && break
            warn "tmux 会话已退出但无 _DONE，看 $BASE/gen.log"
            break
        fi
        sleep 10
    done
    tmux kill-session -t "$SESS" 2>/dev/null || true
    RC=0
fi
T1=$(date +%s)
ELAPSED=$((T1 - T0))

# ---- 结果 -------------------------------------------------------------------
N_SAVED=$(find "$RAW" -maxdepth 1 -name '*.h5' 2>/dev/null | wc -l)
YIELD=$(awk "BEGIN{printf \"%.1f\", 100.0 * $N_SAVED / $N}")
SIZE=$(du -sh "$RAW" 2>/dev/null | awk '{print $1}')

printf '\n%s════ 结果 ════%s\n' "$BLD" "$RST"
printf '  尝试     : %d\n' "$N"
printf '  产出     : %s 条（%s%%，期望 %s%%）\n' "$N_SAVED" "$YIELD" \
       "$(awk "BEGIN{printf \"%.0f\", ${EXPECT_YIELD:-0.4} * 100}")"
printf '  墙钟     : %d s（单尝试 %s s）\n' "$ELAPSED" \
       "$(awk "BEGIN{printf \"%.1f\", $ELAPSED / $N}")"
printf '  磁盘     : %s\n' "$SIZE"
printf '  目录     : %s\n' "$BASE"

if [[ -f "$RAW/_DONE" ]]; then
    printf '  _DONE    : %s\n' "$(cat "$RAW/_DONE")"
fi

# 产出率偏离告警
EXP="${EXPECT_YIELD:-0.4}"
if awk "BEGIN{exit !($N_SAVED / $N < $EXP * 0.6)}" 2>/dev/null; then
    warn "产出率 $YIELD%% 显著低于期望 $(awk "BEGIN{printf \"%.0f\", $EXP*100}")%"
    warn "  检查 $BASE/gen.log 里的 'no placement' / 'discarded'"
    warn "  常见原因见 docs/06_troubleshooting.md"
fi

# ---- 闸门 -------------------------------------------------------------------
if [[ $DRY_RUN -eq 1 ]]; then
    printf '\n%s跳过闸门（--dry-run）%s\n' "$YLW" "$RST"
    exit 0
fi

# payload/qa_gates.py 跟着代码走（02_patch_upstream.sh 会拷一份到上游目录），
# 所以优先用本仓库 payload 里的那份，退回上游目录里的副本。
# （HERE 已在文件头定义）
GATE=""
for c in "$HERE/payload/qa_gates.py" "$ROOT/dynamic-vla/qa_gates.py" \
         "$ROOT/official/qa_gates.py"; do
    [[ -f "$c" ]] && { GATE="$c"; break; }
done

if [[ -n "$GATE" ]]; then
    printf '\n%s════ 闸门 ════%s\n' "$BLD" "$RST"
    # qa_gates 要 import utils.event_labels，上游代码目录必须在 PYTHONPATH 里
    PP=""
    for c in "$ROOT/dynamic-vla" "$ROOT/official"; do
        [[ -d "$c/utils" ]] && PP="$c:${PP}"
    done
    export PYTHONPATH="${PP}${PYTHONPATH:-}"
    GPY="${VLA_MODEL_PY:-python3}"
    "$GPY" "$GATE" "$BASE" 2>&1 | tee "$BASE/gates.log"
    # 落盘 JSON 供 10_report.py 汇总
    "$GPY" "$GATE" "$BASE" --json > "$BASE/qa_gates.json" 2>/dev/null || true
else
    warn "找不到 qa_gates.py，跳过闸门（先跑 02_patch_upstream.sh）"
fi

printf '\n下一步：bash scripts/07_convert_lerobot.sh --base %s --repo local/%s\n' "$BASE" "$TAG"