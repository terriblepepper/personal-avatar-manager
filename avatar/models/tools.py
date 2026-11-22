"""模型原生工具调用协议适配；只解析调用计划，不在网络适配层执行工具。"""
import json
import re
from fastapi import HTTPException


# Large HTML occupies one string. Bound the surrounding JSON structure before building Python containers.
_JSON_TOKEN = r'"(?:[^"\\]++|\\.)*+"|[]["{},:]'
_STRUCTURE = (re.compile(_JSON_TOKEN,re.DOTALL),re.compile(_JSON_TOKEN.encode(),re.DOTALL))


def load_model_json(value):
    if len(value) > 2*1024*1024:
        scanner = _STRUCTURE[0] if isinstance(value,str) else _STRUCTURE[1]
        depth = 0
        for count, token in enumerate(scanner.finditer(value),1):
            first = value[token.start():token.start()+1]
            if first in ('[','{',b'[',b'{'):
                depth += 1
            elif first in (']','}',b']',b'}'):
                depth -= 1
            elif first in ('"',b'"') and token.end()-token.start()==1:
                raise ValueError('Unterminated model JSON string')
            if count > 65536 or depth > 128:
                raise ValueError('Model JSON structure too large')
    return json.loads(value)


def configure_tools(fmt, body, tools, *, required=True, batch=False):
    """同一份函数定义分别转换为 OpenAI、Anthropic、Gemini 的工具声明。"""
    if fmt == 'openai':
        body.update(tools=[{'type': 'function', 'function': tool} for tool in tools],
                    tool_choice='required' if required else 'auto', parallel_tool_calls=batch)
    elif fmt == 'anthropic':
        body.update(tools=[{'name': t['name'], 'description': t['description'], 'input_schema': t['parameters']} for t in tools],
                    tool_choice={'type': 'any' if required else 'auto', 'disable_parallel_tool_use': not batch})
    else:
        body.update(tools=[{'functionDeclarations': tools}],
                    toolConfig={'functionCallingConfig': {'mode': 'ANY' if required else 'AUTO'}})


def raw_tool_calls(fmt, data):
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
    return calls


def parse_tools(fmt, data, tools, *, required=True, batch=False):
    """调用名/参数严格校验；必需工具缺失时报错，可选工具允许无调用，不从文字猜测计划。"""
    count = None
    issue = '工具调用结构无效'
    try:
        calls = raw_tool_calls(fmt, data)
        count = len(calls)
        if not count and not required:
            return []
        if not 1 <= count <= (8 if batch else 1):
            issue = '未提交所需工具调用' if not count else '单批超过八项工具调用' if batch else '此决策只接受一个工具调用'
            raise ValueError()
        allowed = {tool['name'] for tool in tools}
        parsed = []
        for call in calls:
            name = call.get('name')
            if not isinstance(name,str) or name not in allowed:
                issue = '选择了本步未提供的工具'
                raise ValueError()
            args = call.get('arguments', {})
            if isinstance(args,str):
                issue = '工具参数不是有效 JSON'
                args = load_model_json(args)
            if not isinstance(args,dict):
                issue = '工具参数必须是对象'
                raise ValueError()
            parsed.append({'name':name,'arguments':args})
        return parsed
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise invalid_tool_call(issue,count) from None


def invalid_tool_call(issue,count):
    error = HTTPException(502,'模型未返回有效的工具调用：'+issue+'。本次未执行工具。')
    error.invalid_tool_call = True
    error.tool_issue = issue
    error.tool_count = count
    return error
