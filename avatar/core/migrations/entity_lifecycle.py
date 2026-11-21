# 模块说明：个人正式身份的共享入口、管理任务及协议任务增量结构；保留原业务数据。


# 职责：建立派生身份维护所需的持久表与本地启停状态。
# 输入：db 为初始化事务中的 SQLite 连接。
# 结果与约束：只建表或补列；保存稳定账户独占端口映射；不修改已有 AIC。
def migrate_entity_lifecycle(db):
    if 'registration_phase' not in {r['name'] for r in db.execute('PRAGMA table_info(provision_jobs)')}:
        db.execute("ALTER TABLE provision_jobs ADD COLUMN registration_phase TEXT NOT NULL DEFAULT 'queued'")
    if 'identity_status' not in {r['name'] for r in db.execute('PRAGMA table_info(avatar_records)')}:
        db.execute("ALTER TABLE avatar_records ADD COLUMN identity_status TEXT NOT NULL DEFAULT 'active'")
    db.executescript('''
    CREATE TABLE IF NOT EXISTS avatar_entity_ports (
      user_id TEXT PRIMARY KEY REFERENCES users(id), port INTEGER NOT NULL UNIQUE,
      CHECK(port BETWEEN 1024 AND 65535));
    CREATE TABLE IF NOT EXISTS avatar_identity_jobs (
      id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
      actor TEXT NOT NULL, action TEXT NOT NULL, expected_aic TEXT NOT NULL,
      status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
      next_attempt_at REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE UNIQUE INDEX IF NOT EXISTS avatar_identity_job_busy ON avatar_identity_jobs(user_id)
      WHERE status IN ('queued','running','waiting');
    CREATE TABLE IF NOT EXISTS avatar_identity_changes (
      old_aic TEXT PRIMARY KEY, new_aic TEXT NOT NULL UNIQUE,
      user_id TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS avatar_aip_tasks (
      local_aic TEXT NOT NULL, peer_aic TEXT NOT NULL, group_id TEXT NOT NULL,
      session_id TEXT NOT NULL, task_id TEXT NOT NULL, command_id TEXT NOT NULL,
      document TEXT NOT NULL, deadline REAL,
      PRIMARY KEY(local_aic,peer_aic,group_id,session_id,task_id),
      UNIQUE(local_aic,peer_aic,group_id,session_id,command_id));
    CREATE TABLE IF NOT EXISTS avatar_aip_task_commands (
      local_aic TEXT NOT NULL, peer_aic TEXT NOT NULL, group_id TEXT NOT NULL,
      session_id TEXT NOT NULL, task_id TEXT NOT NULL, command_id TEXT NOT NULL,
      PRIMARY KEY(local_aic,peer_aic,group_id,session_id,command_id),
      FOREIGN KEY(local_aic,peer_aic,group_id,session_id,task_id)
        REFERENCES avatar_aip_tasks(local_aic,peer_aic,group_id,session_id,task_id) ON DELETE CASCADE);
    INSERT OR IGNORE INTO avatar_aip_task_commands
      SELECT local_aic,peer_aic,group_id,session_id,task_id,command_id FROM avatar_aip_tasks;
    ''')
    if 'remote_aic' not in {r['name'] for r in db.execute('PRAGMA table_info(avatar_identity_jobs)')}:
        db.execute("ALTER TABLE avatar_identity_jobs ADD COLUMN remote_aic TEXT NOT NULL DEFAULT ''")
