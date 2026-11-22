# 模块说明：模型完整请求：统一平台协议、返回值与错误，固定验证后的公网 IP 并保留原 TLS 身份。
"""Provider-neutral text generation over OpenAI, Anthropic and Gemini APIs."""
import asyncio
from contextlib import aclosing
import ipaddress
import json
import socket
from dataclasses import dataclass, replace
import httpx
from fastapi import HTTPException
from avatar.core.http_pool import HttpTransportPool
from avatar.models.effort import apply_effort
from avatar.models.diagnostics import transport_error, transport_failure


# 职责：表示不同模型平台统一后的完整回答结果。
# 输入：text 为可见回答，usage 为输入、输出及总 Token 用量字典。
# 结果与约束：不可变数据对象；未知用量保持 None，不将缺失用量伪造为零。
@dataclass(frozen=True)
class ModelResult:
    text: str
    usage: dict
    tool_calls: tuple = ()
    finish_reason: str | None = None
    material_sources: tuple = ()
    # None = ordinary reply, empty tuple = explicit clarification, otherwise verified originals.
    material_delivery: tuple | None = None
    contact_draft: dict | None = None


def accumulate_usage(total, usage):
    current = usage or {}
    fields = ('input_tokens', 'output_tokens', 'total_tokens')
    if total is None:
        return {key: current.get(key) for key in fields}
    return {key: total[key] + current[key] if total[key] is not None and current.get(key) is not None else None
            for key in fields}


# 职责：解析并校验模型服务域名对应的公网 IP。
# 输入：host、port 为原模型地址；DNS 查询在后台线程执行并设置 15 秒等待上限。
# 结果与约束：任一地址非公网即拒绝，防止 SSRF；IPv4 优先返回候选集，解析失败返回脱敏错误。
async def resolve_public_all(host, port):
    try:
        addresses = await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM), 15)
    except (OSError, asyncio.TimeoutError):
        raise transport_error(502, "无法解析模型平台地址，请检查 API 地址及服务器网络")
    ips = list(dict.fromkeys(row[4][0] for row in addresses))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise HTTPException(422, "模型 API 地址必须指向公网服务，不能访问本机或内网地址")
    return sorted(ips,key=lambda ip:':' in ip)


async def resolve_public(host,port):return (await resolve_public_all(host,port))[0]


# 职责：支持运维配置 DNS 别名的公网解析器。
# 输入：别名只替代查询域名，不改变请求 URL、Host、TLS SNI 或证书验证对象。
# 结果与约束：仍执行公网检查，不硬编码目标 IP，也不在此建立代理连接。
class ModelResolver:
    """Operator-configured DNS alias; HTTP identity always remains the API host.

    An alias changes only DNS lookup, never Host, TLS verification or API URLs.
    Public IP checks still apply; no IP is hardcoded and no proxy is used here.
    """
    # 职责：校验并规范化运维配置的域名别名映射。
    # 输入：aliases 为可选域名到域名的字典，不接受地址、路径或其他对象类型。
    # 结果与约束：保存去末尾点和小写的映射，非法域名或结构抛出 ValueError。
    def __init__(self, aliases=None):
        import re
        aliases = {} if aliases is None else aliases
        domain = re.compile(r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z0-9-]{2,63}\.?$")
        if not isinstance(aliases, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or not domain.fullmatch(k) or not domain.fullmatch(v)
            for k, v in aliases.items()
        ):
            raise ValueError("AVATAR_MODEL_DNS_ALIASES 必须是 API 域名到 DNS 别名的 JSON 对象")
        self.aliases = {k.rstrip('.').lower(): v.rstrip('.').lower() for k, v in aliases.items()}

    # 职责：异步解析模型原域名或其配置别名。
    # 输入：host 为 API 原域名，port 为请求端口。
    # 结果与约束：返回通过 resolve_public 校验的 IP；调用方仍须保留原 HTTP 与 TLS 身份。
    async def __call__(self, host, port):
        return await resolve_public(self.aliases.get(host.rstrip('.').lower(), host), port)

    async def candidates(self,host,port):
        return await resolve_public_all(self.aliases.get(host.rstrip('.').lower(),host),port)


# 职责：校验上游报告的 Token 用量是否为有效整数。
# 输入：value 可为缺失或任意上游 JSON 值。
# 结果与约束：只返回非负整数，排除布尔值、负数和其他类型；未知用量返回 None。
def tokens(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


# 职责：解析三种模型接口的完整 JSON 回答与用量。
# 输入：fmt 为接口格式，data 为上游 JSON；OpenAI、Anthropic、Gemini 各自读取对应字段。
# 结果与约束：返回 ModelResult，过滤 Gemini 思考片段；空回答报错，用量优先采用上游总数，无总数时仅在输入输出齐全后求和。
def parse_response(fmt, data, *, config=None, request_id=None, api_key=''):
    if data.get('error'):
        from avatar.models.diagnostics import provider_error,contextual
        raise contextual(provider_error(200,data['error'],request_id,api_key),config or {})
    if fmt == "openai":
        openai_message=(data.get('choices') or [{}])[0].get('message') or {}
        text=openai_message.get('content','')
        refused_part=isinstance(text,list) and any(p.get('type')=='refusal' for p in text)
        if isinstance(text, list):
            text = "\n".join(p.get("text", "") for p in text if p.get("type") == "text")
        usage = data.get("usage") or {}
        incoming, outgoing = tokens(usage.get("prompt_tokens")), tokens(usage.get("completion_tokens"))
    elif fmt == "anthropic":
        text = "\n".join(p.get("text", "") for p in data.get("content",[]) if p.get("type") == "text")
        usage = data.get("usage") or {}
        incoming, outgoing = tokens(usage.get("input_tokens")), tokens(usage.get("output_tokens"))
    else:
        text = "\n".join(p.get("text", "") for p in ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts",[]) if not p.get("thought"))
        usage = data.get("usageMetadata") or {}
        incoming, outgoing = tokens(usage.get("promptTokenCount")), tokens(usage.get("candidatesTokenCount"))
    reported_total = tokens(usage.get("total_tokens", usage.get("totalTokenCount")))
    total = reported_total if reported_total is not None else incoming+outgoing if incoming is not None and outgoing is not None else None
    model_usage = {"input_tokens": incoming, "output_tokens": outgoing, "total_tokens": total}
    reason=((data.get('choices') or [{}])[0].get('finish_reason') if fmt=='openai' else
            data.get('stop_reason') if fmt=='anthropic' else (data.get('candidates') or [{}])[0].get('finishReason'))
    calls=()
    # Distinguish a missing action after private reasoning from a malformed proposed action.
    if config and config.get('_tools'):
        from avatar.models.tools import parse_tools
        try:
            calls=tuple(parse_tools(fmt,data,config['_tools'],required=bool(text and str(text).strip()) and not config.get('_optional_tools'),batch=bool(config.get('_batch_tools'))))
        except HTTPException as error:
            error.model_usage=model_usage
            if str(reason or '').lower() in ('length','max_tokens','max_output_tokens'):
                error.output_limited = True
                error.invalid_tool_call = False
            raise
    if not calls and (not isinstance(text, str) or not text.strip()):
        from avatar.models.diagnostics import empty_response
        if fmt=='openai':
            choice=(data.get('choices') or [{}])[0];message=choice.get('message') or {}
            reason=choice.get('finish_reason');thinking=bool(message.get('reasoning_content'));refusal=bool(message.get('refusal')) or refused_part
        elif fmt=='anthropic':
            reason=data.get('stop_reason');thinking=any(p.get('type')=='thinking' for p in data.get('content',[]));refusal=reason=='refusal'
        else:
            candidate=(data.get('candidates') or [{}])[0];reason=candidate.get('finishReason') or (data.get('promptFeedback') or {}).get('blockReason')
            thinking=any(p.get('thought') for p in (candidate.get('content') or {}).get('parts',[]));refusal=bool((data.get('promptFeedback') or {}).get('blockReason'))
        if (config or {}).get('_call_purpose')=='skill_selection' and reason in ('stop','end_turn','STOP') and not thinking and not refusal and (text is None or isinstance(text,str)):
            text='null'
        else:
            if fmt=='openai':
                action=message.get('tool_calls') or message.get('function_call')
            elif fmt=='anthropic':
                action=any(p.get('type')=='tool_use' for p in data.get('content',[]))
            else:
                action=any('functionCall' in p for p in (candidate.get('content') or {}).get('parts',[]))
            if (config and config.get('_tools') and not config.get('_optional_tools') and
                    not thinking and not refusal and not action and str(reason or '').lower() in ('','stop','end_turn','stop_sequence')):
                try:
                    parse_tools(fmt,data,config['_tools'],required=True)
                except HTTPException as error:
                    error.model_usage=model_usage
                    raise
            raise empty_response(fmt,reason=reason,thinking=thinking and not action,refusal=refusal,usage=usage,request_id=request_id,config=config)
    # 分身可能在下一次完整响应续写，保留两段交界处的空格、换行与代码缩进。
    visible = (text or '') if (config or {}).get('_avatar_agent') else (text or '').strip()
    return ModelResult(visible, model_usage, calls, reason)


# 职责：将上游 HTTP 状态转换为可展示的模型错误。
# 输入：status 为平台响应状态码。
# 结果与约束：成功无返回；区分 Key 权限、额度限流、参数不兼容与平台故障，不透传上游响应体或自动重试计费请求。
def check_status(status):
    if not 200<=status<300:
        from avatar.models.diagnostics import provider_error
        raise provider_error(status,{})


# 按 token 预算分别限制有效内容和 SSE 传输；每个 token 可能单独携带完整的 JSON 事件头。
def response_limit(config,streaming=False):
    wire_allowance = min(64*1024*1024,max(8*1024*1024,config['max_tokens']*2048))
    if config.get('_call_purpose') == 'html_generation' or any(tool.get('name') in ('create_html','create_animation') for tool in config.get('_tools') or ()):
        from avatar.files.parsing.animation import MAX_ENVELOPE_BYTES
        # HTML is nested inside JSON tool arguments. Escapes and streaming headers do not count toward file size.
        return MAX_ENVELOPE_BYTES + (wire_allowance if streaming else 0)
    if streaming:
        return wire_allowance
    return min(64*1024*1024,max(2*1024*1024,config['max_tokens']*32))


def generation_deadline(config):
    return 600 if config.get('_call_purpose') in ('html_generation','animation') else 180


def generation_http_timeout(config, *, streaming=False):
    purpose = config.get('_call_purpose')
    read = 120
    if purpose in ('html_generation','animation'):
        # A non-streaming provider may generate the entire document before sending its first byte.
        read = 240 if streaming and purpose == 'animation' else generation_deadline(config)
    return httpx.Timeout(120, connect=10, read=read)


# 职责：提供多平台完整与流式文本生成的统一客户端。
# 输入：可注入 transport 进行隔离测试，resolver 验证公网地址，proxy 仅使用显式配置。
# 结果与约束：HTTPS、DNS 固定、原 Host/SNI、大小和超时限制共同保护请求；关闭环境代理与自动重定向。
class ModelClient:
    # 职责：保存模型传输、DNS 解析器和可选代理配置。
    # 输入：transport 可替换网络层，resolver 为异步解析函数，proxy 为显式 HTTP/HTTPS 代理 URL。
    # 结果与约束：非法代理结构抛出 ValueError；默认直接连接，未在构造时发送模型请求。
    def __init__(self, transport=None, resolver=resolve_public, proxy=None):
        if proxy:
            from urllib.parse import urlsplit
            parsed = urlsplit(proxy)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment:
                raise ValueError("AVATAR_MODEL_PROXY 必须为 HTTP/HTTPS 代理地址")
        self.transport, self.resolver, self.proxy = transport, resolver, proxy
        self.http_pool = HttpTransportPool()

    def validate_endpoint(self,url):
        if url.scheme not in ('http','https'):
            raise HTTPException(422,'模型 API 地址仅支持 HTTP 或 HTTPS')
        if not url.host or url.userinfo or url.fragment:
            raise HTTPException(422,'模型 API 地址必须包含主机，且不能内嵌凭据或片段')

    @staticmethod
    def validate_http_ip(value):
        ip=ipaddress.ip_address(value)
        ip=getattr(ip,'ipv4_mapped',None) or ip
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise HTTPException(422,'模型 HTTP 地址不能指向回环、链路本地或保留地址')
        return str(ip)

    def session(self, url, timeout):
        self.validate_endpoint(url)
        return self.http_pool.session(url, self._connection, timeout, proxy=bool(self.proxy),
                                      injected=self.transport is not None)

    async def connection(self, url):
        addresses, transport = await self._connection(url)
        return addresses[0], transport

    async def _connection(self,url):
        self.validate_endpoint(url)
        if url.scheme=='http':
            # 允许动态内网地址，解析后固定连接节点，不跟随重定向或环境代理。
            try:ips=[str(ipaddress.ip_address(url.host))]
            except ValueError:
                try:
                    addresses=await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo,url.host,url.port or 80,type=socket.SOCK_STREAM),15)
                except (OSError,TimeoutError):
                    raise transport_error(502,'无法解析模型 HTTP 服务地址') from None
                ips=list(dict.fromkeys(item[4][0] for item in addresses))
            if not ips:raise transport_error(502,'模型 HTTP 服务地址没有可用节点')
            for ip in ips:self.validate_http_ip(ip)
        elif self.transport is not None:ips=[await self.resolver(url.host,url.port or 443)]
        elif hasattr(self.resolver,'candidates'):ips=await self.resolver.candidates(url.host,url.port or 443)
        elif self.resolver is resolve_public:ips=await resolve_public_all(url.host,url.port or 443)
        else:ips=[await self.resolver(url.host,url.port or 443)]
        if self.transport is not None:return tuple(ips),self.transport
        from avatar.models.model_network import PinnedDirectTransport,DirectFirstTransport
        return tuple(ips),DirectFirstTransport(ips,url.host,self.proxy) if self.proxy else PinnedDirectTransport(ips,url.host)

    # 职责：在总超时范围内生成完整模型回答。
    # 输入：config、api_key、system、history、message 分别为已审核配置、凭据、身份提示、历史和问题。
    # 结果与约束：返回 ModelResult；通常 180 秒，HTML 制作与动画分镜 600 秒，超时返回 504 并提示可能计费，不自动重试。
    async def generate(self, config, api_key, system, history, message):
        from avatar.core.execution import check_execution_async
        await check_execution_async()
        try:
            try:
                return await self._generate(config, api_key, system, history, message)
            except HTTPException as first:
                if not getattr(first,'thinking_only',False):
                    raise
                previous=first.model_usage
            await check_execution_async()
            try:
                result=await self._generate({**config,'_recover_thinking':True}, api_key, self.recovery_prompt(system,config), history, message)
            except HTTPException as error:
                error.model_usage=accumulate_usage(previous,getattr(error,'model_usage',None))
                error.recovery_attempted=True
                raise
            return replace(result,usage=accumulate_usage(previous,result.usage))
        except HTTPException as exc:
            from avatar.models.diagnostics import contextual
            error=contextual(exc,config)
            import logging
            logging.getLogger(__name__).warning('Model request failed: %s',error.detail)
            raise error from None
        except TimeoutError as error:
            from avatar.models.diagnostics import contextual
            raise contextual(transport_failure(error,total_timeout=generation_deadline(config)),config) from None

    @staticmethod
    def recovery_prompt(system, config):
        return system + ('\n请直接返回一个符合声明的工具调用。' if config.get('_tools') and not config.get('_optional_tools')
                         else '\n本步需要可见正文或有效工具调用，请直接交付结果。')

    # 职责：将统一上下文转换为对应平台 URL、请求头和 JSON。
    # 输入：config 决定格式与模型，api_key 仅写鉴权头，system/history/message 构成上下文，streaming 控制流式选项。
    # 结果与约束：返回格式、端点、请求头和请求体四元组；Gemini 模型名进行路径编码，OpenAI 流式请求启用用量汇报。
    def build_request(self, config, api_key, system, history, message, streaming=True):
        from avatar.models.limits import check_context
        check_context(config,system,history,message)
        fmt = config["api_format"]
        conversation = [{"role": m["role"], "content": m["content"]} for m in history]
        conversation.append({"role": "user", "content": message})
        images=config.get('_image_inputs',[])
        audio=config.get('_audio_inputs',[])
        if audio and (not config.get('audio_enabled') or fmt not in ('openai','gemini')):
            raise HTTPException(422, '当前模型配置或接口不支持音频输入')
        if (images or audio) and fmt in ('openai','anthropic'):
            parts=[{'type':'text','text':message}]
            for image in images:
                if fmt=='openai':parts.append({'type':'image_url','image_url':{'url':'data:'+image['mime_type']+';base64,'+image['data']}})
                else:parts.append({'type':'image','source':{'type':'base64','media_type':image['mime_type'],'data':image['data']}})
            parts.extend({'type':'input_audio','input_audio':{'data':item['data'],'format':'wav'}} for item in audio)
            conversation[-1]['content']=parts
        base = config["base_url"].rstrip("/")
        if fmt == "openai":
            endpoint = base + "/chat/completions"
            headers = {"Authorization": "Bearer " + api_key}
            limit_field = "max_completion_tokens" if config["provider"] == "openai" else "max_tokens"
            body = {"model": config["model"], "messages": [{"role": "system", "content": system}]+conversation,
                    limit_field: config["max_tokens"], "stream": streaming}
            if (config.get('_recover_thinking') or config.get('_brief_visual_parse') or config.get('_call_purpose') in ('skill_execution','task_planning','material_selection','agent_intent','delivery_review')
                    or (config.get('_tools') and config.get('_call_purpose') in ('file_read','rag_query_construction'))) and not config.get('reasoning_effort') and config['provider']=='custom' and config['model'].split('/')[-1].lower().startswith('qwen'):
                body['enable_thinking']=False
                body['chat_template_kwargs']={'enable_thinking':False}
            if config['provider']=='deepseek':
                from avatar.models.limits import limits
                if limits('deepseek',config['model']):
                    # 平台未提供思考预算配置，显式关闭默认思考，避免吞掉小输出预算而没有可见回答。
                    body['thinking']={'type':'disabled'}
                    if config.get('_file_search'):body['response_format']={'type':'json_object'}
        elif fmt == "anthropic":
            endpoint = base + "/messages"
            headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
            body = {"model": config["model"], "system": system, "messages": conversation, "max_tokens": config["max_tokens"], "stream": streaming}
        elif fmt == "gemini":
            from urllib.parse import quote
            model = config["model"].removeprefix("models/")
            endpoint = base + "/models/" + quote(model, safe="") + (":streamGenerateContent?alt=sse" if streaming else ":generateContent")
            headers = {"x-goog-api-key": api_key}
            body = {"systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]} for m in conversation],
                    "generationConfig": {"maxOutputTokens": config["max_tokens"]}}
            if images:body["contents"][-1]["parts"].extend({"inline_data":{"mime_type":image["mime_type"],"data":image["data"]}} for image in images)
            if audio:body["contents"][-1]["parts"].extend({"inline_data":{"mime_type":item["mime_type"],"data":item["data"]}} for item in audio)
        else:
            raise HTTPException(422, "不支持的模型接口格式")
        apply_effort(config, body)
        if streaming and fmt == "openai":
            body["stream_options"] = {"include_usage": True}
        if config.get('_tools'):
            from avatar.models.tools import configure_tools
            configure_tools(fmt,body,config['_tools'],required=not config.get('_optional_tools'),batch=bool(config.get('_batch_tools')))
        return fmt, endpoint, headers, body

    # 职责：在总超时内转发统一格式的上游流式事件。
    # 输入：参数与 generate 一致，实际解析委托 model_stream.stream_response。
    # 结果与约束：异步 yield 状态、可见文本及用量；通常总时限 180 秒，HTML 制作与动画分镜最多 600 秒，aclosing 保证取消时关闭上游生成器。
    async def stream(self, config, api_key, system, history, message):
        from avatar.core.execution import check_execution_async
        await check_execution_async()
        from avatar.models.model_stream import stream_response
        try:
            previous=None
            emitted=False
            for attempt in range(2):
                await check_execution_async()
                try:
                    async with aclosing(stream_response(self, config, api_key, system, history, message)) as source:
                        async for event in source:
                            if event.get('reset_reply'): emitted=False
                            if event['type']=='delta' and event.get('text'):
                                emitted=True
                            if attempt and event['type']=='usage':
                                event={**event,'usage':accumulate_usage(previous,event['usage'])}
                            yield event
                    return
                except HTTPException as error:
                    if attempt:
                        error.model_usage=accumulate_usage(previous,getattr(error,'model_usage',None))
                        error.recovery_attempted=True
                        raise
                    if emitted or not getattr(error,'thinking_only',False):
                        raise
                    previous=error.model_usage
                    config={**config,'_recover_thinking':True}
                    system=self.recovery_prompt(system,config)
        except HTTPException as exc:
            from avatar.models.diagnostics import contextual
            error=contextual(exc,config)
            import logging
            logging.getLogger(__name__).warning('Model request failed: %s',error.detail)
            raise error from None
        except TimeoutError as error:
            from avatar.models.diagnostics import contextual
            raise contextual(transport_failure(error,total_timeout=generation_deadline(config)),config) from None

    # 职责：执行固定公网 IP 的 HTTPS 模型请求并解析完整回答。
    # 输入：参数为 build_request 所需上下文；DNS 返回值在发起请求前校验。
    # 结果与约束：保留原 Host/SNI，不跟随重定向、不继承代理；响应按输出预算限制为 2–64 MiB，连接与读超时分别设置，错误映射为脱敏 HTTPException。
    async def _generate(self, config, api_key, system, history, message):
        from avatar.models.model_stream import stream_response
        async with aclosing(stream_response(self,config,api_key,system,history,message,collect=True)) as events:
            async for event in events:
                if event['type']=='result':
                    return event['result']
        raise HTTPException(502,'模型流缺少完成回执')
