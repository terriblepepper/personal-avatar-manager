# 模块说明：账户管理业务：账户检索、管理员授权和自主降级；账户角色与个人分身身份独立。
"""Administrative account discovery and transactional role transitions."""
import json
from fastapi import HTTPException
from avatar.accounts.auth import require_admin
from avatar.core.db import event, dump


# 职责：账户检索与管理员角色调整的业务服务。
# 输入：依赖数据库连接，不负责浏览器界面或密码认证。
# 结果与约束：管理操作要求管理员；角色变化保留账户资料、分身与聊天历史。
class AccountManagement:
    # 职责：保存账户管理使用的数据库依赖。
    # 输入：database：提供事务上下文的 Database 实例。
    # 结果与约束：只保存引用；不查询账户或变更权限。
    def __init__(self, database):
        self.database = database

    # 职责：按条件分页检索管理后台账户列表。
    # 输入：actor 为当前管理员；search 搜索本地/Keycloak 用户名、身份及联系方式，role 过滤角色，offset 为分页偏移。
    # 结果与约束：返回 items、total 和分页信息；转义 LIKE 通配符，每页最多 10 条，不返回密码。
    def list(self, actor, search='', role='all', offset=0):
        require_admin(actor)
        clauses, values = [], []
        if search:
            pattern = '%'+search.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
            clauses.append("(u.username LIKE ? ESCAPE '\\' OR u.email LIKE ? ESCAPE '\\' OR u.phone LIKE ? ESCAPE '\\' OR u.student_id LIKE ? ESCAPE '\\' OR json_extract(p.draft,'$.employee_id') LIKE ? ESCAPE '\\' OR json_extract(p.draft,'$.name') LIKE ? ESCAPE '\\' OR i.username LIKE ? ESCAPE '\\')")
            values.extend([pattern]*7)
        if role != 'all':
            clauses.append('u.role=?');values.append(role)
        where = ' WHERE '+' AND '.join(clauses) if clauses else ''
        join = ' FROM users u LEFT JOIN profile_records p ON p.user_id=u.id LEFT JOIN avatar_records a ON a.user_id=u.id LEFT JOIN oidc_identities i ON i.user_id=u.id'
        with self.database.connect() as db:
            total = db.execute('SELECT COUNT(*)'+join+where,values).fetchone()[0]
            rows = db.execute("SELECT u.id,u.username,u.role,u.is_root,u.person_type,u.student_id,u.teacher_id,u.account_status,u.email,u.phone,u.created_at,json_extract(p.draft,'$.name') name,json_extract(p.draft,'$.employee_id') employee_id,a.aic,i.username identity_username"+join+where+' ORDER BY u.is_root DESC,u.created_at DESC,u.id LIMIT 10 OFFSET ?',[*values,offset]).fetchall()
        return {'items':[dict(row) for row in rows],'total':total,'offset':offset,'limit':10}

    def profile(self,actor,target_id):
        """注册已通过账户的登记信息和最新审核快照，不返回未审核草稿或模型凭据。"""
        require_admin(actor)
        with self.database.connect() as db:
            db.execute('BEGIN')
            current=db.execute('SELECT role FROM users WHERE id=?',(actor['id'],)).fetchone()
            require_admin(current if current else {'role':'user'})
            account=db.execute("SELECT u.id,u.username,u.person_type,u.student_id,u.teacher_id,u.email,u.phone,u.account_status,u.created_at,i.username identity_username,COALESCE(json_extract(p.draft,'$.name'),'') name FROM users u LEFT JOIN oidc_identities i ON i.user_id=u.id LEFT JOIN profile_records p ON p.user_id=u.id WHERE u.id=?",(target_id,)).fetchone()
            if not account:raise HTTPException(404,'账户不存在')
            if account['account_status']!='active':raise HTTPException(409,'账户未激活')
            approved=db.execute("SELECT snapshot,reviewed_at FROM profile_submissions WHERE user_id=? AND status='approved' ORDER BY reviewed_at DESC,submitted_at DESC,id DESC LIMIT 1",(target_id,)).fetchone()
            if approved:profile=json.loads(approved['snapshot']);approved_at=approved['reviewed_at']
            else:
                legacy=db.execute('SELECT profile,updated_at FROM avatar_records WHERE user_id=?',(target_id,)).fetchone()
                profile=json.loads(legacy['profile']) if legacy else {};approved_at=legacy['updated_at'] if legacy else None
        profile.pop('attachments',None)
        return {'account':dict(account),'profile':profile,'approved_at':approved_at}

    # 职责：在写事务中任命管理角色或执行本人降级。
    # 输入：actor 为操作者，target_id 为目标账户 ID，role 为接口已校验的目标角色。
    # 结果与约束：事务内重新检查操作者权限；禁止降级他人或根管理员，未完成注册审核者不能升权；返回变更结果并记录事件。
    def set_role(self, actor, target_id, role):
        # 在写事务中重新检查操作者权限，防止其他请求在自主降级前
        # 已读取旧权限并在降级后继续执行授权操作。
        with self.database.connect(write=True) as db:
            current = db.execute('SELECT * FROM users WHERE id=?',(actor['id'],)).fetchone()
            require_admin(dict(current) if current else {'role':'user'})
            target = db.execute('SELECT * FROM users WHERE id=?',(target_id,)).fetchone()
            if not target:
                raise HTTPException(404,'账户不存在')
            if role == 'user':
                if target_id != actor['id']:
                    raise HTTPException(403,'管理员只能将自己降级为普通用户')
                if target['is_root']:
                    raise HTTPException(403,'根管理员 admin 保留管理权限，不能降级')
            elif role == 'super_admin':
                if (not current['is_root'] or current['account_status'] != 'active'
                        or actor.get('access_scope') != 'full' or actor.get('auth_method') != 'local'):
                    raise HTTPException(403, '只有本地根管理员可以任命超级管理员')
                if target['is_root']:
                    raise HTTPException(403, '根管理员权限保留')
            elif target['role'] == 'super_admin':
                raise HTTPException(403, '超级管理员只能由本人降级为普通用户')
            if role != 'user' and target['account_status'] != 'active':
                raise HTTPException(409,'只有已激活账户可以成为管理员')
            if target['role'] == role:
                return {'id':target_id,'role':role,'changed':False}
            db.execute('UPDATE users SET role=? WHERE id=?',(role,target_id))
            action = {'admin':'授予管理员权限','super_admin':'授予超级管理员权限','user':'管理员自主降级'}[role]
            event(db,target_id,current['username'],action,
                  dump({'username':target['username'],'from':target['role'],'to':role}), actor_id=current['id'])
        # 现有会话每次读取 users 当前角色，不使用缓存权限的 JWT，角色变化立即参与后续校验。
        return {'id':target_id,'role':role,'changed':True}
