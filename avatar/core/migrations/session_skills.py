# 模块说明：保留聊天历史，增加每条回答技能快照及本人会话的技能停用状态。


# 职责：增量建立会话技能表并补充记忆失效日期、回答技能元数据。
# 输入：已初始化聊天、用户及技能表的数据库连接。
# 结果与约束：外键和触发器禁止把技能关联到其他账户或内部协议会话，卸载技能自动清理关联。
def migrate_session_skills(db):
    for table,column,declaration in [('chat_messages','used_skills',"TEXT NOT NULL DEFAULT '[]'"),('chat_conversations','skills_updated_at',"TEXT NOT NULL DEFAULT ''"),('chat_files','description',"TEXT NOT NULL DEFAULT ''"),('chat_files','parse_checked','INTEGER NOT NULL DEFAULT 0')]:
        if column not in {r['name'] for r in db.execute('PRAGMA table_info('+table+')')}:
            db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {declaration}')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS chat_session_skills (
      conversation_id TEXT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
      skill_id TEXT NOT NULL REFERENCES agent_skills(id) ON DELETE CASCADE,
      enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),last_used_at TEXT NOT NULL DEFAULT '',
      PRIMARY KEY(conversation_id,skill_id));
    CREATE TRIGGER IF NOT EXISTS session_skill_owner BEFORE INSERT ON chat_session_skills
      WHEN NOT EXISTS(SELECT 1 FROM chat_conversations c JOIN agent_skills s ON s.user_id=c.user_id
                      WHERE c.id=NEW.conversation_id AND s.id=NEW.skill_id AND c.platform='web' AND c.parent_id IS NULL)
      BEGIN SELECT RAISE(ABORT,'session skill owner mismatch'); END;
    CREATE TRIGGER IF NOT EXISTS session_skill_immutable BEFORE UPDATE OF conversation_id,skill_id ON chat_session_skills
      WHEN NEW.conversation_id IS NOT OLD.conversation_id OR NEW.skill_id IS NOT OLD.skill_id
      BEGIN SELECT RAISE(ABORT,'session skill association is immutable'); END;
    ''')
    if 'authorized' not in {r['name'] for r in db.execute('PRAGMA table_info(chat_session_skills)')}:
        db.execute('ALTER TABLE chat_session_skills ADD COLUMN authorized INTEGER NOT NULL DEFAULT 0')
        # 旧使用记录无法证明用户主动引入，不将自动加载历史升级为授权。
        db.execute("UPDATE chat_conversations SET summary='',summary_through=0,summary_profile_modified_at='' WHERE id IN (SELECT conversation_id FROM chat_session_skills)")
