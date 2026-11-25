"""Local Word conversion and exhaustive document visual traversal; no model keys."""
from contextlib import contextmanager
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile
from xml.etree import ElementTree
from fastapi import HTTPException

RASTER = ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tif', '.tiff', '.webp')


def pdf_image(path,page):
    with tempfile.TemporaryDirectory(prefix='avatar-rag-') as directory:
        output=Path(directory)/'page'
        subprocess.run(['pdftoppm','-f',str(page),'-l',str(page),'-singlefile','-scale-to','1600','-jpeg',str(path),str(output)],capture_output=True,timeout=30,check=True)
        return (output.with_suffix('.jpg')).read_bytes()


def office_runtime():
    root = Path(os.environ.get('AVATAR_OFFICE_RUNTIME') or Path(sys.prefix) / 'share/avatar-office').resolve()
    local = root / 'usr/lib/libreoffice/program'
    binary = shutil.which('libreoffice') or shutil.which('soffice')
    env = {**os.environ, 'SAL_USE_VCLPLUGIN': 'svp', 'OMP_THREAD_LIMIT': '1'}
    if not binary and (local / 'soffice').is_file():
        binary = str(local / 'soffice')
        libs = (root / 'usr/lib').glob('*-linux-gnu')
        env['LD_LIBRARY_PATH'] = os.pathsep.join([str(local), *map(str, libs)])
    if not binary:
        raise ValueError('Word 转换需要 LibreOffice；请安装 libreoffice-writer 或执行 scripts/install_runtime.py office')
    return binary, env


@contextmanager
def converted(path, extension, output):
    binary, env = office_runtime()
    with tempfile.TemporaryDirectory(prefix='avatar-office-') as directory:
        root = Path(directory)
        profile = root / 'profile/user'
        profile.mkdir(parents=True)
        # Each conversion has a fresh profile with macros and link updates disabled.
        (profile / 'registrymodifications.xcu').write_text('''<?xml version="1.0"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>0</value></prop></item>
</oor:items>''')
        source = root / ('source' + extension)
        shutil.copyfile(path, source)
        result = subprocess.run([sys.executable,'-m','avatar.files.parsing.office',binary, '-env:UserInstallation=' + (root/'profile').as_uri(),
            '--headless', '--nologo', '--nodefault', '--norestore', '--nolockcheck',
            '--convert-to', output, '--outdir', str(root), str(source)],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        target = root / ('source.' + output)
        if result.returncode or not target.is_file():
            raise ValueError('Word 转换失败：文件可能已损坏、加密或格式不受支持')
        if target.stat().st_size > 64*1024*1024:
            raise ValueError('Word 转换结果超过 64 MiB 资源上限，未作为完整解析')
        yield target


def word_vectors(path, extension):
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('Word 原件超过 64 MiB 转换上限')
    vector = True
    if extension == '.docx':
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if len(entries) > 5000 or sum(item.file_size for item in entries) > 64 * 1024 * 1024:
                    raise ValueError('DOCX 解压规模超限')
                if any('vbaproject' in item.filename.lower() for item in entries):
                    raise ValueError('DOCX 不接受宏')
                if archive.getinfo('word/document.xml').file_size > 5 * 1024 * 1024:
                    raise ValueError('DOCX XML 超限')
                names = archive.namelist()
                vector = any(name.startswith(('word/charts/', 'word/diagrams/'))
                             or name.startswith('word/media/') and not name.endswith('/') and Path(name).suffix.lower() not in RASTER
                             or name.startswith('word/') and name.endswith('.xml') and needs_word_render(archive.read(name))
                             for name in names)
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
            raise ValueError('DOCX 文件结构无效') from None
    return vector


def pdf_visual_pages(reader, *, vector=True):
    paints = {b'Do', b'INLINE IMAGE'}
    if vector:
        paints.update((b'sh', b'S', b's', b'f', b'f*', b'F', b'B', b'B*', b'b', b'b*'))
    for index, page in enumerate(reader.pages, 1):
        content = page.get_contents()
        if content is not None and any(operator in paints for _, operator in content.operations):
            yield index


def pdf_images(path, pages=None, *, vector=True):
    from pypdf import PdfReader
    from avatar.files.parsing.images import jpeg_bytes
    reader = PdfReader(path, strict=False)
    if reader.is_encrypted:
        raise ValueError('PDF 已加密，无法解析图片')
    for number in pdf_visual_pages(reader, vector=vector):
        if pages is not None and number not in pages:
            continue
        # Render the page as well: vector plots, masks and nested forms are visual evidence too.
        with tempfile.TemporaryDirectory(prefix='avatar-document-page-') as directory:
            target = Path(directory) / 'page'
            subprocess.run(['pdftoppm', '-f', str(number), '-l', str(number), '-singlefile',
                '-scale-to', '2400', '-jpeg', str(path), str(target)],
                stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=30)
            yield {'id':f'page-{number}', 'page':number, 'label':f'第 {number} 页完整画面（含图表、矢量图及布局）', 'data':target.with_suffix('.jpg').read_bytes()}
        try:
            images = reader.pages[number-1].images
            for index in range(len(images)):
                try:
                    image = images[index]
                    data = jpeg_bytes(io.BytesIO(image.data))
                except (ValueError, OSError, NotImplementedError, HTTPException):
                    continue  # This object is already covered by the rendered page.
                yield {'id':f'page-{number}-image-{index+1}', 'page':number, 'label':f'第 {number} 页图片 {index+1}', 'data':data}
        except (ValueError, OSError, NotImplementedError):
            pass  # The full page remains the fallback for unsupported image codecs.


def needs_word_render(xml):
    picture = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pict'
    drawing = '{http://schemas.microsoft.com/office/word/2010/wordprocessingShape}wsp'
    rect = '{urn:schemas-microsoft-com:vml}rect'
    horizontal_rule = '{urn:schemas-microsoft-com:office:office}hr'
    for element in ElementTree.fromstring(xml).iter():
        if element.tag in (drawing, '{urn:schemas-microsoft-com:vml}shape'):
            return True
        if element.tag == picture:
            # Word stores paragraph separators as VML rectangles, without image or drawing content.
            if len(element) == 1:
                child = element[0]
                if child.tag == rect and not len(child) and child.get(horizontal_rule) in ('t', 'true', '1'):
                    continue
            return True
    return False


async def traverse(path, pages=None, *, vector=True):
    import asyncio
    import base64
    import json
    import signal
    from avatar.core.config import ROOT
    from avatar.core.performance import measure
    with measure('document_image_process_start'):
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-m', 'avatar.files.parsing.document_images', str(path),
            json.dumps(sorted(pages) if pages is not None else None), *([] if vector else ['--images-only']),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL, cwd=ROOT, limit=16*1024*1024, start_new_session=True,
        )
    try:
        while True:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=90)
            if not line:
                raise ValueError('文档图片解析中断，未完成全部图片读取')
            item = json.loads(line)
            if item.get('error'):
                raise ValueError(item['error'])
            if item.get('done'):
                await process.wait()
                if process.returncode:
                    raise ValueError('文档图片解析进程异常退出')
                break
            item['data'] = base64.b64decode(item['data'])
            yield item
    finally:
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        await process.wait()


def main():
    import base64
    import json
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (4*1024**3, 4*1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    try:
        pages = json.loads(sys.argv[2])
        collected = []
        size = 0
        for item in pdf_images(Path(sys.argv[1]), set(pages) if pages is not None else None, vector='--images-only' not in sys.argv):
            value = {**item, 'data':base64.b64encode(item['data']).decode()}
            if '--collect' in sys.argv:
                size += len(value['data'])
                if size > 64*1024*1024:
                    raise ValueError('文档图片合计超过 64 MiB，请拆分文档；未作为完整解析')
                collected.append(value)
            else:
                print(json.dumps(value), flush=True)
        print(json.dumps({'done':True, **({'images':collected} if '--collect' in sys.argv else {})}), flush=True)
    except Exception as exc:
        print(json.dumps({'error':str(exc) if isinstance(exc,ValueError) else '文档图片解析失败（'+type(exc).__name__+'）'}), flush=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
