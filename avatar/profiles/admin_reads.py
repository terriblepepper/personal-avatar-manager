"""按管理员页面读取统计、平台 URL 和分身操作列表。"""
import json

from fastapi import HTTPException

from avatar.core.schemas import PlatformSettings
from avatar.profiles.pending_reviews import pending_count
from avatar.protocols.registration.entity import staged_entity_metadata
from avatar.protocols.transport.endpoints import entity_port_pool, preview_entity_urls


def counts(db):
    row = db.execute("""SELECT (SELECT count(*) FROM users) AS users,
                       (SELECT count(*) FROM avatar_records) AS agents,
                       (SELECT count(*) FROM provision_jobs WHERE status='failed') AS failed""").fetchone()
    return {**dict(row), 'pending': pending_count(db)}


def settings_view(state):
    keys = tuple(PlatformSettings.model_fields)
    with state.database.connect() as db:
        rows = db.execute(f"SELECT key,value FROM settings WHERE key IN ({','.join('?' for _ in keys)})", keys)
        values = {row['key']: json.loads(row['value']) for row in rows}
    return {'settings': values, 'environment': state.environment, 'mode': getattr(state.service.provider, 'mode', 'demo')}


def agent_detail(state, row):
    if row is None:
        raise HTTPException(404, '分身不存在')
    agent = state.service._agent(row, admin=True)
    agent['protocol'] = state.personal_aip.status(row['user_id'])
    agent['staged_identity'] = staged_entity_metadata(state.settings.data_dir, row['user_id'])
    return agent


def operations(state, search, offset):
    source = ' FROM avatar_records a JOIN users u ON u.id=a.user_id'
    params = []
    if search:
        source += " WHERE instr(lower(u.username||' '||a.aic||' '||a.profile),lower(?))>0"
        params.append(search)
    with state.database.connect() as db:
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        total = db.execute('SELECT count(*)'+source, params).fetchone()[0]
        offset = min(offset, (max(total, 1)-1)//10*10)
        rows = db.execute(f"""WITH page AS (
          SELECT a.user_id, u.username, a.aic, a.identity_status, a.updated_at, a.last_modified_at,
            json_extract(a.profile,'$.avatar_name') AS avatar_name,
            json_extract(a.certificate,'$.id') AS certificate_id,
            json_extract(a.certificate,'$.expires_at') AS expires_at,
            json_extract(a.certificate,'$.mode') AS certificate_mode,
            json_extract(a.acs_document,'$.version') AS acs_version,
            json_extract(a.acs_document,'$.entityMeta.environment') AS environment
          {source} ORDER BY a.updated_at DESC,a.user_id LIMIT 10 OFFSET ?
        ), jobs AS (
          SELECT j.user_id,j.id,j.action,j.status,j.error,
            row_number() OVER (PARTITION BY j.user_id ORDER BY j.created_at DESC,j.id DESC) AS rank
          FROM page p JOIN avatar_identity_jobs j ON j.user_id=p.user_id
          WHERE ((j.expected_aic='' AND j.remote_aic='') OR j.expected_aic=p.aic OR j.remote_aic=p.aic)
            AND NOT (j.action='unregister' AND j.status='done' AND p.identity_status='active')
        )
        SELECT p.*,j.id AS job_id,j.action,j.status,j.error FROM page p
          LEFT JOIN jobs j ON j.user_id=p.user_id AND j.rank=1
          ORDER BY p.updated_at DESC,p.user_id""", [*params,offset]).fetchall()
        pool = entity_port_pool(state.settings, db=db)
        endpoints = preview_entity_urls(state.settings, db, [row['user_id'] for row in rows], pool)
    runtime = state.personal_aip
    pool['online'] = runtime.online_count()
    items = []
    for row in rows:
        uid = row['user_id']
        job = ({'id': row['job_id'], 'action': row['action'], 'status': row['status'], 'error': row['error']}
               if row['job_id'] else None)
        items.append({'user_id': uid, 'username': row['username'], 'aic': row['aic'],
                      'identity_status': row['identity_status'], 'last_modified_at': row['last_modified_at'],
                      'profile': {'avatar_name': row['avatar_name']},
                      'certificate': {'id': row['certificate_id'], 'expires_at': row['expires_at'], 'mode': row['certificate_mode']},
                      'acs_document': {'version': row['acs_version'], 'entityMeta': {'environment': row['environment']}},
                      'protocol': runtime.operation_protocol(uid, endpoints.get(uid)),
                      'staged_identity': staged_entity_metadata(state.settings.data_dir, uid), 'identity_job': job})
    return {'items': items, 'offset': offset, 'total': total, 'limit': 10, 'entity_port_pool': pool,
            'entity_registration_enabled': state.settings.entity_aip_enabled, 'personal_registration_mode': state.settings.provider,
            'mode': getattr(state.service.provider, 'mode', 'demo')}
