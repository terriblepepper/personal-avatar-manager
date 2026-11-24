# PostgreSQL 部署、迁移与恢复

## 新部署

业务状态、系统 AIP 任务、资料向量和 EverOS 索引使用同一个 PostgreSQL 数据库。后端必须配置 `DATABASE_URL`。用 PostgreSQL 管理账户在 `psql` 中创建应用角色和数据库，按提示设置密码。

```sql
CREATE ROLE avatar_app LOGIN;
\password avatar_app
CREATE DATABASE avatar OWNER avatar_app;
```

在服务的私有 `.env` 中填写 `DATABASE_URL`。直接运行 CLI、Uvicorn 或迁移脚本时，须通过调用进程的环境显式传入。使用 psycopg 的原生 DSN，URL 中密码的特殊字符需要编码。示例使用已配置本机认证的数据库角色。

```bash
export DATABASE_URL='postgresql:///avatar?user=avatar_app'
```

源码部署的 `./service.sh start` 先启动自管 PostgreSQL 16（`data/pgsql`），再启动应用。`./service.sh stop` 同时停止自管数据库。外部 PostgreSQL 由其管理员维护。可选读缓存使用服务器项目根目录的 `redis/`，套接字示例为 `AVATAR_REDIS_SOCKET=redis/redis.sock`。Redis 不可用时管理员待审计数回源 PostgreSQL。

任务和通知继续在 PostgreSQL 提交后生效。应用启动时在当前 schema 安装事务内事件表和变更触发器。回滚同时撤销事件。转发器分批读取和清理事件，避免 PostgreSQL NOTIFY 给并发提交增加全局锁。Kafka 的任务唤醒和 Redis 的客户端刷新可通过数据库账本与快照恢复，队列或缓存数据可重新生成。认证的会话和 OIDC 查询复用一次连接池借用。事务和写锁保持原有一致性边界。

## 旧版迁移

使用 v0.1.0 或 v0.1.1 的部署，先在独立数据副本上用原版 1.1.0（`eda041e`）的 `Database(副本目录).initialize()` 离线升级 SQLite 结构，再把升级后的副本交给迁移器。

1. 确认聊天、索引和 Soul 构建结束，暂停所有应用写入和后台 worker，保持 PostgreSQL 在线。用户级 systemd 部署可执行 `systemctl --user stop personal-avatar`，其余部署停止对应应用进程。
2. 在项目外受保护的位置保存旧版代码标识、私有配置、主密钥、原件、EverOS 记忆文件和完整身份材料。冻结旧 `data` 的文件副本，保留 SQLite 主文件和 WAL、Chroma 元数据与全部 HNSW 段、EverOS 队列与 LanceDB 段；对于已经使用 PostgreSQL 的部署，按下文逻辑备份步骤保存同批主库。文件备份排除正在运行的 `data/pgsql`。保留一份原始快照用于恢复。
3. 如果旧版业务主库已使用 PostgreSQL，用它的隔离恢复副本先演练。如果业务主库仍是 `avatar.sqlite3`，先用 `scripts/migrate_sqlite_to_postgres.py SOURCE --backup BACKUP` 将数据库导入空 PostgreSQL schema。`SOURCE` 为冻结副本内的主库，`BACKUP` 为新建的私有备份文件。`--dry-run` 可先校验源表并检查目标为空。已有业务表的生产主库跳过这项旧主库导入。
4. 切到与目标版本对应的源码，在停写状态下导入本地存储。`DATABASE_URL` 必须指向已包含原账户和业务清单的目标库。`--snapshot` 指向冻结的 `data` 目录副本本身，账户归属由目标业务库确定。旧 Chroma 读取依赖通过 `legacy-migration` 安装。

```bash
uv sync --locked --extra legacy-migration
.venv/bin/python scripts/migrate_local_stores.py --snapshot /secure/avatar-backup/data
```

5. 核对导入报告中的系统 AIP 任务数、资料向量数、EverOS 行数和当前 RAG 发布引用数。工具在一个 PostgreSQL 事务内导入并核验引用。失败时保留快照，修正缺件、归属或配置后重新执行。索引迁移复用已有向量，不调用模型。
6. 隔离验证登录、资料授权与引用、Soul 记忆树、原件读取、AIP 旧回执和健康端点。正式切换也需在停写后对最终快照执行同样步骤。旧数据文件在确认迁移和恢复演练完成前应完整保留。

## 备份与恢复

在停写窗口设置私有文件权限，使用已配置 libpq 认证的账户导出。以下路径表示项目外的受保护目录，恢复目标须预先创建为空库。

```bash
umask 077
pg_dump --dbname=avatar --format=custom --file=/secure/avatar-backup/avatar.dump
pg_restore --exit-on-error --single-transaction --dbname=avatar_restore /secure/avatar-backup/avatar.dump
```

数据库转储、`.env`、`model-secrets.key`、用户原件、EverOS 记忆目录及证书、ACS、ATR 文件必须来自同一停写窗口。核对校验和并在隔离环境试恢复，再连接恢复后的库启动匹配版本。Redis 不保存持久业务数据。回退到使用旧存储格式的版本时，恢复迁移前配套的数据库和文件；切换后新增的记录需先留存并核对。详见[操作手册](操作手册.md)。

## 开发与测试隔离

先创建仅供测试的数据库并允许测试角色在其中创建 schema。`TEST_POSTGRES_URL` 须显式指定库名，`test` 必须是由开头、结尾、下划线或连字符分隔的完整词，如 `avatar_test`。

```bash
export TEST_POSTGRES_URL='postgresql:///avatar_test'
.venv/bin/python scripts/run_tests.py --jobs 4 unit -q
(cd frontend && npm run test:browser)
```

测试后端实例使用独立的 `avatar_test_<uuid>` schema，原件使用 `data/test/run-*` 私有工作区。正常结束时各自清理。异常中断后，下次运行检查所属角色、测试标记、数据库锁及活动连接，再回收已停用的 schema；文件工作区按运行标记清理。Vitest 和前端静态构建可独立运行。
