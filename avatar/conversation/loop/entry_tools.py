"""Bind the current actor's authorized retrieval and sandbox to one execution turn."""
import json

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from avatar.core.execution import check_execution_async, run_blocking


class ToolQuestion(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=2000)


class SearchQuestion(ToolQuestion):
    folder: str = Field(default='', max_length=512)


def background(system):
    return json.JSONDecoder().raw_decode(system.split('\n', 1)[1])[0]


async def bind_entry_tools(chat, context, owner, aic, session, files, *, include_private=False,
                           sandbox=None, status=None, progress=None):
    """Keep grants and source revisions in Python; only schemas and sanitized results reach the LLM."""
    config, key, system, history = context
    scope = (owner, aic, session, files, include_private)
    store = config.get('_run_store')
    state = await run_blocking(store.load) if store else {}
    saved = state.get('entry_material', {})
    sources = list(saved.get('sources', ()))
    revisions = {tuple(identity): revision for identity, revision in saved.get('revisions', ())}
    if revisions:
        config.update(_rag_access={'scope':scope,'revisions':list(revisions.items())}, _material_sources=sources)
        await run_blocking(chat.rag.validate, context)
    outputs = config.setdefault('_generated_files', [])
    tools = []

    def tool(name, description, schema=ToolQuestion):
        tools.append({'name':name, 'description':description, 'parameters':schema.model_json_schema()})

    tool('search_materials', '按自然语言问题混合检索本会话已授权资料的正文片段。可指定逻辑目录。', SearchQuestion)
    tool('read_material', '按问题从本会话有权读取的资料目录选择原件并读取指定内容，可按文件名或说明定位。')
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
        if revisions:
            chat.rag.validate(context)
        if config.get('_validate_sandbox'):
            config['_validate_sandbox']()

    async def execute(name, args):
        await check_execution_async(validate)
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
        selected = await chat.rag.enrich(owner,aic,session,files,({**config,'_answer_route':route},key,system,history),
                                         request.query,include_private=include_private,progress=progress)
        resolved, message = await chat.rag.answer_context(selected, request.query, progress=progress)
        cfg = resolved[0]
        data = background(resolved[2])
        evidence = data.get('file_evidence', [])
        reading = message[len(request.query):].strip()
        if name=='read_material' and not cfg.get('_material_sources'):
            raise ValueError('本步未读到授权原件正文。请调整资料查询或请用户确认文件。')
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
        config.update(_rag_access={'scope':scope,'revisions':list(revisions.items())}, _material_sources=sources)
        if cfg.get('_image_inputs'):
            config['_image_inputs'] = cfg['_image_inputs']
        if store:
            await run_blocking(store.merge, {'entry_material':{'sources':sources,'revisions':list(revisions.items())}})
        await check_execution_async(validate)
        return json.dumps({'query':request.query,'matches':evidence,'reading':reading,
                           'sources':cfg.get('_material_sources',[]),'notices':cfg.get('_workflow_notices',[])},ensure_ascii=False)

    config.update(_entry_tools=tools,_execute_entry_tool=execute,_validate_entry_results=validate)
    return context
