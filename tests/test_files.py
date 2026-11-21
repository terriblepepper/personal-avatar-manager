"""资料上传、批量操作、访问范围和文件存储。"""
import asyncio
import hashlib
import threading
from tempfile import TemporaryFile

import pytest

from avatar.core.user_storage import UserBlobs, migrate_user_storage, user_root
from avatar.files.library.attachments import MAX_STORED_SIZE
from avatar.files.uploads.buffer import upload_buffer, upload_work
from avatar.knowledge.media import MAX_ASSET
from tests.support import upload, upload_profile


def batch(user, action, ids=(), folders=()):
    return user.post('/api/me/attachments/batch', json={'action': action, 'ids': list(ids), 'folders': list(folders)})

def inventory(user):
    return {r['filename']: r for r in user.get('/api/me/attachments').json()}

def test_folder_permissions_inherit_without_overwriting_manual_files_or_child_folders(context):
    app, user, _ = context
    first = upload(user, 'first.txt', 'parent/first.txt')
    fixed = upload(user, 'fixed.txt', 'parent/fixed.txt')
    child = upload(user, 'child.txt', 'parent/sub/child.txt')
    assert user.patch('/api/me/attachments/' + fixed['id'], json={'visibility': 'private'}).status_code == 200
    assert batch(user, 'public', folders=['parent']).status_code == 200
    rows = inventory(user)
    assert rows['first.txt']['visibility'] == 'public' and (not rows['first.txt']['visibility_override'])
    assert rows['fixed.txt']['visibility'] == 'private' and rows['fixed.txt']['visibility_override']
    assert rows['child.txt']['visibility'] == 'public'
    assert upload(user, 'new.txt', 'parent/sub/new.txt')['visibility'] == 'public'
    assert batch(user, 'private', folders=['parent/sub']).status_code == 200
    assert batch(user, 'private', folders=['parent']).status_code == 200
    assert batch(user, 'public', folders=['parent']).status_code == 200
    assert inventory(user)['child.txt']['visibility'] == 'private'
    assert inventory(user)['new.txt']['visibility'] == 'private'
    assert batch(user, 'public', ids=[child['id']]).status_code == 200
    assert batch(user, 'private', folders=['parent']).status_code == 200
    assert inventory(user)['child.txt']['visibility'] == 'public'
    assert user.patch('/api/me/attachments/' + first['id'], json={'caption': 'note'}).status_code == 200
    assert batch(user, 'public', folders=['parent']).status_code == 200
    assert inventory(user)['first.txt']['visibility'] == 'public'
    tree = user.get('/api/me/material-folders').json()
    assert tree['visibility']['parent'] == 'public' and tree['visibility']['parent/sub'] == 'private'

def test_batch_file_permissions_and_mixed_folder_delete_are_atomic_and_owner_only(context):
    app, user, admin = context
    first = upload(user, 'first.txt', 'dir/first.txt')
    second = upload(user, 'second.txt', 'dir/sub/second.txt')
    third = upload(user, 'third.txt')
    assert batch(user, 'public', ids=[first['id'], third['id']]).status_code == 200
    assert batch(user, 'private', ids=[first['id'], third['id']]).status_code == 200
    assert batch(admin, 'delete', ids=[first['id']]).status_code == 404
    assert batch(user, 'delete', ids=[third['id'], 'missing']).status_code == 404
    assert user.get(third['download_url']).status_code == 200
    done = batch(user, 'delete', ids=[first['id'], third['id']], folders=['dir', 'dir/sub'])
    assert done.status_code == 200 and done.json()['changed_count'] == 3
    assert done.json()['files'] == [] and done.json()['items'] == []
    assert not list(app.state.service.attachments.directory.glob('*.blob'))
    assert batch(user, 'delete').status_code == 422

def test_soul_batch_delete_rejects_in_use_material_without_partial_changes(context):
    app, user, admin = context
    first = user.post('/api/me/knowledge/assets', files={'file': ('one.txt', b'one')}).json()
    second = user.post('/api/me/knowledge/assets', files={'file': ('two.txt', b'two')}).json()
    url = '/api/me/knowledge/assets/batch'
    assert user.post(url, json={'action': 'public', 'ids': [first['id']]}).status_code == 422
    assert admin.post(url, json={'action': 'delete', 'ids': [first['id']]}).status_code == 404
    assert user.post(url, json={'action': 'delete', 'ids': [first['id'], 'missing']}).status_code == 404
    owner = user.get('/api/auth/me').json()['user']['id']
    with app.state.database.connect(write=True) as db:
        db.execute("INSERT INTO soul_builds(id,user_id,prompt,asset_ids,profile_snapshot,profile_revision,status,stage,created_at,updated_at) VALUES('batch-build',?,'',?,'{}','','running','','now','now')", (owner, '["' + second['id'] + '"]'))
    response = user.post(url, json={'action': 'delete', 'ids': [first['id'], second['id']]})
    assert response.status_code == 409
    assert user.get('/api/me/knowledge/assets/' + first['id']).status_code == 200
    with app.state.database.connect(write=True) as db:
        db.execute("UPDATE soul_builds SET status='failed' WHERE id='batch-build'")
    assert user.post(url, json={'action': 'delete', 'ids': [first['id'], second['id']]}).status_code == 200
    assert not user.get('/api/me/knowledge').json()['assets']

def test_cancelled_upload_keeps_mapping_alive_until_worker_exits():
    entered = threading.Event()
    release = threading.Event()
    seen = []

    def read(data):
        entered.set()
        release.wait(2)
        seen.append(data[:])

    async def run(data):
        task = asyncio.create_task(upload_work(read, data))
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    with TemporaryFile() as file:
        file.write(b'original')
        with upload_buffer(file) as data:
            asyncio.run(run(data))
    assert seen == [b'original']

def test_interrupted_delete_recovers_committed_and_uncommitted_originals(context):
    from avatar.files.library.attachments import AttachmentService
    app, user, _ = context
    item = upload(user, 'recover.txt')
    directory = app.state.service.attachments.directory
    original = directory / (item['id'] + '.blob')
    original.replace(directory / ('.removed-' + item['id'] + '-interrupted'))
    orphan = original.parent / '.removed-00000000-0000-0000-0000-000000000000-interrupted'
    orphan.write_bytes(b'deleted')
    AttachmentService(app.state.database)
    assert user.get(item['download_url']).content == b'recover.txt'
    assert not orphan.exists() and (not list(directory.glob('.removed-*')))

def test_profile_quota_counts_staged_and_saved_files_and_releases_on_delete(context):
    app, user, _ = context
    first = upload_profile(user, 'first.txt', b'a').json()
    uid = user.get('/api/auth/me').json()['user']['id']
    with app.state.database.connect(write=True) as db:
        db.execute('UPDATE attachments SET size=? WHERE id=?', (MAX_STORED_SIZE - 1, first['id']))
    second = upload_profile(user, 'second.txt', b'b')
    assert second.status_code == 201, second.text
    before = set(app.state.service.attachments.directory.glob('*.blob'))
    assert upload_profile(user, 'overflow.txt', b'c').status_code == 413
    assert set(app.state.service.attachments.directory.glob('*.blob')) == before
    assert user.patch('/api/me/attachments/' + second.json()['id'], json={'caption': ''}).status_code == 200
    assert user.delete('/api/me/attachments/' + first['id']).status_code == 200
    assert upload_profile(user, 'after-delete.txt', b'd').status_code == 201

def test_soul_quota_exempts_imports_and_keeps_private_copy_after_source_deletion(context):
    app, user, admin = context
    first = user.post('/api/me/knowledge/assets', files={'file': ('first.txt', b'first')}).json()
    with app.state.database.connect(write=True) as db:
        db.execute('UPDATE knowledge_assets SET size=? WHERE id=?', (MAX_ASSET, first['id']))
    source = upload_profile(user, 'source.txt', b'private source').json()
    owner = user.get('/api/auth/me').json()['user']
    imported = asyncio.run(app.state.knowledge.import_material(owner, source['id']))
    assert imported['source_attachment_id'] == source['id']
    assert user.post('/api/me/knowledge/assets', files={'file': ('overflow.txt', b'overflow')}).status_code == 413
    duplicate = user.post('/api/me/knowledge/assets', files={'file': ('same.txt', b'private source')})
    assert duplicate.status_code == 201 and duplicate.json()['id'] == imported['id']
    assert user.delete('/api/me/attachments/' + source['id']).status_code == 200
    app.state.database.initialize()
    copy = user.get('/api/me/knowledge/assets/' + imported['id'])
    assert copy.status_code == 200 and copy.content == b'private source'
    assert app.state.knowledge.asset(owner['id'], imported['id'])['source_attachment_id'] == source['id']
    assert user.delete('/api/me/knowledge/assets/' + first['id']).status_code == 200
    assert user.post('/api/me/knowledge/assets', files={'file': ('after.txt', b'after')}).status_code == 201

def test_uploads_over_old_single_file_limit_use_disk_backed_buffers(context):
    app, user, _ = context
    data = b'a' * (10 * 1024 * 1024 + 1)
    profile = upload_profile(user, 'large.txt', data)
    assert profile.status_code == 201, profile.text
    soul = user.post('/api/me/knowledge/assets', files={'file': ('large.txt', data)})
    assert soul.status_code == 201, soul.text
    assert user.get(profile.json()['download_url']).content == data
    assert user.get('/api/me/knowledge/assets/' + soul.json()['id']).content == data

def test_legacy_matching_materials_get_durable_import_exemption(context):
    app, user, _ = context
    source = upload_profile(user, 'source.txt', b'same content').json()
    material = user.post('/api/me/knowledge/assets', files={'file': ('copy.txt', b'same content')}).json()
    owner = user.get('/api/auth/me').json()['user']
    with app.state.database.connect(write=True) as db:
        db.execute('ALTER TABLE knowledge_assets DROP COLUMN source_attachment_id')
    app.state.database.initialize()
    assert app.state.knowledge.asset(owner['id'], material['id'])['source_attachment_id'] == source['id']
    upload_profile(user, 'new-source.txt', b'new content')
    separate = user.post('/api/me/knowledge/assets', files={'file': ('new-copy.txt', b'new content')}).json()
    app.state.database.initialize()
    assert app.state.knowledge.asset(owner['id'], separate['id'])['source_attachment_id'] == ''


def test_streamed_upload_batch_completion_and_download(context):
    import uuid
    import httpx

    app, user, _ = context
    batch = uuid.uuid4().hex
    assert upload_profile(user, 'old.txt', b'older audit row without batch').status_code == 201
    chunks = (b'first chunk\n', b'second chunk\n')

    async def body():
        for chunk in chunks:
            yield chunk

    async def upload():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url=str(user.base_url),
                                     cookies=dict(user.cookies.items()),headers={'X-CSRF-Token':user.headers['X-CSRF-Token']}) as client:
            return await client.post('/api/me/attachments', content=body(), headers={
                'Content-Type':'application/octet-stream','X-Attachment-Filename':'streamed.txt','X-Upload-Batch':batch,
            })

    saved = asyncio.run(upload())
    assert saved.status_code == 201, saved.text
    endpoint = f'/api/me/upload-batches/{batch}/complete'
    expected = {'completed':1,'skipped':1,'failed':0}
    for _ in range(2):
        result = user.post(endpoint, json={'kind':'profile','selected':2,'skipped':1})
        assert result.status_code == 200, result.text
        assert result.json() == expected
    download = user.get('/api/me/attachments/' + saved.json()['id'])
    assert download.status_code == 200 and download.content == b''.join(chunks)
    with app.state.database.connect() as db:
        assert db.execute('SELECT count(*) FROM agent_notifications WHERE dedupe_key=?',
                          ('upload:profile:' + batch,)).fetchone()[0] == 1


@pytest.mark.parametrize('cancel_early', [False, True])
def test_rag_graph_stream_preserves_events_and_closes_the_model(cancel_early):
    from contextlib import aclosing
    from types import SimpleNamespace
    from avatar.rag.service import MaterialRag

    async def exercise():
        closed = asyncio.Event()
        messages = [{'type':'status','stage':'generating'}, {'type':'delta','text':'first'},
                    {'type':'delta','text':'second'}, {'type':'usage','usage':{'input_tokens':7}}]

        async def model_stream(*args):
            try:
                yield messages[0]
                if cancel_early:
                    await asyncio.Event().wait()
                for event in messages[1:]:
                    yield event
            finally:
                closed.set()

        async def answer_context(context, message):
            return context, message

        rag = object.__new__(MaterialRag)
        rag.chat = SimpleNamespace(model_client=SimpleNamespace(stream=model_stream))
        rag.answer_context = answer_context
        config = {'_material_sources':[{'filename':'report.txt', 'type':'RAG'}]}
        async with aclosing(rag.stream((config, '', '', []), 'question')) as stream:
            assert await anext(stream) == messages[0]
            if not cancel_early:
                assert [item async for item in stream] == [
                    *messages[1:], {'type':'delta','text':'\n\n> 引用来源：\n> report.txt · RAG  \n'}]
        assert closed.is_set(), 'closing the graph stream must finish cleanup of the model iterator'

    asyncio.run(asyncio.wait_for(exercise(), timeout=10))


# Owner layout, repeatable migration and containment smoke checks

def test_owner_originals_migrate_once_and_new_uploads_use_owner(context):
    app,client,admin=context
    database=app.state.database
    uid=client.get('/api/auth/me').json()['user']['id']
    blobs=UserBlobs(database,'attachments','attachments')
    path=blobs.new(uid,'example.blob');path.write_bytes(b'original')
    assert path.parent==database.data_dir/'users'/uid/'attachments'
    legacy=database.data_dir/'everos'/hashlib.sha256(uid.encode()).hexdigest()
    legacy.mkdir(parents=True);(legacy/'memory.txt').write_text('retained')
    response=client.post('/api/me/attachments',content=b'kept original',headers={'X-Attachment-Filename':'kept.txt'})
    assert response.status_code==201,response.text
    item=response.json();original=blobs/(item['id']+'.blob')
    old=database.data_dir/'attachments';old.mkdir()
    original.rename(old/original.name)
    migrate_user_storage(database);migrate_user_storage(database)
    assert client.get(item['download_url']).content==b'kept original'
    assert (user_root(database.data_dir,uid)/'everos/memory.txt').read_text()=='retained'
    assert path.read_bytes()==b'original' and not legacy.exists()
    from avatar.skills.service import UserSkills
    assert UserSkills(database).folder(uid,create=True)==user_root(database.data_dir,uid)/'skills'
    assert not (user_root(database.data_dir,uid)/'file_rag'/'chroma.sqlite3').exists()

    with pytest.raises(ValueError):user_root(database.data_dir,'../other')
    with pytest.raises(ValueError):blobs/'../private'


def test_symlink_owner_is_rejected(context):
    app,client,_=context
    base=app.state.database.data_dir
    (base/'users').mkdir(exist_ok=True)
    (base/'users'/'linked').symlink_to(base,target_is_directory=True)
    with pytest.raises(ValueError):user_root(base,'linked')
