# 集群输入复制与执行日志

[返回工程总览](../README.md) · [工程规范](../../AGENTS.md)

覆盖文件：[src/gpuq/cluster_transfer.py](../../src/gpuq/cluster_transfer.py)。

## 职责与边界

复用 [sync](sync.md) 内容哈希与校验，经 SSH 将输入持久缓存到执行节点。
输入传输不入队、不选主、不启动 worker；集群控制面必须等足够副本确认后才确认提交。

## 接口与数据

snapshot 返回 (manifest identity, manifest)，manifest 包含 files、relative_cwd、source_root。
prepare_local 验证并缓存本地内容；call 执行单次远程协议；serve 处理内部 --gpuq-cluster-call。
请求必须带匹配的 namespace；prepare 的响应包括 node/identity/namespace，调用方必须匹配预期节点，不能仅凭 SSH 成功推断副本位置。
read_log 只接受 execution key，通过节点执行数据库找到日志，不允许任意路径读取。

## 关键流程与不变量

先接收和校验 blob，再原子保存 manifests/<hash>.json。工作区后续修改不改变已保存 manifest。
每次实际执行必须复制独立快照，不能直接在共享 blob 中运行。
日志初次按行尾读取，后续按 offset 返回最多 256 KiB；记录结果只涉及读取，不改变训练状态。

## 失败与限制

SSH 中断可能留下已验证的缓存，不会留下已入队任务。没有自动重试任务或跨节点删除。
同一物理主机的不同目录不能当作独立故障域；管理员负责节点身份与 SSH profile 的对应关系。
日志 tail 与文件大小不是原子快照，保留现有日志跟踪的并发限制。

## 修改与验证

验证第二次上传无重复 blob、确认节点身份、manifest 校验、传输失败不入队、日志路径约束。
完整集群就绪仍取决于控制面、节点配置和执行协议；本模块不能单独提供自动接管。
