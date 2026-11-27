"""按管理员页面读取统计、平台 URL 和分身操作列表。"""
import json

from fastapi import HTTPException

from avatar.core.platform_settings import PlatformSettings
from avatar.profiles.pending_reviews import pending_count
from avatar.protocols.registration.entity import staged_entity_metadata
from avatar.protocols.transport.endpoints import entity_port_pool, preview_entity_urls
from avatar.models.artifact_cache import fingerprint
from avatar.models.capabilities import apply_capabilities, target
from avatar.models.model_config import public_config


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


def agent_details(state, db, rows):
    """批量读取旧管理记录页展示的完整分身详情，保持单项详情的权限和字段。"""
    owners = [row['user_id'] for row in rows]
    if not owners:
        return []
    configs = {row['user_id']: json.loads(row['model_config']) for row in db.execute(
        'SELECT user_id,model_config FROM model_settings WHERE user_id=ANY(?::text[])', (owners,))}
    keys = [(owner,target(config),fingerprint(config.get('model',''))) for owner,config in configs.items() if config]
    evidence = {}
    if keys:
        evidence = {r['user_id']: json.loads(r['value']) for r in db.execute(
            """SELECT c.user_id,c.value FROM model_artifact_cache c
               JOIN unnest(?::text[],?::text[],?::text[]) k(user_id,signature,digest)
               USING(user_id,signature,digest) WHERE c.kind='capabilities'""",
            tuple(map(list, zip(*keys))))}
    attachments = state.service.attachments
    files = {}
    for row in db.execute('SELECT user_id,'+attachments.summary_fields+' FROM attachments '
                          'WHERE user_id=ANY(?::text[]) ORDER BY created_at DESC,id', (owners,)):
        item = attachments._summary(row, admin=True)
        files.setdefault(item.pop('user_id'), []).append(item)
    pool = entity_port_pool(state.settings, db=db) if state.settings.entity_aip_enabled else {}
    errors = {}
    urls = preview_entity_urls(state.settings, db, owners, pool, errors=errors)
    items = []
    for row in rows:
        uid = row['user_id']
        config = configs.get(uid)
        agent = state.service.decode_agent(row)
        agent.update(model_config=public_config(apply_capabilities(config,evidence.get(uid,{})) if config else {}),
                     attachments=files.get(uid,[]), protocol=state.personal_aip.status(uid,
                         endpoint_result=(urls.get(uid), errors.get(uid) or pool.get('error'))),
                     staged_identity=staged_entity_metadata(state.settings.data_dir,uid))
        items.append(agent)
    return items


def operations(state, search, offset):
    source = ' FROM avatar_records a JOIN users u ON u.id=a.user_id'
    params = []
    if search:
        source += " WHERE strpos(lower(u.username||' '||a.aic||' '||a.profile),lower(?))>0"
        params.append(search)
    with state.database.connect() as db:
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        total = db.execute('SELECT count(*)'+source, params).fetchone()[0]
        offset = min(offset, (max(total, 1)-1)//10*10)
        rows = db.execute(f"""WITH page AS (
          SELECT a.user_id, u.username, a.aic, a.identity_status, a.updated_at, a.last_modified_at,
            (a.profile::jsonb->>'avatar_name') AS avatar_name,
            (a.certificate::jsonb->>'id') AS certificate_id,
            (a.certificate::jsonb->>'expires_at') AS expires_at,
            (a.certificate::jsonb->>'mode') AS certificate_mode,
            (a.acs_document::jsonb->>'version') AS acs_version,
            (a.acs_document::jsonb->'entityMeta'->>'environment') AS environment
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
