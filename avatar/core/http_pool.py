"""Short-lived, loop-local transport leases. Request clients own headers and cookies."""
import asyncio
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
    key: tuple
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
        self.entries = {}
        self.validated = {}

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
            self.validated.clear()
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
            for key, entry in tuple(self.entries.items()):
                if self.entries.get(key) is entry and entry.expires <= asyncio.get_running_loop().time():
                    await self._discard(key)

    async def _discard(self, key):
        entry = self.entries.pop(key, None)
        if entry is not None:
            self.validated.pop(key[0], None)
            if entry.transport is not None and not entry.users:
                await entry.transport.aclose()

    async def _reserve(self, origin, stop):
        loop = asyncio.get_running_loop()
        # A request waiting on eviction may resume after a different lifespan has started.
        while not stop.is_set():
            entry = self.entries.get((origin, self.validated.get(origin)))
            if entry is not None:
                if entry.expires > loop.time():
                    return entry
                await self._discard(entry.key)
            elif len(self.entries) >= self.capacity:
                await self._discard(next(iter(self.entries)))
            else:
                key = (origin, None)
                entry = self.entries[key] = Endpoint(key)
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
                    addresses, entry.transport = await connect(url)
                    key = (origin, addresses)
                    if self.entries.get(entry.key) is entry:
                        del self.entries[entry.key]
                        self.validated[origin] = addresses
                        self.entries[key] = entry
                    entry.key = key
                    entry.expires = loop.time() + self.ttl
            async with httpx.AsyncClient(transport=BorrowedTransport(entry.transport), **options) as client:
                yield entry.key[1], client
        finally:
            entry.users -= 1
            if not entry.users:
                if self.entries.get(entry.key) is not entry:
                    if entry.transport is not None:
                        await entry.transport.aclose()
                elif entry.transport is None:
                    self.entries.pop(entry.key)
