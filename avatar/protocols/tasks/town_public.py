"""小镇公开访客鉴权；认证不建立业务账号、分身或证书。"""
import hashlib
from dataclasses import replace
import jwt
from fastapi import HTTPException
from avatar.accounts.oidc import OidcService
from avatar.core.db import dump
from avatar.protocols.tasks.store import ManagementTaskError


class TownPublicAuthorization:
    def __init__(self, database, settings):
        self.settings = settings
        self.oidc = OidcService(database, replace(settings, oidc_enabled=settings.town_public_enabled and bool(settings.town_oidc_issuer),
            oidc_issuer=settings.town_oidc_issuer, oidc_client_id=settings.town_oidc_client_id))

    async def authorize(self, context):
        if not self.settings.town_public_enabled:
            raise ManagementTaskError(-32009, '小镇公开访客对话尚未启用')
        headers = {k.lower(): v for k, v in getattr(context, 'headers', {}).items()}
        value = headers.get('authorization', '')
        if not value:
            # 无用户凭据只创建访客作用域；peer 来自 mTLS，HTTP 使用随机会话 Cookie。
            session=headers.get('x-avatar-visitor-session','')
            if not isinstance(session,str) or len(session)>128:
                raise ManagementTaskError(-32602,'访客会话标识无效')
            scope=['anonymous',context.peer_aic,getattr(context,'group_id',None) or '',session]
            return {'issuer':'','subject':'','anonymous':True,
                    'visitor_id':hashlib.sha256(dump(scope).encode()).hexdigest()}
        if not isinstance(value, str) or not value.startswith('Bearer ') or not 1 <= len(value[7:]) <= 16384:
            raise ManagementTaskError(-32008, '访客 access token 格式无效')
        if not (self.settings.town_oidc_issuer and self.settings.town_oidc_client_id and self.settings.town_oidc_audiences):
            raise ManagementTaskError(-32008, '访客认证尚未配置')
        token = value[7:]
        try:
            discovery = await self.oidc.discovery()
            keys = await self.oidc.request_json('GET', discovery['jwks_uri'])
            header = jwt.get_unverified_header(token)
            if header.get('alg') != 'RS256' or not isinstance(header.get('kid'), str):
                raise ValueError('无效签名算法')
            matching = [k for k in keys.get('keys', []) if k.get('kid') == header['kid']
                        and k.get('use', 'sig') == 'sig' and k.get('alg', 'RS256') == 'RS256']
            if len(matching) != 1 or not self.settings.town_oidc_audiences:
                raise ValueError('公钥或受众配置无效')
            key = jwt.PyJWK.from_dict(matching[0], algorithm='RS256').key
            claims = jwt.decode(token, key, algorithms=['RS256'], issuer=self.oidc.issuer,
                audience=list(self.settings.town_oidc_audiences),
                options={'require': ['iss', 'sub', 'aud', 'exp', 'iat', 'azp']})
            if (claims['azp'] != self.settings.town_oidc_client_id or not isinstance(claims['sub'], str)
                    or not 1 <= len(claims['sub']) <= 255
                    or type(claims['exp']) not in (int, float) or type(claims['iat']) not in (int, float)
                    or not isinstance(claims.get('scope'), str) or 'openid' not in claims['scope'].split()):
                raise ValueError('访客令牌声明无效')
            info = await self.oidc.request_json('GET', discovery['userinfo_endpoint'], authentication=True,
                                               headers={'Authorization': value})
            if info.get('sub') != claims['sub']:
                raise ValueError('访客主体不一致')
        except HTTPException as exc:
            raise ManagementTaskError(-32008 if exc.status_code == 401 else -32603,
                '访客凭据失效' if exc.status_code == 401 else '访客认证服务暂不可用') from None
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, AttributeError):
            raise ManagementTaskError(-32008, '访客令牌签名、issuer、受众或权限无效') from None
        return {'issuer': self.oidc.issuer, 'subject': claims['sub'],
                'visitor_id': hashlib.sha256(dump([self.oidc.issuer, claims['sub']]).encode()).hexdigest()}
