# 模块说明：平台绑定：只接受可信适配器核验后的身份，采用一对一绑定与受限临时入口，不合并账户。
"""Verified platform bindings; never merge accounts by student ID."""
from dataclasses import dataclass
import hashlib
import secrets
import time
from urllib.parse import urlsplit
from fastapi import HTTPException
from avatar.core.db import event, now
from avatar.accounts.sessions import issue_session

PLATFORM = 'beiyou-dingdang'


# 职责：表示可信平台适配器已核验的请求者身份。
# 输入：account_id 为平台稳定账号标识，platform 固定为北邮小镇叮当平台标识。
# 结果与约束：这是内部身份契约，本类不调用平台认证接口；不得由匿名浏览器声明直接构造可信身份。
@dataclass(frozen=True)
class VerifiedPlatformIdentity:
    # 只有真实叮当身份接口核验实际请求者后，适配器才可构造此身份对象。
    # 本对象只是内部约定，不是公开身份核验接口，也不会自行执行认证。
    account_id: str
    platform: str = PLATFORM

    # 职责：检查已核验身份对象的结构与平台范围。
    # 输入：读取 account_id 和 platform，拒绝空值、首尾空白、控制字符及过长标识。
    # 结果与约束：结构不合法抛出 ValueError；结构合法不代表已执行真实平台身份核验。
    def __post_init__(self):
        if self.platform != PLATFORM or not isinstance(self.account_id,str) or not self.account_id or self.account_id != self.account_id.strip() or len(self.account_id)>256 or any(ord(c)<32 for c in self.account_id):
            raise ValueError('平台身份无效')


# 职责：管理平台账号绑定和一次性个人资料入口。
# 输入：依赖 Database，关联平台账号、本地账户、令牌哈希及签发时绑定快照。
# 结果与约束：不按学号合并账户，不提供匿名签发接口；绑定与消费令牌使用写事务。
class PlatformBindingService:
    # 职责：注入绑定流程使用的数据库。
    # 输入：database 为应用共享的 Database 实例。
    # 结果与约束：仅保存依赖，身份核验仍由未来平台适配器完成。
    def __init__(self,database):self.database=database

    # 职责：读取并验证一次性入口令牌及其绑定快照。
    # 输入：db 为调用方连接，token 为原始入口令牌，查询时转换为 SHA-256 摘要。
    # 结果与约束：返回令牌记录；失效或已用返回 410，当前绑定与签发时不符返回 409；本方法不消费令牌。
    @staticmethod
    def _token(db,token):
        row=db.execute('SELECT * FROM platform_entry_tokens WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row or row['used_at'] or row['expires']<=time.time():
            raise HTTPException(410,'临时入口已失效或已使用，请从北邮小镇叮当平台重新发起')
        binding=db.execute('SELECT user_id FROM platform_bindings WHERE platform=? AND account_id=?',(row['platform'],row['account_id'])).fetchone()
        # 签发时未绑定的入口不能在关系变化后获得新账户身份，绑定快照不一致必须重新签发。
        if (binding['user_id'] if binding else None)!=row['issued_for_user_id']:
            raise HTTPException(409,'平台绑定状态已变化，请重新获取临时入口')
        return row

    # 职责：为已核验平台身份签发一次性资料入口。
    # 输入：identity 必须为 VerifiedPlatformIdentity；base_url 为可信服务根地址；ttl 为 60–900 秒，默认 300 秒。
    # 结果与约束：返回 fragment 携带随机令牌的 URL；只保存哈希和绑定快照并清理过期记录，不自动绑定或发送链接。
    def issue_entry_link(self,identity,base_url,ttl=300):
        if not isinstance(identity,VerifiedPlatformIdentity):raise TypeError('需要已核验的平台身份')
        parsed=urlsplit(base_url)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
            raise ValueError('入口地址应为可信 HTTP/HTTPS 服务根地址')
        if not 60<=ttl<=900:raise ValueError('临时链接有效期应为 60..900 秒')
        # 使用不可预测的随机凭证，URL 只交给核验后的账号本人；数据库不保存原令牌。
        token=secrets.token_urlsafe(32)
        with self.database.connect(write=True) as db:
            row=db.execute('SELECT user_id FROM platform_bindings WHERE platform=? AND account_id=?',(identity.platform,identity.account_id)).fetchone()
            db.execute('DELETE FROM platform_entry_tokens WHERE expires<?',(time.time(),))
            db.execute('INSERT INTO platform_entry_tokens(token_hash,platform,account_id,issued_for_user_id,expires) VALUES(?,?,?,?,?)',
                       (hashlib.sha256(token.encode()).hexdigest(),identity.platform,identity.account_id,row['user_id'] if row else None,time.time()+ttl))
        return base_url.rstrip('/')+'/platform-entry#token='+token

    # 职责：预览有效入口应进入资料页还是注册页。
    # 输入：token 为链接中的随机令牌。
    # 结果与约束：返回 mode、平台名称及账号标识；只校验不消费，供前端决定页面流程。
    def preview(self,token):
        with self.database.connect() as db:row=self._token(db,token)
        return {'mode':'profile' if row['issued_for_user_id'] else 'register','platform':'北邮小镇叮当平台','account_id':row['account_id']}

    # 职责：在调用者事务内建立平台账号与本地账户的一对一绑定。
    # 输入：db 必须为已有写事务；user_id 为本地账户；token 证明平台核验结果。
    # 结果与约束：拒绝覆盖任何已有绑定，插入关系并消费令牌、记录事件；异常由外层事务回滚，注册与绑定可原子完成。
    def bind_in_transaction(self,db,user_id,token):
        row=self._token(db,token)
        if row['issued_for_user_id']:raise HTTPException(409,'该平台账号已绑定，请使用新的资料入口')
        if db.execute('SELECT 1 FROM platform_bindings WHERE platform=? AND user_id=?',(row['platform'],user_id)).fetchone():
            raise HTTPException(409,'当前账户已经绑定北邮小镇叮当平台，不能覆盖绑定')
        db.execute('INSERT INTO platform_bindings VALUES(?,?,?,?)',(row['platform'],row['account_id'],user_id,now()))
        db.execute('UPDATE platform_entry_tokens SET used_at=? WHERE token_hash=?',(now(),row['token_hash']))
        event(db,user_id,'verified-platform','绑定北邮小镇叮当平台账号')

    # 职责：为已完成完整本地或 OIDC 认证的账户完成平台绑定。
    # 输入：user 为当前完整会话账户，token 为未绑定账号的有效入口令牌。
    # 结果与约束：临时资料会话不能执行；成功返回提示，不改变原密码、资料、模型 Key 或管理员角色。
    def bind_existing(self,user,token):
        if user.get('access_scope','full')!='full':raise HTTPException(403,'请完成正常登录后绑定')
        with self.database.connect(write=True) as db:self.bind_in_transaction(db,user['id'],token)
        return {'message':'已绑定北邮小镇叮当平台账号'}

    # 职责：消费已绑定平台账号的入口并签发受限登录会话。
    # 输入：token 必须有效、未使用，且绑定关系与签发快照相同。
    # 结果与约束：同一事务内消费令牌并生成 1800 秒 profile 会话；返回令牌、CSRF 和受限用户信息，不改变数据库真实角色。
    def redeem(self,token):
        with self.database.connect() as db:
            owner=self._token(db,token)['issued_for_user_id']
        if not owner:raise HTTPException(409,'平台账号尚未绑定，请注册或登录已有账户完成绑定')
        with self.database.connect(write=True, account_scope=owner) as db:
            row=self._token(db,token)
            if row['issued_for_user_id']!=owner:raise HTTPException(409,'平台绑定状态已变化，请重新获取临时入口')
            db.execute('UPDATE platform_entry_tokens SET used_at=? WHERE token_hash=?',(now(),row['token_hash']))
            # 消费令牌和创建受限会话同事务完成，避免失败后出现已消费但未登录的半成状态。
            session=issue_session(db,row['issued_for_user_id'],1800,'profile')
            event(db,row['issued_for_user_id'],'verified-platform','通过平台临时入口进入个人资料')
        return session

    # 职责：查询指定本地账户已有的平台绑定。
    # 输入：user_id 为已通过权限检查的本地账户 ID。
    # 结果与约束：返回平台、账号标识及绑定时间列表；不查询其他账户密码，也不签发新入口。
    def bindings(self,user_id):
        with self.database.connect() as db:
            return [dict(r) for r in db.execute('SELECT platform,account_id,created_at FROM platform_bindings WHERE user_id=?',(user_id,))]
