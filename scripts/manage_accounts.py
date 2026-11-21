# 模块说明：服务器本机创建额外管理员：交互输入密码、哈希保存、生成资料草稿并留审计，不提升或覆盖已有账户。
"""可信本机账户运维，交互创建管理员或绑定已核验的 OIDC 身份。"""
import argparse
import getpass
from psycopg import IntegrityError
import sys
import uuid
from urllib.parse import urlsplit
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from avatar.accounts.auth import hash_password
from avatar.core.config import Settings
from avatar.core.db import Database, event, now, dump
from avatar.core.schemas import Credentials
from pydantic import ValidationError


def create_admin(database, credentials):
    user_id = str(uuid.uuid4())
    with database.connect(write=True) as db:
        db.execute("INSERT INTO users(id,username,password,role,created_at) VALUES(?,?,?,?,?)",
                   (user_id, credentials.username, hash_password(credentials.password), "admin", now()))
        event(db, user_id, "local-operator", "创建管理员账户", credentials.username)
        modified=now()
        db.execute('INSERT INTO profile_records(user_id,draft,last_modified_at,updated_at) VALUES(?,?,?,?)', (user_id,dump({'person_type':'student','student_id':''}),modified,modified))
    return user_id


def bind_account(database, username, issuer, subject):
    """由可信运维提供已核验的 Keycloak User ID，仅建立关联，不更改密码、资料或手机号。"""
    parsed = urlsplit(issuer)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or not subject or len(subject) > 255:
        raise ValueError('issuer 或 Keycloak User ID 无效')
    with database.connect(write=True) as db:
        user = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        if not user:
            raise ValueError('本地账号不存在')
        if user['is_root']:
            raise ValueError('根管理员保留本地登录，禁止关联 Keycloak')
        existing = db.execute('SELECT * FROM oidc_identities WHERE user_id=?', (user['id'],)).fetchone()
        if existing:
            if (existing['issuer'], existing['subject']) == (issuer, subject):
                return
            raise ValueError('本地账号已有其他统一身份，拒绝覆盖')
        db.execute('INSERT INTO oidc_identities(issuer,subject,user_id) VALUES(?,?,?)', (issuer, subject, user['id']))
        db.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
        event(db, user['id'], 'local-operator', '核验后关联统一身份')


def main():
    parser = argparse.ArgumentParser(description="服务器本机账户运维")
    actions = parser.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create-admin", help="交互创建管理员")
    create.add_argument("username", help="3–40 位字母、数字、点、横线或下划线")
    bind = actions.add_parser("bind-oidc", help="关联已核验的 Keycloak 身份")
    bind.add_argument("username")
    bind.add_argument("subject", help="Keycloak 用户详情中的稳定 User ID，不能填写手机号")
    bind.add_argument("--issuer", help="默认使用 AVATAR_OIDC_ISSUER")
    for command in (create, bind):
        command.add_argument("--data-dir", type=Path, help="业务文件目录；数据库使用 DATABASE_URL")
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.action == "create-admin":
        password = getpass.getpass("管理员密码（8–128 位，不回显）: ")
        if password != getpass.getpass("再次输入密码: "):
            parser.error("两次输入的密码不一致")
        try:
            credentials = Credentials(username=args.username, password=password)
        except ValidationError:
            parser.error("用户名或密码不符合要求；用户名 3–40 位指定字符，密码 8–128 位")
    try:
        with Database.open_existing((args.data_dir or settings.data_dir).resolve(), settings.database_url) as database:
            if args.action == "create-admin":
                create_admin(database, credentials)
            else:
                bind_account(database, args.username, (args.issuer or settings.oidc_issuer).rstrip("/"), args.subject)
    except ValueError as exc:
        parser.error(str(exc))
    except IntegrityError:
        parser.error("用户名已存在，请选择新用户名；此命令不会提升或覆盖已有账户" if args.action == "create-admin"
                     else "统一身份已被关联，拒绝合并或覆盖")
    if args.action == "create-admin":
        print(f"管理员 {args.username} 已创建。OIDC 关闭时可本地登录；启用后须核验并关联 Keycloak 身份。")
    else:
        print("关联完成，后续使用已关联的 Keycloak 账号登录；原业务数据保留。")


if __name__ == "__main__":
    main()
