# EverOS 记忆桥

固定源码位于 `vendor/EverOS`（1.5.1，提交及来源见其 `UPSTREAM.json`），保留 Apache-2.0 LICENSE/NOTICE。平台、RAG 和 EverOS 共用根目录 `.venv`、`pyproject.toml` 与 `uv.lock`，推荐 Python 3.13：

```bash
uv sync --locked
```

`worker.py` 使用服务的 `sys.executable`，不另建虚拟环境、不启动独立 EverOS HTTP 服务。每次任务由隔离进程执行，LLM/Embedding 和 PostgreSQL 存储经标准输入输出请求父进程；父进程负责账户授权、HTTPS、模型 Key、调用费用及状态。worker 不接收 Key，也不接收数据库 DSN。

## 存储与增量

- 私有根目录按内部账户 ID 隔离，原生 owner 取该 ID 的 SHA256，查询同时固定 owner/app/project；每个手工修订或学习版本使用独立构建项目，不能搜索其他用户或待审项目。
- 原生 Episode/Profile 处理表达证据，Markdown、journal 和文件锁是可恢复来源；派生 queue 与向量存 PostgreSQL，向量列为 `real[]`，运行时不启动 SQLite、LanceDB、OME 或 APScheduler。
- 已处理证据摘要和成功向量随当前树继承，只处理新增证据；编辑/删除节点经独立修订及审核，失败不覆盖当前生效指针和增量游标。
- 自动学习只接收本人表达标签，不接收助手、访客、任务气泡、私人事实或凭据。成功仅更新当前 Soul 及旁边状态，不加入人工修订历史或消息。
- 同一项目的构建与树读取共用进程锁；不同项目提取模型期间可独立读树。账户共享锁只保护文件发布、恢复及索引操作。原生派生索引按嵌入配置共享，检索同时限定 owner、app 和项目。seed 在验证原件、画像和处理清单后发布；旧版本遗留的不完整拷贝只补齐可确认缺少的文件。
- 每次 episode/profile 写入与处理清单共用本地提交记录。工作进程在返回前保存与输入匹配的结果；手动恢复时会先回放已确认的结果。遇到结果不确定的模型响应，保留现有数据并停止。仅明确尚未建立连接的临时错误允许重试。

每次进程请求有请求数、输出和总时限约束，错误仅报告阶段/类别。备份需包括业务数据库、保险库主密钥、知识库原件与 `data/users/<内部ID>/everos/`（含隐藏的提交记录），源码不能替代用户记忆备份。

完整策略和命令见 [知识库与 Soul](../../docs/知识库与soul.md)及[验收说明](../../docs/验收说明.md)。开发测试使用模拟模型和私有临时存储，不修改 vendor 源码。
