# 模块说明：模型流式请求：处理 UTF-8 分片与 SSE，统一可见文本和用量，不暴露模型思考内容。
"""Bounded SSE decoding and provider-neutral visible text deltas."""
import asyncio
from contextlib import aclosing
import logging
from io import StringIO
import json
import httpx
from fastapi import HTTPException
from avatar.models.tools import invalid_tool_call, load_model_json
from avatar.models.client import parse_response, tokens, response_limit, generation_http_timeout, generation_deadline
from avatar.models.diagnostics import transport_failure, unconfirmed_response


def check_size(kind, size, limit):
    if size > limit:
        logging.getLogger(__name__).warning('Model response size limit kind=%s bytes=%d limit=%d',kind,size,limit)
        raise HTTPException(502,f'模型响应超过大小限制（{kind}，{size} / {limit} 字节）')


# 职责：将上游分片字节解析为完整 SSE 帧。
# 输入：response 为 httpx 流式响应，保留未收全的行以兼容中文跨分片。
# 结果与约束：分别限制传输总量与单个事件；每行完整后解码，兼容跨分片的中文和 CRLF。
async def frames(response, wire_limit, event_limit):
    pending = bytearray()
    data, event = [], ''
    wire_size = event_size = 0
    chunks = response.aiter_bytes()
    async for chunk in chunks:
        wire_size += len(chunk)
        check_size('SSE 传输',wire_size,wire_limit)
        start = 0
        while start < len(chunk):
            newline = chunk.find(b'\n',start)
            end = len(chunk) if newline < 0 else newline + 1
            event_size += end - start
            check_size('SSE 单帧',event_size,event_limit)
            pending.extend(memoryview(chunk)[start:end])
            start = end
            if newline < 0:
                break
            line = pending.decode().rstrip('\r\n')
            pending.clear()
            if not line:
                event_size = 0
                if data == ['[DONE]']:
                    # Let HTTP consume the message terminator and reuse the socket. Never wait for a long-lived SSE tail.
                    try:
                        async with asyncio.timeout(.01):
                            await anext(chunks, None)
                    except (TimeoutError, httpx.HTTPError):
                        pass
                    yield event, '[DONE]'
                    return
                if data:
                    yield event, '\n'.join(data)
                data, event = [], ''
            elif line.startswith('data:'):
                data.append(line[5:].lstrip(' '))
            elif line.startswith('event:'):
                event = line[6:].strip()
    try: buffer = pending.decode()
    except UnicodeDecodeError: raise interrupted() from None
    # 连接结束时若仍有未闭合 SSE 帧，不能将部分事件视为完整成功响应。
    # 部分兼容服务最后一条 JSON 没有空行；交给下游终止标记校验，不能误判为随机断流。
    if buffer.startswith('data:'):data.append(buffer[5:].lstrip(' '))
    elif buffer.strip() and not buffer.startswith(':'):raise interrupted()
    if data:
        raw='\n'.join(data)
        if raw!='[DONE]':
            try: load_model_json(raw)
            except ValueError: raise interrupted() from None
        yield event,raw


# 职责：规范化流式接口累计的 Token 用量。
# 输入：usage 为平台原始用量，fmt 决定字段名。
# 结果与约束：返回 input_tokens、output_tokens、total_tokens；无效数值为 None，仅在两项齐全时计算缺失总数。
def usage_summary(usage, fmt):
    if fmt == 'gemini':
        incoming, outgoing = tokens(usage.get('promptTokenCount')), tokens(usage.get('candidatesTokenCount'))
        total = tokens(usage.get('totalTokenCount'))
    else:
        incoming = tokens(usage.get('input_tokens' if fmt == 'anthropic' else 'prompt_tokens'))
        outgoing = tokens(usage.get('output_tokens' if fmt == 'anthropic' else 'completion_tokens'))
        total = tokens(usage.get('total_tokens'))
    if total is None and incoming is not None and outgoing is not None:
        total = incoming + outgoing
    return {'input_tokens': incoming, 'output_tokens': outgoing, 'total_tokens': total}


def interrupted():
    error = unconfirmed_response(502,'模型响应流在完成前中断')
    error.stream_interrupted = True
    return error


async def response_frames(response, wire_limit, event_limit, complete):
    # After a terminal frame only optional usage remains. Losing that tail cannot invalidate a completed call.
    async with aclosing(frames(response,wire_limit,event_limit)) as source:
        tail_deadline = None
        while True:
            if complete() and tail_deadline is None:
                tail_deadline = asyncio.get_running_loop().time()+1
            try:
                if tail_deadline is None:
                    item = await anext(source,None)
                else:
                    async with asyncio.timeout_at(tail_deadline):
                        item = await anext(source,None)
            except (TimeoutError,httpx.ReadError,httpx.ReadTimeout,httpx.RemoteProtocolError):
                if complete(): return
                raise
            if item is None: return
            yield item


class ToolFragments:
    """Keep interleaved arguments apart until the provider confirms completion."""
    def __init__(self, limit):
        self.size = 0
        self.limit = limit
        self.openai = {}
        self.anthropic = {}
        self.open_blocks = set()
        self.gemini = []

    @staticmethod
    def check_count(calls):
        if len(calls) >= 8:
            raise invalid_tool_call('单批超过八项工具调用',len(calls)+1)

    def account(self, value):
        if not isinstance(value,str):
            value = json.dumps(value,ensure_ascii=False,separators=(',',':'))
        self.size += len(value.encode())
        check_size('有效输出',self.size,self.limit)

    def add_openai(self, delta):
        for part in delta.get('tool_calls') or ():
            index = part.get('index',0)
            if type(index) is not int or index < 0:
                raise ValueError('Invalid tool index')
            if index not in self.openai:
                self.check_count(self.openai)
                self.openai[index] = (StringIO(),StringIO())
            name,args = self.openai[index]
            function = part.get('function') or {}
            if function.get('name'):
                self.account(function['name'])
                name.write(function['name'])
            if function.get('arguments'):
                self.account(function['arguments'])
                args.write(function['arguments'])

    def add_anthropic(self, kind, data):
        index = data.get('index',0)
        block = data.get('content_block') or {}
        delta = data.get('delta') or {}
        if kind=='content_block_start' and block.get('type')=='tool_use':
            if index in self.anthropic:
                raise ValueError('Repeated tool block')
            self.check_count(self.anthropic)
            self.account(block.get('name'))
            self.account(block.get('input',{}))
            self.anthropic[index] = (block.get('name'),block.get('input',{}),StringIO())
            self.open_blocks.add(index)
        elif kind=='content_block_delta' and delta.get('type')=='input_json_delta':
            if index not in self.open_blocks:
                raise ValueError('Missing tool block')
            self.account(delta['partial_json'])
            self.anthropic[index][2].write(delta['partial_json'])
        elif kind=='content_block_stop':
            self.open_blocks.discard(index)

    def response(self, fmt, text, usage, reason, thinking, refusal):
        if fmt=='openai':
            tools=[{'function':{'name':name.getvalue(),'arguments':args.getvalue()}} for _,(name,args) in sorted(self.openai.items())]
            message={'content':text,'tool_calls':tools,'reasoning_content':thinking,'refusal':refusal}
            return {'choices':[{'message':message,'finish_reason':reason}],'usage':usage}
        if fmt=='anthropic':
            blocks=[{'type':'text','text':text}]
            if thinking: blocks.append({'type':'thinking'})
            for _,(name,initial,parts) in sorted(self.anthropic.items()):
                blocks.append({'type':'tool_use','name':name,'input':parts.getvalue() if parts.tell() else initial})
            return {'content':blocks,'stop_reason':reason,'usage':usage}
        parts=[{'text':text},*self.gemini]
        if thinking: parts.append({'thought':True})
        return {'candidates':[{'content':{'parts':parts},'finishReason':reason}],
                'usageMetadata':usage,'promptFeedback':{'blockReason':reason} if refusal else {}}


async def stream_response(model, config, api_key, system, history, message, *, collect=False):
    from avatar.core.execution import check_execution_async
    for attempt in range(6):
        await check_execution_async()
        try:
            async with aclosing(_stream_once(model,config,api_key,system,history,message,collect)) as source:
                async for event in source:
                    yield event
            return
        except HTTPException as error:
            if not getattr(error,'stream_interrupted',False):
                raise
            logging.getLogger(__name__).warning('Model stream interrupted purpose=%s attempt=%d/6',
                                                config.get('_call_purpose','answer'),attempt+1)
            if attempt==5:
                error.stream_exhausted=True
                error.detail='模型响应流中断，5 次自动重试均未取得完整结果。'
                raise
        yield {'type':'status','stage':'stream_retry','reset_reply':True,
               'message':f'模型响应流中断，正在进行第 {attempt+1}/5 次重试'}
        await asyncio.sleep(min(2**attempt,4))


# 职责：请求上游模型并解析为平台无关的可见事件。
# 输入：model 提供请求构建、解析器及传输；其余参数为已审核模型上下文。
# 结果与约束：HTTPS 固定公网 IP 并保留原 Host/SNI，输出连接/生成状态、文本与用量；过滤思考内容，完整 JSON 一次输出，缺终止标记拒绝作为成功结果。
async def _stream_once(model, config, api_key, system, history, message, collect):
    fmt, endpoint, headers, body = model.build_request(config, api_key, system, history, message, streaming=True)
    url = httpx.URL(endpoint)
    model.validate_endpoint(url)
    yield {'type': 'status', 'stage': 'connecting', 'message': '连接模型平台'}
    headers['Host'] = url.netloc.decode()
    timeout = generation_http_timeout(config,streaming=True)
    receiving = False
    try:
        async with asyncio.timeout(generation_deadline(config)), model.session(url, timeout) as (addresses, client):
            yield {'type': 'status', 'stage': 'waiting', 'message': '等待模型响应'}
            async with client.stream('POST', url.copy_with(host=addresses[0]), headers=headers, json=body,
                                     extensions={'sni_hostname': url.host}) as response:
                from avatar.models.diagnostics import check_response,empty_response,request_id
                await check_response(response,api_key)
                receiving = True
                yield {'type': 'status', 'stage': 'generating', 'message': '生成回答'}
                content_limit = response_limit(config)
                if 'text/event-stream' not in response.headers.get('content-type', ''):
                    # 部分兼容平台忽略 stream=true 并返回完整 JSON，此时只显示一次真实完整回答，
                    # 不把完整结果伪装成上游正在逐字生成。
                    payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        check_size('完整 JSON',len(payload)+len(chunk),content_limit)
                        payload.extend(chunk)
                    result = parse_response(fmt,load_model_json(payload),config=config,request_id=request_id(response.headers),api_key=api_key)
                    if collect:
                        yield {'type':'result','result':result}
                        return
                    yield {'type': 'delta', 'text': result.text}
                    yield {'type': 'usage', 'usage': result.usage, **({'finish_reason': result.finish_reason} if config.get('_avatar_agent') else {})}
                    return
                chunks = StringIO()
                fragments = ToolFragments(content_limit)
                usage, finished, thinking, action = {}, False, False, False
                reason,refusal,visible=None,False,False
                async for event, raw in response_frames(response,response_limit(config,True),content_limit,lambda: finished and not fragments.open_blocks):
                    if raw == '[DONE]':
                        finished = finished or (fmt=='openai' and not action)
                        break
                    data = load_model_json(raw)
                    if event == 'error' or data.get('error') or data.get('type') == 'error':
                        from avatar.models.diagnostics import provider_error
                        raise provider_error(200,data.get('error') or data,request_id(response.headers),api_key)
                    text, thought = '', False
                    if fmt == 'openai':
                        if data.get('usage'):
                            usage.update(data['usage'])
                        choices = data.get('choices') or []
                        if choices:
                            choice = next((c for c in choices if c.get('index', 0) == 0), {})
                            delta = choice.get('delta') or {}
                            text = delta.get('content') or ''
                            if isinstance(text, list):
                                refusal |= any(p.get('type')=='refusal' for p in text)
                                text = ''.join(p.get('text', '') for p in text if p.get('type') == 'text')
                            if collect: fragments.add_openai(delta)
                            action |= bool(delta.get('tool_calls') or delta.get('function_call'))
                            thought = bool(delta.get('reasoning_content'))
                            reason=choice.get('finish_reason') or reason
                            refusal|=bool(delta.get('refusal'))
                            finished |= choice.get('finish_reason') is not None
                    elif fmt == 'anthropic':
                        kind = data.get('type', event)
                        if collect: fragments.add_anthropic(kind,data)
                        action |= (data.get('content_block') or {}).get('type')=='tool_use' or (data.get('delta') or {}).get('type')=='input_json_delta'
                        if kind == 'message_start':
                            usage.update(data.get('message', {}).get('usage') or {})
                        elif kind == 'message_delta':
                            reason=(data.get('delta') or {}).get('stop_reason') or reason
                            usage.update(data.get('usage') or {})
                        elif kind == 'content_block_delta':
                            delta = data.get('delta') or {}
                            text = delta.get('text', '') if delta.get('type') == 'text_delta' else ''
                            thought = delta.get('type') == 'thinking_delta'
                        elif kind == 'message_stop':
                            finished = True
                    else:
                        usage.update(data.get('usageMetadata') or {})
                        refusal|=bool((data.get('promptFeedback') or {}).get('blockReason'))
                        reason=(data.get('promptFeedback') or {}).get('blockReason') or reason
                        candidates = data.get('candidates') or []
                        if candidates:
                            parts = candidates[0].get('content', {}).get('parts') or []
                            text = ''.join(p.get('text', '') for p in parts if not p.get('thought'))
                            if collect:
                                for part in parts:
                                    if 'functionCall' in part and not part.get('thought'):
                                        if 'partialArgs' in part['functionCall'] or part['functionCall'].get('willContinue'):
                                            raise ValueError('Unexpected partial function call')
                                        fragments.check_count(fragments.gemini)
                                        fragments.account(part['functionCall'])
                                        fragments.gemini.append({'functionCall':part['functionCall']})
                            action |= any('functionCall' in p for p in parts)
                            thought = any(p.get('thought') for p in parts)
                            reason=candidates[0].get('finishReason') or reason
                            finished |= bool(candidates[0].get('finishReason'))
                    if thought and not thinking:
                        thinking = True
                        yield {'type': 'status', 'stage': 'processing', 'message': '模型正在处理问题'}
                    if text:
                        if not isinstance(text, str):
                            raise ValueError('Invalid text delta')
                        fragments.account(text)
                        visible|=bool(text.strip())
                        if collect: chunks.write(text)
                        else: yield {'type': 'delta', 'text': text}
                if refusal and not finished:
                    raise empty_response(fmt,reason=reason,refusal=True,usage=usage,request_id=request_id(response.headers),config=config)
                if not finished or fragments.open_blocks:
                    raise interrupted()
                if collect:
                    data = fragments.response(fmt,chunks.getvalue(),usage,reason,thinking,refusal)
                    result = parse_response(fmt,data,config=config,request_id=request_id(response.headers),api_key=api_key)
                    yield {'type':'result','result':result}
                    return
                if not visible:raise empty_response(fmt,reason=reason,thinking=thinking and not action,refusal=refusal,usage=usage,request_id=request_id(response.headers),config=config)
                yield {'type': 'usage', 'usage': usage_summary(usage, fmt), **({'finish_reason': reason} if config.get('_avatar_agent') else {})}
    except TimeoutError as error:
        if receiving: raise interrupted() from None
        raise transport_failure(error,total_timeout=generation_deadline(config)) from None
    except httpx.HTTPError as error:
        if receiving and isinstance(error,(httpx.ReadError,httpx.ReadTimeout,httpx.RemoteProtocolError)):
            raise interrupted() from None
        raise transport_failure(error,read_timeout=timeout.read) from None
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise HTTPException(502, '模型流式响应格式与所选接口不匹配')
