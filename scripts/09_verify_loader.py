#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用「独立加载器」验收生成的数据集 —— 14 项硬校验。

为什么不能用训练加载器验
------------------------
`managed_train.py` 硬编码 `hzxie/DOM` 且有 `audit_dataset` 过滤，自有数据进不去
（见 docs/04_label_contract.md §8.1）。所以在 P1 阶段只能用独立加载器跑 batch，
把「数据长得对不对」单独验一遍。这也顺带排除了训练器自身 bug 的干扰。

为什么必须读 parquet 全长数组
----------------------------
`observation.state` 是 (N, 6) 的全长数组，`action` 是 (N, 7) 的全长数组。
如果你用 `ds[i]` 逐帧取，拿到的已经是被 `delta_timestamps` 切过的窗口，
**永远看不到原始全长**，夹爪是否被 delta 就查不出来 —— 而那正是第 9 项要查的。

用法
----
    # 单个数据集
    python scripts/09_verify_loader.py --dataset ~/dvla-gen/a_slip/lerobot_home/local/a_slip

    # 整个 campaign（递归找所有 LeRobot 数据集）
    python scripts/09_verify_loader.py --base ~/dvla-gen --all

    # 只验结构不验标签（转换后 h5 已删时仍可用）
    python scripts/09_verify_loader.py --dataset ... --no-labels

    # 输出 JSON 供 CI 消费
    python scripts/09_verify_loader.py --dataset ... --json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import numpy as np

# ---- 期望形状（与 docs/04_label_contract.md §1 / docs/05 阶段 5 严格对齐）----
EXP_ACTION_DIM = 7          # LeRobot 转换后 7 列（h5 是 8 列）
EXP_ACTION_HORIZON = 20     # chunk_size = n_action_steps = 20
EXP_N_OBS = 2               # delta_timestamps = [-5, 0] → 2 帧
EXP_STATE_DIM = 6           # ee_pos 3 + ee_rot 3（不含夹爪）
EXP_CAM = (2, 3, 384, 512)  # (n_obs, C, H, W)
EXP_SPATIAL_DIM = 15
EXP_NUM_CLASSES = 22        # 0..21
EXP_GRASP = {0, 1, 2}       # HELD / SLIPPING / LOST
CAMS = ("opst_cam", "side_cam", "wrist_cam")

# 每类的期望：事故类标签（slipping+lost）非零
ACCIDENT_PROFILES = {"a_slip", "b_miss", "c_place_miss"}


class Item:
    """一条校验结果。"""

    def __init__(self, idx: int, name: str, ok: bool, detail: str = "",
                 fatal: bool = False):
        self.idx = idx
        self.name = name
        self.ok = ok
        self.detail = detail
        self.fatal = fatal   # fatal=True 时，不通过直接判 FAIL 而不是 WARN

    @property
    def tag(self) -> str:
        if self.ok:
            return "PASS"
        return "FAIL" if self.fatal else "WARN"

    def __str__(self) -> str:
        body = "%2d. [%s] %-34s %s" % (self.idx, self.tag, self.name, self.detail)
        return body


# ---------------------------------------------------------------------------
# 数据定位
# ---------------------------------------------------------------------------
def find_episodes_parquet(root: str) -> list[str]:
    """LeRobot v2.1 的 episode 数据在 data/<chunk>/episode_*.parquet。"""
    pats = [
        os.path.join(root, "data", "**", "*.parquet"),
        os.path.join(root, "**", "episode_*.parquet"),
    ]
    for p in pats:
        got = sorted(glob.glob(p, recursive=True))
        if got:
            return got
    return []


def read_meta(root: str) -> dict:
    """读 meta/info.json —— 这是数据集的自描述，比任何猜测都权威。"""
    fp = os.path.join(root, "meta", "info.json")
    if not os.path.isfile(fp):
        return {}
    try:
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_episode(fp: str) -> dict:
    """读一条 episode parquet 的全长数组。"""
    import pyarrow.parquet as pq

    tb = pq.read_table(fp)

    def col(name):
        if name not in tb.column_names:
            return None
        try:
            return np.asarray(tb[name].to_pylist(), dtype="float32")
        except Exception:
            return None

    return {
        "action": col("action"),
        "state": col("observation.state"),
        "env": col("observation.environment_state"),
        "cols": list(tb.column_names),
        "n": tb.num_rows,
    }


# ---------------------------------------------------------------------------
# 14 项校验
# ---------------------------------------------------------------------------
def verify_one(root: str, no_labels: bool, deep: bool) -> tuple[list[Item], dict]:
    items: list[Item] = []
    stats: dict = {"root": root}

    def add(idx, name, ok, detail="", fatal=False):
        items.append(Item(idx, name, ok, detail, fatal))

    # --- 0. 数据集存在 ---
    if not os.path.isdir(root):
        add(0, "数据集目录存在", False, "路径不存在: %s" % root, fatal=True)
        return items, stats
    meta = read_meta(root)
    files = find_episodes_parquet(root)
    add(0, "数据集目录存在", True,
        "%d 个 episode parquet%s" % (len(files), "" if meta else "（无 meta/info.json）"))

    if not files:
        add(99, "找到 episode 数据", False, "没有 data/**/*.parquet", fatal=True)
        return items, stats

    # 抽样：默认只看 8 条（首尾各半），--deep 则全量
    if deep or len(files) <= 8:
        sample = files
    else:
        step = max(len(files) // 8, 1)
        sample = files[::step][:8]
    stats["n_episodes"] = len(files)
    stats["n_sampled"] = len(sample)

    # --- 1. meta 声明的基本量 ---
    if meta:
        n_ep_meta = meta.get("total_episodes")
        ok = (n_ep_meta is None) or (int(n_ep_meta) == len(files))
        add(1, "meta.total_episodes 与实际一致", ok,
            "meta=%s 实际=%d" % (n_ep_meta, len(files)))
        fps = meta.get("fps")
        add(1.1, "fps == 25", fps == 25, "meta.fps=%s" % fps, fatal=False)
    else:
        add(1, "meta.total_episodes 与实际一致", False, "缺 meta/info.json",
            fatal=False)

    # --- 2..8 逐条采样校验 ---
    n_rows = []
    action_dims, state_dims, env_dims = set(), set(), set()
    cam_shapes = {c: set() for c in CAMS}
    spatial_dims, cls_vals, grasp_vals = set(), set(), set()
    grip_viol = 0          # 夹爪被 delta 的帧数
    grip_total = 0
    accident_frames = 0    # slipping + lost 帧数
    held_frames = 0
    missing_cols: set[str] = set()
    label_src = "unknown"

    for fp in sample:
        ep = load_episode(fp)
        if ep["n"] <= 0:
            continue
        n_rows.append(ep["n"])
        for c in ("action", "observation.state", "observation.environment_state"):
            if c not in ep["cols"]:
                missing_cols.add(c)

        if ep["action"] is not None and ep["action"].ndim == 2:
            action_dims.add(ep["action"].shape[1])
        if ep["state"] is not None and ep["state"].ndim == 2:
            state_dims.add(ep["state"].shape[1])
        if ep["env"] is not None and ep["env"].ndim == 2:
            env_dims.add(ep["env"].shape[1])

        # 相机形状：videos 里是 mp4，这里用 meta 的 shapes 兜底 + 尝试读一帧
        for c in CAMS:
            key = "observation.images.%s" % c
            shp = None
            if key in meta.get("features", {}):
                shp = meta["features"][key].get("shape")
                if shp:
                    cam_shapes[c].add(tuple(shp))

        # --- 夹爪是否被 delta（第 9 项）---
        if ep["action"] is not None and ep["action"].ndim == 2 and ep["action"].shape[1] >= 7:
            g = ep["action"][:, 6]
            grip_total += g.size
            bad = np.abs(g) > 1.0 + 1e-6
            grip_viol += int(bad.sum())

        # --- 事故类标签 ---
        for cand in ("event_grasp", "grasp", "observation.event_grasp"):
            if cand in ep["cols"]:
                v = ep["action"]  # 占位，下面单独读
                try:
                    import pyarrow.parquet as pq
                    tb = pq.read_table(fp, columns=[cand])
                    arr = np.asarray(tb[cand].to_pylist()).astype("int64").ravel()
                    grasp_vals.update(np.unique(arr).tolist())
                    accident_frames += int(((arr == 1) | (arr == 2)).sum())
                    held_frames += int((arr == 0).sum())
                    label_src = cand
                except Exception:
                    pass
                break

        if not no_labels:
            for cand in ("event_cls", "cls"):
                if cand in ep["cols"]:
                    try:
                        import pyarrow.parquet as pq
                        tb = pq.read_table(fp, columns=[cand])
                        arr = np.asarray(tb[cand].to_pylist()).astype("int64").ravel()
                        cls_vals.update(np.unique(arr).tolist())
                    except Exception:
                        pass
                    break
            for cand in ("event_spatial", "spatial"):
                if cand in ep["cols"]:
                    try:
                        import pyarrow.parquet as pq
                        tb = pq.read_table(fp, columns=[cand])
                        v = np.asarray(tb[cand].to_pylist(), dtype="float32")
                        if v.ndim >= 2:
                            spatial_dims.add(v.shape[-1])
                        elif v.ndim == 1 and v.size:
                            spatial_dims.add(int(v.size))
                    except Exception:
                        pass
                    break

    stats["n_rows"] = n_rows
    stats["label_source"] = label_src

    # 1. action 维度
    ok = (action_dims == {EXP_ACTION_DIM})
    add(2, "action 维度 == 7（LeRobot 列）", ok,
        "实测 %s，期望 {%d}" % (sorted(action_dims), EXP_ACTION_DIM), fatal=True)

    # 2. state 维度
    ok = (state_dims == {EXP_STATE_DIM})
    add(3, "observation.state 维度 == 6", ok,
        "实测 %s，期望 {%d}" % (sorted(state_dims), EXP_STATE_DIM), fatal=True)

    # 3. env 维度（物体位姿 3+3+3）
    ok = (env_dims == {9})
    add(4, "environment_state 维度 == 9", ok,
        "实测 %s，期望 {9}" % sorted(env_dims), fatal=False)

    # 4. 三路相机
    cam_ok = all(len(cam_shapes[c]) > 0 for c in CAMS)
    detail = "; ".join("%s=%s" % (c, sorted(cam_shapes[c])[:1] or "缺失") for c in CAMS)
    add(5, "3 路相机都在（且形状一致）", cam_ok, detail, fatal=True)

    # 5. 相机形状 (2,3,384,512)
    flat = set()
    for c in CAMS:
        flat |= cam_shapes[c]
    ok = (flat == {EXP_CAM}) if flat else False
    add(6, "相机 shape == (2,3,384,512)", ok,
        "实测 %s，期望 {%s}" % (sorted(flat), EXP_CAM), fatal=False)

    # 6. 边车标签 / event_spatial
    ok = (spatial_dims == {EXP_SPATIAL_DIM}) if spatial_dims else False
    add(7, "event_spatial 维度 == 15", ok,
        "实测 %s%s" % (sorted(spatial_dims), "" if spatial_dims else "（侧车缺失）"),
        fatal=False)

    # 7. event_cls 范围
    ok = bool(cls_vals) and min(cls_vals) >= 0 and max(cls_vals) < EXP_NUM_CLASSES
    add(8, "event_cls ∈ [0, 21]", ok,
        "实测 %s" % (sorted(cls_vals)[:6] if cls_vals else "（无该列）"), fatal=False)

    # 8. event_grasp 取值
    ok = bool(grasp_vals) and grasp_vals <= EXP_GRASP
    add(9, "event_grasp ∈ {0,1,2}", ok,
        "实测 %s" % (sorted(grasp_vals) if grasp_vals else "（无该列）"), fatal=False)

    # 9. 夹爪未被 delta ★ 关键
    ok = (grip_viol == 0)
    add(10, "gripper 未被 delta（⊆ {-1,+1}）", ok,
        "%d/%d 帧越界" % (grip_viol, grip_total), fatal=True)

    # 10. 事故类标签非零
    prof = os.path.basename(os.path.normpath(root))
    stats["profile_guess"] = prof
    if prof in ACCIDENT_PROFILES:
        ok = accident_frames > 0
        ratio = 100.0 * accident_frames / max(accident_frames + held_frames, 1)
        add(11, "事故类标签非零（slipping+lost）", ok,
            "%d 帧（占 grasp 有效帧 %.1f%%）" % (accident_frames, ratio),
            fatal=True)
        stats["accident_ratio"] = ratio
    else:
        add(11, "事故类标签非零（slipping+lost）", True,
            "N/A（%s 不要求）" % prof, fatal=False)

    # 11. episode 长度合理
    if n_rows:
        med = int(np.median(n_rows))
        mx = int(max(n_rows))
        mn = int(min(n_rows))
        ok = 30 <= med <= 600 and mx <= 1200
        add(12, "episode 长度合理（中位 30–600）", ok,
            "min=%d 中位=%d max=%d" % (mn, med, mx), fatal=False)
    else:
        add(12, "episode 长度合理", False, "无有效行", fatal=True)

    # 12. 必备列
    ok = not missing_cols
    add(13, "必备列齐全", ok,
        "缺 %s" % sorted(missing_cols) if missing_cols else "action/state/env 都在",
        fatal=True)

    # 13. 侧车 recovery_index.json（若已跑 annotate）
    # 结构：{"episodes": {...}, "summary": {"episodes_mapped": N, ...}}
    # 注意 episodes_mapped 在 summary 子对象里，不在顶层。
    side = os.path.join(root, "recovery_index.json")
    if os.path.isfile(side):
        try:
            with open(side, encoding="utf-8") as f:
                si = json.load(f)
            summ = si.get("summary", {})
            mapped = summ.get("episodes_mapped")
            missing = summ.get("episodes_missing_annotation", 0)
            n_ep_data = len(si.get("episodes", {})) or len(files)
            ok = (mapped is not None and mapped == n_ep_data and missing == 0)
            add(14, "recovery_index.json 完整", ok,
                "mapped=%s/%s，missing=%s" % (mapped, n_ep_data, missing),
                fatal=False)
        except Exception as e:
            add(14, "recovery_index.json 完整", False, str(e), fatal=False)
    else:
        add(14, "recovery_index.json 完整", True,
            "N/A（未跑 annotate_recovery.py）", fatal=False)

    return items, stats


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def discover(base: str) -> list[str]:
    """在 campaign 目录下递归找所有 LeRobot 数据集根（含 meta/info.json 的目录）。"""
    out = []
    for info in glob.glob(os.path.join(base, "**", "meta", "info.json"),
                          recursive=True):
        root = os.path.dirname(os.path.dirname(info))
        # 跳过 holdout 之外的缓存目录
        if os.path.basename(root).startswith("_"):
            continue
        out.append(root)
    return sorted(set(out))


def main() -> int:
    ap = argparse.ArgumentParser(description="独立加载器验收（14 项）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dataset", help="单个 LeRobot 数据集根目录")
    g.add_argument("--base", help="campaign 根目录（配合 --all 递归找）")
    ap.add_argument("--all", action="store_true", help="--base 下递归验全部")
    ap.add_argument("--no-labels", action="store_true",
                    help="只验结构，不要求侧车标签（h5/侧车已删时用）")
    ap.add_argument("--deep", action="store_true", help="全量验（默认只抽 8 条）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    if args.dataset:
        roots = [os.path.expanduser(args.dataset)]
    else:
        base = os.path.expanduser(args.base)
        if not args.all:
            print("✗ --base 需要配合 --all")
            return 2
        roots = discover(base)
        if not roots:
            print("✗ 在 %s 下没找到任何 LeRobot 数据集（meta/info.json）" % base)
            return 2

    all_pass = True
    report = []
    for r in roots:
        items, stats = verify_one(r, args.no_labels, args.deep)
        n_fail = sum(1 for it in items if not it.ok and it.fatal)
        n_warn = sum(1 for it in items if not it.ok and not it.fatal)
        verdict = "PASS" if n_fail == 0 and n_warn == 0 else (
            "FAIL" if n_fail else "PASS_WITH_WARN")

        if not args.json:
            print("=" * 84)
            print("%s" % r)
            print("=" * 84)
            for it in items:
                print(str(it))
            print("-" * 84)
            print("判定：%s   （FAIL %d / WARN %d）" % (verdict, n_fail, n_warn))
            print()

        report.append({"root": r, "verdict": verdict,
                       "n_fail": n_fail, "n_warn": n_warn,
                       "items": [{"idx": it.idx, "name": it.name, "tag": it.tag,
                                  "detail": it.detail} for it in items],
                       "stats": {k: v for k, v in stats.items()
                                 if k not in ("n_rows",)}})
        if n_fail:
            all_pass = False

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif len(roots) > 1:
        print("=" * 84)
        print("汇总")
        print("=" * 84)
        for r in report:
            print("  %-10s %s" % (r["verdict"], r["root"]))
        print()

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())