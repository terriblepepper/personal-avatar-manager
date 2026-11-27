"""资料检索、索引发布、PostgreSQL 向量与历史索引导入。"""
import asyncio
from dataclasses import replace

import pytest

from avatar.core.resource_budget import ResourceController, ResourcePolicy, ResourceSample
from avatar.rag.query import FileQuery
from tests.support import upload


# RAG lifecycle, resource isolation and retrieval contracts; no paid API calls

GiB=1024**3


def quiet(**changes):
    return replace(ResourceSample(cpu_percent=10,cpu_cores=80,total_bytes=94*GiB,
        available_bytes=70*GiB,process_bytes=GiB),**changes)


def test_adaptive_budget_respects_host_and_parent_limits():
    policy=ResourcePolicy(max_jobs=16,max_cpu_cores=8,max_memory_gib=16,cache_gib=8)
    controller=ResourceController(policy,sampler=lambda:quiet())
    initial=controller.status()['limits']['index']
    sample=quiet(quota_cpu_cores=4,quota_memory_bytes=8*GiB)
    for _ in range(20):controller.update(sample)
    state=controller.status()
    assert initial<state['limits']['index']<=policy.max_jobs-policy.query_reserved
    assert state['effective_cpu_cores']==4 and state['effective_memory_bytes']==8*GiB
    before=state['limits']['index']
    controller.update(replace(sample,cpu_percent=95,available_bytes=2*GiB,process_bytes=7*GiB))
    pressured=controller.status()
    assert pressured['limits']['index']<before
    assert pressured['cache_budget_bytes']<=state['cache_budget_bytes']
    assert pressured['pressure']


def test_index_admission_preserves_query_capacity_and_cancelled_waiters():
    async def check():
        controller=ResourceController(ResourcePolicy(max_jobs=2,query_reserved=1),sampler=lambda:quiet())
        gate=asyncio.Event();started=asyncio.Event()
        async def indexing(owner):
            async with controller.slot(owner,'index'):
                started.set();await gate.wait()
        first=asyncio.create_task(indexing('A'));await started.wait();started.clear()
        second=asyncio.create_task(indexing('B'));await asyncio.sleep(0)
        async with asyncio.timeout(.3):
            async with controller.slot('C','query'):assert controller.status()['active_jobs']==2
        assert not started.is_set()
        second.cancel();await asyncio.gather(second,return_exceptions=True)
        gate.set();await first
        assert controller.status()['active_jobs']==0
        assert controller.status()['waiting']==0
    asyncio.run(check())


def test_nested_slots_and_blocked_owner_do_not_block_another_owner():
    async def check():
        controller=ResourceController(ResourcePolicy(max_jobs=4,query_reserved=1,max_index_per_user=1),sampler=lambda:quiet())
        gate=asyncio.Event();ready=asyncio.Event()
        async def hold():
            async with controller.slot('A','index'):
                async with controller.slot('A','index'):
                    async with controller.slot('A','embedding'):
                        ready.set();await gate.wait()
        async def blocked():
            async with controller.slot('A','index'):pass
        first=asyncio.create_task(hold());await ready.wait()
        second=asyncio.create_task(blocked());await asyncio.sleep(0)
        async with asyncio.timeout(.3):
            async with controller.slot('B','index'):pass
        second.cancel();await asyncio.gather(second,return_exceptions=True)
        gate.set();await first
        assert controller.status()['active_jobs']==0
    asyncio.run(check())


def test_resource_settings_are_admin_only_and_save_atomically(context):
    app,user,admin=context
    assert user.get('/api/admin/rag-resources').status_code==403
    original=admin.get('/api/admin/rag-resources').json()['policy']
    result=admin.put('/api/admin/rag-resources',json={**original,'max_cpu_cores':6})
    assert result.status_code==200,result.text
    assert result.json()['policy']['max_cpu_cores']==6
    invalid=admin.put('/api/admin/rag-resources',json={**original,'max_jobs':0})
    assert invalid.status_code==422
    state=admin.get('/api/admin/rag-resources').json()
    assert state['policy']['max_cpu_cores']==6
    assert state['enforcement']['mode']=='soft'
    assert 'encrypted_key' not in str(state)


def test_failed_os_limit_change_keeps_previous_settings(context,monkeypatch):
    from fastapi import HTTPException
    import avatar.api.resource_routes as routes
    app,_,admin=context
    original=admin.get('/api/admin/rag-resources').json()['policy']
    def denied(*args):raise HTTPException(503,'simulated systemd permission denial')
    monkeypatch.setattr(routes.ServiceLimits,'apply',denied)
    response=admin.put('/api/admin/rag-resources',json={**original,'enforce_systemd':True})
    assert response.status_code==503
    state=admin.get('/api/admin/rag-resources').json()
    assert state['policy']==original and state['enforcement']['mode']=='soft'


def test_small_parent_cpu_quota_still_allows_background_progress():
    controller=ResourceController(sampler=lambda:quiet(quota_cpu_cores=.5,quota_memory_bytes=2*GiB))
    for _ in range(5):controller.update(quiet(quota_cpu_cores=.5,quota_memory_bytes=2*GiB))
    assert controller.status()['limits']['index']>=1


def test_os_enforcement_refuses_to_change_an_unrelated_service(monkeypatch):
    import os
    from types import SimpleNamespace
    from fastapi import HTTPException
    import avatar.core.resource_limits as limits
    calls=[]
    def run(command,**kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0,stdout=str(os.getpid()+1),stderr='')
    monkeypatch.setattr(limits.subprocess,'run',run)
    with pytest.raises(HTTPException):limits.ServiceLimits().apply(ResourcePolicy(enforce_systemd=True),quiet())
    assert all('set-property' not in command for command in calls)


def test_index_dispatch_is_globally_bounded_and_fair(pg_database):
    from avatar.rag.indexing.service import FileRag

    users = [f"owner-{index:03}" for index in range(100)]
    with pg_database.connect(write=True) as db:
        db.executemany(
            "INSERT INTO users(id,username,password,role,created_at) VALUES(?,?,'hash','user','2026-01-01')",
            [(uid, uid) for uid in users],
        )
        db.executemany(
            "INSERT INTO file_rag_documents(source,file_id,user_id,status,updated_at) "
            "VALUES('profile',?,?,'queued','2026-01-01')",
            [(f"{uid}-{file_index}", uid) for uid in users for file_index in range(10)],
        )

    async def check():
        controller=ResourceController(ResourcePolicy(max_jobs=8,query_reserved=2),sampler=lambda:quiet())
        async def wait(row,lease=None):await asyncio.Event().wait()
        rag=FileRag.__new__(FileRag)
        rag.database, rag.running, rag.resources, rag.queued_index = pg_database, {}, controller, wait
        pending={}
        try:
            await rag.dispatch(pending)
            assert 1<len(pending)<=controller.status()['limits']['index']
            assert len({row[2] for row in pending.values()})==len(pending)
        finally:
            for task in pending:task.cancel()
            await asyncio.gather(*pending,return_exceptions=True)
    asyncio.run(check())


def test_query_budget_is_independent_of_busy_account_index_slots():
    from avatar.rag.indexing.work import AccountIndexWork
    async def check():
        controller=ResourceController(ResourcePolicy(max_jobs=4,query_reserved=1),sampler=lambda:quiet())
        work=AccountIndexWork(controller);gate=asyncio.Event();ready=[]
        async def index():
            async with work.slot('A'):
                ready.append(1);await gate.wait()
        tasks=[asyncio.create_task(index()) for _ in range(2)]
        try:
            for _ in range(100):
                if len(ready)==2:break
                await asyncio.sleep(.001)
            assert len(ready)==2
            async with asyncio.timeout(.3):
                async with work.slot('A','query'):pass
        finally:
            gate.set();await asyncio.gather(*tasks)
    asyncio.run(check())


def test_index_consumer_survives_unexpected_task_failure(context,monkeypatch):
    from unittest.mock import Mock
    from tests.support import saved_file
    app,client,_=context;rag=app.state.chat.rag
    startup = Mock(side_effect=[ConnectionError('database starting'), None])
    recover = rag.recover_queue
    def flaky_recovery():
        startup()
        recover()
    monkeypatch.setattr(rag, 'recover_queue', flaky_recovery)
    bad=saved_file(client,'bad.txt',b'bad');good=saved_file(client,'good.txt',b'good')
    async def index(source,identifier,uid):
        if identifier==bad:raise RuntimeError('simulated isolated parser failure')
        with app.state.database.connect(write=True) as db:
            db.execute("UPDATE file_rag_documents SET status='ready' WHERE file_id=?",(identifier,))
        return True
    monkeypatch.setattr(rag,'index',index)
    async def check():
        worker=asyncio.create_task(rag.run())
        try:
            for _ in range(400):
                with app.state.database.connect() as db:
                    states=dict(db.execute('SELECT file_id,status FROM file_rag_documents'))
                if states.get(good)=='ready' and states.get(bad)=='failed':break
                if worker.done():break
                await asyncio.sleep(.02)
            assert not worker.done() and startup.call_count == 2
            assert states[good]=='ready' and states[bad]=='failed'
        finally:
            worker.cancel();await asyncio.gather(worker,return_exceptions=True)
    asyncio.run(check())


def test_soul_progress_is_isolated_between_concurrent_builds(context):
    app,_,_=context;service=app.state.soul_builds
    async def check():
        ready=asyncio.Event();release=asyncio.Event();values=[]
        async def first():
            service.percent,service.stage=12,'first'
            ready.set();await release.wait();values.append((service.percent,service.stage))
        async def second():
            await ready.wait();service.percent,service.stage=65,'second';release.set()
        await asyncio.gather(first(),second())
        assert values==[(12,'first')]
    asyncio.run(check())


def insert_soul(database,identifier,uid,status='queued'):
    from avatar.core.db import now
    with database.connect(write=True) as db:
        db.execute("INSERT INTO soul_builds(id,user_id,prompt,asset_ids,profile_snapshot,profile_revision,status,stage,created_at,updated_at) VALUES(?,?,?,'[]','{}','',?,'',?,?)",(identifier,uid,'test',status,now(),now()))


def test_soul_consumer_starts_other_users_without_waiting_for_first(context,monkeypatch):
    from avatar.core.execution import run_blocking
    app,client,admin=context;service=app.state.soul_builds
    service.chat.rag.resources.update(quiet())
    ids=[client.get('/api/auth/me').json()['user']['id'],admin.get('/api/auth/me').json()['user']['id']]
    for i,uid in enumerate(ids):insert_soul(app.state.database,'parallel-'+str(i),uid)
    async def check():
        gate=asyncio.Event();both_started=asyncio.Event();started=[]
        def transition(identifier,user_id,status):
            with app.state.database.connect(write=True,account_scope=user_id) as db:
                assert db.execute('UPDATE soul_builds SET status=? WHERE id=? AND user_id=?',
                                  (status,identifier,user_id)).rowcount==1
        async def once(identifier,*,user_id):
            await run_blocking(transition,identifier,user_id,'running')
            started.append((identifier,user_id))
            if len(started)==2:both_started.set()
            await gate.wait()
            await run_blocking(transition,identifier,user_id,'ready')
            return True
        monkeypatch.setattr(service,'once',once);monkeypatch.setattr(service.learning,'schedule',lambda:None)
        worker=asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(both_started.wait(),5)
            assert set(started)=={('parallel-0',ids[0]),('parallel-1',ids[1])}
        finally:
            gate.set();worker.cancel();await asyncio.gather(worker,return_exceptions=True)
    asyncio.run(check())


def test_soul_material_analysis_uses_bounded_workers(context,monkeypatch):
    app,client,_=context;service=app.state.soul_builds;uid=client.get('/api/auth/me').json()['user']['id']
    insert_soul(app.state.database,'materials',uid,status='running')
    async def check():
        active=0;peak=0;gate=asyncio.Event()
        async def analyse(row,identifier,*args):
            nonlocal active,peak
            active+=1;peak=max(peak,active);await gate.wait();active-=1
            return {'id':identifier},['observation']
        monkeypatch.setattr(service,'analyse_asset',analyse)
        task=asyncio.create_task(service.analyse_assets({'id':'materials','user_id':uid,'asset_ids':[str(i) for i in range(50)]},{},'',{},[],[]))
        try:
            await asyncio.sleep(.05);assert 0<peak<=10
        finally:gate.set();await asyncio.gather(task,return_exceptions=True)
    asyncio.run(check())


class MemoryVectors:
    from avatar.rag.stores.postgres import PostgresFiles
    pack=staticmethod(PostgresFiles.pack)
    def __init__(self):self.data={}
    def write(self,uid,collection,records,vectors):
        for identifier,meta,text in zip(*records,strict=True):
            self.data[(uid,collection,identifier)]={'id':identifier,'text':text,'metadata':meta}
    def contains(self,uid,collection,ids,connection=None):return all((uid,collection,identifier) in self.data for identifier in ids)
    def records(self,uid,collection,ids):return [self.data[(uid,collection,i)] for i in ids if (uid,collection,i) in self.data]
    def delete_in(self,db,uid,collection,ids):
        for identifier in ids:self.data.pop((uid,collection,identifier),None)
    def close(self):pass


def prepared_index(context,monkeypatch):
    from tests.support import saved_file
    app,client,_=context;rag=app.state.chat.rag;uid=client.get('/api/auth/me').json()['user']['id']
    fid=saved_file(client,'agent-notes.txt',b'agent evidence')
    store=MemoryVectors();monkeypatch.setattr(rag,'store',store)
    rag.resources.update(quiet())
    async def prepare(row,*args):
        return [{'page':1,'text':'智能体协议 agent development ACPS','modality':'text','section':'协议'},
                {'page':2,'text':'retry_request handles HTTP timeout','modality':'text','section':'代码'}],'native_text'
    async def vectors(uid,config,inputs,**kwargs):return [[1.0]+[0.0]*63 for _ in inputs]
    monkeypatch.setattr(rag,'prepare',prepare);monkeypatch.setattr(rag,'vectors',vectors)
    return rag,uid,fid,store


def test_search_materials_returns_exact_query_evidence_and_notices(context, monkeypatch):
    from fastapi import HTTPException
    from unittest.mock import Mock
    from tests.support import saved_file

    app, client, _ = context
    rag = app.state.chat.rag
    owner = client.get('/api/auth/me').json()['user']['id']
    good = saved_file(client, 'retry.txt', b'Retry after a timeout.')
    failed = saved_file(client, 'unavailable.txt', b'Unavailable content.')
    excluded = saved_file(client, 'other.txt', b'Retry after a timeout.')
    with rag.database.connect(write=True) as db:
        db.execute("""INSERT INTO avatar_records(user_id,aic,certificate,profile,last_modified_at,created_at,updated_at)
                      VALUES(?,'fixture-aic','{}','{}','1','1','1')""", (owner,))
        db.execute("UPDATE attachments SET relative_path='notes/' || filename WHERE id IN (?,?)", (good, failed))
        db.execute("UPDATE file_rag_documents SET status='failed',error='parse failed' WHERE file_id=?", (failed,))

    asyncio.run(rag.index('profile', good, owner))

    async def generate(*args, **kwargs):
        pytest.fail('material search called a generative model')

    monkeypatch.setattr(rag.chat.model_client, 'generate', generate)
    search = Mock(wraps=rag.store.search)
    monkeypatch.setattr(rag.store, 'search', search)
    scope = (owner, 'fixture-aic', {'id':'session'}, [], True)
    connect=rag.database.connect
    def off_loop(*args,**kwargs):
        try:asyncio.get_running_loop()
        except RuntimeError:return connect(*args,**kwargs)
        raise AssertionError('RAG retrieval opened a blocking DB connection on its event loop')
    monkeypatch.setattr(rag.database,'connect',off_loop)
    result = asyncio.run(rag.search_tool(scope, {'query':' retry after timeout ', 'folder':'notes'}))

    async def stale_stream(*args):
        yield {'type':'delta', 'text':'unreachable'}

    async def read_stale():
        proof = {'scope':scope, 'revisions':[(('profile', good), 'old-version')]}
        async for _ in rag.stream(({'_rag_access':proof}, '', '', []), 'query'):
            pytest.fail('revoked material reached the stream')
    monkeypatch.setattr(rag.chat.model_client, 'stream', stale_stream)
    with pytest.raises(HTTPException) as denied:
        asyncio.run(read_stale())
    assert denied.value.status_code == 409

    assert search.call_args.args[3] == ['retry after timeout']
    assert {(row['file_id'], row['relative_path']) for row in result.content['evidence']} == {(good, 'notes/retry.txt')}
    assert any('Retry after a timeout.' in row['text'] for row in result.content['evidence'])
    assert {row['file_id'] for row in result.content['catalog']} == {good, failed}
    assert any('parse failed' in notice and 'unavailable.txt' in notice for notice in result.content['notices'])
    assert {(source, uid) for (source, uid), _ in result.revisions} == {('profile', good), ('profile', failed)}
    assert result.sources == [{'file_id':good, 'filename':'retry.txt', 'type':'RAG'}]
    assert excluded not in str(result.content)


def test_index_does_not_publish_when_vector_write_is_incomplete(context,monkeypatch):
    rag,uid,fid,store=prepared_index(context,monkeypatch)
    monkeypatch.setattr(store,'contains',lambda *args:False)
    asyncio.run(rag.index('profile',fid,uid))
    with rag.database.connect() as db:
        assert db.execute('SELECT status FROM file_rag_documents WHERE file_id=?',(fid,)).fetchone()[0]=='failed'



def test_sync_only_requeues_incomplete_publications(context,monkeypatch):
    postgres = context[0].state.chat.rag.store
    rag,owner,identifier,_ = prepared_index(context,monkeypatch)
    monkeypatch.setattr(rag,'store',postgres)
    asyncio.run(rag.index('profile',identifier,owner))
    def sync_status():
        status = rag.sync({'id':owner})
        return next(item['status'] for item in status['items'] if item['file_id']==identifier)
    assert sync_status() == 'ready'
    with rag.database.connect(write=True) as db:
        db.execute("""DELETE FROM rag_vector_records
            WHERE user_id=? AND record_id=(SELECT record_id FROM file_rag_nodes WHERE file_id=? LIMIT 1)""",
            (owner,identifier))
    assert sync_status() == 'queued'

def test_published_index_survives_failed_same_content_rebuild(context,monkeypatch):
    from avatar.rag.indexing.service import revision,signature
    from avatar.models.embedding import EmbeddingSettings
    rag,uid,fid,store=prepared_index(context,monkeypatch)
    asyncio.run(rag.index('profile',fid,uid))
    with rag.database.connect() as db:
        row=rag.file(db,'profile',fid,uid)
        token=signature(EmbeddingSettings.current(db,uid),rag.chat.service.models.current(db,uid))
    active=rag.active_index('profile',fid,uid,revision(row),token)
    assert active and active['manifest']['expected_chunks']==2
    rag.sync({'id':uid},force=True)
    assert rag.active_index('profile',fid,uid,revision(row),token)['id']==active['id']
    def fail(*args):raise RuntimeError('simulated vector storage outage')
    monkeypatch.setattr(store,'write',fail)
    asyncio.run(rag.index('profile',fid,uid))
    assert rag.active_index('profile',fid,uid,revision(row),token)['id']==active['id']
    with rag.database.connect(write=True) as db:db.execute("UPDATE attachments SET caption='changed' WHERE id=?",(fid,))
    with rag.database.connect() as db:changed=rag.file(db,'profile',fid,uid)
    assert rag.active_index('profile',fid,uid,revision(changed),token) is None


def test_lexical_index_finds_cjk_and_code_and_hides_staging(context,monkeypatch):
    from avatar.rag.stores.lexical import LexicalStore
    rag,uid,fid,store=prepared_index(context,monkeypatch);lexical=LexicalStore(rag.database)
    asyncio.run(rag.index('profile',fid,uid))
    with rag.database.connect() as db:
        document=db.execute('SELECT collection FROM file_rag_documents WHERE file_id=?',(fid,)).fetchone()
        ids=[row[0] for row in db.execute('SELECT record_id FROM file_rag_nodes WHERE file_id=?',(fid,))]
    collection=document[0]
    assert lexical.search(uid,collection,ids,['智能体协议'],12)
    assert lexical.search(uid,collection,ids,['retry_request'],12)
    assert lexical.search('another-user',collection,ids,['智能体协议'],12)==[]
    assert lexical.search(uid,collection,[],['retry_request'],12)==[]
    staged=(['not-published'],[{'file_id':fid,'source':'profile','filename':'new.txt'}],['hidden secret'])
    lexical.stage(uid,collection,staged)
    assert lexical.search(uid,collection,['not-published'],['hidden'],12)==[]


def test_lexical_incomplete_write_does_not_publish(context,monkeypatch):
    from avatar.rag.stores.lexical import LexicalStore
    rag,uid,fid,_=prepared_index(context,monkeypatch)
    monkeypatch.setattr(LexicalStore,'contains',lambda *args,**kwargs:False)
    asyncio.run(rag.index('profile',fid,uid))
    with rag.database.connect() as db:assert db.execute('SELECT status FROM file_rag_documents WHERE file_id=?',(fid,)).fetchone()[0]=='failed'


def test_gc_cannot_delete_staging_records_during_publication(context,monkeypatch):
    import threading
    rag,uid,fid,store=prepared_index(context,monkeypatch)
    verified=threading.Event();resume=threading.Event();write=store.write;contains=store.contains
    def staged(*args):
        write(*args)
        with rag.database.connect(write=True) as db:db.execute('UPDATE file_rag_gc SET delete_after=0 WHERE file_id=?',(fid,))
    def check(*args):
        result=contains(*args);verified.set();resume.wait(3);return result
    monkeypatch.setattr(store,'write',staged);monkeypatch.setattr(store,'contains',check)
    async def run():
        index=asyncio.create_task(rag.index('profile',fid,uid));prune=None
        try:
            assert await asyncio.to_thread(verified.wait,3)
            prune=asyncio.create_task(asyncio.to_thread(rag.prune))
            await asyncio.sleep(.05)
        finally:
            resume.set();await index
            if prune:await prune
    asyncio.run(run())
    with rag.database.connect() as db:
        doc=db.execute('SELECT status,collection FROM file_rag_documents WHERE file_id=?',(fid,)).fetchone()
        ids=[r[0] for r in db.execute('SELECT record_id FROM file_rag_nodes WHERE file_id=?',(fid,))]
    assert doc['status']=='ready' and ids and contains(uid,doc['collection'],ids)


def test_worker_renews_foreground_index_lease(context,monkeypatch):
    from types import SimpleNamespace
    import avatar.rag.indexing.service as module
    rag,uid,fid,_=prepared_index(context,monkeypatch)
    clock=[1000.0]
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:clock[0],monotonic=lambda:clock[0]))
    original_vectors=rag.vectors
    async def check():
        cycle=asyncio.Event();started=asyncio.Event();release=asyncio.Event()
        monkeypatch.setattr(rag,'dispatch',lambda pending:cycle.set())
        async def vectors(*args):
            started.set();await release.wait();return await original_vectors(*args)
        monkeypatch.setattr(rag,'vectors',vectors)
        worker=asyncio.create_task(rag.run());foreground=None
        try:
            await asyncio.wait_for(cycle.wait(),3)
            foreground=asyncio.create_task(rag.index('profile',fid,uid))
            await asyncio.wait_for(started.wait(),3)
            clock[0]+=40
            for _ in range(150):
                with rag.database.connect() as db:
                    row=db.execute('SELECT status,lease_until FROM file_rag_documents WHERE file_id=?',(fid,)).fetchone()
                if row['status']!='running' or row['lease_until']>clock[0]:break
                await asyncio.sleep(.02)
            assert row['status']=='running' and row['lease_until']>clock[0]
        finally:
            release.set()
            if foreground:await foreground
            worker.cancel();await asyncio.gather(worker,return_exceptions=True)
        with rag.database.connect() as db:
            assert db.execute('SELECT status FROM file_rag_documents WHERE file_id=?',(fid,)).fetchone()[0]=='ready'
    asyncio.run(check())


def test_startup_only_fails_expired_index_leases(context, monkeypatch):
    import time
    from tests.support import saved_file

    rag, _, first, _ = prepared_index(context, monkeypatch)
    second = saved_file(context[1], 'expired.txt', b'expired')
    with rag.database.connect(write=True) as db:
        db.executemany("UPDATE file_rag_documents SET status='running',lease_until=? WHERE file_id=?",
                       [(time.time() + 300, first), (time.time() - 1, second)])

    async def check():
        started = asyncio.Event()
        monkeypatch.setattr(rag, 'dispatch', lambda pending: started.set())
        worker = asyncio.create_task(rag.run())
        try:
            await asyncio.wait_for(started.wait(), 3)
            with rag.database.connect() as db:
                states = dict(db.execute('SELECT file_id,status FROM file_rag_documents'))
            assert states[first] == 'running'
            assert states[second] == 'failed'
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(check())


@pytest.mark.parametrize('outcome', ['success', 'error', 'cancel'])
def test_old_index_completion_preserves_new_running_generation(context, monkeypatch, outcome):
    rag, owner, fid, _ = prepared_index(context, monkeypatch)
    prepare = rag.prepare

    async def replace_generation(*args):
        with rag.database.connect(write=True) as db:
            db.execute("UPDATE file_rag_documents SET generation=generation+1 WHERE file_id=?", (fid,))
        if outcome == 'error':
            raise ValueError('obsolete job')
        if outcome == 'cancel':
            raise asyncio.CancelledError()
        return await prepare(*args)

    monkeypatch.setattr(rag, 'prepare', replace_generation)
    if outcome == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(rag.index('profile', fid, owner))
    else:
        asyncio.run(rag.index('profile', fid, owner))
    with rag.database.connect() as db:
        assert db.execute('SELECT status FROM file_rag_documents WHERE file_id=?', (fid,)).fetchone()[0] == 'running'


@pytest.mark.parametrize('status_code', [404, 503])
def test_unclaimed_index_error_leaves_claimed_job_alone(context, monkeypatch, status_code):
    from fastapi import HTTPException

    rag, owner, fid, _ = prepared_index(context, monkeypatch)

    async def other_worker_claimed(*args):
        with rag.database.connect(write=True) as db:
            db.execute("UPDATE file_rag_documents SET status='running' WHERE file_id=?", (fid,))
        raise HTTPException(status_code, 'stale lookup')

    monkeypatch.setattr(rag, 'index', other_worker_claimed)
    asyncio.run(rag.queued_index(('profile', fid, owner)))
    with rag.database.connect() as db:
        assert db.execute('SELECT status FROM file_rag_documents WHERE file_id=?', (fid,)).fetchone()[0] == 'running'


def test_index_status_can_read_profile_only_and_counts_partial(context):
    from tests.support import saved_file
    app, user, _ = context
    file_id = saved_file(user, 'partial.txt', b'partial')
    with app.state.database.connect(write=True) as db:
        db.execute("UPDATE file_rag_documents SET status='partial' WHERE source='profile' AND file_id=?", (file_id,))
    url = '/api/me/file-index'
    value = user.get(url, params={'source': 'profile'}).json()
    assert set(value['summary']) == {'profile'}
    assert value['summary']['profile']['partial'] == 1
    assert all(row['source'] == 'profile' for row in value['items'])
    assert set(user.get(url).json()['summary']) == {'profile', 'chat'}
    assert user.get(url, params={'source': 'invalid'}).status_code == 422


# PostgreSQL exact-cosine retrieval and transactional pruning

def test_vectors_rank_only_visible_chunks_and_prune_atomically(pg_database):
    from avatar.rag.stores.postgres import PostgresFiles

    with pg_database.connect(write=True) as conn:
        for uid in ('alice', 'bob'):
            conn.execute("INSERT INTO users(id,username,password,role,created_at) VALUES(?,?,?,?,?)",
                         (uid, uid, 'hash', 'user', '2026'))
    store = PostgresFiles(pg_database)
    rows = [{'text': text, 'page': page, 'modality': 'text'} for text, page in
            [('best', 2), ('second', 3), ('wrong page', 1), ('not granted', 2)]]
    vectors = [[9., 0.], [1., 1.], [1., 0.], [1., 0.]]
    records = store.pack('profile', 'file-1', 'a.txt', rows, vectors, 'full')
    ids = records[0]
    store.write('alice', 'col', records, vectors)
    store.write('alice', 'col', records, vectors)
    store.write('bob', 'col', (ids, records[1], ['private'] * len(ids)), list(reversed(vectors)))
    plan = FileQuery(query='unmatched', page_start=2)
    assert not store.contains('alice', 'other-col', ids)
    assert not store.contains('unknown-user', 'col', ids)
    assert store.contains('alice', 'col', ids)
    found = store.search('alice', 'col', ids[:3], ['unmatched'], [[1., 0.]], plan)
    assert [row['text'] for row in found] == ['best', 'second']
    assert store.search('unknown-user', 'col', ids, ['second'], [[1., 0.]], plan) == []
    fused = store.search('alice', 'col', ids[:3], ['second'], [[1., 0.]], plan)
    assert fused[0]['text'] == 'second'
    from avatar.rag.stores.lexical import LexicalStore
    LexicalStore(pg_database).stage('alice', 'col', records)
    assert store.search('alice', 'col', ids[:3], ['second'], [[1., 0.]], plan) == fused
    with pytest.raises(RuntimeError):
        with pg_database.connect(write=True) as conn:
            store.delete_in(conn, 'alice', 'col', ids[:1])
            raise RuntimeError('rollback')
    assert store.contains('alice', 'col', ids)
    with pg_database.connect(write=True) as conn:
        store.delete_in(conn, 'alice', 'col', ids[:1])
    assert not store.contains('alice', 'col', ids) and store.contains('bob', 'col', ids)


def test_dense_cache_validates_current_rows_on_every_search(pg_database, monkeypatch):
    from avatar.rag.stores.postgres import PostgresFiles

    with pg_database.connect(write=True) as conn:
        for uid in ('alice', 'bob'):
            conn.execute('INSERT INTO users(id,username,password,role,created_at) VALUES(?,?,?,?,?)',
                         (uid, uid, 'hash', 'user', '2026'))
    store = PostgresFiles(pg_database)
    reads = []
    load = store._read_dense_block

    def counted(db, owner, collection, ids):
        reads.append(owner)
        return load(db, owner, collection, ids)

    monkeypatch.setattr(store, '_read_dense_block', counted)
    rows = [dict(text=t, modality='text') for t in ('one', 'two')]
    records = store.pack('profile', 'f', 'f.txt', rows, [[1., 0.], [0., 1.]], 'full')
    ids = records[0]
    for uid, vectors in [('alice', [[1., 0.], [0., 1.]]), ('bob', [[0., 1.], [1., 0.]])]:
        store.write(uid, 'c', records, vectors)

    def first(uid, vector, allowed=ids):
        return store.search(uid, 'c', allowed, [], [vector], FileQuery(query='q'))[0]['text']

    assert first('alice', [1., 0.]) == 'one'
    assert first('alice', [0., 1.]) == 'two'
    assert reads == ['alice']
    assert first('bob', [1., 0.]) == 'two'
    assert first('alice', [1., 0.], ids[1:]) == 'two'
    from avatar.rag.stores.lexical import LexicalStore
    LexicalStore(pg_database).stage('alice', 'c', records)
    for _ in range(3):
        assert store.search('alice', 'c', ids, ['two'], [], FileQuery(query='two'))[0]['text'] == 'two'
    with pg_database.connect(write=True) as conn:
        conn.execute("UPDATE rag_lexical_records SET tokens='changed' WHERE user_id=? AND record_id=?",
                     ('alice', ids[1]))
    assert store.search('alice', 'c', ids, ['two'], [], FileQuery(query='two')) == []
    assert store.search('alice', 'c', ids, ['changed'], [], FileQuery(query='changed'))[0]['text'] == 'two'

    with pg_database.connect(write=True) as conn:
        conn.execute('UPDATE rag_vector_records SET embedding=? WHERE user_id=? AND record_id=?',
                     ([-1., 0.], 'alice', ids[0]))
    assert first('alice', [1., 0.]) == 'two'
    with pg_database.connect(write=True) as conn:
        store.delete_in(conn, 'alice', 'c', ids)
    assert store.search('alice', 'c', ids, [], [[1., 0.]], FileQuery(query='q')) == []


def test_visual_index_preserves_errors_and_uses_non_thinking_budget(context,tmp_path,monkeypatch):
    from PIL import Image
    from types import SimpleNamespace
    from fastapi import HTTPException
    from avatar.models.client import ModelClient
    app,user,_=context;uid=user.get('/api/auth/me').json()['user']['id'];rag=app.state.chat.rag
    path=tmp_path/'visual.png';Image.new('RGB',(20,20),'white').save(path)
    config={'provider':'custom','api_format':'openai','base_url':'http://10.0.0.23:5000/v1','model':'qwen3.8','vision_enabled':True,'max_tokens':16384,'context_tokens':32768,'encrypted_key':app.state.service.vault.encrypt('local')}
    row={'user_id':uid,'sha256':'test-visual-source'};seen=[]
    monkeypatch.setattr(rag.cache,'get',lambda *args:None)
    monkeypatch.setattr(rag.cache,'put',lambda *args:seen.append(args))
    async def success(settings,key,system,history,message):
        assert settings['max_tokens']==config['max_tokens']
        body=ModelClient().build_request(settings,key,system,history,message)[3]
        assert body['enable_thinking'] is False and body['chat_template_kwargs']=={'enable_thinking':False}
        return SimpleNamespace(text='图片文字',usage={},finish_reason='stop')
    monkeypatch.setattr(app.state.chat.model_client,'generate',success)
    assert asyncio.run(rag.describe(row,path,config))==('图片文字','llm_vision')
    assert asyncio.run(rag.describe(row,path.read_bytes(),config))==('图片文字','llm_vision')
    assert seen[0][3]==seen[1][3]
    seen.pop()
    assert len(seen)==1
    async def truncated(*args,**kwargs):return SimpleNamespace(text='未完成',usage={},finish_reason='length')
    monkeypatch.setattr(app.state.chat.model_client,'generate',truncated)
    with pytest.raises(HTTPException,match='Token 上限'):asyncio.run(rag.describe(row,path,config))
    assert len(seen)==1
    error=HTTPException(502,'上游具体解析错误')
    async def failed(*args,**kwargs):raise error
    monkeypatch.setattr(app.state.chat.model_client,'generate',failed)
    with pytest.raises(HTTPException) as caught:asyncio.run(rag.describe(row,path,config))
    assert caught.value is error and len(seen)==1


def test_model_configuration_probe_failure_preserves_settings_and_index(context,monkeypatch):
    from fastapi import HTTPException
    from tests.support import MODEL
    app,user,_=context
    first=user.put('/api/me/model-settings',json={'model_config_input':MODEL})
    assert first.status_code==200,first.text
    upload(user,'probe-preserve.txt')
    old_chat=user.get('/api/me/model-settings').json()
    old_embedding=user.get('/api/me/embedding-settings').json()
    uid=user.get('/api/auth/me').json()['user']['id']
    def snapshot():
        with app.state.database.connect() as db:
            return [tuple(row) for row in db.execute('SELECT * FROM file_rag_documents WHERE user_id=?',(uid,))]
    before=snapshot()
    async def fail(*args,**kwargs):raise HTTPException(502,'upstream unavailable')
    monkeypatch.setattr(app.state.chat.model_client,'generate',fail)
    response=user.put('/api/me/model-settings',json={'model_config_input':{**MODEL,'model':'unavailable-model'},'updated_at':old_chat['updated_at']})
    assert response.status_code==502 and '未保存配置' in response.json()['detail']
    assert user.get('/api/me/model-settings').json()==old_chat
    assert snapshot()==before
    monkeypatch.setattr('avatar.models.embedding.embed',fail)
    response=user.put('/api/me/embedding-settings',json={'provider':'custom','base_url':'https://example.com/v1','model':'unavailable','api_key':'local','dimensions':64,'updated_at':old_embedding['updated_at']})
    assert response.status_code==502
    assert user.get('/api/me/embedding-settings').json()==old_embedding
    assert snapshot()==before


def test_source_footer_uses_observed_sources_and_honors_omission():
    from avatar.rag.service import MaterialRag

    sources = [{'file_id':'pdf', 'filename':'report_[final].pdf', 'type':'RAG'},
               {'file_id':'pdf', 'filename':'report_[final].pdf', 'type':'源文件'}]
    config = {'_material_sources':sources}
    footer = MaterialRag.source_footer((config, '', '', []))
    assert footer.count('report\\_\\[final\\].pdf') == 2
    assert '· RAG' in footer and '· 源文件' in footer
    assert MaterialRag.source_footer(({**config, '_omit_source_citations':True}, '', '', [])) == ''


def test_embedding_retries_only_transient_failures(monkeypatch):
    from avatar.models import embedding
    from avatar.models.diagnostics import transport_error
    from fastapi import HTTPException
    calls=[];delays=[]
    async def once(*args):
        calls.append(1)
        if len(calls)<4:raise transport_error(502,'connection lost')
        return [[1.0]],{}
    async def delay(value):delays.append(value)
    monkeypatch.setattr(embedding,'_embed_once',once)
    monkeypatch.setattr(embedding.asyncio,'sleep',delay)
    assert asyncio.run(embedding.embed(None,{},'local',['query']))==([[1.0]],{})
    assert len(calls)==4 and delays==[2,5,10]
    async def permanent(*args):raise HTTPException(502,'invalid dimension')
    monkeypatch.setattr(embedding,'_embed_once',permanent)
    with pytest.raises(HTTPException,match='invalid dimension'):
        asyncio.run(embedding.embed(None,{},'local',['query']))
    assert delays==[2,5,10]


def test_atomic_original_read_preserves_cursor_and_checks_private_access(provisioned_context, monkeypatch):
    from fastapi import HTTPException
    from tests.support import saved_file

    app, user, _ = provisioned_context
    content = 'Native text. ' * 1500
    identifier = saved_file(user, 'private-notes.txt', content.encode())
    uid = user.get('/api/auth/me').json()['user']['id']
    with app.state.database.connect(write=True) as db:
        aic = db.execute('SELECT aic FROM avatar_records WHERE user_id=?', (uid,)).fetchone()[0]
        db.execute("UPDATE attachments SET visibility='private' WHERE id=?", (identifier,))

    async def generate(*args, **kwargs):
        pytest.fail('original read called a generative model')

    rag = app.state.chat.rag
    monkeypatch.setattr(rag.chat.model_client, 'generate', generate)
    scope = (uid, aic, {'id':'named-read'}, [], True)
    first = asyncio.run(rag.read_tool(scope, {'file_id':identifier}))
    assert not first.content['complete']
    second = asyncio.run(rag.read_tool(scope, {'file_id':identifier, **first.content['next']}))
    assert first.content['pages'][0]['text'] + second.content['pages'][0]['text'] == content
    assert second.content['complete']
    assert first.sources == [{'file_id':identifier, 'filename':'private-notes.txt', 'type':'源文件'}]
    assert first.revisions == second.revisions
    with pytest.raises(HTTPException) as denied:
        asyncio.run(rag.read_tool((*scope[:-1], False), {'file_id':identifier}))
    assert denied.value.status_code == 404


def test_atomic_pdf_read_keeps_a_cursor_through_blank_pages(tmp_path):
    from pypdf import PdfWriter
    from avatar.rag.reading import ReadRequest, read_original_tool

    path = tmp_path / 'blank.pdf'
    writer = PdfWriter()
    for _ in range(17):
        writer.add_blank_page(width=100, height=100)
    writer.write(path)

    request = ReadRequest(file_id='pdf', mode='full')
    first, images, audio = asyncio.run(read_original_tool(path, path.name, 'application/pdf',
                                                         request, vision=False, audio=False))
    assert len(first['pages']) == 16 and not first['complete']
    assert first['next'] == {'start':17, 'offset':0}
    assert not (images or audio)
    second, _, _ = asyncio.run(read_original_tool(path, path.name, 'application/pdf',
                                                 ReadRequest(file_id='pdf', **first['next']),
                                                 vision=False, audio=False))
    assert second['complete'] and second['empty_pages'] == [17]
