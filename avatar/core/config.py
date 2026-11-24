# 模块说明：启动配置：统一读取运行目录、网络、会话 Cookie 及记忆预算的环境参数。
import os
import json
from dataclasses import dataclass, field
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ROOT = PACKAGE_ROOT / '_resources' if (PACKAGE_ROOT / '_resources').is_dir() else SOURCE_ROOT
# Installed packages are read-only resources; mutable state stays outside site-packages.
DEFAULT_DATA_DIR = (SOURCE_ROOT / 'data' if ROOT == SOURCE_ROOT else
                    Path(os.getenv('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'personal-avatar-manager')


def public_url():
    return (os.getenv('AVATAR_PUBLIC_URL') or f"http://127.0.0.1:{os.getenv('AVATAR_PORT', '18080')}").rstrip('/')


def aip_public_url():
    return os.getenv('AVATAR_SYSTEM_AIP_PUBLIC_URL') or f"https://127.0.0.1:{os.getenv('AVATAR_SYSTEM_AIP_PORT', '10000')}/aip/rpc"


def require_database_url(value):
    from psycopg import ProgrammingError
    from psycopg.conninfo import conninfo_to_dict

    if not value or not value.strip():
        raise ValueError('必须配置 PostgreSQL DATABASE_URL')
    try:
        conninfo_to_dict(value)
    except ProgrammingError:
        raise ValueError('DATABASE_URL 必须是有效的 PostgreSQL DSN') from None
    return value


def application_version():
    """部署环境优先；源码读取项目元数据，安装包读取发行元数据。"""
    configured = os.getenv('AVATAR_VERSION', '').strip()
    if configured:
        return configured
    project = SOURCE_ROOT / 'pyproject.toml'
    if project.is_file():
        import tomllib
        return tomllib.loads(project.read_text())['project']['version']
    from importlib.metadata import version
    return version('personal-avatar-manager')


def runtime_environment(settings, provider):
    """Describe this startup from the active adapter, configuration and installed resources."""
    from importlib.metadata import PackageNotFoundError, version
    frameworks = {}
    for package in ('wit', 'acps-sdk'):
        try:
            frameworks[package] = version(package)
        except PackageNotFoundError:
            frameworks[package] = None
    manual = ROOT / 'docs' / '操作手册.md'
    return {
        'registration_adapter': type(provider).__name__,
        'registration_mode': getattr(provider, 'mode', settings.provider),
        'communication': [
            {'name': 'Web', 'value': settings.oidc_public_url},
            {'name': '系统 mTLS AIP', 'value': settings.system_aip_public_url if settings.system_aip_enabled else None},
            {'name': '个人分身 mTLS AIP', 'value': f'{settings.entity_aip_port}-{settings.entity_aip_port_end}' if settings.entity_aip_enabled else None},
        ],
        'framework_versions': frameworks,
        'manual_path': str(manual.resolve()) if manual.is_file() else None,
    }


# 职责：保存应用启动配置的不可变数据对象。
# 输入：包含数据目录、Cookie、注册适配器、模型网络、记忆、OIDC 及系统 AIP、管理/个人本体独立注册开关。
# 结果与约束：默认使用根目录 data、demo 注册器并关闭系统 AIP；协议身份与模型凭据独立，冻结对象防止运行期误改。
@dataclass(frozen=True)
class Settings:
    data_dir: Path = DEFAULT_DATA_DIR
    secure_cookie: bool = False
    provider: str = "demo"
    model_proxy: str | None = None
    model_dns_aliases: dict[str, str] = field(default_factory=dict)
    memory_recent_messages: int = 6
    oidc_enabled: bool = False
    oidc_issuer: str = ""
    oidc_client_id: str = "personal-avatar"
    oidc_client_secret: str = field(default="", repr=False)
    oidc_public_url: str = field(default_factory=public_url)
    system_aip_enabled: bool = False
    system_aip_aic: str = ''
    system_aip_bind_address: str = '0.0.0.0'
    system_aip_port: int = 10000
    system_aip_public_url: str = field(default_factory=aip_public_url)
    system_atr_enabled: bool = False
    registry_username: str = field(default='', repr=False)
    registry_password: str = field(default='', repr=False)
    system_atr_poll_interval: float = 3600.0
    system_atr_call_timeout: float = 60.0
    avatar_atr_enabled: bool = False
    entity_aip_enabled: bool = False
    entity_aip_port: int = 10001
    entity_aip_port_end: int = 15000

    town_public_enabled: bool = False
    town_oidc_issuer: str = ''
    town_oidc_audiences: tuple[str, ...] = ()
    database_url: str = field(default="", repr=False)
    redis_socket: str = ""
    town_oidc_client_id: str = ""
    kafka_bootstrap: str = ""

    # 职责：从环境变量构建启动配置。
    # 输入：cls 为配置类型；读取 AVATAR_DATA_DIR、模型、记忆、OIDC 和 AVATAR_SYSTEM_AIP_* 环境变量。
    # 结果与约束：返回 Settings；DNS 别名解析为 JSON，数值转为整数，错误配置直接抛出异常，不静默替换。
    @classmethod
    def from_env(cls):
        return cls(Path(os.getenv("AVATAR_DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser().resolve(),
                   os.getenv("AVATAR_SECURE_COOKIE", "0") == "1",
                   os.getenv("AVATAR_PROVIDER", "demo"),
                   os.getenv("AVATAR_MODEL_PROXY") or None,
                   json.loads(os.getenv("AVATAR_MODEL_DNS_ALIASES", "{}")),
                   int(os.getenv('AVATAR_MEMORY_RECENT_MESSAGES', '6')),
                   os.getenv('AVATAR_OIDC_ENABLED', '0') == '1',
                   os.getenv('AVATAR_OIDC_ISSUER', '').rstrip('/'),
                   os.getenv('AVATAR_OIDC_CLIENT_ID', 'personal-avatar'),
                   os.getenv('AVATAR_OIDC_CLIENT_SECRET', ''),
                   public_url(),
                   os.getenv('AVATAR_SYSTEM_AIP_ENABLED', '0') == '1',
                   os.getenv('AVATAR_SYSTEM_AIP_AIC', ''),
                   os.getenv('AVATAR_SYSTEM_AIP_HOST', '0.0.0.0'),
                   int(os.getenv('AVATAR_SYSTEM_AIP_PORT', '10000')),
                   aip_public_url(),
                   os.getenv('AVATAR_SYSTEM_ATR_ENABLED', '0') == '1',
                   os.getenv('WIT_REGISTRY_USERNAME', ''),
                   os.getenv('WIT_REGISTRY_PASSWORD', ''),
                   max(3600.0, float(os.getenv('AVATAR_SYSTEM_ATR_POLL_INTERVAL', '3600'))),
                   float(os.getenv('AVATAR_SYSTEM_ATR_CALL_TIMEOUT', '60')),
                   os.getenv('AVATAR_PERSONAL_ATR_ENABLED', '0') == '1',
                   os.getenv('AVATAR_ENTITY_AIP_ENABLED', '0') == '1',
                   int(os.getenv('AVATAR_ENTITY_AIP_PORT', '10001')),
                   int(os.getenv('AVATAR_ENTITY_AIP_PORT_END', '15000')),
                   os.getenv('AVATAR_TOWN_PUBLIC_ENABLED', '0') == '1',
                   os.getenv('AVATAR_TOWN_OIDC_ISSUER', '').rstrip('/'),
                   tuple(v.strip() for v in os.getenv('AVATAR_TOWN_OIDC_AUDIENCES', '').split(',') if v.strip()),
                   require_database_url(os.getenv('DATABASE_URL')),
                   os.getenv('AVATAR_REDIS_SOCKET', '').strip(),
                   os.getenv('AVATAR_TOWN_OIDC_CLIENT_ID', '').strip(),
                   os.getenv('AVATAR_KAFKA_BOOTSTRAP', '').strip())
