"""Soul 学习、EverOS 队列与快照恢复。"""
import asyncio
import base64
import hashlib
import json
from pathlib import Path

import pytest
from everos.core.persistence import MemoryRoot
from tools.everos import worker


# everos resume

OWNER = 'a' * 64
PROJECT = 'build-' + 'b' * 32


def test_seed_validates_all_before_publishing_and_repairs_legacy_prefix(tmp_path):
    root = MemoryRoot(tmp_path)
    source = root.users_dir('avatar-soul', 'learning') / OWNER
    target = root.users_dir('avatar-soul', PROJECT) / OWNER
    episode = Path('episodes/episode-20261118.md')
    (source / episode.parent).mkdir(parents=True)
    (source / episode).write_text('original\n')
    manifest = Path('episodes/.avatar-sources.json')
    (source / manifest).write_text('["old"]')
    (source / 'user.md').symlink_to(tmp_path / 'private')
    with pytest.raises(ValueError):
        asyncio.run(worker.seed_tree(root, OWNER, 'learning', PROJECT))
    assert not target.exists()
    (source / 'user.md').unlink()
    (source / 'user.md').write_text('profile\n')
    (target / episode.parent).mkdir(parents=True)
    (target / episode).write_text('original\n')
    asyncio.run(worker.seed_tree(root, OWNER, 'learning', PROJECT))
    assert (target / episode).read_text() == 'original\n'
    assert (target / 'user.md').read_text() == 'profile\n'
    assert json.loads((target / manifest).read_text()) == ['old']


def test_published_update_recovers_manifest_and_keeps_other_projects(tmp_path, monkeypatch):
    root = MemoryRoot(tmp_path)
    project = worker.ProjectFiles(root, OWNER, PROJECT)
    other = worker.ProjectFiles(root, OWNER, 'learning')
    other.publish({'user.md':'other'})
    project.publish({'user.md':'before'})
    replace = worker.os.replace
    def interrupted(source, destination):
        if Path(destination) == project.directory / 'episodes/.avatar-sources.json':
            raise OSError('interrupted')
        return replace(source, destination)
    with monkeypatch.context() as m:
        m.setattr(worker.os, 'replace', interrupted)
        with pytest.raises(OSError):
            project.publish({'user.md':'after', 'episodes/.avatar-sources.json':'{"processed":["entry"]}'})
    project.recover()
    project.recover()
    assert (project.directory / 'user.md').read_text() == 'after'
    assert json.loads((project.directory / 'episodes/.avatar-sources.json').read_text())['processed'] == ['entry']
    assert (other.directory / 'user.md').read_text() == 'other'


def test_completed_result_replayed_before_index_startup(tmp_path, monkeypatch):
    project = worker.ProjectFiles(MemoryRoot(tmp_path), OWNER, PROJECT)
    payload = dict(owner=OWNER, project=PROJECT, root=str(tmp_path), source_id='c' * 64,
                   dimensions=2, embedding_model='fake', index_signature='d' * 24,
                   embedding_enabled=False, evidence={})
    result = {'memories':{'episodes':[]}}
    project.save_result(payload, result)
    emitted = []
    monkeypatch.setattr(worker, 'emit_done', emitted.append)
    asyncio.run(worker.main(payload))
    assert emitted == [result]
    assert project.result({**payload, 'evidence':{'new':'changed'}}) is None


def test_everos_episode_profile_and_completed_receipt_offline(tmp_path):
    import subprocess
    import sys
    payload = dict(owner=OWNER, project=PROJECT, root=str(tmp_path), source_id='e' * 64,
                   dimensions=8, embedding_model='offline', index_signature='f' * 24,
                   embedding_enabled=True, media=[{'filename':'sample','caption':'synthetic','marker':'fixture'}],
                   evidence={'new_style_observations':['synthetic']})
    files = worker.ProjectFiles(MemoryRoot(tmp_path), OWNER, PROJECT)
    files.save_result(payload, {'Subject':'Recovered','Summary':'summary','Content':'fixture'}, 'episode')
    files.save_result(payload, {'id':'profile_'+OWNER,'user_id':OWNER,'summary':'synthetic','profile_timestamp_ms':1,
                                'implicit_traits':[],'explicit_info':[]}, 'profile')
    command = [sys.executable, '-I', str(Path(worker.__file__))]
    queue = {}
    records = []
    def parent_reply(message):
        kind = message['type']
        if kind == 'embed':
            return {'result':[[1.0]+[0.0]*7 for _ in message['texts']]}
        if kind == 'pg':
            operation = message['operation']
            if operation == 'ensure':
                return {'result':{'ready':True}}
            if operation in ('queue_upsert','queue_sync'):
                for row in message['rows']:
                    queue[row['md_path']] = dict(row, status='pending', change_type=row.get('change_type','modified'), retry_count=0)
                return {'result':{'queued':len(message['rows'])}}
            if operation == 'queue_claim':
                rows = [dict(row, status='processing') for row in queue.values() if row['status'] == 'pending']
                for row in rows:
                    queue[row['md_path']]['status'] = 'processing'
                return {'result':{'rows':rows}}
            if operation == 'existing_vectors':
                return {'result':{'rows':[row for row in records if row.get('md_path') == message.get('md_path')]}}
            if operation == 'has_episode_vectors':
                return {'result':{'ready':any(row['kind']=='episode' and (row.get('embedding') or row.get('subject_embedding')) for row in records)}}
            if operation == 'upsert_file':
                records[:] = [row for row in records if row.get('md_path') != message['md_path']] + message['records']
                return {'result':{'upserted':len(message['records'])}}
            if operation == 'delete_file':
                records[:] = [row for row in records if row.get('md_path') != message['md_path']]
                return {'result':{'deleted':1}}
            if operation == 'vector_begin':
                return {'result':{'begun':True}}
            if operation == 'vector_chunk':
                records.extend(message['records'])
                return {'result':{'staged':len(message['records'])}}
            if operation == 'vector_commit':
                return {'result':{'committed':len(records)}}
            if operation == 'vector_commit_file':
                return {'result':{'committed':len(records)}}
            if operation == 'queue_complete':
                for row in message['rows']:
                    queue[row['md_path']]['status'] = 'done'
                return {'result':{'completed':True}}
            if operation == 'search':
                rows = [{'kind':row['kind'],'record_id':row['record_id'],'group_name':row['group'],
                         'title':row['title'],'summary':row['summary'],'content':row['content'],
                         'metadata':row['metadata'],'score':1.0} for row in records if row['kind']=='episode']
                return {'result':{'rows':rows}}
            if operation == 'profile':
                row=next((row for row in records if row['kind']=='user_profile'),None)
                return {'result':{'row':row}}
        raise AssertionError('unexpected worker RPC: '+kind)
    def call(request):
        process = subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,text=True)
        try:
            process.stdin.write(json.dumps(request)+'\n');process.stdin.flush()
            while line := process.stdout.readline():
                message = json.loads(line)
                if message['type'] == 'done':
                    return message['result']
                process.stdin.write(json.dumps(parent_reply(message))+'\n');process.stdin.flush()
            raise AssertionError(process.stderr.read()[-2500:])
        finally:
            process.stdin.close();process.wait(timeout=45)
            process.stdout.close();process.stderr.close()
    first = call(payload)
    assert call(payload) == first
    tree = call({**payload,'operation':'tree'})
    assert sorted(node['title'] for node in tree['nodes']) == ['Recovered', 'sample']
    assert (files.directory / 'user.md').is_file()
    assert not list(tmp_path.rglob('*.db'))
    assert not list(tmp_path.rglob('*.sqlite3'))
def test_soul_embedding_uncertain_response_is_single_shot(monkeypatch):
    from fastapi import HTTPException
    from unittest.mock import AsyncMock
    from avatar.models import embedding
    error = HTTPException(502, 'result unconfirmed')
    error.retryable = True
    request = AsyncMock(side_effect=error)
    monkeypatch.setattr(embedding, '_embed_once', request)
    with pytest.raises(HTTPException):
        asyncio.run(embedding.embed(None, {}, None, ['synthetic'], retry_uncertain=False))
    request.assert_awaited_once()


def test_parent_ack_only_accepts_running_owned_build(tmp_path, pg_database):
    from avatar.core.db import now
    from avatar.knowledge.build_worker import SoulBuildWorker
    from avatar.knowledge.service import KnowledgeService
    db = pg_database
    with db.connect(write=True) as conn:
        conn.execute("INSERT INTO users(id,username,password,role,created_at) VALUES('owner','owner','unused','user',?)", (now(),))
        conn.execute("""INSERT INTO soul_builds(id,user_id,prompt,asset_ids,profile_snapshot,profile_revision,status,stage,created_at,updated_at)
                        VALUES('build','owner','','[]','{}','1','canceled','',?,?)""", (now(), now()))
    workspace = KnowledgeService(db, None)
    runner = SoulBuildWorker(workspace)
    bridge = workspace.memory
    owner, private = bridge.scope('owner')
    payload = dict(root=str(private),owner=owner,project=PROJECT,source_id='9'*64)
    result = {'memories':{'episodes':[]}}
    worker.ProjectFiles(MemoryRoot(private),owner,PROJECT).save_result(payload,result)
    row = {'id':'build','user_id':'owner'}
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bridge.request(payload,row,fingerprint='verified',worker=runner))
    with db.connect(write=True) as conn:
        assert conn.execute("SELECT memory_fingerprint FROM soul_builds WHERE id='build'").fetchone()[0] == ''
        conn.execute("UPDATE soul_builds SET status='running' WHERE id='build'")
    assert asyncio.run(bridge.request(payload,row,fingerprint='verified',worker=runner)) == result
    with db.connect() as conn:
        assert conn.execute("SELECT memory_fingerprint FROM soul_builds WHERE id='build'").fetchone()[0] == 'verified'


def test_cross_project_tree_reads_while_model_is_pending(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    builds=[]
    async def slow_compile(*args):
        builds.append(args[0]['project'])
        entered.set()
        await release.wait()
        return {'memories':{}}
    monkeypatch.setattr(worker, 'compile_memory', slow_compile)
    monkeypatch.setattr(worker, 'emit_done', lambda result:None)
    args = dict(owner=OWNER, root=str(tmp_path), project=PROJECT)
    async def check():
        running = asyncio.create_task(worker.main(args))
        await asyncio.wait_for(entered.wait(), 4)
        duplicate = asyncio.create_task(worker.main(dict(args)))
        try:
            await asyncio.wait_for(worker.main({**args, 'project':'learning', 'operation':'tree'}), 2)
            assert not duplicate.done()
        finally:
            release.set()
            await asyncio.gather(running,duplicate)
        assert builds == [PROJECT]
    asyncio.run(check())


def test_compile_verifies_effective_seed_and_model_for_both_caches(tmp_path, monkeypatch, pg_database):
    from fastapi import HTTPException
    from avatar.core.db import now
    from avatar.knowledge.build_worker import SoulBuildWorker
    from avatar.knowledge.service import KnowledgeService
    from avatar.knowledge.everos_memory import EmbeddingSettings
    database=pg_database
    with database.connect(write=True) as db:
        db.execute("INSERT INTO users(id,username,password,role,created_at) VALUES('owner','owner','unused','user',?)", (now(),))
        for build,status in [('legacy','approved'),('target','running')]:
            db.execute("""INSERT INTO soul_builds(id,user_id,prompt,asset_ids,profile_snapshot,profile_revision,status,stage,created_at,updated_at)
                          VALUES(?,'owner','','[]','{}','1',?,'',?,?)""", (build,status,now(),now()))
        db.execute("INSERT INTO active_souls VALUES('owner','legacy')")
    monkeypatch.setattr(EmbeddingSettings,'current',lambda *args:{'encrypted_key':'test','model':'embed','dimensions':2})
    workspace = KnowledgeService(database, None)
    runner = SoulBuildWorker(workspace)
    bridge = workspace.memory
    source=bridge.project({'id':'legacy','origin':'build'})
    row=dict(id='target',user_id='owner',origin='build',memory_tree={'source_project':source})
    calls=[]
    async def request(payload,row,config,key,embedding,fingerprint,media,*,worker):
        assert worker is runner
        calls.append((payload,fingerprint))
        return {'model':config['model']}
    monkeypatch.setattr(bridge,'request',request)
    def compile(model):
        return asyncio.run(bridge.compile(row,{'model':model},'test',{'fact':'synthetic'},worker=runner))
    connect=database.connect
    def off_loop(*args,**kwargs):
        try:asyncio.get_running_loop()
        except RuntimeError:return connect(*args,**kwargs)
        raise AssertionError('Soul compile accessed DB on the event loop')
    monkeypatch.setattr(database,'connect',off_loop)
    first=compile('a')
    assert calls[0][0]['source_project']==''
    row.update(memory_fingerprint=calls[0][1],memory_context=first)
    assert compile('a')==first and len(calls)==1
    assert compile('b')=={'model':'b'} and len(calls)==2
    assert all(worker.ProjectFiles.fingerprint(payload)==fingerprint for payload,fingerprint in calls)
    with database.connect(write=True) as db:
        db.execute("UPDATE soul_builds SET memory_fingerprint='confirmed' WHERE id='legacy'")
    with pytest.raises(HTTPException,match='记忆'):
        compile('a')


def test_postgres_bridge_scope_and_vector_round_trip(pg_database,monkeypatch):
    from types import SimpleNamespace
    from avatar.knowledge.everos_memory import EverOSMemory
    bridge=EverOSMemory(SimpleNamespace(database=pg_database))
    transactions=[]
    connect=pg_database.connect
    def tracked(write=False, *, account_scope=None):
        transactions.append((write,account_scope))
        return connect(write=write,account_scope=account_scope)
    monkeypatch.setattr(pg_database,'connect',tracked)
    owner='owner'
    index='3'*24
    project='build-'+'4'*32
    assert bridge.postgres(owner,{'operation':'ensure','project':project,'index_signature':index})=={'ready':True}
    assert bridge.postgres(owner,{'operation':'ensure','project':project,'index_signature':index})=={'ready':True}
    bridge.postgres(owner,{'operation':'vector_begin','project':project,'index_signature':index,'generation':'5'*32})
    bridge.postgres(owner,{'operation':'vector_chunk','project':project,'index_signature':index,'generation':'5'*32,
        'records':[{'kind':'episode','record_id':'one','group':'daily','title':'T','summary':'S','content':'C','metadata':{'synthetic':True},'embedding':[1.0,0.0]}]})
    assert bridge.postgres(owner,{'operation':'vector_commit','project':project,'index_signature':index,'generation':'5'*32})=={'committed':1}
    found=bridge.postgres(owner,{'operation':'search','project':project,'index_signature':index,'query':'C','method':'hybrid','embedding':[1.0,0.0]})
    assert found['rows'][0]['record_id']=='one'
    assert transactions==[(True,None)]+[(True,owner)]*3+[(False,None)]


# PostgreSQL-only EverOS queue, real[] vectors, and scope isolation

@pytest.fixture
def pg_connection(postgres_url):
    from avatar.core.postgres import PostgresDatabase
    database=PostgresDatabase(postgres_url)
    try:
        with database.connect(write=True) as connection:yield connection
    finally:
        database.close()


def test_scoped_queue_claim_and_real_array_vectors(pg_connection):
    from avatar.knowledge import everos_storage

    owner_a, owner_b = 'a' * 64, 'b' * 64
    index = 'c' * 24
    project = 'build-' + 'd' * 32
    other_project = 'build-' + 'e' * 32
    everos_storage.ensure_schema(pg_connection)
    everos_storage.queue_upsert(pg_connection, (owner_a, index), project, [{'md_path':'episodes/a.md','kind':'episode','mtime':1}])
    everos_storage.queue_upsert(pg_connection, (owner_a, index), other_project, [{'md_path':'episodes/a.md','kind':'episode','mtime':2}])
    everos_storage.queue_upsert(pg_connection, (owner_b, index), project, [{'md_path':'episodes/a.md','kind':'episode','mtime':3}])
    pg_connection.raw.commit()
    claimed = everos_storage.queue_claim(pg_connection, (owner_a, index), project, limit=10)
    assert [(row['md_path'], row['kind']) for row in claimed] == [('episodes/a.md', 'episode')]
    everos_storage.replace_vectors(pg_connection, (owner_a, index), project, [{
        'kind':'episode','record_id':'record-a','group':'daily','title':'A','summary':'S','content':'A style',
        'metadata':{'synthetic':True},'embedding':[1.0,0.0,0.0],
    }])
    found = everos_storage.search_vectors(pg_connection, (owner_a, index), project, 'A style', method='hybrid', embedding=[1.0,0.0,0.0])
    assert found[0]['record_id'] == 'record-a' and found[0]['score'] > 0
    assert everos_storage.search_vectors(pg_connection, (owner_b, index), project, 'A style', method='hybrid', embedding=[1.0,0.0,0.0]) == []
    assert 'embedding' not in found[0] and 'subject_embedding' not in found[0]
    declared = pg_connection.execute("SELECT pg_typeof(embedding) AS kind FROM everos_vectors LIMIT 1").fetchone()['kind']
    assert str(declared) == 'real[]'


def test_vector_replacement_removes_only_current_project(pg_connection):
    from avatar.knowledge import everos_storage

    owner, index = 'f' * 64, '1' * 24
    project_a, project_b = 'build-' + '2' * 32, 'build-' + '3' * 32
    everos_storage.ensure_schema(pg_connection)
    base = {'kind':'episode','group':'daily','title':'T','summary':'S','content':'C','metadata':{},'embedding':[1.0,0.0]}
    everos_storage.replace_vectors(pg_connection, (owner, index), project_a, [{**base,'record_id':'a'}])
    everos_storage.replace_vectors(pg_connection, (owner, index), project_b, [{**base,'record_id':'b'}])
    everos_storage.replace_vectors(pg_connection, (owner, index), project_a, [{**base,'record_id':'new'}])
    assert [row['record_id'] for row in everos_storage.search_vectors(pg_connection, (owner, index), project_a, 'C', method='hybrid', embedding=[1.0,0.0])] == ['new']
    assert [row['record_id'] for row in everos_storage.search_vectors(pg_connection, (owner, index), project_b, 'C', method='hybrid', embedding=[1.0,0.0])] == ['b']


def test_worker_entry_has_no_sqlite_or_lancedb_startup():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / 'tools/everos/worker.py').read_text()
    for forbidden in ('SqliteLifespanProvider','LanceDBLifespanProvider','CascadeOrchestrator','memory.search','service import search'):
        assert forbidden not in source


def test_staged_vector_generation_leaves_live_index_unchanged_on_abort(pg_connection):
    from avatar.knowledge import everos_storage
    owner, index = '4' * 64, '5' * 24
    project = 'build-' + '6' * 32
    everos_storage.ensure_schema(pg_connection)
    base = {'kind':'episode','group':'daily','title':'old','summary':'old','content':'old','metadata':{},'embedding':[1.0,0.0]}
    everos_storage.replace_vectors(pg_connection,(owner,index),project,[{**base,'record_id':'old'}])
    everos_storage.vector_begin(pg_connection,(owner,index),project,'7'*32)
    everos_storage.vector_chunk(pg_connection,(owner,index),project,'7'*32,[{**base,'record_id':'staged','content':'staged'}])
    assert [row['record_id'] for row in everos_storage.search_vectors(pg_connection,(owner,index),project,'old',method='keyword')] == ['old']


def test_import_queue_preserves_retry_and_resets_processing(pg_connection):
    from avatar.knowledge import everos_storage
    owner, index = '8' * 64, '9' * 24
    project = 'build-' + 'a' * 32
    everos_storage.ensure_schema(pg_connection)
    everos_storage.import_queue_rows(pg_connection,(owner,index),project,[
        {'md_path':'episodes/failed.md','kind':'episode','change_type':'modified','lsn':9,'mtime':1,'status':'failed','retryable':True,'retry_count':2,'error':'synthetic'},
        {'md_path':'episodes/processing.md','kind':'episode','mtime':2,'status':'processing','retry_count':1,'error':''},
    ])
    rows=pg_connection.execute('SELECT md_path,status,retryable,retry_count,error,lease_until,lsn FROM everos_queue ORDER BY md_path').fetchall()
    assert [(row['md_path'],row['status'],row['retryable'],row['retry_count'],row['error'],row['lease_until'],row['lsn']) for row in rows] == [
        ('episodes/failed.md','failed',True,2,'synthetic',0.0,9),
        ('episodes/processing.md','pending',None,1,'',0.0,0),
    ]


def test_queue_keeps_done_until_file_changes_and_preserves_failed(pg_connection):
    from avatar.knowledge import everos_storage
    owner,index='a'*64,'b'*24
    project='build-'+'c'*32
    everos_storage.ensure_schema(pg_connection)
    row={'md_path':'episodes/a.md','kind':'episode','mtime':1,'change_type':'modified'}
    everos_storage.queue_upsert(pg_connection,(owner,index),project,[row]);pg_connection.raw.commit()
    claimed=everos_storage.queue_claim(pg_connection,(owner,index),project);pg_connection.raw.commit()
    everos_storage.queue_complete(pg_connection,(owner,index),project,[{'md_path':row['md_path'],'status':'done'}]);pg_connection.raw.commit()
    assert everos_storage.queue_upsert(pg_connection,(owner,index),project,[row])==0
    assert everos_storage.queue_claim(pg_connection,(owner,index),project)==[]
    everos_storage.import_queue_rows(pg_connection,(owner,index),project,[
        {'md_path':'episodes/f.md','kind':'episode','mtime':2,'status':'failed','retryable':True,'retry_count':3,'error':'network'},
        {'md_path':'episodes/p.md','kind':'episode','mtime':3,'status':'pending'},
    ])
    pg_connection.raw.commit();rows=everos_storage.queue_claim(pg_connection,(owner,index),project,limit=10);pg_connection.raw.commit()
    assert [row['md_path'] for row in rows]==['episodes/p.md']
    failed=pg_connection.execute("SELECT status,retryable,retry_count,error FROM everos_queue WHERE md_path='episodes/f.md'").fetchone()
    assert tuple(failed)==('failed',True,3,'network')


def test_keyword_hybrid_profiles_and_no_embedding_leak(pg_connection):
    from avatar.knowledge import everos_storage
    owner,index='a'*63+'1','b'*24
    project='build-'+'c'*32
    everos_storage.ensure_schema(pg_connection)
    base={'kind':'episode','group':'daily','title':'风格','summary':'说话简洁','content':'表达喜欢直接和简短',
          'metadata':{'owner_id':owner},'content_sha256':'x','md_path':'episodes/1.md'}
    everos_storage.upsert_file_vectors(pg_connection,(owner,index),project,'episodes/1.md',[
        {**base,'record_id':owner+'_ep_1','embedding':[1.0,0.0],'subject_embedding':[1.0,0.0]},
        {**base,'record_id':owner+'_ep_2','content_sha256':'y','content':'另一种叙述','embedding':[0.0,1.0],'subject_embedding':None},
    ])
    everos_storage.upsert_file_vectors(pg_connection,(owner,index),project,'user.md',[
        {'kind':'user_profile','record_id':owner,'entry_id':'','md_path':'user.md','content_sha256':'p','group':'profile','title':'画像','summary':'简洁','content':'简洁','metadata':{'owner_id':owner,'profile_data':{'summary':'简洁'}},'embedding':None,'subject_embedding':None}
    ])
    keyword=everos_storage.search_vectors(pg_connection,(owner,index),project,'表达风格',method='keyword')
    hybrid=everos_storage.search_vectors(pg_connection,(owner,index),project,'表达风格',method='hybrid',embedding=[1.0,0.0])
    assert keyword and hybrid
    assert keyword[0]['record_id']==owner+'_ep_1'
    assert all('embedding' not in row and 'subject_embedding' not in row for row in hybrid)
    profile=everos_storage.get_profile(pg_connection,(owner,index),project)
    assert profile['record_id']==owner and profile['metadata']['profile_data']['summary']=='简洁'


def test_copy_vectors_preserves_history_for_new_project(pg_connection):
    from avatar.knowledge import everos_storage
    owner,index='f'*64,'e'*24;source='build-'+'1'*32;target='build-'+'2'*32
    everos_storage.ensure_schema(pg_connection)
    record={'kind':'episode','record_id':owner+'_ep_1','entry_id':'ep_1','md_path':'episodes/a.md','content_sha256':'same','group':'daily','title':'T','summary':'S','content':'C','metadata':{},'embedding':[1.0,2.0],'subject_embedding':[2.0,1.0]}
    everos_storage.upsert_file_vectors(pg_connection,(owner,index),source,'episodes/a.md',[record])
    assert everos_storage.copy_vectors(pg_connection,(owner,index),source,target)==1
    copied=everos_storage.existing_vectors(pg_connection,(owner,index),target)[0]
    assert copied['embedding']==[1.0,2.0] and copied['subject_embedding']==[2.0,1.0] and copied['record_id']==owner+'_ep_1'
    assert copied['group_name']=='daily'
    assert everos_storage.copy_vectors(pg_connection,(owner,index),source,target)==1
    rows=[{**record,'record_id':f'v-{n:03}','embedding':[1.0,0.0],'subject_embedding':None} for n in range(65)]
    rows.extend([{**record,'record_id':'z-best','embedding':[0.0,1.0],'subject_embedding':None},
                 {**record,'record_id':'wrong-dim','embedding':[1.0],'subject_embedding':None}])
    everos_storage.upsert_file_vectors(pg_connection,(owner,index),target,'episodes/a.md',rows)
    results=everos_storage.search_vectors(pg_connection,(owner,index),target,'',method='vector',embedding=[0,1],limit=3)
    assert [row['record_id'] for row in results]==['z-best','v-000','v-001']


def test_keyword_works_without_embeddings_and_hybrid_uses_dense_history(pg_connection):
    from avatar.knowledge import everos_storage
    owner,index='0'*63+'1','2'*24;project='build-'+'3'*32
    everos_storage.ensure_schema(pg_connection)
    everos_storage.upsert_file_vectors(pg_connection,(owner,index),project,'episodes/a.md',[
        {'kind':'episode','record_id':owner+'_a','entry_id':'a','md_path':'episodes/a.md','content_sha256':'a','group':'daily','title':'口语','summary':'简短','content':'吃饭了吗？','metadata':{},'embedding':None,'subject_embedding':None},
        {'kind':'episode','record_id':owner+'_b','entry_id':'b','md_path':'episodes/a.md','content_sha256':'b','group':'daily','title':'正式','summary':'long','content':'Please provide a formal answer.','metadata':{},'embedding':[0.0,1.0],'subject_embedding':[0.0,1.0]},
    ])
    keyword=everos_storage.search_vectors(pg_connection,(owner,index),project,'口语',method='keyword')
    hybrid=everos_storage.search_vectors(pg_connection,(owner,index),project,'formal answer',method='hybrid',embedding=[0.0,1.0])
    assert keyword[0]['record_id']==owner+'_a'
    assert hybrid[0]['record_id']==owner+'_b'


def _worker_vector(text):
    import hashlib
    digest=hashlib.sha256(text.encode()).digest()
    vector=[float(digest[i])+1.0 for i in range(8)]
    scale=sum(value*value for value in vector)**0.5
    return [value/scale for value in vector]


def _storage_reply(conn,message):
    from avatar.knowledge import everos_storage
    operation=message['operation'];scope=(message['owner'],message['index_signature']);project=message['project']
    if operation=='ensure':everos_storage.ensure_schema(conn)
    elif operation=='queue_sync':everos_storage.queue_sync(conn,scope,project,message['rows'])
    elif operation=='queue_claim':return {'rows':everos_storage.queue_claim(conn,scope,project,message.get('limit',50))}
    elif operation=='queue_complete':everos_storage.queue_complete(conn,scope,project,message['rows'])
    elif operation=='existing_vectors':return {'rows':everos_storage.existing_vectors(conn,scope,project,message.get('md_path'))}
    elif operation=='has_episode_vectors':return {'ready':everos_storage.has_episode_vectors(conn,scope,project)}
    elif operation=='vector_begin':everos_storage.vector_begin(conn,scope,project,message['generation'])
    elif operation=='vector_chunk':everos_storage.vector_chunk(conn,scope,project,message['generation'],message['records'])
    elif operation=='vector_commit_file':everos_storage.vector_commit_file(conn,scope,project,message['generation'],message['md_path'])
    elif operation=='upsert_file':everos_storage.upsert_file_vectors(conn,scope,project,message['md_path'],message['records'])
    elif operation=='delete_file':everos_storage.delete_file_vectors(conn,scope,project,message['md_path'])
    elif operation=='copy_vectors':everos_storage.copy_vectors(conn,scope,message['source'],project)
    elif operation=='search':return {'rows':everos_storage.search_vectors(conn,scope,project,message['query'],message.get('method','keyword'),message.get('embedding'),message.get('limit',6),message.get('include_profile',False),message.get('query_tokens'))}
    elif operation=='profile':return {'row':everos_storage.get_profile(conn,scope,project)}
    else:raise AssertionError('unexpected storage operation '+operation)
    conn.raw.commit();return {}


def _run_worker(conn,tmp_path,payload,embedding_calls):
    import json,subprocess,sys
    from pathlib import Path
    from tools.everos import worker
    command=[sys.executable,'-I',str(Path(worker.__file__))]
    process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        process.stdin.write(json.dumps(payload)+'\n');process.stdin.flush()
        while line:=process.stdout.readline():
            message=json.loads(line)
            if message['type']=='done':return message['result']
            if message['type']=='error':
                process.stdin.close();process.wait(timeout=45)
                raise AssertionError(json.dumps(message)+'\n'+process.stderr.read()[-3000:])
            if message['type']=='embed':
                embedding_calls.extend(message['texts'])
                reply={'result':[_worker_vector(text) for text in message['texts']]}
            elif message['type']=='pg':reply={'result':_storage_reply(conn,message)}
            else:raise AssertionError('unexpected RPC '+message['type'])
            process.stdin.write(json.dumps(reply)+'\n');process.stdin.flush()
        raise AssertionError(process.stderr.read()[-3000:])
    finally:
        process.stdin.close();process.wait(timeout=45);process.stdout.close();process.stderr.close()


def test_actual_build_pg_e2e_reuses_vectors_and_restarts(pg_connection,tmp_path):
    from everos.core.persistence import MemoryRoot
    from tools.everos import worker
    owner='7'*64;project='build-'+'8'*32;index='9'*24
    payload={'owner':owner,'project':project,'root':str(tmp_path),'source_id':'a'*64,
             'dimensions':8,'embedding_model':'synthetic','index_signature':index,
             'embedding_enabled':True,'media':[{'filename':'sample.txt','caption':'synthetic','marker':'fixture'}],
             'evidence':{'new_style_observations':['concise']}}
    files=worker.ProjectFiles(MemoryRoot(tmp_path),owner,project)
    files.save_result(payload,{'Subject':'合成主题','Summary':'合成摘要','Content':'我通常会简短直接地表达。'},'episode')
    files.save_result(payload,{'id':'profile_'+owner,'user_id':owner,'summary':'简短直接','profile_timestamp_ms':1,
                                'implicit_traits':[],'explicit_info':[]},'profile')
    first_calls=[];first=_run_worker(pg_connection,tmp_path,payload,first_calls)
    assert first['method']=='hybrid'
    assert first['memories']['episodes'] and first['memories']['profiles']
    assert all(row['id'].startswith(owner+'_') for row in first['memories']['episodes'])
    assert first['memories']['profiles'][0]['id']==owner
    assert all('embedding' not in row for row in first['memories']['episodes'])
    vector_rows=pg_connection.execute('SELECT kind,record_id,content_sha256,tokens,embedding,subject_embedding FROM everos_vectors ORDER BY record_id').fetchall()
    assert vector_rows and all(row['embedding'] is not None for row in vector_rows if row['kind']!='user_profile')
    assert all(row['tokens'] for row in vector_rows if row['kind']!='user_profile')
    files.state.joinpath('result.json').unlink()
    second_calls=[];second=_run_worker(pg_connection,tmp_path,payload,second_calls)
    assert second==first
    assert len(second_calls) <= 1  # query embedding only; all entry vectors were reused
    assert not list(tmp_path.rglob('*.db')) and not list(tmp_path.rglob('*.sqlite3'))


def test_incremental_embedding_reuse_and_missing_history_repair():
    from tools.everos import worker
    unchanged={'kind':'episode','record_id':'owner_ep','content_sha256':'same','title':'subject','content':'content',
               'embedding':'old-body','subject_embedding':'old-subject'}
    record={'kind':'episode','record_id':'owner_ep','content_sha256':'same','title':'subject','content':'content',
            'embedding':None,'subject_embedding':None}
    assert worker._reuse_embeddings([record],[unchanged],True)==([],[])
    missing={**unchanged,'embedding':None,'subject_embedding':None}
    changed,to_embed=worker._reuse_embeddings([record],[missing],True)
    assert changed==[record] and to_embed==[record]
    no_subject={**unchanged,'subject_embedding':None}
    changed,to_embed=worker._reuse_embeddings([record],[no_subject],True)
    assert changed==[record] and to_embed==[record]


def test_import_preserves_all_queue_timestamps(pg_connection):
    from avatar.knowledge import everos_storage
    owner,index='d'*64,'e'*24;project='build-'+'f'*32
    everos_storage.ensure_schema(pg_connection)
    everos_storage.import_queue_rows(pg_connection,(owner,index),project,[{
        'md_path':'episodes/a.md','kind':'episode','change_type':'modified','lsn':7,'mtime':1.5,
        'status':'failed','retryable':True,'retry_count':2,'error':'synthetic',
        'first_seen_at':'2026-01-01T00:00:00+00:00','last_changed_at':'2026-01-02T00:00:00+00:00',
        'last_attempt_at':'2026-01-03T00:00:00+00:00',
    }]);pg_connection.raw.commit()
    row=pg_connection.execute('SELECT first_seen_at,last_changed_at,last_attempt_at,change_type,lsn,status,retryable,retry_count FROM everos_queue').fetchone()
    assert tuple(row)==('2026-01-01T00:00:00+00:00','2026-01-02T00:00:00+00:00',
                        '2026-01-03T00:00:00+00:00','modified',7,'failed',True,2)


def test_archive_rows_blobs_and_source_summary_round_trip(pg_connection):
    from avatar.knowledge import everos_storage
    owner,index='a'*63+'2','b'*24
    everos_storage.ensure_schema(pg_connection)
    everos_storage.archive_rows(pg_connection,(owner,index),'memorize_inbox',[{
        'source_row_id':'1','row_data':{'status':'success','attempt':1},'digest':'r1'}],'schema-v1')
    payload=b'opaque-lance-index-bytes'
    everos_storage.archive_blob(pg_connection,(owner,index),'lance-index','idx',payload,'application/octet-stream')
    everos_storage.archive_source(pg_connection,(owner,index),'memorize_inbox','schema-v1',1,'source-digest')
    pg_connection.raw.commit()
    assert everos_storage.load_blob(pg_connection,(owner,index),'lance-index','idx')['payload']==payload
    rows=pg_connection.execute("SELECT source_row_id,row_data,digest FROM everos_archive_rows WHERE source_table='memorize_inbox' ORDER BY source_row_id").fetchall()
    assert rows[0]['row_data']=={'attempt':1,'status':'success'} and rows[0]['digest']=='r1'
    assert rows[1]['source_row_id']=='__source__' and rows[1]['row_data']['source_count']==1


def test_actual_build_without_embeddings_stays_keyword_only(pg_connection,tmp_path):
    from everos.core.persistence import MemoryRoot
    from tools.everos import worker
    owner='5'*64;project='build-'+'6'*32;index='4'*24
    payload={'owner':owner,'project':project,'root':str(tmp_path),'source_id':'3'*64,
             'dimensions':8,'embedding_model':'keyword-only','index_signature':index,
             'embedding_enabled':False,'media':[],
             'evidence':{'new_style_observations':['direct']}}
    files=worker.ProjectFiles(MemoryRoot(tmp_path),owner,project)
    files.save_result(payload,{'Subject':'直接表达','Summary':'简短','Content':'我说话比较直接。'},'episode')
    files.save_result(payload,{'id':'profile_'+owner,'user_id':owner,'summary':'直接','profile_timestamp_ms':1,
                                'implicit_traits':[],'explicit_info':[]},'profile')
    embedding_calls=[];result=_run_worker(pg_connection,tmp_path,payload,embedding_calls)
    assert embedding_calls==[]
    assert result['method']=='keyword'
    rows=[tuple(row) for row in pg_connection.execute('SELECT kind,tokens,embedding FROM everos_vectors ORDER BY kind').fetchall()]
    assert result['memories']['episodes'] and result['memories']['profiles'], (result,rows)
    assert pg_connection.execute('SELECT count(*) FROM everos_vectors WHERE embedding IS NOT NULL').fetchone()[0]==0


def test_parent_request_hashes_raw_user_id_before_pg_scope(pg_database,tmp_path):
    import asyncio
    import hashlib
    from everos.core.persistence import MemoryRoot
    from avatar.knowledge.build_worker import SoulBuildWorker
    from avatar.knowledge.service import KnowledgeService
    from tools.everos import worker

    raw_uid='stable-user-id'
    owner=hashlib.sha256(raw_uid.encode()).hexdigest()
    project='build-'+'b'*32
    with pg_database.connect(write=True) as db:
        db.execute("INSERT INTO users(id,username,password,role,created_at) VALUES(?,'synthetic','unused','user','2026')",(raw_uid,))
        db.execute("""INSERT INTO soul_builds(id,user_id,prompt,asset_ids,profile_snapshot,profile_revision,status,stage,created_at,updated_at)
          VALUES('parent-request',?,'','[]','{}','','running','','2026','2026')""",(raw_uid,))
    (tmp_path/'users'/raw_uid/'everos').mkdir(parents=True)
    payload={'root':str((tmp_path/'users'/raw_uid/'everos').resolve()),'owner':owner,'project':project,'source_id':'c'*64,
             'dimensions':8,'embedding_model':'keyword-only','index_signature':'d'*24,
             'embedding_enabled':False,'media':[],'evidence':{'prompt':'本人表达风格、语气、措辞、句式、习惯和最新修正'}}
    files=worker.ProjectFiles(MemoryRoot(payload['root']),owner,project)
    files.save_result(payload,{'Subject':'本人表达风格','Summary':'语气措辞句式习惯的最新修正','Content':'本人表达风格、语气、措辞、句式、习惯和最新修正。'},'episode')
    workspace = KnowledgeService(pg_database, None)
    runner = SoulBuildWorker(workspace)
    bridge = workspace.memory
    row={'id':'parent-request','user_id':raw_uid}
    result=asyncio.run(bridge.request(payload,row=row,fingerprint='synthetic-fingerprint',worker=runner))
    assert result['memories']['episodes']
    with pg_database.connect() as db:
        assert db.execute('SELECT count(*) FROM everos_queue WHERE owner_hash=?',(owner,)).fetchone()[0]>0
        assert db.execute('SELECT count(*) FROM everos_queue WHERE owner_hash=?',(raw_uid,)).fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM everos_vectors WHERE owner_hash=?',(owner,)).fetchone()[0]>0
        assert db.execute("SELECT memory_fingerprint FROM soul_builds WHERE id='parent-request'").fetchone()[0]=='synthetic-fingerprint'


def test_cancel_during_claim_drains_transaction_and_recovers_job(monkeypatch):
    import threading
    from types import SimpleNamespace
    from unittest.mock import Mock
    from avatar.knowledge.build_worker import SoulBuildWorker

    runner=SoulBuildWorker(SimpleNamespace(database=None,chat=None,memory=None,learning=None,file=None,decode=None))
    row={'id':'job','user_id':'owner'}
    failure=Mock()
    monkeypatch.setattr(runner,'_fail',failure)
    async def cancel():
        loop=asyncio.get_running_loop()
        started=asyncio.Event()
        release=threading.Event()
        def claim(identifier,owner):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(3)
            return row,None,None
        monkeypatch.setattr(runner,'_claim',claim)
        job=asyncio.create_task(runner.once('job',user_id='owner'))
        try:
            await asyncio.wait_for(started.wait(),2)
            job.cancel()
            await asyncio.sleep(0)
            assert not job.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):await job
        failure.assert_called_once_with(row,'构建中断','构建中断；未自动重试收费请求')
    asyncio.run(cancel())


def test_legacy_soul_visual_refresh_reads_owned_word_derivative(tmp_path):
    from types import SimpleNamespace
    from PIL import Image
    from avatar.core.execution import run_blocking
    from avatar.knowledge.model_refresh import has_visual_input

    path=tmp_path/'original.blob'
    path.write_bytes(b'word original')
    Image.new('RGB',(20,20),'white').save(path.with_suffix('.word-vector.pdf'))
    async def execute(user,stage,call,*args):
        return await run_blocking(call,*args)
    asset={'filename':'document.docx','mime':'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}
    def owned_file(user,identifier):
        assert (user,identifier)==('owner','original')
        return path,asset
    service=SimpleNamespace(asset=lambda *args:asset,file=owned_file,
                            chat=SimpleNamespace(rag=SimpleNamespace(work=SimpleNamespace(execute=execute))))
    assert asyncio.run(has_visual_input(service,{'user_id':'owner','asset_ids':['original'],'notes':[]}))


@pytest.mark.parametrize('operation', ('learn', 'build', 'sync', 'visual_refresh', 'unexpected_error'))
def test_soul_worker_uses_real_workspace_and_everos(context, monkeypatch, caplog, operation):
    from fastapi import HTTPException

    from avatar.core.db import dump, now
    from avatar.knowledge import model_refresh
    from avatar.knowledge.soul_document import FIELDS
    from avatar.models.client import ModelResult
    from tests.support import MODEL

    app, client, _ = context
    database, knowledge, runner = app.state.database, app.state.knowledge, app.state.soul_builds
    actor = client.get('/api/auth/me').json()['user']
    uid = actor['id']
    assert client.put('/api/me/model-settings', json={'model_config_input': MODEL}).status_code == 200
    document = dict.fromkeys(FIELDS, '') | {'voice': '平实'}
    traits = {'length': 'short', 'tones': ['direct']}
    calls = set()
    fail_episode = operation == 'learn'

    async def generate(config, key, system, history, message):
        nonlocal fail_episode
        if operation == 'unexpected_error':
            raise RuntimeError('synthetic-private-details')
        if config.get('_tools'):
            calls.add('profile')
            return ModelResult('', None, tool_calls=({
                'name': 'write_style_profile',
                'arguments': {'explicit_info': [], 'implicit_traits': [{'traits': traits}]},
            },))
        if '聊天风格特征提取器' in system:
            assert 'observations' not in calls
            calls.add('observations')
            return ModelResult(dump({'observations': [traits]}), None)
        if 'EverOS 记忆整理器' in system:
            if fail_episode:
                fail_episode = False
                raise HTTPException(502, 'fixture episode failure')
            calls.add('episode')
            return ModelResult(dump({'title': '表达习惯', 'summary': '先说结论', 'content': '表达直接且简短。'}), None)
        assert '构建分身 soul' in system
        calls.add('draft')
        return ModelResult(dump(document), None)

    async def vectors(model, config, key, inputs, *, retry_uncertain):
        assert not retry_uncertain
        calls.add('embed')
        return [[1.0] + [0.0] * (config['dimensions'] - 1) for _ in inputs], {'total_tokens': len(inputs)}

    monkeypatch.setattr(app.state.chat.model_client, 'generate', generate)
    monkeypatch.setattr('avatar.knowledge.everos_memory.embed', vectors)

    stamp = now()
    with database.connect(write=True) as db:
        db.execute("""INSERT INTO avatar_records(user_id,aic,certificate,profile,last_modified_at,created_at,updated_at)
                   VALUES(?,'soul-test-aic','{}','{}',?,?,?)""", (uid, stamp, stamp, stamp))
        if operation in ('sync', 'visual_refresh'):
            db.execute("""INSERT INTO knowledge_assets(id,user_id,filename,mime,size,sha256,kind,created_at,relative_path)
                       VALUES('image',?,'sample.png','image/png',1,'synthetic-image','image',?,'sample.png')""", (uid, stamp))
        identifier = 'initial'
        queued = operation in ('build', 'unexpected_error')
        db.execute("""INSERT INTO soul_builds(id,user_id,prompt,asset_ids,profile_snapshot,profile_revision,status,stage,created_at,updated_at,document)
                      VALUES('initial',?,'采用简短表达',?,'{}',?,?,'',?,?,?)""",
                   (uid, dump(['image'] if operation == 'visual_refresh' else []), stamp,
                    'queued' if queued else 'approved', stamp, stamp, dump(document)))
        if not queued:
            db.execute("INSERT INTO active_souls VALUES(?,'initial')", (uid,))
        if operation == 'learn':
            db.execute("""INSERT INTO chat_conversations(id,user_id,channel,target_aic,caller_id,platform,title,created_at,updated_at)
                       VALUES('owned',?,'assistant','soul-test-aic',?,'web','样本',?,?)""", (uid, uid, stamp, stamp))
            learned_through = db.execute("""INSERT INTO chat_messages(user_id,channel,role,content,created_at,conversation_id)
                                            VALUES(?,'assistant','user','好的，先讲结论。',?,'owned')""", (uid, stamp)).lastrowid
        elif operation == 'sync':
            db.execute("INSERT INTO knowledge_asset_updates(user_id,revision,updated_at) VALUES(?,'r1','2000-01-01T00:00:00+00:00')",
                       (uid,))
        elif operation == 'visual_refresh':
            identifier = model_refresh.schedule(db, uid, visual=True)
            assert identifier

    # Only the loop running the worker is checked; TestClient owns a separate thread.
    import threading
    worker_thread=threading.get_ident()
    connect=database.connect
    def off_loop(*args,**kwargs):
        if threading.get_ident()==worker_thread:
            try:asyncio.get_running_loop()
            except RuntimeError:pass
            else:raise AssertionError('Soul worker opened a DB connection on its event loop')
        return connect(*args,**kwargs)
    monkeypatch.setattr(database,'connect',off_loop)

    if operation == 'sync':
        assert [r['user_id'] for r in runner._pending('[]',10)]==[uid]
        assert not asyncio.run(runner.once('unrelated'))
        with database.connect() as db:
            assert db.execute('SELECT count(*) FROM knowledge_asset_updates WHERE user_id=?', (uid,)).fetchone()[0] == 0
            revision = db.execute("SELECT parent_build,status,asset_ids FROM soul_builds WHERE user_id=? AND origin='refine'",
                                  (uid,)).fetchone()
            assert tuple(revision) == ('initial', 'queued', '["image"]')
        return

    if operation == 'learn':
        failed = knowledge.learning.queue(actor, 'owned')['id']
        assert asyncio.run(runner.once(failed))
        with database.connect() as db:
            assert tuple(db.execute('SELECT status,error FROM soul_builds WHERE id=?', (failed,)).fetchone()) == (
                'failed', 'fixture episode failure')
            assert db.execute("SELECT last_message_id FROM style_learning_settings WHERE conversation_id='owned'").fetchone()[0] == 0
            assert db.execute('SELECT build_id FROM active_souls WHERE user_id=?', (uid,)).fetchone()[0] == 'initial'
        identifier = knowledge.learning.retry(actor)['id']

    assert asyncio.run(runner.once(identifier))
    with database.connect() as db:
        result = db.execute('SELECT status,phase,progress,memory_fingerprint,error FROM soul_builds WHERE id=?',
                            (identifier,)).fetchone()
        if operation == 'unexpected_error':
            assert result['status'] == 'failed'
            assert 'synthetic-private-details' not in result['error'] + caplog.text
            assert identifier in caplog.text and 'RuntimeError' in caplog.text and 'Traceback' in caplog.text
            return
        assert result['status'] == ('pending' if operation == 'build' else 'approved'), result['error']
        assert result['phase'] == 'draft' and result['progress'] == 100 and result['memory_fingerprint']
        if operation in ('learn', 'visual_refresh'):
            assert db.execute('SELECT build_id FROM active_souls WHERE user_id=?', (uid,)).fetchone()[0] == identifier
        if operation == 'learn':
            assert db.execute("SELECT last_message_id FROM style_learning_settings WHERE conversation_id='owned'").fetchone()[0] == learned_through
            assert calls == {'observations', 'episode', 'profile', 'embed'}
        elif operation == 'build':
            assert calls == {'episode', 'embed', 'draft'}
