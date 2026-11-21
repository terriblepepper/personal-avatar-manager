# 模块说明：会话工具开关与生成文件的增量迁移，保留原消息、文件及账户归属。


# 职责：增加工具设置及文件来源/输出关联，不将历史会话自动授权给执行工具。
def migrate_conversation_tools(db):
    for table, fields in {
        'chat_messages': {'tool_events': "TEXT NOT NULL DEFAULT '[]'"},
        'chat_conversations': {'tools_config': "TEXT NOT NULL DEFAULT '{}'", 'tools_updated_at': "TEXT NOT NULL DEFAULT ''"},
        'chat_files': {'origin': "TEXT NOT NULL DEFAULT 'upload'", 'generated_message_id': 'INTEGER REFERENCES chat_messages(id) ON DELETE SET NULL'},
    }.items():
        columns={row['name'] for row in db.execute('PRAGMA table_info('+table+')')}
        for name, definition in fields.items():
            if name not in columns:db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
    columns={row['name'] for row in db.execute('PRAGMA table_info(chat_files)')}
    if 'referenced' not in columns:
        db.execute('ALTER TABLE chat_files ADD COLUMN referenced INTEGER NOT NULL DEFAULT 1 CHECK(referenced IN (0,1))')
        # 历史文件最多保留最近五份引用，避免迁移后默认选中超过单轮上限；不删除任何文件。
        db.execute('''UPDATE chat_files SET referenced=0 WHERE origin='generated' OR id IN (
            SELECT id FROM (SELECT id,row_number() OVER(PARTITION BY conversation_id ORDER BY created_at DESC,id DESC) AS position
                            FROM chat_files WHERE origin!='generated') WHERE position>5)''')
    if 'source_label' not in columns:
        db.execute("ALTER TABLE chat_files ADD COLUMN source_label TEXT NOT NULL DEFAULT ''")
    db.executescript('''
    CREATE TRIGGER IF NOT EXISTS chat_output_owner BEFORE UPDATE OF generated_message_id ON chat_files
      WHEN NEW.generated_message_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM chat_messages m JOIN chat_conversations c ON c.id=m.conversation_id
        WHERE m.id=NEW.generated_message_id AND m.user_id=NEW.user_id AND m.role='assistant'
          AND (c.id=NEW.conversation_id OR c.parent_id=NEW.conversation_id))
      BEGIN SELECT RAISE(ABORT,'generated file owner mismatch'); END;
    CREATE INDEX IF NOT EXISTS chat_files_output_message ON chat_files(generated_message_id);
    ''')
