"""Apply anchored edits to a previously read original, then use the normal creation validator."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from avatar.core.artifact_limits import MAX_SOURCE_BYTES


class SourceEdit(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    field: Literal['html'] = 'html'
    old: str = Field(min_length=1, description='从原件复制的准确文本。未提供 offset 时须唯一匹配。')
    offset: int | None = Field(default=None, ge=0, description='可选的绝对 UTF-8 字节偏移，用于定位重复文本。')
    new: str


class EditHtml(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    filename: str = Field(min_length=5, max_length=200, pattern=r'(?i)[.]html?$')
    file_id: str = Field(min_length=1, max_length=80)
    sha256: str = Field(pattern='^[0-9a-f]{64}$')
    edits: list[SourceEdit] = Field(min_length=1, max_length=32)


EDIT_HTML = {
    'name':'edit_html',
    'description':'局部修改已读取的 HTML 原件。填原文件 ID、SHA-256 和准确片段，另存为新作品。',
    'parameters':EditHtml.model_json_schema(),
}


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


def expand_revision(change, load_source):
    if load_source is None:
        raise ValueError('请先读取当前会话授权的 HTML 原件')
    _, source, _ = load_source(change.file_id, change.sha256)
    return {'filename':change.filename, 'html':apply_edits(source, change.edits)}
