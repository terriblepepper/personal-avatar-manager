"""Complete platform component; the host WitAgent owns its lifetime."""
import asyncio
from contextlib import contextmanager
from fastapi import FastAPI
from wit.components import ServiceKey, WitComponent
from avatar.core.config import Settings

PLATFORM_APP = ServiceKey[FastAPI]('avatar.platform.app')


class AvatarPlatformComponent(WitComponent[None]):
    """Install the Web UI, business services and configured protocol endpoints.

    With serve_http=False the host mounts PLATFORM_APP and this component still
    owns its lifespan. Do not enter the application's lifespan a second time.
    """
    name = 'avatar-platform'
    provides = (PLATFORM_APP,)

    def __init__(self, settings=None, *, host='127.0.0.1', port=18080,
                 serve_http=True, run_worker=True, provider=None, model_client=None):
        self.settings = settings
        self.host, self.port = host, port
        self.serve_http, self.run_worker = serve_http, run_worker
        self.provider, self.model_client = provider, model_client
        self.app = self.server = None

    async def setup(self, ctx):
        from avatar.api.app import create_app
        self.app = create_app(self.settings or Settings.from_env(), self.provider,
                              self.run_worker, self.model_client)
        await ctx.effect(self._start, label='avatar-platform-lifetime')
        ctx.provide(PLATFORM_APP, self.app)

    async def _start(self):
        if not self.serve_http:
            lifespan = self.app.router.lifespan_context(self.app)
            await lifespan.__aenter__()

            async def close():
                await lifespan.__aexit__(None, None, None)

            return close

        import uvicorn

        class EmbeddedServer(uvicorn.Server):
            # The host owns process signals; a component must not replace them.
            @contextmanager
            def capture_signals(self):
                yield

            async def startup(self, sockets=None):
                try:
                    await super().startup(sockets=sockets)
                except SystemExit as exc:
                    raise RuntimeError('平台 HTTP 端口启动失败') from exc

        self.server = EmbeddedServer(uvicorn.Config(
            self.app, host=self.host, port=self.port, loop='asyncio',
            workers=1, proxy_headers=False, access_log=False,
            timeout_graceful_shutdown=15))
        task = asyncio.create_task(self.server.serve(), name='avatar-platform-http')

        async def close():
            self.server.should_exit = True
            try:
                await asyncio.wait_for(task, timeout=25)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise RuntimeError('平台 HTTP 服务未能按时关闭') from None

        try:
            async with asyncio.timeout(30):
                while not self.server.started:
                    if task.done():
                        await task
                        raise RuntimeError('平台 HTTP 服务未能启动')
                    await asyncio.sleep(.01)
        except BaseException:
            await close()
            raise
        return close
