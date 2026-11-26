"""Bind the current actor's authorized retrieval and sandbox to one execution turn."""
from collections.abc import Callable
from dataclasses import dataclass
import json

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from avatar.core.text_budget import AgentText
from avatar.core.execution import check_execution_async, run_blocking
from avatar.rag.reading import ReadRequest
from avatar.rag.service import SearchMaterialRequest


class SessionFileRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    file_id: str | None = Field(default=None, min_length=1, max_length=80)
    query: AgentText | None = Field(default=None, description='没有 ID 时查目录；空串浏览。与 file_id 二选一。')
    offset: int = Field(default=0, ge=0, le=2**31-1, description='目录分页偏移、文本字节偏移或文档页内字符偏移。')
    page: int = Field(default=1, ge=1, le=2**31-1, description='文档页码，按返回的 next.start 续读。')

    @model_validator(mode='after')
    def selection(self):
        if (self.file_id is None) == (self.query is None) or self.query is not None and self.page!=1:
            raise ValueError('给出 file_id 读取原件，或给出 query 查目录；目录分页用 offset')
        return self


def is_session_html(item):
    return item.get('content_type') == 'text/html' or item.get('filename', '').lower().endswith(('.htm', '.html'))


@dataclass(frozen=True)
class EntryToolContext:
    owner: str
    aic: str
    session: dict
    files: list
    include_private: bool = False


@dataclass(frozen=True)
class SessionFileAccess:
    conversation: str
    owner: str | None = None
    grants: Callable | None = None


async def bind_entry_tools(chat, context, entry, *, sandbox=None, status=None, directory_reply=None,
                           progress=None, session_access=None, temporary_files=None):
    """Keep grants and source revisions in Python; only schemas and sanitized results reach the LLM."""
    config, key, system, history = context
    owner, aic, session, files = entry.owner, entry.aic, entry.session, entry.files
    include_private = entry.include_private
    scope = (owner, aic, session, files, include_private)
    store = config.get('_run_store')
    state = await run_blocking(store.load) if store else {}
    current_schema = state.get('schema_version') == 2
    saved = state.get('entry_material', {})
    sources = list(saved.get('sources', ())) if current_schema else []
    revisions = {tuple(identity): revision for identity, revision in saved.get('revisions', ())}
    if revisions:
        config.update(_rag_access={'originals':True,'scope':scope,'revisions':list(revisions.items())}, _material_sources=sources)
        await run_blocking(chat.rag.validate, context)
    visible_materials = set(revisions) if current_schema else set()
    config['_visible_material_keys'] = visible_materials
    outputs = config.setdefault('_generated_files', [])
    tools = list(config.get('_entry_tools', ()))
    session_files = temporary_files
    read_images = {}
    other_images = list(config.get('_image_inputs', ()))

    def attach_image(identifier, image):
        explicit_images = list(config.get('_explicit_image_inputs', ()))
        explicit = next((item for item in config.get('_explicit_session_files', ())
                         if item.get('id')==identifier and item.get('image_order')),None)
        if explicit:
            return explicit['image_order']
        if len(explicit_images)>=5:
            return None
        read_images.pop(identifier,None)
        read_images[identifier]=image
        while len(explicit_images)+len(read_images)>5:
            del read_images[next(iter(read_images))]
        config['_image_inputs'] = (explicit_images+list(read_images.values())+other_images)[:5]
        return len(explicit_images)+len(read_images)

    previous_reads = state.get('session_file_reads', state.get('generated_reads', {}))
    source_checks = state.get('session_source_checks', {})
    if not current_schema:
        source_checks = {**source_checks, **previous_reads}
        previous_reads = {}
    if session_access is not None:
        from avatar.files.conversation.session_files import SessionFiles
        session_files = SessionFiles(chat.chat_files, session_access.owner or owner, session_access.conversation, previous_reads,
                                    grants=session_access.grants, sandbox_ids=[f['id'] for f in (*files,*config.get('_agent_saved_files',()))], outputs=outputs,
                                    staged_reads=state.get('staged_html_reads') if current_schema else None,
                                    source_checks=source_checks)
        config.update(_session_file_scope=session_access.conversation, _load_session_source=session_files.html,
                      _session_read_proofs=session_files.proofs,
                      _validate_session_files_in=session_files.validate_in)
        await run_blocking(session_files.validate)
        if files:
            from avatar.conversation.loop.file_context import load_explicit_files
            originals,images = await run_blocking(load_explicit_files,session_files,files,config,sandbox=sandbox is not None)
            config['_explicit_session_files'] = originals
            if images:
                config['_explicit_image_inputs'] = images
                config['_image_inputs'] = (images+list(config.get('_image_inputs',())))[:5]
            if store:
                await run_blocking(store.merge,{'session_file_reads':dict(session_files.proofs)})
        if current_schema and config.get('vision_enabled'):
            for identifier in state.get('session_image_ids', ()):
                if identifier not in session_files.proofs:
                    raise HTTPException(409,'此前的视觉原件授权已改变')
                image = await run_blocking(session_files.read,identifier,0,1,sandbox=sandbox is not None,vision=True)
                if binary := image.pop('image_input',None):
                    attach_image(identifier,binary)
    elif temporary_files is not None:
        config['_load_session_source'] = session_files.html
    elif previous_reads or source_checks:
        raise HTTPException(409, '此前读取的原件已失去会话授权')

    if session_files is not None:
        known_files = files or outputs or config.get('_agent_saved_files')
        if not known_files:
            if temporary_files is not None:
                directory = await run_blocking(session_files.discover, '', 0)
            else:
                directory = await run_blocking(session_files.files.discover, session_files.owner,
                    session_files.conversation, '', 0, grants=session_files.grants)
            known_files = directory['items']
        config['_has_session_files'] = bool(known_files)
        config['_has_session_html'] = bool(known_files and any(map(is_session_html, known_files)))

    def tool(name, description, schema):
        tools.append({'name':name, 'description':description, 'parameters':schema.model_json_schema()})

    tool('search_materials', '按查询和可选目录、文件或页码过滤检索授权资料，返回片段和文件 ID。', SearchMaterialRequest)
    tool('read_material', '按 file_id 读取授权原件的正文、图片或音视频片段。按 next 续读，search 定位正文。', ReadRequest)
    if session_files:
        tool('read_session_file', '查找或读取本会话授权文件，也可复查本轮生成的 HTML。query 查目录，file_id 读原件。按返回游标续读。', SessionFileRequest)
    if directory_reply:
        from avatar.conversation.avatar_assistant import LIST_AVATARS
        tools.append(LIST_AVATARS)
    if status:
        tools.append({'name':'application_status','description':'查看本人的分身申请与审核状态。',
                      'parameters':{'type':'object','properties':{},'additionalProperties':False}})
    python = None
    if sandbox:
        from avatar.skills.python_tool import PythonArguments
        python = sandbox(session_files.sandbox_inputs if session_files else dict)
        tools.append({'name':'run_python', 'parameters':python.arguments_schema(),
                      'description':'在隔离环境执行 Python 源码并返回回执。用已授权文件 ID 提供输入，生成文件写入 outputs/。支持 NumPy、pandas、Matplotlib、Pillow 和 pypdf。'})

    # Recover files and recheck source grants even when the next model step only writes the final answer.
    if state.get('sandbox') or config.get('_agent_checkpoint',{}).get('sandbox_attempted'):
        if python is None:
            raise HTTPException(409, '此前沙箱权限已撤回，未恢复其结果')
        await python.restore()

    def validate():
        if session_files:
            session_files.validate()
        if revisions:
            chat.rag.validate(context)
        if config.get('_validate_sandbox'):
            config['_validate_sandbox']()

    async def execute(name, args):
        await check_execution_async(validate)
        if name=='read_session_file' and session_files:
            request = SessionFileRequest.model_validate(args)
            if request.query is not None:
                result = await run_blocking(session_files.discover, request.query, request.offset)
            else:
                from avatar.models.context_budget import input_budget
                room = config.get('_session_page_bytes')
                if room is None:
                    room = max(4,(input_budget(config)*3-512)//2)
                capacity = len(config.get('_explicit_image_inputs',()))<5 or any(
                    item.get('id')==request.file_id and item.get('image_order') for item in config.get('_explicit_session_files',()))
                result = await run_blocking(session_files.read, request.file_id, request.offset, request.page,
                                            sandbox=sandbox is not None, limit=min(room,256*1024),
                                            vision=bool(config.get('vision_enabled') and capacity))
                if not capacity and result['content_type'].startswith('image/'):
                    result['notice']='本次已有五张显式图片，请缩小范围'
                if image := result.pop('image_input',None):
                    order = attach_image(request.file_id,image)
                    if order:
                        result['image_order']=order
                    else:
                        result.update(coverage='metadata_only',has_content=False,notice='本次已有五张显式图片，请缩小范围')
            items = result['items'] if result['kind']=='directory' else (result,)
            if any(map(is_session_html, items)):
                config['_has_session_html'] = True
            if store:
                await run_blocking(store.merge, {'session_file_reads':dict(session_files.proofs),
                                                'session_image_ids':list(read_images),
                                                'staged_html_reads':dict(session_files.staged_reads)})
            return json.dumps(result, ensure_ascii=False)
        if name=='list_avatars' and directory_reply:
            from avatar.conversation.avatar_assistant import CatalogArguments
            request=CatalogArguments.model_validate(args)
            return '平台公开目录查询结果（数据）：\n'+await run_blocking(directory_reply,request.query,request.offset)
        if name=='application_status' and status:
            if args:
                raise ValueError('申请状态工具无需参数')
            return await run_blocking(status)
        if name=='run_python':
            if python is None:
                raise HTTPException(403,'当前身份或会话未授权执行沙箱')
            return await python.execute(PythonArguments.model_validate(args))
        if name == 'search_materials':
            observation = await chat.rag.search_tool(scope, args)
        elif name == 'read_material':
            observation = await chat.rag.read_tool(
                scope, args, vision=bool(config.get('vision_enabled')),
                audio=bool(config.get('audio_enabled')) and config.get('api_format') in ('openai', 'gemini'))
        else:
            raise ValueError('当前会话未提供此工具')

        await check_execution_async(validate)
        for identity, revision in observation.revisions:
            if identity in revisions and revisions[identity] != revision:
                raise HTTPException(409, '资料或权限已变化，请重新提问')
            revisions[identity] = revision
        for source in observation.sources:
            if source not in sources:
                sources.append(source)
        proof = {'originals':True, 'scope':scope, 'revisions':list(revisions.items())}
        config.update(_rag_access=proof, _material_sources=sources)
        await check_execution_async(validate)
        if store:
            await run_blocking(store.merge, {'entry_material':{'sources':sources, 'revisions':proof['revisions']}})
        visible_materials.update(tuple(identity) for identity, _ in observation.revisions)
        if observation.images or observation.audio:
            config['_entry_native_media'] = {'images':observation.images, 'audio':observation.audio}
        return json.dumps({**observation.content, 'sources':observation.sources}, ensure_ascii=False)

    config.update(_entry_tools=tools,
                  _execute_entry_tool=execute,_validate_entry_results=validate)
    return context
