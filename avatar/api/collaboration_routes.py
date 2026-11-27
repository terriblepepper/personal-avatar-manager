# 模块说明：双分身协作 HTTP 边界，复用完整登录会话及 CSRF 校验。
import uuid
from typing import Annotated,Literal
from fastapi import APIRouter, Body, Depends, Request, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints
from avatar.core.text_budget import AgentText
from avatar.api.dependencies import PageOffset, PageSearch, SkillToggle
from avatar.accounts.auth import current_user

router=APIRouter(prefix='/api',tags=['分身协作'])


# 职责：约束任务委托和幂等键。
# 输入：指定目标 AIC、任务和幂等键，不设总轮数。
# 结果与约束：本人身份由登录会话确定；request_id 支持客户端重复提交去重。
class DialogueRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    target_aic: str=Field(min_length=1,max_length=160)
    objective: AgentText=Field(min_length=1)
    request_id: str=Field(default_factory=lambda:str(uuid.uuid4()),min_length=1,max_length=80)
    max_rounds: int|None=Field(default=None,ge=1,le=1000)
    kind: Literal['chat','booking']='chat'
    file_ids:list[Annotated[str,Field(min_length=1,max_length=80)]]=Field(default_factory=list,max_length=5)


class DialogueResume(BaseModel):
    steer: StrictBool=False
    question_id: str|None=Field(default=None,pattern=r'^[0-9a-f]{32}$')
    expected_revision: int|None=Field(default=None,ge=0)
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    message: AgentText=''
    remove_skills: list[Annotated[str,Field(min_length=1,max_length=80)]]=Field(default_factory=list,max_length=3)
    request_id: str=Field(min_length=1,max_length=80)
    file_ids:list[Annotated[str,Field(min_length=1,max_length=80)]]=Field(default_factory=list,max_length=5)


class DialogueRetry(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    request_id:str=Field(min_length=1,max_length=80)


# 职责：获取可选择的其他用户分身。
# 输入：当前完整登录用户。
# 结果与约束：仅输出公开名称和 AIC，不泄露技能列表或模型配置。
@router.get('/agent-dialogues/targets')
def dialogue_targets(request: Request,user=Depends(current_user)):
    return request.app.state.dialogues.targets(user)


# 职责：列出本人参与的最近任务。
# 输入：当前完整登录用户。
# 结果与约束：仅双方可见，与普通聊天历史隔离。
@router.get('/agent-dialogues')
def dialogues(request: Request,offset: PageOffset = 0,user=Depends(current_user)):
    return request.app.state.dialogues.session_store.list(user,offset)


# 职责：创建双方分身交替发言的后台任务。
# 输入：body 限定目标、委托及幂等键。
# 结果与约束：返回持久任务 ID，后台分别使用各所有人的 Key；不触发外部预订。
@router.post('/agent-dialogues',status_code=202)
def create_dialogue(body: DialogueRequest,request: Request,user=Depends(current_user)):
    return {'id':request.app.state.dialogues.create(user,body)}


# 职责：读取本人参与的任务进度和完成发言。
# 输入：identifier 为创建返回的任务 ID。
# 结果与约束：第三方统一 404，不返回私有档案或其他聊天。
@router.get('/agent-dialogues/{identifier}')
def dialogue(identifier: str, request: Request, before: int|None=None, after: int|None=None,
        task_offset: PageOffset = 0, task_search: PageSearch = '', user=Depends(current_user)):
    return request.app.state.dialogues.session_store.get(user,identifier,before=before,after=after,task_paging=(task_offset,task_search))


@router.post('/agent-dialogues/{identifier}/retry-draft')
def retry_dialogue(identifier:str,body:DialogueRetry,request:Request,user=Depends(current_user)):
    return request.app.state.dialogues.retry_draft(user,identifier,body.request_id)


# 职责：停止本人参与的任务。
# 输入：identifier 与当前用户。
# 结果与约束：取消运行中的模型请求，保留完成发言和已记录用量。
@router.post('/agent-dialogues/{identifier}/cancel')
async def cancel_dialogue(identifier: str,request: Request,user=Depends(current_user)):
    return await request.app.state.dialogues.cancel(user,identifier)


# 职责：由本人确认双方任务出的意向。
# 输入：identifier 与当前用户。
# 结果与约束：按服务器指定的待确认方显示；本人确认不代表另一方同意或已执行外部预订。
@router.post('/agent-dialogues/{identifier}/confirm')
async def confirm_dialogue(identifier: str,request: Request,body:DialogueResume|None=None,user=Depends(current_user)):
    return await request.app.state.dialogues.approve(user,identifier,body)


# 职责：校验用户补充要求，不接受客户端指定发言身份。
# 输入：message 可留空以直接继续；request_id 用于防止重复注入。
# 结果与约束：沿用单条消息大小约束；自动聊天按账户配置的轮次暂停，可由用户继续。


# 职责：继续本人参与的任务，可插入本人的补充要求。
# 输入：完整会话、任务 ID 和补充输入。
# 结果与约束：核验归属后持久排队，当前回答完成后插入，不取消或重放已计费生成。
@router.post('/agent-dialogues/{identifier}/resume')
async def resume_dialogue(identifier: str,body: DialogueResume,request: Request,user=Depends(current_user)):
    return await request.app.state.dialogues.resume(user,identifier,body)


# 职责：从本人列表删除任务。
# 输入：完整会话与任务 ID。
# 结果与约束：删除本方任务记录；未完成时提前终止且不回传总结，已完成的返回记忆独立保留。
@router.delete('/agent-dialogues/{identifier}')
async def delete_dialogue(identifier: str,request: Request,user=Depends(current_user)):
    return await request.app.state.dialogues.delete(user,identifier)


# 职责：由任一参与者结束任务，不要求另一方确认。
# 输入：完整登录会话、任务 ID。
# 结果与约束：取消当前任务并保留长期配对会话，下次消息可创建新任务。
@router.post('/agent-dialogues/{identifier}/end')
async def end_dialogue(identifier: str,request: Request,expected_revision:int|None=Query(default=None,ge=0),user=Depends(current_user)):
    return await request.app.state.dialogues.end(user,identifier,expected_revision=expected_revision)


# 同一个配对只有一个会话；消息条目与任务分别分页，所有操作仍验证真实登录及 CSRF。
class DialogueSettings(BaseModel):
    model_config=ConfigDict(extra='forbid')
    max_rounds:int=Field(ge=1,le=1000)

class SessionMessage(DialogueResume):
    new_task:bool=False
    delivery_mode:Literal['message','negotiation']='message'

class IntentChoice(BaseModel):
    """第一阶段只确认对话类型，不发送请求。"""
    model_config=ConfigDict(extra='forbid')
    delivery_mode:Literal['message','negotiation']

class IntentConfirm(BaseModel):
    model_config=ConfigDict(extra='forbid')
    target_aic:Annotated[str,StringConstraints(strip_whitespace=True,min_length=1,max_length=160)]
    objective:AgentText=Field(min_length=1)
    share_content:bool=False
    shared_content:str=Field(default='',max_length=60000)
    file_ids:list[Annotated[str,StringConstraints(strip_whitespace=True,min_length=1,max_length=80)]]=Field(default_factory=list,max_length=5)

@router.get('/agent-dialogue-settings')
def dialogue_settings(request:Request,user=Depends(current_user)):
    return request.app.state.dialogues.session_store.preferences(user['id'])

@router.put('/agent-dialogue-settings')
def save_dialogue_settings(body:DialogueSettings,request:Request,user=Depends(current_user)):
    return request.app.state.dialogues.session_store.preferences(user['id'],body.max_rounds)

@router.get('/agent-sessions')
def agent_sessions(request:Request,offset: PageOffset = 0,search: PageSearch = '',summary:bool=False,user=Depends(current_user)):
    return request.app.state.dialogues.session_store.sessions(user,offset,search,summary=summary)

@router.get('/agent-sessions/{identifier}')
def agent_session(identifier: str, request: Request, before: int|None=None, after: int|None=None,
        task_offset: PageOffset = 0, task_search: PageSearch = '', user=Depends(current_user)):
    return request.app.state.dialogues.session_store.session(user,identifier,before=before,after=after,task_paging=(task_offset,task_search))

@router.put('/agent-sessions/{identifier}/auto-accept')
def set_auto_accept(identifier: str, body: SkillToggle, request: Request, user=Depends(current_user)):
    return request.app.state.dialogues.session_store.auto_accept(user,identifier,body.enabled)


@router.post('/agent-dialogues/{identifier}/accept')
async def accept_agent_task(identifier:str,request:Request,body:DialogueResume|None=None,user=Depends(current_user)):
    """只由目标方接受当前任务，补充要求与附件随同意一起放行。"""
    return await request.app.state.dialogues.approve(user,identifier,body,initial=True)

@router.post('/agent-dialogues/{identifier}/reject')
def reject_agent_task(identifier: str, request: Request, user=Depends(current_user)):
    return request.app.state.dialogues.reject(user,identifier)


@router.post('/agent-sessions/{identifier}/messages')
async def send_agent_session(identifier:str,body:SessionMessage,request:Request,user=Depends(current_user)):
    return await request.app.state.dialogues.send_session(user,identifier,body)

@router.post('/agent-sessions/{identifier}/task-draft')
def draft_agent_task(identifier:str,body:SessionMessage,request:Request,user=Depends(current_user)):
    """本人显式创建任务：复用目标选择、Markdown 编辑和取消回执表单。"""
    return request.app.state.dialogues.task_draft(user,identifier,body)

@router.post('/agent-sessions/delete-all')
def delete_all_agent_sessions(request:Request,user=Depends(current_user)):
    return request.app.state.dialogues.dismiss_all_history(user)


@router.post('/agent-sessions/{identifier}/tasks/delete-all')
def delete_all_session_tasks(identifier:str,request:Request,user=Depends(current_user)):
    return request.app.state.dialogues.dismiss_all_history(user,identifier)


@router.get('/agent-sessions/{identifier}/compact')
def compact_usage(identifier:str,request:Request,task_id:str|None=None,user=Depends(current_user)):
    return request.app.state.dialogues.compact_usage(user,identifier,task_id)


@router.post('/agent-sessions/{identifier}/compact')
async def compact_peer_session(identifier:str,request:Request,task_id:str|None=None,focus:str=Body(default='',embed=True,max_length=2000),user=Depends(current_user)):
    return await request.app.state.dialogues.compact(user,identifier,task_id,focus)

@router.post('/agent-sessions/{identifier}/clear')
def clear_agent_session(identifier:str,request:Request,user=Depends(current_user)):
    return request.app.state.dialogues.clear_session(user,identifier)

@router.delete('/agent-sessions/{identifier}')
def delete_agent_session(identifier:str,request:Request,user=Depends(current_user)):
    return request.app.state.dialogues.clear_session(user,identifier,True)

@router.post('/agent-intents/{identifier}/confirm')
async def confirm_agent_intent(identifier:str,body:IntentConfirm,request:Request,user=Depends(current_user)):
    return await request.app.state.dialogues.confirm_intent(user,identifier,body)

@router.get('/agent-intents/{identifier}')
def agent_intent(identifier:str,request:Request,user=Depends(current_user)):
    return request.app.state.dialogues.intent.get(user,identifier)

@router.post('/agent-intents/{identifier}/choose')
async def choose_agent_intent(identifier:str,body:IntentChoice,request:Request,user=Depends(current_user)):
    return await request.app.state.dialogues.intent.choose(user,identifier,body.delivery_mode)

@router.post('/agent-intents/{identifier}/cancel')
def cancel_agent_intent(identifier:str,request:Request,user=Depends(current_user)):
    return request.app.state.dialogues.intent.cancel(user,identifier)

@router.get('/agent-intents/{identifier}/targets')
def search_agent_intent_targets(identifier:str,request:Request,search: PageSearch = '',offset: PageOffset = 0,user=Depends(current_user)):
    request.app.state.dialogues.intent.get(user,identifier)
    return request.app.state.dialogues.intent.search(user,search,offset)

@router.get('/agent-sessions/{identifier}/tasks')
def agent_session_tasks(identifier:str,request:Request,offset: PageOffset = 0,search: PageSearch = '',user=Depends(current_user)):
    return request.app.state.dialogues.session_store.task_page(user,identifier,offset,search)
