# 模块说明：个人分身从独立个人能力本体派生；冻结候选、可恢复签发、账户稳定关联。
import asyncio
import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from avatar.protocols.transport.certificates import registration_component
from wit import WitAgent
from wit.acps.atr import TRUSTED_REGISTRATION
from wit.acps.identity.acs import REGISTRATION_CANDIDATE_LAST_MODIFIED_TIME, validate_registration_candidate_acs, AcsContractError
from avatar.protocols.identity.atr_config import read_atr_config
from avatar.protocols.transport.registry import EntityRegistry
from avatar.protocols.registration.base import RegistrationFailure, RegistrationPending
from avatar.protocols.identity.system import (system_identity_paths,
                                             private_directory, create_identity_json)
from avatar.protocols.registration.system import plain_json, publish_identity_document
from avatar.protocols.identity.ontology import avatar_ontology_paths, validate_avatar_ontology
from avatar.protocols.transport.endpoints import entity_endpoint, entity_oidc_identity
from avatar.protocols.identity.document import wit_candidate_config, sync_component_acs


# 职责：描述一个派生实体独占的 ACS、注册配置及 wit 状态路径。
# 输入：root 必须由稳定本地 user_id 计算，不能使用姓名、邮箱或外部账号作为路径。
# 结果与约束：个人实体在 avatar_identities/{user_id}，各账户不共享私钥或 ATR 状态。
@dataclass(frozen=True)
class EntityIdentityPaths:
    root: Path
    acs: Path
    registration: Path
    certificates: Path


# 职责：为一个个人账户计算安全独立的实体身份路径。
# 输入：data_dir 为数据根，user_id 为稳定内部账户 ID。
# 结果与约束：拒绝路径穿越；只计算路径，不创建目录或接触账号资料。
def entity_identity_paths(data_dir, user_id):
    if not isinstance(user_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', user_id):
        raise ValueError('实体用户 ID 不允许用作路径')
    from avatar.core.user_storage import user_root
    root = user_root(data_dir,user_id) / 'identity'
    return EntityIdentityPaths(root, root / 'acs.json', root / 'registration.json', root / 'certificates')


# 职责：向管理员提供未完成派生的公开阶段与已分配 AIC。
# 输入：data_dir 和稳定 user_id 均来自服务端。
# 结果与约束：只投影公开元数据，不返回私钥、完整状态、EAB 或错误正文；缺失状态表示尚未尝试，不表示已上线。
def staged_entity_metadata(data_dir, user_id):
    paths = entity_identity_paths(data_dir, user_id)
    state = paths.certificates / 'atr-state.json'
    if not state.exists():
        return None
    try:
        if state.is_symlink() or paths.acs.is_symlink():
            raise ValueError('身份状态不可用')
        candidate = json.loads(paths.acs.read_text())
        if candidate.get('entityUserId') != user_id:
            raise ValueError('身份状态不属于该账户')
        data = json.loads(state.read_text())
        result = {key: data.get(key) for key in ('phase', 'aic', 'registration_id')}
        if any(value is not None and (not isinstance(value, str) or len(value) > 300) for value in result.values()):
            raise ValueError('身份元数据格式异常')
        return result
    except (ValueError, OSError):
        return {'phase': 'unavailable', 'aic': None, 'registration_id': None}


# 职责：从已验证正式本体 ACS 生成实体的完整预期候选。
# 输入：ontology 为框架验证过的本体正式 ACS，user_id 是账户稳定标识，profile 仅为已审核资料。
# 结果与约束：能力等字段全部继承；只改实体元数据、用户标识、端点及 SAN；外部用户名可为邮箱，其他私密资料和 Key 不提交。
def build_entity_candidate(ontology, user_id, profile, endpoint=None, oidc_identity=None):
    try:
        validate_avatar_ontology(ontology)
    except ValueError:
        raise RegistrationFailure('个人分身必须继承个人分身本体，不能使用管理系统 ACS') from None
    candidate = copy.deepcopy(plain_json(ontology))
    candidate.update(aic='', active=False, lastModifiedTime=REGISTRATION_CANDIDATE_LAST_MODIFIED_TIME,
                     entityUserId=user_id,
                     endPoints=[endpoint] if endpoint else [])
    candidate['entityMeta'] = {'serviceMode': 'personal-avatar',
                               'aipRole': 'partner', 'ontologyAic': ontology['aic']}
    candidate['entityMeta'].update(name=str(profile.get('name', '')), avatarName=str(profile.get('avatar_name', '')))
    if oidc_identity:
        candidate['entityMeta'].update(oidc_identity)
    if endpoint:
        from urllib.parse import urlsplit
        host = urlsplit(endpoint['url']).hostname
        names = candidate.get('certificate', {}).get('altNames', {}).get('dns', [])
        import ipaddress
        try:ip = str(ipaddress.ip_address(host))
        except ValueError:ip = None
        covered = ip in candidate.get('certificate', {}).get('altNames', {}).get('ip', []) if ip else any(host == name or (name.startswith('*.') and host.endswith(name[1:])
                                      and host.count('.') == name.count('.')) for name in names)
        if not covered:
            candidate['certificate'] = {**candidate.get('certificate', {}),
                                        'altNames': {'dns': [] if ip else [host], 'ip': [ip] if ip else []}}
    # 实体拥有独立的 DNS SAN；Registry 按派生公开契约接收 certificate，不复用本体证书。
    validate_registration_candidate_acs(candidate)
    return candidate


# 职责：把已验证实体的公开证书转换为数据库可保存的投影。
# 输入：material 是 read_active_snapshot 的当前材料，generation 为框架当前代次，ontology_aic 为父身份。
# 结果与约束：保存公开 PEM、指纹、到期日等，不保存私钥、ACME 账号或 EAB；私有状态继续由 wit 专用目录维护。
def certificate_projection(material, generation, ontology_aic):
    certificate = x509.load_pem_x509_certificate(material.certificate.encode())
    server = x509.load_pem_x509_certificate(material.server_certificate.encode())
    return {'id': certificate.fingerprint(hashes.SHA256()).hex(), 'generation': generation,
            'expires_at': min(certificate.not_valid_after_utc, server.not_valid_after_utc).isoformat(), 'issuer': certificate.issuer.rfc4514_string(),
            'mode': 'acps-entity', 'status': '正式证书已核验', 'ontology_aic': ontology_aic,
            'public_pem': material.certificate, 'server_public_pem': material.server_certificate}


# 职责：用 wit 公开 ATR 状态机推进一个派生实体，恢复时复用原候选和目录。
# 输入：settings 提供显式服务配置及 Registry 凭据；不接收浏览器提供的本体 AIC 或证书路径。
# 结果与约束：本体未就绪只等待；实体注册无需逐个人工审核，仍须通过 Registry/CA 实际核验；不自动批准本体。
class EntityRegistrationRunner:
    # 职责：保存只读服务配置。
    # 输入：settings 为应用启动配置。
    # 结果与约束：构造无网络或文件副作用。
    def __init__(self, settings, database):
        self.settings, self.database = settings, database

    # 职责：核验本体并冻结一个实体候选及独占注册配置。
    # 输入：内部 user_id、已审核 profile；父目录只允许个人能力本体。
    # 结果与约束：候选只首次创建，重试不因个人资料变化而改变指纹；父身份异常不新建子身份。
    async def configuration(self, user_id, profile):
        settings = self.settings
        parent_paths = avatar_ontology_paths(settings.data_dir)
        paths = entity_identity_paths(settings.data_dir, user_id)
        if paths.registration.exists():
            previous_config = read_atr_config(paths.registration)
            previous_parent = previous_config.paths.parent_state_dir
            if previous_parent and previous_parent.resolve() == system_identity_paths(settings.data_dir).certificates.resolve():
                raise RegistrationFailure('已有个人实体状态指向管理本体；请保留原状态并按平台规范迁移，不会自动更换父身份或 AIC')
        if not parent_paths.registration.exists():
            raise RegistrationPending('等待个人分身本体配置、审核及证书就绪', settings.system_atr_poll_interval)
        parent = read_atr_config(parent_paths.registration, settings.registry_username, settings.registry_password)
        if parent.mode != 'ontology':
            raise RegistrationFailure('个人能力模板必须以 ontology 模式注册，不能使用普通管理服务作为父身份')
        if not settings.registry_username or not settings.registry_password:
            raise RegistrationPending('等待服务器配置 Registry 注册凭据', settings.system_atr_poll_interval)
        from urllib.parse import urlsplit
        if (not parent.endpoints.registry_entity_mtls_url
                or urlsplit(parent.endpoints.registry_entity_mtls_url).scheme != 'https'):
            raise RegistrationFailure('派生注册需要配置 Registry 9002 的 HTTPS mTLS 根地址及可信 CA')
        try:
            # ontology 不提供 ACTIVE_IDENTITY，也不能调用 read_active_snapshot 当作运行实体。
            # 这里只读恢复状态和正式能力；子实体 up() 会自行读取并再次核验父证书及 CA 状态。
            with wit_candidate_config(parent) as parent_input:
                component = registration_component(parent_input)
                agent = WitAgent(metadata={'application': 'personal-avatar-manager', 'entry': 'ontology-read'})
                agent.install(component, required=True)
                async with agent:
                    status = await agent.context.get(TRUSTED_REGISTRATION).refresh_certificate_status()
                    if str(status.phase) != 'active' or component.final_acs is None:
                        raise RegistrationPending('等待本体审核及证书就绪', settings.system_atr_poll_interval)
                    ontology = plain_json(component.final_acs)
        except RegistrationPending:
            raise
        except FileNotFoundError:
            raise RegistrationFailure('个人分身本体注册材料缺失，请恢复原 ACS 和 ATR 状态；不要重新申请本体') from None
        except (OSError, RuntimeError, ValueError, LookupError):
            raise RegistrationPending('等待本体审核、证书和 CA 状态核验就绪；不会重新申请本体', settings.system_atr_poll_interval) from None
        expected=json.loads(parent_paths.registration.read_text()).get('expected_aic')
        if expected and ontology.get('aic')!=expected:raise RegistrationFailure('个人本体 AIC 与明确指定的本体不一致，拒绝派生')
        if ontology.get('endPoints'):
            raise RegistrationFailure('可派生本体不得声明服务端点，请保留旧身份并按平台规范配置本体')
        try:
            validate_avatar_ontology(ontology)
        except ValueError:
            raise RegistrationFailure('个人分身本体用途或技能不符，拒绝使用管理系统模板注册个人分身') from None
        for directory in (paths.root.parent, paths.root, paths.certificates):
            private_directory(directory)
        from avatar.core.execution import run_blocking
        def allocate():
            with self.database.connect(write=True) as db:
                endpoint = entity_endpoint(settings, user_id, db=db)
                return endpoint, entity_oidc_identity(settings, user_id, db=db) if endpoint else None
        endpoint, oidc_identity = await run_blocking(allocate)
        create_identity_json(paths.acs, build_entity_candidate(ontology, user_id, profile, endpoint, oidc_identity))
        create_identity_json(paths.registration, {
            'registry_url': parent.endpoints.registry_url,
            'certificate_authority_url': parent.endpoints.certificate_authority_url,
            'registry_entity_mtls_url': parent.endpoints.registry_entity_mtls_url,
            'ca_cert_path': str(parent.endpoints.ca_cert_path.resolve()) if parent.endpoints.ca_cert_path else '',
            'acs_path': 'acs.json', 'state_dir': 'certificates', 'parent_state_dir': str(parent.paths.state_dir.resolve()),
            'mode': 'entity', 'use_saved_urls': False})
        config = read_atr_config(paths.registration, settings.registry_username, settings.registry_password)
        if (config.mode != 'entity' or config.paths.parent_state_dir is None
                or config.paths.parent_state_dir.resolve() != parent.paths.state_dir.resolve()
                or config.endpoints != parent.endpoints):
            raise RegistrationFailure('已有实体配置的父身份不一致，禁止自动更换本体')
        stored = json.loads(paths.acs.read_text())
        if (stored.get('entityUserId') != user_id or stored.get('entityMeta', {}).get('ontologyAic') != ontology['aic']
                or stored.get('endPoints', []) != ([endpoint] if endpoint else [])
                or (oidc_identity and any(stored.get('entityMeta', {}).get(k) != v for k, v in oidc_identity.items()))):
            raise RegistrationFailure('已有实体候选与账户、本体或端点不一致，请按平台规范处理，禁止覆盖候选')
        return paths, config, ontology

    # 职责：推进实体注册与独立证书签发，只在完整 active 时返回正式身份。
    # 输入：稳定 user_id 与审核快照；请求有总超时，所有写入状态由 wit 持久化。
    # 结果与约束：pending 延后重试；blocked/rejected 要求人工处理，远端结果不确定不重建；释放 ATR 写锁后才读取当前证书。
    async def advance(self, user_id, profile):
        async with asyncio.timeout(self.settings.system_atr_call_timeout):
            paths, config, ontology = await self.configuration(user_id, profile)
            with wit_candidate_config(config) as input_config:
                component = registration_component(input_config, registry=EntityRegistry(config))
                agent = WitAgent(metadata={'application': 'personal-avatar-manager', 'entry': 'entity-registration'})
                agent.install(component, required=True)
                try:
                    async with agent:
                        try:
                            result = await agent.context.get(TRUSTED_REGISTRATION).up()
                        except AcsContractError as exc:
                            if 'entity expectation differs from authoritative ontology ACS' in str(exc) and '$.certificate' in str(exc):
                                raise RegistrationFailure('实体证书声明与已核验本体的继承规则不一致（$.certificate），请核对实体端点和 DNS SAN 后再提交') from None
                            raise
                        phase = str(result.phase)
                        if phase in ('blocked', 'rejected'):
                            raise RegistrationFailure('派生注册受阻或被拒绝，请保留原状态并用 wit 对账/检查 Registry 完整 ACS')
                        if phase != 'active':
                            raise RegistrationPending('实体身份或证书尚未就绪，保留原状态继续推进', self.settings.system_atr_poll_interval)
                        if not result.aic or component.final_acs is None:
                            raise RegistrationFailure('框架未返回正式实体 ACS，不能标记注册成功')
                        document = plain_json(component.final_acs)
                finally:
                    sync_component_acs(config, component)
            publish_identity_document(paths.acs, document, result.aic, user_id)
            with wit_candidate_config(config) as input_config:
                material, generation, _ = await registration_component(input_config).read_active_snapshot()
            if material.aic != document['aic']:
                raise RegistrationFailure('实体证书与已确认 ACS 的 AIC 不一致，禁止发布身份')
            return {'aic': material.aic, 'certificate': certificate_projection(material, generation, ontology['aic']),
                    'acs_document': document}
