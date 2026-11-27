"""资料、知识库与会话上传共用的扩展名、文件头和 Office 目录校验。"""
import zipfile
from io import BytesIO
from pathlib import Path

from fastapi import HTTPException
from avatar.core.artifact_limits import MAX_SOURCE_BYTES
from avatar.files.parsing.media_types import MEDIA, validate_media


# Model and sandbox outputs retain a separate per-file processing bound.
MAX_FILE = 10 * 1024 * 1024
TYPES = {'.txt': 'text/plain', '.md': 'text/markdown', '.mmd': 'text/x-mermaid', '.html': 'text/html', '.htm': 'text/html', '.csv': 'text/csv', '.json': 'application/json',
         '.log': 'text/plain', '.tsv': 'text/tab-separated-values',
         '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
         '.webp': 'image/webp', '.gif': 'image/gif', '.bmp': 'image/bmp', '.pdf': 'application/pdf',
         '.doc': 'application/msword', '.ppt': 'application/vnd.ms-powerpoint',
         '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
         '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',**MEDIA}


ARTIFACT_SUFFIXES = ('.html', '.htm')


def html_file_limit(filename):
    return MAX_SOURCE_BYTES if (filename or '').lower().endswith(ARTIFACT_SUFFIXES) else None


def chat_file_limit(filename):
    limit = html_file_limit(filename)
    return MAX_FILE if limit is None else limit


class OfficeDirectory:
    """Storage examines only ZIP directory metadata. Parsers bound expanded document data separately."""

    def __init__(self, stream, length):
        self.stream, self.length = stream, length
        self.seek, self.tell = stream.seek, stream.tell

    def read(self, size=-1):
        if size < 0:
            size = max(0,self.length - self.tell())
        if size > 16 * 1024**2:
            raise ValueError('Office ZIP 目录过大')
        return self.stream.read(size)


# 职责：校验上传名称、扩展名和基础文件结构。
# 输入：filename 为客户端文件名，data 为已经限长的原件字节。
# 结果与约束：名称不参与存储路径；拒绝空文件、不支持类型、扩展名伪装及宏文件/超限压缩包。
def inspect_file(filename, data, *, max_size=None, generated=False):
    if not filename or len(filename) > 200 or any(ord(c) < 32 for c in filename) or '/' in filename or '\\' in filename:
        raise HTTPException(422, '文件名无效')
    extension = Path(filename).suffix.lower()
    if extension not in TYPES and not (generated and extension=='.zip'):
        raise HTTPException(415, '支持文本、图片、音频、视频、CSV/TSV/XLSX、PDF、Word 和 PPT；旧 XLS 请先另存为 XLSX/CSV')
    if not data:
        raise HTTPException(422, '不能上传空文件')
    html_limit = html_file_limit(filename)
    if html_limit is not None and len(data) > html_limit:
        raise HTTPException(413, 'HTML 文件不能超过 100 MiB')
    if max_size is not None and len(data) > max_size:
        raise HTTPException(413, '文件超过上传大小限制')
    if extension in ('.zip','.docx','.pptx','.xlsx'):
        archive_source = data if hasattr(data, 'seek') else BytesIO(data)
    if extension=='.zip' and not zipfile.is_zipfile(archive_source):
        raise HTTPException(415,'ZIP 文件格式无效')
    if extension in MEDIA:validate_media(extension,data[:32])
    if extension in ('.docx', '.pptx', '.xlsx'):
        try:
            if data[:4] != b'PK\x03\x04':
                raise ValueError('Office 文件头无效')
            with zipfile.ZipFile(OfficeDirectory(archive_source, len(data))) as archive:
                items = archive.infolist()
                required = {'.docx':'word/document.xml','.pptx':'ppt/presentation.xml','.xlsx':'xl/workbook.xml'}[extension]
                if required not in archive.namelist() or len(items) > 5000:
                    raise ValueError()
                if any('vbaproject' in item.filename.lower() for item in items):
                    raise ValueError()
        except (zipfile.BadZipFile, ValueError):
            raise HTTPException(415, 'Office 文件格式无效、包含宏或目录超限，请使用普通 DOCX/PPTX')
    signatures = {'.pdf': b'%PDF-', '.png': b'\x89PNG\r\n\x1a\n', '.jpg': b'\xff\xd8', '.jpeg': b'\xff\xd8',
                  '.gif': b'GIF8', '.bmp': b'BM', '.doc': b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1',
                  '.ppt': b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'}
    if extension in signatures and data[:len(signatures[extension])] != signatures[extension]:
        raise HTTPException(415, '文件内容与扩展名不匹配')
    if extension == '.webp' and not (data[:4] == b'RIFF' and data[8:12] == b'WEBP'):
        raise HTTPException(415, 'WebP 文件格式无效')
    if extension in ('.txt', '.md', '.mmd', '.csv', '.tsv', '.json', '.log', '.html', '.htm') and data.find(b'\x00') >= 0 and not data[:4].startswith((b'\xff\xfe',b'\xfe\xff',b'\x00\x00\xfe\xff')):
        raise HTTPException(415, '文本文件包含二进制内容')
    return extension
