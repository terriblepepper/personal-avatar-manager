"""模型原生工具协议、流式报文与模型配置。"""
import asyncio
import json
import httpx
import pytest
from fastapi import HTTPException
from avatar.models.client import ModelClient


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
        return '8.8.8.8'
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
                                        [{'role':'user','content':'question'},
                                         {'role':'assistant','content':'earlier'}], 'new')
        if fmt == 'gemini':
            assert plain['contents'] == [{'role':'user','parts':[{'text':'question'}]},
                                         {'role':'model','parts':[{'text':'earlier'}]},
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


@pytest.mark.parametrize('fmt', ['anthropic', 'gemini'])
def test_native_tool_replies_merge_adjacent_user_blocks_and_keep_media(fmt):
    expected_tools = ['first', 'second']
    history = [{'role':'user','content':'earlier'}, {'role':'user','content':'objective'},
               {'role':'assistant','content':'','tool_calls':[
                   {'name':'lookup','id':call_id,'wire_id':call_id,'arguments':{}} for call_id in expected_tools]},
               {'role':'user','content':'before receipts'},
               {'role':'tool','tool_call_id':'first','name':'lookup','content':'found'},
               {'role':'user','content':'between receipts'},
               {'role':'tool','tool_call_id':'second','name':'lookup','content':'failure','is_error':True}]
    config = {'api_format':fmt, 'provider':'custom', 'model':'mock', 'base_url':'https://example.test',
              'max_tokens':512, '_react_tool_calls':True, '_image_inputs':[{'mime_type':'image/png','data':'YQ=='}]}
    if fmt == 'gemini':
        config.update(audio_enabled=True, _audio_inputs=[{'mime_type':'audio/wav','data':'Yg=='}])
    *_, body = ModelClient().build_request(config, 'key', 'system', history, 'latest text')
    if fmt == 'anthropic':
        request, _, merged = body['messages']
        assert [part['text'] for part in request['content']] == ['earlier','objective']
        blocks = merged['content']
        assert [r['tool_use_id'] for r in blocks[:2]] == expected_tools
        assert blocks[1]['is_error']
        assert blocks[-1]['source']['data'] == 'YQ=='
        text = blocks[2:-1]
    else:
        request, _, merged = body['contents']
        assert [part['text'] for part in request['parts']] == ['earlier','objective']
        blocks = merged['parts']
        assert [r['functionResponse']['id'] for r in blocks[:2]] == expected_tools
        assert blocks[1]['functionResponse']['response'] == {'error':'failure'}
        assert [p['inline_data']['data'] for p in blocks[-2:]] == ['YQ==','Yg==']
        text = blocks[2:-2]
    assert [part['text'] for part in text] == ['before receipts','between receipts','latest text']


@pytest.mark.parametrize('fmt', ['anthropic','gemini'])
def test_native_tool_normalizes_orphan_and_adjacent_assistant_blocks(fmt):
    from avatar.models.tools import wire_messages

    private = ({'type':'thinking','thinking':'','signature':'opaque'} if fmt == 'anthropic'
               else {'text':'','thoughtSignature':'opaque','thought':True})
    call_layout = {'call':0}
    if fmt == 'gemini':
        call_layout['thoughtSignature'] = 'call-signature'
    history = [
        {'role':'user','content':' '},
        {'role':'assistant','content':'older answer'},
        {'role':'assistant','content':''},
        {'role':'user','content':' '},
        {'role':'assistant','content':'', 'replay':{'layout':[private, {'text_length':0}, call_layout]},
         'tool_calls':[{'id':'native','wire_id':'native','name':'lookup','arguments':{}}]},
        {'role':'assistant','content':'latest note'},
        {'role':'tool','tool_call_id':'native','name':'lookup','content':'found'},
        {'role':'user','content':'question'},
        {'role':'user','content':''},
    ]
    anchor, reply, receipt = wire_messages(fmt, history)
    assert anchor['role'] == receipt['role'] == 'user'
    if fmt == 'anthropic':
        assert anchor['content'].strip()
        assert reply['role'] == 'assistant'
        older, thought, call, newer = reply['content']
        assert call['id'] == receipt['content'][0]['tool_use_id'] == 'native'
        assert receipt['content'][1]['text'] == 'question'
    else:
        assert anchor['parts'][0]['text'].strip()
        assert reply['role'] == 'model'
        older, thought, call, newer = reply['parts']
        assert call['thoughtSignature'] == 'call-signature'
        assert call['functionCall']['id'] == receipt['parts'][0]['functionResponse']['id'] == 'native'
        assert receipt['parts'][1]['text'] == 'question'
    assert (older['text'], newer['text']) == ('older answer', 'latest note')
    assert thought == private


def test_native_tool_keeps_media_only_user_message():
    config = {'api_format':'gemini','provider':'custom','model':'mock','base_url':'https://example.test',
              'max_tokens':512,'_image_inputs':[{'mime_type':'image/png','data':'YQ=='}]}
    *_, body = ModelClient().build_request(config, 'key', 'system', [], '')
    user, = body['contents']
    assert user['role'] == 'user'
    assert user['parts'][-1]['inline_data']['data'] == 'YQ=='
    assert all('text' not in part or part['text'].strip() for part in user['parts'])


def test_native_tool_html_response_and_idle_budgets_use_the_same_policy():
    from avatar.core.artifact_limits import MAX_ENVELOPE_BYTES
    from avatar.models.client import generation_deadline, generation_http_timeout, response_limit

    base = {'max_tokens':512, '_call_purpose':'react'}
    assert generation_deadline(base) == 180 and generation_http_timeout(base).read == 120
    for overrides in (
        {'_call_purpose':'html_generation'},
        *({'_tools':[{'name':name}]} for name in ('create_html','append_html_draft','edit_html')),
    ):
        config = {**base, **overrides}
        assert response_limit(config) == MAX_ENVELOPE_BYTES
        assert response_limit(config, streaming=True) > MAX_ENVELOPE_BYTES
        assert generation_deadline(config) == generation_http_timeout(config).read == 600


def test_http_model_addresses_require_explicit_private_allowlist(monkeypatch):
    import socket
    from avatar.models.model_network import PinnedDirectTransport

    async def check():
        monkeypatch.delenv('AVATAR_MODEL_HTTP_ALLOWLIST', raising=False)
        target = httpx.URL('http://gateway.example:8081/v1')
        addresses = ['192.168.82.7', '8.8.8.8']
        monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 8081)) for ip in addresses])
        with pytest.raises(HTTPException, match='422'):
            await ModelClient().connection(target)
        for rule in ('gateway.example:8081', '192.168.82.0/24:8081', '192.168.82.7'):
            monkeypatch.setenv('AVATAR_MODEL_HTTP_ALLOWLIST', rule)
            client = ModelClient(proxy='http://proxy.example')
            ips, transport = await client._connection(target)
            try:
                assert ips == tuple(addresses)
                assert isinstance(transport, PinnedDirectTransport)
            finally:
                await transport.aclose()
        monkeypatch.setenv('AVATAR_MODEL_HTTP_ALLOWLIST', 'gateway.example:8082')
        with pytest.raises(HTTPException):
            await ModelClient().connection(target)
        monkeypatch.setenv('AVATAR_MODEL_HTTP_ALLOWLIST', 'gateway.example')
        for special in ('127.0.0.1','169.254.0.1','0.0.0.0','224.0.0.1','::1','::ffff:127.0.0.1','100.64.0.1'):
            addresses[:] = ['8.8.8.8', special]
            with pytest.raises(HTTPException):
                await ModelClient().connection(target)
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
