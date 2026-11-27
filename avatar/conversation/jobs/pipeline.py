"""Generation scope crosses WIT adapters without adding client-controlled fields."""
from fastapi import HTTPException
from pathlib import Path
import json
import hashlib
from contextlib import contextmanager
from avatar.core.execution import current_generation, authorization, execution_guard
from avatar.skills.policy import require_skills_enabled
from avatar.conversation.pause_state import pending, available_outputs


@contextmanager
def generation_scope(job,database=None):
    token = current_generation.set(job)
    try:
        def validate():
            with database.connect() as db:validate_authorization(db,job)
        with execution_guard(validate if database else None):yield
    finally:
        current_generation.reset(token)


def capture_authorization(db,user_id,body):
    files=[row['id'] for row in db.execute('SELECT id FROM chat_files WHERE user_id=? AND conversation_id=? AND referenced=1',(user_id,body.conversation_id))]
    materials=[row['id'] for row in db.execute("SELECT id FROM attachments WHERE visibility='public' OR user_id=?",(user_id,))]
    versions={}
    for table,column in (('model_settings','model_config'),('embedding_settings','config')):
        row=db.execute(f'SELECT {column} FROM {table} WHERE user_id=?',(user_id,)).fetchone()
        versions[table]=hashlib.sha256((row[0] if row else '{}').encode()).hexdigest()
    conversation = db.execute('SELECT tools_config FROM chat_conversations WHERE id=? AND user_id=?',
                              (body.conversation_id,user_id)).fetchone()
    effort = json.loads(conversation[0]).get('reasoning_effort') if conversation else None
    return dict(skills={},files=files,materials=materials,versions=versions,reasoning_effort=effort)


def validate_versions(db,user_id,scope):
    for table,column in (('model_settings','model_config'),('embedding_settings','config')):
        row=db.execute(f'SELECT {column} FROM {table} WHERE user_id=?',(user_id,)).fetchone()
        expected=scope.get('versions',{}).get(table)
        if expected and expected!=hashlib.sha256((row[0] if row else '{}').encode()).hexdigest():
            raise HTTPException(409,'排队期间模型配置已改变，请重新发起请求')


def validate_authorization(db,job=None,*,lock=False):
    job=job or current_generation.get()
    if not job:return
    if not isinstance(job,dict):job=dict(job)
    uid=job['user_id'];scope=json.loads(job['authorization'])
    if not db.execute("SELECT 1 FROM users WHERE id=? AND account_status='active'",(uid,)).fetchone():
        raise HTTPException(403,'账户已停用，未继续执行或保存回答')
    query="""SELECT 1 FROM chat_generation_jobs WHERE task_id=? AND user_id=? AND state='running'
        AND (authorization::jsonb->>'claim_token') IS NOT DISTINCT FROM ?"""
    if lock:query+=' FOR UPDATE'
    if not db.execute(query,(job['task_id'],uid,scope.get('claim_token'))).fetchone():
        raise HTTPException(409,'原请求已结束，未继续执行或保存回答')
    validate_versions(db,uid,scope)
    if job.get('_source_guard'):
        job['_source_guard'](db,full=lock)
    if scope.get('skills'):
        require_skills_enabled()


def sandbox_inputs(chat,user_id,parent_id,files,saved=(),pause_id=None):
    database=chat.service.database;scope=authorization();job=current_generation.get()
    with database.connect() as db:
        profile=[dict(row) for row in db.execute('SELECT id,filename FROM attachments WHERE user_id=?',(user_id,))]
    def read(source,identifier,directory):
        with database.connect() as db:
            validate_authorization(db,job)
            if source=='generated':
                task=pending(db,user_id,parent_id)
                row=bool(task and task['snapshot']['agent_pause_id']==pause_id and identifier in task['outputs']
                         and available_outputs(chat,db,user_id,parent_id,[identifier]))
            elif source=='profile':
                row=db.execute('SELECT 1 FROM attachments WHERE id=? AND user_id=?',(identifier,user_id)).fetchone()
            else:
                row=db.execute("SELECT 1 FROM chat_files WHERE id=? AND user_id=? AND conversation_id=? AND referenced=1 AND (expires_at IS NULL OR avatar_utc_time(expires_at)>CURRENT_TIMESTAMP)",(identifier,user_id,parent_id)).fetchone()
            if not row:raise HTTPException(409,'文件已删除或引用授权已撤销，未读取原件')
            try:
                path=(directory.new(user_id,identifier+'.blob') if source=='profile'
                      else directory.path(user_id,parent_id,identifier+'.blob'))
                with path.open('rb') as stream:return stream.read(8*1024*1024+1)
            except FileNotFoundError:raise HTTPException(404,'文件原件已删除') from None
    inputs={}
    for source,items,directory,permission in [('chat',files,chat.chat_files.directory,'files'),('profile',profile,chat.service.attachments.directory,'materials'),('generated',saved,chat.chat_files.directory,None)]:
        for item in items:
            identifier=item['id']
            if permission and scope is not None and identifier not in scope.get(permission,[]):continue
            inputs[identifier]={'path':'inputs/'+identifier+'/'+Path(item['filename']).name,
                                'read':lambda source=source,identifier=identifier,directory=directory:read(source,identifier,directory)}
    return inputs
