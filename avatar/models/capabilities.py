"""明确的服务端能力证据优先；未知能力允许用户纠正。"""
import asyncio
import json
import logging
from urllib.parse import urlsplit
from fastapi import HTTPException

from avatar.core.db import dump,now
from avatar.core.execution import run_blocking
from avatar.models.artifact_cache import UPSERT, fingerprint, prune
from avatar.models.discovery import discover

log = logging.getLogger(__name__)


def target(config):
    return fingerprint({key:config.get(key,'').rstrip('/') if key=='base_url' else config.get(key,'') for key in ('provider','api_format','base_url')})


def remember(database,user_id,config,rows,*,on_change):
    signature=target(config)
    with database.connect(write=True, account_scope=user_id) as db:
        with db.raw.cursor() as cursor:
            cursor.executemany(UPSERT, (
                (user_id,'capabilities',signature,fingerprint(row['id']),'',dump(row).encode(),now())
                for row in rows))
        db.execute("""DELETE FROM model_artifact_cache c USING (
            SELECT signature,digest FROM model_artifact_cache WHERE user_id=? AND kind='capabilities'
            ORDER BY updated_at DESC,signature,digest OFFSET 4000) stale
            WHERE c.user_id=? AND c.kind='capabilities' AND (c.signature,c.digest)=(stale.signature,stale.digest)""",
            (user_id,user_id))
        prune(db,user_id)
        refresh_saved(db,user_id,on_change)


def automatic(db,user_id,config,*,embedding=False):
    if not config:return config
    row=db.execute("SELECT value FROM model_artifact_cache WHERE user_id=? AND kind='capabilities' AND signature=? AND digest=?",
                   (user_id,target(config),fingerprint(config.get('model','')))).fetchone()
    return apply_capabilities(config, json.loads(row[0]) if row else {}, embedding=embedding)


def apply_capabilities(config, info, *, embedding=False):
    if not info and config.get('capability_status')=='confirmed' and config.get('capability_source') in ('api','official_model','official_catalog'):
        # Server-only snapshots survive bounded cache eviction; request schemas never accept these fields.
        info={'vision':config.get('multimodal_enabled') if embedding else config.get('vision_enabled'),'audio':config.get('audio_enabled',False),'input_modalities':config.get('input_modalities',[]),'source':config['capability_source'],'category':'embedding' if embedding else 'text'}
    if not info and config.get('provider')=='siliconflow' and urlsplit(config.get('base_url','')).hostname=='api.siliconflow.cn':
        from avatar.models.metadata import EMBEDDINGS
        spec=EMBEDDINGS.get(config.get('model','').removeprefix('Pro/'))
        if spec:info={'vision':spec[2],'audio':False,'input_modalities':['text','image'] if spec[2] else ['text'],'source':'official_model','category':'embedding','chat_compatible':False}
    modalities=info.get('input_modalities',[])
    vision=info.get('vision') is True or 'image' in modalities or embedding and not modalities and info.get('embedding_input')=='multimodal' and info.get('embedding_source') in ('api','official_model')
    usable=info.get('category') in (None,'unknown','embedding') if embedding else info.get('chat_compatible',True)
    vision=vision and usable
    audio=usable and (info.get('audio') is True or 'audio' in modalities) and config.get('api_format') in ('openai','gemini')
    known=bool(modalities) or isinstance(info.get('vision'),bool) or isinstance(info.get('audio'),bool) or embedding and info.get('embedding_source')=='official_model'
    source=info.get('embedding_source') if embedding and info.get('embedding_source')=='official_model' else info.get('source','unknown')
    if not known:
        vision=usable and bool(config.get('multimodal_enabled') if embedding else config.get('vision_enabled'))
        audio=usable and bool(config.get('audio_enabled')) and config.get('api_format') in ('openai','gemini')
        source='user' if vision or audio else 'unknown'
        modalities=['text']+(['image'] if vision else [])+(['audio'] if audio else [])
    result={**config,'capability_status':'confirmed' if known else 'unknown','capability_source':source,'input_modalities':modalities}
    if embedding:result['multimodal_enabled']=vision
    else:result.update(vision_enabled=vision,audio_enabled=audio)
    return result


def refresh_saved(db,user_id,on_change):
    for table,column,embedding in (('model_settings','model_config',False),('embedding_settings','config',True)):
        row=db.execute(f'SELECT {column} FROM {table} WHERE user_id=?',(user_id,)).fetchone()
        if not row:continue
        previous=json.loads(row[0]);config=automatic(db,user_id,previous,embedding=embedding)
        if config!=previous:db.execute(f'UPDATE {table} SET {column}=? WHERE user_id=?',(dump(config),user_id))
        if not embedding or bool(config.get('multimodal_enabled')) != bool(previous.get('multimodal_enabled')):
            on_change(db,user_id,previous,config,embedding=embedding)


async def detect(database,user_id,config,client,vault,*,on_change):
    if not config.get('encrypted_key'):return
    try:
        result=await discover(client,config,vault.decrypt(config['encrypted_key']))
    except HTTPException as error:
        log.info('Capability discovery unavailable (HTTP %s)', error.status_code)
        return
    await run_blocking(remember,database,user_id,config,result['models'],on_change=on_change)


async def refresh_all(database,client,vault,*,on_change):
    def list_configs():
        with database.connect() as db:return db.execute('SELECT user_id,model_config FROM model_settings UNION ALL SELECT user_id,config FROM embedding_settings').fetchall()
    rows=await run_blocking(list_configs)
    semaphore=asyncio.Semaphore(2)
    async def one(row):
        async with semaphore:
            try:
                await detect(database,row[0],json.loads(row[1]),client,vault,on_change=on_change)
            except Exception:
                log.exception('Saved model capability refresh failed')
    await asyncio.gather(*(one(row) for row in rows))
