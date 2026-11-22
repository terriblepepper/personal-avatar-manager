"""Bind the current actor's authorized retrieval and sandbox to one execution turn."""
import json

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from avatar.core.text_budget import AgentText
from avatar.core.execution import check_execution_async, run_blocking
from avatar.rag.reading import ReadingNeedsInput


class ToolQuestion(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    query: AgentText = Field(min_length=1)


class SearchQuestion(ToolQuestion):
    folder: str = Field(default='', max_length=512)


class SessionFileRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    file_id: str | None = Field(default=None, min_length=1, max_length=80)
    query: AgentText | None = Field(default=None, description='没有 ID 时查目录；空串浏览。与 file_id 二选一。')
    offset: int = Field(default=0, ge=0, le=2**31-1)
    page: int = Field(default=1, ge=1, le=2**31-1)

    @model_validator(mode='after')
    def selection(self):
        if (self.file_id is None) == (self.query is None) or self.query is not None and self.page!=1:
            raise ValueError('给出 file_id 读取原件，或给出 query 查目录；目录分页用 offset')
        return self


def background(system):
    return json.JSONDecoder().raw_decode(system.split('\n', 1)[1])[0]


async def bind_entry_tools(chat, context, owner, aic, session, files, *, include_private=False,
                           sandbox=None, status=None, progress=None, session_file_scope=None, session_file_grants=None, session_file_owner=None):
    """Keep grants and source revisions in Python; only schemas and sanitized results reach the LLM."""
    config, key, system, history = context
    scope = (owner, aic, session, files, include_private)
    store = config.get('_run_store')
    state = await run_blocking(store.load) if store else {}
    saved = state.get('entry_material', {})
    sources = list(saved.get('sources', ()))
    revisions = {tuple(identity): revision for identity, revision in saved.get('revisions', ())}
    if revisions:
        config.update(_rag_access={'originals':True,'scope':scope,'revisions':list(revisions.items())}, _material_sources=sources)
        await run_blocking(chat.rag.validate, context)
    def remaining_output_bytes():
        from avatar.files.conversation.resources import remaining_chat_bytes
        with chat.chat_files.database.connect() as db:
            return remaining_chat_bytes(db,owner)

    config['_remaining_output_bytes'] = remaining_output_bytes
    outputs = config.setdefault('_generated_files', [])
    tools = []
    session_files = None
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
    if session_file_scope is not None:
        from avatar.files.conversation.session_files import SessionFiles
        session_files = SessionFiles(chat.chat_files, session_file_owner or owner, session_file_scope, previous_reads,
                                    grants=session_file_grants, sandbox_ids=[f['id'] for f in (*files,*config.get('_agent_saved_files',()))])
        config.update(_session_file_scope=session_file_scope, _load_session_source=session_files.html,
                      _session_read_proofs=session_files.proofs, _session_sandbox_inputs=session_files.sandbox_inputs,
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
        if config.get('vision_enabled'):
            for identifier in state.get('session_image_ids', ()):
                if identifier not in session_files.proofs:
                    raise HTTPException(409,'此前的视觉原件授权已改变')
                image = await run_blocking(session_files.read,identifier,0,1,sandbox=sandbox is not None,vision=True)
                if binary := image.pop('image_input',None):
                    attach_image(identifier,binary)
    elif previous_reads:
        raise HTTPException(409, '此前读取的原件已失去会话授权')

    def tool(name, description, schema=ToolQuestion):
        tools.append({'name':name, 'description':description, 'parameters':schema.model_json_schema()})

    tool('search_materials', '按自然语言问题混合检索本会话已授权资料的正文片段。可指定逻辑目录。', SearchQuestion)
    tool('read_material', '按问题从本会话有权读取的资料目录选择原件并读取指定内容，可按文件名或说明定位。')
    if session_files:
        tool('read_session_file', '用真实 ID 读取当前会话获准的原件（上传、生成或已分享资料）。已知 ID 直接读取；未知 ID 用 query 查目录，空串浏览，再按 can_read=true 项读取。文本/HTML 返回原文，按 next_offset 字节翻页；文档按 next.start 页和 next.offset 字符继续；二进制可交给获准的 Python 处理。', SessionFileRequest)
    if status:
        tools.append({'name':'application_status','description':'查看本人的分身申请与审核状态。',
                      'parameters':{'type':'object','properties':{},'additionalProperties':False}})
    if sandbox:
        tool('python', '运行获准的沙箱计算或技能脚本；query 说明需验证的计算、输入和期望产物。')

    async def run_python(query, *, restoring=False):
        if sandbox is None:
            raise HTTPException(403,'当前身份或会话未授权执行沙箱')
        cfg, _, enriched, _ = await sandbox(({**config, '_restore_only':restoring}, key, system, history), query)
        for name in ('_validate_sandbox','_sandbox_succeeded','_sandbox_delivered','_agent_checkpoint','_workflow_notices'):
            if name in cfg:
                config[name] = cfg[name]
        known = {item['id'] for item in outputs}
        outputs.extend(item for item in cfg.get('_generated_files', ()) if item['id'] not in known)
        records = background(enriched).get('skill_execution_results', [])
        if not restoring and not records:
            raise ValueError('沙箱未执行，缺少实际计算回执')
        if not restoring and records[-1].get('status')!='success':
            last = records[-1]
            raise ValueError('沙箱执行失败：'+str(last.get('error') or last.get('stderr') or '没有成功回执')[:500])
        return json.dumps({'results':records}, ensure_ascii=False)

    # Recover files and recheck source grants even when the next model step only writes the final answer.
    if state.get('sandbox') or config.get('_sandbox_receipts') or config.get('_agent_checkpoint',{}).get('sandbox_attempted'):
        if not sandbox:
            raise HTTPException(409, '此前沙箱权限已撤回，未恢复其结果')
        await run_python('', restoring=True)

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
                result = await run_blocking(session_files.discover, request.query, request.offset, outputs)
            else:
                from avatar.conversation.working_context import input_budget
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
            if store:
                await run_blocking(store.merge, {'session_file_reads':dict(session_files.proofs),
                                                'session_image_ids':list(read_images)})
            return json.dumps(result, ensure_ascii=False)
        if name=='application_status' and status:
            if args:
                raise ValueError('申请状态工具无需参数')
            return await run_blocking(status)
        if name=='python':
            return await run_python(ToolQuestion.model_validate(args).query)
        if name not in ('read_material','search_materials'):
            raise ValueError('当前会话未提供此工具')
        request = (SearchQuestion if name=='search_materials' else ToolQuestion).model_validate(args)
        route = {'mode':'search' if name=='search_materials' else 'read', **request.model_dump()}
        # A new lookup must not inherit proof or excerpts from an earlier successful lookup.
        lookup={k:v for k,v in config.items() if k not in ('_rag_access','_material_sources','_image_inputs','_workflow_notices')}
        data=background(system)
        for field in ('file_evidence','file_catalog','material_readings'):
            data.pop(field,None)
        lookup_system=system.split('\n',1)[0]+'\n'+json.dumps(data,ensure_ascii=False)
        selected = await chat.rag.enrich(owner,aic,session,files,({**lookup,'_answer_route':route},key,lookup_system,history),
                                         request.query,include_private=include_private,progress=progress)
        try:
            resolved, message = await chat.rag.answer_context(selected, request.query, progress=progress)
        except ReadingNeedsInput as missing:
            await check_execution_async(validate)
            return json.dumps({'query':request.query,'has_evidence':False,'matches':[],
                               'sources':[],'notices':[str(missing)]},ensure_ascii=False)
        cfg = resolved[0]
        data = background(resolved[2])
        evidence = data.get('file_evidence', [])
        reading = message[len(request.query):].strip()
        # Older protected observations stay bound to their original revisions across compaction and restart.
        await check_execution_async(validate)
        proof = cfg.get('_rag_access', {})
        visible_ids = {item['file_id'] for item in cfg.get('_material_sources', ())}
        visible_ids.update(item['file_id'] for item in evidence if item.get('file_id'))
        for identity, revision in proof.get('revisions', ()):
            if identity[1] not in visible_ids:
                continue
            identity = tuple(identity)
            if identity in revisions and revisions[identity] != revision:
                raise HTTPException(409,'资料或权限已变化，请重新提问')
            revisions[identity] = revision
        for source in cfg.get('_material_sources', ()):
            if source not in sources:
                sources.append(source)
        config.update(_rag_access={'originals':True,'scope':scope,'revisions':list(revisions.items())}, _material_sources=sources)
        if cfg.get('_image_inputs'):
            other_images[:] = cfg['_image_inputs']
            config['_image_inputs'] = (list(config.get('_explicit_image_inputs',()))+list(read_images.values())+other_images)[:5]
        if store:
            await run_blocking(store.merge, {'entry_material':{'sources':sources,'revisions':list(revisions.items())}})
        await check_execution_async(validate)
        proven_ids={identity[1] for identity,_ in proof.get('revisions',())}
        original=any(item.get('file_id') in proven_ids and item.get('type')=='源文件'
                     for item in cfg.get('_material_sources',()))
        matched=any(item.get('file_id') in proven_ids and str(item.get('text') or '').strip() for item in evidence)
        return json.dumps({'query':request.query,'has_evidence':original or (name=='search_materials' and matched),
                           'matches':evidence,'reading':reading,
                           'sources':cfg.get('_material_sources',[]),'notices':cfg.get('_workflow_notices',[])},ensure_ascii=False)

    config.update(_entry_tools=tools,_execute_entry_tool=execute,_validate_entry_results=validate)
    return context
