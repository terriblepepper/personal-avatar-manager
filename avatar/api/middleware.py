"""HTTP request limits, proxy trust and response policy for the management API."""
import ipaddress
import re
from tempfile import SpooledTemporaryFile
from httpx import URL

from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders, UploadFile
from starlette.formparsers import MultiPartException

from avatar.core.text_budget import TEXT_BYTES
from avatar.files.library.attachments import MAX_STORED_SIZE
from avatar.files.uploads import validation
from avatar.knowledge.media import MAX_ASSET
from avatar.skills.policy import MAX_UPLOAD


SHELL_CSP = "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; font-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"


def client_address(request):
    if not request.client:
        return 'unknown'
    try:
        peer = ipaddress.ip_address(request.client.host)
    except ValueError:
        return 'unknown'
    trusted = request.app.state.settings.trusted_proxies
    current = peer
    forwarded = ','.join(request.headers.getlist('x-forwarded-for'))
    if forwarded:
        for value in reversed(forwarded.split(',')):
            if not any(current in network for network in trusted):
                break
            try:
                current = ipaddress.ip_address(value.strip())
            except ValueError:
                return str(peer)
    return str(current)


# 职责：生成不泄露请求凭据的输入校验错误响应。
# 输入：request 为当前请求，exc 为 Pydantic/FastAPI 校验异常。
# 结果与约束：只返回字段位置、消息与类型，剔除可能包含 API Key 的输入正文，HTTP 状态为 422。
async def invalid_request(request, exc):
    # 原始校验错误可能包含整个请求正文及 API Key，响应只保留安全的字段描述。
    return JSONResponse(
        {
            "detail": [
                {"loc": e["loc"], "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
        },
        status_code=422,
    )


def streaming_upload_limit(method, path):
    if method == 'POST':
        if path == '/api/me/attachments':
            return MAX_STORED_SIZE
        if path == '/api/me/knowledge/assets':
            return MAX_ASSET + 65536
        if re.fullmatch(r'/api/conversations/[^/]+/files', path):
            return validation.MAX_CHAT_UPLOAD + 65536
    return None


def body_limit(method, path):
    limit = (
        MAX_UPLOAD + 65536
        if method == "POST" and path == "/api/me/skills"
        else 256 * 1024
        if path == "/api/me/profile"
        else 65536
    )
    if path.startswith(
        "/api/me/knowledge/builds/"
    ) and path.endswith("/questionnaire"):
        limit = 1024 * 1024
    if path.startswith(
        "/api/me/knowledge/builds/"
    ) and path.endswith("/refine"):
        limit = 256 * 1024
    if path == "/api/admin/soul-question-bank/import":
        limit = 8 * 1024 * 1024
    if re.fullmatch(r"/api/conversations/[^/]+/diagrams/promote", path):
        limit = 7 * 1024 * 1024
    if re.fullmatch(r"/api/(?:chat/(?:stream|generations)|agent-dialogues(?:/[^/]+/(?:resume|confirm|accept))?|"
                    r"agent-sessions/[^/]+/(?:messages|task-draft)|agent-intents/[^/]+/confirm|"
                    r"town/avatars/[^/]+/tasks)", path):
        # A JSON escape can take six wire bytes for one byte of decoded text.
        limit = 7 * TEXT_BYTES
        if path.startswith("/api/agent-intents/"):
            limit += 12 * 60000  # Optional shared_content, up to 60k Unicode characters.
    return limit


def response_headers(path, headers):
    headers["X-Content-Type-Options"] = "nosniff"
    headers["X-Frame-Options"] = "DENY"
    headers.setdefault("Referrer-Policy", "same-origin")
    if path == "/api/auth/oidc/callback":
        headers["Referrer-Policy"] = "no-referrer"
    if path.startswith("/api/"):
        headers["Cache-Control"] = "no-store"
    if path in (
        "/",
        "/admin",
        "/platform-entry",
    ) or path.startswith("/static/"):
        headers["Cache-Control"] = "no-cache"
    if path in ("/", "/admin", "/platform-entry"):
        headers["Referrer-Policy"] = "no-referrer"
        headers["Content-Security-Policy"] = SHELL_CSP
    if path == "/static/preview/mermaid.html":
        # 仅受信静态渲染页允许同源嵌入；iframe 自身不带 allow-same-origin，拥有不透明 origin。
        headers["X-Frame-Options"] = "SAMEORIGIN"
        headers["Referrer-Policy"] = "no-referrer"
        headers["Content-Security-Policy"] = (
            "sandbox allow-scripts; default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; connect-src 'none'; object-src 'none'; frame-src 'none'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'"
        )
    if path == "/static/preview/html.html":
        headers["X-Frame-Options"] = "SAMEORIGIN"
        headers["Referrer-Policy"] = "no-referrer"
        headers["Content-Security-Policy"] = (
            "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline' data:; worker-src 'none'; "
            "style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data: blob:; connect-src 'none'; "
            "frame-ancestors 'self'; base-uri 'none'; form-action 'none'"
        )


class SecurityMiddleware:
    def __init__(self, app, public_url):
        self.app = app
        public = URL(public_url)
        if public.scheme not in ('http', 'https') or not public.host or public.userinfo:
            raise ValueError('AVATAR_PUBLIC_URL 须为没有内嵌凭据的 HTTP/HTTPS 地址')
        self.origin = f'{public.scheme}://{public.netloc.decode()}'

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        method, path = scope['method'], scope['path']

        async def secure_send(message):
            if message['type'] == 'http.response.start':
                response_headers(path, MutableHeaders(scope=message))
            await send(message)

        if method in ('GET', 'HEAD', 'OPTIONS'):
            return await self.app(scope, receive, secure_send)

        async def reject(status, detail):
            await JSONResponse({'detail': detail}, status)(scope, receive, secure_send)

        headers = Headers(scope=scope)
        origins = headers.getlist('origin')
        if origins and origins != [self.origin]:
            return await reject(403, '禁止跨站请求')
        lengths = headers.getlist('content-length')
        if lengths and (len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,20}', lengths[0])):
            return await reject(400, '请求长度无效')
        upload_limit = streaming_upload_limit(method, path)
        limit = upload_limit if upload_limit is not None else body_limit(method, path)
        if lengths and int(lengths[0]) > limit:
            return await reject(413, '请求内容过大')

        size = 0
        async def limited_receive():
            nonlocal size
            message = await receive()
            size += len(message.get('body', b''))
            if size > limit:
                # Starlette closes incomplete multipart files on this exception.
                raise MultiPartException('请求内容过大')
            return message

        if upload_limit is not None:
            # Uploads authorize first and consume the entire stream before storing.
            try:
                return await self.app(scope, limited_receive, secure_send)
            except MultiPartException:
                if size <= limit:
                    raise
                return await reject(413, '请求内容过大')

        # Some write routes do not consume their body. Validate before any side effect.
        with SpooledTemporaryFile(max_size=1024 * 1024) as spool:
            body = UploadFile(spool)
            try:
                while True:
                    message = await limited_receive()
                    if message['type'] == 'http.disconnect':
                        return
                    await body.write(message.get('body', b''))
                    if not message.get('more_body', False):
                        break
            except MultiPartException:
                return await reject(413, '请求内容过大')
            await body.seek(0)
            remaining = size

            async def replay():
                nonlocal remaining
                if remaining < 0:
                    return await receive()
                chunk = await body.read(min(65536, remaining))
                remaining -= len(chunk)
                more = remaining > 0
                if not more:
                    remaining = -1
                return {'type':'http.request', 'body':chunk, 'more_body':more}

            await self.app(scope, replay, secure_send)
