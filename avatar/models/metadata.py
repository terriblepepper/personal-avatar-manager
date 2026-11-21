"""目录参数补全：API 优先，官方公开目录次之，未知值不猜测；不向公共来源转发 Key。"""
import asyncio
import json
import os
import re
import time
import httpx
from fastapi import HTTPException
from avatar.models.limits import limits

SILICON_MODELS=os.getenv('AVATAR_SILICONFLOW_CATALOG_URL', 'https://www.siliconflow.cn/models')
EMBEDDINGS={
    'BAAI/bge-m3':(8192,1024,False),
    'BAAI/bge-large-zh-v1.5':(512,1024,False),
    'BAAI/bge-large-en-v1.5':(512,1024,False),
    'netease-youdao/bce-embedding-base_v1':(512,768,False),
    'Qwen/Qwen3-Embedding-0.6B':(32768,1024,False),
    'Qwen/Qwen3-Embedding-4B':(32768,2560,False),
    'Qwen/Qwen3-Embedding-8B':(32768,4096,False),
    'Qwen/Qwen3-VL-Embedding-8B':(32768,4096,True),
}
_catalog=(0,{})


def parse_silicon_catalog(html):
    decoder=json.JSONDecoder();parts=[]
    for match in re.finditer(r'self\.__next_f\.push\(',html):
        try:value,_=decoder.raw_decode(html,match.end())
        except ValueError:continue
        if isinstance(value,list) and len(value)>1 and isinstance(value[1],str):parts.append(value[1])
    rows={};text=''.join(parts)
    for match in re.finditer(r'\{"modelId":',text):
        try:row,_=decoder.raw_decode(text,match.start())
        except ValueError:continue
        name=row.get('modelName');context=row.get('contextLen')
        if not isinstance(name,str) or not re.fullmatch(r'[a-zA-Z0-9_./:@-]{1,160}',name):continue
        if not isinstance(context,int) or isinstance(context,bool) or not 128<=context<=10000000:continue
        item={'context_tokens':context}
        if row.get('subType')=='chat':
            item.update(category='multimodal' if row.get('vlm') is True else 'text',vision=row.get('vlm') is True,
                input_modalities=['text','image'] if row.get('vlm') is True else ['text'])
        elif row.get('subType')=='embedding':item['category']='embedding'
        rows[name]=item
        if len(rows)>=2000:break
    return rows


async def silicon_catalog(client):
    global _catalog
    if _catalog[0]>time.monotonic():return _catalog[1]
    try:
        async with asyncio.timeout(10):
            url=httpx.URL(SILICON_MODELS)
            async with client.session(url, httpx.Timeout(8, connect=6)) as (addresses, session):
                async with session.stream('GET',url.copy_with(host=addresses[0]),headers={'Host':url.host},extensions={'sni_hostname':url.host}) as response:
                    if response.status_code!=200:return {}
                    raw=bytearray()
                    async for part in response.aiter_bytes():
                        raw.extend(part)
                        if len(raw)>4*1024*1024:return {}
        rows=parse_silicon_catalog(raw.decode())
        if rows:_catalog=(time.monotonic()+3600,rows)
        return rows
    except (HTTPException,httpx.HTTPError,TimeoutError,ValueError):return {}


async def enrich(client,config,items):
    from avatar.models.discovery import LABELS
    official=config.get('provider')=='siliconflow' and httpx.URL(config['base_url']).host=='api.siliconflow.cn'
    catalog=await silicon_catalog(client) if official else {}
    for identifier,item in items.items():
        sources=item['parameter_sources']
        for field in ('context_tokens','output_tokens','dimensions'):
            if item.get(field) is not None:sources[field]='api'
        published=catalog.get(identifier,{})
        for field in ('context_tokens','vision','input_modalities'):
            if field=='input_modalities' and item.get('vision') is False and 'image' in published.get(field,[]):continue
            if (item.get(field) is None or field=='input_modalities' and not item[field]) and field in published:
                item[field]=published[field];sources[field]='official_catalog'
        if item['source']!='api' and published.get('category'):
            item.update(category=published['category'],category_label=LABELS[published['category']],source='official_catalog')
        known=limits(config.get('provider'),identifier) or {}
        for field,value in known.items():
            if item.get(field) is None:item[field]=value;sources[field]='official_model'
        spec=EMBEDDINGS.get(identifier.removeprefix('Pro/')) if official else None
        if spec:
            if item['source']!='api':item.update(category='embedding',source='official_model')
            for field,value in (('context_tokens',spec[0]),('dimensions',spec[1])):
                if item.get(field) is None:item[field]=value;sources[field]='official_model'
            if item['embedding_input'] is None:item['embedding_input']='multimodal' if spec[2] else 'text';item['embedding_source']='official_model'
        if item['category']=='embedding':item['category_label']='多模态嵌入模型' if item['embedding_input']=='multimodal' else '文本嵌入模型' if item['embedding_input']=='text' else LABELS['embedding']
        item['chat_compatible']=item['category'] not in ('embedding','rerank','image','audio','video','other')
