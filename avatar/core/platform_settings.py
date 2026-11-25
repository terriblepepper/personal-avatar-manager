"""Validated platform configuration values."""
from pydantic import BaseModel, Field, field_validator


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
