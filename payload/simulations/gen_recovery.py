# -*- coding: utf-8 -*-
"""Generator for "grasp -> slip -> retry -> success" episodes.

Thin driver around the official ``simulate.simulate()``. It swaps the place
state machine for ``PlaceRecoveryStateMachine`` and raises the episode time
budget, but reuses *everything* else from the official pipeline (scene
generation, object sampling, camera capture, h5/json layout), so the output
stays compatible with the DOM dataset format.

Run with the Isaac Sim python, e.g.
    python simulations/gen_recovery.py --task place --robot franka -n 1 \
        --save --enable_cameras --headless --disable_fabric \
        -o /path/to/out
"""

import argparse
import json
import logging
import os
import random
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np
import torch
import yaml

from isaaclab.app import AppLauncher

logging.basicConfig(
    format="[%(levelname)s] %(asctime)s %(message)s", level=logging.INFO
)

parser = argparse.ArgumentParser(description="DynamicVLA recovery-data generator")
AppLauncher.add_app_launcher_args(parser)

# NOTE: parse_known_args() must happen while only the AppLauncher arguments are
# registered -- exactly like simulate.py does. Otherwise ``-n`` and friends get
# swallowed by isaaclab_args and silently fall back to their defaults below.
isaaclab_args, script_args = parser.parse_known_args()

parser.add_argument("--task", default="place")
parser.add_argument("--robot", default="franka")
parser.add_argument("--scene_dir", default=os.path.join(_ROOT, os.pardir, "scenes"))
parser.add_argument("--object_dir", default=os.path.join(_ROOT, os.pardir, "objects"))
parser.add_argument("-o", "--output_dir", default=os.path.join(_ROOT, "out_recovery"))
parser.add_argument(
    "-c",
    "--sim_cfg_file",
    default=os.path.join(_HERE, "configs", "sim_cfg.yaml"),
)
parser.add_argument("--debug", action="store_true", default=False)
parser.add_argument("--disable_fabric", action="store_true", default=False)
parser.add_argument("--disable_sm", action="store_true", default=False)
parser.add_argument("--path_tracing", action="store_true", default=False)
parser.add_argument("--seed", type=int, default=None)
parser.add_argument("-n", "--n_simulations", type=int, default=10)
parser.add_argument("--num_envs", type=int, default=1)
parser.add_argument("--save", action="store_true", default=False)
parser.add_argument(
    "--episode_length",
    type=float,
    default=30.0,
    help="episode time budget in seconds (official place uses 12)",
)
parser.add_argument(
    "--sm",
    choices=["recovery", "official"],
    default="recovery",
    help="which place state machine to run (official = A/B baseline)",
)
parser.add_argument(
    "--require_slip",
    action="store_true",
    default=False,
    help="only keep episodes that slipped and recovered",
)
parser.add_argument(
    "--keep_imperfect",
    action="store_true",
    default=False,
    help=(
        "keep episodes that were kept out by --require_slip, i.e. clumsy grasps "
        "that never slipped. Needed when REC_GRASP_JITTER_XY/YAW are on and you "
        "want the 'grabbed it slightly wrong but did not drop it' samples."
    ),
)
parser.add_argument(
    "--keep_moving_objects",
    action="store_true",
    default=False,
    help="disable the official 'object moved at init' filters",
)
SHARED_PARAMETERS = ["num_envs", "save"]

args = parser.parse_args(script_args)
for _sp in SHARED_PARAMETERS:
    if _sp in isaaclab_args:
        setattr(args, _sp, getattr(isaaclab_args, _sp))

app_launcher = AppLauncher(isaaclab_args)
args.enable_cameras = app_launcher._enable_cameras
os.makedirs(args.output_dir, exist_ok=True)

# Imported only after the app is up, exactly like simulate.py does.
import simulate as sim_mod  # noqa: E402
from state_machines import place_recovery_sm  # noqa: E402


def build_sim_cfg():
    with open(args.sim_cfg_file) as fp:
        sim_cfg = yaml.load(fp, Loader=yaml.FullLoader)

    sim_cfg.update(
        {
            "debug": args.debug,
            "device": args.device,
            "disable_fabric": args.disable_fabric,
            "disable_sm": args.disable_sm,
            "enable_cameras": args.enable_cameras,
            "num_envs": args.num_envs,
            "path_tracing": args.path_tracing,
        }
    )
    task_cfg = sim_cfg["tasks"][args.task]
    if args.sm == "official":
        task_cfg["sm"] = "state_machines.place_sm.PlaceStateMachine"
    else:
        task_cfg["sm"] = "state_machines.place_recovery_sm.PlaceRecoveryStateMachine"
    task_cfg["episode_length"] = args.episode_length
    return sim_cfg


def analyse(sm_state):
    """Summarise a recovery episode from its state trace."""
    s = np.asarray(sm_state).astype(int).ravel()
    S = place_recovery_sm.PlaceRecSmState

    def first(state):
        idx = np.flatnonzero(s == state)
        return int(idx[0]) if idx.size else -1

    n_grasp = int((s == S.GRASP_OBJECT).sum())
    return {
        "n_frames": int(s.size),
        "slip_frame": first(S.SLIP_OBJECT),
        "settle_frame": first(S.WAIT_SETTLE),
        "retry_frame": first(S.RECOVER_APPROACH),
        "giveup_frame": first(S.GIVE_UP),
        "n_grasp_frames": n_grasp,
        "n_lift_frames": int((s == S.LIFT_OBJECT).sum()),
        "has_slip": bool((s == S.SLIP_OBJECT).any()),
        "has_retry": bool((s == S.RECOVER_APPROACH).any()),
        "gave_up": bool((s == S.GIVE_UP).any()),
    }


def main():
    # simulate.py keeps a module-level `args` (it is normally run as __main__)
    # and get_object_metadata() reads args.object_dir from it.
    sim_mod.args = args

    # Hook the material setter: it is the one place simulate.py hands us the
    # rigid-object handle, which the friction slip needs. Called once per
    # episode right after the scene is built.
    _orig_set_material = sim_mod.set_object_material

    def _set_material_and_bind(target_object, n_envs=1):
        place_recovery_sm.bind_object(target_object)
        return _orig_set_material(target_object, n_envs=n_envs)

    sim_mod.set_object_material = _set_material_and_bind

    sim_cfg = build_sim_cfg()
    object_categories = sim_cfg["scene"]["objects"]["categories"]
    container_categories = sim_cfg["scene"]["containers"].get("categories", [])
    object_metadata = sim_mod.get_object_metadata(
        args.object_dir, object_categories + container_categories
    )

    seed = args.seed if args.seed is not None else random.randint(0, 65535)
    # completion sentinel for the shell wrappers (log files appear late and are
    # awkward to poll); removed here so a stale marker can never fool them
    done_marker = os.path.join(args.output_dir, "_DONE")
    if os.path.exists(done_marker):
        os.remove(done_marker)
    logging.info(
        "config: n=%d seed=%s ep_len=%.1fs slip=%s h=%.3f save=%s"
        % (
            args.n_simulations,
            args.seed,
            args.episode_length,
            place_recovery_sm.SLIP_ENABLED,
            place_recovery_sm.SLIP_LIFT_HEIGHT,
            args.save,
        )
    )
    n_done, n_saved, n_slip_saved = 0, 0, 0
    summary = []

    while n_done < args.n_simulations:
        logging.info("Running simulation with seed: %d" % seed)
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)

        place_recovery_sm.EVENTS.clear()
        if place_recovery_sm.DEBUG:
            open(place_recovery_sm.TRACE_PATH, "w").close()

        try:
            env_cfg, object_tags, env_states = sim_mod.simulate(
                sim_cfg,
                args.task,
                args.robot,
                args.scene_dir,
                object_metadata,
                seed,
            )
        except Exception as ex:
            logging.exception(ex)
            seed += 1
            n_done += 1
            continue

        if place_recovery_sm.EVENTS:
            evdir = os.path.join(args.output_dir, "events")
            os.makedirs(evdir, exist_ok=True)
            with open(os.path.join(evdir, "%06d.jsonl" % seed), "w") as fp:
                for rec in place_recovery_sm.EVENTS:
                    fp.write(json.dumps(rec) + "\n")

        if not env_states:
            logging.info(
                "seed %d: no placement within %.0f s, discarded"
                % (seed, args.episode_length)
            )

        env_cfg_dict = env_cfg.to_dict()
        for es in env_states:
            if not args.keep_moving_objects:
                if sim_mod.is_object_stopped(env_cfg_dict["scene"], es["object_vel"]):
                    logging.info("seed %d: skip, object stopped at init" % seed)
                    continue
                if sim_mod.is_object_direction_changed(
                    env_cfg_dict["scene"], es["object_vel"]
                ):
                    object_tags["objects"] = [
                        t for t in object_tags["objects"] if not t.endswith("direction")
                    ]

            info = analyse(es["sm_state"])
            info["seed"] = seed
            logging.info(
                "seed %d: placed in %d frames, slip=%s retry=%s giveup=%s"
                % (
                    seed,
                    info["n_frames"],
                    info["has_slip"],
                    info["has_retry"],
                    info["gave_up"],
                )
            )
            if (
                args.require_slip
                and not args.keep_imperfect
                and not (info["has_slip"] and info["has_retry"])
            ):
                logging.info("  -> discarded (needs slip + retry)")
                continue

            episode_name = sim_mod.get_episode_name(
                args.task, args.robot, seed, env_cfg_dict["scene"]
            )
            logging.info(
                "Saving %s: %d frames, slip@%d, retry@%d"
                % (
                    episode_name,
                    info["n_frames"],
                    info["slip_frame"],
                    info["retry_frame"],
                )
            )

            if args.save:
                env_cfg_dict["seed"] = seed
                env_cfg_dict["instruction"] = {"task": args.task, **object_tags}
                env_cfg_dict["recovery"] = info
                with open(
                    os.path.join(args.output_dir, "%s.json" % episode_name), "w"
                ) as fp:
                    json.dump(
                        sim_mod.get_object_without_numpy(env_cfg_dict), fp, indent=2
                    )

                import h5py

                with h5py.File(
                    os.path.join(args.output_dir, "%s.h5" % episode_name), "w"
                ) as fp:
                    for k, v in es.items():
                        fp.create_dataset(k, data=v, compression="gzip")

                if args.debug and args.enable_cameras:
                    sim_mod.dump_video(
                        sim_mod.get_frames(es),
                        os.path.join(args.output_dir, "%s.mp4" % episode_name),
                    )

            summary.append({**info, "episode": episode_name})
            n_saved += 1
            if info["has_slip"]:
                n_slip_saved += 1

        seed += 1
        n_done += 1
        logging.info(
            "progress: %d/%d attempts, %d saved (%d with slip)"
            % (n_done, args.n_simulations, n_saved, n_slip_saved)
        )

    with open(os.path.join(args.output_dir, "recovery_summary.json"), "w") as fp:
        json.dump(summary, fp, indent=2)
    logging.info("done: %d saved, %d with slip" % (n_saved, n_slip_saved))
    with open(done_marker, "w") as fp:
        json.dump(
            {
                "attempts": n_done,
                "saved": n_saved,
                "with_slip": n_slip_saved,
                "finished": time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
            fp,
        )
    app_launcher.app.close()


if __name__ == "__main__":
    main()
