"""PostgreSQL 初始化、运行、隔离、协议存储及迁移。"""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from hashlib import sha256
from types import SimpleNamespace

import psycopg
import pytest

from avatar.core.db import Database
from avatar.runtime.schema import install_schema
from avatar.protocols.tasks.management import SystemManagementService
from avatar.protocols.tasks.store import ManagementTaskError, SystemTaskStore
from scripts.migrations.system_tasks import import_system_tasks
from scripts.migrate_sqlite_to_postgres import (
    create_snapshot,
    dependency_order,
    validate_schema,
    validate_source,
)
from tests.support import legacy_sqlite


# All test writes require a schema owned by the current test process

def test_unsafe_test_database_is_rejected_before_connecting(monkeypatch, postgres_schema):
    for target in ('', 'dbname=avatar_production host=invalid.invalid'):
        monkeypatch.setenv('TEST_POSTGRES_URL', target)
        with pytest.raises(RuntimeError, match='TEST_POSTGRES_URL'):
            with postgres_schema():
                pytest.fail('unsafe database accepted')


def test_unscoped_database_cannot_be_opened(tmp_path, postgres_url):
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    config = conninfo_to_dict(postgres_url)
    del config['options']
    with pytest.raises(RuntimeError, match='schema'):
        Database(tmp_path, make_conninfo(**config))
    with closing(Database(tmp_path, postgres_url)) as scoped:
        with scoped.connect() as db:
            assert db.execute('SELECT current_schema()').fetchone()[0].startswith('avatar_test_')


def test_orphan_cleanup_waits_for_workers_and_tagged_connections(postgres_schema):
    import signal
    import subprocess
    import sys

    import psycopg
    from psycopg.conninfo import make_conninfo
    from tests.support import test_dsn

    dsn, _ = test_dsn()
    child_code = """
import signal
from psycopg.conninfo import conninfo_to_dict
from tests.support import isolated_schema
with isolated_schema() as url:
    print(conninfo_to_dict(url)['application_name'], flush=True)
    signal.pause()
"""
    with subprocess.Popen([sys.executable, '-c', child_code], stdout=subprocess.PIPE, text=True) as child:
        try:
            schema = child.stdout.readline().strip()
            assert schema.startswith('avatar_test_')
            with psycopg.connect(dsn, autocommit=True) as check:
                def exists():
                    return check.execute('SELECT to_regnamespace(%s) IS NOT NULL', (schema,)).fetchone()[0]
                with postgres_schema():
                    assert exists(), 'another active worker owns the lock'
                with psycopg.connect(make_conninfo(dsn, application_name=schema)):
                    child.kill()
                    assert child.wait(timeout=10) == -signal.SIGKILL
                    with postgres_schema():
                        assert exists(), 'a surviving service still uses this schema'
                with postgres_schema():
                    assert not exists(), 'the next run must reap the abandoned schema'
        finally:
            if child.poll() is None:
                child.kill()


# PostgreSQL schema must preserve cross-account owner checks

def test_schema_initializes_and_rejects_cross_account_owner(postgres_url):
    with psycopg.connect(postgres_url) as conn:
        with conn.transaction():
            install_schema(conn)
            indexes = {row[0] for row in conn.execute("""SELECT indexname FROM pg_indexes
                WHERE schemaname=current_schema() AND tablename='avatar_aip_tasks'""")}
            assert {'avatar_aip_working_deadline', 'avatar_aip_guest_sent_at'} <= indexes
            conn.execute("INSERT INTO users(id,username,password,role,created_at) VALUES ('schema-u','schema-u','x','user','x')")
            conn.execute("INSERT INTO users(id,username,password,role,created_at) VALUES ('schema-v','schema-v','x','user','x')")
            conn.execute("INSERT INTO knowledge_assets(id,user_id,filename,mime,size,sha256,kind,caption,created_at,relative_path) VALUES ('schema-a','schema-u','x','x',1,'x','x','x','x','x')")
            with pytest.raises(psycopg.IntegrityError):
                with conn.transaction():
                    conn.execute("UPDATE knowledge_assets SET user_id='schema-v' WHERE id='schema-a'")


def test_existing_account_stays_out_of_first_run_on_upgrade(postgres_url):
    with psycopg.connect(postgres_url) as conn:
        install_schema(conn)
        conn.execute("ALTER TABLE users DROP COLUMN onboarding_stage")
        conn.execute("INSERT INTO users(id,username,password,role,created_at) VALUES ('legacy','legacy','x','user','x')")
        install_schema(conn)
        assert conn.execute("SELECT onboarding_stage FROM users WHERE id='legacy'").fetchone() == ('done',)


# postgres runtime

def test_postgres_transactions_and_rows(pg_database):
    with pg_database.connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", ('runtime_test', 'first'))
        db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", ('runtime_test', 'second'))
        row = db.execute("SELECT key,value FROM settings WHERE key=?", ('runtime_test',)).fetchone()
        assert row[0] == row['key'] == 'runtime_test'
        assert dict(row)['value'] == 'second'
    with pytest.raises(RuntimeError):
        with pg_database.connect(write=True) as db:
            db.execute("UPDATE settings SET value=? WHERE key=?", ('rolled back', 'runtime_test'))
            raise RuntimeError()
    with pg_database.connect(write=True) as db:
        assert db.execute("SELECT value FROM settings WHERE key=?", ('runtime_test',)).fetchone()[0] == 'second'
        db.execute("DELETE FROM settings WHERE key=?", ('runtime_test',))


def test_literal_placeholders_and_json_projection(pg_database):
    with pg_database.connect() as db:
        row = db.execute("SELECT '?' AS literal, json_extract(?, '$.name') AS name, instr(?, ?) AS pos", ('{\"name\":\"A\"}', 'abc', 'b')).fetchone()
        assert tuple(row) == ('?', 'A', 2)
        assert db.execute("SELECT value FROM json_each(?)", ('[\"a\",\"b\"]',)).fetchall()[0][0] == 'a'



def test_postgres_rowid_identity_and_reinitialization(pg_database):
    pg_database.initialize()
    with pg_database.connect() as db:
        assert db.execute('SELECT count(*) FROM avatar_schema_version').fetchone()[0] == 1
    with pg_database.connect(write=True) as db:
        db.execute("INSERT INTO users(id,username,password,role,created_at) VALUES(?,?,?,?,?)", ('rowid-user', 'rowid-user', 'hash', 'user', '2026'))
        db.execute("INSERT INTO model_artifact_cache VALUES(?,?,?,?,?,?,?)", ('rowid-user', 'test', 's', 'd', 'h', '{}', '2026'))
        row = db.execute("SELECT rowid,* FROM model_artifact_cache WHERE kind=?", ('test',)).fetchone()
        assert isinstance(row['rowid'], int)
        assert 'rowid' not in db.execute("SELECT * FROM model_artifact_cache WHERE kind=?", ('test',)).fetchone().keys()
        db.execute('DELETE FROM model_artifact_cache WHERE rowid=?', (row['rowid'],))
        cursor = db.execute("INSERT INTO audit_events(user_id,actor,action,detail,created_at) VALUES(?,?,?,?,?)", (None, 'test', 'test', '', '2026'))
        assert isinstance(cursor.lastrowid, int)
        assert db.execute('SELECT action FROM audit_events WHERE id=?', (cursor.lastrowid,)).fetchone()[0] == 'test'

def test_postgres_lexical_search_and_contains(pg_database):
    from avatar.rag.stores.lexical import LexicalStore
    store = LexicalStore(pg_database)
    with pg_database.connect(write=True) as db:
        db.execute("INSERT INTO users(id,username,password,role,created_at) VALUES(?,?,?,?,?)", ('lex-user', 'lex-user', 'hash', 'user', '2026'))
        db.execute("INSERT INTO file_rag_documents(source,file_id,user_id) VALUES(?,?,?)", ('chat', 'lex-file', 'lex-user'))
        db.execute("""INSERT INTO rag_publications(id,user_id,source,file_id,revision,signature,collection,coverage,record_ids,manifest,lexical_ready,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", ('lex-pub', 'lex-user', 'chat', 'lex-file', 'r1', 'sig', 'profile', 'full', '[]', '[]', 1, '2026'))
        db.execute("UPDATE file_rag_documents SET active_publication=? WHERE file_id=?", ('lex-pub', 'lex-file'))
        db.execute("INSERT INTO file_rag_nodes(source,file_id,user_id,ordinal,collection,record_id) VALUES(?,?,?,?,?,?)", ('chat', 'lex-file', 'lex-user', 0, 'profile', 'rec-1'))
    store.stage('lex-user', 'profile', (['rec-1'], [{'file_id': 'lex-file', 'source': 'chat', 'filename': 'guide.md'}], ['PostgreSQL 迁移指南全文']))
    assert store.contains('lex-user', 'profile', ['rec-1'])
    results = store.search('lex-user', 'profile', ['rec-1'], ['迁移'])
    assert [row['id'] for row in results] == ['rec-1']
    assert results[0]['metadata']['source'] == 'chat'
    assert store.search('lex-user', 'profile', ['rec-1'], ['absenttermxyz']) == []
    assert store.search('lex-user', 'profile', [], ['迁移']) == []


def test_postgres_http_registration_and_login(tmp_path, postgres_url):
    import httpx
    from fastapi.testclient import TestClient
    from avatar.api.app import create_app
    from avatar.core.config import Settings
    from avatar.models.client import ModelClient
    from tests.support import fake_completion, fake_dns, login, submit, approve, MODEL

    model = ModelClient(httpx.MockTransport(fake_completion), resolver=fake_dns)
    app = create_app(Settings(data_dir=tmp_path, database_url=postgres_url), run_worker=False, model_client=model)
    admin_password = (tmp_path / 'initial-admin.txt').read_text().split('password: ')[1].strip()
    with TestClient(app) as client:
        response = client.post('/api/auth/register', json={
            'username': 'pguser', 'password': 'testpassword123',
            'student_id': '20260001', 'email': 'pguser@example.com', 'phone': '13800000001',
        })
        assert response.status_code == 201, response.text
        login(client, 'pguser', 'testpassword123')
        assert client.get('/api/me').status_code == 200
        assert client.put('/api/me/model-settings', json={'model_config_input': MODEL}).status_code == 200
        submission = submit(client)
        admin = TestClient(app)
        login(admin, 'admin', admin_password)
        assert admin.get('/api/admin/accounts?search=pguser').status_code == 200
        approve(admin, submission)
        response = client.post('/api/conversations', json={'channel': 'assistant', 'title': 'pg test'})
        assert response.status_code == 201, response.text
        cid = response.json()['id']
        assert client.patch('/api/conversations/' + cid, json={'title': 'renamed'}).status_code == 200
        assert client.get('/api/conversations/' + cid + '/skills').status_code == 200
        assert client.get('/api/conversations/' + cid + '/tools').status_code == 200
        assert client.get('/api/conversations/' + cid + '/style-learning').status_code == 200
        response = client.get('/api/conversations?channel=assistant&personal=true')
        assert response.status_code == 200, response.text
        response = client.post('/api/chat/generations', json={
            'conversation_id': cid, 'channel': 'assistant', 'personal': True,
            'message': 'hello', 'request_id': 'pg-request',
        })
        assert response.status_code == 202, response.text
        assert client.get('/api/chat/pending', params={'conversation_id': cid}).status_code == 200
        from avatar.conversation.jobs.store import GenerationStore
        from avatar.conversation.jobs.pipeline import generation_scope
        from avatar.conversation.schemas import ChatRequest
        user = client.get('/api/auth/me').json()['user']
        store = GenerationStore(app.state.database)
        job = store.claim()[0]
        body = ChatRequest(conversation_id=cid, channel='assistant', personal=True, message='hello', request_id='pg-request')
        with generation_scope(job):
            app.state.chat.save(user, body, {'agent': None}, 'saved response')
        response = client.get('/api/chat', params={'channel': 'assistant', 'personal': True, 'conversation_id': cid})
        assert response.status_code == 200, response.text
        assert any(message['content'] == 'saved response' for message in response.json())
        assert client.post('/api/conversations/' + cid + '/clear?channel=assistant&personal=true').status_code == 200
        response = client.post('/api/me/attachments', content=b'postgres smoke', headers={'X-Attachment-Filename': 'smoke.txt'})
        assert response.status_code == 201, response.text
        attachment = response.json()['id']
        assert client.get('/api/me/attachments').status_code == 200
        assert client.get('/api/me/attachments/' + attachment).content == b'postgres smoke'
        assert client.delete('/api/me/attachments/' + attachment).status_code == 200
        assert client.get('/api/notifications?search=smoke').status_code == 200
    assert not (tmp_path / 'avatar.sqlite3').exists()

def test_entity_admin_reads_use_configured_store(tmp_path, postgres_url):
    from fastapi.testclient import TestClient
    from avatar.api.app import create_app
    from avatar.core.config import Settings
    from avatar.core.db import now
    from tests.support import login

    settings = Settings(data_dir=tmp_path, database_url=postgres_url, entity_aip_enabled=True)
    app = create_app(settings, run_worker=False)
    password = (tmp_path / 'initial-admin.txt').read_text().split('password: ')[1].strip()
    with TestClient(app) as client:
        login(client, 'admin', password)
        uid = client.get('/api/auth/me').json()['user']['id']
        with app.state.database.connect(write=True) as db:
            db.execute("INSERT INTO avatar_records(user_id,aic,certificate,profile,last_modified_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                       (uid, 'fixture-aic', '{}', '{}', now(), now(), now()))
            db.execute('INSERT INTO avatar_entity_ports(user_id,port) VALUES(?,?)', (uid, settings.entity_aip_port))
        overview = client.get('/api/admin/agents/operations')
        agents = client.get('/api/records/agents', params={'scope': 'admin'})
        assert overview.status_code == agents.status_code == 200
        assert overview.json()['entity_port_pool']['allocated'] == 1
        assert agents.json()['items'][0]['protocol']['endpoint'].endswith(f':{settings.entity_aip_port}/aip/rpc')
    assert not (tmp_path / 'avatar.sqlite3').exists()


def test_postgres_soul_progress_is_monotonic_and_preserves_cancellation(pg_database):
    import asyncio
    from types import SimpleNamespace
    from avatar.knowledge.build_worker import SoulBuildWorker

    with pg_database.connect(write=True) as db:
        db.execute("INSERT INTO users(id,username,password,role,created_at) VALUES('owner','owner','hash','user','2026')")
        db.execute("""INSERT INTO soul_builds(id,user_id,prompt,asset_ids,profile_snapshot,profile_revision,status,stage,created_at,updated_at)
                      VALUES('job','owner','','[]','{}','','running','','2026','2026')""")
    service = SimpleNamespace(database=pg_database)
    for percent in (75, 10):
        SoulBuildWorker.progress(service, 'job', percent, 'work')
    with pg_database.connect(write=True) as db:
        assert db.execute("SELECT progress FROM soul_builds WHERE id='job'").fetchone()[0] == 75
        db.execute("UPDATE soul_builds SET status='canceled' WHERE id='job'")
    with pytest.raises(asyncio.CancelledError):
        SoulBuildWorker.progress(service, 'job', 100, 'late')


def test_rag_catalog_reads_are_bounded_and_recheck_permissions(pg_database, monkeypatch):
    import asyncio
    from contextlib import contextmanager
    from types import SimpleNamespace
    from fastapi import HTTPException
    from avatar.rag.service import MaterialRag
    from avatar.rag.indexing.service import revision

    with pg_database.connect(write=True) as db:
        db.executemany("INSERT INTO users(id,username,password,role,created_at) VALUES(?,?,'hash','user','2026')",
                       [('owner','owner'), ('peer','peer')])
        db.execute("""INSERT INTO avatar_records(user_id,aic,certificate,profile,last_modified_at,created_at,updated_at)
                      VALUES('owner','aic','','{}','','','')""")
        db.execute("""INSERT INTO chat_conversations(id,user_id,channel,target_aic,caller_id,platform,title,created_at,updated_at)
                      VALUES('parent','peer','avatar','aic','peer','web','','','')""")
        db.execute("INSERT INTO material_folders(user_id,path,caption) VALUES('owner','folder','context')")

    queries = []
    @contextmanager
    def counted_connection():
        with pg_database.connect() as db:
            def execute(query, params=()):
                queries.append(query)
                return db.execute(query, params)
            yield SimpleNamespace(execute=execute)

    rag = object.__new__(MaterialRag)
    rag.database = SimpleNamespace(connect=counted_connection)
    selected = []
    scope = ('owner','aic',{'id':'child','parent_id':'parent'},selected,False)
    count = 0
    for size in (1, 100, 500):
        with pg_database.connect(write=True) as db:
            db.executemany("""INSERT INTO attachments(id,user_id,filename,content_type,size,sha256,extraction_status,created_at,visibility,relative_path)
                              VALUES(?,'owner','note.txt','text/plain',1,'hash','ready','','public','folder/note.txt')""",
                           [(f'profile-{i}',) for i in range(count, size)])
            db.executemany("""INSERT INTO chat_files(id,user_id,conversation_id,filename,content_type,size,extraction_status,created_at)
                              VALUES(?,'peer','parent','chat.txt','text/plain',1,'ready','')""",
                           [(f'chat-{i}',) for i in range(count, size)])
        selected.extend({'id':f'chat-{i}', 'user_id':'peer'} for i in range(count, size))
        count = size
        queries.clear()
        rows = rag.documents(*scope)
        assert len(rows) == size * 2
        assert rows[0]['folder_descriptions'] == [{'path':'folder','description':'context'}]
        assert [r['id'] for r in rows if r['source']=='chat'] == [item['id'] for item in selected]
        assert len(queries) <= 4, (size, len(queries))

        proof = {'scope':scope, 'revisions':[((r['source'], r['id']), revision(r)) for r in rows]}
        queries.clear()
        rag.validate(({'_rag_access':proof},))
        assert len(queries) <= 4, (size, len(queries))

    def check_invalid():
        with pytest.raises(HTTPException) as error:
            rag.validate(({'_rag_access':proof},))
        assert error.value.status_code == 409

    with pg_database.connect(write=True) as db:
        db.execute("UPDATE material_folders SET caption='changed' WHERE user_id='owner'")
    check_invalid()
    with pg_database.connect(write=True) as db:
        db.execute("UPDATE material_folders SET caption='context' WHERE user_id='owner'")
        db.execute("UPDATE attachments SET visibility='private' WHERE id='profile-0'")
    check_invalid()
    with pg_database.connect(write=True) as db:
        db.execute("UPDATE attachments SET visibility='public' WHERE id='profile-0'")
        db.execute("UPDATE chat_files SET expires_at='2000-01-01' WHERE id='chat-0'")
    check_invalid()
    with pg_database.connect(write=True) as db:
        db.execute("UPDATE chat_files SET expires_at=NULL,referenced=0 WHERE id='chat-0'")
    check_invalid()
    selected[1]['user_id'] = 'owner'
    monkeypatch.setattr('avatar.core.execution.authorization', lambda: {'materials':['profile-0'], 'files':['chat-0','chat-1','chat-2']})
    rows = rag.documents(*scope)
    assert [r['id'] for r in rows] == ['profile-0', 'chat-2']

    async def stream_with_revocation(*, final_event):
        delivered = asyncio.Event()
        async def model_stream(*args):
            yield {'type':'delta', 'text':'first'}
            await asyncio.wait_for(delivered.wait(), 3)
            if not final_event:
                yield {'type':'delta', 'text':'revoked'}

        async def answer_context(context, message):
            return context, message

        monkeypatch.setattr(rag, 'answer_context', answer_context)
        rag.chat = SimpleNamespace(model_client=SimpleNamespace(stream=model_stream))
        proof['revisions'] = [((r['source'], r['id']), revision(r)) for r in rows]
        config = {'_rag_access':proof, '_material_sources':[{'filename':'chat.txt','type':'RAG'}]}
        texts = []
        with pytest.raises(HTTPException) as error:
            async for event in rag.stream((config, '', '', []), 'question'):
                texts.append(event['text'])
                with pg_database.connect(write=True) as db:
                    db.execute("UPDATE chat_files SET referenced=0 WHERE id='chat-2'")
                delivered.set()
        assert error.value.status_code == 409
        assert texts == ['first']

    asyncio.run(stream_with_revocation(final_event=False))
    with pg_database.connect(write=True) as db:
        db.execute("UPDATE chat_files SET referenced=1 WHERE id='chat-2'")
    asyncio.run(stream_with_revocation(final_event=True))


# 管理系统协议账本的 PostgreSQL 回执和离线迁移

def test_pg_system_receipt_concurrency_scope_and_expiry(pg_database):
    store = SystemTaskStore(pg_database)
    store.initialize()
    management = SystemManagementService(pg_database, 'https://example.invalid')
    ctx = SimpleNamespace(local_aic='system',peer_aic='peer',group_id=None)
    command = {'command':'start','taskId':'task','id':'cmd','dataItems':[{'type':'text','text':'列出分身'}]}
    def call(**updates):
        return store.handle({**command, **updates},ctx,management)
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies=list(pool.map(lambda _:call(),range(8)))
    assert all(item==replies[0] for item in replies)
    first=replies[0]
    assert first['status']['state']=='awaiting-completion'
    with pytest.raises(ManagementTaskError):
        call(taskId='another')
    with pytest.raises(ManagementTaskError):
        store.handle({**command,'command':'get'},SimpleNamespace(local_aic='system',peer_aic='other',group_id=None),management)
    closed=call(command='complete',sessionId=first['sessionId'])
    assert closed['id']!=first['id'] and closed['status']['state']=='completed'
    assert call(command='cancel')==closed
    other=call(taskId='expires',id='another')
    with pg_database.connect(write=True) as db:
        db.execute("UPDATE system_management_tasks SET deadline=0 WHERE task_id='expires'")
    expired=call(command='get',taskId='expires',sessionId=other['sessionId'])
    assert expired['status']['state']=='failed'
    assert call(command='complete',taskId='expires')==expired


def test_system_ledger_import_checks_each_of_eleven_receipts_and_rolls_back(pg_database,postgres_url,tmp_path):
    import psycopg
    path=tmp_path/'legacy.sqlite3'
    schema="""CREATE TABLE system_management_tasks(
        local_aic TEXT NOT NULL, peer_aic TEXT NOT NULL, group_id TEXT NOT NULL,
        session_key TEXT NOT NULL, session_id TEXT NOT NULL, task_id TEXT NOT NULL,
        command_id TEXT NOT NULL,document TEXT NOT NULL, deadline REAL,
        PRIMARY KEY(local_aic,peer_aic,group_id,session_key,task_id),
        UNIQUE(local_aic,peer_aic,group_id,session_key,command_id))"""
    rows=[('system','peer','','','session',f'{i:02}',str(i),
           json.dumps({'id':str(i),'status':{'state':'completed' if i%2 else 'awaiting-completion'}},indent=1),
           None if i%2 else 1700000000.123456+i) for i in range(11)]
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(schema)
        db.execute('PRAGMA user_version=1')
        db.executemany('INSERT INTO system_management_tasks VALUES(?,?,?,?,?,?,?,?,?)',rows)
    expected=sha256()
    for row in rows:
        expected.update(sha256(json.dumps(row,ensure_ascii=False,separators=(',',':')).encode()).digest())
    before=sha256(path.read_bytes()).digest()
    with psycopg.connect(postgres_url,autocommit=True) as raw, pytest.raises(ValueError,match='事务'):
        import_system_tasks(raw,path)
    with pytest.raises(ValueError,match='尚未导入'):
        SystemTaskStore(pg_database).initialize(legacy_present=True)
    with pg_database.connect(write=True) as target:
        report=import_system_tasks(target.raw,path)
    SystemTaskStore(pg_database).initialize(legacy_present=True)
    assert report=={'count':11,'sha256':expected.hexdigest()}
    with pg_database.connect(write=True) as target:
        assert import_system_tasks(target.raw,path)==report
    assert sha256(path.read_bytes()).digest()==before
    with pg_database.connect(write=True) as db:
        db.execute("DELETE FROM system_management_tasks WHERE task_id='00'")
        db.execute("UPDATE system_management_tasks SET document='{}' WHERE task_id='10'")
    with pytest.raises(ValueError, match='不一致'), pg_database.connect(write=True) as target:
        import_system_tasks(target.raw,path)
    with pg_database.connect() as db:
        assert db.execute('SELECT count(*) FROM system_management_tasks').fetchone()[0]==10


# The application must reject missing PostgreSQL before touching identity files

def test_explicit_settings_require_postgres_before_migrating_identities(tmp_path, monkeypatch):
    from avatar.api.app import create_app
    from avatar.core.config import Settings
    from avatar.protocols.identity import document

    monkeypatch.setattr(document, 'migrate_identity_document', lambda _: pytest.fail('identity write before validating DSN'))
    with pytest.raises(ValueError, match='PostgreSQL'):
        create_app(Settings(data_dir=tmp_path, database_url=''), run_worker=False)


def test_frozen_sources_require_unambiguous_ownership(tmp_path):
    import hashlib
    from scripts.migrate_local_stores import sources

    user = 'owner'
    shared = tmp_path/'users'/user/'file_rag'
    shared.mkdir(parents=True)
    (shared/'chroma.sqlite3').touch()
    (tmp_path/'system_identity').mkdir()
    (tmp_path/'system_identity'/'aip-tasks.sqlite3').touch()
    assert {kind for kind, _, _ in sources(tmp_path, [user])} == {'chroma','aip'}
    with pytest.raises(ValueError, match='owner'):
        sources(tmp_path, [])
    historical = tmp_path/'file_rag'/hashlib.sha256(user.encode()).hexdigest()
    historical.mkdir(parents=True)
    (historical/'chroma.sqlite3').touch()
    with pytest.raises(ValueError, match='ambiguous'):
        sources(tmp_path, [user])
    (historical/'chroma.sqlite3').unlink()
    historical.rmdir()
    historical.symlink_to(shared, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        sources(tmp_path, [user])


# postgres migration

def test_rejects_foreign_key_violations(tmp_path):
    source = tmp_path / 'invalid.sqlite3'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE parents(id INTEGER PRIMARY KEY)')
        db.execute('CREATE TABLE children(parent_id INTEGER REFERENCES parents(id))')
        db.execute('INSERT INTO children VALUES(9)')
        with pytest.raises(ValueError, match='foreign key'):
            validate_source(db)


def test_dependency_order_handles_self_references_and_rejects_cycle(tmp_path):
    with sqlite3.connect(tmp_path / 'db.sqlite3') as db:
        db.execute('CREATE TABLE parent(id INTEGER PRIMARY KEY, ancestor INTEGER REFERENCES parent(id))')
        db.execute('CREATE TABLE child(parent INTEGER REFERENCES parent(id))')
        assert dependency_order(db, ['child', 'parent']) == ['parent', 'child']
        db.execute('CREATE TABLE a(id INTEGER PRIMARY KEY, b INTEGER REFERENCES b(id))')
        db.execute('CREATE TABLE b(id INTEGER PRIMARY KEY, a INTEGER REFERENCES a(id))')
        with pytest.raises(ValueError, match='cycle'):
            dependency_order(db, ['a', 'b'])


def test_schema_preflight_rejects_unexpected_table(legacy_sqlite):
    with sqlite3.connect(legacy_sqlite) as db:
        validate_schema(db)
        db.execute('CREATE TABLE unexpected (secret TEXT)')
        with pytest.raises(ValueError, match='schema'):
            validate_schema(db)


def test_schema_check_normalizes_only_historical_column_order(legacy_sqlite):
    with sqlite3.connect(legacy_sqlite) as db:
        db.execute('DROP TABLE protocol_message_versions')
        validate_schema(db)
        db.execute('CREATE TABLE protocol_message_versions(version INTEGER NOT NULL,kind TEXT PRIMARY KEY)')
        validate_schema(db)
        db.execute('DROP INDEX rag_nodes_record')
        with pytest.raises(ValueError, match='schema'):
            validate_schema(db)


def test_blob_columns_normalize_sqlite_text_values():
    from scripts.migrate_sqlite_to_postgres import canonical_row, normalize_value
    assert normalize_value('{"id": "model"}', 'BLOB') == b'{"id": "model"}'
    assert normalize_value(memoryview(b'raw'), 'BLOB') == b'raw'
    assert normalize_value('note', 'TEXT') == 'note'
    assert canonical_row(('a', 'b', memoryview(b'c')), ['TEXT', 'BLOB', 'BLOB']) == ('a', b'b', b'c')


def test_legacy_asset_import_records_only_the_owners_matching_attachment(legacy_sqlite, postgres_url, tmp_path):
    import hashlib
    from contextlib import closing

    import psycopg
    from scripts.migrate_sqlite_to_postgres import migrate

    with closing(sqlite3.connect(legacy_sqlite)) as source, source:
        source.execute('ALTER TABLE knowledge_assets DROP COLUMN source_attachment_id')
        for owner in ('a', 'b', 'c'):
            source.execute("INSERT INTO users(id,username,password,role,created_at) VALUES(?,?,'x','user','2026')",
                           (owner, owner))
            source.execute("""INSERT INTO knowledge_assets(id,user_id,filename,mime,size,sha256,kind,caption,created_at)
                VALUES(?,?,'copy.txt','text/plain',4,'same','text','','2026')""", ('copy-' + owner, owner))
            if owner != 'c':
                source.execute("""INSERT INTO attachments(id,user_id,filename,content_type,size,sha256,extraction_status,created_at)
                    VALUES(?,?,'source.txt','text/plain',4,'same','ok','2026')""", ('source-' + owner, owner))
    original = hashlib.sha256(legacy_sqlite.read_bytes()).digest()
    migrate(legacy_sqlite, tmp_path / 'backup.sqlite3', postgres_url)
    assert hashlib.sha256(legacy_sqlite.read_bytes()).digest() == original
    with psycopg.connect(postgres_url) as db:
        assert db.execute('SELECT user_id,source_attachment_id FROM knowledge_assets ORDER BY user_id').fetchall() == [
            ('a', 'source-a'), ('b', 'source-b'), ('c', '')]


# sqlite snapshot

def test_snapshot_includes_uncheckpointed_wal_and_does_not_replace_existing(tmp_path):
    source = tmp_path / 'source.sqlite3'
    backup = tmp_path / 'backup.sqlite3'
    with sqlite3.connect(source) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute('CREATE TABLE data (value TEXT)')
        db.commit()
        db.execute('INSERT INTO data VALUES (?)', ('uncheckpointed',))
        db.commit()
        create_snapshot(source, backup)
        with sqlite3.connect(backup) as copied:
            assert copied.execute('SELECT value FROM data').fetchone() == ('uncheckpointed',)
        with pytest.raises(FileExistsError):
            create_snapshot(source, backup)
        with sqlite3.connect(backup) as copied:
            assert copied.execute('SELECT value FROM data').fetchone() == ('uncheckpointed',)


def test_snapshot_rejects_missing_source_without_creating_it(tmp_path):
    source = tmp_path / 'missing.sqlite3'
    with pytest.raises((FileNotFoundError, sqlite3.OperationalError)):
        create_snapshot(source, tmp_path / 'backup.sqlite3')
    assert not source.exists()


def test_snapshot_removes_incomplete_copy_if_integrity_check_fails(tmp_path):
    source = tmp_path / 'corrupt.sqlite3'
    destination = tmp_path / 'backup.sqlite3'
    source.write_bytes(b'not a database')
    with pytest.raises(sqlite3.DatabaseError):
        create_snapshot(source, destination)
    assert not destination.exists()
    assert source.read_bytes() == b'not a database'


def test_snapshot_is_private(tmp_path):
    source = tmp_path / 'source.sqlite3'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE data (value TEXT)')
    destination = create_snapshot(source, tmp_path / 'backup.sqlite3')
    assert destination.stat().st_mode & 0o077 == 0
