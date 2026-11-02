#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# ===========================================================================
# 4090 实例上的「链路快速验证」脚本
#
# 目的：在有限的租用时间里，用最少的 episode 验证整条链路是否通，
#       并同时量出真实吞吐和画面质量。
#
# 设计原则
#   1. 先跑 1 条最短的（--num_envs 1，几秒就出结果），确认能起得来
#   2. 再跑 batch，量吞吐 + 产出率（带 slip 的比例）
#   3. 最后跑噪声探针，与 DOM 基线（0.47~0.56）对比
#
# 用法：
#   bash 20_smoke_4090.sh                # 默认 1 条
#   bash 20_smoke_4090.sh --n 4          # 跑 4 条
#   bash 20_smoke_4090.sh --strict       # 要求必须带 slip
# ===========================================================================
set -uo pipefail

CODE="${CODE:-/root/gpufree-data/work/code}"
OUT="${OUT:-/root/gpufree-data/work/out}"
ISAAC_PY="${ISAAC_PY:-/root/isaacsim/python.sh}"
N=1
STRICT=0
EXTRA=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --n)      N="${2:-1}"; shift 2 ;;
        --strict) STRICT=1; shift ;;
        --out)    OUT="${2:-}"; shift 2 ;;
        *) EXTRA+=("$1"); shift ;;
    esac
done

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLD=$'\033[1m'; RST=$'\033[0m'
ok()   { printf '%s✓%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s!%s %s\n' "$YLW" "$RST" "$*"; }
die()  { printf '%s✗%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }

# 缓存全部指到数据盘，避免 30G 系统盘被吃光
export PIP_CACHE_DIR=/root/gpufree-data/pip
export XDG_CACHE_HOME=/root/gpufree-data/cache
export OV_CACHE_PATH=/root/gpufree-data/ovcache
export PYTHONPATH="$CODE:$CODE/simulations"

printf '%s\n' "${BLD}== 20_smoke_4090 =="${RST}
printf 'code   : %s\n' "$CODE"
printf 'out    : %s\n' "$OUT"
printf 'n      : %s\n' "$N"
printf 'strict : %s\n\n' "$STRICT"

[[ -d "$CODE/simulations" ]] || die "找不到 $CODE/simulations"
[[ -f "$CODE/simulations/gen_recovery.py" ]] || die "找不到 gen_recovery.py"
[[ -d "$CODE/scenes" ]] || die "找不到 scenes/（DOM-Test 没解压）"
[[ -d "$CODE/objects" ]] || die "找不到 objects/（DOM-3D-Objects 没解压）"
ok "资产与代码就位"

n_scene=$(find "$CODE/scenes" -maxdepth 1 -name '*.usd' 2>/dev/null | wc -l)
n_obj=$(find "$CODE/objects" -name '*.usd' 2>/dev/null | wc -l)
printf '  场景 usd: %s   对象 usd: %s\n' "$n_scene" "$n_obj"
(( n_scene > 0 )) || die "scenes 里没有 usd"
(( n_obj > 0 ))   || die "objects 里没有 usd"

mkdir -p "$OUT"

# ---- 关键：gen_recovery.py 是裸 `import simulate`，必须在 simulations/ 下运行
cd "$CODE/simulations" || die "cd 失败"

ARGS=(
    --task place --robot franka
    -n "$N"
    --save --enable_cameras --headless --disable_fabric
    --episode_length 30
    --scene_dir "$CODE/scenes"
    --object_dir "$CODE/objects"
    -o "$OUT"
)
(( STRICT == 1 )) && ARGS+=(--require_slip)
(( ${#EXTRA[@]} > 0 )) && ARGS+=("${EXTRA[@]}")

printf '\n%s命令%s: %s %s\n\n' "$BLD" "$RST" "$ISAAC_PY" "${ARGS[*]}"
printf '%s\n' "---------------------------------------------------------------"

T0=$(date +%s)
"$ISAAC_PY" gen_recovery.py "${ARGS[@]}" 2>&1 | tee "$OUT/run.log" | tail -40
RC=${PIPESTATUS[0]}
T1=$(date +%s)
ELAPSED=$(( T1 - T0 ))

printf '%s\n' "---------------------------------------------------------------"
printf '退出码: %s   墙钟: %s s\n' "$RC" "$ELAPSED"

# ---- 统计产出
n_h5=$(find "$OUT" -maxdepth 1 -name '*.h5' 2>/dev/null | wc -l)
printf '产出 h5: %s 条\n' "$n_h5"
if (( n_h5 > 0 )); then
    du -sh "$OUT" 2>/dev/null | sed 's/^/总占用: /'
    # 单条平均体积 —— 用于估算 300 条的磁盘需求
    "$ISAAC_PY" - "$OUT" <<'PY' 2>/dev/null || true
import glob, os, sys
d = sys.argv[1]
fs = sorted(glob.glob(os.path.join(d, "*.h5")))
if fs:
    sizes = [os.path.getsize(f) / 1024 / 1024 for f in fs]
    print("单条体积: 中位 %.1f MB / 最大 %.1f MB" % (sorted(sizes)[len(sizes)//2], max(sizes)))
    print("300 条预估: %.1f GB" % (sum(sizes)/len(sizes)*300/1024))
PY
fi

# ---- 噪声探针（与 DOM 的 0.47~0.56 对比）
if (( n_h5 > 0 )); then
    printf '\n%s画面质量（对照 DOM 基线 颗粒 0.47~0.56 / 闪烁 0.28~1.62）%s\n' "$BLD" "$RST"
    F=$(find "$OUT" -maxdepth 1 -name '*.h5' | head -1)
    "$ISAAC_PY" "$CODE/../work/scripts/11_probe_noise.py" --h5 "$F" --label ours 2>&1 | \
        grep -E "ours/|判据" | head -8
fi

printf '\n'
if (( RC != 0 )); then
    die "链路验证失败（退出码 $RC），请看 $OUT/run.log"
elif (( n_h5 == 0 )); then
    warn "跑完了但没产出 h5 —— 可能是 ${'"'}--require_slip${'"'} 过滤太严，看日志"
else
    ok "链路打通：$n_h5 条，用时 ${ELAPSED}s"
    if (( n_h5 > 0 )); then
        printf '  单条均耗 %.1f s（含 %s s 的一次性启动开销）\n' \
            "$(python3 -c "print($ELAPSED/$n_h5)" 2>/dev/null || echo '?')" "$ELAPSED"
    fi
fi