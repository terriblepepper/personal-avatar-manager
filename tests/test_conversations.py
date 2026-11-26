"""聊天任务、跨分身协作与技能执行。"""
import asyncio
import json
import runpy
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from avatar.runtime.schema import install_schema
from avatar.core.db import now
from avatar.conversation.schemas import ChatRequest
from avatar.models.client import ModelClient
from avatar.protocols.tasks.dialogues import PeerDialogueTasks
from avatar.protocols.tasks.store import ManagementTaskError
from avatar.protocols.transport.peer import PeerAcpsTransport
from avatar.core.execution import AgentNeedsInput
from avatar.skills.execution import SkillExecution, SkillEnrichRequest
from tests.support import legacy_sqlite



@pytest.mark.parametrize('fmt', ['openai', 'anthropic', 'gemini'])
def test_native_tool_protocol_replays_calls_and_results(fmt):
    from avatar.models.client import parse_response
    tool = {'name': 'lookup', 'description': 'lookup', 'parameters': {'type':'object'}}
    config = {'api_format':fmt, 'provider':'custom', 'model':'mock', 'base_url':'https://example.test',
              'max_tokens':512, '_tools':[tool], '_react_tool_calls':True, '_optional_tools':True, '_batch_tools':True}
    if fmt == 'openai':
        payload = {'choices':[{'finish_reason':'tool_calls','message':{'reasoning_content':'private',
            'tool_calls':[{'id':'actual','function':{'name':'lookup','arguments':'{"q":"one"}'}},
                          {'id':'other','function':{'name':'hidden','arguments':'{'}}]}}]}
    elif fmt == 'anthropic':
        payload = {'stop_reason':'tool_use','content':[{'type':'thinking','thinking':'private','signature':'opaque'},
            {'type':'tool_use','id':'actual','name':'lookup','input':{'q':'one'}},
            {'type':'tool_use','id':'other','name':'hidden','input':'{'}]}
    else:
        payload = {'candidates':[{'finishReason':'STOP','content':{'parts':[
            {'text':'private','thought':True,'thoughtSignature':'first'},
            {'functionCall':{'id':'actual','name':'lookup','args':{'q':'one'}},'thoughtSignature':'opaque'},
            {'functionCall':{'name':'hidden','args':['bad']}}]}}]}
    result = parse_response(fmt, payload, config=config)
    first, second = result.tool_calls
    assert first['id'] == 'actual' and first['arguments'] == {'q':'one'}
    assert second['name'] == 'hidden' and second['argument_error'] and second['arguments'] == {}
    assert result.stop_reason == 'tool'
    assert 'private' not in repr(result)
    history = [{'role':'user','content':'look it up'},
               {'role':'assistant','content':result.text,'tool_calls':result.tool_calls,'replay':result.replay},
               {'role':'tool','tool_call_id':first['id'],'name':'lookup','content':'found'},
               {'role':'tool','tool_call_id':second['id'],'name':'hidden','content':'invalid','is_error':True}]
    *_, body = ModelClient().build_request(config, 'key', 'system', history, None)
    if fmt == 'openai':
        assistant, receipt, error = body['messages'][-3:]
        assert assistant['reasoning_content'] == 'private'
        assert [c['id'] for c in assistant['tool_calls']] == ['actual','other']
        assert receipt == {'role':'tool','tool_call_id':'actual','content':'found'}
        assert error['tool_call_id'] == 'other'
    elif fmt == 'anthropic':
        assistant, reply = body['messages'][-2:]
        assert assistant['content'][0] == payload['content'][0]
        assert [(r['tool_use_id'],r.get('is_error')) for r in reply['content']] == [('actual',None),('other',True)]
    else:
        assistant, reply = body['contents'][-2:]
        declaration, = body['tools'][0]['functionDeclarations']
        assert declaration == {'name':'lookup','description':'lookup','parametersJsonSchema':tool['parameters']}
        assert assistant['parts'][0] == payload['candidates'][0]['content']['parts'][0]
        assert assistant['parts'][1]['thoughtSignature'] == 'opaque'
        assert assistant['parts'][1]['functionCall']['id'] == reply['parts'][0]['functionResponse']['id'] == 'actual'
        assert 'id' not in reply['parts'][1]['functionResponse']
        assert reply['parts'][1]['functionResponse']['response'] == {'error':'invalid'}



@pytest.mark.parametrize('fmt', ['openai', 'anthropic', 'gemini'])
def test_native_tool_stream_preserves_replay_and_call_ids(fmt):
    from avatar.models.trace import model_io_scope
    tool = {'name': 'lookup', 'description': 'lookup', 'parameters': {'type':'object'}}
    config = {'api_format':fmt, 'provider':'custom', 'model':'mock', 'base_url':'https://example.test',
              'max_tokens':512, '_tools':[tool], '_react_tool_calls':True, '_optional_tools':True, '_batch_tools':True}
    if fmt == 'openai':
        frames = [
            {'choices':[{'delta':{'reasoning_content':'private_'},'finish_reason':None}]},
            {'choices':[{'delta':{'reasoning_content':'opaque','tool_calls':[
                {'index':0,'id':'first','function':{'name':'lookup','arguments':'{"q"'}}]},'finish_reason':None}]},
            {'choices':[{'delta':{'tool_calls':[{'index':0,'function':{'arguments':':"one"}'}}]},
                         'finish_reason':'tool_calls'}]},
        ]
    elif fmt == 'anthropic':
        frames = [
            {'type':'content_block_start','index':0,'content_block':{'type':'thinking','thinking':''}},
            {'type':'content_block_delta','index':0,'delta':{'type':'thinking_delta','thinking':'private_opaque'}},
            {'type':'content_block_delta','index':0,'delta':{'type':'signature_delta','signature':'signed'}},
            {'type':'content_block_stop','index':0},
            {'type':'content_block_start','index':1,'content_block':{'type':'tool_use','id':'first','name':'lookup','input':{}}},
            {'type':'content_block_delta','index':1,'delta':{'type':'input_json_delta','partial_json':'{"q":"one"}'}},
            {'type':'content_block_stop','index':1},
            {'type':'message_delta','delta':{'stop_reason':'tool_use'}},
            {'type':'message_stop'},
        ]
    else:
        frames = [
            {'candidates':[{'content':{'parts':[
                {'text':'private_opaque','thought':True,'thoughtSignature':'signed'},
                {'functionCall':{'id':'first','name':'lookup','willContinue':True,'partialArgs':[
                    {'jsonPath':'$.q','stringValue':'o','willContinue':True}]},'thoughtSignature':'opaque'},
            ]}}]},
            {'candidates':[{'content':{'parts':[{'functionCall':{'willContinue':True,
               'partialArgs':[{'jsonPath':'$.q','stringValue':'ne','willContinue':True}]}}]}}]},
            {'candidates':[{'content':{'parts':[{'functionCall':{'partialArgs':[{'jsonPath':'$.q'}]}}]},
                            'finishReason':'STOP'}]},
        ]
    stream = ''.join('data: '+json.dumps(frame)+'\n\n' for frame in frames)
    async def resolver(host, port):
        return '203.0.113.9'
    model = ModelClient(httpx.MockTransport(lambda _: httpx.Response(
        200, headers={'content-type':'text/event-stream'}, text=stream)), resolver=resolver)

    async def exercise():
        events = []
        with model_io_scope(events.append, private=True):
            reply = [event async for event in model.stream(config, 'key', 'system', [], 'find')]
        assert reply[-1]['type'] == 'usage'
        call, = reply[-1]['tool_calls']
        assert call['id'] == 'first' and call['arguments'] == {'q':'one'}
        assert 'private_opaque' in json.dumps(reply[-1]['replay'])
        assert 'private_opaque' not in json.dumps(events)
        assert not any(event['type']=='delta' for event in reply)
        *_, plain = model.build_request(config, 'key', 'system',
                                        [{'role':'assistant','content':'earlier'}], 'new')
        if fmt == 'gemini':
            assert plain['contents'] == [{'role':'model','parts':[{'text':'earlier'}]},
                                         {'role':'user','parts':[{'text':'new'}]}]
        else:
            assert plain['messages'][-2:] == [{'role':'assistant','content':'earlier'},
                                              {'role':'user','content':'new'}]
    asyncio.run(exercise())



@pytest.mark.parametrize(('fmt', 'payload'), [
    ('openai', {'choices':[{'finish_reason':'content_filter','message':{'content':'unfinished'}}],
                'usage':{'prompt_tokens':4,'completion_tokens':2}}),
    ('anthropic', {'stop_reason':'refusal','content':[{'type':'text','text':'unfinished'}],
                   'usage':{'input_tokens':4,'output_tokens':2}}),
    ('gemini', {'candidates':[{'finishReason':'MALFORMED_FUNCTION_CALL','content':{'parts':[{'text':'unfinished'}]}}],
                'usageMetadata':{'promptTokenCount':4,'candidatesTokenCount':2}}),
])
def test_native_tool_rejected_terminal_is_not_a_final_answer(fmt, payload):
    from avatar.models.client import parse_response

    with pytest.raises(HTTPException) as failure:
        parse_response(fmt, payload, config={'_react_tool_calls':True})
    assert failure.value.stop_reason == 'error'
    assert failure.value.model_usage['total_tokens'] == 6
    assert 'unfinished' not in failure.value.detail


def test_native_tool_history_budget_counts_arguments_and_replay():
    from avatar.models.limits import check_context
    config = {'api_format':'openai', 'max_tokens':128, 'context_tokens':1200}
    history = [{'role':'assistant','content':'','tool_calls':[{'id':'one','name':'lookup','arguments':{'q':'x'*1200}}],
                'replay':{'reasoning_content':'x'*1200}}]
    with pytest.raises(HTTPException) as failure:
        check_context(config, 'system', history, None)
    assert failure.value.stop_reason == 'context_overflow'
    assert check_context(config, 'system', [{'role':'user','content':'plain'}], 'follow-up') > 0


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


@pytest.mark.parametrize('corrupt',[False,True])
def test_legacy_snapshot_migration_is_idempotent_and_unlocks(legacy_sqlite,postgres_url,tmp_path,corrupt):
    from contextlib import closing
    import json
    import sqlite3
    from avatar.conversation.jobs.store import GenerationStore
    from avatar.core.db import Database
    from scripts.migrations.chat_generations import restore_legacy_generations
    from scripts.migrate_sqlite_to_postgres import migrate

    snapshot='invalid JSON' if corrupt else json.dumps({'request_id':'legacy','done':False,'stage':'accepted','error':''})
    with closing(sqlite3.connect(legacy_sqlite)) as source, source:
        if corrupt:
            source.execute('DROP TRIGGER chat_generation_forget')
            source.execute('DROP TABLE chat_generation_jobs')
        source.execute("INSERT INTO users(id,username,password,role,created_at) VALUES('old','old','x','user','2026')")
        source.execute("""INSERT INTO chat_conversations
            (id,user_id,channel,target_aic,caller_id,platform,title,created_at,updated_at,lease_until)
            VALUES('chat','old','assistant','','','','old','2026','2026',1000)""")
        source.execute("INSERT INTO chat_pending VALUES('chat','old',?,1)", (snapshot,))
    migrate(legacy_sqlite,tmp_path/'snapshot.sqlite3',postgres_url)
    expected = 'failed' if corrupt else 'indeterminate'
    with closing(Database(tmp_path/'target',postgres_url, schema=install_schema)) as database:
        with database.connect(write=True) as db:
            states = db.execute("SELECT state FROM chat_generation_jobs WHERE conversation_id='chat'").fetchall()
            assert [row[0] for row in states] == [expected]
            assert db.execute("SELECT lease_until FROM chat_conversations WHERE id='chat'").fetchone()[0] == 0
            # An earlier PostgreSQL import copied only the legacy snapshot.
            db.execute("DELETE FROM chat_generation_jobs WHERE conversation_id='chat'")
            db.execute("UPDATE chat_conversations SET lease_until=1000 WHERE id='chat'")
        database.initialize()
        store = GenerationStore(database)
        assert store.latest('old','chat') is None
        with database.connect(write=True) as db:
            restore_legacy_generations(db)
        assert store.latest('old','chat')['state'] == expected
        with database.connect(write=True) as db:
            assert db.execute("SELECT lease_until FROM chat_conversations WHERE id='chat'").fetchone()[0] == 0
            db.execute("UPDATE chat_generation_jobs SET state='running' WHERE conversation_id='chat'")
            db.execute("UPDATE chat_conversations SET lease_until=2000 WHERE id='chat'")
        database.initialize()
        with database.connect() as db:
            states = db.execute("SELECT state FROM chat_generation_jobs WHERE conversation_id='chat'").fetchall()
            assert [row[0] for row in states] == ['running']
            assert db.execute("SELECT lease_until FROM chat_conversations WHERE id='chat'").fetchone()[0] == 2000


def test_real_runtime_releases_session_before_title_work(context,monkeypatch):
    import asyncio
    from avatar.conversation.jobs.runner import GenerationRunner
    from avatar.conversation.jobs.store import GenerationStore
    import avatar.conversation.jobs.titles as titles
    app,client,_=context;user=client.get('/api/auth/me').json()['user']
    cid=client.post('/api/conversations',json={'channel':'assistant'}).json()['id']
    store=GenerationStore(app.state.database);store.submit(user,request(cid,'real'))
    observed=[]
    async def title(chat,uid,conversation_id,*args):
        with store.database.connect() as db:
            observed.append(db.execute('SELECT lease_until FROM chat_conversations WHERE id=?',(conversation_id,)).fetchone()[0])
        assert store.get(uid,'real')['done']
    monkeypatch.setattr(titles,'generate_title',title)
    async def check():
        runner=GenerationRunner(app.state.runtime,app.state.database)
        await runner.execute(store.claim()[0])
    asyncio.run(check())
    assert observed==[0]
    assert store.get(user['id'],'real')['done']


def test_peer_protocol_accepts_before_generation_and_deduplicates(context):
    import asyncio
    from types import SimpleNamespace
    from avatar.protocols.tasks.dialogues import PeerDialogueTasks
    app,_,_=context;calls=[]
    async def check():
        gate=asyncio.Event()
        async def answer(*args):
            calls.append(1);await gate.wait()
            return SimpleNamespace(text='peer answer',usage=None,task_action=None)
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


def test_react_single_streaming_answer():
    from avatar.conversation.agent_loop import run_agent

    calls, deltas = [], []

    async def stream(config, key, system, history, message):
        calls.append(config)
        yield {'type':'delta','text':'TCP 提供可靠且有序的传输。'}
        yield {'type':'usage','usage':{'total_tokens':20},'finish_reason':'stop'}

    rag = SimpleNamespace(chat=SimpleNamespace(model_client=SimpleNamespace(stream=stream)), validate=lambda _: None)
    config = {'provider':'custom','model':'test','api_format':'openai','context_tokens':32768,'max_tokens':1024}
    result = asyncio.run(run_agent(rag, (config,'key','测试分身',[]), '用一句话介绍 TCP。',
                                   progress=lambda event:None,on_delta=deltas.append))
    assert result.text == 'TCP 提供可靠且有序的传输。'
    assert deltas == [result.text]
    assert result.usage['total_tokens'] == 20
    assert len(calls)==1
    assert {'ask_user','get_current_time'} <= {tool['name'] for tool in calls[0]['_tools']}


def test_react_ask_user_resumes_by_id_and_continues_truncated_reply():
    from avatar.conversation.agent_loop import run_agent
    from avatar.conversation.loop.store import MemoryRunStore
    from avatar.core.execution import AgentNeedsInput

    store, requests = MemoryRunStore(), []

    async def stream(config, key, system, history, message):
        requests.append(history)
        if len(requests)==1:
            yield {'type':'usage','usage':None,'finish_reason':'tool_calls','tool_calls':[
                {'id':'call_ask','name':'ask_user','arguments':{'question':'几点开始？','options':['八点','九点']}}]}
        else:
            assert any(row['role']=='tool' and row['tool_call_id']=='call_ask' for row in history)
            assert any('九点' in row['content'] for row in history if row['role']=='user')
            text, reason = ('课程从','length') if len(requests)==2 else ('九点开始。','stop')
            yield {'type':'delta','text':text}
            yield {'type':'usage','usage':None,'finish_reason':reason}

    async def run():
        rag = SimpleNamespace(chat=SimpleNamespace(model_client=SimpleNamespace(stream=stream)), validate=lambda _:None)
        config = {'context_tokens':32768,'max_tokens':1024,'_run_store':store}
        context = (config,'key','测试分身',[])
        with pytest.raises(AgentNeedsInput) as first:
            await run_agent(rag,context,'安排课程。')
        question = first.value.user_question
        state = store.load()
        assert state['schema_version']==2 and state['phase']=='paused' and state['pending_batch'] is None
        config['_auto_recover'] = True
        with pytest.raises(AgentNeedsInput) as recovered:
            await run_agent(rag,context,'安排课程。')
        assert recovered.value.user_question==question and len(requests)==1
        config.pop('_auto_recover')
        config['_answered_question'] = {**question,'answer':'九点'}
        deltas=[]
        answer = await run_agent(rag,context,'安排课程。',progress=lambda _:None,on_delta=deltas.append)
        assert answer.text==''.join(deltas)=='课程从九点开始。'
        assert (await run_agent(rag,context,'安排课程。')).text==answer.text and len(requests)==3
    asyncio.run(run())


@pytest.mark.parametrize('scenario', ['batch', 'unavailable', 'read_then_create', 'duplicate'])
def test_react_executes_native_batches_and_returns_each_receipt(scenario):
    from avatar.conversation.agent_loop import run_agent
    from avatar.conversation.loop.entry_tools import SessionFileRequest
    from tests.support import fake_dns

    text = '<!DOCTYPE html><html><body><main>测试页面</main></body></html>'
    definition = {'name':'read_session_file','description':'读取当前会话原件',
                  'parameters':SessionFileRequest.model_json_schema()}
    requests, executed = [], []
    name = 'unknown_tool' if scenario=='unavailable' else 'read_session_file'

    def tool(id, name, arguments):
        return {'id':id,'type':'function','function':{'name':name,'arguments':json.dumps(arguments)}}

    def upstream(request):
        body = json.loads(request.content)
        declared = {t['function']['name'] for t in body['tools']}
        assert 'read_session_file' in declared
        assert not {'plan_task','update_task','request_tools'} & declared
        requests.append(body)
        step = len(requests)
        if step == 1:
            calls = [tool('call_read',name,{'file_id':'original'})]
            if scenario == 'batch':
                calls.append(tool('call_time','get_current_time',{}))
        elif step == 2 and scenario in ('read_then_create','duplicate'):
            assert body['messages'][-1]['tool_call_id']=='call_read'
            calls = [tool('call_follow','create_html',{'filename':'updated.html','html':text})] if scenario=='read_then_create' else [tool('call_follow',name,{'file_id':'original'})]
        else:
            messages = [m for m in body['messages'] if m['role']=='tool']
            assert [m['tool_call_id'] for m in messages] == ['call_read'] + (
                ['call_time'] if scenario=='batch' else ['call_follow'] if scenario in ('duplicate','read_then_create') else [])
            if scenario in ('unavailable','duplicate'):
                assert json.loads(messages[-1]['content'])['error']
            calls = []
        deltas = [{'delta':{'tool_calls':[{**c,'index':i} for i,c in enumerate(calls)]},'finish_reason':None}] if calls else [{'delta':{'content':'已处理。'},'finish_reason':None}]
        deltas.append({'delta':{},'finish_reason':'tool_calls' if calls else 'stop'})
        events = ''.join('data: '+json.dumps({'choices':[delta]})+'\n\n' for delta in deltas)
        return httpx.Response(200,content=(events+'data: [DONE]\n\n').encode(),
                              headers={'content-type':'text/event-stream'})

    async def execute(tool_name, args):
        assert args['file_id'] == 'original'
        executed.append(tool_name)
        return json.dumps({'kind':'file','has_content':True,'text':text})

    model = ModelClient(httpx.MockTransport(upstream),resolver=fake_dns)
    rag = SimpleNamespace(chat=SimpleNamespace(model_client=model),validate=lambda _:None)
    config = {'provider':'custom','model':'test','api_format':'openai','base_url':'https://model.example/v1',
              'context_tokens':32768,'max_tokens':1024,'_entry_tools':[definition],
              '_execute_entry_tool':execute,
              '_html_enabled':scenario=='read_then_create'}
    result = asyncio.run(run_agent(rag,(config,'key','测试分身',[]),'处理本轮请求。'))
    assert result.text == '已处理。'
    assert executed == ([] if scenario=='unavailable' else ['read_session_file'])
    if scenario=='read_then_create':
        assert [item['filename'] for item in config['_generated_files']] == ['updated.html']


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
    async def rag(uid,aic,session,files,context,*args,**kwargs):return context
    async def execute(context, enrichment, model, **kwargs):
        captured.update(enrichment.inputs)
        prefix, data = context[2].split('\n',1)
        evidence = {**json.loads(data), 'skill_execution_results':[{'status':'success'}]}
        return context[0],context[1],prefix+'\n'+json.dumps(evidence),context[3]
    monkeypatch.setattr(chat.memory,'prepare',memory)
    monkeypatch.setattr(chat.memory,'needs_compression',lambda *args:False)
    monkeypatch.setattr(chat.skills,'load_context',skills)
    monkeypatch.setattr(chat.rag,'enrich',rag)
    monkeypatch.setattr(chat.skill_execution,'enrich',execute)
    overview={'agent':{'user_id':user['id'],'aic':'aic','last_modified_at':''},'_private_context':True}
    session={'id':cid,'parent_id':None}
    context=({'context_tokens':32768,'max_tokens':16},'key','system\n'+json.dumps({'available_skills':[]}),[])
    async def use_sandbox():
        prepared=await chat.prepare(user,request(cid,'inputs'),overview,context,session)
        assert not captured
        await prepared[0]['_execute_entry_tool']('python',{'query':'读取可用输入'})
    with generation_scope(job):asyncio.run(use_sandbox())
    assert original in captured and late not in captured
    assert captured[original]['read']()==b'original'
    with store.database.connect(write=True) as db:db.execute('DELETE FROM attachments WHERE id=?',(original,))
    with pytest.raises(HTTPException):captured[original]['read']()


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
        config = {}
        bind_public_material_tools(app.state.chat, config, library, owner, store, state,
                                   workspace=run.workspace, task_id=task_id)
        return store, config

    store, _ = bind()
    store.merge({
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
        rowid = PersonalTaskService.task_row(db, scope)['rowid']

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
                    db.execute('UPDATE avatar_aip_tasks SET document=? WHERE rowid=?', (dump(document), rowid))
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
        observed = [row for row in history if row['role'] == 'tool' and row.get('name') == 'contact_avatar']
        if observed:
            assert observed[-1]['tool_call_id'] == 'contact'
            assert observed[-1]['is_error'] and '不可用的文件' in observed[-1]['content']
            yield {'type': 'delta', 'text': '指定附件不可用。'}
            yield {'type': 'usage', 'usage': {}, 'finish_reason': 'stop'}
            return
        assert 'contact_avatar' in {tool['name'] for tool in config['_tools']}
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


@pytest.mark.parametrize('legacy', [True, False])
def test_sqlite_snapshot_to_postgres_preserves_replay_contract(legacy_sqlite, tmp_path, postgres_url, legacy):
    import hashlib
    import sqlite3
    from fastapi.testclient import TestClient
    from avatar.api.app import create_app
    from avatar.core.config import Settings
    from scripts.migrate_sqlite_to_postgres import migrate

    task, stamp = 'historical-task', '2026-01-01T00:00:00+00:00'
    body = {'request_id':'before-upgrade','target_aic':'admin','objective':'keep'}
    with sqlite3.connect(legacy_sqlite) as db:
        db.execute('PRAGMA foreign_keys=ON')
        for name, role in [('admin','admin'), ('tester','user')]:
            db.execute('INSERT INTO users(id,username,password,role,created_at,is_root) VALUES(?,?,?,?,?,?)',
                       (name,name,'unused',role,stamp,int(role == 'admin')))
            db.execute('INSERT INTO avatar_records(user_id,aic,profile,certificate,last_modified_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                       (name,name,'{}','{}',stamp,stamp,stamp))
        token = 'historical-session'
        db.execute('INSERT INTO sessions(token,user_id,csrf,expires) VALUES(?,?,?,?)',
                   (hashlib.sha256(token.encode()).hexdigest(),'tester','historical-csrf',4102444800))
        db.execute('INSERT INTO agent_peer_sessions(id,left_user_id,right_user_id,creator_id,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                   ('session','admin','tester','tester',stamp,stamp))
        db.execute('''INSERT INTO agent_dialogues(id,user_id,own_aic,target_user_id,target_aic,objective,
                   request_id,created_at,updated_at,status,session_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                   (task,'tester','tester','admin','admin',body['objective'],body['request_id'],stamp,stamp,'awaiting_acceptance','session'))
        for name in ('admin','tester'):
            db.execute('INSERT INTO agent_session_members(session_id,user_id) VALUES(?,?)',('session',name))
            db.execute('INSERT INTO agent_task_members(task_id,user_id) VALUES(?,?)',(task,name))
        for rowid in (31, 76):
            db.execute('''INSERT INTO agent_wire_results(rowid,command_id,local_aic,peer_aic,session_id,task_id,state,document)
                          VALUES(?,?,'tester','admin','session',?,'completed','{}')''', (rowid,str(rowid),task))
        if legacy:
            db.execute('DROP TABLE agent_request_receipts')
            db.execute('DROP TABLE agent_wire_receipts')
        else:
            # The v1.1.0 serializer included request defaults in the hash.
            payload = b'{"file_ids":[],"kind":"chat","max_rounds":null,"objective":"keep","target_aic":"admin"}'
            db.execute('INSERT INTO agent_request_receipts(user_id,scope,request_id,fingerprint,task_id) VALUES(?,?,?,?,?)',
                       ('tester','create',body['request_id'],hashlib.sha256(payload).hexdigest(),task))
            db.execute("INSERT INTO agent_wire_receipts VALUES('self','peer','control','fingerprint','{}')")
    migrate(legacy_sqlite, tmp_path / 'snapshot #1.sqlite3', postgres_url)
    with sqlite3.connect(legacy_sqlite) as db:
        assert bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_request_receipts'").fetchone()) != legacy
    target = tmp_path / 'target'
    path = '/api/agent-dialogues'
    pg_app = create_app(Settings(data_dir=target, database_url=postgres_url), run_worker=False)
    with pg_app.state.database.connect(write=True) as db:
        db.execute("INSERT INTO agent_wire_results VALUES('new','tester','admin','session',?,'completed','{}')", (task,))
        assert [row[0] for row in db.execute('SELECT rowid FROM agent_wire_results ORDER BY rowid')] == [31,76,77]
        count = db.execute("SELECT count(*) FROM agent_wire_receipts WHERE command_id='control'").fetchone()[0]
        assert count == (0 if legacy else 1)
    with TestClient(pg_app) as client:
        client.cookies.set('avatar_session', token)
        client.headers['X-CSRF-Token'] = 'historical-csrf'
        replay = client.post(path, json=body)
        if legacy:
            assert replay.status_code == 409
        else:
            assert replay.status_code == 202 and replay.json()['id'] == task
        new = {**body, 'request_id':'after-upgrade'}
        created = client.post(path, json=new)
        assert created.status_code == 202
        assert client.post(path, json=new).json() == created.json()
        assert client.post(path, json={**new,'max_rounds':5}).status_code == 409


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


# skill execution

# 模块说明：技能执行图回归测试；重复动作必须立即终止循环，不能空转到 LangGraph 递归上限（GraphRecursionError 会让沙箱结果丢失且无最终回答）。


class FakeResponse:
    def __init__(self,text):
        self.text=text
        self.usage={'input_tokens':10,'output_tokens':5}


class FakeModel:
    # 职责：按脚本返回规划 JSON；列表耗尽后重复最后一项，模拟模型执行成功后仍复读同一动作。
    def __init__(self,actions):
        self.actions=list(actions)
        self.calls=0
    async def generate(self,*args,**kwargs):
        action=self.actions[min(self.calls,len(self.actions)-1)]
        self.calls+=1
        return FakeResponse(json.dumps(action,ensure_ascii=False))


def build_context():
    background={'installed_skills':[]}
    system='PREFIX\n'+json.dumps(background,ensure_ascii=False)+'\nSUFFIX'
    config={'provider':'deepseek','model':'deepseek-v4-flash','max_tokens':1024}
    return config,'test-key',system,[]


def make_execution(context):
    app,user,_=context
    execution=SkillExecution(app.state.chat.skills)
    async def fake_execute(user_id,resources,script,args,**kwargs):
        return {'status':'success','stdout':['1986569672'],'stderr':[],'error':'','files':[]}
    execution.sandbox.execute=fake_execute
    return execution,user.get('/api/auth/me').json()['user']['id']


def background_of(merged_system):
    value,_=json.JSONDecoder().raw_decode(merged_system.split('\n',1)[1])
    return value


def test_repeated_python_action_terminates_and_keeps_result(context):
    # 复现线上故障：模型两次返回同一 python 动作，第二次命中重复守卫后必须结束并保留已成功的沙箱结果。
    execution,user_id=make_execution(context)
    model=FakeModel([{'action':'python','code':'print(17348 * 114514)','input_ids':[]}])
    config,key,system,history=build_context()
    merged,_,merged_system,_=asyncio.run(execution.enrich((config,key,system,history),SkillEnrichRequest(user_id,'调用sandbox计算17348×114514','c1',allow_code=True),model))
    assert model.calls==2
    results=background_of(merged_system)['skill_execution_results']
    assert len(results)==1 and results[0]['status']=='success'
    assert results[0]['stdout']=='1986569672'
    assert '不得只复述脚本代码' in background_of(merged_system)['skill_execution_rules']
    assert merged['_generated_files']==[]
    assert not any('未成功' in notice or '未完成' in notice for notice in merged['_workflow_notices'])


def test_distinct_actions_stop_at_four_run_cap(context):
    execution,user_id=make_execution(context)
    actions=[{'action':'python','code':'print('+str(n)+')','input_ids':[]} for n in range(6)]
    model=FakeModel(actions)
    config,key,system,history=build_context()
    merged,_,_,_=asyncio.run(execution.enrich((config,key,system,history),SkillEnrichRequest(user_id,'多步计算','c1',allow_code=True),model))
    assert model.calls==4
    assert any('已运行四次' in notice for notice in merged['_workflow_notices'])


def test_finish_action_never_executes(context):
    execution,user_id=make_execution(context)
    model=FakeModel([{'action':'finish'}])
    config,key,system,history=build_context()
    _,_,merged_system,_=asyncio.run(execution.enrich((config,key,system,history),SkillEnrichRequest(user_id,'普通聊天','c1',allow_code=True),model))
    assert model.calls==1 and background_of(merged_system)['skill_execution_results']==[]


# Keep the offline Deno command and permissions stable across Python dependency updates

def test_offline_command_with_legacy_ready_and_no_python_wrapper(tmp_path, monkeypatch):
    module = Path(__file__).resolve().parents[1] / 'avatar/skills/sandbox.py'
    monkeypatch.setitem(sys.modules, 'langchain_sandbox', None)
    monkeypatch.setattr(sys, 'prefix', str(tmp_path))
    sandbox = runpy.run_path(str(module))['LocalPythonSandbox']()
    runtime = tmp_path / 'share/avatar-sandbox'
    cache = runtime / 'cache'
    cache.mkdir(parents=True)
    (runtime / 'ready').write_text('langchain-sandbox=0.0.6; pyodide=0.27.8\n')
    observed = []

    async def capture(*command, cwd, env, stdout, stderr, start_new_session):
        binary_dir = tmp_path / 'bin'
        source = Path(cwd) / 'request.py'
        assert command == (
            '/usr/bin/prlimit', '--cpu=60', '--', str(binary_dir / 'deno'), 'run',
            '--no-prompt', '--cached-only', '--no-lock', '--deny-net', '--deny-env',
            '--deny-run', '--deny-ffi', '--deny-write', '--deny-import',
            f'--allow-read={cache}/npm/registry.npmjs.org/pyodide/0.27.8,{source}',
            '--node-modules-dir=none', '--v8-flags=--max-old-space-size=512',
            str(module.with_name('runtime.ts')), str(source),
        )
        assert env == {'DENO_DIR': str(Path(cwd) / 'cache'), 'PATH': f'{binary_dir}:/usr/bin:/bin', 'HOME': cwd}
        assert stdout == stderr == asyncio.subprocess.PIPE
        assert start_new_session is True
        assert source.read_text() == 'print(42)'
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
        observed.append(source)
        raise OSError('test launch')

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', capture)
    with pytest.raises(OSError, match='test launch'):
        asyncio.run(sandbox.run('print(42)', 5))
    assert len(observed) == 1 and not observed[0].parent.exists()


def test_http_model_addresses_allow_changing_ips_without_allowlist(monkeypatch):
    from avatar.models.schemas import ModelConfigInput
    endpoint='http://10.0.0.21:5000/v1'
    assert ModelConfigInput(provider='custom',api_format='openai',base_url=endpoint,model='qwen3.8',api_key='local').base_url==endpoint
    async def check():
        client=ModelClient(transport=httpx.MockTransport(lambda request:httpx.Response(200)))
        for address in ['10.0.0.21','10.0.0.22','192.168.1.9','8.8.8.8']:
            ip,_=await client.connection(httpx.URL('http://'+address+':5000/v1/models'))
            assert ip==address
        import socket
        monkeypatch.setattr(socket,'getaddrinfo',lambda *args,**kwargs:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('10.0.0.22',5000))])
        assert (await client.connection(httpx.URL('http://lab-model:5000/v1')))[0]=='10.0.0.22'
        for address in ['127.0.0.1','169.254.169.254','0.0.0.0','224.0.0.1','[::1]','[::ffff:127.0.0.1]']:
            with pytest.raises(HTTPException):await client.connection(httpx.URL('http://'+address+'/v1'))
        monkeypatch.setattr(socket,'getaddrinfo',lambda *args,**kwargs:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',5000))])
        with pytest.raises(HTTPException):await client.connection(httpx.URL('http://lab-model:5000/v1'))
    asyncio.run(check())


@pytest.mark.parametrize('disconnect',[True,False])
def test_chat_generation_survives_refresh_and_bounded_queue(context,disconnect):
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
        response=chat_response(Chat(),actor,body,app.state.database)
        iterator=response.body_iterator
        try:
            assert 'queued' in await anext(iterator)
            if disconnect:await iterator.aclose()
            resume.set()
            if not disconnect:
                async with asyncio.timeout(5):
                    tail = [chunk async for chunk in iterator]
                assert any(chunk.startswith('event: done') for chunk in tail)
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


def test_model_direct_first_fallback_never_replays_submitted_requests():
    from avatar.models.model_network import DirectFirstTransport
    import ssl
    async def check():
        for failure in [None,httpx.ConnectTimeout('connect'),httpx.ReadTimeout('read'),httpx.WriteError('write'),httpx.RemoteProtocolError('protocol'),httpx.ConnectError('certificate verify failed')]:
            calls=[]
            async def direct(request):
                calls.append('direct')
                if failure:raise failure
                return httpx.Response(502)
            async def proxy(request):calls.append('proxy');return httpx.Response(200)
            transport=DirectFirstTransport.__new__(DirectFirstTransport)
            transport.direct=httpx.MockTransport(direct);transport.fallback=httpx.MockTransport(proxy)
            async with httpx.AsyncClient(transport=transport) as client:
                if failure and not isinstance(failure,httpx.ConnectTimeout):
                    with pytest.raises(type(failure)):await client.post('http://10.0.0.22:5000/v1/chat/completions',json={'model':'test'})
                else:
                    result=await client.post('http://10.0.0.22:5000/v1/chat/completions',json={'model':'test'})
                    assert result.status_code==(200 if failure else 502)
            assert calls==(['direct','proxy'] if isinstance(failure,httpx.ConnectTimeout) else ['direct'])
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


def test_unknown_capabilities_allow_correction_but_metadata_locks(context):
    from avatar.models.capabilities import automatic,remember
    app,user,_=context
    uid=user.get('/api/auth/me').json()['user']['id']
    config={'provider':'custom','api_format':'openai','base_url':'https://gateway.example/v1','model':'custom-vision','vision_enabled':True,'audio_enabled':False}
    with app.state.database.connect() as db:
        corrected=automatic(db,uid,config)
        assert corrected['vision_enabled'] and corrected['capability_status']=='unknown'
        assert automatic(db,uid,corrected)['vision_enabled']
        assert automatic(db,uid,{**config,'multimodal_enabled':True},embedding=True)['multimodal_enabled']
    remember(app.state.database,uid,config,[{'id':'custom-vision','vision':False,'audio':False,'input_modalities':['text'],'source':'api','category':'text','chat_compatible':True}],on_change=app.state.service.models.on_change)
    with app.state.database.connect() as db:
        locked=automatic(db,uid,config)
        assert not locked['vision_enabled'] and locked['capability_status']=='confirmed'
    remember(app.state.database,uid,config,[{'id':'custom-vision','vision':True,'audio':False,'input_modalities':['text','image'],'source':'api','category':'multimodal','chat_compatible':True}],on_change=app.state.service.models.on_change)
    with app.state.database.connect() as db:assert automatic(db,uid,{**config,'vision_enabled':False})['vision_enabled']
    response=user.put('/api/me/model-settings',json={'model_config_input':{'provider':'custom','base_url':'https://gateway.example/v1','model':'manual-only','api_key':'local','vision_enabled':True}})
    assert response.status_code==200,response.text
    saved=user.get('/api/me/model-settings').json()['model_config']
    assert saved['vision_enabled'] and saved['capability_status']=='unknown'


def test_preset_provider_addresses_cannot_be_overridden(context):
    from avatar.models.model_config import PROVIDERS,provider_base_url
    from fastapi import HTTPException
    for provider,(_,_,url) in PROVIDERS.items():
        if provider=='custom':continue
        assert provider_base_url(provider,'')==url
        assert provider_base_url(provider,url+'/')==url
        with pytest.raises(HTTPException):provider_base_url(provider,'https://different.example/v1')
    assert provider_base_url('custom','https://gateway.example/v1/')=='https://gateway.example/v1'
    _,user,_=context
    old=user.get('/api/me/embedding-settings').json()
    result=user.put('/api/me/embedding-settings',json={'provider':'siliconflow','base_url':'https://different.example/v1','api_key':'local','model':'test','dimensions':64,'updated_at':old['updated_at']})
    assert result.status_code==422
    assert user.get('/api/me/embedding-settings').json()==old
    result=user.post('/api/me/model-options',json={'provider':'deepseek','base_url':'https://different.example/v1','api_key':'local'})
    assert result.status_code==422


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


def test_native_html_draft_survives_truncation_and_writer_change(pg_database):
    from hashlib import sha256

    from avatar.conversation.loop.drafts import HtmlDrafts
    from avatar.conversation.loop.store import RunConflict, RunStore
    from avatar.conversation.tools.html import CREATE_HTML
    from tests.support import fake_dns

    stamp = now()
    with pg_database.connect(write=True) as db:
        db.execute("INSERT INTO users(id,username,password,role,created_at) VALUES('draft-owner','draft-owner','x','user',?)", (stamp,))
        db.execute("""INSERT INTO chat_conversations(id,user_id,channel,target_aic,caller_id,platform,title,created_at,updated_at)
            VALUES('draft-chat','draft-owner','personal','avatar','draft-owner','web','drafts',?,?)""", (stamp, stamp))

    source = '<main>起点' + '🕰️继续' * 80
    prefix = '{"filename":"timeline.html","html":"' + source + r'\ud83d'
    frames = [
        {'choices':[{'delta':{'tool_calls':[{'index':0,'id':'html-start',
            'function':{'name':'create_html','arguments':prefix}}]},'finish_reason':None}]},
        {'choices':[{'delta':{},'finish_reason':'length'}]},
    ]
    stream = ''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames)
    model = ModelClient(httpx.MockTransport(lambda _: httpx.Response(
        200, headers={'content-type':'text/event-stream'}, text=stream)), resolver=fake_dns)
    config = {'api_format':'openai','provider':'custom','model':'mock','base_url':'https://example.test',
              'max_tokens':512,'_tools':[CREATE_HTML],'_react_tool_calls':True,'_optional_tools':True}

    async def exercise():
        run = ('draft-owner', 'chat', 'draft-chat', 'draft-run')
        origin = RunStore(pg_database, *run, 'writer-1')
        state = origin.acquire()
        patch = {'schema_version':2, 'step':1, 'trajectory':[]}
        origin.merge(patch)
        state.update(patch)
        events = [event async for event in model.stream(config, 'key', 'system', [], 'create HTML')]
        clipped, = events[-1]['partial_tool_calls']
        outputs = [{'id':'python-result','filename':'result.txt','data':b'result'}]
        draft = await HtmlDrafts(origin, state, outputs).capture(clipped)
        assert draft['offset'] == len(source.encode()) and draft['tail_preview'] == source[-300:]

        new = RunStore(pg_database, *run, 'writer-2')
        restored = new.acquire()
        with pytest.raises(RunConflict):
            origin.check()
        assert restored['trajectory'] == []
        drafts = HtmlDrafts(new, restored, outputs)
        [resume] = await drafts.resume_receipts()
        assert resume == {**draft, 'status':'draft_resumed'}
        arguments = {'draft_id':resume['draft_id'], 'offset':resume['offset'], 'chunk':'下一段'}
        with pytest.raises(ValueError, match=str(resume['offset'])):
            await drafts.capture({'name':'append_html_draft','id':'stale',
                                  'arguments':{**arguments,'offset':0}})
        next_prefix = json.dumps(arguments, ensure_ascii=False)[:-1] + ',"do'
        appended = await drafts.capture({'name':'append_html_draft','id':'next','arguments':next_prefix})
        typed_prefix = [
            {'args':{'draft_id':resume['draft_id']}},
            {'partialArgs':[{'jsonPath':'$.chunk','stringValue':'终点','willContinue':True}]},
            {'partialArgs':[{'jsonPath':'$.offset','numberValue':float(appended['offset'])},
                            {'jsonPath':'$.chunk'}]},
        ]
        typed = await drafts.capture({'name':'append_html_draft','id':'gemini',
            'argument_format':'gemini_partial_args','arguments':{'chunks':typed_prefix}})
        page = await drafts.append({**arguments, 'offset':typed['offset'], 'chunk':'</main>', 'done':True})
        [receipt] = new.load()['files']
        assert [item['id'] for item in outputs] == ['python-result', page['id']]
        data = (source + '下一段终点</main>').encode()
        assert outputs[1]['data'] == data and new.read(receipt['draft']).encode() == data
        assert receipt['sha256'] == sha256(data).hexdigest() and not drafts.active

    asyncio.run(exercise())




def test_react_checkpoint_rehydrates_legacy_artifacts_and_fences_receipt_writer(pg_database):
    from hashlib import sha256

    from avatar.conversation.loop.context import ReActContextManager
    from avatar.conversation.loop.runtime import LoopState, load_checkpoint, restore_files, restore_materials, resume_safe
    from avatar.conversation.loop.store import RunConflict, RunStore
    from avatar.models.client import ModelResult

    with pg_database.connect(write=True) as db:
        stamp = now()
        db.execute("INSERT INTO users(id,username,password,role,created_at) VALUES('receipt-owner','receipt-owner','unused','user',?)", (stamp,))
        db.execute("""INSERT INTO chat_conversations(id,user_id,channel,target_aic,caller_id,platform,title,created_at,updated_at)
            VALUES('receipt-chat','receipt-owner','personal','receipt-avatar','receipt-owner','web','test',?,?)""", (stamp, stamp))
    key = (pg_database, 'receipt-owner', 'chat', 'receipt-chat', 'receipt-run')
    first = RunStore(*key, 'old-writer')
    first.acquire()
    html = '<!doctype html><main>旧作品🙂</main>'
    content = html.encode()
    artifact = {'id':'old-artifact', 'filename':'old.html', 'html_mode':'html', 'draft':'artifact-old',
                'size':len(content), 'sha256':sha256(content).hexdigest()}
    proofs = {'sources':[{'file_id':'source-1'}], 'revisions':[[['profile','source-1'],'source-revision']]}
    first.append('artifact-old', 0, html, patch={
        'phase':'retry', 'files':[artifact], 'sources':{'revision':'v1'}, 'plan':{'steps':[]},
        'observations':['已经制作旧作品'], 'public_visible':['former-candidate'], 'draft':{'name':'discard-me'},
        'entry_material':proofs, 'session_file_reads':{'session-file':{'read_content':True}},
        'session_image_ids':['session-file'], 'material':[{'id':'only-prepared'}],
    })

    first.append('incomplete-legacy-draft', 0, 'unused data')

    async def run():
        checkpoint = await load_checkpoint(first)
        assert checkpoint['schema_version'] == 2 and checkpoint['trajectory'] == [] and checkpoint['step'] == 0
        assert checkpoint['sources'] == {'revision':'v1'} and '已经制作旧作品' in checkpoint['legacy_summary']
        assert not {'public_visible','draft','plan','observations','session_image_ids','material'} & checkpoint.keys()
        assert checkpoint['entry_material'] == proofs and 'session-file' in checkpoint['session_file_reads']
        assert first.size('incomplete-legacy-draft') == 0
        assert resume_safe(checkpoint) and not resume_safe(checkpoint, automatic=True)
        config = {}
        await restore_files(first, checkpoint, config)
        assert config['_generated_files'][0]['data'] == content

        manager = ReActContextManager([], '继续', store=first, checkpoint=checkpoint)
        evidence = '开头' + '👩测试' * 30000 + '结尾'
        preview, number = await manager.archive(evidence)
        assert '开头' in preview and '结尾' in preview

        second = RunStore(*key, 'new-writer')
        fresh = second.acquire()
        with pytest.raises(RunConflict):
            first.archive('stale', b'evidence', patch={})
        manager = ReActContextManager([], '继续', store=second, checkpoint=fresh)
        pieces = []
        offset = 0
        while offset is not None:
            page = await manager.read_receipt({'receipt':number, 'offset':offset})
            pieces.append(page['text'])
            offset = page['next_offset']
        assert ''.join(pieces) == evidence

        result = ModelResult('', {'total_tokens':5}, ({'id':'finish','name':'end_task','arguments':{}},), 'tool_calls')
        await LoopState(second, fresh).answer(result)
        assert await LoopState(second, second.load()).restore_answer(None) == result
        second.merge({'usage':None})
        assert (await LoopState(second, second.load()).restore_answer(None)).usage is None
        delivery = SimpleNamespace(visible_ids=set(), prepared=[], library=SimpleNamespace(prepare=lambda *_args,**_kwargs: {}))
        with pytest.raises(HTTPException) as denied:
            await restore_materials(delivery, [{'id':'forged','relative_path':'file.txt','version':[]}])
        assert denied.value.status_code == 409 and not delivery.prepared

    asyncio.run(run())


def test_react_context_preserves_call_result_pairs_when_compacting():
    from avatar.conversation.loop.context import ReActContextManager

    async def run():
        manager = ReActContextManager([], '分析此文件')
        trajectory = []
        for index in range(8):
            call = {'id':f'call_{index}', 'name':'find_material', 'arguments':{'page':index}}
            observation = {'role':'tool', 'name':call['name'], 'tool_call_id':call['id'], 'content':f'页{index}' + '条目' * 400}
            trajectory.extend([{'role':'assistant', 'content':'', 'tool_calls':[call]}, observation])
        request = await manager.project({'context_tokens':5000, 'max_tokens':1000}, '系统', trajectory)
        calls = [call['id'] for row in request for call in row.get('tool_calls', ())]
        observations = [row['tool_call_id'] for row in request if row['role'] == 'tool']
        assert calls == observations and calls[-1] == 'call_7' and len(calls) < 8
        catalog = await manager.read_receipt({'receipt':0})
        assert catalog['items'][0]['label'].startswith('页0')
        assert manager.omitted_count == 8 * 2 - len(calls) * 2
        remaining = trajectory[manager.omitted_count:]
        resumed = ReActContextManager([], '分析此文件', store=manager.store,
                                      checkpoint={'history_summary':manager.summary, 'receipt_refs':manager.receipt_refs})
        request = await resumed.project({'context_tokens':5000, 'max_tokens':1000}, '系统', remaining)
        assert any('较早工具步骤' in row['content'] and '回执' in row['content'] for row in request)


    asyncio.run(run())



def test_react_recovery_republishes_held_question_without_model_call():
    from avatar.conversation.agent_loop import run_agent
    from avatar.conversation.loop.runtime import load_checkpoint, resume_safe
    from avatar.conversation.loop.store import MemoryRunStore
    from avatar.core.execution import AskUser

    async def run():
        store = MemoryRunStore()
        question = AskUser(question='哪天上课？', options=['周一', '周二']).pending()
        paused = {'schema_version':2,'phase':'paused','user_question':question}
        assert resume_safe(paused, automatic=True)
        assert not resume_safe(paused, automatic=True, allow_question=False)
        assert not resume_safe({**paused,'user_question':{**question,'id':'invalid'}}, automatic=True)
        assert not resume_safe({**paused,'user_question':None}, automatic=True)
        store.save(paused)
        config = {'_run_store':store, '_auto_recover':True}
        model = SimpleNamespace(stream=AsyncMock(side_effect=AssertionError('paused job called the model')))
        rag = SimpleNamespace(chat=SimpleNamespace(model_client=model), validate=lambda _: None)
        with pytest.raises(AgentNeedsInput) as caught:
            await run_agent(rag,(config,'key','system',[]),'查看课程')
        assert caught.value.user_question == question
        model.stream.assert_not_called()

        legacy = {'phase':'paused','observations':['旧任务回执']}
        store.save(legacy)
        assert resume_safe(legacy) and not resume_safe(legacy, automatic=True)
        checkpoint = await load_checkpoint(store)
        assert checkpoint['phase'] == 'running' and checkpoint['trajectory'] == []
        assert '旧任务回执' in checkpoint['legacy_summary']

    asyncio.run(run())
