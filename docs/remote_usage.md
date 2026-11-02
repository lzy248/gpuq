# 本地与远程统一使用

本页 --remote 是独立远程队列；如需本地与远程统一调度和主备切换，使用 [--cluster](cluster_usage.md)。


[工程总览](README.md) · [English](remote_usage.en.md) · [CLI 手册](cli_reference.md)

两端安装支持此功能的 gpuq，配置好 OpenSSH 密钥和主机验证后，本地直接使用：

```bash
gpuq --remote user@gpu-server daemon --background --max-running-jobs 2
gpuq --remote user@gpu-server submit --project-limit 2 -- python3 train.py
gpuq --remote user@gpu-server list --json
gpuq --remote user@gpu-server logs 1 -f
gpuq --remote user@gpu-server cancel 1
```

所有全局参数放在子命令之前。--remote 可为 IP、user@host、SSH config 别名或本地配置的 profile。
任务 ID、依赖、项目公平限额和总并发只属于选定远程队列；多张 GPU 指同一远程主机上的多卡。
命令、环境变量通过 stdin 传送，无隐式 shell。使用 shell 时显式写 bash -lc，并自行处理脚本中的路径。
远程安装必须兼容此协议；连接失败不会回退本地执行，也不会自动重复提交。若响应丢失，先查 list。

## 持久工作区、增量代码与模型缓存

默认从本地 cwd 自动寻找 Git 根（其次 pyproject.toml/package.json，最后 cwd），
计算文件 SHA-256，与远程内容缓存协商后只发送缺失内容。每个任务建立独立可写快照，
后续同步、删除、修改不会改变已提交任务的代码；相同内容只上传一次，改权限/改名也可复用。
这不是双向实时镜像，训练产物不会自动下载回来。

默认远程根目录为 ~/.local/share/gpuq/workspaces，可通过 --remote-workdir 或 profile.work_dir 修改。
其中 job-* 为任务快照/默认相对产物目录，blobs 为内容缓存，environments 为依赖环境，cache 为框架缓存。
--env 可显式设置 HF_HOME/TORCH_HOME/PIP_CACHE_DIR，否则沿用远程进程已有同名变量，
没有时使用 workspace/cache 下的 huggingface、torch、pip。训练脚本通过远程 HF 下载模型后
可复用缓存；指定模型 revision 有助于复现。gpuq 不主动下载模型，也不复制本地模型缓存。

Git 项目遵循 Git ignore（已跟踪文件仍参与），额外默认排除：.git、.venv、venv、__pycache__、
*.pyc、.env/.env.*、.ssh、.aws、.cache、*.safetensors、*.bin、*.pt、*.pth、*.ckpt、*.onnx、*.h5、
checkpoints、outputs、wandb、runs。项目的 .gpuqignore 和重复 --sync-exclude 可追加排除 glob；
.gpuqignore 支持空行/#注释，不支持 ! 反选。大型数据目录需自行加入排除，外部数据使用远程绝对路径。

```bash
# 不连接远程，先检查上传文件清单
gpuq --remote train submit --sync-preview -- python3 train.py
# 显式带上恢复训练所需 checkpoint；路径相对于项目根，覆盖 ignore 规则
gpuq --remote train submit --sync-include checkpoints/last.ckpt -- \
  python3 train.py --resume checkpoints/last.ckpt
# checkpoint 已经在远程时不传输它
gpuq --remote train submit -- python3 train.py --resume /srv/checkpoints/last.ckpt
```

--sync-include 接受项目内相对文件/目录，不支持 glob；显式包含目录会连同其中被忽略的文件上传。
选择性传输并非秘密扫描，应使用 preview 核对。符号链接只支持不含 .. 的项目内相对链接。
源码同步不递归展开 Git submodule；此类目录条目会被拒绝，应明确排除或用既有远程 checkout。
普通空目录不保留，但提交 cwd 会创建。快照与缓存无自动清理；释放磁盘前确认没有运行、重试或依赖任务引用它们。

## 配置远程环境

本地选项覆盖同名 profile；没有 profile 时 --remote 的值直接作为 SSH 目标。

| profile 字段 | 默认值 / 所在主机 |
|---|---|
| host | profile 名称本身，或 --remote 的字面目标 |
| port / identity | 不传给 SSH 时沿用 OpenSSH 配置；identity 是本地路径 |
| command | 先查远程 PATH 中的 gpuq，找不到则用 ~/.local/bin/gpuq |
| config / state_dir | 不显式传递时由远程配置加载器按其环境/默认值选择 |
| work_dir | 远程 ~/.local/share/gpuq/workspaces |
| cwd | 无默认值；仅供 --no-sync 使用 |
| python | 不托管环境时不替换命令；启用 requirements 且未指定时用远程 python3 |
| requirements | 默认不管理环境；设置为同步项目根内的依赖锁文件相对路径 |


本地 ~/.config/gpuq/config.toml（或 --config/GPUQ_CONFIG）示例：

```toml
[remotes.train]
host = "user@gpu-server"
# port = 22
# identity = "~/.ssh/id_ed25519"
command = ["~/.local/bin/gpuq"]
config = "~/.config/gpuq/config.toml"
state_dir = "~/.local/state/gpuq"
work_dir = "/srv/my-gpuq-workspace"
python = "/opt/conda/envs/training/bin/python"
# requirements = "requirements.lock"
```

未配置 requirements 时使用已有远程环境；首个 python/python3.x 命令替换为 profile.python，
并将其 bin 加到 PATH（显式 --env PATH 优先），torchrun 等命令可从该环境找到。
不要把本地 venv/Conda 目录上传到远程。未配置 python 时命令使用远程 PATH。
本地 --config 只选择连接 profile，--remote-config 指定远程配置，--state-dir 在 --remote 模式指远程路径。
不会把本地 [gpuq] 配置自动覆盖到远程。

启用 requirements 后，锁文件必须随代码同步、自包含且包含精确版本与内容哈希，例如
pip-compile --generate-hashes 生成的锁文件。远程以指定 Python 创建 venv，执行 pip install --require-hashes。
相同锁和解释器复用环境，锁变化新建环境；失败则在入队前终止，不更改已有环境。
不支持锁文件中的 -r/-c/-e；这不管理 NVIDIA 驱动、系统库或跨平台 CUDA wheel 兼容性。
managed 环境设置 VIRTUAL_ENV/PATH（覆盖同名 --env）并替换首个 Python 命令；
脚本内部硬编码的绝对解释器不重写。安装依赖时 pip 缓存固定在 workspace/cache/pip。

已有远程 checkout 可完全跳过同步：

```bash
gpuq --remote train submit --no-sync --remote-cwd /srv/project -- python3 train.py
```

也可在 profile 设置 cwd。no-sync 与配置了 requirements 的 profile 不兼容，会拒绝提交；
请改用只设置 python 的 profile。no-sync 也不注入 workspace 的模型缓存默认值，沿用远程环境或显式 --env。
此模式训练读取共享 checkout，代码变化由用户管理。

## 调度服务与公平性

--background 显式启动脱离 SSH 的 daemon，输出写远程 state_dir/daemon.log。已有实例会直接复用，
不会应用新参数或重启；更改已有服务配置后需按原服务管理方式重启 daemon。持锁就绪不代表 GPU 检查成功。
训练 runner/worker 独立于客户端，关闭 SSH 不取消训练。

默认普通任务按项目公平分配新的启动机会；项目限制默认 1，--project-limit 2 持久改为 2。
总上限 2 时，只有 A 可 A+A；A/B 都待运行时 A+B。B 后来进入且 A+A 已运行时，不暂停训练，
下一空位给 B。--priority 正数优先，但不突破总上限、显存和多卡约束。
详见 [调度语义](scheduling.md)。
