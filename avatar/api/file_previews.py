"""Bounded Word PDF previews for file routes that already authorize the original."""
from pathlib import Path
from subprocess import SubprocessError
from threading import BoundedSemaphore

from fastapi import HTTPException, Response

from avatar.files.parsing.document_images import WORD_SUFFIXES, word_pdf as convert_word

MAX_WORD_PREVIEW_BYTES = 10 * 1024 * 1024
_word_conversions = BoundedSemaphore(2)


def word_pdf(resolve):
    path, row = resolve()
    extension = Path(row['filename']).suffix.lower()
    if extension not in WORD_SUFFIXES:
        raise HTTPException(415, '此文件不支持 Word 版式预览')
    if not _word_conversions.acquire(blocking=False):
        raise HTTPException(503, 'Word 预览繁忙，请稍后再试')
    try:
        if path.stat().st_size > 64 * 1024 * 1024:
            raise HTTPException(413, 'Word 原件超出预览大小限制，请下载查看')
        with convert_word(path, extension) as (pdf, _):
            if pdf.stat().st_size > MAX_WORD_PREVIEW_BYTES:
                raise HTTPException(413, 'Word 预览超过 10 MiB，请下载原件查看')
            content = pdf.read_bytes()
    except FileNotFoundError:
        raise HTTPException(404, 'Word 原件不存在或已过期') from None
    except (OSError, ValueError, SubprocessError):
        raise HTTPException(422, 'Word 无法转换为预览，请下载原件查看') from None
    finally:
        _word_conversions.release()
    resolve()
    return Response(content, media_type='application/pdf', headers={
        'Cache-Control': 'private, no-store',
        'Content-Disposition': 'attachment',
        'X-Content-Type-Options': 'nosniff',
    })
