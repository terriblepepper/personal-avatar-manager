"""在当轮授权目录中合并路径、模糊名称和已发布正文命中。"""
from datetime import date
from difflib import get_close_matches
import re
import unicodedata

from avatar.rag.query import directory_catalog, file_catalog
from avatar.rag.stores.tokenization import tokens
from avatar.files.parsing.animation import is_animation_file


def folded(text):
    return unicodedata.normalize('NFKC',text or '').casefold()


def terms(text):
    words = list(dict.fromkeys(word for word in tokens(text) if len(word)>1 or len(text)==1))
    # Bound per-file fuzzy comparisons; exact matching and hybrid retrieval keep the full query.
    if len(words) > 256:
        return [words[i * (len(words)-1) // 255] for i in range(256)]
    return words


_DATE=re.compile(r'(?<!\d)((?:19|20)\d{2})[-./年_]?([01]?\d)[-./月_]?([0-3]?\d)(?:日)?(?!\d)')


def document_date(text):
    for match in _DATE.finditer(text):
        try:
            return date(*map(int,match.groups())).isoformat()
        except ValueError:
            continue
    return ''


class MaterialDiscovery:
    def __init__(self, documents, retrieve, excluded=()):
        self.documents=[row for row in documents if row['id'] not in excluded]
        self.catalog={item['file_id']:item for item in file_catalog(self.documents)}
        self.directories=directory_catalog(self.documents)
        self.retrieve=retrieve
        self.last_key=None
        self.last_rows=[]

    async def page(self, request, *, animation_only=False):
        query=folded(request.query.strip())
        folder=folded(request.folder.strip('/'))
        prefix=folder+'/' if folder else ''
        directories=[item for item in self.directories if folded(item['path']).startswith(prefix)]
        if request.mode=='directories':
            matches=[item for item in directories if not query or query in folded(item['path']+' '+item['description'])]
            return {'items':[], 'directories':matches[request.offset:request.offset+10],
                    'next_offset':request.offset+10 if len(matches)>request.offset+10 else None}
        key=(query,folder,animation_only,request.sort)
        if key!=self.last_key:
            scoped=[row for row in self.documents if folded(row.get('relative_path') or row['filename']).startswith(prefix)
                    and (not animation_only or is_animation_file(row))]
            self.last_rows=await self.rank(scoped,request.query.strip(),latest=request.sort=='latest')
            self.last_key=key
        rows=self.last_rows
        return {'items':rows[request.offset:request.offset+10],
                'directories':directories[:10],
                'next_offset':request.offset+10 if len(rows)>request.offset+10 else None}

    async def rank(self, documents, question, *, latest):
        query=folded(question)
        words=terms(query)
        ascii_words=[word for word in words if len(word)>=4 and word.isascii()]
        hits={}
        for row in documents:
            item=self.catalog[row['id']]
            name=folded(item['filename'])
            path=folded(item['relative_path'])
            exact=bool(query and query in (name,path,row['id'],name.rsplit('.',1)[0]))
            named=bool(query and (name in query or path in query))
            meta=folded(' '.join([path,item['description'],item.get('title',''),*item.get('tags',())]))
            overlap=sum(1 for word in words if word in meta)
            fuzzy=0
            if query and not (exact or named) and ascii_words:
                parts=re.findall(r'[a-z0-9]+',path)
                fuzzy=sum(bool(get_close_matches(word,parts,n=1,cutoff=.72)) for word in ascii_words)
            name_hits=sum(word in name for word in words)
            hits[row['id']]=[exact,named,0,overlap,fuzzy,'',name_hits]
        if query and documents and not any(hit[0] for hit in hits.values()):
            evidence,_=await self.retrieve(documents,question,published_only=True)
            for rank,doc in enumerate(evidence):
                hit=hits.get(doc.metadata.get('file_id'))
                if hit is not None and not hit[2]:
                    hit[2]=len(evidence)-rank
                    hit[5]=doc.page_content[:320]
        # Dates in a title/path refer to the work. Upload time alone cannot establish its publication date.
        dates={}
        if latest:
            for identifier,hit in hits.items():
                item=self.catalog[identifier]
                text=item['description']+' '+hit[5]
                publication=re.search(r'(?:发布日期|发布时间|发表于|发布于|published(?: on)?)\s*[:：]?\s*('+_DATE.pattern+r')',text,re.I)
                if publication:
                    dates[identifier]=(document_date(publication[1]),'正文或说明标注')
                else:
                    dates[identifier]=(document_date(item['relative_path']),'文件名或目录')
        result=[]
        def priority(entry):
            exact,named,rag,metadata,fuzzy,_,filename=entry[1]
            if latest:
                item=self.catalog[entry[0]]
                relevant=(not query or query in folded(item['filename']+' '+item['description']+' '+entry[1][5]))
                return exact,named,relevant,dates[entry[0]][0] if relevant else '',bool(filename and rag),filename,rag or fuzzy,metadata
            return exact,named,bool(filename and rag),max(filename,metadata),rag or fuzzy,filename
        for identifier,hit in sorted(hits.items(),key=priority,reverse=True):
            if query and not any(hit[:5]):
                continue
            item=self.catalog[identifier]
            result.append({'file_id':identifier,'filename':item['filename'],'relative_path':item['relative_path'],
                           'description':item['description'],'content_type':item['content_type'],'exact':hit[0],
                           **({'title':item['title'],'tags':item['tags']} if item.get('title') else {}),
                           **({'document_date':dates[identifier][0],'date_source':dates[identifier][1]} if latest and dates[identifier][0] else {}),
                           **({'evidence':hit[5]} if hit[5] else {})})
        return result
