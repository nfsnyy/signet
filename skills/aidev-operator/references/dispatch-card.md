# 派活卡模板（operator）

> 本文件由 SKILL.md §6 外置。出派活卡前读本文件；对账与返场前置检查见 SKILL.md §7。

卡 = **角色**（任务 id、spec 状态、执行者标签、skill 指针、实现卡加粗"必须新开 session"）→ **入场**（`login` / `claim --handoff <sigt> --workspace <路径> --on-behalf-of <标签>` 命令、sigt 代码块、"单行勿折行/验签失败找 operator 重发勿自行拼接"）→ **输入材料**（需求正文路径、原型页、格式范本 spec 路径、被测代码文件清单）→ **范围+纪律**（以冻结三件套/任务标题为准绳；不 freeze、不宣布完成、不 push；歧义列裁决项等 operator；五段式报告 + evidence）→ **收尾协议**（submit 后汇报"等 operator"，不宣布完成）。operator 侧动词参数示例一律用逻辑 id 或省略（路径形态 repo 已被服务端拒绝，400 repo.invalid_identifier）。

- 起草卡：产出三件套 + 用例；新特性用例"红即规格目标"，回归类当场绿；py_compile 全过。
- 实现卡：照冻结三件套实现；用例全绿才算完；全量回归基线只增不减。

**返场指令四要素**（SKILL.md §7.9 前置检查通过后发出）：修正内容、执行步骤、身份要求（login 谁 / `--on-behalf-of` 谁 / 私有 HOME 防 client.json 串身份）、状态前置（任务非 Ready 时先由 operator reject，或写明"卡住等 operator reject"）。
