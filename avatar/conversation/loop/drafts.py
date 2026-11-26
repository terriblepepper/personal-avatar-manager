"""Durable UTF-8 HTML drafts and validated artifact publication."""

import json
import re
from hashlib import sha256

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from avatar.conversation.loop.store import RunConflict
from avatar.conversation.tools.html import HtmlFile, checked_args, create_html
from avatar.core.artifact_limits import MAX_SOURCE_BYTES
from avatar.core.execution import current_tool_call, run_blocking
from avatar.files.conversation.resources import validate_output


class AppendHtmlDraft(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    draft_id: str = Field(pattern=r'^draft-[a-f0-9]{32}$')
    offset: int = Field(ge=0, description='上一条草稿回执中的 UTF-8 字节游标。')
    chunk: str = Field(max_length=MAX_SOURCE_BYTES, description='紧接草稿尾部的 HTML 源码。')
    done: bool = Field(description='本段追加后是否可发布完整作品。')

    @field_validator('offset', mode='before')
    @classmethod
    def integer_offset(cls, value):
        # Gemini's typed numberValue may encode an integer as a floating-point number.
        return int(value) if type(value) is float and value.is_integer() else value


APPEND_HTML_DRAFT = {
    'name': 'append_html_draft',
    'description': '继续暂存的 HTML。使用最近回执的游标，完成后交付作品。',
    'parameters': AppendHtmlDraft.model_json_schema(),
}

_DECODER = json.JSONDecoder()
_SPACE = re.compile(r'\s*')
_STRING_PART = re.compile(r'[^"\\\x00-\x1f]+|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})')
_CUT_ESCAPE = re.compile(r'\\(?:u[0-9a-fA-F]{0,3})?')


def _unfinished_string(raw, start):
    cursor = start + 1
    while part := _STRING_PART.match(raw, cursor):
        cursor = part.end()
    if cursor < len(raw) and not _CUT_ESCAPE.fullmatch(raw, cursor):
        raise ValueError('无效的 JSON 字符串片段')
    value = _DECODER.decode(raw[start:cursor] + '"')
    if value and 0xd800 <= ord(value[-1]) <= 0xdbff:
        value = value[:-1]
    value.encode('utf-8')
    return value


def _json_prefix(raw, fields, content):
    cursor = _SPACE.match(raw).end()
    if raw[cursor:cursor + 1] != '{':
        return {}
    values = {}
    while (cursor := _SPACE.match(raw, cursor + 1).end()) < len(raw):
        try:
            key, end = _DECODER.raw_decode(raw, cursor)
        except json.JSONDecodeError:
            if raw[cursor] != '"':
                raise
            _unfinished_string(raw, cursor)
            break
        if key not in fields or key in values:
            raise ValueError('草稿参数名称无效')
        colon = _SPACE.match(raw, end).end()
        if raw[colon:colon + 1] != ':':
            break
        start = _SPACE.match(raw, colon + 1).end()
        try:
            value, end = _DECODER.raw_decode(raw, start)
        except json.JSONDecodeError:
            if key == content and raw[start:start + 1] == '"':
                values[key] = _unfinished_string(raw, start)
            break
        cursor = _SPACE.match(raw, end).end()
        delimiter = raw[cursor:cursor + 1]
        if delimiter not in ('', ',', '}'):
            raise ValueError('草稿参数分隔符无效')
        # A number at end-of-input may be a torn byte cursor.
        if delimiter or type(value) is str:
            values[key] = value
        if delimiter != ',':
            break
    return values


def _gemini_prefix(chunks, fields, content):
    values = {}
    parts = []
    for chunk in chunks:
        base = chunk.get('args', {})
        if set(base) - fields or set(base) & values.keys() or (parts and content in base):
            raise ValueError('草稿参数发生冲突')
        values.update(base)
        for update in chunk.get('partialArgs', ()):
            field = update['jsonPath'].removeprefix('$.')
            if update['jsonPath'] != '$.' + field or field not in fields or field in values:
                raise ValueError('草稿参数路径无效')
            more = update.get('willContinue')
            if field == content:
                if 'stringValue' in update:
                    value = update['stringValue']
                    if not isinstance(value, str):
                        raise ValueError('草稿正文必须是文本')
                    parts.append(value)
                elif not parts or more or set(update) - {'jsonPath', 'willContinue'}:
                    raise ValueError('草稿正文分片无效')
                if not more:
                    values[field] = ''.join(parts)
                    parts.clear()
            elif more:
                raise ValueError('草稿标识或游标未确认')
            else:
                kind = {'offset':'numberValue','done':'boolValue'}.get(field, 'stringValue')
                values[field] = update[kind]
    if parts:
        value = ''.join(parts)
        values[content] = value[:-1] if value and 0xd800 <= ord(value[-1]) <= 0xdbff else value
    return values


class HtmlDrafts:
    def __init__(self, store, state, outputs, *, saved_files=()):
        self.store = store
        self.state = state
        self.outputs = outputs
        self.saved_files = saved_files

    @property
    def active(self):
        return bool(self.state.get('drafts'))

    async def capture(self, call):
        return await run_blocking(self._capture, call)

    async def append(self, arguments):
        return await run_blocking(self._append, checked_args(AppendHtmlDraft, arguments))

    async def artifact(self, arguments):
        return await run_blocking(self._publish, arguments)

    async def resume_receipts(self):
        return await run_blocking(self._resume_receipts)

    def _resume_receipts(self):
        return [self._draft_receipt(name, draft, 'draft_resumed')
                for name, draft in self.state.get('drafts', {}).items()]

    def _capture(self, call):
        is_create = call['name'] == 'create_html'
        if not is_create and call['name'] != 'append_html_draft':
            return None
        fields = {'filename','html'} if is_create else set(AppendHtmlDraft.model_fields)
        content = 'html' if is_create else 'chunk'
        try:
            raw = call['arguments']
            if call.get('argument_format') == 'gemini_partial_args':
                values = _gemini_prefix(raw['chunks'], fields, content)
            else:
                values = _json_prefix(raw, fields, content) if isinstance(raw, str) else raw
            if not isinstance(values, dict) or set(values) - fields:
                return None
            text = values.get(content)
            size = len(text.encode('utf-8')) if isinstance(text, str) and '\x00' not in text else 0
            if is_create:
                filename = checked_args(HtmlFile, {'filename':values.get('filename')}).filename
                validate_output(filename, b'')
            else:
                request = checked_args(AppendHtmlDraft, {**values, 'done':False})
        except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
            return None

        if not is_create:
            return self._append(request) if size else None
        drafts = self.state.get('drafts', {})
        for name, draft in drafts.items():
            if draft['filename'] == filename:
                return self._draft_receipt(name, draft, 'draft_exists')
        if not size:
            return None
        key = f"{self.state.get('step', 0)}\0{call['id']}\0{filename}"
        name = 'draft-' + sha256(key.encode()).hexdigest()[:32]
        draft = {'name':name, 'filename':filename, 'offset':size}
        patch = {'drafts':{**drafts, name:draft}}
        self._check_quota(self.outputs, patch['drafts'])
        self._write_text(name, 0, text, patch)
        return self._draft_receipt(name, draft, 'truncated_draft_saved')

    def _append(self, request):
        invocation = current_tool_call.get()
        call = list(invocation[:2]) if invocation else None
        committed = self.state.get('html_append', {})
        if invocation and invocation[2] and committed.get('call') == call:
            if receipt := committed.get('artifact'):
                self.store.check()
                return receipt
            draft = committed['draft']
            return self._draft_receipt(draft['name'], draft, 'draft_appended')

        name = request.draft_id
        drafts = self.state.get('drafts', {})
        draft = drafts.get(name)
        if draft is None:
            raise ValueError('草稿不存在或已交付')
        if request.offset != draft['offset']:
            raise ValueError(f"草稿游标已变更，请从 {draft['offset']} 继续")
        if '\x00' in request.chunk:
            raise ValueError('HTML 必须为有效文本')
        if request.done:
            source = self.store.read(draft['name'])
            if len(source.encode('utf-8')) != draft['offset']:
                raise RunConflict('草稿正文与已确认游标不一致')
            return self._publish({'filename':draft['filename'], 'html':source + request.chunk},
                                 request=request, append_call=call)
        if not request.chunk:
            raise ValueError('请提交非空的源码片段')
        updated = {**draft, 'offset':request.offset + len(request.chunk.encode('utf-8'))}
        patch = {'drafts':{**drafts, name:updated}}
        if call:
            patch['html_append'] = {'call':call, 'draft':updated}
        self._check_quota(self.outputs, patch['drafts'])
        self._write_text(draft['name'], request.offset, request.chunk, patch)
        return self._draft_receipt(name, updated, 'draft_appended')

    def _check_quota(self, outputs, drafts):
        sizes = {item['id']:item['size'] for item in self.saved_files}
        sizes.update({item['id']:len(item['data']) for item in outputs if 'data' in item})
        if sum(sizes.values()) + sum(draft['offset'] for draft in drafts.values()) > MAX_SOURCE_BYTES:
            raise HTTPException(413, '本轮作品和草稿合计超过 100 MiB')

    def _publish(self, arguments, *, request=None, append_call=None):
        candidates = list(self.outputs)
        try:
            item = create_html(arguments, candidates)
        except HTTPException as error:
            if error.status_code != 409:
                raise
            raise ValueError(error.detail) from None
        if any(saved['filename'] == item['filename'] for saved in self.saved_files):
            raise ValueError('本轮已保存同名作品，请使用不同文件名')
        active = self.state.get('drafts', {})
        drafts = {name:draft for name,draft in active.items() if draft['filename'] != item['filename']}
        self._check_quota(candidates, drafts)
        outcome = {key:item[key] for key in ('id','filename')}
        patch = {'drafts':drafts}
        if append_call:
            patch['html_append'] = {'call':append_call, 'artifact':outcome}
        reused = len(candidates) == len(self.outputs)
        if not reused:
            name = active[request.draft_id]['name'] if request else 'artifact-' + item['id']
            data = item['data']
            receipt = {key:item[key] for key in ('id','filename','html_mode')} | {
                'draft':name, 'size':len(data), 'sha256':sha256(data).hexdigest(),
            }
            patch['files'] = [*self.state.get('files', ()), receipt]
            text = request.chunk if request else arguments['html']
            self._write_text(name, request.offset if request else 0, text, patch)
            self.outputs.append(item)
        elif drafts != active:
            self._write_text('', 0, '', patch)
        for key in active.keys() - drafts.keys():
            if reused or not request or key != request.draft_id:
                self.store.discard(active[key]['name'])
        return outcome

    def _write_text(self, name, offset, text, patch):
        if text:
            size = self.store.append(name, offset, text, patch=patch)
            if size != offset + len(text.encode('utf-8')):
                raise RunConflict('草稿写入游标不一致')
        else:
            self.store.merge(patch)
        self.state.update(patch)

    def _draft_receipt(self, name, draft, status):
        # Bound the source excerpt even when an upstream generation leaves a very large prefix.
        length = min(1200, draft['offset'])
        data = self.store.read_range(draft['name'], draft['offset'] - length, length)
        tail = data.decode('utf-8', errors='ignore')[-300:]
        return {'status':status, 'draft_id':name, 'filename':draft['filename'],
                'offset':draft['offset'], 'tail_preview':tail,
                'hint':'使用 append_html_draft 和回执游标继续追加源码，最后一段设 done=true。'}
