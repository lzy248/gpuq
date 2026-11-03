# 简单 GPU 池存储与 SSH 访问

[返回工程总览](../README.md) · [工程规范](../../AGENTS.md)

覆盖文件：[src/gpuq/pool_store.py](../../src/gpuq/pool_store.py)。

## 职责与边界

SQLiteStore 为单主 GPU 池提供与 EtcdStore 相同的 Record/CAS/租约接口，数据库仅位于主机
work_dir/control.sqlite3。SSHStore 经已有 SSH profile 访问它，无额外监听端口或 etcd 服务。
不复制控制数据库，不做主机故障接管；主机恢复后依靠原数据库继续。

## 接口与数据

get/scan 返回 Record；txn 检查 revision 后原子写入；grant/keepalive 管理有限租约；members=1。
records、leases、sequence 三张表记录 JSON、版本、租约截止与持久递增序列，WAL 模式。
所有调用使用 BEGIN IMMEDIATE，统一处理过期租约和受其约束的记录。每记录限 1 MiB。
SSHStore 使用持久子进程，--gpuq-pool-store 先读 config/namespace 再处理有界 JSON 行。
每 RPC 默认 15 秒，16 MiB；远程服务只允许 get/scan/txn/grant/keepalive/members。
配置必须是 namespace 相同的 sqlite 主机，不允许来访者指定任意数据库路径。

## 关键流程与不变量

只在主机本地打开 SQLite；不能经共享文件系统运行第二主机。SSH 工作者通过同一事务服务
共享全局队列，只有初始化 master 的 node_id 能当调度主。租约只用于会话失效和旧指令隔离。
写请求失败不自动重发；关闭失效 SSH 通道，下一个明确调用可重连。调用方先核对持久状态。
主机不可达时抛 Unavailable，不回退成本地独立队列；已启动 runner 仍写节点 journal。

## 失败与限制

需要工作节点能 SSH 到主机，提交主机能 SSH 到输入副本节点；凭据/连通性由部署者准备。
SSH 访问使用同一受信用户权限，不是多租户权限边界。单主数据库损坏/丢失需备份恢复。
RPC 断开可能发生在提交以后，不能用通信错误推断任务未提交。agent 退出由 OS 关闭 SSH 管道。

## 修改与验证

验证持久版本、并发 CAS、租约过期、事务回滚、真实子进程假 SSH、错误 namespace/后端拒绝，
以及固定主停止后工作节点不接管。测试入口为 pytest tests/test_pool_store.py。联动 [cluster](cluster.md)、[CLI](cli.md) 和简单池手册。
