"""Own worker tasks and close their dependencies after they have stopped."""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager

from avatar.core.performance import record_duration
from avatar.core.resource_limits import ServiceLimits
from avatar.files.parsing import text_worker
from avatar.models.capabilities import refresh_all


async def sample_loop_lag():
    loop = asyncio.get_running_loop()
    while True:
        due = loop.time() + .25
        await asyncio.sleep(.25)
        record_duration('event_loop_lag', max(0., loop.time() - due))


async def stop_tasks(tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def enforce_resources(resources):
    try:
        return ServiceLimits().apply(resources.policy, resources.sampler())
    except Exception:
        return {'mode': 'unavailable', 'error': '系统硬限制未确认，仅使用应用预算；请管理员检查'}


# 职责：将 FastAPI 生命周期连接到 wit 3.0 组件容器。
# 输入：app 为 HTTP 应用，runtime 为当前应用独立的智能体运行时。
# 结果与约束：系统注册异步等待审核，不阻塞 Web；退出先取消协商和维护任务，取消 ATR 防止重新启用 AIP，再关闭协议和 Web。
@asynccontextmanager
async def lifespan(app):
    state = app.state
    settings = state.settings
    town = state.personal_aip.tasks
    tasks = []

    def start(coroutine, name):
        tasks.append(asyncio.create_task(coroutine, name=name))

    async with AsyncExitStack() as stack:
        stack.push_async_callback(asyncio.to_thread, state.database.close)
        stack.push_async_callback(asyncio.to_thread, state.chat.rag.store.close)
        stack.push_async_callback(asyncio.to_thread, text_worker.close)
        stack.push_async_callback(asyncio.to_thread, state.pending_reviews.close)
        for service in (state.chat.model_client, state.oidc, town.authorization.oidc,
                        town.public_authorization.oidc):
            if (pool := getattr(service, 'http_pool', None)) is not None:
                await stack.enter_async_context(pool.lifespan())
        stack.push_async_callback(state.capability_probes.close)
        await state.runtime.start()
        stack.push_async_callback(state.runtime.stop)
        stack.push_async_callback(state.system_aip.stop)
        stack.push_async_callback(state.system_registration.stop)
        if state.runtime.run_worker and settings.town_public_enabled and not settings.entity_aip_enabled:
            stack.push_async_callback(town.stop)
        stack.push_async_callback(stop_tasks, tasks)

        start(sample_loop_lag(), 'avatar-event-loop-lag')
        start(state.monitor.run(), 'avatar-platform-monitor')
        if settings.redis_socket or settings.kafka_bootstrap:
            start(state.changes.run(), 'avatar-commit-dispatch')
        if settings.system_atr_enabled:
            await state.system_registration.start()
        else:
            await state.system_aip.start()
        if state.rag_resources.policy.enforce_systemd:
            state.rag_resources.enforcement = await asyncio.to_thread(enforce_resources, state.rag_resources)
        if state.runtime.run_worker:
            start(state.platform_certificate.run(), 'platform-certificate-maintenance')
            start(state.rag_resources.run(), 'avatar-resource-budget')
            start(state.generations.run(), 'avatar-chat-generations')
            start(refresh_all(state.database, state.chat.model_client, state.service.vault,
                              on_change=state.service.models.on_change), 'avatar-model-capabilities')
            start(state.soul_builds.run(), 'avatar-soul-builds')
            start(state.chat.rag.run(), 'avatar-file-rag')
            start(state.dialogues.recovery.run(), 'avatar-dialogues')
            start(state.entity_lifecycle.run(), 'avatar-identity-maintenance')
            start(town.maintenance(), 'avatar-maintenance')
            if settings.entity_aip_enabled or settings.town_public_enabled:
                await asyncio.to_thread(town.files.clear_scratch)
                start(town.files.janitor(), 'avatar-town-files')
            if settings.entity_aip_enabled:
                start(state.personal_aip.run(), 'avatar-personal-aip')
            elif settings.town_public_enabled:
                await asyncio.to_thread(town.recover)
        yield
