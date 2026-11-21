"""Generation scope crosses WIT adapters without adding client-controlled fields."""
import json
import hashlib
import re
from contextlib import contextmanager
from avatar.core.execution import current_generation, authorization


@contextmanager
def generation_scope(job,database=None):
    token = current_generation.set(job)
    try:
        from avatar.core.execution import execution_guard
        def validate():
            with database.connect() as db:validate_authorization(db,job)
        with execution_guard(validate if database else None):yield
    finally:
        current_generation.reset(token)


def capture_authorization(db,user_id,body):
    from avatar.skills.service import UserSkills
    rows=db.execute('SELECT id,name,updated_at FROM agent_skills WHERE user_id=? AND enabled=1',(user_id,)).fetchall()
    active={row['skill_id'] for row in db.execute('SELECT skill_id FROM chat_session_skills WHERE conversation_id=? AND enabled=1 AND authorized=1',(body.conversation_id,))}
    names={name.casefold() for name in UserSkills.command_names([dict(row) for row in rows],body.message)}
    skills={row['id']:row['updated_at'] for row in rows if row['id'] in active or row['name'].casefold() in names or re.search(r'(?<![\w-])/'+re.escape(row['name'])+r'(?![\w-])',body.message)}
    files=[row['id'] for row in db.execute('SELECT id FROM chat_files WHERE user_id=? AND conversation_id=? AND referenced=1',(user_id,body.conversation_id))]
    materials=[row['id'] for row in db.execute("SELECT id FROM attachments WHERE visibility='public' OR user_id=?",(user_id,))]
    versions={}
    for table,column in (('model_settings','model_config'),('embedding_settings','config')):
        row=db.execute(f'SELECT {column} FROM {table} WHERE user_id=?',(user_id,)).fetchone()
        versions[table]=hashlib.sha256((row[0] if row else '{}').encode()).hexdigest()
    return dict(skills=skills,files=files,materials=materials,versions=versions)


def validate_versions(db,user_id,scope):
    from fastapi import HTTPException
    for table,column in (('model_settings','model_config'),('embedding_settings','config')):
        row=db.execute(f'SELECT {column} FROM {table} WHERE user_id=?',(user_id,)).fetchone()
        expected=scope.get('versions',{}).get(table)
        if expected and expected!=hashlib.sha256((row[0] if row else '{}').encode()).hexdigest():
            raise HTTPException(409,'排队期间模型配置已改变，请重新发起请求')


def validate_authorization(db,job=None):
    from fastapi import HTTPException
    job=job or current_generation.get()
    if not job:return
    uid=job['user_id'];scope=json.loads(job['authorization'])
    if not db.execute("SELECT 1 FROM users WHERE id=? AND account_status='active'",(uid,)).fetchone():
        raise HTTPException(403,'账户已停用，未继续执行或保存回答')
    if not db.execute("SELECT 1 FROM chat_generation_jobs WHERE task_id=? AND user_id=? AND state='running'",(job['task_id'],uid)).fetchone():
        raise HTTPException(409,'原请求已结束，未继续执行或保存回答')
    validate_versions(db,uid,scope)
    for identifier,version in scope.get('skills',{}).items():
        row=db.execute('SELECT enabled,updated_at FROM agent_skills WHERE id=? AND user_id=?',(identifier,uid)).fetchone()
        session=db.execute('SELECT enabled,authorized FROM chat_session_skills WHERE conversation_id=? AND skill_id=?',(job['conversation_id'],identifier)).fetchone()
        if not row or not row['enabled'] or row['updated_at']!=version or session and not(session['enabled'] and session['authorized']):
            raise HTTPException(409,'技能授权或内容已变化，未继续执行或保存回答')


def sandbox_inputs(chat,user_id,parent_id,files,saved=(),pause_id=None):
    from pathlib import Path
    from fastapi import HTTPException
    database=chat.service.database;scope=authorization();job=current_generation.get()
    with database.connect() as db:
        profile=[dict(row) for row in db.execute('SELECT id,filename FROM attachments WHERE user_id=?',(user_id,))]
    def read(source,identifier,directory):
        with database.connect() as db:
            validate_authorization(db,job)
            if source=='generated':
                from avatar.conversation.pauses import pending, available_outputs
                task=pending(db,user_id,parent_id)
                row=bool(task and task['snapshot']['agent_pause_id']==pause_id and identifier in task['outputs']
                         and available_outputs(chat,db,user_id,parent_id,[identifier]))
            elif source=='profile':
                row=db.execute('SELECT 1 FROM attachments WHERE id=? AND user_id=?',(identifier,user_id)).fetchone()
            else:
                row=db.execute("SELECT 1 FROM chat_files WHERE id=? AND user_id=? AND conversation_id=? AND referenced=1 AND (expires_at IS NULL OR julianday(expires_at)>julianday('now'))",(identifier,user_id,parent_id)).fetchone()
            if not row:raise HTTPException(409,'文件已删除或引用授权已撤销，未读取原件')
            try:
                path=directory.new(user_id,identifier+'.blob')
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
