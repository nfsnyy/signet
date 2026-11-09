# 任务工作区约束

- task_id: task-f9bbae0b-a145-472a-98a3-539942464aa5
- requirement: t2-auth-fixture
- spec_pin: null
- 冻结文件：contract/acceptance-card.json、contract/contract.json、contract/mapping.json。
- 不得修改冻结文件，不得自行变更任务状态。
- 写操作必须通过 agent CLI 形成 outbox 提案，并等待 operator sync 接受。
- 提交报告必须包含：实现了什么、运行了什么、未运行或跳过项及原因、与验收卡的偏差、协作请求清单。
