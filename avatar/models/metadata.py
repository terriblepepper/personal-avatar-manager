"""目录参数补全：API 优先，官方公开目录次之，未知值不猜测；不向公共来源转发 Key。"""
import asyncio
import json
import os
import re
import time
import httpx
from avatar.models.model_network import pinned_request_url
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


def dimension(row):
    for field in ('dimensions','embedding_dimensions','embedding_dimension','dimension','output_dimension'):
        value=row.get(field)
        if isinstance(value,int) and not isinstance(value,bool) and 64<=value<=4096:return value
    return None


LABELS={'multimodal':'多模态模型','text':'文本模型','embedding':'向量模型','rerank':'重排模型','image':'图像生成模型','audio':'音频模型','video':'视频模型','other':'其他模型','unknown':'类型待确认'}


# 职责：归一化平台能力元数据；类型推测明确标识，只有 API 明示图片输入时才确认视觉能力。
# 结果与约束：未知能力保持未知；非对话类型不能误作文本聊天模型，展示字段均有长度限制。
def describe(row):
    identifier=row.get('id') or row.get('name','')
    if not row.get('id') and isinstance(identifier,str):identifier=identifier.removeprefix('models/')
    if not isinstance(identifier,str) or not re.fullmatch(r'[a-zA-Z0-9_./:@-]{1,160}',identifier):return None
    architecture=row.get('architecture') if isinstance(row.get('architecture'),dict) else {}
    capabilities=row.get('capabilities') if isinstance(row.get('capabilities'),dict) else {}
    # 职责：只接受平台布尔值或 supported 布尔字段，不把字符串 false 当作真。
    def supported(name):
        value=capabilities.get(name)
        return value.get('supported') if isinstance(value,dict) else value if isinstance(value,bool) else None
    incoming=row.get('input_modalities',architecture.get('input_modalities'))
    outgoing=row.get('output_modalities',architecture.get('output_modalities'))
    incoming=[v for v in incoming if isinstance(v,str)] if isinstance(incoming,list) else []
    outgoing=[v for v in outgoing if isinstance(v,str)] if isinstance(outgoing,list) else []
    modality=architecture.get('modality')
    if not incoming and not outgoing and isinstance(modality,str) and len(modality)<100 and '->' in modality:
        left,right=modality.split('->',1);incoming=left.split('+');outgoing=right.split('+')
    vision=True if 'image' in incoming or supported('image_input') is True or row.get('vision') is True else False if incoming or supported('image_input') is False or row.get('vision') is False else None
    audio=True if 'audio' in incoming or supported('audio_input') is True else False if incoming or supported('audio_input') is False else None
    kind=str(row.get('task') or row.get('type') or '').lower();methods=row.get('supportedGenerationMethods',[])
    methods=methods if isinstance(methods,list) else []
    category='unknown';source='api'
    if 'embed' in kind or any('embed' in str(m).lower() for m in methods) and 'generateContent' not in methods:category='embedding'
    elif 'rerank' in kind:category='rerank'
    elif not kind and re.search(r'embed|bge-(?!rerank)|e5-',identifier,re.I):category='embedding';source='name_hint'
    elif kind in ('text-to-image','image-generation','image'):category='image'
    elif kind in ('text-to-video','video-generation','video'):category='video'
    elif kind in ('text-to-speech','speech-to-text','audio'):category='audio'
    elif kind in ('image-to-text','vision','multimodal'):category='multimodal';vision=True if kind in ('image-to-text','vision') else vision
    elif kind in ('text','chat','text-generation'):category='text'
    elif outgoing and 'text' not in outgoing:category='video' if 'video' in outgoing else 'image' if 'image' in outgoing else 'audio' if 'audio' in outgoing else 'other'
    elif vision is True or any(v in incoming for v in ('audio','video','file')) or any(v in outgoing for v in ('image','audio','video')):category='multimodal'
    elif incoming==['text'] or vision is False:category='text'
    else:
        # shortcut: 普通 /models 常只有 ID；名称只作分组提示，不能据此开启图片输入。
        hints=[('rerank',r'rerank'),('embedding',r'embed|bge-|e5-'),('image',r'dall-e|stable-diffusion|flux[.-]|imagen'),('video',r'sora|veo[.-]'),('audio',r'whisper|tts|transcri|audio'),('multimodal',r'vision|(?:^|[-/])vl(?:[-/]|$)|llava|pixtral|gpt-4o|gpt-4\.1|claude-|gemini-'),('text',r'deepseek|moonshot-v1|qwen-(?:plus|turbo|max)|llama|gpt-3\.5')]
        category=next((name for name,pattern in hints if re.search(pattern,identifier,re.I)),'unknown')
        source='name_hint' if category!='unknown' else 'unknown'
    # 职责：仅传递合理整数预算，布尔值和异常元数据不能污染用户模型设置。
    def budget(value):return value if isinstance(value,int) and not isinstance(value,bool) and 128<=value<=10000000 else None
    top=row.get('top_provider') if isinstance(row.get('top_provider'),dict) else {}
    embedding_input=None;embedding_source='unknown';label=LABELS[category]
    if category=='embedding':
        if incoming or vision is not None:
            embedding_input='multimodal' if vision is True or any(v in incoming for v in ('image','audio','video')) else 'text'
            embedding_source='api'
        elif identifier=='Qwen/Qwen3-VL-Embedding-8B':embedding_input='multimodal';embedding_source='official_model'
        elif identifier in ('BAAI/bge-m3','Qwen/Qwen3-Embedding-4B','Qwen/Qwen3-Embedding-8B'):embedding_input='text';embedding_source='official_model'
        label={'multimodal':'多模态嵌入模型','text':'文本嵌入模型',None:'向量模型 · 输入待确认'}[embedding_input]
    return {'id':identifier,'name':str(row.get('display_name') or row.get('name') or identifier)[:160],
            'category':category,'category_label':label,'source':source,'vision':vision,'audio':audio,
            'embedding_input':embedding_input,'embedding_source':embedding_source,
            'chat_compatible':category not in ('embedding','rerank','image','audio','video','other'),
            'input_modalities':incoming[:10],'output_modalities':outgoing[:10],
            'context_tokens':budget(row.get('context_tokens') or row.get('context_length') or row.get('contextLen') or row.get('inputTokenLimit') or row.get('max_input_tokens') or row.get('max_model_len')),
            'output_tokens':budget(row.get('output_tokens') or row.get('max_output_tokens') or row.get('outputTokenLimit') or row.get('max_tokens') or top.get('max_completion_tokens')),
            'dimensions':dimension(row),
            'parameter_sources':{}}


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
                async with session.stream('GET',pinned_request_url(url, addresses),headers={'Host':url.netloc.decode()},extensions={'sni_hostname':url.host}) as response:
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
