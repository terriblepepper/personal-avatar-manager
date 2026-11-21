"""Word 原文转换与抽取；隔离进程保留解压、宏和资源限制。"""
import zipfile


def docx_html(path):
    import mammoth
    with zipfile.ZipFile(path) as archive:
        entries=archive.infolist()
        if len(entries)>5000 or sum(item.file_size for item in entries)>64*1024*1024:raise ValueError('DOCX 解压规模超限')
        if any('vbaproject' in item.filename.lower() for item in entries):raise ValueError('DOCX 不接受宏')
        if archive.getinfo('word/document.xml').file_size>5*1024*1024:raise ValueError('DOCX XML 超限')
    with path.open('rb') as stream:
        result=mammoth.convert_to_html(stream,convert_image=mammoth.images.img_element(lambda image:{'src':'','alt':'[文档内嵌图片，另行解析]'}))
    if len(result.value)>4*1024*1024:raise ValueError('DOCX 转换结果超限')
    return result.value

def docx_text(path):
    from bs4 import BeautifulSoup
    text=BeautifulSoup(docx_html(path),'html.parser').get_text('\n',strip=True)
    return text[:1000000],'truncated' if len(text)>1000000 else 'docx_html'

def main():
    import json,resource,sys
    from pathlib import Path
    resource.setrlimit(resource.RLIMIT_AS,(4*1024**3,4*1024**3))
    resource.setrlimit(resource.RLIMIT_CPU,(30,30))
    from avatar.files.parsing.document_images import word_source
    extension=sys.argv[2] if len(sys.argv)>2 else '.docx'
    # LibreOffice children need more virtual address space than the text parser.
    # Limits are set before conversion so uploaded documents remain bounded.
    with word_source(Path(sys.argv[1]),extension) as source:
        html=docx_html(source)
    from bs4 import BeautifulSoup
    text=BeautifulSoup(html,'html.parser').get_text('\n',strip=True)
    print(json.dumps({'text':text[:1000000],'html':html,'status':'truncated' if len(text)>1000000 else 'docx_html'}))


if __name__=='__main__':main()
