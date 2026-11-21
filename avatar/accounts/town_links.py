"""预核验的统一身份关联导入；不按用户名推测，不变更原 ACS 或业务归属。"""
from urllib.parse import urlsplit
from avatar.core.db import now


def import_links(database, entries, *, apply=False):
    if not isinstance(entries, list) or not entries:
        raise ValueError('需要非空身份映射列表')
    result = []
    seen = set()
    owners = {}
    with database.connect(write=apply) as db:
        for item in entries:
            required = {'old_issuer', 'old_sub', 'new_issuer', 'new_sub', 'evidence'}
            if not isinstance(item, dict) or set(item) - (required | {'username', 'town_principal_id'}) or not required <= set(item):
                raise ValueError('身份映射字段不完整或包含未知字段')
            if any(not isinstance(item[k], str) or not 1 <= len(item[k]) <= 2048 for k in required):
                raise ValueError('身份字段或核验依据无效')
            for key in ('old_issuer', 'new_issuer'):
                url = urlsplit(item[key])
                if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or item[key].endswith('/'):
                    raise ValueError('issuer 必须是无凭据的规范 HTTPS 地址')
            for key in ('username', 'town_principal_id'):
                if key in item and (not isinstance(item[key], str) or len(item[key]) > 160):
                    raise ValueError('可选身份字段无效')
            old = db.execute('SELECT o.user_id,u.is_root FROM oidc_identities o JOIN users u ON u.id=o.user_id WHERE o.issuer=? AND o.subject=?',
                             (item['old_issuer'], item['old_sub'])).fetchone()
            if not old or old['is_root']:
                raise ValueError('旧身份不存在或为根账号，拒绝关联')
            owner_key = (old['user_id'], item['new_issuer'])
            if owner_key in owners and owners[owner_key] != item['new_sub']:
                raise ValueError('同一账号在一个 issuer 下只能关联一个主体')
            owners[owner_key] = item['new_sub']
            key = (item['new_issuer'], item['new_sub'])
            if key in seen:
                raise ValueError('重复目标身份')
            seen.add(key)
            matches = db.execute('''SELECT user_id FROM oidc_identities WHERE issuer=? AND subject=?
              UNION SELECT user_id FROM oidc_identity_links WHERE issuer=? AND subject=?''', (*key, *key)).fetchall()
            bound = db.execute('SELECT subject FROM oidc_identity_links WHERE user_id=? AND issuer=?', (old['user_id'], item['new_issuer'])).fetchone()
            if any(r['user_id'] != old['user_id'] for r in matches) or bound and bound['subject'] != item['new_sub']:
                raise ValueError('新身份已关联其他账号或原账号已有不同关联；不合并业务数据')
            result.append({'user_id': old['user_id'], 'issuer': key[0], 'subject': key[1], 'action': 'existing' if matches else 'link'})
            if apply and not matches:
                db.execute('INSERT INTO oidc_identity_links(issuer,subject,user_id,username,town_principal_id,evidence,verified_at) VALUES(?,?,?,?,?,?,?)',
                           (*key, old['user_id'], item.get('username', ''), item.get('town_principal_id'), item['evidence'], now()))
    return result
