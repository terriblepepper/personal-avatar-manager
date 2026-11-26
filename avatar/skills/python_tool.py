"""Execute a model-authored Python action once inside the authorized sandbox."""
from hashlib import sha256
import json
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from avatar.core.db import now
from avatar.core.execution import AgentNeedsInput, check_execution, current_tool_call, run_blocking, sandbox_checkpoint
from avatar.skills.effects import SandboxEffects, file_versions, https_versions
from avatar.skills.https_inputs import configured_resources
from avatar.skills.sandbox import SandboxExecutionError


class PythonArguments(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    code: str = Field(min_length=1, max_length=24000, description='在隔离环境运行的 Python 源码。产物写入 outputs/。')
    input_file_ids: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=511, description='本轮已授权原件的真实 ID。文件放在 inputs/<ID>/<文件名>。')
    https_inputs: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default_factory=list, max_length=2, description='运维授权的只读 HTTPS 资源名称，文件位于 https/<名称>。沙箱内无网络权限。')


class AtomicPython:
    def __init__(self, sandbox, config, *, owner, conversation, caller=None, parent=None, inputs, progress=None):
        self.sandbox = sandbox
        self.config = config
        self.scope = [owner, caller or owner, conversation, parent]
        self.inputs = inputs
        self.progress = progress
        self.store = config.get('_run_store')
        self.journal = None
        self.local_receipts = []
        self.outputs = config.setdefault('_generated_files', [])
        self.generated = {}

    def arguments_schema(self):
        schema = PythonArguments.model_json_schema()
        try:
            names = sorted(configured_resources()) if self.config.get('_sandbox_https_enabled', True) else []
        except ValueError:
            names = []
        if names:
            schema['properties']['https_inputs']['items']['enum'] = names
        else:
            del schema['properties']['https_inputs']
        return schema

    @property
    def receipts(self):
        return self.journal.receipts if self.journal else self.local_receipts

    def _catalog(self):
        return {**self.inputs(), **self.generated}

    def _verify(self, catalog, sources):
        check_execution()
        if sources['skill'] is not None:
            raise HTTPException(409, '旧技能脚本的运行授权已撤回，不能使用原回执')
        if sources['https'] and not self.config.get('_sandbox_https_enabled', True):
            raise HTTPException(409, '当前会话未授权使用沙箱 HTTPS 资源')
        if file_versions(catalog, sources['files']) != sources['files'] or https_versions(sources['https']) != sources['https']:
            raise HTTPException(409, '沙箱原件或资源授权已改变，未使用结果')

    def validate(self):
        if self.journal:
            self.journal.check_current()
        if self.receipts:
            catalog = self._catalog()
            for receipt in self.receipts:
                self._verify(catalog, receipt['sources'])

    def _adopt(self, files):
        known = {item['id'] for item in self.outputs}
        for item in files:
            fid, name, data = item['id'], item['filename'], item['data']
            self.generated[fid] = {'path': f'inputs/{fid}/{Path(name).name}', 'read': lambda data=data: data}
            if fid not in known:
                self.outputs.append(item)

    async def restore(self):
        if self.store is None or self.journal is not None:
            return
        checkpoint = sandbox_checkpoint(self.config.get('_agent_checkpoint'))
        self.journal = await run_blocking(SandboxEffects, self.store, self.scope, checkpoint)
        if not self.receipts:
            return
        saved = {item['id'] for item in self.config.get('_agent_saved_files', ())}
        persisted = set(self.config.get('_agent_persisted_file_ids', ()))
        def recover():
            self.journal.check_current()
            catalog = self._catalog()
            return self.journal.restore(catalog, lambda sources: self._verify(catalog, sources), saved, persisted)
        records, files = await run_blocking(recover)
        self._adopt(files)
        self.config['_validate_sandbox'] = self.validate
        if any(record['status'] == 'success' for record in records):
            self.config['_sandbox_succeeded'] = True

    async def execute(self, arguments: PythonArguments):
        await self.restore()
        if not arguments.code.strip():
            raise ValueError('Python 代码不能为空')
        ids = sorted(set(arguments.input_file_ids))
        https = arguments.https_inputs
        if https and (not self.config.get('_sandbox_https_enabled', True) or
                      any(name not in configured_resources() for name in https) or len(set(https)) != len(https)):
            raise ValueError('请求的 HTTPS 资源超出本轮授权')
        if len(ids) + len(https) > 511:
            raise ValueError('沙箱输入超过 512 个文件')

        def prepare():
            self.validate()
            catalog = self._catalog()
            if any(fid not in catalog for fid in ids):
                raise ValueError('脚本请求了未授权文件，请使用当前可见的文件 ID')
            sources = {'skill': None, 'skill_version': None, 'files': file_versions(catalog, ids, resources),
                       'https': https_versions(https)}
            if sum(map(len, resources.values())) > 8 * 1024 * 1024:
                raise HTTPException(413, '沙箱代码与原件输入合计超过 8 MiB')
            return sources

        script = '__task__.py'
        resources = {script: arguments.code.encode()}
        command = ['python', arguments.code]
        if https:
            command.append(sorted(https))
        # Keep the same fingerprint as earlier confirmed direct-code actions after migration.
        token = sha256(json.dumps([command, [], ids], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        checkpoint = sandbox_checkpoint(self.config.get('_agent_checkpoint'))
        invocation = None
        if call := current_tool_call.get():
            step, call_id, replay = call
            invocation = f'{step}:{call_id}'
            if replay and self.journal:
                receipt = next((item for item in self.receipts
                                if item['token'] == token and item.get('invocation') == invocation), None)
                if receipt:
                    await run_blocking(self.validate)
                    return self.journal.read_record(receipt)

        prior = {receipt['token'] for receipt in self.receipts} | set(checkpoint.get('sandbox_actions', ()))
        if token in prior:
            raise ValueError('相同 Python 操作已有回执，请复用已有结果或修改参数')
        if len(prior) >= 16:
            raise AgentNeedsInput.from_context('此任务已累计尝试十六次沙箱操作，已保存进度', self.config)
        sources = await run_blocking(prepare)
        if self.journal:
            await run_blocking(self.journal.claim, token, sources, invocation=invocation)
        self.config['_agent_checkpoint'] = {**checkpoint, 'sandbox_attempted': True,
                                            'sandbox_actions': sorted(prior | {token})}

        def authorize():
            if self.journal:
                self.journal.check_current()
            self._verify(self._catalog(), sources)

        activity = {'type': 'tool', 'id': uuid4().hex, 'group': 'sandbox', 'name': 'Python 沙箱'}
        if self.progress:
            self.progress({**activity, 'status': 'running', 'input': {'input_count': len(ids)},
                           'steps': [{'time': now(), 'message': '正在运行 Python 沙箱'}]})
            self.progress({'type': 'status', 'stage': 'skill_execution', 'message': '正在运行 Python 沙箱'})
        try:
            result = await self.sandbox.execute(
                self.scope[0], resources, script, [], https_inputs=https, authorize=authorize,
                workspace=self.config.get('_sandbox_workspace'),
            )
        except (SandboxExecutionError, ValueError) as exc:
            result = {'status': 'error', 'error': str(exc)[:2000]}
        except (RuntimeError, OSError) as exc:
            if self.journal:
                raise AgentNeedsInput.from_context('沙箱操作未返回可确认的结果，已暂停等待核对', self.config) from None
            result = {'status': 'error', 'error': str(exc)[:2000]}
        await run_blocking(authorize)
        files = result.get('files', ()) if result['status'] == 'success' else ()
        size = sum(sum(receipt['sizes']) for receipt in self.receipts) + sum(len(item['data']) for item in files)
        if size > 20 * 1024 * 1024 or (files and not self.scope[3]):
            message = '本轮累计普通生成文件超过 20 MiB' if self.scope[3] else '当前通道不支持保存生成文件'
            result = {'status': 'error', 'error': message}
            files = ()
        files = [{**file, 'id': str(uuid4())} for file in files]
        record = {'name': 'Python 沙箱', 'status': result['status'], 'error': result.get('error', ''),
                  'outputs': [{'id': item['id'], 'filename': item['filename'],
                               'download_url': f"/api/conversations/{self.scope[3]}/files/{item['id']}"} for item in files]}
        for field in ('stdout', 'stderr'):
            value = result.get(field, '')
            record[field] = '\n'.join(map(str, value)) if isinstance(value, list) else value
        if self.journal:
            try:
                await run_blocking(self.journal.confirm, token, record, files, None)
            except ValueError:
                raise AgentNeedsInput.from_context('沙箱回执未保存，已暂停等待核对', self.config) from None
        else:
            self.local_receipts.append({'token': token, 'sources': sources, 'record': record,
                                        'sizes': [len(item['data']) for item in files]})
        self._adopt(files)
        self.config['_validate_sandbox'] = self.validate
        if result['status'] == 'success':
            self.config['_sandbox_succeeded'] = True
        if self.progress:
            self.progress({**activity, 'status': 'completed' if result['status'] == 'success' else 'error',
                           'output': {'status': result['status'], 'file_count': len(files)}})
        return record
