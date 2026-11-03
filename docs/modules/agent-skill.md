# gpuq 操作技能

[返回工程总览](../README.md) · [工程规范](../../AGENTS.md)

覆盖文件：[SKILL.md](../../skills/gpuq/SKILL.md)、[UI 元数据](../../skills/gpuq/agents/openai.yaml)、
[远程参考](../../skills/gpuq/references/remote.md)、[集群参考](../../skills/gpuq/references/cluster.md)、
[恢复参考](../../skills/gpuq/references/recovery.md)。

## 职责与边界

给使用 gpuq 的 agent 提供任务操作流程，而非修改 gpuq 源码的工程规则。技能名 gpuq，
基础操作面向 0.3.0 CLI，简单池/--local/default_queue 明确标记为更新源码能力，覆盖选择队列、提交/确认、单机多卡、项目并发、SSH 快照、集群与故障处理。
不创建测试任务到用户真实队列，不自动扩展用户请求为集群部署或配置重置。

## 接口与数据

后续简单池支持 default_queue=cluster，操作技能需先确定默认路由；--local 显式回到独立本地队列。


SKILL.md 的 YAML name/description 供技能发现；正文选择工作流，references 按需读取。
UI 元数据提供显示名、简述及含 $gpuq 的调用提示，保留默认自动发现。
技能文件夹可独立复制，内部引用只依赖文件夹内资源；完整手册使用版本固定的公开链接。
用户可输入“用 $gpuq 提交两卡训练到 train 远程并查看状态”。队列前缀与任务 ID 必须成对保留。

## 关键流程与不变量

仓库 skills/gpuq 是维护来源；安装副本放在 ${CODEX_HOME:-$HOME/.codex}/skills/gpuq。
README 在功能列表提供入口，正文给出安装前提、完整目录复制命令、默认路径及本地两卡/远程续训/
集群排查的自然语言调用示例；升级时先比较本机副本，避免覆盖用户定制。其他 agent 加载器可复制
同一目录到其技能搜索路径；UI YAML 是可选的 Codex 元数据，不是执行依赖。
源码发行包通过 MANIFEST.in 包含技能，wheel/独立二进制不自动安装技能。

操作先确定本地/远程/集群与配置，再执行 CLI；识别同一任务的卡数与每卡显存，区分
提交成功和训练完成。模糊响应先查队列，不盲目重复提交。失联重排明确至少执行一次、
原输入恢复、旧执行取消确认和外部产物幂等边界；不自动修改用户已有重试策略。

## 失败与限制

安装技能不安装 gpuq、etcd 或 GPU 驱动，不启动 daemon、不迁移队列、不自动部署远程。
当前会话是否可发现新技能取决于 agent 的加载机制，必要时新建会话或刷新技能列表。
参考资料针对 0.3.0；版本不符先检查实际 --help。技能不是授权用户未请求的执行/取消操作。

## 修改与验证

变更 CLI、同步或故障语义时同步本技能对应参考文件，并人工走查本地两卡、远程续训、
集群响应丢失/旧执行等场景。检查示例参数与 parser，避免用文字匹配测试代替语义验证。
运行 python scripts/check_docs.py；有 skill-creator 时运行其 quick_validate.py skills/gpuq。
新增可执行脚本需隔离测试；本技能不含可执行脚本，不需要真实 GPU 或访问真实队列验收。
