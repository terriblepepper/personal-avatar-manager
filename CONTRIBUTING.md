# 贡献指南（分支与 PR 流程）

本仓库按 Git Flow 简化版协作：**`main` 只接受 PR，不接受直接 push**。
目的是让每一次代码改动都留下「为什么改、改了什么、怎么验证过」的记录。

## 分支模型

```text
main            始终可发布的稳定分支
 ├─ feat/...    新功能
 ├─ fix/...     缺陷修复
 ├─ docs/...    仅文档
 └─ chore/...   构建、依赖、脚本、杂项
```

- 一律从 `main` 切出，合并回 `main`。
- 命名用小写连字符，并带模块范围：`feat/files-upload-batch`、`fix/conversation-generation-lease`、`docs/readme-rewrite`。
- 一个分支只做一件事。发现顺手的小问题另开分支，不要混进当前 PR。
- 长期分支需要定期 `git merge main`（或 rebase 未推送的提交）以保持可合并。

## 提交规范

```text
<type>(<scope>): <祈使句摘要，50 字以内>

<可选正文：说明动机与取舍，每行 72 字以内>
```

- `type` 取 `feat` / `fix` / `docs` / `test` / `refactor` / `chore` / `perf`。
- `scope` 用包名或模块名：`api`、`conversation`、`files`、`knowledge`、`models`、`protocols`、`runtime`、`skills`、`web`、`docs`、`build`。
- 摘要说「做了什么」，正文说「为什么」；不要写「修改代码」「更新文件」这类空话。
- 一次提交是一个完整可回滚的逻辑单元；调试残留、格式化噪音单独提交或单独 PR。

示例：

```text
fix(conversation): 终态事务内释放会话租约

成功落库与生成器清理之间进程退出时，旧 lease_until 会让同一会话
最长约 480 秒无法继续对话。改为在持久终态事务中一并归零。
```

## 开发环境

```bash
uv sync --locked          # 严格按 uv.lock 安装，含 dev 组
# 预先创建专用库，授予测试角色创建 schema 的权限
export TEST_POSTGRES_URL=postgresql:///avatar_test
.venv/bin/python scripts/run_tests.py --jobs 4 all -q
```

- Python 3.12 / 3.13（推荐 3.13，暂不支持 3.14）。
- 依赖变更必须同时更新 `pyproject.toml` 与 `uv.lock`，两者在同一个 PR 内提交。
- `wit` 与 `acps-sdk` 走根目录本地 wheel（`[tool.uv.sources]`），不要改成网络源。
- `everos` 由 `vendor/EverOS` 以 uv workspace 成员方式解析。

## PR 前自检（必须全部通过）

```bash
uv sync --locked
export TEST_POSTGRES_URL=postgresql:///avatar_test
.venv/bin/python scripts/run_tests.py --jobs 4 all -q
.venv/bin/python scripts/run_tests.py all --collect-only -q
```

1. 测试全绿，且新增行为有对应回归用例（先写失败用例，再实现修复）。
2. 测试围绕实际风险和接口契约增量维护，不为覆盖率添加重复用例（见 [docs/验收说明.md](docs/验收说明.md)）。
3. 数据库结构变更同步 `avatar/core/postgres_schema.py` 或其调用的模块建表逻辑，验证空库初始化与已有 PostgreSQL 数据升级。
4. 文档同步：新增/变更的配置项、接口、运维步骤写进 `docs/` 对应文档，必要时更新 README 索引。
5. 没有把 `data/`、`.env`、`dist/`、`build/`、`.venv/`、`.tools/` 里的内容误提交。
6. Vue 前端运行 `npm run typecheck`、`npm test`、`npm run build`；涉及页面流程时补充 Playwright 验证。

## PR 要求

- 标题与提交规范一致；描述包括动机、改动范围、验证方式及影响与风险，**验证方式一栏要贴实际命令与结果**，不写「应该没问题」。
- 至少一名评审通过；评审关注设计一致性、边界与并发/事务窗口，而不只是代码风格。
- 有冲突由作者解决后再请求复审。
- 合并方式：squash 或 merge 均可，但不得 force push 改写已合并到 `main` 的历史。
- 合并后删除远端分支。

## 发布

```bash
.venv/bin/python scripts/build_release.py
```

- 发行版本在 `pyproject.toml`（`[project] version`）维护，并用 `uv lock` 同步本项目锁文件元数据；发布时从合并后的 `main` 打 `v<MAJOR>.<MINOR>.<PATCH>` 标签。
- 平台 API 与管理 ACS 通过 `avatar/core/config.py:application_version()` 优先读取 `AVATAR_VERSION`；源码读取项目元数据，安装包读取发行元数据。个人分身 ACS 保留用户填写的版本；能力本体模板版本独立维护。
- 发布 PR 同步 README、`.env.example` 和打包示例，API 或平台管理 ACS 不再写死版本；Release 说明直接填写在 GitHub 发布页，不在 docs 内维护副本。
- 代码里的 ACS 版本只影响**新生成**的身份材料：`create_identity_json` 遇到已存在的 `data/system_identity/acs.json` 不覆盖。线上要更新 ACS 版本必须重写材料并重新注册（注销旧身份、撤旧证书、同端口重申，AIC 会变）。
- 交付物为 `dist/` 下的 wheel、sdist 与交付 ZIP（含依赖 wheel 与 SHA256 清单）。
- `dist/` 本身不入库（见 `.gitignore`），三个产物一律挂到 **GitHub Release** 对应 tag 下，Release 是唯一的对外分发处。
- 发布顺序：合并发布 PR → 在 `main` 上打 tag 并推送 → 本地执行 `scripts/build_release.py` → 将三个产物上传到该 tag 的草稿 Release → 回读校验后公开发布。发布凭据仅用于操作进程，不写入仓库、打包产物或日志。
- 部署前按 [docs/操作手册.md](docs/操作手册.md) 在停写窗口成套备份 PostgreSQL、私有文件和 `.env`；回退恢复匹配版本及同批快照。

后端测试运行器默认使用 `all` 全量模式。`unit` 选择无需 PostgreSQL fixture 的用例，`quick` 运行发布冒烟用例。两种模式均接受 pytest 的路径及筛选参数。
