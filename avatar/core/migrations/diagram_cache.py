"""会话绘图缓存，存于业务数据库；删除会话级联清理，个人资料副本独立保留。"""


# 元数据和图像同事务保存，避免磁盘缓存孤儿；所有者由父会话约束，不能跨账户移动。
def migrate_diagram_cache(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS conversation_diagrams (
      id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
      conversation_id TEXT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
      cache_key TEXT NOT NULL, source TEXT NOT NULL, svg TEXT NOT NULL, png BLOB NOT NULL,
      blocked INTEGER NOT NULL DEFAULT 0, height INTEGER NOT NULL,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      promoted_id TEXT REFERENCES attachments(id) ON DELETE SET NULL,
      UNIQUE(user_id,conversation_id,cache_key));
    CREATE INDEX IF NOT EXISTS diagram_conversation ON conversation_diagrams(conversation_id);
    CREATE TRIGGER IF NOT EXISTS diagram_owner_insert BEFORE INSERT ON conversation_diagrams
      WHEN NOT EXISTS (SELECT 1 FROM chat_conversations WHERE id=NEW.conversation_id AND user_id=NEW.user_id AND platform='web' AND parent_id IS NULL)
      BEGIN SELECT RAISE(ABORT,'diagram owner mismatch'); END;
    CREATE TRIGGER IF NOT EXISTS diagram_owner_update BEFORE UPDATE ON conversation_diagrams
      WHEN NEW.user_id!=OLD.user_id OR NEW.conversation_id!=OLD.conversation_id
        OR (NEW.promoted_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM attachments WHERE id=NEW.promoted_id AND user_id=NEW.user_id))
      BEGIN SELECT RAISE(ABORT,'diagram owner mismatch'); END;
    ''')
