"""Disk-backed upload buffers keep multi-GiB originals out of Python heap."""
from contextlib import contextmanager
import hashlib
import mmap
import os


@contextmanager
def upload_buffer(file):
    file.flush()
    file.seek(0, 2)
    if file.tell() == 0:
        yield b''
    else:
        with mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as data:
            yield data


def write_original(path, data):
    """Hash the same bounded blocks written to a newly created private original."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        checksum = hashlib.sha256()
        with os.fdopen(descriptor, 'wb') as output, memoryview(data) as view:
            for start in range(0, len(view), 1024**2):
                with view[start:start+1024**2] as block:
                    checksum.update(block)
                    output.write(block)
        return checksum.hexdigest()
    except BaseException:
        path.unlink(missing_ok=True)
        raise


async def upload_work(function,*args,**kwargs):
    from avatar.core.execution import run_blocking
    return await run_blocking(function,*args,**kwargs)


def clean_removed(originals):
    import logging
    for _,staged in originals:
        try:staged.unlink(missing_ok=True)
        except OSError:logging.getLogger(__name__).warning('Deferred private original cleanup until restart')


def recover_removed(database,directory,table):
    if table not in ('attachments','knowledge_assets'):raise ValueError('Invalid original table')
    for staged in directory.glob('.removed-*'):
        identifier=staged.name[len('.removed-'):len('.removed-')+36]
        with database.connect() as db:exists=db.execute('SELECT 1 FROM '+table+' WHERE id=?',(identifier,)).fetchone()
        if exists:staged.replace(directory/(identifier+'.blob'))
        else:staged.unlink(missing_ok=True)
