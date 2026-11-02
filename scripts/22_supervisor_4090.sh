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
set -uo pipefail

WORK=${WORK:-/root/gpufree-data/work}
LOG=$WORK/NOTES/supervisor.log
MARGIN_GB=${MARGIN_GB:-8}
EP_PER_ROUND=${EP_PER_ROUND:-50}

rm -f "$WORK/STOP"
mkdir -p "$WORK/NOTES"

worker_loop() {
  local W=$1 BASE=$2 ROUND=0 SEED FREE_GB
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
    "$WORK/run_worker.sh" "prod_$W" "$SEED" "$EP_PER_ROUND" >> "$WORK/NOTES/prod_$W.log" 2>&1
    ROUND=$((ROUND + 1))
    echo "$(date '+%F %T') [$W] 第 $ROUND 轮结束" >> "$LOG"
  done
  echo "$(date '+%F %T') [$W] 收到 STOP，退出" >> "$LOG"
}

# seed 段分配: b=2xxxxxx, c=3xxxxxx，每轮 +1000
worker_loop b 2000001 &
worker_loop c 3000001 &
wait
