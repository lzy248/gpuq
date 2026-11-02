# SQLite 持久化与状态转换

[返回工程总览](../README.md) · [工程规范](../../AGENTS.md)

覆盖文件：[src/gpuq/db.py](../../src/gpuq/db.py)。

## 职责与边界

`QueueDB(path)` 是任务状态写入入口。初始化会创建父目录、schema 和缺失列；
不做 GPU 发现、不发信号。scheduler/runner/CLI/recovery 可在不同进程打开同一 DB。
字段语义见 [模型](models.md)，执行时序见 [调度器](scheduler.md)。

## 接口与数据

`jobs` 保存命令/env/allowlist/完整分配/逐卡启动空闲值的 JSON，其余为标量。
依赖引用 jobs.id；状态/优先级、依赖列有索引。
`connect` 使用 WAL、foreign_keys=ON、30 秒连接/忙等待超时、autocommit 模式。
`transaction(immediate=True)` 使用 BEGIN IMMEDIATE，成功 commit，异常 rollback，最后关闭连接。
恢复计数表由 [recovery](recovery.md) 懒创建，不属于 SCHEMA 常驻建表语句。

| 方法 | 关键契约 |
|---|---|
| submit | 必须有命令；memory>0、reserve>=0、stable>=1、retries>=0；校验 mode、utilization、gpu_count、allowlist 大小和依赖存在 |
| get | 返回新快照；不存在抛 KeyError |
| list | active 优先，其后 priority DESC/id ASC；默认最多 200 |
| active | list 的 starting/running 过滤，仍受默认 200 上限影响 |
| runnable | **有写入**：先把父任务失败的候选改为 blocked_dependency，再返回父任务完成或无依赖的候选，按 priority DESC/id ASC |
| claim | 在即时事务中核对完整卡数、唯一性/首卡/allowlist，条件 UPDATE；成功才 attempt+1，返回 bool |
| mark_running / set_worker_pid | 前者仅更新 starting；后者写实际命令 PID |
| update_peak | MAX 保留历史最高观测，更新 updated_at |
| finish | 只接受终态值，但 UPDATE 本身不要求旧状态 active；不可用于失联竞争判断 |
| fail_if_active / fail_impossible_memory | 带旧状态条件，避免覆盖已经写回的其他状态；返回是否更新 |
| request_cancel | 排队任务直接 cancelled；active 设置标志，信号由 scheduler.cancel_job 发出 |
| retry | 只允许终态；可更换 memory/mode，mode 显式给出时重置 OOM 学习元数据 |
| requeue_after_oom | runner 使用；保存新显存下界并增加 oom_count，不在这里决定重试策略 |
| set_priority | 不存在抛 KeyError，返回更新后 Job |

## 关键流程与不变量

| 转换 | 条件与写入方 |
|---|---|
| 新任务 → queued | CLI → submit |
| queued/waiting_memory/retry_wait → waiting_memory | scheduler 未找到可启动资源 |
| 同上 → starting | claim 原子写整组分配、启动容量、attempt+1 |
| starting → running | scheduler 启动 runner 后 mark_running；此时 worker 可能尚未出现 |
| running → completed/failed/cancelled | runner 记录命令结果；管理取消也可写 cancelled |
| running → retry_wait | runner 识别 OOM 并符合 auto/fixed 策略 |
| 候选 → blocked_dependency | runnable 发现父任务 failed/cancelled/blocked_dependency |
| active → queued 或 failed/orphaned | 失联核对：恢复有剩余预算，或 fail_if_active |
| 校准后的 auto 候选 → failed/oom_exceeds_device | scheduler 判断每卡容量不可能满足完整组 |
| 终态 → queued | 人工 retry；不自动追溯普通 failed |

清理规则：finish 清 PID/PGID/worker，但**保留 GPU 分配历史**；成功 auto 用 peak 校准 memory。
OOM、fail_if_active、fail_impossible_memory、人工 retry、失联恢复清第一张及完整 GPU 分配和
逐卡启动空闲值。释放资源实际通过不再属于 active 实现，没有驱动级锁。
人工 retry 保留 gpu_count/allowlist/命令，按显式 memory_mode 决定是否清学习元数据；
不自动清 peak，不自动清 started_at；reset_attempts 不重置 orphan_recoveries。

旧库按缺列 ALTER TABLE 迁移：memory_mode、OOM/启动容量字段、gpu_count 和多卡 JSON。
旧记录 gpu_count=1；没有 assigned_gpus_json 时从 assigned_gpu 恢复单元素 tuple。
事务领取保护的是一条任务的完整状态，不是跨用户 GPU 排他锁；每队列单 daemon 仍必要。

## 失败与限制

所有写入都有文件副作用，runnable 也不是只读查询。不要用真实队列测试迁移。
finish/requeue_after_oom/set_worker_pid 不是全面的 compare-and-set API；改变取消/完成竞争
必须增加真实交错测试。active 的 200 条上限限制大队列观察范围。
时间使用带 offset 的 ISO 字符串；已提交实现由 utcnow 生成 UTC 时间，恢复表也使用 UTC。
工作区可能有尚未提交的时间显示调整，核实行为时先检查 diff。
不要对字符串做时区无关的字典序时间比较。

## 修改与验证

schema、migrations、_job 和 Job/as_dict 必须同步。新增资源字段逐项核对上面的清理规则，
检查旧库已有 queued/running 记录，而不只是空库建表。
运行 `pytest tests/test_db.py tests/test_multigpu.py tests/test_orphan_recovery.py`。
当前迁移测试覆盖旧单卡分配、学习字段和重新打开数据库；修改并发路径需补相应竞争用例。
