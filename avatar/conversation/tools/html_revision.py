"""Apply anchored edits to a previously read original, then use the normal creation validator."""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from avatar.files.parsing.animation import MAX_SOURCE_BYTES


class SourceEdit(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    field: Literal['html', 'render_frame'] = 'html'
    old: str = Field(min_length=1, description='从原件复制的准确文本。未提供 offset 时须唯一匹配。')
    offset: int | None = Field(default=None, ge=0, description='可选的绝对 UTF-8 字节偏移，用于定位重复文本。')
    new: str


class Revision(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    file_id: str = Field(min_length=1, max_length=80)
    sha256: str = Field(pattern='^[0-9a-f]{64}$')
    edits: list[SourceEdit] = Field(min_length=1, max_length=32)


REVISION_SCHEMA = Revision.model_json_schema()
_FRAME_CALL = re.compile(r'\ninstallAnimationClock\(host, ([0-9.eE+-]+), function\(time, duration, state\) \{\n')
_PLAYER = re.compile(r'<script type="module">\n(?:[^\n]*\n)?function installAnimationClock\(host, duration, renderFrame\) \{')


def revision_tool(tool):
    if tool['name']!='create_html':
        return tool
    form = tool['parameters']
    properties = {**form['properties'], 'revision':{k:v for k,v in REVISION_SCHEMA.items() if k!='$defs'}}
    return {**tool, 'description':tool['description']+' 局部修改使用已读取原件的 file_id、sha256 和准确片段，另存新文件。',
            'parameters':{**form,'properties':properties,'$defs':{**form.get('$defs',{}),**REVISION_SCHEMA['$defs']},'required':['filename']}}


def author_fields(source, mode):
    if mode not in ('2d','3d'):
        return {'html':source}
    start = _PLAYER.search(source)
    if not start or _PLAYER.search(source,start.end()):
        raise ValueError('旧动画播放器边界不明确，请读取源码后提交完整作品')
    end = source.find('</script>', start.end())
    call = _FRAME_CALL.search(source,start.end(),max(end,0))
    if not call or source[end-5:end] != '\n});\n':
        raise ValueError('旧动画帧函数边界不明确，请读取源码后提交完整作品')
    after = end+len('</script>')
    if source[after:after+1]=='\n':
        after+=1
    return {'html':source[:start.start()]+source[after:], 'render_frame':source[call.end():end-5],
            'mode':mode, 'duration_seconds':float(call[1])}


def apply_edits(text, edits):
    source=text.encode('utf-8')
    size=len(source)
    spans=[]
    for edit in edits:
        old=edit.old.encode('utf-8')
        new=edit.new.encode('utf-8')
        if old==new:
            raise ValueError('修改前后的文本相同，请提交实际改动')
        if edit.offset is None:
            start=source.find(old)
            if start < 0 or source.find(old,start+1)>=0:
                raise ValueError('修改片段必须唯一匹配，或填写核对过的 UTF-8 字节偏移')
        else:
            start=edit.offset
            if not source.startswith(old,start):
                raise ValueError('字节偏移处的内容与原文不符，请重新读取对应片段')
        spans.append((start,start+len(old),new))
        size += len(new)-len(old)
    if size>MAX_SOURCE_BYTES:
        raise ValueError('修订后源码超过 100 MiB')
    parts=[]
    view=memoryview(source)
    previous=0
    for start,end,replacement in sorted(spans):
        if start < previous:
            raise ValueError('修改片段重叠，请合并成一个修改')
        parts.extend((view[previous:start],replacement))
        previous=end
    parts.append(view[previous:])
    return b''.join(parts).decode('utf-8')


def expand_revision(args, load_source, *, legacy_animation=False):
    from avatar.conversation.tools.html import checked_args
    allowed = {'revision','filename'} | ({'mode','duration_seconds'} if legacy_animation else set())
    if not load_source or set(args)-allowed:
        raise ValueError('修订需使用已读取的当前会话原件，完整源码与局部修改不可混用')
    change=checked_args(Revision,args['revision'])
    _,source,mode=load_source(change.file_id,change.sha256)
    original=author_fields(source,mode) if legacy_animation else {'html':source}
    result=dict(original)
    for field in dict.fromkeys(edit.field for edit in change.edits):
        if field not in result:
            raise ValueError('此原件没有要修改的字段')
        result[field]=apply_edits(result[field],[edit for edit in change.edits if edit.field==field])
    result.update((k,v) for k,v in args.items() if k not in ('revision','filename'))
    if result==original:
        raise ValueError('修改结果与原件相同，请核对要修改的位置')
    result['filename']=args.get('filename')
    return result
