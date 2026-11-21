"""个人分身使用服务器 IP 和稳定独占端口；用户名 DNS 映射保留供域名部署使用。"""
import re
from contextlib import closing, contextmanager
from urllib.parse import urlsplit
from avatar.core.db import Database
from avatar.protocols.registration.base import RegistrationFailure

LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')


# 职责：以无碰撞的字符映射将用户名转换为 DNS 标签。
# 合法小写标签直接使用；其余以 u- 开头，非小写字母/数字逐 UTF-8 字节映射为 -hh。
# u- 前缀保留，原用户名中的连字符也转义，避免 a@b 与 a-40b 等用户名撞名；超长明确拒绝。
def username_label(username):
    if not isinstance(username, str) or not username or username != username.strip():
        raise RegistrationFailure('请先绑定有效的 Keycloak 用户名')
    if LABEL.fullmatch(username) and not username.startswith('u-'):
        return username
    label = 'u-' + ''.join(chr(b) if 97 <= b <= 122 or 48 <= b <= 57 else f'-{b:02x}'
                           for b in username.encode('utf-8'))
    if not LABEL.fullmatch(label):
        raise RegistrationFailure('用户名映射后超过 DNS 标签的 63 字符限制，请缩短 Keycloak 用户名')
    return label


@contextmanager
def entity_connection(settings, db=None, *, write=False):
    if db is not None:
        yield db
    else:
        with closing(Database(settings.data_dir, settings.database_url)) as store, store.connect(write=write) as connection:
            yield connection


# 职责：取得指定内部账户已绑定的 Keycloak 身份，不使用本地用户名冒充 OIDC 用户。
# 仅信任配置 issuer 下的唯一绑定；根管理员无外部身份，不能注册为小镇个人分身。
def entity_oidc_identity(settings, user_id, *, db=None):
    with entity_connection(settings, db) as db:
        row = db.execute('SELECT o.issuer,o.subject,o.username FROM oidc_identities o JOIN users u ON u.id=o.user_id WHERE o.user_id=? AND o.issuer=? AND u.is_root=0',
                         (user_id, settings.oidc_issuer.rstrip('/'))).fetchone()
        if not row:
            row = db.execute('SELECT issuer,subject,username FROM oidc_identity_links WHERE user_id=? AND issuer=?',
                             (user_id, settings.oidc_issuer.rstrip('/'))).fetchone()
    if not row or not row['subject'] or not row['username']:
        raise RegistrationFailure('个人分身需要先绑定 Keycloak 账户并登录同步用户名')
    return {'keycloakUserId': row['username'], 'keycloakSubject': row['subject'], 'keycloakIssuer': row['issuer']}


# 验证服务器 IP 和端口段；不要求 DNS/hosts，不把客户端输入当作监听地址。
def entity_ingress_config(settings):
    import ipaddress
    start,end=settings.entity_aip_port,settings.entity_aip_port_end
    try:
        public=urlsplit(settings.system_aip_public_url)
        host=str(ipaddress.ip_address(public.hostname))
        web=urlsplit(settings.oidc_public_url)
        web_port=web.port or (443 if web.scheme=='https' else 80)
    except ValueError:
        raise RegistrationFailure('固定端口模式需要有效的服务器 IP 与 Web 地址') from None
    if not 1024<=start<=end<=65535 or any(start<=p<=end for p in (settings.system_aip_port,web_port)):
        raise RegistrationFailure('个人端口段无效或与 Web/管理入口冲突')
    return start,end,host


def entity_port_pool(settings, *, db=None):
    try:start,end,host=entity_ingress_config(settings)
    except RegistrationFailure as exc:return {'error':str(exc)}
    with entity_connection(settings, db) as db:
        allocated=db.execute('SELECT COUNT(*) FROM avatar_entity_ports').fetchone()[0]
        count=db.execute("SELECT COUNT(*) FROM avatar_records WHERE aic NOT LIKE 'DEMO-%' AND identity_status='active'").fetchone()[0]
    return {'mode':'ports','host':host,'start':start,'end':end,'allocated':allocated,'registered':count}



def available_port(db, start, end):
    used = {row['port'] for row in db.execute('SELECT port FROM avatar_entity_ports WHERE port BETWEEN ? AND ?', (start, end))}
    return next((port for port in range(start, end+1) if port not in used), None)


def entity_url(host, port):
    authority = '['+host+']' if ':' in host else host
    return f'https://{authority}:{port}/aip/rpc'


def preview_entity_urls(settings, db, user_ids, pool):
    """批量投影一页的固定或候选端点，不为预览分配端口。"""
    if not user_ids or not settings.entity_aip_enabled or 'error' in pool:
        return {}
    marks = ','.join('?' for _ in user_ids)
    rows = db.execute(f"""SELECT u.id,p.port,COALESCE(i.subject,l.subject) AS subject,
                           COALESCE(i.username,l.username) AS username FROM users u
      LEFT JOIN avatar_entity_ports p ON p.user_id=u.id
      LEFT JOIN oidc_identities i ON i.user_id=u.id AND i.issuer=? AND u.is_root=0
      LEFT JOIN oidc_identity_links l ON l.user_id=u.id AND l.issuer=?
      WHERE u.id IN ({marks})""", [settings.oidc_issuer.rstrip('/')]*2 + user_ids).fetchall()
    start, end = pool['start'], pool['end']
    candidate = None
    if any(r['port'] is None and r['subject'] and r['username'] for r in rows):
        candidate = available_port(db, start, end)
    urls = {}
    for row in rows:
        port = row['port']
        if port is None and row['subject'] and row['username']:
            port = candidate
        if port is not None and start <= port <= end:
            urls[row['id']] = entity_url(pool['host'], port)
    return urls


# 同一账户永久保留端口；事务和 UNIQUE 约束防止并发撞号，不回收其他账户的占位。
def entity_endpoint(settings,user_id,*,allocate=True,db=None,existing_only=False):
    """existing_only 只返回已预留的固定端口；普通预览仍可计算候选端口。"""
    if not settings.entity_aip_enabled:return None
    start,end,host=entity_ingress_config(settings)
    with entity_connection(settings, db, write=allocate and not existing_only) as db:
        row=db.execute('SELECT port FROM avatar_entity_ports WHERE user_id=?',(user_id,)).fetchone()
        if row:
            port=row['port']
            if not start<=port<=end:raise RegistrationFailure('已有固定端口不在当前范围，需显式迁移')
        elif existing_only:
            return None
        else:
            entity_oidc_identity(settings,user_id,db=db)
            port=available_port(db,start,end)
            if port is None:raise RegistrationFailure('个人分身固定端口段已用尽')
            if allocate:db.execute('INSERT INTO avatar_entity_ports(user_id,port) VALUES(?,?)',(user_id,port))
    return {'url':entity_url(host,port),'transport':'JSONRPC','security':[{'mtls':[]}]}


def registered_oidc_matches(settings, user_id, metadata, *, db=None):
    """已签发 ACS 保留原绑定；显式链接新 issuer 不改证书或已审核 ACS。"""
    with entity_connection(settings, db) as db:
        rows = db.execute('''SELECT user_id FROM oidc_identities WHERE issuer=? AND subject=?
          UNION SELECT user_id FROM oidc_identity_links WHERE issuer=? AND subject=?''',
          (metadata.get('keycloakIssuer'), metadata.get('keycloakSubject')) * 2).fetchall()
        return len(rows) == 1 and rows[0]['user_id'] == user_id
