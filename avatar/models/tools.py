"""模型原生工具调用协议适配；只解析调用计划，不在网络适配层执行工具。"""
import json
from fastapi import HTTPException


def configure_tools(fmt, body, tools, *, required=True):
    """同一份函数定义分别转换为 OpenAI、Anthropic、Gemini 的工具声明。"""
    if fmt == 'openai':
        body.update(tools=[{'type': 'function', 'function': tool} for tool in tools],
                    tool_choice='required' if required else 'auto', parallel_tool_calls=False)
    elif fmt == 'anthropic':
        body.update(tools=[{'name': t['name'], 'description': t['description'], 'input_schema': t['parameters']} for t in tools],
                    tool_choice={'type': 'any' if required else 'auto', 'disable_parallel_tool_use': True})
    else:
        body.update(tools=[{'functionDeclarations': tools}],
                    toolConfig={'functionCallingConfig': {'mode': 'ANY' if required else 'AUTO'}})


def parse_tools(fmt, data, tools, *, required=True):
    """调用名/参数严格校验；必需工具缺失时报错，可选工具允许无调用，不从文字猜测计划。"""
    if fmt == 'openai':
        calls=[c.get('function', {}) for c in ((data.get('choices') or [{}])[0].get('message') or {}).get('tool_calls', [])]
    elif fmt == 'anthropic':
        calls=[{'name': c.get('name'), 'arguments': c.get('input')} for c in data.get('content', []) if c.get('type') == 'tool_use']
    else:
        parts=((data.get('candidates') or [{}])[0].get('content') or {}).get('parts', [])
        calls=[{'name': c['functionCall'].get('name'), 'arguments': c['functionCall'].get('args', {})} for c in parts if 'functionCall' in c and not c.get('thought')]
    try:
        if not calls and not required:return []
        if len(calls) != 1 or calls[0].get('name') not in {t['name'] for t in tools}:
            raise ValueError()
        args=calls[0].get('arguments', {})
        if isinstance(args, str):
            args=json.loads(args)
        if not isinstance(args, dict):
            raise ValueError()
        return [{'name': calls[0]['name'], 'arguments': args}]
    except (ValueError, TypeError):
        error=HTTPException(502, '模型未返回有效的工具调用；请确认所选模型及 API 支持工具调用。无效调用未执行，该次模型调用可能已消耗额度')
        error.invalid_tool_call=True
        raise error from None
