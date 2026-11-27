"""Authenticated read-only access to previously uploaded skills."""
import json
from typing import Literal

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from avatar.api.dependencies import SkillToggle

from avatar.accounts.auth import current_user
from avatar.skills.policy import require_skills_enabled

router = APIRouter(prefix='/api', tags=['个人技能与分身任务'])


# 职责：列出本人安装的技能。
# 输入：完整登录用户，临时资料入口无此权限。
# 结果与约束：仅本人元数据，管理员也不能读取其他账户。
@router.get('/me/skills')
def skills(request: Request,user=Depends(current_user)):
    return request.app.state.chat.skills.list(user['id'])


# 上传请求保留表单验证和文件关闭，暂停期间不读取包。
@router.post('/me/skills',status_code=201)
async def install_skill(request: Request,file: UploadFile=File(...),user=Depends(current_user)):
    try:
        require_skills_enabled()
    finally:
        await file.close()


# 职责：限制远程安装输入为一个仓库 URL，拒绝客户端指定账户或服务器路径。
# 输入：url 为公共 GitHub 仓库首页或 tree/分支/技能目录链接。
# 结果与约束：下载器再次检查域名、路径及网络边界，安装仍按当前会话所有人隔离。
class SkillSource(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    url: str=Field(min_length=1,max_length=500)


# 暂停期间保留请求验证并拒绝远程安装。
@router.post('/me/skills/source',status_code=201)
async def install_skill_source(body: SkillSource,request: Request,user=Depends(current_user)):
    require_skills_enabled()


# 职责：约束多选技能操作，不允许客户端传入账户或路径。
# 输入：ids 为选中的技能，action 决定启用、停用或卸载。
# 结果与约束：最多一百项，所有者在领域事务中逐项核验。
class SkillBatch(BaseModel):
    model_config=ConfigDict(extra='forbid')
    ids: list[str]=Field(min_length=1,max_length=100)
    action: Literal['enable','disable','delete']


@router.post('/me/skills/batch')
def batch_skills(body: SkillBatch,request: Request,user=Depends(current_user)):
    require_skills_enabled()


# 职责：查看本人包的说明和资源清单。
# 输入：skill_id 为已安装标识。
# 结果与约束：先校验所有人，再返回正文；不会返回服务器绝对路径。
@router.get('/me/skills/{skill_id}')
def skill_detail(skill_id: str,request: Request,user=Depends(current_user)):
    row=request.app.state.chat.skills.get(user['id'],skill_id)
    return {key:json.loads(row[key]) if key=='manifest' else row[key] for key in ('id','name','description','instructions','manifest','enabled')}


@router.patch('/me/skills/{skill_id}')
def toggle_skill(skill_id: str,body: SkillToggle,request: Request,user=Depends(current_user)):
    require_skills_enabled()


@router.delete('/me/skills/{skill_id}')
def delete_skill(skill_id: str,request: Request,user=Depends(current_user)):
    require_skills_enabled()


# 职责：以下载方式返回本人技能资源。
# 输入：resource 是清单中的完整相对路径。
# 结果与约束：校验所有人和路径；附件下载且禁止缓存，不直接执行 HTML 或脚本。
@router.get('/me/skills/{skill_id}/files/{resource:path}')
def skill_file(skill_id: str,resource: str,request: Request,user=Depends(current_user)):
    path=request.app.state.chat.skills.resource(user['id'],skill_id,resource)
    return FileResponse(path,filename=path.name,media_type='application/octet-stream')
