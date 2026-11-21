"""HTTP adapters; accepted work belongs to the application runner."""
import asyncio
import json
import uuid
from fastapi.responses import StreamingResponse
from avatar.conversation.jobs.store import GenerationStore, TERMINAL
from avatar.conversation.jobs.runner import GenerationRunner


def submit_generation(database, user, body):
    return GenerationStore(database).submit(user, body)


def chat_response(chat, user, body, database):
    body = body.model_copy(update={'request_id': body.request_id or uuid.uuid4().hex})
    submit_generation(database, user, body)
    store = GenerationStore(database)

    async def events():
        previous = None
        delivered = ''
        while True:
            snapshot = await asyncio.to_thread(store.get,user['id'],body.request_id)
            if snapshot != previous:
                event = {'type': 'snapshot', 'snapshot': snapshot}
                yield 'event: snapshot\ndata: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
                if snapshot:
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
            await asyncio.sleep(.5)

    return StreamingResponse(events(), media_type='text/event-stream',
                             headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-store'})


async def generation_worker(chat, database):
    await GenerationRunner(chat, database).run()
