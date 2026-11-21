# 模块说明：正式个人实体证书续期及注销，始终调用 wit/Registry 公开接口。
import shutil
from pathlib import Path
import ssl
import httpx
from avatar.protocols.transport.certificates import certificate_authority, registration_component
from wit import WitAgent
from wit.acps.atr import (TRUSTED_CERTIFICATES,
                          CertificateRenewRequest, CertificateRevokeRequest, RegistryClient)
from avatar.protocols.identity.atr_config import read_atr_config
from avatar.protocols.registration.entity import entity_identity_paths, certificate_projection
from avatar.protocols.transport.registry import EntityRegistry
from avatar.protocols.transport.ready import RegistryReadyTransport
from avatar.protocols.registration.base import RegistrationFailure, RegistrationPending
from avatar.protocols.identity.system import create_identity_json
from avatar.protocols.identity.document import identity_json, wit_candidate_config


RENEWAL_SCAN_SECONDS = 30 * 60


def remove_retired_identity(data_dir, user_id):
    """旧身份已远端注销；缺失目录表示上次清理已完成。"""
    try:
        root = entity_identity_paths(data_dir, user_id).root
        if root.is_symlink():
            raise ValueError('身份目录不能为符号链接')
        try:
            shutil.rmtree(root)
        except FileNotFoundError as exc:
            if Path(exc.filename) != root:
                raise
    except (OSError, ValueError) as exc:
        raise RegistrationPending('远端身份已注销，等待完成本地身份目录清理', phase='identity_cleanup') from exc


# 职责：为服务端自己拥有的实体执行 Registry 查询及删除。
# 输入：config 提供独立 Registry 配置及环境凭据，transport 仅用于隔离测试。
# 结果与约束：不使用管理员审核接口，不删除本体；写请求后读回确认，拒绝盲目重发不确定删除。
class EntityRegistryMaintenance:
    # 职责：保存已授权实体的网络参数。
    # 输入：config 是实体 AtrConfig，transport 可选。
    # 结果与约束：构造不联网、不输出凭据。
    def __init__(self, config, transport=None):
        self.config, self.transport = config, transport

    # 职责：删除唯一匹配 AIC 的个人实体并核实删除状态。
    # 输入：aic 必须来自本地已核验的个人实体记录。
    # 结果与约束：先精确查询并核对非本体，再删除和读回；部分提交保留状态供管理员恢复。
    async def unregister(self, aic, *, ontology=False):
        context = ssl.create_default_context(cafile=str(self.config.endpoints.ca_cert_path))
        async with httpx.AsyncClient(base_url=self.config.endpoints.registry_url, verify=context,
                                     transport=RegistryReadyTransport(self.config,self.transport), trust_env=False, follow_redirects=False, timeout=30) as client:
            credentials = self.config.registry_credentials
            response = await client.post('/api/v1/auth/login', data={'username': credentials.username, 'password': credentials.password})
            response.raise_for_status()
            tokens = response.json()
            headers = {'Authorization': 'Bearer ' + tokens['access_token']}
            target = self.config.paths.state_dir / 'unregister-target.json'
            saved = identity_json(target) if target.exists() else None
            if saved is None:
                response = await client.get('/api/v1/agent/client', params={'aic': aic, 'page_size': 100}, headers=headers)
                if response.status_code == 401 and tokens.get('refresh_token'):
                    refreshed = await client.post('/api/v1/auth/refresh-token', json={'refresh_token': tokens['refresh_token']})
                    refreshed.raise_for_status()
                    headers = {'Authorization': 'Bearer ' + refreshed.json()['access_token']}
                    response = await client.get('/api/v1/agent/client', params={'aic': aic, 'page_size': 100}, headers=headers)
                response.raise_for_status()
                matches = [item for item in response.json()['items'] if item.get('aic') == aic]
                if len(matches) != 1 or matches[0].get('is_ontology') is not ontology:
                    raise RegistrationFailure('未找到唯一的自有个人实体，拒绝注销本体或不明确的记录')
                saved = create_identity_json(target,{'aic':aic,'id':matches[0]['id'],'is_ontology':ontology})
            if saved.get('aic') != aic or saved.get('is_ontology') is not ontology:
                raise RegistrationFailure('待注销记录与当前身份不一致')
            from urllib.parse import quote
            path = '/api/v1/agent/client/' + quote(str(saved['id']),safe='')
            current = await client.get(path,headers=headers)
            current.raise_for_status()
            record = current.json()
            if record.get('aic') != aic or record.get('is_ontology',saved['is_ontology']) is not ontology or not isinstance(record.get('is_deleted'),bool):
                raise RegistrationFailure('注销前读回的身份不匹配')
            if not record.get('is_deleted'):
                # 某些部署在提交删除后 webhook 失败返回 400，因此必须读回而不能只看响应码。
                await client.delete(path, headers=headers)
            verified = await client.get(path, headers=headers)
            verified.raise_for_status()
            data = verified.json()
            if data.get('aic') != aic or data.get('is_deleted') is not True:
                raise RegistrationFailure('Registry 未确认注销，保留本地停用状态，请对账后重试')


# 职责：执行个人证书维护并返回可存数据库的公开投影。
# 输入：settings、稳定 user_id、expected_aic、action；只能由已授权管理队列调用。
# 结果与约束：不在数据库事务内联网；注销保留私有状态和业务数据，续期必须核验完整证书后才能发布。
async def maintain_entity(settings, user_id, expected_aic, action):
    paths = entity_identity_paths(settings.data_dir, user_id)
    config = read_atr_config(paths.registration, settings.registry_username, settings.registry_password)
    if config.mode not in ('entity','integrated'):
        raise RegistrationFailure('仅允许维护个人正式实体')
    return await maintain_registered(config, expected_aic, action)


async def maintain_registered(config, expected_aic, action, *, expected_certificate=None):
    with wit_candidate_config(config) as input_config:
        registry = EntityRegistry(input_config) if config.mode=='entity' else RegistryClient(input_config,transport=RegistryReadyTransport(input_config))
        component = registration_component(input_config, registry=registry)
        agent = WitAgent(metadata={'application': 'personal-avatar-manager', 'entry': 'entity-maintenance'})
        handle = agent.install(component, required=True)
        async with agent:
            await handle.wait_loaded()
            if component.state.aic != expected_aic:
                raise RegistrationFailure('本地账户与 ATR 的 AIC 不一致，拒绝维护')
            certificates = agent.context.get(TRUSTED_CERTIFICATES)
            if action == 'unregister':
                instances = tuple(certificates.certificates)
                if not instances and str(component.state.phase)=='active':
                    raise RegistrationFailure('未找到可核验的证书记录，未完成注销')
                await EntityRegistryMaintenance(config).unregister(expected_aic)
                ca = certificate_authority(config)
                for cert in instances:
                    if cert.status != 'revoked':
                        await certificates.revoke(CertificateRevokeRequest(aic=expected_aic, usage=cert.usage,
                            generation=cert.generation, fingerprint=cert.fingerprint, reason=5))
                    if (await ca.certificate_status(cert.serial)).lower() != 'revoked':
                        raise RegistrationFailure('CA 尚未确认旧证书已撤销，请重试核验',phase='certificate_revocation')
                if any(cert.status != 'revoked' for cert in certificates.certificates):
                    raise RegistrationFailure('证书撤销尚未全部确认，请继续注销任务')
                return None
            if action != 'renew':
                raise RegistrationFailure('未知个人证书维护操作')
            previous = identity_json(config.paths.state_dir / 'atr-state.json')['active']
            if expected_certificate and previous.get('client') != expected_certificate:
                raise RegistrationFailure('证书已变化，请重新读取当前代次')
            await certificates.renew(CertificateRenewRequest(aic=expected_aic, usages=('client', 'server')))
            ontology_aic = component.final_acs.get('entityMeta',{}).get('ontologyAic','')
        material, generation, _ = await registration_component(input_config).read_active_snapshot()
        if material.aic != expected_aic:
            raise RegistrationFailure('续期后的证书身份不一致，拒绝发布')
        projection=certificate_projection(material,generation,ontology_aic)
        active = identity_json(config.paths.state_dir / 'atr-state.json')['active']
        if (projection['id'] != active.get('client') or not active.get('server') or
                any(active.get(usage) == previous.get(usage) for usage in ('client','server'))):
            raise RegistrationFailure('未读回新一代客户端和服务端证书')
        if config.mode=='integrated':projection.update(mode='acps-direct');projection.pop('ontology_aic',None)
        return projection
