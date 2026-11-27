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
