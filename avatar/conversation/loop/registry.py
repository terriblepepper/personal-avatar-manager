"""Validate tool calls and produce an observation for every provider call."""
import json
from hashlib import sha256
from typing import Annotated

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from avatar.conversation.loop.store import RunCapacity
from avatar.conversation.loop.types import Disclosure, ToolOutcome, ToolSpec
from avatar.core.execution import check_execution_async
from avatar.models.context_budget import excerpt
from avatar.models.tools import compact_schema


class EnableTools(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    names: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        min_length=1, max_length=8, description='要从扩展目录加载的工具名称。')


class ToolRegistry:
    def __init__(self, validate, trace):
        self.validate = validate
        self.trace = trace
        self.tools = {}

    def add(self, schema, execute, parameters=None, *, control=False, checkpoint=False,
            disclosure: Disclosure = 'core', summary='', when=None):
        schema = {**schema, 'parameters': compact_schema(schema['parameters'])}
        self.tools[schema['name']] = ToolSpec(schema, execute, parameters, control, checkpoint,
                                             disclosure, summary, when)

    def available(self, name: str) -> bool:
        tool = self.tools.get(name)
        return bool(tool and (tool.disclosure != 'state' or tool.when and tool.when()))

    def sync_state(self, unlocked: list[str]) -> list[str]:
        unlocked[:] = [name for name in unlocked if self.available(name)]
        for name, tool in self.tools.items():
            if tool.disclosure == 'state' and name not in unlocked and self.available(name):
                unlocked.append(name)
        return unlocked

    def active_names(self, unlocked: list[str]) -> list[str]:
        core = [name for name, tool in self.tools.items() if tool.disclosure == 'core']
        return list(dict.fromkeys([*core, *(name for name in unlocked if self.available(name))]))

    def active_schemas(self, unlocked: list[str]) -> list[dict]:
        return [self.tools[name].schema for name in self.active_names(unlocked)]

    def activatable(self, name: str) -> bool:
        tool = self.tools.get(name)
        return bool(tool and tool.disclosure == 'deferred')

    def catalog_prompt(self, active: set[str]) -> str:
        entries = [f'{name}({tool.summary})' for name, tool in self.tools.items()
                   if name not in active and self.activatable(name)]
        return '\n当前已声明的工具直接调用。仅在缺少所需能力时，用 enable_tools 加载目录中的工具：' + '；'.join(entries) + '。' if entries else ''

    def register_enable_tools(self, unlocked, *, replay=False):
        if not replay and not self.catalog_prompt(set(self.active_names(unlocked))):
            return

        async def enable(args):
            active = set(self.active_names(unlocked))
            receipt = {'activated': [], 'already_active': [], 'unavailable': []}
            for name in dict.fromkeys(args.names):
                if name in active:
                    receipt['already_active'].append(name)
                elif self.activatable(name):
                    receipt['activated'].append(name)
                else:
                    receipt['unavailable'].append(name)
            invalid = not (receipt['activated'] or receipt['already_active'])
            if invalid:
                receipt['error'] = '所请求的工具当前不可用。请选择扩展目录中的工具。'
            return ToolOutcome(receipt, is_error=invalid, activated=tuple(receipt['activated']))

        self.add({'name':'enable_tools', 'description':'加载目录中所需工具的完整定义，下一轮可用。',
                      'parameters':EnableTools.model_json_schema()}, enable, EnableTools)

    @staticmethod
    def identity(call, args):
        name = call['name']
        if name == 'enable_tools':
            names = args.get('names')
            if isinstance(names, list) and all(isinstance(item, str) for item in names):
                args = {**args, 'names': sorted(set(names))}
        encoded = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        if name=='read_session_file' and args.get('query') is not None:
            name = 'session_directory'
        return name + ':' + sha256(encoded.encode()).hexdigest()

    async def dispatch(self, call, seen, step, *, active=None):
        name, args = call['name'], call['arguments']
        await check_execution_async(self.validate)
        if not self.available(name):
            return ToolOutcome.error('当前会话未授权此工具。请选择已声明的工具。'), None
        if active is not None and name not in active:
            if not self.activatable(name):
                return ToolOutcome.error('此工具未在当前步骤声明。请选择已声明的工具。'), None
            return ToolOutcome.error(f'已加载 {name} 的完整参数定义，请在下一步重新调用。', activated=(name,)), None
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
