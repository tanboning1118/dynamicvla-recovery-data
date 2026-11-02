#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定量测量「雪花」—— 用判据代替肉眼。

背景
----
在 5060 上不加 ``--path_tracing`` 时画面出现雪花，三路相机都有。但官方 DOM
数据集（27 万条）同样没有开路径追踪，抽帧却非常干净。所以"雪花"必有具体
成因，而且**与显卡算力无关** —— 它由渲染采样策略和编码参数决定。

本脚本把三种可能的成因分别量化，避免互相混淆：

1. ``static_flicker``  静止段相邻帧的平均绝对差（MAD，0-255 尺度）。
   机械臂与物体都不动时，理论上应只剩渲染噪声。
   → 高 = RTX 实时渲染的时域降噪未收敛（**渲染侧问题**，改渲染参数可解）

2. ``local_grain``     单帧 3x3 局部标准差的中位数。
   平坦表面（桌面、墙面）在干净画面里应接近 0。
   → 高而 static_flicker 低 = 每帧都有的固定颗粒（**降噪器残留或后处理**）

3. ``frame_delta``     相邻帧 MAD（含真实运动）。
   与物体速度强相关，**不能单独当噪声指标**，仅作参考。

对 mp4 输入额外给 ``encode_noise``：静止段帧间差的低位，即**纯压缩噪声**
（AV1 741 kbps 对 480x360@25fps 属于够用但偏紧）。

用法
----
    # DOM 基线（建参照）
    python 11_probe_noise.py --mp4 episode_000000.mp4 --label DOM_opst

    # 我们自己生成的 h5
    python 11_probe_noise.py --h5 out/xxx.h5 --label ours_notrace

    # 两个版本对比（同 seed 开/关 path_tracing）
    python 11_probe_noise.py --h5 a.h5 --h5 b.h5 --label off on

判据
----
    static_flicker <= 1.0   肉眼不可辨
    1.0 < x <= 3.0          轻微，可接受
    x > 3.0                 明显闪烁，需要改渲染设置

    local_grain   <= 1.5    干净
    1.5 < x <= 3.5          轻微颗粒
    x > 3.5                 明显颗粒（俗称雪花）

注意
----
``cv2.VideoCapture`` **解不了 AV1**，必须走 ffmpeg 管道，所以本脚本对 mp4
一律调用 ``ffmpeg -f rawvideo`` 解码，不 import cv2。
"""

import argparse
import json
import os
import subprocess
import sys

import numpy as np


# --------------------------------------------------------------- 读取
def read_mp4_frames(path, max_frames=None):
    """用 ffmpeg 解码成 rawvideo。AV1 必须走这条路，cv2 会静默返回空。"""
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0", path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    w, h = [int(x) for x in probe.stdout.strip().splitlines()[0].split(",")[:2]]
    # 注意参数顺序：``-frames:v`` 属于输出选项，必须放在 ``-i`` 之后、
    # 输出文件之前。早先版本用 ``cmd[:4] + [...] + cmd[4:]`` 把它插到了
    # ``-i`` 前面，ffmpeg 直接报错退出（输入被当成 "-frames:v"）。
    cmd = ["ffmpeg", "-v", "error"]
    if max_frames:
        cmd += ["-i", path, "-frames:v", str(max_frames)]
    else:
        cmd += ["-i", path]
    cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    buf = np.frombuffer(out, dtype=np.uint8)
    n = buf.size // (w * h * 3)
    if n == 0:
        raise RuntimeError("ffmpeg 没解出任何帧: %s" % path)
    return buf[: n * w * h * 3].reshape(n, h, w, 3)


def read_h5_signals(path):
    """读 h5 里的 rgb 序列与 sm_state / object_vel。"""
    import h5py

    frames, states, vels = {}, None, None
    with h5py.File(path, "r") as fp:
        for k in fp.keys():
            if k.endswith("_rgb"):
                frames[k[: -len("_rgb")]] = np.asarray(fp[k])
        if "sm_state" in fp:
            states = np.asarray(fp["sm_state"]).ravel()
        if "object_vel" in fp:
            vels = np.asarray(fp["object_vel"])
    if not frames:
        raise RuntimeError("h5 里没有 *_rgb 数据集: %s" % path)
    return frames, states, vels


def read_images(paths):
    """读一组 png/jpg，用最朴素的方式（PIL 或 imageio 都行）。"""
    out = []
    for p in paths:
        try:
            from PIL import Image

            out.append(np.asarray(Image.open(p).convert("RGB")))
        except ImportError:  # pragma: no cover
            import imageio.v3 as iio

            out.append(iio.imread(p)[..., :3])
    return out


# --------------------------------------------------------------- 指标
def mad(a, b):
    """平均绝对差，0-255 尺度。用 int16 避免 uint8 下溢。"""
    return float(
        np.abs(a.astype(np.int16) - b.astype(np.int16)).mean()
    )


def local_grain(frame, k=3):
    """k x k 局部标准差的中位数 —— 平坦区的颗粒感。

    雪花在平坦表面上表现为高频抖动，所以局部 std 会显著抬高；
    干净画面的桌面、墙面局部 std 接近 0。
    """
    g = frame.astype(np.float32).mean(axis=-1)  # 转灰度，避免色度插值干扰
    # 只取整数倍的窗口，规整成 (H-k+1, W-k+1, k, k)
    hh = (g.shape[0] // k) * k
    ww = (g.shape[1] // k) * k
    g = g[:hh, :ww]
    win = g.reshape(hh // k, k, ww // k, k).transpose(0, 2, 1, 3)
    std = win.reshape(hh // k, ww // k, k * k).std(axis=-1)
    return float(np.median(std))


def static_index_pairs(states, vels, min_run=5, vel_eps=0.02):
    """找出「状态不变且物体基本静止」的连续段，返回段内相邻帧下标对。

    这是本脚本最关键的判据：这种片段里机器人和物体都没动，
    相邻两帧的差异**只可能**来自渲染噪声或编码噪声。
    """
    if states is None:
        return []
    s = np.asarray(states).ravel()
    pairs, i = [], 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and s[j + 1] == s[i]:
            j += 1
        if j - i + 1 >= min_run:
            for t in range(i, j):
                if vels is not None and t + 1 < len(vels):
                    v = np.linalg.norm(np.asarray(vels[t + 1]) - np.asarray(vels[t]))
                    if v > vel_eps:
                        continue
                pairs.append((t, t + 1))
        i = j + 1
    return pairs


def grade(value, lo, hi):
    if value is None:
        return "n/a"
    if value <= lo:
        return "干净"
    if value <= hi:
        return "轻微"
    return "明显"


def analyse_camera(name, frames, states, vels, grain_samples=12):
    """对一路相机算全部指标。"""
    frames = np.asarray(frames)
    n = len(frames)
    res = {"camera": name, "n_frames": int(n), "shape": list(frames.shape[1:])}

    # --- 1) 静止段帧间闪烁
    pairs = static_index_pairs(states, vels)
    if pairs:
        vals = [mad(frames[a], frames[b]) for a, b in pairs]
        res["static_flicker"] = round(float(np.mean(vals)), 4)
        res["static_flicker_max"] = round(float(np.max(vals)), 4)
        res["static_pairs"] = len(pairs)
    else:
        res["static_flicker"] = None
        res["static_pairs"] = 0

    # --- 2) 单帧颗粒感（抽样，避免大文件跑太久）
    idx = (
        np.linspace(0, n - 1, min(grain_samples, n)).astype(int)
        if n > grain_samples
        else np.arange(n)
    )
    grains = [local_grain(frames[i]) for i in idx]
    res["local_grain"] = round(float(np.median(grains)), 4)
    res["local_grain_p90"] = round(float(np.percentile(grains, 90)), 4)

    # --- 3) 全体相邻帧差（仅参考）
    dl = [mad(frames[t], frames[t + 1]) for t in range(n - 1)]
    if dl:
        res["frame_delta"] = round(float(np.mean(dl)), 4)
        res["frame_delta_p10"] = round(float(np.percentile(dl, 10)), 4)

    res["verdict_grain"] = grade(res["local_grain"], 1.5, 3.5)
    res["verdict_flicker"] = grade(res["static_flicker"], 1.0, 3.0)
    return res


# --------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(
        description="定量测量渲染/编码噪声（「雪花」），用判据代替肉眼"
    )
    ap.add_argument("--h5", action="append", default=[], help="可多次指定")
    ap.add_argument("--mp4", action="append", default=[], help="可多次指定")
    ap.add_argument("--img", action="append", default=[], help="单帧图片，仅算颗粒")
    ap.add_argument(
        "--label",
        action="append",
        default=[],
        help="与输入一一对应的标签，不给就用文件名",
    )
    ap.add_argument("--max_frames", type=int, default=200, help="mp4 最多解多少帧")
    ap.add_argument("--json", default=None, help="把结果写到这个 json")
    args = ap.parse_args()

    inputs = [("h5", p) for p in args.h5] + [("mp4", p) for p in args.mp4]
    if args.img:
        inputs.append(("img", None))
    if not inputs:
        ap.error("至少要给一个 --h5 / --mp4 / --img")

    labels = list(args.label)
    while len(labels) < len(inputs):
        labels.append(os.path.basename(inputs[len(labels)][1] or "images"))

    results = []
    for (kind, path), label in zip(inputs, labels):
        print("=" * 72)
        print("[%s] %s" % (label, kind if kind == "img" else path))
        try:
            if kind == "h5":
                frames, states, vels = read_h5_signals(path)
                for cam, fr in frames.items():
                    r = analyse_camera(cam, fr, states, vels)
                    r["source"] = label
                    r["kind"] = "h5"
                    results.append(r)
            elif kind == "mp4":
                fr = read_mp4_frames(path, max_frames=args.max_frames)
                # mp4 没有 sm_state，退化为纯帧差；用低分位估计静止段
                r = analyse_camera("video", fr, None, None)
                dl = [
                    mad(fr[t], fr[t + 1]) for t in range(len(fr) - 1)
                ]
                if dl:
                    r["static_flicker"] = round(
                        float(np.percentile(dl, 10)), 4
                    )  # 低分位≈静止段
                    r["verdict_flicker"] = grade(r["static_flicker"], 1.0, 3.0)
                    r["note"] = "mp4 无 sm_state，用帧差 p10 粗略代表静止段"
                r["source"] = label
                r["kind"] = "mp4"
                results.append(r)
            else:
                imgs = read_images(args.img)
                r = {
                    "camera": "images",
                    "n_frames": len(imgs),
                    "shape": list(imgs[0].shape),
                    "local_grain": round(
                        float(np.median([local_grain(i) for i in imgs])), 4
                    ),
                    "source": label,
                    "kind": "img",
                }
                r["verdict_grain"] = grade(r["local_grain"], 1.5, 3.5)
                results.append(r)
        except Exception as exc:
            print("  !! 失败: %r" % (exc,))
            results.append({"source": label, "kind": kind, "error": repr(exc)})

    # ---- 汇总表
    print()
    print("=" * 72)
    print("汇总（0-255 尺度；越小越干净）")
    print("=" * 72)
    hdr = "%-22s %-12s %8s %8s %8s %8s" % (
        "来源/相机", "帧数", "颗粒", "闪烁", "帧差", "判定",
    )
    print(hdr)
    print("-" * 72)
    for r in results:
        if "error" in r:
            print("%-22s  ERROR %s" % (r["source"], r["error"]))
            continue
        verdict = "%s/%s" % (
            r.get("verdict_grain", "-"), r.get("verdict_flicker", "-")
        )
        print(
            "%-22s %-12s %8s %8s %8s %8s"
            % (
                ("%s/%s" % (r["source"], r["camera"]))[:22],
                r.get("n_frames", "-"),
                r.get("local_grain", "-"),
                r.get("static_flicker", "-"),
                r.get("frame_delta", "-"),
                verdict,
            )
        )
    print()
    print("判据: 颗粒 ≤1.5 干净 / ≤3.5 轻微 / >3.5 明显")
    print("      闪烁 ≤1.0 干净 / ≤3.0 轻微 / >3.0 明显")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fp:
            json.dump(results, fp, indent=2, ensure_ascii=False)
        print("\n已写入 %s" % args.json)

    return 0


if __name__ == "__main__":
    sys.exit(main())