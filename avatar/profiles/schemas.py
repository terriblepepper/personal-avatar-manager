"""Profile drafts and review requests."""
import re
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from avatar.accounts.schemas import normalize_student_id
from avatar.contracts.profile_fields import FIELD_NAMES, FIXED_PUBLIC_FIELDS
from avatar.core.revisions import ModifiedAt
from avatar.files.library.attachments import AttachmentRef
from avatar.protocols.identity.acs import AcsInput


# 普通文本与既有结构化数组兼容；编辑器不要求用户输入 JSON，旧调用者仍可保留原数组。
FlexibleRecords = Annotated[str, Field(max_length=6000)] | Annotated[list[dict], Field(max_length=50)]

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
        if set(value) - FIELD_NAMES:
            raise ValueError("包含未知隐私字段")
        if any(value.get(key) == 'private' for key in FIXED_PUBLIC_FIELDS):
            raise ValueError('姓名和分身名称必须公开，不能设置为私密')
        return {**value, **{key: 'public' for key in FIXED_PUBLIC_FIELDS}}

    # 职责：检查草稿是否具备提交分身审核的条件。
    # 输入：读取姓名、分身名称、简介、身份学号与可选联系方式。
    # 结果与约束：必填或格式不满足返回 422；通过无返回值，模型配置完整性与账户激活状态在业务服务另行检查。
    def validate_submission(self):
        for key in ("name", "avatar_name", "bio"):
            if not getattr(self, key):
                raise HTTPException(422, "姓名、分身名称和简介为必填项")
        if self.person_type == "teacher" and self.student_id:
            raise HTTPException(422, "教师资料不包含学号")
        if self.person_type == "student" and self.employee_id:
            raise HTTPException(422, "学生资料不包含教师工号")
        if self.contact and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+|\+?[\d ()-]{7,25}", self.contact):
            raise HTTPException(422, "联系方式请填写邮箱或电话号码")
        if self.email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", self.email):
            raise HTTPException(422, "邮箱格式不正确")
        if self.phone and not re.fullmatch(r"\+?[\d ()-]{7,25}", self.phone):
            raise HTTPException(422, "手机号 / 电话格式不正确")


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
