"""仅将已确认工具调用的系统回执合并展示，保留原始消息和记忆。"""
from avatar.core.db import dump,now


def migrate_task_events(db):
    if db.execute("SELECT 1 FROM data_migrations WHERE name='inline_task_receipts'").fetchone():return
    receipts={'negotiation':'已确认发起任务，请求已进入通信队列。实际送达与失败原因请查看分身会话；尚不能视为已送达。','message':'已确认发送消息，请求已进入通信队列。实际送达与失败原因请查看分身会话；尚不能视为已送达。'}
    rows=db.execute('''SELECT i.message_id,i.task_id,d.objective,d.delivery_mode,r.id AS receipt_id
      FROM agent_intents i JOIN agent_dialogues d ON d.id=i.task_id
      JOIN chat_messages original ON original.id=i.message_id AND original.user_id=i.user_id AND original.conversation_id=i.conversation_id AND original.role='assistant'
      JOIN chat_messages r ON r.user_id=i.user_id AND r.conversation_id=i.conversation_id AND r.role='assistant' AND r.id>original.id AND r.created_at>=d.created_at
      WHERE i.status='confirmed' AND r.content IN (?,?)
      AND NOT EXISTS(SELECT 1 FROM chat_messages next WHERE next.conversation_id=i.conversation_id AND next.id>original.id AND next.id<r.id AND next.role='user')''',tuple(receipts.values())).fetchall()
    for row in rows:
        receipt=db.execute('SELECT content FROM chat_messages WHERE id=?',(row['receipt_id'],)).fetchone()
        if receipt['content']!=receipts.get(row['delivery_mode']):continue
        event={'kind':'start','task_id':row['task_id'],'title':row['objective'],'mode':row['delivery_mode'],'state':'submitted'}
        db.execute('UPDATE chat_messages SET task_event=? WHERE id=?',(dump(event),row['message_id']))
        db.execute('UPDATE chat_messages SET task_event=? WHERE id=?',(dump({'kind':'superseded','message_id':row['message_id']}),row['receipt_id']))
    db.execute("INSERT INTO data_migrations VALUES('inline_task_receipts',?)",(now(),))


def migrate_cancelled_intent_receipts(db):
    """只隐藏旧版本追加的精确取消回执，原记录保留供记忆读取。"""
    if db.execute("SELECT 1 FROM data_migrations WHERE name='inline_cancelled_intent_receipts'").fetchone():return
    receipts=tuple('用户已取消分身通信调用（'+stage+'阶段）。本次未发送消息或任务请求，不得视为已发送。' for stage in ('内容表单','类型选择'))
    rows=db.execute('''SELECT i.message_id,r.id AS receipt_id FROM agent_intents i
      JOIN chat_messages original ON original.id=i.message_id AND original.user_id=i.user_id AND original.conversation_id=i.conversation_id AND original.role='assistant'
      JOIN chat_messages r ON r.user_id=i.user_id AND r.conversation_id=i.conversation_id AND r.role='assistant' AND r.id>original.id
      WHERE i.status='cancelled' AND r.content IN (?,?)
      AND NOT EXISTS(SELECT 1 FROM chat_messages next WHERE next.conversation_id=i.conversation_id AND next.id>original.id AND next.id<r.id AND next.role='user')''',receipts).fetchall()
    for row in rows:
        db.execute('UPDATE chat_messages SET task_event=? WHERE id=?',(dump({'kind':'superseded','message_id':row['message_id']}),row['receipt_id']))
    db.execute("INSERT INTO data_migrations VALUES('inline_cancelled_intent_receipts',?)",(now(),))
