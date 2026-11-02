#!/usr/bin/env bash
# 22_supervisor_4090.sh — RTX 4090 (24GB) 双 worker 滚动生产 supervisor
#
# 用法:
#   setsid nohup ./22_supervisor_4090.sh > /dev/null 2>&1 < /dev/null &
# 停止:
#   touch <WORK>/STOP          # 当前轮跑完后优雅退出
#   rm  <WORK>/STOP            # 清除停止标记
#
# 实测结论（2026-11-02, RTX 4090 / Isaac Sim 4.5.0 / IsaacLab 2.2.1）:
#   - 单 worker 稳态显存 9~10GB → 双 worker ~19GB，4090 上限即 2 个
#     （三 worker 预计 28GB+, 必 OOM）
#   - 双 worker 吞吐 ≈ 28s/attempt，GPU 利用率 ~33%，接近线性加速
#   - 30s episode 预算下 save 率 30~40%，saved 产出 ~75MB/ep (h5 含 3 路 360x480 rgb+seg)
#
# 磁盘守卫：剩余 < 8GB 时暂停发起新轮（等待转存/扩容），不杀正在跑的轮。
#
# ⚠️ START_ROUND：如果之前已手动跑过若干批次（例如手工启了 seed 2000001 的 50 条），
#    必须把 START_ROUND 设成对应轮次，否则 supervisor 会从 seed 2000001 重新跑一遍，
#    造成重复生产。规则：seed = BASE + ROUND*1000，手动批次占用了 ROUND=0，
#    则 START_ROUND=1（下一个 seed = 2001001）。
set -uo pipefail

WORK=${WORK:-/root/gpufree-data/work}
LOG=$WORK/NOTES/supervisor.log
MARGIN_GB=${MARGIN_GB:-8}
EP_PER_ROUND=${EP_PER_ROUND:-50}
START_ROUND=${START_ROUND:-0}
# 错峰秒数：两个 kit 同时冷启动会双双卡死在 RTX 初始化后（显存只申请 ~1GB、
# 永不就绪），三组对照实验（同时/错峰/隔离缓存）只有错峰活下来。
# worker b 立即启动，worker c 延迟 STAGGER 秒。只在总启动时错一次；
# 之后的滚动轮次里 c 自然落在 b 的稳态期之后，无需再错。
STAGGER=${STAGGER:-150}
# 生产时必须按类注入：PROFILE_B/PROFILE_C 指向 profiles/*.env，
# 走 24_run_class.sh（profile 唯一真源，含 --require_slip / EP 预算等）。
# 不设置则退回 run_worker.sh（裸 gen_recovery，吃默认 env —— 只适合基准测试，
# 默认 SLIP_MODE=open 的滑脱对训练不可见，切勿用它生产 A 类）。
PROFILE_B=${PROFILE_B:-}
PROFILE_C=${PROFILE_C:-}
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

rm -f "$WORK/STOP"
mkdir -p "$WORK/NOTES"

launch_worker() {
  # $1=worker名 $2=seed $3=条数；两条路径的参数顺序不同，分开传
  if [ -n "${4:-}" ]; then
    bash "$REPO_DIR/scripts/24_run_class.sh" "$4" "$1" "$2" "$3" \
      >> "$WORK/NOTES/$1.log" 2>&1
  else
    "$WORK/run_worker.sh" "$1" "$2" "$3" >> "$WORK/NOTES/$1.log" 2>&1
  fi
}

worker_loop() {
  local W=$1 BASE=$2 PROFILE=${3:-} STAGGER_S=${4:-0} ROUND=${START_ROUND:-0} SEED FREE_GB
  # 错峰等待：必须等先启 worker 的 kit 完成 RTX 初始化后再冷启动，
  # 否则两个 kit 双双卡死（只在进程出生时错一次）
  if [ "$STAGGER_S" -gt 0 ]; then
    echo "$(date '+%F %T') [$W] 错峰 ${STAGGER_S}s 后启动" >> "$LOG"
    sleep "$STAGGER_S"
  fi
  # 若已有同名 worker 在跑（如手动启动的），等它退出后再接管
  while pgrep -f "gen_recovery.*prod_$W" > /dev/null; do sleep 60; done
  while [ ! -f "$WORK/STOP" ]; do
    FREE_GB=$(df --output=avail -B 1G /root/gpufree-data | tail -1 | tr -d ' ')
    if [ "${FREE_GB:-0}" -lt "$MARGIN_GB" ]; then
      echo "$(date '+%F %T') [$W] 磁盘剩 ${FREE_GB}G < ${MARGIN_GB}G，暂停新轮" >> "$LOG"
      sleep 180; continue
    fi
    SEED=$((BASE + ROUND * 1000))
    echo "$(date '+%F %T') [$W] 第 $ROUND 轮 seed=$SEED n=$EP_PER_ROUND 启动" >> "$LOG"
    launch_worker "prod_$W" "$SEED" "$EP_PER_ROUND" "$PROFILE"
    ROUND=$((ROUND + 1))
    echo "$(date '+%F %T') [$W] 第 $ROUND 轮结束" >> "$LOG"
  done
  echo "$(date '+%F %T') [$W] 收到 STOP，退出" >> "$LOG"
}

# seed 段分配: b=2xxxxxx, c=3xxxxxx，每轮 +1000
worker_loop b 2000001 "$PROFILE_B" 0 &
# 错峰：必须等 b 的 kit 完成 RTX 初始化后 c 才能冷启动（见 STAGGER 注释）
worker_loop c 3000001 "$PROFILE_C" "$STAGGER" &
wait
