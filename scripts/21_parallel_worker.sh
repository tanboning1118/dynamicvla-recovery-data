#!/usr/bin/env bash
# 21_parallel_worker.sh — 单个并行生产 worker（gen_recovery.py 的标准封装）
#
# 用法:
#   ./21_parallel_worker.sh <worker名> <起始seed> <episode数> [输出根目录]
# 示例:
#   ./21_parallel_worker.sh prod_b 2000001 50 /root/gpufree-data/work/out
#
# 说明:
#   - 每个 worker 独立 Isaac Sim 实例 (num_envs=1)，独立 seed 段，独立输出目录
#   - episode 文件名含 seed（place_franka_<obj>_<seed8>_<hash>.h5），worker 间天然无冲突
#   - LD_LIBRARY_PATH 必须在 App 启动前导出：Isaac Sim 启动后会重排库搜索路径，
#     否则 torch 报 libtorch_cuda_linalg.so cannot open
set -euo pipefail

W=${1:?worker 名，如 prod_b}
SEED=${2:?起始 seed}
N=${3:?episode 数}
OUTROOT=${4:-/root/gpufree-data/work/out}

VENV=/root/gpufree-data/venv/dvla
SP=$VENV/lib/python3.10/site-packages
PY=$VENV/bin/python

export LD_LIBRARY_PATH="$SP/torch/lib:$SP/nvidia/cublas/lib:$SP/nvidia/cusparse/lib:$SP/nvidia/cudnn/lib:$SP/nvidia/cuda_runtime/lib:$SP/nvidia/cufft/lib:$SP/nvidia/curand/lib:$SP/nvidia/cusolver/lib:$SP/nvidia/nccl/lib:$SP/nvidia/cuda_nvrtc/lib:$SP/cusparselt/lib:/usr/local/cuda/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

OUTDIR=$OUTROOT/$W
mkdir -p "$OUTDIR"
cd "$(dirname "$0")/../code/simulations" 2>/dev/null || cd "${SIM_DIR:?请设置 SIM_DIR 指向 simulations 目录}"

exec stdbuf -oL -eL "$PY" gen_recovery.py --headless --enable_cameras --disable_fabric \
  -n "$N" --seed "$SEED" --save \
  -o "$OUTDIR"
