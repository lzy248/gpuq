<p align="center">
  <img src="assets/gpuq-logo.svg" width="180" alt="gpuq 标志">
</p>

<h1 align="center">gpuq</h1>

<p align="center"><strong>任务排好队，显存一空就开跑。</strong></p>
<p align="center">别再盯卡，让空闲显存自动叫醒下一个任务。</p>

<p align="center">
  <a href="README.md">English</a> · 简体中文
</p>

<p align="center">
  <a href="https://github.com/lzy248/gpuq/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/lzy248/gpuq/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-3776AB">
  <a href="LICENSE"><img alt="MIT 许可证" src="https://img.shields.io/badge/license-MIT-22c55e"></a>
</p>

`gpuq` 是一个轻量级 GPU 任务队列，用于训练、评估和批量实验。
提交命令后，它会监测可用显存，在资源满足条件时自动启动任务。
既可以用于单台工作站，也可以通过 SSH 管理远程服务器，或将多台机器组成统一 GPU 池。

## 适合谁？

- 在实验室共享 GPU 上运行实验的研究人员和学生。
- 需要批量训练、评估或调参，希望提交后自动排队运行的开发者。
- 同时维护多个项目，需要设置并发数量和任务优先级的用户。
- 经常使用本地和远程 GPU，希望统一提交、查看日志和管理任务的用户。

## 主要功能

- **自动调度。** 根据可用显存和利用率选择 GPU，支持连续采样和启动前显存余量设置。
- **单卡与多卡任务。** 为一个命令分配一张或同一主机上的多张 GPU，可配合 `torchrun` 使用。
- **显存学习。** 默认自动估计显存，遇到 GPU OOM 后学习并重试；也可指定显存需求和重试次数。
- **项目并发管理。** 按项目轮转启动任务，设置项目和总并发上限，并通过优先级安排紧急任务。
- **持久任务管理。** 保存任务状态、依赖、日志和通知，终端关闭或调度器重启后继续管理；
  支持交互式列表和便于脚本处理的 JSON 输出。
- **远程工作区。** 通过 SSH 提交和管理任务，增量同步代码，复用各节点的 Python 环境与模型缓存。
- **统一 GPU 池。** 可选跨主机调度，支持主节点选举、备节点接管、原主回切和可配置的失联任务重排。

- **Agent 技能。** 在对话中让 coding agent 提交和查看任务，见[安装与使用示例](#agent-使用技能)。

## 环境要求

- Linux
- NVIDIA 驱动和可用的 `nvidia-smi`
- CUDA 子任务应使用可见设备编号：单卡为 `cuda:0`，多卡按 local rank 选择设备

独立二进制不需要 Python；源码安装需要 Python 3.10+。Python 3.11+ 只使用标准库；
Python 3.10 会安装用于读取配置的轻量兼容依赖 `tomli`。

## 安装

当前版本为 **0.3.0**，可安装独立二进制或通过源码安装。

### 独立二进制（推荐，Linux x86_64）

```bash
VERSION=0.3.0
cd /tmp
curl -fLO "https://github.com/lzy248/gpuq/releases/download/v${VERSION}/gpuq-v${VERSION}-linux-x86_64.tar.gz"
curl -fLO "https://github.com/lzy248/gpuq/releases/download/v${VERSION}/gpuq-v${VERSION}-linux-x86_64.tar.gz.sha256"
sha256sum -c "gpuq-v${VERSION}-linux-x86_64.tar.gz.sha256"
tar -xzf "gpuq-v${VERSION}-linux-x86_64.tar.gz"
install -Dm755 "gpuq-v${VERSION}-linux-x86_64/gpuq" "$HOME/.local/bin/gpuq"
gpuq --version
```

确认 `~/.local/bin` 已加入 `PATH`，然后初始化：

```bash
gpuq init
gpuq doctor
```

Release 压缩包还包含示例配置、systemd unit、许可证和中英文 README。

### 源码安装（可选，适用于其他支持架构）

```bash
git clone https://github.com/lzy248/gpuq.git
cd gpuq
export PATH="$(python -m site --user-base)/bin:$PATH"
python -m pip install --user -e .
gpuq init
gpuq doctor
```

这行 `PATH` 设置让 Shell 能找到用户级安装的 `gpuq` 和 `gpuqd` 命令；放在安装前，
也能避免 pip 提示脚本目录不在 `PATH` 中。它只对当前终端及其子进程生效。
若希望新终端也能直接使用这些命令，可将同一行 `export` 添加到所用 Shell 的启动
文件中（例如交互式 Bash 的 `~/.bashrc`），然后打开新终端或重新加载该文件。

## 快速开始

为一个状态目录启动唯一常驻调度器：

```bash
gpuq daemon
```

提交任务。自动显存是默认模式，因此无需写 `--memory`：

```bash
gpuq submit \
  --name train-model \
  --gpu 0,1 \
  --reserve 1500 \
  --stable-samples 3 \
  --cwd "$PWD" \
  --env PYTHONPATH=. \
  -- python train.py --device cuda:0
```

已知峰值时，可以显式使用固定估计：

```bash
gpuq submit \
  --memory 12000 \
  --reserve 1500 \
  --retry-on-oom \
  --max-retries 2 \
  -- python train.py --device cuda:0
```

查看和控制任务：

```bash
gpuq list
gpuq show 1
gpuq logs -f 1
gpuq cancel 1
gpuq retry 1 --memory auto
```

### 同一个任务使用多张 GPU

使用 `--gpus N`（别名 `--gpu-count N`）申请本机 N 张卡。默认仍为单卡；单独写
`--gpu 0,1` 只是允许选卡的范围，不表示同时申请两张卡。

```bash
gpuq submit --gpus 2 --gpu 0,1,2,3 --memory 12000 --reserve 1500 \
  -- torchrun --standalone --nproc_per_node=2 train.py
```

这会等待两张符合条件的 GPU：扣除队列预留后，每张卡都至少有 13500MB 可用。
所有选中卡都通过稳定采样和启动前复查后才启动任务。`--memory`、`--reserve`
和 OOM 学习均按每张卡计算。训练程序本身需要支持多卡；gpuq 只启动一次命令。
请使用 `torchrun` 等会等待所有 worker 结束的启动器。

### 物理 GPU 和程序内设备编号

调度器选择物理 GPU 1 时，会设置：

```text
CUDA_VISIBLE_DEVICES=1
```

子进程只能看到这一张卡，并把它编号为 `cuda:0`。因此训练命令应使用 `cuda:0`，
不能继续使用原物理编号。

若分配到物理 GPU 1 和 3，子进程收到按物理编号排序的 `CUDA_VISIBLE_DEVICES=1,3`，
在程序内分别对应 `cuda:0` 和 `cuda:1`；分布式进程应使用自己的 local rank。
`gpuq list` 和 `gpuq show --json JOB_ID` 会显示全部已分配的物理 GPU 编号。

## 自动显存估计

默认 auto 模式在空闲显存稳定满足余量和初始探测需求后试运行任务。识别到 GPU OOM 时，
队列会提高显存估计并重新排队；其他命令错误会记录为失败，方便检查。
已知峰值时可用 `--memory N` 指定需求，显存和余量均按每张 GPU 计算。

显存学习、选卡和重试规则见[调度说明](docs/scheduling.md)。

## SSH 远程与项目并发

使用 `gpuq --remote user@host ...` 统一提交/查看/取消远程任务，默认增量同步独立代码快照，复用远程模型缓存。
详见 [远程工作区与环境](docs/remote_usage.md)。`daemon --background` 可显式后台启动。

`submit --project` 覆盖自动项目身份，`--project-limit N` 持久设置项目普通任务并发上限（默认 1）。
`daemon --max-running-jobs N` 限制所有任务总并发（0 不限制），`--scheduling fair|fifo` 选择项目公平或旧 FIFO。
总上限 2 时 A 单独可跑 2 个，A/B 竞争分为 1+1；运行任务不暂停。`projects --json` 查看策略与计数。

### 本地与远程统一 GPU 池

完成[集群配置](docs/cluster_usage.md)后，使用 `--cluster` 操作共享队列：

```bash
gpuq --cluster submit -- python train.py
gpuq --cluster list
gpuq --cluster logs -f 1
```

统一池由三个 etcd 投票成员协调，第三个可部署在不带 GPU 的轻量见证主机上。
每个任务在一台机器上运行。失联重排策略可配置；网络分区时新旧执行可能重叠，
重排参数和 checkpoint 处理方式见集群手册。

## 常用命令

| 命令 | 用途 |
|---|---|
| `gpuq init` | 初始化状态目录和示例配置 |
| `gpuq submit -- COMMAND...` | 提交命令；默认自动显存 |
| `gpuq list` / `gpuq ls` | 交互式或脚本友好的队列视图 |
| `gpuq show JOB_ID` | 显示完整状态和学习到的显存字段 |
| `gpuq logs -f JOB_ID` | 跟踪合并后的 stdout/stderr |
| `gpuq cancel JOB_ID` | 只停止该任务进程组 |
| `gpuq retry JOB_ID` | 重新排队终态任务 |
| `gpuq priority JOB_ID N` | 修改优先级 |
| `gpuq gpus` | 查看调度器采样到的 GPU 状态 |
| `gpuq daemon` | 启动常驻调度循环 |
| `gpuq doctor` | 检查状态、数据库和 GPU 发现 |
| `gpuq projects --json` | 查看项目并发策略和任务数量 |
| `gpuq --remote HOST list` | 通过 SSH 查看远程队列 |
| `gpuq cluster status` | 查看统一池的主节点与节点状态 |
| `gpuq --cluster list` | 查看共享队列 |

完整参数见 [CLI 参数手册](docs/cli_reference.md)。

## 作为用户服务运行

```bash
mkdir -p ~/.config/systemd/user
cp systemd/gpuqd.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now gpuqd.service
systemctl --user status gpuqd.service
```

如果 `gpuq` 不在 `~/.local/bin`，需要修改 `ExecStart`。仓库提供的 unit 使用
`KillMode=process`，仅重启 daemon 不会连带杀死独立 runner 和训练进程。

## 在共享 GPU 上使用

gpuq 管理你提交的任务，保持其他用户的进程正常运行。启动前检查可用资源，
`--reserve` 用于为共享 GPU 上的显存波动留出余量。
资源检查与显存采样的具体行为见[调度说明](docs/scheduling.md)。

## Agent 使用技能

给 coding agent 安装 [gpuq skill](skills/gpuq/SKILL.md)，就可以在对话中提交和管理 GPU 任务。
技能涵盖队列选择、单卡/多卡提交、状态与日志、远程代码/checkpoint 同步，以及等待或失败任务的排查。
远程和集群的详细说明按需读取。

### 安装到 Codex

先按上文安装 gpuq，再进入包含 `skills/gpuq` 的仓库根目录，复制完整技能文件夹：

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R skills/gpuq "${CODEX_HOME:-$HOME/.codex}/skills/"
```

默认安装位置是 `~/.codex/skills/gpuq`。已有同名技能时先对比文件，保留你的定制内容。
安装后新建会话或刷新技能列表。技能随仓库提供，单独安装 gpuq 二进制不会自动安装技能。

### 在对话中使用

提到 `$gpuq`，说明任务和目标队列即可，例如：

- “用 $gpuq 在本机申请两张卡，运行 `torchrun --standalone --nproc_per_node=2 train.py`。”
- “用 $gpuq 把训练提交到我的 `train` 远程 profile，带上 `checkpoints/last.ckpt` 继续训练。”
- “用 $gpuq 检查统一池里的 42 号任务为什么还在等待，并查看相关日志。”

使用非默认配置时附上配置路径。agent 会通过 CLI 完成所请求的操作，并返回任务 ID 和实际状态。
远程 profile 或统一池应已配置好；需要协助配置时，在请求中一并说明。

其他支持 `SKILL.md` 的 agent 可以将整个 `skills/gpuq/` 目录放到其技能搜索路径，
保留其中的 `references/`。`agents/openai.yaml` 是可选的 Codex 界面元数据，技能正文和参考文件可独立使用。

## 开发

从[工程总览](docs/README.md)了解架构和模块接口，文档维护规范见 [AGENTS.md](AGENTS.md)。

```bash
python -m pip install -e '.[dev]'
python scripts/check_docs.py
ruff check src tests scripts/check_docs.py
pytest
```

测试使用虚拟 GPU backend，不需要真实 GPU。欢迎贡献，详见
[CONTRIBUTING.md](CONTRIBUTING.md)；安全问题请按 [SECURITY.md](SECURITY.md) 报告。

## 文档

- [CLI 参数手册](docs/cli_reference.md) · [English](docs/cli_reference.en.md)
- [调度说明](docs/scheduling.md) · [English](docs/scheduling.en.md)
- [远程工作区与环境](docs/remote_usage.md) · [English](docs/remote_usage.en.md)
- [统一 GPU 池与主备切换](docs/cluster_usage.md) · [English](docs/cluster_usage.en.md)
- [变更日志](CHANGELOG.md)

## 许可证

MIT © Liu Zeyang 与 gpuq contributors。详见 [LICENSE](LICENSE)。
