"""独立消息收件箱；已读和删除只改变本人消息条目，不删除会话、任务或对方数据。"""
import json
from contextlib import contextmanager
from fastapi import HTTPException
from avatar.accounts.roles import ADMIN_ROLES
from avatar.core.db import now
from avatar.core.activity_visibility import event_scope,notification_scope,REVIEW_REQUEST


# 旧版逐条发言/暂停提醒也从收件箱隐藏；保留会话原文与单独发送消息的收件提醒。
PEER_NOTIFICATION_VISIBILITY="NOT (kind IN ('message','paused') AND EXISTS (SELECT 1 FROM agent_dialogues d WHERE d.id=agent_notifications.task_id AND d.delivery_mode!='message'))"


SOUL_COMPLETIONS=('soul 构建完成','soul 草案等待本人确认','soul 构建完成，等待审核')
SOUL_NOTIFICATION_VISIBILITY="(kind!='activity' OR (strpos(lower(title),'soul')=0 AND strpos(title,'知识库')=0) OR title IN ('开始构建 soul','提交 soul 构建','soul 构建完成','soul 草案等待本人确认','soul 构建完成，等待审核'))"


# 页面标识来自固定业务映射，不允许审计文本提供外部链接或任意导航目标。
def activity_page(action,management):
    if not management and ('soul' in action or '知识库' in action):return 'knowledge'
    rules=(('教师注册','accounts'),('权限','users'),('审核','reviews'),('任务','jobs'),('身份','agents'),('分身','agents'),('平台 URL','settings')) if management else (
        ('审核','history'),('申请','history'),('模型','models'),('附件','files'),('资料文件','files'),('资料','profile'),('分身','avatar'),('账户','account'),('认证','account'))
    return next((page for word,page in rules if word in action),'')


# 只在原业务事务中写通知，以幂等键防止任务重试产生重复提醒。
def notify(db,user_id,kind,session_id,task_id,title,content,key):
    db.execute('INSERT OR IGNORE INTO agent_notifications(user_id,kind,session_id,task_id,title,content,created_at,dedupe_key,category) VALUES(?,?,?,?,?,?,?,?,?)',
               (user_id,kind,session_id,task_id,title[:160],content[:1000],now(),key,'request' if kind=='request' else 'chat'))


# 从审计增量生成个人动态；角色变化时重新投影，幂等键保留已读/删除状态，降权立即隐藏管理动态。
def sync_activity(db,user_id,*,check_only=False):
    state = db.execute("""
        SELECT COALESCE((SELECT role IN (?,?) FROM users WHERE id=?),false) AS is_admin,
               c.admin AS cursor_admin,c.last_event,h.upper_event
        FROM (SELECT COALESCE(MAX(id),0) AS upper_event FROM audit_events) h
        LEFT JOIN agent_activity_cursors c ON c.user_id=?
    """, (*ADMIN_ROLES, user_id, user_id)).fetchone()
    admin, upper = state['is_admin'], state['upper_event']
    same_role = state['cursor_admin'] is not None and bool(state['cursor_admin']) == admin
    last = state['last_event'] if same_role else 0
    if same_role and upper == last:return admin
    if check_only:return None
    scope,values=event_scope(user_id,admin)
    rows=db.execute('SELECT e.*,'+REVIEW_REQUEST+' AS processing_request FROM audit_events e WHERE e.id>? AND e.id<=? AND '+scope+' ORDER BY e.id',(last,upper,*values)).fetchall()
    for row in rows:
        action=row['action'];management=row['user_id']!=user_id
        if action in ('上传资料附件','上传知识库素材'):
            try:
                if isinstance(json.loads(row['detail']),dict) and json.loads(row['detail']).get('upload_batch_id'):continue
            except (ValueError,TypeError):pass
        title=action
        if 'soul' in action.lower() or '知识库' in action:
            if admin and management and row['processing_request']:pass
            elif action in ('开始构建 soul','提交 soul 构建'):title='开始构建 soul'
            elif action in SOUL_COMPLETIONS:
                if db.execute("SELECT 1 FROM agent_notifications n JOIN audit_events e ON n.dedupe_key='event:'||e.id WHERE n.user_id=? AND e.user_id=? AND e.detail=? AND e.action IN (?,?,?)",(user_id,row['user_id'],row['detail'],*SOUL_COMPLETIONS)).fetchone():continue
                title='soul 构建完成'
            else:continue
        category='notification' if any(word in action for word in ('通知','审核','失败','权限','维护','登录')) else 'activity'
        if admin and management and row['processing_request']:category='request'
        page='reviews' if category=='request' else activity_page(action,management)
        content=row['actor']+' · '+row['detail']
        if action=='统一认证登录':
            try:
                device=json.loads(row['detail'])
                if isinstance(device,dict) and all(key in device for key in ('os','webview','ip')):
                    content='新设备登录提醒\nOS：'+str(device['os'])+'\n浏览器：'+str(device.get('browser','未知'))+'\nWebView / 引擎：'+str(device['webview'])+'\nIP：'+str(device['ip'])
            except (ValueError,TypeError):pass
        db.execute('''INSERT OR IGNORE INTO agent_notifications
          (user_id,kind,title,content,created_at,dedupe_key,category,target_page,admin_only)
          VALUES(?,'activity',?,?,?,?,?,?,?)''',
          (user_id,title,content,row['created_at'],'event:'+str(row['id']),category,page,int(management)))
    db.execute('INSERT INTO agent_activity_cursors VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET last_event=excluded.last_event,admin=excluded.admin',(user_id,upper,int(admin)))
    return admin


def visible_notifications(user_id, admin):
    activity, values = notification_scope(user_id, admin)
    condition = ('user_id=? AND deleted_at IS NULL AND ' + PEER_NOTIFICATION_VISIBILITY
                 + ' AND ' + SOUL_NOTIFICATION_VISIBILITY + ' AND ' + activity)
    if not admin:
        condition += ' AND admin_only=0'
    return condition, (user_id, *values)


def count_unread(db, condition, values):
    return db.execute('SELECT COUNT(*) FROM agent_notifications WHERE ' + condition
                      + ' AND read_at IS NULL', values).fetchone()[0]


# 本人可分页读、标记或隐藏通知；管理员身份不授予其他人的收件箱权限。
class AgentInbox:
    def __init__(self,database):self.database=database

    @contextmanager
    def _synced_inbox(self, user_id):
        with self.database.connect() as db:
            # 角色、动态游标和可见消息必须来自同一快照。
            db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            admin = sync_activity(db, user_id, check_only=True)
            if admin is not None:
                yield db, admin
                return
        with self.database.connect(write=True) as db:
            yield db, sync_activity(db, user_id)

    def unread_count(self, user_id):
        with self._synced_inbox(user_id) as (db, admin):
            return count_unread(db, *visible_notifications(user_id, admin))

    def list(self,user_id,offset=0,unread=False,status='all',category='all',search=''):
        if not 0<=offset<=2**63-1:raise HTTPException(422,'分页偏移无效')
        if status not in ('all','read','unread'):raise HTTPException(422,'消息筛选无效')
        if category not in ('all','chat','request','notification','activity') or len(search)>200:raise HTTPException(422,'消息分类或搜索无效')
        with self._synced_inbox(user_id) as (db, admin):
            base,base_values=visible_notifications(user_id,admin)
            condition=' AND read_at IS NULL' if unread or status=='unread' else ' AND read_at IS NOT NULL' if status=='read' else ''
            values=list(base_values)
            if category!='all':condition+=' AND category=?';values.append(category)
            if search:condition+=" AND strpos(lower(title||' '||content),lower(?))>0";values.append(search)
            if condition:
                count=count_unread(db,base,base_values)
                total=count if condition==' AND read_at IS NULL' else db.execute('SELECT COUNT(*) FROM agent_notifications WHERE '+base+condition,values).fetchone()[0]
            else:
                total,count=db.execute('SELECT COUNT(*),COUNT(*) FILTER (WHERE read_at IS NULL) FROM agent_notifications WHERE '+base,base_values).fetchone()
            offset=min(offset,((max(total,1)-1)//10)*10)
            rows=db.execute("""SELECT id,kind,category,target_page,admin_only,session_id,task_id,title,content,created_at,read_at,
                CASE WHEN kind='request' THEN (
                    SELECT CASE WHEN d.rejected=1 THEN 'rejected' WHEN d.accepted=1 THEN 'accepted'
                        WHEN d.status='awaiting_acceptance' THEN 'pending' END
                    FROM agent_dialogues d JOIN agent_task_members m ON m.task_id=d.id AND m.user_id=d.target_user_id
                    WHERE d.id=agent_notifications.task_id AND d.target_user_id=agent_notifications.user_id
                      AND d.delivery_mode<>'message' AND m.hidden=0
                ) END AS task_request_state
                FROM agent_notifications WHERE """+base+condition+
                ' ORDER BY avatar_utc_time(created_at) DESC,id DESC LIMIT 10 OFFSET ?',(*values,offset)).fetchall()
        return {'items':[dict(r) for r in rows],'total':total,'unread':count,'offset':offset,'limit':10}

    def change(self,user_id,identifier,action):
        self.change_many(user_id,[identifier],action)
        return {'updated':True}

    # 批量操作先核验所有条目归属；混入他人或已删除消息时整批拒绝，不产生部分修改。
    def change_many(self,user_id,identifiers,action):
        if action not in ('read','unread','delete'):raise HTTPException(422,'消息操作无效')
        identifiers=list(dict.fromkeys(identifiers))
        if not 1<=len(identifiers)<=100 or any(type(i) is not int or i<=0 for i in identifiers):raise HTTPException(422,'请选择 1～100 条有效消息')
        slots=','.join('?' for _ in identifiers)
        with self.database.connect(write=True) as db:
            admin=bool(db.execute("SELECT role IN (?,?) FROM users WHERE id=?",(*ADMIN_ROLES,user_id)).fetchone()[0])
            visible,values=visible_notifications(user_id,admin)
            if db.execute(f'SELECT COUNT(*) FROM agent_notifications WHERE {visible} AND id IN ({slots})',(*values,*identifiers)).fetchone()[0]!=len(identifiers):raise HTTPException(404,'消息不存在或无权操作')
            field='deleted_at' if action=='delete' else 'read_at'
            db.execute(f'UPDATE agent_notifications SET {field}=? WHERE user_id=? AND id IN ({slots})',(None if action=='unread' else now(),user_id,*identifiers))
        return {'updated':len(identifiers)}


    def delete_all(self,user_id):
        with self.database.connect(write=True) as db:
            admin=sync_activity(db,user_id)
            visible,values=visible_notifications(user_id,admin)
            count=db.execute('UPDATE agent_notifications SET deleted_at=? WHERE '+visible,(now(),*values)).rowcount
        return {'deleted':count}
