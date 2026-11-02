# 06 · 踩坑清单

按**症状**检索，不按原因归类 —— 出事的时候你知道自己看到什么，不知道根因。

每条都标了实测来源：
- **【实测】** 我们在工位机（RTX 5060 8G）或 DSW（8×A100）上真实撞到并解决了
- **【推断】** 从代码/文档推导，未亲自复现，但机理明确
- **【上游】** 上游仓库已知问题，不是本仓库引入的

---

## 快速索引

| 症状 | 跳转 |
|---|---|
| Isaac Sim 启动到一半不动 / 显存只占 800 MiB | [T1](#t1-第二个-isaac-sim-实例卡死在-kit-初始化) |
| `assert num_envs == 1` | [T2](#t2-num_envs--1-断言失败) |
| 造出来的滑脱在训练里「看不见」 | [T3](#t3-滑脱数据造出来了但训练侧完全不可见) |
| B 类一条都没产出 | [T4](#t4-b-类产出率为-0) |
| episode 帧数暴涨到 1000 | [T5](#t5-episode-帧数暴涨到-1000) |
| 空抓手被标成「已握持」 | [T6](#t6-空抓被标成已握持) |
| 转换后标签全没了 / 全是 0 | [T7](#t7-转换后事件标签全丢) |
| 夹爪值不是 ±1 | [T8](#t8-夹爪值不是-1) |
| 抽了 200 条发现「零变化」，以为是好消息 | [T9](#t9-dom-抽样得出零变化的假结论) |
| 单个 episode 越跑越慢 / 内存涨到 20 GB | [T10](#t10-内存随时间单调上涨) |
| 改了 `event_labels.py` 后训练起不来 | [T11](#t11-改了代码后训练拒绝启动) |
| 训练 loss 变 NaN / 梯度爆炸 | [T12](#t12-loss-nan) |
| `pkill` 之后进程还在 | [T13](#t13-pkill-杀不掉) |
| 补丁打完 git diff 变成全文重写 | [T14](#t14-补丁后-git-diff-全文重写) |
| h5 存到网络盘上慢得离谱 | [T15](#t15-网络盘上-io-慢) |
| 报警告 `ossfs` / `nfs` 但不知道要不要管 | [T16](#t16-磁盘类型告警) |

---

## T1 第二个 Isaac Sim 实例卡死在 kit 初始化

**症状**

```
$ nvidia-smi --query-compute-apps=pid,used_memory --format=csv
pid, used_gpu_memory [MiB]
12345, 5660 MiB     ← 正常那个
12346, 821 MiB      ← 卡死的
```

第二个进程占 821 MiB 就再也不动了。日志停在 `omni.physx` 之后。不报错、不超时、永不退出。

**原因**【实测】

Omniverse Kit 的**全局单例锁**（`omni.kvdb` / `omni.fabric` / `omni.physx`）。
同一台机器上第二个 Kit 进程会在初始化阶段等待一个永不释放的锁。

**验证方法**（40 秒就有结论）

```bash
PY=~/venvs/isac/bin/python
$PY payload/simulations/gen_recovery.py --n 1 --seed 1 -o /tmp/pa > /tmp/a.log 2>&1 &
sleep 60
$PY payload/simulations/gen_recovery.py --n 1 --seed 2 -o /tmp/pb > /tmp/b.log 2>&1 &
sleep 180
nvidia-smi --query-compute-apps=pid,used_memory --format=csv
# 有一个 < 1 GB 就是死锁，别等它
pkill -9 -f gen_recovery
```

**不要试这些**（全部实测无效）

| 尝试 | 结果 |
|---|---|
| 错峰 60 s 启动 | ✗ 仍然卡死 |
| 隔离 `XDG_CACHE_HOME` + `OV_CACHE_PATH` | ✗ 两个都卡死 |
| `--num_envs 2` 单进程多环境 | ✗ `assert num_envs == 1`，见 T2 |
| 加显存 / 换 4090 | ✗ 是锁不是资源 |

**唯一可行**：多台单卡机，每台跑一个 shard。

```bash
# 机器 1
python scripts/06_run_campaign.py --profiles a_slip:120 --seed-base 9000   --out-root ~/dvla-gen
# 机器 2
python scripts/06_run_campaign.py --profiles b_miss:75  --seed-base 12000  --out-root ~/dvla-gen
```

h5 按 seed 命名，天然不冲突。完整分析见 [`07_concurrency_finding.md`](07_concurrency_finding.md)。

---

## T2 `num_envs == 1` 断言失败

**症状**

```
AssertionError: assert num_envs == 1
```

**原因**【实测】

`PlaceRecoveryStateMachine` 逐 Python step 实现，每一帧都要读写刚体句柄，
无法向量化到 N 个环境。这是**设计约束**，不是 bug。

**处理**

- 不要试图改这条断言绕过（会有物理状态串台，数据静默出错）
- 想要吞吐只能横向扩机器（见 T1）

---

## T3 滑脱数据造出来了但训练侧「完全不可见」

**症状**

A 类数据跑完了，`qa_gates` 说滑脱段存在，但训练日志里 `event_grasp` 的
`SLIPPING` 帧数几乎为 0。

**原因**【实测】

`place_recovery_sm.py` 里 `REC_SLIP_MODE` **默认是 `open`**，
而历史 pipeline 默认 `friction`。`open` 模式下夹爪在滑脱瞬间是**张开的**，
标签器只看距离 → 物体离开 5 cm 就判 `LOST`，永远不产生 `SLIPPING`。

**处理**

```bash
export REC_SLIP_MODE=friction    # ★ 必须显式写
```

`profiles/a_slip.env` 里已显式赋值。`scripts/05_generate.sh` 会在检测到
「A 类但 mode≠friction」时直接 `die`。

**自检**：`run_config.json` 里应该有 `"REC_SLIP_MODE": "friction"`。

---

## T4 B 类产出率为 0

**两个独立原因，都会导致 0 产出**

### 原因一：`--require_slip` 把 B 类的数据全丢了

`--require_slip` 与 `--keep_imperfect` 是**互斥**的，不是叠加。

B 类只造「抓空」，本来就不含 slip。加了 `--require_slip` 之后，每条都被判定为
「不含滑脱」→ 整条丢弃 → 0 条。

| 类 | 该用哪个 |
|---|---|
| A | `--require_slip` |
| B / C / D / E | `--keep_imperfect` |

### 原因二：`REC_SLIP=1` + `MISS_OFFSET>0` 双异常

B 类需要 `MISS_OFFSET`。但如果在 `REC_SLIP=1` 的同时设 `MISS_OFFSET>0`，
一根轨迹里会塞**两次异常**（先滑脱、再抓空）。

```python
# place_recovery_sm.py
slip_armed = SLIP_ENABLED and self.attempt == 0 and not self.slip_done
```

`MISS_OFFSET` 只加 `_grasp_misses`，**不动 `attempt`**。所以第一次尝试会先走滑脱路径，
之后才走抓空路径 —— 结果帧数拉到约 1000 帧（DOM 的 9 倍），30 s 预算内
**实测 0/9 全部超时**。

**处理**

```bash
export REC_SLIP=0               # ★ B 类必须关
export REC_GRASP_CHECK=1
export REC_MISS_OFFSET=0.05
export GEN_EXTRA_FLAGS="--keep_imperfect"
```

`profiles/b_miss.env` 已固定这组值，`05_generate.sh` 会拦。

---

## T5 episode 帧数暴涨到 1000

见 [T4 原因二](#原因二rec_slip1--miss_offset0-双异常)。单变量原则：一次只注入一种异常。

改完先跑 2 条看长度：

```bash
python scripts/10_report.py --base ~/dvla-gen    # 看 len_max 列
```

`len_max > 600` 基本就是双异常或状态机没退出。

---

## T6 空抓被标成「已握持」

**症状**

B 类数据里，明明相机拍到机械手悬在物体旁边夹了个空，标签却是 `HELD`（`event_grasp=0`）。
模型学到「手贴过去就算抓住了」。

**原因**【实测】

`grasp_label` 只用 `|p_ee − p_obj| < 5 cm` 判「手里有东西」。
空抓闭合时手**恰好环绕物体**，距离同样 < 5 cm。

实测（2026-11-02，工位机，33 个 hold 段）：

| 类别 | 段数 | `rise_max` | 标签分布 |
|---|---:|---|---|
| 真抓 | 26 | **≥ 93 mm** | `HELD` 100% |
| 空抓 | 7 | **≤ 17 mm** | `HELD` 21% / `SLIPPING` 57% / `LOST` 21% |

三个物理空抓段分别有 21% / 21% / 18% 的帧错标 `HELD`。

**处理**

```bash
# 1) 先看会改什么（不落盘）
python patches/apply_grasp_rise_patch.py \
    --file ~/dynamicvla-training/official/utils/event_labels.py --check

# 2) 落盘（自动备份 .bak_<时间戳>，语法检查失败自动回滚）
python patches/apply_grasp_rise_patch.py \
    --file ~/dynamicvla-training/official/utils/event_labels.py --apply

# 3) 在 DOM 上做零副作用校验
python patches/apply_grasp_rise_patch.py \
    --file ~/dynamicvla-training/official/utils/event_labels.py \
    --validate-dom ~/datasets/hzxie/DOM --n-ep 200
```

**为什么是段级不是帧级**：逐帧要求「已抬升」会把 friction 的真 hold 从 28 帧砍到 9 帧 ——
抓握初期本来还没抬起来。

**为什么阈值取 30 mm**：双峰谷底宽 76 mm（17 → 93），20–90 mm 之间任取结论不变。
取中位保守偏空抓侧。

**DOM 上影响**：200 条等距抽样，210 个 hold 段里 `rise<30mm` 仅 2 个（1.0%），
60 条完整对账共 20 帧变化，**全部是修正**。

打完之后必须做 T11 的三件事。

---

## T7 转换后事件标签全丢

**症状**

LeRobot 数据集里没有 `event_cls` / `event_grasp` / `event_spatial`，或全是 0。

**原因**【实测】

`sm_state` 和事件标签在 **h5** 里。h5 → LeRobot 的转换器**不认识它们**，会直接丢掉。

**处理**

必须先跑 `annotate_recovery.py` 把 `sm_state` 挖出来写成侧车索引，
再转换，再 join 回去。`scripts/07_convert_lerobot.sh` 已经把这
三步串好了（`挖 sm_state → 转换 → join 侧车`），不要手工跳过。

```bash
bash scripts/07_convert_lerobot.sh --root ~/dynamicvla-demo --base ~/dvla-gen/a_slip
```

**注意**：`annotate_recovery.py` 必须在**删 h5 之前**跑。所以顺序是
**转换 → annotate → 验证 → 删 h5**，不是转换完就删。

---

## T8 夹爪值不是 ±1

**症状**

`observation`/`action` 里的夹爪列出现 ±2、±0.3 之类。

**原因**【实测】

夹爪被当作连续量做了 delta。DOM 的夹爪在原始 parquet 里是 **`+1` 张开 / `−1` 闭合**的离散量，
delta 之后变成 `0` 或 `±2`。

**排查**

`action` 在 **h5 里是 8 列，夹爪在 index 7**；转 LeRobot 后是 **7 列，夹爪在 index 6**。
查 h5 用 7，查 parquet 用 6 —— **弄错索引会得出「夹爪正常」的假结论**。

```python
# 验 parquet（LeRobot 后）
g = parquet_action[:, 6]
assert set(np.unique(g)) <= {-1.0, 1.0}, np.unique(g)
```

`scripts/09_verify_loader.py` 第 10 项就是查这个，且是 **fatal**（不通过直接 FAIL）。

**必须读全长数组**：用 `ds[i]` 逐帧取拿到的是已切过的窗口，看不到原始全长，查不出来。

---

## T9 DOM 抽样得出「零变化」的假结论

**症状**

打补丁后在 DOM 上抽样 200 条，`--validate-dom` 报告「零变化」，以为补丁没用。

**原因**【实测】

DOM **按任务严格分块**：

| episode 区间 | 任务 | 占比 |
|---|---|---:|
| 0 – 5880 | long-horizon | 2.84% |
| 5881 – 74354 | pick | 33.0% |
| 74355 – 207305 | place | 64.1% |

如果写 `files[:200]`，**100% 落在 long-horizon**。而 long-horizon 第一次抓的是**另一个物体**
（距离恒 14–30 cm），早在 `>8cm` 分支就返回 `LOST(invalid)`，**根本走不到抬升判据**。
所以「零变化」是抽样偏差，不是补丁无效。

**处理**

必须跨全范围**等距**抽样：

```python
step = max(total_files // n_ep, 1)
files = files[::step][:n_ep]
```

`apply_grasp_rise_patch.py --validate-dom` 已内置这个写法，并在代码里留了注释说明为什么。

**推广**：任何在大数据集上做的抽样验证，先确认数据集是否按某种维度分块。
只看开头 = 只看了最不典型的那一段。

---

## T10 内存随时间单调上涨

**症状**

单进程跑几十条之后 RSS 从 8.4 GB 涨到 20 GB+，开始 swap。

**原因**【实测】

Isaac Sim 启动后基本**不回收**内存，每 episode 泄漏约 **200 MB**。

**处理**

- 每 **50 条**重启一次进程（`06_run_campaign.py` 的 chunk 机制就是干这个的，默认 `--chunk-size 50`）
- 不要把 5 类塞进同一个 loop（出坏批次时分不清是哪类参数问题）
- 不要用一个进程跑完全部 chunk

**监控**

```bash
watch -n 5 'ps -o pid,rss,cmd -C python | grep gen_recovery'
```

RSS 超过 16 GB 就该考虑调小 `--chunk-size`。

---

## T11 改了代码后训练拒绝启动

**症状**

```
AssertionError: managed-readiness: sha256 mismatch for utils/event_labels.py
# 或
FAIL: preflight: working tree dirty (untracked/modified files not in allowlist)
```

**原因**【上游设计】

两道独立闸门：

1. `managed_readiness.assert_ready` 校验 **20 个文件**的 sha256
2. preflight 的 **git 白名单**只允许特定文件被改

**处理**

改完 `event_labels.py`（打 T6 的补丁）后，**三件事一件都不能少**：

```bash
# 1) 单测
pytest tests/ -q                      # 期望仍 95 passed

# 2) 重建 readiness 清单（不能手改 json 伪造）
python rebuild_ready.py

# 3) 把 utils/event_labels.py 加进 preflight 白名单
#    位置：preflight_train.py 里的 allowed 列表
```

**⚠️ `migrate_checkpoint.SOURCE_FILES` 包含 `managed_train.py` /
`managed_runtime.py` / `fullparams.yaml`** —— 这三个文件一改，
**旧 checkpoint 不能直接 resume**，必须重跑迁移。

**禁止伪造 sha256**：readiness 的意义就是防止「代码和权重不匹配」，
手改 json 会让训练跑在未知状态上，损失的是一条完整训练的成本。

---

## T12 loss NaN

**症状**

训练日志 `val_l1` 或 `event_cls` 变 `nan`。

**可能原因（按概率排序）**

1. **yaw wrap 没打** —— 8.9% 的帧有 ±2π 尖峰污染 delta。检查 `utils/rotation.py` 是否存在，
   且 `datasets.py` 的 delta 分支是否调用了 `wrap_yaw`。
   铁律：**训练侧折差值**回 [−π,π)，**绝不能**折绝对角度（roll 2.97–3.32 横跨 π）。
2. **事件损失权重过大** —— 实测可用值 0.02 / 0.01 / 0.002。调大 10 倍容易炸。
3. **`v_rel` 自己差分算错了** —— 直接取 `env_state[6:9]`，别自己差分。
   `object_vel` 是高精度真值（与 ±6 帧拟合只差 4.11 mm/s = 3.6%）。

**排查顺序**：先查数据（跑 `09_verify_loader.py`），再查 loss 权重，最后才怀疑模型。

---

## T13 `pkill` 杀不掉

**症状**

```bash
$ pkill -f gen_recovery
$ pgrep -c -f gen_recovery
1                                  ← 还在
$ nvidia-smi                       ← 显存也还占着 534 MiB
```

**原因**【实测】

死在 Kit 锁上的进程**不会响应普通 SIGTERM**（它卡在 C++ 层的等待里，
Python 的信号处理根本没机会跑）。

**处理**

```bash
pkill -9 -f gen_recovery           # -9 是唯一干净的方式
sleep 3
nvidia-smi --query-compute-apps=pid,used_memory --format=csv   # 确认清空
```

如果 `-9` 之后显存还在，等 10–20 s（驱动回收有延迟），仍不释放就重启容器/实例。

---

## T14 补丁后 git diff 全文重写

**症状**

```bash
$ git diff --stat
 utils/event_labels.py | 594 +++++++++++++++++++++---------------------
 1 file changed, 302 insertions(+), 292 deletions(-)
```

只该改 4 处，却整个文件都变了。

**原因**【实测，本仓库自身踩过】

在 Windows 上跑补丁脚本时，`open(path, "w")` 默认做换行转换，
把 594 行的 **LF** 文件写成 609 行的 **CRLF**。拷回 Linux 后每行末尾都带 `\r`。

**已在 `patches/apply_grasp_rise_patch.py` 修掉**：读写都用 `newline=""`，
并在落盘时按原文件风格还原，输出会打印 `换行风格：LF（保持原样）`。

**如果你用别的工具改代码**（sed / IDE / 手写脚本），同样要小心。检查方法：

```bash
python -c "b=open('utils/event_labels.py','rb').read(); \
print('CRLF=%d LF=%d' % (b.count(b'\r\n'), b.count(b'\n')))"
# CRLF 应为 0
```

---

## T15 网络盘上 IO 慢

**症状**

单条 episode 卡 30 s+，吞吐掉一半。

**原因**【实测】

DSW 上 `/mnt/data` 是 **ossfs**（对象存储挂载成文件系统），随机写小文件极慢。
`/mnt/cpfs` 与 `/mnt/workspace` 是**同一个 cpfs 的两个挂载点**，且 cpfs 已用 99%。

**处理**

| 用途 | 该写哪 |
|---|---|
| h5 中间产物 | **本地盘**（`/tmp` 或容器本地卷） |
| LeRobot 成品 | 可以写 cpfs/ossfs，但要接受转换阶段的慢 |
| checkpoint | 一律写 `/mnt/data`（`/mnt/cpfs` 只剩 286 G） |

`scripts/03_preflight.sh` 会检测输出目录的文件系统类型，
命中 `nfs*|fuse*|ossfs*|cifs*|smb*` 直接判红。

---

## T16 磁盘类型告警

**症状**

```
[FAIL] 输出目录在 nfs/fuse/ossfs 上：/mnt/data/boning.tbn/dvla-gen
```

**要不要管**

看你要写什么：

- **写 h5** → 必须换本地盘。1.2 MB/帧 × 108 帧 = 130 MB/条，网络盘上这 130 MB
  会被拆成成千上万个小写操作，慢到不可用。
- **只写最终 LeRobot** → 可以接受，但转换时间会显著变长。
- **只在上面读** → 忽略这个告警。

想跳过检查（已确认可接受）：

```bash
bash scripts/03_preflight.sh --root ~/dynamicvla-demo --allow-network-fs
```

---

## 附：三个「看起来该报错但不会报错」的静默失败

这几个最危险，因为它们**不报错**，只让数据悄悄变差。

| # | 现象 | 真相 | 防线 |
|---:|---|---|---|
| 1 | 批次跑完了，产出率也「正常」 | `REC_SLIP_MODE=open`，滑脱在训练侧不可见 | `05_generate.sh` 自检 + `run_config.json` 快照 |
| 2 | `qa_gates` 全绿 | 闸门只按 A 类设计，对 B/C/E 类误判 PASS | `qa_gates.py` 的 `EXPECT` 按类自适应 |
| 3 | 抽样验证「零变化」，以为安全 | 抽样全落在 long-horizon，走不到判据 | `files[::step]` 等距抽样 |

**共同的防御姿势**：每一批都落 `run_config.json`，每一批都过闸门，
抽样必须跨全范围。不要相信「跑完了没报错」。

---

## 还搞不定？

带上这些信息提 issue：

```bash
bash scripts/03_preflight.sh --root ~/dynamicvla-demo > /tmp/preflight.log 2>&1
python scripts/09_verify_loader.py --base ~/dvla-gen --json > /tmp/verify.json
cat ~/dvla-gen/*/run_config.json > /tmp/runs.json
```

外加：出错那条 h5 的 `*.json` 元数据、`nvidia-smi` 快照、
以及出问题的类别与实际用的 `profiles/*.env` 内容。