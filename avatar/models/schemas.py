"""Provider-neutral model settings inputs."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, field_validator

from avatar.models.effort import Effort


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
