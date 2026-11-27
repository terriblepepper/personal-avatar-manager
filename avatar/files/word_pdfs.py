"""Private, immutable Word derivatives owned by their original file."""
from contextlib import contextmanager
from pathlib import Path
import os
import shutil
import tempfile

WORD_SUFFIXES = ('.doc', '.docx')
WORD_PDF_SUFFIXES = ('.word-raster.pdf', '.word-vector.pdf')


@contextmanager
def _locked_folder(folder):
    from fcntl import LOCK_EX, flock

    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        flock(fd, LOCK_EX)
        yield
    finally:
        os.close(fd)


def _stored_pdf(path):
    for vector, suffix in enumerate(WORD_PDF_SUFFIXES):
        candidate = path.with_suffix(suffix)
        if not candidate.is_symlink() and candidate.is_file():
            return candidate, bool(vector)
    return None


def read_word_pdf(path):
    path.stat()
    pdf = _stored_pdf(path)
    if pdf is None:
        raise ValueError('Word 的 PDF 副本尚未准备好，请联系管理员')
    return pdf


def prepare_word_pdf(path, extension):
    from fcntl import LOCK_EX, flock

    with path.open('rb') as original:
        flock(original, LOCK_EX)
        identity = os.fstat(original.fileno())
        if not os.path.samestat(path.stat(), identity):
            raise FileNotFoundError(path)
        if pdf := _stored_pdf(path):
            return pdf
        # Keep the optional conversion backend out of the isolated plain-text reader.
        from avatar.files.parsing.document_images import converted, word_vectors
        vector = word_vectors(path, extension)
        target = path.with_suffix(WORD_PDF_SUFFIXES[vector])
        with converted(path, extension, 'pdf') as pdf, _locked_folder(path.parent):
            if not os.path.samestat(path.stat(), identity):
                raise FileNotFoundError(path)
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f'.word-pdf-{path.stem}-', delete=False) as staged:
                try:
                    with pdf.open('rb') as stream:
                        if stream.read(5) != b'%PDF-':
                            raise ValueError('LibreOffice 未返回有效的 PDF')
                        stream.seek(0)
                        shutil.copyfileobj(stream, staged)
                    staged.flush()
                    os.fsync(staged.fileno())
                    staged.close()
                    os.replace(staged.name, target)
                finally:
                    Path(staged.name).unlink(missing_ok=True)
        return target, vector


def link_word_pdf(source, target):
    pdf = _stored_pdf(source)
    if pdf is None:
        return None
    original, vector = pdf
    destination = target.with_suffix(WORD_PDF_SUFFIXES[vector])
    os.link(original, destination, follow_symlinks=False)
    return destination


def discard_original(path, *, staged=None):
    if path.suffix != '.blob':
        path.unlink(missing_ok=True)
        return
    try:
        with _locked_folder(path.parent):
            (staged or path).unlink(missing_ok=True)
            for suffix in WORD_PDF_SUFFIXES:
                path.with_suffix(suffix).unlink(missing_ok=True)
            for partial in path.parent.glob(f'.word-pdf-{path.stem}-*'):
                partial.unlink(missing_ok=True)
    except FileNotFoundError:
        pass
