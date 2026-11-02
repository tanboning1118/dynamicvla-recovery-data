# 05 · 生产 SOP：从空机器到 330 条可用数据

> 这份是**执行清单**，不是原理说明。原理见 01/03，格式见 04。
> 每一步都有「做到什么算过」的判据。

---

## 阶段 0 · 环境（约 2–3 h）

| # | 动作 | 判据 |
|---:|---|---|
| 0.1 | `nvidia-smi` 看驱动 | 驱动 ≥ 535，`CUDA Version ≥ 12.2` |
| 0.2 | `nvidia-smi --query-gpu=compute_cap --format=csv` | 4090 → **8.9** |
| 0.3 | `free -g` / `nproc` / `df -h` | 内存 ≥ 64 G（4 进程）/ 核 ≥ 16 / 本地盘 ≥ 120 G |
| 0.4 | 建 conda env + 装 Isaac Sim 4.5 | `python -c "import isaacsim;print(isaacsim.__version__)"` → 4.5.0.0 |
| 0.5 | 装 Isaac Lab 2.2.1 | `pip list | grep isaaclab` → 0.45.9 |
| 0.6 | 拉上游锁 commit | `git -C dynamic-vla rev-parse HEAD` → `a27a06d...` |
| 0.7 | 下载 + 解压资产 | `ls objects | wc -l`=23，`ls scenes/*.usd | wc -l`=82 |
| 0.8 | `python scripts/02_patch_upstream.sh` | payload 两文件在位 |
| 0.9 | `bash scripts/03_preflight.sh` | **全绿** |

**0.9 的 preflight 检查项**（任何一项红都不许往下走）：

```
[ ] python 3.10.x
[ ] torch 有 CUDA，cuda_available=True
[ ] compute_cap 与 GPU 名匹配（4090 → 8.9）
[ ] isaacsim 4.5.0.0 / isaaclab 0.45.9 / isaaclab_tasks 0.10.47
[ ] dynamic-vla 在 a27a06d
[ ] objects/metadata.json 189 条
[ ] scenes/*.usd 82 个
[ ] 输出盘剩余 ≥ 100 GB
[ ] 系统内存 ≥ 32 GB
[ ] h5py / pyyaml / cv2 可 import
```

---

## 阶段 1 · 单条跑通（约 5 min）

```bash
export REC_SLIP=0 REC_GRASP_CHECK=0 REC_MISS_OFFSET=0 REC_DISTURB_AT=-1
python simulations/gen_recovery.py --task place --robot franka \
    -n 2 --seed 500 --save --enable_cameras --headless \
    --keep_imperfect --episode_length 30 -o /tmp/first_try
```

| 判据 | 值 |
|---|---|
| 总墙钟 | 100–150 s |
| `_DONE` 里的 `attempts` | 2 |
| `saved` | 0 或 1（baseline 成功率 37%，0 也正常） |
| h5 存在时大小 | 120–200 MB |
| 日志里有 `Completed setting up the environment` | 是 |

**常见失败**：

| 现象 | 原因 | 修 |
|---|---|---|
| `no USD found` | 资产没解压到上游根目录 | 见 00_quickstart §4 |
| `ModuleNotFoundError: state_machines` | 在错误目录下跑 | `cd dynamic-vla` |
| `no kernel image is available` | torch 是从 Blackwell 机器拷的 | 重建 env |
| 卡在 `Starting the simulation` > 3 min | 首次启动在编译 warp kernel / 下扩展缓存 | 等；或先跑一次预热 |

---

## 阶段 2 · 产能实测（约 30 min，**不可跳过**）

```bash
bash scripts/04_benchmark_single_card.sh --root ~/dynamicvla-demo --rounds 5
```

**这一步决定后面所有时间估算，也是唯一能定并发度的依据。**

| 输出指标 | 用来定什么 |
|---|---|
| `gpu_peak_MiB` | 并发上限 = `floor((总显存 − 1200) / peak)` |
| `rss_peak_GB` | `CHUNK` = `floor((可用内存 − 4) / (rss × BN) × 20)` 的反向 |
| `per_ep_s` 各并发度 | 选出 `linearity ≥ 0.70` 的最大并发 |
| `linearity` | `< 0.70` 就降到上一档 |

**把结果填回 README §4.2 的表**（那张表现在是推算值）。

---

## 阶段 3 · 四类小批校准（约 1.5 h）

**每类各跑 8–20 次尝试，只生成不转换**，目的是拿到 C/D 的真实产出率，并确认
**恢复路径真的被走过了**（不是「保存成功」就算数）。

```bash
for p in a_slip b_miss c_place_miss d_control; do
    bash scripts/24_run_class.sh profiles/$p.env calib_$p 7000001 20
    bash scripts/08_qa_gates.sh ~/dvla-gen/calib_$p
done
```

> 用 `24_run_class.sh` 而不是手拼命令行：四类的差异不只是「开哪个注入」，
> 还有 episode 预算和 `--require_slip` / `--keep_imperfect` 的取舍。漏一项就会
> 静默产废数据，且从保存率上看不出来（见下表 B 行）。

**验收标准不是产出率，是「恢复状态出现过」**：

| 类 | 必须出现的状态 | 判废的假阳性 |
|---|---|---|
| A 滑脱 | 10 / 11 / 12 | 只有 5→6 直接过（没滑脱） |
| B 抓空 | 12（无 10/11） | 保存成功但状态里没有 12（偏移注入了却没抓住空） |
| C 放偏 | 1（RESET）**且触发前状态 ≥ 8** | 有 1 但来自抓取/搬运失败 —— 只看「含 1」会全部误判为有效 |
| D 对照 | 无 1/10/11/12/13 | 出现任一 → 该条应被剔除 |

> C 那一行是本轮最重要的修正。**「含状态 1」不等于 C 类成立**：同一批样本里，
> apple03d 的 RESET 来自 `6`（搬运途中掉件），只有 kiwi00d 的来自 `8/9`（放置失败）。
> 前者会被归为 `X_reset` 并单独列出，不冒充 C。
> `scripts/25_verify_episode.py` 会自动打印这个归因。

| 类 | 实测产出率（2026-11-02，4090 量产） | 若显著低于此值 | 调什么 |
|---|---:|---|---|
| A 滑脱 | 26%（13/50），**类别纯度 100%** | 检查 `REC_SLIP_MODE` 是否真为 `friction` | `SETTLE_MAX_TIME` 提到 6.0 |
| B 抓空 | 保存率 40%（20/50），**其中真抓空 11/20（55%）** | 真抓空样本全超时 → 预算不够 | `GEN_EPISODE_LENGTH` 30 → 45，并加 `POST_FILTER=has_retry` |
| C 放偏 | 自然筛选约 1/8；`REC_PLACE_OFFSET=0.06` 不稳定 | 偏移不够狠，放置仍然成功 | 偏移提到 0.10–0.12，或换小口径容器 |
| D 对照 | 约 35–40% | — | 无需调 |

⚠️ **量产目录必须过滤后再转换**。B 类用 `--keep_imperfect` 保存，混入的干净轨迹
实测占 4/8；不过滤会让事故标签密度虚高一半。一条命令解决：

```bash
python scripts/25_verify_episode.py <batch_dir> --expect B_miss --prune
```

**闸门必须全绿**：

```
G1 配置闸  PASS
G2 机制闸  PASS（A 类必须 9/9 以上的比例）
G3 几何闸  PASS
G4 标签闸  PASS（lost 占比 > 0）
G5 可复现闸 PASS（run_config.json 存在）
```

C/E 类 `G2/G3` 会是 `N/A`（没有滑脱，无从判定）——**这是正常的**，不是失败。

---

## 阶段 4 · 正式量产

```bash
# 并发度用阶段 2 的实测值；条数用阶段 3 校准后的产出率反推
BN=4 CHUNK=20 bash scripts/06_run_campaign.py \
    --root ~/dynamicvla-demo \
    --profiles a_slip:420 b_miss:150 c_place_miss:60 d_control:40 \
    --seed-base 9000 \
    --out-root ~/dvla-gen
```

调度器行为：

| 行为 | 说明 |
|---|---|
| chunk 队列 | 每个 (profile, chunk) 一个任务，`CHUNK=20` 次尝试 |
| 进程生命周期 | **一个 chunk 一个进程**，退出即回收（兜 Isaac 的 RSS 泄漏） |
| seed 段 | profile 间不重叠，chunk 间不重叠 |
| 监控 | 每 30 s 查显存/内存/磁盘，超阈值暂停新 chunk |
| 失败重试 | 单 chunk 返回码非 0 时重跑 1 次，再失败则记入 `failed_chunks.json` |

**别做的事**：

- ❌ 用一个进程跑完全部 chunk（RSS 会涨到 20 GB+）
- ❌ 把 5 类塞进同一个 loop（出坏批次分不清哪类参数问题）
- ❌ 把 h5 写到网络盘（ossfs 上单条卡 30 s+，吞吐砍半）

---

## 阶段 5 · 转换与验收

```bash
for d in ~/dvla-gen/*/; do
    bash scripts/07_convert_lerobot.sh --base "$d" --repo "local/$(basename $d)"
done
```

**转换前**：`annotate_recovery.py` 会先把 `sm_state` 挖出来写侧车索引（转换器会丢掉它）。
**转换后**：立刻删 h5。

验收（`scripts/09_verify_loader.py`，14 项）：

| 项 | 期望 |
|---|---|
| `action` | `(20, 7)` |
| `observation.state` | `(2, 6)` |
| 3 路相机 | `(2, 3, 384, 512)` |
| `event_spatial` | `(15,)` float32 |
| `event_cls` | ∈[0,21] |
| `event_grasp` | ∈{0,1,2} |
| **gripper 未被 delta** | parquet 全长 `⊆ {−1,+1}` |
| **事故类标签非零** | `slipping + lost > 0` |

---

## 阶段 6 · holdout 隔离

**holdout 必须用独立 seed 段，且不进训练集。**

```bash
bash scripts/06_run_campaign.py --profiles a_slip:38 b_miss:12 ... \
    --seed-base 19000 --out-root ~/dvla-gen-holdout
```

| 要求 | 做法 |
|---|---|
| seed 不重叠 | 训练用 `--seed-base 9000`，holdout 用 `19000` |
| 目录隔离 | `~/dvla-gen` vs `~/dvla-gen-holdout` |
| 命名带标记 | `--repo local/holdout_a_slip` |
| 训练时排除 | `03_train.sh` 里显式列 repo，不用通配符 |

---

## 阶段 7 · 交付检查

```bash
python scripts/10_report.py --base ~/dvla-gen --holdout ~/dvla-gen-holdout
```

输出一张表，逐项打勾才算完成：

```
[ ] 每类条数 ≥ 目标（A120 B75 C60 D30 E15）
[ ] 每类闸门全绿（或 N/A 合理）
[ ] 帧数分布：中位在 120–220，无 > 600 帧的异常
[ ] 事故类标签占比：A 类 ≥ 40%，B 类 ≥ 30%
[ ] holdout 30 条已隔离，seed 段不重叠
[ ] h5 已删除，常驻空间 ≤ 10 GB
[ ] recovery_index.json 存在，且 summary.episodes_mapped == len(episodes)、missing == 0
[ ] 每批的 run_config.json 都在（可复现）
```

---

## 附：时间线（4090 4 进程，推算）

| 阶段 | 墙钟 |
|---|---:|
| 0 环境 | 2–3 h |
| 1 单条跑通 | 5 min |
| 2 产能实测 | 30 min |
| 3 四类校准（80 尝试） | 1.5 h |
| 4 正式量产（766 尝试） | 3.5–4 h |
| 5 转换 + 验收 | 40 min |
| 6 holdout | 20 min |
| 7 交付检查 | 15 min |
| **合计** | **~9 h** |

含 20% 缓冲 → **~11 h**。两天内稳妥完成。

---

## 附：出问题时的第一反应

| 症状 | 先看哪里 |
|---|---|
| 产出条数为 0 | `gen.log` 里搜 `no placement`；再看 `_DONE` 的 `attempts` 是否为 0 |
| 产出率突然腰斩 | 比对 `run_config.json` 与上一批是否一致 |
| 闸门 G2 FAIL | `REC_SLIP_MODE` 不是 friction（默认值是 open！） |
| 闸门 G4 FAIL（lost=0） | 整批没造出事故，检查注入是否真的生效 |
| 训练侧 loss 爆炸 | 先确认 `grasp_label` 补丁打了没有 |
| 转完 LeRobot 条数变少 | 转换器会丢「帧数不足或无有效 action」的 episode，看转换日志 |
| 内存 OOM | `CHUNK` 降到 10；`BN` 减一 |