"""隔离数据库、测试账户与文件工厂，以及浏览器联测使用的模拟模型。"""
import asyncio
import json
import os
import re
import sqlite3
import uuid
import zipfile
from contextlib import contextmanager
from io import BytesIO
from urllib.parse import quote

import httpx
import psycopg
import pytest
import uvicorn
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from avatar.accounts.auth import hash_password
from avatar.api.app import create_app
from avatar.core.config import Settings
from avatar.core.migrations.rag_publications import migrate_rag_publications
from avatar.core.postgres_schema import SOURCE_SCHEMA
from avatar.models.client import ModelClient


# 数据库隔离

MARKER = 'personal-avatar pytest schema v1'
OWNED = set()
CANDIDATES = """
    SELECT n.nspname FROM pg_namespace n
    WHERE n.nspowner = current_user::regrole
      AND n.nspname ~ '^avatar_test_[0-9a-f]{32}$'
      AND obj_description(n.oid, 'pg_namespace') = %s
"""


def test_dsn():
    value = os.environ.get('TEST_POSTGRES_URL', '')
    database = conninfo_to_dict(value).get('dbname', '')
    if not re.search(r'(?:^|[_-])test(?:$|[_-])', database, flags=re.I):
        raise RuntimeError('TEST_POSTGRES_URL 必须显式指向专用测试数据库')
    return value, database


def reap(control):
    for (schema,) in control.execute(CANDIDATES, (MARKER,)).fetchall():
        with control.transaction():
            if not control.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))', (schema,)).fetchone()[0]:
                continue
            # A killed pytest worker may leave another process using its schema.
            safe = control.execute(CANDIDATES + """
                AND n.nspname = %s AND NOT EXISTS (
                    SELECT 1 FROM pg_stat_activity
                    WHERE datname = current_database() AND application_name = n.nspname
                )
            """, (MARKER, schema)).fetchone()
            if safe:
                control.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


@contextmanager
def isolated_schema():
    dsn, database = test_dsn()
    schema = 'avatar_test_' + uuid.uuid4().hex
    identifier = sql.Identifier(schema)
    url = make_conninfo(dsn, options=f'-csearch_path={schema}', application_name=schema)
    with psycopg.connect(dsn, autocommit=True) as control:
        if control.execute('SELECT current_database()').fetchone() != (database,):
            raise RuntimeError('TEST_POSTGRES_URL 与实际连接的数据库不一致')
        reap(control)
        with control.transaction():
            control.execute('SELECT pg_advisory_lock(hashtextextended(%s,0))', (schema,))
            control.execute(sql.SQL('CREATE SCHEMA {}').format(identifier))
            control.execute(sql.SQL('COMMENT ON SCHEMA {} IS {}').format(identifier, sql.Literal(MARKER)))
        OWNED.add(url)
        try:
            yield url
        finally:
            OWNED.discard(url)
            control.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(identifier))


# 旧库快照

@pytest.fixture
def legacy_sqlite(tmp_path):
    source = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(source) as db:
        for ddl in sorted(SOURCE_SCHEMA.values(), key=lambda sql: not sql.startswith('CREATE TABLE')):
            db.execute(ddl)
        # The historical SQLite lexical index also contains FTS5 objects.
        migrate_rag_publications(db)
    return source


# 账户与模型

PROFILE = {"student_id": "20260001", "name": "测试用户", "avatar_name": "测试分身", "bio": "从事智能体研究", "contact": "tester@example.com", "specialty": "智能体协作"}

MODEL = {"provider": "deepseek", "model": "test-model", "api_key": "test-user-key-123"}


async def fake_dns(host, port):
    return "203.0.113.10"


def modified_at(client):
    response=client.get('/api/me')
    return response.json()['profile']['last_modified_at'] if response.status_code==200 else '2000-01-01T00:00:00.000000+00:00'


def fake_completion(request):
    body = json.loads(request.content)
    declared = {tool['function']['name'] for tool in body.get('tools', ())}
    for name, arguments in (('plan_task', {'steps': []}), ('check_file_receipt', {'decision': 'plain_answer'}),
                            ('finish_reading', {}), ('continue_answer', {})):
        if name in declared:
            call = {'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}
            return httpx.Response(200, json={'choices': [{'message': {'tool_calls': [call]}}], 'usage': {'total_tokens': 2}})
    return httpx.Response(200, json={"choices": [{"message": {"content": "模型回答："+body["messages"][0]["content"]}}],
                                    "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}})


@pytest.fixture
def context(tmp_path,monkeypatch,request,postgres_url):
    model_client = ModelClient(httpx.MockTransport(fake_completion), resolver=fake_dns)
    app = create_app(Settings(data_dir=tmp_path, database_url=postgres_url), run_worker=False, model_client=model_client)
    request.addfinalizer(app.state.database.close)
    request.addfinalizer(app.state.chat.rag.store.close)
    admin_password = (tmp_path / "initial-admin.txt").read_text().split("password: ")[1].strip()
    admin = TestClient(app)
    request.addfinalizer(admin.close)
    login(admin, "admin", admin_password)
    user = TestClient(app)
    request.addfinalizer(user.close)
    assert user.post("/api/auth/register", json={"username": "tester", "password": "testpassword123", "student_id": "20260001", "email": "tester@example.com", "phone": "13800000001"}).status_code == 201
    login(user, "tester", "testpassword123")
    async def embeddings(client,config,key,inputs):
        import hashlib,re
        vectors=[]
        for item in inputs:
            text=item if isinstance(item,str) else 'image'
            vector=[0.0]*config['dimensions']
            for token in re.findall(r'[a-z0-9_]+|[\u4e00-\u9fff]',text.lower()):vector[int.from_bytes(hashlib.sha256(token.encode()).digest()[:4],'big')%len(vector)]+=1
            if not any(vector):vector[0]=1
            vectors.append(vector)
        return vectors,{'total_tokens':len(inputs)}
    monkeypatch.setattr('avatar.rag.indexing.service.embed',embeddings)
    monkeypatch.setattr('avatar.models.embedding.embed',embeddings)
    assert user.put('/api/me/embedding-settings',json={'provider':'siliconflow','model':'BAAI/bge-m3','dimensions':64,'api_key':'embedding-fixture-key'}).status_code==200
    return app, user, admin


@pytest.fixture
def previously_installed_skill(context, monkeypatch):
    app, user, _ = context
    uid = user.get('/api/auth/me').json()['user']['id']
    with monkeypatch.context() as before_pause:
        before_pause.setattr('avatar.skills.policy.SKILLS_PAUSED', False)
        return app.state.chat.skills.install(
            uid, 'SKILL.md', b'---\nname: archived-skill\ndescription: Archived skill\n---\nOld instructions.\n'
        )


def login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    client.headers["X-CSRF-Token"] = r.json()["csrf"]


def submit(user, profile=None):
    version = user.get("/api/me").json()["profile"]["last_modified_at"]
    model = user.put("/api/me/model-settings", json={"model_config_input": MODEL})
    assert model.status_code == 200, model.text
    saved = user.put("/api/me/profile", json={"profile": profile or PROFILE, "last_modified_at": version})
    assert saved.status_code == 200, saved.text
    r = user.post("/api/me/submit", json={"last_modified_at": saved.json()["profile"]["last_modified_at"]})
    assert r.status_code == 200, r.text
    return r.json()["history"][0]["id"]


def approve(admin, submission):
    r = admin.post(f"/api/admin/reviews/{submission}", json={"approve": True})
    assert r.status_code == 200, r.text


# 资料文件

def saved_file(owner,name,body,caption='研究资料'):
    row=upload_profile(owner,name,body,caption=caption).json()
    result=owner.patch('/api/me/attachments/'+row['id'],json={'caption':caption,'visibility':'private'})
    assert result.status_code==200,result.text
    return row['id']


def upload_profile(user, name, body, version=None, caption="测试资料内容说明"):
    version = user.get('/api/me').json().get('files_modified_at') if version is None else version
    return user.post('/api/me/attachments'+('?last_modified_at='+quote(version,safe="") if version else ''), content=body,
                     headers={'Content-Type':'application/octet-stream','X-Attachment-Caption':quote(caption),'X-Attachment-Filename':quote(name)})


def word_document(text):
    target=BytesIO()
    with zipfile.ZipFile(target,'w') as archive:
        from xml.sax.saxutils import escape
        archive.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'+escape(text)+'</w:t></w:r></w:p></w:body></w:document>')
    return target.getvalue()


# 浏览器联测服务

class DelayedStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        for part in ('浏览器流式', '回答完成<img src=x onerror="window.__xss_test=1">'):
            yield ('data: '+json.dumps({'choices':[{'delta':{'content':part}}]}, ensure_ascii=False)+'\n\n').encode()
            await asyncio.sleep(0.8)
        yield b'data: [DONE]\n\n'


def completion(request):
    if request.method == 'GET':
        return httpx.Response(200, json={'data':[]})
    body=json.loads(request.content)
    if body.get('stream'):
        if body.get('tools'):
            calls = fake_completion(request).json()['choices'][0]['message'].get('tool_calls')
            if calls:
                delta = {'tool_calls':[{**call, 'index':i} for i,call in enumerate(calls)]}
                event = {'choices':[{'delta':delta,'finish_reason':'tool_calls'}]}
                return httpx.Response(200, headers={'content-type':'text/event-stream'},
                                      content='data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n')
        return httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=DelayedStream())
    return fake_completion(request)


def setup(app):
    with app.state.database.connect(write=True) as db:
        db.execute("UPDATE users SET password=? WHERE is_root=1", (hash_password('browser-admin-password'),))
    admin=TestClient(app, base_url='http://127.0.0.1:18081')
    user=TestClient(app, base_url='http://127.0.0.1:18081')
    login(admin, 'admin', 'browser-admin-password')
    response=user.post('/api/auth/register',json={'username':'browser-user','password':'browser-user-password','student_id':'20260001','email':'browser@example.test'})
    if response.status_code!=201:raise RuntimeError(response.text)
    login(user, 'browser-user', 'browser-user-password')
    user.post('/api/me/onboarding/done').raise_for_status()
    submission=submit(user, PROFILE)
    approve(admin, submission)
    admin.close();user.close()


def serve_browser(path):
    with isolated_schema() as database_url:
        app=create_app(Settings(data_dir=path,database_url=database_url,oidc_public_url='http://127.0.0.1:18081',provider='demo',system_atr_enabled=False,avatar_atr_enabled=False,entity_aip_enabled=False),run_worker=True,model_client=ModelClient(httpx.MockTransport(completion),resolver=fake_dns))
        try:
            setup(app)
            uvicorn.run(app,host='127.0.0.1',port=18081,log_level='warning')
        finally:
            app.state.database.close()


@pytest.fixture
def provisioned_context(context):
    app, user, admin = context
    approve(admin, submit(user))
    assert asyncio.run(app.state.worker.once())
    return context


def upload(user, name, path=''):
    response = user.post('/api/me/attachments', content=name.encode(), headers={'X-Attachment-Filename': name, 'X-Attachment-Relative-Path': path or name})
    assert response.status_code == 201, response.text
    return response.json()
