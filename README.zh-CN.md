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

`gpuq` 是面向无统一调度器共享服务器的轻量级个人 GPU 机会式任务队列。它监测
空闲显存，等待资源连续稳定后启动下一个任务，而且永远不会终止、暂停或占位抢夺
其他人的进程。

它刻意**不做迷你 Slurm**。Slurm 管理整个集群；`gpuq` 解决的是一个人在无人
管理的 GPU 服务器上不想继续手工盯卡的问题。

## 适合谁？

以下环境很适合 `gpuq`：

- 实验室或团队成员直接运行 CUDA 命令，共享一台工作站或服务器；
- 手里有一批训练、评估任务，但没有 Slurm 管理员；
- GPU 经常只占用一部分显存，希望安全利用剩余空间；
- 需要持久队列、日志、依赖、优先级、重试和通知；
- 无法安装 root 级服务，也不能接管其他用户的进程。

如果需要多机训练、跨用户配额、公平调度、记账、硬资源预留、抢占或管理员级隔离，
应使用 Slurm、Kubernetes 或其他正式集群调度器。

## gpuq 和 Slurm 有什么区别？

| | gpuq | Slurm |
|---|---|---|
| 管理范围 | 当前用户自己的机会式队列 | 整个集群的资源管理器 |
| 部署方式 | 用户态 Python + SQLite，无需 root | 管理员部署控制平面 |
| 资源权力 | 观察空闲显存，放得下才启动 | 分配并强制执行资源边界 |
| 对待其他用户 | 永不终止或暂停他人进程 | 协调所有纳管任务 |
| 部分空闲 GPU | 利用剩余显存回填任务 | 通常按配置资源分配 GPU |
| 公平、配额、记账 | 有意不提供 | 核心能力 |
| 多机/分布式分配 | 不支持 | 支持 |
| 最适用场景 | 无托管的共享 GPU 服务器 | 正式管理的计算集群 |

`gpuq` 无法阻止队列外用户在最后一次检查后抢先申请显存。它通过稳定采样、启动前
复查、安全余量和 OOM 学习降低竞态，但无法提供 Slurm 的硬保证。

## 主要特性

- **默认自动显存模式。** 无需手填预估；识别到 CUDA/HIP OOM 后学习更高下界，
  并在不超过单卡上限时不限次数等待重试。
- **明确的失败边界。** 非 OOM 的代码、数据和启动错误立即失败；学习需求超过所有
  允许单卡时终止为 `oom_exceeds_device`，不会永久等待。
- **机会式回填。** 显存是硬门槛；候选卡优先选择低利用率，利用率相同再 best-fit。
- **降低启动竞态。** 连续稳定采样、启动前二次快照和慢启动任务的未兑现预留共同
  降低队列内部超分配风险。
- **持久个人队列。** SQLite 保存任务，终端或 daemon 重启后仍可恢复；支持依赖、
  优先级、取消、日志和通知。
- **交互式列表。** 终端中 `gpuq list` 默认每页 20 条，左右方向键翻页，`q/Esc`
  退出；备用屏幕保证翻页不污染终端历史，管道和 JSON 始终保持非交互输出。
- **绝不抢占。** `gpuq` 只控制由自己启动的任务进程组。

## 环境要求

- Linux
- NVIDIA 驱动和可用的 `nvidia-smi`
- CUDA 子任务应使用可见设备编号：单卡为 `cuda:0`，多卡按 local rank 选择设备

独立二进制不需要 Python；源码安装需要 Python 3.10+。Python 3.11+ 只使用标准库；
Python 3.10 会安装用于读取配置的轻量兼容依赖 `tomli`。

## 安装

### 独立二进制（推荐，Linux x86_64）

```bash
VERSION=0.2.0
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

## 自动显存如何工作？

```text
queued
  │ 空闲显存连续满足 reserve + 1MB
  ▼
试运行 ── 成功 ───────────────────► completed
  │
  ├─ 非 OOM / 启动错误 ───────────► 立即 failed
  │
  └─ 识别到 CUDA/HIP OOM
          │ 学习更高最低显存
          ├─ 最低值 + reserve > 最大允许单卡 ─► failed/oom_exceeds_device
          └─ 否则 ─► retry_wait ─► 等待稳定空闲显存 ─► 再运行
```

OOM 后的任务显存下界取以下最大值：

```text
ceil(旧估计 × 1.25)
观测进程峰值 + 失败分配量 + 256MB
启动时空闲显存 - reserve + 256MB
```

只有明确识别出的 GPU OOM 才进入不限次数重试路径，避免坏命令永久循环。精确语义
和局限见[调度模型与安全边界](docs/scheduling.md)。

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

## 安全模型和局限

`gpuq` 永远不会：

- 终止、暂停或抢占无关 GPU 进程；
- 分配占位 tensor 来“预留”显存；
- 声称可以提供跨用户公平性或隔离；
- 保证队列外进程不会赢得启动竞态。

支持单机多 GPU 任务，不支持多机调度。按进程采样峰值可能低估脱离任务进程树的
worker 或极短峰值。执行昂贵的无人值守任务前，请阅读
[调度模型与安全边界](docs/scheduling.md)。

## 开发

```bash
python -m pip install -e '.[dev]'
ruff check src tests
pytest
```

测试使用虚拟 GPU backend，不需要真实 GPU。欢迎贡献，详见
[CONTRIBUTING.md](CONTRIBUTING.md)；安全问题请按 [SECURITY.md](SECURITY.md) 报告。

## 文档

- [CLI 参数手册](docs/cli_reference.md) · [English](docs/cli_reference.en.md)
- [调度模型与安全边界](docs/scheduling.md) · [English](docs/scheduling.en.md)
- [变更日志](CHANGELOG.md)

## 许可证

MIT © Liu Zeyang 与 gpuq contributors。详见 [LICENSE](LICENSE)。
