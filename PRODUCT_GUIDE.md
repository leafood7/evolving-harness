# Evolving Harness：本地可用版

## 通用任务评测入口

`/harness eval prepare 配置路径` 冻结源码、任务、两版 Skill 和独立检查；`/harness eval run` 展示载荷并确认整批真实执行；`/harness eval status` 或 `report` 查看统一 HTML / JSON 报告。配置1—5个 Python 任务，可比较未启用候选与旧版本，不自动激活。详细协议、限制和零模型演示见 [评测使用说明](examples/evaluation/README.md)。

启动与环境配置见 [README](README.md)。沿用原生 Pi 界面，独立 Python 控制层，不修改 Pi 源码。已有数据库可继续使用；新增组件表自动创建，不替换已有记忆或策略。

## 日常入口

```text
/harness home
/harness card
/harness card full
/harness rate 满意
/harness rate 需调整 真实意见
/harness ratings
```

当前操作以 [快速指南](examples/quickstart/README.md) 和 [卡片与反馈说明](examples/completion/README.md) 为准。启动首页区分日常功能与实验功能；卡片不会把模型自述、缺失验收或旧代码的结果当作当前验证通过。满意不触发自动升级。

## 进阶：组件与递归演示

`/harness center` 查看组件。`/harness demo recursive` 在临时数据库运行有边界的两代规则演示，不调用模型、不改变当前项目。这些是管理和实验入口，不是使用前必须执行的步骤，也不证明自主能力增长。以下保留仍受支持的进阶功能与限制。

## 用户中心与上下文

```text
/harness profile
/harness package edit context
```

profile 编辑器每行一条明确偏好，经过确认后跨当前用户的项目使用，不影响其他用户。context 可编辑注入记忆数量、字符预算和去重；预算以字符计，不是精确Token。不会截断半条记忆。两者均保留版本，任务记录实际使用的组件ID和上下文统计。

组件编辑只创建候选。复制输出中的真实ID，再执行：

```text
/harness package evaluate 组件ID
/harness package activate 组件ID 评估ID
```

启用会确认并开始新会话。用户偏好组件通过 package 管理时，show/evaluate/activate 后追加 `user`，例如 `package activate 组件ID 评估ID user`。

## 复盘方法自身改进

```text
/harness strategy
/harness strategy feedback expiry 说明真实观察到的过时记忆问题
/harness strategy propose
```

候选仅根据已有反馈生成，不应为了版本号伪造反馈。支持 scope、expiry、duplicate、test_count 四种有限检查；当前策略的 priority 决定先处理哪个。按 evaluate/activate 流程启用。每个后继保存 parent、generator、证据和评估；新版实际用于后续记忆模型评审，并能生成自己的后继。固定回归门槛不允许候选更改。

也支持模型提出策略、Skill或上下文配置候选：

```text
/harness package model reviewer
/harness package model context
```

任务结束时，会从实际“0条测试”工具输出、已有重复/过期元数据审计中自动收集线索，生成有限规则候选；不从模型自述推断事实，不自动调用模型或启用。相同证据不会重复采集。

对比候选与父策略的模型表现：

```text
/harness benchmark 复盘策略组件ID
```

先预览，再确认六个独立会话请求（3个固定模拟评审案例×2个版本）。单请求约30秒，总上限240秒；平局、失败与超时均保留，不自动启用。只发送固定案例和策略，不发送真实项目文件。该小样本使用简单内容检查，不是语义正确性证明或统计结论。

先显示完整载荷报告，再由用户确认发送。一次无工具请求、关闭自动重试、约130秒限时；只发送当前范围的组件及证据摘录，可能含源码。严格JSON和证据引用检查通过后仍只是候选，不能自动启用。模型保持现状、提案重复或未通过门槛都属于正常结果。

## Skill 与工具扩展

```text
/harness package edit skill:repair
/harness package edit tool:inspect
```

Skill 使用 description、triggers、steps 结构。启用后按任务触发词加入工作流上下文，最多3项、总计6000字符；任务记录实际Skill版本。`package model skill:repair` 可据证据提出后继。`package export 组件ID` 导出原生 SKILL.md，可用 Pi `--skill 路径` 加载；导出不自动改写用户已有Skill目录。

已接受的 workflow 类型经验可转为待评估Skill：

```text
/harness skill from 经验ID repair 修复
```

工具配方支持 workspace_summary、unittest、checks_runner 三种可信适配器，启用前检查、执行前再确认。已启用工具会进入任务上下文，Agent可通过注册的 `harness_recipe` 工具请求执行；也可手动运行：

```text
/harness tool run tool:inspect
```

执行测试需要以 `native --allow-write` 启动；运行当前用户权限，不是操作系统沙盒。测试0条不算通过。工具配方不是任意生成的Extension/MCP代码；新增可执行适配器仍是可信程序开发工作，不能把有限配方扩展宣传成任意代码自改。

## 记忆、回退与实验

现有 `/harness memory-review model 记忆ID`、候选接受/拒绝仍可用。新增：

```text
/harness memory-review rollback 已接受的修订候选ID
/harness package rollback reviewer
/harness package rollback context
/harness package disable skill:repair
/harness package disable tool:inspect
/harness experiments
```

记忆回退恢复旧内容为一个新版本，不抹掉历史；若已有后续修改则拒绝覆盖。组件回退到父版本，optional Skill/工具可停用。experiments 根据当前复盘策略优先级展示待处理缺陷和失败方案；重复模型载荷拒绝再次建候选，避免重复试验。每次外部模型请求都需确认，不是无人值守搜索系统。

## 能证明到哪里

- 已实现：证据采集、分范围记忆、人工控制、版本化组件、有限规则递归、按任务注入、可扩展Skill/工具配方及回退。
- 固定策略案例验证的是有限行为改善；Skill/工具/偏好主要通过契约检查，不能当作实际编程效果评估。
- 已有真实模型的过时命令修订案例；已有对照实验平局必须保留。没有证据就不能宣称普遍变强。
- 不包含任意工具代码自动晋升、评估器自我改写、操作系统级隔离、网络多用户鉴权或大规模并行搜索。

日常使用先在真实项目完成任务，记录可复现的问题，再提出候选；版本升级本身不算收益。`/harness help` 查看命令入口。

模型组件提案失败时，可运行 `/harness package last-error` 查看最近诊断及完整报告路径。诊断区分空回复、非法JSON、候选校验失败、连接错误和重复方案；只保存脱敏可见正文，不含隐藏推理。只允许完整JSON或完整JSON代码块，不从解释文字里猜测提取。旧版未保存的失败回复无法追溯，不会自动重试。

## 批量改进入口（推荐）

`/harness improve` 自动检查最近最多20个已结束原生任务、补齐本地元数据/工具线索、复用或生成有限规则候选并执行固定评估；无新证据就保持现状。通过后只需一次确认，自动启用并开始新会话，无需手动复制候选ID和评估ID。此入口不调用模型。

`/harness improve status` 查看本批记录及后续使用回执。任务上下文加载新策略配置与模型复盘实际使用该策略，分别记录；两者都不等于已证明收益。原有模型候选、对照和手动组件命令继续保留。

## 在真实日志项目中对照Skill

`/harness skill-compare` 会准备两个相同源码副本，固定同一解析模块拆分任务，随机两组执行顺序。两组使用相同模型、用户偏好、项目记忆和基础指令；只在其中一组加载 portable-test-entry Skill，不加载其他Skill。先显示完整载荷报告（含源码），由用户一次确认两组模型请求和真实Shell权限。

每组模型阶段上限115秒，之后独立执行隔离测试和兼容性探针。保留既有测试、AGENTS.md和测试入口；改动受保护文件、运行失败或超时不会伪装成有效收益。同一批不自动重试。副本只是正常文件改动的隔离，不是OS沙盒，真实Shell仍运行用户权限。

`/harness skill-compare status` 查看结果。功能正确性与是否主动做隔离检查分别记录；附工具错误、耗时和模型报告用量，缺失用量不当成0。单次对照不证明统计或泛化收益，不自动启用组件。源码复制仅覆盖声明的有界文本文件，拒绝符号链接/目录联接。
