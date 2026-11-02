#!/usr/bin/env bash
# ===========================================================================
# 00 · 拉上游 DynamicVLA 代码到固定 commit
#
# 本仓库的注入脚本按 a27a06d 的 interfaces 写的。上游若改了 simulate.py 的签名，
# gen_recovery.py 会挂。所以锁 commit，不跟 main。
#
# 用法：
#   bash scripts/00_setup.sh --upstream ~/dynamicvla-demo
#   bash scripts/00_setup.sh --upstream ~/dynamicvla-demo --commit a27a06d2
#   bash scripts/00_setup.sh ~/dynamicvla-demo          # 位置参数也支持
# ===========================================================================
set -euo pipefail

UPSTREAM_URL="${DYNAMICVLA_UPSTREAM_URL:-https://github.com/hzxie/DynamicVLA.git}"
UPSTREAM_COMMIT="${DYNAMICVLA_UPSTREAM_COMMIT:-a27a06d2ca74d0e987a5e552e01013073e93cfd8}"
DEST=""

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLD=$'\033[1m'; RST=$'\033[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s!%s %s\n' "$YLW" "$RST" "$*"; }
die()  { printf '%s✗%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --upstream) DEST="${2:-}"; shift 2 ;;
        --commit)   UPSTREAM_COMMIT="${2:-}"; shift 2 ;;
        --url)      UPSTREAM_URL="${2:-}"; shift 2 ;;
        -h|--help)  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) die "未知参数：$1" ;;
        *)  DEST="$1"; shift ;;
    esac
done

# --upstream 给的是 <root>（工作根目录）还是 <root>/dynamic-vla 都兼容
DEST="${DEST:-$HOME/dynamicvla-demo}"
case "$(basename "$DEST")" in
    dynamic-vla) : ;;
    *) DEST="$DEST/dynamic-vla" ;;
esac

say "${BLD}=== 00 · 拉上游代码 ===${RST}"
say "repo   : $UPSTREAM_URL"
say "commit : $UPSTREAM_COMMIT"
say "dest   : $DEST"
say ""

if [[ -d "$DEST/.git" ]]; then
    ok "已存在 git 仓库，检查 commit"
    cur="$(git -C "$DEST" rev-parse HEAD)"
    if [[ "$cur" == "$UPSTREAM_COMMIT" ]]; then
        ok "已经在目标 commit：$cur"
    else
        warn "当前 $cur，切到 $UPSTREAM_COMMIT"
        git -C "$DEST" fetch origin "$UPSTREAM_COMMIT" || die "fetch 失败"
        git -C "$DEST" checkout --detach "$UPSTREAM_COMMIT"
    fi
else
    mkdir -p "$(dirname "$DEST")"
    git clone "$UPSTREAM_URL" "$DEST"
    git -C "$DEST" fetch origin "$UPSTREAM_COMMIT"
    git -C "$DEST" checkout --detach "$UPSTREAM_COMMIT"
fi

# 校验：必须是干净 checkout（注入文件是 untracked，不算 dirty）
dirty="$(git -C "$DEST" status --porcelain --untracked-files=no)"
if [[ -n "$dirty" ]]; then
    warn "工作区有已跟踪文件的改动："
    printf '%s\n' "$dirty"
    warn "如果这是你自己改的（比如 yaw wrap 补丁），可以继续；否则先 git checkout ."
fi

ok "commit: $(git -C "$DEST" rev-parse --short HEAD)"
say ""
say "${BLD}下一步：${RST}"
say "  bash scripts/01_fetch_assets.sh --root $(dirname "$DEST") --zips <下载目录>"
say "  （资产约 2 GB，不随 git 分发，必须手动下载）"