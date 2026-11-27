"""跨分身协作的创建、共享、恢复与回执。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from avatar.core.db import now
from avatar.conversation.schemas import ChatRequest
from avatar.protocols.tasks.dialogues import PeerDialogueTasks
from avatar.protocols.tasks.store import ManagementTaskError
from avatar.protocols.transport.peer import PeerAcpsTransport
from avatar.core.execution import AgentNeedsInput
from tests.support import (
    _react_probe_call,
    _react_probe_receipt,
)


def test_peer_protocol_accepts_before_generation_and_deduplicates(context,monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from avatar.protocols.tasks.dialogues import PeerDialogueTasks
    app,_,_=context;calls=[]
    async def check():
        gate=asyncio.Event()
        async def answer(*args):
            calls.append(1);await gate.wait()
            return SimpleNamespace(text='peer answer',usage=None,task_action=None)
        import threading
        event_thread = threading.get_ident()
        connect = app.state.database.connect
        def threaded_connect(*args, **kwargs):
            assert threading.get_ident() != event_thread
            return connect(*args, **kwargs)
        monkeypatch.setattr(app.state.database, 'connect', threaded_connect)
        handler=PeerDialogueTasks(SimpleNamespace(database=app.state.database, executor=SimpleNamespace(answer_peer=answer)),app.state.settings)
        handler.scope=lambda *args:dict(user_id='left',target_user_id='right',next_speaker=1,status='running')
        wire=SimpleNamespace(local_aic='right-aic',peer_aic='left-aic')
        command=dict(id='peer-command',taskId='peer-task',sessionId='peer-session',command='start')
        try:
            initial=await asyncio.wait_for(handler.handle(command,wire,'right'),.2)
            assert initial['status']['state']=='working'
            assert (await handler.handle(command,wire,'right'))['status']['state']=='working'
            from avatar.protocols.tasks.store import ManagementTaskError
            with pytest.raises(ManagementTaskError):
                await handler.handle({**command,'id':'parallel-command'},wire,'right')
            gate.set()
            for _ in range(100):
                result=await handler.handle({**command,'id':'query','command':'get'},wire,'right')
                if result['status']['state']!='working':break
                await asyncio.sleep(.01)
            assert result['status']['state']=='awaiting-completion'
            assert result['products'][0]['dataItems'][0]['text']=='peer answer'
            assert calls==[1]
        finally:
            gate.set()
            for task in list(handler.running.values()):task.cancel()
            await asyncio.gather(*list(handler.running.values()),return_exceptions=True)
    asyncio.run(check())


def test_peer_sender_resolves_oidc_through_runtime_pool_off_loop(monkeypatch):
    from contextlib import contextmanager, nullcontext
    from threading import get_ident
    from avatar.protocols.transport import peer

    event_thread = get_ident()
    calls = []
    @contextmanager
    def connect():
        assert get_ident() != event_thread
        calls.append('identity')
        identity = {'subject':'subject','username':'owner','issuer':'issuer'}
        yield SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: identity))

    rpc = AsyncMock(return_value={'status':{'state':'working'}})
    leader = SimpleNamespace(context={peer.DIRECT_LEADER:SimpleNamespace(get_task=rpc)})
    runtime = SimpleNamespace(
        settings=SimpleNamespace(oidc_issuer='issuer'), database=SimpleNamespace(connect=connect),
        instances={'leader':('L',leader,SimpleNamespace(serving=True),'local'),
                   'peer':('P',None,SimpleNamespace(serving=True),'remote')},
        directories={'leader':SimpleNamespace(select=AsyncMock(return_value=object()))})
    monkeypatch.setattr(peer,'use_local_entity',lambda entity: nullcontext())
    transport = PeerAcpsTransport(runtime)
    result = asyncio.run(transport.send({'id':'task','session_id':'session'},'leader','peer','get','request'))
    assert calls == ['identity'] and result['status']['state'] == 'working'
    assert rpc.await_args.args[0]['commandParams']['requester'] == {'id':'subject','issuer':'issuer'}


def test_peer_transport_polls_accepted_task_without_resending(context):
    import asyncio
    from types import SimpleNamespace
    from avatar.protocols.transport.peer import PeerAcpsTransport
    app,_,_=context
    transport=PeerAcpsTransport(SimpleNamespace(database=app.state.database));operations=[]
    async def send(job,left,right,operation,command_id):
        operations.append(operation)
        return {'status':{'state':'working' if len(operations)==1 else 'awaiting-completion'},'products':[{'dataItems':[{'type':'text','text':'done'}]}]}
    transport.send=send
    result=asyncio.run(transport.exchange({'id':'task','session_id':'session','revision':1},'left','right'))
    assert result['text']=='done' and operations==['start','get']


@pytest.fixture
def supplemented_task(context,monkeypatch):
    from types import SimpleNamespace
    from avatar.core.db import now,dump
    app,left,right=context;service=app.state.dialogues
    users=[client.get('/api/auth/me').json()['user'] for client in (left,right)]
    with service.database.connect(write=True) as db:
        for index,user in enumerate(users):
            db.execute("INSERT INTO avatar_records(user_id,aic,certificate,profile,last_modified_at,created_at,updated_at,identity_status) VALUES(?,?,?,?,?,?,?,'active')",(user['id'],'peer-'+str(index),'{}',dump({'name':['测试甲','测试乙'][index],'avatar_name':['测试甲的分身','测试乙的分身'][index]}),now(),now(),now()))
    identifier=service.create(users[0],SimpleNamespace(target_aic='peer-1',objective='商定会议时间',request_id='task-test'))
    with service.database.connect(write=True) as db:db.execute("UPDATE agent_dialogues SET accepted=1,status='paused',round_limit=1 WHERE id=?",(identifier,))
    async def no_controls(*args):pass
    monkeypatch.setattr(service.recovery,'flush_controls',no_controls)
    return service,users,identifier


def test_peer_public_material_staging_reuses_verified_receipt(context, supplemented_task):
    from avatar.conversation.loop.peer import PeerTurn
    from avatar.conversation.loop.runtime import load_checkpoint
    from avatar.conversation.loop.public_materials import bind_public_material_tools
    from avatar.files.library.delivery import SavedMaterials
    from avatar.files.library.public import PublicMaterials
    from tests.support import saved_file

    app, _, receiver = context
    service, users, task_id = supplemented_task
    owner = users[1]['id']
    public = saved_file(receiver, '公开原件.txt', b'public')
    private = saved_file(receiver, '私有资料.txt', b'private')
    with service.database.connect(write=True) as db:
        db.execute("UPDATE attachments SET visibility='public' WHERE id=?", (public,))
        db.execute("UPDATE agent_dialogues SET status='running',next_speaker=1 WHERE id=?", (task_id,))
        task = dict(service.session_store.owned(db, users[1], task_id))

    library = SavedMaterials(app.state.chat.service.attachments, owner)

    def bind():
        run = PeerTurn(service.executor, task, owner)
        store, state = run.open()
        assert state['schema_version'] == 2
        config = {}
        bind_public_material_tools(app.state.chat, config, library, owner, store, state,
                                   workspace=run.workspace, task_id=task_id)
        return store, config

    store, _ = bind()
    legacy = {key:value for key,value in store.load().items() if key != 'schema_version'}
    store.save({**legacy,
        'phase':'external_handoff',
        'handoff':{'name':'send_public_material'},
        'peer_export':{'exports':[{'id':'legacy'}], 'receipts':[{'prepared_originals':[]}]},
        'history_summary':{'text':'核对旧附件'},
    })
    store, config = bind()
    migrated = asyncio.run(load_checkpoint(store))
    assert migrated['schema_version'] == 2 and migrated['trajectory'] == []
    assert migrated['legacy_summary'] == '核对旧附件' and not migrated.get('peer_export')
    tools = config['_react_handlers']
    send = tools['send_public_material']
    with pytest.raises(ValueError, match='可见'):
        asyncio.run(send({'file_ids':[public]}))
    found = asyncio.run(tools['find_public_material']({}))
    assert [row['file_id'] for row in found['items']] == [public]
    with pytest.raises(ValueError, match='可见'):
        asyncio.run(send({'file_ids':[private]}))
    receipt = asyncio.run(send({'file_ids':[public,public]}))

    store, config = bind()
    repeated = asyncio.run(config['_react_handlers']['send_public_material']({'file_ids':[public]}))
    assert receipt == repeated
    output, = store.load()['public_files']
    assert output['id'] == receipt['files'][0]['id']
    with service.database.connect() as db:
        assert PublicMaterials.validate(db, owner, output)['id'] == public
        assert not db.execute('SELECT 1 FROM chat_files WHERE id=?', (output['id'],)).fetchone()


def test_town_public_material_stages_with_marker_and_releases_outputs_on_completion(context, supplemented_task, monkeypatch):
    import time
    from uuid import uuid4
    from avatar.conversation.loop.store import RunStore
    from avatar.conversation.loop.town import bind_town_tools
    from avatar.core.db import dump
    from avatar.protocols.tasks.personal import PersonalTaskService
    from tests.support import saved_file

    app, _, receiver = context
    service, users, _ = supplemented_task
    owner = users[1]['id']
    public = saved_file(receiver, '公开原件.txt', b'public')
    scope = ('peer-1','town#public:visitor','','town-session','town-task')
    with service.database.connect(write=True) as db:
        db.execute("UPDATE attachments SET visibility='public' WHERE id=?", (public,))
        db.execute('INSERT INTO avatar_aip_tasks VALUES(?,?,?,?,?,?,?,?)',
                   (*scope,'start',dump({'status':{'state':'working'}}),time.time()+60))
        rowid = PersonalTaskService.task_row(db, scope)['task_number']

    chat = app.state.chat
    task_service = PersonalTaskService(chat, app.state.settings)
    files = task_service.files

    async def principal(_):
        return {'visitor_id':'visitor','anonymous':False}

    monkeypatch.setattr(task_service.public_authorization, 'authorize', principal)
    wire = SimpleNamespace(peer_aic='town',group_id='',headers={'x-avatar-access':'public'})
    command = {'taskId':'town-task','sessionId':'town-session'}
    store = RunStore(service.database, owner, 'protocol', str(rowid), 'run', uuid4().hex)
    store.acquire()
    store.merge({'schema_version':2, 'phase':'running'})
    async def run():
        async with files.workspace() as slot:
            config = {'_run_store':store}
            await bind_town_tools(chat, (config,'key','test\n{}',[]), {'id':owner,'aic':'peer-1'}, 'town',
                                  {'id':'town-session'}, slot, store, None, {})
            assert {'find_public_material', 'send_public_material'} <= {tool['name'] for tool in config['_entry_tools']}
            handlers = config['_react_handlers']
            assert [row['file_id'] for row in (await handlers['find_public_material']({}))['items']] == [public]
            receipt = await handlers['send_public_material']({'file_ids':[public]})
            original, = config['_react_public_files']
            assert receipt['files'][0]['marker'] == '[[attachment:'+original['id']+']]'
            assert not config['_generated_files']
            html = {'id':str(uuid4()), 'filename':'town.html', 'data':b'<!doctype html><html>town</html>'}
            (slot/'draft').write_text('working')
            async with files.prepare(rowid, [html], [original]) as staging:
                with service.database.connect(write=True) as db:
                    delivery, products = files.publish(db, rowid, owner, staging)
                    document = {**command,'status':{'state':'awaiting-completion'},'_file_deliveries':[delivery],
                                'products':[{'dataItems':[{'type':'text','text':'town'},*products]}]}
                    db.execute('UPDATE avatar_aip_tasks SET document=? WHERE task_number=?', (dump(document), rowid))
                result = await task_service.handle({**command,'id':'get','command':'get'}, wire, owner, 'peer-1')
                text = result['products'][0]['dataItems'][0]['text']
                assert html['data'].decode() in text
                stream, _ = files.download(rowid, delivery['token'], original['id'])
                with stream:
                    assert stream.read() == b'public'
        assert list(slot.iterdir()) == []
        result = await task_service.handle({**command,'id':'complete','command':'complete'}, wire, owner, 'peer-1')
        assert result['status']['state'] == 'completed'
        assert result['products'][0]['dataItems'] == [{'type':'text','text':'town'}]
        assert not files.folder(rowid, delivery['token']).exists()
        with pytest.raises(HTTPException) as failure:
            files.download(rowid, delivery['token'], original['id'])
        assert failure.value.status_code == 404
        assert chat.service.attachments.directory.available(owner, public, len(b'public'))

    asyncio.run(run())


def test_acps_get_revalidates_saved_peer_reply(supplemented_task):
    from hashlib import sha256
    from avatar.conversation.loop.peer import PeerTurn
    from avatar.core.db import dump

    service, users, task_id = supplemented_task
    owner = users[1]['id']
    with service.database.connect(write=True) as db:
        db.execute("UPDATE agent_dialogues SET status='running',next_speaker=1 WHERE id=?", (task_id,))
        task = dict(service.session_store.owned(db, users[1], task_id))
    turn = PeerTurn(service.executor, task, owner)
    handler = PeerDialogueTasks(service, SimpleNamespace())
    handler.scope = lambda command, context, user_id, db: dict(service.session_store.owned(db, users[1], task_id))
    wire = SimpleNamespace(local_aic=task['target_aic'],peer_aic=task['own_aic'])
    command = dict(id='query',taskId=task_id,sessionId=task['session_id'],command='get')
    receipt = handler.result(command, wire, 'awaiting-completion', '已保存答复')
    delivery = dict(revision=task['revision'],speaker=owner,authority=turn.source,
                    peer_style=turn.style,workspace=turn.workspace,answer={'text':'已保存答复'},
                    reply_sha=sha256('已保存答复'.encode()).hexdigest())
    with service.database.connect(write=True) as db:
        db.execute('UPDATE agent_dialogues SET pending_file_delivery=? WHERE id=?', (dump(delivery),task_id))
        db.execute('INSERT INTO agent_wire_results VALUES(?,?,?,?,?,?,?)',
                   ('reply',wire.local_aic,wire.peer_aic,task['session_id'],task_id,'awaiting-completion',dump(receipt)))

    assert asyncio.run(handler.handle(command, wire, owner)) == receipt
    with service.database.connect(write=True) as db:
        db.execute("UPDATE avatar_records SET last_modified_at='changed' WHERE user_id=?", (owner,))
    with pytest.raises(ManagementTaskError) as revoked:
        asyncio.run(handler.handle(command, wire, owner))
    assert revoked.value.code == -32009


def test_peer_polling_snapshot_tracks_viewer_history(supplemented_task,context,monkeypatch):
    from avatar.core.db import now
    service, users, identifier = supplemented_task
    app, user, other = context
    sid = service.session_store.get(users[0], identifier)['session_id']
    path = '/api/agent-sessions/' + sid
    response = user.get(path)
    assert response.is_success, response.text
    first = response.json()
    assert [t['id'] for t in first['task_page']['items']] == [identifier]
    assert user.get(path,params={'task_search':'absent'}).json()['task_page']['total'] == 0
    assert user.get('/api/agent-dialogues/'+identifier).json()['history_revision'] == first['history_revision']
    previous_peer = other.get(path).json()['history_revision']
    with app.state.database.connect(write=True) as db:
        db.execute("UPDATE agent_dialogues SET delivery_mode='message' WHERE id=?", (identifier,))
        db.execute('''INSERT INTO agent_dialogue_messages(dialogue_id,turn,speaker_aic,speaker_name,speaker_role,content,usage,created_at)
                      VALUES(?,1,'peer-1','Peer','agent','summary text','null',?)''',(identifier,now()))
    with monkeypatch.context() as patch:
        patch.setattr(service.session_store, 'session', lambda *a, **kw: pytest.fail('compact list hydrated entire session'))
        summary = user.get('/api/agent-sessions?summary=true').json()['items'][0]
        assert summary['messages'] == [{'speaker_role':'agent','content':'summary text'}]
    with app.state.database.connect(write=True) as db:
        db.execute('UPDATE agent_task_members SET hidden=1 WHERE user_id=? AND task_id=?',(users[0]['id'],identifier))
    hidden = user.get(path).json()
    assert user.get('/api/agent-sessions?summary=true').json()['items'][0]['messages'] == []
    assert hidden['history_revision'] != first['history_revision'] and hidden['task_page']['items'] == []
    assert other.get(path).json()['history_revision'] == previous_peer
    assert user.get('/api/agent-dialogues/'+identifier).status_code == 404
    assert user.post(path+'/clear').is_success
    assert user.get(path).json()['history_revision'] != hidden['history_revision']


@pytest.mark.parametrize('side',[0,1])
def test_task_supplement_is_attributed_and_peer_replies_before_end(supplemented_task,side):
    import asyncio
    from types import SimpleNamespace
    service,users,identifier=supplemented_task
    with service.database.connect(write=True) as db:
        db.execute("INSERT INTO agent_pending_inputs(task_id,user_id,request_id,message) VALUES(?,?,?,?)",(identifier,users[side]['id'],'confirm','有空，答应老师就行'))
        service.apply_inputs(db,identifier)
    # Both viewers must see the same author, with opposite ownership.
    for index,user in enumerate(users):
        message=service.session_store.get(user,identifier)['messages'][-1]
        assert message['is_own']==(index==side)
        assert message['speaker_name']==['测试甲','测试乙'][side]
    # Legacy rows without speaker_user_id still resolve by the task's original AIC.
    with service.database.connect(write=True) as db:
        db.execute('UPDATE agent_dialogue_messages SET speaker_user_id=NULL WHERE dialogue_id=?',(identifier,))
    legacy=service.session_store.get(users[1-side],identifier)['messages'][-1]
    assert not legacy['is_own'] and legacy['speaker_name']==['测试甲','测试乙'][side]
    speakers=[]
    async def exchange(job,peer_id,speaker_id):
        speakers.append(speaker_id)
        text='我有空，请转告对方' if len(speakers)==1 else '收到，会议时间确认'
        return {'text':text,'task_action':{'name':'end_task','arguments':{'content':text,'reason':'已确认'}},'usage':None}
    service.transport=SimpleNamespace(exchange=exchange)
    async def advance():
        with service.database.connect(write=True) as db:
            db.execute("UPDATE agent_dialogues SET status='running' WHERE id=?",(identifier,))
            job=dict(db.execute('SELECT * FROM agent_dialogues WHERE id=?',(identifier,)).fetchone())
        await service.executor.turn(job)
    asyncio.run(advance())
    with service.database.connect() as db:
        task=dict(db.execute('SELECT * FROM agent_dialogues WHERE id=?',(identifier,)).fetchone())
        last=db.execute('SELECT state,task_action FROM agent_dialogue_messages WHERE dialogue_id=? ORDER BY id DESC LIMIT 1',(identifier,)).fetchone()
        assert task['status']=='queued' and last['state']=='queued' and last['task_action']=='send_message'
        assert not db.execute('SELECT 1 FROM agent_protocol_controls WHERE task_id=?',(identifier,)).fetchone()
    asyncio.run(advance())
    with service.database.connect() as db:
        assert db.execute('SELECT status FROM agent_dialogues WHERE id=?',(identifier,)).fetchone()[0]=='ended'
    assert speakers==[users[side]['id'],users[1-side]['id']]


def test_task_supplement_during_final_answer_keeps_task_running(supplemented_task):
    import asyncio
    from types import SimpleNamespace
    service,users,identifier=supplemented_task
    async def exchange(*args):
        with service.database.connect(write=True) as db:
            db.execute("INSERT INTO agent_pending_inputs(task_id,user_id,request_id,message) VALUES(?,?,?,?)",(identifier,users[1]['id'],'late','改成四点'))
        return {'text':'原时间已确认','task_action':{'name':'end_task','arguments':{'content':'原时间已确认','reason':'完成'}}}
    service.transport=SimpleNamespace(exchange=exchange)
    with service.database.connect(write=True) as db:
        db.execute("UPDATE agent_dialogues SET status='running' WHERE id=?",(identifier,))
        job=dict(db.execute('SELECT * FROM agent_dialogues WHERE id=?',(identifier,)).fetchone())
    asyncio.run(service.executor.turn(job))
    with service.database.connect() as db:
        task=db.execute('SELECT status,next_speaker FROM agent_dialogues WHERE id=?',(identifier,)).fetchone()
        assert tuple(task)==('queued',1)
        assert not db.execute("SELECT 1 FROM agent_dialogue_messages WHERE dialogue_id=? AND state='ended'",(identifier,)).fetchone()


def test_task_supplement_requires_actual_peer_reply_not_empty_end(supplemented_task):
    import asyncio
    from types import SimpleNamespace
    from avatar.core.db import now
    service,users,identifier=supplemented_task
    with service.database.connect(write=True) as db:
        db.execute("INSERT INTO agent_pending_inputs(task_id,user_id,request_id,message) VALUES(?,?,?,?)",(identifier,users[0]['id'],'confirm','有空'))
        service.apply_inputs(db,identifier)
        db.execute("INSERT INTO agent_dialogue_messages(dialogue_id,turn,speaker_aic,speaker_name,content,usage,created_at) VALUES(?,2,'peer-0','测试甲的分身','本人已确认有空','null',?)",(identifier,now()))
        db.execute("UPDATE agent_dialogues SET status='running',next_speaker=1 WHERE id=?",(identifier,))
        job=dict(db.execute('SELECT * FROM agent_dialogues WHERE id=?',(identifier,)).fetchone())
    async def exchange(*args):return {'text':'','task_action':{'name':'end_task','arguments':{'reason':'结束'}}}
    service.transport=SimpleNamespace(exchange=exchange)
    with pytest.raises(HTTPException) as error:asyncio.run(service.executor.turn(job))
    assert error.value.status_code==502
    with service.database.connect() as db:
        assert db.execute('SELECT status FROM agent_dialogues WHERE id=?',(identifier,)).fetchone()[0]!='ended'


# Peer request receipts cover the public creation, supplement and session routes

@pytest.fixture
def peers(context, monkeypatch):
    app, actor, peer = context
    with app.state.database.connect(write=True) as db:
        for row in db.execute('SELECT id,username FROM users').fetchall():
            db.execute('INSERT INTO avatar_records(user_id,aic,profile,certificate,last_modified_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                       (row['id'], row['username'], '{}', '{}', now(), now(), now()))
    monkeypatch.setattr(app.state.dialogues.intent, 'requires_confirmation', AsyncMock(return_value=False))
    return app, actor, peer


def test_member_settings_and_shared_file_lock_their_respective_owners(peers, monkeypatch):
    from avatar.api.chat_file_routes import remove
    from avatar.core import postgres

    app, actor, peer = peers
    db = app.state.database
    sessions = app.state.dialogues.session_store
    files = app.state.chat.chat_files
    first = actor.get('/api/auth/me').json()['user']
    second = peer.get('/api/auth/me').json()['user']
    task = actor.post('/api/agent-dialogues',json={'target_aic':'admin','objective':'scope','request_id':'scope'}).json()['id']
    with db.connect(write=True) as tx:
        sid = tx.execute('SELECT session_id FROM agent_dialogues WHERE id=?',(task,)).fetchone()[0]
        shared = app.state.dialogues.resources.workspace(tx,first['id'],sid)
        tx.execute("""INSERT INTO chat_files(id,user_id,conversation_id,filename,content_type,size,extraction_status,created_at)
            VALUES('scoped-file',?,?,'test.txt','text/plain',1,'ready',?)""",(first['id'],shared,now()))
    path = files.directory.path(first['id'],shared,'scoped-file.blob',create=True)
    path.write_bytes(b'x')
    original = postgres.lock_schema_transaction
    def bounded(raw, *args, **kwargs):
        raw.execute("SET LOCAL lock_timeout='500ms'")
        original(raw,*args,**kwargs)
    monkeypatch.setattr(postgres,'lock_schema_transaction',bounded)
    with db.connect(write=True, account_scope=first['id']):
        assert sessions.auto_accept(second,sid,True)['auto_accept']
        sessions.session(second,sid)
    with db.connect() as tx:
        private = tx.execute("""SELECT c.user_id FROM agent_session_members m JOIN chat_conversations c
            ON c.id=m.resource_conversation_id WHERE m.session_id=? AND m.user_id=?""",(sid,second['id'])).fetchone()
        assert private[0] == second['id']
    with pytest.raises(HTTPException) as denied:
        remove(shared,'scoped-file',SimpleNamespace(app=app),{'id':'not-a-member'})
    assert denied.value.status_code == 404 and path.exists()
    with db.connect(write=True, account_scope=second['id']):
        remove(shared,'scoped-file',SimpleNamespace(app=app),second)
    assert not path.exists()


def test_contact_tool_requires_confirmation_and_rejects_foreign_files(peers, monkeypatch):
    from tests.support import MODEL, saved_file

    app, actor, peer = peers
    assert actor.put('/api/me/model-settings', json={'model_config_input': MODEL}).status_code == 200
    user = actor.get('/api/auth/me').json()['user']
    parent = actor.post('/api/conversations', json={'channel': 'assistant'}).json()
    own_file = saved_file(actor, 'mine.txt', b'my notes')
    other_file = saved_file(peer, 'other.txt', b'private notes')
    ticks = []

    async def stream(config, key, system, history, message):
        ticks.append(config.get('_call_purpose'))
        observed = [row for row in history if row['role'] == 'tool' and row['tool_call_id'] == 'contact']
        if observed:
            assert observed[-1]['tool_call_id'] == 'contact'
            assert observed[-1]['is_error'] and '不可用的文件' in observed[-1]['content']
            yield {'type': 'delta', 'text': '指定附件不可用。'}
            yield {'type': 'usage', 'usage': {}, 'finish_reason': 'stop'}
            return
        active = {tool['name'] for tool in config['_tools']}
        if 'contact_avatar' not in active:
            assert 'enable_tools' in active
            yield _react_probe_receipt(_react_probe_call('contact-enable','enable_tools',{'names':['contact_avatar']}))
            return
        prompt = message or next(row['content'] for row in reversed(history) if row['role'] == 'user')
        called = {'id': 'contact', 'name': 'contact_avatar', 'arguments': {
            'target_query': 'admin', 'content': 'review notes',
            'file_ids': [other_file if 'other' in prompt else own_file],
        }}
        yield {'type': 'usage', 'usage': {}, 'tool_calls': (called,), 'finish_reason': 'tool_calls'}

    async def run(message):
        body = ChatRequest(conversation_id=parent['id'], message=message, personal=True)
        events = [event async for event in app.state.chat.stream(user, body)]
        assert events[-1]['type'] == 'done', events[-1]
        return events[-1]

    monkeypatch.setattr(app.state.chat.model_client, 'stream', stream)
    draft = asyncio.run(run('Share my notes for review'))['intent']
    assert draft['status'] == 'pending' and draft['conversation_id'] == parent['id']
    assert draft['requested_files'] == [{'source': 'profile', 'id': own_file}]
    denied = asyncio.run(run('Share the other notes'))
    assert denied['intent'] is None and denied['reply'] == '指定附件不可用。'
    assert 'agent_intent' not in ticks
    with app.state.database.connect() as db:
        intents = db.execute('SELECT id,status,task_id FROM agent_intents WHERE user_id=?', (user['id'],)).fetchall()
        assert [tuple(row) for row in intents] == [(draft['id'], 'pending', None)]
        assert db.execute('SELECT count(*) FROM agent_peer_sessions').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM agent_dialogues').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM agent_task_files').fetchone()[0] == 0


def test_peer_creation_limits_include_pending_and_allow_replay(peers):
    app, actor, peer = peers
    service = app.state.dialogues
    with service.database.connect() as db:
        ids = {row['username']: row['id'] for row in db.execute('SELECT username,id FROM users')}
    actor_id = next(value for name, value in ids.items() if name != 'admin')
    owner = {'id': actor_id}

    def create(key):
        return service.create(owner, SimpleNamespace(target_aic='admin', objective='start', request_id=key))

    first = create('quota-first')
    for index in range(6):
        create('quota-' + str(index))

    def last_slot(key):
        try:
            return create(key)
        except HTTPException as error:
            assert error.status_code == 429
            return None

    with ThreadPoolExecutor(2) as pool:
        assert sum(bool(item) for item in pool.map(last_slot, ['quota-left', 'quota-right'])) == 1
    assert create('quota-first') == first
    with service.database.connect() as db:
        assert db.execute("SELECT count(*) FROM agent_dialogues WHERE status='awaiting_acceptance'").fetchone()[0] == 8

    session = service.session_store.get(owner,first)['session_id']
    expected = service.session_store.session(owner,session)
    assert service.session_store.sessions(owner)['items'] == [expected]
    with service.database.connect(write=True) as db:
        db.execute("UPDATE agent_dialogues SET status='ended' WHERE id=?", (first,))
        db.execute("""INSERT INTO agent_dialogues(id,user_id,own_aic,target_user_id,target_aic,objective,request_id,created_at,updated_at)
            SELECT 'global-'||i,?,'admin',?,'peer','task','global-'||i,?,? FROM generate_series(1,57) i""",
            (ids['admin'],actor_id,now(),now()))
    with pytest.raises(HTTPException, match='队列已满') as full:
        create('quota-platform')
    assert full.value.status_code == 429


def test_legacy_peer_states_migrate_once_without_changing_originals(peers):
    from avatar.core.migrations.peer_states import migrate_peer_states
    app, actor, _ = peers
    service = app.state.dialogues
    task = actor.post('/api/agent-dialogues',json={'request_id':'legacy','target_aic':'admin','objective':'legacy'}).json()['id']
    fenced = '示例：\n~~~\n[任务结束] 不是状态\n~~~'
    original = '已经完成。\n[任务结束] 原因：已交付'
    with service.database.connect(write=True) as db:
        db.execute("UPDATE agent_dialogues SET status='queued' WHERE id=?", (task,))
        for turn,body in enumerate([fenced,original],1):
            db.execute("""INSERT INTO agent_dialogue_messages(dialogue_id,turn,speaker_aic,speaker_name,content,usage,created_at)
                VALUES(?,?,'admin','peer',?,'null',?)""",(task,turn,body,now()))
        db.execute("DELETE FROM data_migrations WHERE name='peer-structured-states-v1'")
        migrate_peer_states(db.raw)
        migrate_peer_states(db.raw)
        rows = db.execute("""SELECT content,legacy_display_content,state,task_action,state_detail FROM agent_dialogue_messages
            WHERE dialogue_id=? ORDER BY id""",(task,)).fetchall()
        assert tuple(rows[0]) == (fenced,None,'','','')
        assert tuple(rows[1]) == (original,'已经完成。','ended','end_task','已交付')
    service.recovery.recover()
    service.recovery.recover()
    with service.database.connect() as db:
        assert tuple(db.execute('SELECT status,end_reason FROM agent_dialogues WHERE id=?',(task,)).fetchone()) == ('ended','已交付')
        assert db.execute('SELECT count(*) FROM agent_protocol_controls WHERE task_id=?',(task,)).fetchone()[0] == 1


def test_create_and_resume_compare_original_payload(peers):
    app, actor, peer = peers
    path = '/api/agent-dialogues'
    body = {'request_id': 'create-1', 'target_aic': 'admin', 'objective': 'original'}
    task = actor.post(path, json=body).json()['id']
    assert actor.post(path, json=body).json()['id'] == task
    for change in ({'objective': 'changed'}, {'kind': 'booking'}, {'max_rounds': 8}, {'file_ids': ['extra']}):
        assert actor.post(path, json={**body, **change}).status_code == 409
    resume = f'{path}/{task}/resume'
    extra = {'request_id': 'supplement-1', 'message': 'first'}
    assert actor.post(resume, json=extra).status_code == 200
    assert actor.post(resume, json=extra).status_code == 200
    assert actor.post(resume, json={**extra, 'file_ids': ['extra']}).status_code == 409
    assert peer.post(resume, json=extra).status_code == 200
    with app.state.database.connect(write=True) as db:
        assert db.execute('SELECT count(*) FROM agent_pending_inputs').fetchone()[0] == 2
        db.execute('DELETE FROM agent_request_receipts')
    assert actor.post(path, json=body).status_code == 409
    assert actor.post(resume, json=extra).status_code == 409


def test_session_replay_keeps_original_task_after_completion(peers):
    app, actor, _ = peers
    task = actor.post('/api/agent-dialogues', json={'request_id':'base', 'target_aic':'admin', 'objective':'start'}).json()['id']
    with app.state.database.connect() as db:
        session = db.execute('SELECT session_id FROM agent_dialogues WHERE id=?', (task,)).fetchone()[0]
    path = f'/api/agent-sessions/{session}/messages'
    body = {'request_id':'session-message', 'message':'more', 'delivery_mode':'negotiation'}
    assert actor.post(path, json=body).json()['id'] == task
    with app.state.database.connect(write=True) as db:
        db.execute("UPDATE agent_dialogues SET status='ended', ended_at=? WHERE id=?", (now(), task))
    assert actor.post(path, json=body).json()['id'] == task
    assert actor.post(path, json={**body,'new_task':True}).status_code == 409
    with app.state.database.connect() as db:
        assert db.execute('SELECT count(*) FROM agent_dialogues').fetchone()[0] == 1


def test_approval_with_input_is_one_receipt(peers):
    app, actor, peer = peers
    task = actor.post('/api/agent-dialogues', json={'request_id':'approve-base','target_aic':'admin','objective':'start'}).json()['id']
    path = f'/api/agent-dialogues/{task}/accept'
    body = {'request_id':'approve-1','message':'ack'}
    assert actor.post(path, json=body).status_code == 403
    assert peer.post(path, json=body).status_code == 200
    assert peer.post(path, json=body).status_code == 200
    assert peer.post(path, json={**body,'message':'change'}).status_code == 409
    with app.state.database.connect() as db:
        assert db.execute('SELECT count(*) FROM agent_pending_inputs WHERE task_id=?', (task,)).fetchone()[0] == 1


def test_concurrent_supplements_classify_once_and_hidden_task_stays_hidden(peers, monkeypatch):
    import asyncio
    from avatar.api.collaboration_routes import DialogueResume
    app, actor, _ = peers
    user = actor.get('/api/auth/me').json()['user']
    body = {'request_id':'concurrent-base', 'target_aic':'admin', 'objective':'start'}
    task = actor.post('/api/agent-dialogues', json=body).json()['id']
    calls = []
    async def classify(*args):
        calls.append(1)
        await asyncio.sleep(0)
        return False
    dialogues = app.state.dialogues
    monkeypatch.setattr(dialogues.intent, 'requires_confirmation', classify)
    request = DialogueResume(request_id='same', message='extra')
    async def concurrent():
        return await asyncio.gather(dialogues.resume(user, task, request), dialogues.resume(user, task, request))
    assert all(row['id'] == task for row in asyncio.run(concurrent()))
    assert len(calls) == 1
    with app.state.database.connect(write=True) as db:
        db.execute('UPDATE agent_task_members SET hidden=1 WHERE task_id=? AND user_id=?', (task, user['id']))
    assert actor.post('/api/agent-dialogues', json=body).status_code == 404


# Peer acknowledgements survive process restarts and never invent terminal states

@pytest.mark.parametrize('action', ['complete', 'cancel'])
def test_control_receipt_survives_lost_response_and_late_generation(pg_database, action):
    wire = SimpleNamespace(local_aic='receiver', peer_aic='sender')
    async def check():
        gate = asyncio.Event()
        attempts = []
        async def answer(*args):
            attempts.append(1)
            try:
                await gate.wait()
            except asyncio.CancelledError:
                pass
            return SimpleNamespace(text='model output', usage=None, task_action=None)

        def handler():
            h = PeerDialogueTasks(SimpleNamespace(database=pg_database, executor=SimpleNamespace(answer_peer=answer)), SimpleNamespace())
            h.scope = lambda *args: dict(user_id='left', target_user_id='right', next_speaker=1, status='running')
            return h

        h = handler()
        def cmd(operation, identifier):
            return dict(id=identifier, taskId='T', sessionId='S', command=operation)
        async def send(operation, identifier):
            return await h.handle(cmd(operation, identifier), wire, 'right')

        with pytest.raises(ManagementTaskError) as missing:
            await send(action, 'missing')
        assert missing.value.code == -32001
        with pytest.raises(ManagementTaskError):
            await send('continue', 'out-of-order')
        starts = await asyncio.gather(send('start', 'generation'), send('start', 'generation'))
        assert starts[0] == starts[1] and starts[0]['status']['state'] == 'working'
        pending = list(h.running.values())[0]
        await asyncio.sleep(0)
        with pytest.raises(ManagementTaskError) as changed:
            await h.handle({**cmd('start','generation'),'dataItems':[{'type':'text','text':'changed'}]},wire,'right')
        assert changed.value.code == -32602
        with pytest.raises(ManagementTaskError) as premature:
            await send('complete', 'premature')
        assert premature.value.code == -32010
        if action == 'complete':
            gate.set()
            await pending
        receipt = await send(action, 'control')
        gate.set()
        await pending
        h = handler()
        h.recover()
        assert await send(action, 'control') == receipt
        assert (await send('get', 'read'))['status']['state'] == ('completed' if action == 'complete' else 'canceled')
        with pytest.raises(ManagementTaskError):
            await send('cancel' if action == 'complete' else 'complete', 'control')
        with pytest.raises(ManagementTaskError):
            await send('continue', 'later')
        with pg_database.connect(write=True) as db:
            db.execute('DELETE FROM agent_wire_receipts WHERE command_id=?', ('generation',))
        with pytest.raises(ManagementTaskError):
            await send('start', 'generation')
        assert (await send('get', 'read'))['status']['state'] == receipt['status']['state']
        assert attempts == [1]
    asyncio.run(check())


def test_control_delivery_only_visits_participating_direction(context):
    app, _, _ = context
    # Seed only the inbound leg, as happens when a task ends after its first speaker.
    uid, aic = 'receiver', 'receiver-aic'
    with app.state.database.connect(write=True) as db:
        db.execute('INSERT INTO agent_wire_results VALUES(?,?,?,?,?,?,?)',
                   ('turn', aic, 'other-aic', 'S', 'T', 'awaiting-completion', '{}'))
    transport = PeerAcpsTransport(SimpleNamespace(database=app.state.database))
    sent = []
    async def record(job, leader, partner, operation, command_id):
        sent.append((leader, partner, operation, command_id))
        return {'status': {'state': 'completed'}, 'taskId':job['id'], 'sessionId':job['session_id']}
    transport.send = record
    job = dict(id='T',session_id='S',own_aic='other-aic',target_aic=aic)
    asyncio.run(transport.complete(job, 'other-user', uid))
    asyncio.run(transport.complete(job, 'other-user', uid))
    assert len(sent) == 2 and sent[0] == sent[1] and sent[0][:3] == ('other-user',uid,'complete')


def test_competing_terminal_commands_across_workers(pg_database):
    wire = SimpleNamespace(local_aic='receiver', peer_aic='sender')
    barrier = Barrier(2)
    with pg_database.connect(write=True) as db:
        db.execute('INSERT INTO agent_wire_results VALUES(?,?,?,?,?,?,?)',
                   ('generation','receiver','sender','S','T','awaiting-completion','{}'))

    def deliver(action):
        handler = PeerDialogueTasks(SimpleNamespace(database=pg_database), SimpleNamespace())
        handler.scope = lambda *args: {}
        command = dict(id=action, taskId='T', sessionId='S', command=action)
        barrier.wait(timeout=10)
        try:
            return asyncio.run(handler.handle(command,wire,'right'))['status']['state']
        except ManagementTaskError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(deliver, ['complete','cancel']))
    with pg_database.connect() as db:
        state = db.execute('SELECT state FROM agent_wire_results').fetchone()[0]
        assert db.execute('SELECT count(*) FROM agent_wire_receipts').fetchone()[0] == 1
    assert state in ('completed','canceled') and set(states) == {state,-32010}


def test_control_requires_matching_ack_for_each_direction(context):
    app, _, _ = context
    database = app.state.database
    transport = PeerAcpsTransport(SimpleNamespace(database=database))
    job = dict(id='T', session_id='S', own_aic='A', target_aic='B')
    with database.connect(write=True) as db:
        for local, peer in [('A','B'),('B','A')]:
            db.execute('INSERT INTO agent_wire_results VALUES(?,?,?,?,?,?,?)',
                       ('turn',local,peer,'S','T','awaiting-completion','{}'))
    sent = []
    async def send(job, leader, partner, operation, command_id):
        sent.append((leader,command_id))
        session = 'stale' if len(sent) == 2 else 'S'
        return {'status':{'state':'completed'}, 'sessionId':session,'taskId':'T'}
    transport.send = send
    with pytest.raises(HTTPException) as stale:
        asyncio.run(transport.complete(job,'left','right'))
    assert stale.value.status_code == 502
    asyncio.run(transport.complete(job,'left','right'))
    assert len(sent) == 4 and sent[:2] == sent[2:] and sent[0] != sent[1]
    with pytest.raises(HTTPException):
        asyncio.run(transport.complete({**job,'target_aic':'changed'},'left','right'))


@pytest.mark.parametrize('operation',['cleanup','authority'])
def test_peer_shutdown_drains_work_before_database_close(operation):
    from threading import Event
    from avatar.core.execution import check_execution_async, execution_guard
    from avatar.conversation.collaboration.recovery import PeerRecovery

    async def check():
        entered, release = Event(), Event()
        order = []
        def cleanup():
            entered.set()
            assert release.wait(5)
            order.append('cleanup finished')

        recovery = PeerRecovery(SimpleNamespace(database=None,resources=None,transport=None,close_record=None),{})
        recovery.recover = lambda: None
        recovery.cleanup = cleanup
        recovery.worker = recovery.control_loop = asyncio.Event().wait

        async def lifespan():
            try:
                if operation == 'cleanup':
                    await recovery.run()
                else:
                    with execution_guard(cleanup):
                        await check_execution_async()
            finally:
                order.append('database closed')

        task = asyncio.create_task(lifespan())
        try:
            assert await asyncio.to_thread(entered.wait,3)
            task.cancel()
            done, _ = await asyncio.wait({task},timeout=.1)
            assert not done
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled() and order == ['cleanup finished','database closed']
    asyncio.run(check())


def test_peer_scheduler_recovers_database_without_replaying_sends(monkeypatch):
    from collections import Counter
    from psycopg import OperationalError
    from psycopg_pool import PoolTimeout
    from avatar.conversation.collaboration import recovery as module

    attempts = Counter()
    def fail_first(name,error,result=None):
        attempts[name] += 1
        if attempts[name] == 1:
            raise error
        return result

    async def check():
        async def turn(job):
            attempts['turn'] += 1
            raise HTTPException(502,'unknown response')
        async def deliver(*args):
            attempts['control'] += 1
        recovery = module.PeerRecovery(SimpleNamespace(database=None,resources=None,
            transport=lambda:SimpleNamespace(complete=deliver),close_record=None,turn=turn),{})
        job = {'id':'task','revision':0,'error':'','status':'queued','retry_until':0}
        control = {'task_id':'task','action':'complete','user_id':'a','target_user_id':'b'}
        recovery.recover = lambda: fail_first('recover',PoolTimeout())
        recovery.claim = lambda *args: fail_first('claim',OperationalError(),job)
        recovery.claim_control = lambda *args: fail_first('claim_control',PoolTimeout(),control)
        recovery.record_failure = lambda *args: fail_first('record_failure',OperationalError())
        recovery.finish_control = lambda *args: fail_first('finish_control',OperationalError())
        async def worker():
            if not attempts['worker']:
                attempts['worker'] += 1
                assert await recovery.once()
        recovery.worker = worker
        recovery.control_loop = recovery.flush_controls
        recovery.cleanup_loop = AsyncMock()
        monkeypatch.setattr(module,'DB_RETRY_DELAY',.001,raising=False)
        await asyncio.wait_for(recovery.run(),3)
        assert attempts == {'recover':2,'claim':2,'claim_control':2,'worker':1,'record_failure':2,
                            'finish_control':2,'control':1,'turn':1}
    asyncio.run(check())


def test_peer_claim_confirms_lost_commit_without_claiming_another_job(pg_database,monkeypatch):
    from contextlib import contextmanager
    from psycopg import OperationalError
    from avatar.conversation.collaboration import recovery as module

    store = module.PeerRecovery(SimpleNamespace(database=pg_database,resources=None,transport=None,close_record=None),{})
    original = pg_database.connect
    monkeypatch.setattr(module,'DB_RETRY_DELAY',.001)
    for committed in (True,False):
        key = 'lost-claim-' + str(committed)
        attempts = []
        drop = True
        @contextmanager
        def lose_connection(*args,**kwargs):
            nonlocal drop
            fail,drop = drop,False
            with original(*args,**kwargs) as db:
                yield db
                if fail and not committed:
                    raise OperationalError('rolled back')
            if fail:
                raise OperationalError('commit reply lost')
        def select(db):
            attempts.append(key)
            db.execute('INSERT INTO settings(key,value) VALUES(?,?)',(key,'claimed'))
            return {'id':key}
        monkeypatch.setattr(pg_database,'connect',lose_connection)
        assert asyncio.run(module.retry_database(store.claim_record, [], select)) == {'id':key}
        assert attempts == [key] * (1 if committed else 2)


def test_peer_shutdown_cancels_database_backoff_after_acknowledged_control(monkeypatch):
    from threading import Event
    from psycopg_pool import PoolTimeout
    from avatar.conversation.collaboration import recovery as module

    async def check():
        failed = Event()
        sent = AsyncMock()
        attempts = []
        def offline(task_id,error=None):
            attempts.append(error)
            failed.set()
            raise PoolTimeout()
        recovery = module.PeerRecovery(SimpleNamespace(database=None,resources=None,close_record=None,
            transport=lambda:SimpleNamespace(complete=sent)),{})
        recovery.claim_control = lambda *args: {'task_id':'T','action':'complete','user_id':'a','target_user_id':'b'}
        recovery.finish_control = offline
        monkeypatch.setattr(module,'DB_RETRY_DELAY',60)
        task=asyncio.create_task(recovery.flush_controls())
        assert await asyncio.to_thread(failed.wait,3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task,.5)
        assert attempts == [None,None] and sent.await_count == 1
    asyncio.run(check())


def test_cancelled_peer_claim_releases_the_persisted_work():
    from threading import Event
    from avatar.conversation.collaboration.recovery import PeerRecovery

    async def check():
        entered, release = Event(), Event()
        assigned = {'id':'task','task_id':'task','status':'queued','error':'','retry_until':0}
        results = []
        def claim(*args):
            entered.set()
            assert release.wait(3)
            return assigned
        recovery = PeerRecovery(SimpleNamespace(database=None,resources=None,transport=None,close_record=None),{})
        recovery.claim = recovery.claim_control = claim
        recovery.record_failure = recovery.finish_control = lambda *args: results.append(args)
        for start in (recovery.once, recovery.flush_controls):
            entered.clear()
            release.clear()
            work = asyncio.create_task(start())
            assert await asyncio.to_thread(entered.wait,3)
            work.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await work
        assert results == [(assigned,'queued','',0), ('task','CancelledError')]
    asyncio.run(check())


def test_control_queue_recovers_running_delivery_after_restart(peers, monkeypatch):
    app, actor, _ = peers
    service = app.state.dialogues
    task = actor.post('/api/agent-dialogues',json={'request_id':'queue','target_aic':'admin','objective':'delivery'}).json()['id']
    with service.database.connect(write=True) as db:
        db.execute("UPDATE agent_dialogues SET status='ended' WHERE id=?", (task,))
        db.execute("INSERT INTO agent_protocol_controls(task_id,action,status) VALUES(?,'cancel','running')", (task,))

    calls = []
    async def deliver(job, left, right):
        calls.append((job['id'],left,right))
        if len(calls) == 1:
            raise TimeoutError('lost response')
    async def idle():
        pass
    monkeypatch.setattr(service,'transport',SimpleNamespace(cancel=deliver))
    monkeypatch.setattr(service.recovery,'worker',idle)
    monkeypatch.setattr(service.recovery,'control_loop',lambda: service.recovery.flush_controls(task))
    monkeypatch.setattr(service.recovery,'cleanup_loop',idle)
    asyncio.run(service.recovery.run())
    with service.database.connect(write=True) as db:
        row = db.execute('SELECT status,error FROM agent_protocol_controls WHERE task_id=?',(task,)).fetchone()
        assert tuple(row) == ('queued','TimeoutError')
        db.execute('UPDATE agent_protocol_controls SET next_attempt=0 WHERE task_id=?',(task,))
    assert asyncio.run(service.recovery.flush_controls(task))
    with service.database.connect() as db:
        row = db.execute('SELECT status,error FROM agent_protocol_controls WHERE task_id=?',(task,)).fetchone()
        assert tuple(row) == ('done','')
    assert len(calls) == 2 and calls[0] == calls[1]


def test_exchange_replays_first_command_without_changing_operation(context):
    from avatar.core.db import now
    app, actor, peer = context
    leader = actor.get('/api/auth/me').json()['user']['id']
    partner = peer.get('/api/auth/me').json()['user']['id']
    database = app.state.database
    with database.connect(write=True) as db:
        for uid, aic in ((leader,'sender'), (partner,'receiver')):
            db.execute('INSERT INTO avatar_records(user_id,aic,profile,certificate,last_modified_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                       (uid, aic, '{}', '{}', now(), now(), now()))
    transport = PeerAcpsTransport(SimpleNamespace(database=database))
    sent = []
    async def send(job, leader, partner, operation, key):
        sent.append((operation,key))
        with database.connect(write=True) as db:
            db.execute('INSERT OR IGNORE INTO agent_wire_results VALUES(?,?,?,?,?,?,?)',
                       (key, 'receiver', 'sender', 'S', 'T', 'awaiting-completion', '{}'))
        return {'status': {'state': 'awaiting-completion'}, 'products': []}
    transport.send = send
    job = {'id':'T', 'session_id':'S', 'revision':1}
    for revision in (1,1,2,1):
        asyncio.run(transport.exchange({**job,'revision':revision}, leader, partner))
    assert sent[0] == sent[1] == sent[3]
    assert sent[0][0] == 'start' and sent[2][0] == 'continue'


def test_peer_message_files_persist_within_session_but_not_tasks(pg_database):
    from avatar.files.conversation.peer import PeerResources

    with pg_database.connect(write=True) as db:
        db.executemany(
            "INSERT INTO users(id,username,password,role,created_at) VALUES(?,?,'hash','user','2026-01-01')",
            [(uid, uid) for uid in ('recipient', 'sender', 'someone-else')],
        )
        db.executemany(
            "INSERT INTO agent_peer_sessions(id,left_user_id,right_user_id,creator_id,created_at,updated_at) "
            "VALUES(?,'recipient',?,'recipient','2026-01-01','2026-01-01')",
            [('pair', 'sender'), ('elsewhere', 'someone-else')],
        )
        db.executemany(
            "INSERT INTO agent_dialogues(id,user_id,own_aic,target_user_id,target_aic,objective,request_id,"
            "created_at,updated_at,session_id,delivery_mode) "
            "VALUES(?,'recipient','recipient-aic',?,'peer-aic','test',?,'2026-01-01','2026-01-01',?,?)",
            [(task, peer, task, session, mode) for task, peer, session, mode in (
                ('old', 'sender', 'pair', 'message'), ('new', 'sender', 'pair', 'message'),
                ('task', 'sender', 'pair', 'task'), ('other', 'someone-else', 'elsewhere', 'message'),
            )],
        )
        db.executemany(
            "INSERT INTO chat_conversations(id,user_id,channel,target_aic,caller_id,platform,title,created_at,updated_at) "
            "VALUES(?,'recipient','peer','recipient-aic','recipient','web','','2026-01-01','2026-01-01')",
            [('pair-workspace',), ('elsewhere-workspace',), ('task-workspace',)],
        )
        db.executemany(
            "INSERT INTO agent_session_members(session_id,user_id,resource_conversation_id) VALUES(?,'recipient',?)",
            [('pair', 'pair-workspace'), ('elsewhere', 'elsewhere-workspace')],
        )
        db.executemany(
            "INSERT INTO agent_task_members(task_id,user_id,resource_conversation_id) VALUES(?,'recipient',?)",
            [('old', None), ('new', None), ('task', 'task-workspace'), ('other', None)],
        )
        db.executemany(
            "INSERT INTO chat_files(id,user_id,conversation_id,filename,content_type,size,extraction_status,created_at,referenced) "
            "VALUES(?,'recipient',?,'note.txt','text/plain',1,'ready','2026-01-01',?)",
            [('shared', 'pair-workspace', 1), ('disabled', 'pair-workspace', 0),
             ('task-only', 'task-workspace', 1), ('other-pair', 'elsewhere-workspace', 1)],
        )
        db.executemany(
            "INSERT INTO agent_task_files(task_id,file_id,source_user_id) VALUES(?,?,?)",
            [('old', 'shared', 'sender'), ('old', 'disabled', 'sender'),
             ('task', 'task-only', 'sender'), ('other', 'other-pair', 'someone-else')],
        )
        service=object.__new__(PeerResources)
        assert [row['id'] for row in service.selected(db,'new','recipient')]==['shared']
        assert [row['id'] for row in service.selected(db,'task','recipient')]==['task-only']
        assert service.selected(db,'new','someone-else')==[]
        db.execute("UPDATE chat_files SET expires_at='2000-01-01' WHERE id='shared'")
        assert service.selected(db,'new','recipient')==[]


def test_react_entry_checkpoints_keep_proofs_written_before_model(context, supplemented_task, monkeypatch):
    import time
    from avatar.conversation.loop import personal
    from avatar.conversation.loop.peer import PeerTurn
    from avatar.conversation.loop.runtime import load_checkpoint
    from avatar.core.db import dump
    from avatar.protocols.tasks import personal_turns
    from avatar.protocols.tasks.personal import PersonalTaskService

    app, _, _ = context
    service, users, task_id = supplemented_task
    owner = users[1]
    chat = app.state.chat
    session = chat.session(owner['id'], 'avatar')
    lease = chat.memory.acquire(session['id'])
    proof = {'session_file_reads':{'image':{'sha256':'kept'}}, 'session_image_ids':['image'],
             'entry_material':{'sources':[{'file_id':'source'}],'revisions':[[['profile','source'],'version']]}}

    async def assert_new(store):
        assert store.load()['schema_version'] == 2
        store.merge(proof)
        state = await load_checkpoint(store)
        assert {key:state[key] for key in proof} == proof

    try:
        with service.database.connect() as db:
            sources = personal.source_snapshot(db, owner['id'], session['id'], owner['id'], 'peer-1')
        overview = {'agent':{'user_id':owner['id'],'aic':'peer-1'}, '_run_sources':sources}
        request = ChatRequest(channel='avatar', message='读取附件')
        model = ({'provider':'custom','model':'test'},'key','system',[])
        config,*_ = personal.attach(chat, owner, request, session, session, overview, model, None, None,
                                    'entry-proof-test', lease, None)
        asyncio.run(assert_new(config['_run_store']))
    finally:
        chat.memory.release(session['id'], lease)

    with service.database.connect(write=True) as db:
        db.execute("UPDATE agent_dialogues SET status='running',next_speaker=1 WHERE id=?", (task_id,))
        task = dict(service.session_store.owned(db, owner, task_id))
    store, _ = PeerTurn(service.executor, task, owner['id']).open()
    asyncio.run(assert_new(store))

    task_service = PersonalTaskService(chat, app.state.settings)
    scope = ('peer-1','town#public:visitor','','entry-town-session','entry-town-task')
    result = {'id':'entry-writer','taskId':scope[4],'sessionId':scope[3]}
    with service.database.connect(write=True) as db:
        run = {'owner':owner['id'],'run_id':'entry-town-run','automatic':False,'skills':{},
               'sources':personal_turns.source_revision(db, owner['id'], 'peer-1')}
        document = {**result,'status':{'state':'working'},'_agent_resume':run}
        db.execute('INSERT INTO avatar_aip_tasks VALUES(?,?,?,?,?,?,?,?)',(*scope,'start',dump(document),time.time()+60))
        rowid = task_service.task_row(db, scope)['task_number']

    async def bind(*args, store, **kwargs):
        await assert_new(store)
        raise AgentNeedsInput('保留测试证明')

    monkeypatch.setattr(task_service,'target',lambda *_:owner)
    monkeypatch.setattr(task_service,'approved_skills',lambda *_:[])
    monkeypatch.setattr(task_service,'answer',bind)
    progress = SimpleNamespace(deadline=lambda:time.time()+60, update=None, stream_state=None)
    outcome = asyncio.run(task_service.generate(scope,rowid,result,None,None,run,progress))
    assert outcome['status']['state'] == 'awaiting-input'
