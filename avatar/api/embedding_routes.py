import asyncio
from fastapi import APIRouter,Depends,Request
from avatar.api.knowledge_routes import owner
from avatar.models.embedding import EmbeddingInput,EmbeddingSettings
from avatar.rag.invalidation import index_status

router=APIRouter(prefix='/api/me/embedding-settings',tags=['嵌入模型'])

@router.get('')
def get(request:Request,user=Depends(owner)):
    service = EmbeddingSettings(request.app.state.service.models)
    return index_status(service.database, user['id'], service.get(user['id']))

@router.put('')
async def save(body:EmbeddingInput,request:Request,user=Depends(owner)):
    service=EmbeddingSettings(request.app.state.service.models)
    def prepare():
        with service.database.connect() as db:
            config=service.resolve(db,user,body)
            row=db.execute('SELECT updated_at FROM embedding_settings WHERE user_id=?',(user['id'],)).fetchone()
            if body.updated_at!=(row[0] if row else None):
                from fastapi import HTTPException
                raise HTTPException(409,'嵌入配置已变化，请刷新后保存')
        return config
    config=await asyncio.to_thread(prepare)
    from avatar.models.model_settings import verify_api
    await verify_api(request.app.state.chat.model_client,config,service.vault,embedding=True)
    saved=await asyncio.to_thread(service.save,user,body,validated=config)
    from avatar.models.capabilities import detect
    asyncio.create_task(detect(service.database,user['id'],config,request.app.state.chat.model_client,service.vault,on_change=service.models.on_change),name='avatar-embedding-capability-detect')
    return await asyncio.to_thread(index_status, service.database, user['id'], saved)

@router.post('/options')
async def options(body:EmbeddingInput,request:Request,user=Depends(owner)):
    from avatar.models.discovery import discover
    service=EmbeddingSettings(request.app.state.service.models)
    def prepare():
        with service.database.connect() as db:return service.resolve(db,user,body)
    config=await asyncio.to_thread(prepare)
    result=await discover(request.app.state.chat.model_client,config,service.vault.decrypt(config['encrypted_key']))
    from avatar.models.capabilities import remember
    await asyncio.to_thread(remember,service.database,user['id'],config,result['models'],on_change=service.models.on_change)
    result['models']=[item for item in result['models'] if item['category'] in ('embedding','unknown')]
    return result
