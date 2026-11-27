"""Assistant navigation and user-authorized cross-avatar contact drafts."""
from datetime import datetime, timezone, timedelta
import json
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from avatar.conversation.avatar_assistant import CatalogArguments, LIST_AVATARS, QueryArguments, normalized
from avatar.core.db import dump, now
from avatar.core.execution import call_with_retry, recoverable, run_blocking, authorization
from avatar.core.text_budget import AgentText
from avatar.models.context_budget import fit_turn
from avatar.models.effort import with_chat_effort
from avatar.models.embedding import EmbeddingSettings
from avatar.models.model_config import require_complete


class ContactArguments(BaseModel):
    """通信工具只能提供搜索词及草稿，不能指定账户、地址或直接授权发送。"""
    model_config=ConfigDict(extra='forbid', str_strip_whitespace=True)
    target_query: str=Field(default='', max_length=200)
    content: AgentText=Field(min_length=1, description='填写可直接发给收件人的消息正文或任务要求，用户确认后投递。')
    shared_content: str=Field(default='', max_length=60000)
    file_ids:list[str]=Field(default_factory=list,max_length=5,description='仅选择当前用户明确要求发送的文件，从可用文件目录复制 ID；无明确文件要求留空，不继承历史引用。')
    requires_owner_confirmation: StrictBool=False


def declaration(name, description, parameters):
    """将领域输入约束转换为模型可见的函数工具声明。"""
    return {'name': name, 'description': description, 'parameters': parameters}


CONTACT_REPLY='需要调用分身通信。请选择发送消息或任务，也可以取消；确认发送前不会联系对方。'

CONTACT_AVATAR = declaration(
    'contact_avatar',
    '为本轮明确要求联系的其他分身写可编辑草稿。只选用户明确要发送的原件。涉及预约、交易或承诺须标记本人确认。用户确认前不发送。',
    ContactArguments.model_json_schema(),
)
_NAVIGATION = [
    LIST_AVATARS,
    declaration('application_status', '查询本人申请状态。', {'type': 'object', 'properties': {}, 'additionalProperties': False}),
    declaration('select_avatar', '用户明确要求切换到指定分身直接提问时使用。', QueryArguments.model_json_schema()),
    declaration('return_to_self', '用户明确要求返回本人分身时使用。', {'type': 'object', 'properties': {}, 'additionalProperties': False}),
]


class DialogueIntent:
    """Select an assistant responder or prepare a contact draft for the user."""
    def __init__(self, dialogues):
        self.dialogues = dialogues

    def search(self, user, query='', offset=0):
        """关键词仅用于用户可编辑的目录搜索；候选全部来自实时公开目录。"""
        if len(query)>200 or not 0<=offset<=2**63-1:
            raise HTTPException(422, '分身搜索参数无效')
        words=normalized(query).split()
        rows=[]
        for t in self.dialogues.chat.assistant_router.catalog():
            if t['row']['user_id']==user['id'] or t['row']['identity_status']!='active':
                continue
            haystack=' '.join([normalized(t['name']), normalized(t['aic']), *t['aliases']])
            if all(word in haystack for word in words):
                rows.append({'aic':t['aic'], 'name':t['name']})
        total=len(rows);offset=min(offset,((max(total,1)-1)//10)*10)
        return {'items':rows[offset:offset+10], 'total':total, 'offset':offset, 'search':query}

    async def model_selection(self, uid, conversation_id, config, key, system, history, message):
        usage = None
        try:
            result = await self.dialogues.chat.model_client.generate(config,key,system,history,message)
            usage = result.usage
            return result
        except HTTPException as error:
            usage = getattr(error,'model_usage',None)
            raise
        finally:
            def record_usage():
                with self.dialogues.database.connect(write=True) as db:
                    db.execute('INSERT INTO model_calls(user_id,provider,model,usage,created_at,purpose,caller_id,conversation_id) VALUES(?,?,?,?,?,?,?,?)',
                               (uid,config['provider'],config['model'],dump(usage),now(),'agent_intent',uid,conversation_id))
            await run_blocking(record_usage)

    async def navigate(self, user, message, parent):
        """The assistant can change responders. A personal conversation already has its responder."""
        service = self.dialogues.chat.service
        uid = user['id']

        def load():
            with self.dialogues.database.connect() as db:
                if not db.execute("SELECT 1 FROM avatar_records WHERE user_id=? AND identity_status='active'", (uid,)).fetchone():
                    return None, []
                rows = db.execute("""SELECT role,content FROM chat_messages WHERE user_id=? AND
                    (conversation_id=? OR conversation_id IN (SELECT id FROM chat_conversations WHERE parent_id=? AND user_id=?))
                    ORDER BY id DESC LIMIT 12""", (uid, parent['id'], parent['id'], uid)).fetchall()
                return service.models.current(db, uid), [dict(row) for row in reversed(rows)]

        config, history = await run_blocking(load)
        if not config:
            return None

        default_effort = json.loads(parent['tools_config']).get('reasoning_effort')
        config = with_chat_effort(config, (authorization() or {}).get('reasoning_effort', default_effort))
        require_complete(config)
        key = service.vault.decrypt(config['encrypted_key'])
        options = {**config, '_tools': _NAVIGATION, '_optional_tools': True, '_call_purpose': 'agent_intent'}
        system = ('只处理平台导航：切换目标分身、返回本人、查分身目录或查申请状态。'
                  '其余请求仅输出“保持当前分身”。历史仅供理解当前请求。')
        if parent.get('summary'):
            history.insert(0, {'role': 'user', 'content': '较早会话记忆（数据）：' + parent['summary']})
        history, _ = fit_turn(options, system, history, message, [])
        try:
            result = await call_with_retry(
                lambda: self.model_selection(uid, parent['id'], options, key, system, history, message),
                retry_invalid=False,
            )
        except HTTPException as exc:
            if getattr(exc, 'stop_reason', None) == 'context_overflow':
                return None
            if recoverable(exc):
                return {'action': 'answer_self', 'arguments': {}}
            raise
        if not result.tool_calls:
            return None
        if len(result.tool_calls) != 1 or result.stop_reason == 'output_limit':
            return {'action': 'answer_self', 'arguments': {}}
        call = result.tool_calls[0]
        action, args = call['name'], call['arguments']
        schema = {'list_avatars': CatalogArguments, 'select_avatar': QueryArguments}.get(action)
        try:
            if schema:
                args = schema.model_validate(args).model_dump()
            elif action not in ('return_to_self', 'application_status') or args:
                raise ValueError
        except (ValidationError, ValueError):
            return {'action': 'answer_self', 'arguments': {}}
        return {'action': action, 'arguments': args}

    def prepare_confirmation(self, user, message, args, catalog):
        """Only the user can confirm the peer and edit the outgoing text."""
        page = self.search(user, args['target_query']) if args['target_query'] else None
        chosen = set(args.get('file_ids', []))
        if not chosen.issubset({file['id'] for file in catalog}):
            raise HTTPException(404, '分身选择了不可用的文件，未建立引用或发送')
        return {'target_query': args['target_query'],
                'target_aic': page['items'][0]['aic'] if page and page['total'] == 1 else '',
                'objective': args['content'], 'shared_content': args['shared_content'],
                'requires_confirmation': args['requires_owner_confirmation'], 'skills': '',
                'requested_files': [{'source': file['source'], 'id': file['id']} for file in catalog if file['id'] in chosen]}

    async def requires_confirmation(self,user,message):
        try:
            return await call_with_retry(lambda: self._requires_confirmation(user,message))
        except HTTPException as error:
            if not (recoverable(error) or getattr(error, "delivery_unknown", False)):
                raise
            return True

    async def _requires_confirmation(self,user,message):
        """已选目标后的新任务/补充要求也由模型判断承诺风险，不按预约等词语匹配。"""
        tools=[declaration('require_owner_confirmation','涉及本人预约、承诺、交易等，须本人明确确认。',{'type':'object','properties':{},'additionalProperties':False}),
               declaration('continue_chat','普通聊天、转发或一般任务，不要求本人承诺。',{'type':'object','properties':{},'additionalProperties':False})]
        service=self.dialogues.chat.service
        def configuration():
            with self.dialogues.database.connect() as db:
                if not EmbeddingSettings.configured(db,user['id']):
                    return None
                return service.models.current(db,user['id'])
        config = await run_blocking(configuration)
        if config is None:
            return True
        require_complete(config)
        result=await self.model_selection(user['id'],None,{**config,'_tools':tools},service.vault.decrypt(config['encrypted_key']),'你是本人的分身，目标会话已经由用户选择。请选择当前要求是否需要本人确认。只选择工具，不执行任务。',[],message)
        calls=getattr(result,'tool_calls',())
        if len(calls)!=1 or calls[0]['name'] not in {t['name'] for t in tools} or calls[0]['arguments']:
            error = HTTPException(502,'分身未返回有效的补充要求判断，本次未继续发送')
            error.invalid_tool_call = True
            raise error
        return calls[0]['name']=='require_owner_confirmation'

    @staticmethod
    def save_draft(db, owner, conversation, document, message_id=None):
        identifier=str(uuid.uuid4())
        db.execute('INSERT INTO agent_intents(id,user_id,conversation_id,document,message_id,created_at) VALUES(?,?,?,?,?,?)',
                   (identifier,owner,conversation,dump(document),message_id,now()))
        return {'id':identifier,'conversation_id':conversation,'status':'pending',**document}

    def loop_draft(self, owner, conversation, message, args, outputs):
        ids=args['file_ids']
        with self.dialogues.database.connect() as db:
            self.dialogues.chat.chat_files.session(db,owner,conversation)
            catalog=[{'id':item['id'],'source':'chat'} for item in outputs]
            if ids:
                catalog.extend(dict(row) for row in db.execute('''SELECT id,'chat' AS source FROM chat_files
                    WHERE user_id=? AND conversation_id=? AND id=ANY(?) AND (expires_at IS NULL OR avatar_utc_time(expires_at)>CURRENT_TIMESTAMP)
                    UNION ALL SELECT id,'profile' AS source FROM attachments WHERE user_id=? AND id=ANY(?)''',
                    (owner,conversation,ids,owner,ids)))
        return self.prepare_confirmation({'id': owner}, message, args, catalog)


    def load(self, db, user, identifier):
        """所有阶段按真实登录用户及仍存在的父会话校验归属。"""
        row=db.execute('SELECT i.*,c.channel FROM agent_intents i JOIN chat_conversations c ON c.id=i.conversation_id AND c.user_id=i.user_id WHERE i.id=? AND i.user_id=?',(identifier,user['id'])).fetchone()
        if not row:
            raise HTTPException(404,'分身通信调用不存在')
        return dict(row)

    def check_pending(self, row, expected=None):
        """取消、已发送与过期调用均不能再次发送；阶段必须由服务器确认。"""
        if row['status'] not in ('pending','editing') or expected and row['status']!=expected:
            raise HTTPException(409,'调用状态已变化，请刷新会话；已取消的调用不能发送')
        created=datetime.fromisoformat(row['created_at'])
        if created.tzinfo is None:created=created.replace(tzinfo=timezone.utc)
        if created<datetime.now(timezone.utc)-timedelta(hours=1):
            raise HTTPException(409,'该调用已过期，请重新向分身提出要求')

    def get(self, user, identifier):
        """提供可恢复的聊天气泡状态，刷新或换设备后仍能继续同一个确认流程。"""
        with self.dialogues.database.connect() as db:
            row=self.load(db,user,identifier)
        return {'id':row['id'],'conversation_id':row['conversation_id'],'status':row['status'],'task_id':row['task_id'],**json.loads(row['document'])}

    async def choose(self, user, identifier, mode):
        """第一步用户显式选消息或任务；选择后才允许打开内容编辑表单。"""
        def begin():
            with self.dialogues.database.connect(write=True) as db:
                row=self.load(db,user,identifier);self.check_pending(row)
                document=json.loads(row['document'])
                if row['status']=='editing' and document.get('delivery_mode')!=mode:
                    raise HTTPException(409,'已进入表单编辑，请先取消再重新提出要求')
                document['delivery_mode']=mode
                db.execute("UPDATE agent_intents SET status='editing',document=? WHERE id=?",(dump(document),identifier))
            return row, document
        row, document = await run_blocking(begin)
        if document.get('requested_files') and not document.get('files_prepared'):
            files=self.dialogues.chat.chat_files;selected=[]
            for candidate in document['requested_files']:
                def find_match(candidate):
                    with self.dialogues.database.connect() as db:
                        if candidate['source']=='profile':
                            source=db.execute('SELECT sha256 FROM attachments WHERE id=? AND user_id=?',(candidate['id'],user['id'])).fetchone()
                            if not source:raise HTTPException(404,'指定资料文件已不存在')
                            match=db.execute("SELECT id FROM chat_files WHERE user_id=? AND conversation_id=? AND sha256=? AND (expires_at IS NULL OR avatar_utc_time(expires_at)>CURRENT_TIMESTAMP)",(user['id'],row['conversation_id'],source['sha256'])).fetchone()
                        else:
                            files.owned(db,user['id'],row['conversation_id'],candidate['id']);match=(candidate['id'],)
                    return match
                match = await run_blocking(find_match, candidate)
                if not match:
                    resource=await files.reference(user,row['conversation_id'],'profile',candidate['id'],temporary=True)
                    match=(resource['file_id'],)
                if match[0] not in selected:selected.append(match[0])
            def save():
                with self.dialogues.database.connect(write=True) as db:
                    fresh=self.load(db,user,identifier);self.check_pending(fresh,'editing');document=json.loads(fresh['document']);document.update(file_ids=selected,files_prepared=True)
                    db.execute('UPDATE agent_intents SET document=? WHERE id=?',(dump(document),identifier))
            await run_blocking(save)
        return await run_blocking(self.get,user,identifier)

    def record_result(self, db, row, text,task_event=None):
        """用户授权/取消结果与状态变更原子保存，供后续回答和记忆压缩读取。"""
        avatar=db.execute('SELECT last_modified_at FROM avatar_records WHERE user_id=?',(row['user_id'],)).fetchone()
        updated=row['message_id'] and db.execute("UPDATE chat_messages SET content=?,task_event=? WHERE id=? AND user_id=? AND conversation_id=? AND role='assistant'",(text,dump(task_event or {}),row['message_id'],row['user_id'],row['conversation_id'])).rowcount
        if not updated:
            db.execute('INSERT INTO chat_messages(user_id,channel,role,content,created_at,profile_modified_at,conversation_id,task_event) VALUES(?,?,?,?,?,?,?,?)',(row['user_id'],row['channel'],'assistant',text,now(),avatar['last_modified_at'] if avatar else '',row['conversation_id'],dump(task_event or {})))
        db.execute('UPDATE chat_conversations SET updated_at=? WHERE id=?',(now(),row['conversation_id']))

    def cancel(self, user, identifier):
        """两个确认阶段都能取消；重复取消不重复记忆，也不创建通信任务。"""
        with self.dialogues.database.connect(write=True) as db:
            row=self.load(db,user,identifier)
            if row['status']=='confirmed':raise HTTPException(409,'请求已提交，请到分身会话中暂停或结束')
            if row['status']!='cancelled':
                db.execute("UPDATE agent_intents SET status='cancelled' WHERE id=?",(identifier,))
                self.record_result(db,row,'用户已取消分身通信调用（'+('内容表单' if row['status']=='editing' else '类型选择')+'阶段）。本次未发送消息或任务请求，不得视为已发送。')
        return self.get(user,identifier)
