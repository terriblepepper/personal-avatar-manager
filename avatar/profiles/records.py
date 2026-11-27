# 模块说明：记录列表按数据库分页，复用原业务投影，普通用户只能读取本人的记录。
import json
from fastapi import HTTPException
from avatar.accounts.auth import require_admin
from avatar.accounts.roles import ADMIN_ROLES
from avatar.core.activity_visibility import event_scope
from avatar.core.db import now
from avatar.profiles.admin_reads import agent_details


# 职责：读取一页历史、日志、调用、审核、任务或协商记录。
# 输入：state 为应用依赖，user 为当前会话，collection/scope/filter 由接口校验，offset 为非负偏移。
# 结果与约束：固定十条，统计与读取在同一事务；SQL 结构来自白名单，参数不能指定其他账户。
def record_page(state,user,collection,scope='user',offset=0,search='',status='all'):
    service=state.service;admin=scope=='admin'
    if admin:require_admin(user)
    values=[];where=[];decode=dict
    if collection in ('history','submissions'):
        if collection=='submissions' and not admin:raise HTTPException(403,'仅管理员可读取审核记录')
        source='profile_submissions s JOIN users u ON u.id=s.user_id LEFT JOIN users r ON r.id=s.reviewer'
        fields='s.*,u.username,r.username AS reviewer_name';order='s.submitted_at DESC,s.id DESC'
        if not admin:where.append('s.user_id=?');values.append(user['id'])
        if status=='reviewed':where.append("s.status!='pending'")
        elif status!='all':where.append('s.status=?');values.append(status)
        if search:where.append("strpos(lower(u.username||' '||COALESCE((s.snapshot::jsonb->>'name'),'')||' '||COALESCE((s.snapshot::jsonb->>'avatar_name'),'')),lower(?))>0");values.append(search)
    elif collection=='events':
        source='audit_events e';fields='e.*';order='e.id DESC'
        current_admin=user['role'] in ADMIN_ROLES
        condition,args=event_scope(user['id'],current_admin)
        where.append(condition);values.extend(args)
    elif collection=='model_calls' and not admin:
        source='model_calls c';fields='c.id,c.provider,c.model,c.usage,c.created_at,c.purpose';order='c.id DESC'
        where.append('c.user_id=?');values.append(user['id'])
        decode=lambda row:{**dict(row),'usage':json.loads(row['usage'])}
    elif collection=='dialogues' and not admin:
        source='agent_dialogues d JOIN agent_session_members visible ON visible.session_id=d.session_id';fields='d.*';order='d.created_at DESC,d.id DESC'
        where.append("visible.user_id=? AND visible.hidden=0 AND (d.status!='ended' OR EXISTS(SELECT 1 FROM agent_dialogue_messages WHERE dialogue_id=d.id AND id>visible.cleared_through))");values.append(user['id'])
    elif admin:
        definitions={
            'agents':('avatar_records a JOIN users u ON u.id=a.user_id','a.*,u.username','a.updated_at DESC,a.user_id'),
            'jobs':('provision_jobs j JOIN profile_submissions s ON s.id=j.submission_id JOIN users u ON u.id=s.user_id LEFT JOIN oidc_identities i ON i.user_id=u.id',"j.*,COALESCE(NULLIF(trim(i.username),''),u.username) AS username",'j.updated_at DESC,j.id'),
            'identity_jobs':('avatar_identity_jobs j JOIN users u ON u.id=j.user_id','j.*,u.username','j.created_at DESC,j.id'),
            'registrations':("users u LEFT JOIN profile_records p ON p.user_id=u.id",'u.id,u.username,u.teacher_id,u.account_status,u.account_reason,u.created_at,p.draft','u.created_at DESC,u.id'),
            'account_reviews':('account_reviews r JOIN users u ON u.id=r.user_id JOIN users a ON a.id=r.reviewer','r.*,u.username AS applicant,a.username AS reviewer_name','r.id DESC')}
        if collection not in definitions:raise HTTPException(404,'记录类型不存在')
        source,fields,order=definitions[collection]
        if collection=='registrations':
            where.append("u.person_type='teacher'")
            decode=lambda row:{**dict(row),'draft':json.loads(row['draft'] or '{}')}
    else:raise HTTPException(403,'无权读取此类记录')
    if search and collection not in ('history','submissions'):
        fields_by_collection={
            'events':"e.actor||' '||e.action||' '||e.detail||' '||e.created_at",
            'model_calls':"c.provider||' '||c.model||' '||c.purpose||' '||c.created_at",
            'dialogues':"d.objective||' '||d.status||' '||d.created_at",
            'agents':"u.username||' '||a.aic||' '||a.profile",
            'jobs':"COALESCE(NULLIF(trim(i.username),''),u.username)||' '||j.id||' '||j.status||' '||j.error",
            'identity_jobs':"u.username||' '||j.status||' '||j.action",
            'registrations':"u.username||' '||COALESCE(u.teacher_id,'')||' '||COALESCE(p.draft,'')",
            'account_reviews':"u.username||' '||a.username||' '||r.reason"}
        where.append('strpos(lower('+fields_by_collection[collection]+'),lower(?))>0');values.append(search)
    if collection in ('model_calls','identity_jobs','jobs','history','events'):
        alias={'model_calls':'c','identity_jobs':'j','jobs':'j','history':'s','events':'e'}[collection]
        where.append(f'NOT EXISTS(SELECT 1 FROM record_dismissals hidden WHERE hidden.user_id=? AND hidden.collection=? AND hidden.record_id=CAST({alias}.id AS TEXT))');values.extend((user['id'],collection))
    clause=' WHERE '+' AND '.join(where) if where else ''
    with service.database.connect() as db:
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        total=db.execute('SELECT COUNT(*) FROM '+source+clause,values).fetchone()[0]
        offset=min(offset,((max(total,1)-1)//10)*10)
        rows=db.execute('SELECT '+fields+' FROM '+source+clause+' ORDER BY '+order+' LIMIT 10 OFFSET ?',[*values,offset]).fetchall()
        if collection in ('history','submissions'):
            items = service.submission_page(db, rows, admin)
        elif collection == 'agents':
            items = agent_details(state, db, rows)
        elif collection == 'dialogues':
            items = state.dialogues.session_store.views(db, rows, user)
        else:
            items = None
    return {'items':items if items is not None else [decode(row) for row in rows],'total':total,'offset':offset,'limit':10}


def dismiss_records(state,user,collection,ids):
    """白名单类别、本人调用记录／管理员维护记录；整批核验后原子隐藏。"""
    ids=list(dict.fromkeys(ids))
    if not ids or len(ids)>100 or any(not value or len(value)>128 for value in ids):raise HTTPException(422,'请选择最多 100 条记录')
    if collection in ('identity_jobs','jobs'):require_admin(user)
    elif collection not in ('model_calls','history'):raise HTTPException(404,'不支持删除此类记录')
    table={'model_calls':'model_calls','identity_jobs':'avatar_identity_jobs','jobs':'provision_jobs','history':'profile_submissions'}[collection]
    with state.service.database.connect(write=True, account_scope=user['id']) as db:
        clause='id IN ('+','.join('?' for _ in ids)+')';values=list(ids)
        if collection in ('model_calls','history'):clause+=' AND user_id=?';values.append(user['id'])
        if collection=='jobs':clause+=" AND status='done'"
        if collection=='identity_jobs':clause+=" AND status IN ('done','failed')"
        if collection=='history':clause+=" AND status IN ('approved','rejected')"
        rows=db.execute('SELECT id FROM '+table+' WHERE '+clause,values).fetchall()
        if len(rows)!=len(ids):raise HTTPException(404,'记录不存在或无权操作')
        db.executemany('INSERT OR IGNORE INTO record_dismissals VALUES(?,?,?,?)',[(user['id'],collection,value,now()) for value in ids])
    return {'deleted':len(ids)}


def dismiss_all_records(state,user,collection):
    """Hide all eligible records in one transaction, independent of paging/search."""
    if collection in ('identity_jobs','jobs'):require_admin(user)
    tables={'model_calls':'model_calls','identity_jobs':'avatar_identity_jobs','jobs':'provision_jobs','history':'profile_submissions','events':'audit_events'}
    if collection not in tables:raise HTTPException(404,'不支持删除此类记录')
    where=[];values=[]
    if collection in ('model_calls','history'):where.append('user_id=?');values.append(user['id'])
    if collection=='history':where.append("status IN ('approved','rejected')")
    if collection=='jobs':where.append("status='done'")
    if collection=='identity_jobs':where.append("status IN ('done','failed')")
    if collection=='events':
        clause,args=event_scope(user['id'],user['role'] in ADMIN_ROLES);where.append(clause);values.extend(args)
    where.append('NOT EXISTS(SELECT 1 FROM record_dismissals h WHERE h.user_id=? AND h.collection=? AND h.record_id=CAST('+('e' if collection=='events' else tables[collection])+'.id AS TEXT))');values.extend((user['id'],collection))
    with state.service.database.connect(write=True, account_scope=user['id']) as db:
        changed=db.execute('INSERT INTO record_dismissals(user_id,collection,record_id,created_at) SELECT ?,?,CAST(id AS TEXT),? FROM '+tables[collection]+(' e' if collection=='events' else '')+' WHERE '+' AND '.join(where),(user['id'],collection,now(),*values)).rowcount
    return {'deleted':changed}
