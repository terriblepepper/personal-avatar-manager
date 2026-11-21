"""Installed command; configuration is explicit environment, never a bundled .env."""
import argparse
import os
from importlib.metadata import version


def main():
    parser = argparse.ArgumentParser(description='个人分身智能体管理平台')
    parser.add_argument('--version', action='version', version=version('personal-avatar-manager'))
    parser.add_argument('--host', default=os.getenv('AVATAR_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.getenv('AVATAR_PORT', '18080')))
    parser.add_argument('--data-dir', help='持久化数据目录，不使用安装目录保存数据')
    options = parser.parse_args()
    if options.data_dir:
        os.environ['AVATAR_DATA_DIR'] = options.data_dir
    import uvicorn
    uvicorn.run('avatar.api.app:create_app', factory=True, host=options.host,
                port=options.port, workers=1, loop='asyncio',
                proxy_headers=False, access_log=False)
