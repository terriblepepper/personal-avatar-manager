"""模型原生工具调用协议适配；只解析调用计划，不在网络适配层执行工具。"""
import json
import math
import re

from fastapi import HTTPException


_TOOL_NAME = re.compile(r'[A-Za-z_][A-Za-z_0-9.-]{0,127}\Z')
_JSON_PATH = re.compile(r'\.([^.[\]]+)|\[(0|[1-9][0-9]{0,5})\]')

# Large HTML occupies one string. Bound the surrounding JSON structure before building Python containers.
_JSON_TOKEN = r'"(?:[^"\\]++|\\.)*+"|[]["{},:]'
_STRUCTURE = (re.compile(_JSON_TOKEN,re.DOTALL),re.compile(_JSON_TOKEN.encode(),re.DOTALL))


class IncompleteModelString(ValueError):
    """The JSON reader reached a string that can still receive more streamed bytes."""



def _json_constant(value):
    raise ValueError('Invalid JSON number: '+value)


def _json_float(value):
    number = float(value)
    if not math.isfinite(number):
        _json_constant(value)
    return number


def load_model_json(value, *, object_pairs_hook=None):
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
                raise IncompleteModelString('Unterminated model JSON string')
            if count > 65536 or depth > 128:
                raise ValueError('Model JSON structure too large')
    try:
        return json.loads(value, object_pairs_hook=object_pairs_hook,
                          parse_constant=_json_constant, parse_float=_json_float)
    except RecursionError:
        raise ValueError('Model JSON structure too deep') from None


def configure_tools(fmt, body, tools, *, required=True, batch=False, native=False):
    """同一份函数定义分别转换为 OpenAI、Anthropic、Gemini 的工具声明。"""
    if fmt == 'openai':
        body.update(tools=[{'type': 'function', 'function': tool} for tool in tools],
                    tool_choice='required' if required else 'auto', parallel_tool_calls=batch)
    elif fmt == 'anthropic':
        body.update(tools=[{'name': t['name'], 'description': t['description'], 'input_schema': t['parameters']} for t in tools],
                    tool_choice={'type': 'any' if required else 'auto', 'disable_parallel_tool_use': not batch})
    else:
        declarations = [{'name':tool['name'], 'description':tool['description'],
                         'parametersJsonSchema':tool['parameters']} for tool in tools] if native else tools
        body.update(tools=[{'functionDeclarations': declarations}],
                    toolConfig={'functionCallingConfig': {'mode': 'ANY' if required else 'AUTO'}})


def raw_tool_calls(fmt, data):
    if fmt == 'openai':
        message = (data.get('choices') or [{}])[0].get('message') or {}
        raw = message.get('tool_calls')
        if raw is None:
            raw = [{'function':message['function_call']}] if message.get('function_call') else []
        if not isinstance(raw,list):
            raise ValueError()
        calls = [{'index': c.get('index', i), 'id': c.get('id'), 'name': (c.get('function') or {}).get('name', ''),
                  'arguments': (c.get('function') or {}).get('arguments', {})} for i, c in enumerate(raw)]
    elif fmt == 'anthropic':
        calls = [{'index': c.get('index', i), 'id': c.get('id'), 'name': c.get('name',''), 'arguments': c.get('input',{})}
                 for i, c in enumerate(data.get('content',[])) if c.get('type')=='tool_use']
    else:
        parts = ((data.get('candidates') or [{}])[0].get('content') or {}).get('parts',[])
        calls = []
        for p in parts:
            if 'functionCall' in p and not p.get('thought'):
                function = p['functionCall']
                call = {'index': len(calls), 'id': function.get('id'), 'name': function.get('name',''), 'arguments': function.get('args',{})}
                # Custom Gemini gateways can stream typed JSONPath updates instead of args.
                if 'argumentChunks' in function or 'partialArgs' in function or function.get('willContinue'):
                    chunks = function.get('argumentChunks')
                    if chunks is None:
                        chunks = [{k:function[k] for k in ('args','partialArgs','willContinue') if k in function}]
                    call.update(arguments={'chunks':chunks}, argument_format='gemini_partial_args')
                calls.append(call)
    return calls



def identify_calls(calls):
    """Keep provider IDs and synthesize stable IDs for gateways that omit them."""
    reserved = {call['id'] for call in calls if isinstance(call.get('id'), str) and call['id']}
    seen = set()
    for index, call in enumerate(calls):
        call_id = call.get('id')
        if not isinstance(call_id, str) or not call_id or call_id in seen:
            call_id = f'call_{index}'
            while call_id in reserved or call_id in seen:
                call_id += '_'
            call.pop('wire_id', None)
        else:
            call['wire_id'] = call_id
        call['id'] = call_id
        seen.add(call_id)
    return calls



def gemini_arguments(chunks):
    """Assemble typed JSONPath deltas from a completed Vertex Gemini function call."""
    if not chunks or chunks[-1].get('willContinue'):
        raise ValueError('Incomplete function call')
    args, pending = {}, {}
    for chunk in chunks:
        initial = chunk.get('args', {})
        if not isinstance(initial, dict):
            raise ValueError('Invalid initial arguments')
        args.update(initial)
        for item in chunk.get('partialArgs', ()):
            path = item['jsonPath']
            parts = list(_JSON_PATH.finditer(path, 1))
            if (not path.startswith('$') or not 1 <= len(parts) <= 32 or not parts[0][1] or
                    '$' + ''.join(p[0] for p in parts) != path):
                raise ValueError('Invalid argument path')
            keys = set(item) - {'jsonPath', 'willContinue'}
            more = item.get('willContinue', False)
            if keys == {'stringValue'} and isinstance(item['stringValue'], str):
                pending.setdefault(path, []).append(item['stringValue'])
            elif keys or path not in pending:
                if (more or path in pending or len(keys) != 1 or
                        not _gemini_scalar(next(iter(keys)), item)):
                    raise ValueError('Invalid argument fragment')
                value = item[next(iter(keys))]
            if more:
                continue
            if path in pending:
                value = ''.join(pending.pop(path))
            branch = args
            for part in parts[:-1]:
                branch = branch.setdefault(part[1] if part[1] else int(part[2]), {})
            leaf = parts[-1]
            branch[leaf[1] if leaf[1] else int(leaf[2])] = value
    if pending:
        raise ValueError('Incomplete argument string')
    return _gemini_arguments_tree(args)


def _gemini_scalar(key, item):
    value = item[key]
    return ((key == 'nullValue' and value is None) or
            (key == 'boolValue' and type(value) is bool) or
            (key == 'numberValue' and (type(value) is int or type(value) is float and math.isfinite(value))))


def _gemini_arguments_tree(value):
    if not isinstance(value, dict):
        return value
    if any(isinstance(k, int) for k in value):
        if set(value) != set(range(len(value))):
            raise ValueError('Invalid array arguments')
        return [_gemini_arguments_tree(value[i]) for i in range(len(value))]
    return {k: _gemini_arguments_tree(v) for k, v in value.items()}


def parse_native_calls(fmt, data):
    try:
        calls = identify_calls(raw_tool_calls(fmt, data))
        if len(calls) > 8:
            raise invalid_tool_call('单批超过八项工具调用', len(calls))
        replay = native_replay(fmt, data)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise invalid_tool_call('工具调用结构无效', None) from None
    parsed = []
    for call in calls:
        name = call['name']
        args = call['arguments']
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            raise invalid_tool_call('工具名称格式无效', len(calls))
        error = None
        chunked = call.get('argument_format') == 'gemini_partial_args'
        try:
            if chunked:
                args = gemini_arguments(args['chunks'])
            if isinstance(args, str):
                args = load_model_json(args)
            if not isinstance(args, dict):
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            error = '工具参数分片不完整或格式无效' if chunked else '工具参数必须是有效 JSON 对象'
            args = {}
        item = {'id': call['id'], 'name': name, 'arguments': args}
        if fmt == 'gemini' and 'wire_id' in call:
            item['wire_id'] = call['wire_id']
        if error:
            item['argument_error'] = error
        parsed.append(item)
    return tuple(parsed), replay


def native_replay(fmt, data):
    """Retain provider-private blocks without duplicating large tool arguments or visible text."""
    if fmt == 'openai':
        message = (data.get('choices') or [{}])[0].get('message') or {}
        return {'reasoning_content': message['reasoning_content']} if message.get('reasoning_content') else None
    blocks = (data.get('content', []) if fmt == 'anthropic' else
              ((data.get('candidates') or [{}])[0].get('content') or {}).get('parts', []))
    layout = []
    index = 0
    for block in blocks:
        if fmt == 'anthropic':
            if block.get('type') == 'tool_use':
                layout.append({'call': index})
                index += 1
            elif block.get('type') in ('thinking', 'redacted_thinking'):
                layout.append(block)
            elif block.get('type') == 'text':
                layout.append({'text_length': len(block.get('text', ''))})
        elif 'functionCall' in block and not block.get('thought'):
            layout.append({**{key: block[key] for key in ('thoughtSignature',) if key in block}, 'call': index})
            index += 1
        elif block.get('thought'):
            layout.append(block)
        elif 'text' in block or 'thoughtSignature' in block:
            layout.append({key: value for key, value in block.items() if key != 'text'} |
                          ({'text_length': len(block['text'])} if 'text' in block else {}))
    return {'layout': layout} if layout else None


def _wire_calls(fmt, calls):
    if fmt == 'anthropic':
        return [{'type': 'tool_use', 'id': call['id'], 'name': call['name'], 'input': call['arguments']}
                for call in calls]
    return [{'functionCall': {'name': call['name'], 'args': call['arguments'],
                              **({'id': call['wire_id']} if call.get('wire_id') else {})}} for call in calls]


def _wire_assistant_parts(fmt, row):
    text = row['content']
    blocks = _wire_calls(fmt, row.get('tool_calls') or ())
    text_tag = {'type': 'text'} if fmt == 'anthropic' else {}
    layout = (row.get('replay') or {}).get('layout')
    if not layout:
        return ([{**text_tag, 'text': text}] if text or not blocks else []) + blocks
    offset = 0
    parts = []
    for entry in layout:
        extras = {key: value for key, value in entry.items() if key not in ('call', 'text_length')}
        if 'call' in entry:
            parts.append({**blocks[entry['call']], **extras})
        elif 'text_length' in entry:
            end = offset + entry['text_length']
            parts.append({**text_tag, **extras, 'text': text[offset:end]})
            offset = end
        else:
            parts.append(extras)
    return parts


def wire_messages(fmt, history):
    """Encode the unified transcript in each provider's native assistant and tool roles."""
    rows = []
    gemini_ids = {}
    for row in history:
        role, content = row['role'], row['content']
        calls = row.get('tool_calls') or ()
        if fmt == 'openai':
            wire = {'role': role, 'content': content}
            if role == 'tool':
                wire['tool_call_id'] = row['tool_call_id']
            if role == 'assistant':
                if calls:
                    wire['content'] = content or None
                    wire['tool_calls'] = [
                        {'id': c['id'], 'type': 'function', 'function': {
                            'name': c['name'], 'arguments': json.dumps(c['arguments'], ensure_ascii=False, separators=(',', ':')),
                        }} for c in calls
                    ]
                reasoning = (row.get('replay') or {}).get('reasoning_content')
                if reasoning:
                    wire['reasoning_content'] = reasoning
            rows.append(wire)
            continue
        key = 'parts' if fmt == 'gemini' else 'content'
        text_tag = {'type':'text'} if fmt == 'anthropic' else {}
        if role == 'assistant':
            gemini_ids = {c['id']: c['wire_id'] for c in calls if c.get('wire_id')}
            wire_role = 'model' if fmt == 'gemini' else 'assistant'
            value = _wire_assistant_parts(fmt, row) if fmt == 'gemini' or calls or row.get('replay') else content
            rows.append({'role': wire_role, key: value})
            continue
        if role == 'tool':
            call_id = row['tool_call_id']
            if fmt == 'gemini':
                result = {'name': row['name'], 'response': {'error' if row.get('is_error') else 'output': content}}
                if call_id in gemini_ids:
                    result['id'] = gemini_ids[call_id]
                part = {'functionResponse': result}
            else:
                part = {'type': 'tool_result', 'tool_use_id': call_id, 'content': content}
                if row.get('is_error'):
                    part['is_error'] = True
        else:
            part = {**text_tag, 'text': content}
        if not rows or rows[-1]['role'] != 'user':
            value = content if fmt == 'anthropic' and role == 'user' else [part]
            rows.append({'role': 'user', key: value})
            continue
        parts = rows[-1][key]
        if isinstance(parts, str):
            parts = rows[-1][key] = [{**text_tag, 'text':parts}]
        if role == 'tool':
            # Keep the corresponding result blocks directly after the assistant's tool calls.
            first_text = next((index for index, block in enumerate(parts) if 'text' in block), len(parts))
            parts.insert(first_text, part)
        else:
            parts.append(part)

    return rows


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
        undisclosed = tuple(dict.fromkeys(
            call['name'] for call in calls
            if isinstance(call.get('name'), str) and call['name'] not in allowed
        ))
        if undisclosed:
            error = invalid_tool_call('选择了本步未提供的工具',count)
            error.undisclosed_tools = undisclosed
            raise error
        parsed = []
        for call in calls:
            if call.get('argument_format') == 'gemini_partial_args':
                issue = '工具参数尚未传输完整'
                raise ValueError()
            name = call.get('name')
            if not isinstance(name,str):
                issue = '工具名称格式无效'
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
