# 数据模型与序列化

[返回工程总览](../README.md) · [工程规范](../../AGENTS.md)

覆盖文件：[src/gpuq/models.py](../../src/gpuq/models.py)。

## 职责与边界

定义冻结 dataclass 和状态字符串；不做 IO、不执行状态迁移。
[db](db.md) 将 SQLite 行转成 Job；[GPU](gpu.md) 将采样转成 GPUState；
CLI/scheduler/runner 共用这些契约。冻结对象是快照，env/dict 内容并非深度不可变。

## 接口与数据

集群在 Job JSON 外保存 node/execution/manifest/generation/attempt_base 等路由元数据，
另保存 lost_since（UTC epoch）、failover_count 和 executions 内 superseded/cancellation_ack 审计；
show 把时间转为本地 offset，原始存储不改。并扩展 assigned 状态（已分配、未接受），不写入本地 JobStatus 枚举。全局编号与节点 SQLite
编号不同；只有 execution/submission_key 可跨边界对应，见[集群](cluster.md)。


`GPUProcess(pid, used_memory_mb)` 是一条进程显存观测。
`GPUState(index, uuid, name, total_memory_mb, used_memory_mb, free_memory_mb,
utilization_percent, temperature_c, processes=())`；compute_process_count 为进程条目数。
物理 index 不等于训练子进程的可见编号；UUID 用于拼接 nvidia-smi 两份 CSV。

| Job 字段组 | 字段及约定 |
|---|---|
| 身份/命令 | id, name, command（argv tuple）, cwd, env, project_id（稳定字符串，JSON 同名） |
| 选卡 | gpu_allowlist（None 表示任意）, gpu_count（默认 1） |
| 每卡资源 | memory_mb（正数）, memory_mode（fixed/auto）, reserve_mb, max_utilization, stable_samples, exclusive |
| 队列 | priority, status, dependency_id |
| 重试 | max_retries, retry_on_oom, attempt（领取时 +1）, oom_count |
| 分配 | assigned_gpu（第一张兼容字段）, assigned_gpus（完整 tuple） |
| 进程 | pid/pgid（runner）, worker_pid（命令 PID，也作为其组 ID） |
| 结果 | exit_code, cancel_requested, log_path, error, failure_kind, notify |
| 显存学习 | peak_memory_mb, launch_free_memory_mb, launch_total_memory_mb, last_oom_requested_mb, launch_free_memory_by_gpu |
| 时间 | created_at, updated_at, started_at, finished_at；带 offset 的 ISO 文本，后两项可空 |

多卡的 scalar launch free/total 是本次各卡中的最小值；launch_free_memory_by_gpu 保留各卡
各自值。多卡 peak 是每卡进程用量之和中的最大值，不是跨卡总和。

## 关键流程与不变量

`required_free_memory_mb = memory_mb + reserve_mb` 是基础需求；调度器实际还会考虑
同卡已有任务的最大 reserve 和未兑现预留。
`is_auto_memory` 检查 mode；`is_uncalibrated_auto_probe` 同时要求 auto、oom_count=0、memory_mb=1。
`as_dict()` 返回 JSON 友好字典：command/allowlist/assigned_gpus 转 list；空分配是 `[]`；
JSON 编码会把按 GPU 索引的 dict 键变为字符串。模型 as_dict 保留原始时间字符串；
CLI 在副本中把 show/list 输出时间转成本地时区，不改动 Job 或数据库。

project_id 在 dataclass 中默认空字符串仅用于构造兼容，正常 DB 提交会推导/校验非空身份。
它不是必须存在的路径（显式 ID 或远程 hostname:root 都合法）；项目并发策略不存到 Job，
由 projects 表按身份共享。JSON 增加 project_id，retry/恢复保留它，见 [项目识别](projects.md)。

JobStatus 的值为 queued、waiting_memory、starting、running、retry_wait、completed、failed、
cancelled、blocked_dependency。后四项属于 TERMINAL_STATUSES，详见 [状态转换](db.md)。

## 失败与限制

dataclass 不代替提交/数据库校验，不能认为手工构造任意 Job 就安全。
进程、取消标记会变化，worker 启动前后应重新读 DB。保留 assigned_gpu 是兼容约束，
新增多卡逻辑不能只读第一张卡。终态保留分配用于历史不等于仍预留 GPU。

## 修改与验证

加字段检查 dataclass 默认值、as_dict、schema/migration、_job、submit、claim、retry、
OOM/失联清理、CLI 及测试 fixtures；使用 [数据库](db.md) 清理矩阵逐项核对。
运行 `pytest tests/test_db.py tests/test_multigpu.py tests/test_cli.py`。
