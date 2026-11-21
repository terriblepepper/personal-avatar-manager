# 模块说明：模型流式请求：处理 UTF-8 分片与 SSE，统一可见文本和用量，不暴露模型思考内容。
"""Bounded SSE decoding and provider-neutral visible text deltas."""
import asyncio
import codecs
import json
import httpx
from fastapi import HTTPException
from avatar.models.client import parse_response, tokens, response_limit
from avatar.models.diagnostics import transport_failure, unconfirmed_response

LIMIT = 2 * 1024 * 1024


# 职责：将上游分片字节解析为完整 SSE 帧。
# 输入：response 为 httpx 流式响应，使用增量 UTF-8 解码以兼容中文跨分片。
# 结果与约束：异步返回事件名与 data 文本，支持 CRLF 和多行 data；总大小由调用者按输出预算限制，默认 2 MiB、最高 64 MiB，未完成帧结束视为流中断。
async def frames(response,limit=LIMIT):
    decoder = codecs.getincrementaldecoder('utf-8')()
    buffer, data, event, size = '', [], '', 0
    chunks = response.aiter_bytes()
    async for chunk in chunks:
        size += len(chunk)
        if size > limit:
            raise HTTPException(502, '模型响应超过大小限制')
        buffer += decoder.decode(chunk)
        while '\n' in buffer:
            line, buffer = buffer.split('\n', 1)
            line = line.rstrip('\r')
            if not line:
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
    buffer += decoder.decode(b'', final=True)
    # 连接结束时若仍有未闭合 SSE 帧，不能将部分事件视为完整成功响应。
    # 部分兼容服务最后一条 JSON 没有空行；交给下游终止标记校验，不能误判为随机断流。
    if buffer.startswith('data:'):data.append(buffer[5:].lstrip(' '))
    elif buffer.strip() and not buffer.startswith(':'):raise unconfirmed_response(502,'模型流式响应中断，已显示内容可能不完整，请求未重发')
    if data:yield event,'\n'.join(data)


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


# 职责：请求上游模型并解析为平台无关的可见事件。
# 输入：model 提供请求构建、解析器及传输；其余参数为已审核模型上下文。
# 结果与约束：HTTPS 固定公网 IP 并保留原 Host/SNI，输出连接/生成状态、文本与用量；过滤思考内容，完整 JSON 一次输出，缺终止标记拒绝作为成功结果。
async def stream_response(model, config, api_key, system, history, message):
    fmt, endpoint, headers, body = model.build_request(config, api_key, system, history, message, streaming=True)
    url = httpx.URL(endpoint)
    model.validate_endpoint(url)
    yield {'type': 'status', 'stage': 'connecting', 'message': '连接模型平台'}
    headers['Host'] = url.netloc.decode()
    timeout = httpx.Timeout(120, connect=10, read=240 if config.get('_call_purpose') == 'animation' else 120)
    try:
        async with model.session(url, timeout) as (addresses, client):
            yield {'type': 'status', 'stage': 'waiting', 'message': '等待模型响应'}
            async with client.stream('POST', url.copy_with(host=addresses[0]), headers=headers, json=body,
                                     extensions={'sni_hostname': url.host}) as response:
                from avatar.models.diagnostics import check_response,empty_response,request_id
                await check_response(response,api_key)
                yield {'type': 'status', 'stage': 'generating', 'message': '生成回答'}
                if 'text/event-stream' not in response.headers.get('content-type', ''):
                    # 部分兼容平台忽略 stream=true 并返回完整 JSON，此时只显示一次真实完整回答，
                    # 不把完整结果伪装成上游正在逐字生成。
                    payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload) > response_limit(config):
                            raise HTTPException(502, '模型响应超过大小限制')
                    result = parse_response(fmt,json.loads(payload),config=config,request_id=request_id(response.headers),api_key=api_key)
                    yield {'type': 'delta', 'text': result.text}
                    yield {'type': 'usage', 'usage': result.usage, **({'finish_reason': result.finish_reason} if config.get('_avatar_agent') else {})}
                    return
                usage, finished, thinking, action = {}, False, False, False
                reason,refusal,visible=None,False,False
                async for event, raw in frames(response,response_limit(config,True)):
                    if raw == '[DONE]':
                        finished = True
                        break
                    data = json.loads(raw)
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
                            action |= bool(delta.get('tool_calls') or delta.get('function_call'))
                            thought = bool(delta.get('reasoning_content'))
                            reason=choice.get('finish_reason') or reason
                            refusal|=bool(delta.get('refusal'))
                            finished |= choice.get('finish_reason') is not None
                    elif fmt == 'anthropic':
                        kind = data.get('type', event)
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
                        visible|=bool(text.strip())
                        yield {'type': 'delta', 'text': text}
                if not finished and not visible and (reason or refusal):raise empty_response(fmt,reason=reason,thinking=thinking,refusal=refusal,usage=usage,request_id=request_id(response.headers),config=config)
                if not finished:
                    raise unconfirmed_response(502, '模型流式响应中断，已显示内容可能不完整，请求未重发')
                if not visible:raise empty_response(fmt,reason=reason,thinking=thinking and not action,refusal=refusal,usage=usage,request_id=request_id(response.headers),config=config)
                yield {'type': 'usage', 'usage': usage_summary(usage, fmt), **({'finish_reason': reason} if config.get('_avatar_agent') else {})}
    except httpx.HTTPError as error:
        raise transport_failure(error) from None
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise HTTPException(502, '模型流式响应格式与所选接口不匹配')
