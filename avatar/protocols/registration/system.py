# 模块说明：系统 ATR 注册前置校验与正式 ACS 发布；只使用明确配置及 SDK 公共契约，不补造 AIC、证书或审批权限。
import ipaddress
import json
import os
import ssl
import tempfile
from collections.abc import Mapping
from urllib.parse import urlsplit
import httpx
from wit.acps.identity.acs import validate_registry_acs
from avatar.protocols.identity.system import SYSTEM_ENTITY_ID, system_aip_endpoint, system_identity_paths
from avatar.protocols.identity.document import registration_candidate


# 职责：用稳定错误码表达注册前置条件，不把密码或远端响应写进异常。
# 输入：code 为应用定义的配置/协议错误标识。
# 结果与约束：供后台显示固定中文说明，不能把异常正文作为用户提示或自动重新注册依据。
class RegistrationConfigurationError(ValueError):
    # 职责：保存可公开给运维的稳定错误码。
    # 输入：code 为不含凭据的短字符串。
    # 结果与约束：异常消息仅含错误码；不携带原始 ACS、HTTP 正文或 Token。
    def __init__(self, code):
        self.code = code
        super().__init__(code)


# 职责：检查自动注册的本地输入，避免缺少信任材料或候选不合法时提交远端申请。
# 输入：settings 包含只读启动配置，config 为已构造的 AtrConfig。
# 结果与约束：不修改候选或状态，不发起网络；拒绝不属于系统的 ACS、错误 mTLS 端点、缺失 SAN 和无效 CA 文件。
def validate_system_registration(settings, config, *, expected_entity_id=SYSTEM_ENTITY_ID, require_service_san=True):
    if not settings.registry_username or not settings.registry_password:
        raise RegistrationConfigurationError('registry_credentials_missing')
    if not 1 <= settings.system_atr_poll_interval <= 3600 or not 1 <= settings.system_atr_call_timeout <= 600:
        raise RegistrationConfigurationError('invalid_poll_configuration')
    if urlsplit(config.endpoints.certificate_authority_url).scheme != 'https':
        raise RegistrationConfigurationError('ca_https_required')
    path = config.endpoints.ca_cert_path
    if path is None or not path.is_file():
        raise RegistrationConfigurationError('trusted_ca_missing')
    try:
        ssl.create_default_context(cafile=str(path))
    except (OSError, ssl.SSLError):
        raise RegistrationConfigurationError('trusted_ca_invalid') from None
    try:
        candidate = registration_candidate(config)
        endpoint = system_aip_endpoint(settings.system_aip_public_url) if require_service_san else None
    except (ValueError, OSError):
        raise RegistrationConfigurationError('invalid_candidate') from None
    expected_mode = 'integrated' if require_service_san else 'ontology'
    if config.mode != expected_mode:
        raise RegistrationConfigurationError('system_mode_required' if require_service_san else 'ontology_mode_required')
    if not require_service_san and candidate.get('endPoints'):
        raise RegistrationConfigurationError('ontology_endpoint_forbidden')
    if (candidate.get('entityUserId') != expected_entity_id
            or (config.mode == 'integrated' and endpoint not in candidate.get('endPoints', []))):
        raise RegistrationConfigurationError('candidate_endpoint_mismatch')
    if not require_service_san:
        return
    hostname = urlsplit(settings.system_aip_public_url).hostname
    sans = candidate.get('certificate', {}).get('altNames', {})
    try:
        covered = ipaddress.ip_address(hostname) in {ipaddress.ip_address(value) for value in sans.get('ip', [])}
    except ValueError:
        hostname = hostname.lower()
        covered = any(hostname == value.lower() or (value.startswith('*.') and hostname.endswith(value[1:].lower())
                      and hostname.count('.') == value.count('.')) for value in sans.get('dns', []))
    if not covered:
        raise RegistrationConfigurationError('certificate_san_mismatch')


# 职责：在提交注册前核对 ACME directory 的地址归属。
# 输入：config 包含 CA 根地址和运维确认的信任文件，transport 仅供隔离测试注入。
# 结果与约束：只做 HTTPS GET；必须与广告的 ACME origin 和部署前缀一致，不跟随重定向、不继承代理，不下载并自动信任未知 CA。
async def validate_ca_directory(config, transport=None):
    from avatar.protocols.transport.certificates import CertificateTransport
    root = config.endpoints.certificate_authority_url.rstrip('/')
    expected = urlsplit(root)
    context = ssl.create_default_context(cafile=str(config.endpoints.ca_cert_path))
    async with httpx.AsyncClient(verify=context, transport=CertificateTransport(context,transport), trust_env=False, timeout=10, follow_redirects=False) as client:
        response = await client.get(root + '/acps-atr-v2/acme/directory')
        response.raise_for_status()
        try:
            document = response.json()
        except ValueError:
            raise RegistrationConfigurationError('ca_directory_invalid') from None
    if not isinstance(document, dict):
        raise RegistrationConfigurationError('ca_directory_invalid')
    for key in ('newNonce', 'newAccount', 'newOrder', 'revokeCert', 'keyChange'):
        value = document.get(key)
        if not isinstance(value, str):
            raise RegistrationConfigurationError('ca_directory_invalid')
        target = urlsplit(value)
        if (target.scheme != 'https' or target.hostname != expected.hostname or (target.port or 443) != (expected.port or 443)
                or target.username or target.password or target.fragment or target.query
                or not target.path.startswith(expected.path.rstrip('/') + '/')):
            raise RegistrationConfigurationError('ca_origin_mismatch')


# 职责：把框架只读 JSON 映射/元组转换为普通 JSON 容器。
# 输入：value 是 Registry 已确认的 ACS 数据，不包含凭据或证书材料。
# 结果与约束：保留字段和值，不经过会补充默认值的模型重建，不改变已确认 ACS 的语义内容。
def plain_json(value):
    if isinstance(value, Mapping):
        return {key: plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_json(item) for item in value]
    return value


# 职责：将 Registry 已确认的系统正式 ACS 写回唯一 acs.json。
# 输入：data_dir 是系统数据根，document 是 TrustedRegistrationComponent.final_acs，aic 来自 RegistrationStatus。
# 结果与约束：校验正式 ACS 和 AIC 一致性，以 0600 原子替换；原候选由 ATR 状态保存，不改证书或业务库。
def publish_system_acs(data_dir, document, aic):
    return publish_identity_document(system_identity_paths(data_dir).acs, document, aic, SYSTEM_ENTITY_ID)


# 职责：原子写回一个系统、本体或实体已经核验的正式 ACS。
# 输入：path 是身份专用目录文件，document 为框架 final_acs，aic 与 entity_user_id 为预期稳定身份。
# 结果与约束：不生成缺失字段；只允许同一用户候选升级或同一 AIC 更新，权限为 0600，相同内容不重复写盘。
def publish_identity_document(path, document, aic, entity_user_id):
    document = plain_json(document)
    validate_registry_acs(document)
    if document.get('aic') != aic or document.get('entityUserId') != entity_user_id:
        raise RegistrationConfigurationError('registered_identity_mismatch')
    if path.is_symlink():
        raise RegistrationConfigurationError('unsafe_acs_path')
    if path.exists():
        previous = json.loads(path.read_text())
        if previous.get('entityUserId') != entity_user_id or previous.get('aic') not in ('', aic):
            raise RegistrationConfigurationError('registered_identity_mismatch')
        if previous == document:
            return
    descriptor, name = tempfile.mkstemp(prefix='.acs-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)
