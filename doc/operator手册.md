# signet 平台 operator 手册

> **权威版本 = 平台仓 `skills/aidev-operator/SKILL.md`（operator skill，随代码版本走，带机测护栏）。**
> 本文档是其扩展阅读：多留叙事性说明与新 session 接管清单；与 skill 冲突时以 skill 为准。
> 岗位剧本：任何 session 读 operator skill + `skills/aidev-agent/SKILL.md` + 状态仓账本，即可接管 operator 岗。
> 适用版本：平台仓 T8 收官之后（v0.9.2 形态）。更新时机：任务流程动词、凭证模型、环境约定变化时。

## 1. 岗位定义

operator = 流程仪仗 + 验收人 + 平台运维。**永远不实现任务**（写验收代码属实现，归实现会话）。

- 做：需求登记/确认、init-task、签发 sigt、出派活卡、起草裁决、组织 H1/H2 签署、freeze/refreeze、sync、verify、close --ship、环境维护（8000 服务）、经验合入（hints.md / §15 债务）。
- 不做：改平台功能代码（那是任务）、替 agent 写三件套（那是起草会话的活）、把密码/令牌交给任务之外的任何人。

## 2. 环境约定（每次开工前）

```bash
export SIGNET_BASE_URL=http://127.0.0.1:8000      # 平台服务
export SIGNET_PLATFORM_REPO='C:/myFolder/dsh/ai-registry/辅助开发平台/iteration-1'  # 仅 operator 动词（ship/verify 定位被测代码）需要
# 每次 CLI 操作前必做（共享 HOME 的 client.json 会被其它登录覆盖）：
python aidev-agent.py login --user nfsnyy --password '<口令在 hints.md/口头约定>'
# Git Bash 路径一律 C:/ 形态；中文路径注意引号
```

8000 常驻服务：`SIGNET_STATE_REPO=<状态仓> SIGNET_PLATFORM_REPO=<平台仓> python -m aidev.web.server --port 8000`（后台）。平台仓代码变更后**必须重启**才生效；服务绑定状态仓，重启不动账本。

## 3. 任务生命周期仪式（每任务标准序列）

```
需求确认（改 requirements/<id>/requirement.md status 字段，commit 状态仓）
  → init-task（Draft）→ 签发起草 sigt → 出起草派活卡（用户转发给新 session）
  → 起草会话 submit → operator 对账（账本事件链/归因/资产哈希/用例 py_compile）
  → 逐项裁决起草方提的问题 → 出 H1/H2 签署摘要 → 用户"签"
  → freeze（Implemented 未钉扎可直接 freeze——3126303 已开门）
  → 建 agent-tN 账号 + 签实现 sigt → 出实现派活卡（强调：实现会话必须新开窗口）
  → 实现会话 submit → 对账 → 独立复跑冻结用例
  → 人工 AC：出人审走查清单，用户走查"通过" → 写 manual-results（证据文件+sha256，证据须 {path,sha256} 结构）
  → verify --execute --manual-results ...（落 verification.initial/confirm/task.verified）
  → close 摘要 → 用户"确认" → close --decision accept --push --ship
  → 清杂（删 spec 目录 __pycache__ 等）→ disable agent-tN 账号 → 设计稿变更历史 + §15 补记
```

## 4. 关键命令模板

```bash
# 建任务（工作区由 init-task 创建）
python aidev-operator.py init-task --repo <状态仓> --workspace <工作区路径> --title '<标题>' --requirement <需求id> --owner nfsnyy --actor nfsnyy

# 签发 sigt（编排式 claim，任务转 In Progress；返回 handoff_token——第一时间存 .sigt-*.txt）
curl -s -X POST "http://127.0.0.1:8000/api/tasks/<task_id>/claims" \
  -H "Authorization: Bearer <nfsnyy 令牌>" -H "Content-Type: application/json" -d '{"actor":"nfsnyy"}'
# 令牌丢失/损坏可本地重铸（mint_sigt 确定性）：
python -c "from aidev.gitrepo import ensure_repository; from aidev.authn import mint_sigt; \
print(mint_sigt('<task_id>','nfsnyy',str(ensure_repository('<状态仓>'))))"

# freeze（从工作区 contract/ 读三件套；CWD 必须在 contract 上级，相对路径以 CWD 解析）
python aidev-operator.py freeze --repo <状态仓> --task-id <id> --workspace <工作区> \
  --acceptance-card contract/acceptance-card.json --contract contract/contract.json \
  --mapping contract/mapping.json --h1-actor nfsnyy --h2-actor nfsnyy
# refreeze 追加 --reason '...'；其余参数同

# 对账用例（从冻结 spec 目录跑才是真净室基线；跑完删 __pycache__）
cd <状态仓>/specs/<spec_id>/<version> && python case-xxx.py

# verify（先写 manual-results 文件，AC-人审一条，证据 {path,sha256}）
python aidev-operator.py verify --repo <状态仓> --task-id <id> --execute \
  --manual-results <证据json> --verifier operator --confirmed-by nfsnyy

# close（ship 需 SIGNET_PLATFORM_REPO；推双仓）
python aidev-operator.py close --repo <状态仓> --task-id <id> --decision accept \
  --actor nfsnyy --reason '...' --push --ship
```

## 5. 派活卡模板（起草/实现两张卡）

卡 = 五段：**角色**（任务 id、spec 状态、执行者标签、skill 指针）→ **入场**（login/claim --handoff 命令、workspace 路径、sigt 代码块、"单行勿折行/损坏找 operator 重发"）→ **输入材料**（需求正文路径、原型页、格式范本 spec 路径、被测代码文件）→ **范围+纪律**（以冻结三件套/任务标题为准绳；不 freeze、不宣布完成、不 push；歧义列裁决项；五段式报告+evidence；实现卡额外强调"新开窗口"）→ **收尾协议**（submit 后汇报"等 operator"，不宣布完成）。

起草卡与实现卡的差异：起草卡写"产出三件套+用例（红即规格目标）"；实现卡写"照冻结三件套实现（用例必须全绿）+ 全量回归基线只增不减"。

## 6. 对账清单（起草/实现 submit 后必做）

1. 账本事件链完整（initialized→claimed→…→submitted），归因 actor=签发人/obo=执行者
2. 变更面：实现交付后 `git status` 应只有预期文件；起草期 contract 外无杂散
3. contract.json assets sha256 逐一对账磁盘文件；T7 教训——改冻结资产必须同步更新清单
4. 用例 py_compile 全过；起草期能跑的跑一遍（回归类当场绿，新特性红是基线）
5. mapping 一案一行（1:1）、strength 只 must/must-not/safety-veto、allowed_env 含标准 9 项
6. spec 目录复跑用例后 `find . -name __pycache__` 清杂再 push

## 7. 故障速查

| 现象 | 原因与处置 |
|---|---|
| claim.not_ready / proposal.stale | 工作区投影滞后：operator sync 该工作区刷新后，会话重 claim |
| freeze.invalid_state | 任务不是 Draft/Implemented 未钉扎；查状态机位置 |
| c1.duplicate_case | 一个用例挂多条判据——改映射回 1:1 |
| c1.invalid_strength | 人工项 strength 写了 manual——must + mapping manual:true |
| c2/c3 类校验红 | 按报错词典对照三件套编写规范 §4 逐条修 |
| verify 净室红（本地绿） | allowed_env 缺标准项（#21）；refreeze 补清单 |
| 共享 HOME client.json 被覆盖 | 每次操作前重新 login（本机多 session 并行的固有坑） |
| sigt 验签失败 | 多半是复制折行——operator 重发（mint_sigt 确定性，内容不变） |

## 8. 新 session 接管清单

1. 读本手册 + SKILL.md + 状态仓 hints.md
2. `git log --oneline -8` 状态仓/平台仓看最近动态；任务池：`for d in <状态仓>/tasks/task-*/; do ...` 或看板页面
3. 确认 8000 服务在跑（`/api/health`）；不在则按 §2 拉起
4. 向用户报到，从当前任务序列的断点继续
