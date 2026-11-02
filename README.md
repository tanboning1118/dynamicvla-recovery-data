# dynamicvla-recovery-data

**为 DynamicVLA 生产「意外 — 检测 — 恢复」轨迹的开源数据工厂。**

DOM 数据集是 20 万条**全成功**轨迹。里面 `slipping + lost` 只占 **1.34%** —— 模型一辈子见不到几次「握不住」。
本仓库提供一套能在单张 4090 上跑起来的生成管线，把事故类标签密度推到 **49%**（37 倍），
用来教会模型：**东西掉了要捡起来，没抓到要重抓，推歪了要重新定位。**

---

## ⚠️ 先读这一段：本仓库能用在哪、不能用在哪

**能做的**（已实测）：

- 在**原版上游 `hzxie/DynamicVLA`**（commit `a27a06d`）上生成 5 类恢复轨迹的 **h5 / LeRobot 数据**
- 单卡串行跑通，产出结构与 DOM 对齐
- 三类独立验收：闸门（`08`）、加载器 14 项（`09`）、交付汇总（`10`）

**不能做的**（诚实边界）：

| 限制 | 原因 |
|---|---|
| ❌ **`utils/event_labels.py` 不在公开上游里** | 那是我在自己改造的 `server_fullft` 里新增的文件。原版上游 `utils/` 下只有 `average_meter/datasets/distributed/helpers/instruction_generator/maya_controller/memcached/summary_writer` 八个文件。**生成链路不依赖它**（`gen_recovery.py` 只 import `simulate` 和 `place_recovery_sm`），但**训练侧的标签计算依赖** |
| ❌ 生成的 h5 不能直接喂给 `managed_train.py` | 它硬编码 `hzxie/DOM` 且有 `audit_dataset` 过滤，自有数据进不去（`docs/04` §8.1） |
| ⚠️ `patches/apply_grasp_rise_patch.py` 只在**你的 `server_fullft`** 上有意义 | 它改的是 `event_labels.py`，而那个文件不在上游。如果你的 fork 里没有这个文件，`02` 会自动跳过补丁步骤（不会报错） |
| ⚠️ 并发能力取决于显存 | 8 GB 卡只能串行；**4090 24 GB 实测双进程可并行**（§4.2 / `docs/08`） |

**所以这个仓库的定位是**：把「仿真侧怎么造出这类数据」这部分完整开源出来 ——
脚本、配方、场景设计、标签契约、踩坑全部可复现。**训练侧的对接需要你自己有一份
带 `event_labels.py` 的 fork**，或者按 `docs/04_label_contract.md` 自己实现标签器。

---

## 0. 这个仓库解决什么问题

在 DOM 闭环评测的 1800 条视频里，**只有约 1/4 的失败是「抓住后滑脱」**。

| 失败形态 | 占全量失败 | 含义 |
|---|---:|---|
| 全程尝试但做满上限 | **48.4%** | 模型根本没进入持握状态 |
| 中途早停（≤ 上限×0.55） | **26.7%** | 已经开始搬，搬的过程中出事，然后放弃 |
| 其余 | 24.9% | — |

所以「只造一条滑脱」的数据覆盖不到 3/4 的失败。本仓库造 **5 类**，配比按失败份额反推：

| 类 | 场景 | 注入手段 | 对应失败 | 目标条数 |
|---|---|---|---:|---:|
| **A** | 滑脱后重抓 | 摩擦降到 μ=0.02 + 配重 ×8，夹爪保持闭合 | R2 | 120 |
| **B** | 抓空后重试 | 首抓横向偏移 5 cm → 闭合夹到空气 → 抬升检测为 0 → 重抓 | R1 | 75 |
| **C** | 推偏后重新定位 | 机械臂接近途中物体被侧向速度冲走 | R3 | 60 |
| **D** | 放偏后二次放置 | 放置后 `is_object_placed` 连续 5 帧为假 → RESET 重试 | R4 | 30 |
| **E** | 纯成功对照 | 不注入 | 负对照 | 15 |

合计 **300 条**。每类的物理机制、参数依据、实测产出率见 [`docs/01_scenario_design.md`](docs/01_scenario_design.md)。

---

## 1. 硬件与依赖

生成阶段**只跑仿真，不需要模型权重**，一张消费级卡就够。

| 项 | 本仓库实测量级 |
|---|---|
| GPU | **单环境峰值 5.7 GB 显存**（3 路 480×360 相机 + 物理 + RTX 光栅） |
| 显存下限 | 8 GB（已在 RTX 5060 8G 上跑通全部 5 类） |
| 目标卡 | **RTX 4090 24 GB** —— **双进程并行**（19 GB / 33% util，见 §4.2、`docs/08`）；8 GB 卡需串行 |
| CPU | 单进程约 1.5 核有效占用；建议 ≥16 核 |
| 内存 | **单进程 RSS 峰值 ~8.4 GB**（Isaac Sim 启动后基本不回收，每 episode 泄漏 ~200 MB） |
| 磁盘 | h5 临时占用 **~180 MB/episode**，转 LeRobot 后 **~22 MB/episode**，转换完必须删 h5 |
| 软件 | Isaac Sim **4.5.0** + Isaac Lab **2.2.1**，Python 3.10，torch 2.7.1+cu128 |

> 🔴 **最重要的一条约束**：`PlaceRecoveryStateMachine` 有 `assert num_envs == 1`（单实例内不能多环境）。
> 是否能用多进程，**取决于显卡显存**：
> - **8 GB 级卡（如 5060）**：第二个实例会卡死在 Omniverse kit 初始化（实测只申请 821 MiB，永不退出）→ **只能串行**
> - **24 GB 级卡（4090 实测 2026-11-02）**：**双进程可并行**，显存 19 GB / GPU 利用率 33% / 吞吐接近线性
>   → 直接用 [`scripts/22_supervisor_4090.sh`](scripts/22_supervisor_4090.sh)（三进程必 OOM）
>
> 详见 [`docs/07_concurrency_finding.md`](docs/07_concurrency_finding.md) §12 与
> [`docs/08_4090_parallel.md`](docs/08_4090_parallel.md)。

> ⚠️ **`sm_89`（4090 Ada）与 Blackwell 的 torch 轮子不同**。如果你从 Blackwell 机器复制环境，必须重建，
> 不能直接拷 conda env。见 [`docs/00_quickstart.md`](docs/00_quickstart.md) §2。
>
> ⚠️ 4090 的 `sm_89` 需要 **CUDA 12.x** 轮子；Isaac Sim 4.5 默认拉 cu12，一般无需额外处理。

---

## 2. 五分钟跑通

```bash
git clone <this-repo> && cd dynamicvla-recovery-data

# 1) 拉上游代码（固定 commit，不跟 main）
bash scripts/00_setup.sh --upstream ~/dynamicvla-demo

# 2) 下载资产（约 2 GB，不随 git 分发）
#    手动从 https://gateway.infinitescript.com/?f=DOM-Test
#         和 https://gateway.infinitescript.com/?f=DOM-3D-Objects 下载后：
bash scripts/01_fetch_assets.sh --zips ~/Downloads

# 3) 把注入脚本挂进上游 + 打标签补丁
bash scripts/02_patch_upstream.sh --root ~/dynamicvla-demo

# 4) 自检（环境版本 / 资产完整性 / 显卡 / 磁盘）
bash scripts/03_preflight.sh --root ~/dynamicvla-demo

# 5) 单卡产能实测 —— 不可跳过。它会先验证「能不能同时跑」，再谈吞吐
#    --rounds 是「每个进程试几次」，不是总条数
bash scripts/04_benchmark_single_card.sh --root ~/dynamicvla-demo --rounds 3

# 6) 小批试产（每类 2 条，约 15 分钟）
bash scripts/05_generate.sh --root ~/dynamicvla-demo --profile profiles/a_slip.env --n 4 --seed 9000

# 7) 转 LeRobot → 过闸门 → 验收
bash scripts/07_convert_lerobot.sh --root ~/dynamicvla-demo --base ~/dvla-gen/a_slip
bash scripts/08_qa_gates.sh ~/dvla-gen/a_slip --profile a_slip
python scripts/09_verify_loader.py --dataset ~/dvla-gen/a_slip/lerobot_home/local/a_slip

# 8) 看整体交付情况
python scripts/10_report.py --base ~/dvla-gen
```

> 第 5 步在 **8 GB 卡**上的结论很可能是「只能开 1 个进程」——那不是你配错了，是显存不足时
> Omniverse kit 初始化抢锁失败。**24 GB 的 4090 上双进程是可用的**（2026-11-02 实测）。
> 详见 [`docs/07_concurrency_finding.md`](docs/07_concurrency_finding.md) §12。

---

## 3. 目录

```
payload/                      ← 注入上游的两个文件，原样拷进 dynamic-vla/
  simulations/gen_recovery.py           生成器驱动（薄封装官方 simulate）
  simulations/state_machines/place_recovery_sm.py   带恢复的放置状态机
  annotate_recovery.py                  把 sm_state 落到 LeRobot 侧车索引
  qa_gates.py                           五道生产闸门 + 标签分布实测
profiles/                     ← 5 类配方，参数全显式，不依赖任何默认值
scripts/                      ← 一键链 00→10
patches/                      ← grasp_label 段级抬升先决补丁（阻断级，见 §5）
docs/                         ← 场景设计 / 采集规模 / 单卡实测 / 标签契约 / SOP / 踩坑 / 4090 并行
```

`scripts/` 全清单：

| 脚本 | 作用 | 什么时候跑 |
|---|---|---|
| `00_setup.sh` | 拉上游代码，锁 commit | 一次 |
| `01_fetch_assets.sh` | 装资产（约 2 GB，不随 git） | 一次 |
| `02_patch_upstream.sh` | 注入脚本挂进上游 + 打补丁 | 一次（或上游更新后） |
| `03_preflight.sh` | 环境/资产/显卡/磁盘/一致性自检 | 每次换机器 |
| `04_benchmark_single_card.sh` | **验证能不能并发** + 测产能 | 每台新机器 |
| `05_generate.sh` | 单批生成（含危险组合自检） | 每批 |
| `06_run_campaign.py` | 多批调度（chunk / 资源闸门 / 重试） | 量产 |
| `07_convert_lerobot.sh` | h5 → LeRobot（含侧车 join） | 每批 |
| `08_qa_gates.sh` | 五道闸门，按类自适应 | 每批 |
| `09_verify_loader.py` | 独立加载器 14 项验收 | 每批 |
| `10_report.py` | 交付汇总（8 个勾选项） | 阶段性 |
| `21_parallel_worker.sh` | **单 worker 封装**（含 LD_LIBRARY_PATH 修复） | 4090 并行生产 |
| `22_supervisor_4090.sh` | **双 worker 滚动生产**（seed 递增 + 磁盘守卫） | 4090 量产 |
| `23_pull_4090_to_workstation.sh` | 远端拉取 + 传完即删（防半写） | 磁盘吃紧时 |

---

## 4. 单卡产能（实测，含一次被推翻的推算）

### 4.1 实测：RTX 5060 8 GB，单进程

在工位机 `RTX 5060 8G / 16 核 / 15.8 GB RAM` 上实测（2026-11-02）：

| 指标 | 实测值 |
|---|---|
| Isaac Sim 应用启动（一次性） | **~35 s** |
| 单 episode 仿真（place，108–190 帧） | **10–50 s** |
| 单环境显存峰值 | **5 660 MiB** |
| 单进程 RSS 峰值 | **8.4 GB** |
| h5 体积（108 帧） | **130 MB**（≈1.2 MB/帧，3 路相机 gzip） |
| 3 次尝试总墙钟（含启动） | **121 s** |

> 单条耗时随轨迹长度线性变化：108 帧的干净成功 ≈ 10 s 仿真；滑脱恢复轨迹（169–190 帧
> + settle 等待）≈ 40–50 s。P1.6 的 friction 批次实测平均 **68 s/尝试**（含被丢弃的尝试）。

### 4.2 ⚠️ 8 GB 卡上不能开多进程（实测），但 24 GB 的 4090 可以

我原本按显存算术推算「4090 24 GB 能开 4 个进程，300 条约 3.9 h」。**在 5060 8 GB 上实测，
这个推算被推翻了**（下表的实验全部在 5060 8 GB 上做）：

| 实验 | 配置 | 进程 A | 进程 B | 有效吞吐 |
|---|---|---|---:|---:|
| 基线 | 单进程 | ✓ 121 s / 1 条 | — | **2.5 条/min** |
| 同时启动 | 2 进程同时 | ✓ 515 s / 2 条 | ✗ **卡死** 0 条 | 0.23 条/min |
| 错峰启动 | 隔 60 s | ✓ 115 s / 3 条 | ✗ **卡死** 0 条 | — |
| 隔离缓存 | 隔 20 s + 独立 XDG/OV 缓存 | ✗ 卡死 | ✗ 卡死 | **0** |

**死锁特征**：第二个实例只申请到 **821 MiB 显存**就停住，日志停在 `omni.physx` / `omni.fabric`
初始化，**不报错、不超时、不退出**。根因是 Omniverse Kit 的全局单例锁（`omni.kvdb` / `omni.fabric` / `omni.physx`）。

两条并发路都堵住了，但**堵的性质不同**：

```
多环境（--num_envs N）？  ✗ assert num_envs == 1 ← 我们自己的实现约束，可以改
多进程？                  ✗ Omniverse kit 全局锁 ← Isaac Sim 硬约束，改不了
```

> **别把这两条混为一谈。** 上游 `place_sm.py` 本身就是向量化的（全程 `[:, ...]`，
> 支持 `num_envs > 1`）；我们为简化把 `place_recovery_sm.py` 砍成了单环境。
> 所以「单卡只能串行」一半是平台的、一半是我们自己的。
> **要真正加速，先把恢复状态机重新向量化（约 2–4 天），再考虑租更多机器。**
> 难点与解法见 [`docs/07_concurrency_finding.md`](docs/07_concurrency_finding.md) §6。

> 完整实验数据、诊断命令、以及「只做资源算术不做启动验证」这条方法论教训，
> 见 [`docs/07_concurrency_finding.md`](docs/07_concurrency_finding.md)。

**🔴 但 4090 24 GB 上双进程是可用的（2026-11-02 实测）**：两个 worker 同时跑，
显存 8.9 + 10.1 ≈ **19 GB**、GPU 利用率 **33%**（单进程仅 17%）、吞吐 **~28 s/attempt**
（单进程 37–41 s），接近线性。三进程预计 28 GB+ 必 OOM。

| 卡 | 双进程 | 依据 |
|---|---|---|
| 5060 8 GB | ✗ 第二个卡死在 kit 初始化 | 上表实测 |
| **4090 24 GB** | ✅ **19 GB / 33% util / 产出正常** | 本次实测（`docs/08`） |

> 别把一台机器的死锁结论当成平台定律。换卡、换驱动、换 kit 配置都要重测一次。
> 一键部署：`scripts/22_supervisor_4090.sh`。

### 4.3 4090 的真实收益：双进程并行 + 单进程略快

| 维度 | 8 GB（5060） | 24 GB（4090） | 对生成的影响 |
|---|---|---|---|
| 显存峰值（单实例） | 5.66 GB | 9~10 GB（含资产波动） | 无差别 |
| **并行进程数** | **1（第二实例死锁）** | **2（实测 19 GB）** | **吞吐 ≈ ×2** |
| 光栅吞吐 | 入门 | 完整 AD102 | 单进程略快 |
| 显存带宽 | 448 GB/s | 1008 GB/s | 图像回读更快 |

**4090 上机第一件事**：跑 `scripts/04_benchmark_single_card.sh` 确认本机能开几个进程，
再用 `scripts/21_parallel_worker.sh` / `22_supervisor_4090.sh` 起并行生产。

### 4.4 300 条需要多久（串行修正版）

| 类 | 条数 | 尝试数（产出率反推） | 单尝试耗时 | 单进程耗时 |
|---|---:|---:|---:|---:|
| A 滑脱 | 120 | 320（37.5%） | 68 s | 6.0 h |
| B 抓空 | 75 | 91（83%） | 45 s | 1.1 h |
| C 推偏 | 60 | 150（40%，待实测） | 50 s | 2.1 h |
| D 放偏 | 30 | 75（40%，待实测） | 70 s | 1.5 h |
| E 对照 | 15 | 45（33%） | 30 s | 0.4 h |
| **合计** | **300** | **~680** | — | **~11 h** |

| 场景 | 5060 单进程 | 4090 单进程（1.3×，估） |
|---|---:|---:|
| 理论（766 尝试） | 12.4 h | **9.5 h** |
| 含 20% 缓冲（920 尝试） | 14.9 h | **11.4 h** |

- 磁盘：h5 峰值 **~80 GB**，转完删掉后 LeRobot 常驻 **~7.3 GB**
- **如果要多机并行**：唯一验证过的方式是「多台单卡机，每台跑一个 shard」。
  4 台单卡 ≈ 1 台理论上能开 4 进程的机器，且没有 kit 锁问题（见 §8）

> C/D 两类的产出率**尚未实测**，表里是保守估计。真实值请用 `scripts/05_generate.sh --dry-run`
> 跑 20 次试探后再定条数。

---

## 5. 阻断级注意事项（不看会踩）

### 5.1 `grasp_label` 会把「空抓」标成「已握持」

现有标签器只用 `|p_ee − p_obj| < 5 cm` 判「手里有东西」。**空抓时手恰好贴着或围着物体，距离同样 < 5 cm**，
于是 21% 的帧被标成 `HELD`，而相机里那只手是空的。

| 场景 | 物体抬升 `rise_max` | 标签分布 |
|---|---|---|
| 真抓（26 段） | **≥ 93 mm** | `HELD` 100% |
| 空抓（7 段） | **≤ 17 mm** | `HELD` 21% + `SLIPPING` 57% + `LOST` 21% |

**修法**：加段级抬升先决，`rise < RISE_TOL` → 整段判 `LOST`。33 段全量双峰，**谷底宽 76 mm**，
阈值 20–90 mm 之间任取结论不变，工程取中位 **30 mm**。

```bash
python patches/apply_grasp_rise_patch.py --file ~/dynamicvla-training/official/utils/event_labels.py --apply
```

**这个 bug 在 DOM 上可忽略（200 条抽样只影响 1/210 段 = 0.5%，且那 20 帧全是修正），
但在自制 B 类数据上不能忽略** —— 空抓是 B 类要教的**系统性主模式**（5 条里 3 段）。
**先打补丁，再量产 B 类。顺序不能反。**

### 5.2 三个会静默毁掉批次的默认值陷阱

| 陷阱 | 现象 | 正确做法 |
|---|---|---|
| `REC_SLIP_MODE` 在 `place_recovery_sm.py` **默认 `open`**，而历史 pipeline 默认 `friction` | 造出来的滑脱在训练侧**完全不可见**（夹爪是张开的，标签只有 `lost`） | **显式** `export REC_SLIP_MODE=friction` |
| `--require_slip` 与 `--keep_imperfect` **互斥**（非叠加） | B 类只造抓空、不含 slip，被 `--require_slip` 整条丢掉 → 产出 0 | B/C/D/E 类一律 `--keep_imperfect` |
| `REC_SLIP=1` + `MISS_OFFSET>0` 会在一根轨迹里塞**两次异常** | 帧数拉到 1000（DOM 的 9 倍），30 s 预算内 0/9 全超时 | B 类**必须** `REC_SLIP=0` 单变量归因 |

全部 5 类配方都把参数写成 **`export` 显式赋值**，一个都不靠默认值，同时落 `run_config.json` 快照供闸门核查。
完整踩坑清单见 [`docs/06_troubleshooting.md`](docs/06_troubleshooting.md)。

### 5.3 资产不随 git 分发

上游 README 明确 `USD assets, logs and generated videos are intentionally excluded from Git`，
`git ls-files objects` 返回 0。必须走官方下载：

- [DOM Testing Set](https://gateway.infinitescript.com/?f=DOM-Test)
- [DOM 3D Objects](https://gateway.infinitescript.com/?f=DOM-3D-Objects)

解压后 `objects/` 340 MB（23 类 / 189 个 USD + 带 tags 的 `metadata.json`），
`scenes/` 1.7 GB（82 个 USD）。

---

## 6. 数据契约（与你现有加载器的对接点）

生成产物先落 **h5**（1 份 `*.h5` + 1 份 `*.json` + `events/*.jsonl`），再转 **LeRobot v2.1**。

h5 字段（`write_h5` 全量落盘，`compression=gzip`）：

| 键 | 形状 | 说明 |
|---|---|---|
| `action` | `(T, 8)` | `pos 3 + quat 4 + gripper 1`。⚠️ **夹爪在 index 7，不是 6** |
| `ee_pos` / `ee_quat` | `(T,3)` / `(T,4)` | 末端位姿 |
| `joints` | `(T,9)` | 7 关节 + 2 指 |
| `object_pos` / `object_quat` / `object_vel` | `(T,3)` / `(T,4)` / `(T,3)` | **物体真值**，`object_vel` 是高精度真值 |
| `sm_state` | `(T,)` | 状态机轨迹，`10=SLIP / 11=SETTLE / 12=RETRY / 13=GIVE_UP` |
| `{opst,side,wrist}_cam_rgb` | `(T,360,480,3)` | 3 路 RGB |
| `{opst,side,wrist}_cam_seg` | `(T,360,480,1)` | 语义分割 |

转换后 `action` 变 **7 列**（`pos 3 + rotvec 3 + gripper 1`），夹爪落到 **index 6**。
`sm_state` 会被转换器丢掉，所以 `annotate_recovery.py` 先把它挖出来写成
`<lerobot>/recovery_index.json` 侧车索引。

标签契约（22 类阶段 / grasp 3 类 / 15 维空间量）见 [`docs/04_label_contract.md`](docs/04_label_contract.md)。

---

## 7. 闸门（不过闸门不许进训练集）

`payload/qa_gates.py` 跑 5 道，设计原则是 **「用事实判定，不用声明判定」**——日志可能被重定向、也可能撒谎，机制不会。

| 闸 | 判据 | 为什么这么定 |
|---|---|---|
| G1 配置闸 | `friction` 模式 | 由 G2 反推：夹爪全程闭合而物体确实滑出 ⟹ 物理上只可能是 friction |
| G2 机制闸 | 滑脱窗口内夹爪命令**恒为 −1** | 张开就是不 `open` 模式，滑脱对训练不可见 |
| G3 几何闸 | 滑脱后出现连续 **≥10 帧** `|p_ee−p_obj| > 5cm` | 证明物体真的离开了手 |
| G4 标签闸 | `lost` 占比非 0 非退化 | 全 `held` 说明整批没造出事故 |
| G5 可复现闸 | `run_config.json` 存在且字段 ≥8 | 参数快照，防止「日志在别处」导致静默失配 |

A 类实测：**G2 9/9 PASS、G3 9/9 PASS、G4 lost=33.27% PASS**，事故类标签 `slipping+lost = 49.35%`（DOM 1.34%）。

---

## 8. 已验证的东西 / 未验证的东西

**已实测（2026-11-02，RTX 5060 8G）：**

- ✅ A 类 friction：24 尝试 → 9 条，**产出率 37.5%**，9/9 完整走 `10→11→12`，全部以 `sm_state=9` 结束
- ✅ B 类抓空：6 尝试 → 5 条，**产出率 83%**，帧数中位 169，5/5 成功放置
- ✅ 转 LeRobot 零丢帧（2129 → 2129），改造后 `server_fullft` 加载器 **14 PASS / 0 FAIL**
- ✅ `grasp_label` 补丁在 DOM 200 条抽样上只影响 1/210 段，20 帧变化全是修正
- ✅ D 类可行性：`TO_TARGET` 状态里已有 `is_placed >= 5 → RESET` 重试路径

**未实测（勿当结论用）：**

- ⚠️ **4090 并发度**（§4.2 全是推算）
- ⚠️ C 类扰动、D 类放偏的实际产出率（配方已写，未跑量）
- ⚠️ 多进程同时跑 Isaac Sim 的稳定性（单次测试中 2 进程共存显存正常，但未跑完一组完整对照）
- ⚠️ 生成器对官方 `simulate.py` 的**像素级忠实度**（同种子 A/B，PSNR ≥ 40 dB —— 待做）

---

## 9. 许可

- 本仓库**自有代码**（`payload/gen_recovery.py`、`payload/place_recovery_sm.py`、`scripts/`、`patches/`、
  `annotate_recovery.py`、`qa_gates.py`）：**MIT**。
- 上游 [hzxie/DynamicVLA](https://github.com/hzxie/DynamicVLA) 及其资产：**S-Lab License 1.0，
  仅限非商业用途**。本仓库不重新分发任何上游源码或 USD 资产，只提供下载与注入脚本。
- Isaac Sim / Isaac Lab：NVIDIA 许可，需自行安装。

---

## 10. 引用

如果你用到这套数据或生成器：

```bibtex
@misc{dynamicvla-recovery-data,
  title  = {dynamicvla-recovery-data: A single-GPU factory for slip / miss / disturbance recovery trajectories},
  year   = {2026},
  note   = {Built on DynamicVLA (S-Lab, NTU). Non-commercial use only.}
}
```