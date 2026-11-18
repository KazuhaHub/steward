# Steward：通用的 MCP 凭据代管服务（设计方案）

状态：草案，待 owner 决定。日期 2026-11-01。基于 `selfhost` 分支 `fef4080`（v1.14.3），
FastMCP 4.1.4，以及 `kazuha/feat/hub-session`、`kazuha/portal-official-hub` 两个分支的设计。

## 1. 要解决的问题

现在的 selfhost 层（`src/canvas_mcp/core/selfhost/`，约 3.3 万行，加上 `web/` 前端）直接长在
Canvas MCP 包里面。带来两个问题：

1. **上游同步很贵。** selfhost 通过 `core/credentials.py` 的 contextvar、`tool_gate` 中间件、
   课程缓存模式等进程内钩子接进 Canvas 工具。上游每改一次共享核心，都要逐条对照、改写测试
   （例如 v1.14.3 之后那次移植，测试里的 `cache.is_http_request_active` 要换成
   `uses_request_local_course_state`）。
2. **不能复用。** 账号、登录、给 MCP 客户端的 OAuth、管理后台、凭据托管、到期提醒，这些和
   Canvas 无关，但别的 MCP 项目（比如 Anteater）用不上。

目标：把这层做成一个**独立的 Steward**。任何 MCP server 都能放在它后面，用一个小的
connector 插件声明自己需要什么凭据，并且用统一的方式接入 MCPHub。

## 2. 现状：哪些通用，哪些是 Canvas 专用

按代码粗分（细表见附录 A）：

| 部分 | 规模 | 归属 |
|---|---|---|
| `authz/`：给 MCP 客户端的 OAuth 授权服务（DCR/CIMD、PKCE、consent、grants） | 约 3.9k 行 | 通用（但只支持一个 resource，见 4.6） |
| `login/`：本地、OIDC、GitHub、Google、Entra 登录，账号绑定，登录策略 | 约 5.3k 行 | 通用 |
| `db/`：引擎、sealed 列、迁移框架和迁移、repos | 约 6.8k 行（含迁移） | 大部分通用，表结构混合 |
| 账号、身份、邮件、维护、edge guard、限流、用量、运行时密钥 | 约 3.5k 行 | 通用 |
| `token_store`、`account_core`、`account_api`、`token_admin`、`request_context`、`tool_gate`、`tool_prefs`、`config_store`、`settings`、`app` | 约 1.3 万行 | 混合：通用机制加 Canvas 字段 |
| `canvas_expiry`、`schools` 的学校概念、`profile`、迁移 0005/0006 | 不到 1k 行 | Canvas 专用 |

真正 Canvas 专用的概念只有这些：

- **凭据字段**：实例 URL 加 token，上游用户 id 和名字（`canvas_credentials` 表）。
- **实例白名单**：“学校”目录，含 featured、搜索、allow_private（`canvas_schools`、`schools.py`）。
  其中的 SSRF 和 hostname 检查本身是通用的。
- **校验探测**：入驻时和 token 被拒时调 `GET /users/self`（`account_core.py`、`token_health.py`）。
- **到期发现**：`canvas_expiry.py`，加上 `expiry_source` 区分手填还是 Canvas 提供。
- **托管确认文案**：机制通用（`custody.py`），文案写的是 Canvas。
- **写工具的分类和分组**：`tool_policy.TOOL_EFFECTS`、`tool_prefs.py`。

selfhost 和外部的耦合点也不多（附录 B）：`server.py` 启动入口；`tools/discovery.py` 用
`tool_prefs.decide` 过滤搜索结果；`core/client.py` 借用 `selfhost.schools` 做上传 SSRF 检查。
其余全部通过 `core/credentials.py` 的 contextvar 交互。

## 3. 和 MCPHub 怎么分工

已经定下的部署方向（`kazuha/portal-official-hub` 分支）：官方 MCPHub 不改，Canvas Portal（即现在的 selfhost 部署）
独立部署；Hub 通过 `streamable-http` 加 `oauth.identityMode: personal` 连 Portal，只保存每个
用户授权给 Hub 的 grant。`kazuha/feat/hub-session` 分支又让 Portal 可以直接用 MCPHub 会话登录。
Steward 就是把这个 Portal 通用化。

模板沿用这个分工，不重复做 Hub 已有的东西：

| 职责 | MCPHub | Steward |
|---|---|---|
| 面向 MCP 客户端的入口、聚合多个 server、路由、用户 bearer key | ✓ | — |
| 用户登录 | ✓（自己的账号） | 可以复用 Hub 会话登录（hub-session），也保留本地和 OIDC 登录 |
| 每个用户的**上游**凭据（Canvas token 等）：托管、加密、校验、到期提醒 | 不负责（Steward connector 不配 `credentialTemplate`） | ✓ |
| 准入：审批、规则、学校策略 | — | ✓ |
| 每个用户的写工具开关 | — | ✓ |
| 给 Hub 的 personal OAuth | 作为客户端 | 作为授权服务 |

所以 Steward **不做聚合**。一个 Steward 部署可以挂多个 connector，但每个 connector 在 Hub 里是一个
独立的 server 条目。

## 4. 方案：独立 Steward，以代理方式接后端 MCP

### 4.1 选型

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **A. 代理网关（推荐）** | Steward 是独立服务，用 FastMCP `ProxyProvider` 把 `/mcp` 转发给后端 MCP server，每个请求注入这个用户的上游凭据 | 后端可以是任何语言；Canvas 后端可以几乎原样跟上游走；现有的工具开关中间件可以直接挂在代理上 | 有几项进程内能力要换成基于协议信号的做法（见 4.4）；多一跳网络 |
| B. Python 库 | 各项目 `import steward`，挂到自己的 FastMCP app 上 | 保留全部进程内能力 | 只有 Python/FastMCP 能用；上游同步的问题照旧 |
| C. 只用 MCPHub 的 `credentialTemplate` | 不要 Steward，用户在 Hub 里填 token | 零开发 | 没有校验、到期提醒、托管确认、学校策略、写工具开关；`kazuha/portal-official-hub` 已经因此决定由 Portal 托管 token |

推荐 A。B 可以作为 connector 的一种可选接法保留：对 Canvas 这种 Python 后端，迁移期间
Steward 继续进程内挂载，这样过渡期不丢功能（见第 6 节第 2、3 步）。

### 4.2 结构

```
MCP 客户端 ──► MCPHub ──(personal OAuth)──► Steward ──(注入凭据请求头)──► 后端 MCP server ──► 上游 API
                                              │
                                              ├─ 账号 / 登录 / 管理后台（web）
                                              ├─ connector 插件：字段、校验、到期、请求头映射
                                              └─ 加密凭据库、到期提醒、审计、用量
```

- 第一阶段一个 Steward 部署只挂**一个** connector，继续用 `/mcp` 和 `Canvas.Access`，这样 Hub
  里已有的条目和 grant 不受影响。多 connector（`/mcp/<connector>`）要先改 OAuth，见 4.6。
- 请求进来：bearer token → 账号 → 这个 connector 的凭据（解密）→ 托管确认、generation、
  准入检查 → `client_factory` 建一个带请求头的后端 `Client` → 转发。
- 任何检查失败，都由 Steward 直接返回 MCP 错误，不转发。现在 `request_context.py` 里
  “没入驻、学校不再提供、token 被拒”这些消息就是这么处理的，原样保留。

**代理的硬性要求**（评审发现 FastMCP 4.1.4 的默认行为不安全）：

- **不转发客户端的请求头。** `ProxyClient` 默认 `forward_incoming_headers=True`，而且会带上
  `Authorization`（`fastmcp/server/providers/proxy.py:1721`、`fastmcp/client/dependencies.py:32`）。
  不关掉的话，后端会拿到用户的 Steward access token，可以拿去重放；客户端自己伪造的
  `X-Canvas-URL`、`X-Steward-Principal`、`X-MCP-Access-Key` 也会被透传。Steward 必须设置
  `forward_incoming_headers=False`，发给后端的请求头**只能**由 connector 的 `inject` 加上
  Steward 自己的密钥头组成。
- **每次调用都用新的 `Client`。** `client_factory` 每次调用都会执行（`proxy.py:346-351`），
  能看到当前请求的 contextvar。绝不能把一个已连接的 client 交给代理复用
  （`proxy.py:1371-1385`），否则会跨用户共享会话。代价是每次调用都要和后端做一次 initialize，
  性能要在原型里测。
- **后端的连接错误要转成 `SOURCE_UNAVAILABLE`**，不能把原文（可能含内部 URL，
  `proxy.py:147-159`）传给客户端。
- **没入驻的用户也要能列出工具。** 现在的 gate 故意不拦 list 和 initialize，好让用户能连上并
  看到入驻提示（`tool_gate.py:1-10`）。代理模式下 list 要先连后端，而后端没有 token 会直接
  401（`server.py:452-454`）。所以 Steward 要用 connector 提供的工具目录快照（Canvas 已有
  `tools/TOOL_MANIFEST.json`）应答这些用户，或者给后端加一个不需要 token 的 list 模式。

### 4.3 Connector 插件接口

两种写法：

- **声明式（YAML/JSON）**：覆盖大多数“API key 或 token”类服务，比如 Anteater。
- **Python 插件**：需要复杂逻辑的用这个，比如 Canvas 的学校目录和到期发现。

两种写法要表达的内容相同：

| 能力 | 说明 | Canvas 的实现 |
|---|---|---|
| `fields` | 凭据字段的 schema（名字、类型、是否保密、标签、i18n）。前端据此生成表单 | `instance_url`、`token` |
| `instances`（可选） | 实例白名单或目录，带 SSRF 检查。没有的 connector 用固定上游 | 现在的学校目录 |
| `validate(fields)` | 入驻时调用，返回上游身份（id、显示名）和可选的到期提示 | `GET /users/self`，加 token 到期发现 |
| `same_identity(old, new)` | 换 token 时证明还是同一个上游用户 | 比较 `canvas_user_id` |
| `inject(fields)` | 生成发给后端的请求头或环境变量 | `X-Canvas-Token`、`X-Canvas-URL` |
| `dead_credential_signal` | 怎么从后端结果判断凭据已失效 | 工具错误码 `CREDENTIAL_INVALID`（严格解析，见 4.4） |
| `tool_effects` | 工具读写分类表，由 connector 维护，**不信任后端的 `readOnlyHint`**。表里没有的工具一律按写处理，而且用户不能自己打开 | `TOOL_EFFECTS` 表 |
| `tool_catalog` | 工具目录快照，用来应答还没入驻的用户的 `tools/list` | `tools/TOOL_MANIFEST.json` |
| `disabled_tools` | 运营者或 profile 禁用的工具，由 Steward 在 list 和 call 时拦截 | pilot profile 禁用的文件工具 |
| `backend_requirements` | 后端必须满足的配置（例如 `--role student`、禁止代码执行、禁止下载 Canvas 以外的文件），Steward 启动时校验 | pilot 的限制 |
| `custody` | 托管确认的文案和版本号 | 现在的 Canvas 文案 |
| `ui_slots`（可选） | 前端扩展点，用于特殊页面 | 学校选择器 |

声明式写法的样子（示意，不是最终格式）：

```yaml
id: anteater
display_name: Anteater
backend: { transport: streamable-http, url: http://anteater:8080/mcp }
fields:
  - { name: api_key, secret: true, label: { en: "API key", zh: "API 密钥" } }
validate:
  request: { method: GET, url: "https://api.example.com/v1/me", auth: "Bearer {api_key}" }
  identity: { id: "$.id", name: "$.name" }
inject:
  headers: { X-Anteater-Key: "{api_key}" }
custody: { version: 1, text_ref: anteater-custody }
```

### 4.4 改成代理以后，进程内能力怎么处理

| 现有能力 | 代理模式下怎么做 | 损失 |
|---|---|---|
| token 被拒时的健康监控（`client.py` → `confirm_dead_token`） | Steward 检查 `tools/call` 结果：只有 `isError=true`、而且整个结果文本恰好是 `{"error": "CREDENTIAL_INVALID", ...}` 这个 JSON 时才算数，并且只标记这次调用用的那个 generation（现在 `core/token_health.py:156-161` 也是这样带上 token 版本和 generation 的）。正常结果当作 `note_success` | 不能宽松匹配：工具结果里会有 Canvas 内容（比如帖子正文），学生可以在里面写一段假的错误码，把老师的 token 标成失效 |
| 写工具开关（`SelfhostCredentialGate`） | 同一个 FastMCP 中间件挂在代理 server 上，`on_list_tools` 过滤，`on_call_tool` 拒绝 | 中间件确实会作用在代理的工具上。调用时的检查仍然是安全边界 |
| 运营者禁用的工具（`apply_disabled_tools`、`apply_tool_policy`） | 现在这两个只从 Steward 自己的注册表里删工具（`app.py:441-457`、`tool_policy.py:326`），对代理过来的工具无效。改成在 gate 的 list 和 call 里拦截 | pilot 禁用的是**读**工具（`profile.py:8`），gate 本来会放行，必须单独处理 |
| 写工具的上限 | Steward 的开关之外，后端的 `ALLOWED_WRITE_TOOLS`（默认只读，不认识的名字直接拒绝，`tool_policy.py:227-255`）仍然是第二道上限。Steward 允许的写工具必须是后端允许的子集，启动时校验 | 两边配置可能不一致，需要启动校验 |
| pilot 的进程内保证 | `ALLOW_OFF_CANVAS_DOWNLOADS=False`（`app.py:501`）和禁止代码执行（`app.py:132-135`）现在是在进程内设置的。代理模式下由后端自己的配置保证，并由 `backend_requirements` 在启动时校验 | 上游没有关掉“下载 Canvas 以外的文件”的配置项，要加（见 4.5） |
| `search_canvas_tools` 的结果过滤 | 代理看不到这个工具内部的结果。可以把写工具开关作为请求头传给后端，或者对这个工具单独处理 | 不处理的话只会泄露工具名，调用照样会被拦住 |
| 课程缓存 `per_principal` | 请求头模式下后端的课程缓存是 `request_local` | `request_local` 本来就是默认值，pilot profile 还强制要求它。只有手动开了 `per_principal` 的部署会多一些 `/courses` 请求 |
| 匿名化的化名映射 | 没有 principal 时，后端把映射放进进程级 LRU，按 token 的 hash 区分（`anonymization.py:50-65`、`credentials.py:223-232`、`:421-424`）。Steward 删除 token 时通知不到后端，映射会一直留到被挤出 LRU | 要给后端加 TTL，或者加一个由 Steward 触发的清除接口（见 4.5） |
| 审计里的用户身份（`core/audit.py`） | Steward 自己记工具调用审计（账号、connector、工具名、结果码） | 后端日志里没有用户。需要的话加一个 `X-Steward-Principal` 请求头，**每次都由 Steward 设置**（不转发客户端的），后端只接受来自 Steward 的连接 |
| 凭据 generation 失效 | Steward 每个请求都现查 generation，后端不缓存凭据 | 正在执行的那一个调用不会被中途撤销，影响很小 |
| 写操作的预览和确认 | 确认令牌用的是每个进程自己的密钥（`write_confirmation.py:90`），请求头模式下只绑定 Canvas token（`:143`） | 后端只能跑**一个副本**，或者多个副本共享密钥；后端重启后，之前的预览不能再确认 |
| 上传 SSRF 检查（`client.py` 借用 `selfhost.schools`） | 把 hostname/SSRF 工具移到 `core/`，两边各用一份 | 无 |
| 课程标签 | 本来就在后端，按请求处理 | 无 |

### 4.5 Canvas 后端的前提：每个请求要能带自己的实例 URL

上游 canvas-mcp 的请求头模式（`CanvasCredentialMiddleware`，`server.py:313-460`）只读
`X-Canvas-Token`，实例 URL 是启动时用 `CANVAS_API_URL` 固定的，`X-Canvas-URL` 会被忽略。
Steward 的用户分属不同学校。再加上 4.4 里列出的几项，后端需要这些改动：

1. **每个请求的实例 URL**：新增 `X-Canvas-URL`，只在开启“受信任前端”模式时接受。学校白名单
   放在 Steward 的数据库里，由 Steward 决定；后端不再维护一份静态白名单（否则新增学校、
   `allow_private` 主机都要重新部署后端，两边会不一致），但仍然自己做公网主机和 SSRF 检查。
2. **只信任 Steward**：后端只接受带 Steward 密钥头（现有的 `X-MCP-Access-Key`）并且来自内网的连接。
   上面这些请求头只在这种连接上生效。
3. **不需要 token 的 `tools/list`**，或者由 Steward 用工具目录快照代答（二选一）。
4. **关掉“下载 Canvas 以外的文件”的配置项**，给 pilot 用。
5. **化名映射加 TTL，或者加清除接口。**
6. **可选：`X-Steward-Principal`**，用于后端审计。

第 1、2、4、5 项和 selfhost 无关，**可以提给上游**。上游接受的话，Canvas 后端就能直接用上游
发布的版本；不接受的话，fork 要维护这一组补丁。这一组比现在的整层 selfhost 小得多，但不是
“一个小补丁”，要如实评估。

### 4.6 OAuth：现在只支持一个 resource

现在的授权服务只为一个 MCP 资源设计：只有一个 `mcp_path`、audience 和 scope
（`authz/server.py:205-241` 里有 `set_mcp_path` 的断言；`authz/runtime.py:100-106`），
access token 的 `aud` 精确比对（`authz/tokens.py:236`）。如果直接加 `/mcp/<connector>`，一个
connector 的 token 在别的 connector 上也能用。

要支持一个 Steward 挂多个 connector，需要：

- 每个 connector 有自己的 audience 和 scope；
- 按路径发布 RFC 9728 的 protected resource metadata；
- 授权请求里的 RFC 8707 `resource` 参数绑定到 grant，grant 按 resource 存；
- 现有 grant 迁移成默认 connector 的 grant。

这是一块独立的工作，放在第 6 节第 5 步，在此之前每个部署只挂一个 connector。

### 4.7 保住现有的 MCPHub grant

Hub 里已经授权过的用户，不应该因为这次重构都要重新 Connect。需要同时保持：

- 签发 JWT 的密钥：它是从 `CANVAS_TOKEN_KEYS` 派生的（`authz/tokens.py:4-9`），拆仓库时不能改
  环境变量名或 keyring，最多加一个新名字作为别名；
- issuer（`PUBLIC_BASE_URL/`）、audience 和 `/mcp` 路径；
- 数据库里的 refresh token。

另外，现在还有一条 **stdio 路径**：MCPHub 用 `credentialTemplate` 给每个用户启动 Canvas 子进程
（`deploy/mcphub/README.md:3-10`）。从这条路径切到 Steward，`identityMode` 会变，用户之前在 Hub
里的绑定会失效，每个人都要重新 Connect 一次（`PORTAL-SERVER-PROMPT.md`）。这个切换要提前通知，
按 PORTAL-SERVER-PROMPT.md 的做法先并行试运行再切。hub-session 登录用的 nginx location 指向
Steward 容器的端口，Steward 的部署方式变了也要同步改。

## 5. 数据模型的变化

| 现在 | 之后 |
|---|---|
| `canvas_credentials` | `upstream_credentials`：`connector_id`、`instance_id`（可空）、`fields_sealed`（插件定义的字段，整体加密）、`upstream_user_id`、`upstream_user_name`、托管列、到期列、`generation` |
| `canvas_schools` | `connector_instances`：`connector_id`、`host`、目录属性 |
| `account_write_tools` | 加 `connector_id` |
| `canvas_oauth_apps`（保留未用） | `connector_oauth_apps` |
| 其他账号、登录、OAuth、邮件、审计表 | 不变 |

加密要特别小心。现在的 AAD 绑定了 principal、host、kind（pat/oauth）和 key id，防止密文在行
之间挪动（`token_store.py:761-773`、`db/schema.py:481-482`）。迁移时：

- 新表**保留 `kind` 列**，AAD 继续绑定它。
- `SealedColumn.aad` 不接受空值（`db/sealed.py:46-52`），所以可空的 `instance_id` 不能直接进
  AAD。没有实例的 connector 用一个固定的哨兵值，例如 `-`。
- AAD 在两处构造：`credential_aad` 和 `SEALED` 注册表，必须一起改，并用测试保证两边一致。
- 现在每张表只有一个 AAD 前缀，没有按行的版本号，所以新旧格式的行**不能共存**。迁移在一个事务里
  用同一个 keyring 把所有行解密、按新格式（加上 `connector_id`）重新加密，保留 `key_id`。
- 回滚只能靠备份：迁移前强制备份数据库和 keyring（沿用 OPERATOR.md 的规则）；同时提高数据库的
  兼容版本下限，让旧镜像拒绝在新库上启动。
- `SEALED` 注册表测试继续守住“每个加密列都在注册表里”。

## 6. 分步迁移：新开 `steward` 仓库

决定：直接在 org 下新开 `steward` 仓库，不先在 canvas-mcp 里重构。理由是 `steward-core` 根本
import 不到 `canvas_mcp`，接口边界由仓库边界强制，不会写成“名义上通用”。原来“先在仓库内划接口”
那一步因此省掉了。

### 6.1 前提

1. **冻结旧 selfhost。** canvas-mcp 里的 selfhost 层从此只修安全问题和严重 bug，不加功能。
   过渡期修的东西，在 Steward 里也要修一遍；过渡期越短越好。
2. **带着历史迁移。** 在 canvas-mcp 的一个**新克隆**上，用 `git filter-repo`（或
   `git subtree split`）抽出通用模块和它们的测试，作为 `steward` 的初始历史。这不改 canvas-mcp
   已有的历史，也不 force push。安全相关代码的来历和 blame 都保留。
3. **直接读旧数据。** Steward 上线时对现有数据库做一次迁移（第 5 节），用同一个 keyring，
   不建新库、不要求用户重新入驻；JWT 签名密钥、issuer、audience 和 `/mcp` 路径保持不变（4.7），
   MCPHub 里已有的 grant 继续有效。
4. **对齐清单是上线门槛**（6.3）。全部通过之前，生产继续跑旧 selfhost。

### 6.2 步骤

1. **建仓库，迁入通用代码。**
   - 抽出 `authz/`、`login/`、`db/`（引擎、sealed、迁移框架）、账号、身份、邮件、维护、
     edge guard、限流、用量、运行时密钥，以及 `web/` 里的通用页面，连同测试。
   - 去掉所有 `canvas_mcp` import，换成 connector 接口（4.3）。CI 加一条检查：`steward-core`
     里不允许出现 `canvas`。
   - 完成标准：迁入的测试全部通过；不依赖 canvas-mcp 也能启动（没有 connector 时只有账号和登录）。
2. **写 Canvas connector。** `connectors/canvas`：字段、学校目录、`/users/self` 探测、到期发现、
   托管文案、工具分类表、工具目录快照、`backend_requirements`。混合模块（`token_store`、
   `account_core`、`account_api`、`request_context`、`tool_gate`、`tool_prefs` 等）在这一步拆开：
   机制留在 core，Canvas 的部分进 connector。
3. **数据迁移。** 一个 Alembic 迁移，从 canvas-mcp selfhost 的最新 schema 升到通用 schema
   （第 5 节）。完成标准：拿 v1.14.x 的真实备份（脱敏副本）升级后，两个测试用户的凭据、写工具
   开关和 Hub grant 都还在；备份可以恢复。
4. **代理模式和 Canvas 后端补丁。**
   - Steward 用 `ProxyProvider` 把 `/mcp` 转发给 Canvas 后端（4.2 的硬性要求）。
   - canvas-mcp 加上 4.5 列出的后端改动，同时提给上游。
5. **按对齐清单验收，然后切生产。** 先并行试运行，再按 PORTAL-SERVER-PROMPT.md 的方式切换；
   旧 selfhost 的镜像和数据备份保留，作为回滚路径。切换后 canvas-mcp 删除 selfhost 层，缩回
   “上游 + 4.5 那组补丁”。
6. **多 connector 和第二个 connector。**
   - 先做 4.6 的 OAuth 多 resource 支持。
   - 再用声明式写法接入 Anteater，同一个 Steward 部署、同一个账号同时用 Canvas 和 Anteater。
   - 完成标准：Anteater 不需要改 `steward-core` 的代码；一个 connector 的 token 在另一个上被拒；
     现有的 Canvas grant 不受影响。
7. **模板化。**
   - 提供 `steward new-connector` 脚手架。
   - 自动生成 MCPHub 的 `mcp_settings` 片段（`streamable-http` 加 `identityMode: personal`）和
     market manifest。
   - 写一份“三步接入一个新 MCP”的文档。

第 1、2 步工作量最大，主要是把混合模块拆干净；第 4、5 步风险最高，因为要改请求路径并切生产。

### 6.3 对齐清单（上线门槛）

全程只用模拟的 Canvas API，不碰真实 Canvas 写操作。

- 两个用户分别连不同学校，各自的 `get_my_profile` 正确；
- 未入驻的用户能列出工具、看到入驻提示，调用时被拒；
- 写工具开关和运营者禁用的工具在 list 和 call 时都生效；pilot profile 的限制都在；
- 后端收到的请求头里没有用户的 Steward token，也没有客户端伪造的头；
- 被撤销的 token 能被标记为失效，工具结果里伪造的错误码不会；
- 托管确认：没确认不能入驻，版本更新后要求重新确认；
- 到期提醒邮件照常发送，邮件里不出现 token；
- 本地登录、OIDC 登录和 hub-session 登录都能用，账号关联规则不变；
- Hub 里已有的 grant 不用重新授权；
- 从旧数据库迁移后，凭据、偏好设置和审计记录都在；
- 写操作的预览和确认能走通。

### 6.4 仓库结构

```
steward/
  steward-core/        Python：账号、登录、OAuth、凭据库、gate、代理、提醒、管理 API
  steward-web/         前端：通用页面；凭据表单由 fields 生成，特殊页面走 ui_slots
  connectors/canvas/   Canvas connector（Python）
  connectors/examples/ 声明式 connector 示例
  deploy/              compose、Caddy、nginx、MCPHub 配置片段
```

包名和镜像名用 `mcp-steward`（PyPI 上不带前缀的同名已被占）。

## 7. 必须保留的安全约束

拆分后由 `steward-core` 的测试守住：

- **托管确认**：没有当前版本的确认就不入驻；每个 MCP 请求都检查；版本过期就不解密
  （V1-DESIGN.md D10）。
- **加密存储**：AES-256-GCM，每行独立 nonce 和 `key_id`，AAD 绑定行身份，`SEALED` 注册表测试。
- **generation 不变式**：凭据的 generation 必须等于账号的 generation，不一致就返回 503。
- **失败时关闭**：配置快照重建失败返回 503；调用时的工具检查是安全边界，列表过滤只是方便；
  任何 MCP 工具都不能读写偏好设置。
- **默认 request-local 课程状态**：pilot profile 强制使用。
- **账号登录规则**（ACCOUNT-LOGIN-DESIGN.md）：
  - 不静默合并账号，邮箱相同也不自动关联。
  - 收紧策略时作废会话和 grant。
  - 改名需要最近登录过。
- **提醒邮件里不出现 token。**
- **后端只信任 Steward**：共享密钥加网络隔离；客户端的请求头一律不转发，发给后端的请求头全部由
  Steward 生成；学校策略由 Steward 决定，后端仍然自己做 SSRF 检查。
- **凭据失效只认严格格式的错误结果**，并且只作用于这次调用用的 generation。
- **签发 JWT 的密钥、issuer 和 audience 在重构中保持不变**，现有的 MCPHub grant 继续有效。

## 8. 待决定的问题

1. **方案 A 的方向**，以及第 4.5 节的补丁要不要提给上游（需要和上游 maintainer 沟通）。
2. **一个 Steward 部署挂多个 connector**，还是每个 MCP 一个 Steward。本方案推荐前者：同一个账号、
   一套登录和提醒。但这要先做 4.6 的 OAuth 改造，所以第 1 到 4 步只有 Canvas 一个 connector，
   数据模型从一开始就按多个设计。
3. **只支持 stdio 的 MCP server**（比如只能用环境变量传凭据的）：Steward 要不要像 MCPHub 那样
   按用户启动子进程。建议先不做，需要时用 MCPHub 的 `credentialTemplate`。
4. **已定**：仓库 [KazuhaHub/steward](https://github.com/KazuhaHub/steward)，公开，2026-11-01 建为空仓库。
   许可证沿用 MIT：代码来自 canvas-mcp，LICENSE 保留 Vishal Sachdev 的版权声明，再加上本项目的。
   org 里已有 Go 写的 `authcore`（只提供机制、不含账号模型的 OIDC、SAML、passkey、限流等包）。
   Steward 继续用 Python，因为要复用现有约 3 万行经过测试的代码；不复用 authcore，
   但遵循它“机制和策略分开”的原则。
5. **FastMCP 代理的细节要先做原型验证**：
   - `ProxyProvider` 的工具列表缓存是全局的（`cache_ttl` 默认 300 秒），只缓存元数据，不缓存
     client，所以不会泄露凭据。但如果后端的工具列表因人而异（例如按角色），要设成 0，代价是
     每次调用多一次 list。
   - 每次调用都会新建后端会话并 initialize，要测延迟。
   - resources、prompts、sampling、elicitation 和进度通知的透传。
6. **后端能不能多副本**：写操作的预览和确认要求同一个进程（4.4），要么只跑一个副本，要么给
   后端加共享密钥。

## 附录 A：模块归属（摘要）

- **通用**：`authz/*`、`login/*`、`db/engine.py`、`db/sealed.py`、`db/authz_repos.py`、
  迁移框架、`accounts.py`、`identity.py`、`account_spa.py`、`mail.py`、`maintenance.py`
  （学校清理除外）、`edge_guard.py`、`limits.py`、`usage.py`、`runtime_secrets.py`、
  `site_url.py`、`server_settings.py`。
- **混合**：`token_store.py`、`account_api.py`、`account_core.py`、`account_ops.py`、
  `token_admin.py`、`token_health.py`、`token_reminders.py`、`request_context.py`、
  `tool_gate.py`、`tool_prefs.py`、`config_store.py`、`config_snapshot.py`、`settings.py`、
  `app.py`、`schools.py`、`custody.py`、`db/schema.py`、`db/repos.py`。
- **Canvas 专用**：`canvas_expiry.py`、`profile.py`、迁移 0005/0006。
- **前端**：登录、setup、consent、已连接应用、活动、管理后台的账号/设置/审计都是通用的。
  `TokenView`、`components/token/*`、`CustodyNotice`、`AdminEnrollmentsView`、
  `EnrollmentExpiry` 是 Canvas 专用的，要改成由 `fields` 生成的表单加上 `ui_slots`。

## 附录 B：现有耦合点

- `server.py:41-47`、`server.py:703-813`：selfhost 的启动入口。
- `tools/discovery.py:18`、`:70-76`：用 `tool_prefs.decide` 过滤工具搜索结果。
- `core/client.py:753`：上传 SSRF 检查借用 `selfhost.schools`。
- `selfhost/request_context.py:12-29`：设置 credentials、principal、token 状态和工具偏好的
  contextvar。
- `selfhost/token_health.py:42`、`:288`：借用核心的 `_says_token_is_dead` 和
  `_canvas_auth_headers`。
- `selfhost/app.py:499-501`：根据 profile 设置 `course_files.ALLOW_OFF_CANVAS_DOWNLOADS`。
- 用到 FastMCP 私有接口的地方：`selfhost/app.py:441-457`（`apply_disabled_tools`）和
  `core/tool_policy.py:326`（`apply_tool_policy`）调用 `local_provider.remove_tool`；
  `selfhost/authz/fastmcp_compat.py:105` 调用 `provider._get_resource_url`。
