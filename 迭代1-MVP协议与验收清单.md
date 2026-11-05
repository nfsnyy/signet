# 迭代 1：MVP 协议与验收清单 v0.1

> 状态：实施准备草案，未冻结。
> 唯一设计依据：`../辅助开发平台.md`。
> 目的：把原设计转换为可实施的 MVP 范围、协议骨架、技术约束和验收清单。
> 本文不新增产品目标；实现细节如与原设计冲突，先回到原设计形成决策，不在代码中静默改变规则。

## 1. 迭代 1 的完成定义

迭代 1 要交付一个可供单人 operator 在本地信任域使用的研发治理工具，而不是通用项目管理系统或 agent 对话平台。

完成时必须同时满足：

1. 一个任务能从 Draft 到 Done，全程只通过 CLI 和规定的人工签字完成，不绕近路。
2. agent 只能提交执行类提案，不能执行管理类操作，也不能直接写权威仓库。
3. 验收卡、契约、映射表经过检查和冻结后才能使任务 Ready。
4. 每个任务永久钉扎自己的规格版本，验收只使用该版本。
5. 所有权威状态迁移都有事件，能够完整回放任务历史。
6. 原设计 M1–M5、B1–B4 的埋雷测试全部通过。
7. 文档规定的迭代 2 准入事项有明确结论；这些事项不阻塞迭代 1 开发，但阻塞迭代 2 开始。

### 1.1 本迭代包含

- 双入口 CLI。
- Git 权威仓库及提案、校验、入库、推送协议。
- 任务目录初始化、工作区指令生成和钉扎位预留。
- 任务状态机、原子租约及完整事件日志。
- 验收卡、契约、映射表的冻结与 C1–C5 检查。
- 执行提交、block、reject、作废及规格作废传播。
- 基于钉扎契约和干净环境的验收。
- 安全否决项降级的结构触发、批准、延迟和留痕。
- 文件型 inbox，由人按消息类型分发。
- 端到端埋雷测试和真实验收卡试用。

### 1.2 本迭代不包含

- Web 页面、数据库和服务化部署。
- Entra ID；身份暂用 git user，自报身份不代表强身份认证。
- 服务端 CI 验证管道。
- 协作消息自动路由。
- 独立的需求档案实体。
- Skill 体系。
- 对恶意 agent、工具篡改或 operator 主机失陷的防御。

### 1.3 适用范围

仅限合作式 agent、单人 operator、本地信任域。增加第二个使用者、多 agent 并发、不可信 agent 或对外服务化之前，必须先完成信任边界升级，不能把它当作普通功能迭代。

## 2. MVP 技术基线

| 项目 | 迭代 1 基线 |
|---|---|
| 语言 | Python 3，最低版本在实施任务中固定，优先使用当前稳定版本 |
| 依赖 | 仅 Python 标准库；新增依赖必须形成明确决策 |
| 可执行入口 | agent 与 operator 两个独立入口，管理命令不出现在 agent 工作区 |
| 状态与工件 | Git 文件仓库；任务状态放 frontmatter，结构化协议数据用 JSON |
| 事件 | append-only JSONL |
| 权威副本 | 远端 Git 仓库，只由 operator 工具写入 |
| agent 工作区 | 位于平台权威仓库之外，只通过 outbox 提交提案 |
| 哈希 | SHA-256，哈希对象和规范化规则必须固定 |
| 时间与标识 | UTC 时间戳；任务、提案、事件使用唯一 ID |
| 测试 | 真实 subprocess 调用 CLI，检查退出码、JSON、文件、Git 提交和事件 |
| 验收环境 | 临时目录、全新依赖环境、白名单环境变量、受控进程与端点 |

远端选择 ADO 或 GitHub 不阻塞本地协议开发，但首次配置真实权威仓库前必须确定。

### 2.1 前置技术验证

在批量实现命令前，先完成三个小验证：

1. **frontmatter 验证**：只用标准库可靠读写所选格式，并验证状态更新不会改动任务正文。
2. **Git 提交验证**：工具能够只提交自己生成的状态、规格和事件，不把工作区其他改动捎带入库。
3. **并发验证**：两个本地提案竞争同一任务时，权威 `sync` 只能接受一个合法 claim，另一个得到明确拒绝事件或结果。

这些验证不是另造一套产品，而是对将采用的真实 CLI 和仓库布局做端到端试探。

## 3. 仓库与工作区边界

建议逻辑布局如下，具体目录名可在实现任务中固定：

```text
platform-repository/              # 权威仓库，仅 operator 工具写入
  discussions/                    # 人工维护的 decision record
  specs/                          # 已冻结的规格版本
    <spec-id>/
      <version>/
        acceptance-card.json
        contract.json
        mapping.json
  tasks/
    <task-id>/
      task.md                     # frontmatter 状态 + 任务正文
      verification/               # 工具生成的验收记录或链接
  events/
    events.jsonl                  # 全局追加式事件日志

agent-workspace/                  # 权威仓库之外
  AGENTS.md                       # init-task 机械生成
  task.md                         # 执行任务所需任务包
  contract/                       # 钉扎规格的只读副本
  evidence/                       # 执行证据
  report.md                       # 四段式报告
  inbox/                          # 人工分发的类型化消息
  outbox/                         # agent CLI 生成的操作提案
```

约束：

- 平台只保存事件、状态、规格以及外部内容的链接和哈希，不复制代码库内容。
- 代码仍由代码库管理，证据文件可留在工件存储。
- agent 不直接编辑权威任务状态、事件或冻结规格。
- operator 工具永不执行全量 `git add`。
- 读取面保持文件/Git 可用；离线操作先形成提案，权威状态以成功 `sync` 为准。
- 代码库事件接入属于后续能力，MVP 不实现双向同步。

## 4. 角色、身份与权限

### 4.1 角色

- **实现 agent**：领取、执行、提交、block、登记 enhancement。
- **验收 agent**：使用新 session 初验和整理证据，不得验收自己实现的任务。
- **需求 owner**：进行 H1 内容确认。
- **架构师**：进行 H2 一致性确认、裁决 block、处理规格作废。
- **验收人**：不是实现者，记录逐条验收结论并进行人工 confirm。
- **stakeholder**：执行 close 或拒收，对交付负责。
- **operator**：执行管理入口和 `sync`，其操作不能替代必须由对应角色作出的判断。

小团队可以一人兼任多个角色，但 H1/H2、初验/confirm、verify/close 必须作为不同动作分别记录。

### 4.2 MVP 身份边界

- 以 git user 记录自报身份。
- agent 动作记录 `actor` 和 `on_behalf_of`。
- 实现者与验收者不同主要由入口隔离、新 session 和人工 confirm 保证，不声称已解决身份伪造。
- operator 入口不得复制进 agent 工作区。
- 权威仓库只接受经过 `sync` 完整验证的工具生成内容。

## 5. 核心数据协议

以下字段是协议骨架，实施时可补充字段，但不能删除原设计要求的约束。

### 5.1 任务

任务至少记录：

- `task_id`、`title`、`requirement`、`owner`。
- `state`：`Draft / Ready / In Progress / Implemented / Verified / Done / Blocked / Void`。
- `spec_id` 和预留的 `spec_pin`。
- `lease`：`claimed_by / claimed_at / expires_at / proposal_id`，无租约时为空。
- `void_reason`：定义缺陷、取消或规格作废波及。
- 外部 PR、代码和证据的链接与哈希，不以其内容替代平台状态。

`spec_pin` 至少包含：

```json
{
  "spec_id": "spec-example",
  "acceptance_card": {"version": "1", "sha256": "..."},
  "contract": {"version": "1", "sha256": "..."},
  "mapping": {"version": "1", "sha256": "..."}
}
```

任务 Ready 时正式生成，此后不可换 pin。规格重新冻结产生新版本，不改变在途任务。

### 5.2 验收卡

每条标准至少包含：

- 稳定的 `criterion_id`。
- 可独立验收的标准陈述。
- 强度：`must / must-not / safety-veto`。
- 是否允许人工验收；安全否决项的核心验收路径不得仅写“人工”而不给操作和证据要求。

验收卡只表达“什么算好”，不复制契约用例内容。

### 5.3 契约

契约至少包含：

- `contract_type`：`baseline / target`，缺失则拒冻。
- 需求侧操作集的机械来源引用；无操作集概念的契约应显式声明不适用，不能靠空列表绕过 C2。
- 声明的操作集和实际用例集。
- 用例、fixtures 及其解析入口。
- 环境规格：运行器与依赖版本、端点解析规则、环境变量白名单。
- 从 `baseline` 改为 `target` 时必须升版本并记录理由。

### 5.4 映射表

每行表达一个验收标准与一个或多个用例的关系，或明确标记人工验收：

```json
{
  "criterion_id": "AC-001",
  "case_ids": ["CASE-001"],
  "manual": false
}
```

必须能自动发现未覆盖标准、孤儿用例和安全否决项相关面。

### 5.5 四段式报告

报告必须机器可解析，固定包含：

1. 实现了什么。
2. 运行了什么：命令和输出位置。
3. 未运行或跳过项及原因。
4. 与验收卡的偏差。

另含协作请求清单；没有时必须显式为空，不能省略。

### 5.6 操作提案

agent 的写操作生成提案，不直接改变权威状态。提案至少包含：

- `proposal_id`、`task_id`、`operation`。
- actor、on_behalf_of、创建时间。
- 操作参数和证据链接/哈希。
- 提案所依据的权威任务版本、状态、租约和 `spec_pin`。
- 本地校验结果；本地结果仅供审计，不能替代 `sync` 校验。

### 5.7 事件

每条事件至少包含：

- `event_id`、`event_type`、`task_id`、UTC 时间。
- actor、on_behalf_of、入口类型。
- 操作前后状态；无状态变化时明确为空。
- 关联提案、规格版本、哈希、PR 或证据。
- 拒绝、block、reject、void、裁决、降级等动作的结构化理由。

事件日志只追加。`log` 按任务过滤并按权威顺序回放，不从当前任务文件反推历史。

## 6. 状态机与操作协议

### 6.1 合法状态变化

| 操作 | 前置状态 | 结果状态 | 核心条件 |
|---|---|---|---|
| init-task | 无 | Draft | 创建任务、工作区指令和钉扎位 |
| freeze | Draft | Ready | H1/H2 已分别记录，C1–C5 通过，三件套冻结并生成 pin |
| claim | Ready | In Progress | pin 哈希一致，无有效租约，原子领取成功 |
| submit | In Progress | Implemented | 租约属于提交者，报告、证据、协作清单完整，释放租约 |
| block | In Progress | Blocked | 理由和证据完整，创建讨论条目，释放租约 |
| adjudicate | Blocked | Ready | 架构师认定不是定义错误，旧 session 结束，可重新 claim |
| adjudicate + void | Blocked | Void | 架构师认定定义错误，记录原因并处理规格作废 |
| verify | Implemented | Verified | 独立初验、逐条证据、人工 confirm 均完成 |
| reject：实现问题 | Implemented | Ready | 写明原因，释放租约，新 session 重新 claim |
| reject：输入问题 | Ready / In Progress / Implemented | Void | 写明原因，回需求定义重建任务 |
| close：接受 | Verified | Done | stakeholder 看过证据摘要并明确负责 |
| close：拒收 | Verified | Verified | 记录异议，不自动变成 Done |
| void：规格作废波及 | Ready / In Progress / Implemented / Verified | Void | 反查同一 pin 规格版本并传播 |
| void：取消 | Ready / In Progress / Implemented | Void | 架构师记录取消原因 |

Done 不因规格作废自动改为 Void，但必须生成追溯审查任务。任何未列出的迁移默认拒绝。

### 6.2 CLI 动词面

**agent 入口：**

- `status`：返回身份、任务状态、pin、冻结清单、租约和 inbox。
- `log`：回放任务事件。
- `claim`：本地预检并生成领取提案。
- `submit`：校验报告和证据后生成提交提案。
- `block`：生成 block 提案，附理由和证据。
- `enhancement`：只登记新事项，不修改当前范围或规格。

**operator 入口：**

- `init-task`、`freeze`、`sync`。
- `verify`、`reject`、`close`、`void`、`adjudicate`。
- `veto` 子命令：提出、批准、驳回、撤回否决项降级。
- `status`、`log` 的只读能力可与 agent 共用实现，但输出和权限协议一致。

所有命令：

- stdout 输出机器可解析 JSON，诊断信息走 stderr。
- 成功退出码为 0；业务/闸门拒绝、运行环境失败、内部错误使用不同且稳定的非零类别。
- 可重入：同一提案重复 `sync` 不得重复迁移状态或追加重复效果。
- 拒绝响应至少包含 `reason`、`code`、`task_id` 和当前权威状态。

### 6.3 claim 与租约

- 本地 claim 成功只表示提案已接受本地预检，不代表权威状态已变。
- `sync` 按权威顺序串行验证提案，重新检查状态、pin 和租约。
- 同一任务只能有一个有效租约。
- submit、block、reject、裁决回 Ready 时释放租约。
- 默认 TTL 为 24 小时；过期租约由 `sync` 回收并记录事件。
- 旧 session 的提案不能因为曾经持有租约而在租约释放后继续生效。

### 6.4 sync 提交协议

`sync` 必须按顺序执行：

1. 读取权威状态和 outbox 提案。
2. 对每个提案完整重验，包括身份入口、状态迁移、pin、哈希、租约和报告。
3. 只生成工具拥有的状态、规格和事件变更。
4. 拒绝非法提案并留下可审计结果，不带入其夹带修改。
5. 仅暂存工具明确生成的文件。
6. commit、push；远端冲突时重新读取权威状态并重验，不能盲目覆盖或强行推送。
7. 返回每个提案的接受或拒绝结果。

本地文件校验通过不代表获得入库资格。

## 7. 冻结与验收协议

### 7.1 冻结顺序

1. 需求 owner 完成 H1，冻结验收卡。
2. 契约和映射表基于该卡完成，架构师进行 H2。
3. 从冻结到 Ready 期间验收卡哈希不得变化。
4. C1–C5 全部通过，工具记录三件套版本和哈希。
5. 正式生成 `spec_pin`，任务进入 Ready。

H1/H2 是人的判断，CLI 只能要求明确签字和记录，不能自动生成“同意”。

### 7.2 机器检查

| 检查 | MVP 放行条件 |
|---|---|
| C1 | 每条标准至少有用例或明确人工项；每个用例都有标准出处；安全否决项不得无覆盖 |
| C2 | 需求侧操作集、契约声明集、用例实际覆盖集三者一致；不适用必须显式且有合法类型 |
| C3 | 每个用例可解析，引用 fixtures 存在，环境地址按冻结规则验证有效 |
| C4 | 无原地修改，版本只增，冻结顺序正确，卡哈希未在两次冻结间变化 |
| C5 | baseline 的全部用例已在真实环境运行并附证据；target 可豁免，但标签变更受版本和事件约束 |

机器检查不证明用例语义足够，H2 必须检查映射含义和断言强度。

### 7.3 验收

- 验收 agent 必须使用不同 session。
- 只读取任务 `spec_pin` 指向的契约、用例和 fixtures，并重新校验哈希。
- 在临时目录重建环境，不继承实现工作区的 shell、PATH、代理、环境变量、残留进程或 mock。
- 每条标准都有 pass/fail 和证据；任何安全否决项失败都一票否决。
- 安全否决项必须由人亲手在同一干净环境重跑。
- 初验和人工 confirm 是两次独立事件。
- verify 只表示验收通过；close 才表示责任交接完成。

## 8. 异常、协作与否决项

### 8.1 三类执行反馈

- **实现问题**：就地修，不改变规格。
- **定义错误**：block，由架构师裁决；定义确认错误后老任务 Void，新任务重新排队。
- **新情况**：登记 enhancement，当前任务范围不变。

agent 可以提出分类和证据，但不能自行认定定义错误，也不能修改冻结文件。

### 8.2 规格作废传播

- 定义错误导致任务 Void 时，将其规格版本标记作废，并记录 supersedes。
- 反查所有仍钉扎该版本的任务，包括 Verified，统一置 Void。
- 不允许给旧任务换 pin 或沿用旧证据重验。
- Done 保持不变，但生成追溯审查任务。
- 重冻结必须记录“哪项 C/H 检查漏掉了问题”及加严措施。

### 8.3 协作消息

MVP 只实现文件型 inbox 和人工分发，消息类型固定为：

- 规格内请求。
- 定义存疑，必须进入 block 通道。
- 新范围，只能登记 enhancement。

消息挂接双方任务并留痕；还价最多两轮，之后升级架构师。消息本身无权改变规格。

### 8.4 安全否决项降级

- 重冻结 diff 命中否决标准、其映射用例或相关环境条目时，结构自动进入降级窄门。
- 提出必须包含理由和具体替代控制。
- 有权限的第二人批准可立即生效。
- 提出人与批准人相同时进入默认 24 小时延迟模式；第二人可提前批准或驳回，提出人可撤回。
- 到期无人动作才自动生效，全程记录。
- 任务不得提议降级自己未通过的否决项。
- 分类存疑按降级处理，不能走普通重冻结绕过。

## 9. 实施顺序

### 阶段 A：协议与真实链路验证

- 固定目录、文件格式、命令输出、退出码和事件骨架。
- 完成 frontmatter、选择性 Git 提交和并发竞争三个前置验证。
- 用一张真实验收卡、一份契约和映射表演练 C1/C2，不等人肉平台流程。

出口：协议可以被真实文件和真实 Git 操作执行，不只是纸面可解析。

### 阶段 B：最小纵向闭环

实现：

```text
init-task → freeze → claim → sync → submit → sync → verify → close → log
```

先覆盖一个 target 规格的正常路径，以及非 Ready claim、重复 claim、报告缺段三个拒绝路径。

出口：正常任务可走到 Done；此阶段只代表骨架可用，不代表 MVP 完成。

### 阶段 C：定义与权威边界

- 补齐 C1–C5、版本和冻结顺序。
- 完成提案重验、幂等 sync、选择性提交、远端冲突处理。
- 完成租约释放、TTL 和旧提案失效。

出口：M1–M5 与提交协议、并发 claim、租约残留对抗用例通过。

### 阶段 D：独立验收与异常治理

- 干净环境重建、钉扎 fixtures、安全项人工重跑。
- block 裁决、reject 分类、规格作废传播和追溯审查。
- enhancement、文件 inbox、veto.downgrade。

出口：B1–B4 及安全降级旁路测试通过。

### 阶段 E：迭代验收与试用准备

- 执行第 10 节全部验收。
- 用真实任务验收卡至少试用并修订一次。
- 对人工验收和 verify/close 独立性形成决策。
- 整理操作说明和已知边界，不提前宣称具备多人或不可信环境能力。

## 10. 迭代 1 验收清单

### 10.1 正常闭环

- [ ] 仅从 CLI 完成 Draft → Done，所有人工判断均有独立记录。
- [ ] 任务的 pin 在整个生命周期不变。
- [ ] 每个权威状态变化恰好产生对应事件，重复 sync 不重复生效。
- [ ] `log` 能还原讨论关联、规格、领取、提交、验收和关闭的完整链。
- [ ] PR 或代码状态不会自行把任务改成 Done。

### 10.2 机器闸门

- [ ] M1：存在无用例且未标人工的标准，C1 拒冻。
- [ ] M1：存在无标准出处的孤儿用例，C1 拒冻。
- [ ] M2：需求侧、声明侧、实际覆盖侧任一操作集不一致，C2 拒冻。
- [ ] M2：通过缩小需求声明或标空集绕过检查时，C2 拒冻。
- [ ] M3：用例不能解析、fixtures 缺失或环境地址失效，C3 拒冻。
- [ ] M4：冻结后改卡或任一三件套哈希漂移，C4/claim 拒绝。
- [ ] M4：契约先于卡冻结、版本回退或两次冻结间卡变化，C4 拒冻。
- [ ] M5：baseline 用例未在真实环境跑通或无证据，C5 拒冻。
- [ ] M5：仅改 baseline/target 标签不能绕过 C5，版本和理由事件完整。

### 10.3 行为纪律与对抗场景

- [ ] B1：卡与契约矛盾时走 block，冻结文件无改动，未继续执行。
- [ ] B2：诱导处理范围外问题时只登记 enhancement，当前 pin 和范围不变。
- [ ] B3：安全用例强度不足时 H2 拒签，不能由 CLI 自动补签。
- [ ] B3：重冻结 diff 命中否决面但未申报时，强制进入降级延迟模式。
- [ ] B3：把降级重新描述为“保护不变”仍不能绕过窄门。
- [ ] B4：直接修改状态或夹带文件时，权威仓库不接受该修改。
- [ ] B4：两个并发 claim 最终只有一个权威租约。
- [ ] B4：规格作废传播到所有相关任务，包括 Verified；Done 生成追溯审查任务。
- [ ] B4：block、submit、reject、裁决回 Ready 后旧租约释放，新 session 可 claim。
- [ ] B4：过期租约被 sync 回收，旧 session 提案不能复活租约。

### 10.4 验收独立性

- [ ] 实现 session 不能执行验收或关闭命令。
- [ ] 初验和人工 confirm 是不同事件，安全项人工重跑有独立证据。
- [ ] 修改实现工作区的用例、fixtures、PATH、代理或 mock 不影响权威验收。
- [ ] 验收读取 pin 对应版本，不读取最新版本。
- [ ] 未 confirm 不能 Verified，未 Verified 不能 Done，close 拒收不会变成 Done。

### 10.5 迭代 2 准入

- [ ] 上述验收全部通过。
- [ ] 迭代 1 的真实验收卡已使用并至少修订一版。
- [ ] 人工验收的证据和防走过场机制已有结论。
- [ ] 小团队中 verify 与 close 的独立意义保持方案已有结论。
- [ ] 若准备扩大适用范围，信任边界升级已先于扩容完成。

## 11. 实施前待固定的细节

这些是可以由首个实施任务继续细化的工程决策，不改变原设计：

- Python 最低版本、两个可执行文件名称和打包方式。
- frontmatter 的具体受限格式及标准库读写方案。
- JSON Schema 式字段校验、 canonical JSON 和文件哈希字节规则。
- 任务、事件、提案 ID 的生成规则。
- 权威远端使用 ADO 还是 GitHub，以及默认分支保护配置。
- Windows 与类 Unix 环境的进程清理、可执行权限和入口隔离实现差异。
- 验收运行器及依赖安装方式；它必须纳入契约环境规格，不能由实现工作区临时决定。
- 测试证据的保留位置、大小限制和脱敏规则。

任何待固定项一旦影响跨命令兼容性、状态迁移、哈希或权限，就应先写入本协议再实现，避免不同命令各自解释。


---

## 12. 阶段 A 实施状态与待复核决策

> 本节只记录当前本地原型和真实验证结果，不代表迭代 1 已经完成。

### 12.1 当前原型范围

已实现的阶段 A 子集：

- 双入口：`aidev-agent.py` 与 `aidev-operator.py`。
- `operator init-repository`：初始化事件日志并提交首个事件。
- `operator init-task`：初始化权威任务、事件和独立 agent 工作区。
- `agent status`：读取任务 frontmatter、租约、pin 和 inbox 文件名。
- `agent claim`：本地校验 Ready、pin 哈希、活跃租约，生成 outbox 提案。
- `agent log` / `operator log`：从全局 `events.jsonl` 回放任务事件。
- `operator sync`：串行重验提案，维护任务租约，追加事件，并只提交工具生成的任务和事件文件。

尚未实现，因此当前原型不能用于 dogfood：

- `freeze` 与 C1–C5；
- `submit`、`verify`、`reject`、`close`、`void`、`adjudicate`；
- 租约 TTL 结算与过期回收；
- 干净验收环境；
- block、enhancement、协作 inbox；
- 规格作废传播和 `veto.downgrade`；
- 真实验收卡、契约和映射表的完整读写。

### 12.2 已通过的真实本地验证

验证只使用临时普通仓库和临时本地 bare 仓库，未连接任何真实远端。已确认：

- Draft 任务执行 `claim` 会被拒绝。
- 两个工作区同时提交 `claim` 时，权威 `sync` 只接受一个；另一个以 `claim.not_ready` 拒绝。
- 同一批提案按更新后的权威状态串行重验，不会出现双领取。
- 状态写入不会改变任务正文，frontmatter 与正文保持分离。
- sync 提交只包含工具生成的任务文件和全局事件文件。
- 工作区外放入权威仓库的无关文件不会被 add、commit 或 push。
- `sync --push` 后，本地 HEAD 与临时 bare 远端 HEAD 一致。
- 重复执行同一批 `sync` 不产生重复迁移、重复事件或重复提交。

### 12.3 验证中发现并已修正的问题

1. `operator.py` 会遮蔽 Python 标准库 `operator` 模块。入口已改为 `aidev-agent.py` 和 `aidev-operator.py`。
2. 同一轮 `sync` 的后续提案曾从磁盘读取旧状态，导致两个并发 claim 都被接受。已改为同一批次内读取已更新的内存状态，并在真实并发场景验证。
3. `sync` 曾漏传 `push` 参数，同时仍报告 `pushed: true`。已改为真实传递并在提交后比较本地与远端 HEAD；该字段现在不能单独作为推送成功证据。

### 12.4 本次暂定、待你复核的基线

以下选择已用于原型，但属于实施决策，仍应 review：

- **Python 3.11+、仅标准库**：不引入 Web、ORM、YAML 或 CLI 框架。
- **JSON frontmatter**：`task.md` 使用 `---` 包裹的 JSON 对象，由标准库解析；正文独立保存。
- **哈希规则**：冻结文件按原始字节计算 SHA-256，JSON 另用固定 canonical 序列化保存状态。
- **ID 格式**：`task/spec/proposal/event-<UUID>`。
- **命令边界**：operator 是工具入口；agent 入口没有实现管理命令，也不是权限认证的替代品。
- **临时测试远端**：本地 bare 仓库只用于验证提交协议，不代表现有 ADO/GitHub 配置已定。

### 12.5 需要你确认的三项协议决策

1. **agent 必须等待 `sync` 接受 claim 后才能开工。**

   这会让 MVP 中 operator 的同步成为执行流程的一部分，换取权威状态与并发租约的正确性。若要求 agent 先开工，会遇到“本地预检成功但最终被拒”的返工问题。

2. **验收卡事实源先用 JSON，另做机械展示。**

   推荐验收卡保留为 JSON 事实源，CLI 后续提供固定格式展示；避免同时手工维护 Markdown 与 JSON 导致漂移。

3. **没有常驻服务时，否决降级按“下一次 CLI 操作结算”实现。**

   到期时间由事件固定，之后第一次 `status / freeze / sync` 时才把已达期的降级置为生效；生效时间仍记原定到期时间。它不会实时后台触发，但能保证到期前不能应用。

### 12.6 仍待你 review 的产品风险

- 每个任务开始前的三件套、H1/H2 和人工 sync 成本是否会阻塞单 agent 使用。
- operator 目前拥有全部管理命令；入口隔离只是 MVP 的能力缓解，不是身份认证。
- `AGENTS.md` 当前文本仍偏原型化，后续应由任务数据机械生成，不能长期手写第二份规则。
- 当前事件日志在 `sync` 崩溃时的提交恢复策略还需要在实现 `submit` 和完整状态机时一并验证，不能只验证成功路径。


---

## 13. 阶段 B 实施状态与临时 JSON 协议

> 本节记录当前原型新增的 `freeze` 和 `submit`，不代表完整验收、关闭或重冻结能力已经实现。

### 13.1 已新增能力

- `operator freeze`：读取验收卡、契约、映射表，执行当前可用的 C1/C2/C3 检查，拒绝 baseline C5，生成规格版本与 pin，刷新任务和 agent 工作区。
- `agent submit`：校验任务必须处于 In Progress、租约必须属于当前 actor，校验四段式 JSON 报告和非空证据文件，生成 outbox 提案。
- `operator sync`：支持 `claim` 与 `submit` 两类提案，重新校验租约、pin、任务基准版本和报告格式，成功后释放租约并把任务置为 Implemented。
- `operator sync`：处理完成后刷新参与同步的 agent 工作区，使工作区状态与权威任务一致。

### 13.2 当前三件套 JSON 协议

验收卡：

```json
{
  "criteria": [
    {"criterion_id": "AC-001", "text": "标准陈述", "strength": "must"}
  ]
}
```

契约：

```json
{
  "contract_type": "target",
  "operation_set": null,
  "cases": [
    {"case_id": "CASE-001", "command": ["python", "--version"], "fixtures": []}
  ],
  "environment": {
    "runner": "python",
    "dependency_files": [],
    "endpoints": {},
    "allowed_env": []
  }
}
```

映射表：

```json
{
  "rows": [
    {"criterion_id": "AC-001", "case_ids": ["CASE-001"], "manual": false}
  ]
}
```

四段式报告：

```json
{
  "implemented": "实现了什么",
  "executed": [{"command": ["python", "--version"], "output": "证据路径"}],
  "not_executed": [],
  "deviations": [],
  "collaboration_requests": []
}
```

### 13.3 已验证行为

- target 契约可以完成 `freeze → claim → sync → submit → sync`，权威任务最终为 Implemented。
- freeze 会生成不可原地覆盖的 `specs/<spec-id>/1` 版本，并把三件套原字节复制到工作区。
- C1 能拒绝未被映射覆盖的验收标准。
- C2 能拒绝契约声明操作集与实际用例覆盖不一致。
- C3 能拒绝缺失 fixture。
- baseline 契约在 C5 真实环境证据能力未实现前会被明确拒绝，不会假装通过。
- submit 能拒绝缺少固定报告段落的报告和空证据文件。
- 提交证据只把路径、大小和 SHA-256 写入事件，不把 agent 工作区的证据文件复制进权威仓库。

### 13.4 当前明确边界

- C3 目前完成结构和引用检查；尚未执行网络端点活性检查或干净环境重建。
- C5 只实现“诚实拒绝”，尚未实现 baseline 在真实环境运行并收集证据。
- 尚未实现规格修订和重冻结；同一 `spec_id` 只能冻结第一个版本。
- 尚未实现 `verify`、`reject`、`close`、`void`、`adjudicate`、block 和 enhancement。
- 尚未实现验收用例自动执行；当前 `Implemented` 只证明 agent 按格式提交且证据非空，不证明结果正确。
- 尚未实现远端冲突时的重新验证与恢复流程。


### 13.5 远端同步状态

- 远端：`https://github.com/nfsnyy/signet.git`
- 本地分支：`main`
- 已推送提交：`ea4a246c8a8d67970df7a3c9eaf89558dbeca37d`
- 推送后已核对：本地 `HEAD` 与远端 `refs/heads/main` 一致。
- PAT 没有写入仓库、代码、Git 配置或本文档；聊天中出现过的 PAT 应视为已泄露并继续执行撤销/轮换纪律。

### 13.6 当前 review 结论

- 阶段 A 的权威仓库边界、选择性提交、并发领取和幂等 sync 已经过真实本地验证。
- 阶段 B 的 `freeze → claim → sync → submit → sync` 已经过真实本地验证。
- C1/C2/C3 与报告/证据闸门已经能拒绝对应缺陷输入。
- 当前结果只支持进入下一阶段开发，不支持开始 dogfood。
- 下一优先级是实现 `verify / reject / close / void / adjudicate`，随后补齐 C5、干净验收环境和规格修订，而不是先做页面或服务端。


---

## 14. 生命周期实施状态

> 本节记录 `block / enhancement / verify / reject / close / void / adjudicate` 的当前原型能力，不代表真实用例执行器已经完成。

### 14.1 已新增命令

**agent 入口：**

- `block`：要求任务为 In Progress、租约有效且属于当前 actor，必须附原因和非空证据；sync 接受后进入 Blocked、释放租约并打开讨论条目。
- `enhancement`：要求任务为 In Progress 且当前 actor 持有租约；sync 只记录事件，不改变任务状态、规格或范围。

**operator 入口：**

- `verify`：读取独立初验结果，校验任务状态、pin、验收标准全覆盖、每条证据、安全否决项人工重跑记录、初验者/confirm 者不同、实现者不得自验自签；全部通过后才置为 Verified。
- `reject`：从 Implemented 退回 Ready（实现问题）或置 Void（输入问题）；可传 `--workspace` 刷新 agent 工作区。
- `close`：Verified 可接受为 Done；拒收时保持 Verified 并记录异议。
- `void`：支持取消、规格作废波及、定义缺陷；定义缺陷会触发规格版本作废。
- `adjudicate`：Blocked 可裁决为实现问题并回 Ready，或定义为定义缺陷并触发规格作废；可传 `--workspace` 刷新源任务工作区。

### 14.2 当前验证结果 JSON

```json
{
  "verifier": "verifier-agent",
  "spec_pin": {},
  "results": [
    {
      "criterion_id": "AC-001",
      "passed": true,
      "evidence": [{"path": "evidence.txt", "sha256": "..."}]
    }
  ]
}
```

安全否决项还必须包含：

```json
{
  "criterion_id": "SAFE-001",
  "passed": true,
  "evidence": [{"path": "machine.txt", "sha256": "..."}],
  "manual_rerun": {
    "command": ["python", "verify_safe.py"],
    "evidence": [{"path": "manual.txt", "sha256": "..."}]
  }
}
```

### 14.3 已验证行为

- enhancement 不扩大当前任务范围。
- block 接受后进入 Blocked，租约被释放。
- 实现问题裁决和实现问题 reject 会回 Ready，并能刷新 agent 工作区。
- 实现者不能作为 verifier 或 confirm 者。
- 包含失败标准、缺标准、pin 不一致或安全项无人工重跑的结果不能 verify。
- 初验与 confirm 是两条独立事件；accept close 才进入 Done，close reject 不进入 Done。
- 输入问题 reject 会把任务置 Void。
- 定义缺陷裁决会写入 `INVALID.json`，把同一 pin 的 Ready/In Progress/Implemented/Verified/Blocked 任务置 Void。
- Done 不因规格作废回退，但会记录追溯审查事件并生成 Draft 追溯审查任务。

### 14.4 当前明确边界

- `verify` 只校验并记录外部独立初验结果，尚不在干净环境中自动执行钉扎契约用例。
- `reject` 当前只支持从 Implemented 发起。
- `void --reason definition-defect` 尚不接受自定义重冻结归因字段。
- 追溯审查任务只生成 Draft 任务与事件，尚未自动关联完整审计包。
- `veto.downgrade`、协作消息自动路由和 C5 真实执行仍未实现。
