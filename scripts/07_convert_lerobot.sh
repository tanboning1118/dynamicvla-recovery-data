#!/usr/bin/env bash
# ===========================================================================
# 07 · h5 → LeRobot v2.1
#
# 做三件事：
#   1. annotate_recovery.py  --raw     从 h5 挖 sm_state，写侧车缓存
#   2. create_lerobot_dataset.py       转换（默认删 h5）
#   3. annotate_recovery.py  --lerobot 把缓存 join 成 recovery_index.json
#
# ⚠️ 转换器会丢掉 sm_state —— 那是「滑脱/重试发生在哪一帧」的唯一记录。
#    必须先挖出来，再转换。
#
# 用法：
#   bash scripts/07_convert_lerobot.sh --base ~/dvla-gen/a_slip_xxx --repo local/a_slip
#   bash scripts/07_convert_lerobot.sh --base ~/dvla-gen/a_slip_xxx --keep-h5
#   bash scripts/07_convert_lerobot.sh --base-str ~/dvla-gen/campaign_xxx/a_slip --repo local/a_slip
# ===========================================================================
set -uo pipefail

BASE=""
REPO="local/recovery"
KEEP_H5=0
ROOT="${ROOT:-$HOME/dynamicvla-demo}"

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLD=$'\033[1m'; RST=$'\033[0m'
ok()   { printf '%s✓%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s!%s %s\n' "$YLW" "$RST" "$*"; }
die()  { printf '%s✗%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }
log()  { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --base)     BASE="${2:-}"; shift 2 ;;
        --base-str) BASE="${2:-}"; shift 2 ;;
        --repo)     REPO="${2:-}"; shift 2 ;;
        --root)     ROOT="${2:-}"; shift 2 ;;
        --keep-h5)  KEEP_H5=1; shift ;;
        -h|--help)  sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "未知参数：$1" ;;
    esac
done

[[ -n "$BASE" ]] || die "必须指定 --base"
BASE="${BASE%/}"
[[ -d "$BASE" ]] || die "目录不存在：$BASE"

CODE="$ROOT/dynamic-vla"
CONV="$CODE/scripts/create_lerobot_dataset.py"
ANNO="$HOME/annotate_recovery.py"
PY_MODEL="${VLA_MODEL_PY:-$(dirname "${VLA_SIM_PY:-$HOME/miniconda3/envs/dvla-isaac45-blackwell/bin/python}")/python}"

[[ -f "$CONV" ]] || die "找不到 $CONV"
[[ -f "$ANNO" ]] || die "找不到 $ANNO（先跑 02_patch_upstream.sh）"

# ---- 汇总 raw 目录 ----------------------------------------------------------
# 支持两种布局：
#   <base>/raw/*.h5                  （单批）
#   <base>/c*/raw/*.h5               （campaign 多 chunk）
H5_COUNT=$(find "$BASE" -name '*.h5' 2>/dev/null | wc -l)
[[ $H5_COUNT -gt 0 ]] || die "$BASE 下没有 h5（先跑 05_generate.sh）"

printf '%s========================================%s\n' "$BLD" "$RST"
printf '%s  07 · 转 LeRobot%s\n' "$BLD" "$RST"
printf '%s========================================%s\n' "$BLD" "$RST"
printf 'base  : %s\n' "$BASE"
printf 'repo  : %s\n' "$REPO"
printf 'h5    : %d 条\n' "$H5_COUNT"
printf 'keep  : %s\n' "$([[ $KEEP_H5 -eq 1 ]] && echo yes || echo "no（转完删 h5）")"

LEROBOT_HOME="$BASE/lerobot_home"
OUT="$LEROBOT_HOME/$REPO"
CACHE="$BASE/recovery_annotations.json"
MERGE="$BASE/_merge_raw"

# ---- 如果 h5 分散在 c*/raw，先硬链到一个目录（转换器只认单目录）------------
n_dirs=$(find "$BASE" -mindepth 2 -maxdepth 3 -type d -name raw 2>/dev/null | wc -l)
if [[ $n_dirs -gt 1 ]]; then
    log "h5 分散在 $n_dirs 个 raw 目录，硬链合并到 $MERGE"
    rm -rf "$MERGE"; mkdir -p "$MERGE"
    find "$BASE" -mindepth 2 -maxdepth 3 -type d -name raw -print0 2>/dev/null \
    | while IFS= read -r -d '' d; do
        for f in "$d"/*.h5; do
            [[ -e "$f" ]] || continue
            b="$(basename "$f")"
            if [[ -e "$MERGE/$b" ]]; then
                warn "重名跳过：$b（seed 段重叠？）"
                continue
            fi
            # 优先硬链，跨设备则拷贝
            ln "$f" "$MERGE/$b" 2>/dev/null || cp -n "$f" "$MERGE/$b"
        done
    done
    RAW_DIR="$MERGE"
    MERGED=1
    ok "合并 $(find "$MERGE" -name '*.h5' | wc -l) 条"
else
    RAW_DIR="$(find "$BASE" -mindepth 1 -maxdepth 2 -type d -name raw | head -1)"
    [[ -n "$RAW_DIR" ]] || RAW_DIR="$BASE/raw"
    MERGED=0
fi
[[ -d "$RAW_DIR" ]] || die "找不到 raw 目录"
printf 'raw   : %s\n' "$RAW_DIR"

# ---- 1. 挖 sm_state --------------------------------------------------------
printf '\n%s--- 1. 挖 sm_state 侧车缓存 ---%s\n' "$BLD" "$RST"
"$PY_MODEL" -u "$ANNO" --raw "$RAW_DIR" --cache "$CACHE" 2>&1 | tail -20
[[ -f "$CACHE" ]] || die "侧车缓存没生成：$CACHE"
ok "缓存 -> $CACHE（$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(len(d) if isinstance(d,list) else len(d.get('episodes',d)))" "$CACHE" 2>/dev/null || echo '?') 条）"

# ---- 2. 转换 ---------------------------------------------------------------
printf '\n%s--- 2. 转 LeRobot v2.1 ---%s\n' "$BLD" "$RST"
if [[ -d "$OUT" ]]; then
    warn "删除旧的 LeRobot 构建（可从 raw 重生成）：$OUT"
    rm -rf "$OUT"
fi
mkdir -p "$LEROBOT_HOME"

export LD_LIBRARY_PATH="$HOME/miniconda3/lib:${LD_LIBRARY_PATH:-}"
export HF_LEROBOT_HOME="$LEROBOT_HOME"

EXTRA=""
[[ $KEEP_H5 -eq 1 ]] || EXTRA="--remove-source"

T0=$(date +%s)
"$PY_MODEL" -u "$CONV" \
    --repo_id "$REPO" \
    --dataset_dir "$RAW_DIR" \
    --rotation euler \
    $EXTRA 2>&1 | grep -vE '^Svt\[' | tail -30
RC=${PIPESTATUS[0]}
T1=$(date +%s)

[[ -f "$OUT/meta/info.json" ]] || die "转换失败，没有 info.json。看上面的输出"
ok "转换完成，耗时 $((T1 - T0)) s"

# ---- 3. join 侧车 ----------------------------------------------------------
printf '\n%s--- 3. 关联侧车索引 ---%s\n' "$BLD" "$RST"
"$PY_MODEL" -u "$ANNO" --raw "$RAW_DIR" --cache "$CACHE" --lerobot "$OUT" 2>&1 | tail -10
[[ -f "$OUT/recovery_index.json" ]] && ok "recovery_index.json 已生成" \
    || warn "recovery_index.json 未生成（D 类后置过滤会失败）"

# ---- 摘要 ------------------------------------------------------------------
printf '\n%s--- 摘要 ---%s\n' "$BLD" "$RST"
"$PY_MODEL" - "$OUT" "$BASE" <<'PY'
import json, os, pathlib, sys
root = pathlib.Path(sys.argv[1]); base = pathlib.Path(sys.argv[2])
info = json.load(open(root / "meta/info.json"))
print("  repo          : %s" % root)
print("  episodes      : %d" % info["total_episodes"])
print("  frames        : %d  (%.1f min)" % (info["total_frames"],
                                              info["total_frames"] / 25 / 60))
print("  avg length    : %.0f frames" % (info["total_frames"] / max(info["total_episodes"], 1)))
print("  features      : %s" % ", ".join(info["features"].keys()))
sz = sum(f.stat().st_size for f in root.rglob("*") if f.is_file()) / 1024**3
print("  disk          : %.2f GB" % sz)
idx = root / "recovery_index.json"
if idx.is_file():
    d = json.load(open(idx))
    s = d.get("summary", {})
    print("  with_slip     : %s" % s.get("with_slip", "?"))
    print("  with_retry    : %s" % s.get("with_retry", "?"))
    print("  mapped        : %s" % s.get("episodes_mapped", "?"))
PY

# ---- 清理合并目录 ----------------------------------------------------------
if [[ "${MERGED:-0}" -eq 1 ]]; then
    rm -rf "$MERGE"
    log "已清理临时合并目录"
fi

printf '\n下一步：python scripts/09_verify_loader.py --dataset %s\n' "$OUT"