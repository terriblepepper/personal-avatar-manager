# 模块说明：Keycloak OIDC 边界；使用授权码、PKCE 和验签声明建立业务会话，不接收用户密码或短信验证码。
import asyncio
import base64
import hashlib
import hmac
import json
import secrets
from psycopg import IntegrityError
import time
import uuid
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from fastapi import HTTPException
from avatar.core.http_pool import HttpTransportPool
from avatar.accounts.auth import hash_password, issue_session
from avatar.core.db import dump, event, now


# 职责：对随机状态和浏览器凭据生成摘要。
# 输入：value 是服务端随机字符串；不会接收密码。
# 结果与约束：返回 SHA-256 十六进制摘要，数据库不保存可兑换的原状态或浏览器凭据。
def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


# 职责：封装 OIDC 协议、浏览器绑定和本地业务账号关联。
# 输入：database 与 Settings；测试可设置 transport 注入隔离 HTTP 服务。
# 结果与约束：仅信任固定 issuer 的签名账号标识；不要求手机号、不绑定根管理员、不按用户名关联旧账号或继承外部角色。
class OidcService:
    # 职责：保存依赖并验证启用时的固定外部地址。
    # 输入：settings 包含 issuer、client_id、可选后端 secret 及固定回调域名。
    # 结果与约束：不在初始化时联网；issuer 必须 HTTPS，配置中拒绝凭据、查询及片段。
    def __init__(self, database, settings):
        self.database, self.settings, self.transport = database, settings, None
        self.http_pool = HttpTransportPool()
        self.issuer = settings.oidc_issuer.rstrip('/')
        self.public_url = settings.oidc_public_url.rstrip('/')
        if settings.oidc_enabled:
            for value, schemes in ((self.issuer, ('https',)), (self.public_url, ('http', 'https'))):
                parsed = urlsplit(value)
                if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ValueError('OIDC 地址配置无效')
            if not settings.oidc_client_id:
                raise ValueError('OIDC client_id 不能为空')

    # 职责：向浏览器返回不含凭据的认证入口配置。
    # 输入：无；固定回调和账号管理地址来自可信部署配置。
    # 结果与约束：Secret 永不返回；默认关闭时维持旧 Demo 认证，启用后仅根管理员使用本地密码。
    def public_config(self):
        return {'oidc_enabled': self.settings.oidc_enabled, 'issuer': self.issuer, 'runtime_mode': self.settings.provider,
                'client_id': self.settings.oidc_client_id, 'account_url': self.issuer + '/account',
                'callback_url': self.public_url + '/api/auth/oidc/callback'}

    async def _connection(self, url):
        return (), self.transport if self.transport is not None else httpx.AsyncHTTPTransport(trust_env=False)

    # 职责：执行有时限、有响应上限且不跟随重定向的认证服务请求。
    # 输入：method/url 是内部确定的 OIDC 端点，kwargs 可含授权码或服务凭据。
    # 结果与约束：禁用环境代理，不记录请求和远端正文；网络/协议失败统一返回安全错误。
    async def request_json(self, method, url, *, authentication=False, **kwargs):
        try:
            async with self.http_pool.session(httpx.URL(url), self._connection, 15,
                                              injected=self.transport is not None) as (_, client):
                async with client.stream(method, url, **kwargs) as response:
                    response.raise_for_status()
                    payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload) > 1024 * 1024:
                            raise ValueError('认证响应过大')
                    value = json.loads(payload)
                    if not isinstance(value, dict):
                        raise ValueError('认证响应类型错误')
                    return value
        except httpx.HTTPStatusError as exc:
            if authentication and exc.response.status_code in (401, 403):
                raise HTTPException(401, 'Keycloak 访问令牌无效或已过期') from None
            raise HTTPException(502, '统一认证服务请求失败，请稍后重试') from None
        except (httpx.HTTPError, ValueError, UnicodeError):
            raise HTTPException(502, '统一认证服务请求失败，请重新登录；未修改本地账号') from None

    # 职责：读取并约束 OIDC discovery 中的端点。
    # 输入：无，issuer 固定于服务配置，不从请求参数获取。
    # 结果与约束：要求 issuer 精确匹配、端点同源 HTTPS、支持 S256；不允许发现文档引导携带授权码访问其他域名。
    async def discovery(self):
        if not self.settings.oidc_enabled:
            raise HTTPException(409, '统一认证尚未启用')
        value = await self.request_json('GET', self.issuer + '/.well-known/openid-configuration')
        origin = urlsplit(self.issuer)
        if value.get('issuer') != self.issuer or 'S256' not in value.get('code_challenge_methods_supported', []):
            raise HTTPException(502, '统一认证 issuer 或 PKCE 配置不匹配')
        for key in ('authorization_endpoint', 'token_endpoint', 'userinfo_endpoint', 'jwks_uri', *(['end_session_endpoint'] if 'end_session_endpoint' in value else [])):
            endpoint = urlsplit(value.get(key, ''))
            if (endpoint.scheme, endpoint.netloc) != (origin.scheme, origin.netloc) or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
                raise HTTPException(502, '统一认证端点配置不匹配')
        return value

    # 职责：建立短期、绑定当前浏览器的授权码事务。
    # 输入：受限返回入口、可选平台绑定令牌及请求 IP；身份在本人资料页选择，不属于认证参数。
    # 结果与约束：返回授权地址和 HttpOnly Cookie 原值；状态 5 分钟一次使用，携带 S256/nonce，按 IP 限流并清除过期状态。
    async def start(self, return_to, platform_token, ip):
        state, browser, verifier, nonce = (secrets.token_urlsafe(32) for _ in range(4))
        current = time.time()
        def admit():
            with self.database.connect(write=True) as db:
                key = 'oidc:' + digest(ip)
                limit = db.execute('SELECT * FROM login_limits WHERE key=?', (key,)).fetchone()
                if limit and limit['reset_at'] > current and limit['attempts'] >= 20:
                    raise HTTPException(429, '统一认证请求过多，请稍后重试')
                attempts = limit['attempts'] + 1 if limit and limit['reset_at'] > current else 1
                reset = limit['reset_at'] if limit and limit['reset_at'] > current else current + 300
                db.execute('INSERT OR REPLACE INTO login_limits VALUES(?,?,?)', (key, attempts, reset))
                db.execute('DELETE FROM oidc_login_states WHERE expires<?', (current,))
        await asyncio.to_thread(admit)
        configuration = await self.discovery()
        if platform_token:
            return_to = '/#profile'
            from avatar.accounts.platform_binding import PlatformBindingService
            await asyncio.to_thread(PlatformBindingService(self.database).preview,platform_token)
        def save_state():
            with self.database.connect(write=True) as db:
                db.execute('INSERT INTO oidc_login_states VALUES(?,?,?,?,?,?,?,?)',
                           (digest(state), digest(browser), verifier, nonce, 'student', return_to, platform_token, current + 300))
        await asyncio.to_thread(save_state)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        params = {'client_id': self.settings.oidc_client_id, 'redirect_uri': self.public_config()['callback_url'],
                  'response_type': 'code', 'scope': 'openid profile', 'state': state,
                  'nonce': nonce, 'code_challenge': challenge, 'code_challenge_method': 'S256'}
        return configuration['authorization_endpoint'] + '?' + urlencode(params), browser

    # 职责：校验并一次性取出当前浏览器的授权事务。
    # 输入：state 来自 OIDC 回调，browser 来自 HttpOnly 短期 Cookie。
    # 结果与约束：过期、缺失、错误浏览器和重放均拒绝；正确状态先消费再进行远程请求，失败须重新发起登录。
    def consume(self, state, browser):
        with self.database.connect(write=True) as db:
            row = db.execute('SELECT * FROM oidc_login_states WHERE state_hash=?', (digest(state),)).fetchone()
            if not row or row['expires'] <= time.time() or not browser or not hmac.compare_digest(row['browser_hash'], digest(browser)):
                raise HTTPException(400, '登录状态失效或浏览器不匹配，请重新发起登录')
            db.execute('DELETE FROM oidc_login_states WHERE state_hash=?', (digest(state),))
            return dict(row)

    # 职责：验证 Keycloak 签发的 ID Token，并要求精确 nonce 和客户端受众。
    # 输入：encoded 是授权码换回的 ID Token，keys 为固定 JWKS 端点返回的公钥集。
    # 结果与约束：只接受部署已探测的 RS256 签名，检查 exp/iat/iss/aud/sub/nonce；拒绝算法降级和不匹配 azp。
    def verify_identity(self, encoded, keys, nonce):
        try:
            header = jwt.get_unverified_header(encoded)
            if header.get('alg') != 'RS256' or not isinstance(header.get('kid'), str):
                raise ValueError('不支持的签名')
            candidates = [k for k in keys.get('keys', []) if k.get('kid') == header['kid'] and k.get('use', 'sig') == 'sig' and k.get('alg', 'RS256') == 'RS256']
            if len(candidates) != 1:
                raise ValueError('签名公钥不唯一')
            key = jwt.PyJWK.from_dict(candidates[0], algorithm='RS256').key
            claims = jwt.decode(encoded, key, algorithms=['RS256'], audience=self.settings.oidc_client_id,
                                issuer=self.issuer, options={'require': ['exp', 'iat', 'iss', 'aud', 'sub', 'nonce']})
            if not isinstance(claims['nonce'], str) or not hmac.compare_digest(claims['nonce'], nonce):
                raise ValueError('nonce 不匹配')
            if claims.get('azp', self.settings.oidc_client_id) != self.settings.oidc_client_id:
                raise ValueError('azp 不匹配')
            if isinstance(claims['aud'], list) and len(claims['aud']) > 1 and 'azp' not in claims:
                raise ValueError('缺少 azp')
            if type(claims['exp']) not in (int, float) or type(claims['iat']) not in (int, float):
                raise ValueError('令牌时间类型错误')
            if not isinstance(claims['sub'], str) or not 1 <= len(claims['sub']) <= 255:
                raise ValueError('主体无效')
            return claims
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise HTTPException(401, '统一认证身份校验失败，请重新登录') from None

    # 职责：以经签名验证的 Keycloak 账号主体关联本地业务身份。
    # 输入：claims 含可信 issuer/sub 及可选 preferred_username/name，transaction 含平台绑定令牌及返回入口。
    # 结果与约束：身份只由固定 issuer/sub 决定；用户名仅用于展示，手机号不参与登录、同步或合并；根账号禁止外部绑定。
    def identify(self, claims, transaction, device=None):
        identity_username = claims.get('preferred_username', '')
        identity_username = identity_username[:160] if isinstance(identity_username, str) else ''
        try:
            with self.database.connect(write=True) as db:
                identity = db.execute('SELECT * FROM oidc_identities WHERE issuer=? AND subject=?', (self.issuer, claims['sub'])).fetchone()
                linked = False
                if not identity:
                    identity = db.execute('SELECT * FROM oidc_identity_links WHERE issuer=? AND subject=?', (self.issuer, claims['sub'])).fetchone()
                    linked = identity is not None
                if identity:
                    user_id = identity['user_id']
                    user = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
                    if not user or user['is_root']:
                        raise HTTPException(403, '根管理员仅允许本地登录')
                else:
                    user_id = str(uuid.uuid4())
                    # 新账号仅建立业务草稿；学生/教师身份由本人在个人资料页选择，旧账号身份不改变。
                    person_type = 'student'
                    username = 'kc_' + uuid.uuid4().hex
                    # 本地不可知随机密码仅兼容旧表 NOT NULL，不接受该账户本地密码登录。
                    db.execute("INSERT INTO users(id,username,password,role,created_at,person_type,teacher_id,account_status,onboarding_stage) VALUES(?,?,?,?,?,?,?,?,'welcome')",
                               (user_id, username, hash_password(secrets.token_urlsafe(48)), 'user', now(), person_type,
                                None,
                                'pending' if person_type == 'teacher' else 'active'))
                    modified = now()
                    name = claims.get('name', '')
                    name = name[:80] if isinstance(name, str) else ''
                    db.execute('INSERT INTO profile_records(user_id,draft,last_modified_at,updated_at) VALUES(?,?,?,?)',
                               (user_id, dump({'person_type': person_type, 'name': name, 'student_id': ''}), modified, modified))
                    db.execute('INSERT INTO oidc_identities(issuer,subject,user_id) VALUES(?,?,?)', (self.issuer, claims['sub'], user_id))
                    event(db, user_id, username, '统一认证首次建立业务账户', actor_id=user_id)
                # verified_at 兼容历史字段名，现在记录统一账号验签登录时间；不表示手机号已验证。
                table = 'oidc_identity_links' if linked else 'oidc_identities'
                db.execute('UPDATE '+table+' SET username=?,verified_at=? WHERE issuer=? AND subject=?',
                           (identity_username, now(), self.issuer, claims['sub']))
                if transaction.get('platform_token'):
                    from avatar.accounts.platform_binding import PlatformBindingService
                    PlatformBindingService(self.database).bind_in_transaction(db, user_id, transaction['platform_token'])
                from avatar.accounts.login_devices import record_device
                record_device(db,user_id,device)
                # ID Token expires after authentication; the server-owned session has its own bounded lifetime.
                duration = 43200 if claims['exp'] > time.time() else 0
                if duration <= 0:
                    raise HTTPException(401, '统一认证令牌已过期，请重新登录')
                token, csrf, account = issue_session(db, user_id, duration, auth_method='oidc')
                account.update(identity_username=identity_username, oidc_subject=claims['sub'], identity_verified=True)
                return token, csrf, account, duration
        except IntegrityError:
            raise HTTPException(409, '统一身份已关联其他账户，请联系管理员；未合并或覆盖资料') from None

    # 职责：兑换授权码、校验身份并建立本地业务会话。
    # 输入：回调 state/code、浏览器 Cookie，以及可选授权响应 issuer。
    # 结果与约束：固定回调和 PKCE，userinfo 必须与签名 sub 相同，凭据仅驻留请求内存，不返回浏览器或存入业务资料。
    async def finish(self, state, code, browser, response_issuer=None, device=None):
        transaction = await asyncio.to_thread(self.consume,state,browser)
        if response_issuer is not None and response_issuer != self.issuer:
            raise HTTPException(400, '授权响应 issuer 不匹配')
        configuration = await self.discovery()
        form = {'grant_type': 'authorization_code', 'client_id': self.settings.oidc_client_id,
                'code': code, 'redirect_uri': self.public_config()['callback_url'], 'code_verifier': transaction['verifier']}
        if self.settings.oidc_client_secret:
            form['client_secret'] = self.settings.oidc_client_secret
        tokens = await self.request_json('POST', configuration['token_endpoint'], data=form)
        keys = await self.request_json('GET', configuration['jwks_uri'])
        claims = await asyncio.to_thread(self.verify_identity,tokens.get('id_token', ''),keys,transaction['nonce'])
        access_token = tokens.get('access_token')
        if not isinstance(access_token, str) or not access_token:
            raise HTTPException(502, '统一认证未返回访问凭据')
        info = await self.request_json('GET', configuration['userinfo_endpoint'], headers={'Authorization': 'Bearer ' + access_token})
        if info.get('sub') != claims['sub']:
            raise HTTPException(401, '统一认证用户信息与令牌主体不匹配')
        # 只取可信端点的资料声明，绝不覆盖已验签的 iss/aud/exp/nonce。
        claims.update({k: info[k] for k in ('preferred_username', 'name') if k in info})
        account = await asyncio.to_thread(self.identify,claims,transaction,device)
        return account, transaction['return_to']
