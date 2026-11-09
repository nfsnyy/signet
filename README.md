# 辅助开发平台（aidev / signet）

一套以 Git 状态仓库为权威账本的轻量人机协作开发平台：operator（人）建任务、冻结规格、sync 提案；agent（AI）在工作区里 claim、干活、submit 提案。所有状态变更都由 operator 验证后才入账，agent 侧永远不直接改账本。

## 部署前提

- Python 3.11+。
- Git 可用且加入 PATH（状态仓账本依赖 Git 提交与哈希校验）。
- 平台服务依赖 FastAPI 与 uvicorn：`pip install -r requirements.txt`（`aidev/` 核心层仍仅使用标准库，第三方依赖只出现在 `aidev/web/`）。
- 平台本体即本仓库；agent 工作区由 operator 另行创建，且工作区不得位于状态仓内部。

## 服务启动

平台以常驻 HTTP 服务形式运行，CLI 只是薄壳客户端（参数解析 + HTTP 请求 + JSON 透传，不含业务逻辑）：

```bash
python -m aidev.web.server            # 默认监听 127.0.0.1:8000
python -m aidev.web.server --host 127.0.0.1 --port 8000
```

- 服务用 `SIGNET_STATE_REPO` 环境变量（或等价的 `--repo` 启动参数）绑定权威状态仓：请求带**可用的**显式 `repo` 时尊重请求（operator 动词 `--repo` 须被尊重），否则落到绑定仓；都不可用时报错 `git.not_repository`。编排写入（init-task/freeze/claim 等）因此总是落在绑定仓上。
- `SIGNET_PLATFORM_REPO` 与绑定无关：它仅供 runner/CLI 定位**被测代码**（平台仓工作副本），服务端不读取它作为状态仓；验收形态即 `SIGNET_STATE_REPO=<状态仓克隆>` + `SIGNET_PLATFORM_REPO=<平台仓>` 双变量并存。
- 存活探针：`GET /api/health` 返回 `{"status": "ok"}`，公开无鉴权。
- 除 `/api/health` 与 `POST /api/login` 外，全部 `/api/*` 端点一律要求凭据：无凭据/坏凭据返回 401 JSON，不进入应用层。凭据可以是 `Authorization: Bearer` 令牌，也可以是有效会话 cookie（浏览器直接访问读端点）。
- 全部 API 响应统一为信封：`{"ok": true, "result": ...}` 或 `{"ok": false, "error": {"code", "message", "details"}}`。
- 全动词端点见仓库根 `api-catalog.json`；服务同时提供 `/openapi.json`。

## project 与需求只读面（T3）

- `GET /api/project`：概况 JSON，含状态仓标识 `repo`、任务状态计数 `task_counts`（各状态之和恒等于 `tasks/` 目录数）、待验收置顶列表 `pending_verification`（状态为 Implemented 的任务）、事件时间线 `recent_events`（新的在前）与当前 `commit` 标识（git HEAD）。服务端写动词提交后重读即时反映。
- `GET /api/requirements`：需求录列表，每条含 `requirement_id`/`title`/`status`/`priority`/`owner`/`progress`/`task_count`；`progress = Done 数 /（反查任务总数 - Void 数）`，分母为 0 时取 0。
- `GET /api/requirements/{requirement_id}`：需求详情，`body` 为仓内 `requirement.md` 去掉首个 YAML frontmatter 块后的原稿（逐字节一致，v1 不做 markdown 渲染）；`tasks` 为反查任务集合；`progress` 口径同上。
- 页面三件套（未登录一律 302 到 `/login`）：`GET /` 概况（任务计数、待验收、最近事件）、`GET /requirements` 需求列表、`GET /requirements/{requirement_id}` 详情（正文以转义原稿展示）。
- 读模型以状态仓为唯一事实源直读，见 `aidev/readmodel.py`；页面渲染见 `aidev/web/pages.py`。

## 登录与凭据

- **账号**：服务端数据目录 `~/.signet/`（不进 git）持有 `users.json`（PBKDF2 口令散列）、`server-secret`（HMAC 密钥，0600，惰性生成）与 `service-audit.jsonl`（管理审计，追加写）。服务每次登录实时读取 `users.json`，新建账号下一次登录即认可，无需重启。
- **管理入口**：仓根第三个 CLI `aidev-admin.py` 提供 `create-user` / `disable-user` / `reset-password` / `list-users`，直写服务端数据目录。
- **CLI 登录**：`python aidev-operator.py login --user <名> --password <口令>` 成功后，令牌写入 `~/.signet/client.json`（`token` 字段），此后两个 CLI 的所有动词自动以 `Authorization: Bearer` 携带。同一用户连续 5 次口令错误后，后续登录请求返回 429，成功登录即重置计数。
- **浏览器登录**：未登录访问 `GET /` 重定向 `/login`；登录成功持有 HttpOnly + SameSite=Lax 会话 cookie，`GET /` 呈现平台概况（当前用户、任务计数、待验收、最近事件）。会话存内存表，服务重启即失效。
- **身份锁定**：服务端以凭据身份覆盖请求体中的 `actor` / `on_behalf_of`，事件链归因恒等于凭据身份（`--actor` 参数仅保留签名兼容）。
- **handoff 能力令牌（sigt）**：`sigt-<b64url(json{v,task,actor,repo})>-<hmac256-32hex>`，签名 = HMAC-SHA256(key=server-secret, message=b64 段) 前 32 个 hex；claim 的编排响应经 `handoff_token` 字段签发。`aidev-agent.py handoff-inspect <令牌>` 纯本地解析 + 验签（读本机 secret，不联网），输出 `v/task/actor/repo` 与 `valid`。
- **授权域**：sigt 调 operator 面端点一律 403；sigt 任务与路径 task_id 不一致一律 403；claim 以外动词要求任务活跃租约由令牌 actor 持有，否则 403。

### 凭据纪律

- CLI 令牌、会话 cookie 与 sigt 只在本机本工作区内使用，**永不外发**（不贴进聊天、不提交进 git、不写进工作区交付物）。
- `server-secret` 与 `users.json` 只存在于服务端数据目录，绝不进入 git；代码评审以“无硬编码密钥形态”为准入线。
- `handoff-inspect` 只读本机 `~/.signet/server-secret`，不发起任何网络请求。

## CLI 入口

仓库根目录提供三个入口脚本，动词面严格划分、互不越权：

- `python aidev-agent.py <动词> --workspace <工作区路径>` —— **agent 面**。面向执行任务的 AI：`status` / `log` / `health`（只读），`claim` / `submit` / `block` / `enhancement` / `collab-send` / `collab-answer`（写操作，仅生成 outbox 提案），`handoff-inspect`（本地验签）。
- `python aidev-operator.py <动词> ...` —— **operator 面**。面向运营平台的人：`login` / `init-repository` / `init-task` / `freeze` / `refreeze` / `sync` / `verify` / `reject` / `close` / `void` / `adjudicate` / `veto` / `inbox` / `log` / `health`。
- `python aidev-admin.py <动词> ...` —— **管理面**（本机）：`create-user` / `disable-user` / `reset-password` / `list-users`。

CLI 通过以下顺序确定服务地址（`aidev-agent.py health` 可用于检查连通性）：

1. 环境变量 `SIGNET_BASE_URL`（如 `http://127.0.0.1:8000`）；
2. `~/.signet/client.json` 中的 `base_url`；
3. 默认 `http://127.0.0.1:8000`。

服务不可达时 CLI 非零退出，并在 stderr 给出启动提示。

## 状态仓与提交协议

- **权威副本在远端**：任务状态账本是一个 Git 仓库，权威状态以 operator 侧的账本为准；各工作区里的 `task.md` 等文件只是账本的投影，可能滞后。
- **agent 动作 = 提案 → sync 验证入库**：agent 的 `claim` / `submit` / `block` 等写操作只会在工作区 `outbox/` 生成提案 JSON；operator 执行 `sync` 时校验规格 pin（SHA-256）、租约归属与状态前置条件，全部通过事件才进入账本并刷新工作区。携带凭据但不含工作区的编排式 claim/block 则由服务端同步执行（权威立即生效并签发 handoff 令牌），不经过 outbox 提案。
- **工具永不全量 add**：`sync` 等命令只把已登记的提案文件与规定路径加入提交，绝不使用全量 `git add -A` 或 `git add .`，避免无关或敏感文件被带进账本。

## 适用范围与威胁模型

- **适用范围**：单人 operator 的本地信任域——同一台机器上，operator 与若干 agent 协作完成开发任务。
- **身份与授权**：actor 不再只是约定别名——登录账号（本地 `users.json`）签发 CLI 令牌，handoff 能力令牌（sigt）以 HMAC 绑定任务与 actor，事件归因以凭据身份为准；签名伪造或篡改一律 401，越权访问一律 403。
- **不防恶意 operator 本机用户**：协议防的是误操作、凭据伪造与流程绕过；拿到 `~/.signet/` 与本机账号口令的本机用户等同于该用户本人。接入网络或多人共用环境前，需在此之上叠加传输加密与更细粒度权限。

## 环境自检

`python scripts/check_env.py` 输出本机环境摘要（JSON）：Python 版本、Git 版本、仓库根 `project.json` 中的 `project_id`（无该文件则为 `null`）。`--require-version X.Y` 可要求最低 Python 版本，不满足时以非零退出并给出 `reason`。

仓库根定位顺序：环境变量 `SIGNET_PLATFORM_REPO` 优先，否则取脚本所在仓库根（`scripts/` 的上一级）。
