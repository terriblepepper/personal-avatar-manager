"""Execution boundaries can revoke work without depending on a transport."""
import asyncio
import json
import re
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar

from fastapi import HTTPException

_validator=ContextVar('execution_validator',default=None)


@contextmanager
def execution_guard(validator):
    token=_validator.set(validator)
    try:yield
    finally:_validator.reset(token)


def check_execution():
    validator=_validator.get()
    if validator:validator()


async def check_execution_async(validate=None):
    guard = _validator.get()
    if guard or validate:
        def check():
            if guard: guard()
            if validate: validate()
        await asyncio.to_thread(check)


async def run_blocking(function, *args, **kwargs):
    """A cancelled caller must outlive its transaction, lease or temporary input."""
    return await finish_on_cancel(asyncio.to_thread(function, *args, **kwargs))


async def finish_on_cancel(coroutine):
    """Drain side effects and cleanup before propagating one or more cancellations."""
    task = asyncio.create_task(coroutine)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A concurrent shutdown and user stop may cancel the same task twice.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


# The same context follows generation work through model, sandbox and RAG boundaries.
current_generation = ContextVar('current_generation', default=None)


def authorization():
    job = current_generation.get()
    return json.loads(job['authorization']) if job else None


def sandbox_checkpoint(raw):
    value = raw if isinstance(raw, dict) else {}
    attempted = value.get('sandbox_attempted') is True
    result = {'v': 1, 'sandbox_attempted': attempted}
    actions = value.get('sandbox_actions')
    if attempted and isinstance(actions, list) and 0 < len(actions) <= 16 and all(
        isinstance(action, str) and re.fullmatch(r'[0-9a-f]{64}', action) for action in actions
    ):
        result['sandbox_actions'] = list(dict.fromkeys(actions))
    return result


class AgentNeedsInput(Exception):
    def __init__(self, message, usage=None, checkpoint=None, *, question=None):
        super().__init__(message)
        self.question = question
        self.usage = usage
        self.checkpoint = sandbox_checkpoint(checkpoint)
        self.generated_files = []

    @classmethod
    def from_context(cls, message, config, usage=None, *, question=None):
        error = cls(message, usage, config.get('_agent_checkpoint'), question=question)
        error.generated_files = config.get('_generated_files', [])
        return error


def recoverable(error):
    return not getattr(error,'recovery_attempted',False) and (getattr(error, 'invalid_tool_call', False) or getattr(error, 'retryable', False))


async def call_with_retry(call, validate=None, *, retry_invalid=True):
    """Only replay a planner or a read. Never wrap a domain action or a sandbox execution."""
    for attempt in range(2):
        await check_execution_async(validate)
        try:
            return await call()
        except HTTPException as error:
            if attempt or not recoverable(error) or (not retry_invalid and getattr(error, 'invalid_tool_call', False)):
                raise
            await asyncio.sleep(0.2)


def _trace_value(value, depth=0):
    if isinstance(value, str):
        if re.search(r'(?:Bearer\s+[^\s]+|github_pat_|sk-[a-z0-9_-]{12,}|-----BEGIN [A-Z ]+KEY-----)', value, re.I):
            return '[敏感内容已隐藏]'
        return value[:500] + ('…' if len(value) > 500 else '')
    if depth >= 5:
        return '…'
    if isinstance(value, Mapping):
        return {str(key): _trace_value(item, depth+1) for key, item in list(value.items())[:10]}
    if isinstance(value, (list, tuple)):
        return [_trace_value(item, depth+1) for item in value[:8]]
    return value


def model_failure(error):
    issue = getattr(error,'tool_issue',None)
    if not issue:
        if getattr(error,'delivery_unknown',False):
            issue = '上游请求已发出，响应未确认，未自动重发'
        elif getattr(error,'request_not_sent',False):
            issue = '连接失败，请求未发出'
        elif getattr(error,'output_limited',False):
            issue = '模型输出达到上限'
        elif getattr(error,'empty_model_output',False):
            issue = '模型未返回可用输出'
        else:
            issue = '模型决策请求失败'
    return {'reason':issue,'tool_count':getattr(error,'tool_count',None),
            'delivery_unknown':bool(getattr(error,'delivery_unknown',False))}


class AgentSteps:
    """Publish bounded workflow state. Callers project permitted fields before tracing."""
    PHASES = {'perceive': '感知', 'reason': '推理', 'act': '行动',
              'observe': '观察', 'check': '完成检查', 'final': '最终输出'}

    def __init__(self, progress):
        self.progress = progress

    def point(self, phase, inputs, output, *, status='completed'):
        if self.progress:
            self.progress({'type': 'tool', 'id': uuid.uuid4().hex, 'group': 'agent_trace',
                           'name': self.PHASES[phase], 'phase': phase, 'status': status,
                           'input': _trace_value(inputs), 'output': _trace_value(output)})

    @contextmanager
    def step(self, phase, inputs):
        output = {}
        if not self.progress:
            yield output
            return
        event = {'type': 'tool', 'id': uuid.uuid4().hex, 'group': 'agent_trace',
                 'name': self.PHASES[phase], 'phase': phase, 'status': 'running', 'input': _trace_value(inputs)}
        self.progress(event)
        status = 'interrupted'
        try:
            yield output
            status = 'error' if output.get('retry_required') else 'completed'
        except Exception as error:
            status = 'error'
            output['error'] = type(error).__name__
            if hasattr(error, 'status_code'):
                output['status_code'] = error.status_code
                if phase=='reason' and 'iteration' in inputs:
                    output.update(model_failure(error))
            raise
        finally:
            self.progress({**event, 'status': status, 'output': _trace_value(output)})
