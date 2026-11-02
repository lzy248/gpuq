# 远程可复用 Python 环境

[返回工程总览](../README.md) · [工程规范](../../AGENTS.md)

覆盖文件：[src/gpuq/environment.py](../../src/gpuq/environment.py)。调用方：[remote](remote.md)。

## 职责与边界

prepare_environment(workspace, snapshot, requirements, python) 在远程准备或复用 venv，返回环境目录。
这是可选的 profile.requirements 路径；未配置时复用 profile.python 或远程 PATH，用户自行维护环境。
不搬运本地虚拟环境、不安装驱动/系统包、不保证本地与远程 CUDA/系统库一致。

## 接口与数据

requirements 是已同步的项目内相对文件，必须自包含，不接受 -r/-c/-e 或对应长选项。
调用远程 python 读取版本、真实 executable 与机器架构，和锁文件字节一起 SHA-256 作为环境键。
workspaces/environments/<hash> 保存环境；同级 <hash>.lock 用 flock 串行初始化；
.gpuq-ready 只在 venv/pip 全部成功后写入锁文件内容。

### 与提交和 runner 的接口

prepare_environment 返回环境根目录 Path，不启动训练也不写任务 DB。remote.serve 用返回值
构造命令路径、VIRTUAL_ENV 和 PATH，再通过正常 submit 把它们保存到 Job.command/Job.env。
[runner](runner.md) 后续按记录执行，不重新解析 profile、不重新安装环境。
managed 模式追加的 PATH/VIRTUAL_ENV 会覆盖同名 --env；未托管 profile.python 模式则保留显式 PATH。
模型缓存变量与环境安装缓存不同：准备环境的 pip 固定使用 workspace/cache/pip，训练进程中的
PIP_CACHE_DIR 仍按 remote 的环境优先顺序解析。

.gpuq-ready 是安装成功标志，不是环境完整性校验；复用时不会重新核验已安装包。
安装命令带 --require-hashes、--disable-pip-version-check、--no-input，避免提交期间交互等待。
每个环境锁仅保护创建过程，不锁整个训练生命周期，任务主动改包仍会影响共享此环境的任务。

## 关键流程与不变量

调用远程 python -m venv，然后环境 Python -m pip install --require-hashes -r 锁文件。
依赖必须精确锁版本及下载内容哈希（例如 pip-compile --generate-hashes 的输出），pip 负责完整校验。
pip 缓存为 workspace/cache/pip；命令输出走 stderr，保留远程 JSON stdout。
相同锁+解释器复用环境；锁改变创建新环境，不就地升级旧环境，不影响运行中任务。
失败删除不完整环境，留旧完整环境。profile 的 index 配置/平台依赖仍由远程 pip 处理。

remote 为 managed 环境设置 VIRTUAL_ENV、PATH 并替换首个 Python 命令；不解析任意 shell 脚本
内的绝对解释器。任务仍可能主动修改其可写环境；这是协作型复用，不是不可变容器。

## 失败与限制

无远程 Python/venv/pip、锁文件不完整、下载失败都会在入队前失败。系统驱动和 GPU wheel 选择
须由用户提供兼容锁。无网络时仅能复用已有环境/可用 pip 缓存，并非保证 pip 离线解析成功。
如果准备后提交失败，已经准备的环境仍保留以便复用；环境与模型缓存均无自动 GC。
模型不由本模块下载：remote 注入 HF_HOME/TORCH_HOME/PIP_CACHE_DIR（显式 --env 优先，
其次远程进程现有同名变量，再其次 workspace/cache），由训练框架在远程下载并长期复用。

## 修改与验证

运行 `pytest tests/test_environment.py tests/test_remote.py`。包括离线空锁文件真实 venv/pip、
环境复用、锁变更、失败清理；测试不从外网下载模型/软件包，不证明真实 CUDA 兼容性。
