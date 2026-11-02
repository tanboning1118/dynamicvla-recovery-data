#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 grasp_label 加「段级抬升先决」—— 修复空抓被错标成 HELD 的阻断级缺陷。

问题
----
`grasp_label` 只用 `|p_ee − p_obj| < 5cm` 判「手里有东西」。空抓（手落在物体旁边、
闭合时夹到空气）时手恰好贴着或围着物体，距离同样 <5cm → 被标成 HELD。

实测（2026-11-02，工位机）：3 个物理空抓段（物体 rise_max ≤ 4.3 mm）分别有
21% / 21% / 18% 的帧被标成 HELD，另有 57% 被标成 SLIPPING。两个语义都不对。

对照真抓段：HELD 40/40 = 100%。

修法
----
在 grasp_label 里加段级先决：

    rise = obj_z[start:end].max() - obj_z[start]
    if rise < RISE_TOL:  return GRASP_LOST, True     # 物体从未被带起来

33 段全量双峰：
    空抓簇 rise ≤ 17 mm（7 段）
    真抓簇 rise ≥ 93 mm（26 段）
    谷底宽 76 mm，5.4 倍空档
阈值 20–90 mm 之间任取结论不变，工程取中位 30 mm。

为什么必须段级不能帧级
----------------------
逐帧要求「已抬升」会把 friction 的真 hold 从 28 帧砍到 9 帧 —— 抓握初期本来还没抬起来。

对 DOM 的影响（已实测）
----------------------
200 条跨任务等距抽样：hold 段 210 个，rise<30mm 的 2 个（1.0%），
其中被错标 HELD 的帧 0；60 条完整对账共 20 帧变化，全部是修正。

用法
----
    # 先看会改什么（不落盘）
    python patches/apply_grasp_rise_patch.py --file ~/dynamicvla-training/official/utils/event_labels.py --check

    # 真正落盘（自动备份 .bak_<时间戳>）
    python patches/apply_grasp_rise_patch.py --file ... --apply

    # 回滚
    python patches/apply_grasp_rise_patch.py --file ... --rollback

    # DOM 侧零副作用校验
    python patches/apply_grasp_rise_patch.py --file ... --validate-dom ~/datasets/hzxie/DOM --n-ep 200
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import sys
import time

# ---- 补丁内容 ---------------------------------------------------------------
# 1) 新增常量：插在 GRASP_WARN 之后
ANCHOR_CONST = "GRASP_WARN = "
CONST_ADD = '''
#: 段级「抬升先决」阈值。物体在 hold 段内的 z 向最大抬升低于此值，判为空抓。
#: 实测（2026-11-02，33 个 hold 段）：真抓 ≥ 93 mm，空抓 ≤ 17 mm，谷底宽 76 mm。
#: 20–90 mm 之间任取，friction 真滑脱数据零误判。取中位 30 mm 保守偏空抓侧。
RISE_TOL = 0.03
'''

# 2) 函数签名加 obj_z（默认 None，保持向后兼容）
ANCHOR_SIG = """def grasp_label(
    holds: list[tuple[int, int]],
    dist: torch.Tensor,
    t: int,
) -> tuple[int, bool]:"""
SIG_NEW = """def grasp_label(
    holds: list[tuple[int, int]],
    dist: torch.Tensor,
    t: int,
    obj_z: torch.Tensor | None = None,
) -> tuple[int, bool]:"""

# 3) 函数体内插入段级先决
ANCHOR_BODY = """        if not inside.any():
            # 整段没进过 5 cm。手到过旁边（<8 cm）→ 真的抓空；否则是别的物体
            return GRASP_LOST, bool(float(seg.min()) < OBJ_TRACK_TOL)
"""
BODY_NEW = """        if not inside.any():
            # 整段没进过 5 cm。手到过旁边（<8 cm）→ 真的抓空；否则是别的物体
            return GRASP_LOST, bool(float(seg.min()) < OBJ_TRACK_TOL)
        # 段级抬升先决：5 cm 距离判不出「手贴着物体但没夹住」——空抓闭合时手
        # 恰好环绕物体，距离同样在 5 cm 内，会被错标成 HELD。
        # 物体若从未被带起来（rise < RISE_TOL），整段就是空手，与距离无关。
        # 必须段级不能帧级：抓握初期本来还没抬起来，逐帧判会把真 hold 砍掉 2/3。
        if obj_z is not None:
            rise = float(obj_z[start:end].max() - obj_z[start])
            if rise < RISE_TOL:
                return GRASP_LOST, True
"""

# 4) build_targets 调用点
ANCHOR_CALL = "    grasp, grasp_valid = grasp_label(holds, dist, t)"
CALL_NEW = ("    # 物体 z 序列供 grasp_label 做段级抬升判定（空抓 vs 真抓）\n"
            "    grasp, grasp_valid = grasp_label(holds, dist, t, env_state[:, 2])")


def find_anchor_style(src: str) -> dict:
    """上游可能改过缩进/写法，做几种容错匹配。"""
    cands = {}
    # 签名
    for pat in [ANCHOR_SIG,
                ANCHOR_SIG.replace("\n", "\r\n"),
                ANCHOR_SIG.replace("torch.Tensor", "Tensor")]:
        if pat in src:
            cands["sig"] = pat
            break
    # 函数体
    if ANCHOR_BODY in src:
        cands["body"] = ANCHOR_BODY
    else:
        # 容错：只按第一行 + return 匹配
        import re
        m = re.search(
            r"( +)if not inside\.any\(\):\n(.*?)return GRASP_LOST, bool\(float\(seg\.min\(\)\) < OBJ_TRACK_TOL\)\n",
            src, re.S)
        if m:
            cands["body"] = m.group(0)
            cands["body_indent"] = m.group(1)
    # 调用点
    for pat in [ANCHOR_CALL,
                "    grasp, grasp_valid = grasp_label(holds, dist, t, )",
                "\tgrasp, grasp_valid = grasp_label(holds, dist, t)"]:
        if pat in src:
            cands["call"] = pat
            break
    return cands


def _read(path: str) -> str:
    """读源码。

    ★ 必须 ``newline=""``。默认模式下 Python 会把 CRLF 归一成 LF 读进来，
    写回时又按平台转回 CRLF —— 在 Windows 上跑 ``--apply`` 会把整个 594 行
    文件改写成 CRLF，git diff 变成全文重写，拷回 Linux 还留下 ''。
    ``newline=""`` 关掉这个转换，保证只改该改的那几行。
    """
    with open(path, encoding="utf-8", newline="") as fp:
        return fp.read()


def _write(path: str, text: str) -> None:
    """写源码（不做任何换行转换）。"""
    with open(path, "w", encoding="utf-8", newline="") as fp:
        fp.write(text)


def check(path: str) -> int:
    src = _read(path)
    print("=" * 80)
    print("检查：%s" % path)
    print("=" * 80)

    if "RISE_TOL" in src:
        print("  ! 已包含 RISE_TOL —— 补丁可能已打过")
        return 0

    cands = find_anchor_style(src)
    need = ["sig", "body", "call"]
    missing = [k for k in need if k not in cands]

    for k, label in [("sig", "grasp_label 签名"),
                     ("body", "函数体内 if not inside.any() 分支"),
                     ("call", "build_targets 调用点")]:
        if k in cands:
            print("  ✓ 找到%s" % label)
        else:
            print("  ✗ 未找到%s —— 上游可能改过这段代码" % label)

    has_const = ANCHOR_CONST in src
    print("  %s 找到常量锚点 GRASP_WARN" % ("✓" if has_const else "✗"))

    if missing or not has_const:
        print("\n无法安全打补丁。请人工处理，或把这段代码贴到 issue。")
        return 2

    print("\n可以打补丁。用 --apply 落盘。")
    return 0


def apply_patch(path: str, dry: bool) -> int:
    raw = _read(path)

    # 换行风格探测。上游是 LF（Linux 仓库），但不排除被 CRLF 化过。
    # 内部统一按 LF 处理，落盘时再还原 —— 否则锚点里的 \n 匹配不上。
    crlf = "\r\n" in raw
    src = raw.replace("\r\n", "\n") if crlf else raw

    if "RISE_TOL" in src:
        print("! 文件已包含 RISE_TOL，跳过（幂等）")
        return 0

    cands = find_anchor_style(src)
    if not all(k in cands for k in ("sig", "body", "call")):
        print("✗ 锚点不全，拒绝打补丁。先跑 --check 看细节")
        return 2

    new = src
    changes = []

    # 1. 常量
    if ANCHOR_CONST in new:
        # 找到 GRASP_WARN 那一整行，插在它后面
        lines = new.split("\n")
        for i, ln in enumerate(lines):
            if ln.startswith("GRASP_WARN"):
                lines.insert(i + 1, CONST_ADD.rstrip("\n"))
                changes.append("新增 RISE_TOL 常量")
                break
        new = "\n".join(lines)
    else:
        print("✗ 找不到 GRASP_WARN 常量行")
        return 2

    # 2. 签名
    new = new.replace(cands["sig"], SIG_NEW, 1)
    changes.append("grasp_label 签名加 obj_z 参数")

    # 3. 函数体
    body = cands["body"]
    if "body_indent" in cands:
        ind = cands["body_indent"]
        insert = (
            "%s# 段级抬升先决：5 cm 距离判不出「手贴着物体但没夹住」。\n"
            "%sif obj_z is not None:\n"
            "%s    rise = float(obj_z[start:end].max() - obj_z[start])\n"
            "%s    if rise < RISE_TOL:\n"
            "%s        return GRASP_LOST, True\n" % (ind, ind, ind, ind, ind))
    else:
        insert = BODY_NEW[len(body):]
    new = new.replace(body, body + insert, 1)
    changes.append("函数体插入段级抬升先决")

    # 4. 调用点
    call = cands["call"]
    if call == ANCHOR_CALL:
        new = new.replace(call, CALL_NEW, 1)
    else:
        new = new.replace(
            call, call.replace("grasp_label(holds, dist, t)",
                               "grasp_label(holds, dist, t, env_state[:, 2])"), 1)
    changes.append("build_targets 调用点传 obj_z")

    print("=" * 80)
    print("将做 %d 处改动：" % len(changes))
    for i, c in enumerate(changes, 1):
        print("  %d. %s" % (i, c))
    print("=" * 80)

    if dry:
        # 打印 diff 摘要
        import difflib
        diff = list(difflib.unified_diff(
            src.split("\n"), new.split("\n"),
            fromfile="a/event_labels.py", tofile="b/event_labels.py", lineterm="", n=4))
        print("\n".join(diff[:120]))
        if len(diff) > 120:
            print("... （diff 共 %d 行，已截断）" % len(diff))
        print("\n（--check 模式，未落盘）")
        return 0

    # 备份 + 写
    bak = "%s.bak_%s" % (path, time.strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(path, bak)

    out = new.replace("\n", "\r\n") if crlf else new
    _write(path, out)
    print("\n✓ 已落盘：%s" % path)
    print("  备份：%s" % bak)
    print("  换行风格：%s（保持原样）" % ("CRLF" if crlf else "LF"))

    # 语法检查
    import ast
    try:
        ast.parse(new)
        print("  ✓ 语法检查通过")
    except SyntaxError as e:
        print("  ✗ 语法错误！回滚")
        shutil.copy2(bak, path)
        print("  %r" % e)
        return 3

    print("\n下一步：")
    print("  1. pytest tests/ -q          （期望仍 95 passed）")
    print("  2. python rebuild_ready.py    （重建 managed-ready.json 的 sha256）")
    print("  3. 把 utils/event_labels.py 加进 preflight 的 git 白名单")
    return 0


def rollback(path: str) -> int:
    baks = sorted(glob.glob(path + ".bak_*"))
    if not baks:
        print("✗ 没有备份可回滚")
        return 2
    latest = baks[-1]
    shutil.copy2(latest, path)
    print("✓ 已从 %s 回滚" % latest)
    return 0


def validate_dom(path: str, dom_root: str, n_ep: int) -> int:
    """在 DOM 真实数据上对比改前/改后的标签分布。"""
    if not os.path.isdir(dom_root):
        print("[SKIP] DOM 根目录不存在：%s" % dom_root)
        return 0

    sys.path.insert(0, os.path.dirname(os.path.abspath(path)))
    sys.path.insert(0, os.getcwd())
    try:
        from utils import event_labels as EL
    except Exception:
        import importlib.util
        spec = importlib.util.spec_from_file_location("event_labels", path)
        EL = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(EL)

    RISE = getattr(EL, "RISE_TOL", 0.03)

    def label_new(holds, dist, t, obj_z):
        """补丁后的逻辑（副本）。"""
        n = dist.shape[0]
        if t < 0 or t >= n:
            return EL.GRASP_HELD, False
        for s, e in holds:
            if not (s <= t < e):
                continue
            seg = dist[s:e]
            inside = seg < EL.GRASP_SLIP_TOL
            if not inside.any():
                return EL.GRASP_LOST, bool(float(seg.min()) < EL.OBJ_TRACK_TOL)
            if obj_z is not None:
                rise = float(obj_z[s:e].max() - obj_z[s])
                if rise < RISE:
                    return EL.GRASP_LOST, True
            idx = __import__("torch").nonzero(inside, as_tuple=False).flatten()
            last = int(idx[-1].item())
            if last == seg.shape[0] - 1:
                return EL.GRASP_HELD, True
            t_rel = t - s
            if t_rel > last:
                return EL.GRASP_LOST, True
            if last - t_rel <= EL.GRASP_WARN:
                return EL.GRASP_SLIPPING, True
            return EL.GRASP_HELD, True
        return EL.GRASP_HELD, False

    import numpy as np
    import pyarrow.parquet as pq
    import torch

    files = sorted(glob.glob(os.path.join(dom_root, "data", "**", "*.parquet"),
                             recursive=True))
    if not files:
        print("[SKIP] 未找到 parquet：%s" % dom_root)
        return 0

    # ★ 必须跨全范围抽样。DOM 按任务严格分块：
    #   ep 0-5880 long-horizon（2.84%）/ 5881-74354 pick（33%）/ 74355-207305 place（64%）
    # 只取前 N 个会 100% 落在 long-horizon，那里第一次抓取抓的是另一个物体
    # （距离 14-30cm），早在 >8cm 分支返回 LOST(invalid)，走不到抬升判据
    # → 抽样偏差会得出「零变化」的假结论。
    total_files = len(files)
    step = max(total_files // max(n_ep, 1), 1)
    files = files[::step][:n_ep]

    print("DOM parquet 总数 %d，等距抽样 %d 个（step=%d）"
          % (total_files, len(files), step))

    names = {0: "HELD", 1: "SLIPPING", 2: "LOST"}
    old_cnt, new_cnt = {}, {}
    seg_tot = seg_low = 0
    ep_used = 0

    for fp in files:
        try:
            tb = pq.read_table(fp, columns=[
                "action", "observation.state", "observation.environment_state"])
        except Exception as ex:
            print("  skip %s (%s)" % (os.path.basename(fp), ex))
            continue
        act = np.asarray(tb["action"].to_pylist(), dtype="float32")
        st = np.asarray(tb["observation.state"].to_pylist(), dtype="float32")
        env = np.asarray(tb["observation.environment_state"].to_pylist(), dtype="float32")
        if act.ndim != 2 or act.shape[1] < 7:
            continue
        ep_used += 1
        grip = torch.from_numpy(act[:, 6])
        dist = torch.from_numpy(np.linalg.norm(st[:, 0:3] - env[:, 0:3], axis=1))
        obj_z = torch.from_numpy(env[:, 2])
        holds = EL.detect_holds(grip)
        for a, b in holds:
            seg_tot += 1
            if float(obj_z[a:b].max() - obj_z[a]) < RISE:
                seg_low += 1
        for a, b in holds:
            for t in range(a, b):
                g_old, v_old = EL.grasp_label(holds, dist, t)
                g_new, v_new = label_new(holds, dist, t, obj_z)
                ko = "%s%s" % (names[g_old], "" if v_old else "(invalid)")
                kn = "%s%s" % (names[g_new], "" if v_new else "(invalid)")
                old_cnt[ko] = old_cnt.get(ko, 0) + 1
                new_cnt[kn] = new_cnt.get(kn, 0) + 1

    print("\nepisode %d，hold 段 %d，其中 rise < %.0f mm 的段 %d (%.1f%%)"
          % (ep_used, seg_tot, RISE * 1000, seg_low, 100.0 * seg_low / max(seg_tot, 1)))
    tot = max(sum(old_cnt.values()), 1)
    print("\n%-18s %12s %12s" % ("标签", "改前", "改后"))
    for k in sorted(set(old_cnt) | set(new_cnt)):
        o, nn = old_cnt.get(k, 0), new_cnt.get(k, 0)
        print("%-18s %6d(%5.1f%%) %6d(%5.1f%%)%s"
              % (k, o, 100.0 * o / tot, nn, 100.0 * nn / tot,
                 "   <-- 变化" if o != nn else ""))
    diff = sum(abs(old_cnt.get(k, 0) - new_cnt.get(k, 0)) for k in old_cnt)
    pct = 100.0 * diff / 2 / tot
    print("\n总帧 %d，标签变化帧 %d（%.3f%%）" % (tot, diff // 2, pct))
    print("判定：%s" % ("<1% 视为零副作用，可落盘" if pct < 1.0 else ">1%，需复查"))
    return 0 if pct < 1.0 else 4


def main() -> int:
    ap = argparse.ArgumentParser(description="grasp_label 段级抬升先决补丁")
    ap.add_argument("--file", required=True, help="官方 event_labels.py 路径")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="只检查锚点，不落盘")
    g.add_argument("--apply", action="store_true", help="落盘（自动备份）")
    g.add_argument("--rollback", action="store_true", help="从最近备份回滚")
    g.add_argument("--validate-dom", metavar="DOM_ROOT", help="在 DOM 上做零副作用校验")
    ap.add_argument("--n-ep", type=int, default=200, help="DOM 抽样条数（默认 200）")
    args = ap.parse_args()

    path = os.path.expanduser(args.file)
    if not os.path.isfile(path):
        print("✗ 文件不存在：%s" % path)
        return 1

    if args.rollback:
        return rollback(path)
    if args.check:
        return check(path)
    if args.validate_dom:
        return validate_dom(path, os.path.expanduser(args.validate_dom), args.n_ep)
    return apply_patch(path, dry=False)


if __name__ == "__main__":
    sys.exit(main())