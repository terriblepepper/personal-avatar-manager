# 模块说明：ACS 固定表单契约与生成器，分离用户内容、平台维护字段和正式身份；不触发 Registry 或 CA 请求。
import ipaddress
import uuid
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from acps_sdk.acs.models import AgentCapabilitySpec
from wit import __version__ as wit_version
from avatar.core.config import public_url
from avatar.core.db import now
from avatar.profiles.profile_fields import public_profile
from avatar.profiles.profile_dates import ModifiedAt

VERSION_PATTERN = r'^[A-Za-z0-9][A-Za-z0-9._+-]*$'


# 职责：定义提供者逐条填写的域名备案信息。
# 输入：域名、注册或备案号，以及 ICP/WHOIS 类型；不接受网址代替主域名。
# 结果与约束：不伪造备案号，保存草稿允许空条目，提交时要求已添加条目完整。
class AcsDomainRegistration(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    domain: str = Field(default='', max_length=253)
    registration_number: str = Field(default='', max_length=160)
    registration_type: str = Field(default='ICP', pattern=r'^(ICP|WHOIS)$')

    # 职责：规范化域名并阻止误填 URL、IP、通配符或路径。
    # 输入：value 为用户填写的主域名，允许国际域名转为 IDNA。
    # 结果与约束：返回小写 ASCII 域名，空值仅供草稿；非法结构抛出校验错误。
    @field_validator('domain')
    @classmethod
    def valid_domain(cls, value):
        import re
        if value:
            try:
                value = value.rstrip('.').encode('idna').decode().lower()
            except UnicodeError:
                raise ValueError('域名格式不正确')
            if not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}', value):
                raise ValueError('请填写主域名，不带协议、路径、通配符或 IP')
        return value


# 职责：定义提供者的固定可编辑字段，避免原始 JSON 和任意字段注入。
# 输入：国家代码、组织、部门、提供者名称，均由用户显式填写。
# 结果与约束：不自动引用登录邮箱或私密资料；域名备案条目固定结构，不能注入任意字段，不自动使用注册账户的私密联系方式。
class AcsProviderInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    country_code: str = Field(default='CN', pattern=r'^[A-Z]{2}$')
    organization: str = Field(default='', max_length=160)
    department: str = Field(default='', max_length=160)
    name: str = Field(default='', max_length=80)
    url: str = Field(default='', max_length=500)
    license: str = Field(default='', max_length=160)
    email: str = Field(default='', max_length=160)
    domain_registrations: list[AcsDomainRegistration] = Field(default_factory=list, max_length=20)

    # 职责：校验用户主动声明的提供者官网地址。
    # 输入：value 为可选 HTTP/HTTPS 地址。
    # 结果与约束：使用统一 URL 规则，只保存链接、不发送外部请求。
    @field_validator('url')
    @classmethod
    def valid_url(cls, value):
        return checked_url(value)

    # 职责：规范化提供者联系人邮箱。
    # 输入：value 为用户明确填写的可公开邮箱，不自动读取登录联系方式。
    # 结果与约束：空值允许，非空要求邮箱格式，返回小写结果；不执行持有验证。
    @field_validator('email')
    @classmethod
    def valid_email(cls, value):
        import re
        if value and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
            raise ValueError('提供者邮箱格式不正确')
        return value.lower()

    # 职责：拒绝提供者重复填写同一域名备案条目。
    # 输入：self 为已完成字段校验的提供者对象。
    # 结果与约束：非空域名要求唯一，空条目留待提交完整性检查；通过后返回自身。
    @model_validator(mode='after')
    def unique_domains(self):
        domains = [item.domain for item in self.domain_registrations if item.domain]
        if len(set(domains)) != len(domains):
            raise ValueError('域名备案条目不能重复')
        return self


# 职责：定义每条技能表单的固定结构与类型。
# 输入：名称、描述、版本、标签、示例与输入输出 MIME；id 首次生成后稳定保留。
# 结果与约束：允许保存不完整草稿，提交时再校验必填；条目 ID 不承载权限，且仅允许安全字符。
class AcsSkillInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    id: str = Field(default_factory=lambda: 'skill.' + uuid.uuid4().hex, max_length=100, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')
    name: str = Field(default='', max_length=100)
    # 最长 80 字姓名与“能够”另计，旧用户的 2000 字正文可完整保留。
    description: str = Field(default='', max_length=2082)
    version: str = Field(default='1.0.0', pattern=VERSION_PATTERN, max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=20)
    examples: list[str] = Field(default_factory=list, max_length=20)
    input_modes: list[str] = Field(default_factory=lambda: ['text/plain'], min_length=1, max_length=10)
    output_modes: list[str] = Field(default_factory=lambda: ['text/plain'], min_length=1, max_length=10)

    # 职责：清理标签和示例，校验每项长度并去掉空行。
    # 输入：value 为控件转换后的字符串列表，要求每项为文本。
    # 结果与约束：返回顺序不变的非空列表；拒绝过长或非文本条目，不接受 JSON 对象作为标签。
    @field_validator('tags', 'examples')
    @classmethod
    def clean_lines(cls, value):
        if any(len(item) > 500 for item in value):
            raise ValueError('标签和示例每项最多 500 个字符')
        return [item.strip() for item in value if item.strip()]

    # 职责：校验技能支持的 MIME 列表并去重。
    # 输入：value 是选择控件或逐行输入转换的 MIME 文本数组。
    # 结果与约束：至少保留一种合法 type/subtype，声明只描述候选能力，不自动新增模型工具或协议实现。
    @field_validator('input_modes', 'output_modes')
    @classmethod
    def valid_modes(cls, value):
        return mime_modes(value)


# 职责：定义可由用户填写的 ACS 基础信息和技能列表。
# 输入：名称、版本、描述、图标/文档链接、地点、提供者及技能，不接受 AIC、协议版本或端点覆盖。
# 结果与约束：固定结构并禁止额外字段；所有发布内容须用户确认，个人资料的私密字段不会自动复制。
class AcsInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(default='', max_length=100)
    description: str = Field(default='', max_length=2000)
    version: str = Field(default='1.0.0', pattern=VERSION_PATTERN, max_length=80)
    template_owner: str = Field(default='', max_length=80)
    icon_url: str = Field(default='', max_length=500)
    documentation_url: str = Field(default='', max_length=500)
    location: str = Field(default='', max_length=160)
    provider: AcsProviderInput = Field(default_factory=AcsProviderInput)
    default_input_modes: list[str] = Field(default_factory=lambda: ['text/plain'], min_length=1, max_length=10)
    default_output_modes: list[str] = Field(default_factory=lambda: ['text/plain'], min_length=1, max_length=10)
    skills: list[AcsSkillInput] = Field(default_factory=list, max_length=20)

    # 职责：校验默认输入输出格式与技能 MIME 使用同一规则。
    # 输入：value 为 MIME 字符串数组。
    # 结果与约束：返回规范化并去重后的非空列表，不允许缺失默认格式。
    @field_validator('default_input_modes', 'default_output_modes')
    @classmethod
    def valid_modes(cls, value):
        return mime_modes(value)

    # 职责：校验可选图标与文档链接的结构。
    # 输入：value 为用户提供的 HTTP/HTTPS URL，空值表示未填写。
    # 结果与约束：禁止凭据及控制字符；只保存链接、不请求地址、不上传文件。
    @field_validator('icon_url', 'documentation_url')
    @classmethod
    def valid_url(cls, value):
        return checked_url(value)

    # 职责：拒绝同一技能列表中重复的标识。
    # 输入：self 为完成字段校验的固定 ACS 输入对象。
    # 结果与约束：重复 id 抛出校验错误，防止编辑或追加时覆盖另一条技能；合法时返回自身。
    @model_validator(mode='after')
    def unique_skills(self):
        ids = [skill.id for skill in self.skills]
        if len(set(ids)) != len(ids):
            raise ValueError('技能标识必须唯一，请删除重复条目后重新添加')
        return self

    # 职责：检查完整预览或提交审核的必填内容。
    # 输入：当前基础信息及每条技能内容。
    # 结果与约束：返回可展示错误列表；名称、能力描述及至少一条技能必填，每条技能名称和描述也必填。
    def errors(self):
        errors = []
        if not self.name:
            errors.append('请填写 ACS 名称')
        if not self.description:
            errors.append('请填写 ACS 能力描述')
        if not self.skills:
            errors.append('请至少添加一条技能')
        for index, skill in enumerate(self.skills, 1):
            if not skill.name:
                errors.append(f'第 {index} 条技能缺少名称')
            if not skill.description:
                errors.append(f'第 {index} 条技能缺少描述')
        for index, registration in enumerate(self.provider.domain_registrations, 1):
            if not registration.domain or not registration.registration_number:
                errors.append(f'第 {index} 条域名备案缺少域名或备案号')
        return errors


# 职责：定义无副作用的未保存表单预览请求。
# 输入：acs_input 为固定字段内容，last_modified_at 为此前读取的资料修改日期。
# 结果与约束：请求不可指定其他账户或修改系统字段；服务器只读取本人资料并返回候选文档；可预览修改后的姓名，但不能将姓名标为私密。
class AcsPreviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    acs_input: AcsInput
    last_modified_at: ModifiedAt
    profile_name: str | None = Field(default=None, max_length=80)
    name_visibility: str | None = Field(default=None, pattern=r"^public$")
    profile_avatar_name: str | None = Field(default=None,max_length=120)
    profile_bio: str | None = Field(default=None,max_length=2000)
    profile_organization: str | None = Field(default=None,max_length=200)
    bio_visibility: str | None = Field(default=None,pattern=r"^(public|private)$")
    organization_visibility: str | None = Field(default=None,pattern=r"^(public|private)$")


# 职责：统一检查 ACS 各类用户 URL 的结构。
# 输入：value 为可选 URL 字符串。
# 结果与约束：只允许不含凭据与控制字符的 HTTP/HTTPS，校验端口，不实际请求链接。
def checked_url(value):
    if value:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or any(ord(c) < 32 for c in value):
            raise ValueError('请填写不含账号密码的 HTTP/HTTPS 链接')
        try:
            parsed.port
        except ValueError:
            raise ValueError('URL 端口格式不正确')
    return value


# 职责：统一校验 MIME 字符串列表。
# 输入：value 为非空字符串列表。
# 结果与约束：拒绝空项、非法格式及过长值，按原顺序去重；不存在任何原始 JSON 编辑入口。
def mime_modes(value):
    import re
    if not value or any(not re.fullmatch(r'[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+', item) or len(item) > 100 for item in value):
        raise ValueError('MIME 请填写类似 text/plain 或 application/json 的格式')
    return list(dict.fromkeys(value))


def generated_description(profile):
    name = public_profile(profile).get('name') or '该用户'
    return f'作为{name}的分身，能根据{name}的资料和语言习惯回答问题。'


def default_input(profile):
    public = public_profile(profile)
    owner = public.get('name') or '该用户'
    modes = ['text/plain', 'application/json', 'text/markdown']
    return AcsInput(name=f'{owner}的分身', template_owner=owner,
                    description=generated_description(profile),
                    provider=AcsProviderInput(organization=public.get('organization') or '', name=public.get('name') or ''),
                    skills=[AcsSkillInput(id='personal-profile.introduction.qa', name='个人介绍问答',
                                         description=f'{owner}能够根据已审核公开资料回答个人背景、经历和研究方向等问题。',
                                         tags=['个人介绍', '问答'], examples=['请介绍一下你自己'],
                                         input_modes=modes, output_modes=modes)])


def resolve_input(stored, profile):
    defaults = default_input(profile)
    if not stored:
        return defaults
    content = AcsInput.model_validate(stored)
    owner = content.template_owner or defaults.template_owner
    prefix = owner + '能够'
    name = content.name
    if not name or name == owner + '的分身':
        name = defaults.name
    description = content.description
    if not description or description in (
        f'作为{owner}的分身，能根据{owner}的资料和语言习惯回答问题。',
        f'作为（{owner}）的分身，你需要根据个人资料库以（{owner}）的视角回答提问。',
    ):
        description = defaults.description
    skills = []
    for skill in content.skills:
        suffix = skill.description.removeprefix(prefix).strip()
        value = defaults.template_owner + '能够' + suffix if suffix else ''
        if len(suffix) > 2000:
            from fastapi import HTTPException
            raise HTTPException(422, '技能描述正文最多 2000 个字符')
        skills.append(skill.model_copy(update={'description': value}))
    return content.model_copy(update={'template_owner': defaults.template_owner, 'name': name,
                                      'description': description, 'skills': skills})


# 职责：根据表单与平台状态生成示例结构的完整 ACS 候选。
# 输入：content 为 AcsInput，user_id 为内部账户 ID，base_url 为平台设置地址，aic 与时间由平台提供。
# 结果与约束：未正式注册时 active=false、协议端点为空；不复制示例身份、不包含 Key 或私密资料，保留证书申请元数据但不签发证书。
def build_acs(content, user_id, base_url, aic='', modified=None):
    parsed = urlsplit(base_url)
    hostname = parsed.hostname
    if parsed.scheme not in ('http', 'https') or not hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('系统对外访问 URL 无效，请管理员检查平台设置')
    try:
        ipaddress.ip_address(hostname)
        dns, ips = [], [hostname]
    except ValueError:
        dns, ips = [hostname], []
    from datetime import datetime, timezone, timedelta
    timestamp = datetime.fromisoformat(modified or now()).astimezone(timezone(timedelta(hours=8))).isoformat()
    document = {
        'aic': aic, 'active': False, 'lastModifiedTime': timestamp, 'protocolVersion': '02.02',
        'name': content.name, 'description': content.description, 'version': content.version,
        'webAppUrl': base_url.rstrip('/') + '/',
        'provider': {'countryCode': content.provider.country_code, 'organization': content.provider.organization,
                     'department': content.provider.department, 'name': content.provider.name, 'url': content.provider.url,
                     'license': content.provider.license, 'email': content.provider.email,
                     'domainRegistrations': [{'domain': row.domain, 'registrationNumber': row.registration_number,
                                              'registrationType': row.registration_type} for row in content.provider.domain_registrations]},
        'securitySchemes': {'mtls': {'type': 'mutualTLS', 'description': 'mTLS 双向身份认证。'}},
        'endPoints': [], 'capabilities': {'streaming': False, 'notification': False, 'messageQueue': []},
        'defaultInputModes': content.default_input_modes, 'defaultOutputModes': content.default_output_modes,
        'skills': [{'id': skill.id, 'name': skill.name, 'description': skill.description, 'version': skill.version,
                    'tags': skill.tags, 'examples': skill.examples, 'inputModes': skill.input_modes, 'outputModes': skill.output_modes}
                   for skill in content.skills],
        'entityUserId': user_id,
        'entityMeta': {'location': content.location, 'environment': '1.0.0', 'userRelation': 'personal-avatar',
                       'aipRole': 'partner', 'framework': 'wit-framework', 'frameworkVersion': wit_version,
                       'serviceMode': 'personal-avatar-qa'},
        'certificate': {'altNames': {'dns': dns, 'ip': ips}, 'requestedValidity': 49},
    }
    document['iconUrl'] = content.icon_url
    document['documentationUrl'] = content.documentation_url
    # 使用已安装 SDK 校验标准字段；保留示例中的 certificate 和 domainRegistrations 扩展，不因 SDK 忽略扩展而删除。
    AgentCapabilitySpec.model_validate(document)
    return document


# 职责：读取管理员保存的服务地址，提供统一候选生成配置。
# 输入：db 为已有连接，默认值使用本系统实际对外地址。
# 结果与约束：不根据浏览器 Host 拼接部署地址，不访问 Registry、CA 或目标 URL。
def public_base_url(db):
    import json
    row = db.execute("SELECT value FROM settings WHERE key='public_url'").fetchone()
    return (json.loads(row[0]) if row else '') or public_url()
