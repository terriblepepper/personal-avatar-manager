"""同一服务账户的 Registry 和 CA 请求共用队列，包含后台任务及运维 CLI。"""
import asyncio
from contextlib import contextmanager
import os
import ssl
import time
from pathlib import Path
from weakref import WeakKeyDictionary

import httpx
from avatar.core.execution import blocking_context, finish_on_cancel, run_blocking
from wit.acps.atr import CertificateAuthority, RegistryClient, TrustedRegistrationComponent
from wit.acps.atr.storage import lock_file


_locks = WeakKeyDictionary()


def trust_context(config):
    path = config.endpoints.ca_cert_path
    return ssl.create_default_context(cafile=str(path) if path else None)


@contextmanager
def _request_clock():
    directory = Path.home() / '.cache/personal-avatar-manager'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory / 'certificate-requests.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as clock:
        yield clock


def _lock_clock(clock):
    lock_file(clock.fileno(), blocking=False)
    return float(clock.read() or 0)


def _record_completion(clock):
    clock.seek(0)
    clock.write(str(time.time()))
    clock.truncate()


class CertificateTransport(httpx.AsyncBaseTransport):
    def __init__(self, context, transport=None):
        self.transport = transport if transport is not None else httpx.AsyncHTTPTransport(verify=context, trust_env=False)

    async def handle_async_request(self, request):
        # asyncio 按等待顺序放行；文件锁同时约束同机运维进程，并跨重启保留最近完成时间。
        loop = asyncio.get_running_loop()
        async with _locks.setdefault(loop, asyncio.Lock()):
            async with blocking_context(_request_clock()) as clock:
                while True:
                    try:
                        previous = await run_blocking(_lock_clock, clock)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(.05)
                elapsed = max(0, time.time() - previous)
                if elapsed < .5:
                    await asyncio.sleep(.5 - elapsed)
                try:
                    response = await self.transport.handle_async_request(request)
                    try:
                        await response.aread()
                    except BaseException:
                        await finish_on_cancel(response.aclose())
                        raise
                    return response
                finally:
                    await run_blocking(_record_completion, clock)

    async def aclose(self):
        await self.transport.aclose()


def certificate_authority(config):
    return CertificateAuthority(config, transport=CertificateTransport(trust_context(config)))


def registration_component(config, *, registry=None):
    if registry is None:
        registry = RegistryClient(config, transport=CertificateTransport(trust_context(config)))
    return TrustedRegistrationComponent(config, registry=registry,
        ca=certificate_authority(config))
