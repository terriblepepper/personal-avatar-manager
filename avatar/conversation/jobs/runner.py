"""Application-owned generation; browsers only observe persisted snapshots."""
import asyncio
import json
import logging
import time
from contextlib import aclosing
from fastapi import HTTPException
from avatar.core.schemas import ChatRequest
from avatar.models.client import collected_model_status
from avatar.core.execution import run_blocking
from avatar.conversation.jobs.store import GenerationStore
from avatar.conversation.jobs.pipeline import generation_scope

log = logging.getLogger(__name__)


class GenerationRunner:
    def __init__(self, chat, database, concurrency=10):
        self.chat, self.database = chat, database
        self.store = GenerationStore(database)
        self.concurrency = concurrency
        self.tasks = set()
        self.active_jobs = {}

    async def cancel(self,actor,request_id):
        snapshot = await asyncio.to_thread(self.store.get, actor['id'], request_id)
        active = self.active_jobs.get(snapshot['task_id']) if snapshot else None
        if active and snapshot['state'] == 'running':
            row, task = active
            row['_user_pause'] = True
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return await run_blocking(self._cancel, actor, request_id, asyncio.get_running_loop(), active[0] if active else None)

    def _cancel(self,actor,request_id,loop,active=None):
        from avatar.conversation import pauses
        uid=actor['id']
        with self.database.connect(write=True) as db:
            row=db.execute('SELECT * FROM chat_generation_jobs WHERE user_id=? AND request_id=?',(uid,request_id)).fetchone()
            if not row:raise HTTPException(404,'任务不存在')
            if row['state'] not in ('running','queued'):return self.store.view(row,db)
            payload=json.loads(row['payload'])
            parent=db.execute('SELECT * FROM chat_conversations WHERE id=? AND user_id=?',(row['conversation_id'],uid)).fetchone()
            result={'reply':'已停止本次任务。','conversation_id':parent['id']}
            chat=getattr(getattr(self.chat,'services',None),'chat',self.chat)
            if payload.get('personal') and hasattr(chat, 'service'):
                from avatar.core.execution import AgentNeedsInput
                request=ChatRequest.model_validate(payload).model_copy(update={'channel':parent['channel']})
                previous=pauses.pending(db,uid,parent['id'])
                state=(active or {}).get('_pause_state')
                target=db.execute('SELECT user_id,aic,last_modified_at FROM avatar_records WHERE user_id=?',(uid,)).fetchone()
                if (state and state[2].get('agent')) or target:
                    if state and state[2].get('agent'):
                        session,work,overview,context=state
                    else:
                        session,work,context=parent,request,None
                        if previous:
                            session=db.execute('SELECT * FROM chat_conversations WHERE id=? AND user_id=?',
                                               (previous['session_id'],uid)).fetchone() or parent
                            suffix='\n\n用户补充：'+request.message if request.message and request.agent_control=='continue' else ''
                            objective=previous.get('goal',previous['objective']) if suffix else previous['objective']
                            work=request.model_copy(update={'message':objective+suffix,
                                                             'attachment_ids':request.attachment_ids or previous.get('attachment_ids',[])})
                            target=db.execute('SELECT user_id,aic,last_modified_at FROM avatar_records WHERE user_id=? AND aic=?',
                                              (previous['owner'],previous['aic'])).fetchone() or target
                        overview={'agent':dict(target),'_tool_events':json.loads(row['snapshot']).get('tool_events',[])}
                    config=context[0] if context else {'_agent_checkpoint':(previous or {}).get('checkpoint')}
                    error=AgentNeedsInput.from_context('任务已暂停。点击继续，或补充信息后继续。',config)
                    error.retry_dispatch=not state and bool((previous or {}).get('retry_dispatch', not previous))
                    error.answer_route=config.get('_answer_route')
                    pauses.pause(chat,actor,request,work,parent,session,overview,error,previous,db=db,job=row)
                    return self.store.view(
                        db.execute('SELECT * FROM chat_generation_jobs WHERE task_id=?',(row['task_id'],)).fetchone(),db)
            self.store.finish_in(db,uid,request_id,'cancelled',result)
        # Commit the durable stop before notifying the in-process task on its owning loop.
        loop.call_soon_threadsafe(self._interrupt,row['task_id'])
        return self.store.get(uid,request_id)

    def _interrupt(self,task_id):
        for task in self.tasks:
            if task.get_name()=='chat-'+task_id:
                task.cancel()
                break

    async def execute(self, row):
        with generation_scope(row,database=self.database):
            await self.generate(row)
        saved=await asyncio.to_thread(self.store.get,row['user_id'],row['request_id'])
        engine=getattr(getattr(self.chat,'services',None),'chat',self.chat)
        if saved and saved['done'] and hasattr(engine,'service') and json.loads(row['payload']).get('personal') and not json.loads(row['payload']).get('agent_control'):
            from avatar.conversation.jobs.titles import generate_title
            await generate_title(engine,row['user_id'],saved['conversation_id'],saved['message'],saved['reply'])

    async def generate(self, row):
        uid, rid = row['user_id'], row['request_id']
        try:
            if not isinstance(json.loads(row['snapshot']),dict):raise ValueError('Invalid task snapshot')
            snapshot = self.store.view(row)
            last_saved=0
            body = ChatRequest.model_validate(json.loads(row['payload']))
            def load_actor():
                with self.database.connect() as db:
                    actor = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
                    from avatar.conversation.jobs.pipeline import validate_versions
                    validate_versions(db,uid,json.loads(row['authorization']))
                return actor
            actor = await asyncio.to_thread(load_actor)
            if not actor or actor['account_status']!='active':
                await run_blocking(self.store.finish,uid, rid, 'cancelled', error='账户不可用，未执行请求')
                return
            publishing = asyncio.Lock()
            async def report_model_status(message):
                async with publishing:
                    snapshot.update(status=message, stage='model_progress')
                    await run_blocking(self.store.record, uid, rid, snapshot)

            token = collected_model_status.set(report_model_status)
            try:
                async with aclosing(self.chat.stream(dict(actor), body)) as source:
                    async for event in source:
                        async with publishing:
                            kind = event['type']
                            first_delta = kind == 'delta' and not snapshot['reply'] and bool(event['text'])
                            if kind == 'delta': snapshot['reply'] += event['text']
                            elif kind == 'status':
                                if event.get('reset_reply'): snapshot['reply']=''
                                snapshot.update(status=event['message'], stage=event.get('stage', 'generating'))
                                if event.get('target'): snapshot['target'] = event['target']
                            elif kind == 'usage': snapshot['usage'] = event['usage']
                            elif kind == 'skills': snapshot['used_skills'] = event['items']
                            elif kind == 'tools': snapshot['mermaid_enabled'] = event['mermaid']
                            elif kind == 'tool':
                                items=snapshot.setdefault('tool_events',[])
                                item=next((item for item in items if item['id']==event['id']),None)
                                if item is None:items.append(dict(event))
                                else:item.update(event)
                            elif kind == 'error':
                                await run_blocking(self.store.finish,uid, rid, 'failed', error=event['message'])
                                return
                            elif kind == 'awaiting_input':
                                await run_blocking(self.store.finish,uid, rid, 'awaiting_input', result=event)
                                return
                            elif kind == 'done':
                                await run_blocking(self.store.finish,uid, rid, 'succeeded', result=event)
                                return
                            # Publish the first visible text immediately; coalesce subsequent provider chunks.
                            if kind=='delta' and not first_delta and time.monotonic()-last_saved<.25:continue
                            if not await run_blocking(self.store.record,uid, rid, snapshot): return
                            last_saved=time.monotonic()
            finally:
                collected_model_status.reset(token)
            await run_blocking(self.store.finish,uid, rid, 'indeterminate', error='原请求未返回最终结果，不会自动重发')
        except asyncio.CancelledError:
            if not row.get('_user_pause'):
                await run_blocking(self.store.finish,uid, rid, 'indeterminate', error='服务已停止，原请求结果未确认；不会自动重发')
            raise
        except HTTPException as error:
            await run_blocking(self.store.finish,uid,rid,'failed',error=str(error.detail))
        except Exception:
            log.exception('Generation failed: %s', row['task_id'])
            await run_blocking(self.store.finish,uid, rid, 'failed', error='回答处理失败，请检查配置和已保存记录')

    async def claim(self, capacity):
        claimed = []
        def reserve():
            claimed.extend(self.store.claim(capacity))
        try:
            await run_blocking(reserve)
        except asyncio.CancelledError:
            if claimed:
                # Shutdown interrupted the poll before any execute task could start.
                await run_blocking(self.store.release_unstarted, [row['task_id'] for row in claimed])
            raise
        return claimed

    async def run(self):
        try:
            while True:
                try:
                    active = {task.get_name().removeprefix('chat-') for task in self.tasks}
                    capacity = self.concurrency - len(self.tasks)
                    queued = await run_blocking(self.store.recover, active)
                    rows = await self.claim(capacity) if queued and capacity > 0 else ()
                    for row in rows:
                        task = asyncio.create_task(self.execute(row), name='chat-' + row['task_id'])
                        self.tasks.add(task)
                        self.active_jobs[row['task_id']] = (row, task)
                        task.add_done_callback(self.completed)
                except Exception:
                    log.exception('Generation queue temporarily unavailable')
                await asyncio.sleep(.25)
        finally:
            active = tuple(self.tasks)
            for task in active: task.cancel()
            await asyncio.gather(*active, return_exceptions=True)

    def completed(self, task):
        self.tasks.discard(task)
        self.active_jobs.pop(task.get_name().removeprefix('chat-'), None)
        if not task.cancelled() and task.exception():
            log.error('Generation cleanup failed', exc_info=task.exception())
