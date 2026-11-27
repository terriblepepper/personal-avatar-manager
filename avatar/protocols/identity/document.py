# 模块说明：唯一 acs.json 的候选/正式文档切换，以及 ATR 的原候选输入兼容。
import json
import os
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from avatar.core.execution import run_blocking
from avatar.protocols.errors import RegistrationConfigurationError
from dataclasses import replace
from pathlib import Path
from wit.acps.identity.acs import validate_registration_candidate_acs, validate_registry_acs


# 职责：读取身份 JSON 并拒绝符号链接和非对象内容。
# 输入：path 为服务器配置的身份文件路径，不接受浏览器输入。
# 结果与约束：只读文件；错误不包含文件正文，避免泄露 ATR 私有状态。
def identity_json(path):
    if path.is_symlink():
        raise ValueError('身份文件不能使用符号链接')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError('身份 JSON 必须是对象')
    return value


# 职责：取得首次注册的原始候选，兼容 acs.json 已更新为正式文档的情况。
# 输入：config 是该身份的明确 ATR 配置；正式文档必须与同目录的 ATR 状态一致。
# 结果与约束：不修改候选、指纹或状态；仅已固定 AIC 的待审本体允许先公布正式 ACS 再恢复原申请，证书仍由 WIT 核验。
def registration_candidate(config):
    if config.paths.state_dir.is_symlink():
        raise ValueError('ATR 状态目录不能使用符号链接')
    document = identity_json(config.paths.acs_path)
    if not document.get('aic'):
        validate_registration_candidate_acs(document)
        return document
    validate_registry_acs(document)
    state = identity_json(config.paths.state_dir / 'atr-state.json')
    recovery = False
    if (config.mode == state.get('mode') == 'ontology' and state.get('phase') == 'pending_review'
            and state.get('registration_id') and not state.get('aic') and not state.get('final_acs')):
        declaration = config.paths.acs_path.parent / 'registration.json'
        recovery = declaration.is_file() and identity_json(declaration).get('expected_aic') == document['aic']
    if not recovery and (state.get('mode') != config.mode or state.get('aic') != document['aic']
            or state.get('final_acs') != document):
        raise ValueError('正式 ACS 与原 ATR 状态不一致，禁止新建身份或改写指纹')
    candidate = state.get('candidate')
    if not isinstance(candidate, dict) or candidate.get('entityUserId') != document.get('entityUserId'):
        raise ValueError('原始注册候选不可用或身份不一致')
    validate_registration_candidate_acs(candidate)
    return candidate


# 职责：为要求原始候选文件的 ATR 组件提供有界临时输入。
# 输入：config 的 acs_path 始终指向对外唯一的 acs.json。
# 结果与约束：未注册时直接使用原文件；正式身份从 ATR 原候选生成 0600 临时输入，退出或异常时清理；不增加持久 ACS 副本，不放宽 wit 的指纹校验。
@contextmanager
def wit_candidate_config(config):
    candidate = registration_candidate(config)
    if not identity_json(config.paths.acs_path).get('aic'):
        yield config
        return
    with tempfile.TemporaryDirectory(prefix='.wit-input-', dir=config.paths.state_dir) as directory:
        path = Path(directory) / 'candidate.json'
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(candidate, stream, ensure_ascii=False)
        yield replace(config, paths=replace(config.paths, acs_path=path))


class LocalEntityDirectory:
    """只装载已提交的 active 材料；日常通信不查询 Registry 或 CA。"""
    local_only=True
    def __init__(self,registrations):self.registrations=dict(registrations)

    async def select(self,aic):
        return await run_blocking(self.load, self.registrations[aic], aic)

    @staticmethod
    def load(config,aic):
        import hashlib
        from urllib.parse import urlsplit
        from wit.acps.atr.storage import lock_file
        from wit.acps.identity import LocalEntity,IdentityMaterial
        from wit.acps.identity.certificates import validate_certificate_material
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        root=config.paths.state_dir
        if root.is_symlink():raise ValueError('身份状态目录不能为符号链接')
        descriptor=os.open(root/'.atr.lock',os.O_RDWR|os.O_NOFOLLOW)
        try:
            lock_file(descriptor,blocking=False)
            state=identity_json(root/'atr-state.json')
            document=identity_json(config.paths.acs_path)
            candidate=registration_candidate(config)
            if (state.get('phase')!='active' or state.get('aic')!=aic or document.get('aic')!=aic
                    or document.get('active') is not True or state.get('final_acs')!=document):
                raise ValueError('本地身份未激活或 ACS 不一致')
            fingerprint=hashlib.sha256(json.dumps(candidate,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            if fingerprint!=state.get('fingerprint'):raise ValueError('本地 ACS 指纹不一致')
            def record(usage):
                reference=state.get('active',{}).get(usage)
                rows=[row for row in state.get('certificates',[]) if row.get('fingerprint')==reference and row.get('usage')==usage]
                if len(rows)!=1 or rows[0].get('status')!='issued' or rows[0].get('aic')!=aic:
                    raise ValueError('本地有效证书记录不唯一或已撤销')
                row=rows[0]
                certificate=x509.load_pem_x509_certificate(row['certificate'].encode())
                if certificate.fingerprint(hashes.SHA256()).hex()!=reference:raise ValueError('本地证书指纹不一致')
                return row
            client,server=record('client'),record('server')
            endpoints=document.get('endPoints',[])
            if not endpoints:raise ValueError('正式 ACS 缺少端点')
            endpoint=endpoints[0]['url']
            material=IdentityMaterial(aic=aic,certificate=client['certificate'],private_key=client['private_key'],
                trust_bundle=client['trust'],acs_fingerprint=fingerprint,
                server_certificate=server['certificate'],server_private_key=server['private_key'])
            validate_certificate_material(aic=aic,client_certificate_pem=material.certificate.encode(),
                client_private_key_pem=material.private_key.encode(),server_certificate_pem=material.server_certificate.encode(),
                server_private_key_pem=material.server_private_key.encode(),trust_bundle_pem=material.trust_bundle.encode(),public_endpoint=endpoint)
            return LocalEntity(aic=aic,material=material,generation=state['identity_generation'],hostname=urlsplit(endpoint).hostname)
        finally:os.close(descriptor)


# 职责：将一个身份遗留的正式 ACS 缓存迁移回唯一 acs.json。
# 输入：config 指向该身份的 ACS 及 ATR 状态；兼容旧文件名的连字符和下划线写法。
# 结果与约束：丢失的正式 ACS 仅从 active ATR 原状态恢复；旧缓存核对一致才移除，不改状态、证书或 AIC。
def migrate_identity_document(config):
    root = config.paths.acs_path.parent
    candidates = [root / 'registered-acs.json', root / 'registered_acs.json']
    if config.paths.state_dir / 'acs.json' != config.paths.acs_path:
        candidates.append(config.paths.state_dir / 'acs.json')
    legacy = [p for p in candidates if p.exists() or p.is_symlink()]
    if not config.paths.acs_path.exists() and config.paths.state_dir.exists():
        if config.paths.acs_path.is_symlink() or config.paths.state_dir.is_symlink():
            raise ValueError('身份文件或 ATR 状态目录不能使用符号链接')
        state = identity_json(config.paths.state_dir / 'atr-state.json')
        document, candidate = state.get('final_acs'), state.get('candidate')
        if not isinstance(document, dict) or not isinstance(candidate, dict):
            raise ValueError('ACS 文件缺失且原 ATR 正式身份不可恢复')
        validate_registry_acs(document)
        validate_registration_candidate_acs(candidate)
        declaration = identity_json(root / 'registration.json')
        expected = declaration.get('expected_aic')
        if (state.get('phase') != 'active' or state.get('mode') != config.mode
                or not document.get('active') or state.get('aic') != document.get('aic')
                or candidate.get('entityUserId') != document.get('entityUserId')
                or (expected and expected != document['aic'])
                or any(identity_json(p) != document for p in legacy)):
            raise ValueError('缺失 ACS 与原 ATR 身份不一致，拒绝恢复或新建身份')
        publish_identity_document(config.paths.acs_path, document, state['aic'], candidate['entityUserId'])
    if not legacy:
        return
    if config.paths.acs_path in legacy:
        raise ValueError('注册配置须指向 acs.json，不能将旧缓存作为输入文件')
    state = identity_json(config.paths.state_dir / 'atr-state.json')
    document = identity_json(legacy[0])
    if (state.get('mode') != config.mode or state.get('aic') != document.get('aic')
            or state.get('final_acs') != document or any(identity_json(p) != document for p in legacy)):
        raise ValueError('旧正式 ACS 与 ATR 身份不一致，保留文件待核对')
    if registration_candidate(config) != state.get('candidate'):
        raise ValueError('原 ACS 候选与 ATR 状态不一致，拒绝迁移')
    publish_identity_document(config.paths.acs_path, document, state['aic'], state['candidate']['entityUserId'])
    for path in legacy:
        path.unlink()


# 职责：把框架已经取得并核验的正式 ACS 写回唯一文件。
# 输入：config 是原始身份配置，component 是已退出容器的真实 ATR 组件；publisher 可附加本体用途校验。
# 结果与约束：仅有正式 AIC 和完整 final_acs 才更新；即使后续签证失败也保留已确认 ACS，未获取或无效文档不覆盖候选。
def sync_component_acs(config, component, publisher=None):
    document = component.final_acs
    status = getattr(component, 'state', None)
    if document is not None and status is not None and status.aic:
        if publisher is not None:
            publisher(document, status.aic)
        else:
            candidate = status.acs_candidate
            publish_identity_document(config.paths.acs_path, document, status.aic, candidate['entityUserId'])


# 职责：把框架只读 JSON 映射/元组转换为普通 JSON 容器。
# 输入：value 是 Registry 已确认的 ACS 数据，不包含凭据或证书材料。
# 结果与约束：保留字段和值，不经过会补充默认值的模型重建，不改变已确认 ACS 的语义内容。
def plain_json(value):
    if isinstance(value, Mapping):
        return {key: plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_json(item) for item in value]
    return value


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
