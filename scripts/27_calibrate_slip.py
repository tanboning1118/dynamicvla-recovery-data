#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A 类滑脱强度标定：找「还滑得动、但不至于滚飞」的最小配重。

为什么需要这个脚本
------------------
`REC_SLIP_MASS_SCALE=8` 是「保证一定滑脱」的暴力值，不是物理必需值。
它不改变滑脱是否发生（那由「质量 × 降摩擦后是否压过指间最大静摩擦」决定），
只改变滑脱之后的一切：

    物体落地动能 ∝ m  ->  弹跳高度、滚动距离、静止所需时间

而 A 类的两条放弃线全部挂在滑脱之后：

    WAIT_SETTLE:  dist_grasp >= max_reach_dist  -> GIVE_UP
    WAIT_SETTLE:  wait >= SETTLE_MAX_TIME(5 s)  -> GIVE_UP

实测 A 类产出 26%（13/50），损失发生在滑脱之后而不是之前 —— 所以配重是
当前最大的产出率杠杆。但它有下界：质量必须大到压过 Franka 位置驱动手指
产生的法向力，物体才会真的从指间滑出。这个下界只能在仿真里实测，推不出来。

本脚本扫这条曲线：固定摩擦 μ、抬升高度、settle 预算，只动配重，
记录「真滑脱率」与「放弃率」的交叉点。

怎么读结果
----------
    配重  尝试  保存  真滑脱   放弃   纯度
      8     8     2     2/8    6/8   100%   <- 滑得动，但滚飞了
      4     8     6     6/8    1/8   100%   <- 甜点区
      2     8     8     0/8    0/8      -    <- 夹住了，没滑脱

选「真滑脱率 > 60% 且放弃率最低」的最小配重。太小的配重不会报错，
它只会让滑脱不再发生 —— 而 `--require_slip` 会把这类样本整条丢掉，
所以表现为「保存率很高但产出全废」，正是本项目最典型的静默失败。

用法
----
    python scripts/27_calibrate_slip.py --scales 2,3,4,5,6,8 --n 8
    python scripts/27_calibrate_slip.py --scales 3,4 --n 4 --dry-run
    python scripts/27_calibrate_slip.py --scales 3,4 --n 4 --profile profiles/a_slip.env

不传 `--scales` 时只跑当前的 8，等价于「复测一次基线」。
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys

import h5py
import numpy as np

# 严格对齐 place_recovery_sm.PlaceRecSmState（4=GRASP、5=LIFT，错一位归因全偏）
S_RESET = 1
S_SLIP = 10
S_SETTLE = 11
S_RECOVER = 12
S_GIVE_UP = 13

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


# --------------------------------------------------------------------- profile
def apply_overrides(text, overrides):
    """把 KEY=VALUE 覆盖写进 profile 文本。

    有则替换 export 行，无则在文件头插入。这让同一脚本既能扫配重也能扫
    别的任何单变量（如 GEN_EPISODE_LENGTH 的 A 类 30 s vs 45 s 对照）——
    B 类的「真抓空 100% 超时」教训就是 EP 预算不足，A 类从没对照过。
    """
    for key, value in overrides:
        pat = re.compile(r"^export\s+%s=.*$" % re.escape(key), re.M)
        line = "export %s=%s" % (key, value)
        if pat.search(text):
            text = pat.sub(line, text)
        else:
            text = line + "\n" + text
    return text


def make_profile(base_path, mass_scale, dst_path, overrides=None):
    """复制 base profile 并替换配重（以及任何 --override 的变量）。

    不直接改 profiles/a_slip.env：那是已验证基线的唯一真源（26% 产出率
    就是它跑出来的），标定过程必须零污染。
    """
    with open(base_path, "r", encoding="utf-8") as fp:
        text = fp.read()

    pat = re.compile(r"^export\s+REC_SLIP_MASS_SCALE=.*$", re.M)
    if not pat.search(text):
        raise SystemExit(
            "%s 里没有 export REC_SLIP_MASS_SCALE= —— 标定无从下手" % base_path
        )
    text = pat.sub("export REC_SLIP_MASS_SCALE=%g" % mass_scale, text)
    if overrides:
        text = apply_overrides(text, overrides)
    text = (
        "# 自动生成：27_calibrate_slip.py（不要手改，也不要提交）\n"
        + text
    )

    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    with open(dst_path, "w", encoding="utf-8") as fp:
        fp.write(text)
    return dst_path


# ------------------------------------------------------------------------ 统计
def scan_batch(batch_dir):
    """读一批 h5 的 sm_state，统计滑脱/放弃/RESET。

    只读 sm_state 不算像素：标定关心的是「状态机走没走到滑脱」，相机冻屏
    是另一个失效模式（由 25_verify_episode.py 负责），混在一起会让配重
    曲线失真。
    """
    rows = {"saved": 0, "slip": 0, "give_up": 0, "reset": 0, "settle": 0,
            "bad": 0}
    files = sorted(glob.glob(os.path.join(batch_dir, "*.h5")))
    for path in files:
        rows["saved"] += 1
        try:
            with h5py.File(path, "r") as fp:
                if "sm_state" not in fp:
                    rows["bad"] += 1
                    continue
                st = set(int(s) for s in fp["sm_state"][:])
        except Exception:
            rows["bad"] += 1
            continue
        # 真滑脱 = 进过 10 且成功进入恢复段 12。只有 10 没有 12 是
        # 「滑了但没救回来」，训练价值仍在，但不该计入成功产率。
        if S_SLIP in st and S_RECOVER in st:
            rows["slip"] += 1
        if S_GIVE_UP in st:
            rows["give_up"] += 1
        if S_RESET in st:
            rows["reset"] += 1
        if S_SETTLE in st:
            rows["settle"] += 1
    rows["n_files"] = len(files)
    return rows


def find_batch(out_root, name):
    """24_run_class.sh 可能把 h5 放在 OUTDIR 根下或子目录里，两种都认。"""
    base = os.path.join(out_root, name)
    if not os.path.isdir(base):
        return None
    if glob.glob(os.path.join(base, "*.h5")):
        return base
    subs = [d for d in glob.glob(os.path.join(base, "*")) if os.path.isdir(d)]
    # 取 h5 最多的那个子目录（生成器有时按 seed 分桶）
    subs.sort(key=lambda d: len(glob.glob(os.path.join(d, "*.h5"))), reverse=True)
    if subs and glob.glob(os.path.join(subs[0], "*.h5")):
        return subs[0]
    return None


# ------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scales", default="8",
                    help="逗号分隔的配重倍数，默认 8（复测基线）")
    ap.add_argument("--n", type=int, default=8, help="每档尝试条数")
    ap.add_argument("--seed", type=int, default=9100001, help="起始 seed")
    ap.add_argument("--profile", default=os.path.join(REPO, "profiles/a_slip.env"))
    ap.add_argument("--out", default=os.environ.get("OUT_ROOT",
                                                    "/root/gpufree-data/work/out"))
    ap.add_argument("--tag", default="calib_slip", help="输出子目录前缀")
    ap.add_argument("--override", action="append", default=[],
                    metavar="KEY=VALUE",
                    help="额外覆盖的 profile 变量，可重复。例："
                         "--override GEN_EPISODE_LENGTH=45 做 A 类 EP 对照")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印将执行的命令，不跑仿真")
    args = ap.parse_args()

    overrides = []
    for ov in args.override:
        if "=" not in ov:
            raise SystemExit("--override 需要 KEY=VALUE 形式，收到 %r" % ov)
        k, v = ov.split("=", 1)
        overrides.append((k.strip(), v.strip()))

    scales = [float(x) for x in args.scales.split(",") if x.strip()]
    if not scales:
        raise SystemExit("--scales 为空")

    run_sh = os.path.join(HERE, "24_run_class.sh")
    if not os.path.exists(run_sh):
        raise SystemExit("找不到 %s" % run_sh)

    tmp_dir = os.path.join(args.out, "_calib_profiles")
    results = []

    for i, m in enumerate(scales):
        name = "%s_m%g" % (args.tag, m)
        prof = os.path.join(tmp_dir, "a_slip_m%g.env" % m)
        seed = args.seed + i * 100000
        cmd = ["bash", run_sh, prof, name, str(seed), str(args.n), args.out]

        print("\n" + "=" * 68)
        print("[%d/%d] 配重 ×%g  ->  %s" % (i + 1, len(scales), m,
                                            os.path.join(args.out, name)))
        print("      " + " ".join(cmd))
        if args.dry_run:
            # dry-run 不落盘：曾经在 Windows 上按 /root 生成过一堆空目录，
            # 预演就该只预演。
            print("      (dry-run) 将生成 profile：%s" % prof)
            results.append({"mass": m, "dry": True})
            continue

        make_profile(args.profile, m, prof, overrides)

        rc = subprocess.call(cmd)
        if rc != 0:
            print("[warn] 退出码 %d（仿真崩了？继续下一档）" % rc)

        batch = find_batch(args.out, name)
        if batch is None:
            print("[warn] %s 下没有 h5，本档跳过" % name)
            results.append({"mass": m, "saved": 0, "slip": 0, "give_up": 0,
                            "reset": 0, "settle": 0, "bad": 0, "n": args.n})
            continue

        rows = scan_batch(batch)
        rows["mass"] = m
        rows["n"] = args.n
        rows["dir"] = batch
        results.append(rows)

    if args.dry_run:
        print("\n[dry-run] 未执行。去掉 --dry-run 即开跑。")
        return 0

    # ------------------------------------------------------------- 汇总
    ov_txt = ("，叠加 " + " ".join(k + "=" + v for k, v in overrides)) if overrides else ""
    print("\n" + "=" * 68)
    print("单变量扫描汇总（每档 %d 次尝试，扫描维度=REC_SLIP_MASS_SCALE%s）"
          % (args.n, ov_txt))
    print("-" * 68)
    print("%-6s %6s %6s %10s %8s %8s %8s" % (
        "扫描值", "保存", "真滑脱", "滑脱率", "放弃", "RESET", "纯度"))
    print("-" * 68)
    best = None
    for r in results:
        n = max(r["n"], 1)
        rate = r["slip"] / float(n)
        purity = ("%5.0f%%" % (100.0 * r["slip"] / r["saved"])) if r["saved"] else "    -"
        print("%-6g %6d %6d %9.0f%% %8d %8d %8s" % (
            r["mass"], r["saved"], r["slip"], 100 * rate,
            r["give_up"], r["reset"], purity))
        # 甜点：滑脱率够高、放弃最少。同等滑脱率下取更小的扫描值
        if rate >= 0.6:
            key = (r["give_up"], -rate, r["mass"])
            if best is None or key < best[0]:
                best = (key, r)
    print("-" * 68)

    if best is None:
        print("[结论] 没有任何一档的滑脱率达到 60%。")
        if overrides:
            print("       本次叠加了 override，先排查叠加项本身（如 EP 太短、")
            print("       关键注入被关掉），再复核 REC_SLIP_FRICTION 是否生效")
            print("       （看 run_config.json 里实际读到的值）。")
        else:
            print("       扫描值太小 -> 物体被夹住没滑出；需要往大调，或复核")
            print("       REC_SLIP_FRICTION 是否真的写进了场景（看 run_config.json）。")
    else:
        r = best[1]
        print("[结论] 扫描值 %g%s 最优：滑脱率 %.0f%%，放弃 %d/%d。"
              % (r["mass"], ov_txt, 100.0 * r["slip"] / max(r["n"], 1),
                 r["give_up"], r["n"]))
        print("       按此把 profiles/a_slip.env 的对应参数改掉，")
        print("       再用 25_verify_episode.py --expect A_slip 复验纯度，")
        print("       然后重跑一批 n=50 更新 docs/05 的产出率基线。")

    print("\n提示：标定产物在 %s/%s_* ，" % (args.out, args.tag))
    print("      确认结论后可以整体删掉，不要混进量产目录。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
