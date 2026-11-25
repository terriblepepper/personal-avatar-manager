"""按页和字符位置读取授权原件；子进程资源有界，不依赖 RAG 命中范围。"""
import json
import re
from pathlib import Path
from avatar.files.parsing import PLAIN_TEXT_EXTENSIONS


def read(path,extension,start=1,end=None,offset=0,limit=12000,query='',visual_text=None):
    from avatar.files.parsing.extract import extract
    if extension in ('.doc', '.docx'):
        from avatar.files.word_pdfs import read_word_pdf
        pdf, _ = read_word_pdf(Path(path))
        return read(pdf, '.pdf', start, end, offset, limit, query, visual_text)
    if extension=='.pdf':
        from pypdf import PdfReader
        reader=PdfReader(path,strict=False)
        if reader.is_encrypted:raise ValueError('PDF 已加密，无法读取正文')
        total=len(reader.pages);end=min(end or total,total)
        if start>total:raise ValueError('页码超出文档范围')
        pages=((index+1,lambda index=index:(reader.pages[index].extract_text() or '')+((visual_text or {}).get(str(index+1),''))) for index in range(start-1,end))
    else:
        if extension in PLAIN_TEXT_EXTENSIONS:
            from avatar.files.parsing.extract import text_content
            body=text_content(path)
            status='extracted'
        else:body,status=extract(Path(path),extension)
        body+=(visual_text or {}).get('1','')
        total=1;end=1;pages=[(1,lambda:body)]
        if start!=1:raise ValueError('文本文件使用字符偏移，不支持此页码')
    parts=[];size=0;missing=[];continuation=None
    for page,load in pages:
        body=load();position=offset if page==start else 0
        if position>len(body):raise ValueError('字符偏移超出本页范围')
        if not body.strip():missing.append(page)
        if query:
            match=re.search(r'\s+'.join(re.escape(part) for part in query.split()),body[position:],re.I)
            if not match:continue
            position=max(position,position+match.start()-min(256,limit//4))
        take=min(len(body)-position,max(0,limit-size));fragment=body[position:position+take]
        parts.append({'page':page,'offset':position,'text':fragment});size+=take
        if position+take<len(body):continuation={'start':page,'offset':position+take};break
        if query:
            if page<end:continuation={'start':page+1,'offset':0}
            break
        if size>=limit and page<end:continuation={'start':page+1,'offset':0};break
    return {'pages':parts,'total_pages':total,'requested_end':end,'next':continuation,'empty_pages':missing,
            'coverage':'native_text' if extension=='.pdf' else status,'complete':continuation is None and (extension=='.pdf' or status!='truncated'),
            'parse_truncated':extension!='.pdf' and status=='truncated',
            **({'query':query,'found':bool(parts)} if query else {})}


def main():
    import resource,sys
    memory=4*1024**3 if sys.argv[2] in ('.doc','.docx') else 512*1024*1024
    resource.setrlimit(resource.RLIMIT_AS,(memory,memory))
    resource.setrlimit(resource.RLIMIT_CPU,(30,30))
    try:result=read(Path(sys.argv[1]),sys.argv[2],**json.loads(sys.stdin.read() if len(sys.argv)<4 else sys.argv[3]))
    except Exception as error:result={'error':str(error) if isinstance(error,ValueError) else '原件解析失败（'+type(error).__name__+'）'}
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
