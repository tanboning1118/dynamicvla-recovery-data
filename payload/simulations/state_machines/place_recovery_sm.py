# -*- coding: utf-8 -*-
"""Drop-and-retry place state machine for DynamicVLA data generation.

Faithful Python port of ``state_machines.place_sm.PlaceStateMachine`` (a warp
kernel) with three extra states that inject a *physical* slip and recover from
it:

    LIFT_OBJECT
        |  object lifted more than `slip_lift_height`
        v
    SLIP_OBJECT      gripper opens, arm freezes -> object falls under gravity
        |  after `slip_open_time`
        v
    WAIT_SETTLE      gripper stays open, wait until the object rests on the table
        |  |v_obj| < 0.05 and obj_z > 0.10 and wait >= `settle_min_time`
        v
    RECOVER_APPROACH  re-approach the object at its NEW pose
        v
    APPROACH_OBJECT -> GRASP_OBJECT -> LIFT_OBJECT (attempt 1, no second slip)
        v
    ... -> APPROACH_ABOVE_TARGET -> PLACE_OBJECT -> TO_TARGET  (success)

The 10 original states keep the original thresholds and wait times, so the
nominal portion of the trajectory is directly comparable with the official
generator.

Configuration is read from environment variables so that no official config
file has to be touched (the evaluation path stays bit-identical):

    REC_SLIP             1/0   enable slip injection          (default 1)
    REC_SLIP_LIFT_HEIGHT float lift height before release, m (default 0.06)
    REC_SLIP_STEP_MAX    int   hard cap on the pre-release climb (default 20)
    REC_SLIP_OPEN_TIME   float gripper-open duration, s       (default 0.20)
    REC_SETTLE_MIN_TIME  float minimum settle time, s         (default 0.40)
    REC_SETTLE_MAX_TIME  float give up if not settled, s      (default 3.00)
    REC_MAX_ATTEMPTS     int   max recovery attempts          (default 2)
    REC_GRASP_JITTER_XY  float max lateral grasp offset, m     (default 0 = off)
    REC_GRASP_JITTER_YAW float max grasp yaw offset, deg       (default 0 = off)
    REC_GRASP_JITTER_RESEED  1/0 redraw the offset on retries  (default 1)
    REC_GRASP_CHECK      1/0  verify the grasp in LIFT_OBJECT  (default 0)
    REC_GRASP_CHECK_DELAY int  frames after closing to judge   (default 10)
    REC_GRASP_MIN_LIFT   float object must rise this much, m   (default 0.005)
    REC_GRASP_MISS_DIST  float ee-obj distance when empty, m   (default 0.03)
    REC_MAX_MISSES       int   empty grasps before GIVE_UP      (default 2)
    REC_MISS_OFFSET      float first-grasp lateral offset, m    (default 0 = off)
    REC_DISTURB_AT       int   step index of the shove          (-1 = off)
    REC_DISTURB_VEL      float shove speed, m/s                 (default 0)
    REC_DISTURB_AXIS     x|y   shove direction in the table plane (default x)
    REC_DISTURB_RANGE    float fire only when the arm is this close, m (0.15)
    REC_DRIFT_TOL        float object drift that invalidates the plan, m (0 = off)
    REC_PLACE_OFFSET     float lateral offset of the drop point, m     (0 = off)
    REC_DEBUG           1/0   dump per-step trace to /tmp/rec_trace.jsonl

The two jitter knobs implement target (1) "learn from a less-than-ideal
trajectory": the very first grasp is offset from the pose the official state
machine computes, instead of only being interrupted after a perfect grasp.
Both default to 0, in which case the jitter code path is skipped completely and
the recorded trajectory stays bit-identical to the un-jittered generator.
"""

import json
import math
import os

import numpy as np
import torch

from simulations import helpers

from . import sm_utils

# ---------------------------------------------------------------- configuration
def _envf(name, default):
    return float(os.environ.get(name, default))


def _envi(name, default):
    return int(os.environ.get(name, default))


SLIP_ENABLED = _envi("REC_SLIP", 1) != 0
SLIP_LIFT_HEIGHT = _envf("REC_SLIP_LIFT_HEIGHT", 0.06)
SLIP_STEP_MAX = _envi("REC_SLIP_STEP_MAX", 20)
SLIP_OPEN_TIME = _envf("REC_SLIP_OPEN_TIME", 0.20)
# open     : gripper is commanded open, the object falls out (a deliberate drop)
# friction : gripper stays CLOSED, the contact friction is dropped so the object
#            slides out of the fingers -> genuine "closed but slipping" frames
SLIP_MODE = os.environ.get("REC_SLIP_MODE", "open").strip().lower()
SLIP_FRICTION = _envf("REC_SLIP_FRICTION", 0.02)
SLIP_MASS_SCALE = _envf("REC_SLIP_MASS_SCALE", 1.0)
SLIP_FRICTION_TIME = _envf("REC_SLIP_FRICTION_TIME", 1.20)
SLIP_SLIDE_DIST = _envf("REC_SLIP_SLIDE_DIST", 0.015)
SETTLE_MIN_TIME = _envf("REC_SETTLE_MIN_TIME", 0.40)
SETTLE_MAX_TIME = _envf("REC_SETTLE_MAX_TIME", 3.00)
MAX_ATTEMPTS = _envi("REC_MAX_ATTEMPTS", 2)
DEBUG = _envi("REC_DEBUG", 0) != 0

# ---- initial-grasp jitter -------------------------------------------------
# Radius of the lateral offset (metres, uniform over the disc) and the half
# range of the yaw offset (degrees, uniform in [-v, +v]). 0 disables the feature.
GRASP_JITTER_XY = _envf("REC_GRASP_JITTER_XY", 0.0)
GRASP_JITTER_YAW = _envf("REC_GRASP_JITTER_YAW", 0.0)
# Draw a fresh offset for every recovery attempt, so a single trajectory can
# contain two clumsy grasps in a row (closer to how a human actually fumbles).
GRASP_JITTER_RESEED = _envi("REC_GRASP_JITTER_RESEED", 1) != 0

# ---- grasp verification (the failure the official machine cannot see) ------
# The official LIFT_OBJECT only gives up when the object is more than
# object_dist_thres (10 cm) away. A "slightly too far" grasp therefore closes
# the fingers on air and the arm carries nothing to the container -- which is
# exactly what the evaluation videos show. These knobs add an explicit check:
# after closing, the object has to rise with the gripper, otherwise re-approach.
GRASP_CHECK = _envi("REC_GRASP_CHECK", 0) != 0
GRASP_CHECK_DELAY = _envi("REC_GRASP_CHECK_DELAY", 10)   # frames after closing
GRASP_MIN_LIFT = _envf("REC_GRASP_MIN_LIFT", 0.005)      # object must rise, m
GRASP_MISS_DIST = _envf("REC_GRASP_MISS_DIST", 0.03)     # ee->obj when empty, m
MAX_MISSES = _envi("REC_MAX_MISSES", 2)                  # misses before GIVE_UP
# Lateral offset applied to the FIRST grasp only, metres. 0 disables. This is
# what manufactures the "closed on air" accident; pair it with GRASP_CHECK so
# the machine notices and retries instead of placating an empty hand.
MISS_OFFSET = _envf("REC_MISS_OFFSET", 0.0)

# ---- external disturbance (evaluation group 3-3) --------------------------
# One-shot lateral velocity impulse on the object, in m/s, applied at
# n_steps >= DISTURB_AT (frame index; -1 disables).
DISTURB_AT = _envi("REC_DISTURB_AT", -1)
DISTURB_VEL = _envf("REC_DISTURB_VEL", 0.0)
DISTURB_AXIS = os.environ.get("REC_DISTURB_AXIS", "x").strip().lower()
# Only fires once the arm is this close (m) with the gripper still open.
DISTURB_RANGE = _envf("REC_DISTURB_RANGE", 0.15)

# ---- stale-plan gate (class C) --------------------------------------------
# The arm recomputes grasp_pos from the LIVE object pose every frame, so a shove
# alone never breaks anything: the arm simply follows the object and the episode
# looks clean. Measured 2026-11-02: 5/5 saved C episodes contained no RESET.
# This gate turns the shove into an actual failure: once the object has drifted
# more than DRIFT_TOL from where it stood when the arm committed to the approach,
# the plan is stale -> RESET and re-plan. 0 disables the gate.
DRIFT_TOL = _envf("REC_DRIFT_TOL", 0.0)

# ---- placement offset (class D) -------------------------------------------
# The official placement is accurate to the millimetre, so is_object_placed()
# is always true and the RESET branch never fires (measured 2026-11-02: 2/2
# saved D episodes had no RESET). Offsetting the drop point laterally by
# PLACE_OFFSET is what actually manufactures the missed placement. 0 disables.
PLACE_OFFSET = _envf("REC_PLACE_OFFSET", 0.0)

TRACE_PATH = "/tmp/rec_trace.jsonl"
# Rolling event log, read by the generator right after simulate() returns.
EVENTS = []


class PlaceRecSmState:
    """State ids. 0-9 are identical to the official state machine."""

    INIT = 0
    RESET = 1
    APPROACH_ABOVE_OBJECT = 2
    APPROACH_OBJECT = 3
    GRASP_OBJECT = 4
    LIFT_OBJECT = 5
    APPROACH_ABOVE_TARGET = 6
    APPROACH_TARGET = 7
    PLACE_OBJECT = 8
    TO_TARGET = 9
    # ---- added by this implementation ----
    SLIP_OBJECT = 10
    WAIT_SETTLE = 11
    RECOVER_APPROACH = 12
    GIVE_UP = 13


STATE_NAMES = {
    0: "INIT",
    1: "RESET",
    2: "APPROACH_ABOVE_OBJECT",
    3: "APPROACH_OBJECT",
    4: "GRASP_OBJECT",
    5: "LIFT_OBJECT",
    6: "APPROACH_ABOVE_TARGET",
    7: "APPROACH_TARGET",
    8: "PLACE_OBJECT",
    9: "TO_TARGET",
    10: "SLIP_OBJECT",
    11: "WAIT_SETTLE",
    12: "RECOVER_APPROACH",
    13: "GIVE_UP",
}


class PlaceSmWaitTime:
    """Wait times (s), identical to the official state machine."""

    INIT = 0.28
    RESET = 0.08
    APPROACH_ABOVE_OBJECT = 0.4
    APPROACH_OBJECT = 0.72
    GRASP_OBJECT = 0.72
    LIFT_OBJECT = 0.0
    APPROACH_ABOVE_TARGET = 0.4
    APPROACH_TARGET = 0.6
    PLACE_OBJECT = 0.4
    TO_TARGET = 0.6


GRIPPER_OPEN = 1.0
GRIPPER_CLOSE = -1.0

# The state machine only receives a plain curr_state dict, so the rigid object
# handle is handed over by the runner once the scene exists. Without it the
# friction mode simply degrades to the open mode.
OBJECT_HANDLE = None
DEFAULT_FRICTION = (0.9, 1.0)


def bind_object(handle):
    """Called by the generator so the friction slip can reach the rigid body."""
    global OBJECT_HANDLE, _MASS0
    OBJECT_HANDLE = handle
    _MASS0 = None  # a new episode spawns a new object with its own mass


def set_object_friction(mu_static, mu_dynamic):
    """Overwrite the object's contact friction (static, dynamic)."""
    if OBJECT_HANDLE is None:
        return False
    try:
        import torch

        view = OBJECT_HANDLE.root_physx_view
        mats = view.get_material_properties()
        mats[..., 0] = mu_static
        mats[..., 1] = mu_dynamic
        mats[..., 2] = 0.0
        view.set_material_properties(mats, torch.arange(mats.shape[0]))
        return True
    except Exception as exc:  # pragma: no cover - physics backend dependent
        if DEBUG:
            print("[recovery-sm] set friction failed: %r" % (exc,))
        return False


_MASS0 = None


def set_object_mass_scale(scale):
    """Make the object heavier so the fingers genuinely cannot hold it.

    Friction alone does not work: the Franka fingers are position driven with a
    very high normal force, so even mu=0.02 keeps the object clamped. Scaling
    the mass is a physically honest way to produce a real slip with the gripper
    closed. The original mass is cached and restored after the object lands.
    """
    global _MASS0
    if OBJECT_HANDLE is None:
        return False
    try:
        import torch

        view = OBJECT_HANDLE.root_physx_view
        if _MASS0 is None:
            _MASS0 = view.get_masses().clone()
        masses = _MASS0 * float(scale)
        view.set_masses(masses, torch.arange(masses.shape[0]))
        return True
    except Exception as exc:  # pragma: no cover - physics backend dependent
        if DEBUG:
            print("[recovery-sm] set mass failed: %r" % (exc,))
        return False


def push_object(vx=0.0, vy=0.0, vz=0.0):
    """Give the object a one-shot linear velocity kick (an external shove).

    Written straight to the rigid body so it does not depend on contact with a
    pusher prim: the evaluation suite's 3-3 "disturbance robustness" scenes shove
    the object while the arm is in flight, and this reproduces that stimulus.
    """
    if OBJECT_HANDLE is None:
        return False
    try:
        import torch

        vel = torch.tensor(
            [[float(vx), float(vy), float(vz), 0.0, 0.0, 0.0]],
            device=OBJECT_HANDLE.device,
            dtype=torch.float32,
        )
        OBJECT_HANDLE.write_root_velocity_to_sim(vel)
        return True
    except Exception as exc:  # pragma: no cover - physics backend dependent
        if DEBUG:
            print("[recovery-sm] push failed: %r" % (exc,))
        return False


def _yaw_quat_apply(yaw_rad, quat_xyzw):
    """Left-multiply ``quat_xyzw`` by a rotation of ``yaw_rad`` about world z.

    ``sm_utils`` hands the grasp orientation back as xyzw, so the Hamilton
    product below is written out in that order rather than calling into warp.
    """
    half = 0.5 * float(yaw_rad)
    s = quat_xyzw.new_tensor(math.sin(half))
    c = quat_xyzw.new_tensor(math.cos(half))
    x, y, z, w = quat_xyzw[0], quat_xyzw[1], quat_xyzw[2], quat_xyzw[3]
    return torch.stack((c * x - s * y, c * y + s * x, c * z + s * w, c * w - s * z))


class PlaceRecoveryStateMachine:
    """Place-with-recovery state machine (single environment only)."""

    def __init__(
        self,
        dt: float,
        num_envs: int,
        init_pose: torch.Tensor,
        final_pose: torch.Tensor,
        max_reach_dist: float,
        grasp_dist_thres: float = 0.01,
        place_dist_thres: float = 0.1,
        grasp_pose_thres: float = 15.0,
        object_dist_thres: float = 0.1,
        gripper_length: float = 0.3,
        device: torch.device | str = "cpu",
    ):
        assert num_envs == 1, "PlaceRecoveryStateMachine supports num_envs == 1 only."

        self.dt = float(dt)
        self.num_envs = num_envs
        self.device = device if isinstance(device, torch.device) else torch.device(device)

        # NOTE: init_pose in the config is (x, y, z, qw, qx, qy, qz); warp wants
        # xyzw, hence the permutation. final_pose is used as-is (matching the
        # official implementation, where [3:7] is fed to quat_multiply as xyzw).
        self.init_pose = init_pose[:, [0, 1, 2, 4, 5, 6, 3]].repeat(num_envs, 1)
        self.final_eef_pose = final_pose.repeat(num_envs, 1)
        self.init_pose = self.init_pose.to(self.device)
        self.final_eef_pose = self.final_eef_pose.to(self.device)

        self.max_reach_dist = float(max_reach_dist)
        self.grasp_dist_thres = float(grasp_dist_thres)
        self.place_dist_thres = float(place_dist_thres)
        self.grasp_pose_thres = float(grasp_pose_thres)
        self.object_dist_thres = float(object_dist_thres)
        self.gripper_length = float(gripper_length)

        self.offset_z = 0.1
        self.thres = torch.tensor(
            [
                self.grasp_dist_thres,
                self.grasp_dist_thres,
                self.offset_z + self.grasp_dist_thres,
            ],
            device=self.device,
        )
        self._z_off = torch.tensor([0.0, 0.0, self.offset_z], device=self.device)

        # runtime state
        self.state = PlaceRecSmState.INIT
        self.state_prev = PlaceRecSmState.INIT
        self.wait = 0.0
        self.is_placed = 0
        self.attempt = 0                  # completed recovery attempts
        self.slip_done = False            # slip already injected in this episode
        self.lift_z0 = None
        self.lift_steps = 0
        self.n_steps = 0

        self.des_pos = self.init_pose[0, :3].clone()
        self.des_quat = self.init_pose[0, 3:7].clone()  # xyzw
        self.grip = GRIPPER_OPEN
        self.use_cache_grasp_quat = False
        self._slip_pose = None
        self._slip_obj_z = None           # object height when the slip started
        self._friction_dropped = False

        self.grasp_position = torch.zeros((num_envs, 3), device=self.device)
        self.grasp_quat = torch.zeros((num_envs, 4), device=self.device)

        # initial-grasp jitter: sampled once per episode (and per retry), never
        # per frame -- re-drawing every step would make the arm chase a target
        # that jumps around and the gripper would visibly chatter.
        self._jitter_on = (
            GRASP_JITTER_XY > 0.0 or GRASP_JITTER_YAW > 0.0 or MISS_OFFSET > 0.0
        )
        self._jitter_rng = torch.Generator(device="cpu")
        self._jitter_pos = torch.zeros(3, device=self.device)
        self._jitter_yaw = 0.0
        self._jitter_seed = -1
        self._jitter_draws = 0
        self._jitter_is_miss = False
        self._miss_pending = MISS_OFFSET > 0.0
        self._grasp_misses = 0
        self._disturbed = False
        self._place_off = torch.zeros(3, device=self.device)
        self._drift_anchor = None
        self._resample_grasp_jitter()

    # ------------------------------------------------------------------ helpers
    def reset_idx(self, env_ids=None):
        self.state = PlaceRecSmState.INIT
        self.state_prev = PlaceRecSmState.INIT
        self.wait = 0.0
        self.is_placed = 0
        self.attempt = 0
        self.slip_done = False
        self.lift_z0 = None
        self.lift_steps = 0
        self.n_steps = 0
        self.des_pos = self.init_pose[0, :3].clone()
        self.des_quat = self.init_pose[0, 3:7].clone()
        self.grip = GRIPPER_OPEN
        self.use_cache_grasp_quat = False
        self._slip_pose = None
        self._slip_obj_z = None           # object height when the slip started
        self._friction_dropped = False
        self._miss_pending = MISS_OFFSET > 0.0
        self._grasp_misses = 0
        self._disturbed = False
        self._drift_anchor = None
        if PLACE_OFFSET > 0.0:
            # 用全局 RNG 而不是 _jitter_rng：D 类不触发 jitter，那个 generator
            # 不会被 _resample_grasp_jitter 重新播种，会让每条 episode 抽到同方向
            ang = float(torch.rand(1)) * 2.0 * math.pi
            self._place_off = torch.tensor(
                [PLACE_OFFSET * math.cos(ang), PLACE_OFFSET * math.sin(ang), 0.0],
                device=self.device,
            )
        else:
            self._place_off = torch.zeros(3, device=self.device)
        self._resample_grasp_jitter()

    # ------------------------------------------------------------- jitter
    def _resample_grasp_jitter(self):
        """Draw a new lateral/yaw offset for the next grasp attempt.

        No-op when both knobs are 0, so the default generator keeps hitting the
        exact same code path as before this feature existed.
        """
        if not self._jitter_on:
            return
        self._jitter_draws += 1
        # Seed from the global RNG so that --seed still makes a whole run
        # reproducible, while different episodes get different offsets.
        self._jitter_seed = int(
            torch.randint(0, 2 ** 31 - 1, (1,), dtype=torch.int64).item()
        )
        self._jitter_rng.manual_seed(self._jitter_seed)

        dx = dy = 0.0
        radius = GRASP_JITTER_XY
        is_miss = False
        if MISS_OFFSET > 0.0 and self._miss_pending:
            # first grasp of the episode is deliberately wide of the object, so
            # the fingers close on air -- the accident the model never saw
            radius = MISS_OFFSET
            is_miss = True
            self._miss_pending = False
        if radius > 0.0:
            ang = float(torch.rand(1, generator=self._jitter_rng)) * 2.0 * math.pi
            rad = math.sqrt(float(torch.rand(1, generator=self._jitter_rng)))
            dx = rad * radius * math.cos(ang)
            dy = rad * radius * math.sin(ang)

        yaw = 0.0
        if GRASP_JITTER_YAW > 0.0:
            yaw = float(torch.rand(1, generator=self._jitter_rng)) * 2.0 - 1.0
            yaw = math.radians(yaw * GRASP_JITTER_YAW)

        self._jitter_pos = torch.tensor([dx, dy, 0.0], device=self.device)
        self._jitter_yaw = yaw
        self._jitter_is_miss = is_miss
        if DEBUG:
            print(
                "[recovery-sm] grasp jitter #%d%s: dx=%.4f dy=%.4f yaw=%.2f deg"
                % (self._jitter_draws, " (INTENDED MISS)" if is_miss else "",
                   dx, dy, math.degrees(yaw))
            )

    def _goto(self, state):
        self.state_prev = self.state
        self.state = state
        self.wait = 0.0
        self._log_transition(state)

    def _set(self, pos, quat_xyzw, grip):
        self.des_pos = pos
        self.des_quat = quat_xyzw
        self.grip = grip

    def _log_transition(self, new_state):
        rec = {
            "step": self.n_steps,
            "from": STATE_NAMES.get(self.state_prev, "?"),
            "to": STATE_NAMES.get(new_state, "?"),
            "attempt": self.attempt,
        }
        rec.update(getattr(self, "_dbg", {}))
        EVENTS.append(rec)
        if DEBUG:
            with open(TRACE_PATH, "a") as fp:
                fp.write(json.dumps(rec) + "\n")

    @staticmethod
    def _offset_below_threshold(offset, thres, vel):
        """Torch port of ``sm_utils.is_offset_below_threshold`` (warp func)."""
        static = torch.norm(vel) < 0.05
        if static:
            return bool((offset[0] <= thres[0]) and (offset[1] <= thres[1]))
        return bool(offset[2] <= thres[2])

    # ------------------------------------------------------------------ compute
    def compute(self, curr_state: dict) -> dict:
        ee = curr_state["end_effector"]
        obj = curr_state["object"]
        ctn = curr_state["container"]

        ee_pos = ee["pos"][0]
        ee_quat = ee["quat"][0]                     # wxyz
        obj_pos = obj["pos"][0]
        obj_vel = obj["velocity"][0]
        obj_size = obj["size"]                      # (1, 3, 3)
        ctn_pos = ctn["pos"][0]

        self.grasp_position = sm_utils.get_grasp_position(
            obj_size, obj["pos"], obj["velocity"], self.gripper_length
        )
        self.grasp_quat = sm_utils.get_grasp_quat(
            obj_size, obj["velocity"], self.final_eef_pose, self.device
        )
        grasp_pos = self.grasp_position[0]
        grasp_quat = self.grasp_quat[0]             # xyzw

        if self._jitter_on:
            # The offset is applied to the *target* handed to the controller, so
            # every downstream gate (offset threshold, pose angle, above_grasp)
            # stays self-consistent: the arm genuinely tries to grasp off-centre
            # instead of being asked to reach a point it cannot verify.
            grasp_pos = grasp_pos + self._jitter_pos.to(
                device=grasp_pos.device, dtype=grasp_pos.dtype
            )
            if self._jitter_yaw != 0.0:
                grasp_quat = _yaw_quat_apply(self._jitter_yaw, grasp_quat)

        pose_angle = float(
            sm_utils.get_pose_angle(
                grasp_quat[[3, 0, 1, 2]].unsqueeze(0), ee["quat"]
            )[0]
        )
        place_pos = ctn_pos + torch.tensor(
            [0.0, 0.0, 0.10], device=self.device, dtype=ctn_pos.dtype
        )
        if PLACE_OFFSET > 0.0:
            # D 类：把落点横向挪开，让 is_object_placed() 判假
            place_pos = place_pos + self._place_off.to(
                device=place_pos.device, dtype=place_pos.dtype
            )
        place_quat = self.final_eef_pose[0, 3:7]

        above_grasp = grasp_pos + self._z_off
        above_obj = obj_pos + self._z_off
        above_place = place_pos + self._z_off

        dist_grasp = float(torch.norm(grasp_pos))
        dist_ee_obj = float(torch.norm(ee_pos - obj_pos))
        obj_z = float(obj_pos[2])
        obj_speed = float(torch.norm(obj_vel))
        # C 类：物体相对「接管时刻」的位置漂移。超过 DRIFT_TOL 说明计划过期。
        plan_stale = (
            DRIFT_TOL > 0.0
            and self._drift_anchor is not None
            and float(torch.norm(obj_pos - self._drift_anchor)) > DRIFT_TOL
        )

        # One-shot external shove (evaluation group 3-3). Fired the first time
        # the arm is within DISTURB_RANGE of the object with the gripper still
        # open -- i.e. mid-approach, the moment 3-3 shoves the object away.
        # Shoving while the object is already clamped is wasted: the fingers
        # just carry it along, which is what the first version of this hook did.
        if (
            DISTURB_AT >= 0
            and not self._disturbed
            and self.n_steps >= DISTURB_AT
            and self.grip == GRIPPER_OPEN
            and dist_ee_obj < DISTURB_RANGE
        ):
            self._disturbed = True
            vx = DISTURB_VEL if DISTURB_AXIS != "y" else 0.0
            vy = DISTURB_VEL if DISTURB_AXIS == "y" else 0.0
            ok = push_object(vx, vy)
            # 记下推之前的站位：此后物体漂移多少，就是「计划失效」多少
            self._drift_anchor = obj_pos.clone()
            if DEBUG:
                print(
                    "[recovery-sm] disturbance at step %d: %.2f m/s along %s,"
                    " ee-obj %.3f m (ok=%s)"
                    % (self.n_steps, DISTURB_VEL, DISTURB_AXIS, dist_ee_obj, ok)
                )

        # snapshot of the decision variables, attached to every transition
        self._dbg = {
            "dist_grasp": round(dist_grasp, 4),
            "wait": round(self.wait, 4),
            "v": round(obj_speed, 4),
            "obj_z": round(obj_z, 4),
            "lift": (
                round(obj_z - self.lift_z0, 4) if self.lift_z0 is not None else None
            ),
            "jxy": (
                round(float(self._jitter_pos[0]), 4),
                round(float(self._jitter_pos[1]), 4),
            ),
            "jyaw": round(math.degrees(self._jitter_yaw), 2),
            "grasp_off": round(float(torch.norm(grasp_pos - obj_pos)), 4),
            "miss": self._grasp_misses,
            "dist": self._disturbed,
        }

        st = self.state
        next_state = st

        # ---------------------------------------------------------------- INIT
        if st == PlaceRecSmState.INIT:
            self._set(self.init_pose[0, :3], self.init_pose[0, 3:7], GRIPPER_OPEN)
            if self.wait >= PlaceSmWaitTime.INIT and dist_grasp < self.max_reach_dist:
                next_state = PlaceRecSmState.APPROACH_ABOVE_OBJECT

        # --------------------------------------------------------------- RESET
        elif st == PlaceRecSmState.RESET:
            if dist_grasp > self.max_reach_dist:
                self._set(
                    self.final_eef_pose[0, :3],
                    self.final_eef_pose[0, 3:7],
                    GRIPPER_OPEN,
                )
            else:
                self._set(above_grasp, grasp_quat, GRIPPER_OPEN)
                if self.wait >= PlaceSmWaitTime.RESET:
                    next_state = PlaceRecSmState.APPROACH_ABOVE_OBJECT

        # ------------------------------------------- APPROACH_ABOVE_OBJECT
        elif st == PlaceRecSmState.APPROACH_ABOVE_OBJECT:
            self._set(above_grasp, grasp_quat, GRIPPER_OPEN)
            if dist_grasp >= self.max_reach_dist:
                next_state = PlaceRecSmState.RESET
            elif plan_stale:
                # 物体已经被推离接管时的站位：当前这条接近轨迹是过期计划
                self._drift_anchor = None
                next_state = PlaceRecSmState.RESET
            elif (
                self._offset_below_threshold(
                    torch.abs(ee_pos - grasp_pos), self.thres, obj_vel
                )
                and pose_angle < self.grasp_pose_thres
            ):
                if self.wait >= PlaceSmWaitTime.APPROACH_ABOVE_OBJECT:
                    next_state = PlaceRecSmState.APPROACH_OBJECT

        # ------------------------------------------------ APPROACH_OBJECT
        elif st == PlaceRecSmState.APPROACH_OBJECT:
            self.grip = GRIPPER_OPEN
            if plan_stale:
                self._drift_anchor = None
                self.use_cache_grasp_quat = False
                next_state = PlaceRecSmState.RESET
            else:
                if not self.use_cache_grasp_quat:
                    self._set(grasp_pos, grasp_quat, GRIPPER_OPEN)
                    self.use_cache_grasp_quat = True
                else:
                    self._set(grasp_pos, self.des_quat, GRIPPER_OPEN)
                if self.wait >= PlaceSmWaitTime.APPROACH_OBJECT:
                    next_state = PlaceRecSmState.GRASP_OBJECT
                    self.use_cache_grasp_quat = False

        # --------------------------------------------------- GRASP_OBJECT
        elif st == PlaceRecSmState.GRASP_OBJECT:
            self.grip = GRIPPER_CLOSE
            if self.wait >= PlaceSmWaitTime.GRASP_OBJECT:
                next_state = PlaceRecSmState.LIFT_OBJECT
                self.lift_steps = 0
                self.lift_z0 = obj_z

        # ---------------------------------------------------- LIFT_OBJECT
        elif st == PlaceRecSmState.LIFT_OBJECT:
            self.grip = GRIPPER_CLOSE
            self.lift_steps += 1
            if self.lift_z0 is None:
                self.lift_z0 = obj_z
            lift_progress = obj_z - self.lift_z0

            # Did we actually get hold of it? The official branch below only
            # bails out when the object is > object_dist_thres (10 cm) away, so
            # a grasp that closed *beside* the object passes unnoticed and the
            # arm carries nothing to the container -- the dominant failure in
            # the evaluation videos. Judge by physics instead: a grasped object
            # has to rise with the gripper.
            lost_grip = (
                GRASP_CHECK
                and self.lift_steps >= GRASP_CHECK_DELAY
                and lift_progress < GRASP_MIN_LIFT
                and dist_ee_obj > GRASP_MISS_DIST
            )

            slip_armed = (
                SLIP_ENABLED and self.attempt == 0 and not self.slip_done
            )

            if lost_grip:
                self._grasp_misses += 1
                if DEBUG:
                    print(
                        "[recovery-sm] empty grasp at step %d: object rose %.4f m,"
                        " ee-obj %.3f m -> re-approach (%d/%d)"
                        % (
                            self.n_steps,
                            lift_progress,
                            dist_ee_obj,
                            self._grasp_misses,
                            MAX_MISSES,
                        )
                    )
                self.lift_steps = 0
                self.lift_z0 = None
                if self._grasp_misses > MAX_MISSES:
                    next_state = PlaceRecSmState.GIVE_UP
                else:
                    if GRASP_JITTER_RESEED:
                        self._resample_grasp_jitter()
                    next_state = PlaceRecSmState.RECOVER_APPROACH
            elif slip_armed:
                if (
                    lift_progress >= SLIP_LIFT_HEIGHT
                    or self.lift_steps >= SLIP_STEP_MAX
                ):
                    # release from here; remember the pose we let go at
                    self.slip_done = True
                    self._slip_pose = (self.des_pos.clone(), self.des_quat.clone())
                    next_state = PlaceRecSmState.SLIP_OBJECT
                else:
                    # Keep climbing: aim higher than the nominal hover so the
                    # object keeps rising until it clears SLIP_LIFT_HEIGHT.
                    self._set(
                        obj_pos + self._z_off * 1.6, grasp_quat, GRIPPER_CLOSE
                    )
            else:
                self._set(above_obj, grasp_quat, GRIPPER_CLOSE)
                dist_ee_target = (
                    float(place_pos[2]) + self.offset_z / 2.0 - float(ee_pos[2])
                )
                if dist_ee_obj > self.object_dist_thres:
                    # official behaviour: the object got away -> restart the try
                    next_state = PlaceRecSmState.RESET
                elif dist_ee_target < self.grasp_dist_thres:
                    next_state = PlaceRecSmState.APPROACH_ABOVE_TARGET

        # --------------------------------------------------- SLIP_OBJECT
        elif st == PlaceRecSmState.SLIP_OBJECT:
            if SLIP_MODE == "friction":
                # Gripper stays CLOSED the whole time. We only make the contact
                # slippery, so the object slides out of the fingers under its own
                # weight -- the recording then contains genuine "closed but not
                # holding" frames, which the open mode can never produce.
                if not self._friction_dropped:
                    self._friction_dropped = True
                    self._slip_obj_z = obj_z
                    set_object_friction(SLIP_FRICTION, SLIP_FRICTION * 1.1)
                    if SLIP_MASS_SCALE != 1.0:
                        set_object_mass_scale(SLIP_MASS_SCALE)
                    if DEBUG:
                        print(
                            "[recovery-sm] slip at z=%.3f mu=%.3f mass x%.1f"
                            % (obj_z, SLIP_FRICTION, SLIP_MASS_SCALE)
                        )
                self._set(self._slip_pose[0], self._slip_pose[1], GRIPPER_CLOSE)
                slid = (
                    self._slip_obj_z is not None
                    and self._slip_obj_z - obj_z >= SLIP_SLIDE_DIST
                )
                if slid or self.wait >= SLIP_FRICTION_TIME:
                    next_state = PlaceRecSmState.WAIT_SETTLE
            else:
                # arm holds still, gripper opens -> object falls with real dynamics
                self._set(self._slip_pose[0], self._slip_pose[1], GRIPPER_OPEN)
                if self.wait >= SLIP_OPEN_TIME:
                    next_state = PlaceRecSmState.WAIT_SETTLE

        # ---------------------------------------------------- WAIT_SETTLE
        elif st == PlaceRecSmState.WAIT_SETTLE:
            # Phase A: keep the arm where it is so the object can fall freely.
            # Phase B: hover above wherever the object ended up.
            grip = GRIPPER_CLOSE if SLIP_MODE == "friction" else GRIPPER_OPEN
            if self.wait < SLIP_OPEN_TIME:
                self._set(self._slip_pose[0], self._slip_pose[1], grip)
            else:
                # Restore the material only once the object has come to rest on
                # the table: restoring mid-flight can snag it back between the
                # fingers, and a resting object cannot slide anyway.
                if self._friction_dropped and obj_speed < 0.05:
                    set_object_friction(*DEFAULT_FRICTION)
                    if SLIP_MASS_SCALE != 1.0:
                        set_object_mass_scale(1.0)
                    self._friction_dropped = False
                self._set(above_obj, self.des_quat, grip)

            if dist_grasp >= self.max_reach_dist:
                next_state = PlaceRecSmState.GIVE_UP
            elif self.wait >= SETTLE_MAX_TIME:
                next_state = PlaceRecSmState.GIVE_UP
            elif self.wait >= SETTLE_MIN_TIME and obj_speed < 0.05:
                self.attempt += 1
                if GRASP_JITTER_RESEED:
                    # the fumbled retry gets its own fresh offset, so one
                    # trajectory can contain two consecutive bad grasps
                    self._resample_grasp_jitter()
                next_state = PlaceRecSmState.RECOVER_APPROACH

            if next_state != st and self._friction_dropped:
                # Leaving this state: the retry must run with the stock material,
                # otherwise the very same heavy object simply slips again.
                set_object_friction(*DEFAULT_FRICTION)
                if SLIP_MASS_SCALE != 1.0:
                    set_object_mass_scale(1.0)
                self._friction_dropped = False

        # ----------------------------------------------- RECOVER_APPROACH
        elif st == PlaceRecSmState.RECOVER_APPROACH:
            self._set(above_grasp, grasp_quat, GRIPPER_OPEN)
            if dist_grasp >= self.max_reach_dist:
                next_state = PlaceRecSmState.GIVE_UP
            elif (
                self._offset_below_threshold(
                    torch.abs(ee_pos - grasp_pos), self.thres, obj_vel
                )
                and pose_angle < self.grasp_pose_thres
            ):
                if self.wait >= PlaceSmWaitTime.APPROACH_ABOVE_OBJECT:
                    next_state = PlaceRecSmState.APPROACH_OBJECT

        # --------------------------------------------- APPROACH_ABOVE_TARGET
        elif st == PlaceRecSmState.APPROACH_ABOVE_TARGET:
            self.grip = GRIPPER_CLOSE
            self._set(above_place, place_quat, GRIPPER_CLOSE)
            if dist_ee_obj > self.object_dist_thres:
                next_state = PlaceRecSmState.RESET
            elif self._offset_below_threshold(
                torch.abs(ee_pos - place_pos), self.thres, obj_vel
            ):
                if self.wait >= PlaceSmWaitTime.APPROACH_ABOVE_TARGET:
                    next_state = PlaceRecSmState.PLACE_OBJECT

        # -------------------------------------------------- APPROACH_TARGET
        elif st == PlaceRecSmState.APPROACH_TARGET:
            self._set(place_pos, place_quat, GRIPPER_CLOSE)
            if self.wait >= PlaceSmWaitTime.APPROACH_TARGET:
                next_state = PlaceRecSmState.PLACE_OBJECT

        # ---------------------------------------------------- PLACE_OBJECT
        elif st == PlaceRecSmState.PLACE_OBJECT:
            self._set(place_pos, place_quat, GRIPPER_OPEN)
            if self.wait >= PlaceSmWaitTime.PLACE_OBJECT:
                next_state = PlaceRecSmState.TO_TARGET
                self.is_placed = 0

        # ------------------------------------------------------- TO_TARGET
        elif st == PlaceRecSmState.TO_TARGET:
            self._set(
                self.final_eef_pose[0, :3],
                self.final_eef_pose[0, 3:7],
                GRIPPER_OPEN,
            )
            placed = bool(
                helpers.is_object_placed(
                    obj["pos"], obj_size, ctn["pos"], ctn["size"]
                )[0]
            )
            if placed:
                self.is_placed = 0
            else:
                self.is_placed += 1
            if self.is_placed >= 5:
                if self.attempt >= MAX_ATTEMPTS:
                    next_state = PlaceRecSmState.GIVE_UP
                else:
                    next_state = PlaceRecSmState.RESET

        # -------------------------------------------------------- GIVE_UP
        elif st == PlaceRecSmState.GIVE_UP:
            if self._friction_dropped:
                set_object_friction(*DEFAULT_FRICTION)
                if SLIP_MASS_SCALE != 1.0:
                    set_object_mass_scale(1.0)
                self._friction_dropped = False
            self._set(
                self.final_eef_pose[0, :3],
                self.final_eef_pose[0, 3:7],
                GRIPPER_OPEN,
            )

        # ------------------------------------------------------- transition
        if next_state != st:
            self._goto(next_state)

        self.wait += self.dt
        self.n_steps += 1

        if DEBUG:
            with open(TRACE_PATH, "a") as fp:
                fp.write(
                    json.dumps(
                        {
                            "step": self.n_steps,
                            "state": STATE_NAMES.get(self.state, "?"),
                            "ee_z": round(float(ee_pos[2]), 4),
                            "obj_z": round(obj_z, 4),
                            "obj_speed": round(obj_speed, 4),
                            "dist_ee_obj": round(dist_ee_obj, 4),
                            "attempt": self.attempt,
                        }
                    )
                    + "\n"
                )

        action = torch.cat(
            [
                self.des_pos.to(self.device),
                self.des_quat.to(self.device)[[3, 0, 1, 2]],  # xyzw -> wxyz
                torch.tensor([self.grip], device=self.device, dtype=torch.float32),
            ]
        ).unsqueeze(0)

        return {
            "action": action,
            "sm_state": torch.tensor(
                [self.state], dtype=torch.int32, device=self.device
            ),
            "grasp_position": self.grasp_position,
            "grasp_quat": self.grasp_quat,
        }
