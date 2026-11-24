"""Import a stopped deployment's local index snapshots into its PostgreSQL database."""
import argparse
import base64
from collections import defaultdict
from contextlib import closing, contextmanager
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import struct
import tempfile
import uuid
from pathlib import Path, PurePosixPath
import sys

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

if __package__ in ('', None):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avatar.core.config import require_database_url
from avatar.core.db import dump
from avatar.core.postgres import Connection, SCHEMA_LOCK, WRITE_LOCK, lock_schema_transaction
from avatar.knowledge import everos_storage as storage
from avatar.protocols.tasks.store import import_system_tasks, initialize_system_tasks
from avatar.rag.stores.postgres import install_vector_schema
from avatar.rag.stores.lexical import LexicalStore
from avatar.rag.invalidation import signature


def _manifest(root):
    files = {}
    for path in root.rglob('*'):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError('Snapshot contains a symlink or special file')
        files[path.relative_to(root)] = (info.st_mode, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return files


@contextmanager
def _private_copy(source, before):
    """Allow libraries to replay WAL only inside a validated, private working copy."""
    with tempfile.TemporaryDirectory(prefix='avatar-index-import-') as temporary:
        copy = shutil.copytree(source, Path(temporary) / 'index', symlinks=True)
        entries = _manifest(copy)
        if _manifest(source) != before:
            raise ValueError('Snapshot changed during copy')
        copy.chmod(0o700)
        for relative, (mode, *_) in entries.items():
            (copy / relative).chmod(0o700 if stat.S_ISDIR(mode) else 0o600)
        yield copy


def _require_transaction(connection):
    if connection.info.transaction_status == TransactionStatus.IDLE:
        if connection.autocommit:
            raise ValueError('Index import requires a caller-managed transaction')
        connection.execute('SELECT 1')


def _collections(database):
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Chroma snapshot failed SQLite integrity check')
        try:
            rows = db.execute('''SELECT c.name,c.dimension,d.tenant_id,d.name
                FROM collections c LEFT JOIN databases d ON d.id=c.database_id ORDER BY c.name''').fetchall()
        except sqlite3.DatabaseError as exc:
            raise ValueError('unrecognized Chroma database') from exc
    if any((tenant, db_name) != ('default_tenant', 'default_database') for _, _, tenant, db_name in rows):
        raise ValueError('unexpected tenant or database in account Chroma snapshot')
    return [(name, dimension) for name, dimension, _, _ in rows]


def _content(document, metadata, embedding):
    return document, json.dumps(metadata, ensure_ascii=False, sort_keys=True, allow_nan=False), struct.pack(
        f'<{len(embedding)}f', *embedding)


def _import_collection(db, user_id, collection, dimension):
    total = collection.count()
    seen = set()
    for offset in range(0, total, 256):
        page = collection.get(limit=256, offset=offset, include=['documents', 'metadatas', 'embeddings'])
        ids = page['ids']
        if len(ids) != min(256, total - offset) or len(set(ids)) != len(ids) or seen.intersection(ids):
            raise ValueError('incomplete or duplicate Chroma snapshot page')
        seen.update(ids)
        inserts, expected = [], {}
        for key, document, metadata, embedding in zip(ids, page['documents'], page['metadatas'],
                                                      page['embeddings'], strict=True):
            vector = embedding.tolist()
            if (not isinstance(document, str) or not isinstance(metadata, dict) or
                    not vector or len(vector) != dimension or not all(map(math.isfinite, vector))):
                raise ValueError('incomplete Chroma record or invalid embedding')
            expected[key] = _content(document, metadata, vector)
            inserts.append((user_id, collection.name, key, document, Jsonb(metadata), vector))
        with db.cursor(binary=True, row_factory=tuple_row) as cur:
            cur.executemany('''INSERT INTO rag_vector_records(user_id,collection,record_id,document,metadata,embedding)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,collection,record_id) DO NOTHING''', inserts)
            rows = cur.execute('''SELECT record_id,document,metadata,embedding FROM rag_vector_records
                WHERE user_id=%s AND collection=%s AND record_id=ANY(%s)''',
                               (user_id, collection.name, ids)).fetchall()
        if {key: _content(doc, meta, vector) for key, doc, meta, vector in rows} != expected:
            raise ValueError('Chroma record conflict or incomplete PostgreSQL import')
    return total


def import_chroma_snapshot(connection, user_id, directory):
    """Copy all readable records, retaining IDs. Empty collections have no published records."""
    source = Path(directory).absolute()
    if source.is_symlink() or any(parent.is_symlink() for parent in source.parents):
        raise ValueError('Chroma snapshot path contains a symlink')
    before = _manifest(source)
    if Path('chroma.sqlite3') not in before or not (source / 'chroma.sqlite3').is_file():
        raise ValueError('missing chroma.sqlite3 in snapshot directory')
    from chromadb import PersistentClient
    from chromadb.config import Settings

    # Opening Chroma can replay SQLite WAL and update HNSW segments, even during reads.
    with _private_copy(source, before) as copy:
        collections = _collections(copy / 'chroma.sqlite3')
        _require_transaction(connection)
        client = PersistentClient(str(copy), settings=Settings(anonymized_telemetry=False))
        try:
            with connection.transaction():
                return sum(_import_collection(connection, user_id,
                           client.get_collection(name, embedding_function=None), dimension)
                           for name, dimension in collections)
        finally:
            client.close()


def _fingerprint(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.name.endswith('-shm'):
            continue
        with path.open('rb') as stream:
            checksum = hashlib.file_digest(stream, 'sha256').digest()
        digest.update(path.relative_to(root).as_posix().encode() + b'\0' + checksum)
    return digest.hexdigest()


def _scope_path(path, owner, kind):
    parts = PurePosixPath(path).parts
    if (str(PurePosixPath(path)) != path or len(parts) < 5 or parts[0] != 'avatar-soul'
            or parts[2:4] != ('users', owner) or '..' in parts or '\\' in path
            or not re.fullmatch(r'build-[a-f0-9]{32}|learning', parts[1])):
        raise ValueError('EverOS path does not belong to this owner/project')
    relative = '/'.join(parts[4:])
    if ((kind == 'user_profile' and relative != 'user.md')
            or (kind == 'episode' and not re.fullmatch(r'episodes/episode-[^/]+\.md', relative))):
        raise ValueError('Unexpected Markdown path for EverOS record kind')
    return parts[1], relative


def _sqlite_value(value):
    if isinstance(value, bytes):
        return {'$sqlite_blob':base64.b64encode(value).decode()}
    if isinstance(value, str) and '\0' in value:
        return {'$sqlite_text':base64.b64encode(value.encode()).decode()}
    if isinstance(value, float) and not math.isfinite(value):
        return {'$sqlite_float':value.hex()}
    return value


def _read_sqlite(conn, scope, directory, counts):
    for path in sorted(directory.iterdir()):
        if path.name.endswith(('-shm', '-wal', '-journal')):
            continue
        if not path.is_file():
            raise ValueError('Unexpected directory in EverOS SQLite snapshot')
        with path.open('rb') as stream:
            if stream.read(16) != b'SQLite format 3\0':
                raise ValueError('Unrecognized file in EverOS SQLite snapshot')

        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as source:
            source.row_factory = sqlite3.Row
            if [r[0] for r in source.execute('PRAGMA integrity_check')] != ['ok']:
                raise ValueError('EverOS SQLite integrity check failed')
            schema = list(source.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name'))
            storage.archive_blob(conn, scope, 'sqlite_schema', path.name, json.dumps([list(r) for r in schema]).encode(), 'application/json')
            for kind, table, ddl in schema:
                if kind != 'table':
                    continue
                quoted = '"' + table.replace('"','""') + '"'
                # Prefix by source DB, preserving row identifiers even across independent legacy databases.
                ordinal = 0
                columns = {r[1].lower() for r in source.execute('PRAGMA table_info('+quoted+')')}
                rowid = next((name for name in ('rowid','_rowid_','oid') if name not in columns), None)
                if re.search(r'\bWITHOUT\s+ROWID\b', ddl or '', re.I):
                    rowid = None
                projection = rowid+' AS __source_rowid__,*' if rowid else '*'
                cursor = source.execute('SELECT '+projection+' FROM '+quoted)
                for batch in iter(lambda:cursor.fetchmany(256), []):
                    archived = []
                    queue = defaultdict(list)
                    for row in batch:
                        values = tuple(row)
                        if rowid:
                            key = f'{path.name}:rowid:{values[0]}'
                            fields = dict(zip(row.keys()[1:], values[1:], strict=True))
                        else:
                            key = f'{path.name}:ordinal:{ordinal}'
                            fields = dict(row)
                        archived.append({'source_row_id':key, 'row_data':{k:_sqlite_value(v) for k,v in fields.items()}})
                        ordinal += 1
                        if table == 'md_change_state' and path.name == 'system.db':
                            project, relative = _scope_path(fields['md_path'], scope[0], fields['kind'])
                            if fields['kind'] in ('episode','user_profile'):
                                queue[project].append({**fields, 'md_path':relative})
                    counts['legacy_rows'] += storage.archive_rows(conn, scope, table, archived, ddl or '')
                    for project, records in queue.items():
                        counts['queue_rows'] += storage.import_queue_rows(conn, scope, project, records)


def _record(kind, row, owner):
    if row['owner_id'] != owner or row['owner_type'] != 'user' or row['app_id'] != 'avatar-soul':
        raise ValueError('Unexpected owner or app in EverOS Lance index')
    project, path = _scope_path(row['md_path'], owner, kind)
    if row['project_id'] != project:
        raise ValueError('Mismatched EverOS Lance project')
    expected_id = owner if kind == 'user_profile' else f"{owner}_{row['entry_id']}"
    if row['id'] != expected_id or (kind == 'episode' and not row['entry_id']):
        raise ValueError('Invalid EverOS record ID for this owner')
    metadata = {'owner_id':owner,'owner_type':'user','app_id':'avatar-soul','project_id':project}
    base = {'kind':kind,'record_id':row['id'],'md_path':path,'content_sha256':row['content_sha256'],
            'metadata':metadata,'summary':row.get('summary') or ''}
    if kind == 'user_profile':
        metadata['profile_data'] = {'summary':base['summary'],'explicit_info':json.loads(row['explicit_info_json']),
            'implicit_traits':json.loads(row['implicit_traits_json']),'profile_timestamp_ms':row['profile_timestamp_ms']}
        return project, {**base,'group':'profile','title':base['summary'],'content':base['summary']}
    for key in ('session_id','parent_type','parent_id','sender_ids','timestamp'):
        value = row.get(key)
        metadata[key] = value.isoformat() if hasattr(value, 'isoformat') else value
    return project, {**base,'entry_id':row['entry_id'],'title':row.get('subject') or row['entry_id'],
        'group':PurePosixPath(path).stem.removeprefix('episode-'),'content':row['episode'],
        'tokens':row.get('episode_tokens') or '', 'embedding':row.get('vector'),'subject_embedding':row.get('subject_vector')}


def _read_lance(conn, scope, directory, counts):
    import lancedb
    projects = {}
    client = lancedb.connect(directory)
    for folder in sorted(directory.glob('*.lance')):
        name = folder.stem
        table = client.open_table(name)
        storage.archive_blob(conn, scope, 'arrow_schema', name, table.schema.serialize().to_pybytes(), 'application/vnd.apache.arrow.schema')
        total = 0
        for number, batch in enumerate(table.search().limit(None).to_batches(batch_size=256)):
            # Keep all Arrow types and all original fields, including unused tables or deprecated rows.
            storage.archive_blob(conn, scope, 'arrow_batch', f'{name}:{number}', batch.serialize().to_pybytes(), 'application/vnd.apache.arrow.record-batch')
            total += batch.num_rows
            if name not in ('episode','user_profile'):
                continue
            groups = defaultdict(list)
            for row in batch.to_pylist():
                if name == 'episode' and row.get('deprecated_by') is not None:
                    continue
                project, record = _record(name, row, scope[0])
                groups[project].append(record)
            for project, records in groups.items():
                if project not in projects:
                    projects[project] = uuid.uuid4().hex
                    storage.vector_begin(conn, scope, project, projects[project])
                storage.vector_chunk(conn, scope, project, projects[project], records)
        if total != table.count_rows():
            raise ValueError('EverOS Lance snapshot row count mismatch')
        counts['legacy_rows'] += total
    for project, generation in projects.items():
        counts['vector_rows'] += storage.vector_commit(conn, scope, project, generation)


def import_everos_snapshot(connection, user_id, directory):
    """Copy every legacy index signature without changing the source or committing the caller's work."""
    root = Path(directory).absolute()
    if root.is_symlink() or any(p.is_symlink() for p in root.parents):
        raise ValueError('EverOS source contains a symlink')
    if not root.is_dir() or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', user_id):
        raise ValueError('Invalid EverOS owner or snapshot directory')
    indexes = root/'.index'
    totals = dict(indexes=0, queue_rows=0, vector_rows=0, legacy_rows=0)
    if indexes.is_symlink():
        raise ValueError('EverOS index is a symlink')
    if not indexes.exists():
        return totals
    if not indexes.is_dir():
        raise ValueError('Invalid EverOS index directory')
    _require_transaction(connection)
    owner = hashlib.sha256(user_id.encode()).hexdigest()
    with connection.transaction(), connection.cursor(row_factory=tuple_row) as cursor:
        conn = Connection(cursor, {})
        lock_schema_transaction(cursor, SCHEMA_LOCK)
        storage.ensure_schema(conn)
        for index in sorted(indexes.iterdir()):
            if index.is_symlink() or not index.is_dir() or not re.fullmatch(r'[a-f0-9]{24}',index.name):
                raise ValueError('Unexpected EverOS index signature or symlink')
            scope = owner,index.name
            before = _manifest(index)
            with _private_copy(index, before) as copy:
                digest = _fingerprint(copy)
                receipt = storage.load_blob(conn, scope, 'import_receipt','v1')
                if receipt is not None:
                    saved = json.loads(receipt['payload'])
                    if saved['digest'] != digest:
                        raise ValueError('A different EverOS snapshot already occupies this scope')
                    counts = saved['counts']
                else:
                    for table in ('everos_vectors','everos_queue','everos_vector_stage','everos_archive_rows','everos_archive_blobs'):
                        if cursor.execute('SELECT 1 FROM '+table+' WHERE owner_hash=%s AND index_signature=%s LIMIT 1',scope).fetchone():
                            raise ValueError('Target EverOS scope already contains data')
                    counts = dict(indexes=1, queue_rows=0, vector_rows=0, legacy_rows=0)
                    sqlite_dir, lance_dir = copy/'sqlite', copy/'lancedb'
                    if not sqlite_dir.is_dir() and not lance_dir.is_dir():
                        raise ValueError('Missing legacy EverOS storage')
                    if sqlite_dir.is_dir():
                        _read_sqlite(conn, scope, sqlite_dir, counts)
                    if lance_dir.is_dir():
                        _read_lance(conn, scope, lance_dir, counts)
                    storage.archive_blob(conn, scope, 'import_receipt','v1',json.dumps({'digest':digest,'counts':counts}).encode(),'application/json')
                for metric in totals:
                    totals[metric] += counts[metric]
    return totals


def sources(root, owners):
    """Accept current and known historical layouts, never infer ownership from contents."""
    root = Path(root).absolute()
    if root.resolve() != root or not root.is_dir():
        raise ValueError('snapshot root is missing or contains a symlink')
    owners = set(owners)
    hashes = {hashlib.sha256(uid.encode()).hexdigest(): uid for uid in owners}
    found = {}
    def add(kind, uid, path):
        if path.resolve() != path:
            raise ValueError('snapshot path contains a symlink')
        if not path.exists():
            return
        if kind == 'chroma' and not (path/'chroma.sqlite3').is_file():
            if path.is_dir() and not any(path.iterdir()):
                return
            raise ValueError('missing Chroma metadata in snapshot')
        if kind == 'everos' and not (path/'.index').exists():
            return
        if uid is None and kind != 'aip':
            raise ValueError('unresolved snapshot owner')
        if (kind, uid) in found:
            raise ValueError('ambiguous account store snapshots')
        found[kind,uid] = path

    add('aip',None,root/'system_identity'/'aip-tasks.sqlite3')
    accounts = root/'users'
    if accounts.exists():
        if accounts.resolve() != accounts:
            raise ValueError('account directory is a symlink')
        for owner in sorted(accounts.iterdir()):
            for kind,folder in (('chroma','file_rag'),('everos','everos')):
                add(kind,owner.name if owner.name in owners else None,owner/folder)
    for kind,folder in (('chroma','file_rag'),('everos','everos')):
        old = root/folder
        if old.exists():
            if old.resolve() != old:
                raise ValueError('legacy directory is a symlink')
            for entry in sorted(old.iterdir()):
                add(kind,hashes.get(entry.name),entry)
    return [(kind,uid,path) for (kind,uid),path in found.items()]


def legacy_signature(embedding,model):
    fields=('provider','base_url','model','dimensions','context_tokens','context_enabled','multimodal_enabled')
    # The historical parser signature keeps previously published embeddings valid.
    return hashlib.sha256(dump(['unstructured-chroma-document-images-v2',{key:embedding.get(key) for key in fields},
        {key:model.get(key) for key in ('provider','base_url','model','vision_enabled','audio_enabled')}]).encode()).hexdigest()


def migrate_postgres_publications(connection):
    """Convert stored manifests and signatures in the caller's offline transaction."""
    lock_schema_transaction(connection, SCHEMA_LOCK)
    lock_schema_transaction(connection, WRITE_LOCK)
    from avatar.models.embedding import EmbeddingSettings
    from avatar.models.model_settings import ModelSettingsService

    db = Connection(connection, {})
    documents = db.execute("""SELECT * FROM file_rag_documents
        WHERE legacy_backfill=1 OR (active_publication='' AND collection<>'') FOR UPDATE""").fetchall()
    count = lexical_count = 0
    for doc in documents:
        owner, source, file_id, collection = (doc[k] for k in ('user_id','source','file_id','collection'))
        ids = [row[0] for row in db.execute("""SELECT record_id FROM file_rag_nodes WHERE user_id=?
            AND source=? AND file_id=? AND collection=? ORDER BY ordinal""", (owner,source,file_id,collection))]
        if not ids:
            raise ValueError('Legacy publication has no vector references')
        rows = {row['record_id']:row for row in db.execute("""SELECT record_id,document,metadata
            FROM rag_vector_records WHERE user_id=? AND collection=? AND record_id=ANY(?::text[])""", (owner,collection,ids))}
        if set(rows) != set(ids):
            raise ValueError('Legacy publication has missing vectors')
        publication = doc['active_publication']
        if publication:
            current = db.execute("""SELECT record_ids,manifest FROM rag_publications WHERE id=?
                AND user_id=? AND source=? AND file_id=? AND collection=?""",
                (publication,owner,source,file_id,collection)).fetchone()
            if not current or json.loads(current['record_ids']) != ids:
                raise ValueError('Legacy publication ownership or vector references differ')
            manifest = json.loads(current['manifest'])
            manifest.pop('legacy',None)
            db.execute('UPDATE rag_publications SET lexical_ready=1,legacy=0,manifest=? WHERE id=?',
                       (dump(manifest),publication))
        else:
            publication = uuid.uuid4().hex
            manifest = dict(expected_chunks=len(ids), processed_chunks=len(ids), expected_pages=None,
                            processed_pages=None, expected_images=None, processed_images=None, complete=None)
            db.execute("""INSERT INTO rag_publications
                (id,user_id,source,file_id,revision,signature,collection,coverage,record_ids,manifest,lexical_ready,legacy,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,1,0,?)""",
                (publication,owner,source,file_id,doc['revision'],doc['signature'],collection,doc['coverage'],
                 dump(ids),dump(manifest),doc['updated_at']))
        LexicalStore.stage_in(db,owner,collection,(ids,[rows[key]['metadata'] for key in ids],[rows[key]['document'] for key in ids]))
        db.execute('UPDATE file_rag_documents SET active_publication=?,legacy_backfill=0 WHERE source=? AND file_id=? AND user_id=?',
                   (publication,source,file_id,owner))
        count += 1
        lexical_count += len(ids)
    signatures = 0
    for (owner,) in connection.execute("SELECT DISTINCT user_id FROM file_rag_documents WHERE signature<>''"):
        embedding = EmbeddingSettings.current(db,owner)
        model = ModelSettingsService.current(db,owner)
        old, new = legacy_signature(embedding,model), signature(embedding,model)
        signatures += db.execute("UPDATE file_rag_documents SET signature=? WHERE user_id=? AND signature=?",
                                 (new,owner,old)).rowcount
        db.execute('UPDATE rag_publications SET signature=? WHERE user_id=? AND signature=?',(new,owner,old))
    return {'publications':count,'lexical_records':lexical_count,'signatures':signatures}


def verify_publications(conn):
    rows = conn.execute("""SELECT d.user_id,d.collection,d.source,d.file_id,p.user_id,p.collection,
               p.source,p.file_id,p.record_ids FROM file_rag_documents d
        LEFT JOIN rag_publications p ON d.active_publication=p.id
        WHERE COALESCE(d.active_publication,'') != ''""").fetchall()
    total = 0
    for uid,collection,source,file_id,owner,target,origin,identifier,serialized in rows:
        if (uid,collection,source,file_id) != (owner,target,origin,identifier):
            raise ValueError('active RAG publication ownership mismatch')
        expected = json.loads(serialized)
        nodes = conn.execute("""SELECT user_id,collection,record_id FROM file_rag_nodes
            WHERE source=%s AND file_id=%s ORDER BY ordinal""",(source,file_id)).fetchall()
        if nodes != [(uid,collection,key) for key in expected]:
            raise ValueError('active RAG publication differs from file nodes')
        if expected:
            actual = conn.execute("""SELECT record_id FROM rag_vector_records
                WHERE user_id=%s AND collection=%s AND record_id=ANY(%s::text[])""",
                (uid,collection,expected)).fetchall()
            if {row[0] for row in actual} != set(expected):
                raise ValueError('active RAG publication references missing vectors')
        total += len(expected)
    return total


def migrate(root, url):

    report = {'aip_rows':0, 'chroma_rows':0, 'chroma_stores':0, 'everos':{}}
    with psycopg.connect(require_database_url(url)) as db:
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended(current_schema()||':local-store-import',0))")
        # 与应用初始化顺序一致：先锁定目标 schema，再创建各组件的表。
        lock_schema_transaction(db, SCHEMA_LOCK)
        owned = [r[0] for r in db.execute('SELECT id FROM users')]
        stores = sources(root,owned)
        initialize_system_tasks(db)
        install_vector_schema(db)
        storage.ensure_schema(db)
        for kind,uid,path in stores:
            if kind == 'aip':
                result = import_system_tasks(db,path)
                report['aip_rows'] = result['count']
                report['aip_sha256'] = result['sha256']
            elif kind == 'chroma':
                report['chroma_rows'] += import_chroma_snapshot(db,uid,path)
                report['chroma_stores'] += 1
            else:
                for key,value in import_everos_snapshot(db,uid,path).items():
                    if isinstance(value,int):
                        report['everos'][key] = report['everos'].get(key,0)+value
        report['publications'] = migrate_postgres_publications(db)
        report['active_publication_references'] = verify_publications(db)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--snapshot',type=Path,help='冻结数据目录的私有副本')
    mode.add_argument('--postgres-only',action='store_true',help='离线转换已导入 PostgreSQL 的资料索引')
    mode.add_argument('--storage-layout',action='store_true',help='离线迁移原件及身份目录')
    args = parser.parse_args()
    live = Path(os.getenv('AVATAR_DATA_DIR',Path(__file__).resolve().parents[1]/'data')).resolve()
    if args.storage_layout:
        from avatar.core.db import Database
        from avatar.core.user_storage import migrate_user_storage
        with Database.open_existing(live, os.getenv('DATABASE_URL','')) as database:
            migrate_user_storage(database)
        print('文件目录迁移已完成')
        return
    if args.postgres_only:
        from avatar.core.migrations.chat_generations import restore_legacy_generations
        with psycopg.connect(require_database_url(os.getenv('DATABASE_URL',''))) as db:
            report = migrate_postgres_publications(db)
            restore_legacy_generations(Connection(db, {}))
            report['active_publication_references'] = verify_publications(db)
        print(json.dumps(report))
        return
    if args.snapshot.resolve() == live:
        parser.error('请先制作离线快照，将 --snapshot 指向副本')
    print(json.dumps(migrate(args.snapshot,os.getenv('DATABASE_URL','')),ensure_ascii=False))


if __name__ == '__main__':
    main()
