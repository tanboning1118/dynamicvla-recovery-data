#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""恢复数据 episode 验收：完整性 + 语义 + 相机活动性，一次判死。

为什么要这个脚本：本项目的失效模式几乎全是"静默产废"——文件正常保存、
帧数正常、数值轨迹正常，但数据本身没有训练价值。已实测踩到的三种：

  1. 相机冻屏：--disable_fabric 与 --enable_cameras 同时用，物理变换只写
     Fabric，渲染器读的 USD 停在初始位姿。ee_pos 跨度 0.81 m，画面却是死的。
  2. 事件没发生：C 类扰动后状态机跟着物体走，D 类放置精度毫米级，产出的
     全是"干净成功轨迹"，但文件名和产出率看不出来。
  3. 超时截断：B 类真抓空样本因 episode 预算不足被丢弃，留下来的反而是没
     抓空的干净轨迹。

只看产出率三个坑一个都发现不了。必须逐条查 sm_state 与像素。

用法：
    python scripts/25_verify_episode.py OUT_DIR [OUT_DIR ...]
    python scripts/25_verify_episode.py --expect A OUT_DIR   # 按类校验
    python scripts/25_verify_episode.py --quiet OUT_DIR      # 只输出汇总

退出码：0 = 全部通过；1 = 存在失败项（可用于 CI 或生产闸门）。
"""
import argparse
import glob
import json
import os
import shutil
import sys

import h5py
import numpy as np

# ------------------------------------------------------------------ 状态机语义
# 严格对齐 payload/simulations/state_machines/place_recovery_sm.py 的 PlaceRecSmState。
# 注意 4 是 GRASP_OBJECT、5 是 LIFT_OBJECT——错一位会让 RESET 归因完全跑偏，
# 初学者最容易在这里把「抬升失败」读成「接近目标失败」。
S_INIT = 0
S_RESET = 1
S_APPROACH_ABOVE_OBJECT = 2
S_APPROACH_OBJECT = 3
S_GRASP_OBJECT = 4
S_LIFT_OBJECT = 5
S_APPROACH_ABOVE_TARGET = 6
S_APPROACH_TARGET = 7
S_PLACE_OBJECT = 8
S_TO_TARGET = 9
S_SLIP_DETECT = 10
S_SETTLE = 11
S_RECOVER = 12
S_GIVE_UP = 13

# 相机冻屏判定。冻屏时渲染器读的 USD 停在初始位姿，首末帧几乎逐像素相同；
# 正常轨迹即使有大量静止等待帧，首末帧差异也在 80% 以上（实测 82–95%）。
# 所以首末差异是主判据，逐帧中位数只能辅助——大量 waiting 帧会把它压到 5% 左右。
FROZEN_FIRST_LAST = 0.05   # 首末显著变化像素低于此值 → 判冻屏
SUSPECT_FIRST_LAST = 0.30  # 低于此值 → 警告（可能视角几乎不变）
PIXEL_DELTA = 10  # uint8 灰度差阈值


def classify(sm):
    """判类。返回 (类别, 依据)。

    四类：A 滑脱 / B 抓空 / C 放偏 / D 干净对照。

    C 不能只看「含状态 1」。实测绝大多数 RESET 来自抓取失败（夹爪没夹住、空手
    抬升、搬运掉件），那是真失败，但不是「放偏后重放」。必须用 RESET 的触发前
    状态区分，否则会把整批抓取失败当成 C 类收下。
    """
    st = set(int(s) for s in sm)
    if S_SLIP_DETECT in st and S_RECOVER in st:
        return "A_slip", "slip(10)+recover(12)"
    if S_RECOVER in st and S_SLIP_DETECT not in st:
        return "B_miss", "recover(12) without slip(10)"
    if S_SLIP_DETECT in st:
        return "unknown", "slip(10) without recover(12)"
    if st & {S_RESET, S_GIVE_UP}:
        whys = sorted({o[2] for o in reset_origin(sm)}) or ["give_up(13)"]
        if "place_failed" in whys:
            return "C_place", "reset from place stage"
        return "X_reset", "reset/abort from " + "/".join(whys)
    if st.isdisjoint({S_RESET, S_SLIP_DETECT, S_RECOVER, S_GIVE_UP}):
        return "D_control", "no recovery event"
    return "unknown", "unexpected state set"


def reset_origin(sm):
    """判定每个 RESET 由什么触发。返回 [(帧号, 前一状态, 归因)]。

    归因规则来自实测 + 状态机真实语义（4=GRASP、5=LIFT，别记错）：
      - 接近阶段（≤3）被 RESET → 计划过期，即物体被移动后目标失效（C 类的签名）
      - GRASP_OBJECT(4) → 夹爪闭合阶段失败
      - LIFT_OBJECT(5) → 抬起来了但物体没跟上来，即空手
      - 接近目标（6/7）→ 搬运途中掉件
      - PLACE/TO_TARGET(8/9) → 放下后判定不在容器内（D 类的签名）
    """
    out = []
    for i in range(1, len(sm)):
        if sm[i] == S_RESET and sm[i - 1] != S_RESET:
            prev = int(sm[i - 1])
            if prev <= S_APPROACH_OBJECT:
                why = "plan_stale(disturb)"
            elif prev == S_GRASP_OBJECT:
                why = "grasp_failed"
            elif prev == S_LIFT_OBJECT:
                why = "grasp_empty"
            elif prev in (S_APPROACH_ABOVE_TARGET, S_APPROACH_TARGET):
                why = "dropped_in_transit"
            elif prev in (S_PLACE_OBJECT, S_TO_TARGET):
                why = "place_failed"
            else:
                why = "state_%d" % prev
            out.append((i, prev, why))
    return out


def cam_frozen(f, cam):
    """判相机是否冻屏。返回 (首末差异占比, 逐帧中位占比) 或 (None, None)。

    首末差异是主判据：冻屏时渲染器读的 USD 停在初始位姿，首末帧逐像素相同。
    """
    if cam not in f:
        return None, None
    v = f[cam][:]
    if len(v) < 3:
        return None, None
    a, b = v[0].astype(np.int16), v[-1].astype(np.int16)
    first_last = float((np.abs(b - a).max(axis=-1) > PIXEL_DELTA).mean())
    step = max(1, len(v) // 60)  # 最多采 60 帧，长 episode 不必逐帧算
    fracs = []
    for i in range(1, len(v), step):
        d = np.abs(v[i].astype(np.int16) - v[i - 1].astype(np.int16))
        fracs.append(float((d.max(axis=-1) > PIXEL_DELTA).mean()))
    med = float(np.median(fracs)) if fracs else 0.0
    return first_last, med


def check_one(h5_path, cam_check=True):
    """验收单条 episode。返回 dict，'errors' 非空即失败。"""
    rec = {"path": h5_path, "errors": [], "warns": []}
    jp = h5_path[:-3] + ".json"
    if not os.path.exists(jp):
        rec["errors"].append("缺 json")
        return rec
    meta = json.load(open(jp))
    rmeta = meta.get("recovery", {})

    try:
        f = h5py.File(h5_path, "r")
    except Exception as e:  # 半写文件
        rec["errors"].append("h5 打不开: %s" % e)
        return rec

    with f:
        if "sm_state" not in f:
            rec["errors"].append("缺 sm_state")
            return rec
        sm = f["sm_state"][:].astype(int)
        n = len(sm)
        rec["frames"] = n

        # --- 完整性：所有数据集首维必须等于帧数，浮点不能有 NaN ---
        for k in f.keys():
            d = f[k]
            if d.ndim == 0:
                continue
            if d.shape[0] != n:
                rec["errors"].append("%s 首维 %d != 帧数 %d" % (k, d.shape[0], n))
            if d.dtype.kind == "f":
                nan = int(np.isnan(d[:]).sum())
                if nan:
                    rec["errors"].append("%s 含 %d 个 NaN" % (k, nan))

        # --- 语义：类别 + RESET 成因 ---
        cls, why = classify(sm)
        rec["class"] = cls
        rec["class_why"] = why
        rec["states"] = sorted(set(int(s) for s in sm))
        rec["resets"] = reset_origin(sm)

        # --- 元数据一致性：json 标注必须和 sm_state 对得上 ---
        if rmeta.get("has_slip") and S_SLIP_DETECT not in rec["states"]:
            rec["errors"].append("json has_slip=1 但 sm_state 无 10")
        if rmeta.get("has_retry") and S_RECOVER not in rec["states"]:
            rec["errors"].append("json has_retry=1 但 sm_state 无 12")
        for key, st in (("slip_frame", S_SLIP_DETECT), ("retry_frame", S_RECOVER)):
            fr = rmeta.get(key)
            if isinstance(fr, int) and 0 <= fr < n and S_SLIP_DETECT in rec["states"]:
                # 标注帧应落在该状态区间内（容许 ±3 帧相位差）
                lo, hi = max(0, fr - 3), min(n, fr + 4)
                if st not in sm[lo:hi]:
                    rec["warns"].append("%s=%d 附近无状态 %d" % (key, fr, st))

        # --- 相机活动性：冻屏是静默失效的头号来源，必须查 ---
        if cam_check:
            cams = {}
            for cam in ("wrist_cam_rgb", "opst_cam_rgb", "side_cam_rgb"):
                first_last, med = cam_frozen(f, cam)
                if first_last is None:
                    continue
                cams[cam] = (first_last, med)
                if first_last < FROZEN_FIRST_LAST:
                    rec["errors"].append("%s 冻屏（首末帧仅 %.2f%% 像素变化）"
                                         % (cam, first_last * 100))
                elif first_last < SUSPECT_FIRST_LAST and cam == "wrist_cam_rgb":
                    # 只有 wrist 是随动相机，健康轨迹首末必然大幅不同。
                    # opst/side 是固定机位，机械臂只占画面一小块，首末 15–25% 属正常。
                    rec["warns"].append("%s 首末差异仅 %.0f%%，视角可能几乎不变"
                                        % (cam, first_last * 100))
            rec["cams"] = cams
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+", help="产出目录")
    ap.add_argument("--expect", default=None,
                    help="期望类别（A_slip/B_miss/C_place/D_control），不符即判失败")
    ap.add_argument("--prune", action="store_true",
                    help="把失败或类别不符的 episode 移到 <dir>/_rejected/（不删除，可审计）")
    ap.add_argument("--no-cam", action="store_true", help="跳过相机检查（大数据集提速）")
    ap.add_argument("--quiet", action="store_true", help="只输出汇总")
    args = ap.parse_args()

    rows, n_fail = [], 0
    for d in args.dirs:
        files = sorted(glob.glob(os.path.join(d, "*.h5")))
        if not files:
            print("[WARN] %s 无 h5" % d)
            continue
        for p in files:
            r = check_one(p, cam_check=not args.no_cam)
            r["batch"] = os.path.basename(os.path.abspath(d))
            r["dir"] = d
            # 类别是否达标。四类互斥，不做兜底——兜底会让 C 类整批误判通过。
            r["class_ok"] = (r.get("class") == args.expect) if args.expect else True
            rows.append(r)
            if r["errors"] or not r["class_ok"]:
                n_fail += 1
            if not args.quiet:
                status = "FAIL" if (r["errors"] or not r["class_ok"]) else (
                    "WARN" if r["warns"] else "ok")
                print("%-4s %s/%s  n=%s  class=%s" % (
                    status, r["batch"], os.path.basename(p)[:44],
                    r.get("frames", "?"), r.get("class", "?")))
                if r.get("cams"):
                    print("      cam 首末/逐帧: " + "  ".join(
                        "%s=%.0f%%/%.1f%%" % (k.replace("_cam_rgb", ""), v[0] * 100, v[1] * 100)
                        for k, v in r["cams"].items()))
                for e in r["errors"]:
                    print("      [ERR] " + e)
                if not r["class_ok"]:
                    print("      [ERR] 类别不符：期望 %s，实际 %s" % (args.expect, r.get("class")))
                for w in r["warns"]:
                    print("      [warn] " + w)

    # ------------------------------------------------------------ 汇总
    print("\n" + "=" * 62)
    print("batch            n   ok  fail  classes")
    by_batch = {}
    for r in rows:
        b = by_batch.setdefault(r["batch"], {"n": 0, "fail": 0, "cls": {}})
        b["n"] += 1
        if r["errors"] or not r["class_ok"]:
            b["fail"] += 1
        c = r.get("class", "?")
        b["cls"][c] = b["cls"].get(c, 0) + 1
    for b, v in sorted(by_batch.items()):
        print("%-15s %3d %3d %5d  %s" % (
            b, v["n"], v["n"] - v["fail"], v["fail"],
            ", ".join("%s=%d" % kv for kv in sorted(v["cls"].items()))))

    print("-" * 62)
    print("总计 %d 条，失败 %d 条" % (len(rows), n_fail))
    if args.expect:
        good = sum(1 for r in rows if r["class_ok"] and not r["errors"])
        print("[%s] --expect %s：%d/%d 符合" % (
            "PASS" if n_fail == 0 else "FAIL", args.expect, good, len(rows)))

    # 含 RESET 但不算 C 类的轨迹：归因于抓取/搬运失败，不是「放偏后重放」。
    xr = [r for r in rows if r.get("class") == "X_reset"]
    if xr:
        print("\n含 RESET 但不成立为 C 类的轨迹（按 RESET 触发前状态归因）：")
        for r in xr:
            origins = sorted({o[2] for o in r["resets"]})
            print("  %-44s %s" % (os.path.basename(r["path"])[:44], origins[:4]))

    # ------------------------------------------------------------ 剔除
    # 移到 _rejected/ 而不是删除：误判时可以捞回来，也留证据说明为什么剔。
    if args.prune:
        moved = 0
        for r in rows:
            if not r["errors"] and r["class_ok"]:
                continue
            h5 = r["path"]
            rej = os.path.join(r["dir"], "_rejected")
            os.makedirs(rej, exist_ok=True)
            for p in (h5, h5[:-3] + ".json"):
                if os.path.exists(p):
                    shutil.move(p, os.path.join(rej, os.path.basename(p)))
            # events 里同名 jsonl 一并移走，避免下游按种子回查时对不上
            ev = os.path.join(r["dir"], "events")
            if os.path.isdir(ev):
                stem = os.path.basename(h5).split("_")[-1].rsplit(".", 1)[0].lstrip("0")
                for p in glob.glob(os.path.join(ev, "%s*.jsonl" % stem)):
                    shutil.move(p, os.path.join(rej, os.path.basename(p)))
            moved += 1
        print("\n[prune] 已移出 %d 条到 <batch>/_rejected/" % moved)
        if moved:
            print("        这些不是坏文件，是「不属于该批次目标类」或「相机有问题的样本」，"
                  "转换前必须剔除，否则事故标签密度会失真。")

    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
