"""为旧审计补充可唯一确认的操作者；不删除审计、消息或已读状态。"""
SYSTEM_ACTORS = ('system','keycloak','demo-recovery','verified-platform')


def migrate_activity_ownership(db):
    if 'actor_id' in {r['name'] for r in db.execute('PRAGMA table_info(audit_events)')}:return
    db.execute('ALTER TABLE audit_events ADD COLUMN actor_id TEXT REFERENCES users(id) ON DELETE SET NULL')
    db.execute("""UPDATE audit_events SET actor_id=(SELECT u.id FROM users u WHERE u.id=audit_events.actor OR u.username=audit_events.actor)
      WHERE actor NOT IN ('system','keycloak','demo-recovery','verified-platform')
      AND (SELECT COUNT(*) FROM users u WHERE u.id=audit_events.actor OR u.username=audit_events.actor)=1""")
    db.execute("""UPDATE audit_events SET actor_id=(SELECT s.reviewer FROM profile_submissions s
      WHERE s.user_id=audit_events.user_id AND s.status='approved' AND s.reviewed_at<=audit_events.created_at
      ORDER BY s.reviewed_at DESC LIMIT 1)
      WHERE actor='system' AND action IN ('分身自动注册完成','分身资料已同步','分身创建或更新失败','跳过已被新资料替代的旧同步任务')""")
    db.execute('CREATE INDEX IF NOT EXISTS audit_actor_time ON audit_events(actor_id,id)')
