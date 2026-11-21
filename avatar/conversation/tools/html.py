"""Create a UTF-8 HTML artifact for the current attachment-capable turn."""
import uuid
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from avatar.files.conversation.resources import validate_output
from avatar.files.parsing.animation import MAX_SOURCE_BYTES


class HtmlArtifact(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    mode: Literal['2d', '3d', 'html'] = 'html'
    filename: str = Field(min_length=5, max_length=200, pattern=r'(?i)\.html?$')
    html: str = Field(min_length=1, max_length=MAX_SOURCE_BYTES)


CREATE_HTML = {
    'name': 'create_html',
    'description': (
        '生成 HTML 原件。mode=2d 用 SVG、Canvas 或 CSS 绘制有时间变化的二维动画；3d 编写三维动画；默认使用 2d，按用户意图决定是否使用 3d。通用网页选 html。'
        '提交完整 HTML/CSS/JavaScript，使用内联代码和内嵌资源。'
        'Three.js 使用 <script type="module"> 和 import * as THREE from "three"；'
        '预览提供 Three.js WebGL 核心，不能加载 CDN、插件或远程数据。'
        '添加描述性 title 和页面正文方便资料检索。'
        '系统自动在气泡中预览文件。最终答复只说明成品，禁止重复任何 HTML、CSS、JavaScript 或写文件的 Python 源码。'
    ),
    'parameters': HtmlArtifact.model_json_schema(),
}


def html_tool(animation_required):
    if not animation_required:
        return CREATE_HTML
    schema = CREATE_HTML['parameters']
    properties = {**schema['properties'], 'mode': {'type': 'string', 'enum': ['2d', '3d'], 'default': '2d'}}
    return {**CREATE_HTML, 'parameters': {**schema, 'properties': properties, 'required': ['mode', 'filename', 'html']}}


def create_html(args, outputs, available, *, animation_required=False):
    artifact = HtmlArtifact.model_validate(args)
    if animation_required and artifact.mode == 'html':
        raise HTTPException(422, '本轮必须交付 HTML 动画。按用户意图选择 2d 或 3d，默认 2d。')
    data = artifact.html.encode('utf-8')
    if b'\x00' in data:
        raise HTTPException(422, 'HTML 必须为有效文本')
    if not artifact.html.lstrip().startswith('<') or '>' not in artifact.html:
        raise HTTPException(422, '请提交包含 HTML 标记的完整页面')
    if artifact.mode == 'html' and (len(artifact.html) > 262144 or len(data) > 1024 * 1024):
        raise HTTPException(422, '通用页面最多 262144 字符、1 MiB')
    validate_output(artifact.filename, data, html_mode=artifact.mode)
    for item in outputs:
        if item['filename'] == artifact.filename:
            if item.get('data') == data and item.get('html_mode', 'html') == artifact.mode:
                return item
            raise HTTPException(409, '本轮已有同名文件，请使用不同文件名')
    if len(outputs) >= available:
        raise HTTPException(413, '本轮附件已达到五份')
    item = {'id': str(uuid.uuid4()), 'filename': artifact.filename, 'data': data, 'html_mode': artifact.mode}
    outputs.append(item)
    return item
