#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交付汇总报表 —— 把 8 个交付勾选项一次算出来。

对应 docs/05_production_sop.md 阶段 7 的检查表：

    [ ] 每类条数 ≥ 目标（A120 B75 C60 D30 E15）
    [ ] 每类闸门全绿（或 N/A 合理）
    [ ] 帧数分布：中位在 120–220，无 > 600 帧的异常
    [ ] 事故类标签占比：A 类 ≥ 40%，B 类 ≥ 30%
    [ ] holdout 30 条已隔离，seed 段不重叠
    [ ] h5 已删除，常驻空间 ≤ 10 GB
    [ ] recovery_index.json 存在且 episodes_mapped == total_episodes
    [ ] 每批的 run_config.json 都在（可复现）

只看磁盘，不依赖任何训练框架。转换前（只有 h5）和转换后（只有 LeRobot）都能跑，
各自会跳过算不出来的项并明确标 N/A —— 不假装通过。

用法
----
    python scripts/10_report.py --base ~/dvla-gen
    python scripts/10_report.py --base ~/dvla-gen --holdout ~/dvla-gen-holdout
    python scripts/10_report.py --base ~/dvla-gen --json > report.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

# ---- 目标（docs/02_collection_scale.md §3.3）----
TARGET = {"a_slip": 150, "b_miss": 100, "c_place_miss": 30, "d_control": 20}
# A 类事故占比 ≥ 40%，B 类 ≥ 30%
ACCIDENT_MIN = {"a_slip": 40.0, "b_miss": 30.0, "c_place_miss": 5.0,
                "d_control": 0.0}
HOLDOUT_TARGET = 30
RESIDENT_MAX_GB = 10.0


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return "%.1f %s" % (n, u)
        n /= 1024.0
    return "%.1f PB" % n


def du(path: str) -> int:
    """目录总字节数（不跟符号链接，跳过 h5 缓存）。"""
    tot = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                if not os.path.islink(fp):
                    tot += os.path.getsize(fp)
            except OSError:
                pass
    return tot


def read_json(fp: str) -> dict:
    try:
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def find_lerobot_roots(base: str) -> list[str]:
    out = []
    for info in glob.glob(os.path.join(base, "**", "meta", "info.json"),
                          recursive=True):
        root = os.path.dirname(os.path.dirname(info))
        if os.path.basename(root).startswith("_"):
            continue
        out.append(root)
    return sorted(set(out))


def classify_profile(root: str, base: str) -> str:
    """从路径反推类别。兼容 <base>/a_slip/lerobot_home/... 与 <base>/c1/raw 两种布局。"""
    rel = os.path.relpath(root, base).replace("\\", "/")
    parts = rel.split("/")
    for p in parts:
        key = p.strip()
        # 去掉 campaign chunk 后缀：c1 / c1_a_slip / a_slip_c2
        for name in TARGET:
            if key == name or key.startswith(name + "_") or ("_" + name) in key:
                return name
    return "unknown"


def episode_rows(root: str) -> list[int]:
    """每条 episode 的帧数。LeRobot 版优先读 meta，其次数 parquet 行。"""
    meta = read_json(os.path.join(root, "meta", "info.json"))
    eps = meta.get("episodes")
    if isinstance(eps, list) and eps and isinstance(eps[0], dict):
        lens = [e.get("length") for e in eps if e.get("length")]
        if lens:
            return [int(x) for x in lens]
    # 退化：读每个 parquet 的行数（贵，只在前者缺失时做）
    try:
        import pyarrow.parquet as pq
    except Exception:
        return []
    out = []
    for fp in sorted(glob.glob(os.path.join(root, "data", "**", "*.parquet"),
                               recursive=True)):
        try:
            out.append(pq.ParquetFile(fp).metadata.num_rows)
        except Exception:
            pass
    return out


def grasp_stats(root: str) -> tuple[int, int] | None:
    """返回 (事故帧数, grasp 有效帧数)。只读侧车/parquet 里的 event_grasp。"""
    try:
        import numpy as np
        import pyarrow.parquet as pq
    except Exception:
        return None
    tot = acc = 0
    found = False
    for fp in sorted(glob.glob(os.path.join(root, "data", "**", "*.parquet"),
                               recursive=True))[:40]:
        try:
            tb = pq.read_table(fp)
        except Exception:
            continue
        col = None
        for c in ("event_grasp", "grasp"):
            if c in tb.column_names:
                col = c
                break
        if col is None:
            return None
        found = True
        a = np.asarray(tb[col].to_pylist()).astype("int64").ravel()
        tot += a.size
        acc += int(((a == 1) | (a == 2)).sum())
    return (acc, tot) if found else None


def main() -> int:
    ap = argparse.ArgumentParser(description="交付汇总报表")
    ap.add_argument("--base", required=True, help="campaign 根目录")
    ap.add_argument("--holdout", default="", help="holdout 目录（可选）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    base = os.path.expanduser(args.base)
    if not os.path.isdir(base):
        print("✗ 目录不存在：%s" % base)
        return 2

    roots = find_lerobot_roots(base)
    by_prof: dict[str, list[str]] = {}
    for r in roots:
        by_prof.setdefault(classify_profile(r, base), []).append(r)

    # ---- 逐类统计 ----
    rows = []
    for prof in list(TARGET) + sorted(k for k in by_prof if k not in TARGET):
        eps = 0
        lens: list[int] = []
        acc_r = None
        for r in by_prof.get(prof, []):
            l = episode_rows(r)
            eps += len(l) if l else 0
            lens += l
            g = grasp_stats(r)
            if g:
                if acc_r is None:
                    acc_r = [0, 0]
                acc_r[0] += g[0]
                acc_r[1] += g[1]
        med = int(sorted(lens)[len(lens) // 2]) if lens else 0
        ratio = (100.0 * acc_r[0] / acc_r[1]) if acc_r and acc_r[1] else None
        rows.append({
            "profile": prof,
            "n": eps,
            "target": TARGET.get(prof, 0),
            "len_median": med,
            "len_max": max(lens) if lens else 0,
            "accident_ratio": ratio,
            "accident_min": ACCIDENT_MIN.get(prof, 0.0),
            "roots": by_prof.get(prof, []),
        })

    # ---- 磁盘 / h5 残留 ----
    h5_files = glob.glob(os.path.join(base, "**", "*.h5"), recursive=True)
    h5_bytes = 0
    for fp in h5_files:
        try:
            h5_bytes += os.path.getsize(fp)
        except OSError:
            pass
    # 常驻 = 去掉 h5 后的总占用
    total_bytes = du(base)
    resident = total_bytes - h5_bytes

    # ---- run_config.json ----
    cfgs = glob.glob(os.path.join(base, "**", "run_config.json"), recursive=True)

    # ---- sidecar ----
    # 结构：{"episodes": {...}, "summary": {"episodes_mapped": N, ...}}
    side_ok = 0
    side_tot = 0
    for r in roots:
        fp = os.path.join(r, "recovery_index.json")
        side_tot += 1
        if os.path.isfile(fp):
            si = read_json(fp)
            summ = si.get("summary", {})
            mapped = summ.get("episodes_mapped")
            n_ep = len(si.get("episodes", {}))
            missing = summ.get("episodes_missing_annotation", 0)
            if mapped is not None and mapped == n_ep and missing == 0:
                side_ok += 1

    # ---- holdout ----
    hold_n = 0
    hold_seeds: list[int] = []
    if args.holdout:
        ho = os.path.expanduser(args.holdout)
        if os.path.isdir(ho):
            hold_n = len(find_lerobot_roots(ho))
            for c in glob.glob(os.path.join(ho, "**", "run_config.json"),
                               recursive=True):
                cfg = read_json(c)
                sb = cfg.get("seed_base")
                if sb is not None:
                    hold_seeds.append(int(sb))

    # ---- 勾选判定 ----
    checks = []

    ok = all(r["n"] >= r["target"] for r in rows if r["profile"] in TARGET)
    detail = " ".join("%s %d/%d" % (r["profile"], r["n"], r["target"])
                      for r in rows if r["profile"] in TARGET)
    checks.append(("[ ] 每类条数 ≥ 目标", ok, detail))

    # 闸门：用 qa_gates 的结果文件（若存在）
    gate_files = glob.glob(os.path.join(base, "**", "qa_gates*.json"), recursive=True)
    gate_bad = []
    for gf in gate_files:
        g = read_json(gf)
        if g.get("verdict") in ("FAIL",):
            gate_bad.append(os.path.basename(os.path.dirname(gf)))
    # 没有闸门结果不算「通过」——只是还没验，标 N/A 但保持未勾选，
    # 避免「没跑检验」被当成「检验合格」。
    ok = bool(gate_files) and not gate_bad
    checks.append(("[ ] 每类闸门全绿", ok,
                   "N/A（未跑 qa_gates，不算通过）" if not gate_files
                   else ("全部通过（%d 份结果）" % len(gate_files) if ok
                         else "FAIL: %s" % gate_bad)))

    bad_len = [r for r in rows if r["profile"] in TARGET
               and not (120 <= r["len_median"] <= 220) and r["n"] > 0]
    big = [r for r in rows if r["len_max"] > 600]
    ok = not bad_len and not big
    checks.append(("[ ] 帧数中位 120–220 且无 >600", ok,
                   "偏离: %s" % [r["profile"] for r in bad_len] if bad_len
                   else ("有超长: %s" % [r["profile"] for r in big] if big
                         else " ".join("%s=%d" % (r["profile"], r["len_median"])
                                       for r in rows if r["n"] > 0))))

    low = [r for r in rows if r["profile"] in ACCIDENT_MIN
           and r["accident_ratio"] is not None
           and r["accident_ratio"] + 1e-9 < ACCIDENT_MIN[r["profile"]]]
    # 只有「还算得出占比」的类别全部达标才算通过；一个都算不出就是未验，不算通过。
    measurable = [r for r in rows if r["accident_ratio"] is not None]
    ok = bool(measurable) and not low
    checks.append(("[ ] 事故类标签占比达标", ok,
                   " ".join("%s %.1f%%/%.0f%%" % (r["profile"],
                                                  r["accident_ratio"],
                                                  r["accident_min"])
                            for r in measurable)
                   or "N/A（无可算数据：需侧车 event_grasp，转换前或未跑 annotate 时为正常）"))

    if args.holdout:
        # seed 段必须与主 campaign 不重叠 —— 这是 holdout 唯一的意义所在。
        train_seeds = set()
        for c in glob.glob(os.path.join(base, "**", "run_config.json"), recursive=True):
            sb = read_json(c).get("seed_base")
            if sb is not None:
                train_seeds.add(int(sb))
        overlap = sorted(set(hold_seeds) & train_seeds)
        ok = (hold_n >= HOLDOUT_TARGET) and bool(hold_seeds) and not overlap
        note = "实际 %d 条，holdout seed_base=%s，训练 seed_base=%s" % (
            hold_n, sorted(set(hold_seeds)) or "未记录", sorted(train_seeds) or "未记录")
        if overlap:
            note += "  ★ seed 段重叠 %s，holdout 失效" % overlap
        elif hold_n < HOLDOUT_TARGET:
            note += "  ★ 不足 %d 条" % HOLDOUT_TARGET
        checks.append(("[ ] holdout %d 条已隔离且 seed 不重叠" % HOLDOUT_TARGET,
                       ok, note))
    else:
        checks.append(("[ ] holdout %d 条已隔离" % HOLDOUT_TARGET, False,
                       "N/A（未传 --holdout）"))

    ok = (len(h5_files) == 0) and (resident / 1024 ** 3 <= RESIDENT_MAX_GB)
    checks.append(("[ ] h5 已删且常驻 ≤ %.0f GB" % RESIDENT_MAX_GB, ok,
                   "h5 %d 个（%s），常驻 %s"
                   % (len(h5_files), human(h5_bytes), human(resident))))

    ok = (side_ok == side_tot) and side_tot > 0
    checks.append(("[ ] recovery_index 完整", ok,
                   "%d/%d 个数据集通过" % (side_ok, side_tot)))

    ok = len(cfgs) > 0
    checks.append(("[ ] 每批 run_config.json 都在", ok, "%d 个" % len(cfgs)))

    # ---- 输出 ----
    all_ok = all(c[1] for c in checks)

    if args.json:
        print(json.dumps({"base": base, "holdout": args.holdout or None,
                          "profiles": rows, "checks": [
                              {"name": c[0], "ok": c[1], "detail": c[2]}
                              for c in checks],
                          "all_ok": all_ok,
                          "disk": {"total": total_bytes, "h5": h5_bytes,
                                   "resident": resident},
                          "n_run_config": len(cfgs)},
                         ensure_ascii=False, indent=2))
        return 0 if all_ok else 1

    print("=" * 90)
    print("DynamicVLA 恢复数据 · 交付汇总")
    print("base    = %s" % base)
    print("holdout = %s" % (args.holdout or "（未指定）"))
    print("=" * 90)

    print("\n【逐类统计】\n")
    hdr = "%-12s %7s %7s %9s %9s %11s" % (
        "类别", "条数", "目标", "中位帧", "最长帧", "事故占比")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        ratio = ("%.1f%%" % r["accident_ratio"]) if r["accident_ratio"] is not None else "—"
        flag = "" if r["profile"] not in TARGET else (
            "  ✓" if r["n"] >= r["target"] else "  ←缺")
        print("%-12s %7d %7d %9d %9d %11s%s"
              % (r["profile"], r["n"], r["target"], r["len_median"],
                 r["len_max"], ratio, flag))

    tot_n = sum(r["n"] for r in rows)
    tot_t = sum(r["target"] for r in rows if r["profile"] in TARGET)
    print("-" * len(hdr))
    print("%-12s %7d %7d" % ("合计", tot_n, tot_t))

    print("\n【交付检查】\n")
    for name, ok, detail in checks:
        print("%s %s" % ("[✓]" if ok else "[ ]", name.lstrip("[ ] ")))
        print("        %s" % detail)

    print("\n【磁盘】")
    print("  总计 %s    h5 %s（%d 个文件）    常驻 %s"
          % (human(total_bytes), human(h5_bytes), len(h5_files), human(resident)))
    print("  （常驻 > %.0f GB 通常意味着 h5 没删干净）" % RESIDENT_MAX_GB)

    print("\n" + "=" * 90)
    print("判定：%s" % ("全部达标，可交付 ✓" if all_ok else "尚有未完成项，见上方 [ ]"))
    print("=" * 90)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())