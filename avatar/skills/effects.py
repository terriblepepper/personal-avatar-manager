"""Private sandbox intents and confirmed outputs, bound to one authorized run."""
import hashlib
import json
import re
from copy import deepcopy

from fastapi import HTTPException

from avatar.core.execution import AgentNeedsInput, check_execution
from avatar.skills.https_inputs import configured_resources
from avatar.skills.policy import require_skills_enabled


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def file_versions(inputs, ids, resources=None):
    versions = {}
    size = 0
    for identifier in ids:
        item = inputs.get(identifier)
        if not item:
            raise HTTPException(409, '沙箱原件已删除或不再授权，未使用结果')
        data = item['read']()
        size += len(data)
        if size > 8 * 1024 * 1024:
            raise HTTPException(413, '沙箱原件输入超过 8 MiB')
        versions[identifier] = _digest(data)
        if resources is not None:
            resources[item['path']] = data
    return versions


def https_versions(names):
    allowed = configured_resources() if names else {}
    if any(name not in allowed for name in names):
        raise HTTPException(409, '沙箱 HTTPS 资源授权已改变')
    return {name: _digest(allowed[name].encode()) for name in names}


class SandboxEffects:
    def __init__(self, store, scope, checkpoint):
        self.store = store
        self.scope = scope
        self.state = store.load().get('sandbox')
        if self.state and self.state.get('scope') != scope:
            raise HTTPException(409, '沙箱任务的账户、会话或发言人已变化')
        for effect in [*self.receipts, ((self.state or {}).get('in_flight') or {})]:
            if effect.get('sources', {}).get('skill') or effect.get('record', {}).get('skill_id'):
                require_skills_enabled()
        if self.state and self.state.get('in_flight'):
            raise AgentNeedsInput('上次沙箱操作结果未确认，已暂停等待核对', checkpoint=checkpoint)
        if self.state is None and checkpoint['sandbox_attempted']:
            raise AgentNeedsInput('此前沙箱操作缺少可核验的回执，请结束任务后重新提交', checkpoint=checkpoint)

    @property
    def receipts(self):
        return (self.state or {}).get('receipts', [])

    def claim(self, token, sources, *, invocation=None):
        check_execution()
        effect = {'token': token, 'sources': sources}
        if invocation is not None:
            effect['invocation'] = invocation
        pending = {**(self.state or {}), 'scope': self.scope, 'receipts': self.receipts, 'in_flight': effect}
        self.store.merge({'sandbox': pending}, expected={'sandbox': self.state})
        self.state = pending

    def check_current(self):
        if self.store.load().get('sandbox') != self.state:
            raise HTTPException(409, '沙箱操作已失效，未执行或使用结果')

    def confirm(self, token, record, files, usage):
        pending = self.state['in_flight']
        if pending['token'] != token:
            raise RuntimeError('沙箱操作与待确认记录不一致')
        stored = deepcopy(record)
        # PostgreSQL jsonb cannot represent NUL or lone surrogates from script output.
        escaped = [key for key,value in stored.items() if isinstance(value,str) and re.search(r'[\x00\ud800-\udfff]',value)]
        for key in escaped:
            stored[key] = json.dumps(stored[key], ensure_ascii=True)
        receipt = {**pending, 'record': stored, 'sizes': [len(item['data']) for item in files]}
        if escaped:
            receipt['escaped'] = escaped
        if usage:
            receipt['usage'] = {k:v for k,v in usage.items() if isinstance(v,int)}
        confirmed = {**self.state, 'in_flight': None, 'receipts': [*self.receipts, receipt]}
        check_execution()
        if files:
            self.store.put_blob('sb-'+token[:32], b''.join(item['data'] for item in files),
                                patch={'sandbox': confirmed}, expected={'sandbox': self.state})
        else:
            self.store.merge({'sandbox': confirmed}, expected={'sandbox': self.state})
        self.state = confirmed

    @staticmethod
    def read_record(receipt):
        record = deepcopy(receipt['record'])
        for key in receipt.get('escaped', ()):
            record[key] = json.loads(record[key])
        return record

    def restore(self, inputs, authorize, saved, persisted):
        results, outputs = [], []
        for receipt in self.receipts:
            authorize(receipt['sources'])
            record = self.read_record(receipt)
            files = record.get('outputs', [])
            sizes = receipt['sizes']
            if len(sizes) != len(files):
                raise RuntimeError('沙箱文件回执损坏')
            ids = {item['id'] for item in files}
            if ids & (persisted - saved):
                raise HTTPException(409, '已保存的沙箱文件被移除或过期，未恢复原件')
            if ids - saved:
                data = self.store.read_blob('sb-'+receipt['token'][:32])
                if sum(sizes) != len(data):
                    raise RuntimeError('沙箱文件回执与原件大小不符')
                offset = 0
                for item, size in zip(files, sizes):
                    identifier = item['id']
                    if identifier not in saved:
                        body = data[offset:offset+size]
                        outputs.append({'id':identifier, 'filename':item['filename'], 'data':body})
                        inputs[identifier] = {'path':f"inputs/{identifier}/{item['filename']}", 'read':lambda data=body:data}
                    offset += size
            results.append(record)
        return results, outputs
