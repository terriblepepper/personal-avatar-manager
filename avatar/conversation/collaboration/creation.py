"""Create a paired task and atomically confirm or reopen its submission."""
from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
import json
from types import SimpleNamespace
import uuid

from fastapi import HTTPException

from avatar.core.db import dump, now
from avatar.conversation.collaboration.inbox import notify
from avatar.models.embedding import EmbeddingSettings


@dataclass(frozen=True)
class PeerRequest:
    scope: str
    key: str
    fingerprint: str

    @classmethod
    def from_body(cls, scope, key, body):
        if isinstance(body, SimpleNamespace):
            payload = {'max_rounds':None, 'kind':'chat', 'file_ids':[], **vars(body)}
            payload.pop('request_id', None)
        else:
            payload = body.model_dump(exclude={'request_id'})
        if payload.get('question_id') is None:
            payload.pop('question_id', None)
        if payload.get('expected_revision') is None:
            payload.pop('expected_revision', None)
        if payload.get('steer') is False:
            payload.pop('steer')
        raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        return cls(scope, key, hashlib.sha256(raw.encode()).hexdigest())

    def remember(self, db, user, task_id):
        db.execute('INSERT INTO agent_request_receipts(user_id,scope,request_id,fingerprint,task_id) VALUES(?,?,?,?,?)',
                   (user['id'], self.scope, self.key, self.fingerprint, task_id))


def check_capacity(db, owner):
    # The caller holds the schema-wide write transaction through insertion and receipt creation.
    counts = db.execute("""SELECT count(*), count(*) FILTER (WHERE user_id=?) FROM agent_dialogues
        WHERE status NOT IN ('ended','failed')""", (owner,)).fetchone()
    if counts[1] >= 8:
        raise HTTPException(429,'本人已有 8 个进行中或待接收的分身任务，请先处理或结束已有任务')
    if counts[0] >= 64:
        raise HTTPException(429,'分身任务队列已满，请先处理或结束已有任务')


# 配对唯一约束覆盖正反两个方向；同一对分身只能有一个长期会话，任务可排队多个。
def create_dialogue(dialogues,user,body,source_conversation_id=None,db=None,request=None,*,prepared=False):
    request = request or PeerRequest.from_body('create', body.request_id, body)
    with nullcontext(db) if db is not None else dialogues.database.connect(write=True) as db:
        previous = db.execute('SELECT id FROM agent_dialogues WHERE user_id=? AND request_id=?',
                              (user['id'], body.request_id)).fetchone()
        replayed = dialogues.replay(db, user, request, previous[0] if previous else None)
        if replayed:
            return replayed
        mode=getattr(body,'delivery_mode','negotiation')
        objective = body.objective
        if prepared:
            if not (objective+body.shared_content).strip():
                raise HTTPException(422,'请补充要发送的内容')
        elif not objective.strip():
            raise HTTPException(422,'请补充要发送的内容或任务')
        own,target=dialogues.session_store.participants(db,user['id'],body.target_aic)
        check_capacity(db,user['id'])
        left,right=sorted((own['user_id'],target['user_id']))
        pair=db.execute('SELECT * FROM agent_peer_sessions WHERE left_user_id=? AND right_user_id=?',(left,right)).fetchone()
        sid=pair['id'] if pair else str(uuid.uuid4());stamp=now()
        if not pair:db.execute('INSERT INTO agent_peer_sessions VALUES(?,?,?,?,0,?,?)',(sid,left,right,user['id'],stamp,stamp))
        for uid in (left,right):db.execute('INSERT OR IGNORE INTO agent_session_members(session_id,user_id) VALUES(?,?)',(sid,uid))
        db.execute('UPDATE agent_session_members SET hidden=0 WHERE session_id=?',(sid,))
        if source_conversation_id:
            source=dialogues.chat.chat_files.session(db,user['id'],source_conversation_id)
            if source['channel']!='peer':
                db.execute('UPDATE agent_session_members SET source_conversation_id=? WHERE session_id=? AND user_id=?',(source_conversation_id,sid,user['id']))
        identifier=str(uuid.uuid4())
        limits={r['user_id']:r['max_rounds'] for r in db.execute('SELECT * FROM agent_dialogue_preferences WHERE user_id IN (?,?)',(left,right))}
        limit=min(getattr(body,'max_rounds',None) or limits.get(user['id'],10),limits.get(target['user_id'],10))
        kind=getattr(body,'kind','chat');shared=getattr(body,'shared_content','')
        db.execute('''INSERT INTO agent_dialogues(id,user_id,own_aic,target_user_id,target_aic,objective,request_id,created_at,updated_at,status,session_id,round_limit,task_kind,shared_content,initiator_skills,source_conversation_id)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(identifier,user['id'],own['aic'],target['user_id'],target['aic'],objective,body.request_id,stamp,stamp,'queued' if mode=='message' else 'awaiting_acceptance',sid,limit,kind,shared,'',source_conversation_id))
        db.execute('UPDATE agent_dialogues SET delivery_mode=?,next_speaker=0,accepted=?,message_prepared=? WHERE id=?',(mode,int(mode=='message'),int(prepared),identifier))
        for uid in (left,right):db.execute('INSERT INTO agent_task_members(task_id,user_id) VALUES(?,?)',(identifier,uid))
        draft_workspace=getattr(body,'resource_conversation_id',None)
        if draft_workspace and mode!='message':
            if draft_workspace!=source_conversation_id:raise HTTPException(422,'任务草稿工作区不匹配')
            taken=db.execute("""SELECT 1 FROM agent_task_members WHERE resource_conversation_id=?
                UNION ALL SELECT 1 FROM agent_session_members WHERE resource_conversation_id=? LIMIT 1""",
                (draft_workspace,draft_workspace)).fetchone()
            private_draft=db.execute("SELECT 1 FROM chat_conversations WHERE id=? AND user_id=? AND channel='peer'",
                                     (draft_workspace,user['id'])).fetchone()
            if private_draft and not taken:
                db.execute('UPDATE agent_task_members SET resource_conversation_id=? WHERE task_id=? AND user_id=?',(draft_workspace,identifier,user['id']))
                dialogues.resources.sync_task_expiry(db,identifier)
        proposal_id=None
        if prepared and mode!='message':
            proposal_id=db.execute('''INSERT INTO agent_dialogue_messages
                (dialogue_id,turn,speaker_aic,speaker_name,speaker_user_id,content,usage,created_at,state,task_action)
                VALUES(?,1,?,?,?,?,'null',?,'awaiting_acceptance','send_message')''',
                (identifier,own['aic'],json.loads(own['profile']).get('avatar_name','分身'),user['id'],objective+shared,stamp)).lastrowid
        elif shared and mode!='message':
            db.execute("INSERT INTO agent_dialogue_messages(dialogue_id,turn,speaker_aic,speaker_name,speaker_role,content,usage,created_at) VALUES(?,1,?,'用户授权共享资料','user',?,'null',?)",(identifier,own['aic'],'任务：'+objective+'\n\n'+shared,stamp))
        auto_accept = mode!='message' and dialogues.session_store.auto_accept_enabled(db,target['user_id'],own['user_id'])
        if mode!='message' and not auto_accept:
            notify(db,target['user_id'],'request',sid,identifier,'收到分身任务请求',objective,'request:'+identifier)
        db.execute('UPDATE agent_peer_sessions SET updated_at=? WHERE id=?',(stamp,sid))
        file_ids=getattr(body,'file_ids',[])
        file_source=source_conversation_id or (dialogues.resources.workspace(db,user['id'],sid) if file_ids else None)
        dialogues.resources.share(db,user,identifier,file_ids,file_source,attachments=request.scope=='intent')
        if proposal_id is not None and file_ids:
            db.execute('UPDATE agent_task_files SET message_id=? WHERE task_id=? AND source_user_id=? AND message_id IS NULL',
                       (proposal_id,identifier,user['id']))
        if auto_accept:
            dialogues.accept({'id':target['user_id']},sid,identifier,transaction=db)
            notify(db,target['user_id'],'accepted',sid,identifier,'已自动同意分身任务',objective,'auto-accept:'+identifier)
        request.remember(db, user, identifier)
    return identifier


def confirm_submission(dialogues, user, identifier, body, request):
    with dialogues.database.connect(write=True) as db:
        row=dialogues.intent.load(db,user,identifier)
        task_id = dialogues.replay(db, user, request, row['task_id'])
        if not task_id:
            dialogues.intent.check_pending(row,'editing')
            saved=json.loads(row['document'])
            if saved.get('delivery_mode')=='negotiation' and not (body.objective+body.shared_content).strip():
                raise HTTPException(422,'请补充任务要求')
            if saved.get('retry_of'):check_retry(dialogues,db,user,saved['retry_of'])
            # 用户在表单内可搜索并选择任何当前公开且可用的其他分身，模型不授予身份权限。
            if body.target_aic not in {t['aic'] for t in dialogues.targets(user,db=db)}:raise HTTPException(422,'目标分身已不可用，请重新搜索选择')
            task=SimpleNamespace(target_aic=body.target_aic,objective=body.objective,request_id='intent:'+identifier,
                kind='booking' if saved.get('requires_confirmation') else 'chat',
                delivery_mode=saved['delivery_mode'],shared_content=body.shared_content if body.share_content else '',max_rounds=None,file_ids=getattr(body,'file_ids',[]),resource_conversation_id=row['conversation_id'] if saved.get('origin')=='peer_task_draft' else None)
            task_id=dialogues.create(user,task,row['conversation_id'],db=db,request=request,prepared=True)
            saved.update(objective=body.objective,target_aic=body.target_aic,shared_content=task.shared_content)
            db.execute("UPDATE agent_intents SET status='confirmed',task_id=?,document=? WHERE id=?",(task_id,dump(saved),identifier))
            dialogues.intent.record_result(db,row,'已确认'+('发送消息' if task.delivery_mode=='message' else '发起任务')+'，请求已进入通信队列。实际送达与失败原因请查看分身会话；尚不能视为已送达。',task_event={'kind':'start','task_id':task_id,'title':body.objective,'mode':task.delivery_mode,'state':'submitted','reason':'请求已提交，实际进度请查看对应会话。'})
    return dialogues.session_store.get(user,task_id)


def check_retry(dialogues,db,user,identifier):
    row=dialogues.session_store.owned(db,user,identifier)
    if row['user_id']!=user['id']:raise HTTPException(403,'只有原请求发起人可以重新编辑发送')
    if not dialogues.session_store.retry_state(db,row,user['id'])['can_retry']:raise HTTPException(409,'请求仍在处理、尚未超时或已重新提交，请查看最新状态')
    return row

def retry_draft(dialogues,user,identifier,request_id):
    """仅超时终态可重新编辑；打开或取消表单均不调用模型、不发送。"""
    intent_id=str(uuid.uuid5(uuid.NAMESPACE_URL,user['id']+':retry:'+identifier+':'+request_id))
    with dialogues.database.connect(write=True) as db:
        task=check_retry(dialogues,db,user,identifier)
        previous=db.execute("SELECT * FROM agent_intents WHERE user_id=? AND status='editing' AND (document::jsonb->>'retry_of')=? ORDER BY created_at DESC LIMIT 1",(user['id'],identifier)).fetchone()
        if previous:
            try:dialogues.intent.check_pending(previous,'editing')
            except HTTPException:previous=None
        if previous:intent_id=previous['id']
        else:
            if db.execute('SELECT 1 FROM agent_intents WHERE id=?',(intent_id,)).fetchone():raise HTTPException(409,'该重发草稿已取消或过期，请重新打开')
            workspace=dialogues.resources.workspace(db,user['id'],task['session_id'],identifier)
            avatar=db.execute('SELECT aic,profile FROM avatar_records WHERE user_id=?',(task['target_user_id'],)).fetchone()
            if not avatar:raise HTTPException(409,'目标分身不可用')
            files=[r[0] for r in db.execute("SELECT f.id FROM chat_files f JOIN agent_task_files a ON a.file_id=f.id WHERE a.task_id=? AND a.source_user_id=? AND f.conversation_id=? AND f.referenced=1 AND (f.expires_at IS NULL OR avatar_utc_time(f.expires_at)>CURRENT_TIMESTAMP) ORDER BY f.created_at,f.id LIMIT 5",(identifier,user['id'],workspace))]
            document={'target_query':json.loads(avatar['profile']).get('avatar_name',''),'target_aic':avatar['aic'],
                      'objective':task['objective'],'shared_content':task['shared_content'],'skills':'',
                      'requires_confirmation':task['task_kind']=='booking','delivery_mode':task['delivery_mode'],
                      'origin':'peer_task_draft','session_id':task['session_id'],'retry_of':identifier,'file_ids':files}
            if task['message_prepared'] or db.execute("SELECT 1 FROM agent_request_receipts WHERE task_id=? AND scope='intent' LIMIT 1",(identifier,)).fetchone():
                document.update(objective=task['objective']+task['shared_content'],shared_content='')
            db.execute("INSERT INTO agent_intents(id,user_id,conversation_id,document,created_at,status) VALUES(?,?,?,?,?,'editing')",(intent_id,user['id'],workspace,dump(document),now()))
    return dialogues.intent.get(user,intent_id)

def task_draft(dialogues,user,identifier,body):
    """用户主动创建任务时复用通信确认表单；保存草稿不会投递或调用模型。"""
    intent_id=str(uuid.uuid5(uuid.NAMESPACE_URL,user['id']+':task-draft:'+body.request_id))
    with dialogues.database.connect(write=True, account_scope=user['id']) as db:
        pair=dialogues.session_store.member(db,user,identifier)
        peer=pair['right_user_id'] if pair['left_user_id']==user['id'] else pair['left_user_id']
        avatar=db.execute('SELECT aic,profile FROM avatar_records WHERE user_id=?',(peer,)).fetchone()
        if not avatar:raise HTTPException(409,'目标分身不可用')
        embedding_status={'configured':EmbeddingSettings.configured(db,user['id']),'peerConfigured':EmbeddingSettings.configured(db,peer)}
        previous=db.execute('SELECT * FROM agent_intents WHERE id=? AND user_id=?',(intent_id,user['id'])).fetchone()
        if previous:
            dialogues.intent.check_pending(previous,'editing')
            saved=json.loads(previous['document'])
            if saved.get('session_id')!=identifier:raise HTTPException(409,'草稿属于另一会话')
            workspace=previous['conversation_id']
        else:workspace=dialogues.resources.new_workspace(db,user['id'],'分身任务草稿')
        objective=body.message
        document={'target_query':json.loads(avatar['profile']).get('avatar_name',''),'target_aic':avatar['aic'],
                  'objective':objective,'shared_content':'','requires_confirmation':False,'skills':'','delivery_mode':'negotiation',
                  'origin':'peer_task_draft','session_id':identifier}
        if previous:
            db.execute('UPDATE agent_intents SET document=? WHERE id=?',(dump(document),intent_id))
        else:db.execute("INSERT INTO agent_intents(id,user_id,conversation_id,document,created_at,status) VALUES(?,?,?,?,?,'editing')",(intent_id,user['id'],workspace,dump(document),now()))
    return {**dialogues.intent.get(user,intent_id),'embedding_status':embedding_status}
