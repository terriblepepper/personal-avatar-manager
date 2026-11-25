"""本人知识库与 soul 生命周期；管理员审核统一进入审核与审计。"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from avatar.api.file_previews import word_pdf
from pydantic import BaseModel, ConfigDict, Field
from starlette.formparsers import MultiPartParser, MultiPartException
from avatar.accounts.auth import current_user
from avatar.knowledge.media import SOUL_FORMATS, supports_asset
from avatar.files.uploads.batches import batch_id
from typing import Literal
import re
import asyncio

router=APIRouter(prefix='/api/me/knowledge',tags=['知识库与 soul'])


def owner(user=Depends(current_user)):
    if user.get('access_scope','full')!='full':raise HTTPException(403,'知识库仅允许本人完整会话访问')
    return user


class BuildRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    prompt:str=Field(default='',max_length=20000)
    asset_ids:list[str]=Field(default_factory=list)
    interview_rounds:int=Field(default=3,ge=0,le=6,strict=True)
    conversation_ids:list[str]=Field(default_factory=list,max_length=20)
    rebuild:bool=Field(default=False,strict=True)


@router.get('')
def overview(request:Request,user=Depends(owner)):
    return request.app.state.knowledge.view(user['id'])


class UnsupportedAsset(MultiPartException):
    pass


class AssetUploadParser(MultiPartParser):
    def __init__(self, headers, stream, file_budget):
        super().__init__(headers,stream,max_files=1,max_fields=3,max_part_size=65536)
        self.file_budget = file_budget
        self.file_bytes = 0
        self.header_bytes = 0

    def count_header(self,count):
        self.header_bytes += count
        if self.header_bytes > 65536:
            raise MultiPartException('素材表单头部过大')

    def on_header_field(self,data,start,end):
        self.count_header(end-start)
        super().on_header_field(data,start,end)

    def on_header_value(self,data,start,end):
        self.count_header(end-start)
        super().on_header_value(data,start,end)

    def on_headers_finished(self):
        super().on_headers_finished()
        file = self._current_part.file
        if file is not None:
            self.file_bytes = 0
            if not supports_asset(file.filename or ''):
                raise UnsupportedAsset(SOUL_FORMATS)

    def on_part_data(self,data,start,end):
        if self._current_part.file is not None:
            self.file_bytes += end-start
            if self.file_bytes > self.file_budget:
                raise MultiPartException('素材超过可用额度')
        super().on_part_data(data,start,end)


@router.post('/assets',status_code=201)
async def upload(request:Request,user=Depends(owner)):
    from avatar.files.uploads.buffer import upload_buffer
    knowledge = request.app.state.knowledge
    budget = await asyncio.to_thread(knowledge.upload_budget,user['id'])
    async def limited_stream():
        total=0
        async for chunk in request.stream():
            total+=len(chunk)
            if total>budget+65536:raise MultiPartException('知识库上传超过大小限制')
            yield chunk
    try:
        form = await AssetUploadParser(request.headers,limited_stream(),budget).parse()
    except UnsupportedAsset as error:
        raise HTTPException(415,str(error)) from None
    except MultiPartException:
        raise HTTPException(413,'上传超过可用空间、文件数或字段数量限制') from None
    try:
        file=form.get('file');caption=form.get('caption','');relative_path=form.get('relative_path','')
        if not hasattr(file,'read') or not isinstance(caption,str) or not isinstance(relative_path,str):raise HTTPException(422,'请提交素材文件与文字说明')
        with upload_buffer(file.file) as data:
            return await knowledge.upload(user,file.filename,data,caption,relative_path,batch_id=batch_id(request))
    finally:await form.close()


@router.get('/assets/{identifier}')
def download(identifier:str,request:Request,user=Depends(owner),preview_pdf:bool=False):
    knowledge = request.app.state.knowledge
    if preview_pdf:
        return word_pdf(lambda: knowledge.file(user['id'], identifier))
    path,item=knowledge.file(user['id'],identifier)
    return FileResponse(path,media_type=item['mime'],filename=item['filename'])


@router.delete('/assets/{identifier}')
def delete_asset(identifier:str,request:Request,user=Depends(owner)):
    return request.app.state.knowledge.delete_asset(user,identifier)


class CaptionRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    caption:str=Field(default='',max_length=2000)


@router.patch('/assets/{identifier}')
def edit_caption(identifier:str,body:CaptionRequest,request:Request,user=Depends(owner)):
    return request.app.state.knowledge.edit_caption(user,identifier,body.caption)


@router.post('/builds',status_code=202)
def build(body:BuildRequest,request:Request,user=Depends(owner)):
    return request.app.state.knowledge.create(user,body.prompt,body.asset_ids,body.interview_rounds,body.conversation_ids,body.rebuild)


class SoulRebuild(BaseModel):
    model_config=ConfigDict(extra='forbid')


@router.post('/rebuild',status_code=202)
def rebuild(request:Request,body:SoulRebuild | None=None,user=Depends(owner)):
    return request.app.state.knowledge.rebuild_index(user)


@router.post('/builds/{identifier}/retry',status_code=202)
def retry_build(identifier:str,request:Request,user=Depends(owner)):
    return request.app.state.knowledge.retry(user,identifier)


@router.get('/memory-tree')
async def memory_tree(request:Request,user=Depends(owner)):
    return await request.app.state.knowledge.memory.tree(user['id'])


@router.get('/builds/{identifier}/memory-tree')
async def revision_memory_tree(identifier:str,request:Request,user=Depends(owner)):
    return await request.app.state.knowledge.memory.tree(user['id'],identifier)


class MemoryEdit(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    id:str=Field(pattern=r'^[a-f0-9]{64}$')
    title:str=Field(max_length=200)
    summary:str=Field(max_length=2000)
    content:str=Field(max_length=8000)
    deleted:bool=Field(default=False,strict=True)


class AnswerRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    answer:str=Field(min_length=1,max_length=4000)
    revision:str=Field(min_length=1,max_length=80)


class ChoiceAnswer(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    id:str=Field(min_length=1,max_length=80)
    choice:Literal['A','B','C','D','E']
    other:str=Field(default='',max_length=4000)


class QuestionnaireRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    revision:str=Field(min_length=1,max_length=80)
    answers:list[ChoiceAnswer]=Field(default_factory=list,max_length=150)
    submit:bool=Field(default=False,strict=True)


@router.post('/builds/{identifier}/questionnaire')
def questionnaire(identifier:str,body:QuestionnaireRequest,request:Request,user=Depends(owner)):
    return request.app.state.knowledge.questionnaire(user,identifier,body.answers,body.revision,body.submit)


class RefineRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    revision:str=Field(min_length=1,max_length=80)
    document:dict[str,str]=Field(min_length=7,max_length=7)
    asset_ids:list[str]=Field(default_factory=list)
    prompt:str|None=Field(default=None,max_length=20000)
    memory_edits:list[MemoryEdit]=Field(default_factory=list,max_length=100)
    memory_revision:str|None=Field(default=None,max_length=64)


@router.post('/builds/{identifier}/refine')
async def refine(identifier:str,body:RefineRequest,request:Request,user=Depends(owner)):
    knowledge=request.app.state.knowledge;tree=None
    if body.memory_edits:
        tree=await knowledge.memory.tree(user['id'],identifier)
        if tree['revision']!=body.memory_revision:raise HTTPException(409,'记忆树已更新，请重新打开后修改')
        known={node['id'] for node in tree['nodes']}
        if len({item.id for item in body.memory_edits})!=len(body.memory_edits) or any(item.id not in known for item in body.memory_edits):raise HTTPException(422,'记忆节点重复或不属于本次修订')
        if sum(len(item.content)+len(item.summary)+len(item.title) for item in body.memory_edits)>60000:raise HTTPException(422,'记忆修改合计最多 60000 字')
        if any(not item.deleted and (not item.title or not item.content) for item in body.memory_edits):raise HTTPException(422,'保留的记忆需要填写标题与内容')
        if any('<!--' in value or re.search(r'(?m)^###\s',value) for item in body.memory_edits for value in (item.title,item.summary,item.content)) or any('\n' in item.title or '\r' in item.title for item in body.memory_edits):raise HTTPException(422,'记忆内容不能包含内部条目标记或章节标题')
    originals={node['id']:node for node in tree['nodes']} if tree else {}
    return await asyncio.to_thread(knowledge.refine,user,identifier,body.document,body.revision,body.asset_ids,body.prompt,[{**item.model_dump(),'before':originals[item.id]} for item in body.memory_edits],tree)


@router.post('/builds/{identifier}/answer',status_code=202)
def answer(identifier:str,body:AnswerRequest,request:Request,user=Depends(owner)):
    return request.app.state.knowledge.answer(user,identifier,body.answer,body.revision)


@router.post('/builds/{identifier}/submit')
def submit(identifier:str,request:Request,user=Depends(owner)):
    result = request.app.state.knowledge.submit(user,identifier)
    request.app.state.pending_reviews.invalidate()
    return result


@router.post('/builds/{identifier}/cancel')
def cancel(identifier:str,request:Request,user=Depends(owner)):
    result = request.app.state.knowledge.cancel(user,identifier)
    request.app.state.pending_reviews.invalidate()
    return result


@router.post('/builds/delete-all')
def delete_all_builds(request:Request,user=Depends(owner)):
    return request.app.state.knowledge.delete_all_builds(user)


@router.delete('/builds/{identifier}')
def delete_build(identifier:str,request:Request,user=Depends(owner)):
    return request.app.state.knowledge.delete_build(user,identifier)


@router.delete('/active')
def disable(request:Request,user=Depends(owner)):
    return request.app.state.knowledge.disable(user)


class MaterialReference(BaseModel):
    model_config=ConfigDict(extra='forbid')
    attachment_id:str=Field(min_length=1,max_length=80)
    folder:str=Field(default='',max_length=1024)


@router.post('/assets/import',status_code=201)
async def import_material(body:MaterialReference,request:Request,user=Depends(owner)):
    return await request.app.state.knowledge.import_material(user,body.attachment_id,body.folder)


from avatar.files.library.folders import FolderRequest,FolderRenameRequest,NameRequest


@router.get('/folders')
def knowledge_folders(request:Request,user=Depends(owner)):
    from avatar.knowledge.library import folders
    return folders(request.app.state.knowledge,user['id'])


@router.post('/folders',status_code=201)
def create_folder(body:FolderRequest,request:Request,user=Depends(owner)):
    from avatar.knowledge.library import folder
    return folder(request.app.state.knowledge,user,body,'create')


@router.patch('/folders')
def edit_folder(body:FolderRenameRequest,request:Request,user=Depends(owner)):
    from avatar.knowledge.library import folder
    return folder(request.app.state.knowledge,user,body,'edit')


@router.delete('/folders')
def remove_folder(body:FolderRequest,request:Request,user=Depends(owner)):
    from avatar.knowledge.library import folder
    return folder(request.app.state.knowledge,user,body,'delete')


@router.patch('/assets/{identifier}/name')
def rename_asset(identifier:str,body:NameRequest,request:Request,user=Depends(owner)):
    from avatar.knowledge.library import rename
    return rename(request.app.state.knowledge,user,identifier,body)


@router.post('/learning/retry',status_code=202)
def retry_learning(request:Request,user=Depends(owner)):
    return request.app.state.knowledge.learning.retry(user)


@router.get('/assets')
def assets(request:Request,user=Depends(owner)):
    with request.app.state.database.connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM knowledge_assets WHERE user_id=? ORDER BY created_at DESC',(user['id'],))]


from avatar.files.library.batch import BatchRequest


@router.post('/assets/batch')
def batch_assets(body:BatchRequest,request:Request,user=Depends(owner)):
    return request.app.state.knowledge.batch(user,body)
