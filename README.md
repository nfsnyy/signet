# 辅助开发平台（aidev / signet）

一套以 Git 状态仓库为权威账本的轻量人机协作开发平台：operator（人）建任务、冻结规格、sync 提案；agent（AI）在工作区里 claim、干活、submit 提案。所有状态变更都由 operator 验证后才入账，agent 侧永远不直接改账本。

## 部署前提

- Python 3.11+。
- Git 可用且加入 PATH（状态仓账本依赖 Git 提交与哈希校验）。
- 平台服务依赖 FastAPI 与 uvicorn：`pip install -r requirements.txt`（`aidev/` 核心层仍仅使用标准库，第三方依赖只出现在 `aidev/web/`）。
- 平台本体即本仓库；agent 工作区由 operator 另行创建，且工作区不得位于状态仓内部。

## 服务启动

平台以常驻 HTTP 服务形式运行，两个 CLI 只是薄壳客户端（参数解析 + HTTP 请求 + JSON 透传，不含业务逻辑）：

```bash
python -m aidev.web.server            # 默认监听 127.0.0.1:8000
python -m aidev.web.server --host 127.0.0.1 --port 8000
```

- 服务可用 `SIGNET_PLATFORM_REPO` 环境变量指定默认状态仓（operator 动词未显式传 `repo` 时使用）。
- 存活探针：`GET /api/health` 返回 `{"status": "ok"}`。
- 全动词端点见仓库根 `api-catalog.json`；服务同时提供 `/openapi.json`。

## CLI 双入口

仓库根目录提供两个入口脚本，动词面严格划分、互不越权：

- `python aidev-agent.py <动词> --workspace <工作区路径>` —— **agent 面**。面向执行任务的 AI：`status` / `log` / `health`（只读），`claim` / `submit` / `block` / `enhancement` / `collab-send` / `collab-answer`（写操作，仅生成 outbox 提案）。
- `python aidev-operator.py <动词> ...` —— **operator 面**。面向运营平台的人：`init-repository` / `init-task` / `freeze` / `refreeze` / `sync` / `verify` / `reject` / `close` / `void` / `adjudicate` / `veto` / `inbox` / `log` / `health`。

CLI 通过以下顺序确定服务地址（`aidev-agent.py health` 可用于检查连通性）：

1. 环境变量 `SIGNET_BASE_URL`（如 `http://127.0.0.1:8000`）；
2. `~/.signet/client.json` 中的 `base_url`；
3. 默认 `http://127.0.0.1:8000`。

服务不可达时 CLI 非零退出，并在 stderr 给出启动提示。

## 状态仓与提交协议

- **权威副本在远端**：任务状态账本是一个 Git 仓库，权威状态以 operator 侧的账本为准；各工作区里的 `task.md` 等文件只是账本的投影，可能滞后。
- **agent 动作 = 提案 → sync 验证入库**：agent 的 `claim` / `submit` / `block` 等写操作只会在工作区 `outbox/` 生成提案 JSON；operator 执行 `sync` 时校验规格 pin（SHA-256）、租约归属与状态前置条件，全部通过事件才进入账本并刷新工作区。
- **工具永不全量 add**：`sync` 等命令只把已登记的提案文件与规定路径加入提交，绝不使用全量 `git add -A` 或 `git add .`，避免无关或敏感文件被带进账本。

## 适用范围与威胁模型

- **适用范围**：单人 operator 的本地信任域——同一台机器上，operator 与若干 agent 协作完成开发任务。
- **不防恶意 agent**：协议假设 agent 大致诚实，防的是误操作与流程绕过，不防蓄意伪造证据或篡改工作区文件；冻结规格靠 pin 哈希校验兜底，发现不一致即拒绝提案。
- **公网暴露前必须补强身份**：当前 actor 只是约定别名，没有认证与授权；若把平台接入网络或多人共用环境，必须先引入身份认证、传输加密与权限隔离。

## 环境自检

`python scripts/check_env.py` 输出本机环境摘要（JSON）：Python 版本、Git 版本、仓库根 `project.json` 中的 `project_id`（无该文件则为 `null`）。`--require-version X.Y` 可要求最低 Python 版本，不满足时以非零退出并给出 `reason`。

仓库根定位顺序：环境变量 `SIGNET_PLATFORM_REPO` 优先，否则取脚本所在仓库根（`scripts/` 的上一级）。
