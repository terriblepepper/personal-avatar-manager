# 模块说明：固定公网地址、原域名 TLS 校验及握手前节点切换；HTTP 提交后不重放。
"""HTTP proxy CONNECT to a validated IP with the original TLS server name.

httpcore's proxy TLS path currently ignores request sni_hostname extensions.
This small transport bridge keeps certificate verification and DNS pinning intact.
"""
import httpcore
import httpx
import time


# 职责：包装底层异步网络流并保留模型原 TLS 域名。
# 输入：stream 为 httpcore 网络流，ip 为已验证公网地址，hostname 为原模型域名。
# 结果与约束：代理 CONNECT 使用固定 IP 时只替换 TLS 名称，仍保留证书验证，其他读写直接委托底层流。
class NamedTLSStream(httpcore.AsyncNetworkStream):
    # 职责：保存底层网络流与 IP、域名对应关系。
    # 输入：stream 为已建立连接，ip 和 hostname 来自调用方验证后的模型地址。
    # 结果与约束：不自行建连或修改证书上下文，仅为后续 start_tls 提供原身份。
    def __init__(self, stream, ip, hostname):
        self.stream, self.ip, self.hostname = stream, ip, hostname

    # 职责：从底层流异步读取字节。
    # 输入：max_bytes 为最多读取长度，timeout 透传到网络层。
    # 结果与约束：返回原流读取结果，不修改数据或吞掉网络异常。
    async def read(self, max_bytes, timeout=None):
        return await self.stream.read(max_bytes, timeout=timeout)

    # 职责：向底层流异步写入字节。
    # 输入：buffer 为待发送字节，timeout 为写超时。
    # 结果与约束：等待底层 write 完成，错误向调用方传播，不记录包含凭据的数据。
    async def write(self, buffer, timeout=None):
        return await self.stream.write(buffer, timeout=timeout)

    # 职责：关闭包装器持有的底层网络流。
    # 输入：无额外参数，由 HTTP 连接生命周期调用。
    # 结果与约束：异步等待关闭，释放 TCP/TLS 资源。
    async def aclose(self):
        await self.stream.aclose()

    # 职责：读取底层网络流附加信息。
    # 输入：info 为 httpcore 约定的信息键。
    # 结果与约束：原样返回底层结果，保持传输接口兼容。
    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)

    # 职责：升级连接为 TLS 并确保使用原模型域名验证。
    # 输入：ssl_context 为验证上下文，server_hostname 为连接层给定名称，timeout 透传。
    # 结果与约束：当给定名为固定 IP 时改用原 hostname，其余名称保持不变；返回新的包装流，不关闭证书验证。
    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        name = self.hostname if server_hostname == self.ip else server_hostname
        stream = await self.stream.start_tls(ssl_context=ssl_context, server_hostname=name, timeout=timeout)
        return NamedTLSStream(stream, self.ip, self.hostname)


# 职责：包装 httpcore 网络后端以生成保留原 TLS 名称的流。
# 输入：ip、hostname 定义受验证目标，backend 可注入底层实现。
# 结果与约束：TCP 与 Unix 连接结果统一包装，适配代理 TLS 路径而不改变 DNS 校验职责。
class NamedTLSBackend(httpcore.AsyncNetworkBackend):
    # 职责：选择底层网络后端并保存目标 TLS 身份。
    # 输入：ip 为固定目标 IP，hostname 为原域名，backend 默认使用 AnyIOBackend。
    # 结果与约束：只配置包装关系，不执行 DNS 或网络请求。
    def __init__(self, ip, hostname, backend=None):
        self.ip, self.hostname = ip, hostname
        self.backend = backend or httpcore.AnyIOBackend()

    # 职责：通过底层后端建立 TCP 连接并包装返回流。
    # 输入：args、kwargs 为 httpcore 连接参数，原样传递。
    # 结果与约束：返回 NamedTLSStream，让后续 TLS 升级保留目标原域名；连接异常继续传播。
    async def connect_tcp(self, *args, **kwargs):
        stream = await self.backend.connect_tcp(*args, **kwargs)
        return NamedTLSStream(stream, self.ip, self.hostname)

    # 职责：兼容底层 Unix 套接字连接接口。
    # 输入：args、kwargs 原样转交底层网络后端。
    # 结果与约束：返回同样的流包装，保持后端接口完整，当前模型公网请求通常使用 TCP。
    async def connect_unix_socket(self, *args, **kwargs):
        stream = await self.backend.connect_unix_socket(*args, **kwargs)
        return NamedTLSStream(stream, self.ip, self.hostname)

    # 职责：委托底层网络后端执行异步等待。
    # 输入：seconds 为等待时长。
    # 结果与约束：保持 httpcore 后端协议兼容，不阻塞同步线程。
    async def sleep(self, seconds):
        await self.backend.sleep(seconds)


# 职责：显式代理传输，固定已验证 IP 并保持原 TLS 域名。
# 输入：基于 httpx 传输与 httpcore 代理连接池，不继承环境代理。
# 结果与约束：依赖受约束的 httpx 版本内部连接池接口；修改依赖须重新验证 TLS 与 DNS 固定行为。
class PinnedProxyTransport(httpx.AsyncHTTPTransport):
    # 职责：为显式代理创建自定义 httpcore 连接池。
    # 输入：proxy_url 为运维配置代理，ip 为校验后地址，hostname 为模型原域名。
    # 结果与约束：配置代理鉴权、验证上下文与 NamedTLSBackend，retries=0 避免隐式重试；不关闭 TLS 证书检查。
    def __init__(self, proxy_url, ip, hostname):
        super().__init__(trust_env=False)
        proxy = httpx.Proxy(proxy_url)
        # _pool 是使用的 httpx 内部扩展点，已经 TLS 探针和回归验证；
        # httpx 约束在兼容的 0.28 系列，升级时须重新验证代理 TLS 行为。
        self._pool = httpcore.AsyncHTTPProxy(
            proxy_url=httpcore.URL(scheme=proxy.url.raw_scheme, host=proxy.url.raw_host,
                                   port=proxy.url.port, target=proxy.url.raw_path),
            proxy_auth=proxy.raw_auth, proxy_headers=proxy.headers.raw,
            ssl_context=httpx.create_ssl_context(trust_env=False),
            proxy_ssl_context=proxy.ssl_context,
            network_backend=NamedTLSBackend(ip, hostname), retries=0)


class FailoverStream(NamedTLSStream):
    def __init__(self,stream,ip,backend,remaining,params,deadline):
        super().__init__(stream,ip,backend.hostname)
        self.backend,self.remaining,self.params,self.deadline=backend,remaining,params,deadline

    async def start_tls(self,ssl_context,server_hostname=None,timeout=None):
        try:
            limit=self.backend.budget(self.deadline,len(self.remaining)+1)
            stream=await self.stream.start_tls(ssl_context=ssl_context,server_hostname=self.hostname,timeout=limit)
            return NamedTLSStream(stream,self.ip,self.hostname)
        except (httpcore.ConnectError,httpcore.ConnectTimeout,OSError):
            await self.stream.aclose()
            if not self.remaining:raise
            stream=await self.backend.connect(self.remaining,self.params,self.deadline)
            return await stream.start_tls(ssl_context,server_hostname,timeout)
        except BaseException:
            await self.stream.aclose();raise


class FailoverBackend(NamedTLSBackend):
    def __init__(self,ips,hostname,backend=None):
        super().__init__(ips[0],hostname,backend);self.ips=ips

    @staticmethod
    def budget(deadline,count):
        left=deadline-time.monotonic()
        if left<=0:raise httpcore.ConnectTimeout('All validated model addresses timed out')
        return min(3.,left/max(1,count))

    async def connect(self,ips,params,deadline):
        for index,ip in enumerate(ips):
            try:
                stream=await self.backend.connect_tcp(host=ip,timeout=self.budget(deadline,len(ips)-index),**params)
                return FailoverStream(stream,ip,self,ips[index+1:],params,deadline)
            except (httpcore.ConnectError,httpcore.ConnectTimeout,OSError):
                if index==len(ips)-1:raise

    async def connect_tcp(self,host,port,timeout=None,local_address=None,socket_options=None):
        # 只在 HTTP 字节发送前切换 DNS 已校验地址；读写阶段绝不重放请求。
        return await self.connect(self.ips,{'port':port,'local_address':local_address,'socket_options':socket_options},time.monotonic()+(timeout or 10))


class PinnedDirectTransport(httpx.AsyncHTTPTransport):
    def __init__(self,ips,hostname):
        super().__init__(trust_env=False,retries=0)
        self._pool._network_backend=FailoverBackend(ips,hostname)


class DirectFirstTransport(httpx.AsyncBaseTransport):
    """Switch only after a failed connection, never after sending HTTP bytes."""
    def __init__(self,ips,hostname,proxy):
        self.direct=PinnedDirectTransport(ips,hostname)
        self.fallback=PinnedProxyTransport(proxy,ips[0],hostname)

    async def handle_async_request(self,request):
        try:return await self.direct.handle_async_request(request)
        except (httpx.ConnectError,httpx.ConnectTimeout) as error:
            import ssl
            current=error
            for _ in range(8):
                if current is None:break
                if isinstance(current,ssl.SSLCertVerificationError) or 'certificate verify failed' in str(current).lower():raise error
                current=current.__cause__ or current.__context__
            import logging
            logging.getLogger(__name__).info('Model direct connection unavailable; using configured tunnel proxy')
            return await self.fallback.handle_async_request(request)

    async def aclose(self):
        try:await self.direct.aclose()
        finally:await self.fallback.aclose()
