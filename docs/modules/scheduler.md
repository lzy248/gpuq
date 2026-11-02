# 调度器、资源预留与进程管理

[返回工程总览](../README.md) · [工程规范](../../AGENTS.md)

覆盖文件：[src/gpuq/scheduler.py](../../src/gpuq/scheduler.py)。

## 职责与边界

消费 [GPU](gpu.md) 快照和 [DB](db.md) 候选任务，选择可启动的整组卡、启动 runner、
核对活跃记录。实际命令与 OOM 解释属于 [runner](runner.md)，失联重排属于 [recovery](recovery.md)。
用户调度说明见 [中文](../scheduling.md) / [English](../scheduling.en.md)。

## 接口与数据

`Scheduler(settings=..., db=None, backend=None, launcher=None, process_checker=None, clock=None)`
允许注入 GPU、启动回调、存活检查与单调时钟。launcher 签名仍为 `(Job, first_gpu_index)`；
完整多卡集合从已领取 Job.assigned_gpus 读。`tick(dry_run=False)` 返回一个 LaunchDecision 或 None。

LaunchDecision 中 gpu_indices 是整组升序物理 ID，gpu_index 是第一张兼容值；多卡 free/stable
是组内最小，required/outstanding/utilization 是组内最大摘要。不能把这些摘要当逐卡原始数据。
GPUReservation 按物理卡保存 outstanding_memory_mb、reserve_mb、active_jobs、exclusive_active。
内存中的 stability 按 (job_id, GPU index) 计数；AutoProbeObservation 记录持有起点与近期样本。

## 关键流程与不变量

一个 tick 顺序是 reap_runners → reconcile_active → 首次 sample → refresh_peaks → runnable →
fail_impossible_auto_jobs → decisions → 第二次 sample → claim → launcher。
每 tick 最多启动一项，之后清零稳定计数；低优先级可在高优先级暂时放不下时 backfill。

每卡 `schedulable_free=max(0, free-outstanding)`；
`required=job.memory_mb+max(job.reserve_mb, active_card.reserve_mb)`。
普通任务还需 allowlist、可选利用率上限，并避开 active exclusive 卡。
独占默认要求无队列任务、无 compute 进程、used<=768MB、util<=5%；
exclusive_shared_gpus 只放宽显式卡上的外部进程/闲置检查，仍禁止与队列任务共卡、仍检查资源。

各卡达到 stable_samples 后按 (utilization, free_after_required, physical_index) 排序，选前 N 张，
最终按物理 ID 升序传递。第二份快照重新校验每张选中卡；任一不合格则整组不领取。
稳定计数不持久化；缺失设备的计数被移除。

普通预留为每卡 `max(0, memory_mb-observed)`，叠加该卡各活跃任务，安全余量取最大而非求和。
未校准 auto 首先保留启动时各卡空闲区域；只有非独占单卡探测可在默认持有 300 秒、12 个
样本波动<=128MB后缩到 max(memory,peak,observed)，保留至少 1024MB margin。
多卡未校准探测始终保留各卡自己的启动区域，避免不同 rank 延迟初始化造成提前回填。
单卡探测稳定窗口从首次有效正数显存观测起算，缺测或非正值会移除窗口并重新计时。

多卡观测按 worker 自身/同组进程/`/proc` 后代汇总每卡用量，再取各卡最大更新 peak。
通过父子关系识别 torchrun 的独立 session rank；单卡仍主要按 worker PID 观测。
校准 auto 的需求若超过第 N 大候选卡总容量则失败；发现卡数<N时等待而非推断永不可能。

run_daemon 在状态目录持有非阻塞 flock；冲突抛 RuntimeError。SIGTERM/SIGINT 请求停止循环，
不杀 worker。监控 OS/subprocess 异常写 stderr；sleep 使用单调时间并夹到非负。
取消先写 DB 标志：排队任务直接终态；运行任务 SIGTERM worker 组（缺 worker 时退到 runner 组），
默认等 10 秒，必要时重新读取 worker PID 后 SIGKILL，再写 cancelled。

## 失败与限制

`dry_run` 在决策之后才返回，之前的失联、峰值、依赖、不可能显存检查仍写 DB。
queue 预留只对同队列起作用；第二次快照不能排除外部抢显存。
启动异常写 `failed/scheduler` 并重新抛出。失联依赖 runner/worker PID 和 boot time，不是进程身份
的完整证明；时钟回拨/脱离并重托管的子进程可能影响判断。
取消只向已知进程组发信号，独立 session rank 依靠启动器转发，不能承诺清理任意 daemonized 后代。
活跃扫描目前受 DB 200 条默认限制；没有严格防饥饿或 GPU 拓扑优化。

## 修改与验证

修改资源策略必须同时覆盖首次决策、第二次检查、逐卡预留、重启重建和单卡兼容。
测试入口：`pytest tests/test_scheduler.py tests/test_multigpu.py tests/test_exclusive_external_sharing.py tests/test_orphan_recovery.py`。
FakeClock 用于稳定窗口，FakeBackend 用于资源变化；启动回调需模拟 mark_running，否则下一轮
会认为没有进程。实际 CPU 子进程测试验证 rank 环境/取消，但不证明真实 CUDA 训练。
