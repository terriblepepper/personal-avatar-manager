"""等待 Registry 登录令牌可读；绝不重放注册、删除等写操作。"""
import asyncio
from .certificates import CertificateTransport, trust_context
import httpx


class RegistryReadyTransport(httpx.AsyncBaseTransport):
    def __init__(self, config, transport=None):
        self.transport = CertificateTransport(trust_context(config), transport)

    async def handle_async_request(self, request):
        response = await self.transport.handle_async_request(request)
        if request.method != 'POST' or request.url.path not in ('/api/v1/auth/login', '/api/v1/auth/refresh-token') or not response.is_success:
            return response
        await response.aread()
        token = response.json().get('access_token')
        if not isinstance(token, str) or not token:
            raise ValueError('Registry 登录没有返回有效 access_token')
        # 部署可能先返回登录响应、后提交令牌；仅对只读认证检查短暂等待。
        for delay in (0, .1, .25, .5, 1, 1.5):
            if delay:
                await asyncio.sleep(delay)
            probe = httpx.Request('GET', request.url.copy_with(path='/api/v1/account/me', query=None),
                                 headers={'Authorization': 'Bearer ' + token}, extensions=request.extensions)
            checked = await self.transport.handle_async_request(probe)
            checked.request = probe
            await checked.aread()
            await checked.aclose()
            if checked.status_code != 401:
                checked.raise_for_status()
                return response
        raise ValueError('Registry 登录后令牌仍未就绪，尚未执行注册或注销写入，请检查认证服务')

    async def aclose(self):
        await self.transport.aclose()
