# gpuq CLI 参数手册

本文覆盖 `gpuq 0.2.0` 的全部公共命令和参数。

## 全局参数

全局参数必须写在子命令之前：

```bash
gpuq [--state-dir PATH] [--config PATH] SUBCOMMAND ...
```

| 参数 | 含义 | 默认值 |
|---|---|---|
| `--state-dir PATH` | SQLite、锁文件和默认日志目录 | `~/.local/state/gpuq` 或 `GPUQ_STATE_DIR` |
| `--config PATH` | TOML 配置文件 | `~/.config/gpuq/config.toml` 或 `GPUQ_CONFIG` |

## `gpuq init`

初始化状态目录、SQLite schema，并在配置文件不存在时写入示例配置。不会覆盖已有配置或任务数据库。

```bash
gpuq init
gpuq --state-dir /data/my-gpuq init
```

## `gpuq submit`

```bash
gpuq submit [OPTIONS] -- COMMAND [ARGUMENTS...]
```

`--` 很重要：它告诉 `gpuq` 后面的参数属于训练命令，而不是调度器。

### 资源参数

| 参数 | 必填 | 含义 |
|---|---:|---|
| `--memory-mb N|auto` / `--memory N|auto` | 否 | 固定峰值（MB）或自动学习；默认 `auto` |
| `--reserve-mb N` / `--reserve N` | 否 | 额外留给竞态、驱动波动和估计误差的显存；默认 1024MB |
| `--gpu LIST` | 否 | 允许使用的物理 GPU，如 `--gpu 1`、`--gpu 0,1`；可重复；省略表示任意卡 |
| `--gpus N` / `--gpu-count N` | 否 | 同一任务同时使用的本机 GPU 数量；正整数，默认 1 |
| `--max-utilization N` | 否 | 启动时 GPU 利用率硬上限 0–100；省略则不设硬上限，候选卡仍优先选择低利用率 |
| `--stable-samples N` | 否 | 资源条件必须连续满足的采样次数；默认 3 |
| `--exclusive` | 否 | 要求无 compute 进程、显存和利用率接近空闲 |

默认可不写 `--memory`。这等价于 `--memory auto`：首次满足 `reserve + 1MB`
和稳定采样条件后试跑；仅当日志包含可识别 CUDA/HIP OOM 时，才学习新的最低
显存并等待重试。显式写数字则进入固定估计模式；该数字应是训练峰值而不是权重
文件大小，并应另留 1～2GB `--reserve`。

显存和安全余量均按**每张 GPU**计算。`--gpu` 指定候选卡范围，`--gpus` 指定选取
数量；请求数量超过 allowlist 中不同编号的数量时拒绝提交。发现的卡数不足时等待，
不会先占住一部分卡。例如：

```bash
gpuq submit --gpus 2 --gpu 0,1,2,3 --memory 12000 --reserve 1500 \
  -- torchrun --standalone --nproc_per_node=2 train.py
```

gpuq 只启动一次命令，并将全部已分配的物理编号按升序放入 `CUDA_VISIBLE_DEVICES`；
程序内对应设备 `0..N-1`。命令本身必须支持多卡且等待 worker 结束（如 `torchrun`）。
独占、稳定采样和启动前资源检查适用于每张卡。`list` 显示全部分配编号，JSON 增加
`gpu_count` 和 `assigned_gpus`；原有 `assigned_gpu` 字段保留为分配列表中的第一张卡。

### 调度参数

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `--priority N` | `0` | 越大越优先；相同优先级按较早 job ID |
| `--after JOB_ID` | 无 | 只有依赖任务 `completed` 后才能运行；失败/取消会令子任务 `blocked_dependency` |
| `--max-retries N` | `0` | 初次尝试之外允许的自动重试次数 |
| `--retry-on-oom` | 关闭 | 仅检测到 OOM 时自动增加显存估计并重排 |

`--max-retries 2` 表示最多三次总尝试：初次运行加两次重试。

以上重试次数只约束固定显存模式。auto 模式对可识别 OOM 不限次数；普通退出、
断言、数据或代码错误立即失败。申请 N 张卡时，若学习出的每卡 `memory + reserve`
超过候选卡中第 N 大的总显存，会以 `failure_kind=oom_exceeds_device` 终止；仅发现
卡数不足时继续等待。多卡 auto 首次探测在本次尝试结束前保留每张卡各自的启动
空闲区；OOM 时将整个任务重新排队。

升级后请先重启 daemon，再提交多卡任务，确保使用新版调度器。已有数据库中的
任务自动按单卡任务迁移。

### 进程与日志参数

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--name TEXT` | 命令文件名 | 队列中的显示名称 |
| `--cwd PATH` | 当前目录 | 子进程工作目录 |
| `--env KEY=VALUE` | 无 | 添加或覆盖环境变量；可重复 |
| `--log PATH` | 状态目录下自动命名 | 合并 stdout/stderr 的日志文件 |
| `--no-notify` | 关闭 | 禁止该任务的完成、失败、取消和 OOM 通知 |

### 示例

只允许物理 GPU 1，显存够即可：

```bash
gpuq submit \
  --name experiment-a \
  --gpu 1 \
  --memory 5000 \
  --reserve 1500 \
  --cwd /home/me/project \
  --env PYTHONPATH=. \
  -- python train.py --device cuda:0
```

让队列自动学习显存（也是默认行为）：

```bash
gpuq submit --gpu 1 --reserve 1500 -- python train.py --device cuda:0
```

只在利用率不高于 50% 时共享：

```bash
gpuq submit \
  --gpu 1 \
  --memory 4000 \
  --max-utilization 50 \
  --stable-samples 6 \
  -- python evaluate.py --device cuda:0
```

要求独占：

```bash
gpuq submit --exclusive --memory 20000 -- python large_train.py
```

任务链：

```bash
gpuq submit --name train --memory 10000 -- python train.py
# 输出 submitted job 21
gpuq submit --name eval --after 21 --memory 6000 -- python evaluate.py
```

Shell 管道或重定向需要显式交给 shell：

```bash
gpuq submit --memory 4000 -- bash -lc 'python train.py 2>&1 | tee extra.log'
```

## `gpuq list` / `gpuq ls`

查看任务队列、运行任务和历史：

```bash
gpuq list
gpuq ls --status running --status waiting_memory
gpuq list --limit 1000 --json
```

| 参数 | 含义 |
|---|---|
| `--status STATUS` | 状态过滤；可重复 |
| `--limit N` | 最大行数，默认 200 |
| `--json` | 输出机器可读 JSON |
| `--page-size N` | 终端交互模式每页条数，默认 20 |
| `--no-pager` | 在终端中禁用交互翻页 |

当 stdin/stdout 都是终端、结果超过一页且未使用 `--json/--no-pager` 时，列表
进入交互模式：按左、右方向键翻页，按 `q` 或 `Esc` 退出。管道、重定向和 JSON
始终一次性静态输出，便于脚本兼容。交互模式使用终端备用屏幕，退出后恢复原页面，
不会在滚动历史里留下每次翻页的内容。

## `gpuq show JOB_ID`

显示命令、资源、PID、物理 GPU、尝试次数、峰值显存、时间和失败原因。

```bash
gpuq show 21
gpuq show 21 --json
```

## `gpuq logs JOB_ID`

```bash
gpuq logs 21
gpuq logs -n 500 21
gpuq logs 21 --tail 8
gpuq logs -f 21
gpuq log 21 --tail 0 -f
```

| 参数 | 含义 |
|---|---|
| `-n N` / `--lines N` / `--tail N` | 首次显示最后 N 行，默认 100；N 必须非负，0 表示不显示已有日志 |
| `-f` / `--follow` | 持续输出新增内容，任务进入终态后结束 |

`log` 是 `logs` 的别名。使用 `--tail 0 -f` 可以只查看新增日志。
读取已有日志时仅保留末尾 N 行，避免将整个日志文件载入内存。

## `gpuq cancel JOB_ID`

排队任务直接标为 `cancelled`。运行任务先向整个任务进程组发送 SIGTERM，等待后发送 SIGKILL。

```bash
gpuq cancel 21
gpuq cancel 21 --force-after 30
```

| 参数 | 默认 | 含义 |
|---|---:|---|
| `--force-after SECONDS` | `10` | SIGTERM 后等待多少秒再 SIGKILL |

它只操作由当前 `gpuq` 启动的任务进程组，不会终止其他用户进程。

## `gpuq retry JOB_ID`

只允许重新排队已经结束的任务：

```bash
gpuq retry 21
gpuq retry 21 --memory 8000 --reset-attempts
gpuq retry 21 --memory auto
```

| 参数 | 含义 |
|---|---|
| `--memory-mb N|auto` / `--memory N|auto` | 覆盖估计，或重置为新的 auto 探测 |
| `--reset-attempts` | 将历史 attempt 计数清零 |

## `gpuq priority JOB_ID VALUE`

```bash
gpuq priority 21 100
gpuq priority 22 -10
```

数值越高越优先。低优先级任务仍可在高优先级任务暂时放不下时被 backfill。

## `gpuq gpus`

显示调度器看到的显存、利用率、温度和 compute PID：

```bash
gpuq gpus
gpuq gpus --json
```

这是 GPU 操作，受所在系统的权限和 NVIDIA 驱动状态约束。

## `gpuq daemon`

启动调度循环：

```bash
gpuq daemon
gpuq daemon --poll-seconds 5
gpuq daemon --once
gpuq daemon --once --dry-run
```

| 参数 | 含义 |
|---|---|
| `--poll-seconds N` | 覆盖配置中的采样间隔 |
| `--once` | 只执行一个调度周期 |
| `--dry-run` | 运行采样和判定但不启动任务；稳定计数只存在于当前 daemon 进程内 |

同一状态目录只允许一个 daemon；通过 `gpuqd.lock` 和 `flock` 保证。

注意：每次 `gpuq daemon --once` 都是一个新进程，连续执行三次不会累积
`stable_samples=3` 的稳定计数。要观察完整稳定窗口，请启动持续运行的
`gpuq daemon --dry-run`，或临时把任务设为 `--stable-samples 1`。

安装后还会提供等价入口 `gpuqd`，例如 `gpuqd --poll-seconds 5`；它等价于
`gpuq daemon --poll-seconds 5`。

## `gpuq doctor`

检查状态目录、SQLite 和 GPU 监控：

```bash
gpuq doctor
```

如果 `nvidia-smi` 不可执行、驱动异常或查询超时，会返回非零退出码。

## 任务状态

| 状态 | 含义 |
|---|---|
| `queued` | 新提交，尚未采样 |
| `waiting_memory` | 当前没有满足显存/利用率/稳定窗口的 GPU |
| `starting` | 已原子领取，runner 正在建立 |
| `running` | 命令正在运行 |
| `retry_wait` | OOM 后已调整估计，等待再次调度 |
| `completed` | 退出码 0 |
| `failed` | 启动、runner 或命令失败 |
| `cancelled` | 用户取消 |
| `blocked_dependency` | 依赖任务失败、取消或被依赖阻塞 |

## 配置文件参数

```toml
[gpuq]
state_dir = "~/.local/state/gpuq"
poll_seconds = 10
default_reserve_mb = 1024
default_stable_samples = 3
auto_probe_hold_seconds = 300
auto_probe_stable_samples = 12
auto_probe_stability_tolerance_mb = 128
auto_probe_release_margin_mb = 1024
exclusive_max_used_mb = 768
exclusive_max_utilization = 5
nvidia_smi = "nvidia-smi"
notify_command = ["notify-send", "{title}", "{message}"]
```

| 配置项 | 说明 |
|---|---|
| `state_dir` | 数据库、锁与默认日志目录 |
| `poll_seconds` | daemon 采样间隔 |
| `default_reserve_mb` | submit 未指定 reserve 时采用 |
| `default_stable_samples` | submit 未指定稳定次数时采用 |
| `auto_probe_hold_seconds` | 首次 auto probe 共享前的最短观察秒数 |
| `auto_probe_stable_samples` | 释放全区预留前的连续显存样本数 |
| `auto_probe_stability_tolerance_mb` | 稳定窗口允许的最大显存波动 |
| `auto_probe_release_margin_mb` | 释放后保留的最小显存波动空间 |
| `exclusive_max_used_mb` | 独占模式允许的驱动/桌面基础显存 |
| `exclusive_max_utilization` | 独占模式允许的最大瞬时利用率 |
| `nvidia_smi` | `nvidia-smi` 可执行路径 |
| `notify_command` | 通知命令参数数组；支持 `{title}`、`{message}`、`{job_id}` |

## 通知配置

未配置 `notify_command` 时，如果系统存在 `notify-send`，`gpuq` 会发送桌面
通知。服务器通常没有桌面会话，建议准备自己的通知脚本：

```toml
[gpuq]
notify_command = [
  "/home/me/bin/gpuq-notify",
  "{title}",
  "{message}",
  "{job_id}"
]
```

任务完成、命令报错、取消或 OOM 重新排队时都会调用它。数组中的每一项
作为一个独立参数传给程序，不经过 shell 展开；脚本内部可接入邮件、企业
微信、Telegram 或其他 webhook。通知程序启动失败不会影响队列继续运行。
单个任务可用 `--no-notify` 关闭通知。
