# 模块说明：管理系统唯一服务 ACS 与专用证书目录；直接声明小镇 RPC 端点，管理服务不作为个人分身本体。
import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from wit.acps.identity.acs import (
    REGISTRATION_CANDIDATE_LAST_MODIFIED_TIME,
    validate_registration_candidate_acs,
    validate_registry_acs,
)
from avatar.core.config import aip_public_url, application_version, public_url
from avatar.protocols.identity.acs import AcsInput, AcsProviderInput, AcsSkillInput, build_acs

SYSTEM_ENTITY_ID = "personal-avatar-manager-system"


def registration_urls(registry_url, ca_url, mtls_url):
    urls = {}
    for key, env, explicit, schemes in (
        ('registry_url', 'WIT_REGISTRY_URL', registry_url, ('https', 'http')),
        ('certificate_authority_url', 'WIT_CA_URL', ca_url, ('https',)),
        ('registry_entity_mtls_url', 'WIT_REGISTRY_MTLS_URL', mtls_url, ('https',)),
    ):
        value = explicit or os.getenv(env, '')
        parsed = urlsplit(value)
        if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(f'请配置有效的 {env}')
        urls[key] = value
    return urls


# 职责：校验系统独立 mTLS RPC 端点并生成 ACS 中的端点声明。
# 输入：url 为运维确认的 HTTPS 地址，路径固定 /aip/rpc，不接受凭据或查询参数。
# 结果与约束：仅声明 JSONRPC 和 mTLS；不虚构 AMQP、流式或通知端点，监听启用另由证书就绪状态决定。
def system_aip_endpoint(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != "/aip/rpc"
        or any(ord(char) <= 32 for char in url)
    ):
        raise ValueError(
            "系统 AIP 地址必须为 HTTPS，路径为 /aip/rpc，且不带凭据、查询参数或 fragment"
        )
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("系统 AIP 端口无效")
    return {"url": url, "transport": "JSONRPC", "security": [{"mtls": []}]}


# 职责：集中表达一个服务或本体的持久身份文件路径。
# 输入：root 是身份独占目录；管理系统和个人分身本体各自提供不同路径，不包含用户账号标识。
# 结果与约束：ACS、注册配置、证书状态及 CA 信任材料均在该目录；不指向账户数据库或个人分身目录。
@dataclass(frozen=True)
class SystemIdentityPaths:
    root: Path
    acs: Path
    registration: Path
    certificates: Path
    ca_bundle: Path


# 职责：根据业务数据目录计算管理系统身份的专用路径。
# 输入：data_dir 为项目 data 或运维显式指定的数据目录。
# 结果与约束：只计算路径、不创建文件；证书目录同时作为 wit ATR 的 state_dir，由框架维护内部布局。
def system_identity_paths(data_dir):
    root = Path(data_dir) / "system_identity"
    return SystemIdentityPaths(
        root,
        root / "acs.json",
        root / "registration.json",
        root / "certificates",
        root / "ca" / "ca.pem",
    )


# 职责：生成管理系统唯一服务身份的完整 ACS 注册候选。
# 输入：base_url 为 Web 根地址，aip_url 为独立 mTLS RPC 地址，不读取个人档案或登录信息。
# 结果与约束：候选保留空 AIC 和 active=false，提前声明 RPC 端点；以普通服务注册，不派生第二个管理身份。
def build_system_acs(base_url=None, aip_url=None):
    base_url = (base_url or public_url()).rstrip("/")
    content = AcsInput(
        name="镜像 · 个人分身智能体管理系统",
        version=application_version(),
        description="提供个人分身申请与资料维护、公开分身目录查询、账户绑定及配置管理入口。",
        icon_url=base_url + "/static/system-avatar.svg",
        documentation_url=base_url + "/api/docs",
        provider=AcsProviderInput(
            organization="个人分身智能体管理系统",
            department="系统服务",
            name="系统维护者",
            url=base_url,
        ),
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
        skills=[
            AcsSkillInput(
                id="avatar.manager.profile",
                name="分身申请与资料维护",
                description="提供资料填写、更新及申请进度查询的页面入口。",
                tags=["分身申请", "个人资料", "审核"],
                examples=["我想申请个人分身", "我要更新个人资料", "查询我的申请状态"],
            ),
            AcsSkillInput(
                id="avatar.manager.discovery",
                name="公开分身目录查询",
                description="根据姓名、分身名称或 AIC 查询公开分身目录。",
                tags=["分身目录", "只读查询"],
                examples=["有哪些分身", "查询公开分身名称和 AIC"],
            ),
            AcsSkillInput(
                id="avatar.manager.account-link",
                name="账户绑定与资料入口",
                description="提供平台账户绑定和个人资料管理入口。",
                tags=["小镇接入", "账户绑定", "资料入口"],
                examples=["从小镇进入我的资料管理", "绑定我的小镇账号"],
            ),
            AcsSkillInput(
                id="avatar.manager.settings",
                name="资料文件与模型配置入口",
                description="提供资料文件及模型配置的管理入口。",
                tags=["资料文件", "模型配置"],
                examples=["管理我上传的资料文件", "配置分身使用的模型"],
            ),
        ],
    )
    document = build_acs(
        content,
        SYSTEM_ENTITY_ID,
        base_url,
        modified=REGISTRATION_CANDIDATE_LAST_MODIFIED_TIME,
    )
    document["entityMeta"].update(
        userRelation="personal-avatar-management-system",
        serviceMode="personal-avatar-management",
        aipRole="partner",
    )
    document["endPoints"] = [system_aip_endpoint(aip_url or aip_public_url())]
    validate_registration_candidate_acs(document)
    return document


# 职责：创建仅供系统身份使用的受保护目录。
# 输入：path 为数据根、系统身份根、证书或 CA 子目录。
# 结果与约束：目录权限为 0700，拒绝符号链接，避免以后证书和私钥写到专用目录之外。
def private_directory(path):
    if path.is_symlink():
        raise ValueError("系统身份目录不能使用符号链接")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


# 职责：首次创建身份 JSON，后续初始化保留已有文件。
# 输入：path 为 ACS/配置路径，value 是不含密码、Token 或私钥的字典。
# 结果与约束：使用排他创建和 0600 权限，不覆盖已有内容；拒绝符号链接，读取已有 JSON 仅作结构校验。
def create_identity_json(path, value):
    if path.is_symlink():
        raise ValueError("系统身份文件不能使用符号链接")
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
    except FileExistsError:
        existing = json.loads(path.read_text())
        if not isinstance(existing, dict):
            raise ValueError("已有系统身份 JSON 必须是对象，不会自动覆盖")
        return existing
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return value


# 职责：初始化系统 ACS、wit 注册配置及未来证书目录。
# 输入：data_dir 与明确的 Web/Registry/CA 根地址；不读取账户数据或注册凭据。
# 结果与约束：只创建缺失文件，已有 ACS/配置/证书不覆盖；不生成伪造 PEM、不修改数据库，也不联系 Registry 或 CA。
def initialize_system_identity(
    data_dir,
    base_url=None,
    registry_url=None,
    ca_url=None,
    aip_url=None,
    mtls_url=None,
):
    data_dir = Path(data_dir)
    paths = system_identity_paths(data_dir)
    for directory in (data_dir, paths.root, paths.certificates, paths.ca_bundle.parent):
        private_directory(directory)
    urls = {} if paths.registration.exists() else registration_urls(registry_url, ca_url, mtls_url)
    candidate = create_identity_json(paths.acs, build_system_acs(base_url, aip_url))
    if candidate.get("entityUserId") != SYSTEM_ENTITY_ID:
        raise ValueError("已有 ACS 不属于管理系统，拒绝覆盖或复用")
    (
        validate_registry_acs
        if candidate.get("aic")
        else validate_registration_candidate_acs
    )(candidate)
    create_identity_json(
        paths.registration,
        {
            **urls,
            "ca_cert_path": "ca/ca.pem",
            "acs_path": "acs.json",
            "state_dir": "certificates",
            "mode": "integrated",
            "use_saved_urls": False,
        },
    )
    return paths
