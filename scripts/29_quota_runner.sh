#!/bin/bash
# quota_runner.sh <profile> <outdir> <target_h5> <seedfile>
#
# 配额自驱量产：按目标 h5 条数自动续轮，达标即停。
#   - 每轮 60 attempts（与 22_supervisor 的轮控 RSS 策略一致）
#   - 轮间零空转：等待同目录的 gen_recovery 退出后立刻开下一轮
#   - 种子自增：每轮 +60，写入 seedfile，重启后可接续
#
# 用法（必须在已部署 24_run_class.sh + profiles 的机器上）：
#   echo 9600001 > /path/out/_seed_a
#   setsid nohup bash scripts/29_quota_runner.sh \
#       profiles/a_slip_prod.env /path/out/prod3_a 158 /path/out/_seed_a \
#       > /tmp/runner_a.log 2>&1 < /dev/null &
#
# 环境变量：
#   SIM_DIR   指向含 gen_recovery.py 的 simulations 目录。
#             默认 <repo>/../code/simulations；24_run_class.sh 会回落到
#             相对路径 <repo>/code/simulations，上游仓库布局下不存在，
#             不显式设置会导致每轮秒退（退出码 1，日志几乎为空）。
#   ROUND_N   每轮 attempts 数，默认 60
set -u
PROFILE=$1; OUTDIR=$2; TARGET=$3; SEEDFILE=$4
ROUND_N=${ROUND_N:-60}
REPO="$(cd "$(dirname "$0")/.." && pwd)"          # 仓库根（scripts/ 的上一级）
export SIM_DIR=${SIM_DIR:-$REPO/../code/simulations}

[ -f "$SEEDFILE" ] || { echo "seedfile 缺失: $SEEDFILE"; exit 1; }

while :; do
  CUR=$(ls "$OUTDIR"/*.h5 2>/dev/null | wc -l)
  if [ "$CUR" -ge "$TARGET" ]; then echo "[$(date '+%F %T')] QUOTA MET: $CUR/$TARGET"; break; fi
  # 等待同目录的 sim 退出（轮间零空转）
  while pgrep -f "gen_recovery.py" > /dev/null && pgrep -af "gen_recovery.py" | grep -q "$OUTDIR"; do sleep 30; done
  CUR=$(ls "$OUTDIR"/*.h5 2>/dev/null | wc -l)
  [ "$CUR" -ge "$TARGET" ] && { echo "[$(date '+%F %T')] QUOTA MET: $CUR/$TARGET"; break; }
  SEED=$(cat "$SEEDFILE")
  echo "[$(date '+%F %T')] round start seed=$SEED cur=$CUR/$TARGET"
  bash "$REPO/scripts/24_run_class.sh" "$PROFILE" "$(basename "$OUTDIR")" "$SEED" "$ROUND_N" "$(dirname "$OUTDIR")"
  echo $((SEED+ROUND_N)) > "$SEEDFILE"
done
