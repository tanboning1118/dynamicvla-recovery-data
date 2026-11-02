#!/usr/bin/env bash
# ===========================================================================
# 01 · 下载并解压 DOM 资产
#
# 资产不随 git 分发（上游 README 明确 USD assets are intentionally excluded from Git，
# git ls-files objects 返回 0）。必须走官方下载：
#   https://gateway.infinitescript.com/?f=DOM-Test
#   https://gateway.infinitescript.com/?f=DOM-3D-Objects
#
# 用法：
#   bash scripts/01_fetch_assets.sh --zips ~/Downloads/dom-assets
#   bash scripts/01_fetch_assets.sh --urls      # 打印链接，不下载（机器上没浏览器时）
#   bash scripts/01_fetch_assets.sh --zips DIR --root ~/dynamicvla-demo
# ===========================================================================
set -euo pipefail

ZIP_DIR=""
ROOT=""
PRINT_ONLY=0
TEST_URL="https://gateway.infinitescript.com/?f=DOM-Test"
OBJ_URL="https://gateway.infinitescript.com/?f=DOM-3D-Objects"

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLD=$'\033[1m'; RST=$'\033[0m'
ok()   { printf '%s✓%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s!%s %s\n' "$YLW" "$RST" "$*"; }
die()  { printf '%s✗%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --zips)  ZIP_DIR="${2:-}"; shift 2 ;;
        --root)  ROOT="${2:-}";    shift 2 ;;
        --urls)  PRINT_ONLY=1;     shift ;;
        -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "未知参数：$1" ;;
    esac
done

if [[ $PRINT_ONLY -eq 1 ]]; then
    printf 'DOM Testing Set : %s\n' "$TEST_URL"
    printf 'DOM 3D Objects  : %s\n' "$OBJ_URL"
    printf '\n用浏览器下载这两个 zip，然后：\n'
    printf '  bash %s --zips <存放目录> [--root <上游根>]\n' "$0"
    exit 0
fi

[[ -n "$ZIP_DIR" ]] || die "必须指定 --zips（或用 --urls 打印链接）"
ROOT="${ROOT:-$HOME/dynamicvla-demo/dynamic-vla}"
[[ -d "$ROOT" ]] || die "上游根目录不存在：$ROOT（先跑 scripts/00_setup.sh）"

TEST_ZIP="$ZIP_DIR/DOM-Test.zip"
OBJ_ZIP="$ZIP_DIR/DOM-3D-Objects.zip"
for z in "$TEST_ZIP" "$OBJ_ZIP"; do
    [[ -f "$z" ]] || die "缺少 $z"
done

printf '%s=== 01 · 解压资产 ===%s\n' "$BLD" "$RST"
printf 'zip dir : %s\n' "$ZIP_DIR"
printf 'root    : %s\n\n' "$ROOT"

# 大小预检：DOM-Test 约 400 MB，DOM-3D-Objects 约 1.6 GB
for spec in "$TEST_ZIP:100" "$OBJ_ZIP:800"; do
    f="${spec%:*}"; min="${spec##*:}"
    mb=$(( $(stat -c%s "$f") / 1024 / 1024 ))
    if [[ $mb -lt $min ]]; then
        die "$(basename "$f") 只有 ${mb} MB，疑似下载不完整（应 > ${min} MB）"
    fi
    ok "$(basename "$f") ${mb} MB"
done

# 解压（-o 覆盖、-q 静默）
unzip -qo "$TEST_ZIP" -d "$ROOT" || die "解压 DOM-Test 失败"
ok "解压 DOM-Test"
unzip -qo "$OBJ_ZIP" -d "$ROOT" || die "解压 DOM-3D-Objects 失败"
ok "解压 DOM-3D-Objects"

# 校验
say() { printf '%s\n' "$*"; }
printf '\n%s=== 校验 ===%s\n' "$BLD" "$RST"
fail=0

n_cat=$(ls -1 "$ROOT/objects" 2>/dev/null | grep -v metadata.json | wc -l)
n_usd=$(find "$ROOT/objects" -name '*.usd' 2>/dev/null | wc -l)
n_scene=$(find "$ROOT/scenes" -maxdepth 1 -name '*.usd' 2>/dev/null | wc -l)
n_meta=$(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1]))))" \
         "$ROOT/objects/metadata.json" 2>/dev/null || echo 0)

chk() { # name actual min
    if [[ "$2" -ge "$3" ]]; then
        ok "$1 = $2（≥ $3）"
    else
        printf '%s✗%s %s = %s（应 ≥ %s）\n' "$RED" "$RST" "$1" "$2" "$3"
        fail=1
    fi
}
chk "对象类别数"   "$n_cat"   20
chk "对象 .usd 数" "$n_usd"   150
chk "场景 .usd 数" "$n_scene" 70
chk "metadata 条数" "$n_meta" 150

printf '\n磁盘占用：\n'
du -sh "$ROOT/objects" "$ROOT/scenes" 2>/dev/null | sed 's/^/  /'

if [[ $fail -ne 0 ]]; then
    die "资产校验未通过。检查 zip 是否下对（不要用镜像站，走官方 gateway）"
fi

printf '\n%s下一步：%s bash scripts/02_patch_upstream.sh --root %s\n' "$BLD" "$RST" "$(dirname "$ROOT")"