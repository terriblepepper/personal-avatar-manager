import asyncio
import base64
from tempfile import TemporaryFile
from urllib.parse import unquote
from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi import Query
from fastapi.responses import FileResponse
from avatar.api.file_previews import word_pdf
from avatar.files.library.folders import MaterialFolders
from avatar.files.library.folders import FolderRequest
from avatar.files.library.folders import NameRequest
from avatar.files.library.folders import FolderRenameRequest
from avatar.files.library.folders import rename_folder
from avatar.files.library.folders import rename_file
from avatar.core.artifact_limits import MAX_SOURCE_BYTES
from avatar.files.uploads.batches import batch_id as upload_batch_id
from avatar.files.uploads.batches import UploadComplete, complete
from avatar.core.revisions import ModifiedAt
from avatar.files.uploads.validation import ARTIFACT_SUFFIXES
from avatar.files.library.attachments import AttachmentDescriptionRequest
import avatar.accounts.auth as auth
from avatar.files.library.batch import BatchRequest, apply
from avatar.api.dependencies import admin
from avatar.files.uploads.buffer import upload_buffer, upload_work
from fastapi import HTTPException


router = APIRouter()


# 职责：接收本人选填资料原件，实际读取时限制大小。
# 输入：原始文件请求体、URL 编码文件名、可选文件集合日期和旧客户端说明请求头；会话与 CSRF 由依赖校验。
# 结果与约束：账户资料文件总额度 10 GiB，上传说明选填且最多 2000 字符，默认私密暂存；不影响资料提交。
@router.get('/me/material-folders')
def material_folders(request:Request,user=Depends(auth.current_user)):
    return MaterialFolders(request.app.state.service.attachments).list(user['id'])


@router.post('/me/material-folders',status_code=201)
def create_material_folder(body:FolderRequest,request:Request,user=Depends(auth.current_user)):
    return MaterialFolders(request.app.state.service.attachments).create(user,body)


@router.patch('/me/material-folders')
def rename_material_folder(body:FolderRenameRequest,request:Request,user=Depends(auth.current_user)):
    return rename_folder(request.app.state.service.attachments,user,body)


@router.patch('/me/attachments/{attachment_id}/name')
def rename_material_file(attachment_id:str,body:NameRequest,request:Request,user=Depends(auth.current_user)):
    return rename_file(request.app.state.service.attachments,user,attachment_id,body)


@router.delete('/me/material-folders')
def delete_material_folder(body:FolderRequest,request:Request,user=Depends(auth.current_user)):
    files=request.app.state.service.attachments
    result=files.remove(user,last_modified_at=body.last_modified_at,folder_path=body.path,recursive=body.recursive)
    return {**result,**MaterialFolders(files).list(user['id']),'files':files.inventory(user['id'])}


@router.post('/me/attachments', status_code=201)
async def upload_attachment(request: Request, filename: str | None = Query(default=None, min_length=1, max_length=200), last_modified_at: ModifiedAt | None = Query(default=None), user=Depends(auth.current_user)):
    encoded = request.headers.get('x-attachment-caption-base64')
    try:
        # UTF-8 Base64 避免长中文说明的百分号编码超过常见 HTTP 头大小上限。
        caption = (base64.b64decode(encoded, validate=True).decode('utf-8') if encoded is not None
                   else unquote(request.headers.get('x-attachment-caption', ''))).strip()
    except (ValueError, UnicodeError):
        raise HTTPException(422, '资料说明编码无效')
    if len(caption) > 2000:
        raise HTTPException(422, '资料说明最多 2000 字')
    if filename is None:
        filename = unquote(request.headers.get("x-attachment-filename", ""))
    files = request.app.state.service.attachments
    def read_quota():
        with files.database.connect() as db:
            return files.quota(db,user['id'])
    quota = await asyncio.to_thread(read_quota)
    remaining = max(0,quota['limit_bytes']-quota['used_bytes'])
    artifact_upload = filename.lower().endswith(ARTIFACT_SUFFIXES)
    limit = min(remaining,MAX_SOURCE_BYTES) if artifact_upload else remaining
    reason = 'HTML 文件不能超过 100 MiB' if artifact_upload and limit==MAX_SOURCE_BYTES else '资料文件剩余额度不足'
    if int(request.headers.get('content-length') or 0) > limit:
        raise HTTPException(413,reason)
    with TemporaryFile() as payload:
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise HTTPException(413,reason)
            await upload_work(payload.write,chunk)
        with upload_buffer(payload) as data:
            return await files.upload(user,last_modified_at,filename,data,caption,
                relative_path=unquote(request.headers.get("x-attachment-relative-path","")),batch_id=upload_batch_id(request))


# 职责：提供个人资料文件管理所需的完整本人文件清单。
# 输入：request 定位应用服务，user 为认证账户，包括受限个人资料会话。
# 结果与约束：返回暂存与已保存独立文件元数据，不暴露磁盘路径或他人资料，不改变任何审核快照。
@router.get('/me/attachments')
def list_attachments(request: Request, user=Depends(auth.current_user)):
    return request.app.state.service.attachments.inventory(user['id'])


# 职责：提供本人附件原件下载。
# 输入：attachment_id 指定文件，user 为已认证所有人。
# 结果与约束：不能通过 ID 访问他人文件；强制附件下载且禁止缓存，不在页面内执行文件内容。
@router.get('/me/attachments/{attachment_id}')
def own_attachment(attachment_id: str, request: Request, user=Depends(auth.current_user), preview_pdf: bool=False):
    files = request.app.state.service.attachments
    if preview_pdf:
        return word_pdf(lambda: files.file_for(user, attachment_id))
    path, row = files.file_for(user, attachment_id)
    return FileResponse(path, media_type=row['content_type'], filename=row['filename'], headers={'Content-Security-Policy': "sandbox; default-src 'none'"})


# 职责：让管理员读取审核所需附件原件。
# 输入：attachment_id 为快照引用文件，user 必须通过 admin 依赖。
# 结果与约束：管理员可核验私密材料，普通账户及临时资料会话不能访问管理路径。
@router.get('/admin/attachments/{attachment_id}')
def review_attachment(attachment_id: str, request: Request, user=Depends(admin), preview_pdf: bool=False):
    files = request.app.state.service.attachments
    if preview_pdf:
        return word_pdf(lambda: files.file_for(user, attachment_id, admin=True))
    path, row = files.file_for(user, attachment_id, admin=True)
    return FileResponse(path, media_type=row['content_type'], filename=row['filename'], headers={'Content-Security-Policy': "sandbox; default-src 'none'"})


# 职责：提供目标所有人当前已保存公开文件下载。
# 输入：aic 指定当前分身，attachment_id 指定其公开文件。
# 结果与约束：暂存、私密、已删除或不属于目标的文件统一拒绝，保存为私密后旧链接立即失效。
@router.get('/avatars/{aic}/attachments/{attachment_id}')
def public_attachment(aic: str, attachment_id: str, request: Request):
    path, row = request.app.state.service.attachments.public_file(aic, attachment_id)
    return FileResponse(path, media_type=row['content_type'], filename=row['filename'], headers={'Content-Security-Policy': "sandbox; default-src 'none'"})


# 职责：彻底删除本人附件原件及全部资料引用和说明。
# 输入：attachment_id 为本人文件，last_modified_at 可选校验文件集合日期；user 经认证和 CSRF 校验。
# 结果与约束：当前及历史文件立即清除；撤销公开下载、推进受影响的生效日期以排除旧记忆，失败回滚。
@router.delete('/me/attachments/{attachment_id}')
def remove_attachment(attachment_id: str, request: Request, last_modified_at: ModifiedAt | None = Query(default=None), user=Depends(auth.current_user)):
    return request.app.state.service.attachments.remove(user, attachment_id, last_modified_at)


# 职责：让本人修改当前或历史文件说明。
# 输入：attachment_id 为本人文件，body 含非空说明和可选日期，user 经认证及 CSRF 检查。
# 结果与约束：历史文件可管理，说明和可见性保存立即生效，无需资料审核；不能修改他人文件或伪造路径。
@router.patch('/me/attachments/{attachment_id}')
def update_attachment_description(attachment_id: str, body: AttachmentDescriptionRequest, request: Request, user=Depends(auth.current_user)):
    return request.app.state.service.attachments.update_description(user,attachment_id,body)


@router.post('/me/attachments/batch')
def batch_attachments(body:BatchRequest,request:Request,user=Depends(auth.current_user)):
    return apply(request.app.state.service.attachments,user,body)


@router.post('/me/upload-batches/{batch_id}/complete')
def complete_upload(batch_id:str,body:UploadComplete,request:Request,user=Depends(auth.current_user)):
    return complete(request.app.state.database,user,batch_id,body)
