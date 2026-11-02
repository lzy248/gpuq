# 任务执行器与 OOM 分类

[返回工程总览](../README.md) · [工程规范](../../AGENTS.md)

覆盖文件：[src/gpuq/runner.py](../../src/gpuq/runner.py)。

## 职责与边界

独立于 daemon 执行已经领取的命令、等待退出、写回状态与日志，并触发 [通知](notify.md)。
不自行选 GPU。进程启动入口见 [入口](entrypoints.md)，领取/取消协调见 [scheduler](scheduler.md)。

## 接口与数据

集群 runner 仍只读写节点 SQLite，不持有 etcd 凭据或主租约；agent 将结果上报全局队列。
独立运行语义保证单纯 agent/主节点切换不停止训练；启用失联替换后，回归 agent 会取消旧执行。
集群环境提供全局任务/执行编号与 failover_count，便于区分跨节点重排的输出。每个全局重试建立新 execution，节点 max_retries
按已用全局 attempt 扣减，学习显存和 oom_count 保留；auto OOM 规则不变。见[集群](cluster.md)。


`execute_job(db, job_id, *, state_dir) -> int` 返回命令退出码（启动失败为 127）。
内部 `main()` 接收 `--state-dir` 和 `--job-id`，打开该目录 queue.sqlite3。
`is_oom_failure`、`parse_oom_requested_mb`、`learned_auto_memory_mb` 是分类/估算测试入口。
`tail_text` 默认读取日志末尾 131072 字节，替换非法 UTF-8。

环境顺序：当前进程环境 → job.env → 强制设置完整 `CUDA_VISIBLE_DEVICES`。
物理 ID 逗号连接，子进程看到从 0 开始的可见编号；用户 env 不能覆盖最终分配。
命令以 argv、job.cwd 启动，不经 shell；stdout/stderr 合并并追加到 job.log_path。

### 远程快照与项目调度的执行边界

远程提交在入队前已把快照 cwd、解释器、PATH/VIRTUAL_ENV、模型缓存路径写入
Job.command/cwd/env；本模块不连接 SSH、不同步代码、不准备环境。
重试沿用同一记录与同一快照，不重新上传最新本地代码；需要新代码时应新提交任务。
删除快照或环境会破坏后续执行/重试，完成任务也不会自动触发缓存或工作区清理。

project_id 是调度身份，不影响 worker 的 cwd 或 CUDA_VISIBLE_DEVICES。
公平准入只由 scheduler 控制新启动；runner 不实现项目时间片、暂停或抢占。
OOM/普通失败分类仍使用现有规则，不能因为远程来源或项目竞争而增加额外自动重试。

## 关键流程与不变量

最多等 10 秒（每 0.05 秒轮询）让 starting 变 running，然后校验完整分配数等于 gpu_count。
worker 使用 start_new_session=True，与管理 runner 分离；登记 worker_pid 后再检查取消标志，
关闭 Popen 与 DB 登记之间的小竞态。命令应自行等待分布式 ranks。

结果判断顺序是取消标志 → exit 0 成功 → 非零时读取尾日志并判断 OOM。
识别标记（忽略大小写）：cuda out of memory、cuda error: out of memory、
cublas_status_alloc_failed、hip out of memory。普通错误归类 command，不自动无限重试。

失败分配解析 `Tried to allocate 数值 单位`，bytes/KB/KiB/MB/MiB/GB/GiB/TB/TiB，
按 1024 进位换成 MB、向上取整，取最大请求；不能解析时 None。
auto 新下界为 max(ceil(old*1.25), peak+requested_or_0+256,
max(1, launch_free_or_required-reserve+256))，整组重排、不设 OOM 次数上限。
fixed 只有 retry_on_oom 且 attempt<=max_retries 才重排；新下界 max(ceil(old*1.25),peak+256)。
最终容量可行性检查在 scheduler 的下一轮，不在本模块。

正常完成/失败记录 code；启动失败写 failure_kind=launch；内部异常在 main 尽力写 runner 后重新抛出。
_send 只在 Job.notify 为真时重新加载配置并调用通知。

## 失败与限制

日志按尝试追加，OOM 检测读取总日志尾部，没有严格按本次 attempt 隔离；旧 OOM 仍在尾部时
可能影响后续错误分类。训练框架隐藏 OOM 文本时按普通错误处理。
取消依赖启动器向独立 session rank 转发信号；gpuq 不自动把单卡脚本转换为 DDP。
通知命令异步启动，不检查其最终退出状态。日志时间由 DB utcnow 生成，保留 UTC offset；logs 命令原样输出，不重写日志时间。

## 修改与验证

改 OOM 分类同时核对 db.requeue_after_oom 清理、scheduler 不可能显存判断与两种重试预算。
改 worker 生命周期必须覆盖取消发生在启动/登记附近、普通失败和多卡环境。
运行 `pytest tests/test_runner.py tests/test_multigpu.py`；不使用真实训练作唯一验收。
日志跨尝试分类是现有限制，测试没有证明它已经隔离。
