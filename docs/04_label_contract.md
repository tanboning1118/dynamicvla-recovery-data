# 04 · 标签契约：生成的数据要满足什么，才能进你的训练器

> 本文档对准的是**改造后的 `server_fullft` 加载器**，不是官方 DOM 原始格式。
> 所有形状都在 2026-11-02 用真实生成的 9 条数据验过（14 PASS / 0 FAIL）。

---

## 1. 两段格式：h5 → LeRobot

生成器先落 **h5**（原始真值，无损），转换器再落 **LeRobot v2.1**（训练读取格式）。
两段的列布局**不同**，这是最容易出错的地方。

| | h5 | LeRobot 转换后 |
|---|---|---|
| `action` 列数 | **8** | **7** |
| 夹爪 index | **7** | **6** |
| 旋转表示 | `quat (x,y,z,w)` | `rotvec`（`--rotation euler` 参数名有误导，实际走 rotvec） |
| `sm_state` | **有** | **没有**（被转换器丢掉） |

> ⚠️ **用 `action[:, 6]` 分析 h5 会得到垃圾**。实测 `grip uniq [0, 0.387]`，hold 段为空。
> h5 的夹爪在 **index 7**。

---

## 2. h5 字段全表

每个 episode 落三个文件：

```
<output_dir>/
├── <episode_name>.h5            数据本体（gzip）
├── <episode_name>.json          env_cfg + instruction + recovery 统计
├── recovery_summary.json        全批汇总
├── events/<seed>.jsonl          状态机事件流（DEBUG 模式才写）
└── _DONE                        完成哨兵（含 attempts / saved / with_slip）
```

### 2.1 h5 数据集

| 键 | 形状 | dtype | 说明 |
|---|---|---|---|
| `action` | `(T, 8)` | float32 | `pos 3 + quat 4 (xyzw) + gripper 1`。**夹爪在 index 7，值域 {−1, +1}** |
| `ee_pos` | `(T, 3)` | float32 | 末端位置，米 |
| `ee_quat` | `(T, 4)` | float32 | 末端四元数 xyzw |
| `joints` | `(T, 9)` | float32 | 7 关节角 + 2 指开合 |
| `object_pos` | `(T, 3)` | float32 | **物体真值位置** |
| `object_quat` | `(T, 4)` | float32 | 物体四元数 xyzw |
| `object_vel` | `(T, 3)` | float32 | **物体线速度真值**（无角速度） |
| `sm_state` | `(T,)` | int32 | 状态机轨迹，见 §3 |
| `opst_cam_rgb` | `(T, 360, 480, 3)` | uint8 | 对侧相机 RGB |
| `side_cam_rgb` | `(T, 360, 480, 3)` | uint8 | 侧相机 RGB |
| `wrist_cam_rgb` | `(T, 360, 480, 3)` | uint8 | 腕部相机 RGB |
| `opst_cam_seg` | `(T, 360, 480, 1)` | uint8 | 语义分割 |
| `side_cam_seg` | `(T, 360, 480, 1)` | uint8 | |
| `wrist_cam_seg` | `(T, 360, 480, 1)` | uint8 | |

### 2.2 体积

实测 **1.2 MB/帧**（3 路 RGB + 3 路 seg，gzip）。

| 帧数 | h5 大小 |
|---:|---:|
| 108（干净成功） | **130 MB** |
| 190（滑脱恢复） | **~230 MB** |
| 284（抓空重试长轨迹） | ~340 MB |

> 这是 h5 必须是**临时文件**的原因：330 条 ≈ 80 GB。

### 2.3 json 侧车

```json
{
  "seed": 9001,
  "instruction": {"task": "place", "objects": ["apple", "red apple", ...]},
  "recovery": {
    "n_frames": 187,
    "slip_frame": 64,
    "settle_frame": 70,
    "retry_frame": 91,
    "giveup_frame": -1,
    "n_grasp_frames": 40,
    "n_lift_frames": 12,
    "has_slip": true,
    "has_retry": true,
    "gave_up": false
  }
}
```

`recovery` 是**筛选依据**：
- `--require_slip` 检查 `has_slip and has_retry`（非 `keep_imperfect` 时）
- 后置过滤（如 D 类只留 `n_grasp_frames` 出现两次以上的）也读这里

---

## 3. `sm_state` 状态码

| 值 | 名称 | 语义 |
|---:|---|---|
| 0 | `INIT` | 起始 |
| 1 | `RESET` | 从头重来 |
| 2 | `APPROACH_ABOVE_OBJECT` | 移到物体上方 10 cm |
| 3 | `APPROACH_OBJECT` | 下降到抓取位 |
| 4 | `GRASP_OBJECT` | 闭合夹爪（**空抓也走这里**） |
| 5 | `LIFT_OBJECT` | 抬升（**空抓检测在这里**） |
| 6 | `APPROACH_ABOVE_TARGET` | 移到容器上方 |
| 7 | `APPROACH_TARGET` | 下降到放置位 |
| 8 | `PLACE_OBJECT` | 张开夹爪 |
| 9 | `TO_TARGET` | 撤手（**放置判定也在这里**） |
| **10** | **`SLIP_OBJECT`** | **滑脱**（friction：夹爪闭合、降摩擦；open：张开） |
| **11** | **`WAIT_SETTLE`** | **等物体静止** |
| **12** | **`RECOVER_APPROACH`** | **重抓（也用于抓空后重抓）** |
| **13** | **`GIVE_UP`** | **放弃** |

0–9 与官方 `place_sm.py` **完全一致**，10–13 是恢复版新增。

### 3.1 各类场景的状态轨迹

| 类 | 轨迹 |
|---|---|
| D 干净成功 | `0→2→3→4→5→6→8→9` |
| **A 滑脱** | `0→2→3→4→5→`**`10→11→12`**`→3→4→5→6→8→9` |
| **B 抓空** | `0→2→3→4→5→`**`12`**`→3→4→5→6→8→9`（无 10/11，因为物体从未被抬起） |
| **C 放偏** | `...→8→9→`**`1 RESET`**`→2→3→4→5→6→8→9` |
| 失败 | 末尾出现 `13 GIVE_UP` |

**A 与 B 的判据差异就在 10/11 有没有出现**——这是「滑脱」与「抓空」最干净的机器可读区分。

---

## 4. LeRobot 转换后的格式（训练读取）

`create_lerobot_dataset.py --rotation euler` 之后：

| 字段 | 形状 | 说明 |
|---|---|---|
| `action` | `(20, 7)` | `pos 3 + rotvec 3 + gripper 1`。**夹爪在 index 6** |
| `observation.state` | `(2, 6)` | 两帧本体状态 `pos 3 + euler 3`，**不含夹爪** |
| `observation.environment_state` | `(15,)` → pad 32 | 物体 15 维（`pos 3 + rot 3 + vel 3` 等） |
| `observation.images.opst_cam` | `(2, 3, 384, 512)` | |
| `observation.images.wrist_cam` | `(2, 3, 384, 512)` | |
| `observation.images.side_cam` | `(2, 3, 384, 512)` | |
| `action_is_pad` | `(20,)` bool | |
| `task` | str | `'get hold of the apple and set on the plate.'` |
| `event_spatial` | `(15,)` float32 | 15 维空间量（由 `event_labels.build_targets` 现算，不落盘） |
| `event_valid` | `(15,)` bool | |
| `event_cls` | int ∈[0,21] | 22 类阶段 |
| `event_grasp` | int ∈{0,1,2} | 0 HELD / 1 SLIPPING / 2 LOST |

> **`event_*` 不在 parquet 里，是加载器现算的。** 所以改 `event_labels.py` 会立刻影响训练读到的标签——
> 这也是 `grasp_label` 补丁必须谨慎的原因。

### 4.1 验收清单（14 项）

| # | 项 | 期望 |
|---:|---|---|
| 1 | dataset 构造 | 不抛异常，`len > 0` |
| 2 | `action` | `(20, 7)` |
| 3 | `observation.state` | `(2, 6)` |
| 4 | `opst_cam` | `(2, 3, 384, 512)` |
| 5 | `wrist_cam` | `(2, 3, 384, 512)` |
| 6 | `event_spatial` | `(15,)` float32 |
| 7 | `event_valid` | `(15,)` bool |
| 8 | `event_cls` | ∈[0,21] |
| 9 | `event_grasp` | ∈{0,1,2} |
| 10 | `action` 值域 | 有限（`max\|a\| < 10`） |
| 11 | `action_is_pad` | 存在 |
| 12 | `task` | 非空字符串 |
| 13 | 抽样 9 个 index | 无异常 |
| 14 | **gripper 未被 delta** | parquet 全长上 `set(action[:,6]) ⊆ {−1, +1}` |
| 15 | **事故类标签非零** | `slipping + lost > 0` |

> ⚠️ **第 14 项必须在 parquet 全长数组上做，不能逐帧 `ds[i]`。**
> `ds[i]["action"]` 是单帧 `(7,)`，`delta_timestamps` 的 `range(20)` 只对 `_get_episode` 生效
> —— 单帧取数时 delta 后值域本来就窄，会得出「gripper 被 delta 了」的**假 FAIL**。

---

## 5. 22 类阶段标签（`event_cls`）

编号定义与 DOM 一致（2026-11-01 起 `hold` 改用**物理接触**定义）：

| 类 | 语义 | DOM 占比 | A 类实测占比 |
|---:|---|---:|---:|
| **0** | 已接触 / 持握中 | **36.40%** | **46.74%** |
| **1–20** | 距最近抓取事件还有 s 步（s = 类号） | 合计 18.46%（单类 0.92%） | 合计 **17.85%** |
| **21** | 太远（>20 步或无后续抓取） | **45.14%** | **35.42%** |

**门控** `α = 1 − p21`：DOM 54.86% / A 类 64.58%。

### 5.1 hold 的物理定义

```
hold = 夹爪命令闭合 (−1)  AND  |p_ee − p_obj| < 5 cm (TOUCH_TOL)
       且短于 3 帧的段丢弃
```

**多抓感知**：`t` 落在任一 hold 段 → 类 0；否则 `s = min{k ∈ K : k ≥ t} − t`，
`s > 20` 或无后续 → 类 21。

> 为什么不用「只看 `action[:,6] < 0`」：实测旧口径会让模型在物体 20 cm 外仍给出
> `P(接触) = 0.996`——滑脱帧被标成「已接触」了。

---

## 6. grasp 3 类标签（`event_grasp`）

| 值 | 名称 | 判据 |
|---:|---|---|
| 0 | `HELD` | 当前帧在 hold 段内，且距离 < 5 cm，且**剩余距离内帧 ≥ 15** |
| 1 | `SLIPPING` | 后续 15 帧内会掉出 5 cm |
| 2 | `LOST` | 距离已超 5 cm / 整段没进过 5 cm / **段级抬升不足** |

### 6.1 ⚠️ 必须打补丁：段级抬升先决

现有实现只用距离，**量不出「手贴着物体但没夹住」**：

```python
# 现状（有 bug）
inside = seg < 0.05
if not inside.any():
    return GRASP_LOST, bool(seg.min() < 0.08)
# → 空抓时手恰好环绕物体，距离同样 <5cm，被判 HELD
```

实测三个物理空抓段（物体 `rise_max` ≤ 4.3 mm）分别有 **21% / 21% / 18%** 的帧标成 `HELD`，
另有 57% 标成 `SLIPPING`。对照真抓段是 **HELD 40/40 = 100%**。

**修法**（`patches/`）：

```python
RISE_TOL = 0.03      # 新增常量

if obj_z is not None:
    rise = float(obj_z[start:end].max() - obj_z[start])
    if rise < RISE_TOL:
        return GRASP_LOST, True       # 整段空手，与距离无关
```

**33 段全量双峰**：

```
空抓簇  rise ≤ 17 mm   （7 段）
真抓簇  rise ≥ 93 mm   （26 段）
谷底宽 76 mm，5.4 倍空档
```

阈值 20–90 mm 之间任取结论完全不变，工程取中位 **30 mm**。

### 6.2 对 DOM 的影响（已实测）

200 条跨任务等距抽样（覆盖 long-horizon / pick / place）：

```
hold 段总数 210，rise<30mm 的段 2（1.0%），其中被错标 HELD 的帧 0
60 条对账：共 20 帧变化
  HELD     2373 → 2369  (−4)
  SLIPPING   32 →   16  (−16)
  LOST       21 →   41  (+20)
```

那 20 帧**全部是修正**（`ep051825`：物体 z 恒为 0.036、`dmin` 只到 3.75 cm，手确实靠近了但物体从未被抬起）。

> **这个 bug 在 DOM 上可忽略（0.5%），在自有数据上不可忽略**——B 类里空抓是设计出来的主模式（5 条里 3 段）。

### 6.3 抽样陷阱（写进 SOP 以免重犯）

DOM 按任务**严格分块**：

| 分块 | episode 范围 | 占比 |
|---|---|---:|
| long-horizon | 0 – 5880 | 2.84% |
| pick | 5881 – 74354 | 33.0% |
| place | 74355 – 207305 | 64.1% |

**「取前 N 个 parquet」100% 落在 long-horizon。** 那里第一次抓取抓的是**另一个物体**，
距离恒 14–30 cm，早在 `seg.min() < OBJ_TRACK_TOL` 分支返回 `LOST(invalid)`，
**根本走不到抬升判据** → 得出「零变化」的假结论。

必须 `files[::len//N]` 跨全范围等距抽样。

---

## 7. 15 维空间量（`event_spatial`）

顺序固定，`normalize_spatial` / `denormalize_spatial` 都在 `event_labels.py`：

| idx | 量 | 单位 |
|---:|---|---|
| 0–2 | `p_obj − p_ee` | m |
| 3–5 | `v_obj`（物体线速度） | m/s |
| 6–8 | `v_rel = v_obj − v_ee` | m/s |
| 9–11 | 物体 rot6d 前 3 维 | — |
| 12–14 | 物体到容器的相对向量 | m |

**`v_rel` 直接取 `env_state[6:9]`，不要自己差分** —— 实测 `object_vel` 是高精度真值，
与 ±6 帧拟合真值只差 4.11 mm/s（3.6%）。

---

## 8. 与训练侧的三个对接约束

| # | 约束 | 后果 |
|---:|---|---|
| 1 | **`delta_timestamps` 收敛到 `server_fullft/obs_spec.py`** | 改帧间隔只改这一处（`OBS_DELTA` / `ACTION_HORIZON` / `DELTA_TIMESTAMPS`）。别在 4 个文件里各改一遍 |
| 2 | **`managed_readiness.assert_ready` 校验 20 个文件 sha256** | 改 `event_labels.py` 后必须重跑 `rebuild_ready.py`，**不能伪造** |
| 3 | **preflight 的 git 白名单** | 目前只允许 `configs/dynamicvla.yaml`、`utils/datasets.py`、`utils/rotation.py`、`policies/dynamicvla/modeling_dynamicvla.py`、`scripts/inference.py`。**`utils/event_labels.py` 不在白名单内** → 打补丁前先把它加进去 |

### 8.1 `managed_train.py` 的多 repo 限制

`managed_train.py` 硬编码 `hzxie/DOM`，且有 `audit_dataset` 过滤。
**自有数据进不去训练器**——这是 P1 待办项，不是本仓库能解决的。

在你修好之前，验证数据集只能用「独立加载器跑 batch」的方式（`scripts/09_verify_loader.py`）。

---

## 9. yaw wrap 铁律（与生成数据相关）

生成侧的欧拉角有 gimbal 表示跳变，但 **转矩阵后连续**（≤30.7°/帧），所以：

| # | 铁律 |
|---:|---|
| 1 | 训练侧折**差值**回 [−π, π)，作用 dim **3:6** |
| 2 | 推理侧对**绝对 yaw** 取模回 [0, 2π)，**只动 dim 5** |
| 3 | **绝不能**对绝对角度折 [−π, π)——roll 2.97–3.32 横跨 π，折了会 2π 跳变 |

`utils/rotation.py` 提供 `ROT_SLICE` / `YAW_INDEX=5` / `wrap_to_pi` / `wrap_yaw`。
生成的数据**不需要额外处理**，但要确认你的加载器已经打了 yaw wrap 补丁，
否则 8.9% 的帧会被 ±2π 尖峰污染。