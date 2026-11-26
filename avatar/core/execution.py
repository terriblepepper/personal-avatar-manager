"""Execution boundaries can revoke work without depending on a transport."""
from avatar.core.text_budget import AgentText, fits_text_budget, budget_error
import asyncio
import json
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from typing import Annotated

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

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
current_tool_call: ContextVar[tuple[int, str, bool] | None] = ContextVar('current_tool_call', default=None)


def authorization():
    job = current_generation.get()
    return json.loads(job['authorization']) if job else None


def sandbox_checkpoint(raw):
    value = raw if isinstance(raw, dict) else {}
    attempted = value.get('sandbox_attempted') is True
    result = {'v': 1, 'sandbox_attempted': attempted}
    actions = value.get('sandbox_actions')
    if attempted and isinstance(actions, list) and actions and all(
        isinstance(action, str) and re.fullmatch(r'[0-9a-f]{64}', action) for action in actions
    ):
        result['sandbox_actions'] = list(dict.fromkeys(actions))
    return result


class AskUser(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, strict=True)
    question: AgentText = Field(min_length=1)
    options: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def check_budget(self):
        if not fits_text_budget(self.question, *self.options):
            raise ValueError(budget_error())
        return self

    def pending(self):
        return {'id': uuid.uuid4().hex, **self.model_dump(exclude_defaults=True)}


class AgentNeedsInput(Exception):
    def __init__(self, message, usage=None, checkpoint=None, *, user_question=None):
        super().__init__(message)
        self.user_question = user_question
        self.question = user_question['question'] if user_question else None
        self.usage = usage
        self.checkpoint = sandbox_checkpoint(checkpoint)
        self.generated_files = []

    @classmethod
    def from_context(cls, message, config, usage=None, *, user_question=None):
        error = cls(message, usage, config.get('_agent_checkpoint'), user_question=user_question)
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


def model_failure(error):
    issue = getattr(error,'tool_issue',None)
    if not issue:
        if getattr(error,'delivery_unknown',False):
            issue = ('模型断流，5 次自动重试已用尽' if getattr(error,'stream_exhausted',False) else
                     getattr(error,'timeout_reason',None) or '上游请求已发出，响应未确认，未自动重发')
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
