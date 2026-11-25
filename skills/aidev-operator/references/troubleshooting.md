# 故障速查（operator）

> 本文件由 SKILL.md §9 外置。CLI 报错先查本表；表外错误读 `error.code` 原文，不要绕过。

| 现象 | 处置 |
|---|---|
| `claim.not_ready` / `proposal.stale`（agent 侧报来） | 工作区投影滞后：sync 该工作区刷新，agent 重 claim |
| `freeze.invalid_state` | 查任务状态：仅 Draft 或 Implemented 未钉扎可 freeze；已钉扎走 refreeze |
| `c1.duplicate_case` | 用例挂多条判据——改回一案一行 |
| `c1.invalid_strength` | strength 写了 manual——改 must，人工属性写 mapping |
| `c3.asset_hash_mismatch` | 改冻结资产后没更新 contract.json assets 清单 |
| verify 净室红（本地全环境绿） | allowed_env 缺标准 9 项（#21）→ refreeze 补清单 |
| verify 超时（CLI socket timeout） | 服务端净室跑案超 CLI 默认 60s：`export SIGNET_REQUEST_TIMEOUT=600` 重试（先查事件确认上次是否已受理，避免重复） |
| `verify.safety_rerun_required` | safety-veto 判据（如 AC-07）要验证人手动复跑：manual-results 里给该判据加 `manual_rerun: {command, evidence}` 再验 |
| `c8.conformance_field_unexpected` | 活面响应字段多于接口声明——起草漏声明（refreeze 补声明）或实现多返回（修实现），对照基线裁定 |
| `client.api_incompatible` / `client.version_probe_failed` | 新 CLI 连旧服务端（版本协商拒绝）——重启服务加载新代码，或确认两端版本匹配 |
| 共享 HOME client.json 被覆盖 | 每次 CLI 前重新 login（本机多 session 并行固有坑）；Windows 下私有 HOME 要覆盖 USERPROFILE（Path.home() 不读 HOME） |
| sigt 验签失败（agent 报来） | 基本是复制折行——重铸重发（见 command-templates.md「sigt 重铸」），内容不变 |
| `ship.platform_repo_required` | 未 export SIGNET_PLATFORM_REPO |
| 8000 端口杀不掉（Git Bash `kill` 报 No such process 但端口仍占用） | Windows 原生进程用 `taskkill //PID <pid> //F`（Git Bash 里 kill 对非 MSYS 进程无效） |
