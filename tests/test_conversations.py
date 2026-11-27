"""持久聊天任务、会话生命周期与浏览器重连。"""
import asyncio
import json
import pytest
from fastapi import HTTPException
from avatar.conversation.schemas import ChatRequest


# Persistent generation identity and lifecycle; no external model requests

@pytest.fixture
def jobs(context):
    from avatar.conversation.jobs.store import GenerationStore
    app,user,other=context
    owner=user.get('/api/auth/me').json()['user']['id']
    stranger=other.get('/api/auth/me').json()['user']['id']
    cid=user.post('/api/conversations',json={'channel':'assistant','title':'durable jobs'}).json()['id']
    return GenerationStore(app.state.database),{'id':owner},stranger,cid


def request(cid,rid,text='hello'):
    return ChatRequest(conversation_id=cid,request_id=rid,message=text,personal=True)


def test_request_identity_survives_later_turns(jobs):
    store,user,_,cid=jobs
    a=store.submit(user,request(cid,'A'))
    store.finish(user['id'],'A','succeeded',{'reply':'first'})
    store.submit(user,request(cid,'B'))
    store.finish(user['id'],'B','succeeded',{'reply':'second'})
    assert store.submit(user,request(cid,'A'))['task_id']==a['task_id']
    assert store.get(user['id'],'A')['reply']=='first'
    assert store.latest(user['id'],cid)['request_id']=='B'


def test_same_identity_cannot_change_payload(jobs):
    store,user,_,cid=jobs
    store.submit(user,request(cid,'same'))
    assert store.submit(user,request(cid,'same'))['accepted']
    with pytest.raises(HTTPException) as error:
        store.submit(user,request(cid,'same','changed'))
    assert error.value.status_code==409


def test_busy_and_cross_account_boundaries(jobs):
    store,user,stranger,cid=jobs
    store.submit(user,request(cid,'one'))
    with pytest.raises(HTTPException) as error:store.submit(user,request(cid,'two'))
    assert error.value.status_code==409
    assert store.get(stranger,'one') is None
    assert store.latest(stranger,cid) is None
    with pytest.raises(HTTPException) as error:store.submit({'id':stranger},request(cid,'foreign'))
    assert error.value.status_code==404


def test_restart_marks_started_job_indeterminate_and_unlocks_session(jobs):
    store,user,_,cid=jobs
    store.submit(user,request(cid,'started'));store.claim()
    store.recover()
    assert store.get(user['id'],'started')['state']=='indeterminate'
    assert store.submit(user,request(cid,'next'))['accepted']
    assert store.submit(user,request(cid,'started'))['accepted']
    assert [row['request_id'] for row in store.claim(10)]==['next']


def test_cancelled_generation_cannot_be_overwritten_by_late_result(jobs):
    store,user,_,cid=jobs
    store.submit(user,request(cid,'cancel'));store.claim()
    assert store.finish(user['id'],'cancel','cancelled',error='用户已取消')
    assert not store.finish(user['id'],'cancel','succeeded',{'reply':'late'})
    assert store.get(user['id'],'cancel')['state']=='cancelled'


def test_restart_releases_generation_lease(jobs):
    import time
    store,user,_,cid=jobs
    store.submit(user,request(cid,'lease'));store.claim()
    with store.database.connect(write=True) as db:
        db.execute('UPDATE chat_conversations SET lease_until=? WHERE id=?',(time.time()+1000,cid))
    store.recover()
    with store.database.connect() as db:
        assert db.execute('SELECT lease_until FROM chat_conversations WHERE id=?',(cid,)).fetchone()[0]==0


def test_history_commit_finishes_job_and_rejects_late_save(context):
    from avatar.conversation.jobs.store import GenerationStore
    from avatar.conversation.jobs.pipeline import generation_scope
    app,client,_=context;user=client.get('/api/auth/me').json()['user']
    cid=client.post('/api/conversations',json={'channel':'assistant'}).json()['id']
    store=GenerationStore(app.state.database);body=request(cid,'atomic')
    store.submit(user,body);job=store.claim()[0]
    overview={'agent':None}
    with generation_scope(job):app.state.chat.save(user,body,overview,'saved')
    assert store.get(user['id'],'atomic')['done']
    with pytest.raises(HTTPException),generation_scope(job):app.state.chat.save(user,body,overview,'late')
    with app.state.database.connect() as db:
        assert db.execute('SELECT count(*) FROM chat_messages WHERE conversation_id=?',(cid,)).fetchone()[0]==2


def test_clear_forgets_job_content_and_does_not_resurrect_it(context,jobs):
    store,user,_,cid=jobs
    store.submit(user,request(cid,'forgotten'));store.finish(user['id'],'forgotten','succeeded',{'reply':'private'})
    context[0].state.chat.memory.remove(cid,clear=True)
    assert store.latest(user['id'],cid) is None
    assert store.get(user['id'],'forgotten')['state']=='cancelled'
    assert 'private' not in str(store.get(user['id'],'forgotten'))


@pytest.mark.parametrize('activation', ['explicit', 'session'])
def test_queued_generation_ignores_paused_skills(context, jobs, previously_installed_skill, activation):
    from avatar.conversation.jobs.pipeline import generation_scope

    app, _, _ = context
    store, user, _, cid = jobs
    skills = app.state.chat.skills
    message = '$archived-skill'
    if activation == 'session':
        with store.database.connect(write=True) as db:
            db.execute('INSERT INTO chat_session_skills(conversation_id,skill_id,authorized) VALUES(?,?,1)',
                       (cid, previously_installed_skill))
        message = '继续'
    store.submit(user, request(cid, 'scoped', message))
    job = store.claim()[0]
    assert json.loads(job['authorization'])['skills'] == {}
    with generation_scope(job), store.database.connect() as db:
        assert skills.context(user['id'], message, db)[0] == []


def test_changed_configuration_fails_without_calling_model(context,jobs):
    import asyncio
    from avatar.conversation.jobs.runner import GenerationRunner
    app,_,_=context;store,user,_,cid=jobs
    store.submit(user,request(cid,'config-changed'))
    with store.database.connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO model_settings VALUES(?,?,'changed')",(user['id'],'{"model":"changed"}'))
    class Chat:
        async def stream(self,*args):
            raise AssertionError('Changed configuration must not invoke model')
            yield
    asyncio.run(GenerationRunner(Chat(),store.database).execute(store.claim()[0]))
    saved=store.get(user['id'],'config-changed')
    assert saved['state']=='failed' and '模型配置已改变' in saved['error']


def test_real_runtime_releases_session_before_title_work(context,monkeypatch):
    import asyncio
    from avatar.conversation.jobs.runner import GenerationRunner
    from avatar.conversation.jobs.store import GenerationStore
    app,client,_=context;user=client.get('/api/auth/me').json()['user']
    cid=client.post('/api/conversations',json={'channel':'assistant'}).json()['id']
    store=GenerationStore(app.state.database);store.submit(user,request(cid,'real'))
    observed=[]
    async def title(chat,uid,conversation_id,*args):
        with store.database.connect() as db:
            observed.append(db.execute('SELECT lease_until FROM chat_conversations WHERE id=?',(conversation_id,)).fetchone()[0])
        assert store.get(uid,'real')['done']
    monkeypatch.setattr('avatar.conversation.jobs.runner.generate_title',title)
    async def check():
        runner=GenerationRunner(app.state.runtime,app.state.database)
        await runner.execute(store.claim()[0])
    asyncio.run(check())
    assert observed==[0]
    assert store.get(user['id'],'real')['done']


@pytest.mark.parametrize('cleanup_failure',[False,True])
@pytest.mark.parametrize('corrupt_snapshot',[False,True])
def test_bad_job_does_not_stop_generation_runner(context,cleanup_failure,corrupt_snapshot):
    import asyncio
    from contextlib import suppress
    from avatar.conversation.jobs.store import GenerationStore
    from avatar.conversation.jobs.runner import GenerationRunner
    app,user,_=context;uid=user.get('/api/auth/me').json()['user']['id'];store=GenerationStore(app.state.database)
    bad=user.post('/api/conversations',json={'channel':'assistant'}).json()['id']
    good=user.post('/api/conversations',json={'channel':'assistant'}).json()['id']
    store.submit({'id':uid},request(bad,'bad'));store.submit({'id':uid},request(good,'good'))
    with app.state.database.connect(write=True) as db:db.execute("UPDATE chat_generation_jobs SET payload='{}' WHERE request_id='bad'")
    if corrupt_snapshot:
        with app.state.database.connect(write=True) as db:db.execute("UPDATE chat_generation_jobs SET snapshot='invalid' WHERE request_id='bad'")
    calls=[]
    class Chat:
        async def stream(self,actor,body):
            calls.append(body.request_id)
            yield {'type':'done','reply':'completed'}
    async def check():
        runner=GenerationRunner(Chat(),app.state.database)
        if cleanup_failure:
            finish=runner.store.finish
            def unavailable(uid,rid,*args,**kwargs):
                if rid=='bad':raise RuntimeError('temporary persistence failure')
                return finish(uid,rid,*args,**kwargs)
            runner.store.finish=unavailable
        worker=asyncio.create_task(runner.run())
        try:
            for _ in range(100):
                with app.state.database.connect() as db:bad_state=db.execute("SELECT state FROM chat_generation_jobs WHERE request_id='bad'").fetchone()[0]
                if store.get(uid,'good')['done'] and bad_state!='running':break
                await asyncio.sleep(.02)
            assert store.get(uid,'bad')['state']==('indeterminate' if cleanup_failure else 'failed')
            assert store.get(uid,'good')['reply']=='completed'
            assert not worker.done()
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):await worker
    asyncio.run(check())
    assert calls==['good']


@pytest.mark.parametrize(('revocation', 'status', 'reason'), [
    ('account', 403, '账户已停用'),
    ('cancelled', 409, '原请求已结束'),
    ('paused_skill', 403, '因安全问题暂时停止分身技能的使用'),
])
def test_revoked_generation_cannot_commit_history(context, jobs, revocation, status, reason):
    from avatar.conversation.jobs.pipeline import generation_scope

    app, _, _ = context
    store, user, _, cid = jobs
    body = request(cid, 'revoke')
    store.submit(user, body)
    job = store.claim(leased=True)[0]
    if revocation == 'cancelled':
        assert store.finish(user['id'], 'revoke', 'cancelled')
    else:
        with store.database.connect(write=True) as db:
            if revocation == 'account':
                db.execute("UPDATE users SET account_status='disabled' WHERE id=?", (user['id'],))
            else:
                scope = json.loads(job['authorization'])
                scope['skills'] = {'archived-skill-id': 'previous-revision'}
                job['authorization'] = json.dumps(scope)
                db.execute('UPDATE chat_generation_jobs SET authorization=? WHERE task_id=?',
                           (job['authorization'], job['task_id']))
    with pytest.raises(HTTPException) as error, generation_scope(job):
        app.state.chat.save(user, body, {'agent': None}, 'late')
    assert error.value.status_code == status and reason in error.value.detail
    with store.database.connect() as db:
        assert db.execute('SELECT count(*) FROM chat_messages WHERE conversation_id=?', (cid,)).fetchone()[0] == 0


def test_revoked_account_cannot_start_model_request(jobs,monkeypatch):
    import asyncio
    from avatar.models.client import ModelClient
    from avatar.conversation.jobs.pipeline import generation_scope
    store,user,_,cid=jobs;store.submit(user,request(cid,'model-guard'));job=store.claim()[0]
    with store.database.connect(write=True) as db:db.execute("UPDATE users SET account_status='disabled' WHERE id=?",(user['id'],))
    client=ModelClient()
    async def forbidden(*args):raise AssertionError('Revoked account reached model transport')
    monkeypatch.setattr(client,'_generate',forbidden)
    with pytest.raises(HTTPException),generation_scope(job,database=store.database):
        asyncio.run(client.generate({},'key','system',[],'hello'))


def test_success_commit_releases_lease_before_generator_closes(context,jobs):
    import time
    from avatar.conversation.jobs.pipeline import generation_scope
    app,_,_=context;store,user,_,cid=jobs;body=request(cid,'commit-lease')
    store.submit(user,body);job=store.claim()[0]
    with store.database.connect(write=True) as db:db.execute('UPDATE chat_conversations SET lease_until=? WHERE id=?',(time.time()+480,cid))
    with generation_scope(job):app.state.chat.save(user,body,{'agent':None},'complete')
    with store.database.connect() as db:assert db.execute('SELECT lease_until FROM chat_conversations WHERE id=?',(cid,)).fetchone()[0]==0


def test_sandbox_inputs_cannot_expand_after_submission(context,jobs,monkeypatch):
    import asyncio,json
    from avatar.conversation.jobs.pipeline import generation_scope
    from tests.support import saved_file
    app,client,_=context;store,user,_,cid=jobs;chat=app.state.chat
    assert client.patch('/api/conversations/'+cid+'/tools',json={'tools':{'python':True}}).status_code==200
    original=saved_file(client,'original.txt',b'original')
    store.submit(user,request(cid,'inputs'));job=store.claim()[0]
    late=saved_file(client,'late.txt',b'late')
    captured={}
    async def memory(session,modified,context,*args,**kwargs):return context
    async def skills(uid,context,*args,**kwargs):return context
    async def execute(owner,resources,script,args,**kwargs):
        kwargs['authorize']()
        captured.update(resources)
        return {'status':'success','stdout':'original','files':[]}
    monkeypatch.setattr(chat.memory,'prepare',memory)
    monkeypatch.setattr(chat.memory,'needs_compression',lambda *args:False)
    monkeypatch.setattr(chat.skills,'load_context',skills)
    monkeypatch.setattr(chat.sandbox,'execute',execute)
    overview={'agent':{'user_id':user['id'],'aic':'aic','last_modified_at':''},'_private_context':True}
    session={'id':cid,'parent_id':None}
    context=({'context_tokens':32768,'max_tokens':16},'key','system\n'+json.dumps({'available_skills':[]}),[])
    async def use_sandbox():
        prepared=await chat.prepare(user,request(cid,'inputs'),overview,context,session)
        tool=prepared[0]['_execute_entry_tool']
        with pytest.raises(ValueError,match='未授权文件'):
            await tool('run_python',{'code':'print(1)','input_file_ids':[late]})
        assert not captured
        receipt=await tool('run_python',{'code':'print(1)','input_file_ids':[original]})
        assert receipt['status']=='success'
        assert captured['inputs/'+original+'/original.txt']==b'original'
        with store.database.connect(write=True) as db:db.execute('DELETE FROM attachments WHERE id=?',(original,))
        with pytest.raises(HTTPException):prepared[0]['_validate_sandbox']()
    with generation_scope(job):asyncio.run(use_sandbox())


@pytest.mark.parametrize(('disconnect', 'compact'), [(True, True), (False, True), (False, False)])
def test_chat_generation_survives_refresh_and_bounded_queue(context, disconnect, compact):
    from avatar.api.streaming import chat_response
    from avatar.conversation.jobs.runner import GenerationRunner
    from avatar.conversation.schemas import ChatRequest
    app,client,_=context
    actor=client.get('/api/auth/me').json()['user']
    cid=client.post('/api/conversations',json={'channel':'assistant'}).json()['id']
    async def check():
        resume=asyncio.Event()
        class Chat:
            async def stream(self,user,body):
                await resume.wait()
                for _ in range(40):yield {'type':'delta','text':'a'}
                yield {'type':'done','reply':'a'*40}
        runner=GenerationRunner(Chat(),app.state.database)
        worker=asyncio.create_task(runner.run())
        body=ChatRequest(channel='assistant',conversation_id=cid,message='hello',request_id='refresh')
        class Subscription:
            headers = {'x-avatar-stream': 'snapshots'} if compact else {}
            closed = False

            async def is_disconnected(self):
                return self.closed

        subscription = Subscription()
        response=chat_response(subscription,actor,body,app.state.database)
        iterator=response.body_iterator
        try:
            assert 'queued' in await anext(iterator)
            if disconnect:
                subscription.closed = True
                async with asyncio.timeout(6):
                    assert all('event:' not in chunk for chunk in [item async for item in iterator])
            resume.set()
            if not disconnect:
                async with asyncio.timeout(5):
                    tail = [chunk async for chunk in iterator]
                assert any(chunk.startswith('event: done') for chunk in tail)
                assert any(chunk.startswith('event: status') for chunk in tail) is (not compact)
                assert any(chunk.startswith('event: delta') for chunk in tail) is (not compact)
            for _ in range(100):
                saved=runner.store.get(actor['id'],'refresh')
                if saved['done']:break
                await asyncio.sleep(.02)
            assert saved['done'] and saved['reply']=='a'*40
        finally:
            await iterator.aclose()
            worker.cancel()
            await asyncio.gather(worker,return_exceptions=True)
    asyncio.run(check())


def test_unified_html_and_animation_setting(context):
    app,user,_=context
    created=user.post('/api/conversations',json={'channel':'assistant','title':'drawing-default'})
    assert created.status_code==201,created.text
    cid=created.json()['id']
    with app.state.database.connect(write=True) as db:
        db.execute('UPDATE chat_conversations SET tools_config=? WHERE id=?',('{"mermaid":false,"python":false}',cid))
    result=user.get('/api/conversations/'+cid+'/tools')
    assert result.status_code==200,result.text
    def switches(response):
        assert response.status_code == 200, response.text
        return {item['id']: item['enabled'] for item in response.json()['items']}

    assert switches(result) == {'python': False, 'animation': False}
    updated = user.patch('/api/conversations/'+cid+'/tools', json={'tools': {'animation': True}})
    assert switches(updated) == {'python': True, 'animation': True}
    assert switches(user.get('/api/conversations/'+cid+'/tools')) == switches(updated)
    from avatar.conversation.tools.html import CREATE_HTML as html
    assert html['description'].startswith('工具用于生成基于html的网页、2d动画和3d动画')
    assert set(html['parameters']['properties'])==set(html['parameters']['required'])=={'filename','html'}
    assert user.patch('/api/conversations/'+cid+'/tools',json={'tools':{'mermaid':False}}).status_code==422
    assert user.patch('/api/conversations/'+cid+'/tools',json={'tools':{'python':True}}).status_code==200
    from avatar.conversation.tools.builtin import MERMAID_INSTRUCTIONS
    assert '始终可用' in MERMAID_INSTRUCTIONS and 'mermaid' in MERMAID_INSTRUCTIONS


def test_chat_accepted_job_finishes_without_any_browser_connection(context):
    import json
    from contextlib import suppress
    from avatar.api.streaming import generation_worker
    app,user,other=context
    cid=user.post('/api/conversations',json={'channel':'assistant','title':'offline'}).json()['id']
    request={'conversation_id':cid,'channel':'assistant','personal':True,'message':'offline generation','request_id':'offline-job'}
    assert user.post('/api/chat/generations',json=request).status_code==202
    assert user.post('/api/chat/generations',json=request).status_code==202
    assert user.post('/api/chat/generations',json={**request,'request_id':'duplicate'}).status_code==409
    assert other.get('/api/chat/pending',params={'conversation_id':cid}).json() is None
    async def check():
        gate=asyncio.Event()
        class Chat:
            async def stream(self,actor,body):
                yield {'type':'status','message':'后台生成'}
                await gate.wait()
                yield {'type':'delta','text':'offline complete'}
                yield {'type':'done','reply':'offline complete'}
        worker=asyncio.create_task(generation_worker(Chat(),app.state.database))
        try:
            await asyncio.sleep(.1)
            gate.set()
            for _ in range(30):
                with app.state.database.connect() as db:row=db.execute('SELECT snapshot FROM chat_generation_jobs WHERE conversation_id=? AND request_id=?',(cid,'offline-job')).fetchone()
                saved=json.loads(row['snapshot'])
                if saved.get('done'):break
                await asyncio.sleep(.05)
            assert saved['done'] and saved['reply']=='offline complete' and saved['request_id']=='offline-job'
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):await worker
    asyncio.run(check())


def test_dispatch_title_and_save_respect_thread_and_account_boundaries(context, monkeypatch):
    from threading import get_ident
    from unittest.mock import AsyncMock
    from avatar.conversation.jobs.titles import generate_title

    app, client, admin = context
    chat = app.state.chat
    user = client.get('/api/auth/me').json()['user']
    peer = admin.get('/api/auth/me').json()['user']['id']
    cid = client.post('/api/conversations',json={'channel':'assistant'}).json()['id']
    parent = chat.session(user['id'],'assistant',cid)
    body = ChatRequest(channel='assistant',conversation_id=cid,message='选分身')
    original = chat.service.database.connect
    accesses = []
    def connect(*args, **kwargs):
        accesses.append((get_ident(),kwargs.get('write'),kwargs.get('account_scope')))
        return original(*args,**kwargs)
    monkeypatch.setattr(chat.service.database,'connect',connect)
    monkeypatch.setattr(chat.dialogues,'trigger',AsyncMock(return_value={
        'action':'select_avatar','arguments':{'query':'missing'}}))
    async def check():
        await chat.dispatch(user,body,parent)
        await generate_title(chat,user['id'],cid,'主题','回复')
    asyncio.run(check())
    assert accesses and all(thread != get_ident() for thread, _, _ in accesses)
    assert {scope for _, write, scope in accesses if write} == {user['id']}
    for owner in (user['id'], peer):
        accesses.clear()
        chat.save(user,body,{'_target':{'aic':'example','name':'target'},
            'agent':{'last_modified_at':'v1','user_id':owner,'model_config':{'provider':'test','model':'test'}}},'reply')
        assert [scope for _, write, scope in accesses if write] == [user['id'] if owner==user['id'] else None]
    with pytest.raises(HTTPException) as denied:
        chat.save({'id':peer},body,{'agent':None},'foreign message')
    assert denied.value.status_code == 404


def test_context_reads_owner_overview_within_repeatable_read(provisioned_context, monkeypatch):
    app, client, admin = provisioned_context
    user = client.get('/api/auth/me').json()['user']
    peer = admin.get('/api/auth/me').json()['user']
    service = app.state.service
    original = service.overview
    def inspect(owner, *, db=None, **kwargs):
        assert db is not None
        assert db.execute('SHOW transaction_isolation').fetchone()[0] == 'repeatable read'
        assert db.execute('SHOW transaction_read_only').fetchone()[0] == 'on'
        return original(owner,db=db,**kwargs)
    monkeypatch.setattr(service,'overview',inspect)
    overview, _ = app.state.chat.context(user,ChatRequest(channel='avatar',message='hello'),private_access=True)
    assert overview['_private_context']
    peer_view, peer_context = app.state.chat.context(peer,ChatRequest(channel='avatar',message='hello'),
                                                     overview['agent'],private_access=True)
    assert not peer_view['_private_context'] and 'completed_agent_tasks' not in peer_context[2]


def test_history_batches_owner_task_cards_and_excludes_foreign_messages(context, monkeypatch):
    from avatar.core.db import dump, now
    from avatar.core.postgres import Connection

    app, client, admin = context
    owner = client.get('/api/auth/me').json()['user']['id']
    other = admin.get('/api/auth/me').json()['user']['id']
    cid = client.post('/api/conversations',json={'channel':'assistant'}).json()['id']
    with app.state.database.connect(write=True) as db:
        for task, source, peer in [('owned',owner,other),('foreign',other,owner)]:
            db.execute("""INSERT INTO agent_dialogues(id,user_id,own_aic,target_user_id,target_aic,
                       objective,request_id,created_at,updated_at,status,error,retry_until)
                       VALUES(?,?,'a',?,'b','例',?,?,?,'failed',?,1)""",(task,source,peer,task,now(),now(),source))
        for source, task in [(owner,'owned'),(owner,'owned'),(owner,'foreign'),(other,'foreign')]:
            db.execute("""INSERT INTO chat_messages(user_id,conversation_id,channel,role,content,created_at,
                       task_event) VALUES(?,?,'assistant','assistant','',?,?)""",
                       (source,cid,now(),dump({'kind':'start','task_id':task})))
    original = Connection.execute
    task_queries = []
    def execute(db, sql, params=()):
        if 'FROM agent_dialogues' in sql:
            task_queries.append(sql)
        return original(db,sql,params)
    monkeypatch.setattr(Connection,'execute',execute)
    rows = app.state.chat.history(owner,'assistant',cid,personal=True)
    assert len(task_queries) == 1
    assert len(rows) == 3 and all(row['user_id']==owner for row in rows)
    assert [r['task_event'].get('reason') for r in rows] == [owner,owner,None]
    assert rows[0]['task_event']['can_retry']
