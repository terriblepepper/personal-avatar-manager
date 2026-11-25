"""Bounded Word PDF previews for file routes that already authorize the original."""
from pathlib import Path

from fastapi import HTTPException, Response

from avatar.files.word_pdfs import WORD_SUFFIXES, read_word_pdf

MAX_WORD_PREVIEW_BYTES = 10 * 1024 * 1024


def word_pdf(resolve):
    path, row = resolve()
    extension = Path(row['filename']).suffix.lower()
    if extension not in WORD_SUFFIXES:
        raise HTTPException(415, '此文件不支持 Word 版式预览')
    try:
        pdf, _ = read_word_pdf(path)
        if pdf.stat().st_size > MAX_WORD_PREVIEW_BYTES:
            raise HTTPException(413, 'Word 预览超过 10 MiB，请下载原件查看')
        content = pdf.read_bytes()
    except FileNotFoundError:
        raise HTTPException(404, 'Word 原件不存在或已过期') from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    except OSError:
        raise HTTPException(422, '无法读取 Word 的 PDF 副本，请联系管理员') from None
    resolve()
    return Response(content, media_type='application/pdf', headers={
        'Cache-Control': 'private, no-store',
        'Content-Disposition': 'attachment',
        'X-Content-Type-Options': 'nosniff',
    })
