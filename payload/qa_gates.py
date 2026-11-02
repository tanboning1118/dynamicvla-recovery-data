# -*- coding: utf-8 -*-
"""五道生产闸门 + 标签分布实测（按 profile 类别自适应）。

设计原则：**用事实判定，不用声明判定。**
配置日志可能被重定向、也可能撒谎；机制不会。

闸门
----
G1  配置闸：批次是否处于预期的注入模式（由 run_config.json 或 G2 事实反推）
G2  机制闸：A 类滑脱窗口内夹爪命令必须恒 -1（否则是 open 模式，滑脱对训练不可见）
G3  几何闸：A 类滑脱后须出现连续 >= 10 帧 |p_ee-p_obj| > 5cm
G4  标签闸：事故类标签（slipping+lost）不得为 0（否则整批没造出事故）
G5  可复现闸：run_config.json 存在且 REC_* 字段完整

按类别自适应（★ 这是本版新增，原版只按 A 类设计，会把 C/E 误判 FAIL）
--------------------------------------------------------------------
A 滑脱 : G1/G2/G3 全查；要求出现 sm_state 10/11/12
B 抓空 : G2/G3 N/A（不滑脱）；要求出现 12，且**不应出现** 10/11
C 推偏 : G2/G3 N/A；要求出现 1（RESET）
D 放偏 : G2/G3 N/A；要求出现 1（RESET）
E 对照 : G2/G3 N/A；**不应出现** 1/10/11/12/13

同时输出：22 类阶段分布 + grasp 3 类分布 + 逐条明细，
这是「注入是否真的在标签层面留下痕迹」的实测。

判据逐字节复刻自 official/utils/event_labels.py（含 RISE_TOL 补丁），
避免用被测代码测自己。

用法
----
    python qa_gates.py <batch_dir>
    python qa_gates.py <batch_dir> --profile a_slip
    python qa_gates.py <batch_dir> --json     # 只输出 JSON
"""
import argparse
import glob
import json
import os
import re
import sys
from collections import Counter

import h5py
import numpy as np

# ---- 与 official/utils/event_labels.py 保持一致的常量 ------------------------
TOL = 0.05          # GRASP_SLIP_TOL
TRACK_TOL = 0.08    # OBJ_TRACK_TOL
WARN = 15           # GRASP_WARN
HORIZON = 20        # CHUNK_SIZE
CLOSE_SIGN = -1.0
RISE_TOL = 0.03     # 段级抬升先决（patches/grasp_rise_prior.patch）
CLASS_HOLD, CLASS_FAR = 0, 21

# ---- 状态码 -----------------------------------------------------------------
S_RESET, S_GRASP, S_TO_TARGET = 1, 4, 9
S_SLIP, S_SETTLE, S_RECOVER, S_GIVEUP = 10, 11, 12, 13

# ---- 每类的期望 -------------------------------------------------------------
EXPECT = {
    "A": {"require": [S_SLIP, S_SETTLE, S_RECOVER], "forbid": [],
          "g23": True,  "incident_min": 0.30,
          "desc": "滑脱后重抓"},
    "B": {"require": [S_RECOVER], "forbid": [S_SLIP, S_SETTLE],
          "g23": False, "incident_min": 0.20,
          "desc": "抓空后重试"},
    "C": {"require": [S_RESET], "forbid": [],
          "g23": False, "incident_min": 0.10,
          "desc": "推偏后重新定位"},
    "D": {"require": [S_RESET], "forbid": [],
          "g23": False, "incident_min": 0.05,
          "desc": "放偏后二次放置"},
    "E": {"require": [], "forbid": [S_RESET, S_SLIP, S_SETTLE, S_RECOVER, S_GIVEUP],
          "g23": False, "incident_min": 0.0,
          "desc": "纯成功对照"},
}


def detect_holds(g, cs=CLOSE_SIGN):
    c = (g == cs).tolist()
    out, t, n = [], 0, len(c)
    while t < n:
        if c[t] and (t == 0 or not c[t - 1]):
            s = t
            while t < n and c[t]:
                t += 1
            out.append((s, t))
        else:
            t += 1
    return out


def classify(holds, t):
    for s, e in holds:
        if s <= t < e:
            return CLASS_HOLD
    fut = [s for s, _ in holds if s >= t]
    if not fut:
        return CLASS_FAR
    gap = min(fut) - t
    return gap if 1 <= gap <= HORIZON else CLASS_FAR


def grasp_label(holds, dist, t, obj_z=None):
    """复刻 event_labels.grasp_label（含 RISE_TOL 段级抬升先决补丁）。"""
    n = len(dist)
    if t < 0 or t >= n:
        return 0, False
    for s, e in holds:
        if not (s <= t < e):
            continue
        seg = dist[s:e]
        inside = seg < TOL
        if not inside.any():
            return 2, bool(float(seg.min()) < TRACK_TOL)   # GRASP_LOST
        # ★ 段级抬升先决：空抓时手绕着物体，距离同样 <5cm，会被错标 HELD
        if obj_z is not None:
            rise = float(obj_z[s:e].max() - obj_z[s])
            if rise < RISE_TOL:
                return 2, True
        last = int(np.nonzero(inside)[0][-1])
        if last == seg.shape[0] - 1:
            return 0, True                                  # held
        tr = t - s
        if tr > last:
            return 2, True                                  # lost
        if last - tr <= WARN:
            return 1, True                                  # slipping
        return 0, True
    return 0, False


def guess_profile(base, cfg):
    """从 run_config.json 推断类别。"""
    cls = str(cfg.get("profile_class", "")).strip().upper()
    if cls in EXPECT:
        return cls
    name = str(cfg.get("profile_name", "") or cfg.get("profile", "")).lower()
    if "slip" in name and "miss" not in name:
        return "A"
    if "miss" in name and "place" not in name:
        return "B"
    if "disturb" in name:
        return "C"
    if "place_miss" in name or "placemiss" in name:
        return "D"
    if "control" in name:
        return "E"
    # 从注入参数反推
    if str(cfg.get("REC_SLIP", "")).strip() in ("1", "True", "true"):
        return "A"
    if str(cfg.get("REC_GRASP_CHECK", "")).strip() in ("1", "True", "true"):
        return "B"
    if str(cfg.get("REC_DISTURB_AT", "-1")).strip() not in ("-1", "", "None"):
        return "C"
    return "E"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base", nargs="?", default=os.path.expanduser("~/dvla-gen/rec_p16"))
    ap.add_argument("--profile", default=None, help="强制指定类别 A/B/C/D/E")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args()

    # ★ 必须 expanduser：os.path.exists 不展开 `~`，会导致所有判断静默为假
    base = os.path.expanduser(args.base.rstrip("/"))

    # 支持 campaign 布局：<base>/c*/raw
    raws = sorted(glob.glob(os.path.join(base, "raw"))) + \
           sorted(glob.glob(os.path.join(base, "c*", "raw")))
    raw = raws[0] if raws else os.path.join(base, "raw")

    h5s = []
    for r in (raws or [raw]):
        h5s += sorted(glob.glob(os.path.join(r, "*.h5")))
    h5s = sorted(h5s)

    # ---- 配置 ---------------------------------------------------------------
    cfg = {}
    for cand in [os.path.join(base, "run_config.json"),
                 os.path.join(base, "c00", "run_config.json")] + \
                sorted(glob.glob(os.path.join(base, "c*", "run_config.json"))):
        if os.path.exists(cand):
            try:
                cfg = json.load(open(cand, encoding="utf-8"))
                break
            except Exception:
                pass

    cls = (args.profile or guess_profile(base, cfg)).upper()
    exp = EXPECT.get(cls, EXPECT["A"])

    if not args.json:
        print("=" * 92)
        print("闸门校验 · %s" % base)
        print("类别：%s 类（%s）｜ h5 条数：%d ｜ 参数快照：%s"
              % (cls, exp["desc"], len(h5s), "有" if cfg else "无"))
        print("=" * 92)

    if not h5s:
        print("\n没有 h5。生成可能失败，检查 gen.log 里的 'no placement'")
        return 1

    # ---- G1 / G5 ------------------------------------------------------------
    # G1：A 类必须 friction；其他类不看 slip 模式
    # G5：参数快照字段数
    g1 = None
    if cls == "A":
        mode = str(cfg.get("REC_SLIP_MODE", "")).lower()
        if mode:
            g1 = (mode == "friction")
        else:
            g1 = None      # 留待 G2 事实反推
    g5 = None
    if cfg:
        rec_keys = [k for k in cfg if k.startswith("REC_")]
        g5 = len(rec_keys) >= 8

    def fmt(v):
        return "N/A" if v is None else ("PASS" if v else "FAIL")

    if not args.json:
        if cls == "A":
            print("G1 配置闸（friction 模式）    : %s%s"
                  % (fmt(g1), "（将 G2 反推）" if g1 is None else ""))
        else:
            print("G1 配置闸（friction 模式）    : N/A（%s 类不涉滑脱）" % cls)
        print("G5 可复现闸（REC_* 快照完整） : %s" % fmt(g5))

    # ---- 逐条解剖 -----------------------------------------------------------
    per = []
    cls_all, grasp_all = Counter(), Counter()
    states_seen = set()

    for p in h5s:
        key = os.path.basename(p)[:-3]
        jf = p[:-3] + ".json"
        rec = {}
        if os.path.exists(jf):
            try:
                d = json.load(open(jf, encoding="utf-8"))
                if isinstance(d, dict):
                    rec = d.get("recovery", {}) or {}
            except Exception:
                pass
        try:
            with h5py.File(p, "r") as f:
                a = f["action"][:]
                e = f["ee_pos"][:]
                op = f["object_pos"][:]
                sm = f["sm_state"][:].astype(int) if "sm_state" in f else None
        except Exception as ex:
            print("  skip %s (%s)" % (key[:30], ex))
            continue

        grip = a[:, -1] if a.shape[1] >= 7 else a[:, 6]
        dist = np.linalg.norm(e - op, axis=1)
        obj_z = op[:, 2]
        holds = detect_holds(grip)
        sf = rec.get("slip_frame", -1)

        # G2：滑脱窗口夹爪是否恒 -1
        g2 = None
        if cls == "A" and 0 <= sf < len(grip):
            w = grip[sf: min(len(grip), sf + 8)]
            g2 = bool(w.size and (w == CLOSE_SIGN).all())

        # G3：滑脱后最长连续 >5cm
        best = 0
        if cls == "A" and 0 <= sf < len(dist):
            run = 0
            for v in (dist[sf: min(len(dist), sf + 60)] >= TOL):
                run = run + 1 if v else 0
                best = max(best, run)
        g3 = (best >= 10) if cls == "A" and 0 <= sf < len(dist) else None

        # 该条的标签分布
        cc = Counter(classify(holds, t) for t in range(len(dist)))
        gc = Counter()
        for t in range(len(dist)):
            k, v = grasp_label(holds, dist, t, obj_z)
            if v:
                gc[k] += 1
        cls_all.update(cc)
        grasp_all.update(gc)
        if sm is not None:
            states_seen |= set(sm.tolist())

        per.append(dict(
            key=key[:36], n=len(dist), holds=len(holds),
            slip=sf, retry=rec.get("retry_frame", -1),
            giveup=rec.get("giveup_frame", -1),
            max_run=best, g2=g2, g3=g3,
            held=gc[0], slip_n=gc[1], lost=gc[2],
            n_cls=len(cc), has_sm=sm is not None,
            sm_uniq=sorted(set(sm.tolist())) if sm is not None else [],
        ))

    if not per:
        print("\n所有 h5 都读不了。检查文件完整性。")
        return 1

    # ---- 明细 ---------------------------------------------------------------
    if not args.json:
        print()
        print("%-36s %5s %4s %5s %5s %6s %5s %5s %5s %4s"
              % ("episode", "N", "hold", "slip", "retry", "run>5",
                 "held", "slip", "lost", "cls"))
        print("-" * 104)
        for r in per:
            print("%-36s %5d %4d %5d %5d %6d %5d %5d %5d %4d"
                  % (r["key"], r["n"], r["holds"], r["slip"], r["retry"],
                     r["max_run"], r["held"], r["slip_n"], r["lost"], r["n_cls"]))

    # ---- 汇总判定 -----------------------------------------------------------
    n = len(per)
    tot_frames = sum(r["n"] for r in per)
    n_valid = sum(grasp_all.values())
    n_incident = grasp_all[1] + grasp_all[2]
    inc_ratio = n_incident / max(n_valid, 1)

    g2p = sum(1 for r in per if r["g2"])
    g3p = sum(1 for r in per if r["g3"])
    g4 = inc_ratio > 0.0

    # G1 由 G2 事实反推
    if cls == "A" and g1 is None:
        g1 = (g2p == n) if n else None

    # 状态码检查
    req_ok = all(s in states_seen for s in exp["require"])
    forbid_hit = sorted(set(exp["forbid"]) & states_seen)

    if not args.json:
        print()
        if exp["g23"]:
            print("G2 机制闸（滑脱窗口 grip 恒 -1）: %s   %d/%d"
                  % ("PASS" if g2p == n else "FAIL", g2p, n))
            print("G3 几何闸（滑脱后连续 ≥10 帧 >5cm）: %s   %d/%d"
                  % ("PASS" if g3p == n else ("PARTIAL" if g3p else "FAIL"), g3p, n))
        else:
            print("G2 机制闸（滑脱窗口 grip 恒 -1）: N/A（%s 类不滑脱）" % cls)
            print("G3 几何闸（滑脱后连续 >5cm）   : N/A（%s 类不滑脱）" % cls)

        print("G4 标签闸（事故类标签非零）    : %s   slipping+lost = %d/%d = %.2f%%（期望 ≥%.0f%%）"
              % ("PASS" if g4 and inc_ratio >= exp["incident_min"] * 0.5 else "FAIL",
                 n_incident, n_valid, 100.0 * inc_ratio, 100.0 * exp["incident_min"]))

        print()
        print("状态码：本批出现 %s" % (sorted(states_seen) or "无 sm_state"))
        if exp["require"]:
            print("  必需 %s : %s" % (exp["require"], "PASS" if req_ok else "FAIL（缺失 %s）"
                                       % [s for s in exp["require"] if s not in states_seen]))
        if exp["forbid"]:
            print("  禁止 %s : %s" % (exp["forbid"],
                                       "PASS" if not forbid_hit else "FAIL（出现了 %s）" % forbid_hit))

    # ---- 分布 ---------------------------------------------------------------
    tot = sum(cls_all.values()) or 1
    hold_p = 100.0 * cls_all[0] / tot
    far_p = 100.0 * cls_all[21] / tot
    mid_p = 100.0 - hold_p - far_p

    g_held = 100.0 * grasp_all[0] / max(n_valid, 1)
    g_slip = 100.0 * grasp_all[1] / max(n_valid, 1)
    g_lost = 100.0 * grasp_all[2] / max(n_valid, 1)

    if not args.json:
        print()
        print("本批总帧 %d，有效 grasp 标注 %d 帧" % (tot_frames, n_valid))
        print("grasp 3 类：held %d (%.2f%%) / slipping %d (%.2f%%) / lost %d (%.2f%%)"
              % (grasp_all[0], g_held, grasp_all[1], g_slip, grasp_all[2], g_lost))
        print()
        print("22 类阶段分布（对比 DOM 的 36.40 / 18.46 / 45.14）：")
        print("  类 0  已接触 : %7d  %6.2f%%" % (cls_all[0], hold_p))
        print("  类 1-20 步进 : %7d  %6.2f%%" % (tot - cls_all[0] - cls_all[21], mid_p))
        print("  类 21 太远  : %7d  %6.2f%%" % (cls_all[21], far_p))
        print("  门控 alpha = 1-p21 = %.2f%% （DOM 54.86%%）" % (100.0 - far_p))

    # ---- 总判定 -------------------------------------------------------------
    verdicts = {
        "cls": cls,
        "n": n,
        "frames": tot_frames,
        "g1": g1,
        "g2_pass": g2p if exp["g23"] else None,
        "g3_pass": g3p if exp["g23"] else None,
        "g4_pass": bool(g4),
        "g5": g5,
        "incident_ratio": round(inc_ratio, 4),
        "states_seen": sorted(states_seen),
        "require_ok": req_ok,
        "forbid_hit": forbid_hit,
        "grasp": dict(grasp_all),
        "cls_dist": dict(cls_all),
        "per": per,
    }
    fails = []
    if cls == "A":
        if g1 is False: fails.append("G1 不是 friction")
        if g2p != n:   fails.append("G2 %d/%d" % (g2p, n))
        if g3p != n:   fails.append("G3 %d/%d" % (g3p, n))
    if not g4: fails.append("G4 事故类标签为 0")
    if g5 is False: fails.append("G5 参数快照不完整")
    if not req_ok: fails.append("必需状态缺失 %s" % [s for s in exp["require"] if s not in states_seen])
    if forbid_hit: fails.append("禁止状态出现 %s" % forbid_hit)
    verdicts["fails"] = fails
    verdicts["verdict"] = "PASS" if not fails else "FAIL"

    if not args.json:
        print()
        print("=" * 92)
        if not fails:
            print("\033[32m✓ 全部闸门通过\033[0m（%s 类，%d 条，%d 帧）" % (cls, n, tot_frames))
        else:
            print("\033[31m✗ 闸门失败\033[0m：%s" % "；".join(fails))
            print("  排查：%s" % {
                "G1 不是 friction": "profile 里 REC_SLIP_MODE 必须是 friction，不能靠默认值（默认 open）",
                "G4 事故类标签为 0": "注入没生效，检查 REC_* 环境变量真的传进去了",
                "G5 参数快照不完整": "用 scripts/05_generate.sh 起批次，不要裸调生成器",
            }.get(fails[0], "见 docs/06_troubleshooting.md"))
        print("=" * 92)

    out = os.path.join(base, "gates_result.json")
    with open(out, "w", encoding="utf-8") as fp:
        json.dump(verdicts, fp, ensure_ascii=False, indent=1)
    if not args.json:
        print("已写 %s" % out)
    else:
        print(json.dumps(verdicts, ensure_ascii=False, indent=1))

    return 0 if not fails else 2


if __name__ == "__main__":
    sys.exit(main())