# 模块说明：Registry 派生请求适配。按部署的 Registry v2.2.1b002 契约提交实体证书字段。
import os
import ssl
import tempfile
from pathlib import Path
from .certificates import CertificateTransport, trust_context
import httpx
from wit.acps.atr import RegistryClient, RegistryRequestRejected, RegistrationReview


# 职责：通过 Registry mTLS 平面注册派生实体，其余操作委托 wit 的公开 RegistryClient。
# 输入：config 是实体 AtrConfig；transport 只供隔离测试注入，正式使用真实 TLS。
# 结果与约束：不修改 wit 私有方法；令牌只在内存，私钥临时文件 0600 并及时清理；只对读取响应做有界的继承字段补齐，不写回 Registry。
class EntityRegistry:
    # 职责：保存实体网络配置及公开客户端委托。
    # 输入：config 提供 Registry/CA 路径和服务器凭据；transport/event_hooks 供 CLI 显式诊断注入；delegate 供隔离测试替身使用。
    # 结果与约束：构造不发网络请求；对象不能序列化进日志或普通用户响应。
    def __init__(self, config, transport=None, *, delegate=None, event_hooks=None):
        self.config, self.transport = config, transport
        self.delegate = delegate if delegate is not None else RegistryClient(config, transport=CertificateTransport(trust_context(config), transport))
        self.event_hooks = event_hooks
        self.token = None

    # 职责：在 wit 标记远端写入不确定之前完成账户认证。
    # 输入：配置中的 Registry 用户名和密码，与 Keycloak 登录及本地 admin 无关。
    # 结果与约束：仅接受明确返回的 access_token；不继承环境代理、不跟随重定向；默认不记录正文，CLI 可显式注入脱敏诊断 hook。
    async def prepare(self):
        context = ssl.create_default_context(cafile=str(self.config.endpoints.ca_cert_path))
        credentials = self.config.registry_credentials
        async with httpx.AsyncClient(verify=context, transport=CertificateTransport(context, self.transport), trust_env=False,
                                     follow_redirects=False, timeout=30, event_hooks=self.event_hooks) as client:
            response = await client.post(self.config.endpoints.registry_url.rstrip('/') + '/api/v1/auth/login',
                                         data={'username': credentials.username, 'password': credentials.password})
            response.raise_for_status()
            token = response.json().get('access_token')
            if not isinstance(token, str) or not token:
                raise ValueError('Registry 登录没有返回有效 access_token')
            self.token = token

    # 职责：用已验证本体证书提交协议允许的派生增量。
    # 输入：candidate 为冻结的完整实体预期 ACS，ontology 是 wit 验证过的本体 IdentityMaterial。
    # 结果与约束：只发送 ontologyAic、端点、用户 ID、元数据及 certificate；不创建人工审核草稿，HTTP 成功后仍核对返回 AIC 所属本体。
    async def register_entity(self, candidate, ontology):
        root = self.config.endpoints.registry_entity_mtls_url
        if not root or not self.token:
            raise ValueError('派生实体需要显式 mTLS URL 及已认证 Registry 用户')
        payload = {'ontologyAic': ontology.aic}
        payload.update({key: candidate[key] for key in ('endPoints', 'entityUserId', 'entityMeta', 'certificate') if key in candidate})
        with tempfile.TemporaryDirectory(prefix='.entity-mtls-', dir=self.config.paths.state_dir) as temporary:
            directory = Path(temporary)
            for name, value in (('client.pem', ontology.certificate), ('client.key', ontology.private_key)):
                path = directory / name
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'w') as stream:
                    stream.write(value)
            context = ssl.create_default_context(cafile=str(self.config.endpoints.ca_cert_path))
            context.load_cert_chain(str(directory / 'client.pem'), str(directory / 'client.key'))
            async with httpx.AsyncClient(verify=context, transport=CertificateTransport(context, self.transport), trust_env=False,
                                         follow_redirects=False, timeout=30, event_hooks=self.event_hooks) as client:
                response = await client.post(root.rstrip('/') + '/entity', json=payload,
                                             headers={'Authorization': 'Bearer ' + self.token})
        if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
            raise RegistryRequestRejected(response.status_code, 'Registry 拒绝实体注册，请核对权限、本体与端点')
        response.raise_for_status()
        document = response.json()
        result = document.get('result')
        if document.get('status') != 'ok' or not isinstance(result, dict):
            raise ValueError('Registry 派生响应无效，需对账而非重新注册')
        aic = result.get('entityAic')
        if not isinstance(aic, str) or not aic or aic == ontology.aic or result.get('ontologyAic') != ontology.aic:
            raise ValueError('Registry 派生响应的本体身份不一致')
        return RegistrationReview(registration_id='entity:' + aic, status='approved', aic=aic)

    # 职责：按 wit 公开客户端获取已提交注册的最新状态。
    # 输入：registration_id 和 wit 的候选/严格审核参数。
    # 结果与约束：原样委托，不伪造审核结果；实体注册通常直接进入 approved。
    async def review(self, registration_id, **kwargs):
        return await self.delegate.review(registration_id, **kwargs)

    # 职责：获取实体 AIC 对应的 EAB，供框架申请自己的证书。
    # 输入：aic 为 Registry 已返回的正式实体身份。
    # 结果与约束：EAB 生命周期交给 wit；不复用本体证书冒充个人实体。
    async def fetch_eab(self, aic):
        return await self.delegate.fetch_eab(aic)

    # 职责：读取 Registry 保存的正式完整 ACS。
    # 输入：aic 是需要核验的正式身份。
    # 结果与约束：完整 ACS 由 WIT 核验。派生实体读取时，WIT 3.2 另传已核验的本体用于补齐继承字段。
    async def final_acs(self, aic):
        return await self.delegate.final_acs(aic)

    async def final_entity_acs(self, aic, ontology_acs):
        return await self.delegate.final_entity_acs(aic, ontology_acs)
