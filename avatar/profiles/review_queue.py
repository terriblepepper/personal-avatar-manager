"""统一分身创建、资料更新和 Soul 审核，并展示历史教师审核记录。"""
import json
from fastapi import HTTPException
from avatar.accounts.auth import require_admin
from avatar.core.db import now


# 分类依据提交前已通过的申请，不用当前是否存在分身反推历史创建申请。
SOURCE="""SELECT 'profile' AS kind,s.id,s.user_id,u.username,
 COALESCE(json_extract(s.snapshot,'$.name'),'') AS name,
 COALESCE(json_extract(s.snapshot,'$.avatar_name'),'') AS avatar_name,
 s.submitted_at AS created_at,s.status,s.reason,s.reviewed_at,
 CASE WHEN EXISTS(SELECT 1 FROM avatar_records a WHERE a.user_id=s.user_id AND julianday(a.created_at)<julianday(s.submitted_at)) OR EXISTS(SELECT 1 FROM profile_submissions p WHERE p.user_id=s.user_id
 AND p.status='approved' AND p.submitted_at<s.submitted_at) THEN 'update' ELSE 'create' END AS category,s.last_modified_at
 FROM profile_submissions s JOIN users u ON u.id=s.user_id

 UNION ALL SELECT 'teacher_history',CAST(r.id AS TEXT),u.id,u.username,
 COALESCE(json_extract(p.draft,'$.name'),''),'',r.created_at,
 CASE WHEN r.approved=1 THEN 'approved' ELSE 'rejected' END,r.reason,r.created_at,'teacher',COALESCE(p.last_modified_at,'')
 FROM account_reviews r JOIN users u ON u.id=r.user_id LEFT JOIN profile_records p ON p.user_id=u.id
 UNION ALL SELECT 'soul',b.id,b.user_id,u.username,
 COALESCE(json_extract(b.profile_snapshot,'$.name'),''),COALESCE(json_extract(b.profile_snapshot,'$.avatar_name'),''),
 b.created_at,b.status,b.reason,b.reviewed_at,'soul',b.profile_revision
 FROM soul_builds b JOIN users u ON u.id=b.user_id WHERE b.status IN ('pending','approved','rejected')"""


# 所有审核查询及变更均再次核验管理员；整批任一失败则回滚审核、任务和审计。
class ReviewQueue:
    def __init__(self,service):self.service=service;self.database=service.database

    def page(self,user,category='all',status='pending',search='',offset=0):
        require_admin(user)
        if category not in ('all','teacher','create','update','soul') or status not in ('all','pending','approved','rejected') or len(search)>100 or not 0<=offset<=2147483647:raise HTTPException(422,'审核筛选无效')
        where=["NOT EXISTS(SELECT 1 FROM record_dismissals hidden WHERE hidden.user_id=? AND hidden.collection='review_queue' AND hidden.record_id=(kind||':'||id))"];values=[user['id']]
        for field,value in [('category',category),('status',status)]:
            if value!='all':where.append(field+'=?');values.append(value)
        if search:where.append("instr(lower(username||' '||name||' '||avatar_name||' '||reason),lower(?))>0");values.append(search)
        clause=' WHERE '+' AND '.join(where)
        with self.database.connect() as db:
            db.execute('BEGIN')
            total=db.execute('SELECT COUNT(*) FROM ('+SOURCE+')'+clause,values).fetchone()[0]
            offset=min(offset,((max(total,1)-1)//10)*10)
            rows=db.execute('SELECT * FROM ('+SOURCE+')'+clause+' ORDER BY created_at DESC,kind,id LIMIT 10 OFFSET ?',(*values,offset)).fetchall()
        return {'items':[dict(r) for r in rows],'total':total,'offset':offset,'limit':10}

    def detail(self,user,kind,identifier):
        require_admin(user)
        with self.database.connect() as db:
            if kind=='soul':
                row=db.execute('SELECT * FROM soul_builds WHERE id=?',(identifier,)).fetchone()
                if row:
                    ids = json.loads(row['asset_ids'])
                    found = {asset['id']: dict(asset) for asset in db.execute(
                        'SELECT id,filename,mime,caption FROM knowledge_assets WHERE user_id=? AND id=ANY(?::text[])',
                        (row['user_id'], ids))}
                    assets = [{**found[asset_id], 'download_url': '/api/admin/review-queue/soul/'+identifier+'/assets/'+asset_id}
                              for asset_id in ids if asset_id in found]
                    return {'kind':kind,**self.service.knowledge.decode(row),'snapshot':json.loads(row['profile_snapshot']),'assets':assets}
            if kind=='profile':
                row=db.execute('SELECT s.*,u.username,r.username AS reviewer_name FROM profile_submissions s JOIN users u ON u.id=s.user_id LEFT JOIN users r ON r.id=s.reviewer WHERE s.id=?',(identifier,)).fetchone()
                if row:
                    job=db.execute('SELECT id,status,registration_phase,error FROM provision_jobs WHERE submission_id=?',(identifier,)).fetchone()
                    return {'kind':kind,**self.service._submission(row,admin=True),'registration':dict(job) if job else None}
            elif kind=='teacher_history':
                row=db.execute('SELECT * FROM ('+SOURCE+') WHERE kind=? AND id=?',(kind,identifier)).fetchone()
                if row:
                    profile=db.execute('SELECT draft FROM profile_records WHERE user_id=?',(row['user_id'],)).fetchone()
                    return {**dict(row),'snapshot':json.loads(profile['draft']) if profile else {}}
        raise HTTPException(404,'审核记录不存在')

    def decide(self,user,items,decision):
        require_admin(user)
        identifiers=list(dict.fromkeys((item.kind,item.id) for item in items))
        if not 1<=len(identifiers)<=100:raise HTTPException(422,'请选择 1～100 项待审核记录')
        with self.database.connect(write=True) as db:
            current=db.execute('SELECT role FROM users WHERE id=?',(user['id'],)).fetchone()
            if not current:raise HTTPException(403,'需要管理员权限')
            require_admin(current)
            for kind,identifier in identifiers:
                if kind=='profile':self.service.review(user,identifier,decision,db=db)
                elif kind=='soul':self.service.knowledge.review(user,identifier,decision,db)
                else:raise HTTPException(422,'只能处理待审核记录')
        return {'updated':len(identifiers)}

    def dismiss(self,user,items):
        """只移除当前管理员的已处理列表条目，原审核决定和再次申请不受影响。"""
        require_admin(user)
        identifiers=list(dict.fromkeys((item.kind,item.id) for item in items))
        if not 1<=len(identifiers)<=100:raise HTTPException(422,'请选择 1～100 项已处理审核记录')
        with self.database.connect(write=True) as db:
            current=db.execute('SELECT role FROM users WHERE id=?',(user['id'],)).fetchone()
            require_admin(current if current else {'role':'user'})
            for kind,identifier in identifiers:
                row=db.execute('SELECT status FROM ('+SOURCE+') WHERE kind=? AND id=?',(kind,identifier)).fetchone()
                if not row:raise HTTPException(404,'审核记录不存在')
                if row['status'] not in ('approved','rejected'):raise HTTPException(409,'只能删除已通过或已退回的记录')
            db.executemany('INSERT OR IGNORE INTO record_dismissals VALUES(?,?,?,?)',[(user['id'],'review_queue',kind+':'+identifier,now()) for kind,identifier in identifiers])
        return {'deleted':len(identifiers)}


    def dismiss_all(self,user):
        require_admin(user)
        with self.database.connect(write=True) as db:
            current=db.execute('SELECT role FROM users WHERE id=?',(user['id'],)).fetchone()
            require_admin(current if current else {'role':'user'})
            count=db.execute("INSERT INTO record_dismissals(user_id,collection,record_id,created_at) SELECT ?,'review_queue',kind||':'||id,? FROM ("+SOURCE+") WHERE status IN ('approved','rejected') AND NOT EXISTS(SELECT 1 FROM record_dismissals h WHERE h.user_id=? AND h.collection='review_queue' AND h.record_id=kind||':'||id)",(user['id'],now(),user['id'])).rowcount
        return {'deleted':count}
