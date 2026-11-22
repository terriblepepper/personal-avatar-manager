"""Private, cursor-checked continuation for a single selected result."""
import asyncio
import hashlib
import json
import re
import uuid

from fastapi import HTTPException

from avatar.conversation.loop.store import OWNER_STORAGE_BYTES, RAW_ARGUMENT_PREFIX, RunCapacity, draft_byte_limit
from avatar.conversation.tools.html import ANIMATION_INPUT, ARTIFACT_TOOLS, OFFLINE_ANIMATION, repaired_html_arguments
from avatar.core.execution import run_blocking
from avatar.files.parsing.animation import MAX_SOURCE_BYTES


_CHUNK_BYTES = (1200, 360, 120)
_TAIL_BYTES = 640
_MARKS = re.compile(r'\\.|[\\\"{}\[\],]', re.DOTALL)
_FIELDS = {'filename', 'mode', 'html', 'duration_seconds', 'render_frame'}


class DraftLimit(HTTPException):
    def __init__(self, detail='单份草稿不能超过 100 MiB'):
        super().__init__(413, detail)


def _check_size(name, size):
    limit = draft_byte_limit(name)
    if size > limit:
        raise DraftLimit(f'本步草稿超过 {limit:,} 字节上限')


class MemoryDraftStore:
    def __init__(self):
        self.buffers = {}
        self.chunk_sizes = {}
        self.storage_bytes = 0

    def append(self, name, offset, text, *, patch=None):
        data = text.encode('utf-8')
        buf = self.buffers.setdefault(name, bytearray())
        sizes = self.chunk_sizes.setdefault(name, {})
        if type(offset) is not int or offset < 0:
            raise HTTPException(409, '无效的草稿游标')
        if offset != len(buf):
            if sizes.get(offset) == len(data) and buf[offset:offset+len(data)] == data:
                return len(buf)
            raise HTTPException(409, '草稿游标已变更')
        _check_size(name, len(buf) + len(data))
        if data:
            charge = len(data) + len(name.encode()) + 256
            if self.storage_bytes + charge > OWNER_STORAGE_BYTES:
                raise RunCapacity('临时草稿存储空间不足')
            self.storage_bytes += charge
            buf.extend(data)
            sizes[offset] = len(data)
        return len(buf)

    def size(self, name):
        return len(self.buffers.get(name, b''))

    def read_range(self, name, start, limit):
        return bytes(self.buffers.get(name, b'')[start:start+limit])

    def tail(self, name, byte_limit):
        if byte_limit <= 0:
            return ''
        return self.buffers.get(name, b'')[-byte_limit:].decode('utf-8', 'ignore')

    def read(self, name):
        return self.buffers.get(name, b'').decode('utf-8')

    def discard(self, name):
        data = self.buffers.pop(name, ())
        sizes = self.chunk_sizes.pop(name, {})
        self.storage_bytes -= len(data) + len(sizes) * (len(name.encode()) + 256)


def _track_field(previous, text):
    """Carry JSON quoting/depth across chunks so braces inside JS strings never select a field."""
    depth, quoted, escaped, expect_key, key, field = previous
    if not text:
        return previous
    start = int(escaped)
    escaped = False
    for marker in _MARKS.finditer(text, start):
        position, token = marker.start(), marker.group()
        if key is not None:
            key = (key + text[start:position])[:32]
        start = marker.end()
        if token.startswith('\\'):
            escaped = token == '\\' and start == len(text)
            if key is not None:
                key = '?'
        elif token == '"':
            if quoted and key is not None:
                field = key if key in _FIELDS else ''
                key = None
            elif not quoted and depth == 1 and expect_key:
                key = ''
                expect_key = False
            quoted = not quoted
        elif not quoted:
            if token in ('{', '['):
                depth += 1
                expect_key = depth == 1 and token == '{'
            elif token in ('}', ']'):
                depth -= 1
                if depth == 0:
                    field = ''
            elif depth == 1:
                expect_key = True
    if key is not None:
        key = (key + text[start:])[:32]
    return [depth, quoted, escaped, expect_key, key, field]


def _edit_field(args, edit):
    field = edit['field']
    if field not in _FIELDS and field not in args:
        raise ValueError()
    if edit['op'] == 'set' and set(edit) == {'op','field','value'}:
        value = edit['value']
        if value is None:
            del args[field]
        elif value != args.get(field):
            args[field] = value
        else:
            raise ValueError()
        return
    if edit['op'] != 'replace' or set(edit) != {'op','field','offset','old','new'}:
        raise ValueError()
    data = args[field].encode('utf-8')
    old, new = edit['old'].encode('utf-8'), edit['new'].encode('utf-8')
    offset = edit['offset']
    if type(offset) is not int or not 0 <= offset <= len(data) or not data.startswith(old, offset) or old == new:
        raise ValueError()
    if len(data) + len(new) - len(old) > MAX_SOURCE_BYTES:
        raise DraftLimit()
    args[field] = (data[:offset] + new + data[offset+len(old):]).decode('utf-8')


def _inspect_field(args, command):
    if set(command) - {'op','field','offset','find'}:
        raise ValueError()
    data = args[command['field']].encode('utf-8')
    offset = command['offset']
    if type(offset) is not int or not 0 <= offset <= len(data):
        raise ValueError()
    if 'find' in command:
        offset = data.find(command['find'].encode('utf-8'), offset)
        if offset == -1:
            raise ValueError()
    if offset < len(data) and data[offset] & 0xc0 == 0x80:
        raise ValueError()
    return {'field': command['field'], 'offset': offset, 'text': data[offset:offset+_TAIL_BYTES].decode('utf-8', 'ignore')}


_SEGMENT_HEADER = re.compile(r'^\s*\{\s*"offset"\s*:\s*(0|[1-9][0-9]*)\s*,\s*"text"\s*:\s*"')
_JSON_TEXT = re.compile(r'[^"\\\x00-\x1f]+|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})')


def partial_segment(envelope, offset, budget):
    """Recover complete JSON string tokens only after the sender confirmed the byte cursor."""
    header = _SEGMENT_HEADER.match(envelope)
    if not header or int(header[1]) != offset:
        return None
    start = end = header.end()
    while token := _JSON_TEXT.match(envelope, end):
        end = token.end()
    remainder = envelope[end:]
    if remainder and not (remainder.startswith('"') or remainder == '\\'
                          or re.fullmatch(r'\\u[0-9a-fA-F]{0,3}', remainder)):
        return None
    if remainder.startswith('"'):
        suffix = ''.join(remainder[1:].split())
        if not any(ending.startswith(suffix) for ending in (',"done":true}', ',"done":false}')):
            return None
    text = json.loads('"' + envelope[start:end] + '"')
    if text and 0xd800 <= ord(text[-1]) <= 0xdbff:
        text = text[:-1]
    return text.encode('utf-8')[:budget].decode('utf-8', 'ignore')


def _gemini_prefix(arguments):
    """Only ordered top-level values have an unambiguous lossless JSON prefix."""
    values, chunks, field_parts, open_field = {}, arguments['chunks'], [], None
    if not isinstance(chunks, list) or not chunks:
        raise ValueError()
    for chunk in chunks:
        base = chunk.get('args', {})
        if not isinstance(base, dict) or (base and open_field) or not set(base) <= _FIELDS or set(base) & values.keys():
            raise ValueError()
        values.update(base)
        for update in chunk.get('partialArgs', []):
            path = update['jsonPath']
            field = path.removeprefix('$.')
            if path == field or field not in _FIELDS or field in values or open_field not in (None, field):
                raise ValueError()
            keys = set(update) - {'jsonPath','willContinue'}
            if keys == {'stringValue'} and isinstance(update['stringValue'], str):
                field_parts.append(update['stringValue'])
                if update.get('willContinue'):
                    open_field = field
                    continue
                values[field] = ''.join(field_parts)
                field_parts.clear()
                open_field = None
            elif keys == {'numberValue'} and not open_field and not update.get('willContinue'):
                values[field] = update['numberValue']
            else:
                raise ValueError()
    if open_field:
        values[open_field] = ''.join(field_parts)
    text = json.dumps(values, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return text[:-2] if open_field else text[:-1]


class SegmentedDraft:
    def __init__(self, store, state, *, max_tokens=None):
        self.store = store
        self.state = state
        self.max_chunk = min(16 * 1024, max(16, max_tokens - 64)) if type(max_tokens) is int else 16 * 1024

    def _budget(self, meta):
        if meta['misses']:
            return min(self.max_chunk, _CHUNK_BYTES[min(meta['misses'], len(_CHUNK_BYTES) - 1)])
        growth = min(meta.get('streak', 0) // 2, 4)
        return min(self.max_chunk, _CHUNK_BYTES[0] * 2 ** growth)

    @property
    def active(self):
        return bool(self.state.get('draft'))

    def clear(self):
        # Run cleanup owns the bytes; a previous checkpoint may still reference them.
        self.state.pop('draft', None)

    def reset_attempts(self):
        if self.active:
            meta = self.state['draft']
            meta['misses'] = 0
            meta['streak'] = 0
            if 'repair' in meta:
                meta['repair']['rounds'] = 0
                meta['repair']['reads'] = 0

    async def migrate_animation(self):
        """An unsubmitted legacy storyboard needs a new free-HTML body for the same goal."""
        meta = self.state.get('draft') or {}
        if meta.get('kind') != 'artifact' or meta.get('tool') != 'create_animation' or meta.get('html_contract'):
            return False
        legacy = {'template','scenes','title'}
        scan = meta.get('json_scan') or [None]
        found = scan[-1] in legacy
        if not found and meta['offset']:
            prefix = await asyncio.to_thread(self.store.read_range,meta['name'],0,4096)
            # Read top-level JSON keys only: template strings inside arbitrary HTML are valid.
            data = prefix.decode('utf-8','ignore').lstrip().removeprefix('{').lstrip()
            decode = json.JSONDecoder().raw_decode
            try:
                while data:
                    key,end = decode(data)
                    if not isinstance(key,str):
                        break
                    data = data[end:].lstrip()
                    if not data.startswith(':'):
                        break
                    if key in legacy:
                        found = True
                        break
                    _,end = decode(data[1:].lstrip())
                    data = data[1:].lstrip()[end:].lstrip()
                    if not data.startswith(','):
                        break
                    data = data[1:].lstrip()
            except (ValueError,RecursionError):
                pass
        if found:
            self.state['draft'] = {'kind':'artifact','tool':'create_animation','mode':meta.get('mode'),
                'filename':meta.get('filename','index.html'),'name':RAW_ARGUMENT_PREFIX+uuid.uuid4().hex,
                'json_scan':[0,False,False,False,None,''], 'offset':0,'misses':0,'html_contract':1}
        else:
            meta['html_contract'] = 1
        return True

    @property
    def exhausted(self):
        meta = self.state.get('draft') or {}
        return meta.get('repair', {}).get('rounds', 0) >= 3

    def begin_repair(self, detail):
        meta = self.state.get('draft') or {}
        if meta.get('kind') != 'artifact' or not meta.get('finished'):
            return False
        repair = meta.setdefault('repair', {'edits': [], 'rounds': 0})
        repair.update(issue=detail[:360], view=None, reads=0)
        meta.update(finished=False, misses=0, streak=0)
        return True

    async def start(self, result, *, tool=None, mode=None, filename='index.html', decision=False):
        await run_blocking(self._start, result, tool, mode, filename, decision)

    def _start(self, result, tool, mode, filename, decision):
        calls = tuple(getattr(result, 'partial_tool_calls', ()) or getattr(result, 'tool_calls', ()))
        text = result.text or ''
        evidence = None
        use_evidence = False
        origin = None
        kind = 'decision' if decision else 'answer'
        if len(calls) == 1 and calls[0].get('name') in ARTIFACT_TOOLS:
            tool = calls[0]['name']
            args = calls[0].get('arguments', '')
            origin = {k: calls[0][k] for k in ('index','name','argument_format') if k in calls[0]}
            origin.setdefault('index', 0)
            origin['arguments_type'] = 'string' if isinstance(args, str) else 'object'
            if calls[0].get('argument_format') == 'gemini_partial_args':
                evidence = json.dumps(args, ensure_ascii=False)
                try:
                    text = _gemini_prefix(args)
                except (KeyError, TypeError, AttributeError, ValueError):
                    use_evidence = True
                    text = ''
            else:
                text = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
            kind = 'artifact'
        elif calls:
            # A partial batch, including its complete-looking calls, can only be reconsidered.
            kind = 'decision'
            if len(text.encode('utf-8')) > MAX_SOURCE_BYTES:
                raise DraftLimit()
            text = json.dumps({'text': text, 'calls': [
                {'index': index, **{key: call[key] for key in ('index','name','arguments','argument_format') if key in call}}
                for index, call in enumerate(calls)]}, ensure_ascii=False)
        elif tool in ARTIFACT_TOOLS:
            kind = 'artifact'
        token = uuid.uuid4().hex
        meta = {'name': 'draft-' + token, 'kind': kind, 'offset': 0, 'misses': 0}
        if kind == 'artifact':
            meta.update(tool=tool, mode=mode, filename=filename)
            if tool == 'create_animation':
                meta['html_contract'] = 1
            if tool != 'create_html' or not text.lstrip().startswith('<'):
                meta['json_scan'] = [0, False, False, False, None, '']
        if 'json_scan' in meta or kind == 'decision' and calls:
            meta['name'] = RAW_ARGUMENT_PREFIX + token
        if origin is not None:
            attachments = []
            origin['arguments_ref'] = meta['name']
            origin['arguments_bytes'] = len(text.encode('utf-8'))
            if evidence is not None:
                ref = RAW_ARGUMENT_PREFIX + token + '-provider'
                origin.update(arguments_ref=ref, arguments_bytes=len(evidence.encode('utf-8')))
                attachments.append((ref, evidence))
                if use_evidence:
                    meta['source'] = ref
            if result.text:
                ref = 'draft-visible-' + token
                origin['text_ref'] = ref
                attachments.append((ref, result.text))
            meta['origin'] = origin
            for i, (ref, content) in enumerate(attachments):
                patch = {'draft': meta} if not text and i == len(attachments)-1 else None
                try:
                    self.store.append(ref, 0, content, patch=patch)
                except RunCapacity as error:
                    raise DraftLimit(str(error)) from error
        self._append(meta, text)

    def _append(self, meta, text):
        meta = dict(meta)
        data = text.encode('utf-8')
        size = len(data)
        offset = meta['offset']
        _check_size(meta['name'], offset + size)
        meta['offset'] += size
        if text:
            meta['last_digest'] = hashlib.sha256(data).hexdigest()
            if 'json_scan' in meta:
                meta['json_scan'] = _track_field(meta['json_scan'], text)
        # Commit the cursor, scan state and bytes together, including after a writer restart.
        if size:
            try:
                patch = {'draft': meta, 'usage': self.state.get('usage')}
                end = self.store.append(meta['name'], offset, text, patch=patch)
            except RunCapacity as error:
                raise DraftLimit(str(error)) from error
            if end != meta['offset']:
                raise HTTPException(409, '草稿进度已变更，请恢复最新检查点')
        self.state['draft'] = meta

    def _miss(self, reason):
        meta = self.state['draft']
        meta['misses'] += 1
        meta['streak'] = 0
        meta['issue'] = reason
        return ('retry' if meta['misses'] >= len(_CHUNK_BYTES) else 'more', None)

    async def request(self, compact=False):
        meta = self.state['draft']
        misses = meta['misses']
        tail_size = 160 if compact else _TAIL_BYTES
        budget = self._budget(meta)
        if 'repair' in meta:
            repair = meta['repair']
            _, call = await asyncio.to_thread(self._finish, meta)
            fields = {k: (len(v.encode('utf-8')) if isinstance(v,str) else type(v).__name__)
                      for k, v in call['arguments'].items() if k in _FIELDS}
            view = repair['view']
            if compact and view:
                view = {**view, 'text': view['text'].encode()[:tail_size].decode('utf-8', 'ignore')}
            info = {'revision': len(repair['edits']), 'issue': repair['issue'], 'fields': fields,
                    'view': view, 'feedback': meta.get('issue')}
            return (f'原稿已保存，只修正失败字段。仅返回 JSON，新增内容最多 {budget} 字节。'
                    '每个操作原样带整数 revision 和顶层字段名 field。'
                    'op=read 带 UTF-8 字节 offset，可带 find 查找字面文本。'
                    'op=replace 带字节 offset、匹配原文 old 与新文字 new。'
                    'op=set 带 value 补充或替换字段，null 删除字段。'
                    '以下数据列出字段长度（UTF-8 字节）和上次查看结果：\n' + json.dumps(info, ensure_ascii=False))
        if meta['kind'] == 'decision':
            return ('上次输出被截断，整批动作均未执行。根据目标及已确认回执，只提交一个新的完整决定。'
                    + ('仅选择当前最小的一步。' if misses else ''))
        tail = await asyncio.to_thread(self.store.tail, meta['name'], tail_size) if meta['offset'] else ''
        if meta['kind'] == 'answer':
            target = '正文只续写下一小段。'
        else:
            target = '为已选工具 ' + meta['tool'] + ' 续写参数。'
            if meta['tool'] == 'create_animation':
                if not compact and (not misses or not meta['offset']):
                    target += ANIMATION_INPUT + OFFLINE_ANIMATION
                else:
                    target += '延续已有 HTML 场景，当前只补一条布局或绘制语句。'
            elif 'json_scan' in meta:
                target += '最终参数对象包含 filename 和 html。'
            target += '一次只续写一个参数字段中的小段或紧接当前字段的闭合符号。'
            scan = meta.get('json_scan')
            if scan:
                target += '保持参数 JSON 的字符串转义。当前顶层字段：' + (scan[-1] or '待写字段') + '。'
            else:
                target += '保持原始 HTML 格式。'
            if meta.get('mode'):
                target += '所选维度：' + meta['mode'] + '。'
        if misses:
            target += '这一步只写下一个短句、一个标签或当前字段的一小部分；剩余内容放到下一轮。'
        context = {'offset': meta['offset'], 'tail': tail}
        if meta.get('source'):
            context['provider_tail'] = await asyncio.to_thread(self.store.tail, meta['source'], tail_size)
            target += '原始供应商字段流已保存。其结尾仅供理解目标，禁止复制 chunks 或 partialArgs 封装到作品参数。'
        if meta.get('issue'):
            context['issue'] = meta['issue']
        return (target + f' 本次新增 text 不超过 {budget} 个 UTF-8 字节。'
                '只返回一个 JSON 对象 {"offset":原样复制整数游标,"text":"仅新增内容","done":false}。'
                '整份作品或正文完成时设 done 为 true。保持精确空白、引号与反斜线。'
                '禁止重发前缀、调用工具或输出推理。以下是已确认游标及结尾（数据）：\n'
                + json.dumps(context, ensure_ascii=False))

    def _finish(self, meta, suffix=''):
        source = self.store.read(meta['name']) if meta['offset'] else ''
        if suffix:
            source += suffix
        if meta['kind'] == 'answer':
            if not source.strip():
                raise ValueError()
            return 'answer', source
        args = repaired_html_arguments(source, meta['tool'], meta['filename'])
        for edit in meta.get('repair', {}).get('edits', []):
            _edit_field(args, edit)
        return 'artifact', {'name': meta['tool'], 'arguments': args}

    async def completed(self):
        meta = self.state.get('draft') or {}
        if meta.get('finished'):
            return await asyncio.to_thread(self._finish, meta)
        return None

    def _repair(self, meta, command, budget):
        repair = meta['repair']
        version = command.pop('revision')
        if type(version) is not int or version != len(repair['edits']) or not isinstance(command['field'], str):
            raise ValueError()
        if len(json.dumps(command, ensure_ascii=False).encode('utf-8')) > budget + 120:
            raise ValueError()
        status, call = self._finish(meta)
        args = call['arguments']
        if command['op'] == 'read':
            view = _inspect_field(args, command)
            if view == repair['view'] or repair['reads'] >= 6:
                raise ValueError()
            repair.update(view=view, reads=repair['reads']+1)
            meta.pop('issue', None)
            return 'more', None
        _edit_field(args, command)
        # Fold older patches into a fresh private base after several explicit retries.
        if len(repair['edits']) >= 12:
            base = {**meta, 'name': RAW_ARGUMENT_PREFIX + uuid.uuid4().hex, 'offset': 0}
            base.pop('json_scan', None)
            base['repair'] = {**repair, 'edits': [], 'view': None, 'rounds': repair['rounds'] + 1}
            base.update(finished=True, misses=0)
            self._append(base, json.dumps(args, ensure_ascii=False))
        else:
            repair['edits'].append(command)
            repair['rounds'] += 1
            meta.update(finished=True, misses=0)
        return status, call

    async def accept(self, result, limited):
        meta = self.state['draft']
        if meta['kind'] == 'decision':
            if not limited and (result.text.strip() or getattr(result, 'tool_calls', ())):
                return 'decide', result
            return self._miss('尚未返回完整的新决定')
        if getattr(result, 'tool_calls', ()) or getattr(result, 'partial_tool_calls', ()):
            return self._miss('只提交带游标的新增内容')
        try:
            budget = self._budget(meta)
            envelope = result.text
            if len(envelope) > 6 * budget + 512:
                if not limited or 'repair' in meta:
                    raise ValueError()
                envelope = envelope[:6 * budget + 512]
            try:
                part = json.loads(envelope)
            except json.JSONDecodeError:
                if not limited or 'repair' in meta:
                    raise
                text = partial_segment(envelope, meta['offset'], budget)
                if not text:
                    raise
                part = {'offset':meta['offset'], 'text':text, 'done':False}
            if 'repair' in meta:
                if self.exhausted:
                    return 'retry', None
                if limited or not isinstance(part, dict):
                    return self._miss('本次字段修正没有返回完整操作')
                try:
                    return await run_blocking(self._repair, meta, part, budget)
                except (ValueError, AttributeError, KeyError, TypeError):
                    return self._miss('修正指令无效，请核对版本、字段及匹配原文的字节位置')
            if not isinstance(part, dict) or set(part) != {'offset', 'text', 'done'}:
                raise ValueError()
            if type(part['offset']) is not int or part['offset'] != meta['offset']:
                return self._miss('返回的游标与已确认游标不一致')
            text, done = part['text'], part['done']
            if not isinstance(text, str) or type(done) is not bool:
                raise ValueError()
            data = text.encode('utf-8')
            if len(data) > budget:
                if not limited:
                    return self._miss('新增片段超过本次字节数，请再拆小')
                text = data[:budget].decode('utf-8','ignore')
                data = text.encode('utf-8')
            if len(data) >= 24 and hashlib.sha256(data).hexdigest() == meta.get('last_digest'):
                return self._miss('上个片段已保存，请从游标之后继续')
        except (ValueError, TypeError, RecursionError):
            return self._miss('缺少可验证的分片，请复制游标后写极短的一段')
        finished = done and not limited
        if not text and not finished:
            return self._miss('没有新的可见内容')
        output = ('more', None)
        if finished:
            try:
                output = await asyncio.to_thread(self._finish, meta, text)
            except (ValueError, TypeError, RecursionError):
                return self._miss('作品参数未形成完整对象，请从已确认游标补齐')
        updated = {**meta, 'misses': 0, 'finished': finished, 'streak': 0 if limited else min(8, meta.get('streak', 0) + 1)}
        updated.pop('issue', None)
        await run_blocking(self._append, updated, text)
        return output
