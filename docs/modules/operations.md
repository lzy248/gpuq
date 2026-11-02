# 构建、部署与仓库运维

[返回工程总览](../README.md) · [工程规范](../../AGENTS.md)

覆盖文件：[pyproject.toml](../../pyproject.toml)、[config.example.toml](../../config.example.toml)、[scripts/build_binary.sh](../../scripts/build_binary.sh)、[systemd/gpuqd.service](../../systemd/gpuqd.service)、[.github/workflows/ci.yml](../../.github/workflows/ci.yml)、[.github/workflows/release.yml](../../.github/workflows/release.yml)、[.github/ISSUE_TEMPLATE/bug_report.yml](../../.github/ISSUE_TEMPLATE/bug_report.yml)、[.github/ISSUE_TEMPLATE/config.yml](../../.github/ISSUE_TEMPLATE/config.yml)、[.github/release-notes/v0.2.0.md](../../.github/release-notes/v0.2.0.md)、[assets/gpuq-logo.svg](../../assets/gpuq-logo.svg)、[LICENSE](../../LICENSE)、[.gitignore](../../.gitignore)。

## 职责与边界

本组文件描述包依赖、发布产物、服务生命周期及协作元数据。
入口路由见 [entrypoints](entrypoints.md)，配置行为见 [config](config.md)，测试见 [testing](testing.md)。

## 接口与数据

源码发行清单：[MANIFEST.in](../../MANIFEST.in)。

| 文件 | 重要信息 |
|---|---|
| pyproject.toml | setuptools>=77，发行包名 personal-gpuq、导入名 gpuq；Python>=3.10；3.10 需 tomli；dev 包含 pytest/build/pytest-cov/Ruff 0.16.6；binary 包含 PyInstaller |
| MANIFEST.in | sdist 包含工程文档、AGENTS、检查器、测试及 fixtures、文档引用的配置/脚本/资产；wheel 仍以运行时包为主 |
| config.example.toml | 常用 [gpuq] 设置与 notify_command argv 示例，不是所有 Settings 字段全集 |
| scripts/build_binary.sh | PYTHON 默认 python3，GPUQ_BINARY_ARCH 默认 linux-x86_64；PyInstaller onefile，产物 dist/gpuq 和带版本 tar.gz/sha256 |
| systemd/gpuqd.service | 用户服务 ExecStart=%h/.local/bin/gpuq daemon；Restart=on-failure、5 秒；KillMode=process；PYTHONUNBUFFERED=1 |
| .github/workflows/ci.yml | main push、PR、手动触发；Python 3.10/3.11/3.12 lint/test；Python 3.12 包构建；文档检查在测试矩阵中执行 |
| .github/workflows/release.yml | v* tag 推送触发；Ubuntu 22.04/Python 3.10 构建 Linux x86_64 二进制，smoke test 后发布 |
| .github/release-notes/v0.2.0.md | 该版本发布说明；新 tag 需对应文件，不能把旧文件当新版本说明 |
| .github/ISSUE_TEMPLATE/bug_report.yml | 双语版本/复现/环境/日志表单，提醒脱敏 |
| .github/ISSUE_TEMPLATE/config.yml | 允许空白 issue |
| assets/gpuq-logo.svg | README 共用的矢量 logo；保持路径和有效 SVG |
| LICENSE | MIT 授权文本；打包必须保留 |
| .gitignore | 排除缓存、虚拟环境、dist/build、egg-info、spec、本地状态和日志；不要忽略应维护的源码/文档 |

## 关键流程与不变量

开发：`python -m pip install -e '.[dev]'`。Lint 锁定 Ruff 0.16.6 和 E4/E7/E9/F，
升级工具不能暗中改变规则集。
Python 包：`python -m build` 生成 sdist/wheel。
独立程序：先安装 `.[binary]`，再 `scripts/build_binary.sh`；脚本从 gpuq.__version__ 读版本，
归档包含 binary、两种 README、配置、systemd unit、LICENSE，计算 SHA-256。

发布时 tag（例如 vX.Y.Z）、包版本、__version__、notes 文件和归档版本必须一致。
工作流通过 tag 定位 dist/gpuq-TAG-linux-x86_64.tar.gz；已有 release 时 upload --clobber，
更新 title/notes；不存在时 create --verify-tag。它没有自动做版本同步。

安装用户服务时复制 unit 到 ~/.config/systemd/user，daemon-reload 后 enable --now。
自定义安装位置需改 ExecStart。KillMode=process 让只重启 daemon 不杀独立 runner/worker；
更新调度功能后应重启 daemon 才使用新代码。正式安装命令见双语 README。

## 失败与限制

二进制只在声明的平台构建/验证；GPUQ_BINARY_ARCH 只是命名，不会交叉编译。
构建脚本会清理该版本的 bundle 目录，PyInstaller 也会覆盖构建输出。
配置样例没有列出所有 auto/recovery/shared-GPU 项，默认值以 [config](config.md) 为准。
GitHub release 上传是外部写入；文档维护/本地测试不意味着授权发布或重启真实服务。
安全协作见 [SECURITY](../../SECURITY.md)，改动历史见 [CHANGELOG](../../CHANGELOG.md)。

## 修改与验证

改 CI、脚本或入口后运行对应 lint/test/build，脚本用 `bash -n scripts/build_binary.sh` 检查。
二进制额外运行 `dist/gpuq --version`、`--help`、`submit --help`、`--gpuq-internal-runner --help`。
修改服务杀进程语义联动 scheduler/runner 与 `pytest tests/test_scheduler.py -k systemd`。
只改文档时不发布 tag、不部署服务；结构校验使用 [文档检查器](documentation.md)。
