"""HTTP adapters; accepted work belongs to the application runner."""
import asyncio
import json
import uuid
from contextlib import nullcontext
from fastapi.responses import StreamingResponse
from avatar.conversation.jobs.store import GenerationStore, TERMINAL
from avatar.conversation.jobs.runner import GenerationRunner
from avatar.core.dispatch import wait_change


def submit_generation(database, user, body):
    return GenerationStore(database).submit(user, body)


def chat_response(request, user, body, database):
    body = body.model_copy(update={'request_id': body.request_id or uuid.uuid4().hex})
    submit_generation(database, user, body)
    store = GenerationStore(database)

    changes = getattr(database, 'changes', None)
    snapshots_only = request.headers.get('x-avatar-stream') == 'snapshots'

    async def stream(wakeup):
        previous = None
        delivered = ''
        while not await request.is_disconnected():
            if wakeup is not None:
                wakeup.take()
            snapshot = await asyncio.to_thread(store.get,user['id'],body.request_id)
            if snapshot != previous:
                event = {'type': 'snapshot', 'snapshot': snapshot}
                yield 'event: snapshot\ndata: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
                if snapshot and not snapshots_only:
                    text=snapshot.get('reply','')
                    if text.startswith(delivered) and text!=delivered:
                        event={'type':'delta','text':text[len(delivered):]}
                        yield 'event: delta\ndata: '+json.dumps(event,ensure_ascii=False)+'\n\n'
                    delivered=text
                    event={'type':'status','message':snapshot.get('status',''),'stage':snapshot.get('stage','')}
                    yield 'event: status\ndata: '+json.dumps(event,ensure_ascii=False)+'\n\n'
                previous = snapshot
            if snapshot and snapshot['state'] in TERMINAL:
                event = ({'type':'cancelled','message':snapshot['status']} if snapshot['state']=='cancelled' and not snapshot['error'] else
                         {'type':'awaiting_input', **snapshot['result']} if snapshot['state']=='awaiting_input' else
                         {'type': 'done', **snapshot.get('result', {})} if snapshot['done'] else
                         {'type': 'error', 'message': snapshot['error']})
                yield 'event: ' + event['type'] + '\ndata: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
                return
            yield ': keep-alive\n\n'
            if wakeup is None:
                await asyncio.sleep(.5)
            else:
                await wait_change(wakeup, 5. if changes.can_push else .5)

    async def events():
        subscription = changes.watch(('generation', user['id'], body.request_id)) if changes else nullcontext()
        with subscription as event:
            async for item in stream(event):
                yield item

    return StreamingResponse(events(), media_type='text/event-stream',
                             headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-store'})


async def generation_worker(chat, database):
    await GenerationRunner(chat, database).run()
