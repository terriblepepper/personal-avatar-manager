"""Create a UTF-8 HTML artifact for the current attachment-capable turn."""
import json
import re
import uuid
from html.parser import HTMLParser
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from avatar.conversation.tools.animation_templates import Scene, TEMPLATES, render_animation
from avatar.files.conversation.resources import validate_output
from avatar.files.parsing.animation import MAX_SOURCE_BYTES
from avatar.models.tools import load_model_json


class HtmlFile(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    filename: str = Field(min_length=5, max_length=200, pattern=r'(?i)\.html?$')


class WebPage(HtmlFile):
    html: str = Field(min_length=1, max_length=MAX_SOURCE_BYTES, description='完整网页源码，内联样式和脚本。')


class Animation(HtmlFile):
    mode: Literal['2d','3d'] = '2d'
    html: str = Field(default='',max_length=MAX_SOURCE_BYTES)
    template: str = Field(default='',max_length=24)
    title: str = Field(default='',max_length=100)
    scenes: list[Scene] = Field(default_factory=list,max_length=12)


CREATE_HTML = {'name':'create_html','description':'创建独立网页，例如个人主页。提交 filename 和完整 html；系统保存并预览。',
               'parameters':WebPage.model_json_schema()}
ANIMATION_PARAMETERS = Animation.model_json_schema()
ANIMATION_PARAMETERS['properties']['scenes']['items'] = ANIMATION_PARAMETERS.pop('$defs')['Scene']
OFFLINE_ANIMATION = ('动画必须完全离线，严禁使用任何网络资源，包括 CDN、远程脚本、字体、纹理、图片、音视频、接口和联网兜底。'
                     '内联 HTML/CSS/JS 和数据，图形自行绘制。Three.js 只用 <script type="module"> 与 '
                     'import * as THREE from "three"，由预览器提供本地核心模块；不要使用插件、外部 importmap 或全局 THREE。')
CREATE_ANIMATION = {
    'name':'create_animation',
    'description':('创建有时间变化的 HTML 动画，按意图选择二维或三维，默认二维。'
                   '自定义动画提交完整 html；模板动画提交 template、title 和 1 至 12 个 scenes（label、caption），省略 html。'
                   + '；'.join(mode+' '+', '.join(key+'='+label for key,label in choices.items()) for mode,choices in TEMPLATES.items())
                   + '。'+OFFLINE_ANIMATION),
    'parameters':ANIMATION_PARAMETERS,
}
ARTIFACT_TOOLS = ('create_html','create_animation')


def animation_tool(mode=None):
    if mode not in ('2d','3d'):
        return CREATE_ANIMATION
    properties = {**ANIMATION_PARAMETERS['properties'],'mode':{'type':'string','enum':[mode],'default':mode}}
    return {**CREATE_ANIMATION,'parameters':{**ANIMATION_PARAMETERS,'properties':properties,'required':['mode','filename']}}


def html_repair_prompt(tool, mode):
    if tool == 'create_html':
        return '直接输出完整 HTML 网页，以 <html> 或 <!doctype html> 开头，以 </html> 结束。内联样式和脚本。'
    choices = {mode:TEMPLATES[mode]} if mode in ('2d','3d') else TEMPLATES
    return ('返回动画工具参数 JSON，包含 filename（.html）、mode、template、title 和 1 至 12 项 scenes。'
            '每项填 label、caption，省略 html。按目标选择动画维度及模板：'+json.dumps(choices,ensure_ascii=False)+OFFLINE_ANIMATION)


def repaired_html_arguments(text, tool, filename):
    source = text.strip()
    if source.startswith('```') and source.endswith('```'):
        header, newline, body = source.partition('\n')
        if newline and header.strip().lower() in ('```','```json','```html'):
            source = body[:-3].strip()
    if tool == 'create_html' and re.match(r'<(?:!doctype\s+html\b|html(?=[\s>]))',source,re.I):
        if source[-7:].lower() != '</html>':
            raise ValueError('修复结果缺少网页结束标签')
        try:
            if validate_output(filename,b'') != 'text/html':
                filename = 'index.html'
        except ValueError:
            filename = 'index.html'
        return {'filename':filename,'html':source}
    args = load_model_json(source)
    if not isinstance(args,dict):
        raise ValueError('修复结果缺少参数对象')
    return args


def checked_args(schema, args):
    if isinstance(args,dict) and isinstance(args.get('html'),str) and len(args['html']) > MAX_SOURCE_BYTES:
        raise HTTPException(413, 'HTML 文件不能超过 100 MiB')
    try:
        return schema.model_validate(args)
    except ValidationError as error:
        issues = []
        for failure in error.errors(include_input=False,include_url=False)[:4]:
            field = '.'.join(str(k) if isinstance(k,int) or k in (*schema.model_fields,'label','caption') else '?' for k in failure['loc'])
            issues.append(field+' '+failure['type'])
        raise ValueError('参数校验失败：'+'；'.join(issues)) from None


def create_html(args, outputs, available):
    page = checked_args(WebPage,args)
    return store_html(page.filename,page.html,'html',outputs,available)


class AnimationDependencies(HTMLParser):
    """Catch external asset declarations before delivery. The sandbox CSP also blocks dynamic requests."""
    block = ''
    css_resources = re.compile(r"""(?:url\(\s*['"]?([^'")\s]+)|@import\s+['"]([^'"]+))""",re.I)
    module_imports = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*)['"]([^'"]+)['"]""")

    @staticmethod
    def check_asset(value):
        if value and not value.strip().lower().startswith(('data:','blob:','#')):
            raise ValueError('动画引用了外部资源。'+OFFLINE_ANIMATION)

    def check_css(self, css):
        for match in self.css_resources.finditer(css):
            self.check_asset(match[1] or match[2])

    def handle_starttag(self, tag, attrs):
        if tag in ('script','style'):
            self.block = tag
        for key, value in attrs:
            if not value:
                continue
            asset = (key in ('src','poster') or (tag in ('link','image','use') and key in ('href','xlink:href'))
                     or (tag=='object' and key=='data'))
            if asset:
                self.check_asset(value)
            elif key == 'style':
                self.check_css(value)
            elif tag == 'script' and key == 'type' and value.strip().lower() == 'importmap':
                raise ValueError('请使用预览器提供的本地 Three.js 模块映射。'+OFFLINE_ANIMATION)

    def handle_endtag(self, tag):
        if tag == self.block:
            self.block = ''

    def handle_data(self, data):
        if self.block == 'style':
            self.check_css(data)
        elif self.block == 'script' and any(m[1] != 'three' for m in self.module_imports.finditer(data)):
            raise ValueError('动画模块依赖不能离线加载。'+OFFLINE_ANIMATION)


def create_animation(args, outputs, available, *, expected_mode=None):
    artifact = checked_args(Animation,args)
    if expected_mode in ('2d','3d') and artifact.mode != expected_mode:
        raise ValueError('mode 应为 '+expected_mode)
    if artifact.template:
        if artifact.html or not artifact.title.strip() or not artifact.scenes:
            raise ValueError('动画模板需要标题和分镜，请省略 html')
        source = render_animation(artifact.mode,artifact.template,artifact.title,artifact.scenes)
    else:
        if artifact.scenes or not artifact.html:
            raise ValueError('自定义动画需要完整 html，使用分镜时须选择动画模板')
        source = artifact.html
        parser = AnimationDependencies()
        parser.feed(source)
        parser.close()
    return store_html(artifact.filename,source,artifact.mode,outputs,available)


def store_html(filename, source, mode, outputs, available):
    data = source.encode('utf-8')
    if b'\x00' in data:
        raise HTTPException(422, 'HTML 必须为有效文本')
    if not source.lstrip().startswith('<') or '>' not in source:
        raise HTTPException(422, '请提交包含 HTML 标记的完整页面')
    if len(data) > MAX_SOURCE_BYTES:
        raise HTTPException(413, 'HTML 文件不能超过 100 MiB')
    validate_output(filename, data)
    for item in outputs:
        if item['filename'] == filename:
            if item.get('data') == data and item.get('html_mode', 'html') == mode:
                return item
            raise HTTPException(409, '本轮已有同名文件，请使用不同文件名')
    if len(outputs) >= available:
        raise HTTPException(413, '本轮附件已达到五份')
    item = {'id': str(uuid.uuid4()), 'filename': filename, 'data': data, 'html_mode': mode}
    outputs.append(item)
    return item
