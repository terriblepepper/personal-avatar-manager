"""Create a UTF-8 HTML artifact for the current attachment-capable turn."""
import json
import re
import uuid
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from avatar.files.conversation.resources import validate_output
from avatar.files.parsing.animation import MAX_SOURCE_BYTES
from avatar.models.tools import load_model_json


class HtmlFile(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    filename: str = Field(min_length=5, max_length=200, pattern=r'(?i)\.html?$')


class WebPage(HtmlFile):
    html: str = Field(min_length=1, max_length=MAX_SOURCE_BYTES, description='完整网页源码，内联样式和脚本。')


class Animation(WebPage):
    mode: Literal['2d','3d'] = '2d'
    duration_seconds: float = Field(gt=0, allow_inf_nan=False, description='动画总秒数。')
    render_frame: str = Field(min_length=1, max_length=MAX_SOURCE_BYTES, description='JavaScript 函数体，按绝对秒数 time 绘制当前帧，可用 duration 和跨帧对象 state。')


CREATE_HTML = {'name':'create_html','description':'创建独立网页，例如个人主页。提交 filename 和完整 html；系统保存并预览。',
               'parameters':WebPage.model_json_schema()}
ANIMATION_PARAMETERS = Animation.model_json_schema()
OFFLINE_ANIMATION = ('动画必须完全离线，严禁使用任何网络资源，包括 CDN、远程脚本、字体、纹理、图片、音视频、接口和联网兜底。'
                     '内联 HTML/CSS/JS 和数据，图形自行绘制。Three.js 只用 <script type="module"> 与 '
                     'import * as THREE from "three"，由预览器提供本地核心模块；不要使用插件、外部 importmap 或全局 THREE。')
ANIMATION_INPUT = ('自由编写完整 html，提供 duration_seconds 和 render_frame。'
                   'html 定义场景和一次性初始化，render_frame 按绝对秒数 time 从任意位置重绘，可用 duration、state。'
                   'Three.js 在 html 模块中初始化，render_frame 可调用挂在 window 上的绘制函数。'
                   '平台提供播放、暂停和可拖动的进度条。所有运动由 time 驱动，勿另起计时、动画循环或独立播放的 CSS 动画。')
CREATE_ANIMATION = {
    'name':'create_animation',
    'description':'创建有时间变化的 HTML 动画，按意图选择二维或三维，默认二维。'+ANIMATION_INPUT+OFFLINE_ANIMATION,
    'parameters':ANIMATION_PARAMETERS,
}
ARTIFACT_TOOLS = ('create_html','create_animation')


def animation_tool(mode=None):
    if mode not in ('2d','3d'):
        return CREATE_ANIMATION
    properties = {**ANIMATION_PARAMETERS['properties'],'mode':{'type':'string','enum':[mode],'default':mode}}
    return {**CREATE_ANIMATION,'parameters':{**ANIMATION_PARAMETERS,'properties':properties,'required':[*ANIMATION_PARAMETERS['required'],'mode']}}


def html_repair_prompt(tool, mode):
    if tool == 'create_html':
        return '直接输出完整 HTML 网页，以 <html> 或 <!doctype html> 开头，以 </html> 结束。内联样式和脚本。'
    dimension = 'mode 必须为 '+mode+'。' if mode in ('2d','3d') else ''
    return '返回完整动画工具参数 JSON，包含 filename（.html）。'+dimension+ANIMATION_INPUT+OFFLINE_ANIMATION


def _artifact_fields(pairs):
    result = {}
    for name,value in pairs:
        if name in result and (type(result[name]) is not type(value) or result[name] != value):
            raise ValueError('作品参数含有取值冲突的重复字段')
        result[name] = value
    return result


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
    args = load_model_json(source,object_pairs_hook=_artifact_fields)
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
            field = '.'.join(str(k) if isinstance(k,int) or k in schema.model_fields else '?' for k in failure['loc'])
            issues.append(field+' '+failure['type'])
        raise ValueError('参数校验失败：'+'；'.join(issues)) from None


def create_html(args, outputs):
    page = checked_args(WebPage,args)
    return store_html(page.filename,page.html,'html',outputs)


class AnimationDependencies(HTMLParser):
    """Catch external asset declarations before delivery. The sandbox CSP also blocks dynamic requests."""
    block = ''
    document_end = None
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
        if self.document_end is None and tag in ('body', 'html'):
            self.document_end = self.getpos()
        if tag == self.block:
            self.block = ''

    def handle_data(self, data):
        if self.block == 'style':
            self.check_css(data)
        elif self.block == 'script' and any(m[1] != 'three' for m in self.module_imports.finditer(data)):
            raise ValueError('动画模块依赖不能离线加载。'+OFFLINE_ANIMATION)


@lru_cache(maxsize=1)
def animation_clock():
    return Path(__file__).with_name('animation_clock.js').read_text(encoding='utf-8')


def custom_animation(artifact):
    if not artifact.html.strip() or not artifact.render_frame.strip():
        raise ValueError('自由绘制需要 html、正数 duration_seconds 和按 time 重绘的 render_frame 函数体')
    if re.search(r'</script\b', artifact.render_frame, re.I):
        raise ValueError('render_frame 只填写 JavaScript 函数体，请移除 script 标签或转义结束标签')
    source = artifact.html
    parser = AnimationDependencies()
    parser.feed(source)
    parser.close()
    if parser.document_end is None:
        raise ValueError('自定义 html 需要完整页面，请补齐 body 或 html 结束标签')
    frame = AnimationDependencies()
    frame.feed('<script>'+artifact.render_frame+'</script>')
    frame.close()
    line, column = parser.document_end
    offset = 0
    for _ in range(line - 1):
        offset = source.index('\n', offset) + 1
    offset += column
    script = ('<script type="module">\n'+animation_clock()+
              '\nconst host = document.createElement("div");'
              '\nhost.style.cssText = "position:fixed;inset:auto 8px 8px;z-index:2147483647";'
              '\ndocument.body.append(host);'
              '\ninstallAnimationClock(host, '+json.dumps(artifact.duration_seconds)+
              ', function(time, duration, state) {\n'+artifact.render_frame+'\n});\n</script>\n')
    return source[:offset]+script+source[offset:]


def create_animation(args, outputs, *, expected_mode=None):
    artifact = checked_args(Animation,args)
    if expected_mode in ('2d','3d') and artifact.mode != expected_mode:
        raise ValueError('mode 应为 '+expected_mode)
    return store_html(artifact.filename,custom_animation(artifact),artifact.mode,outputs)


def store_html(filename, source, mode, outputs):
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
    if sum(len(item.get('data', b'')) for item in outputs) + len(data) > MAX_SOURCE_BYTES:
        raise HTTPException(413, '本轮生成文件合计超过 100 MiB')
    item = {'id': str(uuid.uuid4()), 'filename': filename, 'data': data, 'html_mode': mode}
    outputs.append(item)
    return item
