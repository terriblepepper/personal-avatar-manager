# 模块说明：输入契约：Pydantic 负责字段结构、格式和部分跨字段约束，数据库归属与业务权限另行检查。
from pydantic import StrictBool, BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from typing import Literal, Annotated
from avatar.accounts.roles import AccountRole
from avatar.core.text_budget import AgentText
from avatar.models.effort import Effort
from avatar.protocols.identity.acs import AcsInput
from avatar.files.library.attachments import AttachmentRef
from avatar.profiles.profile_dates import ModifiedAt

# 普通文本与既有结构化数组兼容；编辑器不要求用户输入 JSON，旧调用者仍可保留原数组。
FlexibleRecords = Annotated[str, Field(max_length=6000)] | Annotated[list[dict], Field(max_length=50)]


# 职责：定义本地注册凭据的基础字段与长度约束。
# 输入：username 允许字母数字及点、横线、下划线，password 为 8–128 位。
# 结果与约束：供注册请求继承；邮箱和手机号登录使用更宽的 LoginRequest，不在此执行密码哈希。
class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=40, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)



# 职责：规范化并校验登录邮箱格式。
# 输入：value 为字符串，去首尾空白并转为小写。
# 结果与约束：返回规范化邮箱，可保留空值；非空且格式错误抛出 ValueError，是否必填由具体请求决定。
def normalize_email(value):
    import re
    value = value.strip().lower()
    if value and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise ValueError("邮箱格式不正确")
    return value


# 职责：规范化登录或找回使用的手机号。
# 输入：value 去空白、括号和横线；大陆 +86、0086 与本地号码统一。
# 结果与约束：允许空值用于选填，非空要求 7–15 位数字和可选加号，非法格式抛出 ValueError。
def normalize_phone(value):
    import re
    value = re.sub(r"[ ()-]", "", value.strip())
    if value and not re.fullmatch(r"\+?\d{7,15}", value):
        raise ValueError("手机号格式不正确")
    # 大陆本地号码与 +86、0086 国际形式规范化为同一登录别名。
    if value.startswith('+86') and len(value) == 14:
        value = value[3:]
    elif value.startswith('0086') and len(value) == 15:
        value = value[4:]
    return value


# 职责：定义用户名、邮箱或手机号登录输入。
# 输入：username 承载任意一种登录标识，password 为明文输入。
# 结果与约束：仅校验长度，实际规范化、账户匹配和密码验证由 auth.login 完成。
class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=160)
    password: str = Field(min_length=8, max_length=128)


# 职责：定义 Demo 简化密码找回请求。
# 输入：contact 只能是邮箱或手机号，new_password 为符合长度的新密码。
# 结果与约束：联系人字段规范化，业务仅匹配注册联系方式，不证明联系方式持有人。
class RecoveryRequest(BaseModel):
    contact: str = Field(min_length=3, max_length=160)
    new_password: str = Field(min_length=8, max_length=128)

    # 职责：按邮箱或手机号规则校验找回联系人。
    # 输入：value 为 contact 字段，以是否含 @ 选择规范化方法。
    # 结果与约束：返回规范化联系人，格式错误由 Pydantic 转为请求校验错误。
    @field_validator("contact")
    @classmethod
    def valid_contact(cls, value):
        return normalize_email(value) if '@' in value else normalize_phone(value)


# 职责：定义登录后修改密码的输入。
# 输入：current_password 与 new_password 均须满足密码长度限制。
# 结果与约束：实际验证旧密码和拒绝相同新密码由认证服务完成。
class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=8, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


# 职责：统一学生学号的比较与存储格式。
# 输入：value 为原始学号字符串。
# 结果与约束：NFKC 转换全角、去首尾空白并大写；只做规范化，不查询唯一性或自动合并账户。
def normalize_student_id(value):
    import unicodedata
    return unicodedata.normalize("NFKC", value).strip().upper()


# 职责：定义普通学生或教师本地注册请求。
# 输入：继承凭据，要求邮箱，手机号选填，包含身份、学号、姓名与可选平台入口令牌。
# 结果与约束：学生学号选填，教师必须姓名且无学号；平台账号由令牌服务端记录确定，不能手工指定可信身份。
class RegistrationRequest(Credentials):
    platform_entry_token: str | None = Field(default=None,min_length=32,max_length=128,pattern=r'^[A-Za-z0-9_-]+$')
    email: str = Field(min_length=3, max_length=160)
    phone: str = Field(default="", max_length=40)

    # 职责：校验注册邮箱为非空且格式正确。
    # 输入：value 为 email 字段，先调用 normalize_email。
    # 结果与约束：返回规范化邮箱，空值或错误格式触发校验失败；唯一性由数据库事务检查。
    @field_validator("email")
    @classmethod
    def email_value(cls, value):
        normalized = normalize_email(value)
        if not normalized:
            raise ValueError("邮箱不能为空")
        return normalized

    # 职责：规范化注册时可选的手机号。
    # 输入：value 为 phone 字段。
    # 结果与约束：返回统一登录别名，可为空；格式校验不等同于验证手机号持有人。
    @field_validator("phone")
    @classmethod
    def phone_value(cls, value):
        return normalize_phone(value)

    person_type: Literal["student", "teacher"] = "student"
    student_id: str = Field(default="", max_length=80)
    name: str = Field(default="", max_length=80)

    # 职责：规范化注册时的学生学号。
    # 输入：value 为 student_id 字段。
    # 结果与约束：返回 NFKC、大写和去空白后的值，跨字段身份规则由 identity_requirements 检查。
    @field_validator("student_id")
    @classmethod
    def normalized_id(cls, value):
        return normalize_student_id(value)

    # 职责：检查注册身份与学号、姓名的组合约束。
    # 输入：self 为完成字段校验的注册对象。
    # 结果与约束：教师有学号或无姓名时抛出 ValueError；合法返回自身，不自动授予管理权限。
    @model_validator(mode="after")
    def identity_requirements(self):
        if self.person_type == "teacher" and (self.student_id or not self.name.strip()):
            raise ValueError("教师注册需填写姓名，不填写学号")
        return self


# 职责：定义修改登录联系方式的请求。
# 输入：必填 email 和 current_password，phone 选填。
# 结果与约束：联系方式规范化后由服务校验旧密码和唯一性，不影响个人资料公开字段。
class ContactChangeRequest(BaseModel):
    email: str = Field(min_length=3, max_length=160)
    phone: str = Field(default="", max_length=40)
    current_password: str = Field(min_length=8, max_length=128)

    # 职责：规范化修改后的邮箱并要求非空。
    # 输入：value 为新的登录邮箱。
    # 结果与约束：返回合法小写邮箱；不发送验证邮件，不在校验器中写数据库。
    @field_validator("email")
    @classmethod
    def email_value(cls, value):
        value = normalize_email(value)
        if not value:
            raise ValueError("邮箱不能为空")
        return value

    # 职责：规范化修改后的可选手机号。
    # 输入：value 为新的登录手机号，空值表示不设置。
    # 结果与约束：返回登录别名或空字符串，数据库更新及冲突检查由认证服务完成。
    @field_validator("phone")
    @classmethod
    def phone_value(cls, value):
        return normalize_phone(value)


# 职责：定义平台临时入口的唯一请求参数。
# 输入：token 为 32–128 位 URL 安全字符组成的随机令牌。
# 结果与约束：extra=forbid 拒绝手工附加 account_id 等参数；有效性与绑定范围在服务端验证。
class PlatformEntryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str = Field(min_length=32,max_length=128,pattern=r'^[A-Za-z0-9_-]+$')


# 职责：定义学生、教师个人资料及逐项隐私选择。
# 输入：包含文本、列表、结构化经历和 visibility 映射，允许草稿暂不完整。
# 结果与约束：拒绝未知顶层字段并限制字段长度；提交时另调用 validate_submission，模型 Key 不属于资料字段。
class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(default="", max_length=80)
    avatar_name: str = Field(default="", max_length=100)
    bio: str = Field(default="", max_length=2000)
    organization: str = Field(default="", max_length=160)
    specialty: str = Field(default="", max_length=300)
    contact: str = Field(default="", max_length=160)
    experience: str = Field(default="", max_length=5000)
    other: str = Field(default="", max_length=2000)
    person_type: Literal["student", "teacher"] = "student"
    student_id: str = Field(default="", max_length=80)
    employee_id: str = Field(default="", max_length=80)
    preferred_name: str = Field(default="", max_length=80)
    location: str = Field(default="", max_length=160)
    email: str = Field(default="", max_length=160)
    phone: str = Field(default="", max_length=40)
    homepage: str = Field(default="", max_length=500)
    current_advisor: str = Field(default="", max_length=160)
    gender: str = Field(default="", max_length=40)
    highest_degree: str = Field(default="", max_length=80)
    title: str = Field(default="", max_length=80)
    supervisor_qualification: str = Field(default="", max_length=160)
    current_projects: FlexibleRecords = Field(default_factory=list)
    skills: FlexibleRecords = Field(default_factory=list)
    projects: FlexibleRecords = Field(default_factory=list)
    education: FlexibleRecords = Field(default_factory=list)
    appointments: FlexibleRecords = Field(default_factory=list)
    supervised_students: Annotated[str, Field(max_length=6000)] | Annotated[list[dict], Field(max_length=100)] = Field(default_factory=list)
    workstations: FlexibleRecords = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list, max_length=50)
    academic_highlights: list[str] = Field(default_factory=list, max_length=50)
    interests: list[str] = Field(default_factory=list, max_length=50)
    research_outputs: list[str] = Field(default_factory=list, max_length=100)
    supplementary_text: str = Field(default="", max_length=6000)
    attachments: list[AttachmentRef] = Field(default_factory=list, max_length=20, json_schema_extra={"deprecated":True}, description="仅兼容旧审核记录；当前文件配置请使用独立资料文件接口")
    visibility: dict[str, Literal["public", "private"]] = Field(default_factory=dict)

    # 职责：规范化资料中的学生学号。
    # 输入：value 为 student_id 字段。
    # 结果与约束：返回统一格式；教师不得提交学号；学号是可修改的资料字段，不用于账户绑定。
    @field_validator("student_id")
    @classmethod
    def normalized_id(cls, value):
        return normalize_student_id(value)

    # 职责：校验隐私映射只引用已定义资料字段。
    # 输入：value 为字段名到 public 或 private 的映射。
    # 结果与约束：未知字段或将姓名/分身名称设为私密时拒绝；固定字段补为公开，其文字仍可修改并提交审核。
    @field_validator("visibility")
    @classmethod
    def valid_visibility(cls, value):
        from avatar.profiles.profile_fields import FIELD_NAMES, FIXED_PUBLIC_FIELDS
        if set(value) - FIELD_NAMES:
            raise ValueError("包含未知隐私字段")
        if any(value.get(key) == 'private' for key in FIXED_PUBLIC_FIELDS):
            raise ValueError('姓名和分身名称必须公开，不能设置为私密')
        return {**value, **{key: 'public' for key in FIXED_PUBLIC_FIELDS}}

    # 职责：检查草稿是否具备提交分身审核的条件。
    # 输入：读取姓名、分身名称、简介、身份学号与可选联系方式。
    # 结果与约束：必填或格式不满足返回 422；通过无返回值，模型配置完整性与账户激活状态在业务服务另行检查。
    def validate_submission(self):
        from fastapi import HTTPException
        for key in ("name", "avatar_name", "bio"):
            if not getattr(self, key):
                raise HTTPException(422, "姓名、分身名称和简介为必填项")
        if self.person_type == "teacher" and self.student_id:
            raise HTTPException(422, "教师资料不包含学号")
        if self.person_type == "student" and self.employee_id:
            raise HTTPException(422, "学生资料不包含教师工号")
        import re
        if self.contact and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+|\+?[\d ()-]{7,25}", self.contact):
            raise HTTPException(422, "联系方式请填写邮箱或电话号码")
        if self.email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", self.email):
            raise HTTPException(422, "邮箱格式不正确")
        if self.phone and not re.fullmatch(r"\+?[\d ()-]{7,25}", self.phone):
            raise HTTPException(422, "手机号 / 电话格式不正确")


# 职责：定义用户独立保存的模型平台及凭据配置。
# 输入：支持预设平台、自定义格式和模型 ID，API Key 使用 SecretStr 包装，max_tokens 限制输出预算。
# 结果与约束：禁止未知字段，Key 不直接公开；字段校验不代替公网 DNS 检查或上游可用性验证。
class ModelConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    provider: Literal["openai", "deepseek", "qwen", "moonshot", "glm", "siliconflow", "openrouter", "anthropic", "gemini", "custom"] = "openai"
    api_format: Literal["openai", "anthropic", "gemini"] = "openai"
    base_url: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=160, pattern=r"^[a-zA-Z0-9_./:@-]*$")
    api_key: SecretStr | None = None
    max_tokens: int = Field(default=1024, ge=128, le=10000000)
    context_tokens: int | None = Field(default=None, ge=2048, le=10000000)
    reasoning_effort: Effort | None = None
    compression_ratio: float = Field(default=0.6, ge=0.1, le=0.95)
    vision_enabled: StrictBool = False
    audio_enabled: StrictBool = False

    # 职责：校验自定义模型 API 根地址。
    # 输入：value 可为空以使用平台默认地址；非空为 HTTP/HTTPS 且不包含凭据、查询或 fragment。
    # 结果与约束：HTTP 仍仅可在服务端显式允许的内网 origin 使用；验证端口格式并去末尾斜线。
    @field_validator("base_url")
    @classmethod
    def valid_endpoint(cls, value):
        from urllib.parse import urlsplit
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("模型 API 地址必须为不含凭据、查询参数和片段的 HTTP/HTTPS 根地址")
            try:
                parsed.port
            except ValueError:
                raise ValueError("模型 API 地址端口无效")
        return value.rstrip("/")

    # 职责：校验并清理 API Key 的字符串格式。
    # 输入：value 为可选 SecretStr，读取其明文仅做校验。
    # 结果与约束：限制为最多 4096 位非空白可打印 ASCII，返回新 SecretStr；不联系上游验证权限或余额。
    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value):
        if value:
            key = value.get_secret_value().strip()
            if len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
                raise ValueError("API Key 需为不含空白的有效凭据，最多 4096 位")
            return SecretStr(key)
        return value


# 职责：定义保存个人资料草稿的请求。
# 输入：profile 为资料对象，last_modified_at 为此前读取的修改日期。
# 结果与约束：日期须含时区；事务以日期精确比较防止覆盖他人编辑，不接受数字资料版本。
class DraftRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    profile: Profile
    last_modified_at: ModifiedAt
    acs_input: AcsInput | None = None
    acs_edit: bool = False


# 职责：定义提交当前资料日期对应内容的审核请求。
# 输入：last_modified_at 为前端读取的上次修改日期。
# 结果与约束：服务核对日期与状态，提交不会更改实际资料修改日期；每次申请由独立随机 ID 标识。
class SubmitRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    last_modified_at: ModifiedAt


# 职责：定义分身资料审核决定。
# 输入：approve 表示通过，reason 为最多 1000 字符的原因。
# 结果与约束：驳回原因必填的业务约束由相应服务检查，路由负责管理员授权。
class ReviewRequest(BaseModel):
    approve: bool
    reason: str = Field(default="", max_length=1000)


# 职责：定义本人分身或 Avatar 助手问答请求。
# 输入：message 文本按 UTF-8 计量，最大 64 KiB，channel 仅 assistant/avatar，conversation_id 可选。
# 结果与约束：只承载用户消息和会话 ID，不能由浏览器声明可信平台调用者或任意目标账户身份。
class ChatRequest(BaseModel):
    request_id: str | None = Field(default=None, max_length=80)
    personal: StrictBool = False
    message: AgentText
    channel: str = Field(default="assistant", pattern="^(assistant|avatar)$")
    conversation_id: str | None = Field(default=None, max_length=80)
    attachment_ids: list[str] = Field(default_factory=list,max_length=5)
    question_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    agent_pause_id: str | None = Field(default=None, min_length=1, max_length=80)
    agent_control: Literal['continue','retry','end'] | None = None

    @model_validator(mode='after')
    def validate_agent_control(self):
        if self.question_id and (self.agent_control != 'continue' or not self.message.strip()):
            raise ValueError('回答分身提问时请填写补充内容')
        if not self.message and self.agent_control != 'continue':
            raise ValueError('请填写消息')
        if bool(self.agent_pause_id) != bool(self.agent_control) or self.agent_control and not self.conversation_id:
            raise ValueError('控制暂停任务需同时指定会话、暂停标识和操作')
        return self


# 职责：定义创建一个新 Web 对话会话的参数。
# 输入：channel 选择助手或本人分身，title 为 1–80 字符。
# 结果与约束：会话所属账户和目标由认证上下文与业务服务确定。
class ConversationRequest(BaseModel):
    channel: Literal['assistant', 'avatar'] = 'avatar'
    title: str = Field(default='新会话', min_length=1, max_length=80)


class ConversationTitleRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=80)


# 职责：定义管理员授权或自主降级的目标角色。
# 输入：role 是平台账户角色，禁止多余请求字段。
# 结果与约束：不能通过附加 is_root 取得根身份，实际可升降级范围由账户管理服务检查。
class AccountRoleRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: AccountRole


# 职责：定义运维可保存的 Registry、CA、mTLS 和公开服务地址。
# 输入：各 URL 为可选配置字符串并限制长度。
# 结果与约束：仅保存配置，不启用真实注册或证书签发；字段由统一 URL 校验器处理。
class PlatformSettings(BaseModel):
    registry_url: str = Field(default="", max_length=500)
    certificate_authority_url: str = Field(default="", max_length=500)
    registry_entity_mtls_url: str = Field(default="", max_length=500)
    public_url: str = Field(default="", max_length=500)

    # 职责：校验可选平台服务 URL 的基本结构。
    # 输入：value 为某一配置地址，允许 HTTP/HTTPS，拒绝用户凭据及 fragment。
    # 结果与约束：返回去末尾斜线的 URL 或空值；仅格式检查，真实接口信任和网络核验由后续适配器负责。
    @field_validator("registry_url", "certificate_authority_url", "registry_entity_mtls_url", "public_url")
    @classmethod
    def valid_url(cls, value):
        from urllib.parse import urlsplit
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise ValueError("请填写不含密码的 HTTP/HTTPS URL")
        return value.rstrip("/")
