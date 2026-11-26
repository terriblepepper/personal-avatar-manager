"""Stream and persist one owner-authorized chat turn."""
import asyncio
from contextlib import aclosing
import json
import logging
from fastapi import HTTPException
from avatar.models.effort import with_chat_effort
from avatar.conversation import pauses
from avatar.conversation.tools.reply_artifacts import is_html_output
from avatar.conversation.tools.activity import ToolActivity
from avatar.core.execution import AgentNeedsInput, run_blocking
from avatar.core.execution import authorization, check_execution_async, current_generation


# 职责：以异步事件流生成回答、状态和最终保存结果。
# 输入：user 和 request 为已认证账户与本轮请求；消费者须正确关闭异步生成器。
# 结果与约束：依次 yield 状态、文本、用量和 done；失败转换为脱敏 error，未完成片段不保存，退出始终释放租约与上游资源。
async def stream_chat(chat, user, request):
    lock = chat.locks.setdefault(request.conversation_id or user['id'], asyncio.Lock())
    job = current_generation.get()
    acquired = None
    overview = context = session = prepared = None
    failure_stage='lookup'
    try:
        if lock.locked():
            raise HTTPException(409, '上一条消息仍在生成，请等待完成后再发送')
        async with lock:
            yield {'type':'status','stage':'lookup','message':'读取本人公开与私密资料' if request.personal else ('查询已审核个人资料' if request.channel == 'avatar' else '识别请求并查找目标分身')}
            parent = await asyncio.to_thread(chat.session,user['id'],request.channel,request.conversation_id,personal=request.personal)
            request=request.model_copy(update={'channel':parent['channel']})
            def take_lease():
                nonlocal acquired
                acquired = chat.memory.acquire(parent['id'])
            await run_blocking(take_lease)
            request=request.model_copy(update={'conversation_id':parent['id']})
            original=request
            def checked_pause():
                with chat.service.database.connect() as db:
                    return pauses.checked(db,user['id'],request)
            held=await asyncio.to_thread(checked_pause)
            if request.agent_control=='end':
                yield {'type':'done',**await run_blocking(pauses.end_task,chat,user,parent,request)}
                return
            from avatar.conversation.loop import personal
            durable = parent['platform']=='web'
            run_id = personal.run_id_for(job,held,original)
            previous = await asyncio.to_thread(personal.inspect,chat,user['id'],parent['id'],run_id,job) if durable else None
            recovering = bool(job and json.loads(job['authorization']).get('resume_run'))
            if recovering and not (previous and previous.get('binding')):
                raise HTTPException(409,'缺少可验证的执行状态，原请求不会自动重发')
            if not held and not recovering and (request.personal or request.channel=='assistant'):
                failure_stage='intent_analysis'
                yield {'type':'status','stage':'intent_analysis','message':'正在分析本轮请求的意图…'}
            route_records, route_events = [], []
            if held:
                yield {'type':'status','stage':'resuming','message':'正在恢复上次任务的目标和回执'}
                overview,context,session,request=await asyncio.to_thread(pauses.continue_task,chat,user,request,parent,held)
                local_reply=None
                config = context[0]
                if not (previous and previous.get('binding')) and held.get('retry_dispatch'):
                    overview,context,session,local_reply=await chat.dispatch(user,request,parent)
                    overview['_prior_tool_events']=held.get('activity', []) if held.get('sources') else []
            elif recovering:
                yield {'type':'status','stage':'resuming','message':'正在恢复已核验的任务进度'}
                overview,context,session,local_reply=await asyncio.to_thread(personal.restore,chat,user,request,parent,previous)
                overview['_tool_events']=route_records
            else:
                with ToolActivity(route_records, route_events.append).track(private_io=False):
                    overview,context,session,local_reply=await chat.dispatch(user,request,parent)
                overview['_tool_events'] = route_records
                for event in route_events:
                    yield pauses.public_event(event)
            if context:
                config = context[0]
                if overview['agent']['user_id'] == user['id']:
                    effort = (authorization() or {}).get('reasoning_effort', json.loads(parent['tools_config']).get('reasoning_effort'))
                    config = with_chat_effort(config, effort)
                context=({**config,'_avatar_agent':not config.get('_direct_chat')},*context[1:])
                if durable:
                    context=await run_blocking(personal.attach,chat,user,original,parent,session,overview,context,job,held,run_id,acquired,previous)
            if job:
                job['_pause_state'] = (session, request, overview, context)
            if request.personal and context is None and local_reply:request=request.model_copy(update={'attachment_ids':[]})
            request = request.model_copy(update={'conversation_id':session['id']})
            if overview.get('_target'):
                yield {'type':'status','stage':'routing','message':'已调用分身：'+overview['_target']['name'],'target':overview['_target']}
            text, usage = [], None
            if context:
                from avatar.core.execution import check_execution_async
                events = asyncio.Queue()
                async def answer_with_events():
                    nonlocal prepared
                    try:
                        prepared = await chat.prepare(user, request, overview, context, session, events.put_nowait)
                        if job:
                            job['_pause_state'] = (session, request, overview, prepared)
                        await check_execution_async()
                        events.put_nowait({'type': 'skills', 'items': overview.get('_used_skills', [])})
                        events.put_nowait({'type': 'tools', 'mermaid': prepared[0].get('_mermaid_enabled', True)})
                        with ToolActivity(overview['_tool_events'], events.put_nowait).track(private_io=bool(overview.get('_private_context') and overview['agent']['user_id']==user['id'])) as emit:
                            async with aclosing(chat.rag.stream(prepared, request.message, progress=emit)) as source:
                                async for event in source:
                                    events.put_nowait(event)
                    finally:
                        events.put_nowait(None)
                generation = asyncio.create_task(answer_with_events())
                try:
                    while (event := await events.get()) is not None:
                        if event['type'] == 'status':
                            failure_stage = event['stage']
                            if event.get('reset_reply'): text.clear()
                        elif event['type'] == 'tool' and event.get('group') == 'agent_receipt':
                            continue
                        elif event['type'] == 'contact_draft':
                            overview['_contact_draft']=event['document']
                            continue
                        elif event['type'] == 'material_delivery':
                            overview['_material_outputs']=event['items']
                            continue
                        elif event['type'] == 'delta':
                            text.append(event['text'])
                            if ('_material_outputs' in overview
                                    or any(is_html_output(item) for item in overview.get('_generated_files', ()))
                                    or any(item.get('group') == 'sandbox' for item in overview.get('_tool_events', ()))):
                                continue  # Delivery text and its file cards become visible together after commit.
                        elif event['type'] == 'usage':
                            usage = event['usage']
                        yield pauses.public_event(event)
                    await generation
                finally:
                    if not generation.done():
                        generation.cancel()
                    await asyncio.gather(generation, return_exceptions=True)
            else:
                if request.attachment_ids:
                    raise HTTPException(422,'请先选择一个分身，再发送附件提问')
                yield {'type':'status','stage':'generating','message':'生成引导结果'}
                reply = local_reply or chat.assistant_reply(overview, request.message)
                text.append(reply)
                yield {'type':'delta','text':reply}
            reply = ''.join(text)
            if not reply.strip():
                raise HTTPException(502,'回答生成未收到可见文本，调用适配器未提供终止原因；无法据此判断余额')
            yield {'type':'status','stage':'saving','message':'保存回答记录'}
            failure_stage='saving'
            result = await run_blocking(chat.save, user, request.model_copy(update={'message':original.message}), overview, reply, usage)
            yield {'type':'done',**result}
    except AgentNeedsInput as exc:
        try:
            result=await run_blocking(pauses.pause,chat,user,original,request,parent,session,overview,exc,held)
        except HTTPException as error:
            yield {'type':'error','code':error.status_code,'message':error.detail}
        else:
            yield {'type':'awaiting_input',**result}
    except HTTPException as exc:
        if getattr(exc,'stream_exhausted',False):
            retry_dispatch = overview is None
            if retry_dispatch:
                overview=await asyncio.to_thread(chat.service.overview,user['id'])
            if overview.get('agent'):
                pause_context=prepared or context
                paused=AgentNeedsInput.from_context(exc.detail,pause_context[0] if pause_context else {})
                paused.stream_retryable=True
                paused.retry_dispatch=retry_dispatch or bool(held and held.get('retry_dispatch') and prepared is None)
                try:
                    result=await run_blocking(pauses.pause,chat,user,original,request,parent,session or parent,overview,paused,held)
                except HTTPException as error:
                    yield {'type':'error','code':error.status_code,'message':error.detail}
                else:
                    yield {'type':'awaiting_input',**result}
                return
        logging.getLogger(__name__).warning('Chat request failed: stage=%s code=%s detail=%s',failure_stage,exc.status_code,exc.detail)
        yield {'type':'error','code':exc.status_code,'message':exc.detail}
    except Exception as exc:
        logging.getLogger(__name__).warning('Streaming generation failed (%s)',type(exc).__name__)
        yield {'type':'error','code':500,'message':'回答处理失败，已显示内容可能不完整；系统未自动重试'}
    finally:
        if acquired:
            try:
                if not job and (overview or {}).get('_run_store'):
                    await run_blocking(personal.discard_unpaused,overview['_run_store'],acquired)
            finally:
                await run_blocking(chat.memory.release,parent['id'],acquired)
