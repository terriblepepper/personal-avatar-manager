"""Issue local, OIDC and platform sessions in the caller’s transaction."""
import hashlib
import secrets
import time

from fastapi import HTTPException


# 职责：在已有数据库事务内生成服务端登录会话。
# 输入：db 为连接，user_id 指定账户；duration 为秒数，access_scope 为 full 或 profile，auth_method 标识实际认证来源。
# 结果与约束：仅保存会话令牌哈希并标记认证来源，返回原令牌、CSRF、脱敏账户及统一账号展示名/主体；identity_verified 表示账号验签登录，profile 会话隐藏管理员身份。
def issue_session(db,user_id,duration=43200,access_scope='full',auth_method='local'):
    user=db.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
    if not user:raise HTTPException(404,'账户不存在')
    token,csrf=secrets.token_urlsafe(32),secrets.token_urlsafe(24)
    db.execute('INSERT INTO sessions(token,user_id,csrf,expires,access_scope,auth_method) VALUES(?,?,?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),user_id,csrf,time.time()+duration,access_scope,auth_method))
    result={k:user[k] for k in ('id','username','role','is_root','person_type','student_id','teacher_id','account_status','account_reason','email','phone')}
    result['onboarding_stage'] = user['onboarding_stage'] if user['role'] == 'user' else 'done'
    result['access_scope']=access_scope
    identity=db.execute('SELECT subject,username,verified_at FROM oidc_identities WHERE user_id=?',(user_id,)).fetchone()
    result['oidc_linked']=bool(identity and not user['is_root'])
    result.update(auth_source='oidc' if auth_method=='oidc' and not user['is_root'] else 'local',identity_verified=bool(identity and identity['verified_at'] and not user['is_root']),
                  oidc_subject=identity['subject'] if identity and not user['is_root'] else None,
                  identity_username=identity['username'] if identity and not user['is_root'] else '')
    # 只改变本次会话返回的有效角色，绝不改写 users 中原有管理员身份。
    if access_scope=='profile':result.update(role='user',is_root=0)
    return token,csrf,result
