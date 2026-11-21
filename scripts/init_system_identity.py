# 模块说明：离线创建系统/个人本体唯一 ACS、注册配置及私有目录；保留已有身份，不发起注册和签证。
"""初始化管理系统及个人分身本体的独立 ACS/证书目录；不进行真实注册，不处理用户实体。"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from avatar.core.config import Settings
from avatar.protocols.identity.system import initialize_system_identity
from avatar.protocols.identity.ontology import initialize_avatar_ontology


def main():
    parser = argparse.ArgumentParser(description='生成管理系统 ACS 与未来证书目录；已有身份文件不会被覆盖')
    parser.add_argument('--data-dir', type=Path, default=Settings.from_env().data_dir)
    parser.add_argument('--base-url')
    parser.add_argument('--registry-url')
    parser.add_argument('--ca-url')
    parser.add_argument('--aip-url')
    parser.add_argument('--mtls-url', help='默认使用 WIT_REGISTRY_MTLS_URL')
    parser.add_argument('--kind', choices=('management', 'personal', 'all'), default='management', help='选择管理系统或个人本体，默认只初始化管理系统')
    args = parser.parse_args()
    try:
        if args.kind in ('management', 'all'):
            paths = initialize_system_identity(args.data_dir, args.base_url, args.registry_url, args.ca_url, args.aip_url, args.mtls_url)
            print('管理系统 ACS：', paths.acs.resolve())
        if args.kind in ('personal', 'all'):
            paths = initialize_avatar_ontology(args.data_dir, args.base_url, args.registry_url, args.ca_url, args.mtls_url)
            print('个人分身本体 ACS：', paths.acs.resolve())
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print('注册配置：', paths.registration.resolve())
    print('证书及注册状态目录：', paths.certificates.resolve())
    print('CA 信任材料预留路径：', paths.ca_bundle.resolve())
    print('已有文件保留；未发起注册或签发证书。')


if __name__ == '__main__':
    main()
