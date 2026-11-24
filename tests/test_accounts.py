"""账户、审核、通知与分身生命周期。"""
import asyncio
import hashlib
import json
import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from avatar.accounts.auth import issue_session
from avatar.accounts.management import AccountManagement
from avatar.core.db import now
from avatar.core.schemas import Credentials
from avatar.profiles.entity_lifecycle import EntityLifecycleService
from avatar.protocols.registration.base import RegistrationPending
from avatar.protocols.registration.entity import entity_identity_paths
from avatar.runtime.worker import ProvisionWorker
from scripts.manage_accounts import create_admin
from tests.support import MODEL, PROFILE, approve, login, modified_at, submit


# 账户密码、联系方式、角色与管理权限

def register_auth_user(client, **values):
    return client.post('/api/auth/register', json={'username': 'newstudent', 'password': 'password123', 'student_id': '20269999', 'email': 'new@example.com', **values})


def test_registration_contacts_normalize_unique_and_optional_phone(context):
    app, user, admin = context
    new = TestClient(app)
    assert register_auth_user(new, email='invalid').status_code == 422
    assert register_auth_user(new, email='   ').status_code == 422
    assert register_auth_user(new, phone='invalid').status_code == 422
    assert register_auth_user(new, email='TESTER@example.com').status_code == 409
    assert register_auth_user(new, phone='+86 138-0000-0001').status_code == 409
    assert register_auth_user(new, email=' NEW@example.com ').status_code == 201
    login(new, 'new@example.com', 'password123')
    me = new.get('/api/auth/me').json()['user']
    assert me['email'] == 'new@example.com' and me['phone'] is None

@pytest.mark.parametrize('identifier', ['tester', ' TESTER@EXAMPLE.COM ', '13800000001', '+86 138-0000-0001'])
def test_login_aliases_and_shared_throttle(context, identifier):
    app, user, admin = context
    client = TestClient(app)
    login(client, identifier, 'testpassword123')
    assert client.get('/api/auth/me').json()['user']['username'] == 'tester'
    for n in range(10):
        r = client.post('/api/auth/login', json={'username': 'tester' if n % 2 else 'tester@example.com', 'password': 'wrongpass'})
        assert r.status_code == 401
    assert client.post('/api/auth/login', json={'username': '13800000001', 'password': 'testpassword123'}).status_code == 429

def test_password_change_checks_current_csrf_invalidates_all_sessions(context):
    app, user, admin = context
    second = TestClient(app)
    login(second, 'tester', 'testpassword123')
    body = {'current_password': 'wrongpass', 'new_password': 'updated-pass-123'}
    assert user.post('/api/auth/change-password', json=body).status_code == 400
    assert second.get('/api/me').status_code == 200
    assert user.post('/api/auth/change-password', json={**body, 'current_password': 'testpassword123', 'new_password': 'testpassword123'}).status_code == 422
    token = user.headers.pop('X-CSRF-Token')
    assert user.post('/api/auth/change-password', json=body).status_code == 403
    user.headers['X-CSRF-Token'] = token
    assert user.post('/api/auth/change-password', json={**body, 'current_password': 'testpassword123'}).status_code == 200
    assert user.get('/api/me').status_code == 401
    assert second.get('/api/me').status_code == 401
    assert user.post('/api/auth/login', json={'username': 'tester', 'password': 'testpassword123'}).status_code == 401
    login(user, 'tester@example.com', 'updated-pass-123')

@pytest.mark.parametrize('contact', [' TESTER@EXAMPLE.COM ', '+86 13800000001'])
def test_simple_contact_recovery_invalidates_sessions(context, contact):
    app, user, admin = context
    anonymous = TestClient(app)
    body = {'contact': contact, 'new_password': 'recovered-pass-123'}
    response = anonymous.post('/api/auth/recover-password', json=body)
    assert response.status_code == 200, response.text
    assert user.get('/api/me').status_code == 401
    assert 'recovered-pass' not in response.text
    login(user, 'tester', 'recovered-pass-123')
    assert anonymous.post('/api/auth/recover-password', json={**body, 'contact': 'missing@example.com'}).json() == response.json()
    assert anonymous.post('/api/auth/recover-password', json={**body, 'contact': 'tester'}).status_code == 422
    login(user, 'tester', 'recovered-pass-123')
    for n in range(3):
        assert anonymous.post('/api/auth/recover-password', json={**body, 'contact': 'unknown@example.com'}).status_code == 200
    assert anonymous.post('/api/auth/recover-password', json=body).status_code == 429

def test_controlled_contacts_change_preserves_profile_and_legacy_admin(context):
    app, user, admin = context
    body = {'email': 'changed@example.com', 'phone': '13900000001', 'current_password': 'testpassword123'}
    assert user.put('/api/auth/contacts', json={**body, 'current_password': 'wrongpass'}).status_code == 400
    assert user.put('/api/auth/contacts', json=body).status_code == 200
    assert user.get('/api/me').json()['profile']['draft']['email'] == 'tester@example.com'
    login(TestClient(app), 'changed@example.com', 'testpassword123')
    login(TestClient(app), '13900000001', 'testpassword123')
    password = (app.state.settings.data_dir / 'initial-admin.txt').read_text().split('password: ')[1].strip()
    assert admin.put('/api/auth/contacts', json={**body, 'current_password': password}).status_code == 403
    assert admin.put('/api/auth/contacts', json={'email': 'admin@example.com', 'current_password': password}).status_code == 200
    login(TestClient(app), 'admin@example.com', password)

def test_numeric_username_contact_namespace_conflict(context):
    app, user, admin = context
    new = TestClient(app)
    assert register_auth_user(new, username='13800000001').status_code == 409
    assert register_auth_user(new, username='13900000002').status_code == 201
    assert register_auth_user(new, username='otherstudent', student_id='20260077', email='other77@example.com', phone='13900000002').status_code == 409

def identity(client):
    return client.get('/api/auth/me').json()['user']

def role(client, uid, value):
    return client.put('/api/admin/accounts/' + uid + '/role', json={'role': value})

def register(app, name, teacher=False):
    client = TestClient(app)
    data = {'username': name, 'password': 'testpassword123', 'email': name + '@example.com', 'person_type': 'teacher' if teacher else 'student', 'name': name}
    if not teacher:
        data['student_id'] = 'ROLE-' + name
    assert client.post('/api/auth/register', json=data).status_code == 201
    login(client, name, 'testpassword123')
    return client

def test_root_promotes_and_promoted_admin_delegates(context):
    app, user, root = context
    third = register(app, 'third')
    uid = identity(user)['id']
    tid = identity(third)['id']
    assert identity(root)['is_root'] == 1
    assert role(user, tid, 'admin').status_code == 403
    assert role(root, uid, 'admin').status_code == 200
    assert identity(user)['role'] == 'admin' and (not identity(user)['is_root'])
    assert user.get('/api/admin/overview').status_code == 200
    assert role(user, tid, 'admin').status_code == 200
    assert identity(third)['role'] == 'admin'
    accounts = root.get('/api/admin/accounts').json()['items']
    assert len(accounts) == 3 and sum((u['is_root'] for u in accounts)) == 1
    assert not any(('password' in u or 'model_config' in u for u in accounts))

def test_only_self_demotion_and_root_protection(context):
    app, user, root = context
    third = register(app, 'third')
    uid, rid, tid = (identity(user)['id'], identity(root)['id'], identity(third)['id'])
    assert role(root, uid, 'admin').status_code == 200
    assert role(user, tid, 'admin').status_code == 200
    assert role(root, uid, 'user').status_code == 403
    assert role(user, tid, 'user').status_code == 403
    assert role(user, rid, 'user').status_code == 403
    assert role(root, rid, 'user').status_code == 403
    assert role(user, uid, 'user').status_code == 200
    assert identity(user)['role'] == 'user'
    assert user.get('/api/admin/accounts').status_code == 403
    assert role(user, tid, 'admin').status_code == 403
    assert identity(third)['role'] == 'admin'

def test_demotion_immediate_for_all_sessions_preserves_avatar(context):
    app, user, root = context
    approve(root, submit(user))
    asyncio.run(app.state.worker.once())
    session = user.post('/api/conversations', json={'channel': 'avatar'}).json()
    body = {'channel': 'avatar', 'conversation_id': session['id'], 'request_id': 'before-demotion', 'message': '你好'}
    uid = identity(user)['id']
    with app.state.database.connect(write=True) as db:
        db.execute('DELETE FROM embedding_settings WHERE user_id=?', (uid,))
    assert user.post('/api/chat/generations', json=body).status_code == 202

    async def answer():
        runner = app.state.generations
        jobs = await runner.claim(1)
        assert len(jobs) == 1
        await runner.execute(jobs[0])

    asyncio.run(answer())
    assert user.get('/api/chat/generations/before-demotion').json()['state'] == 'succeeded'
    before = user.get('/api/me').json()
    second = TestClient(app)
    login(second, 'tester', 'testpassword123')
    role(root, uid, 'admin')
    stale_actor = identity(second)
    assert role(user, uid, 'user').status_code == 200
    assert second.get('/api/admin/overview').status_code == 403
    assert identity(second)['role'] == 'user' and second.get('/api/me').status_code == 200
    after = user.get('/api/me').json()
    assert before['agent'] == after['agent'] and before['profile'] == after['profile']
    history = user.get('/api/chat', params={'channel':'avatar','conversation_id':session['id']})
    assert history.status_code == 200
    assert len(history.json()) == 2
    with pytest.raises(HTTPException) as exc:
        AccountManagement(app.state.database).set_role(stale_actor, identity(root)['id'], 'admin')
    assert exc.value.status_code == 403
    with app.state.database.connect() as db:
        actions = [r[0] for r in db.execute('SELECT action FROM audit_events WHERE user_id=?', (uid,))]
    assert '授予管理员权限' in actions and '管理员自主降级' in actions

def test_teacher_can_gain_admin_without_review(context):
    app, _, root = context
    teacher = register(app, 'teacher_role', teacher=True)
    uid = identity(teacher)['id']
    assert role(root, uid, 'admin').status_code == 200
    assert identity(teacher)['person_type'] == 'teacher'
    assert any((u['id'] == uid for u in root.get('/api/admin/accounts').json()['items']))

def test_role_changes_csrf_schema_and_account_search(context):
    _, user, root = context
    uid = identity(user)['id']
    csrf = root.headers.pop('X-CSRF-Token')
    assert role(root, uid, 'admin').status_code == 403
    root.headers['X-CSRF-Token'] = csrf
    assert role(root, uid, 'root').status_code == 422
    assert root.put('/api/admin/accounts/' + uid + '/role', json={'role': 'admin', 'is_root': True}).status_code == 422
    assert role(root, 'missing', 'admin').status_code == 404
    result = root.get('/api/admin/accounts', params={'search': 'tester@example.com', 'role': 'user'}).json()
    assert result['total'] == 1 and result['items'][0]['id'] == uid
    assert root.get('/api/admin/accounts', params={'search': '%'}).json()['total'] == 0
    assert root.get('/api/admin/accounts', params={'offset': -1}).status_code == 422

def test_root_and_cli_admin_can_manage_personal_profiles(context):
    app, _, root = context
    assert root.put('/api/me/model-settings', json={'model_config_input': MODEL}).status_code == 200
    body = {'profile': {**PROFILE, 'student_id': 'ROOT-PERSONAL'}, 'last_modified_at': modified_at(root)}
    assert root.put('/api/me/profile', json=body).status_code == 200
    with app.state.database.connect() as db:
        original_root = db.execute('SELECT id FROM users WHERE is_root=1').fetchone()[0]
    uid = create_admin(app.state.database, Credentials(username='local_admin', password='testpassword123'))
    local = TestClient(app)
    login(local, 'local_admin', 'testpassword123')
    body['profile']['student_id'] = 'CLI-PERSONAL'
    body['last_modified_at'] = modified_at(local)
    assert local.put('/api/me/profile', json=body).status_code == 200
    assert not identity(local)['is_root']
    assert role(local, uid, 'user').status_code == 200
    app.state.database.initialize()
    with app.state.database.connect() as db:
        assert db.execute('SELECT id FROM users WHERE is_root=1').fetchone()[0] == original_root

def test_public_registration_cannot_set_admin_or_root(context):
    app, _, root = context
    client = TestClient(app)
    data = {'username': 'attempt_role', 'password': 'testpassword123', 'email': 'attempt@example.com', 'student_id': 'ROLE-ATTEMPT', 'role': 'admin', 'is_root': True}
    assert client.post('/api/auth/register', json=data).status_code == 201
    login(client, 'attempt_role', 'testpassword123')
    assert identity(client)['role'] == 'user' and (not identity(client)['is_root'])

def test_admin_views_approved_profile_not_new_draft_or_model_credentials(context):
    app, user, root = context
    uid = identity(user)['id']
    path = '/api/admin/accounts/' + uid + '/profile'
    assert user.get(path).status_code == 403
    assert root.get('/api/admin/accounts/missing/profile').status_code == 404
    assert root.get(path).json()['profile'] == {}
    approve(root, submit(user))
    assert asyncio.run(app.state.worker.once())
    saved = user.put('/api/me/profile', json={'last_modified_at': modified_at(user), 'profile': {**PROFILE, 'bio': 'UNREVIEWED_DRAFT_ONLY'}})
    assert saved.status_code == 200, saved.text
    record = root.get(path)
    assert record.status_code == 200
    assert record.json()['profile']['bio'] == PROFILE['bio'] and record.json()['account']['id'] == uid
    assert 'UNREVIEWED_DRAFT_ONLY' not in record.text and MODEL['api_key'] not in record.text
    assert not any((name in record.json() for name in ('password', 'model_config', 'draft')))
    teacher = register(app, 'new_teacher', teacher=True)
    teacher_record = root.get('/api/admin/accounts/' + identity(teacher)['id'] + '/profile')
    assert teacher_record.status_code == 200
    assert teacher_record.json()['account']['person_type'] == 'teacher'
    assert teacher_record.json()['profile'] == {} and teacher_record.json()['approved_at'] is None
    role(root, uid, 'admin')
    stale = identity(user)
    assert role(user, uid, 'user').status_code == 200
    with pytest.raises(HTTPException) as error:
        AccountManagement(app.state.database).profile(stale, uid)
    assert error.value.status_code == 403


def test_previously_installed_skills_are_read_only_and_hidden_from_chat(context, previously_installed_skill):
    import json
    from types import SimpleNamespace

    app, user, _ = context
    uid = identity(user)['id']
    skill = previously_installed_skill
    cid = user.post('/api/conversations', json={'channel': 'assistant'}).json()['id']
    with app.state.database.connect(write=True) as db:
        db.execute('INSERT INTO chat_session_skills(conversation_id,skill_id,authorized) VALUES(?,?,1)', (cid, skill))

    assert [row['id'] for row in user.get('/api/me/skills').json()] == [skill]
    assert user.post('/api/me/skills', files={'file': ('SKILL.md', b'New skill')}).status_code == 403
    assert user.patch(f'/api/me/skills/{skill}', json={'enabled': False}).status_code == 403
    assert user.get(f'/api/conversations/{cid}/skills').json() == []
    assert user.patch(f'/api/conversations/{cid}/skills/{skill}', json={'enabled': True}).status_code == 403

    background = {'available_skills': [{'id': skill, 'name': 'archived-skill'}],
                  'installed_skills': [{'id': skill, 'instructions': 'Old instructions.'}],
                  '_session_skill_ids': [skill]}

    async def forbidden(*args, **kwargs):
        pytest.fail('Paused skills must not trigger a model request')

    skills = app.state.chat.skills
    result = asyncio.run(skills.load_context(
        uid, ({}, 'key', 'system\n' + json.dumps(background), []),
        '$archived-skill', SimpleNamespace(generate=forbidden), uid,
    ))
    assert result[0]['_used_skills'] == []
    assert not background.keys() & json.loads(result[2].split('\n', 1)[1]).keys()


def test_fixture_password_cache_keeps_real_hash_and_fresh_salts():
    import hashlib
    from avatar.accounts.auth import hash_password
    first,second=hash_password('test-only-password'),hash_password('test-only-password')
    assert first!=second
    salt,digest=first.split('$')
    assert digest==hashlib.pbkdf2_hmac('sha256',b'test-only-password',bytes.fromhex(salt),310000).hex()
    assert hash_password('test-only-password',salt)==first


def test_identity_record_batch_refuses_active_jobs_atomically(context):
    app, user, admin = context
    uid = identity(user)['id']
    with app.state.database.connect(write=True) as db:
        db.executemany(
            "INSERT INTO avatar_identity_jobs(id,user_id,actor,action,expected_aic,status,created_at,updated_at) VALUES(?,?,'admin','renew','aic',?,'now','now')",
            [(status, uid, status) for status in ('failed', 'waiting')],
        )
    endpoint = '/api/records/identity_jobs/delete'
    assert admin.post(endpoint, json={'ids': ['failed', 'waiting']}).status_code == 404
    with app.state.database.connect() as db:
        assert not db.execute("SELECT 1 FROM record_dismissals WHERE collection='identity_jobs'").fetchone()
    assert admin.post(endpoint, json={'ids': ['failed']}).json() == {'deleted': 1}


def test_profile_scope_applies_to_every_overview_response(context):
    from avatar.accounts.auth import current_user
    app, applicant, admin = context
    submit(applicant)
    applicant_id = identity(applicant)['id']
    actor = identity(admin)
    assert any(e['user_id'] == applicant_id for e in admin.get('/api/me').json()['events'])

    app.dependency_overrides[current_user] = lambda: {**actor, 'role': 'user', 'access_scope': 'profile'}
    try:
        initial = admin.get('/api/me')
        saved = admin.put('/api/me/profile', json={
            'profile': {**PROFILE, 'student_id': 'ROOT-PERSONAL'},
            'last_modified_at': initial.json()['profile']['last_modified_at'],
        })
        assert saved.status_code == 200
        submitted = admin.post('/api/me/submit', json={'last_modified_at': saved.json()['profile']['last_modified_at']})
        for response in (initial, saved, submitted):
            assert response.status_code == 200
            assert all(event['user_id'] == actor['id'] for event in response.json()['events'])
    finally:
        del app.dependency_overrides[current_user]


def test_demo_overview_reuses_files_and_reads_without_write_lock(context, monkeypatch):
    from unittest.mock import Mock
    app, user, _ = context
    service = app.state.service
    inventory = Mock(wraps=service.attachments.inventory)
    connect = Mock(wraps=service.database.connect)
    monkeypatch.setattr(service.attachments, 'inventory', inventory)
    monkeypatch.setattr(service.database, 'connect', connect)
    response = user.get('/api/me')
    assert response.status_code == 200
    assert response.json()['files'] == response.json()['profile']['files']
    assert inventory.call_count == 1
    assert all(not call.kwargs.get('write') for call in connect.call_args_list)


def test_direct_overview_reserves_once_then_reads_existing_port(context, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import Mock
    app, user, _ = context
    service = app.state.service
    uid = identity(user)['id']
    settings = replace(app.state.settings, entity_aip_enabled=True)
    monkeypatch.setattr(service, 'provider', SimpleNamespace(mode='acps-direct', runner=SimpleNamespace(settings=settings)))
    with service.database.connect(write=True) as db:
        db.execute('INSERT INTO oidc_identities(issuer,subject,username,user_id) VALUES(?,?,?,?)',
                   (settings.oidc_issuer, 'overview-test', 'tester', uid))
    connect = Mock(wraps=service.database.connect)
    monkeypatch.setattr(service.database, 'connect', connect)
    first = user.get('/api/me').json()['profile']['acs_document']['endPoints']
    assert len(first) == 1
    assert sum(bool(call.kwargs.get('write')) for call in connect.call_args_list) == 1
    with service.database.connect() as db:
        assert db.execute('SELECT port FROM avatar_entity_ports WHERE user_id=?', (uid,)).fetchone()[0] == settings.entity_aip_port
    connect.reset_mock()
    assert user.get('/api/me').json()['profile']['acs_document']['endPoints'] == first
    assert not any(call.kwargs.get('write') for call in connect.call_args_list)


def test_onboarding_progress_belongs_to_account_and_only_moves_forward(context):
    app, user, admin = context
    def stage(client):
        return client.get('/api/auth/me').json()['user']['onboarding_stage']
    assert stage(admin) == 'done'
    assert stage(user) == 'welcome'
    second = TestClient(app)
    login(second, 'tester', 'testpassword123')
    assert user.post('/api/me/onboarding/tour').status_code == 200
    assert stage(second) == 'tour'
    assert second.post('/api/me/onboarding/done').status_code == 200
    assert user.post('/api/me/onboarding/tour').status_code == 200
    assert stage(user) == 'done'


def test_oidc_new_account_keeps_its_own_onboarding_progress(context):
    import time
    from avatar.accounts.oidc import OidcService
    app, user, _ = context
    service = OidcService(app.state.database, app.state.settings)
    claims = {'sub': 'guide-oidc', 'exp': time.time() + 600}
    token, csrf, account, _ = service.identify(claims, {})
    assert account['onboarding_stage'] == 'welcome'
    client = TestClient(app)
    try:
        client.cookies.set('avatar_session', token)
        assert client.post('/api/me/onboarding/done').status_code == 403
        assert client.post('/api/me/onboarding/done', headers={'X-CSRF-Token':csrf}).status_code == 200
    finally:
        client.close()
    assert service.identify(claims, {})[2]['onboarding_stage'] == 'done'
    assert user.get('/api/auth/me').json()['user']['onboarding_stage'] == 'welcome'


def test_profile_entry_can_finish_guide_and_open_only_the_user_manual(context):
    from avatar.accounts.auth import issue_session
    app, user, _ = context
    user_id = user.get('/api/auth/me').json()['user']['id']
    with app.state.database.connect(write=True) as db:
        token, csrf, _ = issue_session(db, user_id, access_scope='profile')
    entry = TestClient(app)
    try:
        entry.cookies.set('avatar_session', token)
        assert entry.get('/api/help/用户操作手册.md').status_code == 200
        assert entry.get('/api/help/知识库与soul.md').status_code == 403
        assert entry.post('/api/me/onboarding/done', headers={'X-CSRF-Token': csrf}).status_code == 200
        assert user.get('/api/auth/me').json()['user']['onboarding_stage'] == 'done'
    finally:
        entry.close()


# 合成账户上的管理员读投影、身份权限、分页和查询上限

def seed_agents(db):
    for i in range(12):
        owner = f'owner-{i:02}'
        db.execute("INSERT INTO users(id,username,password,role,created_at) VALUES(?,?,?,'user','1')",
                   (owner, owner, 'unused'))
        db.execute("""INSERT INTO avatar_records(user_id,aic,certificate,profile,last_modified_at,created_at,updated_at)
                      VALUES(?,?,'{}',?,'1','1','1')""",
                   (owner, 'DEMO-'+owner, json.dumps({'avatar_name': owner, 'bio': 'fixture-only-'+'x'*4096})))
    db.executemany("""INSERT INTO avatar_identity_jobs(id,user_id,actor,action,expected_aic,status,created_at,updated_at)
                      VALUES(?,?,'admin',?,?,'done',?,'1')""",
                   [('relevant','owner-11','renew','DEMO-owner-11','1'),
                    ('retired','owner-11','unregister','DEMO-owner-11','3'),
                    ('replaced','owner-11','register','other-aic','4'),
                    *[(f'noise-{i}','owner-00','register','','2') for i in range(210)]])


def test_admin_reads_require_a_full_admin_session(context):
    app, user, admin = context
    paths = ['/api/admin/overview','/api/admin/settings','/api/admin/agents/operations','/api/admin/agents/missing/detail']
    anonymous = TestClient(app)
    for path in paths:
        assert anonymous.get(path).status_code == 401
        assert user.get(path).status_code == 403
    with app.state.database.connect(write=True) as db:
        db.execute("UPDATE users SET role='admin' WHERE username='tester'")
    assert [user.get(path).status_code for path in paths] == [200, 200, 200, 404]
    token = hashlib.sha256(admin.cookies['avatar_session'].encode()).hexdigest()
    with app.state.database.connect(write=True) as db:
        db.execute("UPDATE sessions SET access_scope='profile' WHERE token=?", (token,))
    assert all(admin.get(path).status_code == 403 for path in paths)


def test_admin_page_budgets_and_detail(context, monkeypatch):
    app, _, admin = context
    database = app.state.database
    with database.connect(write=True) as db:
        seed_agents(db)
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('unrelated','123')")
    sql = []
    connect = database.connect

    @contextmanager
    def trace(*args, **kwargs):
        with connect(*args, **kwargs) as db:
            execute = db.execute
            def counted(query, params=()):
                sql.append(query)
                return execute(query, params)
            db.execute = counted
            yield db

    monkeypatch.setattr(database, 'connect', trace)
    def read(path):
        sql.clear()
        response = admin.get('/api/admin/'+path)
        assert response.status_code == 200, response.text
        return response, [s for s in sql if s.lstrip().upper().startswith(('SELECT','WITH'))]

    assert admin.get('/api/admin/dashboard').status_code == 404
    overview, stats = read('overview')
    assert overview.json()['counts']['agents'] == 12
    settings, config = read('settings')
    assert set(settings.json()) == {'settings','environment','mode'}
    assert 'unrelated' not in settings.json()['settings']
    assert len(config) <= 4 and all('avatar_records' not in s for s in config)
    first, queries = read('agents/operations')
    second, _ = read('agents/operations?offset=10')
    page = second.json()
    assert page['total'] == 12 and page['offset'] == 10 and page['limit'] == 10
    assert [item['user_id'] for item in first.json()['items']+page['items']] == [f'owner-{i:02}' for i in range(12)]
    item = page['items'][1]
    assert item['identity_job']['id'] == 'relevant'
    assert page['entity_port_pool'] == first.json()['entity_port_pool']
    assert 'fixture-only-' not in first.text and 'fixture-only-' not in second.text
    assert len(stats) <= 5 and len(queries) <= 9 and len(first.content) < 16000
    detail, _ = read('agents/owner-11/detail')
    assert detail.json()['user_id'] == item['user_id']
    assert {key:detail.json()['protocol'][key] for key in item['protocol']} == item['protocol']
    assert admin.get('/api/admin/agents/missing/detail').status_code == 404
    assert read('agents/operations?search=owner-11')[0].json()['total'] == 1
    print('fixture 12 agents/213 jobs: '
          f'overview {len(stats)} SQL/{len(overview.content)} B, settings {len(config)} SQL/{len(settings.content)} B, '
          f'operations page {len(queries)} SQL/{len(first.content)} B')
    from dataclasses import replace
    enabled = replace(app.state.settings,entity_aip_enabled=True)
    monkeypatch.setattr(app.state, 'settings', enabled)
    monkeypatch.setattr(app.state.personal_aip, 'settings', enabled)
    with connect(write=True) as db:
        db.execute("INSERT INTO avatar_entity_ports(user_id,port) VALUES('owner-00',?)", (enabled.entity_aip_port,))
        db.execute("INSERT INTO oidc_identities(issuer,subject,user_id,username) VALUES(?, 'bound','owner-01','bound')",
                   (enabled.oidc_issuer,))
    active, enabled_queries = read('agents/operations')
    for row in active.json()['items']:
        assert row['protocol']['endpoint'] == app.state.personal_aip.status(row['user_id'])['endpoint']
    assert len(enabled_queries) <= 10
    print(f'operations with entity AIP enabled: {len(enabled_queries)} SQL selects')

def test_postgres_admin_operations_preserve_pages(tmp_path, postgres_url):
    from types import SimpleNamespace
    from avatar.core.config import Settings
    from avatar.core.db import Database
    from avatar.profiles.admin_reads import operations

    database = Database(tmp_path, postgres_url)
    try:
        database.initialize()
        with database.connect(write=True) as db:
            seed_agents(db)
            db.execute("UPDATE avatar_records SET acs_document=? WHERE user_id='owner-11'",
                       ('{"version":"1.1.0","entityMeta":{"environment":"production"}}',))
        state = SimpleNamespace(database=database, settings=Settings(data_dir=tmp_path,entity_aip_enabled=True),
                                service=SimpleNamespace(provider=object()),
                                personal_aip=SimpleNamespace(online_count=lambda: 0,
                                    operation_protocol=lambda uid, endpoint: {'endpoint':endpoint}))
        first = operations(state,'',0)
        last = operations(state,'',700)
        assert first['total'] == last['total'] == 12
        assert last['offset'] == 10 and len({r['user_id'] for r in first['items']+last['items']}) == 12
        row = operations(state,'owner-11',0)['items'][0]
        assert row['identity_job']['id'] == 'relevant'
        assert row['acs_document'] == {'version':'1.1.0','entityMeta':{'environment':'production'}}
    finally:
        database.close()


# 管理员角标只查询汇总，写操作和缓存故障不得改变审核结果

def test_review_badge_tracks_transaction_and_session(context):
    app, user, admin = context
    path = '/api/admin/pending-count'
    assert user.get(path).status_code == 403
    assert admin.get(path).json() == {'pending': 0}
    item = submit(user)
    assert admin.get(path).json() == {'pending': 1}
    approve(admin, item)
    assert admin.get(path).json() == {'pending': 0}
    assert user.get('/api/auth/me').json()['user']['profile_name'] == '测试用户'


def test_cache_outage_uses_database(context, tmp_path):
    from avatar.profiles.pending_reviews import PendingReviews
    app, user, admin = context
    cache = PendingReviews(app.state.database, str(tmp_path / "absent.sock"))
    try:
        cache.invalidate()
        assert cache.get() == 0
    finally:
        cache.close()


@pytest.fixture
def redis_socket():
    socket_path = os.getenv('TEST_AVATAR_REDIS_SOCKET')
    if not socket_path:
        pytest.skip('requires a dedicated Redis socket')
    return socket_path


def test_redis_hit_and_concurrent_invalidation(context, monkeypatch, redis_socket):
    from avatar.profiles.pending_reviews import PendingReviews
    app, user, admin = context
    cache = PendingReviews(app.state.database, redis_socket)
    monkeypatch.setattr(app.state, 'pending_reviews', cache)
    url = '/api/admin/pending-count'
    try:
        assert cache.client.ping()
        assert admin.get(url).json() == {'pending': 0}
        with monkeypatch.context() as m:
            m.setattr(cache, '_read', lambda: pytest.fail('warm badge queried SQL'))
            assert admin.get(url).json() == {'pending': 0}
        item = submit(user)
        assert admin.get(url).json() == {'pending': 1}
        cache.invalidate()
        def review_during_fill():
            approve(admin, item)
            return 1
        with monkeypatch.context() as m:
            m.setattr(cache, '_read', review_during_fill)
            assert cache.get() == 1
        assert cache.get() == 0
        assert 0 < cache.client.pttl(cache.key) <= 1000 * cache.ttl
    finally:
        cache.client.delete(cache.key)
        cache.close()


def test_same_socket_isolates_different_schemas(context, postgres_schema, redis_socket):
    from contextlib import closing
    from avatar.core.db import Database
    from avatar.profiles.pending_reviews import PendingReviews
    app, user, _ = context
    database = app.state.database
    submit(user)
    with postgres_schema() as other_url, closing(Database(database.data_dir / 'other-schema', other_url)) as other_db:
        other_db.initialize()
        with closing(PendingReviews(database, redis_socket)) as first, closing(PendingReviews(other_db, redis_socket)) as second:
            try:
                assert first.get() == 1
                assert second.get() == 0
                second.invalidate()
                assert first.client.get(first.key) == b'1'
            finally:
                first.client.delete(first.key, second.key)


# notifications

def test_unread_count_projects_visible_events_and_follows_read_state(context):
    app, user, admin = context
    path = '/api/notifications/unread-count'

    def check(client):
        result = client.get(path)
        assert result.status_code == 200
        visible = client.get('/api/notifications?status=unread').json()
        assert result.json() == {'unread': visible['total']}
        return visible['items']

    account = user.get('/api/auth/me').json()['user']['id']
    with app.state.database.connect(write=True) as db:
        token, *_ = issue_session(db, account, access_scope='profile')
    assert user.get(path, headers={'cookie': 'avatar_session=' + token}).status_code == 403

    submission = submit(user)
    rows = check(admin)
    request_item = next(item for item in rows if item['category'] == 'request')
    assert all(item['id'] != request_item['id'] for item in check(user))
    changed = admin.post(f"/api/notifications/{request_item['id']}/read")
    assert changed.status_code == 200
    assert request_item['id'] not in {item['id'] for item in check(admin)}
    assert admin.post(f"/api/notifications/{request_item['id']}/unread").status_code == 200
    approve(admin, submission)
    assert request_item['id'] not in {item['id'] for item in check(admin)}


# 直接实体注销及 ACS 替换在本地清理阶段的恢复契约

@pytest.fixture
def owner(tmp_path, pg_database):
    timestamp = now()
    with pg_database.connect(write=True) as db:
        db.execute("INSERT INTO users(id,username,password,role,created_at) VALUES('owner','owner','unused','user',?)", (timestamp,))
        db.execute("""INSERT INTO avatar_records(user_id,aic,certificate,profile,last_modified_at,created_at,updated_at)
                      VALUES('owner','old-aic','{}','{}',?,?,?)""", (timestamp, timestamp, timestamp))
    identity = entity_identity_paths(tmp_path, 'owner').root
    identity.parent.mkdir(parents=True)
    settings = SimpleNamespace(provider='acps-direct', data_dir=tmp_path, system_atr_call_timeout=5, entity_aip_enabled=True)
    return pg_database, identity, settings


def test_unregister_waits_for_local_cleanup_and_resumes_without_remote_call(owner):
    database, identity, settings = owner
    other = settings.data_dir / 'unrelated'
    other.mkdir()
    (other / 'keep').write_text('original')
    identity.symlink_to(other, target_is_directory=True)
    maintainer = AsyncMock(return_value={})
    service = EntityLifecycleService(database, settings, runner=object(), maintainer=maintainer)
    admin = {'id': 'owner', 'username': 'owner'}
    job = service.enqueue(admin, 'owner', 'unregister')
    assert asyncio.run(service.once())
    with database.connect() as db:
        assert db.execute('SELECT status FROM avatar_identity_jobs WHERE id=?', (job['id'],)).fetchone()[0] == 'waiting'
        assert db.execute('SELECT identity_status FROM avatar_records').fetchone()[0] == 'retired'
        assert db.execute("SELECT COUNT(*) FROM audit_events WHERE action='个人身份维护完成'").fetchone()[0] == 0
    assert (other / 'keep').read_text() == 'original'
    identity.unlink()
    resumed = EntityLifecycleService(database, settings, runner=object(), maintainer=maintainer)
    resumed.refresh(admin, job['id'])
    assert asyncio.run(resumed.once())
    assert not asyncio.run(resumed.once())
    with database.connect() as db:
        assert db.execute('SELECT status FROM avatar_identity_jobs WHERE id=?', (job['id'],)).fetchone()[0] == 'done'
        assert db.execute("SELECT COUNT(*) FROM audit_events WHERE action='个人身份维护完成'").fetchone()[0] == 1
    maintainer.assert_awaited_once()


def test_restarted_acs_replacement_cleans_only_old_candidate(owner):
    database, identity, settings = owner
    with database.connect(write=True) as db:
        timestamp = now()
        db.execute("UPDATE avatar_records SET identity_status='retired'")
        db.execute("""INSERT INTO profile_submissions(id,user_id,last_modified_at,snapshot,status,submitted_at)
                      VALUES('submission','owner',?,'{}','approved',?)""", (timestamp, timestamp))
        db.execute("""INSERT INTO provision_jobs(id,submission_id,status,registration_phase,updated_at)
                      VALUES('replacement','submission','running','identity_cleanup',?)""", (timestamp,))
    identity.mkdir()
    old = identity / 'old'
    old.touch()
    candidate = identity / 'new'
    restored = []

    async def provision(*args, **kwargs):
        restored.append((old.exists(), candidate.exists()))
        identity.mkdir(exist_ok=True)
        candidate.touch()
        raise RegistrationPending('等待审核', phase='registry_review')

    worker = ProvisionWorker(database, SimpleNamespace(mode='acps-direct', runner=SimpleNamespace(settings=settings), provision=provision))
    worker.recover()
    assert asyncio.run(worker.once())
    with database.connect(write=True) as db:
        db.execute("UPDATE provision_jobs SET next_attempt_at=0 WHERE id='replacement'")
    assert asyncio.run(worker.once())
    assert restored == [(False, False), (False, True)]


def test_local_identity_loader_rejects_inactive_state_without_network(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from avatar.protocols.identity import document as module
    import httpx
    (tmp_path/'.atr.lock').write_bytes(b'')
    config=SimpleNamespace(paths=SimpleNamespace(state_dir=tmp_path,acs_path=tmp_path/'acs.json'))
    monkeypatch.setattr(module,'registration_candidate',lambda config:{})
    monkeypatch.setattr(module,'identity_json',lambda path:{'phase':'retired','aic':'AIC'})
    def forbidden(*args,**kwargs):raise AssertionError('runtime must not contact Registry or CA')
    monkeypatch.setattr(httpx,'AsyncClient',forbidden)
    with pytest.raises(ValueError,match='未激活'):module.LocalEntityDirectory.load(config,'AIC')


def test_failed_endpoint_activation_waits_for_manual_health(provisioned_context,monkeypatch):
    from dataclasses import replace
    app, user, admin = provisioned_context
    uid=user.get('/api/auth/me').json()['user']['id']
    with app.state.database.connect(write=True) as db:db.execute("UPDATE avatar_records SET aic='TEST-FORMAL',identity_status='active' WHERE user_id=?",(uid,))
    runtime=app.state.personal_aip
    runtime.settings=replace(runtime.settings,entity_aip_enabled=True)
    attempts=[]
    async def unavailable(row):attempts.append(row['user_id']);raise ValueError('CA unavailable')
    monkeypatch.setattr(runtime,'activate',unavailable)
    async def check():
        await runtime.sync();await runtime.sync()
        assert attempts==[uid]
        result=await runtime.health(uid)
        assert not result['ok'] and attempts==[uid,uid]
        await runtime.sync()
        assert attempts==[uid,uid]
    asyncio.run(check())


def test_tls_background_uses_local_snapshot_without_ca_polling(monkeypatch):
    from types import SimpleNamespace
    from datetime import datetime,timezone,timedelta
    from avatar.protocols.transport.directory import CachedEntityDirectory,_tls_background
    calls=[];clock=[100.0];revision=[1]
    entity=SimpleNamespace(aic='AIC')
    class Source:
        async def select(self,aic):calls.append(aic);return entity
    config=SimpleNamespace(certificate_status=SimpleNamespace(max_age=60))
    directory=CachedEntityDirectory({'AIC':config},Source(),clock=lambda:clock[0])
    monkeypatch.setattr(directory,'revision',lambda config:revision[0])
    expires=datetime.now(timezone.utc)+timedelta(days=1)
    monkeypatch.setattr(directory,'expires_at',lambda entity:expires)
    directory.cached['AIC']=(entity,1,0,expires)
    async def check():
        token=_tls_background.set(True)
        try:
            assert await directory.select('AIC') is entity
            assert calls==[]
            revision[0]=2
            with pytest.raises(ValueError):await directory.select('AIC')
            assert calls==[]
        finally:_tls_background.reset(token)
        assert await directory.select('AIC') is entity
        assert calls==['AIC']
    asyncio.run(check())
