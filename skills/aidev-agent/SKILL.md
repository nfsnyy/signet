---
name: aidev-agent
description: 在辅助开发平台（aidev）任务工作区内作为 agent 执行任务的完整协议：aidev-agent CLI 全部动词、参数、硬性约束与工作流
type: prompt
whenToUse: 当任务位于 aidev 工作区（目录里含 task.md、AGENTS.md、contract/、outbox/），或用户要求我作为 agent 通过 aidev-agent CLI 领取、提交、阻塞任务时
---

# aidev-agent 使用协议

你在"辅助开发平台"中扮演 **agent** 角色。平台用 Git 状态仓库做权威账本，你的所有写操作只生成**提案**（JSON 文件），由 operator 执行 `aidev-operator.py sync` 后才生效。

## 0. 平台入口定位

- 本 skill 文件位于 signet 仓库的 `skills/aidev-agent/` 目录下；平台 CLI 在**仓库根目录**，即从本文件所在目录向上两级：`../../aidev-agent.py`（operator 侧为 `../../aidev-operator.py`）。拿到本 skill 时，先确认它的实际落盘位置，再按此相对关系定位 CLI。
- 若工作区 `AGENTS.md` 或用户给出了其他平台路径（例如 skill 被单独拷贝、与仓库分离的情况），以给出的为准。
- 全部命令形如：`python <平台路径>\aidev-agent.py <动词> --workspace <工作区路径> ...`

### 工作区从哪里来（准备阶段是 operator 的职责）

- **工作区由 operator 创建，agent 没有创建仓库/任务的动词，也不要自己 mkdir 补建。** 你通常会从 operator 手里拿到一个已经建好的工作区路径。
- 标准准备流程（由 operator 侧执行，供你了解上下文）：
  1. `python aidev-operator.py init-repository --repo <状态仓库路径> --actor <owner>` —— 建权威账本（一个 Git 仓库）；
  2. `python aidev-operator.py init-task --repo <状态仓库路径> --workspace <工作区路径> --title ... --requirement ... --owner ... --actor ...` —— 建任务并初始化工作区（工作区必须是**空目录**，且不能位于状态仓库内）；
  3. 只有 operator `freeze` 冻结规格后，任务才进入 Ready、才允许 `claim`。
- **agent 入场自检**：工作区里应存在 `task.md`、`AGENTS.md`、`contract/`、`evidence/`、`inbox/`、`outbox/`、`outbox-collab/`。发现缺失或被破坏时，停止并向 operator 报告，不要自行创建或修复这些文件。
- 所有命令输出 JSON：成功 `{"ok": true, "result": ...}`；失败 `{"ok": false, "error": {"code": ...}}`。每一步都要检查 `ok`，失败时读 `error.code` 并按规则纠正后重试，不要绕过。
- 运行前提：Python 3.11+，只用标准库。

## 1. 动词清单（agent 面）

| 动词 | 作用 | 关键参数 | 前置条件 |
|---|---|---|---|
| `status` | 查看任务状态、inbox/outbox-collab 文件列表（只读） | `--workspace` | 已登录（CLI 自动携带令牌） |
| `log` | 回放任务事件日志（只读） | `--repo`、`--task-id` | 已登录 |
| `claim` | 领取任务，生成 claim 提案 | `--actor`、`--on-behalf-of` | 任务 Ready、无活跃租约、contract/ 哈希与 pin 一致 |
| `submit` | 提交实现，生成 submit 提案 | `--actor`、`--on-behalf-of`、`--report <文件>`、`--evidence <文件>`（可重复） | 任务 In Progress 且**你持有活跃租约** |
| `block` | 因定义/前提问题阻塞任务 | `--actor`、`--on-behalf-of`、`--reason`、`--evidence`（可重复） | 任务 In Progress 且你持有租约 |
| `enhancement` | 登记后续改进想法（不改变当前范围） | `--actor`、`--on-behalf-of`、`--title`、`--details` | 任务 In Progress 且你持有租约 |
| `collab-send` | 向另一个任务发协作消息 | `--type`、`--to-task`、`--summary`、`--details` | 见第 4 节 |
| `collab-answer` | 应答发给你任务的协作消息 | `--message-id`、`--action`、`--note` | 消息在 inbox/ 且轮到你 |
| `handoff-inspect` | 纯本地解析并验签 handoff 能力令牌（sigt），输出 v/task/actor/repo 与 valid，不联网 | `<令牌>`（位置参数） | 读本机 `~/.signet/server-secret` |
| `health` | 检查平台服务连通性（只读探针） | 无 | 无 |

operator 面动词（`init-repository/init-task/freeze/refreeze/sync/verify/reject/close/void/adjudicate/veto/inbox/login`）**不是你用的**，不要尝试调用。

## 1.1 登录与凭据（T2 起）

- 平台全部 API（除 `/api/health` 与 `/api/login`）都要求凭据：CLI 用户先用 `aidev-operator.py login --user <名> --password <口令>` 登录，令牌写入 `~/.signet/client.json`，此后两个 CLI 的所有动词自动以 `Authorization: Bearer` 携带。
- 服务端按凭据身份覆盖请求体中的 `actor` / `on_behalf_of`：事件归因永远等于凭据身份，`--actor` 参数仅保留签名兼容。
- 凭据纪律：CLI 令牌与 handoff 令牌只在本机、本工作区内使用，绝不外发；`handoff-inspect` 只读本机 secret，不联网。

## 1.2 handoff 令牌会话启动（claim --handoff）

- 从 operator 拿到 sigt 后，入场第一步：`python <平台路径>\aidev-agent.py claim --handoff <sigt> --workspace <工作区目录>`。CLI 先在**本地预检**验签（复算 HMAC，不发任何请求；坏令牌在指向死端口时也会被拒绝），通过后向服务端幂等接管任务，并把完整任务包落地工作区：`task.md`、`AGENTS.md`、`contract/` 三件套与全部资产（逐项校验 sha256）、`.signet-token`、状态仓根 `hints.md`（有则注入）。
- **工作区自动附带**：含 `.signet-token` 的工作区执行 `status` / `submit` 等动词时，CLI 自动以 sigt 附带（无需 `--handoff`、`--actor`）；`submit` 走服务端直回流，状态立即 Implemented。每轮循环先 `status`；被拒或状态与预期不符时先 `log` 读事件链再决定。
- **重 claim（幂等续作）**：同 actor 持 sigt 重跑 claim = 接管 + 续期（`lease.renewed` 事件、`expires_at` 顺延 24h）；换 actor 必须先由 operator 释放租约，令牌不会越权接管他人租约。
- **返场**：reject 后若工作区投影未刷新、提案 stale（`proposal.stale` / `proposal.spec_pin_mismatch`），处置 = 请 operator sync 后重 claim；不得手改 `task.md` 强推。

## 2. 硬性约束（违反会被拒绝或作废）

1. **不得直接修改** `task.md`、`AGENTS.md`、`contract/` 下任何文件；任务状态只能经提案 + operator sync 变更。
2. `contract/` 是冻结规格副本，claim/submit 时平台会校验其 SHA-256 与 spec_pin 一致，篡改会报 `claim.spec_hash_mismatch`。
3. 提案不是权威状态：`claim` 后若 operator 未 sync，你本地 task.md 不会变 In Progress；先 `status` 确认（必要时请 operator sync）。
4. 租约 24 小时过期；只有租约持有者（你的 `--actor`）能 submit/block/enhancement，否则 `*.lease_owner_mismatch`。
5. `--actor` 必须是合法别名：`^[a-z0-9][a-z0-9\-*]{0,63}$`（如 `agent-kimi`）；禁止真实姓名、邮箱、大写、空格。`--on-behalf-of` 填任务 owner。
6. 所有写命令可重入：同一条提案重复 sync 不会产生重复事件，提案文件留在 outbox/ 属正常。

## 3. submit 报告格式（最容易被拒的地方）

`--report` 指向的 JSON 必须**恰好**包含以下五个段，多一段少一段都会 `submit.invalid_report`：

```json
{
  "implemented": "做了什么（非空字符串）",
  "executed": [{"command": ["python", "--version"], "output": "运行输出（非空）"}],
  "not_executed": [{"item": "未运行项", "reason": "原因"}],
  "deviations": [{"...": "与验收卡的偏差说明，可为空列表"}],
  "collaboration_requests": [{"message_id": "msg-...", "status": "open"}]
}
```

- `--evidence` 至少一个非空文件，每个证据会被记录路径 + SHA-256。
- `collaboration_requests` 里列的 `message_id` 必须是已 sync 归档、且与本任务相关的协作消息，否则提案被 `submit.collaboration_unknown` 拒绝；没有协作请求就放空列表 `[]`。

## 4. 协作消息规则（inbox/outbox-collab）

- 消息固定三类：`in-spec-request`（请对方调整规格，可 accept/reject/counter/escalate/close）、`definition-doubt`（定义疑问，**强制先 block 自己任务**才能发；接收方不可还价，只能 escalate/close）、`new-scope`（新范围，只登记不接需求，接收方只能 close）。
- 消息落在本工作区 `outbox-collab/`，由**人拷贝**到对方工作区 `inbox/`（平台不自动路由）。你收到对方应答也是人拷进来的，用 `status` 查看 inbox 新文件后 `collab-answer`。
- in-spec-request 双方轮流还价，**最多两轮**，第三轮必须 `escalate`（否则 `collab.round_limit`）。
- 只能应答 `awaiting` 指向你方任务的消息（`collab.not_your_turn`）；终态消息不可再答（`collab.invalid_state`）。

## 5. 标准工作流

```text
读 AGENTS.md + contract/ 三件套（验收卡/契约/映射）明确验收标准
  → status 确认 Ready → claim
  → 请 operator sync（提案生效，工作区刷新为 In Progress，获得租约）
  → 在代码仓库干活、运行验证、攒证据文件
  → submit（报告 + 证据）
  → 若定义有问题：先 block（带证据），再视情况 collab-send
  → operator verify/close 后任务关闭；终态(Void/Done)任务不可再 claim
```

先读 `AGENTS.md` 与冻结三件套再动手；不确定能否做某事时，优先用 `block` 提出问题而不是猜。

## 6. 自验环境、收尾与反哺纪律

### 6.1 自验环境（完成定义）

- 验收卡 verify_notes 的字面环境就是完成定义，逐条对齐后再宣布做完：`SIGNET_STATE_REPO` = **可弃置克隆**（可弃置的状态仓克隆；编排会真实写入，绝不用开发主仓）、`SIGNET_PLATFORM_REPO` = 平台仓工作副本（runner 仅用它定位被测代码）、`SIGNET_BASE_URL` = 被测服务自引用（机器用例由服务端执行，取服务端进程环境）；服务端进程环境须自带三者，默认端口 8000 须空闲。
- 双变量分工：STATE 变量绑定服务端（唯一权威账本），PLATFORM 变量只给 runner/CLI 定位代码——写反会静默失效。
- Git Bash 下环境变量路径必须 `C:/` 形态（`/c/...` 不转换、静默失效）；命令行参数两种形态皆可。

### 6.2 收尾

- 收尾必须关闭自己起的全部服务进程（先按端口定位 PID 再杀，注意别只杀掉包装 shell）、清理编排残留（临时工作区、临时 HOME、编排账号）。
- 中断或收尾前在工作区写 `handoff.md` 交接摘要：进展 / 下一步 / 环境残留，供接续者（或返场的自己）入场直读。

### 6.3 反哺（双通道，勿混）

- **经验教训**（后辈该怎么干活）→ 写工作区 `hints-proposal.md`（随手记，一条一行），submit 后由 operator 验收时评审合入状态仓 `hints.md`。
- **改进点**（平台/项目该怎么改）→ 用 `enhancement` 动词登记，进 backlog 容器，operator 分拣。
- 入场必读：状态仓根 `hints.md`（随任务包注入工作区）是本项目环境级经验的唯一入口；动手前先读。

## 附录 B：真人端到端旅程（交接协议实操）

1. **页面领取**：operator 在任务详情页（Draft/Ready，含派活卡）领取任务，`POST /api/tasks/{id}/claims` 签发 handoff 令牌（sigt）；
2. **复制令牌**：把 sigt 交给 agent（仅此工作区使用，绝不外发）；
3. **agent 入场**：装载本 skill → `claim --handoff` 落地任务包 → 干活、按 verify_notes 自验、攒证据；
4. **submit 回流**：工作区 `submit`（自动附带 sigt，直回流 Implemented）→ operator 验收 verify / close。
