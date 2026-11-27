# PostgreSQL 部署、迁移与恢复

## 新部署

业务状态、系统 AIP 任务、资料向量和 EverOS 索引使用同一个 PostgreSQL 数据库。后端必须配置 `DATABASE_URL`。用 PostgreSQL 管理账户在 `psql` 中创建应用角色和数据库，按提示设置密码。

```sql
CREATE ROLE avatar_app LOGIN;
\password avatar_app
CREATE DATABASE avatar OWNER avatar_app;
```

在服务的私有 `.env` 中填写 `DATABASE_URL`。直接运行 CLI 或 Uvicorn时，须通过调用进程的环境显式传入。使用 psycopg 的原生 DSN，URL 中密码的特殊字符需要编码。示例使用已配置本机认证的数据库角色。

```bash
export DATABASE_URL='postgresql:///avatar?user=avatar_app'
```

源码部署的 `./service.sh start` 先启动自管 PostgreSQL 16（`data/pgsql`），再启动应用。`./service.sh restart` 保留数据库和项目独立运行的消息依赖。`./service.sh stop` 仅停止应用。PostgreSQL、Redis 和 Kafka 可继续供其他进程使用。外部 PostgreSQL 由其管理员维护。可选读缓存使用服务器项目根目录的 `redis/`，套接字示例为 `AVATAR_REDIS_SOCKET=redis/redis.sock`。Redis 不可用时管理员待审计数回源 PostgreSQL。

任务和通知继续在 PostgreSQL 提交后生效。数据库初始化时在当前 schema 安装事务内事件表和变更触发器。HTTP 应用运行转发器期间开启事务事件写入。独立 WIT 和 CLI 的写事务首次触发事件时，通过当前 schema 的建议锁检查转发器。监听在线时同样在提交后唤醒任务和订阅者，无监听时不积压事件。触发器同时发送当前 schema 专用的空载荷 NOTIFY。事务回滚会撤销事件与通知。单进程转发器先 LISTEN 再扫描，每批最多处理 128 行；空闲 8 秒复查一次，重连时继续读取已提交的积压。只配置 PostgreSQL 即可在本进程唤醒任务、通知订阅者并使待审缓存失效。Kafka 断线时任务仍走本地唤醒；Redis 缺失或发布失败时订阅者接收本地提示。认证的会话和 OIDC 查询复用一次连接池借用。已核对的单账户写入使用账户锁。同一账户仍串行，跨账户事务继续使用 Schema 写入屏障。

连接池按进程配置。 `AVATAR_PG_POOL_MIN` 默认 1，`AVATAR_PG_POOL_MAX` 默认 10，`AVATAR_PG_POOL_TIMEOUT` 默认 30 秒。通过运行监控的连接申请等待和写锁等待指标判断瓶颈。

## 历史版本

v1.5.0 直接使用已完成迁移的 PostgreSQL 与账户文件布局。已退役的 SQLite、Chroma 和 EverOS 导入工具及其操作说明保存在 Git 标签 `v1.4.5`。保留旧文件备份供恢复演练。
首次启动在同一事务中升级 PostgreSQL。先按下节备份并停写，再为缓存生成 `byte_size`、安装外键前导索引、回填历史文件有效期。建索引和计算存量缓存大小需要时间与磁盘余量；事务提交后，后续启动跳过这次数据回填。


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
.venv/bin/python scripts/run_tests.py --jobs 4 all -q
(cd frontend && npm run test:browser)
```

测试后端实例使用独立的 `avatar_test_<uuid>` schema，原件使用 `data/test/run-*` 私有工作区。正常结束时各自清理。异常中断后，下次运行检查所属角色、测试标记、数据库锁及活动连接，再回收已停用的 schema；文件工作区按运行标记清理。Vitest 和前端静态构建可独立运行。
