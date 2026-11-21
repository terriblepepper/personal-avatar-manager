# 模块说明：会话附件的独立归属、消息关联和个人资料转存记录，不改写现有档案文件。


# 职责：增量建立私有会话附件表及所有者约束。
# 输入：db 为已完成账户、会话和附件基础迁移的连接。
# 结果与约束：会话删除级联移除元数据，原件由会话删除事务负责清理；转存的个人文件不随会话删除。
def migrate_chat_files(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS chat_files (
      id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
      conversation_id TEXT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
      filename TEXT NOT NULL, content_type TEXT NOT NULL, size INTEGER NOT NULL,
      extracted_text TEXT NOT NULL DEFAULT '', extraction_status TEXT NOT NULL,
      created_at TEXT NOT NULL,
      promoted_id TEXT REFERENCES attachments(id) ON DELETE SET NULL);
    CREATE TABLE IF NOT EXISTS chat_file_usages (
      message_id INTEGER NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
      file_id TEXT NOT NULL REFERENCES chat_files(id) ON DELETE CASCADE,
      PRIMARY KEY(message_id,file_id));
    CREATE TRIGGER IF NOT EXISTS chat_file_usage_owner BEFORE INSERT ON chat_file_usages
      WHEN NOT EXISTS (SELECT 1 FROM chat_files f JOIN chat_messages m ON m.id=NEW.message_id
        JOIN chat_conversations c ON c.id=m.conversation_id WHERE f.id=NEW.file_id AND f.user_id=m.user_id
        AND (f.conversation_id=c.id OR f.conversation_id=c.parent_id) AND m.role='user')
      BEGIN SELECT RAISE(ABORT,'chat file usage mismatch'); END;
    CREATE INDEX IF NOT EXISTS chat_file_usages_file ON chat_file_usages(file_id,message_id);
    CREATE INDEX IF NOT EXISTS chat_files_user ON chat_files(user_id);
    CREATE INDEX IF NOT EXISTS chat_files_conversation ON chat_files(conversation_id,created_at);
    CREATE TRIGGER IF NOT EXISTS chat_file_owner_insert BEFORE INSERT ON chat_files
      WHEN NOT EXISTS (SELECT 1 FROM chat_conversations c WHERE c.id=NEW.conversation_id AND c.user_id=NEW.user_id AND c.parent_id IS NULL AND c.platform='web')
      BEGIN SELECT RAISE(ABORT,'chat file owner mismatch'); END;
    CREATE TRIGGER IF NOT EXISTS chat_file_promoted_owner BEFORE UPDATE OF promoted_id ON chat_files
      WHEN NEW.promoted_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM attachments a WHERE a.id=NEW.promoted_id AND a.user_id=NEW.user_id)
      BEGIN SELECT RAISE(ABORT,'chat file promoted owner mismatch'); END;
    CREATE TRIGGER IF NOT EXISTS chat_file_owner_update BEFORE UPDATE OF user_id,conversation_id ON chat_files
      BEGIN SELECT RAISE(ABORT,'chat file owner is immutable'); END;
    ''')
