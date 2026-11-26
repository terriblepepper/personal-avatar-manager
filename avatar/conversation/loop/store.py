"""Owner-scoped PostgreSQL checkpoints and append-only UTF-8 fragments."""
import json
from contextlib import contextmanager
from copy import deepcopy
from threading import RLock

from avatar.core.artifact_limits import MAX_ENVELOPE_BYTES, MAX_SOURCE_BYTES


class RunConflict(RuntimeError):
    """This writer, cursor or checkpoint no longer matches the durable run."""


class RunCapacity(ValueError):
    """The draft or account storage limit has been reached."""


_SCOPE_ACCESS = {
    'chat': 'SELECT 1 FROM chat_conversations WHERE id=? AND user_id=?',
    'dialogue': 'SELECT 1 FROM agent_dialogues WHERE id=? AND ? IN (user_id,target_user_id)',
    'protocol': '''SELECT 1 FROM avatar_aip_tasks t JOIN avatar_records a ON a.aic=t.local_aic
                   WHERE t._rowid=?::bigint AND a.user_id=?''',
}
_UNSEEN = object()
BATCH_PREFIX = 'react-batch-'
# Provider fragments, assembled arguments and a repaired base may coexist with visible text and a finished artifact.
OWNER_STORAGE_BYTES = 4 * MAX_ENVELOPE_BYTES + 2 * MAX_SOURCE_BYTES
_CHUNK_OVERHEAD = 256


def draft_byte_limit(name, binary=False):
    return MAX_ENVELOPE_BYTES if not binary and name.startswith(BATCH_PREFIX) else MAX_SOURCE_BYTES


class RunStore:
    def __init__(self, database, owner, scope, scope_id, run_id, writer, validate=None):
        if scope not in _SCOPE_ACCESS or not all(isinstance(s, str) and s for s in (owner, scope_id, run_id, writer)):
            raise ValueError('无效的分身任务标识')
        self.database, self.owner, self.scope, self.scope_id = database, owner, scope, scope_id
        self.run_id, self.writer, self.validate = run_id, writer, validate
        self._key = (owner, scope, scope_id, run_id)
        self._id = self._revision = self._settlement_revision = None
        self._gate = RLock()

    @staticmethod
    def inspect_in(db, owner, scope, scope_id, run_id):
        row = db.execute('SELECT state FROM agent_loop_runs WHERE owner=? AND scope=? AND scope_id=? AND run_id=?',
                         (owner, scope, scope_id, run_id)).fetchone()
        return row[0] if row else None

    @staticmethod
    def clear_in(db, owner, scope, scope_id, run_id):
        db.execute('DELETE FROM agent_loop_runs WHERE owner=? AND scope=? AND scope_id=? AND run_id=?',
                   (owner, scope, scope_id, run_id))

    @staticmethod
    def mark_usage_recorded_for_in(db, owner, scope, scope_id, run_id):
        # A business-side terminal reconciliation may preserve the draft, but must revoke its former writer.
        db.execute("""UPDATE agent_loop_runs SET state=state-'usage', revision=revision+1,
            retired_writers=array_append(retired_writers,writer), writer=gen_random_uuid()::text, updated_at=now()
            WHERE owner=? AND scope=? AND scope_id=? AND run_id=?""", (owner, scope, scope_id, run_id))

    def mark_usage_recorded_in(self, db):
        self.check_in(db)
        row = db.execute("""UPDATE agent_loop_runs SET state=state-'usage', revision=revision+1, updated_at=now()
            WHERE id=? AND state->'usage' IS NOT NULL RETURNING revision""", (self._id,)).fetchone()
        if row:
            # The business transaction owns the commit; accept its old fence if the ledger insert rolls back.
            self._settlement_revision = row[0]

    @staticmethod
    def clear_scope_in(db, owner, scope, scope_id):
        db.execute('DELETE FROM agent_loop_runs WHERE owner=? AND scope=? AND scope_id=?', (owner, scope, scope_id))

    @staticmethod
    def prune_in(db):
        # A paused or running owner keeps its drafts even if the last append was long ago.
        return db.execute("""WITH expired AS (
            SELECT r.id FROM agent_loop_runs r WHERE updated_at<now()-interval '7 days' AND (
                (r.scope='chat' AND EXISTS (SELECT 1 FROM chat_generation_jobs j WHERE j.task_id=r.run_id
                    AND j.state IN ('succeeded','failed','cancelled','indeterminate'))
                 AND NOT EXISTS (SELECT 1 FROM chat_generation_jobs j WHERE j.conversation_id=r.chat_id
                    AND j.state IN ('running','queued'))
                 AND NOT EXISTS (SELECT 1 FROM chat_agent_pauses p WHERE p.conversation_id=r.chat_id))
                OR EXISTS (SELECT 1 FROM agent_dialogues d WHERE d.id=r.dialogue_id AND d.status='ended')
                OR EXISTS (SELECT 1 FROM avatar_aip_tasks t WHERE t._rowid=r.protocol_id AND
                    t.document::jsonb #>> '{status,state}' IN ('failed','canceled','completed')))
            ORDER BY r.updated_at LIMIT 10 FOR UPDATE OF r SKIP LOCKED)
            DELETE FROM agent_loop_runs r USING expired WHERE r.id=expired.id""").rowcount

    def _authorize(self, db):
        if not db.execute(_SCOPE_ACCESS[self.scope], (self.scope_id, self.owner)).fetchone():
            raise RunConflict('任务作用域已失效')
        if self.validate is not None and self.validate(db) is False:
            raise RunConflict('任务执行权限已改变')

    def acquire(self, *, expected=_UNSEEN):
        with self._gate:
            if self._id is not None:
                state = self.load()
                if expected is not _UNSEEN and state != expected:
                    raise RunConflict('原任务执行状态已改变')
                return state
            with self.database.connect(write=True, account_scope=self.owner) as db:
                self._authorize(db)
                if expected is not _UNSEEN and self.inspect_in(db, *self._key) != expected:
                    raise RunConflict('原任务执行状态已改变')
                row = db.execute("""INSERT INTO agent_loop_runs(owner,scope,scope_id,run_id,writer,chat_job)
                    VALUES(?,?,?,?,?,(SELECT task_id FROM chat_generation_jobs WHERE task_id=? AND user_id=? AND conversation_id=?))
                    ON CONFLICT(owner,scope,scope_id,run_id) DO UPDATE SET writer=excluded.writer,
                    revision=agent_loop_runs.revision+1, updated_at=now(),
                    retired_writers=array_append(agent_loop_runs.retired_writers,agent_loop_runs.writer)
                    WHERE excluded.writer<>agent_loop_runs.writer AND NOT (excluded.writer=ANY(agent_loop_runs.retired_writers))
                    RETURNING id,revision,state""",
                                 (*self._key, self.writer, self.run_id if self.scope == 'chat' else None, self.owner, self.scope_id)).fetchone()
            if row is None:
                raise RunConflict('旧执行者不能重新领取任务')
            self._id, self._revision, state = row
            return state

    def check_in(self, db):
        self._authorize(db)
        if not db.execute('SELECT 1 FROM agent_loop_runs WHERE id=? AND writer=? AND revision IN (?,?) FOR UPDATE',
                          (self._id, self.writer, self._revision, self._settlement_revision)).fetchone():
            raise RunConflict('任务已结束或已由其他执行者接管')

    @contextmanager
    def _read(self):
        with self._gate, self.database.connect() as db:
            self.check_in(db)
            yield db

    @contextmanager
    def _write(self):
        with self._gate:
            with self.database.connect(write=True, account_scope=self.owner) as db:
                self.check_in(db)
                yield db
                revision = db.execute('UPDATE agent_loop_runs SET revision=revision+1,updated_at=now() WHERE id=? RETURNING revision',
                                      (self._id,)).fetchone()[0]
            self._revision = revision
            self._settlement_revision = None

    def check(self):
        with self._read():
            pass

    def load(self):
        with self._read() as db:
            return db.execute('SELECT state FROM agent_loop_runs WHERE id=?', (self._id,)).fetchone()[0]

    @staticmethod
    def _json(value):
        if not isinstance(value, dict):
            raise ValueError('任务状态必须是对象')
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)

    def save(self, state):
        with self._write() as db:
            db.execute('UPDATE agent_loop_runs SET state=?::jsonb WHERE id=?', (self._json(state), self._id))

    def migrate(self, state, keep):
        with self._write() as db:
            db.execute('''WITH removed AS (DELETE FROM agent_loop_chunks WHERE run=? AND NOT (draft=ANY(?::text[]))
                    RETURNING octet_length(content)+octet_length(draft)+? AS charge)
                UPDATE agent_loop_runs SET state=?::jsonb,
                    storage_bytes=storage_bytes-(SELECT coalesce(sum(charge),0) FROM removed) WHERE id=?''',
                       (self._id, sorted(keep), _CHUNK_OVERHEAD, self._json(state), self._id))

    def _patch(self, db, patch):
        if patch is not None:
            db.execute('UPDATE agent_loop_runs SET state=state || ?::jsonb WHERE id=?', (self._json(patch), self._id))

    def _expect(self, db, expected):
        if expected is not None:
            state = db.execute('SELECT state FROM agent_loop_runs WHERE id=?', (self._id,)).fetchone()[0]
            if any(state.get(k) != v for k, v in expected.items()):
                raise RunConflict('任务步骤已由其他执行者更新')

    def merge(self, patch, *, expected=None):
        with self._write() as db:
            self._expect(db, expected)
            self._patch(db, patch)

    @staticmethod
    def _draft(name):
        if not isinstance(name, str) or not name or len(name.encode()) > 256:
            raise ValueError('无效的草稿标识')
        return name

    def _last(self, db, name):
        return db.execute('''SELECT start_byte+octet_length(content),is_binary FROM agent_loop_chunks
            WHERE run=? AND draft=? ORDER BY start_byte DESC LIMIT 1''', (self._id, self._draft(name))).fetchone()

    def size(self, draft_id):
        with self._read() as db:
            last = self._last(db, draft_id)
            return last[0] if last else 0

    def _append(self, name, offset, data, binary, patch, expected=None):
        if type(offset) is not int or offset < 0:
            raise RunConflict('无效的草稿字节游标')
        with self._write() as db:
            last = self._last(db, name)
            size = last[0] if last else 0
            self._expect(db, expected)
            if last and (offset < size or binary or last[1]):
                same = last[1] == binary and db.execute('''SELECT 1 FROM agent_loop_chunks
                    WHERE run=? AND draft=? AND start_byte=? AND content=?''', (self._id, name, offset, data)).fetchone()
                if same:
                    return size
                raise RunConflict('已提交的草稿分片不同')
            if offset != size:
                raise RunConflict('草稿字节游标已改变')
            if not data and not binary:
                return size
            if size + len(data) > draft_byte_limit(name, binary):
                raise RunCapacity('工具参数封装过大' if not binary and name.startswith(BATCH_PREFIX) else '单份草稿超过 100 MiB')
            self._write_chunks(db, name, [(offset, data)], binary)
            self._patch(db, patch)
            return size + len(data)

    def _write_chunks(self, db, name, chunks, binary):
        charge = sum(len(data) + len(name.encode()) + _CHUNK_OVERHEAD for _, data in chunks)
        used = db.execute('SELECT COALESCE(sum(storage_bytes),0) FROM agent_loop_runs WHERE owner=?',
                          (self.owner,)).fetchone()[0]
        if used + charge > OWNER_STORAGE_BYTES:
            raise RunCapacity('账户临时草稿存储空间不足')
        db.executemany('INSERT INTO agent_loop_chunks(run,draft,start_byte,content,is_binary) VALUES(?,?,?,?,?::int::boolean)',
                       ((self._id, name, offset, data, int(binary)) for offset, data in chunks))
        db.execute('UPDATE agent_loop_runs SET storage_bytes=storage_bytes+? WHERE id=?', (charge, self._id))

    def archive(self, name, data, *, patch):
        if len(data) > draft_byte_limit(self._draft(name)):
            raise RunCapacity('单份回执超过 100 MiB')
        # The reference and its independently pageable rows commit under the same writer fence.
        chunks = []
        start = 0
        while start < len(data):
            end = min(start + 256 * 1024, len(data))
            while end < len(data) and data[end] & 0xc0 == 0x80:
                end -= 1
            chunks.append((start, data[start:end]))
            start = end
        with self._write() as db:
            last = self._last(db, name)
            if not last and chunks:
                self._write_chunks(db, name, chunks, False)
            elif last and (last[0] != len(data) or last[1]):
                raise RunConflict('已归档回执的内容不一致')
            self._patch(db, patch)

    def append(self, draft_id, offset, text, *, patch=None):
        return self._append(draft_id, offset, text.encode('utf-8'), False, patch)

    def put_blob(self, name, data, *, patch=None, expected=None):
        if not isinstance(data, bytes):
            raise TypeError('工具输出必须是 bytes')
        self._append(name, 0, data, True, patch, expected)

    def read_blob(self, name):
        with self._read() as db:
            row = db.execute('SELECT is_binary,content FROM agent_loop_chunks WHERE run=? AND draft=? AND start_byte=0',
                             (self._id, self._draft(name))).fetchone()
            if not row or not row[0]:
                raise RunConflict('工具输出不存在')
            return row[1]

    def _text(self, draft_id, limit):
        with self._read() as db:
            last = self._last(db, draft_id)
            if not last:
                return ''
            if last[1]:
                raise RunConflict('此草稿是二进制工具输出')
            start = 0 if limit is None else max(0, last[0] - limit)
            rows = db.execute('''SELECT substring(content FROM greatest(1,(?-start_byte+1)::int))
                FROM agent_loop_chunks WHERE run=? AND draft=? AND start_byte>=(
                    SELECT max(start_byte) FROM agent_loop_chunks WHERE run=? AND draft=? AND start_byte<=?)
                ORDER BY start_byte''', (start, self._id, draft_id, self._id, draft_id, start))
            # Every chunk is valid UTF-8; a byte-limited tail can start inside one character.
            return b''.join(row[0] for row in rows).decode('utf-8', errors='ignore' if start else 'strict')

    def read_range(self, name, start, limit):
        if type(start) is not int or type(limit) is not int or start < 0 or not 0 < limit <= draft_byte_limit(self._draft(name)):
            raise ValueError('无效的回执读取范围')
        end = start + limit
        with self._read() as db:
            rows = db.execute('''SELECT substring(content FROM greatest(0,?-start_byte)::int+1
                    FOR (?-greatest(?,start_byte))::int)
                FROM agent_loop_chunks WHERE run=? AND draft=? AND NOT is_binary AND start_byte < ?
                AND start_byte >= (SELECT max(start_byte) FROM agent_loop_chunks
                    WHERE run=? AND draft=? AND start_byte<=?) ORDER BY start_byte''',
                (start,end,start,self._id,self._draft(name),end,self._id,name,start))
            return b''.join(row[0] for row in rows)

    def read(self, draft_id):
        return self._text(draft_id, None)

    def tail(self, draft_id, limit):
        if type(limit) is not int or limit < 0:
            raise ValueError('无效的草稿尾段长度')
        return self._text(draft_id, limit)

    def discard(self, draft_id):
        with self._write() as db:
            db.execute('''WITH removed AS (DELETE FROM agent_loop_chunks WHERE run=? AND draft=?
                    RETURNING octet_length(content)+octet_length(draft)+? AS charge)
                UPDATE agent_loop_runs SET storage_bytes=storage_bytes-(SELECT coalesce(sum(charge),0) FROM removed)
                WHERE id=?''', (self._id, self._draft(draft_id), _CHUNK_OVERHEAD, self._id))

    def clear(self):
        with self._gate, self.database.connect(write=True, account_scope=self.owner) as db:
            self.check_in(db)
            self.clear_in(db, *self._key)


class MemoryRunStore:
    """Run-local storage for entries without a durable checkpoint."""
    def __init__(self):
        self.state = {}
        self.chunks = {}
        self.storage_bytes = 0

    def check(self):
        pass

    def load(self):
        return deepcopy(self.state)

    def save(self, state):
        self.state = deepcopy(state)

    def migrate(self, state, keep):
        for name in self.chunks.keys() - keep:
            self.discard(name)
        self.save(state)

    def merge(self, patch, *, expected=None):
        if expected and any(self.state.get(key) != value for key, value in expected.items()):
            raise RunConflict('任务步骤已由其他执行者更新')
        self.state.update(deepcopy(patch))

    def append(self, name, offset, text, *, patch=None):
        return self._append(name, offset, text.encode('utf-8'), patch)

    def _append(self, name, offset, data, patch):
        current = self.chunks.get(name, {})
        size = sum(map(len, current.values()))
        if offset != size:
            if current.get(offset) == data:
                return size
            raise RunConflict('草稿字节游标已改变')
        if size + len(data) > draft_byte_limit(name):
            raise RunCapacity('单份草稿超过存储上限')
        if data:
            charge = len(data) + len(name.encode()) + _CHUNK_OVERHEAD
            if self.storage_bytes + charge > OWNER_STORAGE_BYTES:
                raise RunCapacity('临时草稿存储空间不足')
            self.chunks.setdefault(name, {})[offset] = data
            self.storage_bytes += charge
        if patch:
            self.merge(patch)
        return size + len(data)

    def archive(self, name, data, *, patch):
        self._append(name, 0, data, None)
        self.merge(patch)

    def read_range(self, name, start, limit):
        return b''.join(self.chunks.get(name, {}).values())[start:start + limit]

    def read(self, name):
        return b''.join(self.chunks.get(name, {}).values()).decode('utf-8')

    def discard(self, name):
        chunks = self.chunks.pop(name, {})
        self.storage_bytes -= sum(len(data) + len(name.encode()) + _CHUNK_OVERHEAD for data in chunks.values())
