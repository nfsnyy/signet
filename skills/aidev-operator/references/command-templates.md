# 关键命令模板（operator）

> 本文件由 SKILL.md §5 外置。冻结/验收/入库动手前读对应小节；环境约定（env 变量、登录）见 SKILL.md §3。

## 签发 sigt

编排式 claim，任务转 In Progress；返回 handoff_token，第一时间存工作区 `.sigt-*.txt`。

```bash
curl -s -X POST "http://127.0.0.1:8000/api/tasks/<task_id>/claims" \
  -H "Authorization: Bearer <nfsnyy 令牌>" -H "Content-Type: application/json" -d '{"actor":"nfsnyy"}'
```

## sigt 重铸

mint_sigt 确定性——令牌丢失/损坏/折行时内容不变重发。

```bash
python -c "from aidev.gitrepo import ensure_repository; from aidev.authn import mint_sigt; \
print(mint_sigt('<task_id>','nfsnyy',str(ensure_repository('<状态仓>'))))"
```

## freeze（内容上传）

CLI 本地读三件套并自动附 card_sha256/contract_sha256/mapping_sha256 随件摘要 + `[{"path","sha256"}]` 资产清单；`--repo` 可省略=服务端绑定存储，逻辑 id 亦可；成功后投影自动经任务包拉取落盘 `--workspace` 本地目录。

```bash
python aidev-operator.py freeze --task-id <id> \
  --acceptance-card <本地路径>/acceptance-card.json --contract <本地路径>/contract.json \
  --mapping <本地路径>/mapping.json --h1-actor nfsnyy --h2-actor nfsnyy \
  [--repo <逻辑 id>] [--workspace <本地目录>]
```

接口类任务冻结（T12 起）：contract 必须携带非空 interfaces 段，缺省/null → `c2.interfaces_missing` 拒冻。判型信号 = task.md frontmatter `interface_class`（建任务/派生时按「新增/变更端点、表单、事件负载」标注，缺省 false）。声明范式（version 1；endpoints 四凭据族 auth 矩阵 + responses.success/rejection 双形态；forms 字段三键 name/type/required；events payload schema 子集 type/required/properties/items/const）与完整示例见 `doc/三件套编写规范.md` §2.1（示例 = T12 冻结契约 spec-fb64d0cb 的 interfaces 段）。

## verify / close

`--repo` 同 freeze：省略=绑定存储，逻辑 id=绑定仓 project_id。

```bash
python aidev-operator.py verify --task-id <id> --execute \
  --manual-results <人审json> --verifier operator --confirmed-by nfsnyy
python aidev-operator.py close --task-id <id> --decision accept \
  --actor nfsnyy --reason '...' --push --ship   # ship 需 SIGNET_PLATFORM_REPO
```

注意：verify --execute 服务端净室跑全部冻结案可能数分钟，CLI 默认超时 60s——大任务先 `export SIGNET_REQUEST_TIMEOUT=600`（T22 教训）。
