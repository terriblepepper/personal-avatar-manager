"""个人动态按稳定操作者隔离；管理员另接收仍待处理的新申请。"""

REVIEW_REQUEST = """(
 (e.action='soul 构建完成，等待审核' AND EXISTS (SELECT 1 FROM soul_builds b WHERE b.id=e.detail AND b.user_id=e.user_id AND b.status='pending')) OR
 (e.action IN ('提交创建审核','提交更新审核') AND EXISTS (
   SELECT 1 FROM profile_submissions s WHERE s.user_id=e.user_id AND s.status='pending'
   AND e.detail='资料修改于 '||s.last_modified_at))
 OR (e.action IN ('提交教师注册审核','重新提交教师注册审核','选择资料身份','账户注册') AND EXISTS (
   SELECT 1 FROM users u WHERE u.id=e.user_id AND u.person_type='teacher' AND u.account_status='pending')
   AND e.id=(SELECT MAX(t.id) FROM audit_events t WHERE t.user_id=e.user_id
     AND t.action IN ('提交教师注册审核','重新提交教师注册审核','选择资料身份','账户注册')))
)"""


def event_scope(user_id, admin):
    if not admin:return 'e.user_id=?', (user_id,)
    return "((e.action='统一认证登录' AND e.user_id=?) OR (e.action!='统一认证登录' AND (e.actor_id=? OR (e.actor_id IS NULL AND e.user_id=? AND e.actor IN ('system','keycloak','demo-recovery','verified-platform')) OR "+REVIEW_REQUEST+')))', (user_id,user_id,user_id)



def notification_scope(user_id, admin):
    scope, values=event_scope(user_id,admin)
    return "(kind!='activity' OR EXISTS (SELECT 1 FROM audit_events e WHERE dedupe_key='event:'||e.id AND "+scope+'))', values
