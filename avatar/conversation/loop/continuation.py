"""Private, cursor-checked continuation for a single selected result."""
import asyncio
import hashlib
import json
import re
import uuid

from fastapi import HTTPException

from avatar.conversation.loop.artifact_prefix import edit_signature, revision_evidence, salvage_object
from avatar.conversation.loop.store import OWNER_STORAGE_BYTES, RAW_ARGUMENT_PREFIX, RunCapacity, draft_byte_limit
from avatar.conversation.tools.html import ANIMATION_INPUT, ARTIFACT_TOOLS, LEGACY_ANIMATION_TOOL, OFFLINE_ANIMATION, repaired_html_arguments
from avatar.core.execution import run_blocking
from avatar.models.tools import IncompleteModelString, load_model_json
from avatar.files.parsing.animation import MAX_SOURCE_BYTES


_MAX_STALLS = 3
_RECOVERY_SCAN_BYTES = 64 * 1024
_LEGACY_INVALID_OBJECT = '作品参数未形成完整对象，请从已确认游标补齐'
_MARKS = re.compile(r'\\.|[\\\"{}\[\],]', re.DOTALL)
_FIELDS = {'filename', 'mode', 'html', 'duration_seconds', 'render_frame', 'revision'}


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


def _track_field(previous, text, keys):
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
                if field:
                    keys.add(field)
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
    return {'field': command['field'], 'offset': offset}


def _view_room(budget, prompt, context):
    return max(0, budget - len(prompt.encode()) - len(json.dumps(context,ensure_ascii=False).encode()))


def _fit_json_text(text, room, *, tail=False):
    if len(json.dumps(text,ensure_ascii=False).encode()) - 2 <= room:
        return text
    low,high = 0,len(text)
    while low < high:
        count = (low+high+1)//2
        sample = text[-count:] if tail else text[:count]
        if len(json.dumps(sample,ensure_ascii=False).encode()) - 2 <= room:
            low = count
        else:
            high = count-1
    return (text[-low:] if tail else text[:low]) if low else ''


_OFFSET_PREFIX = r'^\s*\{\s*"offset"\s*:\s*(0|[1-9][0-9]*)\s*,\s*'
_SEGMENT_HEADER = re.compile(_OFFSET_PREFIX + r'(?:"field"\s*:\s*"(html|render_frame)"\s*,\s*)?"text"\s*:\s*"')
_FIELDS_HEADER = re.compile(_OFFSET_PREFIX + r'"fields"\s*:\s*(?=\{)')
_JSON_TEXT = re.compile(r'[^"\\\x00-\x1f]+|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})')


def partial_segment(envelope, offset, allowed):
    """Recover only fields or string tokens following an explicit matching cursor."""
    header = _SEGMENT_HEADER.match(envelope)
    if not header:
        header = _FIELDS_HEADER.match(envelope)
        if not header or int(header[1]) != offset or not allowed:
            raise ValueError()
        source = envelope[header.end():]
        keys = set()
        _track_field([0,False,False,False,None,''],source,keys)
        if 'revision' in keys:
            try:
                fields,_ = json.JSONDecoder().raw_decode(source)
            except json.JSONDecodeError:
                return {'offset':offset,'incomplete_revision_fields':source},{}
            return {'offset':offset,'fields':fields,'done':False},{}
        prefix,recovery = salvage_object(source,allowed)
        if recovery['conflicts']:
            raise ValueError()
        field = recovery['partial']
        fields = json.loads(prefix + ('"}' if field else '}'))
        if not field and fields:
            return {'offset':offset,'fields':fields,'done':False},{}
        if field not in ('html','render_frame') or field not in allowed:
            raise ValueError()
        text = fields.pop(field)
        return {'offset':offset,'field':field,'text':text,'end_field':False,'done':False},fields
    if int(header[1]) != offset or header[2] and header[2] not in allowed:
        raise ValueError()
    start = end = header.end()
    while token := _JSON_TEXT.match(envelope, end):
        end = token.end()
    remainder = envelope[end:]
    if remainder and not (remainder.startswith('"') or remainder == '\\'
                          or re.fullmatch(r'\\u[0-9a-fA-F]{0,3}', remainder)):
        raise ValueError()
    closed = remainder.startswith('"')
    if closed:
        suffix = ''.join(remainder[1:].split())
        if header[2]:
            if suffix and not suffix.startswith(','):
                raise ValueError()
        elif not any(ending.startswith(suffix) for ending in (',"done":true}', ',"done":false}')):
            raise ValueError()
    text = json.loads('"' + envelope[start:end] + '"')
    if text and 0xd800 <= ord(text[-1]) <= 0xdbff:
        text = text[:-1]
        closed = False
    if not text:
        raise ValueError()
    part = {'offset':offset,'text':text,'done':False}
    if header[2]:
        # The envelope's text closing quote does not close the underlying source field.
        ended = closed and bool(re.match(r'^"\s*,\s*"end_field"\s*:\s*true\s*[,}]',remainder))
        part.update(field=header[2],end_field=ended)
    return part,{}


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
        self.max_tokens = max_tokens

    @staticmethod
    def _fields(meta):
        return _FIELDS if meta.get('legacy_animation') else {'filename','html','revision'}

    @staticmethod
    def _matches(meta, step):
        return meta.get('tool') == step.get('tool')

    def align(self, pending, *, artifact_ready=True):
        """Park a later artifact until the corresponding goal is eligible."""
        parked = list(self.state.get('parked_drafts') or ())
        active = self.state.get('draft') or {}
        changed = False
        if active.get('kind') == 'artifact' and pending:
            matches = [s for s in pending if self._matches(active,s)]
            bound = active.get('goal_no')
            if bound is None and matches:
                bound = active['goal_no'] = matches[0]['goal_no']
                changed = True
            if not artifact_ready or bound != pending[0]['goal_no'] or not self._matches(active,pending[0]):
                parked.append(active)
                self.clear()
                changed = True
        elif active.get('kind') == 'answer' and pending:
            parked.append(active)
            self.clear()
            changed = True
        if not self.active:
            for index, meta in enumerate(parked):
                if (artifact_ready and pending and self._matches(meta,pending[0]) and
                        meta.get('goal_no') in (None,pending[0]['goal_no'])
                        or not pending and meta.get('kind') == 'answer'):
                    self.state['draft'] = parked.pop(index)
                    changed = True
                    break
        if changed:
            self.state['parked_drafts'] = parked
        return changed

    def revise_goals(self, plan, keep):
        """A new user revision needs model confirmation before inheriting an old work product."""
        active = self.state.get('draft')
        candidates = ([active] if active else []) + list(self.state.get('parked_drafts') or ())
        parked, prior = [], list(self.state.get('prior_drafts') or ())
        self.clear()
        for meta in candidates:
            matches = [i for i,step in enumerate(plan,1) if self._matches(meta,step)]
            reusable = (meta.get('kind')=='answer' and not plan or meta.get('kind')=='artifact' and len(matches)==1)
            if not keep or not reusable:
                prior.append(meta)
                continue
            meta = {**meta,'finished':False,'goal_no':matches[0] if matches else None}
            if not self.active:
                self.state['draft'] = meta
            else:
                parked.append(meta)
        self.state.update(parked_drafts=parked,prior_drafts=prior)

    def _rebase(self, meta, suffix=''):
        if meta.get('kind') != 'artifact' or meta.get('local_revision') or 'repair' in meta or not meta['offset'] and not suffix:
            return False
        # Recovery samples confirmed bytes in bounded pages. Keep the complete original addressable.
        size = meta['offset']
        sample = bytearray()
        for offset in range(0, min(size, _RECOVERY_SCAN_BYTES), 4096):
            sample.extend(self.store.read_range(meta['name'],offset,min(4096,size-offset)))
        source = sample.decode('utf-8','ignore')
        scanned = len(source.encode('utf-8'))
        if size < _RECOVERY_SCAN_BYTES and suffix:
            room = _RECOVERY_SCAN_BYTES - size
            source += suffix[:room].encode('utf-8')[:room].decode('utf-8','ignore')
        if 'json_scan' not in meta and source.lstrip().startswith('<'):
            prefix = '{"html":'+json.dumps(source,ensure_ascii=False)[:-1]
            recovery = {'fields':[], 'conflicts':[], 'partial':'html'}
        else:
            prefix,recovery = salvage_object(source,self._fields(meta))
        if scanned < size:
            recovery.update(scanned_bytes=scanned,source_bytes=size)
        digest = hashlib.sha256(prefix.encode()).hexdigest()
        if digest == meta.get('recovery_digest'):
            return False
        if prefix == '{' and not recovery['fields'] and not recovery['partial'] and meta.get('rebuilt'):
            return False
        updated = {k:meta[k] for k in ('kind','tool','mode','filename','goal_no','legacy_animation') if k in meta}
        updated.update(name=RAW_ARGUMENT_PREFIX+uuid.uuid4().hex, offset=0,misses=0,finished=False,
            json_scan=[0,False,False,False,None,''],recovery_digest=digest,recovered=recovery,
            field_boundary=not recovery['partial'],source_draft=meta.get('source_draft') or meta['name'],
            previous_draft=meta['name'],rebuilt=meta.get('rebuilt') or prefix=='{')
        self._append(updated,prefix)
        return True

    async def prepare_retry(self):
        meta = self.state.get('draft') or {}
        if (meta.get('finished') or not meta.get('misses') or
                not (meta.get('invalid_object') or meta.get('issue') == _LEGACY_INVALID_OBJECT)):
            return False
        return await run_blocking(self._rebase,meta)

    @property
    def active(self):
        return bool(self.state.get('draft'))

    @property
    def needs_visible_output(self):
        meta = self.state.get('draft') or {}
        # Older checkpoints recorded a failed zero-byte draft without the explicit diagnostic.
        return bool(meta.get('empty_limit') or (meta.get('offset') == 0 and meta.get('issue')))

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
                meta['repair']['seen_views'] = []

    async def migrate_html_drafts(self):
        changed = False
        for meta in (self.state.get('draft') or {}, *(self.state.get('parked_drafts') or ())):
            if meta.get('tool')==LEGACY_ANIMATION_TOOL:
                await self._migrate_animation(meta)
                changed=True
        return changed

    async def _migrate_animation(self, meta):
        meta['tool']='create_html'
        if meta.pop('html_contract',False):
            meta['legacy_animation']=True
            return
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
            filename=meta.get('filename','index.html')
            goal_no=meta.get('goal_no')
            meta.clear()
            meta.update(kind='artifact',tool='create_html',filename=filename,goal_no=goal_no,
                name=RAW_ARGUMENT_PREFIX+uuid.uuid4().hex,json_scan=[0,False,False,False,None,''],offset=0,misses=0)
        else:
            meta['legacy_animation']=True

    @property
    def exhausted(self):
        meta = self.state.get('draft') or {}
        return meta.get('repair', {}).get('rounds', 0) >= 3

    def begin_repair(self, detail):
        meta = self.state.get('draft') or {}
        if meta.get('kind') != 'artifact' or not meta.get('finished'):
            return False
        repair = meta.setdefault('repair', {'edits': [], 'rounds': 0})
        repair.update(issue=detail[:360], view=None, reads=0, seen_views=[])
        meta.update(finished=False, misses=0, streak=0)
        return True

    async def start(self, result, *, tool=None, filename='index.html', decision=False):
        await run_blocking(self._start, result, tool, filename, decision)

    def _start(self, result, tool, filename, decision):
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
        if not calls and not text.strip() and getattr(result,'stop_reason',None) == 'output_limit':
            meta['empty_limit'] = True
        if kind == 'artifact':
            meta.update(tool=tool, filename=filename)
            if not text.lstrip().startswith('<'):
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
                keys=set()
                meta['json_scan'] = _track_field(meta['json_scan'],text,keys)
                if 'revision' in keys:
                    meta['local_revision'] = True
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
        return ('retry' if meta['misses'] >= _MAX_STALLS else 'more', None)

    async def request(self, compact=False, *, input_tokens=None):
        return await run_blocking(self._request,compact,input_tokens)

    def _request(self, compact, input_tokens):
        meta = self.state['draft']
        misses = meta['misses']
        budget = 3 * (input_tokens if input_tokens is not None else self.max_tokens or 1024)
        if 'repair' in meta:
            repair = meta['repair']
            _, call = self._finish(meta)
            fields = {k: (len(v.encode('utf-8')) if isinstance(v,str) else type(v).__name__)
                      for k, v in call['arguments'].items() if k in _FIELDS}
            prompt = ('原稿已保存，只修正失败字段。可一次给出完整正确字段，遵循本次模型输出预算。'
                    '需要查证或澄清时可用 {"offset":当前游标,"control":{"name":工具名,"arguments":参数对象}}；当前游标 '
                    + str(meta['offset']) + '。每个修改操作原样带整数 revision 和顶层字段名 field。'
                    'op=read 带 UTF-8 字节 offset，可带 find 查找字面文本。'
                    'op=replace 带字节 offset、匹配原文 old 与新文字 new。'
                    'op=set 带 value 补充或替换字段，null 删除字段。'
                    '以下数据列出字段长度（UTF-8 字节）和上次查看结果：\n')
            view = {k:repair['view'][k] for k in ('field','offset')} if repair['view'] else None
            info = {'revision':len(repair['edits']),'issue':repair['issue'],'fields':fields,
                    'view':view,'feedback':meta.get('issue')}
            if view:
                key,offset = view['field'],view['offset']
                view.update(text='',next_offset=fields[key])
                room = _view_room(budget,prompt,info)
                data = call['arguments'][key].encode('utf-8')
                text = _fit_json_text(data[offset:offset+room].decode('utf-8','ignore'),room)
                view.update(text=text,next_offset=offset+len(text.encode()))
            return prompt + json.dumps(info,ensure_ascii=False)
        if meta['kind'] == 'decision':
            return ('上次输出被截断，整批动作均未执行。根据目标及已确认回执，只提交一个新的完整决定。'
                    + ('仅选择当前最小的一步。' if misses else ''))
        if meta['kind'] == 'answer':
            target = '从断点继续正文。'
        elif meta.get('local_revision'):
            target = ('继续已选择的局部修订。revision.edits 内的改动及原件标识已经保存。'
                      '从游标处续写原参数 JSON 剩余字节，不要重发前缀或改写整份 HTML。'
                      '可用 read 的 source=current 和字节 offset 核对草稿。原格式确实无法补齐时可提交 '
                      '{"offset":当前游标,"reissue_revision":{"filename":输出文件名,"revision":{"file_id":原件ID,'
                      '"sha256":原件SHA256,"edits":[按原顺序保留已写改动并补完]}}}。')
        else:
            target = '为已选工具 ' + meta['tool'] + ' 续写参数。'
            if meta.get('legacy_animation'):
                if not compact and (not misses or not meta['offset']):
                    target += ANIMATION_INPUT + OFFLINE_ANIMATION
                else:
                    target += '延续已有 HTML 场景，在可用输出预算内尽量完成。'
            elif 'json_scan' in meta:
                target += '最终参数对象包含 filename 和 html。'
            target += '可一次完成多个字段。可在对象开头或字段边界提交 {"offset":游标,"fields":{未完成字段:值},"done":true}，平台进行 JSON 转义。'
            target += '正在编写 html 或 render_frame 时可提交 {"offset":游标,"field":"字段名","text":"新增的原始源码","end_field":true,"fields":{},"done":false}。end_field=false 继续同一字段，true 关闭字段并可附其余 fields。'
            scan = meta.get('json_scan')
            if scan:
                target += '保持参数 JSON 的字符串转义。当前顶层字段：' + (scan[-1] or '待写字段') + '。'
            else:
                target += '保持原始 HTML 格式。'
            if meta.get('mode'):
                target += '所选维度：' + meta['mode'] + '。'
        target += '写作时如需补查证据、请求更多工具或向用户澄清，可在游标边界提交 {"offset":当前游标,"control":{"name":已披露工具名,"arguments":参数对象}}，草稿保留。'
        if misses:
            target += '请核对下方反馈和已确认游标，按当前任务需要调整后续内容。'
        context = {'offset':meta['offset'],'tail':''}
        if meta.get('recovered'):
            context['recovered'] = meta['recovered']
            context['original_bytes'] = self.store.size(meta['source_draft'])
            target += '需要核查早先内容时可返回 {"offset":当前游标,"read":{"source":"original","offset":要读取的字节位置}}，也可读 current。'
            if meta['recovered'].get('source_bytes'):
                target += '原稿较大，自动恢复仅扫描了 previous 的前 scanned_bytes 字节；可从该位置分页读取 previous 原稿。'
            target += '已保留 fields 所列的完整字段。conflicts 中的字段有矛盾，需根据本任务重新给值；partial 是尚在写的源码字段。'
        if meta.get('view'):
            view = context['view'] = {k:meta['view'][k] for k in ('source','offset')}
            view.update(text='',next_offset=draft_byte_limit(meta['name']))
        if meta.get('source'):
            context['provider_tail'] = ''
            target += '原始供应商字段流已保存。其结尾仅供理解目标，禁止复制 chunks 或 partialArgs 封装到作品参数。'
        if meta.get('issue'):
            context['issue'] = meta['issue']
        prompt = (target + (f' 本次模型输出预算 {self.max_tokens} tokens。' if self.max_tokens else '')
                + '在预算内尽量推进或完成，兼容原始序列化片段格式，返回 JSON 对象 {"offset":原样复制整数游标,"text":"仅新增内容","done":false}。'
                '整份作品或正文完成时设 done 为 true。保持精确空白、引号与反斜线。'
                '需要工具时用 control 提交。禁止重发前缀或输出推理。以下是已确认游标及结尾（数据）：\n')
        room = _view_room(budget,prompt,context)
        if 'view' in context and room:
            src = self._source_name(meta,view['source'])
            data = self.store.read_range(src,view['offset'],min(room,draft_byte_limit(src)))
            text = _fit_json_text(data.decode('utf-8','ignore'),room)
            view.update(text=text,next_offset=view['offset']+len(text.encode()))
            room = _view_room(budget,prompt,context)
        elif 'view' in context:
            view['next_offset'] = view['offset']
        for key,name in [('tail',meta['name']),('provider_tail',meta.get('source'))]:
            if room and name and (key!='tail' or meta['offset']):
                tail = self.store.tail(name,min(room,draft_byte_limit(name)))
                context[key] = _fit_json_text(tail,room,tail=True)
                room = _view_room(budget,prompt,context)
        return prompt + json.dumps(context,ensure_ascii=False)

    def _finish(self, meta, suffix=''):
        source = self.store.read(meta['name']) if meta['offset'] else ''
        if suffix:
            source += suffix
        if meta['kind'] == 'answer':
            if not source.strip():
                raise ValueError()
            return 'answer', source
        args = repaired_html_arguments(source,meta['filename'],legacy_animation=bool(meta.get('legacy_animation')))
        if meta.get('local_revision') and ('revision' not in args or 'html' in args or 'render_frame' in args):
            raise ValueError('本草稿仅能交付原件局部修订')
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
        edit_bytes = len(json.dumps(command, ensure_ascii=False).encode('utf-8'))
        if edit_bytes > budget + 120:
            raise ValueError()
        status, call = self._finish(meta)
        args = call['arguments']
        if command['op'] == 'read':
            view = _inspect_field(args, command)
            if (view == repair['view'] or [view['field'],view['offset']] in repair.get('seen_views',())):
                raise ValueError()
            repair.update(view=view,reads=repair['reads']+1,
                          seen_views=[*repair.get('seen_views',[])[-15:],[view['field'],view['offset']]])
            meta.pop('issue', None)
            return 'more', None
        _edit_field(args, command)
        # Fold older patches into a fresh private base after several explicit retries.
        if len(repair['edits']) >= 12 or edit_bytes > 8192:
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

    async def _field_segment(self, meta, part, preceding):
        if 'json_scan' not in meta or type(part.get('done')) is not bool:
            raise ValueError()
        allowed = self._fields(meta)
        fields = part.get('fields',{})
        if not isinstance(fields,dict) or not set(fields)<=allowed:
            raise ValueError()
        scan = meta['json_scan']
        tail = await asyncio.to_thread(self.store.tail,meta['name'],256) if meta['offset'] else ''
        base = '' if meta['offset'] else '{'
        separator = '' if not meta['offset'] or tail.rstrip().endswith(('{',',')) else ','
        at_boundary = not meta['offset'] or (not scan[1] and scan[0]==1
                      and (meta.get('field_boundary') or not separator))
        saved = set((meta.get('recovered') or {}).get('fields',())) | set(meta.get('completed_fields',()))
        if saved.intersection(fields) or saved.intersection(preceding):
            raise ValueError()
        if preceding:
            if not at_boundary or not set(preceding) <= allowed:
                raise ValueError()
            base += separator + json.dumps(preceding,ensure_ascii=False,separators=(',',':'),allow_nan=False)[1:-1]
            separator = ','
            saved.update(preceding)
        if 'field' in part:
            if set(part)-{'offset','field','text','end_field','fields','done'}:
                raise ValueError()
            key = part['field']
            if key not in ('html','render_frame') or key not in allowed or key in saved or key in fields:
                raise ValueError()
            if not isinstance(part.get('text'),str) or type(part.get('end_field')) is not bool:
                raise ValueError()
            if (fields or part['done']) and not part['end_field']:
                raise ValueError()
            if at_boundary:
                base += separator+json.dumps(key)+':"'
            elif not (scan[1] and not scan[2] and scan[-1]==key):
                raise ValueError()
            base += json.dumps(part['text'],ensure_ascii=False)[1:-1]
            if not part['end_field']:
                return base,False
            base += '"'
            separator = ','
        elif set(part) != {'offset','fields','done'} or not at_boundary:
            raise ValueError()
        serialized = json.dumps(fields,ensure_ascii=False,separators=(',',':'),allow_nan=False)[1:-1]
        if serialized:
            base += separator+serialized
        if part['done']:
            base += '}'
        return base,part['done']

    @staticmethod
    def _source_name(meta, source):
        return {'current':meta['name'],'original':meta.get('source_draft'),'previous':meta.get('previous_draft')}.get(source)

    async def _inspect_draft(self, meta, command):
        if not isinstance(command,dict) or set(command) != {'source','offset'}:
            raise ValueError()
        source, start = command['source'],command['offset']
        name = self._source_name(meta,source)
        if not name or type(start) is not int or start < 0:
            raise ValueError()
        seen = meta.get('inspections',[])
        if [source,start] in seen:
            return self._miss('该页已在此游标读取；请提交新内容或检查另一页')
        data = await asyncio.to_thread(self.store.read_range,name,start,1)
        if not data or data[0] & 0xc0 == 0x80:
            raise ValueError()
        meta.update(view={'source':source,'offset':start},
                    inspections=[*seen[-15:],[source,start]])
        return 'more',None

    def _reissue_revision(self, meta, args):
        from avatar.conversation.tools.html_revision import Revision

        allowed = {'filename','revision'} | ({'mode','duration_seconds'} if meta.get('legacy_animation') else set())
        if not isinstance(args,dict) or set(args)-allowed or not isinstance(args.get('filename'),str):
            raise ValueError()
        change = Revision.model_validate(args.get('revision'))
        signatures = [edit_signature(edit.model_dump()) for edit in change.edits]
        evidence = meta.get('revision_evidence')
        if evidence is None:
            evidence = meta['revision_evidence'] = revision_evidence(self.store.read(meta['name']))
        confirmed = evidence['edits']
        if any(evidence.get(k,getattr(change,k))!=getattr(change,k) for k in ('file_id','sha256')):
            return self._miss('重新提交的修订必须使用已确认的同一原件 ID 和 SHA-256')
        partial = evidence.get('partial')
        if (signatures[:len(confirmed)]!=confirmed or partial and
                (len(signatures)==len(confirmed) or not partial.items()<=signatures[len(confirmed)].items())):
            return self._miss('重新提交须按原顺序保留此前完整改动和已确认的字段')
        updated = {k:meta[k] for k in ('kind','tool','filename','goal_no','mode','legacy_animation') if k in meta}
        updated.update(name=RAW_ARGUMENT_PREFIX+uuid.uuid4().hex,offset=0,misses=0,finished=True,
                       local_revision=True,source_draft=meta.get('source_draft') or meta['name'])
        self._append(updated,json.dumps(args,ensure_ascii=False,separators=(',',':'),allow_nan=False))
        return 'artifact',{'name':meta['tool'],'arguments':args}

    async def accept(self, result, limited):
        meta = self.state['draft']
        if not (getattr(result,'tool_calls',()) or getattr(result,'partial_tool_calls',()) or result.text.strip()):
            if limited:
                meta['empty_limit'] = True
                return self._miss('本次模型耗尽单次输出预算，未返回任何正文或工具参数，草稿游标未变化')
            return self._miss('模型结束响应，但没有返回可见正文或工具参数，草稿游标未变化')
        if meta['kind'] == 'decision':
            if not limited and (result.text.strip() or getattr(result, 'tool_calls', ())):
                return 'decide', result
            return self._miss('尚未返回完整的新决定')
        if getattr(result, 'tool_calls', ()) or getattr(result, 'partial_tool_calls', ()):
            return self._miss('只提交带游标的新增内容')
        try:
            budget = draft_byte_limit(meta['name'])
            envelope = result.text
            preceding = {}
            recovered_envelope = False
            try:
                part = load_model_json(envelope)
            except (json.JSONDecodeError,IncompleteModelString):
                if not limited or 'repair' in meta:
                    raise
                allowed = self._fields(meta) if meta.get('kind') == 'artifact' else set()
                part,preceding = await run_blocking(partial_segment,envelope,meta['offset'],allowed)
                recovered_envelope = True
            if isinstance(part,dict) and set(part)=={'offset','control'} and not limited:
                if type(part['offset']) is not int or part['offset'] != meta['offset']:
                    return self._miss('请复制当前已确认的字节游标')
                call = part['control']
                if (not isinstance(call,dict) or set(call)!={'name','arguments'} or
                        not isinstance(call['name'],str) or not isinstance(call['arguments'],dict)):
                    raise ValueError()
                return 'control',call
            if 'repair' in meta:
                if self.exhausted:
                    return 'retry', None
                if limited or not isinstance(part, dict):
                    return self._miss('本次字段修正没有返回完整操作')
                try:
                    return await run_blocking(self._repair, meta, part, budget)
                except (ValueError, AttributeError, KeyError, TypeError):
                    return self._miss('修正指令无效，请核对版本、字段及匹配原文的字节位置')
            if not isinstance(part,dict) or type(part.get('offset')) is not int or part['offset'] != meta['offset']:
                return self._miss('请复制当前已确认的字节游标')
            if set(part)=={'offset','reissue_revision'} and meta.get('local_revision') and not limited:
                return await run_blocking(self._reissue_revision,meta,part['reissue_revision'])
            if set(part)=={'offset','read'} and meta.get('kind')=='artifact' and not limited:
                return await self._inspect_draft(meta,part['read'])
            if recovered_envelope and 'incomplete_revision_fields' in part:
                # The fields envelope uses the same JSON representation as the saved tool object.
                await self._field_segment(meta,{'offset':meta['offset'],'fields':{},'done':False},{})
                fragment = part['incomplete_revision_fields']
                if meta['offset']:
                    tail = await asyncio.to_thread(self.store.tail, meta['name'], 256)
                    fragment = ('' if tail.rstrip().endswith(('{',',')) else ',') + fragment[1:]
                text,done = fragment,False
            elif 'fields' in part or 'field' in part:
                text,done = await self._field_segment(meta,part,preceding)
            else:
                if set(part) != {'offset','text','done'} or not isinstance(part['text'],str) or type(part['done']) is not bool:
                    raise ValueError()
                text,done = part['text'],part['done']
            data = text.encode('utf-8')
            _check_size(meta['name'],meta['offset']+len(data))
            if len(data) >= 24 and hashlib.sha256(data).hexdigest() == meta.get('last_digest'):
                return self._miss('上个片段已保存，请从游标之后继续')
        except (ValueError, TypeError, RecursionError):
            return self._miss('缺少可验证的分片，请复制游标并使用声明的分片格式')
        finished = done and not limited
        if not text and not finished:
            return self._miss('没有新的可见内容')
        output = ('more', None)
        if finished:
            try:
                output = await asyncio.to_thread(self._finish, meta, text)
            except (ValueError, TypeError, RecursionError):
                output = None
            if output is None:
                # Leave the exception scope before rebuilding so a traceback cannot pin a large failed parse.
                if await run_blocking(self._rebase,meta,text):
                    return 'more',None
                meta['invalid_object'] = True
                if meta.get('local_revision'):
                    return self._miss('已保留原局部修订及游标；请补齐 revision.edits，或用 reissue_revision 重提同一原件的修订')
                return self._miss('未形成有效作品对象，已保留现有字段，请补齐缺失或冲突字段')
        updated = {**meta, 'misses': 0, 'finished': finished, 'streak': 0 if limited else min(8, meta.get('streak', 0) + 1)}
        updated.pop('issue', None)
        updated.pop('invalid_object',None)
        updated.pop('view',None)
        updated.pop('inspections',None)
        updated['field_boundary'] = bool('fields' in part or 'field' in part) and ('field' not in part or part['end_field'])
        if preceding or 'fields' in part or part.get('end_field'):
            complete = set(meta.get('completed_fields',())) | set(preceding)
            complete.update(part.get('fields',{}))
            if part.get('end_field'):
                complete.add(part['field'])
            updated['completed_fields'] = sorted(complete)
        await run_blocking(self._append, updated, text)
        return output
