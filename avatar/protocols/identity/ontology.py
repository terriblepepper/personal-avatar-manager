# 模块说明：个人分身能力本体；与管理系统服务分开注册、审核和保存证书，不直接提供小镇问答端点。
from pathlib import Path
from wit.acps.identity.acs import REGISTRATION_CANDIDATE_LAST_MODIFIED_TIME, validate_registration_candidate_acs, validate_registry_acs
from avatar.protocols.identity.acs import AcsInput, AcsProviderInput, AcsSkillInput, build_acs
from avatar.core.config import public_url
from avatar.protocols.identity.system import (SystemIdentityPaths, registration_urls,
                                             private_directory, create_identity_json)

AVATAR_ONTOLOGY_ID = 'personal-avatar-ontology'
AVATAR_SKILL_IDS = frozenset({'personal-profile.introduction.qa', 'personal-profile.materials.search'})


# 职责：定位独立个人分身本体的 ACS、配置、可信 CA 和注册证书目录。
# 输入：data_dir 为业务数据根目录。
# 结果与约束：只计算 avatar_ontology 路径，不复用 system_identity 的本体状态或私钥。
def avatar_ontology_paths(data_dir):
    root = Path(data_dir) / 'avatar_ontology'
    return SystemIdentityPaths(root, root / 'acs.json', root / 'registration.json', root / 'certificates', root / 'ca' / 'ca.pem')


# 职责：生成所有个人分身共同继承的通用能力模板。
# 输入：base_url 为部署 Web 根地址，不读取具体用户姓名、资料、模型 Key 或附件内容。
# 结果与约束：只描述已实现的个人资料问答与公开文件检索，不含管理技能；本体及未来个人实体当前不开放小镇问答端点。
def build_avatar_ontology_acs(base_url=None):
    base_url = (base_url or public_url()).rstrip('/')
    content = AcsInput(
        name='镜像 · 个人分身能力本体', version='1.0.0',
        description='以用户视角，根据个人资料进行自我介绍、资料问答和相关文件检索。',
        icon_url=base_url + '/static/system-avatar.svg', documentation_url=base_url + '/api/docs',
        provider=AcsProviderInput(organization='个人分身智能体管理系统', department='个人分身服务', name='系统维护者', url=base_url),
        default_input_modes=['text/plain', 'application/json'], default_output_modes=['text/plain', 'application/json'],
        skills=[
            AcsSkillInput(id='personal-profile.introduction.qa', name='个人介绍与资料问答',
                         description='根据个人档案进行自我介绍，回答个人背景、经历、能力及研究方向等问题。',
                         tags=['个人分身', '个人介绍', '资料问答'], examples=['请介绍一下你自己', '你的研究方向是什么']),
            AcsSkillInput(id='personal-profile.materials.search', name='公开资料文件检索',
                         description='检索资料文件的说明和相关文字片段，为个人资料问答提供参考。',
                         tags=['资料检索', '公开文件'], examples=['请结合你公开的项目资料介绍研究经历']),
        ])
    document = build_acs(content, AVATAR_ONTOLOGY_ID, base_url, modified=REGISTRATION_CANDIDATE_LAST_MODIFIED_TIME)
    # 本体没有独立 Web 页面；不借用管理平台地址作为其 webAppUrl。
    document.pop('webAppUrl', None)
    document['entityMeta'].update(serviceMode='personal-avatar-ontology', userRelation='personal-avatar-template', aipRole='partner')
    validate_registration_candidate_acs(document)
    return document


# 职责：核对本体是否确实代表个人分身能力，而非管理服务或另一个用户实体。
# 输入：document 是候选或框架已验证的正式本体 ACS。
# 结果与约束：检查稳定模板 ID、用途、必要个人技能及无管理技能/端点；不改变远端 ACS 或放宽 wit 的签名、证书及继承核验。
def validate_avatar_ontology(document):
    skills = {item.get('id', '') for item in document.get('skills', [])}
    if (document.get('entityUserId') != AVATAR_ONTOLOGY_ID
            or document.get('entityMeta', {}).get('serviceMode') != 'personal-avatar-ontology'
            or document.get('endPoints') or not AVATAR_SKILL_IDS.issubset(skills)
            or any(skill.startswith('avatar.manager.') for skill in skills)):
        raise ValueError('个人分身必须继承独立个人分身本体，不能继承管理服务本体')


# 职责：离线初始化个人分身本体的专用注册材料。
# 输入：数据目录、Web、Registry、CA 及 Registry mTLS 根地址，均由服务器明确配置。
# 结果与约束：仅创建缺失文件及 0700 目录、0600 JSON，不覆盖已有状态、不联系 Registry，不复制管理系统证书。
def initialize_avatar_ontology(data_dir, base_url=None, registry_url=None,
                               ca_url=None, mtls_url=None):
    data_dir = Path(data_dir)
    paths = avatar_ontology_paths(data_dir)
    for directory in (data_dir, paths.root, paths.certificates, paths.ca_bundle.parent):
        private_directory(directory)
    urls = {} if paths.registration.exists() else registration_urls(registry_url, ca_url, mtls_url)
    candidate = create_identity_json(paths.acs, build_avatar_ontology_acs(base_url))
    (validate_registry_acs if candidate.get('aic') else validate_registration_candidate_acs)(candidate)
    validate_avatar_ontology(candidate)
    create_identity_json(paths.registration, {
        **urls,
        'ca_cert_path': 'ca/ca.pem', 'acs_path': 'acs.json', 'state_dir': 'certificates',
        'mode': 'ontology', 'use_saved_urls': False})
    return paths
