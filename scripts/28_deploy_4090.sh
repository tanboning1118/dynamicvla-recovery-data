#!/usr/bin/env bash
# 28_deploy_4090.sh — 把仓库稳定版一键推到 4090 生产机（在工位机/本地跑）
#
# 用法:
#   ./28_deploy_4090.sh --host root@183.147.142.40 --port 30833
#   ./28_deploy_4090.sh --host root@183.147.142.40 --port 30833 --dry-run
#   ./28_deploy_4090.sh --host root@HOST --port 22 --work /root/gpufree-data/work --force
#
# 认证：脚本不碰密码。已配 ssh key 直接用；密码登录走 SSH_ASKPASS（例）:
#   cat > /tmp/ap.sh <<'EOF'
#   #!/bin/sh
#   echo 'YOUR_PASSWORD'
#   EOF
#   chmod +x /tmp/ap.sh
#   SSH_ASKPASS=/tmp/ap.sh SSH_ASKPASS_REQUIRE=force DISPLAY=dummy \
#     setsid ./28_deploy_4090.sh --host root@HOST --port 30833
#
# 它做什么（按序）:
#   0. 前置校验: 本地 HEAD 与 origin/main 一致（保证推的是「GitHub 稳定版」，
#      而不是某个本地半成品；--force 可跳过）
#   1. ssh 连通性检查
#   2. 远端备份 code/simulations、code/{qa_gates,annotate_recovery}.py、
#      scripts/、profiles/ → $WORK/_backup/deploy_<时间戳>.tgz（可回滚）
#   3. git-archive-over-ssh 推送 payload/simulations + qa_gates/annotate +
#      scripts + profiles。用 git archive 而不是 tar 工作目录，两个理由:
#        - 只打包被跟踪文件，__pycache__ / *.bak / 过程文件天然进不来
#        - .gitattributes (eol=lf) 保证 .sh 是 LF —— Windows 工作目录里
#          的 CRLF 脚本推到 Linux 会 bad interpreter（本仓库实测踩过）
#      远端先解到 .deploy_new 再原子替换，旧目录挪进 .deploy_old 保留不删
#   4. 远端自检: py_compile 状态机/生成器/闸门，bash -n 两个入口
#   5. 提示重启 supervisor —— 部署不热生效，在跑的轮次用的还是旧代码
set -euo pipefail

HOST=${HOST:-}
PORT=${PORT:-22}
WORK=${WORK:-/root/gpufree-data/work}
DRY_RUN=0
FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST=$2; shift 2 ;;
    --port) PORT=$2; shift 2 ;;
    --work) WORK=$2; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --force) FORCE=1; shift ;;
    *) echo "未知参数 $1" >&2; exit 2 ;;
  esac
done
[ -n "$HOST" ] || { echo "用法: $0 --host user@host [--port N] [--work DIR] [--dry-run] [--force]" >&2; exit 2; }

HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
[ -f payload/simulations/gen_recovery.py ] || { echo "请在仓库根的 scripts/ 下运行" >&2; exit 2; }

echo "[0/5] 本地 HEAD vs origin/main"
git fetch -q origin 2>/dev/null || echo "  (fetch 失败，按本地 HEAD 继续)"
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main 2>/dev/null || echo "$LOCAL")
if [ "$LOCAL" != "$REMOTE" ]; then
  if [ "$FORCE" = "1" ]; then
    echo "  ⚠ --force：本地 $LOCAL ≠ origin/main $REMOTE，照推（推的是本地版）"
  else
    echo "  ✗ 本地 $LOCAL ≠ origin/main $REMOTE —— 先 commit+push，或加 --force" >&2
    exit 1
  fi
else
  echo "  一致 ($LOCAL)"
fi

OPTS=""
[ "$FORCE" = "1" ] && OPTS="--force"
if [ "$DRY_RUN" = "1" ]; then
  echo "[dry-run] 将执行:"
  echo "  ssh -p $PORT $HOST '备份 $WORK/{code,scripts,profiles} → _backup/deploy_*.tgz'"
  echo "  git archive HEAD payload scripts profiles | ssh → 远端原子替换 + py_compile/bash -n 自检"
  echo "  提交: $(git log --oneline -1)"
  exit 0
fi

SSH="ssh -p $PORT -o ConnectTimeout=10"
if [ -z "${SSH_ASKPASS:-}" ]; then
  # 无 askpass 时禁交互提示：key 没配好立刻报错而不是挂住
  SSH="$SSH -o BatchMode=yes"
fi
R() { $SSH "$HOST" "$@"; }

echo "[1/5] 连通性 $HOST:$PORT"
R "echo ok" >/dev/null || { echo "  连不上（key/密码/端口？）" >&2; exit 1; }

echo "[2/5] 远端备份"
TS=$(date +%Y%m%d_%H%M%S)
BK="_backup/deploy_$TS.tgz"
R "mkdir -p '$WORK/_backup' && cd '$WORK' && \
   tar czf '$BK' code/simulations code/qa_gates.py code/annotate_recovery.py \
       scripts profiles 2>/dev/null; ls -lh '$BK'"
# 备份 tar 对个别缺失文件（如 annotate 尚未部署）非致命，2>/dev/null 吞掉

echo "[3/5] git archive → 远端原子替换"
git archive HEAD payload scripts profiles \
| $SSH "$HOST" "set -e; cd '$WORK' && \
    rm -rf .deploy_new .deploy_old && mkdir -p .deploy_new && \
    tar x -C .deploy_new && \
    mkdir -p code && \
    { [ -d code/simulations ] && mv code/simulations .deploy_old/ || mkdir -p .deploy_old; } && \
    { [ -d scripts ] && mv scripts .deploy_old/ || true; } && \
    { [ -d profiles ] && mv profiles .deploy_old/ || true; } && \
    cp -a .deploy_new/payload/simulations code/simulations && \
    cp -f .deploy_new/payload/qa_gates.py .deploy_new/payload/annotate_recovery.py code/ && \
    cp -a .deploy_new/scripts scripts && \
    cp -a .deploy_new/profiles profiles && \
    rm -rf .deploy_new && \
    echo DEPLOY_OK" \
|| { echo "  推送失败！远端旧版本在 $WORK/.deploy_old，备份在 $BK" >&2; exit 1; }
# .deploy_old 刻意不删：磁盘只占几百 KB，留作第一重回滚（tgz 是第二重）

echo "[4/5] 远端自检"
R "cd '$WORK/code/simulations' && \
   python3 -m py_compile simulations/state_machines/place_recovery_sm.py \
                          gen_recovery.py ../qa_gates.py && echo PY_OK && \
   bash -n ../scripts/24_run_class.sh ../scripts/22_supervisor_4090.sh && echo SH_OK" \
|| { echo "  自检失败！回滚: ssh $HOST 'cd $WORK && rm -rf code/simulations scripts profiles && \
     cp -a .deploy_old/simulations code/ && cp -a .deploy_old/scripts . && cp -a .deploy_old/profiles .'" >&2; exit 1; }

echo "[5/5] 完成。两个注意:"
echo "  - 正在跑的 supervisor/worker 用的仍是旧代码 —— touch $WORK/STOP"
echo "    等当前轮优雅退出，重启 supervisor 后新代码才生效。"
echo "  - 回滚（两种任选）: mv $WORK/.deploy_old/* 回原位；或"
echo "    tar xzf $WORK/$BK -C $WORK"
