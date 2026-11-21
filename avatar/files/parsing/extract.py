# 模块说明：附件文字解析的隔离子进程；仅解析文件数据，不执行宏、脚本、外部链接或模型请求。
import json
import re
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from avatar.files.parsing import PLAIN_TEXT_EXTENSIONS
from xml.etree import ElementTree

MAX_TEXT = 30000
PDF_MAX_TEXT = 1000000
PDF_MAX_PAGES = 1000


def text_content(path, limit=-1):
    path = Path(path)
    with path.open('rb') as source:
        prefix = source.read(4)
    if prefix.startswith((b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff')):
        encoding = 'utf-32'
    elif prefix.startswith((b'\xff\xfe', b'\xfe\xff')):
        encoding = 'utf-16'
    else:
        encoding = 'utf-8-sig'
    try:
        with path.open(encoding=encoding, newline='') as source:
            return source.read(limit)
    except UnicodeDecodeError:
        with path.open(encoding='gb18030', newline='') as source:
            return source.read(limit)


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.ignored = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'template', 'noscript'):
            self.ignored.append(tag)
        if not self.ignored and tag == 'img':
            alt = dict(attrs).get('alt')
            if alt:
                self.parts.append(alt)

    def handle_endtag(self, tag):
        if self.ignored and self.ignored[-1] == tag:
            self.ignored.pop()

    def handle_data(self, data):
        if not self.ignored and data.strip():
            self.parts.append(' '.join(data.split()))


# 职责：提取 DOCX 或 PPTX 中可直接读取的段落文字。
# 输入：path 为服务器保存的附件，extension 为经过服务端校验的扩展名。
# 结果与约束：不解压到磁盘；限制条目与解压总量，按幻灯片顺序读取，仅输出有限文字。
def office_text(path, extension):
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 5000 or sum(item.file_size for item in infos) > 64 * 1024 * 1024:
            raise ValueError('archive limits')
        if any('vbaproject' in item.filename.lower() for item in infos):
            raise ValueError('macros not accepted')
        if extension == '.docx':
            names = ['word/document.xml']
        else:
            names = sorted((item.filename for item in infos if re.fullmatch(r'ppt/slides/slide\d+\.xml', item.filename)),
                           key=lambda name: int(re.search(r'(\d+)\.xml$', name)[1]))
        parts = []
        for name in names:
            info = archive.getinfo(name)
            if info.file_size > 5 * 1024 * 1024:
                raise ValueError('XML limits')
            node = ElementTree.fromstring(archive.read(name))
            parts.extend(''.join(child.itertext()) for child in node.iter() if child.tag.endswith('}t'))
            if sum(map(len, parts)) >= MAX_TEXT:
                break
        return '\n'.join(parts)[:MAX_TEXT]


# 职责：把 XLSX 工作表转为带工作表名和单元格坐标的有限文本。
# 输入：path 为通过结构与解压限制校验的普通 XLSX。
# 结果与约束：不执行公式或外部链接，只读缓存值；最多 20 表、5000 行和 30000 字符，空缓存公式明确标记。
def spreadsheet_text(path):
    with zipfile.ZipFile(path) as archive:
        infos=archive.infolist()
        if len(infos)>5000 or sum(info.file_size for info in infos)>64*1024*1024:
            raise ValueError('archive limits')
        if any('vbaproject' in item.filename.lower() for item in infos):
            raise ValueError('macros not accepted')
        ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        shared=[]
        if 'xl/sharedStrings.xml' in archive.namelist():
            shared=[''.join(node.itertext()) for node in ElementTree.fromstring(archive.read('xl/sharedStrings.xml')).findall('s:si',ns)]
        book=ElementTree.fromstring(archive.read('xl/workbook.xml'))
        sheets=book.findall('s:sheets/s:sheet',ns)
        rels={node.attrib['Id']:node.attrib['Target'] for node in ElementTree.fromstring(archive.read('xl/_rels/workbook.xml.rels'))}
        parts=[];size=0;rows=0
        for sheet in sheets[:20]:
            target=rels.get(sheet.attrib.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'),'')
            import posixpath
            member=posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/'+target)
            if not member.startswith('xl/') or member not in archive.namelist():continue
            parts.append('工作表：'+sheet.attrib.get('name','未命名'))
            for row in ElementTree.fromstring(archive.read(member)).findall('s:sheetData/s:row',ns):
                cells=[]
                for cell in list(row)[:50]:
                    raw=cell.findtext('s:v','',ns)
                    if cell.attrib.get('t')=='s':raw=shared[int(raw)] if raw and int(raw)<len(shared) else ''
                    elif cell.attrib.get('t')=='inlineStr':raw=''.join(cell.find('s:is',ns).itertext()) if cell.find('s:is',ns) is not None else ''
                    elif cell.find('s:f',ns) is not None and not raw:raw='[公式无缓存，未执行]'
                    if raw:cells.append(cell.attrib.get('r','')+'='+raw)
                line=' | '.join(cells);parts.append(line);size+=len(line);rows+=1
                if size>=MAX_TEXT or rows>=5000:return '\n'.join(parts)[:MAX_TEXT]
        return '\n'.join(parts)[:MAX_TEXT]


# 职责：读取受支持文档的文字，图片和旧式 Office 文件仅保留原件。
# 输入：path 为不可变附件路径，extension 决定解析器。
# 结果与约束：返回文字及状态；PDF 最多解析 1000 页及 100 万字符，扫描页最多三页本地 OCR，加密或未识别内容可由用户补充说明。
def extract(path, extension):
    if extension == '.avanim':
        from avatar.files.parsing.animation import parse_animation
        from avatar.files.uploads.buffer import upload_buffer
        with Path(path).open('rb') as original, upload_buffer(original) as data:
            scene = parse_animation(data)
        sections = [scene.title, scene.summary, ' '.join(scene.tags), scene.accessibility.alt]
        sections.extend('\n'.join((step.title, step.detail, *step.notes)) for step in scene.steps)
        return '\n\n'.join(filter(None, sections)), 'extracted'
    if extension in PLAIN_TEXT_EXTENSIONS:
        text=text_content(path,MAX_TEXT+1)
        if '\x00' in text:return '', 'needs_description'
    elif extension in ('.html', '.htm'):
        source = text_content(path, 1024 * 1024 + 1)
        parser = HTMLText()
        parser.feed(source[:1024 * 1024])
        parser.close()
        text = '\n'.join(parser.parts)
        if len(source) > 1024 * 1024:
            return text[:MAX_TEXT], 'truncated'
    elif extension in ('.doc','.docx'):
        from avatar.files.parsing.document_images import word_source
        from avatar.files.parsing.word import docx_text
        with word_source(path,extension) as source:
            return docx_text(source)
    elif extension == '.xlsx':
        text = spreadsheet_text(path)
    elif extension == '.pptx':
        text = office_text(path, extension)
    elif extension == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted:
            return '', 'needs_description'
        parts=[];ocr_pages=0;unread=0;index=-1
        from avatar.files.parsing.ocr import pdf_page_text
        size=0
        for index in range(min(len(reader.pages),PDF_MAX_PAGES)):
            try:text=reader.pages[index].extract_text() or ''
            except Exception:text=''
            if len(text.strip())<30:
                if ocr_pages<3:
                    ocr_pages+=1
                    try:recognized=pdf_page_text(path,index)
                    except Exception:recognized=''
                    if recognized:text+='\n[本页由 OCR 提取，可能有识别错误]\n'+recognized
                    else:unread+=1
                else:unread+=1
            parts.append(f'[第 {index+1} 页]\n'+text if text.strip() else '')
            size+=len(parts[-1])
            if size>=PDF_MAX_TEXT:break
        text='\n'.join(parts).strip()
        if not text:return '', 'needs_description'
        truncated=index+1<len(reader.pages) or len(text)>PDF_MAX_TEXT
        text=text[:PDF_MAX_TEXT]
        if truncated:text+='\n[解析达到资源上限：仅覆盖前 '+str(index+1)+' 页范围，末页可能截断，后续内容未读取。]'
        if unread:text+='\n[部分页面未提取到文字，请补充说明或用视觉模型查看原件。]'
        return text, 'ocr_partial' if unread else 'truncated' if truncated else 'ocr' if ocr_pages else 'extracted'
    elif extension in ('.png','.jpg','.jpeg','.webp','.gif','.bmp'):
        from avatar.files.parsing.ocr import image_text
        text=image_text(path)
        return text[:MAX_TEXT], 'ocr' if text else 'needs_description'
    else:
        return '', 'needs_description'
    text = text.strip()
    return text[:MAX_TEXT], 'truncated' if len(text) >= MAX_TEXT else 'extracted' if text else 'needs_description'


def native_plain(path):
    text = text_content(path, PDF_MAX_TEXT+1)
    if '\x00' in text:
        raise ValueError('文本文件含无效空字符')
    return {'text': text[:PDF_MAX_TEXT], 'status': 'truncated' if len(text)>PDF_MAX_TEXT else 'extracted'}


# 职责：在资源限制内执行一次附件解析并输出有限 JSON。
# 输入：命令行仅包含服务端生成的本地路径与已校验扩展名。
# 结果与约束：CPU 最多 30 秒、地址空间最多 512 MiB；失败不输出内部异常、文件内容日志或服务凭据。
def main():
    import resource
    memory=4*1024**3 if sys.argv[2] in ('.doc','.docx') else 512*1024*1024
    resource.setrlimit(resource.RLIMIT_AS, (memory,memory))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    try:
        path=Path(sys.argv[1]);extension=sys.argv[2]
        if '--native-only' in sys.argv and extension in PLAIN_TEXT_EXTENSIONS:
            print(json.dumps(native_plain(path),ensure_ascii=False));return
        if '--native-only' in sys.argv and extension=='.pdf':
            from pypdf import PdfReader
            reader=PdfReader(path,strict=False);parts=[];missing=[];size=0;truncated=False
            for index,page in enumerate(reader.pages):
                if index>=PDF_MAX_PAGES or size>=PDF_MAX_TEXT:truncated=True;break
                body=page.extract_text() or '';size+=len(body)
                if body.strip():parts.append(f'[第 {index+1} 页]\n'+body)
                else:missing.append(index+1)
            print(json.dumps({'text':'\n'.join(parts)[:PDF_MAX_TEXT],'status':'truncated' if truncated else 'partial' if missing else 'extracted','empty_pages':missing,'page_count':len(reader.pages)},ensure_ascii=False));return
        if '--native-only' in sys.argv and extension in ('.png','.jpg','.jpeg','.webp','.gif','.bmp'):
            text,status='','needs_description'
        else:text,status=extract(path,extension)
    except Exception as error:
        if sys.argv[2] in ('.doc','.docx'):
            print(json.dumps({'text':'','status':'needs_description','error':str(error) if isinstance(error,ValueError) else 'Word 解析失败（'+type(error).__name__+'）'},ensure_ascii=False));return
        text, status = '', 'needs_description'
    print(json.dumps({'text': text, 'status': status}, ensure_ascii=False))


if __name__ == '__main__':
    main()
