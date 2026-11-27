"""运维授权的只读 HTTPS 资源按名称导入，沙箱内无网络权限。"""
import asyncio
import json
import os
import re
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from avatar.core.network import PinnedDirectTransport, resolve_public_all


def configured_resources():
    raw = os.environ.get('AVATAR_SANDBOX_HTTPS_RESOURCES', '{}')
    try:
        resources = json.loads(raw)
        if not isinstance(resources, dict) or len(resources) > 32:
            raise ValueError
        for key, url in resources.items():
            if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}', key) or not isinstance(url, str):
                raise ValueError
            target = urlsplit(url)
            if (target.scheme != 'https' or not target.hostname or target.username is not None
                    or target.password is not None or target.port not in (None, 443) or target.query or target.fragment
                    or any(ord(c) <= 32 or c in ('*', chr(92)) for c in url)):
                raise ValueError
    except (ValueError, TypeError):
        raise ValueError('AVATAR_SANDBOX_HTTPS_RESOURCES 须为名称与固定 HTTPS 地址组成的 JSON 对象') from None
    return resources


async def fetch_inputs(names):
    if not names:
        return {}
    allowed = configured_resources()
    if (not isinstance(names, list) or len(names) > 2 or any(not isinstance(n, str) or n not in allowed for n in names)
            or len(set(names)) != len(names)):
        raise ValueError('每次仅可选择最多两项已配置的 HTTPS 资源')
    result = {}
    try:
        async with asyncio.timeout(10):
            for name in names:
                url = httpx.URL(allowed[name])
                ips = await resolve_public_all(url.host, 443)
                async with httpx.AsyncClient(transport=PinnedDirectTransport(ips, url.host),
                                             trust_env=False, follow_redirects=False) as client:
                    async with client.stream('GET', allowed[name], headers={'Accept-Encoding': 'identity'}) as response:
                        response.raise_for_status()
                        if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                            raise ValueError('HTTPS 资源必须使用未压缩的响应')
                        body = bytearray()
                        async for block in response.aiter_raw(chunk_size=32768):
                            if len(body) + len(block) > 1024*1024:
                                raise ValueError('HTTPS 资源超过 1 MiB')
                            body.extend(block)
                        result['https/'+name] = bytes(body)
    except (httpx.HTTPError, HTTPException, TimeoutError):
        raise ValueError('受控 HTTPS 读取失败或超时') from None
    return result
