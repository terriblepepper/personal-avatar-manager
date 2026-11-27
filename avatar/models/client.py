# 模块说明：模型完整请求：统一平台协议、返回值与错误，固定验证后的公网 IP 并保留原 TLS 身份。
"""Provider-neutral text generation over OpenAI, Anthropic and Gemini APIs."""
import asyncio
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import replace
from fastapi import HTTPException
from avatar.core.http_pool import HttpTransportPool
from avatar.core.network import address, is_public, resolve_public, resolve_public_all
from avatar.models.model_network import DirectFirstTransport, ModelResolver, PinnedDirectTransport, http_allowlist, resolve_model_http
from avatar.models.codec import ModelResult, build_request, parse_response, tokens
from avatar.models.limits import generation_deadline, generation_http_timeout, response_limit
from avatar.models.model_stream import stream_response
from avatar.models.diagnostics import PURPOSES, transport_error, transport_failure, contextual
from avatar.core.execution import check_execution_async


collected_model_status = ContextVar('collected_model_status', default=None)


def accumulate_usage(total, usage):
    # A rejected step has no new receipt. A provider receipt with unknown fields is different.
    if usage is None:
        return total
    current = usage
    fields = ('input_tokens', 'output_tokens', 'total_tokens')
    if total is None:
        return {key: current.get(key) for key in fields}
    return {key: total[key] + current[key] if total.get(key) is not None and current.get(key) is not None else None
            for key in fields}


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
        self.http_allowlist = http_allowlist()

    def validate_endpoint(self,url):
        if url.scheme not in ('http','https'):
            raise HTTPException(422,'模型 API 地址仅支持 HTTP 或 HTTPS')
        if url.port is not None and not 1 <= url.port <= 65535:
            raise HTTPException(422, '模型 API 端口必须在 1 至 65535 之间')
        if not url.host or url.userinfo or url.fragment:
            raise HTTPException(422,'模型 API 地址必须包含主机，且不能内嵌凭据或片段')

    def session(self, url, timeout):
        self.validate_endpoint(url)
        return self.http_pool.session(url, self._connection, timeout, proxy=bool(self.proxy),
                                      injected=self.transport is not None)

    async def connection(self, url):
        addresses, transport = await self._connection(url)
        return addresses[0], transport

    async def _connection(self,url):
        self.validate_endpoint(url)
        port = url.port or (443 if url.scheme == 'https' else 80)
        try:
            if url.scheme == 'http':
                ips = await resolve_model_http(url.host, port, self.http_allowlist)
            elif hasattr(self.resolver, 'candidates'):
                ips = await self.resolver.candidates(url.host, port)
            elif self.resolver is resolve_public:
                ips = await resolve_public_all(url.host, port)
            else:
                ips = [await self.resolver(url.host, port)]
        except HTTPException as error:
            if error.status_code == 502:
                raise transport_error(502, error.detail) from None
            raise
        if url.scheme == 'https' and not all(is_public(address(ip)) for ip in ips):
            raise HTTPException(422, '模型 HTTPS 地址必须指向公网服务')
        if self.transport is not None:
            return tuple(ips), self.transport
        # HTTPS proxies CONNECT to a pinned IP. A forwarding HTTP proxy may resolve Host again.
        if self.proxy and url.scheme == 'https':
            transport = DirectFirstTransport(ips, url.host, self.proxy)
        else:
            transport = PinnedDirectTransport(ips, url.host)
        return tuple(ips), transport

    # 职责：在总超时范围内生成完整模型回答。
    # 输入：config、api_key、system、history、message 分别为已审核配置、凭据、身份提示、历史和问题。
    # 结果与约束：返回 ModelResult；完整响应等待通常为 180 秒，HTML 为 600 秒；SSE 按有效输出续期。
    async def generate(self, config, api_key, system, history, message):
        try:
            await check_execution_async()
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
        except asyncio.CancelledError as exc:
            exc.stop_reason = 'cancelled'
            raise
        except HTTPException as exc:
            error=contextual(exc,config)
            import logging
            logging.getLogger(__name__).warning('Model request failed: %s',error.detail)
            raise error from None
        except TimeoutError as error:
            raise contextual(transport_failure(error,total_timeout=generation_deadline(config)),config) from None

    @staticmethod
    def recovery_prompt(system, config):
        return system + ('\n请直接返回一个符合声明的工具调用。' if config.get('_tools') and not config.get('_optional_tools')
                         else '\n本步需要可见正文或有效工具调用，请直接交付结果。')

    build_request = staticmethod(build_request)

    # 职责：在总超时内转发统一格式的上游流式事件。
    # 输入：参数与 generate 一致，实际解析委托 model_stream.stream_response。
    # 结果与约束：异步 yield 状态、可见文本及用量；普通 SSE 有效输出间隔上限 180 秒，HTML 制作与动画分镜为 600 秒，aclosing 保证取消时关闭上游生成器。
    async def stream(self, config, api_key, system, history, message):
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
        except asyncio.CancelledError as exc:
            exc.stop_reason = 'cancelled'
            raise
        except HTTPException as exc:
            error=contextual(exc,config)
            import logging
            logging.getLogger(__name__).warning('Model request failed: %s',error.detail)
            raise error from None
        except TimeoutError as error:
            raise contextual(transport_failure(error,total_timeout=generation_deadline(config)),config) from None

    # 职责：执行固定公网 IP 的 HTTPS 模型请求并解析完整回答。
    # 输入：参数为 build_request 所需上下文；DNS 返回值在发起请求前校验。
    # 结果与约束：保留原 Host/SNI，不跟随重定向、不继承代理；响应按输出预算限制为 2–64 MiB，连接与读超时分别设置，错误映射为脱敏 HTTPException。
    async def _generate(self, config, api_key, system, history, message):
        report = collected_model_status.get()
        purpose = PURPOSES.get(config.get('_call_purpose', 'answer'), '模型调用')
        attempt = ''
        async with aclosing(stream_response(self,config,api_key,system,history,message,collect=True)) as events:
            async for event in events:
                if event['type']=='result':
                    return event['result']
                if report and event.get('stage') in ('waiting', 'processing', 'model_progress', 'stream_retry'):
                    message = f"{purpose}{attempt} · {event['message']}"
                    if event['stage'] == 'stream_retry':
                        attempt = f"（重试 {event['retry']}/5）"
                        message = f"{purpose} · {event['message']}"
                    await report(message)
        raise HTTPException(502,'模型流缺少完成回执')
