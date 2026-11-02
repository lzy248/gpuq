# 集群一致性存储适配器

[返回工程总览](../README.md) · [工程规范](../../AGENTS.md)

覆盖文件：[src/gpuq/coordination.py](../../src/gpuq/coordination.py)。

## 职责与边界

EtcdStore 封装 etcd v3 JSON gateway 的线性一致读取、CAS 事务、租约和成员查询。
投票、日志复制和多数派提交由独立部署的 etcd 提供，不在 Python 中重写 Raft。
由 [cluster](cluster.md) 调用，配合节点 journal 与输入复制组成可选跨主机调度。

## 接口与数据

Record(value, revision) 保存 JSON 对象和 mod_revision。get 返回 Record 或 None；scan 返回
前缀范围内的记录。txn(checks, puts, deletes, leases) 用 MOD revision 做原子比较更新，返回 bool。
每条 JSON 记录最多 1 MiB，单次响应最多 16 MiB；任务大文件不应写入 etcd。
客户端支持 HTTPS CA/证书/私钥及已有认证 token；这些连接信息不写任务日志。

## 关键流程与不变量

默认读取不启用 serializable，因此必须获得 quorum 的权威结果。每个调度写入调用方必须把
当前 leader Record 加入 checks，不能只根据本地角色缓存授权。记录 revision 防止删除重建后的 ABA。
读取会尝试配置的其他 endpoint，成功者优先供后续请求使用。写请求超时不自动重试；结果可能已提交，调用方必须先对账。lease keepalive 失效立即抛 Unavailable。
同一客户端观察到不同 etcd cluster_id 时拒绝继续，避免把不同数据库当作一个集群。

## 失败与限制

无多数派/网络失败/无效响应均抛 Unavailable；不能退回 SQLite 副本做调度写入。
members 只统计投票成员，不证明全部在线。TLS 与 etcd 用户授权需由部署者配置。
本适配器不负责启动 etcd、成员重配置、证书续期或恢复 etcd 数据目录。

## 修改与验证

需验证缺键/版本竞争、旧主写入失败、模糊提交结果、租约失效和 quorum 丢失。
协议模拟测试不能代替真实三成员 etcd 的分区/重连测试；完成情况必须分别报告。
