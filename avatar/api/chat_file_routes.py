# 模块说明：登录用户的会话附件 HTTP 边界，提供上传、下载、删除及转存个人资料。
import asyncio
from contextlib import ExitStack
from typing import Literal
from fastapi import APIRouter, Depends, Request, Query, HTTPException
from fastapi.responses import FileResponse
from avatar.api.file_previews import word_pdf
from avatar.api.dependencies import PageOffset, ShortPageSearch
from pydantic import BaseModel, ConfigDict, Field, StrictBool, TypeAdapter, ValidationError
from starlette.formparsers import MultiPartException
from avatar.api.multipart import FileTooLarge, UploadParser
from avatar.files.uploads.validation import html_file_limit
from avatar.files.uploads.buffer import upload_buffer, upload_work
from avatar.models.effort import Effort
from avatar.accounts.auth import current_user

router=APIRouter(prefix='/api/conversations',tags=['会话附件'])


# 职责：约束会话工具设置，仅接受布尔开关，工具名称由服务端目录核验。
class ToolSettings(BaseModel):
    model_config=ConfigDict(extra='forbid')
    tools: dict[str,StrictBool]=Field(min_length=1,max_length=20)


# 职责：列出本人会话可用工具与当前开关，不读取其他会话设置。
@router.get('/{conversation_id}/tools')
def tools(conversation_id: str,request: Request,user=Depends(current_user)):
    return request.app.state.chat.tools.get(user['id'],conversation_id)


# 职责：保存本人会话工具授权，处理中修改及未知工具均拒绝。
@router.patch('/{conversation_id}/tools')
def set_tools(conversation_id: str,body: ToolSettings,request: Request,user=Depends(current_user)):
    return request.app.state.chat.tools.update(user['id'],conversation_id,body.tools)


class EffortSetting(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reasoning_effort: Effort | None


@router.get('/{conversation_id}/reasoning-effort')
def chat_effort(conversation_id: str, request: Request, user=Depends(current_user)):
    return request.app.state.chat.tools.effort(user['id'], conversation_id)


@router.patch('/{conversation_id}/reasoning-effort')
def set_chat_effort(conversation_id: str, body: EffortSetting, request: Request, user=Depends(current_user)):
    return request.app.state.chat.tools.effort(user['id'], conversation_id, selected=body.reasoning_effort)


# 职责：分页浏览本人文件库，供输入区选择引用，文件正文不在目录接口返回。
@router.get('/{conversation_id}/file-library')
def file_library(conversation_id: str,request: Request,offset: PageOffset = 0,search: ShortPageSearch = '',folder:str=Query(default='',max_length=1024),recursive:bool=False,user=Depends(current_user)):
    return request.app.state.chat.chat_files.library(user['id'],conversation_id,search,offset,folder,recursive)


# 职责：约束引用来源为本人资料或会话文件，不接受 URL 或磁盘路径。
class FileReference(BaseModel):
    model_config=ConfigDict(extra='forbid')
    source: Literal['profile','chat']
    id: str=Field(max_length=80,min_length=1)
    temporary:StrictBool=False


# 职责：约束会话文件引用为真实布尔值，避免字符串真值误启用文件。
class ReferenceEnabled(BaseModel):
    model_config=ConfigDict(extra='forbid')
    enabled: StrictBool


# 职责：保存当前文件引用开关；受会话所有者、CSRF 和生成租约约束。
@router.patch('/{conversation_id}/files/{file_id}/reference')
def reference_enabled(conversation_id: str,file_id: str,body: ReferenceEnabled,request: Request,user=Depends(current_user)):
    return {'items':request.app.state.chat.chat_files.reference_enabled(user['id'],conversation_id,file_id,body.enabled)}


# 职责：把已授权原件引用到当前会话；复制副本与本会话上传文件共用配额和删除规则。
@router.post('/{conversation_id}/files/reference',status_code=201)
async def reference_file(conversation_id: str,body: FileReference,request: Request,user=Depends(current_user)):
    return await request.app.state.chat.chat_files.reference(user,conversation_id,body.source,body.id,temporary=body.temporary)


# 职责：限制转存资料的说明和可见性，默认私密。
# 输入：caption 必须由用户填写，visibility 只能公开或私密。
# 结果与约束：不能传入文件路径、其他所有人或已解析文字，不绕过资料文件配额。
class PromoteFile(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    caption: str=Field(default='',max_length=2000)
    visibility: Literal['private','public']='private'
    folder:str=Field(default='',max_length=1024)
    new_name:str|None=Field(default=None,min_length=1,max_length=200)


# 职责：列出本人某父会话附件。
# 输入：conversation_id 与完整登录会话。
# 结果与约束：临时资料入口不授权，其他用户统一 404。
@router.get('/{conversation_id}/files')
def files(conversation_id: str, request: Request, user=Depends(current_user)):
    return request.app.state.chat.chat_files.list(user['id'],conversation_id)


# 职责：上传会话文件并等待解析结果。
# 输入：file 为单份 Multipart，conversation_id 必须属于当前用户。
# 结果与约束：解析前后均检查会话归属，始终关闭上传句柄，不放入个人资料清单。
@router.post('/{conversation_id}/files',status_code=201,openapi_extra={
    'requestBody':{'required':True,'content':{'multipart/form-data':{'schema':{
        'type':'object','required':['file'],'properties':{'file':{'type':'string','format':'binary'},
        'temporary':{'type':'boolean','default':False}}}}}}})
async def upload(conversation_id: str, request: Request, user=Depends(current_user)):
    files = request.app.state.chat.chat_files
    def require_editable():
        with files.database.connect() as db:
            files.session(db,user['id'],conversation_id,editable=True)

    await asyncio.to_thread(require_editable)
    try:
        form = await UploadParser(request.headers, request.stream(), max_fields=1, file_limit=html_file_limit).parse()
    except FileTooLarge:
        raise HTTPException(413, 'HTML 文件不能超过 100 MiB') from None
    except MultiPartException:
        raise HTTPException(413,'上传超过字段或文件数量限制') from None
    try:
        file = form.get('file')
        if not hasattr(file,'file') or not file.filename:
            raise HTTPException(422,'请提交会话文件')
        try:
            temporary = TypeAdapter(bool).validate_python(form.get('temporary',False))
        except ValidationError:
            raise HTTPException(422,'temporary 须为布尔值') from None
        with upload_buffer(file.file) as data:
            return await files.upload(user,conversation_id,file.filename,data,temporary=temporary)
    finally:
        await form.close()


# 职责：下载本人会话的不可变原件。
# 输入：会话和文件 ID，不接受任意路径。
# 结果与约束：原件作为附件返回，不公开内联脚本或 HTML。
@router.get('/{conversation_id}/files/{file_id}')
def download(conversation_id: str, file_id: str, request: Request, user=Depends(current_user), preview: bool=False, preview_pdf: bool=False):
    service=request.app.state.chat.chat_files

    def resolve():
        with service.database.connect() as db:
            row=service.owned(db,user['id'],conversation_id,file_id)
        path=service.directory.path(row['user_id'],row['conversation_id'],file_id+'.blob')
        if not path.is_file():
            raise HTTPException(404,'会话原件不存在')
        return path, row

    if preview_pdf:
        return word_pdf(resolve)
    path, row = resolve()
    if preview:
        # 内联只使用服务端重新编码的 JPEG，下载仍返回授权原件。
        image=service.directory.path(row['user_id'],row['conversation_id'],file_id+'.jpg')
        if not row['content_type'].startswith('image/') or not image.is_file():
            raise HTTPException(404,'图片预览不存在')
        return FileResponse(image,media_type='image/jpeg',headers={'Cache-Control':'private, no-store'})
    return FileResponse(path,filename=row['filename'],media_type='application/octet-stream')


# 职责：删除本人附件及其解析结果，失败时恢复文件。
# 输入：完整登录会话、父会话及文件 ID。
# 结果与约束：回答期间拒绝删除，不影响已转存的个人资料副本。
@router.delete('/{conversation_id}/files/{file_id}')
def remove(conversation_id: str, file_id: str, request: Request, user=Depends(current_user)):
    service=request.app.state.chat.chat_files
    with ExitStack() as stack:
        with service.database.connect(write=True) as db:
            service.session(db,user['id'],conversation_id,editable=True)
            row=service.owned(db,user['id'],conversation_id,file_id)
            stack.enter_context(service.deleting(db,[row]))
    return {'items':service.list(user['id'],conversation_id)}


# 职责：把会话附件转为独立个人资料文件。
# 输入：用户填写的说明和可见性，默认私密。
# 结果与约束：持有会话租约阻止删除/清空竞态；转存成功后个人副本不随会话删除，始终释放租约。
@router.post('/{conversation_id}/files/{file_id}/promote')
async def promote(conversation_id: str, file_id: str, body: PromoteFile, request: Request, user=Depends(current_user)):
    chat=request.app.state.chat
    lease = None
    def acquire():
        nonlocal lease
        with chat.service.database.connect() as db:chat.chat_files.owned(db,user['id'],conversation_id,file_id)
        lease = chat.memory.acquire(conversation_id)
    try:
        await upload_work(acquire)
        result=await chat.chat_files.promote(user,conversation_id,file_id,body.caption,body.visibility,body.folder,body.new_name)
        return {**result,'items':await asyncio.to_thread(chat.chat_files.list,user['id'],conversation_id)}
    finally:
        if lease is not None:await upload_work(chat.memory.release,conversation_id,lease)


# 职责：约束纯文本模型兜底使用的用户文件说明，不能伪造解析结果。
class FileDescription(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    description: str=Field(max_length=2000)


# 职责：保存当前会话文件的补充说明。
# 输入：认证账户、父会话、原件 ID 与两千字内说明。
# 结果与约束：所有权与生成租约再次核验，不改变文件隐私或转存副本。
@router.patch('/{conversation_id}/files/{file_id}')
def describe(conversation_id: str,file_id: str,body: FileDescription,request: Request,user=Depends(current_user)):
    return {'items':request.app.state.chat.chat_files.describe(user['id'],conversation_id,file_id,body.description)}
