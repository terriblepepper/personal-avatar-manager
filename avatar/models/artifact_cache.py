"""账户隔离的有界派生缓存；只缓存成功结果，不缓存凭据或失败。"""
import asyncio
from contextlib import AsyncExitStack
from itertools import batched
from weakref import WeakValueDictionary
import hashlib
import json
from avatar.core.db import dump,now
from avatar.core.execution import run_blocking


def fingerprint(value):return hashlib.sha256(dump(value).encode()).hexdigest()


class ArtifactCache:
    def __init__(self,database):self.database=database;self.locks=WeakValueDictionary()

    def get(self,user_id,kind,signature,key):
        with self.database.connect() as db:
            row=db.execute('SELECT value FROM model_artifact_cache WHERE user_id=? AND kind=? AND signature=? AND digest=?',(user_id,kind,signature,fingerprint(key))).fetchone()
        return json.loads(row[0]) if row else None

    def put(self,user_id,kind,signature,key,value,source_hash=''):
        encoded=dump(value).encode()
        if len(encoded)>8*1024*1024:return
        with self.database.connect(write=True, account_scope=user_id) as db:
            if kind=='parse' and not db.execute('SELECT 1 FROM attachments WHERE user_id=? AND sha256=? UNION ALL SELECT 1 FROM chat_files WHERE user_id=? AND sha256=? LIMIT 1',(user_id,source_hash,user_id,source_hash)).fetchone():return
            db.execute('INSERT OR REPLACE INTO model_artifact_cache VALUES(?,?,?,?,?,?,?)',(user_id,kind,signature,fingerprint(key),source_hash,encoded,now()))
            count,size=db.execute('SELECT count(*),coalesce(sum(length(value)),0) FROM model_artifact_cache WHERE user_id=?',(user_id,)).fetchone()
            if count>4096 or size>64*1024*1024:
                for digest,cache_kind,target,length in db.execute('SELECT digest,kind,signature,length(value) FROM model_artifact_cache WHERE user_id=? ORDER BY updated_at',(user_id,)).fetchall():
                    if count<=4096 and size<=64*1024*1024:break
                    db.execute('DELETE FROM model_artifact_cache WHERE user_id=? AND kind=? AND signature=? AND digest=?',(user_id,cache_kind,target,digest));count-=1;size-=length

    def _read_vectors(self, user_id, kind, signature, keys):
        # Existing vector entries store the fingerprint of the already fingerprinted input.
        by_digest = {fingerprint(key): key for key in keys}
        found = {}
        with self.database.connect() as db:
            for group in batched(by_digest, 8):
                with db.raw.execute("""SELECT digest,value FROM model_artifact_cache
                    WHERE user_id=%s AND kind=%s AND signature=%s AND digest=ANY(%s::text[])""",
                    (user_id, kind, signature, list(group))) as rows:
                    for digest, value in rows:
                        vector = json.loads(value)
                        if vector is not None:
                            found[by_digest[digest]] = vector
        return found

    async def vectors(self,user_id,config,inputs,generate,kind='vector',*,record_usage=None):
        # 完整重构使用独立缓存代次；普通增量仍复用同一代次的成功向量。
        target={key:config.get(key) for key in ('provider','api_format','base_url','model','dimensions')}
        if config.get('_cache_epoch'):target['cache_epoch']=config['_cache_epoch']
        signature=fingerprint(target)
        keys=[fingerprint(value) for value in inputs]
        if not keys:return []
        unique_keys=sorted(set(keys))
        locks=[self.locks.setdefault((user_id,signature,key),asyncio.Lock()) for key in unique_keys]
        async with AsyncExitStack() as stack:
            for lock in locks:await stack.enter_async_context(lock)
            if len(unique_keys)==1:
                key=unique_keys[0]
                found={key:await asyncio.to_thread(self.get,user_id,kind,signature,key)}
            else:
                found=await asyncio.to_thread(self._read_vectors,user_id,kind,signature,unique_keys)
            missing={}
            for key,value in zip(keys,inputs,strict=True):
                if found.get(key) is None:missing.setdefault(key,value)
            items=list(missing.items())
            def save_batch(batch,vectors):
                if record_usage is not None:record_usage()
                for (key,_),vector in zip(batch,vectors,strict=True):
                    self.put(user_id,kind,signature,key,vector)
                    found[key]=vector
            for start in range(0,len(items),8):
                batch=items[start:start+8];vectors=await generate([value for _,value in batch])
                # Finish caching every successful paid result before releasing these input locks.
                await run_blocking(save_batch,batch,vectors)
            return [found[key] for key in keys]
