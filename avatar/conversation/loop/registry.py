"""Validate tool calls and produce an observation for every provider call."""
import json
from hashlib import sha256

from fastapi import HTTPException
from pydantic import ValidationError

from avatar.conversation.loop.store import RunCapacity
from avatar.conversation.loop.types import ToolOutcome, ToolSpec
from avatar.core.execution import check_execution_async
from avatar.models.context_budget import excerpt


class ToolRegistry:
    def __init__(self, validate, trace):
        self.validate = validate
        self.trace = trace
        self.tools = {}

    def add(self, schema, execute, parameters=None, *, control=False, checkpoint=False):
        self.tools[schema['name']] = ToolSpec(schema, execute, parameters, control, checkpoint)

    def schemas(self):
        return [tool.schema for tool in self.tools.values()]

    @staticmethod
    def identity(call, args):
        encoded = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return call['name'] + ':' + sha256(encoded.encode()).hexdigest()

    async def dispatch(self, call, seen, step):
        name, args = call['name'], call['arguments']
        await check_execution_async(self.validate)
        if name not in self.tools:
            return ToolOutcome.error('当前会话未授权此工具。请选择已声明的工具。'), None
        if call.get('argument_error'):
            return ToolOutcome.error(call['argument_error']), None
        spec = self.tools[name]
        try:
            parsed = spec.parameters.model_validate(args) if spec.parameters else args
            normalized = parsed.model_dump() if spec.parameters else args
            identity = self.identity(call, normalized)
            if identity in seen:
                previous = seen[identity]
                return ToolOutcome.error(f'相同参数已在第 {previous} 步取得回执。请复用结果或调整参数。'), identity
            with self.trace.step('act', {'tool':name,'step':step}) as output:
                raw = await spec.execute(parsed)
                outcome = raw if isinstance(raw, ToolOutcome) else ToolOutcome(
                    raw, isinstance(raw, dict) and (bool(raw.get('error')) or raw.get('status')=='error'))
                output['retry_required'] = outcome.is_error
        except RunCapacity:
            raise
        except ValidationError as error:
            errors = error.errors(include_input=False, include_url=False)[:4]
            outcome = ToolOutcome.error('参数校验失败：' + '；'.join(
                '.'.join(map(str, item['loc'])) + ' ' + item['type'] for item in errors))
        except HTTPException as error:
            if (error.status_code in (401, 409, 499) or error.status_code>=500 or
                    getattr(error, 'delivery_unknown', False)):
                raise
            await check_execution_async(self.validate)
            outcome = ToolOutcome.error(excerpt(str(error.detail), 2400))
        except ValueError as error:
            outcome = ToolOutcome.error(excerpt(str(error), 2400))
        else:
            await check_execution_async(self.validate)
            return outcome, identity
        return outcome, None
