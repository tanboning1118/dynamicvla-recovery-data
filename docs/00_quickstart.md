# 00 · 快速开始（4090 + Isaac Sim 4.5 从零到第一条数据）

> 目标机器：**RTX 4090 24 GB / Ubuntu 22.04 / 已装 NVIDIA 驱动（≥ 535，CUDA 12.x）**
> 全程约 2–3 小时（其中 1.5 小时是下载与编译等待）。

---

## 0. 先说三件最容易翻车的事

| # | 坑 | 后果 |
|---|---|---|
| 1 | **4090 是 Ada `sm_89`，不是 Blackwell** | 从别的机器拷 conda env 会直接 `no kernel image is available`。必须重建 |
| 2 | **资产不随 git 分发**（上游明确 excluded） | `objects/` 和 `scenes/` 是空的，跑起来报 `no USD found` |
| 3 | **`REC_SLIP_MODE` 默认是 `open` 不是 `friction`** | 造出来的滑脱在训练侧 100% 不可见 |

---

## 1. 基础环境检查

```bash
nvidia-smi                      # 驱动 ≥ 535，CUDA Version ≥ 12.2
nvidia-smi --query-gpu=name,memory.total,compute_cap --format=csv
# 期望：NVIDIA GeForce RTX 4090, 24564 MiB, 8.9

free -g                         # 建议 ≥ 64 GB（4 进程要 34 GB）
nproc                           # 建议 ≥ 16
df -h /data                     # 输出盘建议 ≥ 100 GB 本地 SSD
python3 -V                      # 需要 3.10
```

**4090 的 compute capability 是 8.9**。Isaac Sim 4.5 自带的 torch 是 cu12 轮子，
`sm_89` 在支持列表里，**不需要**特殊处理。但如果你从 Blackwell 机器（`sm_120`）复制过环境，
那个 torch 是 cu128 专门编的，在新机器上会报错——重建。

---

## 2. 装 Isaac Sim 4.5 + Isaac Lab 2.2.1

### 2.1 创建 conda 环境

```bash
conda create -y -n dvla-isaac45 python=3.10
conda activate dvla-isaac45

# Isaac Sim 4.5 通过 pip 装（需要 pip ≥ 24）
pip install --upgrade pip
pip install "isaacsim[all]==4.5.0.0" \
            --extra-index-url https://pypi.nvidia.com

# 验证
python -c "import isaacsim; print(isaacsim.__version__)"   # 4.5.0.0
```

> `isaacsim[all]` 约 6 GB，装 10–20 分钟。如果只要 headless 生成，可以只装
> `isaacsim` 核心包，但 `isaacsim-replicator` 与 `isaacsim-sensor` 是必需的
> （相机采集靠它们）。

### 2.2 装 Isaac Lab 2.2.1

```bash
cd ~
git clone --depth 1 --branch v2.2.1 https://github.com/isaac-sim/IsaacLab.git IsaacLab-2.2.1
cd IsaacLab-2.2.1
./isaaclab.sh --install
```

> 这一步会编译 warp kernel，约 15–30 分钟。`--install` 会往当前 conda 环境装
> `isaaclab 0.45.9` / `isaaclab_tasks 0.10.47`。

### 2.3 验证版本组合

```bash
python - <<'PY'
import torch, isaacsim
print("torch       ", torch.__version__)              # 期望 2.7.1+cu128 或 2.7.x+cu12x
print("cuda        ", torch.version.cuda)             # 12.x
print("isaacsim    ", isaacsim.__version__)           # 4.5.0.0
print("capability  ", torch.cuda.get_device_capability(0))   # (8, 9) 对 4090
PY
```

**已知可用的组合**（在 RTX 5060 上实测）：

```
Python 3.10.22 / torch 2.7.1+cu128 / CUDA 12.8
isaacsim 4.5.0.0 / isaaclab 0.45.9 / isaaclab_tasks 0.10.47
warp-lang 1.8.1 / h5py 3.16.0
```

---

## 3. 拉上游代码

```bash
git clone https://github.com/hzxie/DynamicVLA.git ~/dynamicvla-demo/dynamic-vla
cd ~/dynamicvla-demo/dynamic-vla
git checkout a27a06d2ca74d0e987a5e552e01013073e93cfd8   # ← 固定 commit，不要跟 main
```

> 本仓库的注入脚本按 `a27a06d` 的接口写的。上游后续若改了 `simulate.py` 的签名，
> `gen_recovery.py` 会挂。**锁 commit**。

### 3.1 装上游依赖

```bash
pip install -r ~/dynamicvla-demo/requirements-sim.txt
```

关键是 `h5py`（写 h5）、`pyyaml`（读 sim_cfg）、`opencv-python`（转视频）。

---

## 4. 下载资产（约 2 GB）

```bash
mkdir -p ~/Downloads/dom-assets && cd ~/Downloads/dom-assets

# 浏览器打开这两个链接下载：
#   https://gateway.infinitescript.com/?f=DOM-Test
#   https://gateway.infinitescript.com/?f=DOM-3D-Objects

ls -lh   # 应看到 DOM-Test.zip 与 DOM-3D-Objects.zip
```

解压到上游根目录：

```bash
cd ~/dynamicvla-demo/dynamic-vla
unzip -q ~/Downloads/dom-assets/DOM-Test.zip
unzip -q ~/Downloads/dom-assets/DOM-3D-Objects.zip
```

解压后应该是：

```
~/dynamicvla-demo/dynamic-vla/
├── objects/         340 MB, 23 类目录 / 189 个 .usd + metadata.json
└── scenes/          1.7 GB, 82 个 .usd
```

**校验**：

```bash
ls objects | wc -l            # 23
ls objects/apple | wc -l      # 21
ls scenes/*.usd | wc -l       # 82
python -c "import json;print(len(json.load(open('objects/metadata.json'))))"   # 189
```

> ⚠️ 上游 `gen_recovery.py` 默认 `--object_dir ../objects` / `--scene_dir ../scenes`，
> 是相对 `simulations/` 的。所以 **objects/scenes 必须在上游根目录**，不是 `simulations/` 里面。

---

## 5. 注入生成器与补丁

```bash
# 从本仓库拷两个文件进上游
cp payload/simulations/gen_recovery.py                        ~/dynamicvla-demo/dynamic-vla/simulations/
cp payload/simulations/state_machines/place_recovery_sm.py    ~/dynamicvla-demo/dynamic-vla/simulations/state_machines/

# 或者用脚本
bash scripts/02_patch_upstream.sh --root ~/dynamicvla-demo
```

### 5.1 打 `grasp_label` 补丁（服务端训练代码）

这一步**不在仿真机上**，在你训练用的机器上：

```bash
python patches/apply_grasp_rise_patch.py \
    --file ~/dynamicvla-training/official/utils/event_labels.py \
    --check          # 先看会不会改，不落盘

python patches/apply_grasp_rise_patch.py \
    --file ~/dynamicvla-training/official/utils/event_labels.py \
    --apply          # 真正落盘（自动备份 .bak）
```

> 为什么要打：`grasp_label` 只用 5 cm 距离判持有，**空抓时手贴着物体，距离也在 5 cm 内**
> → 21% 的帧被标成 `HELD`，而手里是空的。B 类数据全靠这个标签教模型，不打补丁等于教错。

---

## 6. 第一次跑（单条，验证链路）

```bash
cd ~/dynamicvla-demo/dynamic-vla
source ~/miniconda3/etc/profile.d/conda.sh && conda activate dvla-isaac45

export REC_SLIP=0 REC_GRASP_CHECK=0 REC_MISS_OFFSET=0 REC_DISTURB_AT=-1

python simulations/gen_recovery.py \
    --task place --robot franka \
    -n 2 --seed 500 --save --enable_cameras --headless --disable_fabric \
    --keep_imperfect --episode_length 30 \
    -o /tmp/first_try
```

期望：**~120 s**，`/tmp/first_try/*.h5` 出现 1–2 个，每个 100–200 MB。

```bash
ls -lh /tmp/first_try/*.h5
cat /tmp/first_try/_DONE          # {"attempts":2,"saved":1,...}
```

**如果 `saved: 0`** —— 正常，baseline place 成功率约 37%，跑 2 次全失败不奇怪。
看 `no placement within 30 s` 就是超时，加 `-n` 再试。

---

## 7. 产能实测（决定开几个进程）

```bash
bash scripts/04_benchmark_single_card.sh --root ~/dynamicvla-demo --rounds 5
```

它会依次跑 1 / 2 / 3 / 4 进程，各 5 次尝试，输出：

```
concurrency  total_s  saved  per_ep_s  gpu_peak_MiB  rss_peak_GB  linearity
1            121      1      121       5660          8.4          1.00
2            ...      ...    ...       ...           ...          ...
4            ...      ...    ...       ...           ...          ...
```

**判据**：`linearity = (concurrency=1 的 per_ep_s) / (N 的 per_ep_s)`。
N=4 时 `linearity ≥ 2.8` 才算可用，否则降并发。

---

## 8. 小批试产（每类 2 条）

```bash
for p in a_slip b_miss c_disturb d_place_miss e_control; do
    bash scripts/05_generate.sh --profile profiles/$p.env --n 4 --seed 9000
done
```

约 15 分钟。然后逐批过闸门：

```bash
for b in ~/dvla-gen/*/; do
    bash scripts/08_qa_gates.sh "$b"
done
```

---

## 9. 转 LeRobot 并验收

```bash
bash scripts/07_convert_lerobot.sh --base ~/dvla-gen/a_slip --repo local/a_slip
```

输出：`~/dvla-gen/a_slip/lerobot_home/local/a_slip/`（`meta/info.json` + `data/` + `videos/`）。

**验收**（用你改造后的 `server_fullft` 加载器）：

```bash
python scripts/09_verify_loader.py --dataset ~/dvla-gen/a_slip/lerobot_home/local/a_slip
```

期望 14 项全 PASS（结构 12 + 标签 2）。清单见 [`docs/04_label_contract.md`](04_label_contract.md) §4。

---

## 10. 正式量产

```bash
# 按 README §4 推算的并发度（上机实测后定）
BN=4 CHUNK=20 bash scripts/06_run_campaign.py \
    --profiles a_slip:352 b_miss:117 c_disturb:165 d_place_miss:83 e_control:49 \
    --seed-base 9000
```

它会：
- 为每个 profile 起一条 chunk 队列
- 维持 `BN` 个并发进程，进程退出即重启（兜 RSS 泄漏）
- 监控显存 / 内存 / 磁盘，超阈值暂停新 chunk
- 全部结束自动转 LeRobot + 过闸门

---

## 11. 收工

```bash
bash scripts/10_report.py --base ~/dvla-gen       # 汇总各类产出率、帧数、标签分布
tar czf recovery_dataset_$(date +%m%d).tgz ~/dvla-gen/*/lerobot_home
```

**别忘了删 h5**：330 条约 80 GB。

```bash
find ~/dvla-gen -name '*.h5' -delete
```