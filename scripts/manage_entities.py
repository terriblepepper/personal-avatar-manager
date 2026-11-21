# 模块说明：个人派生身份维护队列入口：默认仅排队，停服后可 --execute 离线处理；plan-sni 只读输出迁移规划。
"""个人派生身份运维入口；读取 .env 后运行，复用管理员持久队列。"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from avatar.core.config import Settings
from avatar.core.db import Database
from avatar.profiles.entity_lifecycle import EntityLifecycleService


async def execute(args):
    settings = Settings.from_env()
    with Database.open_existing(settings.data_dir, settings.database_url) as database:
        await manage(args, settings, database)


async def manage(args, settings, database):
    if args.action in ('plan','plan-sni'):
        if args.execute:
            raise ValueError('plan-sni 只读，不允许 --execute')
        from avatar.protocols.transport.endpoints import entity_endpoint, entity_oidc_identity
        from avatar.protocols.registration.entity import staged_entity_metadata
        with database.connect() as db:
            rows = db.execute('SELECT user_id,aic FROM avatar_records ORDER BY user_id').fetchall()
        for row in rows:
            if args.user_id and row['user_id'] != args.user_id:
                continue
            if args.all_demo and not row['aic'].startswith('DEMO-'):
                continue
            endpoint = entity_endpoint(settings,row['user_id'],allocate=False)
            staged = staged_entity_metadata(settings.data_dir, row['user_id']) or {}
            print(json.dumps({'user_id': row['user_id'], 'current_aic': row['aic'],
                              'staged_aic': staged.get('aic'), 'endpoint': endpoint,
                              'entityMeta': entity_oidc_identity(settings, row['user_id']),
                              'notice': '只读规划；未分配端口仅展示当前可用候选，执行注册时在事务内固定端口'}, ensure_ascii=False))
        return
    service = EntityLifecycleService(database, settings)
    with database.connect() as db:
        admin = db.execute('SELECT * FROM users WHERE is_root=1').fetchone()
        if not admin:
            raise ValueError('根管理员尚未初始化')
        if args.all_demo:
            if args.action != 'register':
                raise ValueError('--all-demo 仅用于首次 Demo 迁移')
            users = [r['user_id'] for r in db.execute("SELECT user_id FROM avatar_records WHERE aic LIKE 'DEMO-%' AND identity_status='active' ORDER BY created_at")]
        elif args.user_id:
            users = [args.user_id]
        else:
            raise ValueError('请指定 --user-id 或 register --all-demo')
        if db.execute("SELECT 1 FROM avatar_identity_jobs WHERE status='running'").fetchone():
            raise ValueError('有维护任务正在执行，请等待后台完成，不要并行启动运维程序')
    for user_id in users:
        service.enqueue(dict(admin), user_id, args.action)
    # 应停止应用的维护队列再使用 --execute；生产正常运行时仅排队交给现有服务处理。
    if args.execute:
        while await service.once():
            pass
    with database.connect() as db:
        for user_id in users:
            job = db.execute('SELECT action,status,error FROM avatar_identity_jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 1', (user_id,)).fetchone()
            print(json.dumps({'user_id': user_id, **dict(job)}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description='个人正式身份维护；不输出私钥、Token 或账号密码')
    parser.add_argument('action', choices=('register', 'renew', 'unregister', 'plan','plan-sni'))
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument('--user-id')
    scope.add_argument('--all-demo', action='store_true')
    parser.add_argument('--execute', action='store_true', help='应用已停止时执行队列；默认只排队')
    args = parser.parse_args()
    try:
        asyncio.run(execute(args))
    except Exception as exc:
        print(f'操作未完成（{type(exc).__name__}），请检查配置与维护队列；保留原 ATR 状态。', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
