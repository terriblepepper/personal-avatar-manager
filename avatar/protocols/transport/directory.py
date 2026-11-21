# 模块说明：TLS 装载和通信读取本地证书快照；维护目录仍可使用有界远端缓存。
import asyncio
import logging
import time
from datetime import datetime, timezone
import httpx
from contextvars import ContextVar
from avatar.protocols.transport.tls_listener import FixedEntityListener
from cryptography import x509

ENTITY_CACHE_SECONDS = 60.0
_tls_background=ContextVar('avatar_tls_background',default=False)


class PassiveTlsListener(FixedEntityListener):
    """TLS 定时装载只核对本地快照，不主动查询远端证书状态。"""
    async def _refresh_tls(self,ctx):
        token=_tls_background.set(True)
        try:await super()._refresh_tls(ctx)
        finally:_tls_background.reset(token)


class CertificateStatusRateLimited(ValueError):
    """CA cooldown before any peer invocation; stale identities remain unavailable."""


# 职责：组合真实 ATR 目录，限制重复 CA 状态查询而不永久缓存证书。
# 输入：registrations 为明确的 AIC/config 映射，source 为 wit 公开 EntityDirectory；时钟可注入隔离测试。
# 结果与约束：最多缓存 60 秒且不超出配置新鲜度及证书有效期；文件变化立即失效，刷新失败不使用旧身份，429 期间关闭接入并遵守 Retry-After。
class CachedEntityDirectory:
    # 职责：保存身份配置、原始目录及串行刷新锁。
    # 输入：ttl 是应用缓存秒数，clock 为单调时钟，utcnow 为带时区时间提供者。
    # 结果与约束：构造不读取状态或请求 CA，不接受无界或非正缓存时间。
    def __init__(self, registrations, source, *, ttl=None, clock=None, utcnow=None):
        self.registrations, self.source = dict(registrations), source
        self.ttl = 20.0 if ttl is None else float(ttl)
        if not 0 < self.ttl <= ENTITY_CACHE_SECONDS:
            raise ValueError('身份缓存必须在 0..60 秒内')
        self.clock = clock or time.monotonic
        self.utcnow = utcnow or self.current_utc
        self.cached, self.cooldown = {}, {}
        self.lock = asyncio.Lock()

    # 职责：生成用于证书有效期判断的当前 UTC 时间。
    # 输入：无参数，使用带时区系统时间。
    # 结果与约束：与单调缓存时钟分开，避免时间回拨掩盖证书过期。
    @staticmethod
    def current_utc():
        return datetime.now(timezone.utc)

    # 职责：读取身份关键文件的修订标记，不读取私钥正文。
    # 输入：config 指明候选、ATR 状态及 CA 信任文件。
    # 结果与约束：原子替换、内容修改、权限变化或文件缺失均使旧快照不可复用；符号链接拒绝。
    @staticmethod
    def revision(config):
        paths = [config.paths.acs_path, config.paths.state_dir / 'atr-state.json',
                 config.paths.acs_path.parent / 'registration.json']
        if config.endpoints.ca_cert_path is not None:
            paths.append(config.endpoints.ca_cert_path)
        result = []
        for path in paths:
            if path.is_symlink() or not path.is_file():
                raise ValueError('身份状态文件不可用')
            info = path.stat()
            result.append((str(path), info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_mode))
        return tuple(result)

    # 职责：获得已验证身份双证书中最早的到期时间。
    # 输入：entity 为底层目录真实返回的 LocalEntity。
    # 结果与约束：只读公开证书，任何缺失/无效证书不进入缓存；不使用缓存延长证书有效期。
    @staticmethod
    def expires_at(entity):
        material = entity.material
        values = (material.certificate, material.server_certificate)
        if not all(values):
            raise ValueError('系统双证书未就绪')
        return min(x509.load_pem_x509_certificate(value.encode()).not_valid_after_utc for value in values)

    def available(self, aic):
        cached = self.cached.get(aic)
        if not cached or self.clock() >= cached[2] or self.utcnow() >= cached[3]:
            return False
        try:
            return self.revision(self.registrations[aic]) == cached[1]
        except (OSError, ValueError):
            return False

    # 职责：选择仍处于有效缓存窗口的身份，或执行完整 ATR/CA 校验。
    # 输入：aic 必须在服务器显式配置的注册映射中。
    # 结果与约束：并发请求共用一次刷新；过期或文件变化时先丢弃旧材料；CA 限流期间拒绝接入，不返回陈旧快照或绕过证书校验。
    async def select(self, aic):
        config = self.registrations[aic]
        async with self.lock:
            revision = self.revision(config)
            cached = self.cached.get(aic)
            if _tls_background.get() and not getattr(self.source,'local_only',False):
                if cached and cached[1]==revision and self.utcnow()<cached[3]:return cached[0]
                raise ValueError('本地部署证书不可用，请手动 Health 检查')
            if cached and cached[1] == revision and self.clock() < cached[2] and self.utcnow() < cached[3]:
                return cached[0]
            self.cached.pop(aic, None)
            cooldown = self.cooldown.get(aic)
            if cooldown and cooldown[0] == revision and self.clock() < cooldown[1]:
                raise CertificateStatusRateLimited('CA 状态查询限流，接入暂时关闭')
            self.cooldown.pop(aic, None)
            checked_at = self.clock()
            try:
                entity = await self.source.select(aic)
            except httpx.HTTPStatusError as error:
                logging.getLogger(__name__).warning('Identity CA check failed: aic=%s HTTP=%s',aic,error.response.status_code)
                if error.response.status_code == 429:
                    try:
                        delay = float(error.response.headers.get('retry-after', '60'))
                    except ValueError:
                        delay = 60
                    self.cooldown[aic] = (revision, self.clock() + max(1, min(300, delay)))
                raise
            if entity.aic != aic or self.revision(config) != revision:
                raise ValueError('身份或状态文件在刷新期间发生变化')
            expires = self.expires_at(entity)
            if expires <= self.utcnow():
                raise ValueError('身份证书已经过期')
            ttl = min(self.ttl, config.certificate_status.max_age)
            self.cached[aic] = (entity, revision, checked_at + ttl, expires)
            return entity
