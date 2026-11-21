"""WIT Direct TLS admission. Uvicorn parses HTTP and WIT dispatches the authenticated request."""
import asyncio
import errno
import logging
import socket
import ssl
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from uvicorn import Config
from uvicorn.protocols.http.h11_impl import H11Protocol
from uvicorn.server import ServerState
from wit.acps.aip.direct import BaseAipListenerComponent, BaseDirectPartnerComponent, DIRECT_PARTNER_ENDPOINT
from wit.acps.identity import VerifiedPeer, peer_aic_from_certificate, use_local_entity
from wit.acps.atr.storage import temporary_secrets


def load_cert_chain(context, certificate, private_key):
    with temporary_secrets([certificate.encode(), private_key.encode()]) as paths:
        context.load_cert_chain(*paths)


def server_context(entity):
    material = entity.material
    der = x509.load_pem_x509_certificate(material.server_certificate.encode()).public_bytes(Encoding.DER)
    if peer_aic_from_certificate(der) != entity.aic:
        raise ValueError('服务端证书 AIC 与本地实体不一致')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.num_tickets = 0
    context.load_verify_locations(cadata=material.trust_bundle)
    load_cert_chain(context, material.server_certificate, material.server_private_key)
    return context


async def reject(send, status):
    await send({'type': 'http.response.start', 'status': status, 'headers': [(b'connection', b'close')]})
    await send({'type': 'http.response.body', 'body': b''})


class _HttpConnection(H11Protocol):
    def __init__(self, listener):
        super().__init__(listener.http_config, listener.http_state, {})
        self.listener = listener
        self.closed = asyncio.Event()
        self.app = self.deliver

    def connection_made(self, transport):
        super().connection_made(transport)
        tls = transport.get_extra_info('ssl_object')
        try:
            self.entity, self.endpoint, self.sni = tls.avatar_binding
            self.peer = VerifiedPeer.from_tls(tls)
        except (AttributeError, ValueError):
            transport.abort()

    def connection_lost(self, exc):
        try:
            super().connection_lost(exc)
        finally:
            self.closed.set()

    async def _deliver_verified(self, scope, receive, send):
        host = self.entity.hostname.lower()
        if ':' in host and isinstance(self.endpoint, BaseDirectPartnerComponent):
            # WIT 3.2.0 splits a bracketed IPv6 Host at its first colon in deliver_asgi.
            # Keep its ASGI/JSON-RPC dispatcher after binding the verified TLS peer, Host, SNI and selected entity.
            if self.endpoint.server_name.lower() != host or (self.sni is not None and self.sni != host):
                return await reject(send, 403)
            with use_local_entity(self.entity):
                return await self.endpoint._asgi_app(scope, receive, send, peer=self.peer, peer_aic=self.peer.aic)
        await self.endpoint.deliver_asgi(scope, receive, send, entity=self.entity, peer=self.peer, tls_server_name=self.sni)

    async def deliver(self, scope, receive, send):
        listener = self.listener
        entity = self.entity
        component = listener.component
        try:
            current = await component.directory.select(entity.aic)
            if not listener.serving or current != entity:
                return await reject(send, 403)
            hosts = [v.decode('ascii') for k, v in scope['headers'] if k == b'host']
            if len(hosts) != 1:
                return await reject(send, 400)
            host = urlsplit('//' + hosts[0])
            endpoint_url = urlsplit(self.endpoint.public_endpoint)
            if (host.hostname != entity.hostname.lower() or host.hostname != endpoint_url.hostname
                    or hosts[0].endswith(':') or host.port == 0 or (host.port or 443) != (endpoint_url.port or 443)
                    or host.username is not None or host.path or host.query or host.fragment):
                return await reject(send, 403)
        except (LookupError, ValueError, OSError):
            return await reject(send, 403)
        stream = scope['path'].rstrip('/').rsplit('/', 1)[-1] in ('stream', 'restream')
        if stream and listener.streams >= component.max_streams:
            return await reject(send, 503)
        listener.streams += int(stream)
        try:
            async with asyncio.timeout(component.stream_timeout if stream else component.request_timeout):
                size = 0
                async def bounded_receive():
                    nonlocal size
                    event = await receive()
                    size += len(event.get('body', b''))
                    if size > component.max_body_bytes:
                        raise OverflowError('request body exceeds listener limit')
                    return event
                await self._deliver_verified(scope, bounded_receive, send)
        except OverflowError:
            await reject(send, 413)
        except TimeoutError:
            self.transport.abort()
        finally:
            listener.streams -= int(stream)


class _Listener:
    def __init__(self, component, ctx):
        self.component, self.ctx = component, ctx
        self.active = set()
        self.http_state = ServerState()
        self.streams = 0
        self.ready = False
        self.closed = False
        self.accept_backoff = False

    async def open(self):
        component = self.component
        await component._refresh_tls(self.ctx)
        # TLS verifies the peer against the context selected for this specific SNI.
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.minimum_version = ssl.TLSVersion.TLSv1_3
        self.tls.verify_mode = ssl.CERT_REQUIRED
        self.tls.num_tickets = 0
        self.tls.sni_callback = self._select
        self.http_config = Config(reject, http='h11', ws='none', lifespan='off', proxy_headers=False,
                                  access_log=False, log_config=None, interface='asgi3')
        self.http_config.load()
        family = socket.AF_INET6 if ':' in component.bind_address else socket.AF_INET
        self.sock = socket.create_server((component.bind_address, component.bind_port), family=family, backlog=component.max_connections)
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]
        self.slots = asyncio.Semaphore(component.max_connections)
        self.ready = True
        self.accepting = asyncio.create_task(self._accept())
        self.refreshing = asyncio.create_task(self._refresh())
        return self

    @property
    def serving(self):
        return (self.ready and not self.closed and not self.accept_backoff
                and not self.accepting.done() and not self.refreshing.done())

    def _select(self, tls, name, initial):
        if not self.serving:
            return ssl.ALERT_DESCRIPTION_ACCESS_DENIED
        try:
            sni = name.lower() if name else None
            entity = self.component._select_entity(sni)
            tls.context = self.component._select_tls_context(entity)
            tls.avatar_binding = (entity, self.component._select_endpoint(entity), sni)
        except (LookupError, ValueError):
            return ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME

    async def _refresh(self):
        while True:
            await asyncio.sleep(1)
            try:
                await self.component._refresh_tls(self.ctx)
                self.ready = True
            except Exception as exc:
                if self.ready:
                    logging.getLogger(__name__).warning('AIP certificate refresh failed (%s)', type(exc).__name__)
                self.ready = False
                self._abort_clients()

    def _abort_clients(self):
        for task in (*self.active, *self.http_state.tasks):
            task.cancel()

    async def _accept(self):
        loop = asyncio.get_running_loop()
        retry_delay = 0.05
        while True:
            await self.slots.acquire()
            try:
                connection, _ = await loop.sock_accept(self.sock)
            except asyncio.CancelledError:
                self.slots.release()
                raise
            except OSError as exc:
                self.slots.release()
                if retry_delay == 0.05:
                    logging.getLogger(__name__).warning('AIP socket accept failed (errno %s)', exc.errno)
                if exc.errno in (errno.EBADF, errno.EINVAL, errno.ENOTSOCK):
                    return
                self.accept_backoff = True
                await asyncio.sleep(retry_delay)
                self.accept_backoff = False
                retry_delay = min(retry_delay * 2, 1.0)
                continue
            retry_delay = 0.05
            task = asyncio.create_task(self._serve(connection))
            self.active.add(task)
            def finished(job, sock=connection):
                self.active.discard(job)
                sock.close()
                self.slots.release()
                if not job.cancelled() and job.exception():
                    logging.getLogger(__name__).warning('AIP connection ended (%s)', type(job.exception()).__name__)
            task.add_done_callback(finished)

    async def _serve(self, connection):
        transport = None
        try:
            transport, protocol = await asyncio.get_running_loop().connect_accepted_socket(
                lambda: _HttpConnection(self), connection, ssl=self.tls,
                ssl_handshake_timeout=min(self.component.request_timeout, 15))
            # Bounds clients that authenticate and then never finish HTTP headers.
            async with asyncio.timeout(max(self.component.request_timeout, self.component.stream_timeout) + 5):
                await protocol.closed.wait()
        except (ConnectionError, ssl.SSLError, TimeoutError, OSError):
            pass
        finally:
            if transport is not None:
                transport.abort()

    async def close_admission(self):
        if not self.closed:
            self.closed = True
            self.accepting.cancel()
            self.refreshing.cancel()
            self.sock.close()
            for protocol in tuple(self.http_state.connections):
                protocol.shutdown()
            await asyncio.gather(self.accepting, self.refreshing, return_exceptions=True)

    async def drain(self):
        await asyncio.gather(*self.active, *self.http_state.tasks, return_exceptions=True)

    async def stop(self):
        await self.close_admission()
        self._abort_clients()
        await self.drain()


class TlsListenerComponent(BaseAipListenerComponent):
    prepare_tls_context = staticmethod(server_context)

    def __init__(self, directory, *, bind_address='127.0.0.1', bind_port=0, max_connections=64,
                 max_streams=16, request_timeout=30.0, stream_timeout=300.0, max_body_bytes=1048576):
        super().__init__()
        if min(max_connections, request_timeout, stream_timeout, max_body_bytes) <= 0 or max_streams < 0:
            raise ValueError('AIP listener limits must be positive')
        self.directory, self.bind_address, self.bind_port = directory, bind_address, bind_port
        self.max_connections, self.max_streams = max_connections, max_streams
        self.request_timeout, self.stream_timeout, self.max_body_bytes = request_timeout, stream_timeout, max_body_bytes

    async def _open_listener(self, ctx):
        return await _Listener(self, ctx).open()


class FixedEntityListener(TlsListenerComponent):
    requires = (DIRECT_PARTNER_ENDPOINT,)

    def __init__(self, directory, *, local_aic, **options):
        super().__init__(directory, **options)
        self.local_aic = local_aic
        self.entity = None

    async def _refresh_tls(self, ctx):
        entity = await self.directory.select(self.local_aic)
        endpoint = ctx.get(DIRECT_PARTNER_ENDPOINT)
        if entity.aic != self.local_aic or entity.hostname.lower() != endpoint.server_name.lower():
            raise ValueError('监听端点与已注册身份不符')
        if entity != self.entity:
            self.tls = self.prepare_tls_context(entity)
            self.entity = entity
        self.endpoint = endpoint

    def _select_entity(self, name):
        if name and name != self.entity.hostname.lower():
            raise LookupError('SNI 与固定端点不符')
        return self.entity

    def _select_tls_context(self, entity):
        return self.tls

    def _select_endpoint(self, entity):
        return self.endpoint
