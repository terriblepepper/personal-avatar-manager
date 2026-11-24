"""Owner-scoped PostgreSQL vector storage and hybrid document retrieval."""
from collections import Counter, OrderedDict, defaultdict
from contextlib import nullcontext
import hashlib
import heapq
import json
from threading import Lock
from time import monotonic
from weakref import WeakValueDictionary
from sys import getsizeof
from itertools import chain, groupby
from math import log

import numpy as np
from psycopg.types.json import Jsonb
from avatar.rag.stores.tokenization import tokens


def install_vector_schema(connection):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS rag_vector_records(
            user_id text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            collection text NOT NULL,
            record_id text NOT NULL,
            document text NOT NULL,
            metadata jsonb NOT NULL,
            embedding real[] NOT NULL CHECK (
                array_ndims(embedding)=1 AND cardinality(embedding)>0
                AND array_position(embedding,NULL) IS NULL
                AND NOT embedding && ARRAY['NaN','Infinity','-Infinity']::real[]
            ),
            PRIMARY KEY(user_id,collection,record_id)
        )
    """)

def _cache_bytes(value):
    """Conservative size for cached tuples, JSON, BM25 state and owning NumPy arrays."""
    size = getsizeof(value)
    if isinstance(value, dict):
        size += sum(_cache_bytes(k) + _cache_bytes(v) for k, v in value.items())
    elif isinstance(value, (tuple, list)):
        size += sum(_cache_bytes(v) for v in value)
    return size


class _SearchCorpus:
    """Inverted postings and dense block positions for one previously authorized snapshot."""

    def __init__(self, rows, versions):
        self.rows = rows
        stamps = {key: (vector, lexical) for key, vector, lexical in versions}
        self.versions = {row['id']: (i, *stamps[row['id']]) for i, row in enumerate(rows)}
        postings = defaultdict(list)
        lengths = []
        for index, row in enumerate(rows):
            terms = row.pop('tokens')
            words = (terms.split() if terms is not None else
                     tokens(row['metadata']['filename'] + ' ' + row['metadata']['section'] + ' ' + row['text']))
            words = words or ['_']
            lengths.append(len(words))
            for word, count in Counter(words).items():
                postings[word].append((index, count))
        self.lengths = np.asarray(lengths, dtype=np.int64)
        self.postings = {word: np.asarray(entries, dtype=np.int32) for word, entries in postings.items()}

    def select(self, versions):
        indices = []
        for key, vector, lexical in versions:
            entry = self.versions.get(key)
            if entry is None or entry[1] != vector or entry[2] != lexical:
                return None
            indices.append(entry[0])
        return sorted(indices)

    def lexical_ranks(self, indices, queries):
        # BM25Plus with delta=0. Document frequencies and average length belong to this exact scope.
        count = len(indices)
        lengths = self.lengths[indices]
        normalizer = 1.5 * (0.25 + 0.75 * lengths / (int(lengths.sum()) / count))
        positions = None
        if count != len(self.rows):
            positions = np.full(len(self.rows), -1, dtype=np.intp)
            positions[indices] = np.arange(count)
        for query in queries:
            scores = np.zeros(count)
            for word in tokens(query):
                posting = self.postings.get(word)
                if posting is None:
                    continue
                docs, freq = posting.T
                if positions is not None:
                    docs = positions[docs]
                    visible = docs >= 0
                    docs, freq = docs[visible], freq[visible]
                    if not len(docs):
                        continue
                scores[docs] += log((count + 1) / len(docs)) * (freq * 2.5 / (normalizer[docs] + freq))
            ranked = np.argsort(-scores, kind='stable')[:36]
            yield [self.rows[indices[i]]['id'] for i in ranked if scores[i] > 0]


class PostgresFiles:
    def __init__(self, database):
        self.database = database
        self._cache = OrderedDict()
        self._bytes = 0
        self._peak_bytes = 0
        self._corpus_lookups = 0
        self._corpus_hits = 0
        self._lock = Lock()
        self._loads = WeakValueDictionary()

    def close(self):
        with self._lock:
            self._cache.clear()
            self._bytes = 0

    def cache_stats(self):
        with self._lock:
            return {'bytes': self._bytes, 'peak_bytes': self._peak_bytes,
                    'corpus_lookups': self._corpus_lookups, 'corpus_hits': self._corpus_hits}

    def _lookup(self, key):
        with self._lock:
            entry = self._cache.get(key)
            fresh = entry is not None and monotonic() - entry[0] < 300
            if key[0] == 'corpus' and len(key) == 4:
                self._corpus_lookups += 1
                self._corpus_hits += int(fresh)
            if fresh:
                self._cache.move_to_end(key)
                return entry[1]
        return None

    def _load_lock(self, key):
        # Concurrent cold requests share the build; idle scopes retain no locks.
        with self._lock:
            lock = self._loads.get(key)
            if lock is None:
                lock = self._loads[key] = Lock()
            return lock

    def _remember(self, key, data, size):
        budget = 96 * 1024**2
        size += _cache_bytes(key) + 256
        if size > budget:
            return
        with self._lock:
            previous = self._cache.pop(key, None)
            if previous is not None:
                self._bytes -= previous[2]
            while self._bytes + size > budget:
                _, (_, _, removed) = self._cache.popitem(last=False)
                self._bytes -= removed
            self._cache[key] = (monotonic(), data, size)
            self._bytes += size
            self._peak_bytes = max(self._peak_bytes, self._bytes)

    @staticmethod
    def pack(source,file_id,filename,rows,vectors,coverage):
        identifiers=[];metadata=[]
        for ordinal,(row,vector) in enumerate(zip(rows,vectors,strict=True)):
            meta={'source':source,'file_id':file_id,'filename':filename,'ordinal':ordinal,
                  'page':row.get('page') or 0,'section':row.get('section',''),'parent':row.get('parent',str(ordinal)),
                  'context':row.get('context',row['text']),'parent_text':row.get('parent_text',row['text']),
                  'modality':row['modality'],'coverage':coverage}
            identifier=hashlib.sha256(json.dumps([row['text'],meta,vector],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            identifiers.append(identifier);metadata.append({**meta,'record_id':identifier})
        return identifiers,metadata,[row['text'] for row in rows]

    def write(self, user_id, name, records, vectors):
        ids, metadata, documents = records
        if not ids:
            return
        dimension = len(vectors[0])
        if not dimension or any(len(vector) != dimension for vector in vectors):
            raise ValueError('Inconsistent embedding dimension')
        with self.database.connect(write=True) as db:
            previous = db.execute("""SELECT cardinality(embedding) FROM rag_vector_records
                WHERE user_id=? AND collection=? LIMIT 1""", (user_id, name)).fetchone()
            if previous and previous[0] != dimension:
                raise ValueError('Embedding dimension differs from existing collection')
            with db.raw.cursor() as cursor:
                cursor.executemany("""INSERT INTO rag_vector_records
                    (user_id,collection,record_id,document,metadata,embedding)
                    VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    ((user_id, name, key, text, Jsonb(meta), [float(value) for value in vector])
                     for key, meta, text, vector in zip(ids, metadata, documents, vectors, strict=True)))

    def delete_in(self, db, user_id, name, ids):
        if ids:
            db.execute("""DELETE FROM rag_vector_records WHERE user_id=? AND collection=?
                AND record_id=ANY(?::text[])""", (user_id, name, ids))

    def records(self, user_id, name, ids, connection=None):
        if not ids:
            return []
        with (nullcontext(connection) if connection is not None else self.database.connect()) as db:
            rows = db.execute("""SELECT v.record_id,v.document,v.metadata,l.tokens,v.xmin::text FROM rag_vector_records v
                LEFT JOIN rag_lexical_records l USING (user_id,collection,record_id)
                WHERE v.user_id=? AND v.collection=? AND v.record_id=ANY(?::text[]) ORDER BY v.record_id""",
                (user_id, name, list(ids)))
            return [dict(id=key, text=text, metadata=meta, tokens=terms, version=version)
                    for key, text, meta, terms, version in rows]

    def contains(self, user_id, name, ids, connection=None):
        if not name or not ids:
            return False
        with (nullcontext(connection) if connection is not None else self.database.connect()) as db:
            found = db.execute("""SELECT count(*) FROM rag_vector_records WHERE user_id=?
                AND collection=? AND record_id=ANY(?::text[])""", (user_id, name, list(ids))).fetchone()[0]
        return found == len(set(ids))

    @staticmethod
    def _read_dense_block(db, user_id, name, ids):
        # Each non-null one-dimensional REAL[] has a 20-byte wire header, then (length, value) pairs.
        cell = np.dtype([('length', '>i4'), ('value', '>f4')])
        with db.raw.cursor(binary=True) as cursor:
            cursor.execute("""SELECT record_id,array_send(embedding) FROM rag_vector_records
                WHERE user_id=%s AND collection=%s AND record_id=ANY(%s::text[])""", (user_id, name, ids))
            batch = dict(cursor.fetchall())
        matrix = np.array([np.frombuffer(batch[key], dtype=cell, offset=20)['value']
                           for key in ids], dtype=np.float32)
        return ids, matrix, np.sqrt(np.einsum('ij,ij->i', matrix, matrix, dtype=np.float64))

    def _dense_block(self, db, user_id, name, rows, indices):
        key = (user_id, name, tuple((row['id'], row['version']) for row in rows))
        block = self._lookup(key)
        if len(indices) < len(rows):
            if block is not None:
                ids, matrix, norms = block
                return [ids[i] for i in indices], matrix[indices], norms[indices]
            # Never load a row outside the active snapshot's allowlist and metadata filter.
            rows = [rows[i] for i in indices]
            key = (user_id, name, tuple((row['id'], row['version']) for row in rows))
            block = self._lookup(key)
        if block is None:
            with self._load_lock(key):
                block = self._lookup(key)
                if block is None:
                    block = self._read_dense_block(db, user_id, name, [row['id'] for row in rows])
                    self._remember(key, block, _cache_bytes(block))
        return block

    def _dense_ranks(self, db, user_id, name, corpus, indices, vectors):
        if not vectors:
            return []
        queries = np.asarray(vectors, dtype=np.float64)
        ranks = [[] for _ in vectors]
        for block_number, selected in groupby(indices, key=lambda i: i // 256):
            start = block_number * 256
            local = [i - start for i in selected]
            ids, matrix, norms = self._dense_block(db, user_id, name, corpus.rows[start:start + 256], local)
            dots = np.einsum('ij,kj->ki', matrix, queries, dtype=np.float64)
            scores = np.divide(dots, norms, out=np.zeros_like(dots), where=norms!=0)
            for index, values in enumerate(scores):
                ranks[index] = heapq.nsmallest(36, chain(ranks[index], zip(-values, ids)))
        return [[key for _, key in ranking] for ranking in ranks]

    def _corpus(self, db, user_id, name, identifiers):
        with db.raw.cursor() as cursor:
            cursor.execute("""SELECT v.record_id, v.xmin::text, l.xmin::text FROM rag_vector_records v
                LEFT JOIN rag_lexical_records l USING (user_id,collection,record_id)
                WHERE v.user_id=%s AND v.collection=%s AND v.record_id=ANY(%b::text[]) ORDER BY v.record_id""",
                (user_id, name, list(identifiers)))
            versions = cursor.fetchall()
        if not versions:
            return None, []
        # The latest superset handles narrowed permissions; exact scopes also survive alternating conversations.
        recent = ('recent-corpus', user_id, name)
        latest = self._lookup(recent)
        key = ('corpus', user_id, name, hash(tuple(versions)))
        candidates = (latest, key) if latest is not None and latest != key else (key,)
        for candidate in candidates:
            corpus = self._lookup(candidate)
            if corpus is not None:
                indices = corpus.select(versions)
                if indices is not None:
                    if candidate != latest:
                        self._remember(recent, candidate, _cache_bytes(candidate))
                    return corpus, indices
        with self._load_lock(key):
            current = self._lookup(key)
            if current is not None:
                indices = current.select(versions)
                if indices is not None:
                    return current, indices
            rows = self.records(user_id, name, [row[0] for row in versions], db)
            corpus = _SearchCorpus(rows, versions)
            self._remember(key, corpus, _cache_bytes(vars(corpus)))
            self._remember(recent, key, _cache_bytes(key))
            return corpus, list(range(len(rows)))

    def search(self, user_id, name, identifiers, queries, vectors, plan, limit=12):
        with self.database.connect() as db:
            db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            corpus, indices = self._corpus(db, user_id, name, identifiers)
            if not indices:
                return []
            if plan.model_dump(exclude={'query', 'alternatives'}, exclude_defaults=True):
                indices = [i for i in indices if plan.matches(corpus.rows[i]['metadata'])]
                if not indices:
                    return []
            ranks = self._dense_ranks(db, user_id, name, corpus, indices, vectors)
        ranks.extend(corpus.lexical_ranks(indices, queries))
        allowed = {corpus.rows[i]['id']: corpus.rows[i] for i in indices}
        parent_sizes = Counter((row['metadata']['file_id'], row['metadata']['parent']) for row in allowed.values())
        scores = defaultdict(float)
        for rank in ranks:
            for position,identifier in enumerate(rank,1):scores[identifier]+=1/(60+position)
        parent_hits = Counter((allowed[i]['metadata']['file_id'], allowed[i]['metadata']['parent']) for i in scores)
        chosen=[];parents=set();seen=set()
        for identifier in sorted(scores,key=scores.get,reverse=True):
            row=allowed[identifier];metadata=row['metadata'];parent=(metadata['file_id'],metadata['parent'])
            if parent in parents:continue
            parents.add(parent)
            # 同一父块多数子块命中时提升为父块，否则扩展到相邻子块上下文。
            text=metadata['parent_text'] if parent_sizes[parent]>1 and parent_hits[parent]/parent_sizes[parent]>=.6 else metadata['context']
            identity=(metadata['file_id'],text)
            if identity in seen:continue
            seen.add(identity);chosen.append({'text':text,'metadata':{**metadata,'chunk':metadata['ordinal'],'score':scores[identifier],'matched_text':row['text'],'retrieval':'postgres_bm25_rrf'}})
            if len(chosen)>=limit:break
        return chosen
