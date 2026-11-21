"""明确的服务端能力证据优先；未知能力允许用户纠正。"""
import asyncio
import json
from urllib.parse import urlsplit
from avatar.core.db import dump,now
from avatar.models.artifact_cache import fingerprint


def target(config):
    return fingerprint({key:config.get(key,'').rstrip('/') if key=='base_url' else config.get(key,'') for key in ('provider','api_format','base_url')})


def remember(database,user_id,config,rows):
    signature=target(config)
    with database.connect(write=True) as db:
        db.executemany('INSERT OR REPLACE INTO model_artifact_cache VALUES(?,?,?,?,?,?,?)',
            [(user_id,'capabilities',signature,fingerprint(row['id']),'',dump(row),now()) for row in rows])
        db.execute("DELETE FROM model_artifact_cache WHERE rowid IN (SELECT rowid FROM model_artifact_cache WHERE user_id=? AND kind='capabilities' ORDER BY updated_at DESC LIMIT -1 OFFSET 4000)",(user_id,))
        refresh_saved(db,user_id)


def automatic(db,user_id,config,*,embedding=False):
    if not config:return config
    row=db.execute("SELECT value FROM model_artifact_cache WHERE user_id=? AND kind='capabilities' AND signature=? AND digest=?",
                   (user_id,target(config),fingerprint(config.get('model','')))).fetchone()
    info=json.loads(row[0]) if row else {}
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


def refresh_saved(db,user_id):
    from avatar.rag.invalidation import invalidate
    for table,column,embedding in (('model_settings','model_config',False),('embedding_settings','config',True)):
        row=db.execute(f'SELECT {column} FROM {table} WHERE user_id=?',(user_id,)).fetchone()
        if not row:continue
        previous=json.loads(row[0]);config=automatic(db,user_id,previous,embedding=embedding)
        keys=('multimodal_enabled',) if embedding else ('vision_enabled','audio_enabled')
        if config!=previous:db.execute(f'UPDATE {table} SET {column}=? WHERE user_id=?',(dump(config),user_id))
        if any(bool(config.get(k))!=bool(previous.get(k)) for k in keys):invalidate(db,user_id,force=embedding)


async def detect(database,user_id,config,client,vault):
    if not config.get('encrypted_key'):return
    from avatar.models.discovery import discover
    from fastapi import HTTPException
    try:
        result=await discover(client,config,vault.decrypt(config['encrypted_key']))
    except (HTTPException,AttributeError):return
    await asyncio.to_thread(remember,database,user_id,config,result['models'])


async def refresh_all(database,client,vault):
    def list_configs():
        with database.connect() as db:return db.execute('SELECT user_id,model_config FROM model_settings UNION ALL SELECT user_id,config FROM embedding_settings').fetchall()
    rows=await asyncio.to_thread(list_configs)
    semaphore=asyncio.Semaphore(2)
    async def one(row):
        async with semaphore:await detect(database,row[0],json.loads(row[1]),client,vault)
    await asyncio.gather(*(one(row) for row in rows))
