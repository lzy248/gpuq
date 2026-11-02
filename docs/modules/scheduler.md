# 调度器、资源预留与进程管理

[返回工程总览](../README.md) · [工程规范](../../AGENTS.md)

覆盖文件：[src/gpuq/scheduler.py](../../src/gpuq/scheduler.py)、
[src/gpuq/background.py](../../src/gpuq/background.py)。

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

### 项目准入接口与持久状态边界

order_projects(jobs) 输入为 DB.runnable 已处理依赖后的候选，返回过滤排序后的 Job 列表，
函数本身只读 active/project_policies，不领取任务、不更新轮次。计数单位为任务，不是 GPU；
一个多卡任务仍只占一个并发名额。项目活跃数包括该项目所有优先级的 starting/running。
资源检查在 order_projects 之后执行，因此该函数的返回值不保证任务已经可启动。

| 调用路径 | 是否修改项目策略 / 轮次 |
|---|---|
| submit --project-limit | 更新项目持久上限，影响同项目后续所有普通任务 |
| order_projects / dry-run | 不修改；其他 tick 步骤仍可能写任务状态 |
| DB.claim 成功 | 与 starting、整组分配、attempt 在同一事务推进轮次 |
| DB.claim 失败 | 不推进轮次 |
| launcher 在 claim 后失败 | 任务失败，但已消耗的轮次不回退 |
| retry / OOM / 失联恢复 | 保留 project_id 与原策略；重新参与准入 |

修改顺序/限额需跑 tests/test_projects.py；后台生命周期还需
 tests/test_environment.py 和 tests/test_remote.py 中的脱离 SSH、锁复用用例。
项目公平不替代原有首次/二次 GPU 检查，不能将正优先级解释为绕过物理约束。

## 关键流程与不变量

一个 tick 顺序是 reap_runners → reconcile_active → 首次 sample → refresh_peaks → runnable →
fail_impossible_auto_jobs → order_projects → decisions → 第二次 sample → claim → launcher。
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

默认 fair：总活跃数（starting/running，含所有优先级）达到 max_running_jobs 时停止新启动；
0 不设总上限。普通项目默认上限 1，submit --project-limit 可持久更改。
竞争项目集合来自活跃任务与依赖满足的候选（priority<=0）；设置总上限时，每项目上限
收紧到 min(项目限制, ceil(总上限/竞争项目数))。优先级从大到小，0 优先级按项目当前
活跃数、last_served、任务 ID 排序。正优先级绕过项目限额，但不绕过总上限和 GPU 约束。
负优先级排在 0 后且仍受项目限额。fifo 保留 priority/ID 顺序、忽略项目限额，仍遵守总上限。
总上限 2、A 项目限制 2：单独可 A+A；A/B 同时等待时 A+B。后来 B 到来时不暂停 A，
下一空位优先给 B。资源暂时不足的竞争项目仍保留公平份额，可能留下空位；依赖未满足的项目不参与。
持久轮次由 [DB](db.md) claim 更新，重启不丢轮次；稳定窗口仍不持久化。

background.start_daemon 显式后台启动：检查同一 flock，已有 daemon 返回并保留其旧配置；
否则使用当前 gpuq 入口和完整配置路径启动新 session，stdin=/dev/null，输出写 state_dir/daemon.log，
最多等待 10 秒确认锁。锁竞争由真正的 daemon 裁决；不自动重启现有 daemon，也不停止 worker。
就绪只表示已持锁，不代表 NVIDIA 监控正常；失败/超时需查看 daemon.log。

## 失败与限制

`dry_run` 在决策之后才返回，之前的失联、峰值、依赖、不可能显存检查仍写 DB。
queue 预留只对同队列起作用；第二次快照不能排除外部抢显存。
启动异常写 `failed/scheduler` 并重新抛出。失联依赖 runner/worker PID 和 boot time，不是进程身份
的完整证明；时钟回拨/脱离并重托管的子进程可能影响判断。
取消只向已知进程组发信号，独立 session rank 依靠启动器转发，不能承诺清理任意 daemonized 后代。
活跃扫描无条数上限；GPU 资源约束下仍无严格防饥饿或拓扑优化。

## 修改与验证

修改资源策略必须同时覆盖首次决策、第二次检查、逐卡预留、重启重建和单卡兼容。
测试入口：`pytest tests/test_scheduler.py tests/test_multigpu.py tests/test_exclusive_external_sharing.py tests/test_orphan_recovery.py`。
FakeClock 用于稳定窗口，FakeBackend 用于资源变化；启动回调需模拟 mark_running，否则下一轮
会认为没有进程。实际 CPU 子进程测试验证 rank 环境/取消，但不证明真实 CUDA 训练。
