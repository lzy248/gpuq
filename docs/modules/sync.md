# 增量内容缓存与任务快照

[返回工程总览](../README.md) · [工程规范](../../AGENTS.md)

覆盖文件：[src/gpuq/sync.py](../../src/gpuq/sync.py)。调用方：[remote](remote.md)。

## 职责与边界

选择项目文件、创建内容 manifest、仅传缺失内容、从缓存复制独立任务代码。
它不同步本地环境，不下载模型，不自动回传训练产物，也不是实时双向目录镜像。

## 接口与数据

snapshot_files(root, excludes, includes) 返回选定路径。Git 根采用 ls-files 的 tracked + 非忽略 untracked，
删除的 tracked 文件跳过；无 Git 时递归扫描。默认排除 .git、.venv、venv、__pycache__、*.pyc、
.env/.env.*、.ssh、.aws、.cache、*.safetensors、*.bin、*.pt、*.pth、*.ckpt、*.onnx、*.h5、
checkpoints、outputs、wandb、runs。用户 --sync-exclude 和 .gpuqignore 添加 glob，后者忽略空行/#注释，
不支持 ! 反选。显式 --sync-include 为项目内相对文件或目录，可覆盖所有排除规则（含 Git ignore）。
目录 include 不跟随目录符号链接，不支持 glob。preview 用于检查最终清单。

manifest 的键为规范 POSIX 相对路径；值为 {sha256,mode} 或 {link}。mode 仅保留 0777。
远程 blobs/<sha256> 存内容，missing_blobs 校验已有内容后返回缺失/损坏项；
write_blobs 只上传请求的已知哈希，每个内容只发一次；receive_blobs 校验 digest 后原子替换缓存。

### 上传与物化接口

| 入口 | 输入 → 输出 / 前置条件 |
|---|---|
| snapshot_files | 已 resolve 的 root、排除/包含规则 → 排序后的 Path 列表；不传输 |
| manifest_for | root + 文件列表 → 路径到 {sha256,mode} 或 {link} 的映射 |
| missing_blobs | manifest + cache → 缺失/损坏的唯一哈希列表；会创建 cache |
| write_blobs | root + manifest + 缺失哈希 + 二进制流 → 流式 tar；拒绝未知/重复请求 |
| receive_blobs | 二进制流 + 已存在 cache + 缺失哈希 → 校验后逐项原子落盘 |
| materialize | manifest + cache + 新建 destination → 独立文件/链接；调用方负责失败清理 |

文件选择与哈希遍历不是跨文件原子快照；传输期间变化的上传内容会因校验失败而中止。
缓存命中使用 manifest 指定内容。接收是逐 blob 提交，不是整批事务；后续成员失败不会撤回
前面成功缓存的内容。无 GC、无反向同步、无模型下载；这些边界不可在重构时隐式改变。

## 关键流程与不变量

缓存文件只读；任务快照用 copyfile，再恢复各自 mode，绝不硬链接到可写任务代码。
相同内容换名/改权限不重新上传；代码变化、删除会在新快照准确体现，旧快照不变。
远程任务修改代码不能污染缓存或其他任务。每次仍需读取/哈希本地文件；去重减少网络流量，
不消除磁盘 IO。checkpoint 显式包含后同样按内容去重。

拒绝绝对/.. 路径、文件/符号链接祖先冲突、绝对或含 .. 的链接、特殊 tar 成员、未知/重复 blob。
仅接受普通 blob 文件，hash 验证能识别传输期间本地内容变化；失败不提交任务。
源项目内链接仅限受支持的相对形式；链接目标没有被选中时可保持断链，不自动带入被排除的模型。
只保留选中文件需要的父目录和提交 cwd，不保留其他空目录；不保留所有者、ACL 或 xattr。

## 失败与限制

16 MiB 请求清单上限由 transport 强制。上传中断可能缓存部分完整 blob，后续可复用；
临时文件在异常时清理，进程被强杀可能留临时文件。内容缓存无自动清理/配额；
任务快照会占额外磁盘空间。代码和缓存都是同用户可信工作区，不防御有权限修改它们的恶意本地进程。
Git submodule 在 ls-files 中是目录项，本实现不递归展开，会按不支持的条目拒绝。
大型数据文件不一定匹配默认排除，请配置 .gpuqignore；显式 include 也会包含目录内敏感文件。

## 修改与验证

运行 `pytest tests/test_remote.py`；检查去重、mode、修改隔离、恶意路径/成员、校验失败、ignore/include。
改变默认排除列表时同步双语远程手册，避免静默改变 checkpoint 或权重传输语义。
