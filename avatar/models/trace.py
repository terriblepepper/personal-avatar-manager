"""Bounded, executor-scoped projections of actual model requests and responses."""
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from itertools import chain, islice
import re
import time
from uuid import uuid4

from avatar.core.execution import model_failure
from avatar.models.diagnostics import PURPOSES, stop_reason

_scope = ContextVar('model_io_scope', default=None)
_HIDDEN = '[敏感内容已隐藏]'
_SECRET_KEY = re.compile(r'^(?:authorization|proxy.authorization|x.api.key|api.?key|access.?token|refresh.?token|token|secret|password|cookie|set.cookie|private.?key|client.?secret|session.?id|signature|x.amz.signature|x.goog.signature|sig|reasoning(?:_content)?|thinking|thought(?:s|Signature)?|system(?:Instruction)?)$', re.I)
_SECRET_TEXT = re.compile(r'''(?:\bBearer\s|\bBasic\s+[A-Za-z0-9+/=]+|github_pat_|gh[pousr]_|sk-[\w-]+|-----BEGIN [\w ]*(?:KEY|CERTIFICATE)-----|(?:api.?key|password|authorization|access.?token|token|secret|cookie)["']?\s*[:=]|(?:[?&]|%26)(?:sig(?:nature)?|x[-_]amz[-_](?:signature|credential|security[-_]token)|x[-_]goog[-_](?:signature|credential)|api[-_]?key|access[-_]?token|key[-_]pair[-_]id)(?:=|%3d)|data:[^\s,]+;base64,|[a-z][a-z0-9+.\-]*://[^/?#\s@]+@|[a-z][a-z0-9+.\-]*://[^/?#\s@:]+:(?!\d+(?:[/\s?#]|$))|\beyJ[\w-]+\.eyJ[\w-]+\.|<\s*/?\s*(?:think(?:ing)?|analysis|reasoning)\b)''', re.I)
_TOOL_NAME = re.compile(r'[A-Za-z_][A-Za-z_0-9.-]{0,127}\Z')
_PREVIEW = 800


@contextmanager
def model_io_scope(progress, *, private=False):
    token = _scope.set((progress, bool(private)) if progress else None)
    try:
        yield
    finally:
        _scope.reset(token)


def preview(value, secret=''):
    """Share at most one excerpt budget across nested fields; never serialize a whole artifact."""
    remaining, nodes = _PREVIEW, 40

    def project(item, depth):
        nonlocal remaining, nodes
        if remaining <= 0 or nodes <= 0 or depth > 4:
            return '…'
        nodes -= 1
        remaining -= 2
        if isinstance(item, str):
            excerpt = item[:max(0,remaining) + 128]
            if _SECRET_TEXT.search(excerpt) or (secret and secret in excerpt):
                remaining -= len(_HIDDEN)
                return _HIDDEN
            size = min(len(item), max(remaining,0))
            remaining -= size
            return excerpt[:size] + (f'…〔节选 {size}/{len(item)} 字符〕' if len(item) > size else '')
        if isinstance(item, Mapping):
            result = {}
            priority = ('filename','mode','offset','cursor','goal','tool','next_offset','id','status')
            keys = chain((k for k in priority if k in item), (k for k in item if k not in priority))
            for key in islice(keys, 12):
                if remaining <= 0 or nodes <= 0:
                    break
                child = item[key]
                name = str(key)[:min(80,remaining)]
                remaining -= len(name)+4
                if (secret and secret in name) or _SECRET_TEXT.search(name):
                    continue
                result[name] = project(_HIDDEN if _SECRET_KEY.fullmatch(name) else child, depth+1)
            return result
        if isinstance(item, (tuple,list)):
            return [project(part, depth+1) for part in item[:8] if remaining>0 and nodes>0]
        if type(item) is int and item.bit_length() > 256:
            item = f'〔大整数，{item.bit_length()} bits〕'
        if type(item) not in (int,float,bool,str,type(None)):
            return '…'
        remaining -= len(str(item))
        return item

    return project(value, 0)


def emit_tool_result(name, arguments, result, *, secret):
    scope = _scope.get()
    if not scope or not isinstance(name,str) or not _TOOL_NAME.fullmatch(name):
        return
    emit, private = scope
    event = {'type':'tool','group':'tool_io','id':uuid4().hex,'name':name if private else '工具执行','status':'completed'}
    if private:
        event['input'] = {'tool':name, 'arguments':preview(arguments,secret)}
        event['output'] = {'result':preview(result,secret)}
    emit(event)


class ModelExchange:
    def __init__(self, config, secret, attempt):
        self.scope = _scope.get()
        self.secret = secret
        self.attempt = attempt
        purpose = config.get('_call_purpose', 'answer')
        self.kind = 'artifact' if purpose in ('segmented_writing','html_generation') else 'text'
        self.name = PURPOSES.get(purpose, '模型调用')
        self.purpose = purpose if purpose in PURPOSES else 'answer'
        self.event = None
        self.prefix = ''
        self.characters = 0
        self.last = time.monotonic()
        self.available = set()
        self.last_output = {}

    def start(self, fmt, body):
        if not self.scope:
            return
        if fmt == 'gemini':
            rows = body['contents']
            parts = rows[-1]['parts']
            tools = [t for group in body.get('tools',()) for t in group.get('functionDeclarations',())]
        else:
            rows = body['messages']
            parts = rows[-1]['content']
            tools = [t.get('function',t) for t in body.get('tools',())]
        names = [t['name'] for t in tools if _TOOL_NAME.fullmatch(t.get('name',''))]
        self.available = set(names)
        inputs = {'history_turns':sum(r['role']!='system' for r in rows[:-1]), 'tool_count':len(names), 'attempt':self.attempt}
        if self.scope[1]:
            inputs['tools'] = names
        if self.scope[1] and rows[-1]['role']=='user':
            # Only text from the actual last user-role message. System, history, media and headers stay private.
            message = parts if isinstance(parts,str) else next((p['text'] for p in parts if isinstance(p.get('text'),str)), '')
            inputs.update(message=preview(message,self.secret),message_chars=len(message))
        self.event = {'type':'tool','group':'model_io','id':uuid4().hex,'name':self.name,'purpose':self.purpose,'input':inputs}
        self.send('running', {'kind':self.kind})

    def send(self, status, output):
        if self.event:
            self.last_output = output
            self.scope[0]({**self.event,'status':status,'output':output})

    def delta(self, text, fragments):
        if not self.event:
            return
        self.characters += len(text)
        if self.scope[1] and len(self.prefix) < _PREVIEW + 128:
            self.prefix += text[:_PREVIEW + 128 - len(self.prefix)]
        now = time.monotonic()
        if now-self.last < 2:
            return
        self.last = now
        names = fragments.names()
        output = {'kind':'tool_calls' if names else self.kind, 'received_bytes':fragments.size, 'text_chars':self.characters}
        if names and self.scope[1]:
            output['tool_calls'] = [{'name':n} for n in names if n in self.available]
        # In-flight strings can split credentials and markup across chunks. Only release completed lines.
        if '\n' in self.prefix:
            output['text'] = preview(self.prefix.rsplit('\n',1)[0],self.secret)
        self.send('running',output)

    def proposed(self, fmt, payload):
        if not self.event:
            return
        from avatar.models.tools import raw_tool_calls
        try:
            calls = raw_tool_calls(fmt,payload)
        except (AttributeError, KeyError, IndexError, TypeError, ValueError):
            return
        if calls and self.scope[1]:
            self.last_output = {**self.last_output, 'kind':'tool_calls','tool_calls':self.calls(calls)}

    def calls(self, calls):
        result = []
        for call in calls[:8]:
            name = call.get('name')
            known = isinstance(name,str) and name in self.available
            item = {'name':name if known else '未声明工具'}
            if self.scope[1] and known and 'arguments' in call:
                item['arguments'] = preview(call['arguments'],self.secret)
            result.append(item)
        return result

    def complete(self, result=None, *, reason=None):
        if not self.event:
            return
        calls = (result.tool_calls or result.partial_tool_calls) if result else ()
        reason = result.finish_reason if result else reason
        output = {'kind':'tool_calls' if calls else self.kind, 'finish_reason':stop_reason(reason,calls)}
        if self.scope[1]:
            text = result.text if result else self.prefix
            if text:
                output.update(text=preview(text,self.secret),text_chars=len(text) if result else self.characters)
        if calls and self.scope[1]:
            output['tool_calls'] = self.calls(calls)
        self.send('completed', output)
        self.event = None

    def fail(self, exc, *, retry=None):
        if not self.event:
            return
        status = 'error' if isinstance(exc, Exception) else 'interrupted'
        reason = getattr(exc,'stop_reason',None)
        detail = ('模型请求已中止' if status=='interrupted' else
                  '输入超过上下文窗口' if reason=='context_overflow' else
                  '模型响应流中断' if getattr(exc,'stream_interrupted',False) else
                  model_failure(exc)['reason'])
        output = {**self.last_output, 'reason':preview(detail,self.secret), 'attempt':self.attempt}
        if getattr(exc,'stream_interrupted',False):
            output['stream_interrupted'] = True
        if retry:
            output['retry'], output['retry_limit'] = retry
        if isinstance(getattr(exc,'status_code',None),int):
            output['status_code'] = exc.status_code
        self.send(status, output)
