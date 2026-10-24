# Evolving Harness

以用户为中心、基于 Pi 执行的可扩展编程 Harness。模型权重保持不变，通过分范围记忆、版本化工作流、复盘策略和独立评估改善 Agent 的运行方式。

当前版本 **0.2.0**。已实现可运行的管理与验证闭环；尚未证明系统能在广泛任务上持续提高成功率。真实对照的失败、平局和无结论均保留。

## 快速启动

需要 Python 3.11+。离线测试和演示只用 Python 标准库：

```powershell
git clone https://github.com/leafood7/evolving-harness.git
cd evolving-harness
python -m unittest discover -s tests -v
python -m evolving_harness demo --output .harness/demo-001
```

演示目录须为新目录。演示使用脚本化提案，不调用模型，不代表真实能力收益。

真实任务另外需要 Node.js >=22.19，以及本机已经构建好的 Pi。兼容版本和入口见 [pi-lock.json](pi-lock.json)。本项目不包含 Pi 源码或模型密钥。

```powershell
$env:EH_PI_ROOT = 'D:\path\to\pi'
$env:EH_NODE = 'C:\Program Files\nodejs\node.exe'
python -m evolving_harness doctor
python -m evolving_harness --project my-project native --workspace 'D:\path\to\project' --allow-write --thinking off
```

也可以复制 `harness.example.toml` 为 `harness.local.toml` 配置 Node 路径；模型服务在 Pi 中配置。`doctor` 只检查连接，不调用模型。默认数据库是当前目录的 `.harness/state.sqlite3`，可用全局参数 `--db` 指定。双击 `start-pi-harness.bat` 启动原生界面，附加参数会传给 native；使用 `--resume` 恢复对应范围的最近会话。

运行器使用当前用户权限，`--allow-write` 启用 shell/编辑工具，不提供 OS 沙盒。原生入口默认只显式加载本项目扩展；需要其他扩展时使用 `--with-user-extensions`。Pi 自身的上下文与 Skills 发现仍保持原生行为。

## 日常使用

在 Pi 中完成真实编程任务，然后查看证据与改进建议：

```text
/harness center
/memory add 本项目的测试命令和用户约定
/harness profile
/harness review local
/harness memory-review
/harness improve
/harness improve status
```

- **执行记录**：保存任务、可见工具轨迹、代码快照与验证证据。任务完成不等于测试通过；零测试不算通过。
- **用户与项目记忆**：支持查看、编辑、停用、候选修订及回退，任务记录实际使用的记忆版本。用户范围限同一数据库，不是跨数据库云同步或网络鉴权。
- **版本化扩展**：复盘策略、上下文预算、用户偏好、Skill 和可信工具配方具有版本、来源、评估和回退关系。Skill 按触发词加入上下文，可导出 SKILL.md。
- **递归机制**：有限规则的复盘策略可生成后继；通过门槛并启用的新策略参与后续复盘与提案。人工整理、离线规则演示和真实模型提案分别记录。
- **评估与控制**：候选与活动版本分离；固定门槛和确认控制启用。批量改进无新证据时保持现状。模型提案先展示有界载荷再确认，无自动重试。

完整命令、作用范围及限制见 [产品使用说明](PRODUCT_GUIDE.md)，或 `/harness help`。

## 通用任务评测

新增 `/harness eval prepare 配置路径`、`/harness eval run`、`/harness eval report`。支持配置1—5个 Python 编程任务，对照指定旧版/候选 Skill，冻结起始源码和独立验收，汇总正确性、工具调用、耗时及可用费用，输出 HTML / JSON 报告。不会自动晋升候选，真实请求须先确认。

可先运行零模型演示：`python examples/evaluation/offline_demo.py --output .harness/eval-demo-001`。配置协议和真实任务操作见 [评测使用说明](examples/evaluation/README.md)。这是可配置的 Python 任务入口，不再依赖日志工具；不含模型裁判或 OS 沙盒。

## 实验与边界

提供记忆故障模拟、跨任务实验、复盘策略基准和日志项目 Skill 对照。它们验证不同层次，不能相互替代。

`/harness skill-compare` 专用于满足固定目录契约的 log-inspector 案例，需要活动的 `skill:portable-test-entry`；不是任意项目的一键基准。示例日志项目和个人数据库不随本仓库发布。该实验准备相同源码的两份副本，仅处理组注入 Skill，独立检查受保护文件、测试与接口兼容；确认后才调用模型。源码副本不构成权限沙盒。

当前实际对照曾出现两组均通过、处理组主动增加隔离检查的平局。额外检查、工具调用数量、耗时差或版本升级本身不证明能力提升。策略固定案例与 Skill 契约评估也不等同于真实编程质量评估。

尚不支持任意工具代码自动发布、评估器自修改、操作系统级隔离、多用户网络服务或无人值守的大规模搜索。

## 目录与开发

| 路径 | 用途 |
|---|---|
| `evolving_harness/` | Python 控制层、存储、记忆、候选、评估、实验与 RPC |
| `extensions/` | Pi 原生扩展及受控故障实验扩展 |
| `tests/` | 离线回归与原生扩展协议测试 |
| `examples/sample_project/` | 离线演示使用的小型代码样例 |
| `PRODUCT_GUIDE.md` | 当前产品操作说明 |

源码运行即可，无需 pip 安装。开发约定见 [AGENTS.md](AGENTS.md)。Python 回归命令：`python -m unittest discover -s tests -v`。原生扩展桥测试需要支持 TypeScript 类型擦除的 Node：`node --experimental-strip-types tests/native_extension.mjs`。协议测试不调用外部模型。

数据库、个人记忆、会话、原生报告、模型轨迹、缓存和 `harness.local.toml` 均不上传。GitHub 私有仓库保存可复现源码；本机运行数据由用户独立保管。
