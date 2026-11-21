"""清理完成态的未确认问题；保留用户真实补充及既有记忆。"""
import json
from avatar.core.db import now


def migrate_soul_completion(db):
    if db.execute("SELECT 1 FROM data_migrations WHERE name='soul_completed_questions'").fetchone():return
    for row in db.execute('SELECT id,document,interview FROM soul_builds').fetchall():
        document=json.loads(row['document']);interview=json.loads(row['interview']);changed=False
        if document.get('uncertainties'):
            document['uncertainties']='';changed=True
        retained=[]
        for item in interview:
            if item.get('kind')=='clarification':
                if item.get('answer') is None:changed=True;continue
                if item.get('question'):item={**item,'question':''};changed=True
            retained.append(item)
        if changed:db.execute('UPDATE soul_builds SET document=?,interview=? WHERE id=?',(json.dumps(document,ensure_ascii=False),json.dumps(retained,ensure_ascii=False),row['id']))
    for row in db.execute('SELECT user_id,document FROM style_learning_memory').fetchall():
        document=json.loads(row['document'])
        if document.get('uncertainties'):
            document['uncertainties']='';db.execute('UPDATE style_learning_memory SET document=? WHERE user_id=?',(json.dumps(document,ensure_ascii=False),row['user_id']))
    db.execute("INSERT INTO data_migrations VALUES('soul_completed_questions',?)",(now(),))
