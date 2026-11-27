import asyncio
import socket
import threading
from types import SimpleNamespace

from avatar.core.dispatch import Changes
from avatar.core.http_metrics import Minute
from avatar.core.monitoring import PlatformMonitor
from avatar.protocols.tasks.personal import PersonalTaskService
from avatar.runtime.worker import ProvisionWorker


def test_dispatch_listens_for_commit_and_drains_preexisting_work(pg_database, monkeypatch, tmp_path):
    from psycopg import OperationalError
    from avatar.core import dispatch
    scans = []
    monkeypatch.setattr(dispatch, 'record_duration', lambda *args: scans.append(args))
    # A reserved, non-listening loopback port cannot reach a real broker.
    unavailable = socket.socket()
    unavailable.bind(('127.0.0.1', 0))
    bootstrap = f'127.0.0.1:{unavailable.getsockname()[1]}'
    changes = Changes(pg_database, str(tmp_path / 'absent.sock'), bootstrap, lambda: None)
    pg_database.dispatch_enabled = True

    def pending():
        with pg_database.connect() as db:
            return db.execute('SELECT count(*) FROM avatar_dispatch_events').fetchone()[0]

    def emit(queue):
        with pg_database.connect(write=True) as db:
            db.execute("SELECT avatar_emit('work',?)", (queue,))

    async def check():
        emit('chat')
        listener = asyncio.create_task(changes.run())
        try:
            await asyncio.wait_for(changes.work['chat'].wait(), 2)
            # Ready transitions set all latches. Verify the committed backlog itself.
            async with asyncio.timeout(2):
                while await asyncio.to_thread(pending):
                    await asyncio.sleep(.01)
            await asyncio.sleep(.1)
            baseline = len(scans)
            await asyncio.sleep(.65)
            assert len(scans) == baseline
            changes.work['soul'].clear()
            with pg_database.connect(write=True) as db:
                db.execute("SELECT avatar_emit('work','soul')")
                await asyncio.sleep(.1)
                assert not changes.work['soul'].is_set()

            await asyncio.wait_for(changes.work['soul'].wait(), 1)
        finally:
            listener.cancel()
            await asyncio.wait_for(asyncio.gather(listener, return_exceptions=True), 2)
        assert changes.loop is None and not any(changes.health.values())
        await asyncio.to_thread(emit, 'index')
        assert await asyncio.to_thread(pending)
        restarted = Changes(pg_database, '', '', lambda: None)
        forward = restarted._forward
        batches = []
        async def lose_first_connection(payloads):
            batches.append(tuple(payloads))
            if len(batches) == 1:
                raise OperationalError('test connection lost before acknowledgement')
            await forward(payloads)

        monkeypatch.setattr(restarted, '_forward', lose_first_connection)
        listener = asyncio.create_task(restarted.run())
        try:
            async with asyncio.timeout(5):
                while await asyncio.to_thread(pending):
                    await asyncio.sleep(.01)
            assert batches == [('["work","index"]',)] * 2
        finally:
            listener.cancel()
            await asyncio.wait_for(asyncio.gather(listener, return_exceptions=True), 2)
        assert restarted.loop is None

    with unavailable:
        asyncio.run(check())


def test_monitor_restored_minute_is_counted_once(pg_database, monkeypatch):
    from avatar.core import monitoring
    monkeypatch.setattr(monitoring, 'time', lambda: 6000)
    monitor = PlatformMonitor(pg_database, None, None)
    old = Minute()
    monitor._rollup([(99, old.totals())], 100, False)
    monitor.pending_until = 98
    monitor.http.at(99).counts['requests'] = 3
    asyncio.run(monitor.persist_completed())
    assert [(key, value['requests']) for key, value in monitor.history] == [(99, 3)]


def test_empty_provision_claim_runs_off_loop(pg_database, monkeypatch):
    original = pg_database.connect
    caller_threads = []
    def connect(*args, **kwargs):
        caller_threads.append(threading.get_ident())
        return original(*args, **kwargs)
    monkeypatch.setattr(pg_database, 'connect', connect)
    assert asyncio.run(ProvisionWorker(pg_database, None).once()) is False
    assert caller_threads and threading.get_ident() not in caller_threads


def test_expired_guest_hidden_before_bounded_cleanup(pg_database, monkeypatch):
    from avatar.conversation.memory import ConversationMemory
    from avatar.core.db import now
    service = PersonalTaskService.__new__(PersonalTaskService)
    service.database = pg_database
    memory = ConversationMemory(pg_database)
    service.chat = SimpleNamespace(memory=memory)
    remove = memory.remove

    def remove_after_new_turn(conversation_id, **kwargs):
        if conversation_id == 'refreshed':
            with pg_database.connect(write=True) as db:
                db.execute('UPDATE chat_conversations SET updated_at=? WHERE id=?', (now(), conversation_id))
        remove(conversation_id, **kwargs)

    monkeypatch.setattr(memory, 'remove', remove_after_new_turn)
    with pg_database.connect(write=True) as db:
        db.execute("INSERT INTO users(id,username,password,role,created_at) VALUES('owner','owner','hash','user','2000')")
        for session in ('stale', 'refreshed'):
            db.execute("""INSERT INTO chat_conversations(id,user_id,channel,target_aic,caller_id,platform,title,created_at,updated_at)
                VALUES(?,'owner','acps','owner','visitor','town-guest','guest','2000','2000')""", (session,))
        for task in ('one', 'two', 'three'):
            db.execute("""INSERT INTO avatar_aip_tasks(local_aic,peer_aic,group_id,session_id,task_id,command_id,document)
                VALUES('owner','town-http#guest:key','','session',?,?,'{"sentAt":"2000-01-01"}')""", (task, task))
        row = service.task_row(db, ('owner','town-http#guest:key','','session','two'))
        assert row is None
    service.expire_guests(batch_size=2)
    with pg_database.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM avatar_aip_tasks').fetchone()[0] == 1
        assert [row[0] for row in db.execute('SELECT id FROM chat_conversations')] == ['refreshed']


def test_cancelled_town_request_finishes_admission_then_drains_generation(tmp_path, monkeypatch):
    from avatar.core.config import Settings
    service = SimpleNamespace(database=None, attachments=SimpleNamespace(directory=tmp_path))
    town = PersonalTaskService(SimpleNamespace(service=service), Settings(data_dir=tmp_path, town_public_enabled=True))
    entered, commit = threading.Event(), threading.Event()
    started, stopped = asyncio.Event(), asyncio.Event()

    def admit(command, context, owner, aic, target, principal, public, scope, operation, session):
        entered.set()
        assert commit.wait(3)
        result = {'id':'writer', 'taskId':scope[-1], 'sessionId':session, 'status':{'state':'working'}}
        return 'start', scope, (1, result, {'run_id':'test'}, False)

    async def generate(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(town, 'target', lambda *_: {})
    monkeypatch.setattr(town, '_admit', admit)
    monkeypatch.setattr(town, 'generate', generate)

    async def check():
        command = {'id':'one', 'taskId':'one', 'sessionId':'s', 'command':'start'}
        wire = SimpleNamespace(peer_aic='town', group_id='', headers={'x-avatar-access':'public'})
        request = asyncio.create_task(town.handle(command, wire, 'owner', 'avatar'))
        assert await asyncio.to_thread(entered.wait, 2)
        request.cancel()
        stopping = asyncio.create_task(town.stop())
        await asyncio.sleep(0)
        assert not stopping.done()
        commit.set()
        await asyncio.wait_for(stopping, 2)
        await asyncio.gather(request, return_exceptions=True)
        assert request.cancelled() and started.is_set() and stopped.is_set()
        assert not town.accepting and not town.pending and not town.running

    asyncio.run(check())


def test_committed_certificate_request_wakes_worker_after_client_disconnect(monkeypatch):
    from avatar.runtime.platform_certificate import PlatformCertificate
    cert = PlatformCertificate(None, None)
    entered, committed = threading.Event(), threading.Event()

    def queue(current, actor):
        entered.set()
        assert committed.wait(2)

    monkeypatch.setattr(cert, 'status', lambda: {'certificate':{'id':'test'}})
    monkeypatch.setattr(cert, 'queue', queue)
    async def check():
        request = asyncio.create_task(cert.request('test', 'owner'))
        assert await asyncio.to_thread(entered.wait, 1)
        request.cancel()
        committed.set()
        await asyncio.gather(request, return_exceptions=True)
        assert request.cancelled() and cert.wake.is_set()

    asyncio.run(check())


def test_provision_worker_retries_startup_recovery(monkeypatch):
    from psycopg import OperationalError
    worker = ProvisionWorker(object(), None)
    attempts = []
    resumed = asyncio.Event()

    def recover():
        attempts.append(1)
        if len(attempts) == 1:
            raise OperationalError('test connection unavailable')

    async def once():
        resumed.set()
        return False

    monkeypatch.setattr(worker, 'recover', recover)
    monkeypatch.setattr(worker, 'once', once)
    async def check():
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(resumed.wait(), 4)
            assert len(attempts) == 2
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())
