# 全局队列、选主与节点执行器

[返回工程总览](../README.md) · [工程规范](../../AGENTS.md)

覆盖文件：[src/gpuq/cluster.py](../../src/gpuq/cluster.py)。

## 职责与边界

ClusterQueue 通过 [coordination](coordination.md) 保存唯一全局队列；ClusterAgent 同时作为
候选调度主节点和本机执行节点。当前主机是初始化时默认首选主，恢复后追平再交接。
三投票成员 etcd 提供多数派持久化；第三成员可不运行 gpuq、不安装 GPU。
不是把不同主机的显存相加，也不把一项多卡任务拆到不同主机。

## 接口与数据

load_cluster(Settings) 读取 [cluster] 的节点私有配置：node_id 默认 hostname，namespace 默认 default，
endpoints 必填，work_dir 默认 ~/.local/share/gpuq/clusters/<namespace>，lease_seconds 默认 15（至少10），
python 默认 python3，可选 requirements/ca/cert/key/token；nodes 将节点 ID 映射到 SSH profile 或 local。
全局策略由 cluster init 创建于 etcd，包含 preferred_node、max_running_jobs=0、scheduling_mode=fair、
snapshot_copies=2、failback_seconds=30，以及 failover_after_seconds=3600、failover_when_idle=True、
max_failover_retries=1。init 不覆盖已有策略；旧策略缺字段按失联重排关闭处理。

etcd 命名空间下 config/counter 保存策略和任务编号；jobs/<ID> 保存 Job 字段、提交参数、输入 manifest、
可用节点、执行历史；projects/<hash> 保存项目限额和持久轮次；registry 保存节点持久存储身份；
nodes/<ID> 与 leader 绑定租约。Record.revision 用于防止旧状态覆盖新状态。

ClusterQueue.submit 校验副本数并原子登记 ID/项目/任务。dispatch_one 复用本地公平策略，但把
assigned/starting/running 都计入全局活跃数；最终事务同时比较 leader、job、project、node、config。
WorkerDB 限制现有 Scheduler 只启动已获授权的本地任务。节点数据库单独存于 work_dir/queue.sqlite3。

## 关键流程与不变量

提交先验证本地参数，复制输入到至少两个不同节点，再写全局队列。远端传输见
[cluster-transfer](cluster-transfer.md)；模型仍使用节点缓存，任务输入不进入 etcd。
节点周期发布实际资源检查产生的 offer；主节点全局排序后指定同一节点的完整 GPU 组和唯一 execution。
执行节点比较当前 leader 任期与 job revision，将 assigned 原子接受为 starting 后才生成执行记录。
本地 submission_key=execution 防止重复入队。实际启动仍经过原有稳定采样/二次 GPU 检查。

主节点租约失效后由仍有 quorum 的候选争取 leader。新主只重新授权同一 assigned 执行编号和节点，
已有 runner 独立运行，断网结果留在本地。主节点 requeue_lost 在其他在线可运行工作清空或
失联超时后，可用独立持久预算替换未知执行，属于至少执行一次。默认最多一次；0 关闭。
原首选节点稳定发布 ready 达 failback_seconds 后，现任主比较双方版本并把 leader 绑定到其租约；
交接不重启训练。缺多数派不授权新工作，不能以本地缓存冒充权威队列。

OOM retry_wait 作为新的全局准入，保留学习结果，固定模式 max_retries 减去 attempt_base 后传给
新节点本地任务，防止换节点刷新预算。集群禁止本地自动 orphan 重排；确认 orphan 后上报失败，
人工 retry 仅接受终态。取消运行任务保留占用直到
节点确认。跨节点依赖只等待任务成功，不自动搬运产物；输出路径/共享数据应单独安排。

## 失败与限制

自动失联重排可能与仍在运行的旧训练重叠，不是物理 fencing。替代事务检查 leader/config/job/
原节点缺失/counter；历史 execution 写 superseded，清除当前路由后重新全局准入。
lost_since 用 UTC epoch 持久化，节点回来清除；各主节点需要同步时钟。failover_count 不随人工 retry
或 reset_attempts 重置。只有仍在线的输入副本能接收替代任务；取消请求不重排。
tick 先核对本地进程/开机时间以避免对重启前旧 PID 发信号，再 reconcile 取消本机历史 superseded 执行并写 cancellation_ack，不允许旧结果覆盖当前 revision。
任务环境附带 GPUQ_CLUSTER_JOB_ID/GPUQ_EXECUTION_ID/GPUQ_FAILOVER_COUNT；共享外部副作用需业务幂等。
节点 ID 绑定本地持久 journal，不能给空目录复用同一节点名。资源 offer 不是 GPU 驱动锁。
节点 max_orphan_retries 不用于集群；跨节点失联重排由独立的全局策略控制。首批实现不自动修改 etcd 成员、安装节点或迁移独立队列历史；现有 --remote 模式保持独立。
预览不提交任务；quorum 查询/协议测试需与真实三节点测试分别报告。

## 修改与验证

用户配置、命令、部署和限制见[集群手册](../cluster_usage.md)。

失联判断中，在线活跃任务与依赖就绪且有在线副本的 pending 都阻止 idle 条件（包括等待显存），
没有在线副本或依赖未成功的不阻止。超时不要求 idle；每 tick 最多重新排一项。

重点验证旧任期拒绝、去重、quorum 丢失、断线结果回报、首选主稳定回归、全局限额与输入副本确认。
修改全局状态需联动 CLI、DB 幂等键、Scheduler、输入传输和工程总览，不能只测试正常路径。
