# 模块说明：模型配置与凭据保护：平台建议、配置脱敏及本地主密钥加密，与可公开个人资料分离。
"""Model configuration and encrypted credentials, separate from personal profiles."""
import os
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from avatar.models.limits import limits, validate

PROVIDERS = {
    "openai": ("OpenAI", "openai", os.getenv("AVATAR_OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")),
    "deepseek": ("DeepSeek", "openai", os.getenv("AVATAR_DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")),
    "qwen": ("通义千问 / 百炼", "openai", os.getenv("AVATAR_QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")),
    "moonshot": ("Kimi / Moonshot", "openai", os.getenv("AVATAR_MOONSHOT_BASE_URL", "https://api.moonshot.cn/v1").rstrip("/")),
    "glm": ("智谱 GLM", "openai", os.getenv("AVATAR_GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")),
    "siliconflow": ("硅基流动", "openai", os.getenv("AVATAR_SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1").rstrip("/")),
    "openrouter": ("OpenRouter", "openai", os.getenv("AVATAR_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")),
    "anthropic": ("Anthropic / Claude", "anthropic", os.getenv("AVATAR_ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1").rstrip("/")),
    "gemini": ("Google Gemini", "gemini", os.getenv("AVATAR_GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")),
    "custom": ("自定义平台 / 网关", "openai", ""),
}


# 预设平台只连接固定官方地址，自定义网关必须显式选择 custom。
def provider_base_url(provider,value):
    default=PROVIDERS[provider][2]
    if provider!='custom' and value and value.rstrip('/')!=default:
        raise HTTPException(422,'预设平台的 API 地址固定；使用其他地址请选择自定义平台 / 网关')
    return value.rstrip('/') if provider=='custom' else default


# 模型名称为可编辑建议，实际可用性由上游账户决定。
MODEL_PRESETS = {
    "openai": ["gpt-4.1-mini", "gpt-4.1", "gpt-4o"],
    "deepseek": ["deepseek-v4-flash", "deepseek-v4-pro"],
    "qwen": ["qwen-plus", "qwen-turbo", "qwen-max"],
    "moonshot": ["moonshot-v1-8k", "moonshot-v1-32k"],
    "glm": ["glm-4-plus", "glm-4-flash"],
    "siliconflow": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"],
    "openrouter": ["openai/gpt-4.1-mini", "google/gemini-2.5-flash"],
    "anthropic": ["claude-sonnet-4-5", "claude-haiku-4-5"],
    "gemini": ["gemini-2.5-flash", "gemini-2.5-pro"],
    "custom": [],
}

# 职责：组织前端可选择的平台与模型预设目录。
# 输入：使用 PROVIDERS 与 MODEL_PRESETS 常量，无需网络请求。
# 结果与约束：返回平台 ID、名称、接口格式、默认根地址和模型建议；实际可用性由上游账户决定。
def catalog():
    return [{"id": k, "name": v[0], "api_format": v[1], "base_url": v[2], "models": MODEL_PRESETS[k], "model_limits": {model: limits(k,model) for model in MODEL_PRESETS[k] if limits(k,model)}} for k, v in PROVIDERS.items()]


# 职责：提取可向前端展示的模型配置并隐藏凭据。
# 输入：config 为可能包含 encrypted_key 的配置字典。
# 结果与约束：仅返回平台、格式、地址、模型、输出上限及 key_configured 标志，不返回明文或密文 Key。
def public_config(config):
    return {k: config[k] for k in ("provider", "api_format", "base_url", "model", "max_tokens", "vision_enabled", "audio_enabled", "context_tokens", "compression_ratio", "capability_status", "capability_source") if k in config} | {
        "key_configured": bool(config.get("encrypted_key"))}


# 职责：管理模型 API Key 的本地加密主密钥及配置合并。
# 输入：使用数据目录中的 model-secrets.key 和 Fernet 对称加密。
# 结果与约束：与个人资料公开字段分离；数据库和主密钥须配套备份，已有密文时禁止重新生成替代密钥。
class SecretVault:
    # 职责：加载或首次生成模型凭据加密主密钥。
    # 输入：data_dir 为受保护数据目录，database 用于检查是否已有加密配置。
    # 结果与约束：已有密文但主密钥缺失时启动失败；首次创建使用排他写入、0600 权限和 fsync，随后初始化加密器。
    def __init__(self, data_dir: Path, database):
        path = data_dir / "model-secrets.key"
        if not path.exists():
            with database.connect() as db:
                existing = db.execute("SELECT 1 FROM model_settings WHERE model_config LIKE '%encrypted_key%' LIMIT 1").fetchone()
                if not existing and db.execute("SELECT 1 FROM sqlite_master WHERE name='embedding_settings'").fetchone():
                    existing=db.execute("SELECT 1 FROM embedding_settings WHERE config LIKE '%encrypted_key%' LIMIT 1").fetchone()
            if existing:
                raise RuntimeError("模型加密主密钥缺失，请恢复 data/model-secrets.key，禁止生成新密钥覆盖")
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as out:
                    out.write(Fernet.generate_key())
                    out.flush()
                    os.fsync(out.fileno())
            except FileExistsError:
                pass
        path.chmod(0o600)
        self.cipher = Fernet(path.read_bytes())

    # 职责：加密准备保存的模型 API Key。
    # 输入：value 为明文字符串，仅在服务端处理。
    # 结果与约束：返回 Fernet 密文字符串；调用者负责保存，不应记录明文日志。
    def encrypt(self, value):
        return self.cipher.encrypt(value.encode()).decode()

    # 职责：解密调用上游模型所需的 API Key。
    # 输入：value 为数据库中保存的 Fernet 密文。
    # 结果与约束：返回明文供模型请求使用；密文或主密钥不匹配返回 503，提示恢复主密钥，不自动覆盖。
    def decrypt(self, value):
        try:
            return self.cipher.decrypt(value.encode()).decode()
        except (InvalidToken, ValueError):
            raise HTTPException(503, "模型密钥无法解密，请联系管理员恢复加密主密钥")

    # 职责：将本次模型配置输入与已有配置合并。
    # 输入：request 为可选 ModelConfigInput，previous 为含旧密文的配置字典。
    # 结果与约束：未提交配置时保留原值；新 Key 加密保存，只有平台、格式和地址均未变化才可沿用旧 Key，避免泄露到新端点。
    def resolve(self, request, previous):
        if request is None:
            return previous
        data = request.model_dump(exclude={"api_key"})
        _, api_format, _ = PROVIDERS[request.provider]
        data["api_format"] = api_format if request.provider != "custom" else request.api_format
        data["base_url"] = provider_base_url(request.provider,request.base_url)
        key = request.api_key.get_secret_value().strip() if request.api_key else ""
        # 平台、地址或格式变化时禁止自动携带旧 Key；新目标必须由用户明确提供凭据。
        same_target = all(previous.get(k) == data[k] for k in ("provider", "api_format", "base_url"))
        if same_target and previous.get('model')==data['model']:
            for name in ('vision_enabled','audio_enabled'):
                if name not in request.model_fields_set:data[name]=previous.get(name,False)
        data['context_tokens']=data.get('context_tokens') or (limits(data['provider'],data['model']) or {}).get('context_tokens',128000)
        validate(data)
        data["encrypted_key"] = self.encrypt(key) if key else previous.get("encrypted_key", "") if same_target else ""
        return data


# 职责：检查提交审核或模型调用所需配置是否齐全。
# 输入：config 为已解析模型配置，要求模型、根地址和 encrypted_key 均非空。
# 结果与约束：检查通过无返回值；缺项返回 422，本方法不验证上游 Key 权限或余额。
def require_complete(config):
    if not all(config.get(k) for k in ("model", "base_url", "encrypted_key")):
        raise HTTPException(422, "请配置模型平台、模型名称、API 地址和 API Key 后提交审核")
