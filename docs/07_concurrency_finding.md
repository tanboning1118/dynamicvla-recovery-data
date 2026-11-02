# 07 · 单卡并发实测：一次被推翻的推算（附完整数据）

> **本文是一次诚实的记录。我原本推算 4090 上能开 4 进程，实测证明单卡只能串行跑 1 个 Isaac Sim 实例。**
> 这条经验对任何想靠多进程加速 Isaac Sim 数据生成的人都有用。

---

## 1. 原推算（错的）

| 假设 | 值 | 依据 |
|---|---|---|
| 单环境显存峰值 | 5.66 GB | 实测 |
| 4090 显存 | 24 GB | 规格 |
| → 并发上限 | `(24 − 1.2) / 5.66 ≈ 4` | 纯显存算术 |
| 单进程不因 4090 变快 | ×1.0 | 瓶颈是 USD 解析与 Python 调度 |
| → 300 条耗时 | 12.4 h / 4 / 0.8 ≈ **3.9 h** | 线性外推 × 0.8 折扣 |

看起来无懈可击。**但它错在把「显存能装下」等同于「实例能启动」。**

---

## 2. 实测一：同时启动 2 个实例

**配置**：`RTX 5060 8 GB`，两个 `gen_recovery.py` 同时 `&` 后台启动，各 3 次尝试。

| 进程 | 结果 |
|---|---|
| A（seed 600） | 正常，跑完 2 条，第 3 次超时。**耗时 515 s** |
| B（seed 700） | **完全卡死**，日志 133 行后停住，一行 `Running simulation` 都没有 |

**关键观察**：
- B 进程**不是 OOM**，也不是崩溃 —— 它只是永远停在初始化
- 两进程合计显存只有 **4.9 GB**（远未到 8 GB 上限）
- A 进程完成时间 **515 s**，而单进程跑同样 3 次只要 **121 s** → **慢 4.3 倍**

### 2.1 卡死位置的精确诊断

```bash
$ nvidia-smi --query-compute-apps=pid,used_memory --format=csv
pid, used_gpu_memory [MiB]
1276336, 821 MiB          ← B 进程只申请到 821 MiB

$ tail -1 /tmp/bench2b.log
[Warning] Client omni.physx.plugin has acquired [omni::fabric::IStageReaderWriter v0.12]
          100 times. Consider accessing this interface with carb::getCachedInterface()
```

B 进程卡在 **`omni.physx` / `omni.fabric` 初始化**，从未进入 RTX 渲染器
（正常实例此时显存应该已到 4–5 GB）。

日志里有明确线索：

```
[Warning] [omni.kvdb.plugin] Disabling key-value database because
          another kit process is locking it
```

**Omniverse 的 kit 运行时是进程间独占的**。第二个实例拿不到 `omni.kvdb` 与
`omni.fabric` 的全局锁，就卡在等锁，且**不报错、不超时、不退出**。

---

## 3. 实测二：错峰 60 s 启动

假设是「同时启动撞车」，那么错开就好了。**测试**：A 先跑，60 s 后再起 B。

| 进程 | 结果 |
|---|---|
| A（seed 800） | 正常，3 次全跑完，**115 s** |
| B（seed 900） | **仍然卡死**，显存停在 821 MiB，0 条产出 |

**结论：不是启动时序问题，是运行时独占。** 只要 A 还在跑，B 就进不去。

---

## 4. 实测三：隔离缓存目录

假设是缓存锁争抢，那就给每个进程独立的 `XDG_CACHE_HOME` / `OV_CACHE_PATH` /
`CUDA_CACHE_PATH` / `XDG_DATA_HOME` / `XDG_CONFIG_HOME`。

**结果**：两个进程**都卡死**，显存分别停在 **821 / 323 MiB**，0 条产出。

隔离缓存不仅没解决，还让两个都挂掉（清空缓存后重建反而更容易撞在同一个初始化阶段）。

---

## 5. 三组实测汇总

| 实验 | 配置 | 进程 A | 进程 B | 有效吞吐 |
|---|---|---|---:|---:|
| 基线 | 单进程 3 次 | ✓ 121 s / 1 条 | — | **2.5 条/min** |
| 同时启动 | 2 进程同时 | ✓ 515 s / 2 条 | ✗ 卡死 / 0 条 | **0.23 条/min** |
| 错峰启动 | 2 进程隔 60 s | ✓ 115 s / 3 条 | ✗ 卡死 / 0 条 | 0.9 条/min（瞬时） |
| 隔离缓存 | 2 进程隔 20 s + 独立 XDG | ✗ 卡死 | ✗ 卡死 | **0** |

**结论：在这台机器上，多进程 = 零收益甚至负收益。**

---

## 6. 为什么会这样（机制解释）

Isaac Sim 4.5 基于 Omniverse Kit。Kit 的多项核心服务是**全局单例**：

| 服务 | 锁 | 抢占后果 |
|---|---|---|
| `omni.kvdb` | 键值数据库文件锁 | 后启动者只用内存态，行为未定义 |
| `omni.fabric` | GPU Fabric Realm | **后启动者卡死**（实测） |
| `omni.physx` | PhysX context | 同上 |
| `rtx.scenedb` | TLAS 缓冲 | 显存分配冲突 |
| `omni.localcache` | 本地缓存 | 只影响首次启动速度 |

官方只支持**一个进程内多环境**（`--num_envs N`），而我们的恢复状态机有
`assert num_envs == 1`。两条路都堵住了：

```
多环境？  ✗ assert num_envs == 1（我们自己的实现约束）
多进程？  ✗ kit 全局锁独占（Isaac Sim 的硬约束）
```

> **这里必须说清归属**：多进程那条路是 Isaac Sim 的**硬约束**，谁也绕不过去。
> 多环境那条路是**我们自己的实现选择** —— 上游 `place_sm.py` 本身就是向量化的
> （全程 `[:, ...]` 批量操作，支持 `num_envs > 1`）。我们改写 `place_recovery_sm.py`
> 时为了简化，把多环境砍成了单环境。
>
> 所以「单卡只能串行」这个结论，一半是平台的，一半是我们自己的。**要真正拿到
> `num_envs` 的加速，必须把恢复状态机重新向量化** —— 这是一条我们没走通、
> 但并非走不通的路。诚实起见，把它列为可行性待验证项，而不是声称「不可能」。

多环境向量化的难点（供后来者参考）：

| 难点 | 为什么难 | 可能的解法 |
|---|---|---|
| 每环境独立重试计数 | `attempt` / `_grasp_misses` 是 Python 标量 | 改成 `torch.Tensor(num_envs)` |
| 碰撞事件判定 | 需要读刚体句柄 | 用 `isaaclab` 的 `ContactSensor` 批量订阅 |
| 抓取成功判定 | 逐环境分支 | `torch.where` 全批量，用 mask 累计 |
| 摩擦/配重注入 | 逐环境设置物理材质 | `RigidPrim` 批量 `set_material_props` |

工作量估计：**2–4 天**（含正确性验证）。收益：单卡吞吐 ×N，且没有 kit 锁问题
（单进程 N 环境只有一个 Kit 实例）。**在租 4 台机器之前，这条路值得先试。**

---

## 7. 那 4090 还有意义吗

**有，但意义不是「开更多进程」。**

### 7.1 4090 真正能帮上的地方

| 维度 | 8 GB（5060） | 24 GB（4090） | 对生成的影响 |
|---|---|---|---|
| 显存峰值 | 5.66 GB | 同样 5.66 GB | **无差别**（单实例） |
| 单 episode 帧数上限 | 受显存约束 | 可开更高分辨率 / 更多相机 | **能提高数据质量**，不能提速 |
| 光栅吞吐 | 入门 | 完整 AD102 | **单进程可能快 1.2–1.5×** |
| 显存带宽 | 448 GB/s | 1008 GB/s | 图像回读更快 |

**唯一确定的收益**：单进程略快（估 1.2–1.5×，需实测）+ 能开更高分辨率。

### 7.2 修正后的时间估算

按单进程 68 s/尝试（A 类）与 **1.3×** 加速：

| 场景 | 5060 单进程 | 4090 单进程（估） |
|---|---:|---:|
| A 类 352 尝试 | 6.6 h | **5.1 h** |
| 全部 766 尝试 | 12.4 h | **9.5 h** |
| 含 20% 缓冲 | 14.9 h | **11.4 h** |

> 这是**串行**的数字。原来的「4 进程 3.9 h」不再成立。

### 7.3 如果一定要并发，唯一可能的路

**用多台单卡机器，每台跑一个进程。** 这是本方案唯一验证过的扩展方式。

```bash
# 每台机器跑一个 shard
bash scripts/05_generate.sh --profile profiles/a_slip.env --n 88 --seed 9000  # 机器1
bash scripts/05_generate.sh --profile profiles/a_slip.env --n 88 --seed 9200  # 机器2
...
```

4 台单卡 ≈ 1 台能开 4 进程的机器（如果能开的话）。成本上反而不亏：
4 × 4090 单卡机 ≈ 1 台 4 卡机，但前者没有 kit 锁问题。

---

## 8. 写进仓库的硬约束

`scripts/04_benchmark_single_card.sh` 已按实测结论调整：

| 项 | 原设计 | 现设计 |
|---|---|---|
| 默认 `--max-proc` | 4 | **2**（只做验证，不指望加速） |
| 并发启动方式 | 同时 | **强制错峰 `--stagger 15`** |
| 组二判定 | `linearity ≥ 0.70` 即可用 | **额外要求「所有进程都产出 > 0」**，否则判死锁 |
| 建议输出 | 推荐 BN | **若所有 N≥2 都死锁，明确打印「本机只能串行」** |

`scripts/06_run_campaign.py`：

| 项 | 设计 |
|---|---|
| `--bn` 默认 | **1** |
| `--stagger` 默认 | 15 s（`bn>1` 时强制） |
| `--isolate-cache` | 保留，但注释写明「实测无效，仅为排除干扰保留」 |
| 死锁检测 | 单 chunk 超过 `--deadlock-timeout`（默认 900 s）无任何 h5 产出 → 判死锁，杀掉并告警 |

---

## 9. 一条方法论教训

我犯的错是**只做了资源算术，没做启动验证**。

「24 GB / 5.7 GB = 4」这个计算里没有任何一步是错的，但它假设了
**「资源够 = 能启动」**。对一个 GPU 应用，这个假设经常不成立，因为：

- 显存够不代表 context 能建（全局锁）
- 进程间共享的资源（db、fabric、shader cache）不按显存计费
- 卡死往往**不报错**，表现为「永远停在初始化」

**正确顺序**：先在目标机器上实测「N 个进程能不能同时活着」，再算吞吐。
`04_benchmark_single_card.sh` 的组二就是为此存在的 —— **它的第一职责不是测吞吐，
而是测「能不能跑」**。

---

## 10. 复现命令

```bash
# 组一：单进程基线
bash scripts/04_benchmark_single_card.sh --rounds 3 --max-proc 1

# 组二：并发扫描（会看到 N≥2 全部死锁）
bash scripts/04_benchmark_single_card.sh --rounds 3 --max-proc 2 --skip-soak

# 手工验证一个具体配置
cd ~/dynamicvla-demo/dynamic-vla
export REC_SLIP=0 REC_GRASP_CHECK=0 REC_MISS_OFFSET=0 REC_DISTURB_AT=-1
PY=~/miniconda3/envs/dvla-isaac45-blackwell/bin/python
$PY -u simulations/gen_recovery.py --task place --robot franka -n 3 --seed 600 \
   --save --enable_cameras --headless --disable_fabric --keep_imperfect \
   --episode_length 30 -o /tmp/pa > /tmp/a.log 2>&1 &
sleep 60
$PY -u simulations/gen_recovery.py --task place --robot franka -n 3 --seed 700 \
   --save --enable_cameras --headless --disable_fabric --keep_imperfect \
   --episode_length 30 -o /tmp/pb > /tmp/b.log 2>&1 &

sleep 180
# 判据：nvidia-smi 里应有两个 >3 GB 的进程；若有一个只有 <1 GB，就是死锁
nvidia-smi --query-compute-apps=pid,used_memory --format=csv
```

---

## 11. 环境备注

- **工位机 SSH 在实测期间断了 3 次**（`tailscale relay hkg`，22 端口超时），
  每次自行恢复 5–10 分钟。所有生产脚本用 tmux + `_DONE` 哨兵，断线不影响生成。
- 死锁进程**不会响应 SIGTERM**（卡在 C++ 层等待，Python 信号处理没机会跑），
  `pkill -9 -f gen_recovery` 是唯一干净的清理方式。`-9` 之后显存可能延迟 10–20 s 才释放。
  数据目录里的 `_DONE` 不会生成，所以外部脚本能靠「会话存活但无 `_DONE`」识别死锁。
- `--num_envs > 1` 在恢复状态机上会直接 `AssertionError`，不是静默降级 ——
  这是我们刻意留的（宁可崩掉也不要产出静默错误的物理状态）。
- 上游 `place_sm.py` 本身支持 `num_envs > 1`（全程向量化）。单环境是我们改写的
  代价，不是上游的限制 —— 见 §6 的「多环境向量化」待验证项。

---

## 12. 🔴 2026-11-02 复核：4090 24GB 上双进程**可以**并行

**上文 §2–§5 的结论（"单卡只能串行"）在 RTX 5060 8GB 上成立，但不能外推到 4090 24GB。**
在阿里云 RTX 4090（24GB）实例上，双进程并行**实测跑通并持续产出**：

| 项 | 5060 8GB（上文实测） | **4090 24GB（本次实测）** |
|---|---|---|
| 双进程 | ✗ 第二个卡死在 `omni.fabric` 初始化（821 MiB） | ✅ **两个都在产 h5** |
| 显存 | 4.9 GB（远未满仍死锁） | **8.9 + 10.1 ≈ 19 GB** |
| GPU 利用率 | — | **33%**（单进程仅 17%） |
| 吞吐 | 2 进程 = 0 收益 | **~28 s/attempt（合计），接近线性** |
| 同批产出 | — | b/c 两 worker 各 7 个 episode（seed 互不重叠） |

**为什么这次能行**（相对可能的解释，尚未逐项做对照实验）：

1. **显存余量足够**：5060 只有 8 GB，双实例时第二个进程连 1 GB 都拿不到就卡在 fabric 初始化；
   4090 上两个实例各 9~10 GB 顺利建完 context。
2. **启动时序**：本次为错峰启动（worker b 已在产数据后才起 worker c），
   且 `omni.kvdb` 锁只是「另一个进程锁了就禁用 kvdb」，并不阻塞主流程。
3. **FSD 未激活**：本次运行在 `isaacsim_4_5` 配套 kit（无 `app.useFabricSceneDelegate`）下，
   Fabric Realm 不参与，绕开了 §6 里最容易抢锁的那一环。

**可操作结论（4090 专用）**：
- 4090 24 GB：**双进程 = 安全上限**（19 GB），三进程预计 28 GB+ 必 OOM
- 8 GB 级卡：仍按上文结论按串行设计，或改用多机分片（§7.3）
- 部署脚本：`scripts/21_parallel_worker.sh`（单 worker）+ `scripts/22_supervisor_4090.sh`（双 worker 滚动）
- 完整实测数据见 [`08_4090_parallel.md`](08_4090_parallel.md)

> **方法论补充**：§9 说的「先在目标机器上实测」这次再次应验 ——
> 只是方向反过来：**别把一台机器的死锁结论当成平台定律**。
> 每次换卡、换驱动、换 kit 配置，都要重跑一次双进程验证。