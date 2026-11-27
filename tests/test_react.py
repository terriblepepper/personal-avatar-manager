"""ReAct 流式执行、压缩、暂停和检查点恢复。"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from fastapi import HTTPException
from avatar.core.db import now
from avatar.models.client import ModelClient
from avatar.core.execution import AgentNeedsInput
from tests.support import (
    _react_probe_call,
    _react_probe_definition,
    _react_probe_receipt,
    _run_react_probe,
)


def test_react_single_streaming_answer():
    from avatar.conversation.agent_loop import run_agent

    calls, deltas = [], []

    async def stream(config, key, system, history, message):
        calls.append(config)
        yield {'type':'delta','text':'TCP 提供可靠且有序的传输。'}
        yield {'type':'usage','usage':{'total_tokens':20},'finish_reason':'stop'}

    rag = SimpleNamespace(chat=SimpleNamespace(model_client=SimpleNamespace(stream=stream)), validate=lambda _: None)
    config = {'provider':'custom','model':'test','api_format':'openai','context_tokens':32768,'max_tokens':1024,'_html_enabled':True}
    result = asyncio.run(run_agent(rag, (config,'key','测试分身',[]), '用一句话介绍 TCP。',
                                   progress=lambda event:None,on_delta=deltas.append))
    assert result.text == 'TCP 提供可靠且有序的传输。'
    assert deltas == [result.text]
    assert result.usage['total_tokens'] == 20
    assert len(calls)==1
    active = {tool['name'] for tool in calls[0]['_tools']}
    assert {'ask_user','get_current_time'} <= active
    assert not {'create_html','append_html_draft','read_step_receipt'} & active


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
              '_execute_entry_tool':execute, '_agent_saved_files':[{'id':'original','filename':'original.html','size':32}],
              '_conversation_tools':[{'id':'animation','enabled':scenario=='read_then_create'}],
              '_html_enabled':scenario=='read_then_create'}
    result = asyncio.run(run_agent(rag,(config,'key','测试分身',[]),'处理本轮请求。'))
    assert result.text == '已处理。'
    assert executed == ([] if scenario=='unavailable' else ['read_session_file'])
    if scenario=='read_then_create':
        assert [item['filename'] for item in config['_generated_files']] == ['updated.html']


def test_unknown_sandbox_preserves_context_and_usage():
    from avatar.conversation.loop.store import MemoryRunStore

    store=MemoryRunStore()
    store.merge({'schema_version':2,'phase':'running','unlocked_tools':['run_python']})
    config={'_run_store':store,'_entry_tools':[_react_probe_definition('run_python')],'context_tokens':32768}
    attempts = []

    async def tool(name, args):
        attempts.append(name)
        store.merge({'sandbox':{'in_flight':'unconfirmed'}})
        raise AgentNeedsInput.from_context('沙箱结果未确认',config)

    async def stream(*args):
        yield _react_probe_receipt(_react_probe_call('clock','get_current_time',{}),_react_probe_call('python','run_python',{'code':'print(1)'}),
                      _react_probe_call('skipped','get_current_time',{}))

    config['_execute_entry_tool']=tool
    with pytest.raises(AgentNeedsInput) as paused:
        asyncio.run(_run_react_probe(stream,config))
    state=store.load()
    assert state['phase']=='paused' and state['pending_batch'] is None
    assert paused.value.usage==state['usage'] and state['usage']['total_tokens']==3
    assert state['sandbox']['in_flight']=='unconfirmed'
    rows=[r for r in state['trajectory'] if r['role']=='tool']
    assert [r['tool_call_id'] for r in rows]==['clock','python','skipped']
    assert [r['is_error'] for r in rows]==[False,True,True]
    assert attempts==['run_python']


def test_draft_continues_after_four_productive_truncations_without_phantom_text():
    from avatar.conversation.loop.store import MemoryRunStore

    store=MemoryRunStore()
    config={'_run_store':store,'_html_enabled':True,'context_tokens':32768,'reasoning_effort':'high',
            '_conversation_tools':[{'id':'animation','enabled':True}]}
    rounds=[]
    visible=[]
    progress=[]

    async def stream(settings,*args):
        rounds.append(settings)
        n=len(rounds)
        active = {tool['name'] for tool in settings['_tools']}
        assert 'create_html' in active
        assert ('append_html_draft' in active) == (2 <= n <= 7)
        if n==1:
            yield {'type':'status','stage':'waiting','message':'等待'}
            yield {'type':'delta','text':'请撤回的候选'}
            yield _react_probe_receipt(_react_probe_call('initial','create_html',{'filename':'demo.html','html':'<!doctype html><main>'}),
                          reason='length',partial=True)
        elif n<=6:
            identifier,draft=next(iter(store.load()['drafts'].items()))
            yield _react_probe_receipt(_react_probe_call(str(n),'append_html_draft',{'draft_id':identifier,'offset':draft['offset'],'chunk':'有效字节'}),
                          reason='length',partial=True)
        elif n==7:
            identifier,draft=next(iter(store.load()['drafts'].items()))
            yield _react_probe_receipt(_react_probe_call('finish-draft','append_html_draft',
                               {'draft_id':identifier,'offset':draft['offset'],'chunk':'</main>','done':True}))
        else:
            yield {'type':'delta','text':'成品已就绪。'}
            yield _react_probe_receipt(reason='stop')

    result=asyncio.run(_run_react_probe(stream,config,progress=progress.append,on_delta=visible.append))
    assert result.text=='成品已就绪。'
    assert rounds[-1]['reasoning_effort']=='high'
    assert any(event.get('stage')=='waiting' for event in progress)
    assert b'</main>' in config['_generated_files'][0]['data']


def test_media_and_refreshed_directory_survive_native_steps():
    from avatar.conversation.loop.store import MemoryRunStore

    store=MemoryRunStore()
    store.merge({'schema_version':2,'phase':'running','unlocked_tools':['run_python','read_session_file']})
    image={'mime_type':'image/png','data':'attached'}
    extra={'mime_type':'image/png','data':'tool-read'}
    audio={'mime_type':'audio/wav','data':'tool-audio'}
    config={'_entry_tools':list(map(_react_probe_definition,('read_session_file','read_material','run_python'))),
            'context_tokens':32768,'_has_session_files':True,'_image_inputs':[image],'audio_enabled':True,'_run_store':store,
            '_material_sources':[{'file_id':'a'}]}
    executed=[]
    calls=iter([('read_session_file',{'query':''}),('read_material',{'file_id':'a'}),('run_python',{'code':'print(1)'}),
                ('read_session_file',{'query':''}),('read_material',{'file_id':'a'})])
    received=[]

    async def tool(name,args):
        executed.append(name)
        if name=='read_material':
            config['_entry_native_media']={'images':[image,extra],'audio':[audio]}
        return {'status':'success'}

    async def stream(settings,*args):
        received.append((settings['_image_inputs'],settings['_audio_inputs']))
        if selection:=next(calls,None):
            yield _react_probe_receipt(_react_probe_call(str(len(received)),*selection))
        else:
            yield {'type':'delta','text':'完成'}
            yield _react_probe_receipt(reason='stop')

    config['_execute_entry_tool']=tool
    assert asyncio.run(_run_react_probe(stream,config)).text=='完成'
    assert executed==['read_session_file','read_material','run_python','read_session_file','read_material']
    assert received[2] == received[-1] == ([image,extra],[audio])


def test_tool_media_survives_truncation_and_finish_gate_correction():
    image={'mime_type':'image/png','data':'authorized-view-fixture'}
    audio={'mime_type':'audio/wav','data':'authorized-audio-fixture'}
    replacement={'mime_type':'image/png','data':'second-read-fixture'}
    config={'context_tokens':32768,'audio_enabled':True,'reasoning_effort':'high',
            'provider':'custom','api_format':'openai','model':'qwen-fixture',
            '_entry_tools':[_react_probe_definition('read_material')],'_material_sources':[{'file_id':'original'}]}
    requests=[]

    async def tool(name, args):
        config['_entry_native_media']=({'images':[image],'audio':[audio]} if args['file_id']=='original'
                                       else {'images':[replacement]})
        return {'file_id':'original','text':'已提供画面及录音'}

    async def stream(settings, key, system, history, message):
        requests.append((settings,history))
        n=len(requests)
        if n==1:
            yield _react_probe_receipt(_react_probe_call('read','read_material',{'file_id':'original'}))
        elif n==2:
            yield _react_probe_receipt(_react_probe_call('unavailable','unknown_tool',{}),reason='length',partial=True)
        elif n==3:
            yield _react_probe_receipt(_react_probe_call('bad','unknown_tool',{}))
        elif n==4:
            yield _react_probe_receipt(reason='stop')
        elif n==5:
            yield {'type':'delta','text':'[[attachment:unauthorized]]'}
            yield _react_probe_receipt(reason='stop')
        elif n==6:
            yield _react_probe_receipt(_react_probe_call('second','read_material',{'file_id':'second'}))
        else:
            yield {'type':'delta','text':'已核对素材。'}
            yield _react_probe_receipt(reason='stop')

    config['_execute_entry_tool']=tool
    assert asyncio.run(_run_react_probe(stream,config)).text=='已核对素材。'
    assert all(settings['_image_inputs']==[image] and settings['_audio_inputs']==[audio] for settings,_ in requests[1:-1])
    assert requests[-1][0]['_image_inputs']==[replacement] and not requests[-1][0]['_audio_inputs']
    hint=[row['content'] for row in requests[2][1] if row['role']=='user']
    assert any('未声明' in text for text in hint)
    assert requests[2][0]['_recover_thinking'] is True
    assert not requests[3][0].get('_recover_thinking')
    assert not any(row['role']=='assistant' and not row['content'] and not row.get('tool_calls')
                   for _,history in requests for row in history)


def test_full_media_capacity_keeps_valid_tool_receipt():
    attached=[{'mime_type':'image/png','data':str(index)} for index in range(5)]
    config={'context_tokens':32768,'_image_inputs':attached,'_entry_tools':[_react_probe_definition('read_material')],
            '_material_sources':[{'file_id':'1'}]}
    requests=[]

    async def tool(name, args):
        config['_entry_native_media']={'images':[{'mime_type':'image/png','data':'excess'}]}
        return {'text':'正文读取成功'}

    async def stream(settings, key, system, history, message):
        requests.append((settings,history))
        if len(requests)<=4:
            yield _react_probe_receipt(_react_probe_call(str(len(requests)),'read_material',{'file_id':str(len(requests))}))
        else:
            yield {'type':'delta','text':'已核对正文。'}
            yield _react_probe_receipt(reason='stop')

    config['_execute_entry_tool']=tool
    assert asyncio.run(_run_react_probe(stream,config)).text=='已核对正文。'
    final,history=requests[-1]
    assert final['_image_inputs']==attached and final['_tools']
    rows=[row for row in history if row['role']=='tool']
    assert len(rows)==4 and all(not row['is_error'] for row in rows)
    assert all(json.loads(row['content'])['result']['text']=='正文读取成功' for row in rows)


def test_native_html_draft_survives_truncation_and_writer_change(pg_database):
    from hashlib import sha256

    from avatar.conversation.loop.drafts import APPEND_HTML_DRAFT, HtmlDrafts
    from avatar.conversation.loop.runtime import restore_files
    from avatar.conversation.loop.store import RunConflict, RunStore
    from avatar.conversation.tools.html import CREATE_HTML
    from avatar.core.execution import current_tool_call
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

    async def invoke(scope, execute, arguments):
        token = current_tool_call.set(scope)
        try:
            return await execute(arguments)
        finally:
            current_tool_call.reset(token)

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
        drafts = HtmlDrafts(origin, state, outputs)
        draft = await drafts.capture(clipped)
        assert draft['status'] == 'truncated_draft_saved'
        assert draft['offset'] == len(source.encode()) and draft['tail_preview'] == source[-300:]
        revision = origin._revision
        base_call = {'name':'append_html_draft','id':'missing-cursor'}
        draft_id = json.dumps(draft['draft_id'])
        incomplete = [
            {'arguments':'{"chunk":"尚未保存'},
            {'arguments':'{"draft_id":' + draft_id + ',"chunk":"尚未保存'},
            {'arguments':'{"draft_id":' + draft_id + ',"chunk":"尚未保存","offset":10'},
            {'argument_format':'gemini_partial_args','arguments':{'chunks':[
                {'args':{'draft_id':draft['draft_id']},
                 'partialArgs':[{'jsonPath':'$.offset','numberValue':10,'willContinue':True}]},
            ]}},
        ]
        for fragment in incomplete:
            reminder = await drafts.capture({**base_call, **fragment})
            assert reminder is not None
            assert reminder == {**draft, 'status':'draft_needs_cursor', 'hint':reminder['hint']}
            assert '未保存' in reminder['hint'] and 'chunk' in reminder['hint']
        assert await drafts.capture({**base_call, 'arguments':{'draft_id':'draft-'+'f'*32}}) is None
        assert list(CREATE_HTML['parameters']['properties']) == ['filename','html']
        assert list(APPEND_HTML_DRAFT['parameters']['properties']) == ['draft_id','offset','done','chunk']
        for repeated in (clipped,
                         {**clipped, 'id':'new-id', 'arguments':'{"filename":"timeline.html","html":"different'},
                         {**clipped, 'id':'filename-only', 'arguments':'{"filename":"timeline.html","html":'}):
            assert await drafts.capture(repeated) == {**draft, 'status':'draft_exists'}
        assert origin._revision == revision

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
        call = {'name':'append_html_draft', 'id':'next', 'arguments':next_prefix}
        appended = await invoke((2,'next',False), drafts.capture, call)
        new = RunStore(pg_database, *run, 'writer-3')
        drafts = HtmlDrafts(new, new.acquire(), outputs)
        revision = new._revision
        assert await invoke((2,'next',True), drafts.capture, call) == appended
        with pytest.raises(ValueError, match=str(appended['offset'])):
            await invoke((2,'another',True), drafts.capture, {**call, 'id':'another'})
        assert new._revision == revision
        typed_prefix = [
            {'args':{'draft_id':resume['draft_id']}},
            {'partialArgs':[{'jsonPath':'$.chunk','stringValue':'终点','willContinue':True}]},
            {'partialArgs':[{'jsonPath':'$.offset','numberValue':float(appended['offset'])},
                            {'jsonPath':'$.chunk'}]},
        ]
        typed = await drafts.capture({'name':'append_html_draft','id':'gemini',
            'argument_format':'gemini_partial_args','arguments':{'chunks':typed_prefix}})
        final = {**arguments, 'offset':typed['offset'], 'chunk':'</main>', 'done':True}
        page = await invoke((3,'finish',False), drafts.append, final)
        new = RunStore(pg_database, *run, 'writer-4')
        restored = new.acquire()
        outputs = outputs[:1]
        await restore_files(new, restored, {'_generated_files':outputs})
        drafts = HtmlDrafts(new, restored, outputs)
        revision = new._revision
        assert await invoke((3,'finish',True), drafts.append, final) == page
        with pytest.raises(ValueError, match='草稿不存在或已交付'):
            await invoke((4,'finish',True), drafts.append, final)
        assert new._revision == revision
        [receipt] = new.load()['files']
        assert [item['id'] for item in outputs] == ['python-result', page['id']]
        data = (source + '下一段终点</main>').encode()
        assert outputs[1]['data'] == data and new.read(receipt['draft']).encode() == data
        assert receipt['sha256'] == sha256(data).hexdigest() and not drafts.active
        for filename in ('first.html', 'second.html'):
            assert await drafts.capture({'name':'create_html', 'id':filename,
                                         'arguments':{'filename':filename,'html':'<main>'}})
        revision = new._revision
        assert await drafts.capture({'name':'append_html_draft', 'id':'ambiguous',
                                     'arguments':'{"chunk":"cannot locate'}) is None
        assert new._revision == revision

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
        assert checkpoint['entry_material'] == {'revisions':proofs['revisions']}
        assert 'session-file' in checkpoint['session_source_checks'] and 'session_file_reads' not in checkpoint
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
        past = {'role':'assistant','content':'上一轮答复'}
        config = {'context_tokens':5000, 'max_tokens':1000}
        manager = ReActContextManager([past], '分析此文件',
                                      checkpoint={'legacy_summary':'旧执行结果'})
        trajectory = []
        for index in range(8):
            call = {'id':f'call_{index}', 'name':'find_material', 'arguments':{'page':index}}
            observation = {'role':'tool', 'name':call['name'], 'tool_call_id':call['id'], 'content':f'页{index}' + '条目' * 400}
            trajectory.extend([{'role':'assistant', 'content':'', 'tool_calls':[call]}, observation])
        quick = await manager.project(config, '系统', trajectory[:2])
        assert [row['content'] for row in quick[:2]] == ['上一轮答复', '分析此文件']
        assert '旧执行结果' in quick[2]['content']
        request = await manager.project(config, '系统', trajectory)
        goal = next(index for index,row in enumerate(request) if row['content'] == '分析此文件')
        assert '旧执行结果' in request[goal+1]['content']
        assert '较早工具步骤' in request[goal+2]['content']
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
        assert await load_checkpoint(store, automatic=True) == legacy
        checkpoint = await load_checkpoint(store)
        assert checkpoint['phase'] == 'running' and checkpoint['trajectory'] == []
        assert '旧任务回执' in checkpoint['legacy_summary']

    asyncio.run(run())
