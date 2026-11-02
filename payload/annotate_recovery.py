# -*- coding: utf-8 -*-
"""Carry the recovery annotation across the h5 -> LeRobot conversion.

``create_lerobot_dataset.py`` keeps only action / state / environment_state /
images, so the ``sm_state`` trace -- the only record of *where* the slip and the
retry happened -- is lost. This script mines it from the raw h5 files and writes
a sidecar index keyed by LeRobot episode_index.

It runs in two passes because the converter deletes the h5 files:

    # before conversion -- build the cache from the raw h5 files
    python annotate_recovery.py --raw RAW --cache CACHE

    # after conversion -- join with the LeRobot metadata
    python annotate_recovery.py --raw RAW --cache CACHE --lerobot LEROBOT

Output (<lerobot>/recovery_index.json):

    {"episodes": {"0": {"file": "...", "n_frames": 187, "slip_frame": 65,
                        "settle_frame": 70, "retry_frame": 81,
                        "grasp_frames": [43, 123], "has_slip": true, ...}},
     "summary": {"episodes_mapped": 12, "with_slip": 12, "with_retry": 11}}
"""

import argparse
import glob
import json
import os

import h5py
import numpy as np

SLIP, SETTLE, RETRY, GIVEUP, GRASP = 10, 11, 12, 13, 4


def state_runs(s):
    out, start = [], 0
    for i in range(1, len(s) + 1):
        if i == len(s) or s[i] != s[start]:
            out.append((int(s[start]), start, i))
            start = i
    return out


def scan_h5(path):
    try:
        with h5py.File(path, "r") as f:
            if "sm_state" not in f:
                return None
            s = np.asarray(f["sm_state"]).astype(int).ravel()
            act = np.asarray(f["action"]).astype(np.float32) if "action" in f else None
    except OSError:
        return None
    if s.size == 0:
        return None

    runs = state_runs(s)

    def first(state):
        for st, a, _ in runs:
            if st == state:
                return int(a)
        return -1

    # How many frames of the slip window had the gripper CLOSED. In "friction"
    # mode these are the frames where the object is escaping while the fingers
    # are still shut -- the supervision signal a slip/hold head needs.
    slip_frames = slip_closed = 0
    for st, a, b in runs:
        if st != SLIP:
            continue
        slip_frames += b - a
        if act is not None and act.shape[1] >= 7:
            grip = act[a:b, 6] if act.shape[1] == 7 else act[a:b, -1]
            slip_closed += int((grip < 0).sum())

    return {
        "n_frames": int(s.size),
        "slip_frame": first(SLIP),
        "settle_frame": first(SETTLE),
        "retry_frame": first(RETRY),
        "giveup_frame": first(GIVEUP),
        "grasp_frames": [int(a) for st, a, _ in runs if st == GRASP],
        "slip_window_frames": slip_frames,
        "slip_closed_frames": slip_closed,
        "has_slip": any(st == SLIP for st, _, _ in runs),
        "has_retry": any(st == RETRY for st, _, _ in runs),
        "gave_up": any(st == GIVEUP for st, _, _ in runs),
    }


def load_jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path) as fp:
        for line in fp:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True, help="directory with the .h5 episodes")
    ap.add_argument("--cache", required=True, help="persistent annotation cache json")
    ap.add_argument("--lerobot", default=None, help="LeRobot root; join if given")
    args = ap.parse_args()

    cache = {}
    if os.path.exists(args.cache):
        try:
            cache = json.load(open(args.cache))
        except ValueError:
            cache = {}

    n_new = 0
    h5s = sorted(glob.glob(os.path.join(args.raw, "*.h5")))
    for h5 in h5s:
        name = os.path.splitext(os.path.basename(h5))[0]
        if name in cache:
            continue
        info = scan_h5(h5)
        if info:
            cache[name] = info
            n_new += 1
    with open(args.cache, "w") as fp:
        json.dump(cache, fp, indent=2)
    print("  scan: %d h5 on disk, %d newly annotated, %d in cache" % (len(h5s), n_new, len(cache)))

    if not args.lerobot:
        return

    meta = os.path.join(args.lerobot, "meta")
    recs = load_jsonl(os.path.join(meta, "camera.jsonl")) or load_jsonl(
        os.path.join(meta, "episodes.jsonl")
    )

    episodes, missing = {}, 0
    for rec in recs:
        name = rec.get("filename")
        if not name:
            continue
        idx = str(rec.get("episode_index"))
        info = cache.get(name)
        if info is None:
            missing += 1
            continue
        episodes[idx] = {"file": name, **info}

    payload = {
        "episodes": episodes,
        "summary": {
            "episodes_mapped": len(episodes),
            "episodes_missing_annotation": missing,
            "with_slip": sum(1 for v in episodes.values() if v["has_slip"]),
            "with_retry": sum(1 for v in episodes.values() if v["has_retry"]),
        },
    }
    out_path = os.path.join(args.lerobot, "recovery_index.json")
    with open(out_path, "w") as fp:
        json.dump(payload, fp, indent=2)
    print(
        "  join: %d episodes mapped, %d with slip, %d with retry, %d missing"
        % (
            len(episodes),
            payload["summary"]["with_slip"],
            payload["summary"]["with_retry"],
            missing,
        )
    )


if __name__ == "__main__":
    main()
