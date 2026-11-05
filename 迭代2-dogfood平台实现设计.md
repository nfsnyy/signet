# 迭代 2：dogfood 平台实现设计 v0.1

> 状态：设计草案，待 review；**本文档获批前不进入功能开发**。
> 上游依据：`../辅助开发平台.md`、`./迭代1-MVP协议与验收清单.md`。
> 目的：把已经验证过的 MVP 原型整理成可 dogfood 的平台实现方案，避免继续“边写边想”。

## 0. 开发启动闸门

本文档存在以下任一情况时，不开始迭代 2 功能开发：

1. §22 的必须确认项没有结论；
2. dogfood 准入条件没有逐项定义完成；
3. 没有建立可持续运行的持久测试套件；
4. 状态仓库、身份口径和 actor 命名规则没有确定；
5. 你未明确发出“开始开发”的指令。

后续开发必须按 §20 的阶段顺序推进，每个阶段有可验证出口；不得直接跳到页面或服务端。

## 1. 对当前实现的风险判断

当前代码已经验证了关键机制，但仍是原型：

- 已经验证：双入口、状态闸门、提案 sync、租约、冻结、C1–C5、生命周期、干净环境执行、规格作废传播。
- 尚未形成正式设计：规格修订、安全否决降级、协作消息、远端冲突恢复、多 project、测试体系、发布形态。
- 当前代码是“功能优先”的横向原型，模块边界不足以直接服务化。

因此迭代 2 的第一项工作不是新功能，而是**先把现有行为锁住，再补齐缺口**。

## 2. 设计目标

1. 保持原设计的六阶段、三件套、钉扎、事件日志和提交协议不变。
2. 保持 agent 与 operator 两个能力面不变。
3. 保持 CLI 动词稳定；未来 API/页面只增加外衣，不改协议。
4. 把现有原型整理成可重复测试、可 dogfood、可审计的最小平台。
5. 明确哪些能力属于迭代 2，哪些必须留到服务化阶段。
6. 先建立回归测试，再做任何行为变更。

## 3. 非目标

迭代 2 不做以下事情：

- 不做 Web 页面；
- 不做数据库后端；
- 不做 Entra 强身份；
- 不做多用户并发服务；
- 不做不可信 agent 防护；
- 不做自动协作路由；
- 不把 GitHub repo 直接当多项目生产数据库。

这些属于后续扩容阶段，扩容前必须先完成信任边界升级。

## 4. 目标架构

### 4.1 分层

```text
接口层
  aidev-agent CLI / aidev-operator CLI
  未来：HTTP API / Web / MCP

应用层
  用例编排：init_task、freeze、claim、submit、verify、close……
  事务边界、锁、提案处理、事件写入

领域层
  状态机
  C1–C5 规则
  pin、版本、作废传播、否决项相关面
  报告与验收结构校验
  不依赖 Git、subprocess、文件系统细节

适配层
  Git 状态仓库
  JSON/JSONL 文件
  本地干净环境执行器
  未来服务端存储/远程 Git 适配器
```

当前模块不要求立即大拆，但新代码必须遵守依赖方向；旧代码在测试锁定后逐步迁移。

### 4.2 现有模块迁移方向

| 当前模块 | 目标归属 | 处理建议 |
|---|---|---|
| `cli.py` | 接口层 | 保持薄入口 |
| `service.py` | 应用层 + 领域逻辑混合 | 先补测试，再拆出状态机/提案规则 |
| `specs.py` | 领域规则 + freeze 用例 | 拆分纯检查函数与 freeze 编排 |
| `lifecycle.py` | 应用用例 | 保持为 operator 用例入口 |
| `runner.py` | 执行适配器 | 保持隔离，后续抽象 sandbox |
| `gitrepo.py` | Git 适配器 | 增加 fetch/conflict 策略 |
| `jsonio.py/taskfile.py` | 文件适配器 | 保持底层工具 |

## 5. 运行时拓扑

迭代 2 仍然采用本地信任域：

```text
源码仓库 signet
  保存平台代码与协议文档

状态仓库 signet-dev-state
  保存 tasks / specs / events / discussions

agent 工作区
  每个任务一个独立目录
  只保存任务包、冻结副本、证据、报告、outbox

operator 本机
  执行 operator CLI 与 sync
```

推荐状态仓库为 private。源码仓库可以是 public，但状态仓库不应随源码公开。

## 6. 领域模型

### 6.1 Project

迭代 2 引入 project 的逻辑边界，但不做平台级多项目服务。

MVP 约定：

- 一个状态仓库代表一个 project；
- `project.json` 保存 `project_id、name、owner、created_at`；
- 所有任务、规格、事件都归属该 project；
- 跨项目查询留到服务化阶段。

这样既不误把 repo 当 project 的永久架构，也不提前实现集中式 project 服务。

### 6.2 Task

字段至少包括：

- `task_id`
- `project_id`
- `title`
- `requirement`
- `owner`
- `state`
- `spec_id`
- `spec_pin`
- `lease`
- `void_reason`
- `created_at / updated_at`

`task.md` frontmatter 是状态缓存；**事件日志才是审计事实**。

### 6.3 SpecTriple 与 SpecVersion

- 验收卡、契约、映射表分别保存；
- 每次冻结生成新版本目录；
- `spec_pin` 指向版本和三个哈希；
- 作废不是修改，而是新增 `INVALID.json`；
- 新版本通过 `supersedes` 指向上一个版本。

### 6.4 Proposal

agent 的写操作不直接改变权威状态，只生成提案：

- `proposal_id`
- `task_id`
- `operation`
- actor / on_behalf_of
- base state / pin / lease / task_version
- parameters
- local validation

sync 必须在最新权威状态上重新验证。

### 6.5 Event

事件日志是 append-only 审计事实。每条事件至少包括：

- event_id / event_type / task_id
- timestamp
- actor / on_behalf_of / entrypoint
- before / after
- proposal_id
- payload
- project_id（迭代 2 起）

## 7. 状态机与不变量

### 7.1 状态

```text
Draft → Ready → In Progress → Implemented → Verified → Done
                 ↓             ↑      ↓
              Blocked         Ready  Void
```

终态：

- Done
- Void

### 7.2 不变量

- pin 一旦生成不可变；
- 事件不可原地修改；
- 冻结文件不可无版本覆盖；
- agent 不能做 operator 动作；
- verify 与 close 必须分开；
- 实现者不能验收自己的任务；
- 安全否决项不能仅口头确认；
- 规格作废后不得继续凭旧 pin 交付。

## 8. 动词协议

### agent 面

- `status`
- `log`
- `claim`
- `submit`
- `block`
- `enhancement`

### operator 面

- `init-repository`
- `init-task`
- `freeze`
- `refreeze`（新增）
- `sync`
- `verify`
- `reject`
- `close`
- `void`
- `adjudicate`
- `veto`（新增）
- `inbox` 管理命令（新增或文件协议先行）

所有命令输出 JSON；状态迁移必须产生事件；命令可重入。

## 9. 状态仓库布局

```text
project.json
discussions/
tasks/<task-id>/task.md
tasks/<task-id>/verification/
specs/<spec-id>/<version>/
  acceptance-card.json
  contract.json
  mapping.json
  assets/...
  baseline-evidence/...
  INVALID.json（可选）
events/events.jsonl
enhancements.jsonl（可选）
```

工作区继续放在状态仓库之外。

## 10. freeze / refreeze 设计

### 10.1 freeze

- Draft 才能首冻；
- 先 H1，再 H2；
- C1–C5 全过；
- baseline 在冻结阶段执行并保存证据；
- target 生成可执行契约，不在 freeze 阶段执行；
- 成功后生成 pin，任务 Ready。

### 10.2 refreeze（新增）

触发条件：

- block 裁决定义错误；
- 输入问题回需求定义；
- 规格需要修订；
- veto 批准后的受控变更。

规则：

- 不覆盖旧版本；
- 生成 `<version+1>`；
- 新旧版本都保留；
- 新版本 `supersedes` 旧版本；
- 旧 pin 不变；
- 在途任务不自动换 pin；
- 因旧版本作废而被波及的任务必须 Void，不追加重验。

### 10.3 C4 增强

- 版本号只增；
- 旧文件不可原地改；
- `contract_type` 变更必须升版本并记录理由；
- 同一 spec_id 下，旧版本作废后仍不可删除。

## 11. 执行与验收设计

### 11.1 执行器边界

当前本地执行器继续保留：

- 只读钉扎规格；
- 校验 assets 哈希；
- 临时目录；
- 白名单环境变量；
- 绝对路径 runner；
- timeout；
- 不继承 agent 工作区。

后续再把执行器抽象成 sandbox 接口。

### 11.2 verify

- `verify --results` 保留给外部初验；
- `verify --execute` 用于机器用例；
- 人工项和安全否决项仍需人工证据；
- 失败结果不允许进入 Verified，必须 reject；
- 成功才写 Verified 事件。

### 11.3 reject 补强

当前 reject 只支持 Implemented；迭代 2 应补齐：

- 实现问题：Implemented → Ready；
- 输入问题：Ready / In Progress / Implemented → Void；
- reject 必须写明类别、原因和证据要求；
- 所有路径都释放租约并刷新对应工作区。

## 12. sync 与远端冲突恢复

当前 sync 还没有完整的远端冲突恢复设计。迭代 2 必须补成显式协议：

1. sync 开始先获取远端；
2. 本地权威若落后远端，拒绝处理并提示先 sync 最新状态；
3. 在最新 HEAD 上重验提案；
4. 提交；
5. push；
6. push 非快进时中止，不自动 rebase，不自动覆盖；
7. 下次 sync 重新获取并重验；
8. 提案处理必须幂等，重复 sync 不产生重复事件。

## 13. 异常、作废与追溯

- block 必须带证据；
- adjudicate 定义错误 → 旧任务 Void + 规格版本作废；
- 作废传播到所有同 pin 的非 Done 任务；
- Done 不变，生成追溯审查任务；
- 每次 block 归因必须形成“新增检查或人工规则”的结论；
- 不允许换 pin 重验旧任务。

## 14. veto.downgrade 设计

新增为一等流程，不作为普通 refreeze 的副作用。

### 14.1 触发

对重冻结 diff 做结构分析，命中以下任一“相关面”即进入窄门：

- 验收卡中的安全否决项；
- 映射到这些否决项的用例；
- 这些用例依赖的环境条目或资产。

### 14.2 状态

```text
proposed → approved
         → rejected
         → withdrawn
         → expired_effective
```

### 14.3 单人模式

- 提出人与批准人相同 → 默认 24h 延迟；
- 延迟内第二人可批准、驳回，提出人可撤回；
- 到期后下一次 CLI 操作结算为生效；
- 生效时间记录原定到期时间。

### 14.4 防绕过

- 分类不由 H2 自行决定；
- fail-closed：分类存疑一律按降级处理；
- 任务不得提议降级自己未通过的否决项。

## 15. 协作消息设计

迭代 2 保持“窄管道”。

### 15.1 文件协议

每个任务工作区有：

```text
inbox/
outbox-collab/
```

消息类型固定：

- `in-spec-request`
- `definition-doubt`
- `new-scope`

### 15.2 权限

- in-spec-request：对方 agent 可接受、拒绝、还价，两轮后升级；
- definition-doubt：强制 block，不还价；
- new-scope：只登记 enhancement，不接需求。

### 15.3 不变量

- agent 消息不能改冻结规格；
- 全程挂双方任务事件；
- submit 报告必须列协作请求；
- 人可以不读每条，但验收人能回放。

## 16. 身份、权限与安全边界

- 迭代 2 仍是自报身份；
- 权限边界靠双入口隔离，不靠身份防伪；
- actor 使用稳定别名，不使用真实姓名或公司邮箱；
- operator 入口不进入 agent 工作区；
- 执行器禁止 shell=True；
- 资产路径禁止逃逸；
- 所有命令 JSON 输出，不把敏感内容写入事件 payload。

## 17. 测试策略

必须先建立持久测试，不允许继续只靠一次性脚本。

### 17.1 测试形态

- `unittest` 标准库，不引入 pytest；
- 真实 subprocess 调 CLI；
- 临时 Git 仓库 + 本地 bare remote；
- 每个测试独立 tempdir；
- 不依赖当前用户全局 Git 配置。

### 17.2 覆盖要求

- 正常闭环；
- 所有状态机非法迁移；
- C1–C5 正反路径；
- 并发 claim；
- sync 幂等；
- push conflict；
- block/adjudicate/void 传播；
- verify 成功与失败；
- frozen asset 篡改；
- baseline 成功与失败；
- 工作区刷新；
- 事件完整性。

### 17.3 验收口径

原 M1–M5、B1–B4 转为自动化测试；未自动化前不得宣称 dogfood 就绪。

## 18. 指标与可观测性

最低限度保留命令结果 JSON 与事件日志。后续页面先读事件，不另造状态事实。

建议指标：

- block 率；
- Void 率；
- 已实现待验收队列；
- 协作升级比例；
- 人工验收占比；
- veto.downgrade 次数。

## 19. 发布与运行形态

迭代 2 仍从源码运行：

```bash
python aidev-agent.py ...
python aidev-operator.py ...
```

可以增加一个轻量启动包装，但不引入打包框架。服务化前不追求 pip 安装。

## 20. 实施阶段

### 阶段 0：设计确认

出口：本文档、§22 决策、dogfood 准入清单全部确认。

### 阶段 1：测试锁定现有行为

- 建 `tests/`；
- 固化当前已验证场景；
- 不修行为，只允许补测试；
- 出口：现有能力全部有回归测试。

### 阶段 2：协议缺口补齐

优先级：

1. sync fetch / non-FF 中止；
2. reject 多状态路径；
3. refreeze；
4. veto.downgrade；
5. inbox 文件协议；
6. project.json 与 actor 规范。

出口：新增能力都有正反向测试。

### 阶段 3：结构整理

- 抽出纯状态机与规则函数；
- 收敛 service/specs/lifecycle 的职责；
- 不改变外部命令。

出口：模块依赖方向符合 §4。

### 阶段 4：dogfood 试点

- 新建 private state repo；
- 选 3–5 个真实小任务；
- 全流程使用平台；
- 每周复盘摩擦点。

出口：真实验收卡被使用并至少修订过一版；阻塞项有结论。

## 21. dogfood 准入清单

开始前必须全部满足：

- [ ] 本文档获批；
- [ ] §22 必须决策全部有结论；
- [ ] 状态仓库已创建且 private；
- [ ] actor 命名规则确定；
- [ ] 现有回归测试全部通过；
- [ ] refreeze / veto / inbox 至少达到约定最小实现；
- [ ] 原 M/B 验收自动化通过；
- [ ] 迭代 1 验收卡真实使用过并至少修订一版；
- [ ] 人工验收防走过场机制有结论；
- [ ] verify 与 close 的独立意义有结论；
- [ ] 明确声明仍只适用于单人 operator、本地信任域。

## 22. 开发前必须确认的决策

| 编号 | 决策项 | 建议默认 |
|---|---|---|
| D1 | `signet` 源码仓库是否保持 public | 若不想公开内部治理细节，改 private |
| D2 | dogfood 状态仓库名称和位置 | 新建 private `signet-dev-state` |
| D3 | actor 命名规则 | 使用 `nfsnyy、agent-*、verifier-*` 等别名，不用真实姓名 |
| D4 | 单人是否兼任 H1/H2、verify/close | 允许兼任，但必须分开命令和事件 |
| D5 | 规格版本号规则 | 整数递增，从 1 开始 |
| D6 | veto 单人延迟窗口 | 24 小时 |
| D7 | 协作消息落地方式 | 迭代 2 继续文件型，人当路由器 |
| D8 | 测试框架 | Python 标准库 `unittest` |
| D9 | 是否允许为补测试新增 `tests/` 目录 | 允许，且是阶段 1 的第一项 |
| D10 | project 粒度 | 一个状态仓库一个 project，先不建项目服务 |

## 23. 恢复开发的正确顺序

你 review 后如果同意，先只做阶段 1：为现有行为补持久测试，不改功能。阶段 1 完成并通过后再进入阶段 2。每个阶段结束前都给你 review，不一次性做大爆炸式改造。
