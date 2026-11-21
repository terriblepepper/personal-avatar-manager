# 模块说明：独立个人分身本体自动注册；复用 ATR 生命周期，使用独占候选、审核、证书与发布位置。
import json
from avatar.protocols.identity.atr_config import read_atr_config
from avatar.protocols.identity.ontology import (AVATAR_ONTOLOGY_ID, avatar_ontology_paths,
                                            initialize_avatar_ontology, validate_avatar_ontology)
from avatar.protocols.registration.system import RegistrationConfigurationError, validate_system_registration, publish_identity_document
from avatar.runtime.system_registration import SystemRegistrationRuntime


# 职责：注册个人资料问答能力本体，供个人实体派生，不处理管理服务端点。
# 输入：settings 提供个人本体开关和服务器 Registry 凭据。
# 结果与约束：管理服务与个人本体各自等待人工审核并保存状态；就绪不启动 AIP，不改变任何已有用户 AIC。
class AvatarOntologyRegistrationRuntime(SystemRegistrationRuntime):
    # 职责：建立没有协议监听器的独立本体注册任务。
    # 输入：settings 为不可变启动配置。
    # 结果与约束：继承经过验收的状态推进、每小时检查及取消清理，仅覆盖本体用途相关的扩展点。
    def __init__(self, settings):
        super().__init__(settings, None)

    # 职责：读取个人分身本体专用的自动注册开关。
    # 输入：settings.avatar_atr_enabled。
    # 结果与约束：不受管理系统 ATR 或 AIP 开关隐式控制。
    @property
    def enabled(self):
        return self.settings.avatar_atr_enabled

    # 职责：加载并预检个人分身本体配置和能力模板。
    # 输入：avatar_ontology 专用目录及服务器 Registry 凭据。
    # 结果与约束：只初始化完整缺失的新目录，已有候选/证书不覆盖；不要求管理 RPC 的 SAN，不接受管理技能或 integrated 模式。
    def configuration(self):
        settings = self.settings
        paths = avatar_ontology_paths(settings.data_dir)
        if not paths.acs.exists() and not paths.registration.exists():
            initialize_avatar_ontology(settings.data_dir, settings.oidc_public_url)
        config = read_atr_config(paths.registration, settings.registry_username, settings.registry_password)
        validate_system_registration(settings, config, expected_entity_id=AVATAR_ONTOLOGY_ID, require_service_san=False)
        try:
            validate_avatar_ontology(json.loads(paths.acs.read_text()))
        except ValueError:
            raise RegistrationConfigurationError('avatar_ontology_mismatch') from None
        return config

    # 职责：将个人分身本体正式 ACS 写回其唯一 acs.json。
    # 输入：document 为框架 final_acs，aic 为本体真实身份。
    # 结果与约束：再次核对个人技能语义；不写 system_identity，不伪造缺失继承字段。
    def publish(self, document, aic):
        expected=json.loads(avatar_ontology_paths(self.settings.data_dir).registration.read_text()).get('expected_aic')
        if expected and aic!=expected:raise RegistrationConfigurationError('avatar_ontology_aic_mismatch')
        validate_avatar_ontology(document)
        publish_identity_document(avatar_ontology_paths(self.settings.data_dir).acs, document, aic, AVATAR_ONTOLOGY_ID)

    # 职责：把个人分身本体标记为可供后续派生的状态。
    # 输入：config 为本体配置，真实 AIC 已由父类状态机验证并发布。
    # 结果与约束：不派生管理服务、不启动端口；个人实体由审核队列自行推进。
    async def activate(self, config):
        self.stage = 'ready'

    # 职责：提供只反映个人分身本体进度的管理状态。
    # 输入：继承状态机的最近注册结果，不请求 Registry 或读取用户资料。
    # 结果与约束：本体 active 表示可以派生个人实体，不将管理 AIP 是否监听作为个人本体就绪条件。
    def status(self):
        result = super().status()
        if self.phase == 'active':
            result['message'] = '个人分身本体身份和证书已就绪，可为审核通过的新用户派生分身；本体不提供服务端点。'
        elif self.phase != 'active':
            paths=avatar_ontology_paths(self.settings.data_dir)
            try:
                document=json.loads(paths.acs.read_text());expected=json.loads(paths.registration.read_text()).get('expected_aic')
                if expected and document.get('active') and document.get('aic')==expected:
                    result['registry_approved_aic']=expected
                    result['message']='个人本体正式 ACS 已激活，但私有证书尚未核验就绪；请恢复原注册状态后再派生。'
            except (OSError,ValueError):pass
        return result
