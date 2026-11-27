"""Read-only conversation skill status for authenticated owners."""
from fastapi import HTTPException


class SessionSkills:
    def __init__(self, database):
        self.database = database

    def owned(self,db,user_id,conversation_id):
        row=db.execute("SELECT * FROM chat_conversations WHERE id=? AND user_id=? AND platform='web' AND parent_id IS NULL",(conversation_id,user_id)).fetchone()
        if not row:raise HTTPException(404,'会话不存在')
        return row

    def list(self, user_id, conversation_id):
        with self.database.connect() as db:
            self.owned(db, user_id, conversation_id)
        return []
