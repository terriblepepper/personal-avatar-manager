"""Build the temporary conversation context, run the model, and commit its delivery."""
import json
import re
import time
import uuid

from fastapi import HTTPException

from avatar.core.db import dump, now
from avatar.core.execution import AgentNeedsInput, run_blocking
from avatar.conversation.schemas import ChatRequest
from avatar.conversation.tools.reply_artifacts import reply_with_artifacts, without_source_footer
from avatar.protocols.tasks.guest_maintenance import guest_cutoff
from avatar.protocols.tasks.store import ManagementTaskError
from avatar.conversation.loop.town import bind_town_tools
from avatar.models.trace import model_io_scope


# 职责：使用已通过本人鉴权的所有者资料和模型配置生成个人回答。
# 输入：target 为服务端固定目标，context 是认证 peer/group，session 来自协议会话。
# 结果与约束：不创建或绑定小镇用户账号；协议会话不混入 Web 历史，计费和压缩费用归目标所有者。
async def answer_town(chat, files, target, context, session, *, principal=None, resume, store, progress, commit, stream_state, claim):
    user = {'id': target['user_id'], 'username': target['username'], 'account_status': target['account_status']}
    question = '\n\n用户补充：'.join(resume['inputs'])
    request = ChatRequest(channel='avatar', message=question)
    overview, model = await run_blocking(chat.context, user, request, target, private_access=False)
    config, key, system, history = model
    config = {**config, '_aip_stream_state':stream_state, '_omit_source_citations':True}
    if not config.get('_direct_chat'):
        config = {**config, '_avatar_agent':True, '_allow_user_questions':False,
                  '_agent_checkpoint':resume.get('checkpoint',{}),
                  '_run_store':store, '_run_input_id':resume['input_id'], '_run_supplement':resume['supplement'],
                  '_auto_recover':resume['automatic']}
        prefix, raw = system.split('\n', 1)
        background = json.loads(raw)
        for name in ('available_skills', '_skills_revision'):
            background.pop(name, None)
        prefix += (' 当前为小镇临时会话，可检索和读取公开资料，按用户需求生成和交付文件。'
                   '文件随协议回复交付，正文写结果和必要说明，不列 RAG 或源文件引用，不贴生成源码或虚构链接。'
                   '工具按需使用，工作区只含本次任务文件，Python 和 HTML 预览均离线。'
                   '无法中途提问；缺少信息时，在最终答复中说明缺口。')
        if resume.get('clarification'):
            background['last_clarification'] = resume['clarification']
            prefix += ' last_clarification 是上一轮你向用户提出的问题，只用于理解本轮补充。'
        system = prefix+'\n'+json.dumps(background,ensure_ascii=False)
    model = (config, key, system, history)
    caller = context.peer_aic if principal is None else 'visitor:' + principal['visitor_id']
    platform = 'acps-direct' if principal is None else 'town-guest' if principal.get('anonymous') else 'town-public'
    conversation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, dump(['acps-direct', target['aic'], caller, context.group_id or '', session] if principal is None else [platform, target['aic'], context.peer_aic, caller, context.group_id or '', session])))
    memory = chat.memory
    conversation = await run_blocking(memory.scope, user['id'], 'acps', target['aic'], conversation_id,
                                caller_id=caller, platform=platform, title='小镇公开访客对话' if principal else '小镇分身对话', create=True)
    if platform == 'town-guest' and conversation['updated_at'] < guest_cutoff() and conversation['lease_until'] <= time.time():
        raise ManagementTaskError(-32001, '访客会话已过期，请使用新会话')
    await run_blocking(claim, conversation_id)
    try:
        with model_io_scope(progress,private=False):
            if config.get('_direct_chat'):
                progress({'type':'status','stage':'result_writing'})
                model = await chat.prepare_direct(model, conversation, question)
                result = await chat.model_client.generate(*model, question)
            else:
                model = await memory.prepare(conversation, overview['agent']['last_modified_at'], model, question,
                                             chat.model_client, user['id'], progress=progress)
                previous = await run_blocking(files.files_from_previous,int(store.scope_id),
                                              resume.get('previous_deliveries',()))
                async with files.workspace() as path:
                    model = await bind_town_tools(chat,model,{**user,'aic':target['aic']},caller,
                        conversation,path,store,progress,previous)
                    result = await chat.rag.answer(model, question, progress=progress)
        if not result.text.strip():
            raise HTTPException(502, '模型未返回有效回答')
        validate_sandbox = model[0].get('_validate_sandbox')
        if validate_sandbox:
            await run_blocking(validate_sandbox)
        outputs = model[0].get('_generated_files',())
        originals = model[0].get('_react_public_files',())
        reply = without_source_footer(result.text)
        if not config.get('_direct_chat'):
            attempted = model[0].get('_agent_checkpoint',{}).get('sandbox_attempted')
            events = [{'group':'sandbox'}] if attempted else ()
            reply = reply_with_artifacts(reply, outputs, events)
            reply = re.sub(r'\[\[attachment:[^\]\r\n]+\]\]', '', reply).strip()
        if not reply and (outputs or originals):
            reply = '文件已随本轮答复交付。'
        overview['_target'] = {'aic': target['aic'], 'name': overview['agent']['profile']['avatar_name']}
        async with files.prepare(int(store.scope_id), outputs, originals) as delivery:
            await run_blocking(chat.save, user, request.model_copy(update={'channel': 'acps', 'conversation_id': conversation_id}),
                           overview, reply, result.usage, caller_id=caller,
                           commit_in=lambda db:commit(db,reply,files=delivery) if delivery else commit(db,reply))
        return reply
    except AgentNeedsInput as exc:
        def save_pause():
            with chat.service.database.connect(write=True, account_scope=user['id']) as db:
                store.check_in(db)
                if exc.usage:
                    config = overview['agent']['model_config']
                    db.execute('INSERT INTO model_calls(user_id,provider,model,usage,created_at,caller_id,conversation_id) VALUES(?,?,?,?,?,?,?)',
                               (user['id'], config['provider'], config['model'], dump(exc.usage), now(), caller, conversation_id))
                commit(db,exc.question or str(exc),pause=exc)
        await run_blocking(save_pause)
