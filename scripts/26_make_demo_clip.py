# -*- coding: utf-8 -*-
"""从任意一条恢复 episode 生成 README 演示视频（三路相机 + 状态机字幕）。

与 v1 的差别：v1 只认「滑脱 -> 静止 -> 重抓」这一条时间线，本版从 sm_state
推断恢复类型，同一份代码可出 A/B/C/D/E 五类视频。

用法:
    python scripts/26_make_demo_clip.py --h5 <episode.h5> --slug a_slip
    python scripts/26_make_demo_clip.py --h5 <episode.h5> --slug d_place --start 60 --end 262

输出:
    assets/demo/recovery_<slug>.mp4   (H.264, 15 fps)
    assets/demo/recovery_<slug>.jpg   (异常区间首帧作缩略图)

依赖: cv2 / h5py / numpy / ffmpeg（路径用环境变量 FFMPEG 覆盖）
注意: cv2.putText 只能渲染 ASCII，字幕里不要放中文和 · 之类的字符。
"""
import argparse
import json
import os
import shutil
import subprocess
import tempfile

import cv2
import h5py
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
OUT_DIR = os.environ.get("DEMO_OUT", os.path.join(REPO, "assets", "demo"))
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")

STATE = {
    0: "INIT", 1: "RESET", 2: "APPROACH_ABOVE_OBJECT", 3: "APPROACH_OBJECT",
    4: "GRASP_OBJECT", 5: "LIFT_OBJECT", 6: "APPROACH_ABOVE_TARGET",
    7: "APPROACH_TARGET", 8: "PLACE_OBJECT", 9: "TO_TARGET",
    10: "SLIP_OBJECT", 11: "WAIT_SETTLE", 12: "RECOVER_APPROACH", 13: "GIVE_UP",
}

# 每类恢复的配色与文案；key 由 detect_class() 推断
# 注意：cv2.putText 用的是 Hershey 字体，只能渲染 ASCII，别放 · 之类的字符
LOOK = {
    "a_slip": dict(bgr=(60, 60, 235), tag="SLIP", head="A | slip out of the fingers, then re-grasp"),
    "b_miss": dict(bgr=(40, 190, 240), tag="MISS", head="B | closed on air, then re-grasp"),
    "c_disturb": dict(bgr=(200, 120, 60), tag="PUSHED", head="C | object pushed away, then re-approach"),
    "d_place": dict(bgr=(180, 90, 200), tag="MISSED PLACE", head="D | object missed the container, then reset"),
    "e_control": dict(bgr=(120, 180, 90), tag="CLEAN", head="E | clean run (negative control)"),
}


def runs(mask):
    """把布尔序列压成 [(start, end_exclusive), ...]。"""
    out, start = [], None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, len(mask)))
    return out


def detect_class(sm):
    """从状态序列推断恢复类型；返回 (slug, 异常区间列表, 说明)。

    C 与 D 都只表现为「含状态 1」，靠 RESET 的触发前状态区分（这是本项目
    最容易判错的地方，曾把 D 类视频写成 C 的字幕）：
      - RESET 前在 PLACE_OBJECT(8)/TO_TARGET(9) → 放偏（D）
      - RESET 前在 APPROACH 阶段（≤3）        → 漂移导致计划过期（C）
      - 只在前两者之间（4/5/6/7 抓取与搬运）  → 抓取失败，不是恢复类，标 invalid
    """
    slip = np.isin(sm, [10, 11])
    recover = sm == 12

    if slip.any():
        spans = runs(slip | recover)
        return "a_slip", spans, "object left the gripper mid-carry"
    if recover.any():
        spans = runs(recover)
        return "b_miss", spans, "gripper closed on air, nothing was lifted"

    resets = [i for i in range(1, len(sm)) if sm[i] == 1 and sm[i - 1] != 1]
    if resets:
        prevs = [int(sm[i - 1]) for i in resets]
        spans = runs(sm == 1)
        if any(p >= 8 for p in prevs):
            return "d_place", spans, "placed outside the container, retry from scratch"
        if any(p <= 3 for p in prevs):
            return "c_disturb", spans, "object moved before the grasp"
        return "invalid", spans, "reset from grasp/transport failure, not a recovery class"
    return "e_control", [], "no incident, plain pick-and-place"


def put(img, text, org, scale=0.52, color=(255, 255, 255), thick=1):
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thick + 2, cv2.LINE_AA)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, thick, cv2.LINE_AA)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--h5", required=True)
    ap.add_argument("--slug", default=None, help="输出文件名后缀，缺省自动推断")
    ap.add_argument("--title", default=None, help="顶栏标题，缺省用 LOOK 表")
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--start", type=int, default=0, help="起始帧（裁剪长 episode）")
    ap.add_argument("--end", type=int, default=-1, help="结束帧，不含（-1 = 到末尾）")
    ap.add_argument("--no-thumb", action="store_true")
    args = ap.parse_args()

    meta = json.load(open(args.h5[:-3] + ".json"))
    rec = meta.get("recovery", {})

    f = h5py.File(args.h5, "r")
    N = f["action"].shape[0]
    sm = f["sm_state"][:].astype(int)
    ee, op = f["ee_pos"][:], f["object_pos"][:]
    dist = np.linalg.norm(ee - op, axis=1)

    slug, spans, note = detect_class(sm)
    if slug == "invalid" and not args.slug:
        raise SystemExit(
            "这条 episode 的 RESET 只来自抓取/搬运失败，不属于 A-E 任何一类。\n"
            "硬出片会在 README 里放一条语义错误的演示，故拒绝。\n"
            "确认要用请显式指定 --slug。")
    slug = args.slug or slug
    look = LOOK.get(slug, LOOK["e_control"])
    head = args.title or look["head"]

    cams = ["opst_cam_rgb", "wrist_cam_rgb", "side_cam_rgb"]
    labels = ["opst", "wrist", "side"]
    H, W = 360, 480 * 3
    BAR, FOOT = 34, 58

    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = os.path.join(tempfile.gettempdir(), "_demo_frames_" + slug)
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)

    def in_span(t):
        for a, b in spans:
            if a <= t < b:
                return True
        return False

    thumb = None
    thumb_fallback = None
    t0 = max(0, args.start)
    t1 = N if args.end < 0 else min(N, args.end)
    for i, t in enumerate(range(t0, t1)):
        row = np.concatenate([f[c][t] for c in cams], axis=1)
        canvas = np.zeros((BAR + H + FOOT, W, 3), np.uint8)
        canvas[BAR:BAR + H] = row

        put(canvas, f"f{t:03d}  {t / 25.0:5.2f}s  {STATE.get(sm[t], '?')}   |ee-obj|={dist[t] * 100:5.1f}cm",
            (10, 23), 0.56, (235, 235, 235))
        for k, lb in enumerate(labels):
            put(canvas, lb, (k * 480 + 10, BAR + 22), 0.48, (205, 205, 205))

        put(canvas, head, (10, BAR + H + 24), 0.58, (240, 240, 240))
        put(canvas, note, (10, BAR + H + 48), 0.52, (175, 175, 175))

        if in_span(t):
            cv2.rectangle(canvas, (0, BAR), (W - 1, BAR + H - 1), look["bgr"], 4)
            put(canvas, look["tag"], (W - 210, BAR + H - 18), 0.8, look["bgr"], 2)
            if thumb is None:
                thumb = canvas.copy()

        # 无异常区间（E 类）时，用 1/3 处的画面当缩略图
        if thumb_fallback is None and i >= (t1 - t0) // 3:
            thumb_fallback = canvas.copy()

        # 不用 cv2.imwrite：Windows 下它对非 ASCII 路径会静默失败
        ok, buf = cv2.imencode(".png", canvas)
        with open(os.path.join(tmp, f"{i:04d}.png"), "wb") as fp:
            fp.write(buf.tobytes())

    f.close()

    mp4 = os.path.join(OUT_DIR, f"recovery_{slug}.mp4")
    subprocess.run([
        FFMPEG, "-y", "-loglevel", "error", "-framerate", str(args.fps),
        "-start_number", "0", "-i", os.path.join(tmp, "%04d.png"),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23",
        "-movflags", "+faststart", mp4,
    ], check=True)

    jpg = os.path.join(OUT_DIR, f"recovery_{slug}.jpg")
    if thumb is None:
        thumb = thumb_fallback
    if thumb is not None and not args.no_thumb:
        ok, buf = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 88])
        with open(jpg, "wb") as fp:
            fp.write(buf.tobytes())
    else:
        jpg = None

    shutil.rmtree(tmp)
    print(f"[{slug}] frames={t1 - t0} (episode {N}, {t0}-{t1}) spans={spans[:3]} class={slug}")
    print(f"  video={mp4}  {os.path.getsize(mp4) / 1e6:.2f} MB")
    if jpg:
        print(f"  thumb={jpg}")


if __name__ == "__main__":
    main()
