"""Administrator resource budgets; never claims an unconfirmed OS limit."""
import asyncio
from fastapi import APIRouter,Depends,Request,HTTPException
from avatar.api.routes import admin
from avatar.core.execution import finish_on_cancel
from avatar.core.resource_budget import ResourcePolicy
from avatar.core.resource_limits import ServiceLimits

router=APIRouter(prefix='/api/admin/rag-resources',tags=['资源预算'])


@router.get('')
def status(request:Request,user=Depends(admin)):
    from avatar.core.performance import snapshot
    state = request.app.state
    return {**state.rag_resources.status(), 'performance': snapshot(),
            'rag_cache': state.chat.rag.store.cache_stats(), 'dispatch': dict(state.changes.health)}


@router.put('')
async def save(body:ResourcePolicy,request:Request,user=Depends(admin)):
    controller=request.app.state.rag_resources
    def persist():
        with request.app.state.database.connect(write=True) as db:
            db.execute("INSERT INTO settings(key,value) VALUES('rag_resource_policy',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(body.model_dump_json(),))
    async def apply_and_save(sample):
        old=controller.policy;backend=ServiceLimits();applied=None
        if body.enforce_systemd or old.enforce_systemd:
            try:applied=await asyncio.to_thread(backend.apply,body,sample)
            except HTTPException as error:
                if getattr(error,'system_limit_changed',False):
                    controller.enforcement={'mode':'unconfirmed','error':str(error.detail)}
                    controller.update(await asyncio.to_thread(controller.sampler))
                raise
        try:
            await asyncio.to_thread(persist)
        except Exception:
            if applied:
                try:controller.enforcement=await asyncio.to_thread(backend.apply,old,sample)
                except HTTPException:controller.enforcement={'mode':'unconfirmed','error':'配置未保存，系统限制回滚结果未确认'}
            raise
        controller.policy=body;controller.update(sample)
        if applied:controller.enforcement=applied
    async with controller.settings_lock:
        sample=await asyncio.to_thread(controller.sampler)
        await finish_on_cancel(apply_and_save(sample))
    return controller.status()
