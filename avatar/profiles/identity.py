# 模块说明：资料身份选择与教师注册审核独立于 Keycloak 登录和管理员权限；只在资料保存事务内更新。
from fastapi import HTTPException
from avatar.core.db import event


# 职责：按本人选择同步学生/教师资料身份，并维护教师审核与可修改的资料编号。
# 输入：db 为资料保存的写事务，user 为已登录本人，profile 为校验后的完整资料。
# 结果与约束：不变更 user_id、统一身份、管理员角色或生效分身；教师首次选择进入审核，旧审核结论保留，编号仅为资料，不用于身份绑定。
def save_identity(db, user, profile):
    account = db.execute('SELECT * FROM users WHERE id=?', (user['id'],)).fetchone()
    if not account:
        raise HTTPException(404, '账户不存在')
    if profile.person_type == 'teacher' and profile.student_id:
        raise HTTPException(422, '教师资料没有学号字段')
    if profile.person_type == 'student' and profile.employee_id:
        raise HTTPException(422, '学生资料没有教师工号字段')
    if profile.person_type == 'student':
        db.execute('UPDATE users SET student_id=? WHERE id=?', (profile.student_id or None, user['id']))
    if profile.person_type == account['person_type']:
        return
    status, reason, teacher_id = 'active', '', account['teacher_id']
    if profile.person_type == 'teacher':
        review = db.execute('SELECT approved,reason FROM account_reviews WHERE user_id=? ORDER BY created_at DESC,id DESC LIMIT 1', (user['id'],)).fetchone()
        status = 'active' if review and review['approved'] else 'rejected' if review else 'pending'
        reason = review['reason'] if review and not review['approved'] else ''
    db.execute('UPDATE users SET person_type=?,teacher_id=?,account_status=?,account_reason=? WHERE id=?',
               (profile.person_type, teacher_id, status, reason, user['id']))
    event(db, user['id'], user['username'], '选择资料身份', account['person_type'] + ' → ' + profile.person_type, actor_id=user['id'])
