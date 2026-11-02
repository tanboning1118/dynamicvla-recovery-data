#!/usr/bin/env bash
# ===========================================================================
# 03 · 上机自检（preflight）
#
# 任何一项红都不许往下走。这份检查是「环境问题拖 1 小时，云成本翻倍」的保险。
#
# 用法：bash scripts/03_preflight.sh [--root ~/dynamicvla-demo]
#                                  [--out <输出目录>] [--allow-network-fs]
#
# 退出码：0 = 无 FAIL；1 = 有 FAIL（不许往下走）
# ===========================================================================
set -uo pipefail

ROOT="$HOME/dynamicvla-demo"
OUT_DIR=""
ALLOW_NET_FS=0
for ((i=1; i<=$#; i++)); do
    case "${!i}" in
        --root)  j=$((i+1)); ROOT="${!j}" ;;
        --out)   j=$((i+1)); OUT_DIR="${!j}" ;;
        --allow-network-fs) ALLOW_NET_FS=1 ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    esac
done

CODE="$ROOT/dynamic-vla"
PY="${VLA_SIM_PY:-python3}"
OUT_DIR="${OUT_DIR:-${DVLA_OUT:-$HOME/dvla-gen}}"

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLD=$'\033[1m'; RST=$'\033[0m'
ok()   { printf '%s✓%s %s\n' "$GRN" "$RST" "$*"; }
bad()  { printf '%s✗%s %s\n' "$RED" "$RST" "$*"; FAIL=$((FAIL+1)); }
wrn()  { printf '%s!%s %s\n' "$YLW" "$RST" "$*"; WARN=$((WARN+1)); }
sec()  { printf '\n%s--- %s ---%s\n' "$BLD" "$*" "$RST"; }

FAIL=0; WARN=0

printf '%s========================================%s\n' "$BLD" "$RST"
printf '%s  03 · 上机自检%s\n' "$BLD" "$RST"
printf '%s========================================%s\n' "$BLD" "$RST"
printf 'root : %s\n' "$ROOT"
printf 'code : %s\n' "$CODE"
printf 'python: %s\n' "$PY"

# ---------------------------------------------------------------- 1. 硬件
sec "1. 硬件"

if command -v nvidia-smi >/dev/null 2>&1; then
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
    GPU_MEM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1 | tr -d ' ')
    GPU_USED=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1 | tr -d ' ')
    GPU_CAP=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ')
    ok "GPU: $GPU_NAME / ${GPU_MEM} MiB / cc ${GPU_CAP:-?}"
    ok "显存已用: ${GPU_USED} MiB"

    FREE_MEM=$((GPU_MEM - GPU_USED))
    if [[ $GPU_MEM -lt 8000 ]]; then
        bad "显存 ${GPU_MEM} MiB < 8000 —— 单环境峰值 5.7 GB，低于 8 GB 跑不动"
    elif [[ $FREE_MEM -lt 7000 ]]; then
        wrn "当前空闲 ${FREE_MEM} MiB，可能被其他进程占用（建议 ≥ 7000）"
    else
        ok "空闲显存 ${FREE_MEM} MiB 足够单环境"
    fi

    # 并发度推算
    if [[ $GPU_MEM -ge 22000 ]]; then
        ok "推算并发：显存允许 4 进程（$GPU_MEM MiB / 5.7 GB，留 1.2 GB 余量）"
    elif [[ $GPU_MEM -ge 15000 ]]; then
        ok "推算并发：显存允许 2 进程"
    else
        ok "推算并发：显存只允许 1 进程"
    fi

    case "$GPU_CAP" in
        8.9)  ok "compute capability 8.9 = Ada（4090 / 4080 / 4060Ti）" ;;
        12.0) ok "compute capability 12.0 = Blackwell（5060 / 5070 / 5080）" ;;
        *)    wrn "compute capability $GPU_CAP —— 确认 torch 轮子支持它" ;;
    esac
else
    bad "找不到 nvidia-smi"
fi

NPROC=$(nproc 2>/dev/null || echo 0)
MEM_GB=$(free -g 2>/dev/null | awk '/^Mem:/{print $2}')
ok "CPU: ${NPROC} 核 / 内存 ${MEM_GB:-?} GB"

if [[ "${MEM_GB:-0}" -lt 16 ]]; then
    bad "系统内存 ${MEM_GB} GB < 16 —— 单进程 RSS 峰值 8.4 GB，很危险"
elif [[ "${MEM_GB:-0}" -lt 32 ]]; then
    wrn "系统内存 ${MEM_GB} GB：只够 1–2 进程（每进程 8.4 GB）"
else
    ok "内存足够跑 $(( MEM_GB / 9 > 4 ? 4 : MEM_GB / 9 )) 进程"
fi

# ---------------------------------------------------------------- 2. Python / torch
sec "2. Python 与 torch"

PYVER=$("$PY" -V 2>&1 | awk '{print $2}')
case "$PYVER" in
    3.10.*) ok "Python $PYVER" ;;
    *)      wrn "Python $PYVER（期望 3.10.x，Isaac Lab 2.2.1 官方支持）" ;;
esac

"$PY" - <<'PYEOF' || FAIL=$((FAIL+1))
import sys
try:
    import torch
except Exception as e:
    print("\033[31m✗\033[0m 无法 import torch: %r" % e); sys.exit(1)
print("\033[32m✓\033[0m torch %s / cuda %s" % (torch.__version__, torch.version.cuda))
if not torch.cuda.is_available():
    print("\033[31m✗\033[0m torch.cuda.is_available() = False"); sys.exit(1)
name = torch.cuda.get_device_name(0)
cap = torch.cuda.get_device_capability(0)
print("\033[32m✓\033[0m cuda device: %s, capability %s" % (name, cap))
# 张量实测，比只看版本号靠谱
try:
    x = torch.randn(64, 64, device="cuda")
    y = (x @ x).sum().item()
    print("\033[32m✓\033[0m GPU 矩阵乘实测通过（%.3f）" % y)
except Exception as e:
    print("\033[31m✗\033[0m GPU 计算失败（可能是 arch 不匹配）: %r" % e); sys.exit(1)
PYEOF

# ---------------------------------------------------------------- 3. Isaac
sec "3. Isaac Sim / Isaac Lab"

"$PY" - <<'PYEOF'
import sys
def v(mod, attr="__version__"):
    try:
        m = __import__(mod)
        return getattr(m, attr, "?")
    except Exception as e:
        return None
rows = [("isaacsim", v("isaacsim")),
        ("isaaclab", v("isaaclab")),
        ("isaaclab_tasks", v("isaaclab_tasks")),
        ("warp", v("warp")),
        ("h5py", v("h5py")),
        ("yaml", v("yaml")),
        ("cv2", v("cv2"))]
bad = 0
for name, ver in rows:
    if ver is None:
        print("\033[31m✗\033[0m %s 缺失" % name); bad += 1
    else:
        print("\033[32m✓\033[0m %-16s %s" % (name, ver))
sys.exit(1 if bad else 0)
PYEOF
[[ $? -ne 0 ]] && FAIL=$((FAIL+1))

# ---------------------------------------------------------------- 4. 代码
sec "4. 上游代码"

if [[ -d "$CODE/.git" ]]; then
    SHORT=$(git -C "$CODE" rev-parse --short HEAD)
    FULL=$(git -C "$CODE" rev-parse HEAD)
    ok "git HEAD: $SHORT"
    if [[ "$FULL" == "a27a06d2ca74d0e987a5e552e01013073e93cfd8" ]]; then
        ok "commit 与预期一致（a27a06d）"
    else
        wrn "commit 不是 a27a06d —— 注入脚本可能不兼容，请复核 simulate.py 签名"
    fi
    MOD=$(git -C "$CODE" status --porcelain --untracked-files=no | wc -l)
    if [[ $MOD -eq 0 ]]; then
        ok "已跟踪文件无改动"
    else
        wrn "已跟踪文件有 $MOD 处改动（若是 yaw wrap 等必要补丁则正常）"
    fi
else
    bad "$CODE 不是 git 仓库（先跑 scripts/00_setup.sh）"
fi

for f in "simulations/simulate.py" "simulations/gen_recovery.py" \
         "simulations/state_machines/place_recovery_sm.py" \
         "simulations/configs/sim_cfg.yaml" \
         "scripts/create_lerobot_dataset.py"; do
    if [[ -f "$CODE/$f" ]]; then ok "存在 $f"; else bad "缺少 $f"; fi
done

if grep -q 'REC_SLIP_MODE' "$CODE/simulations/state_machines/place_recovery_sm.py" 2>/dev/null; then
    ok "place_recovery_sm.py 含 REC_SLIP_MODE"
else
    bad "place_recovery_sm.py 不含 REC_SLIP_MODE —— 注入没打上"
fi
if grep -q 'require_slip' "$CODE/simulations/gen_recovery.py" 2>/dev/null; then
    ok "gen_recovery.py 含 require_slip"
else
    bad "gen_recovery.py 缺少 require_slip —— 注入没打上"
fi

for f in "$HOME/annotate_recovery.py" "$HOME/qa_gates.py"; do
    [[ -f "$f" ]] && ok "存在 $(basename "$f")" || bad "缺少 $f"
done

# ---------------------------------------------------------------- 5. 资产
sec "5. 资产"

if [[ -d "$CODE/objects" ]]; then
    N_CAT=$(ls -1 "$CODE/objects" | grep -v metadata.json | wc -l)
    N_USD=$(find "$CODE/objects" -name '*.usd' | wc -l)
    N_META=$(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1]))))" \
             "$CODE/objects/metadata.json" 2>/dev/null || echo 0)
    [[ $N_CAT -ge 20 ]] && ok "objects: $N_CAT 类 / $N_USD 个 USD" || bad "objects: 只有 $N_CAT 类"
    [[ $N_META -ge 150 ]] && ok "metadata.json: $N_META 条" || bad "metadata.json: 只有 $N_META 条"
else
    bad "缺少 objects/ 目录（资产没解压）"
fi

if [[ -d "$CODE/scenes" ]]; then
    N_SC=$(find "$CODE/scenes" -maxdepth 1 -name '*.usd' | wc -l)
    [[ $N_SC -ge 70 ]] && ok "scenes: $N_SC 个 USD" || bad "scenes: 只有 $N_SC 个"
else
    bad "缺少 scenes/ 目录"
fi

# ---------------------------------------------------------------- 6. 磁盘
sec "6. 磁盘"

# OUT_DIR 已在文件头解析（--out / DVLA_OUT / 默认 ~/dvla-gen），这里不要再覆盖
mkdir -p "$OUT_DIR" 2>/dev/null || true
AVAIL_GB=$(df -Pk "$OUT_DIR" 2>/dev/null | awk 'NR==2{print int($4/1048576)}')
ok "输出目录: $OUT_DIR"
if [[ "${AVAIL_GB:-0}" -lt 30 ]]; then
    bad "剩余 ${AVAIL_GB} GB < 30（330 条 h5 峰值约 80 GB）"
elif [[ "${AVAIL_GB:-0}" -lt 100 ]]; then
    wrn "剩余 ${AVAIL_GB} GB，只够跑一批就要清理"
else
    ok "剩余 ${AVAIL_GB} GB"
fi

# 检查是不是网络盘（ossfs/nfs 会拖死吞吐）
FSTYPE=$(df -PT "$OUT_DIR" 2>/dev/null | awk 'NR==2{print $2}')
case "$FSTYPE" in
    nfs*|fuse*|ossfs*|cifs*|smb*)
        if [[ $ALLOW_NET_FS -eq 1 ]]; then
            wrn "输出目录是网络盘（$FSTYPE）—— 已按 --allow-network-fs 放行。"
            wrn "  仅当只写最终 LeRobot 结果时才可以接受；写 h5 会慢到不可用。"
        else
            bad "输出目录是网络盘（$FSTYPE）—— 实测会拖到 30 s/episode。"
            bad "  换本地盘，或确认只写成品后加 --allow-network-fs 重跑。"
        fi ;;
    "") bad "无法判断输出目录的文件系统：$OUT_DIR（目录可能不存在）" ;;
    *)  ok "文件系统: ${FSTYPE}（本地）  $OUT_DIR" ;;
esac

# ---------------------------------------------------------------- 7. 一致性
sec "7. 一致性"

if [[ -f "$CODE/utils/rotation.py" ]]; then
    ok "yaw wrap 补丁在（utils/rotation.py）"
else
    wrn "未找到 utils/rotation.py —— 若你的加载器依赖 yaw wrap，请一并移植"
fi

EVENT_LABELS="$CODE/utils/event_labels.py"
if grep -q 'RISE_TOL' "$CODE/utils/event_labels.py" 2>/dev/null; then
    ok "event_labels.py 已含 RISE_TOL（段级抬升先决补丁已打）"
else
    wrn "event_labels.py 无 RISE_TOL —— 若这是训练机上那份，必须打 B 类补丁"
fi

# ---------------------------------------------------------------- 汇总
printf '\n%s========================================%s\n' "$BLD" "$RST"
if [[ $FAIL -eq 0 ]]; then
    printf '%s  ✓ 自检通过（%d 个警告）%s\n' "$GRN" "$WARN" "$RST"
    printf '%s========================================%s\n' "$BLD" "$RST"
    printf '\n下一步：bash scripts/04_benchmark_single_card.sh --rounds 5\n'
    printf '%s注意：这一步会决定你能开几个进程，不可跳过。%s\n' "$YLW" "$RST"
    exit 0
else
    printf '%s  ✗ 自检失败：%d 项红，%d 项警告%s\n' "$RED" "$FAIL" "$WARN" "$RST"
    printf '%s========================================%s\n' "$BLD" "$RST"
    exit 1
fi