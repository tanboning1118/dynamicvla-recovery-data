# 09 · 开机检查单：把每一秒仿真的有效产出压到最大

> 写在关机后的案头复盘（2026-11-02）。**结论先行：4090 的 GPU 并行度已经到顶
> （双 worker 33% util，三 worker 必 OOM），下一步的吞吐不在「开更多进程」，
> 而在「白跑的 attempt 少一半」。** A 类 26%、B 类有效 22% 的产出率意味着
> 每小时 250 次 attempt 里约 180 次是白跑 —— 任何把产出率提 10 个百分点的
> 改动，都等价于给集群加了一台 4090。
>
> 本文档分四节：固定检查（必做）、时间账（预期管理）、量产前两个对照实验
> （各 ~40 分钟）、量产运行手册（含 22 脚本新参数）。

---

## 1. 开机第一小时：固定检查（顺序执行，缺一不可）

```bash
# ① 磁盘：数据盘剩多少决定今天跑多少（守卫线 8GB 会自动暂停新轮）
df -h /root/gpufree-data
# 每 saved episode 75~150MB，双 worker 约 6GB/h —— 剩 <50GB 就要同时挂转存

# ② venv 在 /tmp 的话重启即清，按 06_troubleshooting 重装三件套
#    （torch / cuda deps / isaaclab），装完确认 isaacsim-4.5.0.0.dist-info 存在
#    否则 AppLauncher 误载 5.0 kit → Failed to create simulation view

# ③ 单 worker 冒烟 2 条（用 24，不用 21 —— 见 §4 的坑）
bash scripts/24_run_class.sh profiles/a_slip.env smoke_a 9900001 2

# ④ 冒烟数据立刻过闸门：相机冻屏是历史上最贵的一次静默事故
python scripts/25_verify_episode.py <out>/smoke_a --expect A_slip
#    wrist 相机首末帧显著变化像素必须 > 50%（冻屏时 < 1%）

# ⑤ 双 worker 冒烟各 2 条，确认错峰后两个 kit 都能进稳态
#    nvidia-smi dmon -s um：稳态 ~19GB / ~33%，>22GB 立即停查
```

**为什么 ③ 必须用 24**：`21_parallel_worker.sh` 直接跑裸 `gen_recovery.py`，
不加载任何 profile —— 吃的是默认环境变量（`REC_SLIP=1` 且 `SLIP_MODE=open`、
EP=30）。open 模式的滑脱夹爪是张开的，实测对训练 **100% 不可见**（0/7 valid）。
21 只用于无注入基准测试，生产一律走 24（profile 唯一真源）。

---

## 2. 时间账：300 条配额 ≈ 6 小时纯仿真，一天绰绰有余

实测吞吐（4090 双 worker，`docs/08`）：**~28 s/attempt 合计，250 attempts/h**。

| 类 | 配额 | 产出率（实测） | 需要 attempts | 纯仿真时长 |
|---|---:|---|---:|---:|
| A 滑脱 | 150 | 26% | ~580 | ~2.3 h |
| B 抓空 | 100 | 保存 40% × 真抓空 55% = 22% | ~455 | ~1.8 h |
| C 放偏 | 30 | 8~12%（不稳定） | 250~375 | 1.0~1.5 h |
| D 对照 | 20 | 35~40% | ~55 | ~0.25 h |
| **合计** | **300** | — | **~1350** | **~5.5 h** |

两个推论：

1. **排实验的时间完全负担得起。** 先花 ~1.5 h 做下面 §3 的两个对照实验，
   若 A 类 EP 延长有效，A 的 2.3 h 直接缩到 ~1.5 h，实验自付自收。
2. **瓶颈在产出率不在吞吐。** 与其跑到深夜，不如把白跑的 180 次/h 砍下来。

---

## 3. 量产前两个对照实验（双 worker 并行，合计 ~1.5 h）

### 实验一：A 类 EP 30 s vs 45 s（头号候选）

B 类的教训原样摆在 A 类面前：**真抓空样本因 EP 预算不足被整批丢弃**
（30 s 预算下 100% 超时，提到 45 s 后修复）。A 类的状态轨迹
`10→11→12→3→4→5→6→8→9` 比 baseline 长得多 —— 滑脱发生在 t=15 s 之后的话，
恢复+重放根本挤不进 30 s 预算。但 A 类**从未对照过**。

```bash
# worker 1：EP=45（脚本会生成临时 profile，不碰 a_slip.env）
python scripts/27_calibrate_slip.py --scales 8 --n 20 \
    --override GEN_EPISODE_LENGTH=45 --tag aep45

# worker 2：同时跑配重标定（实验二）
python scripts/27_calibrate_slip.py --scales 2,3,4,6,8 --n 8 --tag calib
```

判读：`aep45` 的滑脱率/纯度不低于基线、saved 明显更高 → 把
`profiles/a_slip.env` 的 `GEN_EPISODE_LENGTH` 改 45。基线 26%（13/50）
样本量够大，可直接对比；若差异 < 5pp，倾向不动（EP 变长每条多 ~15 s）。

### 实验二：配重标定（为什么 ×8 可能是过冲）

配重不决定「滑不滑」（那由质量能否压过指间最大静摩擦决定），只决定
**滑出之后**的落地动能 —— ×8 让物体弹得远、滚得久，更容易撞上两条放弃线
（滚出可达范围 / settle 超 5 s）。找「还滑得动」的最小配重：

```bash
python scripts/27_calibrate_slip.py --scales 2,3,4,6,8 --n 8
```

⚠️ 配重太小不会报错，只会滑脱不再发生、被 `--require_slip` 静默整条丢弃，
表现为「保存率很高但产出全废」。所以必须实测，且必须看「真滑脱率」列。

### 顺手补的归因数据（零成本）

每轮验收带上归因统计，把「A 类 74% 损失在哪个状态」这张表补上
（首抓阶段 RESET 与滑脱无关，是独立的损失通道）：

```bash
python scripts/25_verify_episode.py <batch> --expect A_slip --quiet
# RESET 归因自动打印触发前状态；攒够 50 次尝试就能定论
```

---

## 4. 量产运行手册（22 脚本已修两处坑）

**坑 1（已修）**：旧版 supervisor 两个 worker 同时冷启动 —— 实测两 kit
会双双卡死在 RTX 初始化后（显存只申请 ~1GB、永不就绪），只有错峰活下来。
新版默认 worker c 延迟 `STAGGER=150` 秒，可用环境变量覆盖。

**坑 2（已修）**：旧版 worker 调 `$WORK/run_worker.sh`（不在仓库里），
内容不透明。新版支持 `PROFILE_B` / `PROFILE_C` 直接指向 profiles/*.env，
内部走 24 —— 参数以 profile 为唯一真源，`--require_slip` 等不再靠手抄。

```bash
# 标准量产启动（A + B 双 worker，seed 自动分段递增）
PROFILE_B=/root/gpufree-data/work/profiles/a_slip.env \
PROFILE_C=/root/gpufree-data/work/profiles/b_miss.env \
EP_PER_ROUND=50 \
setsid nohup bash scripts/22_supervisor_4090.sh > /dev/null 2>&1 < /dev/null &

# 优雅停（当前轮跑完即退）；卡死的 kit 才用 kill -9（Isaac Sim 不吃 SIGTERM）
touch /root/gpufree-data/work/STOP
```

**调度要点**：

- A（68 s/attempt，长杆）和 B（28 s 平均，短）配对，A 的轮次先启动；
  D 类极快，最后单独补；**C 类放最后**——产出率最低且 `REC_PLACE_OFFSET`
  不稳定，若 2 小时凑不齐 30 条，切自然筛选模式（约 1/8）。
- 每轮 50 条 × 2 worker ≈ 23 分钟；轮间冷启动 + 错峰 ≈ 3 分钟（~13% 开销）。
  若一晚跑 > 10 轮，可试 `EP_PER_ROUND=100` 摊薄（RSS 泄漏 200MB/ep，
  需确认主机 RAM 富余；显存稳态实测不涨）。
- **仿真跑着的时候同步验收上一轮**：读 h5 不占 GPU，17% util 的空余全在
  CPU 侧。每轮结束抽 1 条看相机活动性（`25_verify`），发现冻屏立即停线。
- 磁盘剩 < 8GB 自动暂停新轮；工位机端挂 `23_pull_4090_to_workstation.sh`
  滚动转存（只拉 3 分钟前写完的文件，传完删源）。

**收尾**（睡前）：`07_convert_lerobot.sh` 转换 + `08_qa_gates.sh` 闸门 +
`10_report.py` 报表。AV1 软编码吃 CPU 不吃 GPU，可与下一批仿真并行。

---

## 5. 不建议碰的三个方向（记录为什么不碰）

| 方向 | 结论 | 原因 |
|---|---|---|
| 三 worker | ❌ | 9~10GB/worker × 3 ≈ 28.5GB > 24GB，显存硬上限 |
| `--num_envs > 1` 向量化 | ❌ | 状态机按单 env 设计（要读写刚体句柄改摩擦/质量），改造量等于重写，且会改变已验证语义 |
| 降分辨率/关 seg/减采样 | ❌ | 训练视觉输入必须与 DOM 分布一致，渲染参数一动数据就废 |
