# 模块说明：从用户指定模型平台读取可用目录，保留能力来源；不使用生成调用探测，不把名称推测当作已验证视觉能力。
import asyncio
import json
import re
import httpx
from fastapi import HTTPException
from avatar.models.diagnostics import check_response, provider_error

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


# 职责：沿用模型客户端的公网 DNS 校验、固定 IP、原 Host/SNI 及显式代理，不允许重定向携带 Key。
# 输入：config/Key 来自本人输入或同一目标的已保存配置，client 为应用统一模型客户端。
# 结果与约束：GET 目录总超时 20 秒、最多十页/2000 项/4 MiB，不记录凭据或生成计费请求。
async def discover(client,config,key):
    fmt=config['api_format'];base=config['base_url'].rstrip('/')
    url=httpx.URL(base+'/models')
    if url.userinfo or url.query or url.fragment:raise HTTPException(422,'模型目录地址不能含凭据、查询参数或片段')
    client.validate_endpoint(url)
    headers={'Host':url.netloc.decode()}
    if fmt=='anthropic':headers.update({'x-api-key':key,'anthropic-version':'2023-06-01'})
    elif fmt=='gemini':headers['x-goog-api-key']=key
    else:headers['Authorization']='Bearer '+key
    items={};params={};seen=set();total=0;truncated=False
    try:
        async with asyncio.timeout(20), client.session(url, httpx.Timeout(15, connect=8)) as (addresses, session):
            for page in range(10):
                async with session.stream('GET',url.copy_with(host=addresses[0]),params=params,headers=headers,extensions={'sni_hostname':url.host}) as response:
                    if response.status_code in (404,405):raise HTTPException(422,'该地址未提供模型目录接口，请确认 Base URL 包含正确版本路径；可继续手动填写模型 ID')
                    await check_response(response,key)
                    payload=bytearray()
                    async for chunk in response.aiter_bytes():
                        total+=len(chunk)
                        if total>4*1024*1024:raise HTTPException(502,'模型目录超过 4 MiB 限制')
                        payload.extend(chunk)
                data=json.loads(payload)
                if not isinstance(data,dict):raise ValueError('目录必须为对象')
                if data.get('error'):raise provider_error(200,data['error'],None,key)
                rows=data.get('models' if fmt=='gemini' else 'data')
                if not isinstance(rows,list):raise ValueError('模型列表字段缺失')
                for row in rows:
                    if isinstance(row,dict):
                        item=describe(row)
                        if item:items[item['id']]=item
                    if len(items)>=2000:truncated=True;break
                cursor=data.get('nextPageToken') if fmt=='gemini' else data.get('last_id') if data.get('has_more') else None
                if not cursor or truncated:break
                if not isinstance(cursor,str) or len(cursor)>2000 or cursor in seen:truncated=True;break
                seen.add(cursor);params={'pageToken' if fmt=='gemini' else 'after_id' if fmt=='anthropic' else 'after':cursor}
            else:truncated=True
    except (TimeoutError,httpx.TimeoutException):raise HTTPException(504,'获取模型目录超时，请稍后刷新；未发起回答生成') from None
    except httpx.ConnectError as error:
        from avatar.models.diagnostics import connection_error
        raise connection_error(error,'模型目录') from None
    except httpx.HTTPError:raise HTTPException(502,'无法连接模型目录，请检查 Base URL 和服务器出网网络') from None
    except (ValueError,TypeError,AttributeError):raise HTTPException(502,'模型目录格式无法识别，可手动填写模型 ID') from None
    from avatar.models.metadata import enrich
    await enrich(client,config,items)
    return {'models':sorted(items.values(),key=lambda item:(item['category'],item['id'])),'truncated':truncated,
            'notice':'已补充可确认的窗口、输出、维度及输入能力，选择模型后自动填入，未公布参数保留手工设置。类型以接口元数据为准；名称推测不启用多模态；能力自动判定，未确认时按文本处理。目录可见不保证该 Key 具备实际调用权限。'+('目录已截断，请手动输入未显示的模型。' if truncated else '')}
