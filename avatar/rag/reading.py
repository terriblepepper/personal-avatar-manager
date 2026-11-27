"""按需读取授权原件的正文或原生多媒体。"""
import base64
from dataclasses import dataclass, field
from io import BytesIO
from contextlib import aclosing
from pathlib import Path
import subprocess
from fastapi import HTTPException
from pydantic import BaseModel,ConfigDict,Field,model_validator
from typing import Literal
from avatar.files.parsing.text_worker import read_original
from avatar.core.execution import run_blocking
from avatar.core.text_budget import AgentText
from avatar.files.parsing.document_images import traverse
from avatar.files.parsing.images import jpeg_bytes
from avatar.files.word_pdfs import WORD_SUFFIXES, read_word_pdf
from avatar.knowledge.media import command, probe


def read_has_content(item):
    return any((page.get('text') or '').strip() for page in item.get('pages', ()))


class ReadRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    file_id:str=Field(min_length=1,max_length=80)
    mode:Literal['pages','range','search','full']='pages'
    start:int=Field(default=1,ge=1,le=100000)
    end:int|None=Field(default=None,ge=1,le=100000)
    offset:int=Field(default=0,ge=0)
    query:AgentText=''

    @model_validator(mode='after')
    def search_query(self):
        if self.end is not None and self.end < self.start:raise ValueError('结束页不能小于开始页')
        if self.mode=='search' and not self.query.strip():raise ValueError('搜索原文需提供章节标题或原文短语')
        return self


@dataclass
class MaterialToolResult:
    content: dict
    revisions: list[tuple[tuple[str, str], str]]
    sources: list[dict]
    images: list[dict] = field(default_factory=list)
    audio: list[dict] = field(default_factory=list)


def _image(raw):
    return {'mime_type':'image/jpeg', 'data':base64.b64encode(raw).decode('ascii')}


async def read_original_tool(path, filename, media_type, request, *, vision, audio):

    extension = Path(filename).suffix.lower()
    images = []
    try:
        if media_type.startswith(('audio/', 'video/')):
            return await run_blocking(_media_segment, path, request, vision=vision, audio=audio)
        if media_type.startswith('image/'):
            if request.mode == 'search' or request.start != 1 or request.offset:
                raise ValueError('图片仅支持读取第 1 页')
            if not vision:
                raise ValueError('当前模型不支持图片输入，未读取原图')
            return {'coverage':'首帧图像', 'complete':True, 'next':None}, [_image(await run_blocking(jpeg_bytes, path))], []
        vector = True
        if extension in WORD_SUFFIXES:
            path, vector = await run_blocking(read_word_pdf, path)
            extension = '.pdf'
        end = None if request.mode == 'full' else request.end
        if request.mode == 'pages' and end is None:
            end = request.start
        batch_end = min(end or request.start + 15, request.start + 15) if extension == '.pdf' else end
        query = request.query if request.mode == 'search' else ''
        result = await run_blocking(read_original, path, extension, start=request.start, end=batch_end,
                                    offset=request.offset, query=query, limit=12000)
        if result.get('error'):
            raise ValueError(result['error'])
        if extension == '.pdf':
            total = result['total_pages']
            if not result['next'] and result['requested_end'] < min(end or total, total):
                result.update(complete=False, next={'start':result['requested_end']+1, 'offset':0})
            await _pdf_observation(path, request, result, images, vision=vision, vector=vector)
    except FileNotFoundError:
        raise HTTPException(404, '资料文件已不存在') from None
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        message = str(error) if isinstance(error, ValueError) else '原件解析失败或超时'
        raise HTTPException(422, message) from None
    return result, images, []


async def _pdf_observation(path, request, result, images, *, vision, vector):

    if request.mode == 'search':
        result['warning'] = '仅搜索 PDF 正文文字，图像中的文字不参与匹配。'
        return
    if not vision:
        result['warning'] = '仅取得 PDF 正文，当前模型无法读取图片或图表。'
        return
    if request.offset == 0:
        async with aclosing(traverse(path, {request.start}, vector=vector)) as visuals:
            item = await anext(visuals, None)
        if item:
            images.append(_image(await run_blocking(jpeg_bytes, BytesIO(item['data']))))
            result['image_page'] = request.start
    if result['requested_end'] > request.start:
        result['warning'] = '本次最多附带起始页画面，其他页仅读取正文；可按页查看画面。'


def _media_segment(path, request, *, vision, audio):
    import tempfile

    if request.mode == 'search' or request.offset:
        raise ValueError('音视频请按 30 秒片段读取，不支持字符搜索或偏移')
    duration, types = probe(path)
    start = (request.start - 1) * 30
    if start >= duration:
        raise ValueError('媒体片段编号超出范围')
    end = min(start + 30, duration)
    prefix = ['ffmpeg', '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe']
    images = []
    sounds = []
    if audio and 'audio' in types:
        with tempfile.TemporaryDirectory(prefix='avatar-read-media-') as directory:
            target = Path(directory) / 'audio.wav'
            command([*prefix, '-ss', str(start), '-i', str(path), '-t', str(end-start),
                     '-vn', '-ac', '1', '-ar', '16000', '-threads', '1', str(target)])
            raw = target.read_bytes()
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError('音频片段超过读取上限')
            sounds.append({'mime_type':'audio/wav', 'data':base64.b64encode(raw).decode('ascii')})
    if vision and 'video' in types:
        frame = command([*prefix, '-ss', str((start+end)/2), '-i', str(path), '-frames:v', '1',
                         '-vf', 'scale=800:800:force_original_aspect_ratio=decrease',
                         '-threads', '1', '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1'])
        images.append(_image(jpeg_bytes(BytesIO(frame))))
    if not (images or sounds):
        raise ValueError('当前模型无法接收此文件的原生音频或视频画面')
    covered = ['音轨'] if sounds else []
    if images:
        covered.append('视频中点采样画面')
    return {'start_seconds':start, 'end_seconds':end, 'duration_seconds':duration,
            'coverage':'、'.join(covered), 'complete':start == 0 and end == duration and types == {'audio'},
            'next':{'start':request.start+1} if end < duration else None}, images, sounds
