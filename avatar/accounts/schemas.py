"""Account and platform-binding request contracts."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from avatar.accounts.roles import AccountRole


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


# 职责：定义管理员授权或自主降级的目标角色。
# 输入：role 是平台账户角色，禁止多余请求字段。
# 结果与约束：不能通过附加 is_root 取得根身份，实际可升降级范围由账户管理服务检查。
class AccountRoleRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: AccountRole
