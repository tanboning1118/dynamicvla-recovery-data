#!/usr/bin/env bash
# ===========================================================================
# 04 · 单卡产能实测 —— 先确认「能不能同时跑」，再谈吞吐
#
# ⚠️ 这一步不可跳过，但它的**第一职责不是测吞吐，而是测「能不能跑」**。
#
# 实测结论（2026-11-02，RTX 5060 8G）：第二个 Isaac Sim 实例会卡死在 Omniverse
# kit 初始化，只申请 821 MiB 显存，永不退出、不报错、不超时。根因是 Kit 的全局
# 单例锁（omni.kvdb / omni.fabric / omni.physx），加显存换 4090 也没用。
# 详见 docs/07_concurrency_finding.md。
#
# 所以本脚本的默认值已按实测结论收紧：
#   --max-proc 默认 2   （只做验证，不指望加速）
#   --stagger  默认 15  （强制错峰；实测同时启动必死一个）
# 组二的判定额外要求「所有进程都产出 > 0」，否则直接判死锁 —— 见 docs/07 §8。
#
# 它做三件事：
#   组一 单进程基线    : 显存峰值 / RSS 峰值 / 单条耗时
#   组二 死锁验证      : 1 / 2 进程，各 ROUNDS 次尝试 —— 核心看 B 进程是否产出
#   组三 长跑稳定性    : 单进程连跑 20 次不重启，看 RSS 泄漏斜率
#
# 用法：
#   bash scripts/04_benchmark_single_card.sh --rounds 5
#   bash scripts/04_benchmark_single_card.sh --rounds 5 --max-proc 4 --out /tmp/bench
#   bash scripts/04_benchmark_single_card.sh --skip-soak      # 跳过组三
# ===========================================================================
set -uo pipefail

ROUNDS=5
MAX_PROC=2          # ★ 实测：>1 会死锁。保留 2 是为了让组二能验证这一点
STAGGER=15          # ★ 强制错峰秒数。设 0 会让两个进程同时撞锁
SKIP_SOAK=0
ROOT="${ROOT:-$HOME/dynamicvla-demo}"
OUT="${BENCH_OUT:-/tmp/dvla-bench}"
PY="${VLA_SIM_PY:-$HOME/miniconda3/envs/dvla-isaac45-blackwell/bin/python}"

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLD=$'\033[1m'; RST=$'\033[0m'
ok()   { printf '%s✓%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s!%s %s\n' "$YLW" "$RST" "$*"; }
die()  { printf '%s✗%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }
sec()  { printf '\n%s--- %s ---%s\n' "$BLD" "$*" "$RST"; }
ts()   { date +%s; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --rounds)    ROUNDS="${2:-5}";   shift 2 ;;
        --max-proc)  MAX_PROC="${2:-2}"; shift 2 ;;
        --stagger)   STAGGER="${2:-15}"; shift 2 ;;
        --root)      ROOT="${2:-}";      shift 2 ;;
        --out)       OUT="${2:-}";       shift 2 ;;
        --skip-soak) SKIP_SOAK=1;        shift ;;
        -h|--help)   sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "未知参数：$1" ;;
    esac
done

CODE="$ROOT/dynamic-vla"
GEN="$CODE/simulations/gen_recovery.py"
[[ -f "$GEN" ]] || die "找不到 $GEN（先跑 02_patch_upstream.sh）"
[[ -x "$PY" ]]  || die "python 不可执行：$PY（用 VLA_SIM_PY 指定）"

mkdir -p "$OUT"
RESULT="$OUT/benchmark_result.json"

printf '%s========================================%s\n' "$BLD" "$RST"
printf '%s  04 · 单卡产能实测%s\n' "$BLD" "$RST"
printf '%s========================================%s\n' "$BLD" "$RST"
printf 'python  : %s\n' "$PY"
printf 'rounds  : %d 次尝试/进程\n' "$ROUNDS"
printf 'max_proc: %d\n' "$MAX_PROC"
printf 'out     : %s\n' "$OUT"

# ---- 峰值采样器 -------------------------------------------------------------
peak_gpu() { nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1 | tr -d ' '; }
peak_rss() { ps -o rss= -C python 2>/dev/null | sort -rn | head -1 | tr -d ' '; }

# 后台采样：每 2 s 记一次显存与 RSS 峰值
start_sampler() { # prefix
    local prefix="$1"
    ( while true; do
        printf '%s %s %s\n' "$(ts)" "$(peak_gpu)" "$(peak_rss)"
        sleep 2
      done ) > "$OUT/${prefix}.samples" &
    echo $!
}
stop_sampler() { local pid="$1"; kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; }

summarise_samples() { # file
    awk 'NF==3{if($2>g) g=$2; if($3>r) r=$3} END{printf "%d %d", g/1, r/1024}' "$1"
}

# ---- 单个进程的生成任务 ------------------------------------------------------
run_one() { # tag seed n outdir
    local tag="$1" seed="$2" n="$3" outdir="$4"
    mkdir -p "$outdir/raw"
    rm -f "$outdir/raw/_DONE"
    (
        # gen_recovery.py 用的是裸导入 `import simulate`，必须在 simulations/ 下跑
        cd "$CODE/simulations" || exit 1
        export REC_SLIP=0 REC_GRASP_CHECK=0 REC_MISS_OFFSET=0 REC_DISTURB_AT=-1
        export REC_SLIP_MODE=friction REC_SETTLE_MAX_TIME=3.0
        "$PY" -u "$GEN" --task place --robot franka -n "$n" --seed "$seed" \
            --save --enable_cameras --headless --disable_fabric \
            --keep_imperfect --episode_length 30 -o "$outdir/raw"
    ) > "$OUT/${tag}.log" 2>&1
    return $?
}

# =============================================================================
# 组一：单进程基线
# =============================================================================
sec "组一 · 单进程基线（$ROUNDS 次尝试）"

D1="$OUT/single"; rm -rf "$D1"; mkdir -p "$D1"
SM=$(start_sampler "single")
T0=$(ts)
run_one "single" 100 "$ROUNDS" "$D1"
RC=$?
T1=$(ts)
stop_sampler "$SM"

ELAPSED_SINGLE=$((T1 - T0))
read -r GPU_PEAK RSS_PEAK <<< "$(summarise_samples "$OUT/single.samples")"
N_SAVED=$(find "$D1/raw" -maxdepth 1 -name '*.h5' 2>/dev/null | wc -l)
H5_SIZE=$(du -sm "$D1/raw" 2>/dev/null | awk '{print $1}')

printf '  墙钟        : %d s（%d 次尝试，rc=%d）\n' "$ELAPSED_SINGLE" "$ROUNDS" "$RC"
printf '  产出        : %d 条\n' "$N_SAVED"
printf '  显存峰值    : %d MiB\n' "$GPU_PEAK"
printf '  RSS 峰值    : %d MB\n' "$RSS_PEAK"
[[ $N_SAVED -gt 0 ]] && printf '  单条体积    : %d MB\n' $(( H5_SIZE / N_SAVED ))

# 启动开销估算：读日志里 "Completed setting up" 的次数 × 单次
BOOT_S=$(grep -c 'Completed setting up the environment' "$OUT/single.log" 2>/dev/null || echo 0)
printf '  环境重建次数: %d\n' "$BOOT_S"

if [[ $GPU_PEAK -gt 0 ]]; then
    GPU_MEM_TOTAL=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1 | tr -d ' ')
    MAX_BY_GPU=$(( (GPU_MEM_TOTAL - 1200) / (GPU_PEAK > 0 ? GPU_PEAK : 1) ))
    [[ $MAX_BY_GPU -lt 1 ]] && MAX_BY_GPU=1
    ok "按显存推算最多 $MAX_BY_GPU 个并发进程（总 ${GPU_MEM_TOTAL} MiB，留 1.2 GB 余量）"
else
    MAX_BY_GPU=1
    warn "未采到显存峰值"
fi

MEM_TOTAL_GB=$(free -g 2>/dev/null | awk '/^Mem:/{print $2}')
MAX_BY_RAM=$(( (${MEM_TOTAL_GB:-16} - 4) * 1024 / (RSS_PEAK > 0 ? RSS_PEAK : 8192) ))
[[ $MAX_BY_RAM -lt 1 ]] && MAX_BY_RAM=1
ok "按内存推算最多 $MAX_BY_RAM 个并发进程（总 ${MEM_TOTAL_GB:-?} GB，每进程 ${RSS_PEAK} MB）"

SUGGEST=$MAX_BY_GPU
[[ $MAX_BY_RAM -lt $SUGGEST ]] && SUGGEST=$MAX_BY_RAM
[[ $SUGGEST -gt $MAX_PROC ]] && SUGGEST=$MAX_PROC
warn "组一给了「资源上界 $SUGGEST 个」——但这是算术，不是能不能跑。"
warn "  实测：资源够 ≠ 能启动。第二个 Kit 实例会卡在 omni.physx 上（只占 821 MiB）。"
warn "  真正的结论由组二给出，组一只能证伪（不够）不能证实（能跑）。"

# =============================================================================
# 组二：并发扫描 / 死锁验证
# =============================================================================
sec "组二 · 并发扫描（1..$MAX_PROC 进程，各 $ROUNDS 次尝试）"
warn "本组的第一职责不是测吞吐，是测「多个实例能不能同时活着」"
warn "判据：任一进程产出 0 条 → 判死锁（实测只占 821 MiB 就是卡在 kit 初始化）"

declare -A T_TOTAL T_SAVED DEAD
CONC_JSON="{"
for N in $(seq 1 "$MAX_PROC"); do
    D="$OUT/conc$N"; rm -rf "$D"; mkdir -p "$D"
    SM=$(start_sampler "conc$N")
    T0=$(ts)
    pids=()
    for k in $(seq 1 "$N"); do
        run_one "conc${N}_p${k}" $((200 + N*100 + k*10)) "$ROUNDS" "$D/p$k" &
        pids+=($!)
        # 强制错峰，避开 Kit 初始化争抢。--stagger 0 会必死一个。
        [[ $k -lt $N ]] && sleep "$STAGGER"
    done
    for p in "${pids[@]}"; do wait "$p" 2>/dev/null || true; done
    T1=$(ts)
    stop_sampler "$SM"

    EL=$((T1 - T0))
    NS=$(find "$D" -name '*.h5' 2>/dev/null | wc -l)
    ATT=$(( N * ROUNDS ))
    read -r GP RP <<< "$(summarise_samples "$OUT/conc$N.samples")"

    # ★ 逐进程产出：这是死锁判据的核心。
    # 只看总产出会被误导 —— 2 进程时 A 产出 2 条、B 卡死 0 条，总数仍是 2，
    # 看起来「跑通了」，实际吞吐比单进程还差（实测 515 s vs 121 s）。
    DEAD_N=0
    for k in $(seq 1 "$N"); do
        c=$(find "$D/p$k" -name '*.h5' 2>/dev/null | wc -l)
        [[ "$c" -eq 0 ]] && DEAD_N=$((DEAD_N + 1))
    done
    DEAD[$N]=$DEAD_N

    T_TOTAL[$N]=$EL
    T_SAVED[$N]=$NS

    if [[ $NS -gt 0 ]]; then
        PER=$(awk "BEGIN{printf \"%.1f\", $EL / $NS}")
        PER_ATT=$(awk "BEGIN{printf \"%.1f\", $EL / $ATT}")
    else
        PER="n/a"; PER_ATT="n/a"
    fi

    if [[ $DEAD_N -gt 0 ]]; then
        printf '  %d 进程: 墙钟 %5d s / 产出 %2d / %d 尝试 → %s s/条  显存 %5d MiB  %s死锁 %d 个进程%s\n' \
               "$N" "$EL" "$NS" "$ATT" "$PER" "$GP" "$RED" "$DEAD_N" "$RST"
    else
        printf '  %d 进程: 墙钟 %5d s / 产出 %2d / %d 尝试 → %s s/条  显存 %5d MiB  RSS %5d MB\n' \
               "$N" "$EL" "$NS" "$ATT" "$PER" "$GP" "$RP"
    fi

    CONC_JSON+="\"$N\":{\"total_s\":$EL,\"saved\":$NS,\"attempts\":$ATT,\"gpu_peak_mib\":$GP,\"rss_peak_mb\":$RP,\"dead_procs\":$DEAD_N},"
done
CONC_JSON="${CONC_JSON%,}}"

# 线性度 = (1 进程 s/条) / (N 进程 s/条)
sec "线性度"
printf '  %-8s %-12s %-12s %-10s\n' "并发" "s/条" "线性度" "判定"
BEST=$SUGGEST
BEST_LIN=0
for N in $(seq 1 "$MAX_PROC"); do
    NS=${T_SAVED[$N]:-0}
    DN=${DEAD[$N]:-0}
    if [[ "$NS" -gt 0 ]]; then
        PER=$(awk "BEGIN{print $ELAPSED_SINGLE / $NS}")
        if [[ "${T_SAVED[1]:-0}" -gt 0 ]]; then
            BASE=$(awk "BEGIN{print $ELAPSED_SINGLE / ${T_SAVED[1]}}")
            LIN=$(awk "BEGIN{printf \"%.2f\", $BASE / $PER}")
        else
            LIN="n/a"
        fi
        # ★ 有进程死锁时，无论线性度多好看都判不可用。
        if [[ "$DN" -gt 0 ]]; then
            VERDICT="死锁（$DN 个进程零产出）"
        elif awk "BEGIN{exit !($LIN >= 0.70)}" 2>/dev/null; then
            VERDICT="可用"
            if awk "BEGIN{exit !($LIN > $BEST_LIN)}" 2>/dev/null; then
                BEST_LIN=$LIN; BEST=$N
            fi
        else
            VERDICT="不可用（争抢严重）"
        fi
        printf '  %-8s %-12s %-12s %-10s\n' "$N" "$PER" "$LIN" "$VERDICT"
    else
        printf '  %-8s %-12s %-12s %-10s\n' "$N" "-" "-" "该并发零产出"
    fi
done

if [[ "${DEAD[$MAX_PROC]:-0}" -gt 0 || $BEST_LIN -eq 0 ]]; then
    warn "组二结论：单卡只能串行跑 1 个实例 —— 这是 Omniverse Kit 的全局单例锁，"
    warn "  不是显存不够。换 4090 也一样。要提速只能多台机器分 shard。"
    warn "  详见 docs/07_concurrency_finding.md"
else
    ok "组二结论：推荐并发 $BEST（线性度 $BEST_LIN）"
fi

# =============================================================================
# 组三：长跑稳定性
# =============================================================================
if [[ $SKIP_SOAK -eq 0 ]]; then
    sec "组三 · 长跑稳定性（单进程 20 次尝试不重启）"
    D3="$OUT/soak"; rm -rf "$D3"; mkdir -p "$D3"
    SM=$(start_sampler "soak")
    T0=$(ts)
    run_one "soak" 700 20 "$D3"
    T1=$(ts)
    stop_sampler "$SM"
    read -r GP3 RP3 <<< "$(summarise_samples "$OUT/soak.samples")"

    # RSS 斜率：首末采样点之差 / 尝试次数
    FIRST=$(head -1 "$OUT/soak.samples" 2>/dev/null | awk '{print $3}')
    LAST=$(tail -1 "$OUT/soak.samples" 2>/dev/null | awk '{print $3}')
    if [[ -n "${FIRST:-}" && -n "${LAST:-}" && "${FIRST:-0}" -gt 0 ]]; then
        GROWTH_MB=$(( (LAST - FIRST) / 1024 ))
        PER_EP=$(( GROWTH_MB / 20 ))
        printf '  RSS: %d MB → %d MB（+%d MB over 20 ep，约 %d MB/ep）\n' \
               $((FIRST/1024)) $((LAST/1024)) "$GROWTH_MB" "$PER_EP"
        printf '  墙钟: %d s\n' $((T1 - T0))
        printf '  显存峰值: %d MiB\n' "$GP3"

        MEM_AVAIL=$(( (${MEM_TOTAL_GB:-16} - 2) * 1024 ))
        SAFE_CHUNK=$(( (MEM_AVAIL - RP3) / (PER_EP > 0 ? PER_EP : 1) ))
        [[ $SAFE_CHUNK -gt 40 ]] && SAFE_CHUNK=40
        [[ $SAFE_CHUNK -lt 5 ]] && SAFE_CHUNK=5
        ok "组三结论：CHUNK 建议 ≤ $SAFE_CHUNK（按 RSS 增长 $PER_EP MB/ep 推算）"
    else
        warn "组三未采到有效 RSS 数据"
        PER_EP=0; SAFE_CHUNK=20
    fi
else
    PER_EP=0; SAFE_CHUNK=20
    warn "跳过组三（--skip-soak），CHUNK 默认按 20"
fi

# =============================================================================
# 写结果 + 打印建议
# =============================================================================
cat > "$RESULT" <<JSON
{
  "host": "$(hostname)",
  "gpu": "$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)",
  "gpu_total_mib": $(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1 | tr -d ' '),
  "cpu_cores": $(nproc),
  "mem_gb": ${MEM_TOTAL_GB:-0},
  "rounds": $ROUNDS,
  "single": {"total_s": $ELAPSED_SINGLE, "saved": $N_SAVED, "gpu_peak_mib": $GPU_PEAK, "rss_peak_mb": $RSS_PEAK},
  "concurrency": $CONC_JSON,
  "recommend": {"bn": $BEST, "chunk": $SAFE_CHUNK, "linearity": $BEST_LIN, "rss_mb_per_ep": $PER_EP}
}
JSON

sec "结论"
printf '  推荐并发 BN     : %s\n' "$BEST"
printf '  推荐 CHUNK      : %s\n' "$SAFE_CHUNK"
printf '  线性度          : %s\n' "$BEST_LIN"
printf '  RSS 泄漏        : %s MB/ep\n' "$PER_EP"
printf '  结果已写        : %s\n' "$RESULT"

printf '\n%s把 BN 与 CHUNK 填进正式量产命令：%s\n' "$BLD" "$RST"
printf '  BN=%s CHUNK=%s bash scripts/06_run_campaign.py --profiles ...\n' "$BEST" "$SAFE_CHUNK"

if [[ "$BEST" -le 1 ]]; then
    printf '\n%s!! 并发上限只有 1 —— 这台机器不够跑多进程。%s\n' "$YLW" "$RST"
    printf '%s   检查显存与内存；或接受单进程长跑（330 条约 12 h）。%s\n' "$YLW" "$RST"
fi
if [[ "$BEST_LIN" != "0" ]] && awk "BEGIN{exit !($BEST_LIN < 0.70)}" 2>/dev/null; then
    printf '\n%s!! 所有并发度线性度都 < 0.70，多进程没有收益。%s\n' "$RED" "$RST"
    printf '%s   实测常见原因：Isaac Sim 的 kit 初始化抢锁（同时启动会卡死）。%s\n' "$YLW" "$RST"
    printf '%s   缓解：错峰启动（本脚本已内置 15 s 间隔），或改用单进程串行。%s\n' "$YLW" "$RST"
fi