# HTTP 接口概览

本文按模块列出平台对浏览器暴露的全部 HTTP 接口，帮助前端与外部集成快速定位入口。
机器可读的完整定义以运行时生成的 **OpenAPI** 为准：`/api/openapi.json`，交互文档 `/api/docs`。

## 会话提问与补答

个人生成任务等待补答时返回 `user_question`，含稳定的 `id`、`question` 和可选的 `options`。回答时提交该 `question_id`、当前暂停标识和回答内容。协作会话仅向待答用户提供问题，补答沿用该会话的恢复接口。普通聊天消息和问题回答最多 64 KiB，按 UTF-8 编码计量。

## 通用约定

- 所有业务接口挂在 `/api` 前缀下；`/`、`/admin`、`/platform-entry` 返回同一个单页入口 `frontend/dist/index.html`。
- Vue 构建资源位于 `/assets`；已知 Vue 子路由由 FastAPI 回退到构建后的 index。`/static` 提供构建内的协议头像与离线预览资源；未知 API 和资源返回 404，不回退为 HTML。
- 需要登录的接口依赖会话 Cookie（`AVATAR_SECURE_COOKIE=1` 时带 `Secure`）；管理接口额外校验角色。
- 中间件对每个请求做同源 `Origin` 校验、按路径的请求体大小限额，并下发 `Content-Security-Policy`、`X-Frame-Options`、`X-Content-Type-Options`。
- 长耗时聊天不走「请求-等待-响应」，而是 **202 + 轮询**：`POST /api/chat/generations` 立即返回受理，结果按 `request_id` 查询；`POST /api/chat/stream` 提供 SSE 流式投递。
- 分身对外的 ACPS RPC 不在本文范围，见 [小镇接入交接](小镇接入交接.md)。

## 路由模块一览

| 模块 | 前缀 | 接口数 | 主要职责 |
| --- | --- | --- | --- |
| `avatar/api/app.py` | — | 3 | 页面入口 |
| `avatar/api/routes.py` | `/api` | 71 | 账号、认证、资料、附件、会话、聊天、模型设置、管理后台 |
| `avatar/api/collaboration_routes.py` | `/api` | 41 | Agent 对话、意图、会话、任务、通知、用户技能 |
| `avatar/api/knowledge_routes.py` | `/api/me/knowledge` | 26 | Soul 资产、构建流程、记忆树、文件夹、持续学习 |
| `avatar/api/chat_file_routes.py` | `/api/conversations` | 11 | 会话内文件引用、工具授权 |
| `avatar/api/system_routes.py` | — | 6 | 系统 ACS/AIP 状态、系统注册、`/aip/rpc` |
| `avatar/api/diagram_routes.py` | `/api` | 8 | Mermaid 渲染、图形缓存与动画资料转存 |
| `avatar/api/review_routes.py` | `/api/admin/review-queue` | 6 | 审核队列明细 |
| `avatar/api/question_bank_routes.py` | `/api/admin/soul-question-bank` | 6 | Soul 题库与分类维护 |
| `avatar/api/town_routes.py` | `/api/town` | 4 | 小镇公开访问与访客任务 |
| `avatar/api/oidc_routes.py` | `/api/auth` | 3 | OIDC 配置、发起、回调 |
| `avatar/api/style_learning_routes.py` | `/api/conversations` | 3 | 会话风格学习开关与手动学习 |
| `avatar/api/entity_routes.py` | `/api/admin/agents` | 2 | 分身身份动作与作业刷新 |
| `avatar/api/migration_routes.py` | `/api/admin/identity-migration` | 3 | 身份迁移预览与提交 |
| `avatar/api/rag_routes.py` | `/api/me/file-index` | 3 | 资料索引重建与同步 |
| `avatar/api/resource_routes.py` | `/api/admin/rag-resources` | 2 | 全局自适应资源预算查询与更新（管理员） |
| `avatar/api/embedding_routes.py` | `/api/me/embedding-settings` | 3 | 嵌入模型选项 |

合计 **198** 个接口：OpenAPI 中的业务与协议操作 195 个（含 `/aip/rpc`），另有 3 个页面入口。

## 页面入口（`app.py`）

- `GET /` — 用户端
- `GET /admin` — 管理端
- `GET /platform-entry` — 平台落地入口

## 账号与认证（`routes.py`）

- `GET /api/health` — 健康检查
- `GET /api/help/{document}` — 读取随包文档
- `POST /api/auth/register`、`POST /api/auth/login`、`POST /api/auth/logout`
- `GET /api/auth/me` 返回当前会话及 `user.profile_name`。侧栏使用该字段显示资料草稿中的姓名。
- 登录会话中的 `user.onboarding_stage` 为 welcome、tour 或 done。`POST /api/me/onboarding/{stage}` 仅允许本人凭 CSRF 将进度推进到 tour 或 done，响应返回实际阶段。
- `POST /api/auth/recover-password`、`POST /api/auth/change-password`、`PUT /api/auth/contacts`
- `GET /api/auth/config`、`POST /api/auth/oidc/start`、`GET /api/auth/oidc/callback`（OIDC 三个端点在 `oidc_routes.py`）。登录配置中的 `version` 为当前服务启动版本，由 `AVATAR_VERSION` 或项目包版本确定。
- `GET /api/profile-fields` — 资料字段定义
- `GET /api/me`、`PUT /api/me/profile`
- `POST /api/me/submit` — 学生和教师均在此提交分身资料审核
- `GET /api/me/acs`、`POST /api/me/acs/preview` — 分身 ACS 描述与预览
- `GET /api/model-providers` — 可用模型平台预设
- `GET /api/avatars/{aic}/public` — 按 AIC 取公开头像

**平台绑定**

- `POST /api/platform/entry/preview`、`POST /api/platform/entry/session`
- `POST /api/platform/bind`、`GET /api/platform/binding`

**会话与聊天**

- `GET /api/conversations`、`POST /api/conversations`
- `PATCH /api/conversations/{conversation_id}`、`DELETE /api/conversations/{conversation_id}`
- `POST /api/conversations/{conversation_id}/clear`
- `POST /api/conversations/{conversation_id}/self`
- `GET /api/conversations/{conversation_id}/skills`、`PATCH /api/conversations/{conversation_id}/skills/{skill_id}`
- `GET /api/chat`、`POST /api/chat`（兼容入口）
- `POST /api/chat/stream` — SSE 流式
- `POST /api/chat/generations` — 提交持久生成任务，返回 202
- `GET /api/chat/generations/{request_id}` — 按请求查询状态与结果
- `GET /api/chat/pending?conversation_id=…` — 待办输入兼容查询

生成快照的 `state=awaiting_input` 表示该轮已停下，原任务等待用户。SSE 发出 `awaiting_input` 事件并关闭。客户端使用快照的 `conversation_id`、`agent_pause_id` 和新的 `request_id` 提交 `agent_control=continue` 或 `end`。继续时填写 `message`；同一次提交遇到网络中断时复用原请求号。其他客户端已处理的暂停标识会返回 409。

**资料与附件**

- `GET /api/me/material-folders` 的 `quota` 返回 `used_bytes` 与 `limit_bytes`，资料库按账户独立计入暂存和已保存原件。普通文件按剩余空间接收，原生动画每件最多 104857600 字节。
- `POST /api/me/material-folders`、`PATCH /api/me/material-folders`、`DELETE /api/me/material-folders`
- `POST /api/me/attachments`、`POST /api/me/attachments/batch`、`GET /api/me/attachments`
- `GET /api/me/attachments/{attachment_id}`、`PATCH /api/me/attachments/{attachment_id}`、`DELETE /api/me/attachments/{attachment_id}`
- `PATCH /api/me/attachments/{attachment_id}/name` 接收包含原扩展名的 `new_name`，保留扩展名大小写。修改成功后后台自动更新该文件索引。
- `POST /api/me/upload-batches/{batch_id}/complete`
- `GET /api/admin/attachments/{attachment_id}`、`GET /api/avatars/{aic}/attachments/{attachment_id}`

**模型设置**

- `GET /api/me/model-settings`、`PUT /api/me/model-settings`、`POST /api/me/model-options`

**通用记录**

- `GET /api/records/{collection}`、`POST /api/records/{collection}/delete`。按 ID 或全选删除创建任务仅限已完成记录；身份维护仅限已完成或失败记录，跨状态混选不会部分删除。

**管理后台**

- `GET /api/admin/overview` 返回账户、分身、待审和失败创建任务的计数。
- `GET /api/admin/settings` 返回平台 URL、注册模式及环境信息。
- `GET /api/admin/agents/operations?search=&offset=0` 每页返回最多 10 个分身及其最新适用身份任务，并附端口池状态。`GET /api/admin/agents/{user_id}/detail` 返回单个分身的完整管理视图。
- `GET /api/admin/dashboard` 保留完整管理概览，供已有客户端使用。
- `GET /api/admin/pending-count` 返回 `{"pending": 整数}`，合计资料和 Soul 待审记录，完整管理员会话可用。
  - `environment` 为本次启动的环境快照：实际注册适配器与模式、配置的通信地址/端口、已安装框架版本和真实手册路径；缺失元数据或手册返回空值。仅管理员可读取，通信配置不表示实时监听状态。
- `GET /api/admin/accounts`、`GET /api/admin/accounts/{user_id}/profile`、`PUT /api/admin/accounts/{user_id}/role`
- `POST /api/admin/reviews/{submission_id}`
- `POST /api/admin/jobs/{job_id}/retry`
- `POST /api/admin/agents/{user_id}/health`、`POST /api/admin/agents/{user_id}/renew`
- `PUT /api/admin/settings`

## 会话文件与工具（`chat_file_routes.py`，前缀 `/api/conversations`）

- `GET|PATCH /{conversation_id}/tools` — 会话工具授权。分身间会话的 Python 返回 `enabled: true`、`locked: true`。关闭请求返回 409。
- `GET /{conversation_id}/file-library` — 可选文件库
- `GET|POST /{conversation_id}/files`、`GET|PATCH|DELETE /{conversation_id}/files/{file_id}`
- `POST /{conversation_id}/files/reference`、`PATCH /{conversation_id}/files/{file_id}/reference`
- `POST /{conversation_id}/files/{file_id}/promote` — 会话文件转正式资料

## 风格学习（`style_learning_routes.py`，前缀 `/api/conversations`）

- `GET|PATCH /{identifier}/style-learning` — 状态中的 `can_enable` 表示当前有已审核生效的 Soul。开启时缺少生效 Soul 返回 409。
- `POST /{identifier}/style-learning/learn` — 立即学习需要已审核生效的 Soul。

## 分身协作（`collaboration_routes.py`，前缀 `/api`）

运行中向 `POST /agent-dialogues/{id}/resume` 提交 `steer: true` 和补充文字，当前发言结束后按已认证发送方入列。参与方可用 `POST /agent-dialogues/{id}/end` 直接结束，协议取消回执由后台处理。

**用户技能**

上传技能暂停期间，高权限登录也只能读取本人已安装记录。安装、启停、卸载和会话技能修改返回 403。

- `GET|POST /me/skills`、`POST /me/skills/source`、`POST /me/skills/batch`
- `GET|PATCH|DELETE /me/skills/{skill_id}`、`GET /me/skills/{skill_id}/files/{resource:path}`

**Agent 对话**

- `GET /agent-dialogues/targets`、`GET|POST /agent-dialogues`
- `GET /agent-dialogues/{identifier}`
- `POST /agent-dialogues/{identifier}/accept|retry-draft|cancel|confirm|resume|end`
- `DELETE /agent-dialogues/{identifier}`
- `GET|PUT /agent-dialogue-settings`

协作创建、补充输入和会话发消息允许客户端用原 `request_id` 重试。同一账户在同一入口作用域内重发相同载荷，仍指向首次提交的任务。修改内容、附件、技能或交付选项须使用新键。意图确认以意图 ID 绑定确认表单。临时上传与从资料库添加均返回本次文件的 `file_id`。表单选中的附件随本次消息或任务发送副本，后续消息不会自动读取这些附件。接收方可下载或转存到自己的资料库。重复请求仍校验当前可见权限。旧任务缺少可核对的回执时返回 409，可直接查看原任务。

分身遇到重复调用或需要补充时，协作详情返回等待状态和当前修订。允许补充的一方在 `resume` 的 JSON 正文中传 `expected_revision`，或在 `end` 的查询参数中传当前修订。过期操作返回 409。

**Agent 会话与任务**

- `GET /agent-sessions`、`GET /agent-sessions/{identifier}`、`DELETE /agent-sessions/{identifier}`
- `POST /agent-sessions/{identifier}/accept|messages|task-draft|clear`
- `POST /agent-sessions/delete-all`、`POST /agent-sessions/{identifier}/tasks/delete-all`
- `GET /agent-sessions?summary=true` 返回当前可见的会话名单和最近的分身消息预览。省略该参数保留完整响应。
- 会话和任务详情支持 `after` 消息游标（每页至多 10 条）。`task_offset`、`task_search` 控制同一响应中的 `task_page`。清空历史或隐藏任务后，`history_revision` 的变化通知客户端重新读取可见历史。
- `GET /agent-sessions/{identifier}/tasks`

**意图确认**

- `GET /agent-intents/{identifier}`、`GET /agent-intents/{identifier}/targets`
- `POST /agent-intents/{identifier}/confirm|choose|cancel`

**通知中心**

- `GET /notifications/unread-count` 只返回本人当前可见未读数（`unread`）。收件箱列表同时返回同口径的计数。
- `GET /notifications`、`POST /notifications/delete-all`、`POST /notifications/batch`
- `POST /notifications/{identifier}/{action}`

## 知识库与 Soul（`knowledge_routes.py`，前缀 `/api/me/knowledge`）

**资产**

- `GET /api/me/knowledge` 返回知识库总览。`quota.used_bytes` 仅统计 Soul 直接上传的素材，`quota.limit_bytes` 为 10737418240。
- `GET|POST /assets`、`POST /assets/batch`、`POST /assets/import`
- `GET|PATCH|DELETE /assets/{identifier}`、`PATCH /assets/{identifier}/name`
- `GET|POST|PATCH|DELETE /folders`

**Soul 构建流程**

- `POST /builds`、`GET /builds/{identifier}/memory-tree`
- `POST /builds/{identifier}/questionnaire|refine|answer|submit|cancel|retry`
- `DELETE /builds/{identifier}`、`POST /builds/delete-all`、`DELETE /active`

**记忆与学习**

- `GET /memory-tree`
- `POST /learning/retry`

## 图形与索引

- `POST /api/diagrams/image`、`POST /api/diagrams/icons`
- `POST /api/conversations/{conversation_id}/diagrams`、`POST /api/conversations/{conversation_id}/diagrams/lookup`、`POST /api/conversations/{conversation_id}/diagrams/{diagram_id}/promote`
- `POST /api/conversations/{conversation_id}/diagrams/promote` 接收 `source`、可选 `caption`、`visibility` 和 `new_name`，保存为 `.mmd` 源码资料。缓存 ID 转存接口也保存源码。会话附件转存接口同样接受可选的 `new_name`，文件名须保留原后缀。
- `POST /api/conversations/{conversation_id}/animations/lookup` 接收 `source`，按当前账户和规范化内容查找真实资料。返回 `attachment_id` 与 `already_exists`。未转存时 ID 为空。
- `POST /api/conversations/{conversation_id}/animations/promote` 接收 `source` 及可选的 `caption`、`visibility`、`folder`、`new_name`。成功时返回资料 ID 与是否复用了已有资料。超时后可用相同源码查询或重试。
- `create_html` 接收 `filename` 和 `html`，用于生成网页、2D 动画和 3D 动画。预览隔离运行，禁止联网，提供内置 Three.js。会话设置 `{"tools":{"animation":true}}` 将制作动画传达为本轮目标。
- `GET /api/me/attachments/{id}/preview-text` 在本人权限内从 DOCX 原件提取纯文本供小窗阅读。原始排版和图片保留在可下载的原件中。
- 本人会话通过 `find_material` 查询本人已上传的全部类型原件。`mode=directories` 分页列出逻辑目录，`mode=files` 可按 `folder` 限定子树，再按名称、模糊词或已发布的正文索引返回可发送的 file_id。无索引仍能列出原件。saved_at 为空表示尚未显式保存资料设置；本人发送、历史和任务回放统一按所有权及本次授权快照核验。同实例接收方通过 `find_public_material` 浏览可转交的已公开、已保存资料。模型可继续按关键词查询或翻页，以 `select_materials({file_ids,reply})` 选择本轮目录中一至五份原件并撰写正文，或通过 `clarify_material` 请求澄清。普通工具循环也可调用 `send_material`。新生成的 HTML 作品使用通用文件检索。历史动画仍可用 `find_animation` 和 `send_animation`。成功提交的资料附件带有 `origin: "material"` 与授权的 `download_url`，读取原件仍需本人身份。底部资料胶囊列出已提交的文件；正文可选用独占一行的 `[[attachment:ID]]` 在指定位置展示同一份附件。历史和重放重新核对有效原件。

- `GET /api/me/file-index` 可用 `source=profile` 或 `source=chat` 限定本人索引范围。默认包含两类，汇总区分部分索引（`partial`）和阻塞（`blocked`）。索引查询、重建与同步均要求完整会话。
- `POST /api/me/file-index/rebuild`、`POST /api/me/file-index/sync`（`rag_routes.py`）
- `GET|PUT /api/me/embedding-settings`、`POST /api/me/embedding-settings/options`（`embedding_routes.py`）

### 历史动画包 v1 示例

旧消息的动画围栏及已保存的 `.avanim` 原件使用以下格式，可继续预览、下载及转存。新动画通过 HTML 工具生成独立文件。

```json
{
  "format": "acps.animation", "version": 1,
  "title": "客户端发送请求", "summary": "请求从客户端到达服务端。", "tags": ["网络"],
  "canvas": {"width": 640, "height": 320, "background": "paper"},
  "durationMs": 1800, "loop": false,
  "accessibility": {"alt": "客户端沿网络连接向服务端发送请求。", "reducedMotion": "final-frame"},
  "nodes": [
    {"id": "client", "label": "客户端", "kind": "device", "x": 100, "y": 160},
    {"id": "server", "label": "服务端", "kind": "server", "x": 540, "y": 160}
  ],
  "edges": [{"id": "link", "from": "client", "to": "server", "kind": "network"}],
  "steps": [{
    "id": "request", "title": "发送请求", "detail": "客户端将请求沿连接发出。",
    "at": 0, "duration": 1800, "focus": ["client", "link", "server"],
    "packets": [{"from": "client", "to": "server", "edge": "link", "label": "请求"}]
  }]
}
```

## RAG 资源预算（`resource_routes.py`）

- `GET /api/admin/rag-resources` — 管理员读取当前进程的 `performance`（注册路由与内部阶段最近 256 个样本的 P50/P95/P99 毫秒及累计次数）、`rag_cache`（当前及峰值字节、语料查找与命中次数），以及 `policy`、动态工作槽 `limits`、当前占用 `active`、`cache_budget_bytes`、采样和 `enforcement`。
- `PUT /api/admin/rag-resources` — 提交完整资源策略，写入 `settings.rag_resource_policy`，进程内即时生效；字段与默认值见 [配置项说明](配置项说明.md#rag-自适应资源策略)。未知字段、非有限值和不合法上限返回 422。
- 均需管理员登录；PUT 同时遵守平台的 CSRF/同源校验。启用 systemd 硬限制时只允许操作当前进程所属的 `personal-avatar.service`；无法确认或应用时返回 503，内存新上限低于当前安全余量时返回 409。失败不保存新策略；硬限制结果不确定时在 `enforcement` 中明确标记，不宣称已生效。

## 系统身份与注册（`system_routes.py`）

- `GET /api/system/acs`、`GET /api/avatar-ontology/acs`
- `GET /api/system/aip/status`
- `GET /api/admin/system/registration`、`POST /api/admin/system/registration/refresh`
- `POST /aip/rpc` — 系统级 ACPS RPC 入口

## 分身实体与身份迁移（管理端）

- `POST /api/admin/agents/{user_id}/identity/{action}`、`POST /api/admin/agents/identity-jobs/{job_id}/refresh`
- `GET /api/admin/identity-migration`、`POST /api/admin/identity-migration/preview`、`POST /api/admin/identity-migration/commit`
- `GET /api/admin/review-queue/{kind}/{identifier}`、`GET /api/admin/review-queue/soul/{identifier}/assets/{asset_id}`
- `GET /api/admin/review-queue`、`POST /api/admin/review-queue/batch|delete|delete-all`
- `GET /api/admin/soul-question-bank`、`POST /api/admin/soul-question-bank/import|categories|questions`、`PUT /api/admin/soul-question-bank/categories/{identifier}`、`PUT /api/admin/soul-question-bank/questions/{identifier}`

## 小镇公开访问（`town_routes.py`，前缀 `/api/town`）

- `GET /me/avatar`
- `GET /avatars/{target}`、`GET /avatars/{target}/acs`
- `POST /avatars/{target}/tasks`

公开访客任务通过 `POST /api/town/avatars/{target}/tasks` 查询或控制。返回 `awaiting-input` 时，沿用原访客 Cookie、`task_id` 和 `session_id`，以新的 `command_id` 提交 `operation=continue` 和补充的 `message`；结束用 `operation=cancel`。重发同一操作复用同一命令号。

## 状态码约定

| 状态码 | 含义 |
| --- | --- |
| 202 | 生成任务已受理，需按 `request_id` 轮询结果 |
| 401 | 未登录或会话失效 |
| 403 | 角色或资源归属不足 |
| 404 | 资源不存在或不可见 |
| 409 | 冲突：同 `request_id` 内容不同、会话被占用、状态不允许该操作 |
| 413 | 请求体超过该路径限额 |
| 422 | 参数校验失败 |

## 相关文档

- [系统架构](系统架构.md) — 模块职责与身份模型
- [小镇接入交接](小镇接入交接.md) — ACPS RPC 与 Token 校验
- [操作手册](操作手册.md) — 部署、备份与故障排查

### 聊天推理强度

- 模型配置的可选字段 `reasoning_effort` 接受 `null` 或当前模型支持的等级。登录后可用 `GET /api/model-providers/effort-levels` 按 `provider`、`api_format`、`model` 查询等级。
- `GET /api/conversations/{id}/reasoning-effort` 返回模型标识 `model`、本会话选择 `selected`、账户默认值 `default` 和可选 `levels`。同路径 PATCH 接受 `{"reasoning_effort":"high"}`，传 `null` 恢复跟随账户设置。分身间会话使用本方的资源会话 ID。其他账户访问返回 404，处理中修改返回 409，等级不兼容返回 422。

### 小镇公开访问额度

`GET /api/me/town-access-limits` 读取当前账户的 `visitor_daily_limit` 和 `total_daily_limit`。同路径 PUT 提交这两个字段，接受非负整数或 `null`。`null` 表示不限，`0` 暂停公开问答。由已登录的分身所有者保存，按 UTC 日计算当日已受理请求。
