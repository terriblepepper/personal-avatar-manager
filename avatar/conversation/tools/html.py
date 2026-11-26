"""Create a UTF-8 HTML artifact for the current attachment-capable turn."""
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from avatar.files.conversation.resources import validate_output
from avatar.core.artifact_limits import MAX_SOURCE_BYTES


class HtmlFile(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    filename: str = Field(min_length=5, max_length=200, pattern=r'(?i)\.html?$')


class WebPage(HtmlFile):
    html: str = Field(min_length=1, max_length=MAX_SOURCE_BYTES, description='HTML 源码。')


CREATE_HTML = {
    'name':'create_html',
    'description':'工具用于生成基于html的网页、2d动画和3d动画。作品在隔离环境中运行，无联网或宿主文件访问权限，可使用内置 Three.js。',
    'parameters':WebPage.model_json_schema(),
}


def checked_args(schema, args):
    if isinstance(args,dict) and isinstance(args.get('html'),str) and len(args['html']) > MAX_SOURCE_BYTES:
        raise HTTPException(413, 'HTML 文件不能超过 100 MiB')
    try:
        return schema.model_validate(args)
    except ValidationError as error:
        issues = []
        for failure in error.errors(include_input=False,include_url=False)[:4]:
            field = '.'.join(str(k) if isinstance(k,int) or k in schema.model_fields else '?' for k in failure['loc'])
            issues.append(field+' '+failure['type'])
        raise ValueError('参数校验失败：'+'；'.join(issues)) from None


def create_html(args, outputs):
    page = checked_args(WebPage,args)
    filename = page.filename
    data = page.html.encode('utf-8')
    if b'\x00' in data:
        raise HTTPException(422, 'HTML 必须为有效文本')
    if len(data) > MAX_SOURCE_BYTES:
        raise HTTPException(413, 'HTML 文件不能超过 100 MiB')
    validate_output(filename, data)
    for item in outputs:
        if item['filename'] == filename:
            if item.get('data') == data and item.get('html_mode') in (None, 'html'):
                return item
            raise HTTPException(409, '本轮已有同名文件，请使用不同文件名')
    if sum(len(item.get('data', b'')) for item in outputs) + len(data) > MAX_SOURCE_BYTES:
        raise HTTPException(413, '本轮生成文件合计超过 100 MiB')
    item = {'id': str(uuid.uuid4()), 'filename': filename, 'data': data, 'html_mode': 'html'}
    outputs.append(item)
    return item
