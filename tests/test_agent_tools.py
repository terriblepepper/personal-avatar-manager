"""按需工具授权、执行幂等和离线沙箱。"""
import asyncio
import json
import runpy
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from avatar.models.client import ModelClient
from avatar.core.execution import AgentNeedsInput
from avatar.sandbox.python_tool import AtomicPython, PythonArguments
from tests.support import (
    _react_probe_call,
    _react_probe_definition,
    _react_probe_receipt,
    _run_react_probe,
)


def test_repeated_python_action_keeps_result():
    from avatar.core.execution import current_tool_call

    calls=[]
    checkpoint={}
    files={'original':b'original'}
    async def execute(owner,resources,script,args,**kwargs):
        calls.append(resources[script])
        return {'status':'success','stdout':['1986569672'],'files':[]}
    def merge(patch, *, expected):
        assert checkpoint.get('sandbox')==expected['sandbox']
        checkpoint.update(patch)
    store=SimpleNamespace(load=lambda: checkpoint,merge=merge)
    runtime=SimpleNamespace(execute=execute)
    def inputs():
        return {'original':{'path':'inputs/original/file.txt','read':lambda: files['original']}}
    def new_tool(config):
        return AtomicPython(runtime,config,owner='owner',conversation='c1',inputs=inputs)
    args=PythonArguments(code='print(17348 * 114514)',input_file_ids=['original'])

    async def invoke(tool, step, call_id, replay=False):
        token=current_tool_call.set((step,call_id,replay))
        try:
            return await tool.execute(args)
        finally:
            current_tool_call.reset(token)

    config={'_run_store':store}
    async def run():
        receipt=await invoke(new_tool(config),1,'provider-call')
        assert receipt['stdout']=='1986569672'
        resumed=new_tool({'_run_store':store})
        assert await invoke(resumed,1,'provider-call',replay=True)==receipt
        with pytest.raises(ValueError,match='已有回执'):
            await invoke(resumed,1,'provider-call')
        with pytest.raises(ValueError,match='已有回执'):
            await invoke(resumed,2,'provider-call',replay=True)
        files['original']=b'changed'
        with pytest.raises(HTTPException) as rejected:
            await invoke(resumed,1,'provider-call',replay=True)
        assert rejected.value.status_code==409
    asyncio.run(run())
    assert calls==[args.code.encode()]
    assert config['_sandbox_succeeded'] and config['_generated_files']==[]


def test_distinct_actions_stop_at_task_attempt_cap():
    calls=[]
    async def execute(owner,resources,script,args,**kwargs):
        calls.append(resources[script])
        return {'status':'success','stdout':'','files':[]}
    tool=AtomicPython(SimpleNamespace(execute=execute),{},owner='owner',conversation='c1',inputs=dict)
    async def run():
        for index in range(16):
            await tool.execute(PythonArguments(code=f'print({index})'))
        with pytest.raises(ValueError,match='十六次'):
            await tool.execute(PythonArguments(code='print(16)'))
    asyncio.run(run())
    assert len(calls)==16


@pytest.mark.parametrize('interrupted', ['execution', 'confirmation'])
def test_unknown_sandbox_operation_preserves_intent(interrupted):
    from avatar.conversation.loop.store import MemoryRunStore

    class Store(MemoryRunStore):
        def merge(self, patch, *, expected=None):
            if interrupted=='confirmation' and not patch['sandbox']['in_flight']:
                raise OSError('receipt write interrupted')
            super().merge(patch, expected=expected)

    store = Store()
    config = {'_run_store':store}
    runs = []

    async def execute(owner, resources, script, args, **kwargs):
        runs.append(resources[script])
        if interrupted=='execution':
            raise RuntimeError('sandbox result lost')
        return {'status':'success','stdout':'ok','files':[]}

    def sandbox(config):
        return AtomicPython(SimpleNamespace(execute=execute),config,owner='owner',conversation='c1',inputs=dict)

    async def run():
        args = PythonArguments(code='print(1)')
        active = sandbox(config)
        with pytest.raises(AgentNeedsInput) as interrupted_operation:
            await active.execute(args)
        assert interrupted_operation.value.checkpoint['sandbox_attempted']
        assert store.load()['sandbox']['in_flight']
        assert not store.load()['sandbox']['receipts']
        with pytest.raises(AgentNeedsInput,match='上次沙箱操作结果未确认'):
            await active.execute(PythonArguments(code='print(2)'))
        with pytest.raises(AgentNeedsInput,match='上次沙箱操作结果未确认'):
            await sandbox({'_run_store':store}).execute(args)

    asyncio.run(run())
    assert runs==[b'print(1)']


# Keep the offline Deno command and permissions stable across Python dependency updates

def test_offline_command_with_legacy_ready_and_no_python_wrapper(tmp_path, monkeypatch):
    module = Path(__file__).resolve().parents[1] / 'avatar/sandbox/sandbox.py'
    monkeypatch.setitem(sys.modules, 'langchain_sandbox', None)
    monkeypatch.delenv('AVATAR_SANDBOX_RUNTIME', raising=False)
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


def test_disclosure_schema_compaction_preserves_constraints_and_property_names():
    from avatar.conversation.loop.drafts import APPEND_HTML_DRAFT
    from avatar.conversation.tools.html import CREATE_HTML
    from avatar.models.tools import compact_schema
    from pydantic import BaseModel, Field

    class Named(BaseModel):
        title: str = Field(pattern='^[A-Z]+$')
        state: str = Field(json_schema_extra={'enum':['old','new']})
        example: dict = Field(default={'title':'keep data literal'})

    class Envelope(BaseModel):
        title: Named

    schema = Envelope.model_json_schema()
    compact = compact_schema(schema)
    assert 'title' not in compact and 'title' in schema
    assert compact['required'] == ['title']
    assert compact['properties']['title']['$ref'] == '#/$defs/Named'
    fields = compact['$defs']['Named']['properties']
    assert fields['title']['pattern'] == '^[A-Z]+$' and 'title' not in fields['title']
    assert fields['state']['enum'] == ['old','new']
    assert fields['example']['default'] == {'title':'keep data literal'}
    for tool in (CREATE_HTML, APPEND_HTML_DRAFT):
        parameters = tool['parameters']
        result = compact_schema(parameters)
        assert list(result['properties']) == list(parameters['properties'])
        assert result['required'] == parameters['required']
        assert result['additionalProperties'] == parameters['additionalProperties']


def test_disclosure_batches_keep_stable_prefix_and_native_wire_pairs():
    from avatar.conversation.loop.store import MemoryRunStore
    from avatar.models.client import generation_deadline, generation_http_timeout

    request_config = {'_run_store':MemoryRunStore(),'_html_enabled':True,'context_tokens':32768,
                      '_entry_tools':[_react_probe_definition('run_python')],
                      '_load_session_source':lambda *_:None}
    requests, executions = [], []

    async def execute(name, args):
        executions.append((name,args['code']))
        return {'stdout':'1'}

    async def stream(settings, key, system, history, message):
        active = [tool['name'] for tool in settings['_tools']]
        requests.append((settings,system,history,active))
        assert system == requests[0][1]
        replies = {row['tool_call_id']:row for row in history if row['role']=='tool'}
        if len(requests)==1:
            assert 'enable_tools' in active and not {'run_python','create_html','edit_html'} & set(active)
            assert generation_deadline(settings)==180 and generation_http_timeout(settings).read==120
            yield _react_probe_receipt(
                _react_probe_call('activate','enable_tools',{'names':['run_python','create_html']}),
                _react_probe_call('early','run_python',{'code':'print(0)'}),
            )
        elif len(requests)==2:
            assert not executions and replies['early']['is_error'] and not replies['activate']['is_error']
            assert active == [*requests[0][3],'run_python','create_html']
            assert generation_deadline(settings)==600 and generation_http_timeout(settings).read==600
            yield _react_probe_receipt(
                _react_probe_call('repeat','enable_tools',{'names':['create_html','run_python']}),
                _react_probe_call('python','run_python',{'code':'print(1)'}),
                _react_probe_call('html','create_html',{'filename':'demo.html','html':'<!doctype html><main>test</main>'}),
            )
        else:
            assert len(requests)==3
            assert active[:len(requests[1][3])] == requests[1][3] and 'edit_html' in active
            assert replies['repeat']['is_error'] and not replies['python']['is_error'] and not replies['html']['is_error']
            yield {'type':'delta','text':'已完成。'}
            yield _react_probe_receipt(reason='stop')

    request_config['_execute_entry_tool']=execute
    assert asyncio.run(_run_react_probe(stream,request_config)).text=='已完成。'
    assert executions == [('run_python','print(1)')]
    settings,system,history,_ = requests[-1]
    for fmt in ('openai','anthropic','gemini'):
        wire = {**settings,'api_format':fmt,'base_url':'https://model.example/v1','model':'test',
                'provider':'custom','max_tokens':1024}
        *_, body = ModelClient().build_request(wire,'fixture-key',system,history,None)
        if fmt=='openai':
            assert any(row.get('tool_call_id')=='activate' for row in body['messages'])
            assert any(tool['function']['name']=='enable_tools' for tool in body['tools'])
        elif fmt=='anthropic':
            assert any(block.get('tool_use_id')=='activate' for row in body['messages']
                       for block in row.get('content',[]) if isinstance(block,dict))
            assert any(tool['name']=='enable_tools' for tool in body['tools'])
        else:
            parts = [part for row in body['contents'] for part in row['parts']]
            assert any(part.get('functionResponse',{}).get('name')=='enable_tools' for part in parts)
            assert any(tool['name']=='enable_tools' for tool in body['tools'][0]['functionDeclarations'])


def test_disclosure_search_unlocks_verified_materials_and_archived_receipts():
    from avatar.conversation.agent_loop import run_agent
    from avatar.conversation.loop.entry_tools import EntryToolContext, bind_entry_tools

    source = {'file_id':'doc'}
    hit = SimpleNamespace(content={'items':[{'file_id':'doc','text':'x'*12500}]},
                          sources=[],revisions=[(('profile','doc'),'v1')],images=[],audio=[])
    read = SimpleNamespace(content={'text':'授权原件'},sources=[source],
                           revisions=hit.revisions,images=[],audio=[])
    requests = []
    rag = SimpleNamespace(validate=lambda _:None, search_tool=AsyncMock(return_value=hit),
                          read_tool=AsyncMock(return_value=read), retrieve=None)

    async def stream(settings, key, system, history, message):
        active = [tool['name'] for tool in settings['_tools']]
        requests.append(active)
        if len(requests)==1:
            assert 'search_materials' in active
            assert not {'read_material','send_material','read_step_receipt'} & set(active)
            yield _react_probe_receipt(_react_probe_call('search','search_materials',{'query':'资料'}))
        elif len(requests)==2:
            assert active[:len(requests[0])]==requests[0]
            assert {'read_material','send_material','read_step_receipt'} <= set(active)
            search, = [row for row in history if row['role']=='tool']
            assert not search['is_error'] and search['receipt_ref']>0
            yield _react_probe_receipt(_react_probe_call('read','read_material',{'file_id':'doc'}))
        else:
            assert len(requests)==3
            yield {'type':'delta','text':'已核对原件。'}
            yield _react_probe_receipt(reason='stop')

    chat = SimpleNamespace(rag=rag, service=SimpleNamespace(attachments=SimpleNamespace(database=None,directory=None)), model_client=SimpleNamespace(stream=stream))
    rag.chat=chat

    async def run():
        config = {'context_tokens':32768,'_material_owner':'owner'}
        context = await bind_entry_tools(chat,(config,'fixture-key','测试',[]),
                                         EntryToolContext('owner','aic',{'id':'chat'},[]))
        assert (await run_agent(rag,context,'核对原件')).text=='已核对原件。'
        assert config['_material_sources']==[source]
        rag.search_tool.assert_awaited_once()
        rag.read_tool.assert_awaited_once()

    asyncio.run(run())


def test_disclosure_resume_drops_retracted_tools_and_town_questions():
    from avatar.conversation.loop.store import MemoryRunStore

    store=MemoryRunStore()
    config={'_run_store':store,'_html_enabled':True,'context_tokens':32768,
            '_entry_tools':[_react_probe_definition('run_python')]}
    requests=[]

    async def stream(settings, key, system, history, message):
        active = {tool['name'] for tool in settings['_tools']}
        requests.append(active)
        if len(requests)==1:
            yield _react_probe_receipt(_react_probe_call('enable','enable_tools',{'names':['create_html','run_python']}))
        elif len(requests)==2:
            assert {'create_html','run_python'} <= active
            yield _react_probe_receipt(_react_probe_call('ask','ask_user',{'question':'是否继续？'}))
        elif len(requests)==3:
            assert 'create_html' in active and 'run_python' not in active and 'run_python' not in system
            yield _react_probe_receipt(_react_probe_call('revoked','run_python',{'code':'print(1)'}))
        else:
            assert len(requests)==4
            revoked, = [row for row in history if row.get('tool_call_id')=='revoked']
            assert revoked['is_error'] and 'run_python' not in active
            yield {'type':'delta','text':'继续。'}
            yield _react_probe_receipt(reason='stop')

    async def check():
        with pytest.raises(AgentNeedsInput) as paused:
            await _run_react_probe(stream,config)
        assert store.load()['unlocked_tools'] == ['create_html','run_python']
        config['_entry_tools']=[]
        config['_answered_question']={**paused.value.user_question,'answer':'继续'}
        assert (await _run_react_probe(stream,config)).text=='继续。'
        assert store.load()['unlocked_tools']==['create_html']

        rounds=0
        async def town(settings, key, system, history, message):
            nonlocal rounds
            rounds+=1
            active = {tool['name'] for tool in settings['_tools']}
            assert not {'ask_user','run_python'} & active
            assert 'ask_user' not in system
            if rounds==1:
                yield _react_probe_receipt(_react_probe_call('forbidden','enable_tools',{'names':['ask_user','run_python']}))
            else:
                denied, = [row for row in history if row.get('tool_call_id')=='forbidden']
                assert denied['is_error']
                yield {'type':'delta','text':'继续。'}
                yield _react_probe_receipt(reason='stop')
        assert (await _run_react_probe(town,{'_html_enabled':True,'_allow_user_questions':False})).text=='继续。'

    asyncio.run(check())


@pytest.mark.parametrize('truncated,failed_steps', [(False,0), (True,0), (False,3)])
def test_disclosure_undeclared_call_recovers_without_first_execution(truncated,failed_steps):
    from avatar.conversation.loop.store import MemoryRunStore

    store = MemoryRunStore()
    config = {'_run_store':store,'_html_enabled':True,'context_tokens':32768}
    seen = []

    async def stream(settings, key, system, history, message):
        active = {tool['name'] for tool in settings['_tools']}
        seen.append(active)
        args = {'filename':'after.html','html':'<!doctype html><main>done</main>'}
        step = len(seen)
        if step<=failed_steps:
            yield _react_probe_receipt(_react_probe_call(f'unknown-{step}','unknown_tool',{}))
        elif step==failed_steps+1:
            assert 'create_html' not in active
            yield _react_probe_receipt(_react_probe_call('early','create_html',args),
                                       reason='length' if truncated else 'tool_calls',partial=truncated)
        elif step==failed_steps+2:
            assert 'create_html' in active
            assert not store.load()['drafts'] and not store.load().get('files')
            if truncated:
                assert any('create_html' in row['content'] for row in history if row['role']=='user')
            else:
                early, = [row for row in history if row.get('tool_call_id')=='early']
                assert early['is_error']
            yield _react_probe_receipt(_react_probe_call('retry','create_html',args))
        else:
            assert step==failed_steps+3
            assert config['_generated_files'][0]['filename']=='after.html'
            yield {'type':'delta','text':'已完成。'}
            yield _react_probe_receipt(reason='stop')

    assert asyncio.run(_run_react_probe(stream,config)).text=='已完成。'


def test_disclosure_replays_archived_enable_without_exposing_meta_again():
    from avatar.conversation.loop.store import BATCH_PREFIX, MemoryRunStore

    store = MemoryRunStore()
    reference = BATCH_PREFIX + 'enable-replay'
    call = _react_probe_call('pending-enable','enable_tools',{'names':['create_html']})
    batch = {'assistant':{'role':'assistant','content':'','tool_calls':[call]},
             'declared':['enable_tools']}
    store.archive(reference,json.dumps(batch).encode(),patch={
        'schema_version':2,'phase':'running','step':1,'unlocked_tools':['create_html'],
        'pending_batch':{'ref':reference},
    })

    async def stream(settings, key, system, history, message):
        active = {tool['name'] for tool in settings['_tools']}
        assert 'create_html' in active and 'enable_tools' not in active
        assert 'enable_tools' not in system
        receipt, = [row for row in history if row.get('tool_call_id')=='pending-enable']
        assert not receipt['is_error']
        assert json.loads(receipt['content']) == {'activated':[], 'already_active':['create_html'], 'unavailable':[]}
        yield {'type':'delta','text':'已经加载。'}
        yield _react_probe_receipt(reason='stop')

    config = {'_run_store':store,'_html_enabled':True,'context_tokens':32768}
    assert asyncio.run(_run_react_probe(stream,config)).text=='已经加载。'
    assert store.load()['pending_batch'] is None and not store.read(reference)


def test_disclosure_rechecks_state_on_resume_guesses_and_truncation():
    from avatar.conversation.loop.store import MemoryRunStore

    store = MemoryRunStore()
    name = 'read_session_file'
    call = _react_probe_call('stale', name, {})
    store.merge({'schema_version':2, 'phase':'running', 'unlocked_tools':[name],
                 'pending_batch':{'assistant':{'role':'assistant','content':'','tool_calls':[call]},
                                  'declared':[name]}})
    execute = AsyncMock(return_value={'ok':True})
    config = {'_run_store':store, '_entry_tools':list(map(_react_probe_definition, (name,'run_python'))),
              '_execute_entry_tool':execute, 'context_tokens':32768}
    rounds = 0

    async def stream(settings, key, system, history, message):
        nonlocal rounds
        rounds += 1
        active = {tool['name'] for tool in settings['_tools']}
        receipts = {row['tool_call_id']:row for row in history if row['role']=='tool'}
        if rounds <= 3:
            assert name not in active and name not in system
            execute.assert_not_awaited()
        if rounds == 1:
            assert receipts['stale']['is_error']
            yield _react_probe_receipt(_react_probe_call('enable','enable_tools',{'names':[name]}),
                                       _react_probe_call('early',name,{}))
        elif rounds == 2:
            assert receipts['enable']['is_error'] and receipts['early']['is_error']
            yield _react_probe_receipt(_react_probe_call('partial',name,{}), reason='length', partial=True)
        elif rounds == 3:
            config['_has_session_files'] = True
            yield _react_probe_receipt(_react_probe_call('clock','get_current_time',{}))
        elif rounds == 4:
            assert name in active
            yield _react_probe_receipt(_react_probe_call('available',name,{}))
        else:
            assert rounds == 5 and not receipts['available']['is_error']
            yield {'type':'delta','text':'已读。'}
            yield _react_probe_receipt(reason='stop')

    assert asyncio.run(_run_react_probe(stream,config)).text == '已读。'
    execute.assert_awaited_once_with(name,{})
