#!/usr/bin/env bash
# 23_pull_4090_to_workstation.sh — 在工位机端运行：从 4090 拉取产出的 episodes 并删除源端
#
# 前置:
#   工位机 → 4090 免密 ssh 已打通（把工位机公钥加入 4090 的 authorized_keys）
#   4090 地址/端口按实例实际值修改下方 SSH_PORT / HOST
# 用法:
#   setsid nohup ./23_pull_4090_to_workstation.sh >> ~/puller.log 2>&1 < /dev/null &
#
# 安全设计:
#   - 只拉取 mtime > 3 分钟前 的文件（h5 写入是流式的，防止拉走写了一半的 episode）
#   - rsync --remove-source-files 仅在单文件完整传输成功后才删源端
#   - 失败自动重试（外层 while 循环），断点续传 (--partial)
set -uo pipefail

KEY=~/.ssh/id_ed25519_industry
SSH_PORT=30833
HOST=183.147.142.40
SRC=root@$HOST
SSHOPT="-i $KEY -p $SSH_PORT -o StrictHostKeyChecking=no -o ConnectTimeout=30"
DEST=${DEST:-$HOME/dynamicvla-recovery-raw}
REMOTE_OUT=/root/gpufree-data/work/out
WORKERS="prod_b prod_c"

while true; do
  for W in $WORKERS; do
    mkdir -p "$DEST/$W/events"
    # 主体 episode 文件（h5/json/mp4）
    for F in $(ssh $SSHOPT $SRC "find $REMOTE_OUT/$W -maxdepth 1 -type f -mmin +3 2>/dev/null"); do
      rsync -az --partial --remove-source-files -e "ssh $SSHOPT" "$SRC:$F" "$DEST/$W/" \
        && echo "$(date '+%F %T') pulled $F"
    done
    # events jsonl（很小，但同样只拉写完的）
    for F in $(ssh $SSHOPT $SRC "find $REMOTE_OUT/$W/events -type f -mmin +3 2>/dev/null"); do
      rsync -az --partial --remove-source-files -e "ssh $SSHOPT" "$SRC:$F" "$DEST/$W/events/" \
        && echo "$(date '+%F %T') pulled $F"
    done
  done
  sleep 300
done
