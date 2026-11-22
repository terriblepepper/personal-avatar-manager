"""小镇个人任务的执行权、恢复条件和原子协议回执。"""
import hashlib
import json
import time
import uuid

from fastapi import HTTPException
from avatar.core.db import dump, now
from avatar.conversation.loop.runtime import resume_safe
from avatar.conversation.loop.store import RunConflict, RunStore
from avatar.protocols.tasks.store import ManagementTaskError

RENEW_INTERVAL = 30
LEASE_SECONDS = 3 * RENEW_INTERVAL


def public_result(document):
    return {key: value for key, value in document.items() if key != '_agent_resume'}


def source_revision(db, owner, aic):
    row = db.execute("""SELECT a.last_modified_at,md5(a.profile),md5(a.acs_document),c.updated_at,k.updated_at,
        s.build_id,m.updated_at,e.updated_at FROM avatar_records a JOIN users u ON u.id=a.user_id
        LEFT JOIN attachment_collections c ON c.user_id=a.user_id LEFT JOIN agent_skill_collections k ON k.user_id=a.user_id
        LEFT JOIN active_souls s ON s.user_id=a.user_id LEFT JOIN model_settings m ON m.user_id=a.user_id
        LEFT JOIN embedding_settings e ON e.user_id=a.user_id
        WHERE a.user_id=? AND a.aic=? AND a.identity_status='active' AND u.account_status='active'""",
        (owner, aic)).fetchone()
    if not row:
        raise ManagementTaskError(-32003, '个人分身身份当前不可用')
    return hashlib.sha256(dump(list(row)).encode()).hexdigest()


def lease_release(db, run):
    if run.get('lease'):
        db.execute('UPDATE chat_conversations SET lease_until=0 WHERE id=? AND lease_until=?', run.pop('lease'))


def clear_run(db, row, run):
    if run.get('run_id'):
        RunStore.clear_in(db, run['owner'], 'protocol', str(row['rowid']), run['run_id'])


def close_in(db, row, document, state, message=None):
    run = document.pop('_agent_resume', {})
    lease_release(db, run)
    clear_run(db, row, run)
    status = {'state':state,'stateChangedAt':now()}
    if message:
        status['dataItems']=[{'type':'text','text':message}]
    document.update(id=uuid.uuid4().hex,sentAt=now(),status=status)
    db.execute('UPDATE avatar_aip_tasks SET document=?,deadline=NULL WHERE rowid=?',(dump(document),row['rowid']))
    return document


def interrupt_in(db, row, document):
    run = document.pop('_agent_resume', {})
    started = run.get('claimed', False)
    lease_release(db, run)
    saved = RunStore.inspect_in(db, run['owner'], 'protocol', str(row['rowid']), run['run_id']) if run.get('run_id') else None
    if run.get('run_id') and resume_safe(saved or {}, automatic=True, initial=not started):
        run['restart'] = True
        document['_agent_resume'] = run
        state, message = 'awaiting-input', '已保存进度，原调用者查询后将继续'
    elif saved and resume_safe(saved):
        document['_agent_resume'] = run
        state, message = 'awaiting-input', '上次请求的自动重试已用尽，请补充要求后继续'
    else:
        clear_run(db, row, run)
        state, message = 'failed', '上一次操作缺少可安全恢复的回执，请核对已有结果后重新发起任务'
    document.update(id=uuid.uuid4().hex, sentAt=now(), status={'state':state, 'stateChangedAt':now(),
                    'dataItems':[{'type':'text','text':message}]})
    db.execute('UPDATE avatar_aip_tasks SET document=?,deadline=NULL WHERE rowid=?', (dump(document), row['rowid']))


def current_in(db, rowid, writer, run):
    row = db.execute('SELECT document FROM avatar_aip_tasks WHERE rowid=?',(rowid,)).fetchone()
    if row:
        doc = json.loads(row['document'])
        if (doc['status']['state']=='working' and doc['id']==writer and
                doc.get('_agent_resume',{}).get('run_id')==run['run_id']):
            return doc
    raise RunConflict('当前任务已结束或已被其他执行者接管')


def renew(database, rowid, writer, run, conversation_id=None):
    with database.connect(write=True, account_scope=run['owner']) as db:
        doc = current_in(db, rowid, writer, run)
        saved = doc['_agent_resume']
        current = time.time()
        until = current+LEASE_SECONDS
        if conversation_id:
            if not db.execute('UPDATE chat_conversations SET lease_until=? WHERE id=? AND user_id=? AND lease_until<?',
                              (until,conversation_id,run['owner'],current)).rowcount:
                raise HTTPException(409,'本会话上一条消息仍在处理，请等待完成')
        elif saved.get('lease'):
            conversation_id, previous = saved['lease']
            if not db.execute('UPDATE chat_conversations SET lease_until=? WHERE id=? AND lease_until=?',
                              (until,conversation_id,previous)).rowcount:
                raise RunConflict('原任务的会话执行权已变更')
        if conversation_id:
            saved.update(lease=[conversation_id,until],claimed=True)
        db.execute('UPDATE avatar_aip_tasks SET document=?,deadline=? WHERE rowid=?',(dump(doc),until,rowid))

# 职责：保存当前任务结果，保留并发取消或超时的终态。
# 输入：rowid 和当前 working 消息 ID 固定执行者，message 为产品或状态说明。
# 结果与约束：只更新原任务，不覆盖已取消结果；成功等待 Leader 确认最多 300 秒。

def finish_in(db, rowid, result, run, state, message):
    row = db.execute('SELECT rowid,document FROM avatar_aip_tasks WHERE rowid=?',(rowid,)).fetchone()
    if not row:
        raise RunConflict('原任务已删除')
    current = json.loads(row['document'])
    if current['status']['state']!='working' or current['id']!=result['id']:
        return public_result(current)
    document = {**result,'id':uuid.uuid4().hex,'sentAt':now(),'status':{'state':state,'stateChangedAt':now()}}
    lease_release(db,current['_agent_resume'])
    if state=='awaiting-completion':
        document['products']=[{'id':uuid.uuid4().hex,'dataItems':[{'type':'text','text':message}]}]
    else:
        document['status']['dataItems']=[{'type':'text','text':message}]
    if state=='awaiting-input':
        document['_agent_resume']={**current['_agent_resume'],**run}
    else:
        clear_run(db,row,run)
    db.execute('UPDATE avatar_aip_tasks SET document=?,deadline=? WHERE rowid=?',
               (dump(document),time.time()+300 if state=='awaiting-completion' else None,rowid))
    return public_result(document)


def finish(database, rowid, result, run, state, message):
    with database.connect(write=True) as db:
        return finish_in(db,rowid,result,run,state,message)
