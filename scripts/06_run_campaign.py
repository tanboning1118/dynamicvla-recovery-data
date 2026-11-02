#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""06 · 生产调度器：多 profile × 多 chunk，维持 N 个并发进程。

设计要点
--------
1. **一个 chunk 一个进程**。Isaac Sim 每 episode 泄漏 ~200 MB RSS，进程内循环兜不住；
   退出即回收。CHUNK 由 04_benchmark 的组三实测给出。

2. **错峰启动**。实测两个 Isaac Sim 实例同时启动会有一个卡死在 kit 初始化阶段
   （只申请到 821 MiB 显存，永远到不了渲染器）。所以每个新进程之间 sleep STAGGER 秒。

3. **缓存隔离可选**。Omniverse 的 kit 缓存（~/.cache/ov、~/.local/share/ov）是全局锁，
   多实例会打印 `Disabling key-value database because another kit process is locking it`。
   设 --isolate-cache 会给每个 worker 独立 XDG_CACHE_HOME / OV_CACHE_PATH，
   代价是每进程要重建一次 shader cache（首次多花 20–40 s）。

4. **资源闸门**。每 TICK 秒查显存 / 内存 / 磁盘，超阈值不再启动新 chunk；
   已跑的让它跑完，避免半截数据。

用法
----
    python scripts/06_run_campaign.py \
        --profiles a_slip:420 b_miss:150 c_place_miss:60 d_control:40 \
        --seed-base 9000 --bn 4 --chunk 20

    # 只跑不转换（校准产出率）
    python scripts/06_run_campaign.py --profiles c_place_miss:20 --dry-run --bn 1
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent

C_RED = "\033[31m"
C_GRN = "\033[32m"
C_YLW = "\033[33m"
C_BLD = "\033[1m"
C_RST = "\033[0m"


def ok(*a):
    print("%s✓%s %s" % (C_GRN, C_RST, " ".join(str(x) for x in a)), flush=True)


def warn(*a):
    print("%s!%s %s" % (C_YLW, C_RST, " ".join(str(x) for x in a)), flush=True)


def err(*a):
    print("%s✗%s %s" % (C_RED, C_RST, " ".join(str(x) for x in a)), flush=True)


def hdr(*a):
    print("\n%s--- %s ---%s" % (C_BLD, " ".join(str(x) for x in a), C_RST), flush=True)


def log(*a):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), " ".join(str(x) for x in a)), flush=True)


# ---------------------------------------------------------------- 资源探测
def gpu_mem() -> tuple[int, int]:
    """(used_mib, total_mib)"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10).stdout.strip().split("\n")[0]
        u, t = out.split(",")
        return int(u.strip()), int(t.strip())
    except Exception:
        return -1, -1


def mem_avail_gb() -> float:
    try:
        with open("/proc/meminfo") as fp:
            for line in fp:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024 / 1024
    except Exception:
        pass
    return -1.0


def disk_free_gb(path: str) -> float:
    try:
        return shutil.disk_usage(path).free / 1024 ** 3
    except Exception:
        return -1.0


# ---------------------------------------------------------------- 任务
@dataclass
class Chunk:
    profile: str
    cls: str
    idx: int
    n: int
    seed: int
    base: Path
    cmd: list[str] = field(default_factory=list)
    env: dict = field(default_factory=dict)
    proc: subprocess.Popen | None = None
    started: float = 0.0
    finished: float = 0.0
    rc: int | None = None
    tag: str = ""

    @property
    def name(self) -> str:
        return "%s/c%02d" % (self.profile, self.idx)


def load_profile(path: Path) -> dict:
    """source 一个 .env，返回它导出的变量（不污染本进程环境）。"""
    script = (
        "set -a; . %s >/dev/null 2>&1; "
        "python3 -c 'import json,os;print(json.dumps({k:v for k,v in os.environ.items()}))'"
        % path.as_posix()
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit("无法 source profile %s:\n%s" % (path, out.stderr))
    return json.loads(out.stdout.strip().split("\n")[-1])


def build_cmd(prof: dict, seed: int, n: int, out_raw: Path,
              py: str, gen: Path, isolate: bool, workdir: Path) -> tuple[list[str], dict]:
    env = {k: v for k, v in os.environ.items()}
    # 只透传 REC_* 与必要项，避免把父进程的无关环境带进去
    for k, v in prof.items():
        if k.startswith("REC_") or k.startswith("GEN_"):
            env[k] = v

    if isolate:
        tag = workdir.name
        env["XDG_CACHE_HOME"] = str(workdir / ".cache")
        env["XDG_DATA_HOME"] = str(workdir / ".local" / "share")
        env["XDG_CONFIG_HOME"] = str(workdir / ".config")
        env["OV_CACHE_PATH"] = str(workdir / ".ov")
        env["CUDA_CACHE_PATH"] = str(workdir / ".cuda")
        for k in ("XDG_CACHE_HOME", "OV_CACHE_PATH", "CUDA_CACHE_PATH"):
            Path(env[k]).mkdir(parents=True, exist_ok=True)

    extra = prof.get("GEN_EXTRA_FLAGS", "").split()
    cmd = [
        py, "-u", str(gen),
        "--task", prof.get("GEN_TASK", "place"),
        "--robot", prof.get("GEN_ROBOT", "franka"),
        "-n", str(n), "--seed", str(seed),
        "--save", "--enable_cameras", "--headless", *extra,
        "--episode_length", prof.get("GEN_EPISODE_LENGTH", "30"),
        "-o", str(out_raw),
    ]
    return cmd, env


# ---------------------------------------------------------------- 后置过滤
def post_filter(base: Path, prof: dict) -> tuple[int, int]:
    """按 profile 的 POST_FILTER 剔掉不合格 episode。返回 (保留, 删除)。"""
    mode = prof.get("POST_FILTER", "")
    if not mode:
        return (0, 0)

    raw = base / "raw"
    h5s = sorted(raw.glob("*.h5"))
    kept = removed = 0
    for h5 in h5s:
        jf = h5.with_suffix(".json")
        keep = True
        try:
            d = json.loads(jf.read_text(encoding="utf-8"))
            rec = d.get("recovery", {}) if isinstance(d, dict) else {}
        except Exception:
            rec = {}

        if mode == "has_reset":
            # D 类：必须出现 RESET（状态 1）。recovery 里没有直接字段，
            # 用 n_grasp_frames 出现两次间接判断不如直接读 sm_state。
            try:
                import h5py
                with h5py.File(h5, "r") as f:
                    sm = f["sm_state"][:].astype(int)
                keep = bool((sm == 1).any())
            except Exception:
                keep = False

        elif mode == "has_retry":
            # B 类：必须出现 RECOVER_APPROACH（状态 12）。
            # 生成器用了 --keep_imperfect（抓空不含 slip，不能用 --require_slip），
            # 副作用是「注入偏移但恰好还是抓到了」的干净轨迹也会被保存。
            # 不过滤的话 B 目录里会混进大量 ordinary success，事故标签密度虚高。
            try:
                import h5py
                with h5py.File(h5, "r") as f:
                    sm = f["sm_state"][:].astype(int)
                keep = bool((sm == 12).any()) and bool((sm == 9).any())
            except Exception:
                keep = False

        elif mode == "clean_success":
            forbid = {int(x) for x in prof.get("POST_FILTER_FORBID_STATES", "").split(",") if x.strip()}
            try:
                import h5py
                with h5py.File(h5, "r") as f:
                    sm = f["sm_state"][:].astype(int)
                keep = not bool(forbid & set(sm.tolist()))
                # 还要真的成功结束
                keep = keep and bool((sm == 9).any())
            except Exception:
                keep = False

        if keep:
            kept += 1
        else:
            for p in (h5, h5.with_suffix(".json")):
                try:
                    p.unlink()
                except OSError:
                    pass
            removed += 1
    return (kept, removed)


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="恢复数据生产调度器")
    ap.add_argument("--profiles", nargs="+", required=True,
                    help="格式 name:n_attempts，例如 a_slip:352 b_miss:117")
    ap.add_argument("--profile-dir", default=str(HERE / "profiles"))
    ap.add_argument("--seed-base", type=int, default=9000)
    ap.add_argument("--bn", type=int, default=1, help="并发进程数（用 04_benchmark 的结论）")
    ap.add_argument("--chunk", type=int, default=20, help="每个进程跑几次尝试")
    ap.add_argument("--stagger", type=float, default=15.0,
                    help="新进程启动间隔（秒）。实测同时启动会卡死一个，不要设 0")
    ap.add_argument("--root", default=os.path.expanduser("~/dynamicvla-demo"))
    ap.add_argument("--out-root", default=os.path.expanduser("~/dvla-gen"))
    ap.add_argument("--py", default=os.path.expanduser(
        "~/miniconda3/envs/dvla-isaac45-blackwell/bin/python"))
    ap.add_argument("--isolate-cache", action="store_true",
                    help="每个 worker 独立 XDG/OV 缓存目录（多实例必需，但首次慢 20-40 s）")
    ap.add_argument("--gpu-reserve-mib", type=int, default=1500)
    ap.add_argument("--mem-reserve-gb", type=float, default=6.0)
    ap.add_argument("--disk-min-gb", type=float, default=30.0)
    ap.add_argument("--tick", type=float, default=20.0, help="资源检查间隔，秒")
    ap.add_argument("--dry-run", action="store_true", help="只生成，不转换")
    ap.add_argument("--tag", default=None, help="输出子目录名，默认自动")
    args = ap.parse_args()

    code = Path(args.root) / "dynamic-vla"
    gen = code / "simulations" / "gen_recovery.py"
    # gen_recovery.py 里写的是 `import simulate`（裸导入），必须在 simulations/ 下运行。
    # 用 cwd=code 会 ModuleNotFoundError。
    gen_cwd = code / "simulations"
    if not gen.is_file():
        err("找不到", gen, "（先跑 scripts/02_patch_upstream.sh）")
        return 1
    if not Path(args.py).is_file():
        err("python 不可执行：", args.py, "（用 --py 指定）")
        return 1

    tag = args.tag or ("campaign_" + datetime.now().strftime("%m%d_%H%M%S"))
    out_root = Path(args.out_root) / tag
    out_root.mkdir(parents=True, exist_ok=True)
    logs = out_root / "logs"
    logs.mkdir(exist_ok=True)

    print("%s========================================%s" % (C_BLD, C_RST))
    print("%s  06 · 生产调度器%s" % (C_BLD, C_RST))
    print("%s========================================%s" % (C_BLD, C_RST))
    print("output  : %s" % out_root)
    print("bn      : %d   chunk: %d   stagger: %.0fs" % (args.bn, args.chunk, args.stagger))
    print("isolate : %s" % ("on" if args.isolate_cache else "off"))

    # ---- 建 chunk 队列 ----
    queue: deque[Chunk] = deque()
    plan = []
    for spec in args.profiles:
        if ":" not in spec:
            err("--profiles 格式应为 name:n，收到", spec)
            return 2
        name, nstr = spec.rsplit(":", 1)
        n_attempts = int(nstr)
        pfile = Path(args.profile_dir) / ("%s.env" % name)
        if not pfile.is_file():
            err("profile 不存在：", pfile)
            return 2
        prof = load_profile(pfile)
        cls = prof.get("PROFILE_CLASS", "?")

        n_chunks = max((n_attempts + args.chunk - 1) // args.chunk, 1)
        # 每 chunk 的实际尝试数（最后一个可能不满）
        remain = n_attempts
        for i in range(n_chunks):
            this_n = min(args.chunk, remain)
            remain -= this_n
            # seed 段：profile 间不重叠、chunk 间不重叠
            seed = args.seed_base + hash(name) % 1000 * 1000 + i * args.chunk
            base = out_root / name / ("c%02d" % i)
            (base / "raw").mkdir(parents=True, exist_ok=True)
            queue.append(Chunk(profile=name, cls=cls, idx=i, n=this_n,
                               seed=seed, base=base, tag=tag + "/" + name))
        plan.append((name, cls, n_attempts, n_chunks, prof))

    hdr("计划")
    print("  %-14s %-4s %8s %6s %8s" % ("profile", "cls", "attempts", "chunks", "exp.h5"))
    tot_att = 0
    for name, cls, n_att, n_ch, prof in plan:
        exp = n_att * float(prof.get("EXPECT_YIELD", 0.4))
        tot_att += n_att
        print("  %-14s %-4s %8d %6d %8.0f" % (name, cls, n_att, n_ch, exp))
    print("  %-14s %-4s %8d" % ("TOTAL", "", tot_att))

    # ---- 写调度计划快照 ----
    (out_root / "campaign_plan.json").write_text(json.dumps({
        "tag": tag,
        "bn": args.bn, "chunk": args.chunk, "stagger": args.stagger,
        "seed_base": args.seed_base,
        "isolate_cache": args.isolate_cache,
        "profiles": [
            {"name": n, "class": c, "attempts": a, "chunks": ch,
             "profile_file": str(Path(args.profile_dir) / (n + ".env")),
             "expect_yield": p.get("EXPECT_YIELD")}
            for n, c, a, ch, p in plan
        ],
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    # ---- 调度循环 ----
    running: list[Chunk] = []
    done: list[Chunk] = []
    retried: dict[str, int] = {}
    last_start = 0.0
    paused = False
    t_start = time.time()

    def launch(ch: Chunk) -> None:
        nonlocal last_start
        prof = load_profile(Path(args.profile_dir) / (ch.profile + ".env"))
        cmd, env = build_cmd(prof, ch.seed, ch.n, ch.base / "raw",
                            args.py, gen, args.isolate_cache, ch.base)
        (ch.base / "run_config.json").write_text(json.dumps({
            "profile": ch.profile, "class": ch.cls, "chunk": ch.idx,
            "n_attempts": ch.n, "seed": ch.seed,
            **{k: v for k, v in prof.items() if k.startswith(("REC_", "GEN_"))},
        }, indent=2, ensure_ascii=False), encoding="utf-8")

        logf = open(logs / ("%s_c%02d.log" % (ch.profile, ch.idx)), "w")
        ch.cmd = cmd
        ch.proc = subprocess.Popen(cmd, cwd=str(gen_cwd), env=env,
                                   stdout=logf, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        ch.started = time.time()
        last_start = time.time()
        running.append(ch)
        log("启动 %-22s pid=%-7d n=%d seed=%d" % (ch.name, ch.proc.pid, ch.n, ch.seed))

    gpu_total = gpu_mem()[1]
    max_by_gpu = max((gpu_total - args.gpu_reserve_mib) // 5700, 1) if gpu_total > 0 else 1
    bn_eff = min(args.bn, max_by_gpu)
    if bn_eff < args.bn:
        warn("显存只允许 %d 进程，bn 从 %d 降到 %d" % (max_by_gpu, args.bn, bn_eff))

    hdr("开始调度（Ctrl-C 优雅退出，已完成的不丢）")
    interrupted = False

    def on_sigint(sig, frm):
        nonlocal interrupted
        interrupted = True
        warn("收到中断，等待在跑的任务收尾（再按一次强退）")

    signal.signal(signal.SIGINT, on_sigint)

    try:
        while (queue or running) and not interrupted:
            # 收尸
            for ch in list(running):
                rc = ch.proc.poll()
                if rc is None:
                    continue
                ch.rc = rc
                ch.finished = time.time()
                running.remove(ch)
                n_h5 = len(list((ch.base / "raw").glob("*.h5")))
                dur = ch.finished - ch.started
                if rc == 0:
                    log("完成 %-22s %3d 条  %6.0fs  %.1fs/try" % (ch.name, n_h5, dur, dur / ch.n))
                    done.append(ch)
                else:
                    k = ch.name
                    retried[k] = retried.get(k, 0) + 1
                    if retried[k] <= 1:
                        warn("失败 %-22s rc=%s，重试一次" % (ch.name, rc))
                        queue.appendleft(ch)
                    else:
                        err("放弃 %-22s rc=%s（已重试过）" % (ch.name, rc))
                        done.append(ch)

            # 资源闸门
            gu, gt = gpu_mem()
            ma = mem_avail_gb()
            df = disk_free_gb(str(out_root))
            block = None
            if gt > 0 and gu > gt - args.gpu_reserve_mib:
                block = "显存 %d/%d MiB" % (gu, gt)
            elif 0 < ma < args.mem_reserve_gb:
                block = "可用内存 %.1f GB" % ma
            elif 0 < df < args.disk_min_gb:
                block = "磁盘剩 %.1f GB" % df

            if block:
                if not paused:
                    warn("暂停新任务：%s" % block)
                    paused = True
            else:
                if paused:
                    ok("资源恢复，继续")
                    paused = False

            # 补位
            while (not paused and not interrupted and queue
                   and len(running) < bn_eff):
                # 错峰：新进程与上一个启动至少隔 stagger 秒
                if running and (time.time() - last_start) < args.stagger:
                    break
                launch(queue.popleft())

            # 进度
            n_h5 = sum(len(list((c.base / "raw").glob("*.h5"))) for c in done)
            n_h5_run = sum(len(list((c.base / "raw").glob("*.h5"))) for c in running)
            el = time.time() - t_start
            print("\r  [%5.0fs] 队列 %2d | 在跑 %d | 完成 %2d | h5 %4d | 显存 %s | mem %.1fG | disk %.0fG   "
                  % (el, len(queue), len(running),
                     len(done), n_h5 + n_h5_run, "%dM" % gu if gu > 0 else "?",
                     ma if ma > 0 else -1, df if df > 0 else -1),
                  end="", flush=True)
            time.sleep(args.tick)

    finally:
        print()
        if running:
            warn("仍有 %d 个进程在跑，等待收尾…" % len(running))
            for ch in running:
                try:
                    ch.proc.wait(timeout=1800)
                except Exception:
                    warn("强杀", ch.name)
                    ch.proc.kill()
                done.append(ch)

    # ---- 汇总 ----
    hdr("生成完毕")
    per_profile: dict[str, dict] = {}
    for ch in done:
        d = per_profile.setdefault(ch.profile, {"att": 0, "h5": 0, "cls": ch.cls})
        d["att"] += ch.n
        d["h5"] += len(list((ch.base / "raw").glob("*.h5")))

    print("  %-14s %-4s %8s %6s %8s" % ("profile", "cls", "attempts", "h5", "yield"))
    tot_a = tot_h = 0
    for name, d in sorted(per_profile.items()):
        y = 100.0 * d["h5"] / d["att"] if d["att"] else 0
        print("  %-14s %-4s %8d %6d %7.1f%%" % (name, d["cls"], d["att"], d["h5"], y))
        tot_a += d["att"]; tot_h += d["h5"]
    print("  %-14s %-4s %8d %6d %7.1f%%" % ("TOTAL", "", tot_a, tot_h,
                                           100.0 * tot_h / tot_a if tot_a else 0))

    (out_root / "campaign_result.json").write_text(json.dumps({
        "tag": tag, "elapsed_s": round(time.time() - t_start, 1),
        "per_profile": per_profile,
        "failed": {c.name: c.rc for c in done if c.rc not in (0, None)},
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    ok("结果 ->", out_root / "campaign_result.json")

    # ---- 后置过滤 ----
    hdr("后置过滤")
    for name, cls, n_att, n_ch, prof in plan:
        if not prof.get("POST_FILTER"):
            continue
        base = out_root / name
        kept = removed = 0
        for c in sorted(base.glob("c*")):
            k, r = post_filter(c, prof)
            kept += k; removed += r
        print("  %-14s 保留 %3d / 删除 %3d（%s）" % (name, kept, removed, prof["POST_FILTER"]))

    if args.dry_run:
        warn("--dry-run：不转换。看产出率后决定下一批条数")
        return 0

    # ---- 转换 ----
    hdr("转 LeRobot")
    for name, cls, n_att, n_ch, prof in plan:
        base = out_root / name
        n = len(list(base.glob("c*/raw/*.h5")))
        if n == 0:
            warn("%s 无 h5，跳过" % name)
            continue
        conv = HERE / "scripts" / "07_convert_lerobot.sh"
        if not conv.is_file():
            warn("找不到", conv, "跳过转换")
            break
        log("转换 %s（%d 条）" % (name, n))
        rc = subprocess.call(
            ["bash", str(conv), "--base-str", str(base), "--repo", "local/%s" % name],
            cwd=str(HERE))
        if rc != 0:
            err("转换失败", name, "rc=", rc)

    ok("全部完成 ->", out_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())