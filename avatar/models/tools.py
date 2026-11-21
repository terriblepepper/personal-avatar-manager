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
    count = None
    issue = '工具调用结构无效'
    try:
        if fmt == 'openai':
            message = (data.get('choices') or [{}])[0].get('message') or {}
            raw = message.get('tool_calls')
            if raw is None:
                raw = []
            if not isinstance(raw,list):
                raise ValueError()
            calls = [c['function'] for c in raw]
        elif fmt == 'anthropic':
            calls = [{'name':c.get('name'),'arguments':c.get('input')} for c in data.get('content',[]) if c.get('type')=='tool_use']
        else:
            parts = ((data.get('candidates') or [{}])[0].get('content') or {}).get('parts',[])
            calls = [{'name':p['functionCall'].get('name'),'arguments':p['functionCall'].get('args',{})}
                     for p in parts if 'functionCall' in p and not p.get('thought')]
        count = len(calls)
        if not count and not required:
            return []
        if count != 1:
            issue = '未提交所需工具调用' if not count else '单步返回多个工具调用'
            raise ValueError()
        name = calls[0].get('name')
        if name not in [tool['name'] for tool in tools]:
            issue = '选择了本步未提供的工具'
            raise ValueError()
        args = calls[0].get('arguments', {})
        if isinstance(args,str):
            issue = '工具参数不是有效 JSON'
            args = json.loads(args)
        if not isinstance(args,dict):
            issue = '工具参数必须是对象'
            raise ValueError()
        return [{'name':name,'arguments':args}]
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        error = HTTPException(502,'模型未返回有效的工具调用：'+issue+'。本次未执行工具。')
        error.invalid_tool_call = True
        error.tool_issue = issue
        error.tool_count = count
        raise error from None
