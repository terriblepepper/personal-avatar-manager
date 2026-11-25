from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
from fastapi import Request
from fastapi import Query
from pydantic import BaseModel
from pydantic import ConfigDict
import avatar.accounts.auth as auth
from avatar.conversation.schemas import ChatRequest
from avatar.conversation.schemas import ConversationRequest
from avatar.conversation.schemas import ConversationTitleRequest
from pydantic import StrictBool


router = APIRouter()


# 职责：获取本人指定频道与会话的聊天历史。
# 输入：channel 默认 assistant，conversation_id 可选，user 为当前账户。
# 结果与约束：先通过会话归属校验，再返回最近历史；内部子会话不能被冒用为本人 Web 会话。
@router.get("/chat")
def messages(request: Request, channel: str = "assistant", conversation_id: str | None = None, personal: bool=False,before:int|None=Query(default=None,ge=0,le=2**63-1),limit:int=Query(default=100,ge=1,le=100), user=Depends(auth.current_user)):
    return request.app.state.chat.history(user["id"], channel, conversation_id,personal=personal,before=before,limit=limit)


# 职责：列出本人频道会话及当前目标的记忆状态。
# 输入：channel 默认为 avatar，user 用于作用域检索，request 提供对话服务。
# 结果与约束：返回默认会话 ID 和列表；摘要需匹配目标当前资料、文件和技能集合日期，显示元数据而不泄露摘要或私人历史。
@router.get('/conversations')
def conversations(request: Request, channel: str = 'avatar', personal: bool=False, user=Depends(auth.current_user)):
    chat = request.app.state.chat
    default = chat.session(user['id'],channel)
    rows = chat.memory.list(user['id'],channel)
    if personal:
        other='assistant' if channel=='avatar' else 'avatar'
        rows+=chat.memory.list(user['id'],other)
        rows.sort(key=lambda row:row['updated_at'],reverse=True)
    with chat.service.database.connect() as db:
        agent = db.execute('SELECT last_modified_at FROM avatar_records WHERE user_id=?', (user['id'],)).fetchone()
    targets={t['aic']:t for t in chat.assistant_router.catalog()} if channel == 'assistant' and not personal else {}
    for row in rows:
        row['memory_active'] = bool(row['summary_through'] and agent and row['summary_profile_modified_at'].split('|',1)[0] == max(agent['last_modified_at'],request.app.state.service.attachments.collection_date(user['id']),request.app.state.chat.skills.collection_date(user['id']))) if channel == 'avatar' else False
        row['selected_target'] = None
        if channel == 'assistant' and not personal and row.get('selected_aic'):
            target=targets.get(row['selected_aic'])
            if target:
                row['selected_target']={'aic':target['aic'],'name':target['name']}
                with chat.service.database.connect() as db:
                    child=db.execute("SELECT summary_through,summary_profile_modified_at,compression_count FROM chat_conversations WHERE parent_id=? AND target_aic=? AND platform='dingdang'",(row['id'],target['aic'])).fetchone()
                row['memory_active']=bool(child and child['summary_through'] and child['summary_profile_modified_at']==max(target['row']['last_modified_at'],request.app.state.service.attachments.collection_date(target['row']['user_id']),request.app.state.chat.skills.collection_date(target['row']['user_id'])))
                row['compression_count']=child['compression_count'] if child else 0
    if personal:
        for row in rows:
            row['selected_target']=None
            row['memory_active']=bool(row['summary_through'] and agent and row['summary_profile_modified_at'].split('|',1)[0]==max(agent['last_modified_at'],request.app.state.service.attachments.collection_date(user['id']),chat.skills.collection_date(user['id'])))
    default_id=next((row['id'] for row in rows if row['message_count']),default['id']) if personal else default['id']
    return {'default_id':default_id,'items':rows}


# 职责：为本人新建助手或分身对话会话。
# 输入：body 含频道和标题，user 提供认证身份。
# 结果与约束：调用 session 创建隔离会话并返回 ID、标题；本人分身未创建时不能新建其对话。
@router.post('/conversations', status_code=201)
def create_conversation(body: ConversationRequest, request: Request, user=Depends(auth.current_user)):
    row = request.app.state.chat.session(user['id'],body.channel,create=True,title=body.title)
    if body.title not in ('新会话','默认会话'):
        with request.app.state.database.connect(write=True) as db:
            db.execute("UPDATE chat_conversations SET title_source='manual' WHERE id=?", (row['id'],))
    return {'id':row['id'],'title':row['title']}


@router.get('/conversations/{conversation_id}/compact')
def compact_usage(conversation_id:str,request:Request,user=Depends(auth.current_user)):
    return request.app.state.chat.compact_usage(user,conversation_id)


@router.post('/conversations/{conversation_id}/compact')
async def compact_conversation(conversation_id:str,request:Request,focus:str=Body(default='',embed=True,max_length=2000),user=Depends(auth.current_user)):
    return await request.app.state.chat.compact(user,conversation_id,focus)


@router.patch('/conversations/{conversation_id}')
def rename_conversation(conversation_id: str, body: ConversationTitleRequest, request: Request, user=Depends(auth.current_user)):
    chat = request.app.state.chat
    row = chat.session(user['id'], 'assistant', conversation_id, personal=True)
    with chat.service.database.connect(write=True) as db:
        db.execute("UPDATE chat_conversations SET title=?,title_source='manual' WHERE id=? AND user_id=?", (body.title,row['id'],user['id']))
    return {'id':row['id'],'title':body.title}


# 职责：删除本人会话、消息与关联目标子会话。
# 输入：conversation_id 为路径会话，channel 指定频道，user 为本人。
# 结果与约束：先校验完整作用域再删除，处理中拒绝操作，模型计费记录保留。
@router.delete('/conversations/{conversation_id}')
def delete_conversation(conversation_id: str, request: Request, channel: str = 'avatar', personal: bool=False, user=Depends(auth.current_user)):
    chat = request.app.state.chat
    session = chat.session(user['id'],channel,conversation_id,personal=personal)
    chat.memory.remove(session['id'])
    return {'message':'会话及记忆已删除'}


# 职责：清空本人会话历史和压缩记忆。
# 输入：conversation_id、channel 经本人 session 校验。
# 结果与约束：保留父会话 ID，删除历史及子会话并重置摘要与选中目标；处理中返回冲突。
@router.post('/conversations/{conversation_id}/clear')
def clear_conversation(conversation_id: str, request: Request, channel: str = 'avatar', personal: bool=False, user=Depends(auth.current_user)):
    chat = request.app.state.chat
    session = chat.session(user['id'],channel,conversation_id,personal=personal)
    chat.memory.remove(session['id'],clear=True)
    return {'message':'历史记录和压缩记忆已清空'}


# 职责：提供带登录与 CSRF 校验的 SSE 问答入口。
# 输入：body 为 ChatRequest，user 为当前账户，request 提供对话服务。
# 结果与约束：浏览器断开仅停止 SSE 投递，后台继续生成并保存；刷新通过 pending 恢复状态。
@router.post("/chat/stream")
def chat_stream(body: ChatRequest, request: Request, user=Depends(auth.current_user)):
    from avatar.api.streaming import chat_response
    if not body.conversation_id:
        session=request.app.state.chat.session(user['id'],body.channel,None,personal=body.personal)
        body=body.model_copy(update={'conversation_id':session['id']})
    return chat_response(request.app.state.runtime, user, body, request.app.state.database)


@router.post('/chat/generations',status_code=202)
def submit_chat_generation(body: ChatRequest,request: Request,user=Depends(auth.current_user)):
    from avatar.api.streaming import submit_generation
    return submit_generation(request.app.state.database,user,body)


@router.get('/chat/pending')
def pending_chat(conversation_id: str, request: Request, user=Depends(auth.current_user)):
    from avatar.conversation.jobs.store import GenerationStore
    return GenerationStore(request.app.state.database).latest(user['id'],conversation_id)


@router.post('/chat/generations/{request_id}/stop')
async def stop_generation(request_id:str,request:Request,user=Depends(auth.current_user)):
    return await request.app.state.generations.cancel(user,request_id)


@router.get('/chat/generations/{request_id}')
def chat_generation(request_id: str,request: Request,user=Depends(auth.current_user)):
    from avatar.conversation.jobs.store import GenerationStore
    return GenerationStore(request.app.state.database).get(user['id'],request_id)


# 职责：显示本人会话实际使用及移出的技能。
# 输入：认证账户与本人 Web 父会话 ID。
# 结果与约束：返回有限名称元数据，不返回技能正文或其他账户目录。
@router.get('/conversations/{conversation_id}/skills')
def conversation_skills(conversation_id: str,request: Request,user=Depends(auth.current_user)):
    return request.app.state.chat.session_skills.list(user['id'],conversation_id)


# 职责：校验会话技能开关，只接受布尔值，不接受全局设置或账户 ID。
class SessionSkillToggle(BaseModel):
    model_config=ConfigDict(extra='forbid')
    enabled: StrictBool


# 职责：在本会话移出/恢复技能，不卸载用户安装文件。
# 输入：会话、本人技能 ID 和 enabled 布尔值，CSRF 由会话依赖校验。
# 结果与约束：会话生成期间拒绝修改，变更仅影响当前父会话的技能加载与旧摘要。
@router.patch('/conversations/{conversation_id}/skills/{skill_id}')
def conversation_skill_update(conversation_id: str,skill_id: str,body: SessionSkillToggle,request: Request,user=Depends(auth.current_user)):
    from avatar.skills.policy import require_skills_enabled
    require_skills_enabled()
    chat=request.app.state.chat
    with request.app.state.database.connect() as db:chat.session_skills.owned(db,user['id'],conversation_id)
    chat.memory.acquire(conversation_id)
    try:return chat.session_skills.set_enabled(user['id'],conversation_id,skill_id,body.enabled)
    finally:chat.memory.release(conversation_id)
