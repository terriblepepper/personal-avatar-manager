"""WIT 3.2 身份与证书运维入口，支持 integrated、ontology、entity 三种注册模式。

调用链：main → configuration → wit_candidate_config → TrustedRegistrationComponent。
register 使用 up() 恢复原申请并推进双证书，status 刷新已有证书状态，issue/renew 显式签发。
reconcile 用已核实的原申请 ID 对账。正常结果只输出公开元数据；--debug 在 stderr 显示
Registry 的脱敏 HTTP 响应及 traceback。凭据从已加载的环境变量读取，本脚本不自动 source .env。
"""
import argparse
import asyncio
import json
import os
import re
import ssl
import time
import traceback
from urllib.parse import urlsplit, urlunsplit

import httpx
import sys
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 白名单同时适用于 RegistrationStatus 和证书操作结果，避免 asdict() 深拷贝只读 mappingproxy。
# 不包含 acs_candidate、私钥、ACME 账号、EAB 或完整证书正文。
PUBLIC_METADATA_FIELDS = frozenset({
    'phase', 'registration_id', 'aic', 'reason', 'acs_fingerprint',
    'operation', 'status', 'usage', 'generation', 'fingerprint', 'serial', 'instances',
})


class RegistryDiagnostics:
    def __init__(self, secrets=(), *, max_chars=32768, stream=None):
        if not 512 <= max_chars <= 262144:
            raise ValueError('--debug-max-chars 必须在 512..262144 之间')
        self.secrets = {value for value in secrets if isinstance(value, str) and value}
        self.max_chars, self.stream = max_chars, stream or sys.stderr

    @staticmethod
    def sensitive_key(key):
        name = re.sub(r'[^a-z0-9]', '', str(key).lower())
        return (any(word in name for word in ('password', 'passwd', 'token', 'secret', 'privatekey',
                                              'hmackey', 'authorization', 'cookie', 'credential'))
                or name in {'eab', 'externalaccountbinding', 'accountkey', 'accountkeypem', 'kid', 'apikey'})

    def remember(self, value, hidden=False):
        if isinstance(value, dict):
            for key, item in value.items():
                self.remember(item, hidden or self.sensitive_key(key))
        elif isinstance(value, list):
            for item in value:
                self.remember(item, hidden)
        elif hidden and isinstance(value, str) and value:
            self.secrets.add(value)

    def redact_text(self, value):
        text = str(value)
        text = re.sub(r'(?i)(https?://)[^/\s@]+@', r'\1[用户信息已隐藏]@', text)
        text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----',
                      '[私钥已隐藏]', text, flags=re.S)
        text = re.sub(r'(?i)\bBearer\s+[^\s\"\'<>]+', 'Bearer [已隐藏]', text)
        pattern = r'''(?ix)(["']?(?:password|passwd|token|secret|authorization|cookie|eab|kid|account[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|hmac[_-]?key|client[_-]?secret|private[_-]?key)["']?\s*[:=]\s*)("[^"\n]*"|'[^'\n]*'|[^\s&,;}]+)'''
        text = re.sub(pattern, lambda match: match[1] + '[已隐藏]', text)
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, '[已隐藏]')
        return text

    def clean(self, value):
        if isinstance(value, dict):
            return {self.redact_text(key): '[已隐藏]' if self.sensitive_key(key) else self.clean(item)
                    for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [self.clean(item) for item in value]
        if isinstance(value, str):
            return self.redact_text(value)
        return value

    def bounded(self, text):
        if len(text) <= self.max_chars:
            return text
        return text[:self.max_chars] + f'\n[显示已截断，原文共 {len(text)} 字符；可调整 --debug-max-chars]'

    def emit(self, event, **details):
        print('[wit-debug] ' + json.dumps(self.clean({'event': event, **details}), ensure_ascii=False),
              file=self.stream, flush=True)

    @staticmethod
    def public_url(url):
        parsed = urlsplit(str(url))
        host = parsed.hostname or ''
        if ':' in host:
            host = '[' + host + ']'
        if parsed.port is not None:
            host += ':' + str(parsed.port)
        query = '&'.join(part.split('=', 1)[0] + '=[已隐藏]' for part in parsed.query.split('&') if part)
        return urlunsplit((parsed.scheme, host, parsed.path, query, ''))

    async def request(self, request):
        request.extensions['avatar_debug_started'] = time.monotonic()
        self.emit('registry.request', method=request.method, url=self.public_url(request.url))

    async def response(self, response):
        await response.aread()
        try:
            document = response.json()
        except (ValueError, UnicodeError):
            document = response.text
        path = response.request.url.path.lower()
        eab_success = 'eab' in path and 200 <= response.status_code < 300
        errors = ('error', 'detail', 'message', 'reason', 'code', 'status')
        credentials = {k: v for k, v in document.items() if k not in errors} if eab_success and isinstance(document, dict) else document
        self.remember(credentials, hidden=eab_success)
        if eab_success:
            body = {'credentials': '[EAB 凭据响应已隐藏]'}
            if isinstance(document, dict):
                body.update({k: self.clean(document[k]) for k in errors if k in document})
        else:
            body = self.clean(document)
        text = json.dumps(body, ensure_ascii=False, indent=2) if not isinstance(body, str) else body
        headers = {key: response.headers[key] for key in ('content-type', 'x-request-id', 'x-correlation-id', 'retry-after')
                   if key in response.headers}
        details = {'method': response.request.method, 'url': self.public_url(response.request.url),
                   'status': response.status_code, 'headers': headers}
        started = response.request.extensions.get('avatar_debug_started')
        if started is not None:
            details['elapsed_ms'] = round((time.monotonic() - started) * 1000, 1)
        if isinstance(document, dict) and 'eab' not in path:
            details['json_keys'] = list(document)
            if '/acs/' in path:
                required = ('aic', 'protocolVersion', 'description', 'defaultInputModes', 'defaultOutputModes',
                            'iconUrl', 'documentationUrl')
                details['missing_acs_fields'] = [key for key in required if key not in document]
        self.emit('registry.response', **details)
        print('[wit-debug] response-body\n' + self.bounded(text), file=self.stream, flush=True)

    def exception(self, exc):
        stack = ''.join(traceback.TracebackException.from_exception(exc, capture_locals=False).format())
        self.emit('exception', type=type(exc).__name__)
        print(self.bounded(self.redact_text(stack)), file=self.stream, flush=True)


class RegistryDebugTransport(httpx.AsyncBaseTransport):
    def __init__(self, diagnostics, transport=None, *, ca_cert_path=None):
        self.diagnostics, self.transport = diagnostics, transport
        self.ca_cert_path = ca_cert_path

    async def handle_async_request(self, request):
        await self.diagnostics.request(request)
        try:
            if self.transport is None:
                # 延迟构造 TLS：仅查询本地待审状态时不额外读取 CA 文件或创建网络资源。
                trust = ssl.create_default_context(cafile=str(self.ca_cert_path) if self.ca_cert_path else None)
                self.transport = httpx.AsyncHTTPTransport(verify=trust, trust_env=False)
            response = await self.transport.handle_async_request(request)
            response.request = request
            await self.diagnostics.response(response)
            return response
        except Exception as exc:
            self.diagnostics.emit('registry.transport_error', type=type(exc).__name__, message=str(exc))
            raise

    async def aclose(self):
        if self.transport is not None:
            await self.transport.aclose()


def public_metadata(value):
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: public_metadata(getattr(value, item.name))
                for item in fields(value) if item.name in PUBLIC_METADATA_FIELDS}
    if isinstance(value, Mapping):
        return {key: public_metadata(item) for key, item in value.items() if key in PUBLIC_METADATA_FIELDS}
    if isinstance(value, (tuple, list)):
        return [public_metadata(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError('不支持的证书元数据类型')


def configuration(path):
    from wit.acps.atr import AtrConfig, AtrEndpoints, AtrLocalPaths, RegistryServerCredentials
    source = path.resolve()
    document = json.loads(source.read_text())

    def local(value):
        p = Path(value)
        return p if p.is_absolute() else source.parent / p

    # 仅兼容显式 use_saved_urls 的旧运维配置；新身份通常关闭此项，使用独立配置中的服务地址。
    if document.get("use_saved_urls"):
        from avatar.core.config import Settings
        from avatar.core.db import Database
        settings = Settings.from_env()
        with Database.open_existing(settings.data_dir, settings.database_url) as database, database.connect() as db:
            rows = db.execute("SELECT key,value FROM settings WHERE key IN ('registry_url','certificate_authority_url','registry_entity_mtls_url')")
            for key, value in rows:
                if saved := json.loads(value):
                    document[key] = saved
    username = os.environ.get("WIT_REGISTRY_USERNAME")
    password = os.environ.get("WIT_REGISTRY_PASSWORD")
    if not username or not password:
        raise ValueError("请设置 WIT_REGISTRY_USERNAME 和 WIT_REGISTRY_PASSWORD")
    ca_path = local(document["ca_cert_path"]) if document.get("ca_cert_path") else None
    config = AtrConfig(
        endpoints=AtrEndpoints(registry_url=document["registry_url"],
                               certificate_authority_url=document["certificate_authority_url"],
                               registry_entity_mtls_url=document.get("registry_entity_mtls_url") or None,
                               ca_cert_path=ca_path),
        registry_credentials=RegistryServerCredentials(username=username, password=password),
        paths=AtrLocalPaths(acs_path=local(document["acs_path"]), state_dir=local(document["state_dir"]),
                            parent_state_dir=local(document["parent_state_dir"]) if document.get("parent_state_dir") else None),
        mode=document.get("mode", "integrated"))
    from avatar.protocols.identity.document import migrate_identity_document
    migrate_identity_document(config)
    return config


async def register_until_ready(registration, timeout, diagnostics=None):
    if timeout <= 0:
        raise ValueError('注册等待超时必须大于 0')
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return registration.state
        if diagnostics:
            diagnostics.emit('registration.up', remaining_seconds=round(remaining, 2))
        # asyncio.timeout 不会清理或重置 ATR 状态；超时后必须沿用同一个配置恢复。
        async with asyncio.timeout(remaining):
            result = await registration.up()
        if diagnostics:
            diagnostics.emit('registration.state', **public_metadata(result))
        if str(result.phase) in ('active', 'blocked', 'rejected'):
            return result
        await asyncio.sleep(min(2, max(0, deadline - asyncio.get_running_loop().time())))


async def execute(args, diagnostics=None):
    from wit import WitAgent
    from wit.acps.atr import (TRUSTED_REGISTRATION, TRUSTED_CERTIFICATES,
                             CertificateIssueRequest, CertificateRenewRequest, RegistryClient)
    from avatar.protocols.identity.document import wit_candidate_config, sync_component_acs
    config = configuration(args.config)
    if diagnostics:
        diagnostics.remember({'username': config.registry_credentials.username,
                              'password': config.registry_credentials.password}, hidden=True)
    debug_transport = None
    try:
        # acs.json 可能已是正式文档。兼容层从 ATR 状态提供原候选，SDK 仍会严格核对首次指纹。
        with wit_candidate_config(config) as input_config:
            from avatar.protocols.transport.certificates import CertificateTransport, registration_component, trust_context
            registry = None
            hooks = None
            if diagnostics:

                diagnostics.emit('identity.configuration', action=args.action, mode=config.mode,
                                 config=str(args.config.resolve()),
                                 registry_url=diagnostics.public_url(config.endpoints.registry_url))
                # 自定义 transport 自己持有 TLS 配置，必须保留原 ca_cert_path，不能用 verify=False。
                debug_transport = RegistryDebugTransport(diagnostics, ca_cert_path=config.endpoints.ca_cert_path)
                registry = RegistryClient(input_config, transport=CertificateTransport(trust_context(config),debug_transport))
                hooks = {'request': [diagnostics.request], 'response': [diagnostics.response]}
            if config.mode == 'entity':
                from avatar.protocols.transport.registry import EntityRegistry
                # 本体证书只用于 Registry 的 mTLS 派生认证；用户实体证书仍由 wit/CA 独立签发。
                registry = EntityRegistry(input_config, transport=debug_transport, event_hooks=hooks)
            component = registration_component(input_config,registry=registry)
            agent = WitAgent()
            handle = agent.install(component, required=True)
            try:
                async with agent:
                    await handle.wait_loaded()
                    registration = agent.context.get(TRUSTED_REGISTRATION)
                    certificates = agent.context.get(TRUSTED_CERTIFICATES)
                    if diagnostics:
                        diagnostics.emit('operation.start', action=args.action, **public_metadata(registration.state))
                    if args.action == "register":
                        result = await register_until_ready(registration, args.timeout, diagnostics)
                    elif args.action == "reconcile":
                        if not args.registration_id:
                            raise ValueError("reconcile 需要 --registration-id")
                        result = await registration.reconcile_submission(args.registration_id)
                    else:
                        # status 只恢复本地记录及刷新证书状态；issue/renew 也先取得原 AIC，不新建身份。
                        result = await registration.refresh_certificate_status()
                        if args.action in ("renew", "issue"):
                            aic = registration.state.aic
                            if not aic:
                                raise ValueError("未找到已注册 AIC，请先完成 register")
                            if args.action == "renew":
                                result = await certificates.renew(CertificateRenewRequest(aic=aic, usages=("client", "server")))
                            else:
                                result = await certificates.issue(CertificateIssueRequest(aic=aic, usage=args.usage))
            finally:
                # 先退出 WitAgent 释放 ATR 写锁，再发布正式文档。CA 暂时失败也保留已核验的正式 ACS。
                sync_component_acs(config, component)
            outputs = (public_metadata(result), [public_metadata(c) for c in certificates.certificates])
            for output in outputs:
                # 调试时 stdout 也过滤服务端 reason 中可能回显的凭据，仍保留原 JSON 结构。
                print(json.dumps(diagnostics.clean(output) if diagnostics else output, ensure_ascii=False, indent=2))
    finally:
        if debug_transport is not None:
            await debug_transport.aclose()


def main():
    parser = argparse.ArgumentParser(description="Wit 3.0 注册与证书操作工具（真实远程操作）")
    parser.add_argument("action", choices=["register", "status", "issue", "renew", "reconcile"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--usage", choices=["client", "server"], default="client")
    parser.add_argument("--registration-id")
    parser.add_argument('--debug', action='store_true', help='stderr 输出 Registry 脱敏响应、流程阶段及 traceback')
    parser.add_argument('--debug-max-chars', type=int, default=32768, help='每份调试正文显示上限，512..262144 字符')
    args = parser.parse_args()
    diagnostics = None
    try:
        if args.debug:

            diagnostics = RegistryDiagnostics((os.getenv('WIT_REGISTRY_USERNAME'), os.getenv('WIT_REGISTRY_PASSWORD')),
                                              max_chars=args.debug_max_chars)
        asyncio.run(execute(args, diagnostics))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        from avatar.protocols.identity.errors import identity_error_detail
        if diagnostics:
            diagnostics.exception(exc)
        print(f"操作未完成：{identity_error_detail(exc)}。请检查配置、注册审核状态与可信 CA；私密响应不输出。", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
