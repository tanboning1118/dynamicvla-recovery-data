#!/usr/bin/env bash
# 24_run_class.sh — 按 profile 跑某一类恢复数据（五类统一的入口）
#
# 用法:
#   ./24_run_class.sh <profile文件> <输出子目录名> <起始seed> <episode数> [输出根目录]
# 示例:
#   ./24_run_class.sh profiles/b_miss.env demo_b 6100001 8
#   ./24_run_class.sh profiles/a_slip.env prod_a 4000001 50 /root/gpufree-data/work/out
#
# 为什么要有这个脚本:
#   五类的参数差异不只是「开哪个注入」，还包括 episode 预算、--require_slip /
#   --keep_imperfect 的取舍、settle 上限。把这些散在命令行里必然漂移 ——
#   实测就踩过两次：A 用了 open 模式（训练不可见）、B 沿用 30 s 预算（真抓空样本全超时）。
#   所以参数一律以 profiles/*.env 为唯一真源，本脚本只负责把它翻译成命令行。
set -euo pipefail

PROFILE=${1:?profile 文件，如 profiles/b_miss.env}
NAME=${2:?输出子目录名}
SEED=${3:?起始 seed}
N=${4:?episode 数}
OUTROOT=${5:-${OUT_ROOT:-/root/gpufree-data/work/out}}

[ -f "$PROFILE" ] || { echo "找不到 profile: $PROFILE" >&2; exit 2; }
# shellcheck disable=SC1090
set -a; . "$PROFILE"; set +a

VENV=${VENV:-/root/gpufree-data/venv/dvla}
SP=$VENV/lib/python3.10/site-packages
PY=${PY:-$VENV/bin/python}

export LD_LIBRARY_PATH="$SP/torch/lib:$SP/nvidia/cublas/lib:$SP/nvidia/cusparse/lib:$SP/nvidia/cudnn/lib:$SP/nvidia/cuda_runtime/lib:$SP/nvidia/cufft/lib:$SP/nvidia/curand/lib:$SP/nvidia/cusolver/lib:$SP/nvidia/nccl/lib:$SP/nvidia/cuda_nvrtc/lib:$SP/cusparselt/lib:/usr/local/cuda/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

OUTDIR=$OUTROOT/$NAME
mkdir -p "$OUTDIR"
cd "${SIM_DIR:-$(dirname "$0")/../code/simulations}"

EP=${GEN_EPISODE_LENGTH:-30}
EXTRA=${GEN_EXTRA_FLAGS:-}
TASK=${GEN_TASK:-place}
ROBOT=${GEN_ROBOT:-franka}

echo "[$(date '+%F %T')] $PROFILE_NAME -> $OUTDIR  seed=$SEED n=$N ep=${EP}s flags='$EXTRA'"
# shellcheck disable=SC2086
exec stdbuf -oL -eL "$PY" gen_recovery.py --headless --enable_cameras \
  --task "$TASK" --robot "$ROBOT" \
  -n "$N" --seed "$SEED" --save --episode_length "$EP" $EXTRA \
  -o "$OUTDIR"
