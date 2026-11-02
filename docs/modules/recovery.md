# 有界失联恢复

[返回工程总览](../README.md) · [工程规范](../../AGENTS.md)

覆盖文件：[src/gpuq/recovery.py](../../src/gpuq/recovery.py)。

## 职责与边界

`requeue_orphan(db, job_id, limit) -> bool` 实现已经被 scheduler 判定失联的记录重排。
函数自身不检查 OS 进程；调用方负责判断失联。用户使用说明见 [断电恢复](../orphan_recovery.md)。
不替代训练 checkpoint，不用于普通 command/launch/OOM 失败的通用重试。

## 接口与数据

limit 默认由 Settings.max_orphan_retries 提供，默认 0 关闭。独立表 orphan_recoveries：
job_id 外键、recovery 序号、recovered_at（UTC ISO）、previous_attempt；主键 (job_id,recovery)。
每任务累计次数持久化，与 OOM 次数和 attempt 独立；人工 reset_attempts 不清此表。

## 关键流程与不变量

limit<=0 直接 False；否则同一 BEGIN IMMEDIATE 事务中懒建审计表、读任务、查计数。
仅 starting/running、未请求取消、计数<limit 的记录可重排。清完整 GPU 分配、各类 PID、
结果/错误及 started_at/finished_at，保留命令、资源、依赖、优先级、显存学习和 attempt。
一次 queued 更新与一条恢复审计一起提交，不经过临时 failed，避免依赖任务误判父任务失败。

scheduler 首先判断更新时间是否早于 Linux boot time；不是旧启动记录且 runner 或 worker
任一活着时保留。失联后先尝试恢复；关闭/用尽预算则 fail_if_active，避免覆盖晚到的终态。

## 失败与限制

重新启动同一命令不等于恢复模型/optimizer/随机状态，可能从头重算。
不恢复历史已经 failed 的任务，不重排取消或已终态记录。函数事务保护状态与计数，
但不复查 OS PID；失联判定仍受时钟/PID 复用等限制。

## 修改与验证

新增资源字段同步清理列表，并保留资源需求与依赖；改变计数必须明确跨 daemon 重启语义。
运行 `pytest tests/test_orphan_recovery.py tests/test_multigpu.py -k 'recovery or orphan'`。
模拟旧 boot、存活 worker、取消和晚写终态；不能把真实断电当成自动化测试前提。
