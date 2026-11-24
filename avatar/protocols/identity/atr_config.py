# 模块说明：统一解析 ATR 文件配置；管理系统、个人本体及个人实体使用同一套显式路径规则。
import json
from pathlib import Path
from wit.acps.atr import AtrConfig, AtrEndpoints, AtrLocalPaths, RegistryServerCredentials


# 职责：从独占配置文件构造 wit 公开 ATR 配置对象。
# 输入：path 是服务器控制的 registration.json；凭据由调用方显式注入，默认不加载。
# 结果与约束：相对路径锚定配置目录；拒绝隐式业务 URL，不创建身份或读取环境变量。
def read_atr_config(path, username='', password=''):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('ATR 配置不能使用符号链接')
    source = json.loads(path.read_text())
    if source.get('use_saved_urls'):
        raise ValueError('ATR 必须使用独立配置，不能读取业务 URL')

    # 职责：将一个显式配置路径解析到当前身份目录。
    # 输入：value 为文件或目录的相对/绝对路径。
    # 结果与约束：只解析不创建；配置文件由服务器维护，不接收客户端路径。
    def local(value):
        value = Path(value)
        return value if value.is_absolute() else path.parent / value

    config = AtrConfig(
        endpoints=AtrEndpoints(registry_url=source['registry_url'],
                               certificate_authority_url=source['certificate_authority_url'],
                               registry_entity_mtls_url=source.get('registry_entity_mtls_url') or None,
                               ca_cert_path=local(source['ca_cert_path']) if source.get('ca_cert_path') else None),
        registry_credentials=RegistryServerCredentials(username=username, password=password),
        paths=AtrLocalPaths(acs_path=local(source['acs_path']), state_dir=local(source['state_dir']),
                            parent_state_dir=local(source['parent_state_dir']) if source.get('parent_state_dir') else None),
        mode=source.get('mode', 'integrated'))

    return config
