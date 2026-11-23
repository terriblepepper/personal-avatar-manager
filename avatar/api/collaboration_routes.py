# 模块说明：本人技能包管理和双分身任务 HTTP 边界，复用完整登录会话及 CSRF 校验。
import uuid
from typing import Annotated,Literal
import json
from fastapi import APIRouter, Body, Depends, Request, UploadFile, File, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints
from avatar.core.text_budget import AgentText
from avatar.accounts.auth import current_user
from avatar.skills.packages import MAX_UPLOAD
from avatar.skills.policy import require_skills_enabled

router=APIRouter(prefix='/api',tags=['个人技能与分身任务'])


# 职责：约束技能启停输入，避免字符串被隐式视为布尔真值。
# 输入：enabled 为明确 JSON 布尔值。
# 结果与约束：拒绝所有额外字段，不能修改包所有人或文件路径。
class SkillToggle(BaseModel):
    model_config=ConfigDict(extra='forbid')
    enabled: StrictBool


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


# 职责：列出本人安装的技能。
# 输入：完整登录用户，临时资料入口无此权限。
# 结果与约束：仅本人元数据，管理员也不能读取其他账户。
@router.get('/me/skills')
def skills(request: Request,user=Depends(current_user)):
    return request.app.state.chat.skills.list(user['id'])


# 职责：安装自动识别格式的单技能或整仓库技能包。
# 输入：file 为表单上传，user 来自完整会话与 CSRF 校验。
# 结果与约束：只读最多 50 MiB 加一个判界字节；包离线校验，不执行脚本。
@router.post('/me/skills',status_code=201)
async def install_skill(request: Request,file: UploadFile=File(...),user=Depends(current_user)):
    try:
        require_skills_enabled()
        content=await file.read(MAX_UPLOAD+1)
        from starlette.concurrency import run_in_threadpool
        identifiers=await run_in_threadpool(request.app.state.chat.skills.install_many,user['id'],file.filename,content)
        return {'id':identifiers[0],'ids':identifiers,'installed_count':len(identifiers),'items':request.app.state.chat.skills.list(user['id'])}
    finally:
        await file.close()


# 职责：限制远程安装输入为一个仓库 URL，拒绝客户端指定账户或服务器路径。
# 输入：url 为公共 GitHub 仓库首页或 tree/分支/技能目录链接。
# 结果与约束：下载器再次检查域名、路径及网络边界，安装仍按当前会话所有人隔离。
class SkillSource(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    url: str=Field(min_length=1,max_length=500)


# 职责：下载公共 GitHub 仓库，并自动安装整个仓库或指定目录内的技能。
# 输入：完整登录会话和经过 CSRF 校验的仓库 URL。
# 结果与约束：与文件上传复用解析及批量事务；网络失败不改变现有技能，无 staff 或 shell 操作。
@router.post('/me/skills/source',status_code=201)
async def install_skill_source(body: SkillSource,request: Request,user=Depends(current_user)):
    require_skills_enabled()
    from avatar.skills.sources import download_repository
    from starlette.concurrency import run_in_threadpool
    content=await download_repository(body.url)
    identifiers=await run_in_threadpool(request.app.state.chat.skills.install_many,user['id'],'repository.zip',content)
    return {'id':identifiers[0],'ids':identifiers,'installed_count':len(identifiers),'items':request.app.state.chat.skills.list(user['id'])}


# 职责：约束多选技能操作，不允许客户端传入账户或路径。
# 输入：ids 为选中的技能，action 决定启用、停用或卸载。
# 结果与约束：最多一百项，所有者在领域事务中逐项核验。
class SkillBatch(BaseModel):
    model_config=ConfigDict(extra='forbid')
    ids: list[str]=Field(min_length=1,max_length=100)
    action: Literal['enable','disable','delete']


# 职责：批量管理本人技能，全部归属核验通过才修改。
# 输入：完整会话与 CSRF 校验后的多选请求。
# 结果与约束：卸载同步删除选中资源，事务失败恢复目录；返回当前列表。
@router.post('/me/skills/batch')
def batch_skills(body: SkillBatch,request: Request,user=Depends(current_user)):
    request.app.state.chat.skills.batch(user['id'],body.ids,body.action)
    return {'items':request.app.state.chat.skills.list(user['id'])}


# 职责：查看本人包的说明和资源清单。
# 输入：skill_id 为已安装标识。
# 结果与约束：先校验所有人，再返回正文；不会返回服务器绝对路径。
@router.get('/me/skills/{skill_id}')
def skill_detail(skill_id: str,request: Request,user=Depends(current_user)):
    row=request.app.state.chat.skills.get(user['id'],skill_id)
    return {key:json.loads(row[key]) if key=='manifest' else row[key] for key in ('id','name','description','instructions','manifest','enabled')}


# 职责：启用或停用本人的技能。
# 输入：skill_id 和 enabled，归属由当前会话确定。
# 结果与约束：启用后参与任务选择，立即影响之后的回答，不进入资料审核。
@router.patch('/me/skills/{skill_id}')
def toggle_skill(skill_id: str,body: SkillToggle,request: Request,user=Depends(current_user)):
    request.app.state.chat.skills.toggle(user['id'],skill_id,body.enabled)
    return {'items':request.app.state.chat.skills.list(user['id'])}


# 职责：卸载本人技能及原件。
# 输入：skill_id 与当前完整登录会话。
# 结果与约束：撤销数据库记录和文件，不影响其他用户同名包。
@router.delete('/me/skills/{skill_id}')
def delete_skill(skill_id: str,request: Request,user=Depends(current_user)):
    request.app.state.chat.skills.delete(user['id'],skill_id)
    return {'items':request.app.state.chat.skills.list(user['id'])}


# 职责：以下载方式返回本人技能资源。
# 输入：resource 是清单中的完整相对路径。
# 结果与约束：校验所有人和路径；附件下载且禁止缓存，不直接执行 HTML 或脚本。
@router.get('/me/skills/{skill_id}/files/{resource:path}')
def skill_file(skill_id: str,resource: str,request: Request,user=Depends(current_user)):
    path=request.app.state.chat.skills.resource(user['id'],skill_id,resource)
    return FileResponse(path,filename=path.name,media_type='application/octet-stream')


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
def dialogues(request: Request,offset: int=0,user=Depends(current_user)):
    return request.app.state.dialogues.list(user,offset)


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
        task_offset: int=Query(0,ge=0), task_search: str=Query('',max_length=200), user=Depends(current_user)):
    return request.app.state.dialogues.get(user,identifier,before=before,after=after,task_paging=(task_offset,task_search))


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
    return request.app.state.dialogues.preferences(user['id'])

@router.put('/agent-dialogue-settings')
def save_dialogue_settings(body:DialogueSettings,request:Request,user=Depends(current_user)):
    return request.app.state.dialogues.preferences(user['id'],body.max_rounds)

@router.get('/agent-sessions')
def agent_sessions(request:Request,offset:int=0,search:str=Query(default='',max_length=200),summary:bool=False,user=Depends(current_user)):
    return request.app.state.dialogues.sessions(user,offset,search,summary=summary)

@router.get('/agent-sessions/{identifier}')
def agent_session(identifier: str, request: Request, before: int|None=None, after: int|None=None,
        task_offset: int=Query(0,ge=0), task_search: str=Query('',max_length=200), user=Depends(current_user)):
    return request.app.state.dialogues.session(user,identifier,before=before,after=after,task_paging=(task_offset,task_search))

@router.put('/agent-sessions/{identifier}/auto-accept')
def set_auto_accept(identifier: str, body: SkillToggle, request: Request, user=Depends(current_user)):
    return request.app.state.dialogues.auto_accept(user,identifier,body.enabled)


@router.post('/agent-dialogues/{identifier}/accept')
async def accept_agent_task(identifier:str,request:Request,body:DialogueResume|None=None,user=Depends(current_user)):
    """只由目标方接受当前任务，补充要求与附件随同意一起放行。"""
    return await request.app.state.dialogues.approve(user,identifier,body,initial=True)

@router.post('/agent-dialogues/{identifier}/reject')
def reject_agent_task(identifier: str, request: Request, user=Depends(current_user)):
    return request.app.state.dialogues.reject(user,identifier)


@router.post('/agent-sessions/{identifier}/accept')
def accept_agent_session(identifier:str,request:Request,user=Depends(current_user)):
    return request.app.state.dialogues.accept(user,identifier)

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
def search_agent_intent_targets(identifier:str,request:Request,search:str='',offset:int=0,user=Depends(current_user)):
    request.app.state.dialogues.intent.get(user,identifier)
    return request.app.state.dialogues.intent.search(user,search,offset)

@router.get('/notifications/unread-count')
def notification_count(request: Request, user=Depends(current_user)):
    return {'unread': request.app.state.dialogues.inbox.unread_count(user['id'])}


@router.get('/notifications')
def notification_inbox(request:Request,offset:int=0,unread:bool=False,status:Literal['all','read','unread']='all',category:Literal['all','chat','request','notification','activity']='all',search:str='',user=Depends(current_user)):
    return request.app.state.dialogues.inbox.list(user['id'],offset,unread,status,category,search)


# 批量消息参数仅接受正整数 ID，限定百条，拒绝布尔/字符串 ID 与任意额外字段。
class NotificationBatch(BaseModel):
    model_config=ConfigDict(extra='forbid')
    ids:list[Annotated[int,Field(strict=True,gt=0)]]=Field(min_length=1,max_length=100)
    action:Literal['read','unread','delete']


# 完整登录和 CSRF 沿用全局边界；领域服务在一个事务中验证整批归属。
@router.post('/notifications/delete-all')
def delete_all_notifications(request:Request,user=Depends(current_user)):
    return request.app.state.dialogues.inbox.delete_all(user['id'])


@router.post('/notifications/batch')
def batch_notifications(body:NotificationBatch,request:Request,user=Depends(current_user)):
    return request.app.state.dialogues.inbox.change_many(user['id'],body.ids,body.action)

@router.post('/notifications/{identifier}/{action}')
def change_notification(identifier:int,action:str,request:Request,user=Depends(current_user)):
    return request.app.state.dialogues.inbox.change(user['id'],identifier,action)


@router.get('/agent-sessions/{identifier}/tasks')
def agent_session_tasks(identifier:str,request:Request,offset:int=0,search:str=Query(default='',max_length=200),user=Depends(current_user)):
    return request.app.state.dialogues.task_page(user,identifier,offset,search)
