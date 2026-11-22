# 镜像 · 个人分身智能体管理系统

为用户托管个人智能体，支持资料检索、Soul 风格记忆、聊天、审核和跨分身协作。FastAPI 提供 Vue 3 页面与业务 API，业务、协议任务和资料/Soul 索引统一存储于 PostgreSQL。

在分身会话里可以直接问「用动画演示 TCP 三次握手」。勾选「制作动画」后，分身必须交付 HTML 动画。分身按用户意图选择维度，默认 2D，也可用 Three.js 绘制 3D 动画。通用网页由分身按需制作。作品支持气泡预览、小窗和转存。历史 `.avanim` 动画仍可播放。在本人会话中可以一次索要不同主题的资料，例如「发给我 ACPS 技术文档和智能体互联最新文章」。分身逐项查找和核验，可在同一条回复中交付最多五份原件。模型选择原件并编排正文，所有交付文件都能从气泡底部的资料胶囊打开。向同实例其他分身发送资料时，需确认收件人和附件。操作见[用户操作手册](docs/用户操作手册.md#在气泡中制作与播放动画)。

每条新消息由分身当前的模型选择所需工具。执行中可向用户提问、补选工具，也可以直接回答；有资料权限时可随时检索。长任务可分步完成，大型网页和动画支持从已保存的片段续写。展开气泡中的「执行过程」可查看调用和回执，在 Debug 模式下可进一步核对脱敏的输入输出摘录。

## 安装与运行

### 方式一：源码直接跑（开发 / 自托管）

```bash
# 需要 Node.js >=22.12、npm、uv、Python 3.12/3.13 和本机 PostgreSQL 16+
cd frontend
npm ci
npm run build
cd ..
uv sync --locked
# 仅新部署复制模板；已有部署保留自己的 .env
cp -n .env.example .env
# 按 docs/PostgreSQL运行.md 初始化数据库、设置 DATABASE_URL
./service.sh start
```

首次启动会在 `data/initial-admin.txt` 写入根管理员密码。
打开 `$AVATAR_PUBLIC_URL/admin` 登录后即可开始配置。用 `./service.sh stop` 停止。
前端构建产物由 FastAPI 提供。根目录 `redis/` 提供可选的待审计数缓存，服务不可用时接口回源 PostgreSQL。旧部署升级前先按[迁移与恢复说明](docs/PostgreSQL运行.md)冻结并导入历史本地存储。

开发时分别运行以下两个进程；环境变量由 shell 导出，直接 Uvicorn 不自动读取 `.env`：

```bash
# 终端一，项目根目录；使用自己的开发库
export DATABASE_URL='postgresql:///avatar?user=avatar_app'
export AVATAR_PUBLIC_URL=http://127.0.0.1:5173
.venv/bin/python -m uvicorn avatar.api.app:create_app --factory --host 127.0.0.1 --port 8000 --reload

# 终端二
cd frontend
npm run dev
```

Vite 本地提供前端与预览资源，默认代理 `/api` 到 `127.0.0.1:8000`；后端端口不同时，用 `AVATAR_API_TARGET=http://127.0.0.1:18080 npm run dev`。登录 Cookie、CSRF 和 SSE 保持同源。

### 方式二：交给 systemd（推荐长期运行）

```bash
./service.sh install    # 由 deploy/personal-avatar.service.in 生成用户级服务，启用后启动并检查健康状态
journalctl --user -u personal-avatar -f
```

### 方式三：作为 Wit 组件嵌入宿主进程

```python
from wit import WitAgent
from avatar.component import AvatarPlatformComponent, PLATFORM_APP

agent = WitAgent()
agent.install(AvatarPlatformComponent(serve_http=False), required=True)
```

宿主进程须提供 `DATABASE_URL`。启动 `agent` 后挂载 `PLATFORM_APP`；`serve_http=False` 时由组件管理应用 lifespan，宿主无需再次进入。

---

## 访问入口与端口

| 入口 | 地址 | 说明 |
| --- | --- | --- |
| 用户端 | `$AVATAR_PUBLIC_URL/` | 聊天、资料、知识库、技能、模型设置 |
| 管理端 | `$AVATAR_PUBLIC_URL/admin` | 审核、账户、实体、系统注册 |
| 健康检查 | `GET /api/health` | 启动脚本用它判断是否就绪 |
| 接口文档 | `/api/docs`、`/api/openapi.json` | FastAPI 自动生成 |
| 管理 RPC | `$AVATAR_SYSTEM_AIP_PUBLIC_URL` | 系统级 AIP，供外部智能体调用 |
| 个人分身 RPC | `https://<host>:10001..15000/aip/rpc` | 每个分身独占一个端口，路径固定 |

HTTP 中间件会做同源 Origin 校验、按路径的请求体大小限额，并下发 CSP / X-Frame-Options / nosniff 等安全头。

---

## 文档索引

| 文档 | 什么时候看 |
| --- | --- |
| [用户操作手册](docs/用户操作手册.md) | 第一次用平台：创建分身、上传资料、引入技能、配置模型 |
| [操作手册](docs/操作手册.md) | 部署与日常运维：Keycloak、证书端口、备份恢复、升级、故障排查 |
| [系统架构](docs/系统架构.md) | 想理解模块划分、身份模型、数据布局与数据边界 |
| [身份与协议](docs/身份与协议.md) | 分清 ACS / AIC / ATR / AIP，看注册签证流程与系统 AIP、个人 AIP 的启动条件 |
| [PostgreSQL 运行](docs/PostgreSQL运行.md) | 配置、旧版快照迁移、备份恢复与测试隔离 |
| [数据模型](docs/数据模型.md) | 业务逻辑表分组、状态机、数据目录与备份边界 |
| [API 概览](docs/API概览.md) | 前端或集成方查接口：16 个路由模块、188 个业务与协议端点、状态码约定 |
| [配置项说明](docs/配置项说明.md) | 每个环境变量的默认值与作用范围 |
| [术语表](docs/术语表.md) | 看不懂 AIP / ACS / AIC / ATR / Soul / RRF 时查这里 |
| [资料RAG与嵌入](docs/资料RAG与嵌入.md) | 检索链路、分块与召回、嵌入模型选型与校验 |
| [知识库与soul](docs/知识库与soul.md) | Soul 构建流程、六项表达特征、EverOS 风格记忆 |
| [打包与组件部署](docs/打包与组件部署.md) | 出交付包、离线安装、作为 WitComponent 嵌入宿主 |
| [小镇接入交接](docs/小镇接入交接.md) | 与 BUPT 小镇对接：RPC 地址、Token 校验、命令语义与限额 |
| [验收说明](docs/验收说明.md) | 提交前跑哪些命令、测试规模约束 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 分支命名、提交规范、PR 模板、评审与合并要求 |


## 第三方依赖与许可证

- 根目录的 `wit-3.2.0-*.whl`（按 OS 与架构选用）与 `acps_sdk-2.2.0-*.whl` 通过 `[tool.uv.sources]` 以本地路径安装——**不要**在 `.gitignore` 里用通配 `*.whl` 把它们排除。
- `vendor/EverOS/` 是内嵌的 [EverMind-AI/EverOS](https://github.com/EverMind-AI/EverOS) 1.5.1（Apache-2.0），固定 commit `5b0979b1b7f3a0cbe235756ff098515ec490753e`，作为 uv workspace 成员参与依赖解析；元数据见 `vendor/EverOS/UPSTREAM.json`。
- `frontend/public/static/vendor/` 存放离线 PDF.js、Mermaid、隔离页使用的 DOMPurify 及许可证。主页面通过 npm 引入 Marked 与 DOMPurify。
- `psutil`（BSD-3-Clause，PyPI 包）用于检索与索引的资源采样（CPU 亲和性、cgroup、内存）；它是普通 PyPI 依赖，交付 ZIP 的 `dependencies/` 携带 WIT、ACPS、EverOS 和 Redis 客户端 wheel。完全离线安装需自备其余依赖，包括 psutil。
- EverOS 桥接进程在 `tools/everos/worker.py`，与主程序共用同一个 `.venv`。

## Citation

> EverOS: local-first memory runtime for AI agents
>
> Version: 1.5.1
> URL: [https://github.com/EverMind-AI/EverOS](https://github.com/EverMind-AI/EverOS)
> License: Apache 2.0
