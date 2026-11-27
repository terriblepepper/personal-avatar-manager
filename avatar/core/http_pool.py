"""Short-lived, loop-local transport leases. Request clients own headers and cookies."""
import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx


class BorrowedTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport):
        self.transport = transport

    async def handle_async_request(self, request):
        return await self.transport.handle_async_request(request)


@dataclass
class Endpoint:
    addresses: tuple | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    transport: httpx.AsyncBaseTransport | None = None
    expires: float = float('inf')
    users: int = 0


class HttpTransportPool:
    ttl = 60
    capacity = 16

    def __init__(self):
        self.loop = None
        self._stop = None
        self.entries = OrderedDict()

    @asynccontextmanager
    async def lifespan(self):
        loop = asyncio.get_running_loop()
        # An injected service can be shared by embedded apps. Only its original loop owns sockets.
        if self.loop is not None:
            yield
            return
        self.loop = loop
        self._stop = stop = asyncio.Event()
        sweeper = self.loop.create_task(self._sweep(stop))
        try:
            yield
        finally:
            stop.set()
            # Detach before the first await. Active leases close themselves after their response.
            idle = [entry.transport for entry in self.entries.values()
                    if entry.transport is not None and not entry.users]
            self.entries.clear()
            try:
                await sweeper
                for transport in idle:
                    await transport.aclose()
            finally:
                self._stop = None
                self.loop = None

    async def _sweep(self, stop):
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), self.ttl)
                return
            except TimeoutError:
                pass
            for origin, entry in tuple(self.entries.items()):
                if (self.entries.get(origin) is entry and not entry.users
                        and entry.expires <= asyncio.get_running_loop().time()):
                    await self._discard(origin)

    async def _discard(self, origin):
        entry = self.entries.pop(origin)
        if entry.transport is not None:
            await entry.transport.aclose()

    async def _reserve(self, origin, stop):
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            entry = self.entries.get(origin)
            if entry is not None:
                if entry.expires > loop.time():
                    self.entries.move_to_end(origin)
                    return entry
                if entry.users:
                    return None  # A fresh validation gets a request-local transport.
                await self._discard(origin)
            elif len(self.entries) >= self.capacity:
                idle = next((key for key, item in self.entries.items() if not item.users), None)
                if idle is None:
                    return None
                await self._discard(idle)
            else:
                entry = self.entries[origin] = Endpoint()
                return entry
        return None

    @asynccontextmanager
    async def session(self, url, connect, timeout, *, proxy=False, injected=False):
        options = dict(timeout=timeout, trust_env=False, follow_redirects=False)
        loop = asyncio.get_running_loop()
        # One validated address set per origin and TLS/proxy policy.
        origin = (url.scheme, url.host, url.port, proxy)
        entry = None
        if self.loop is loop and self._stop is not None and not injected:
            entry = await self._reserve(origin, self._stop)
        if entry is None:
            addresses, transport = await connect(url)
            async with httpx.AsyncClient(transport=transport, **options) as client:
                yield addresses, client
            return

        entry.users += 1
        try:
            async with entry.lock:
                if entry.transport is None:
                    entry.addresses, entry.transport = await connect(url)
                    entry.expires = loop.time() + self.ttl
            async with httpx.AsyncClient(transport=BorrowedTransport(entry.transport), **options) as client:
                yield entry.addresses, client
        finally:
            entry.users -= 1
            if not entry.users:
                if self.entries.get(origin) is not entry:
                    if entry.transport is not None:
                        await entry.transport.aclose()
                elif entry.transport is None:
                    self.entries.pop(origin)
