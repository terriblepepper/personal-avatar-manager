"""PostgreSQL storage for the EverOS bridge; workers never receive its DSN."""
import json
import math
import re
import time

from avatar.core.postgres import Connection, lock_schema_transaction


DDL = (
    """CREATE TABLE IF NOT EXISTS everos_queue(
      owner_hash TEXT NOT NULL,index_signature TEXT NOT NULL,project_id TEXT NOT NULL,
      md_path TEXT NOT NULL,kind TEXT NOT NULL,change_type TEXT NOT NULL DEFAULT 'modified',
      lsn BIGINT NOT NULL DEFAULT 0,mtime DOUBLE PRECISION NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',retryable BOOLEAN,retry_count INTEGER NOT NULL DEFAULT 0,
      error TEXT NOT NULL DEFAULT '',first_seen_at TEXT NOT NULL DEFAULT '',
      last_changed_at TEXT NOT NULL DEFAULT '',last_attempt_at TEXT,lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
      updated_at DOUBLE PRECISION NOT NULL DEFAULT 0,
      PRIMARY KEY(owner_hash,index_signature,project_id,md_path))""",
    "ALTER TABLE everos_queue ADD COLUMN IF NOT EXISTS first_seen_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE everos_queue ADD COLUMN IF NOT EXISTS last_changed_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE everos_queue ADD COLUMN IF NOT EXISTS last_attempt_at TEXT",
    """CREATE INDEX IF NOT EXISTS everos_queue_pending
      ON everos_queue(owner_hash,index_signature,project_id,status,mtime,md_path)""",
    """CREATE TABLE IF NOT EXISTS everos_vectors(
      owner_hash TEXT NOT NULL,index_signature TEXT NOT NULL,project_id TEXT NOT NULL,
      kind TEXT NOT NULL,record_id TEXT NOT NULL,entry_id TEXT NOT NULL DEFAULT '',
      md_path TEXT NOT NULL DEFAULT '',content_sha256 TEXT NOT NULL DEFAULT '',
      group_name TEXT NOT NULL DEFAULT '',title TEXT NOT NULL DEFAULT '',
      summary TEXT NOT NULL DEFAULT '',content TEXT NOT NULL,tokens TEXT NOT NULL DEFAULT '',
      metadata JSONB NOT NULL DEFAULT '{}'::jsonb,embedding REAL[],
      subject_embedding REAL[],updated_at DOUBLE PRECISION NOT NULL DEFAULT 0,
      PRIMARY KEY(owner_hash,index_signature,project_id,record_id))""",
    """CREATE INDEX IF NOT EXISTS everos_vectors_scope
      ON everos_vectors(owner_hash,index_signature,project_id,kind,md_path)""",
    """CREATE TABLE IF NOT EXISTS everos_archive_rows(
      owner_hash TEXT NOT NULL,index_signature TEXT NOT NULL,source_table TEXT NOT NULL,
      source_row_id TEXT NOT NULL,row_data JSONB NOT NULL,source_schema TEXT NOT NULL DEFAULT '',
      digest TEXT NOT NULL DEFAULT '',PRIMARY KEY(owner_hash,index_signature,source_table,source_row_id))""",
    """CREATE TABLE IF NOT EXISTS everos_archive_blobs(
      owner_hash TEXT NOT NULL,index_signature TEXT NOT NULL,kind TEXT NOT NULL,
      record_id TEXT NOT NULL,media_type TEXT NOT NULL DEFAULT 'application/octet-stream',
      payload BYTEA NOT NULL,digest TEXT NOT NULL DEFAULT '',
      PRIMARY KEY(owner_hash,index_signature,kind,record_id))""",
    """CREATE TABLE IF NOT EXISTS everos_vector_stage(
      owner_hash TEXT NOT NULL,index_signature TEXT NOT NULL,project_id TEXT NOT NULL,
      generation TEXT NOT NULL,kind TEXT NOT NULL,record_id TEXT NOT NULL,
      entry_id TEXT NOT NULL DEFAULT '',md_path TEXT NOT NULL DEFAULT '',
      content_sha256 TEXT NOT NULL DEFAULT '',group_name TEXT NOT NULL DEFAULT '',
      title TEXT NOT NULL DEFAULT '',summary TEXT NOT NULL DEFAULT '',content TEXT NOT NULL,
      tokens TEXT NOT NULL DEFAULT '',metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
      embedding REAL[],subject_embedding REAL[],
      PRIMARY KEY(owner_hash,index_signature,project_id,generation,record_id))""",
)

MIGRATIONS = (
    "ALTER TABLE everos_vectors ADD COLUMN IF NOT EXISTS entry_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE everos_vectors ADD COLUMN IF NOT EXISTS md_path TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE everos_vectors ADD COLUMN IF NOT EXISTS content_sha256 TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE everos_vectors ADD COLUMN IF NOT EXISTS subject_embedding REAL[]",
    "ALTER TABLE everos_vectors ADD COLUMN IF NOT EXISTS tokens TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE everos_vectors ALTER COLUMN embedding DROP NOT NULL",
    "ALTER TABLE everos_vector_stage ADD COLUMN IF NOT EXISTS entry_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE everos_vector_stage ADD COLUMN IF NOT EXISTS md_path TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE everos_vector_stage ADD COLUMN IF NOT EXISTS content_sha256 TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE everos_vector_stage ADD COLUMN IF NOT EXISTS subject_embedding REAL[]",
    "ALTER TABLE everos_vector_stage ADD COLUMN IF NOT EXISTS tokens TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE everos_vector_stage ALTER COLUMN embedding DROP NOT NULL",
)


def ensure_schema(conn):
    lock_schema_transaction(conn.raw if isinstance(conn, Connection) else conn, 118163509)
    for statement in DDL:
        conn.execute(statement)
    for statement in MIGRATIONS:
        conn.execute(statement)


def _retryable(value):
    return None if value is None else 'true' if value else 'false'


def _scope(scope):
    owner_hash,index_signature=scope
    if not re.fullmatch(r'[a-f0-9]{64}',owner_hash) or not re.fullmatch(r'[a-f0-9]{24}',index_signature):
        raise ValueError('Invalid EverOS storage scope')
    return owner_hash,index_signature


def _stamp():
    from datetime import datetime,timezone
    return datetime.now(timezone.utc).isoformat()


def _next_lsn(conn,owner_hash,index_signature):
    row=conn.execute('SELECT coalesce(max(lsn),0)+1 FROM everos_queue WHERE owner_hash=? AND index_signature=?',(owner_hash,index_signature)).fetchone()
    return int(row[0])


def queue_upsert(conn,scope,project,rows):
    owner_hash,index_signature=_scope(scope);stamp=time.time();changed=0
    for row in rows:
        change_type=str(row.get('change_type','modified'))
        lsn=_next_lsn(conn,owner_hash,index_signature)
        result=conn.execute("""INSERT INTO everos_queue(owner_hash,index_signature,project_id,md_path,kind,change_type,lsn,mtime,status,retryable,retry_count,error,first_seen_at,last_changed_at,last_attempt_at,lease_until,updated_at)
          VALUES(?,?,?,?,?,?,?,?, 'pending',NULL,0,'',?,?,NULL,0,?)
          ON CONFLICT(owner_hash,index_signature,project_id,md_path) DO UPDATE SET
            kind=excluded.kind,change_type=excluded.change_type,lsn=excluded.lsn,mtime=excluded.mtime,
            status='pending',retryable=NULL,retry_count=0,error='',last_changed_at=excluded.last_changed_at,
            last_attempt_at=NULL,lease_until=0,updated_at=excluded.updated_at
          WHERE everos_queue.mtime IS DISTINCT FROM excluded.mtime
             OR everos_queue.change_type IS DISTINCT FROM excluded.change_type""",
          (owner_hash,index_signature,project,row['md_path'],row['kind'],change_type,lsn,float(row.get('mtime',0)),_stamp(),_stamp(),stamp))
        changed+=max(0,int(result.rowcount or 0))
    return changed


def queue_sync(conn,scope,project,rows):
    owner_hash,index_signature=_scope(scope);current={row['md_path'] for row in rows};changed=queue_upsert(conn,scope,project,rows)
    existing=conn.execute("""SELECT md_path,kind FROM everos_queue
      WHERE owner_hash=? AND index_signature=? AND project_id=? AND change_type!='deleted'""",
      (owner_hash,index_signature,project)).fetchall()
    deleted=[{'md_path':row[0],'kind':row[1],'mtime':0,'change_type':'deleted'} for row in existing if row[0] not in current]
    changed+=queue_upsert(conn,scope,project,deleted)
    conn.execute("""UPDATE everos_queue SET status='pending',error='',lease_until=0,updated_at=?
      WHERE owner_hash=? AND index_signature=? AND project_id=? AND status='failed' AND retryable=TRUE""",
      (time.time(),owner_hash,index_signature,project))
    return changed


def import_queue_rows(conn,scope,project,rows):
    owner_hash,index_signature=_scope(scope);stamp=time.time()
    for row in rows:
        status=str(row.get('status','pending'))
        if status=='processing':status='pending'
        if status not in ('pending','done','failed'):raise ValueError('Invalid EverOS queue status')
        conn.execute("""INSERT INTO everos_queue(owner_hash,index_signature,project_id,md_path,kind,change_type,lsn,mtime,status,retryable,retry_count,error,first_seen_at,last_changed_at,last_attempt_at,lease_until,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,CAST(? AS boolean),?,?,?,?,?,0,?)
          ON CONFLICT(owner_hash,index_signature,project_id,md_path) DO UPDATE SET
            kind=excluded.kind,change_type=excluded.change_type,lsn=excluded.lsn,mtime=excluded.mtime,
            status=excluded.status,retryable=excluded.retryable,retry_count=excluded.retry_count,
            error=excluded.error,first_seen_at=excluded.first_seen_at,last_changed_at=excluded.last_changed_at,
            last_attempt_at=excluded.last_attempt_at,lease_until=0,updated_at=excluded.updated_at""",
          (owner_hash,index_signature,project,row['md_path'],row['kind'],str(row.get('change_type','modified')),
           int(row.get('lsn',0)),float(row['mtime']),status,_retryable(row.get('retryable')),
           int(row.get('retry_count',0)),str(row.get('error',''))[:1000],str(row.get('first_seen_at','')),
           str(row.get('last_changed_at','')),row.get('last_attempt_at'),stamp))
    return len(rows)


def queue_claim(conn,scope,project,limit=50):
    owner_hash,index_signature=_scope(scope);stamp=time.time()
    conn.execute("""UPDATE everos_queue SET status='pending',lease_until=0,updated_at=?
      WHERE owner_hash=? AND index_signature=? AND project_id=? AND status='processing' AND lease_until<?""",
      (stamp,owner_hash,index_signature,project,stamp))
    rows=conn.execute("""WITH picked AS (
        SELECT owner_hash,index_signature,project_id,md_path FROM everos_queue
        WHERE owner_hash=? AND index_signature=? AND project_id=? AND status='pending'
        ORDER BY lsn,md_path FOR UPDATE SKIP LOCKED LIMIT ?)
      UPDATE everos_queue q SET status='processing',last_attempt_at=?,lease_until=?,updated_at=?
      FROM picked p WHERE q.owner_hash=p.owner_hash AND q.index_signature=p.index_signature
        AND q.project_id=p.project_id AND q.md_path=p.md_path
      RETURNING q.md_path,q.kind,q.change_type,q.retry_count,q.mtime""",
      (owner_hash,index_signature,project,max(1,min(int(limit),500)),_stamp(),stamp+300,stamp)).fetchall()
    return [dict(row) for row in rows]


def queue_complete(conn,scope,project,rows):
    owner_hash,index_signature=_scope(scope);stamp=time.time()
    for row in rows:
        conn.execute("""UPDATE everos_queue SET status=?,retryable=CAST(? AS boolean),retry_count=?,error=?,lease_until=0,updated_at=?
          WHERE owner_hash=? AND index_signature=? AND project_id=? AND md_path=?""",
          (row.get('status','done'),_retryable(row.get('retryable')),int(row.get('retry_count',0)),str(row.get('error',''))[:1000],stamp,
           owner_hash,index_signature,project,row['md_path']))


def _embedding(value):
    if value is None:return None
    result=[float(item) for item in value]
    if not result or any(not math.isfinite(item) for item in result):raise ValueError('Invalid EverOS vector')
    return result


def _record_values(record):
    return (record['kind'],record['record_id'],record.get('entry_id',''),record.get('md_path',''),
            record.get('content_sha256',''),record.get('group',''),record.get('title',''),
            record.get('summary',''),record['content'],record.get('tokens',''),
            json.dumps(record.get('metadata',{}),ensure_ascii=False,sort_keys=True),
            _embedding(record.get('embedding')),_embedding(record.get('subject_embedding')))


def _sha256(payload):
    import hashlib
    return hashlib.sha256(payload).hexdigest()


def archive_rows(conn,scope,table,rows,source_schema=''):
    owner_hash,index_signature=_scope(scope)
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}',table):raise ValueError('Invalid archive table')
    for row in rows:
        record_id=str(row['source_row_id']);payload=json.dumps(row.get('row_data',{}),ensure_ascii=False,sort_keys=True)
        digest=row.get('digest') or _sha256(payload.encode())
        conn.execute("""INSERT INTO everos_archive_rows(owner_hash,index_signature,source_table,source_row_id,row_data,source_schema,digest)
          VALUES(?,?,?,?,CAST(? AS jsonb),?,?)
          ON CONFLICT(owner_hash,index_signature,source_table,source_row_id) DO UPDATE SET
            row_data=excluded.row_data,source_schema=excluded.source_schema,digest=excluded.digest""",
          (owner_hash,index_signature,table,record_id,payload,source_schema,digest))
    return len(rows)


def archive_blob(conn,scope,kind,record_id,payload,media_type='application/octet-stream'):
    owner_hash,index_signature=_scope(scope)
    if not isinstance(payload,(bytes,bytearray,memoryview)):raise TypeError('Archive payload must be bytes')
    raw=bytes(payload);conn.execute("""INSERT INTO everos_archive_blobs(owner_hash,index_signature,kind,record_id,media_type,payload,digest)
      VALUES(?,?,?,?,?,?,?)
      ON CONFLICT(owner_hash,index_signature,kind,record_id) DO UPDATE SET
        media_type=excluded.media_type,payload=excluded.payload,digest=excluded.digest""",
      (owner_hash,index_signature,kind,record_id,media_type,raw,_sha256(raw)))
    return len(raw)


def load_blob(conn,scope,kind,record_id):
    owner_hash,index_signature=_scope(scope)
    row=conn.execute('SELECT media_type,payload,digest FROM everos_archive_blobs WHERE owner_hash=? AND index_signature=? AND kind=? AND record_id=?',
                     (owner_hash,index_signature,kind,record_id)).fetchone()
    if not row:return None
    payload=bytes(row[1])
    if _sha256(payload)!=row[2]:raise ValueError('Archive blob digest mismatch')
    return {'media_type':row[0],'payload':payload,'digest':row[2]}


def existing_vectors(conn,scope,project,md_path=None):
    owner_hash,index_signature=_scope(scope)
    sql="""SELECT kind,record_id,entry_id,md_path,content_sha256,group_name,title,summary,content,tokens,metadata,embedding,subject_embedding
      FROM everos_vectors WHERE owner_hash=? AND index_signature=? AND project_id=?"""
    args=[owner_hash,index_signature,project]
    if md_path is not None:sql+=' AND md_path=?';args.append(md_path)
    sql+=' ORDER BY md_path,record_id'
    return [dict(row) for row in conn.execute(sql,args).fetchall()]


def copy_vectors(conn,scope,source,target):
    owner_hash,index_signature=_scope(scope)
    rows=existing_vectors(conn,scope,source)
    for row in rows:
        row['embedding']=row.get('embedding');row['subject_embedding']=row.get('subject_embedding')
        values=_record_values(row)
        conn.execute("""INSERT INTO everos_vectors(owner_hash,index_signature,project_id,kind,record_id,entry_id,md_path,content_sha256,group_name,title,summary,content,tokens,metadata,embedding,subject_embedding,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,CAST(? AS jsonb),CAST(? AS real[]),CAST(? AS real[]),?)
          ON CONFLICT(owner_hash,index_signature,project_id,record_id) DO UPDATE SET
            kind=excluded.kind,entry_id=excluded.entry_id,md_path=excluded.md_path,content_sha256=excluded.content_sha256,
            group_name=excluded.group_name,title=excluded.title,summary=excluded.summary,content=excluded.content,
            tokens=excluded.tokens,metadata=excluded.metadata,embedding=excluded.embedding,subject_embedding=excluded.subject_embedding,updated_at=excluded.updated_at""",
          (owner_hash,index_signature,target,*values,time.time()))
    return len(rows)


def upsert_file_vectors(conn,scope,project,md_path,records):
    owner_hash,index_signature=_scope(scope);stamp=time.time()
    conn.execute('DELETE FROM everos_vectors WHERE owner_hash=? AND index_signature=? AND project_id=? AND md_path=?',(owner_hash,index_signature,project,md_path))
    for record in records:
        values=_record_values(record)
        conn.execute("""INSERT INTO everos_vectors(owner_hash,index_signature,project_id,kind,record_id,entry_id,md_path,content_sha256,group_name,title,summary,content,tokens,metadata,embedding,subject_embedding,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,CAST(? AS jsonb),CAST(? AS real[]),CAST(? AS real[]),?)""",
          (owner_hash,index_signature,project,*values,stamp))
    return len(records)


def delete_file_vectors(conn,scope,project,md_path):
    owner_hash,index_signature=_scope(scope)
    result=conn.execute('DELETE FROM everos_vectors WHERE owner_hash=? AND index_signature=? AND project_id=? AND md_path=?',(owner_hash,index_signature,project,md_path))
    return int(result.rowcount or 0)


def vector_begin(conn,scope,project,generation):
    owner_hash,index_signature=_scope(scope)
    if not re.fullmatch(r'[a-f0-9]{32}',generation):raise ValueError('Invalid vector generation')
    conn.execute('DELETE FROM everos_vector_stage WHERE owner_hash=? AND index_signature=? AND project_id=?',(owner_hash,index_signature,project))


def vector_chunk(conn,scope,project,generation,records):
    owner_hash,index_signature=_scope(scope)
    if not re.fullmatch(r'[a-f0-9]{32}',generation):raise ValueError('Invalid vector generation')
    for record in records:
        values=_record_values(record)
        conn.execute("""INSERT INTO everos_vector_stage(owner_hash,index_signature,project_id,generation,kind,record_id,entry_id,md_path,content_sha256,group_name,title,summary,content,tokens,metadata,embedding,subject_embedding)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,CAST(? AS jsonb),CAST(? AS real[]),CAST(? AS real[]))""",
          (owner_hash,index_signature,project,generation,*values))
    return len(records)


def vector_commit_file(conn,scope,project,generation,md_path):
    owner_hash,index_signature=_scope(scope);stamp=time.time()
    if not re.fullmatch(r'[a-f0-9]{32}',generation):raise ValueError('Invalid vector generation')
    count=conn.execute('SELECT count(*) FROM everos_vector_stage WHERE owner_hash=? AND index_signature=? AND project_id=? AND generation=? AND md_path=?',
                       (owner_hash,index_signature,project,generation,md_path)).fetchone()[0]
    conn.execute('DELETE FROM everos_vectors WHERE owner_hash=? AND index_signature=? AND project_id=? AND md_path=?',(owner_hash,index_signature,project,md_path))
    conn.execute("""INSERT INTO everos_vectors(owner_hash,index_signature,project_id,kind,record_id,entry_id,md_path,content_sha256,group_name,title,summary,content,tokens,metadata,embedding,subject_embedding,updated_at)
      SELECT owner_hash,index_signature,project_id,kind,record_id,entry_id,md_path,content_sha256,group_name,title,summary,content,tokens,metadata,embedding,subject_embedding,?
      FROM everos_vector_stage WHERE owner_hash=? AND index_signature=? AND project_id=? AND generation=? AND md_path=?""",
      (stamp,owner_hash,index_signature,project,generation,md_path))
    conn.execute('DELETE FROM everos_vector_stage WHERE owner_hash=? AND index_signature=? AND project_id=?',(owner_hash,index_signature,project))
    return int(count)


def vector_commit(conn,scope,project,generation):
    owner_hash,index_signature=_scope(scope);stamp=time.time()
    if not re.fullmatch(r'[a-f0-9]{32}',generation):raise ValueError('Invalid vector generation')
    count=conn.execute('SELECT count(*) FROM everos_vector_stage WHERE owner_hash=? AND index_signature=? AND project_id=? AND generation=?',
                       (owner_hash,index_signature,project,generation)).fetchone()[0]
    conn.execute('DELETE FROM everos_vectors WHERE owner_hash=? AND index_signature=? AND project_id=?',(owner_hash,index_signature,project))
    conn.execute("""INSERT INTO everos_vectors(owner_hash,index_signature,project_id,kind,record_id,entry_id,md_path,content_sha256,group_name,title,summary,content,tokens,metadata,embedding,subject_embedding,updated_at)
      SELECT owner_hash,index_signature,project_id,kind,record_id,entry_id,md_path,content_sha256,group_name,title,summary,content,tokens,metadata,embedding,subject_embedding,?
      FROM everos_vector_stage WHERE owner_hash=? AND index_signature=? AND project_id=? AND generation=?""",
      (stamp,owner_hash,index_signature,project,generation))
    conn.execute('DELETE FROM everos_vector_stage WHERE owner_hash=? AND index_signature=? AND project_id=?',(owner_hash,index_signature,project))
    return int(count)


def archive_source(conn,scope,table,source_schema,source_count,digest):
    owner_hash,index_signature=_scope(scope)
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}',table):raise ValueError('Invalid archive table')
    conn.execute("""INSERT INTO everos_archive_rows(owner_hash,index_signature,source_table,source_row_id,row_data,source_schema,digest)
      VALUES(?,?,?,?,CAST(? AS jsonb),?,?)
      ON CONFLICT(owner_hash,index_signature,source_table,source_row_id) DO UPDATE SET
        row_data=excluded.row_data,source_schema=excluded.source_schema,digest=excluded.digest""",
      (owner_hash,index_signature,table,'__source__',json.dumps({'source_count':int(source_count),'digest':digest},sort_keys=True),source_schema,digest))


def replace_vectors(conn,scope,project,records):
    generation=time.time_ns().to_bytes(16,'big').hex()[-32:]
    vector_begin(conn,scope,project,generation)
    for start in range(0,len(records),32):vector_chunk(conn,scope,project,generation,records[start:start+32])
    return vector_commit(conn,scope,project,generation)


def _tokens(value):
    return set(re.findall(r'[a-z0-9_]+|[\u4e00-\u9fff]',str(value).casefold()))


def _cosine(left,right):
    if not left or not right or len(left)!=len(right):return -1.0
    dot=sum(a*b for a,b in zip(left,right,strict=True));ln=sum(a*a for a in left)**.5;rn=sum(b*b for b in right)**.5
    return dot/(ln*rn) if ln and rn else 0.0


def _public_row(row):
    result={key:value for key,value in row.items() if key not in ('embedding','subject_embedding')}
    result['metadata']=result.get('metadata') or {}
    return result


def search_vectors(conn,scope,project,query,method='keyword',embedding=None,limit=6,include_profile=False,query_tokens=None):
    owner_hash,index_signature=_scope(scope)
    rows=existing_vectors(conn,scope,project)
    if not include_profile:rows=[row for row in rows if row['kind']!='user_profile']
    terms=set(query_tokens) if query_tokens else _tokens(query)
    documents=[]
    for row in rows:
        tokens=str(row.get('tokens','')).split()
        documents.append(tokens or list(_tokens(' '.join((row.get('title',''),row.get('summary',''),row.get('content',''))))))
    total=len(documents);avg_length=sum(len(tokens) for tokens in documents)/max(1,total);document_frequency={term:sum(term in tokens for tokens in documents) for term in terms}
    keyword=[];vector=[]
    for row,tokens in zip(rows,documents,strict=True):
        frequencies={term:tokens.count(term) for term in terms};score=0.0
        for term,frequency in frequencies.items():
            if not frequency:continue
            df=document_frequency[term];inverse=math.log(1+(total-df+.5)/(df+.5))
            score+=inverse*frequency*2.2/(frequency+1.2*(.25+.75*len(tokens)/max(1,avg_length)))
        if score:keyword.append((score,row['record_id'],row))
        if method in ('vector','hybrid') and embedding is not None:
            dense=max(_cosine(embedding,row.get('embedding') or []),_cosine(embedding,row.get('subject_embedding') or []))
            if dense>=0:vector.append((dense,row['record_id'],row))
    keyword.sort(key=lambda item:(-item[0],item[1]));vector.sort(key=lambda item:(-item[0],item[1]))
    if method=='keyword':ranked=[(score,row) for score,_,row in keyword]
    elif method=='vector':ranked=[(score,row) for score,_,row in vector]
    elif method=='hybrid':
        scores={}
        for factor,items in ((1.0,keyword),(1.0,vector)):
            for rank,(_,record_id,row) in enumerate(items,1):scores.setdefault(record_id,[0.0,row])[0]+=factor/(60+rank)
        ranked=[(score,row) for score,row in scores.values()]
        ranked.sort(key=lambda item:(-item[0],item[1]['record_id']))
    else:raise ValueError('Invalid EverOS search method')
    return [{'score':float(score),**_public_row(row)} for score,row in ranked[:max(1,min(int(limit),50))]]


def get_profile(conn,scope,project):
    owner_hash,index_signature=_scope(scope)
    row=conn.execute("""SELECT kind,record_id,entry_id,md_path,content_sha256,group_name,title,summary,content,tokens,metadata,updated_at
      FROM everos_vectors WHERE owner_hash=? AND index_signature=? AND project_id=? AND kind='user_profile'
      ORDER BY updated_at DESC,record_id LIMIT 1""",(owner_hash,index_signature,project)).fetchone()
    return _public_row(dict(row)) if row else None