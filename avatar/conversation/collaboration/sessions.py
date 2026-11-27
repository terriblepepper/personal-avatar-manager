"""Read and authorize paired conversations and their task history."""
from contextlib import contextmanager
import hashlib
import json
import time
from fastapi import HTTPException
from avatar.core.db import dump
from avatar.files.conversation.peer import workspace_expired
from avatar.conversation.tools.activity import had_successful_sandbox, without_false_tool_footer, ToolActivity
from avatar.conversation.collaboration.results import confirmed_message
from avatar.conversation.collaboration.transport import current_protocol
from avatar.conversation.collaboration.session_page import full_page
from avatar.models.embedding import EmbeddingSettings


class PeerSessions:
    def __init__(self, database, resources, transport):
        self.database = database
        self.resources = resources
        self.transport = transport

    # 双方账户和 AIC 只从业务库获取；请求正文不能指定本方或冒充另一账户。
    def participants(self,db,user_id,target_aic):
        rows=[]
        for field,value in [('user_id',user_id),('aic',target_aic)]:
            row=db.execute(f"SELECT a.*,u.username,u.account_status FROM avatar_records a JOIN users u ON u.id=a.user_id WHERE a.{field}=? AND a.identity_status='active' AND u.account_status='active'",(value,)).fetchone()
            if not row:raise HTTPException(409,'双方均需有已创建且可用的分身')
            rows.append(dict(row))
        if rows[0]['user_id']==rows[1]['user_id']:raise HTTPException(422,'请选择其他分身')
        return rows

    def preferences(self,user_id,value=None):
        with self.database.connect(write=value is not None) as db:
            if value is not None:
                if isinstance(value,bool) or not isinstance(value,int) or not 1<=value<=1000:raise HTTPException(422,'最大轮次需为 1～1000')
                db.execute('INSERT INTO agent_dialogue_preferences VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET max_rounds=excluded.max_rounds',(user_id,value))
                db.execute("UPDATE agent_dialogues SET round_limit=LEAST(round_limit,?) WHERE (user_id=? OR target_user_id=?) AND status NOT IN ('ended','failed')",(value,user_id,user_id))
            row=db.execute('SELECT max_rounds FROM agent_dialogue_preferences WHERE user_id=?',(user_id,)).fetchone()
        return {'max_rounds':row[0] if row else 10}

    @staticmethod
    def auto_accept_enabled(db,owner,peer):
        return bool(db.execute('SELECT 1 FROM agent_peer_auto_accept WHERE user_id=? AND peer_user_id=?',(owner,peer)).fetchone())

    @staticmethod
    def auto_accept_available(db,session_id):
        return bool(db.execute('SELECT 1 FROM agent_dialogues WHERE session_id=? LIMIT 1',(session_id,)).fetchone())

    def auto_accept(self,user,session_id,enabled):
        owner=user['id']
        with self.database.connect(write=True, account_scope=owner) as db:
            session=self.member(db,user,session_id,hidden=True)
            peer=session['right_user_id'] if session['left_user_id']==owner else session['left_user_id']
            if enabled:
                if not self.auto_accept_available(db,session_id):
                    raise HTTPException(409,'发送一条消息或任务后可开启自动同意')
                db.execute('INSERT INTO agent_peer_auto_accept(user_id,peer_user_id) VALUES(?,?) ON CONFLICT DO NOTHING',(owner,peer))
            else:
                db.execute('DELETE FROM agent_peer_auto_accept WHERE user_id=? AND peer_user_id=?',(owner,peer))
        return {'auto_accept':enabled}

    def member(self,db,user,identifier,hidden=False):
        row=db.execute('SELECT s.*,m.hidden,m.cleared_through,m.cleared_at,m.summary,m.summary_through FROM agent_peer_sessions s JOIN agent_session_members m ON m.session_id=s.id WHERE s.id=? AND m.user_id=?',(identifier,user['id'])).fetchone()
        if not row or row['hidden'] and not hidden:raise HTTPException(404,'分身会话不存在')
        return dict(row)

    def owned(self,db,user,identifier):
        row=db.execute('SELECT * FROM agent_dialogues WHERE id=?',(identifier,)).fetchone()
        if not row:raise HTTPException(404,'任务不存在')
        ordinary=row['delivery_mode']=='message'
        member=self.member(db,user,row['session_id'],hidden=not ordinary)
        if not db.execute('SELECT 1 FROM agent_task_members WHERE task_id=? AND user_id=? AND hidden=0',(identifier,user['id'])).fetchone():raise HTTPException(404,'本方任务记录已删除')
        if ordinary and row['status']=='ended' and member['cleared_at'] and db.execute('SELECT 1 WHERE avatar_utc_time(?)<=avatar_utc_time(?)',(row['ended_at'] or row['updated_at'],member['cleared_at'])).fetchone() and not db.execute('SELECT 1 FROM agent_dialogue_messages WHERE dialogue_id=? AND id>?',(identifier,member['cleared_through'])).fetchone():raise HTTPException(404,'本方任务记忆已清理')
        return dict(row)

    def view(self,row,user):
        with self.database.connect() as db:
            return self.views(db, [row], user)[0]

    def views(self, db, rows, user):
        if not rows:
            return []
        ids = list({row['id'] for row in rows})
        details = {r['id']: r for r in db.execute("""SELECT d.id,c.action,m.content,
            EXISTS(SELECT 1 FROM agent_intents i WHERE i.user_id=? AND i.status='confirmed'
                   AND i.document::jsonb->>'retry_of'=d.id) AS retried
            FROM agent_dialogues d LEFT JOIN agent_protocol_controls c ON c.task_id=d.id
            LEFT JOIN agent_task_memories m ON m.task_id=d.id AND m.user_id=?
            WHERE d.id=ANY(?::text[])""",(user['id'],user['id'],ids))}
        cards = self.resources.metadata_many(db, rows, user['id'])
        return [self.task_view(dict(row),user,details.get(row['id']),cards[row['id']]) for row in rows]

    def task_view(self,row,user,detail,cards):
        result={k:row[k] for k in ['id','session_id','own_aic','target_aic','objective','status','active_speaker','error','created_at','updated_at','revision','ended_at','end_reason','round_limit','rounds_done','task_kind','delivery_mode','transport_mode','waiting_reason']}
        expired=workspace_expired(row)
        result['files']=cards
        result['tool_activity']=self.visible_tools(row['tool_activity'],user['id'])
        result['active_speaker_is_own'] = (row['next_speaker'] == 0) == (row['user_id'] == user['id'])
        result.update(is_initiator=row['user_id']==user['id'],confirmed_by_me=bool(row['confirmation_acknowledged']&(1 if row['user_id']==user['id'] else 2)),expires_at=row['expires_at'] if row['delivery_mode']!='message' and row['status']=='ended' else None,
                      workspace_expired=expired,other_deleted=False)
        result['rejected']=bool(row['rejected'])
        result['sending_prepared']=confirmed_message(row,row['user_id']) is not None and row['status'] in ('queued','running')
        result['message_delivered']=bool(row['delivery_mode']=='message' and row['status']=='ended' and detail and detail['action']=='complete')
        result['skill_commands']=''
        result['excluded_skills']=[]
        result.update(self.retry_status(row,user['id'],bool(detail and detail['retried'])))
        result['returned_summary']=(detail['content'] or '') if detail else ''
        bit=1 if row['user_id']==user['id'] else 2
        result['can_accept']=row['delivery_mode']!='message' and row['status']=='awaiting_acceptance' and row['target_user_id']==user['id']
        result['needs_confirmation']=row['delivery_mode']!='message' and row['status']=='awaiting_input' and bool(row['confirmation_required']&bit)
        result['waiting_for_other']=row['status'] in ('awaiting_acceptance','awaiting_input') and not (result['can_accept'] or result['needs_confirmation'])
        if row['status']=='awaiting_input':
            checkpoint=json.loads(row['agent_checkpoint'] or '{}')
            if checkpoint and not row['confirmation_required']:
                mine=checkpoint['speaker_id']==user['id']
                question=checkpoint.get('user_question')
                result.update(can_supplement=mine,waiting_for_other=not mine)
                result['waiting_label']=('等待本人补充信息' if mine else '等待对方补充信息') if question else ('等待本人继续或结束' if mine else '等待对方处理')
                if mine and question:
                    result['user_question']=question
                elif not mine:
                    result['waiting_reason']=''
            elif row['confirmation_required']:
                result['waiting_label']='等待双方确认' if row['confirmation_required']==3 else ('等待本人确认' if result['needs_confirmation'] else '等待对方确认')
        return result

    @staticmethod
    def retry_state(db,row,user_id):
        retried=db.execute("SELECT 1 FROM agent_intents WHERE user_id=? AND status='confirmed' AND (document::jsonb->>'retry_of')=?",(user_id,row['id'])).fetchone()
        return PeerSessions.retry_status(row,user_id,bool(retried))

    @staticmethod
    def retry_status(row,user_id,retried):
        deadline=row['retry_until']
        return {'retry_until':deadline,'auto_retrying':row['status']=='awaiting_identity' or row['status']=='running' and bool(deadline),
                'can_retry':row['user_id']==user_id and row['status']=='failed' and 0<deadline<=time.time() and not retried}

    @staticmethod
    def visible_tools(raw,user_id):
        """公开工具状态，脚本输入与结果详情只向执行方本人显示。"""
        names = {group:name for group,name in ToolActivity.GROUPS.values()}
        names.update(sandbox='运行沙箱',notice='需要会话工具')
        visible = []
        for item in json.loads(raw):
            group = item.get('group')
            if group == 'agent_receipt':
                continue
            if item.get('user_id')==user_id:
                visible.append(item)
            elif group in names and (group!='notice' or item.get('status')=='required'):
                visible.append({'id':item['id'],'group':group,'name':names[group],'status':item.get('status')})
        return visible

    def get(self,user,identifier,before=None,after=None,*,task_paging=None):
        with self.database.connect() as db:row=self.owned(db,user,identifier)
        return {**self.session(user,row['session_id'],before,after,task_id=identifier,task_paging=task_paging),'task':self.view(row,user),'id':identifier}

    @contextmanager
    def workspace_transaction(self, owner, scope, shared):
        if all(shared.values()):
            with self.database.connect(write=True, account_scope=owner) as db:
                mappings = db.execute("""SELECT scope_id,conversation_id FROM agent_shared_workspaces
                    WHERE scope=? AND scope_id=ANY(?::text[]) FOR SHARE""",(scope,list(shared)))
                if dict(tuple(row) for row in mappings) == shared:
                    yield db
                    return
        # Creating a shared workspace also imports files from both participants.
        with self.database.connect(write=True) as db:
            yield db

    def session(self,user,identifier,before=None,after=None,task_id=None,*,task_paging=None):
        if before is not None and after is not None or any(v is not None and not 0<=v<=2**63-1 for v in [before,after]):raise HTTPException(422,'消息游标无效')
        with self.database.connect() as db:
            task=self.owned(db,user,task_id) if task_id else None
            separate=bool(task and task['delivery_mode']!='message')
            self.member(db,user,identifier,hidden=separate)
            table,key=('agent_task_members','task_id') if separate else ('agent_session_members','session_id')
            settings_id=db.execute(f'SELECT resource_conversation_id FROM {table} WHERE {key}=? AND user_id=?',(task_id if separate else identifier,user['id'])).fetchone()[0]
            workspace=self.resources.shared_id(db,identifier,task_id if separate else None)
        if separate and workspace_expired(task):
            workspace=settings_id=None
        elif not workspace or not settings_id and not (separate and task['status']=='ended'):
            scope, key = ('task',task_id) if separate else ('session',identifier)
            with self.workspace_transaction(user['id'],scope,{key:workspace}) as db:
                self.member(db,user,identifier,hidden=separate)
                current=self.owned(db,user,task_id) if separate else None
                if not workspace_expired(current):
                    workspace=self.resources.workspace(db,user['id'],identifier,task_id)
                    if not current or current['status']!='ended':
                        settings_id=self.resources.settings_workspace(db,user['id'],identifier,task_id)
        with self.database.connect() as db:
            pair=self.member(db,user,identifier,hidden=separate)
            snapshot={}
            if task_paging is not None:
                hidden = db.execute('''SELECT m.task_id FROM agent_task_members m JOIN agent_dialogues d ON d.id=m.task_id
                        WHERE d.session_id=? AND m.user_id=? AND m.hidden=1 ORDER BY m.task_id''',(identifier,user['id']))
                # Read the viewer's visibility revision before messages so a concurrent deletion forces a fresh tail.
                scope = [user['id'], pair['cleared_through'], pair['cleared_at'], *[r[0] for r in hidden]]
                snapshot['history_revision'] = hashlib.sha256(dump(scope).encode()).hexdigest()
            condition=' AND m.id>?' if after is not None else ' AND m.id<?' if before is not None else ''
            memory=db.execute('SELECT summary_through FROM agent_task_members WHERE task_id=? AND user_id=? AND hidden=0',(task_id,user['id'])).fetchone() if separate else pair
            through=memory['summary_through'] if memory else 0
            snapshot['summary_through']=through if separate or through>pair['cleared_through'] else 0
            params=[identifier,0 if separate else pair['cleared_through']]+([after if after is not None else before] if condition else [])
            scope=' AND d.id=?' if task_id else " AND d.delivery_mode='message'"
            if task_id:params.append(task_id)
            rows=db.execute('''SELECT m.*,d.objective AS task_title,d.user_id AS initiator_id,d.target_user_id AS recipient_id,d.own_aic AS initiator_aic,d.target_aic AS recipient_aic FROM agent_dialogue_messages m JOIN agent_dialogues d ON d.id=m.dialogue_id
               WHERE d.session_id=? AND m.id>?'''+condition+scope+''' AND NOT EXISTS(SELECT 1 FROM agent_task_members v WHERE v.task_id=d.id AND v.user_id=? AND v.hidden=1)
               ORDER BY m.id '''+('ASC' if after is not None else 'DESC')+' LIMIT 10',(*params,user['id'])).fetchall()
            tasks=db.execute("SELECT * FROM agent_dialogues d WHERE session_id=? AND NOT EXISTS(SELECT 1 FROM agent_task_members v WHERE v.task_id=d.id AND v.user_id=? AND v.hidden=1) AND (status!='ended' OR ?='' OR avatar_utc_time(COALESCE(ended_at,updated_at))>avatar_utc_time(?) OR EXISTS(SELECT 1 FROM agent_dialogue_messages WHERE dialogue_id=d.id AND id>?)) ORDER BY created_at DESC,id DESC LIMIT 10",(identifier,user['id'],pair['cleared_at'],pair['cleared_at'],pair['cleared_through'])).fetchall()
            message_ids=list({r['dialogue_id'] for r in rows})
            message_tasks=db.execute('SELECT * FROM agent_dialogues WHERE session_id=? AND id=ANY(?::text[])',(identifier,message_ids)).fetchall() if message_ids else []
            peer=pair['right_user_id'] if pair['left_user_id']==user['id'] else pair['left_user_id']
            auto_accept=self.auto_accept_enabled(db,user['id'],peer)
            auto_accept_available=self.auto_accept_available(db,identifier)
            embedding_status={'configured':EmbeddingSettings.configured(db,user['id']),'peerConfigured':EmbeddingSettings.configured(db,peer)}
            avatar=db.execute('SELECT aic,profile FROM avatar_records WHERE user_id=?',(peer,)).fetchone()
            own=db.execute('SELECT aic FROM avatar_records WHERE user_id=?',(user['id'],)).fetchone()
            acps=getattr(self.transport(),'acps',None)
            protocol=current_protocol(own['aic'],avatar['aic'],ready=bool(acps and acps.ready(user['id'],peer))) if own and avatar else ''
            chat=db.execute("SELECT * FROM agent_dialogues d WHERE session_id=? AND delivery_mode='message' AND NOT EXISTS(SELECT 1 FROM agent_task_members v WHERE v.task_id=d.id AND v.user_id=? AND v.hidden=1) AND (status!='ended' OR ?='' OR avatar_utc_time(COALESCE(ended_at,updated_at))>avatar_utc_time(?)) ORDER BY updated_at DESC,id DESC LIMIT 1",(identifier,user['id'],pair['cleared_at'],pair['cleared_at'])).fetchone()
        with self.database.connect() as db:
            messages=self.project_messages(db,rows,user,{r['id'] for r in message_tasks if workspace_expired(r)})
            all_tasks = {r['id']:r for r in [*tasks,*message_tasks,*([chat] if chat else [])]}
            projected = {t['id']:t for t in self.views(db,list(all_tasks.values()),user)}
        if after is None:messages.reverse()
        page=self.task_page(user,identifier,*(task_paging or (0,'')),from_task=separate)
        if task_paging is not None:snapshot['task_page']=page
        return {'auto_accept':auto_accept,'auto_accept_available':auto_accept_available,'current_transport_mode':protocol,'embedding_status':embedding_status,'viewer_user_id':user['id'],'session_id':identifier,'id':identifier,'resource_conversation_id':workspace,'settings_conversation_id':settings_id,'accepted':bool(pair['accepted']),'can_accept':bool(task_id and any(t['id']==task_id and t['target_user_id']==user['id'] and t['status']=='awaiting_acceptance' for t in tasks)),
                'peer_name':json.loads(avatar['profile']).get('avatar_name','分身') if avatar else '分身','peer_aic':avatar['aic'] if avatar else '',
                'messages':messages,'has_more':len(rows)==10,'chat':projected[chat['id']] if chat else None,'message_tasks':[projected[r['id']] for r in message_tasks],
                'tasks':[projected[r['id']] for r in tasks],'task_total':page['total'],**snapshot}

    def project_messages(self, db, rows, user, expired):
        authors = {}
        if any(r['speaker_role']=='user' and r['speaker_name']=='用户补充要求' for r in rows):
            users = {uid for r in rows for uid in (r['speaker_user_id'],r['initiator_id'],r['recipient_id']) if uid}
            authors = {r['id']:r['name'] or r['username'] for r in db.execute("""SELECT u.id,u.username,a.profile::jsonb->>'name' AS name
                FROM users u LEFT JOIN avatar_records a ON a.user_id=u.id WHERE u.id=ANY(?::text[])""",(list(users),))}
        messages=[]
        for r in rows:
            item=dict(r)
            legacy = item.pop('legacy_display_content')
            if legacy is not None:
                item['content'] = legacy
            if r['speaker_role']=='agent' and had_successful_sandbox(json.loads(r['tool_activity'])):
                item['content']=without_false_tool_footer(item['content'])
            author_id=r['speaker_user_id'] or (r['initiator_id'] if r['speaker_aic']==r['initiator_aic'] else r['recipient_id'] if r['speaker_aic']==r['recipient_aic'] else None)
            item.update(is_own=author_id==user['id'],turn=r['id'],usage=json.loads(r['usage']),content=self.resources.localize(db,item['content'],r['dialogue_id'],user['id'],r['id'],
                expired=r['dialogue_id'] in expired),tool_activity=self.visible_tools(r['tool_activity'],user['id']))
            if item['speaker_role']=='user' and item['speaker_name']=='用户补充要求':
                if author_id in authors:item['speaker_name']=authors[author_id]
            messages.append(item)
        return messages

    def task_page(self,user,identifier,offset=0,search='',*,from_task=False):
        if not 0<=offset<=2**63-1:raise HTTPException(422,'任务分页偏移无效')
        with self.database.connect() as db:
            member=self.member(db,user,identifier,hidden=from_task)
            clause="session_id=? AND delivery_mode!='message' AND NOT EXISTS(SELECT 1 FROM agent_task_members v WHERE v.task_id=d.id AND v.user_id=? AND v.hidden=1) AND (status!='ended' OR ?='' OR avatar_utc_time(COALESCE(ended_at,updated_at))>avatar_utc_time(?) OR EXISTS(SELECT 1 FROM agent_dialogue_messages WHERE dialogue_id=d.id AND id>?))"
            args=(identifier,user['id'],member['cleared_at'],member['cleared_at'],member['cleared_through'])
            if search:
                clause+=" AND strpos(lower(d.objective||' '||d.status),lower(?))>0";args+= (search,)
            total=db.execute('SELECT COUNT(*) FROM agent_dialogues d WHERE '+clause,args).fetchone()[0]
            offset=min(offset,((max(total,1)-1)//10)*10)
            rows=db.execute('SELECT * FROM agent_dialogues d WHERE '+clause+' ORDER BY created_at DESC,id DESC LIMIT 10 OFFSET ?',(*args,offset)).fetchall()
            items = self.views(db, rows, user)
        return {'items':items,'total':total,'offset':offset}

    def sessions(self,user,offset=0,search='',*,summary=False):
        if not 0<=offset<=2**63-1 or len(search)>200:raise HTTPException(422,'分页或搜索无效')
        with self.database.connect() as db:
            source="agent_peer_sessions s JOIN agent_session_members m ON m.session_id=s.id JOIN avatar_records a ON a.user_id=CASE WHEN s.left_user_id=m.user_id THEN s.right_user_id ELSE s.left_user_id END"
            where="m.user_id=? AND m.hidden=0";values=[user['id']]
            if search:where+=" AND strpos(lower(COALESCE((a.profile::jsonb->>'avatar_name'),'')||' '||COALESCE((a.profile::jsonb->>'name'),'')||' '||a.aic),lower(?))>0";values.append(search)
            total=db.execute('SELECT COUNT(*) FROM '+source+' WHERE '+where,values).fetchone()[0]
            offset=min(offset,((max(total,1)-1)//10)*10)
            if summary:
                page = 'SELECT s.id,s.updated_at,m.cleared_through,a.profile FROM '+source+' WHERE '+where+' ORDER BY s.updated_at DESC,s.id LIMIT 10 OFFSET ?'
                rows = db.execute('WITH page AS ('+page+''')
                    SELECT p.id,p.profile,msg.content,msg.dialogue_id,msg.id AS message_id FROM page p LEFT JOIN agent_dialogue_messages msg ON msg.id=(
                      SELECT id FROM (SELECT am.id,am.speaker_role FROM agent_dialogue_messages am JOIN agent_dialogues d ON d.id=am.dialogue_id
                        WHERE d.session_id=p.id AND d.delivery_mode='message' AND am.id>p.cleared_through AND NOT EXISTS(
                          SELECT 1 FROM agent_task_members tm WHERE tm.user_id=? AND tm.task_id=d.id AND tm.hidden=1)
                        ORDER BY am.id DESC LIMIT 10) recent WHERE speaker_role='agent' ORDER BY id DESC LIMIT 1)
                    ORDER BY p.updated_at DESC,p.id''', (*values,offset,user['id'])).fetchall()
                items = []
                for row in rows:
                    previews = []
                    if row['dialogue_id']:
                        content = self.resources.localize(db,row['content'],row['dialogue_id'],user['id'],row['message_id'])
                        previews.append({'speaker_role':'agent','content':content})
                    items.append({'session_id':row['id'],'peer_name':json.loads(row['profile']).get('avatar_name','分身'),'messages':previews})
                return {'items':items,'total':total,'offset':offset}
            rows=db.execute('SELECT s.id FROM '+source+' WHERE '+where+' ORDER BY s.updated_at DESC,s.id LIMIT 10 OFFSET ?',(*values,offset)).fetchall()
            items = full_page(self,db,user,[r['id'] for r in rows])
        missing = [item for item in items if not item['resource_conversation_id'] or not item['settings_conversation_id']]
        if missing:
            shared = {item['id']: item['resource_conversation_id'] for item in missing}
            with self.workspace_transaction(user['id'],'session',shared) as db:
                for item in missing:
                    self.member(db,user,item['id'])
                    item['resource_conversation_id'] = self.resources.workspace(db,user['id'],item['id'])
                    item['settings_conversation_id'] = self.resources.settings_workspace(db,user['id'],item['id'])
        return {'items':items,'total':total,'offset':offset}

    def list(self,user,offset=0):
        return self.sessions(user,offset)['items']
