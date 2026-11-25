# 协作消息与交接旅程（aidev-agent）

> 本文件由 SKILL.md §4 与附录 B 外置。需要发/答协作消息或执行真人交接时读本文件。

## 协作消息规则（inbox/outbox-collab）

- 消息固定三类：`in-spec-request`（请对方调整规格，可 accept/reject/counter/escalate/close）、`definition-doubt`（定义疑问，**强制先 block 自己任务**才能发；接收方不可还价，只能 escalate/close）、`new-scope`（新范围，只登记不接需求，接收方只能 close）。
- 消息落在本工作区 `outbox-collab/`，由**人拷贝**到对方工作区 `inbox/`（平台不自动路由）。你收到对方应答也是人拷进来的，用 `status` 查看 inbox 新文件后 `collab-answer`。
- in-spec-request 双方轮流还价，**最多两轮**，第三轮必须 `escalate`（否则 `collab.round_limit`）。
- 只能应答 `awaiting` 指向你方任务的消息（`collab.not_your_turn`）；终态消息不可再答（`collab.invalid_state`）。

## 附录：真人端到端旅程（交接协议实操）

1. **页面领取**：operator 在任务详情页（Draft/Ready，含派活卡）领取任务，`POST /api/tasks/{id}/claims` 签发 handoff 令牌（sigt）；
2. **复制令牌**：把 sigt 交给 agent（仅此工作区使用，绝不外发）；
3. **agent 入场**：装载本 skill → `claim --handoff` 落地任务包 → 干活、按 verify_notes 自验、攒证据；
4. **submit 回流**：工作区 `submit`（自动附带 sigt，直回流 Implemented）→ operator 验收 verify / close。
