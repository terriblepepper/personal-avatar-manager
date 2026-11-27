"""账户独立嵌入配置；同目标显式复用聊天 Key，网络与聊天使用相同防护。"""
import asyncio
import json
import math
import httpx
from avatar.models.model_network import pinned_request_url
from fastapi import HTTPException
from avatar.models.diagnostics import contextual, transport_failure, check_response
from pydantic import BaseModel,ConfigDict,Field,SecretStr,field_validator
from typing import Literal
from avatar.models.schemas import ModelConfigInput
from avatar.core.db import dump,event,now
from avatar.models.capabilities import automatic
from avatar.models.model_config import provider_base_url
from avatar.core.revisions import next_modified_at
from avatar.core.tokens import estimate


class EmbeddingInput(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    provider:Literal['openai','qwen','glm','siliconflow','openrouter','custom']='siliconflow'
    base_url:str=Field(default='',max_length=500)
    model:str=Field(default='BAAI/bge-m3',min_length=1,max_length=160,pattern=r'^[a-zA-Z0-9_./:@-]+$')
    api_key:SecretStr|None=None
    dimensions:int=Field(default=1024,ge=64,le=4096,strict=True)
    context_tokens:int|None=Field(default=None,ge=512,le=1000000,strict=True)
    context_enabled:bool=Field(default=True,strict=True)
    multimodal_enabled:bool=Field(default=False,strict=True)
    use_chat_key:bool=Field(default=False,strict=True)
    updated_at:str|None=Field(default=None,max_length=80)

    @field_validator('base_url')
    @classmethod
    def endpoint(cls,value):return ModelConfigInput.valid_endpoint(value)

    @field_validator('api_key')
    @classmethod
    def key(cls,value):return ModelConfigInput.valid_key(value)


class EmbeddingSettings:
    def __init__(self,models):self.models=models;self.database=models.database;self.vault=models.vault

    @staticmethod
    def current(db,user_id):
        row=db.execute('SELECT config FROM embedding_settings WHERE user_id=?',(user_id,)).fetchone()
        return automatic(db,user_id,json.loads(row[0]),embedding=True) if row else {}

    @staticmethod
    def configured(db,user_id):
        row = db.execute("SELECT NULLIF(config::jsonb->>'encrypted_key', '') IS NOT NULL FROM embedding_settings WHERE user_id=?", (user_id,)).fetchone()
        return bool(row and row[0])

    def get(self,user_id):
        with self.database.connect() as db:
            row=db.execute('SELECT config,updated_at FROM embedding_settings WHERE user_id=?',(user_id,)).fetchone()
            config=automatic(db,user_id,json.loads(row[0]),embedding=True) if row else {}
        return {'config':{k:v for k,v in config.items() if k not in ('encrypted_key','input_modalities')}|{'key_configured':bool(config.get('encrypted_key'))},'updated_at':row[1] if row else None}

    def resolve(self,db,user,body):
        old=self.current(db,user['id'])
        config={'provider':body.provider,'api_format':'openai','base_url':provider_base_url(body.provider,body.base_url),'model':body.model,'dimensions':body.dimensions,
            'context_tokens':body.context_tokens or embedding_window({'model':body.model}),'context_enabled':body.context_enabled,'multimodal_enabled':body.multimodal_enabled}
        if body.provider=='siliconflow' and 'Qwen3' in body.model and body.context_enabled and config['context_tokens']>32768:raise HTTPException(422,'所选 Qwen3 嵌入模型上下文上限为 32768 Token（32K）')
        if not config['base_url']:raise HTTPException(422,'请填写嵌入 API 根地址')
        if 'multimodal_enabled' not in body.model_fields_set and all(config[k]==old.get(k) for k in ('provider','base_url','model')):
            config['multimodal_enabled']=old.get('multimodal_enabled',False)
        key=body.api_key.get_secret_value() if body.api_key else ''
        if key:config['encrypted_key']=self.vault.encrypt(key)
        elif body.use_chat_key:
            chat=self.models.current(db,user['id'])
            if any(config[k]!=chat.get(k) for k in ('provider','api_format','base_url')) or not chat.get('encrypted_key'):
                raise HTTPException(422,'只能明确复用同平台、同格式、同地址的已保存聊天 Key')
            config['encrypted_key']=chat['encrypted_key']
        else:config['encrypted_key']=old.get('encrypted_key','') if all(config[k]==old.get(k) for k in ('provider','api_format','base_url')) else ''
        if not config['encrypted_key']:raise HTTPException(422,'请填写嵌入 Key 或选择复用同地址的聊天 Key')
        return automatic(db,user['id'],config,embedding=True)

    def save(self,user,body,*,validated=None):
        with self.database.connect(write=True, account_scope=user['id']) as db:
            previous=db.execute('SELECT config,updated_at FROM embedding_settings WHERE user_id=?',(user['id'],)).fetchone()
            if body.updated_at!=(previous[1] if previous else None):raise HTTPException(409,'嵌入配置已变化，请刷新后保存')
            config=validated if validated is not None else self.resolve(db,user,body)
            stamp=next_modified_at(previous[1] if previous else '')
            db.execute('INSERT INTO embedding_settings VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET config=excluded.config,updated_at=excluded.updated_at',(user['id'],dump(config),stamp))
            self.models.on_change(db, user['id'], json.loads(previous[0]) if previous else {}, config,
                                  embedding=True, embedding_key_changed=bool(body.api_key) or previous is None)
            event(db,user['id'],user['username'],'保存嵌入模型配置',actor_id=user['id'])
        return self.get(user['id'])


def index_identity(config):
    return {key:config.get(key) for key in
            ('provider','base_url','model','dimensions','context_tokens','context_enabled','multimodal_enabled')}


def embedding_window(config):
    if config.get('context_enabled',True) and config.get('context_tokens'):return config['context_tokens']
    name=config.get('model','')
    return 32768 if 'Qwen3' in name else 512 if 'large-zh' in name or 'large-en' in name or 'bce-embedding' in name else 8192


def require_embedding(config):
    if not config or not config.get('encrypted_key'):raise HTTPException(422,'请先在模型配置中保存必需的嵌入模型，才能构建、学习或检索文件')


async def embed(client,config,key,texts,*,retry_uncertain=True):
    for attempt in range(4):
        try:return await _embed_once(client,config,key,texts)
        except HTTPException as error:
            if not getattr(error,'retryable',False) or not retry_uncertain and not getattr(error,'request_not_sent',False):raise
            if attempt==3:
                error.retryable=False
                error.index_retries_exhausted=True
                detail=str(error.detail)
                for phrase in ('。系统未自动重试','，未自动重试','；未重发 HTTP 请求'):
                    detail=detail.replace(phrase,'')
                error.detail='嵌入服务在 3 次自动重试后仍失败；'+detail
                raise
            await asyncio.sleep((2,5,10)[attempt])


async def _embed_once(client,config,key,texts):
    if not isinstance(texts,list) or not 1<=len(texts)<=32:raise HTTPException(422,'嵌入批量必须为 1–32 项')
    for item in texts:
        if isinstance(item,str):
            if not item.strip() or estimate(item)+32>embedding_window(config):raise HTTPException(422,'嵌入输入超过配置的上下文窗口，请缩短分片或调整窗口')
        elif isinstance(item,dict) and set(item)=={'image'} and config.get('multimodal_enabled'):
            if not isinstance(item['image'],str) or not item['image'].startswith('data:image/') or len(item['image'])>1500000:raise HTTPException(422,'只允许已授权、本地限尺寸的图片嵌入输入')
            if embedding_window(config)<4096:raise HTTPException(422,'图片嵌入至少需要 4096 Token 的输入窗口')
        else:raise HTTPException(422,'模型多模态开关未开启或嵌入输入格式不支持')
    url=httpx.URL(config['base_url'].rstrip('/')+'/embeddings')
    if url.userinfo or url.query or url.fragment:raise HTTPException(422,'嵌入模型地址不能含凭据、查询参数或片段')
    client.validate_endpoint(url)
    try:
        async with asyncio.timeout(120),client.session(url, 90) as (addresses, session):
            async with session.stream('POST',pinned_request_url(url, addresses),headers={'Host':url.netloc.decode(),'Authorization':'Bearer '+key},json={'model':config['model'],'input':texts,'encoding_format':'float'},extensions={'sni_hostname':url.host}) as response:
                await check_response(response,key)
                payload=bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload)>8*1024*1024:raise HTTPException(502,'嵌入响应超过大小限制')
        data=json.loads(payload);items=data['data']
        if sorted(item['index'] for item in items)!=list(range(len(texts))):raise ValueError()
        vectors=[item['embedding'] for item in sorted(items,key=lambda i:i['index'])];dim=config['dimensions']
        for vector in vectors:
            if not isinstance(vector,list):raise HTTPException(502,'嵌入模型未返回数值向量')
            if len(vector)<dim:raise HTTPException(502,f'模型返回 {len(vector)} 维向量，小于配置的 {dim} 维，请调整嵌入维度')
            if any(type(x) not in (int,float) or not math.isfinite(x) for x in vector):raise HTTPException(502,'嵌入向量包含无效数值')
        vectors=[v[:dim] for v in vectors]
        return vectors,data.get('usage') or {}
    except HTTPException as error:
        raise contextual(error,{**config,'_call_purpose':'file_embedding'}) from None
    except (httpx.HTTPError, TimeoutError) as error:
        raise contextual(transport_failure(error,'嵌入服务'),{**config,'_call_purpose':'file_embedding'}) from None
    except (ValueError,KeyError,TypeError):raise HTTPException(502,'嵌入模型响应无效或实际向量维度小于配置') from None
