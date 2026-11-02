# 08 · RTX 4090 并行生产实测（2026-11-02）

> 目标：在单张 RTX 4090（24GB）上把恢复数据生产吞吐压到硬件上限。
> 结论先行：**双进程并行是 4090 的最优解，吞吐 ≈ 250 attempts/小时、
> 80~100 saved episodes/小时（~6GB/小时），接近线性加速。**

## 1. 为什么是「多进程」而不是「num_envs > 1」

`PlaceRecoveryStateMachine` 是按 `num_envs == 1` 设计的（`assert` 在
`state_machines/place_recovery_sm.py`），向量化改造工作量大且会改变已验证的状态机语义。
而 Isaac Sim 单实例的 GPU 利用率仅 ~17%（瓶颈在 CPU 端 Python 状态机 + 串行渲染），
**多进程恰好补位**：每个进程独立 App、独立 seed 段、独立输出目录，零代码改动。

## 2. 实测数据（Isaac Sim 4.5.0 / IsaacLab 2.2.1 / torch 2.7.1+cu128）

| 配置 | 显存 | GPU 利用率 | 吞吐 |
|---|---|---|---|
| 单 worker | 9~10GB | ~17% | 37~41 s/attempt |
| **双 worker** | **~19GB** | **~33%** | **~28 s/attempt（合计）** |
| 三 worker | 预计 28GB+ | — | **OOM，不可行** |

- 30s episode 预算下 save 率 30~40%（其余为 `no placement within 30 s` 正常丢弃）
- 单个 saved episode ≈ 75MB（h5 含 3 路 360×480 rgb+seg，183 帧量级）
- CPU 占用每进程 4~10 核，128 核平台毫无压力

## 3. 部署

```bash
# 4090 上：单 worker 冒烟（验证环境）
./scripts/21_parallel_worker.sh smoke 1000001 1

# 4090 上：双 worker 滚动生产（每轮 50 episodes，seed 自动递增，磁盘<8G 自动暂停）
setsid nohup ./scripts/22_supervisor_4090.sh > /dev/null 2>&1 < /dev/null &

# 停止（当前轮跑完即退）
touch /root/gpufree-data/work/STOP
```

数据在 4090 磁盘吃紧时，在工位机端起拉取转存
（`23_pull_4090_to_workstation.sh`，只拉 3 分钟前写完的文件，传完即删源端），
即可无限滚动生产；或直接对数据盘扩容。

## 4. seed 段与文件命名

- worker b：seed `2,000,001 + 1000×轮数` 起；worker c：`3,000,001 + 1000×轮数` 起
- episode 文件名含 seed（`place_franka_<obj>_<seed:08d>_<hash>.h5`），
  **worker 间天然无冲突**，多目录可直接合并
- `events/<seed:06d>.jsonl` 与 h5 一一对应，同步合并即可

## 5. 产出质量验收（2026-11-02 抽检 episode seed=2361）

| 检查项 | 结果 |
|---|---|
| h5 结构 | 183 帧 × {action(8), ee_pos/quat, joints(9), object_pos/quat/vel, 3 路 rgb+seg, sm_state} ✅ |
| NaN / 值域 | 全项无 NaN ✅ |
| 滑脱物理 | 持握 8cm/s → 滑脱中 42cm/s → 重试接近 5cm/s，物体速度激增真实 ✅ |
| 夹爪逻辑 | slip 帧夹爪 cmd 仍闭合（-1），重抓阶段张开（+1）✅ |
| 相机视觉 | 三路图清晰无花屏，重试姿态与 events 记录一致 ✅ |
| DOM 兼容 | action 8 维（pos3+quat4+gripper1）由官方转换器转 7 维欧拉，与 DOM 20 万集同管线 ✅ |
| recovery 标注 | json 含 slip_frame/settle_frame/retry_frame/n_grasp_frames/has_slip/has_retry ✅ |

## 6. 4090 环境踩坑速查（详见 06_troubleshooting）

1. **FSD 禁用物理**：`isaacsim-4.5.0.0.dist-info` 必须存在，否则 AppLauncher 误判
   版本加载 5.0 kit（开 FSD）→ `Failed to create simulation view`。
   pip 拷贝安装时极易漏掉主包 dist-info。
2. **libtorch_cuda_linalg.so 加载失败**：Isaac Sim 启动会重排库搜索路径，
   必须先 `export LD_LIBRARY_PATH=$SP/torch/lib:$SP/nvidia/*/lib:$SP/cusparselt/lib:...`
   （已封装在 21_parallel_worker.sh）。
3. **numpy 残留混装**：手动降级 numpy 不清旧 .so，scipy 报
   `All ufuncs must have type numpy.ufunc`。修法：`rm -rf numpy* scipy*` 后重装。
