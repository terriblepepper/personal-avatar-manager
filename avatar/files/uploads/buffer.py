"""Disk-backed upload buffers keep multi-GiB originals out of Python heap."""
from contextlib import contextmanager
import hashlib
import mmap
import os
import uuid
from avatar.core.execution import run_blocking
from avatar.files.word_pdfs import WORD_SUFFIXES, prepare_word_pdf, discard_original


@contextmanager
def upload_buffer(file):
    file.flush()
    file.seek(0, 2)
    if file.tell() == 0:
        yield b''
    else:
        with mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as data:
            yield data


def write_original(path, data, *, extension=None):
    """Hash the same bounded blocks written to a newly created private original."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        checksum = hashlib.sha256()
        with os.fdopen(descriptor, 'wb') as output, memoryview(data) as view:
            for start in range(0, len(view), 1024**2):
                with view[start:start+1024**2] as block:
                    checksum.update(block)
                    output.write(block)
        if extension in WORD_SUFFIXES:
            from subprocess import SubprocessError
            from fastapi import HTTPException
            try:
                prepare_word_pdf(path, extension)
            except (ValueError, SubprocessError):
                raise HTTPException(422, 'Word 转 PDF 失败，请检查文件格式或联系管理员') from None
        return checksum.hexdigest()
    except BaseException:
        discard_original(path)
        raise


async def upload_work(function,*args,**kwargs):
    return await run_blocking(function,*args,**kwargs)


@contextmanager
def staged_removals():
    """包住数据库事务，在提交后清理原件，回滚时恢复同目录暂存文件。"""
    originals = []
    def stage(path):
        if path.exists():
            staged = path.with_name('.removed-' + path.stem + '-' + uuid.uuid4().hex)
            path.replace(staged)
            originals.append((path, staged))
    try:
        yield stage
    except BaseException:
        for path, staged in reversed(originals):
            staged.replace(path)
        raise
    clean_removed(originals)


def clean_removed(originals):
    import logging
    for path,staged in originals:
        try:discard_original(path or staged, staged=staged)
        except OSError:logging.getLogger(__name__).warning('Deferred private original cleanup until restart')


def recover_removed(database,directory,table):
    if table not in ('attachments','knowledge_assets'):raise ValueError('Invalid original table')
    for staged in directory.glob('.removed-*'):
        identifier=staged.name[len('.removed-'):len('.removed-')+36]
        with database.connect() as db:exists=db.execute('SELECT 1 FROM '+table+' WHERE id=?',(identifier,)).fetchone()
        if exists:staged.replace(directory/(identifier+'.blob'))
        else:clean_removed([(staged.with_name(identifier+'.blob'),staged)])
