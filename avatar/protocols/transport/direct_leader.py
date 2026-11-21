"""WIT Direct 任务 RPC 客户端。每次连接按证书 AIC 核验接收方后才发送正文。"""
import asyncio
import json
import ssl
from urllib.parse import urlsplit

import httpcore
from wit.acps.aip.direct import BaseDirectLeaderComponent, DirectAuthorizationError, DirectProtocolError, DirectRequest
from wit.acps.aip.direct.protocol import validate_direct_request, validate_jsonrpc_response, validate_result
from wit.acps.identity import current_local_entity, peer_aic_from_certificate

from avatar.protocols.transport.tls_listener import load_cert_chain


class DirectLeaderComponent(BaseDirectLeaderComponent):
    def __init__(self, directory):
        super().__init__()
        self.directory = directory
        self._tls_entity = None
        self._tls = None

    def _client_tls(self, entity):
        if self._tls_entity != entity:
            material = entity.material
            tls = ssl.create_default_context(cadata=material.trust_bundle)
            tls.hostname_checks_common_name = False
            tls.minimum_version = ssl.TLSVersion.TLSv1_3
            load_cert_chain(tls, material.certificate, material.private_key)
            self._tls_entity, self._tls = entity, tls
        return self._tls

    async def _rpc(self, command, *, context, endpoint=None):
        address = urlsplit(endpoint or '')
        if address.scheme != 'https' or not address.hostname or address.username is not None or address.password is not None or address.fragment:
            raise DirectProtocolError('需要不含凭据和片段的 HTTPS 端点')
        entity = await self.directory.select(context.local_aic)
        bound = current_local_entity()
        if entity.aic != context.local_aic or (bound is not None and bound != entity):
            raise DirectAuthorizationError('发送方授权身份已变更')
        request = DirectRequest(context.peer_aic, endpoint, 'rpc', {'command': command})
        validate_direct_request(request)
        url = httpcore.URL(endpoint)
        payload = json.dumps(request.document(), allow_nan=False, separators=(',', ':')).encode()
        try:
            async with asyncio.timeout(180):
                stream = await httpcore.AnyIOBackend().connect_tcp(address.hostname, address.port or 443, timeout=10)
                try:
                    stream = await stream.start_tls(self._client_tls(entity), server_hostname=address.hostname, timeout=10)
                    session = stream.get_extra_info('ssl_object')
                    if session is None or peer_aic_from_certificate(session.getpeercert(binary_form=True)) != context.peer_aic:
                        raise DirectAuthorizationError('接收方证书 AIC 与授权目标不符')
                    connection = httpcore.AsyncHTTP11Connection(url.origin, stream)
                    headers = {'Host': address.netloc, 'Content-Type': 'application/json'}
                    async with connection.stream('POST', url, content=payload, headers=headers) as response:
                        if response.status != 200:
                            raise DirectProtocolError(f'分身 RPC 返回 HTTP {response.status}')
                        raw = bytearray()
                        async for chunk in response.aiter_stream():
                            if len(raw) + len(chunk) > 1048576:
                                raise DirectProtocolError('分身 RPC 回复超过 1 MiB')
                            raw.extend(chunk)
                finally:
                    await stream.aclose()
        except httpcore.TimeoutException as exc:
            raise TimeoutError('分身 RPC 超时') from exc
        document = json.loads(raw)
        validate_jsonrpc_response(document, request.request_id)
        result = document['result']
        validate_result(result, command, sender_aic=context.peer_aic)
        return result

    _start_task = _continue_task = _get_task = _cancel_task = _complete_task = _rpc
