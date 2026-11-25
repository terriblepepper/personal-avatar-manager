import asyncio
from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi import Query
from fastapi import HTTPException
from typing import Literal
from avatar.models.model_settings import ModelSettingsRequest
from avatar.rag.invalidation import index_status
import avatar.accounts.auth as auth
from avatar.models.model_config import catalog
from avatar.models.schemas import ModelConfigInput


router = APIRouter()


# 职责：返回模型平台及模型建议列表。
# 输入：user 作为已登录依赖，不需要调用上游。
# 结果与约束：返回目录默认值与自定义平台选项，不承诺当前账户具有全部模型权限。
@router.get("/model-providers")
def model_providers(user=Depends(auth.current_user)):
    return catalog()


@router.get("/model-providers/effort-levels")
def model_effort_levels(provider: str, api_format: Literal['openai','anthropic','gemini'],
                        model: str = Query(max_length=160), user=Depends(auth.current_user)):
    from avatar.models.effort import levels
    return levels({'provider': provider, 'api_format': api_format, 'model': model})


# 职责：读取本人独立模型配置。
# 输入：user 为认证账户，request 提供应用服务。
# 结果与约束：仅返回脱敏配置和更新时间，不返回 Key，不依赖资料审核状态。
@router.get('/me/model-settings')
def own_model_settings(request: Request, user=Depends(auth.current_user)):
    models = request.app.state.service.models
    return index_status(models.database, user['id'], models.get(user['id']))


# 职责：保存立即生效的本人模型配置。
# 输入：body 为模型设置请求，user 由认证及 CSRF 确定。
# 结果与约束：无需资料审核，后续问答及记忆压缩使用新配置，已有资料审核任务不会覆盖它。
@router.put('/me/model-settings')
async def save_model_settings(body: ModelSettingsRequest, request: Request, user=Depends(auth.current_user)):
    service=request.app.state.service
    def prepare():
        with service.database.connect() as db:
            row=db.execute('SELECT updated_at FROM model_settings WHERE user_id=?',(user['id'],)).fetchone()
            baseline=row[0] if row else None
            if body.updated_at is not None and body.updated_at!=baseline:raise HTTPException(409,'模型配置已变化，请刷新后重新保存')
            config=service.vault.resolve(body.model_config_input,service.models.current(db,user['id']))
        return baseline,config
    baseline,config=await asyncio.to_thread(prepare)
    from avatar.models.model_settings import verify_api
    await verify_api(request.app.state.chat.model_client,config,service.vault)
    saved=await asyncio.to_thread(service.models.save,user,body,validated=config,baseline=baseline)
    from avatar.models.capabilities import detect
    asyncio.create_task(detect(service.database,user['id'],config,request.app.state.chat.model_client,service.vault,on_change=service.models.on_change),name='avatar-model-capability-detect')
    return await asyncio.to_thread(index_status, service.database, user['id'], saved)


# 职责：根据本人输入的 Base URL/Key 读取模型目录，不保存设置、不使用其他账户密钥。
# 结果与约束：只有平台、格式、地址与已存配置一致才沿用空白 Key；网络请求复用生成客户端防护。
@router.post('/me/model-options')
async def own_model_options(body: ModelConfigInput, request: Request, user=Depends(auth.current_user)):
    from fastapi import HTTPException
    from avatar.models.discovery import discover
    service=request.app.state.service
    def prepare():
        with service.database.connect() as db:previous=service.models.current(db,user['id'])
        return service.vault.resolve(body,previous)
    config=await asyncio.to_thread(prepare)
    if not config.get('encrypted_key'):raise HTTPException(422,'请填写此 Base URL 对应的 API Key 后获取模型')
    result=await discover(request.app.state.chat.model_client,config,service.vault.decrypt(config['encrypted_key']))
    from avatar.models.capabilities import remember
    await asyncio.to_thread(remember,service.database,user['id'],config,result['models'],on_change=service.models.on_change)
    return result
