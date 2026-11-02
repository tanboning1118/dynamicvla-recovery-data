#!/usr/bin/env bash
# ===========================================================================
# 02 · 把注入脚本挂进上游 + 打标签补丁 + 校验
#
# 拷贝两个文件：
#   payload/simulations/gen_recovery.py                         → <root>/dynamic-vla/simulations/
#   payload/simulations/state_machines/place_recovery_sm.py     → <root>/dynamic-vla/simulations/state_machines/
# 以及两个辅助脚本：
#   payload/annotate_recovery.py    → ~/ （转换时挖 sm_state）
#   payload/qa_gates.py             → ~/ （五道闸门）
#
# 然后打**阻断级标签补丁**（docs/06 T6）：
#   utils/event_labels.py 加段级抬升先决，修「空抓被标成已握持」
#   → 这是唯一会改动上游已跟踪文件的步骤，因此会同时：
#     a) 自动备份 .bak_<时间戳>
#     b) 把该文件加进 preflight 的 git 白名单
#     c) 提示你重跑 pytest 与 rebuild_ready.py
#
# 用 --no-patch 可只拷文件不打补丁（只想看生成链路时用）。
# ===========================================================================
set -euo pipefail

ROOT=""
FORCE=0
DO_PATCH=1
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLD=$'\033[1m'; RST=$'\033[0m'
ok()   { printf '%s✓%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s!%s %s\n' "$YLW" "$RST" "$*"; }
die()  { printf '%s✗%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --root)     ROOT="${2:-}"; shift 2 ;;
        --force)    FORCE=1; shift ;;
        --no-patch) DO_PATCH=0; shift ;;
        -h|--help)  sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "未知参数：$1" ;;
    esac
done

ROOT="${ROOT:-$HOME/dynamicvla-demo}"
CODE="$ROOT/dynamic-vla"
[[ -d "$CODE/simulations" ]] || die "找不到 $CODE/simulations（先跑 00_setup.sh）"

printf '%s=== 02 · 注入生成器 ===%s\n' "$BLD" "$RST"
printf 'root : %s\n' "$ROOT"
printf 'code : %s\n\n' "$CODE"

copy_one() { # src dst label
    local src="$1" dst="$2" label="$3"
    [[ -f "$src" ]] || die "payload 缺失：$src"
    if [[ -f "$dst" && $FORCE -eq 0 ]]; then
        if cmp -s "$src" "$dst"; then
            ok "$label 已在位且一致"
            return 0
        fi
        warn "$label 已存在且内容不同，备份为 .bak_$(date +%Y%m%d_%H%M%S)"
        mv "$dst" "$dst.bak_$(date +%Y%m%d_%H%M%S)"
    fi
    install -Dm644 "$src" "$dst"
    ok "$label → $dst"
}

copy_one "$HERE/payload/simulations/gen_recovery.py" \
         "$CODE/simulations/gen_recovery.py" \
         "gen_recovery.py"

copy_one "$HERE/payload/simulations/state_machines/place_recovery_sm.py" \
         "$CODE/simulations/state_machines/place_recovery_sm.py" \
         "place_recovery_sm.py"

copy_one "$HERE/payload/annotate_recovery.py" "$HOME/annotate_recovery.py" "annotate_recovery.py"
copy_one "$HERE/payload/qa_gates.py"          "$HOME/qa_gates.py"          "qa_gates.py"

# ---- 语法检查 ---------------------------------------------------------------
printf '\n%s=== 语法检查 ===%s\n' "$BLD" "$RST"
PY="${VLA_SIM_PY:-python3}"
for f in "$CODE/simulations/gen_recovery.py" \
         "$CODE/simulations/state_machines/place_recovery_sm.py" \
         "$HOME/annotate_recovery.py" "$HOME/qa_gates.py"; do
    if "$PY" -m py_compile "$f" 2>/dev/null; then
        ok "compile: $(basename "$f")"
    else
        die "编译失败：$f"
    fi
done

# ---- 关键常量核验（防「拷错版本」）-----------------------------------------
printf '\n%s=== 常量核验 ===%s\n' "$BLD" "$RST"
grep -q 'SLIP_MODE = os.environ.get("REC_SLIP_MODE", "open")' \
     "$CODE/simulations/state_machines/place_recovery_sm.py" \
    && ok "traced: REC_SLIP_MODE 默认 open（脚本会显式覆盖，profile 里已写 friction）" \
    || warn "REC_SLIP_MODE 默认值不是 open —— 上游可能改过这一行，请核对"

grep -q 'assert num_envs == 1' "$CODE/simulations/state_machines/place_recovery_sm.py" \
    && ok "traced: 单环境约束（并发只能靠多进程）" \
    || warn "未找到 num_envs == 1 断言 —— 上游可能支持多环境了，请复核"

python3 - "$CODE/simulations/state_machines/place_recovery_sm.py" <<'PY'
import re, sys
src = open(sys.argv[1], encoding="utf-8").read()
need = ["REC_SLIP","REC_SLIP_MODE","REC_SLIP_FRICTION","REC_SLIP_MASS_SCALE",
        "REC_SETTLE_MAX_TIME","REC_MAX_ATTEMPTS","REC_GRASP_CHECK","REC_MISS_OFFSET",
        "REC_DISTURB_AT","REC_DISTURB_VEL","REC_GRASP_JITTER_XY","REC_MAX_MISSES"]
miss = [k for k in need if 'REC_%s' % k[4:] not in src and k not in src]
if miss:
    print("  \033[33m!\033[0m 缺少环境变量：%s" % ", ".join(miss))
else:
    print("  \033[32m✓\033[0m 全部 12 个注入开关都在")
PY

# ---- 阻断级标签补丁：grasp_label 段级抬升先决 -------------------------------
# 不改这一处，B 类（抓空重试）产出的数据里会有 21% 的帧被错标成 HELD，
# 模型学到「手贴过去就算抓住了」—— 正是我们要教它区分的东西被抹掉了。
if [[ $DO_PATCH -eq 1 ]]; then
    printf '\n%s=== 标签补丁（阻断级）===%s\n' "$BLD" "$RST"
    EL="$CODE/utils/event_labels.py"
    if [[ ! -f "$EL" ]]; then
        warn "找不到 $EL —— 跳过补丁"
    else
        PATCH="$HERE/patches/apply_grasp_rise_patch.py"
        if [[ ! -f "$PATCH" ]]; then
            warn "找不到 $PATCH —— 跳过补丁"
        elif grep -q 'RISE_TOL' "$EL"; then
            ok "补丁已在位（RISE_TOL 存在），跳过"
        else
            if "$PY" "$PATCH" --file "$EL" --apply; then
                ok "已打补丁：$EL"
                PATCHED=1
            else
                die "补丁失败。该脚本不会留下半成品（自动回滚），请检查锚点是否被上游改过"
            fi
        fi
    fi
fi

# ---- git 状态：确认补丁之外没污染其它已跟踪文件 -----------------------------
printf '\n%s=== git 状态 ===%s\n' "$BLD" "$RST"
cd "$CODE"
if git rev-parse --git-dir >/dev/null 2>&1; then
    mod="$(git status --porcelain --untracked-files=no)"
    if [[ -z "$mod" ]]; then
        ok "上游已跟踪文件无改动（注入文件是 untracked，安全）"
    else
        warn "上游已跟踪文件有改动："
        printf '%s\n' "$mod" | sed 's/^/    /'
        if [[ "${PATCHED:-0}" -eq 1 ]]; then
            ok "其中 utils/event_labels.py 是本脚本刚打的补丁（预期内）"
        fi
        warn "其余改动若不是 yaw wrap 等必要补丁，请先 git checkout ."
    fi
    printf '  commit: %s\n' "$(git rev-parse --short HEAD)"

    # ---- 把补丁过的文件加进 preflight 的 git 白名单 -------------------------
    # 不加这一步，训练侧 preflight 会以「working tree dirty」拒绝启动。
    if [[ "${PATCHED:-0}" -eq 1 ]]; then
        PF=""
        for c in "$CODE/preflight_train.py" \
                 "$ROOT/dynamicvla-training/official/preflight_train.py" \
                 "$ROOT/server_fullft/preflight_train.py"; do
            [[ -f "$c" ]] && { PF="$c"; break; }
        done
        if [[ -z "$PF" ]]; then
            warn "找不到 preflight_train.py —— 请手工把 utils/event_labels.py 加进其 git 白名单"
        elif grep -q 'event_labels\.py' "$PF"; then
            ok "preflight 白名单已包含 event_labels.py"
        else
            "$PY" - "$PF" <<'PYEOF' || true
import re, sys
fp = sys.argv[1]
with open(fp, encoding="utf-8", newline="") as f:
    src = f.read()
m = re.search(r"(\b\w*(?:ALLOW|allow)\w*\s*=\s*[\[\(])(.*?)([\]\)])", src, re.S)
if not m:
    print("  ! 未找到白名单列表，请手工把 utils/event_labels.py 加进去")
    sys.exit(1)
body = m.group(2)
if "event_labels.py" in body:
    print("  ✓ 白名单已有 event_labels.py")
    sys.exit(0)
q = '"' if '"' in body else "'"
item = q + "utils/event_labels.py" + q
if not body.strip():
    # 空列表：直接放进去
    newbody = item
elif "\n" in body:
    # 多行列表：插在开头，缩进复制第一项的缩进
    indent = re.match(r"\s*", body).group(0)
    if not indent:
        mm = re.search(r"\n([ \t]+)\S", body)
        indent = "\n" + (mm.group(1) if mm else "    ")
    newbody = indent + item + "," + body
else:
    # 单行列表：前置 + 逗号
    newbody = item + ", " + body
new = src[:m.start(2)] + newbody + src[m.end(2):]
with open(fp, "w", encoding="utf-8", newline="") as f:
    f.write(new)
print("  ✓ 已把 utils/event_labels.py 加进 %s" % fp)
PYEOF
            grep -q 'event_labels\.py' "$PF" \
                && ok "白名单更新成功" \
                || warn "白名单自动更新失败，请手工加：utils/event_labels.py"
        fi

        printf '\n%s⚠ 打补丁后有两件事必须做，否则训练起不来：%s\n' "$YLW" "$RST"
        printf '   1) cd %s && pytest tests/ -q        # 期望仍 95 passed\n' "$CODE"
        printf '   2) python rebuild_ready.py           # 重建 managed-ready.json 的 sha256\n'
        printf '   （禁止手改 json 伪造 sha256 —— 那会让训练跑在「代码与权重不匹配」的未知状态上）\n'
        printf '   注意：migrate_checkpoint.SOURCE_FILES 若含被改文件，旧 checkpoint 不能直接 resume\n'
    fi
fi

printf '\n%s下一步：%s bash scripts/03_preflight.sh --root %s\n' "$BLD" "$RST" "$ROOT"