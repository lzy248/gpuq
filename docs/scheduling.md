# 调度模型与安全边界

## 1. 普通任务判定

对任务 `j` 和 GPU `g`：

```text
required(j) = estimated_peak_memory(j) + reserve(j)
```

基本可行条件：

```text
free_memory(g) >= required(j)
```

对本队列中已经启动、但 CUDA 尚未完全兑现显存请求的任务，还要扣除未兑现预留：

```text
outstanding(j) = max(0, estimated_peak(j) - observed_process_memory(j))
schedulable_free(g) = physical_free(g) - sum(outstanding(j))
```

并保留同卡 active jobs 与候选任务中最大的安全余量。这避免慢启动任务尚未出现在
`nvidia-smi` 时，daemon 误以为相同显存仍可重复分配。

若任务设置最大利用率，还要求：

```text
utilization(g) <= max_utilization(j)
```

条件连续满足 `stable_samples(j)` 次后才成为候选，启动前重新读取一次 GPU 状态。
默认每 10 秒采样、连续 3 个样本：三个样本本身横跨约 20 秒；若条件刚好在一次
采样后满足，则从满足条件到启动约需 20–30 秒。第三个样本后还会立即进行一次
权威复查，但该复查不额外等待一个采样周期。

## 2. 独占任务

`--exclusive` 额外要求：

- compute process 数为 0；
- 已用显存不超过 `exclusive_max_used_mb`；
- 利用率不超过 `exclusive_max_utilization`。

少量驱动、桌面或监控显存不等于存在 CUDA 训练，因此不要求绝对 0MB。

## 3. 优先级、最低负载与 best-fit

任务首先按：

```text
priority DESC, job_id ASC
```

处理。一个高优先级任务当前放不下时，不会阻塞能够运行的低优先级任务，这属于 backfill。

当前版本不为暂时放不下的高优先级任务预留未来显存；如果低优先级短任务
持续到达，高显存任务可能等待较久。需要严格防饥饿时，应暂停提交小任务，
限制它们的 GPU allowlist，或临时使用 `--exclusive` 配合空卡窗口。

单卡任务有多个候选 GPU 时，选择：

```text
argmin(utilization, free_memory - required_memory, gpu_index)
```

显存首先作为硬门槛；所有满足显存、安全余量、allowlist 和可选利用率上限的 GPU
中，优先选择当前 `GPU-Util` 最低的设备。利用率相同时，再使用“刚好放得下”
的 best-fit GPU，最后用物理编号保证结果确定。

`GPU-Util` 是 `nvidia-smi` 给出的瞬时设备利用率，不等价于长期平均负载；连续
稳定采样主要用于确认资源条件持续成立，最终排序使用最近一次采样值。

### 单机多卡任务

`--gpus N` 按相同排序选取 N 张不同且已通过稳定采样的卡。每张卡独立满足显存、
安全余量、利用率和独占条件。启动前第二次采样重新检查全部 N 张卡，全部通过后
在同一次 SQLite 领取操作中记录完整分配；任何一张不满足时都不领取。每张已分配卡
分别保留未兑现显存，daemon 重启后仍然有效；独占任务阻止全部 N 张卡上的队列回填。

命令只启动一次，`CUDA_VISIBLE_DEVICES` 包含按物理编号升序排列的全部分配卡。
请使用 `torchrun --standalone` 等分布式启动器，等待所有 worker 结束，并将终止信号
转发给在独立 session 中运行的 worker。取消操作向启动器的进程组发信号。
OOM 重试和孤儿恢复均释放完整分配，但保留请求的卡数。多机调度及 GPU 拓扑优化暂不支持。

## 4. 为什么不能真正等价于 Slurm

检查时还有 12GB，不代表启动 CUDA context 时仍有 12GB。其他用户可以在检查和分配之间启动任务，且他们不会读取 `gpuq` 的锁。因此只能降低竞态，不能消除竞态。

本工具采用：

1. 用户提供峰值估计；
2. 独立安全余量；
3. 连续稳定采样；
4. 启动前二次检查；
5. OOM 识别和重新排队；
6. 不使用占位 tensor，不侵占尚未真正使用的显存。

多个用户各自运行 `gpuq` 时，各自的 SQLite 和 `flock` 互不相识，仍可能同时启动任务。只有所有用户接入同一个服务，才能获得真正的全局原子分配。

## 5. 失败隔离

每个任务由独立 runner 和进程组承载：

- stdout/stderr 合并到任务日志；
- runner 将退出状态写回 SQLite；
- daemon 不等待单个任务，可以继续调度；
- `cancel` 只向该任务进程组发送信号；
- daemon 或终端重启不会删除队列和日志；
- runner 独立写回结果，因此 daemon 短暂重启时运行任务仍可结束并落库。

systemd unit 使用 `KillMode=process`，所以仅重启 daemon 不会杀死 runner/worker。
新 daemon 每轮检查 active 记录：runner 或 worker 任一仍存在就保留；二者都消失
且没有写入终态时，原子标记为 `failed`、`failure_kind=orphaned`。该条件更新
不会覆盖恰好同时由 runner 写入的正常完成状态。

## 6. OOM 重试

检测到常见 CUDA/HIP OOM 文本且开启 `--retry-on-oom` 时：

```text
new_estimate = max(ceil(old_estimate * 1.25), observed_peak + 256MB)
```

然后进入 `retry_wait`。只有 OOM 自动重排；数据错误、断言失败和模型代码异常保留为 `failed`，避免无限重试坏任务。

### 6.1 默认 auto 模式

省略 `--memory`（或显式使用 `--memory auto`）时，首次任务以 1MB 内部哨兵值
加 `reserve` 通过资源门槛。领取任务时记录该卡的 `launch_free_memory`；校准完成
之前，调度器先把这块启动空闲区对同一 gpuq 队列的其他任务视为已预留。对于
非独占的单卡长任务，当 worker 显存已经被持续观测至少 5 分钟，且最近 12 个样本位于
128MB 波动带内时，预留可收缩到已观测峰值，并继续保留至少 1024MB 波动空间。
后续峰值上涨会立即抬高预留下界。daemon 重启后内存稳定窗口重新计时；`--exclusive`
任务始终阻止同卡 backfill。该机制不能阻止未接入本队列的其他用户进程抢占显存。

多卡任务按卡汇总 worker 进程组及后代进程（包括 torchrun 的独立 session rank）
的显存，取各卡中的最大值作为峰值，
不把不同卡的显存相加。OOM 学习使用所分配卡中最小的启动空闲显存。
由于各 rank 可能在不同时刻初始化，多卡首次 auto 探测在本次尝试结束前保留每张卡
各自的启动空闲区；已知每卡峰值时可用 `--memory` 允许正常回填。

如果命令以可识别 OOM 退出，设日志中解析到的失败分配为 `requested`（无法解析
则为 0），新的任务显存下界为：

```text
max(
  ceil(old_estimate * 1.25),
  observed_process_peak + requested + 256MB,
  launch_free_memory - reserve + 256MB
)
```

下一次只有当 `free >= learned_memory + reserve` 连续满足稳定采样条件时才重试。
auto 模式不使用 `max_retries` 限制 OOM 次数，每次 OOM 都继续抬高下界；但它
绝不是任意错误重试：非 OOM 退出立即 `failed/command`，启动和 runner 异常也
立即失败。

申请 N 张卡时，若一次 OOM 后学出的每卡 `learned_memory + reserve` 严格大于
当前候选卡中第 N 大的总显存，调度器将任务终止为以下状态；若发现的卡数少于 N，
则继续等待：

```text
status=failed
failure_kind=oom_exceeds_device
```

这避免不可能运行的任务永久停留在 `waiting_memory`。OOM 识别依赖常见
CUDA/HIP 错误文本；自定义框架若吞掉或改写 OOM，gpuq 会按普通命令错误失败，
不会冒险无限重跑。

## 7. 峰值显存的局限

daemon 通过 `nvidia-smi` 采样峰值，多卡任务按 worker 进程组及后代汇总。以下情况可能低估：

- 单卡任务启动 CUDA 子进程，或多卡 rank 脱离进程树并被重新托管；
- 峰值短于采样间隔；
- 驱动不暴露进程级显存；
- 任务绕过可见设备范围使用未分配的 GPU。

所以历史峰值用于辅助下一次估计，不能替代 `--reserve`。
