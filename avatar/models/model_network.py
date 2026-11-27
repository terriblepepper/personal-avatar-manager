"""Model endpoint policy and transport compatibility exports."""
import ipaddress
import os
import re
from fastapi import HTTPException

from avatar.core.network import (
    DirectFirstTransport, FailoverBackend, FailoverStream, NamedTLSBackend, NamedTLSStream,
    PinnedDirectTransport, PinnedProxyTransport, address, is_public, resolve_addresses,
    resolve_public, resolve_public_all,
)


_PRIVATE_RANGES = tuple(map(ipaddress.ip_network, (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7',
)))
_HOST = re.compile(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*'
                   r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')


def pinned_request_url(url, addresses):
    # Private HTTP connects via PinnedDirectTransport. Keep its IP out of httpx URL logs.
    target = addresses[0]
    if url.scheme == 'http' and not is_public(address(target)):
        target = 'model-gateway.invalid'
    return url.copy_with(host=target)


def http_allowlist():
    """Parse exact DNS names and IP networks, optionally restricted to a port."""
    rules = []
    for raw in os.getenv('AVATAR_MODEL_HTTP_ALLOWLIST', '').split(','):
        value = raw.strip().lower()
        if not value:
            continue
        try:
            target, port = value, None
            if value.startswith('['):
                target, suffix = value[1:].split(']', 1)
                if suffix and not re.fullmatch(r':[0-9]+', suffix):
                    raise ValueError
                port = int(suffix[1:]) if suffix else None
            elif value.count(':') == 1:
                target, raw_port = value.split(':')
                if not re.fullmatch(r'[0-9]+', raw_port):
                    raise ValueError
                port = int(raw_port)
            if port is not None and not 1 <= port <= 65535:
                raise ValueError
            try:
                target = ipaddress.ip_network(target, strict=True)
            except ValueError:
                target = target.rstrip('.')
                if not _HOST.fullmatch(target):
                    raise ValueError from None
            rules.append((target, port))
        except ValueError:
            raise ValueError('AVATAR_MODEL_HTTP_ALLOWLIST 含无效的主机、网段或端口') from None
    return tuple(rules)


async def resolve_model_http(host, port, rules):
    ips = await resolve_addresses(host, port)
    host = host.rstrip('.').lower()
    for value in ips:
        ip = address(value)
        if is_public(ip):
            continue
        if any(ip in net for net in _PRIVATE_RANGES) and any(
                (allowed_port is None or allowed_port == port) and
                (host == target if isinstance(target, str) else ip in target)
                for target, allowed_port in rules):
            continue
        raise HTTPException(422, '模型 HTTP 地址受限，请核对运维网关白名单')
    return ips


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
