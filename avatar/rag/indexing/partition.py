"""离线结构化分片，按页和标题保留父子上下文。"""
import re


def text_elements(text,page=None):
    from unstructured.documents.elements import Title,NarrativeText,ElementMetadata
    blocks=re.split(r'\n\s*\n|(?=^#{1,6}\s)|(?<=\n)(?=(?:Abstract|摘要|References|参考文献|\d+(?:\.\d+)*\s+[A-Z\u4e00-\u9fff]))',text,flags=re.M)
    elements=[]
    for block in blocks:
        block=block.strip()
        if not block:continue
        lines=block.splitlines()
        for index,line in enumerate(lines):
            heading=len(line)<160 and re.match(r'^(?:#{1,6}\s|(?:Abstract|摘要|References|参考文献|Introduction|Conclusion)\s*$|\d+(?:\.\d+)*[.、\s]+\S)',line,re.I)
            if heading:
                before='\n'.join(lines[:index]).strip()
                if before:elements.append(NarrativeText(before,metadata=ElementMetadata(page_number=page)))
                elements.append(Title(line.lstrip('# ').strip(),metadata=ElementMetadata(page_number=page)))
                tail='\n'.join(lines[index+1:]).strip()
                if tail:elements.append(NarrativeText(tail,metadata=ElementMetadata(page_number=page)))
                break
        else:elements.append(NarrativeText(block,metadata=ElementMetadata(page_number=page)))
    return elements


def markdown_elements(text):
    from unstructured.documents.elements import Title,NarrativeText
    elements=[];body=[];fence=''
    def flush():
        value='\n'.join(body).strip('\n')
        if value.strip():elements.append(NarrativeText(value))
        body.clear()
    for line in text.splitlines():
        marker=re.match(r'^\s{0,3}(`{3,}|~{3,})',line)
        if marker:
            if not fence:fence=marker[1]
            elif marker[1][0]==fence[0] and len(marker[1])>=len(fence):fence=''
            body.append(line);continue
        if not fence and re.match(r'^#{1,6}\s+\S',line):
            flush();elements.append(Title(line.lstrip('# ').rstrip()))
        else:body.append(line)
    flush();return elements


def partition(extracted,window):
    from unstructured.chunking.title import chunk_by_title
    from unstructured.chunking.basic import chunk_elements
    from unstructured.documents.elements import Title
    if extracted.get('format')=='.md':elements=markdown_elements(extracted['text'])
    else:
        parts=re.split(r'\[第 (\d+) 页\]\n',extracted['text'])
        pages=[(int(parts[i]),parts[i+1]) for i in range(1,len(parts)-1,2)] if len(parts)>1 else [(None,extracted['text'])]
        elements=[element for page,body in pages for element in text_elements(body,page)]
    width=max(128,min(1000,window-128));parent_width=max(width,min(6000,width*6))
    parents=chunk_by_title(elements,max_characters=parent_width,new_after_n_chars=max(width,parent_width-500),combine_text_under_n_chars=0,multipage_sections=False)
    result=[];section=''
    for parent_index,parent in enumerate(parents):
        originals=parent.metadata.orig_elements or [parent]
        section=next((item.text for item in originals if isinstance(item,Title)),section)
        children=chunk_elements(originals,max_characters=width,new_after_n_chars=max(128,width-200),overlap=min(100,width//8),include_orig_elements=True)
        for index,child in enumerate(children):
            neighbors=children[max(0,index-1):index+2]
            context='\n\n'.join(item.text for item in neighbors)
            result.append({'page':child.metadata.page_number,'text':child.text,'modality':'text',
                           'section':section[:200],'parent':str(parent_index),'context':context,'parent_text':parent.text})
    return result
