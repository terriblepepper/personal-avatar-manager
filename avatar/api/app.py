# 模块说明：应用入口：组装 wit 3.0 组件与 HTTP 边界，管理智能体生命周期，装配本人技能、分身协商后台、安全中间件、API 和 Web 静态入口。
from contextlib import AsyncExitStack, asynccontextmanager
import asyncio
import re
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from avatar.core.text_budget import TEXT_BYTES
from avatar.core.config import ROOT, Settings, application_version, require_database_url
from avatar.runtime.wit_runtime import AvatarRuntime
from avatar.files.library.attachments import MAX_STORED_SIZE
from avatar.files.parsing.animation import MAX_ENVELOPE_BYTES
from avatar.knowledge.media import MAX_ASSET
from avatar.api.routes import router
from avatar.profiles.pending_reviews import PendingReviews
from avatar.api.review_routes import router as review_router
from avatar.api.diagram_routes import router as diagram_router
from avatar.accounts.oidc import OidcService
from avatar.accounts.identity_migration import IdentityMigrationService
from avatar.api.migration_routes import router as migration_router
from avatar.api.oidc_routes import router as oidc_router
from avatar.api.system_routes import router as system_router
from avatar.runtime.system_aip import SystemAipRuntime
from avatar.runtime.system_registration import SystemRegistrationRuntime
from avatar.runtime.avatar_ontology_registration import (
    AvatarOntologyRegistrationRuntime,
)
from avatar.profiles.entity_lifecycle import EntityLifecycleService
from avatar.runtime.personal_aip import PersonalAipRuntime
from avatar.api.entity_routes import router as entity_router
from avatar.api.collaboration_routes import router as collaboration_router
from avatar.conversation.collaboration.dialogues import AgentDialogues
from avatar.skills.packages import MAX_UPLOAD


# 职责：组装 FastAPI Web 边界和 wit 3.0 智能体运行时。
# 输入：settings 可覆盖环境配置，provider 和 model_client 可注入测试实现，run_worker 控制是否启动任务轮询。
# 结果与约束：Web、系统 mTLS 和可选系统 ATR 分别装配；系统自动注册须显式启用，个人分身正式派生由配置选择的队列注册器处理；小镇仍禁止分身代答。
def create_app(settings=None, provider=None, run_worker=True, model_client=None):
    settings = settings or Settings.from_env()
    require_database_url(settings.database_url)
    from avatar.protocols.identity.document import migrate_identity_documents

    migrate_identity_documents(settings.data_dir)
    runtime = AvatarRuntime(settings, provider, run_worker, model_client)
    resources = runtime.services
    from avatar.conversation.jobs.runner import GenerationRunner
    generations=GenerationRunner(runtime,resources.database)
    pending_reviews = PendingReviews(resources.database, settings.redis_socket)
    rag_resources=resources.chat.rag.resources
    from avatar.knowledge.service import KnowledgeService

    knowledge = KnowledgeService(resources.database, resources.chat)
    resources.profiles.knowledge = knowledge
    system_aip = SystemAipRuntime(settings, resources.database)
    system_registration = SystemRegistrationRuntime(settings, system_aip)
    avatar_registration = AvatarOntologyRegistrationRuntime(settings)
    from avatar.runtime.platform_certificate import PlatformCertificate
    platform_certificate = PlatformCertificate(resources.database, settings)
    entity_lifecycle = EntityLifecycleService(resources.database, settings)
    personal_aip = PersonalAipRuntime(settings, resources.chat)
    dialogues = AgentDialogues(resources.database, resources.chat)
    resources.chat.dialogues = dialogues
    from avatar.protocols.transport.peer import PeerTransport
    from avatar.protocols.tasks.dialogues import PeerDialogueTasks

    dialogues.transport = PeerTransport(personal_aip, dialogues)
    personal_aip.tasks.peer_dialogues = PeerDialogueTasks(dialogues, settings)

    async def sample_loop_lag():
        from avatar.core.performance import record_duration
        loop = asyncio.get_running_loop()
        while True:
            due = loop.time() + .25
            await asyncio.sleep(.25)
            record_duration('event_loop_lag', max(0., loop.time() - due))

    # 职责：将 FastAPI 生命周期连接到 wit 3.0 组件容器。
    # 输入：app 为 HTTP 应用，runtime 为当前应用独立的智能体运行时。
    # 结果与约束：系统注册异步等待审核，不阻塞 Web；退出先取消协商和维护任务，取消 ATR 防止重新启用 AIP，再关闭协议和 Web。
    @asynccontextmanager
    async def runtime_lifespan(app):
        try:
            await runtime.start()
        except BaseException:
            resources.database.close()
            raise
        tasks = [asyncio.create_task(sample_loop_lag(),name="avatar-event-loop-lag")]
        try:
            if settings.system_atr_enabled:
                await system_registration.start()
            else:
                await system_aip.start()
            await avatar_registration.start()
            if rag_resources.policy.enforce_systemd:
                from avatar.core.resource_limits import ServiceLimits
                try:rag_resources.enforcement=await asyncio.to_thread(ServiceLimits().apply,rag_resources.policy,rag_resources.sampler())
                except Exception:rag_resources.enforcement={'mode':'unavailable','error':'系统硬限制未确认，仅使用应用预算；请管理员检查'}
            if run_worker:
                tasks.append(asyncio.create_task(platform_certificate.run(), name='platform-certificate-maintenance'))
                tasks.append(asyncio.create_task(rag_resources.run(),name='avatar-resource-budget'))

                tasks.append(
                    asyncio.create_task(
                        generations.run(),
                        name="avatar-chat-generations",
                    )
                )
                from avatar.models.capabilities import refresh_all

                tasks.append(
                    asyncio.create_task(
                        refresh_all(
                            resources.database,
                            resources.chat.model_client,
                            resources.profiles.vault,
                        ),
                        name="avatar-model-capabilities",
                    )
                )
                tasks.append(
                    asyncio.create_task(knowledge.run(), name="avatar-soul-builds")
                )
                tasks.append(
                    asyncio.create_task(
                        resources.chat.rag.run(), name="avatar-file-rag"
                    )
                )
                tasks.append(
                    asyncio.create_task(dialogues.run(), name="avatar-dialogues")
                )
                tasks.append(
                    asyncio.create_task(
                        entity_lifecycle.run(), name="avatar-identity-maintenance"
                    )
                )
                if settings.entity_aip_enabled or settings.town_public_enabled:
                    await asyncio.to_thread(personal_aip.tasks.files.clear_scratch)
                    tasks.append(asyncio.create_task(personal_aip.tasks.files.janitor(),name='avatar-town-files'))
                if settings.entity_aip_enabled:
                    tasks.append(
                        asyncio.create_task(
                            personal_aip.run(), name="avatar-personal-aip"
                        )
                    )
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await avatar_registration.stop()
            await system_registration.stop()
            await system_aip.stop()
            await runtime.stop()
            pending_reviews.close()
            from avatar.files.parsing import text_worker
            try:
                await asyncio.to_thread(text_worker.close)
            finally:
                resources.chat.rag.store.close()
                resources.database.close()

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            upstreams = (resources.chat.model_client, app.state.oidc,
                         personal_aip.tasks.authorization.oidc, personal_aip.tasks.public_authorization.oidc)
            for service in upstreams:
                pool = getattr(service, 'http_pool', None)
                if pool is not None:
                    await stack.enter_async_context(pool.lifespan())
            async with runtime_lifespan(app):
                yield

    app = FastAPI(
        title="个人分身智能体管理系统",
        version=application_version(),
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.animation_requests = asyncio.Semaphore(2)
    app.state.pending_reviews = pending_reviews
    app.state.settings, app.state.database = settings, resources.database
    from avatar.core.config import runtime_environment
    app.state.environment = runtime_environment(settings, resources.provider)
    app.state.generations=generations
    app.state.runtime = runtime
    app.state.rag_resources=rag_resources
    app.state.system_aip = system_aip
    app.state.platform_certificate = platform_certificate
    app.state.system_registration = system_registration
    app.state.avatar_registration = avatar_registration
    app.state.entity_lifecycle, app.state.personal_aip = entity_lifecycle, personal_aip
    app.state.knowledge = knowledge
    app.state.dialogues = dialogues
    app.state.oidc = OidcService(resources.database, settings)
    app.state.identity_migration = IdentityMigrationService(
        resources.database, settings
    )
    # HTTP 管理界面与 wit Entry 共用领域对象；兼容测试注入模型客户端，不建立第二套数据或认证。
    app.state.service, app.state.worker, app.state.chat = (
        resources.profiles,
        resources.worker,
        resources.chat,
    )

    # 职责：生成不泄露请求凭据的输入校验错误响应。
    # 输入：request 为当前请求，exc 为 Pydantic/FastAPI 校验异常。
    # 结果与约束：只返回字段位置、消息与类型，剔除可能包含 API Key 的输入正文，HTTP 状态为 422。
    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # 原始校验错误可能包含整个请求正文及 API Key，响应只保留安全的字段描述。
        return JSONResponse(
            {
                "detail": [
                    {"loc": e["loc"], "msg": e["msg"], "type": e["type"]}
                    for e in exc.errors()
                ]
            },
            status_code=422,
        )

    # 职责：执行请求同源、大小限制及响应安全头处理。
    # 输入：request 为 HTTP 请求，call_next 为后续处理器。
    # 结果与约束：写请求检查 Origin 与声明长度，响应添加防嵌入、缓存及 CSP 头；临时入口页禁止 Referrer，认证 CSRF 由 current_user 处理。
    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            # 浏览器写请求只接受同源 Origin；认证 API 还须通过会话 CSRF 校验。
            origin = request.headers.get("origin")
            from urllib.parse import urlsplit

            public = urlsplit(settings.oidc_public_url)
            allowed_origins = {
                str(request.base_url).rstrip("/"),
                public.scheme + "://" + public.netloc,
            }
            if origin and origin not in allowed_origins:
                return JSONResponse({"detail": "禁止跨站请求"}, status_code=403)
            try:
                size = int(request.headers.get("content-length", "0") or "0")
            except ValueError:
                return JSONResponse({"detail": "请求长度无效"}, status_code=400)
            limit = (
                MAX_UPLOAD + 65536
                if request.url.path == "/api/me/skills"
                else MAX_STORED_SIZE
                if request.url.path == "/api/me/attachments"
                else 256 * 1024
                if request.url.path == "/api/me/profile"
                else 65536
            )
            if request.url.path.startswith(
                "/api/me/knowledge/builds/"
            ) and request.url.path.endswith("/questionnaire"):
                limit = 1024 * 1024
            if request.url.path.startswith(
                "/api/me/knowledge/builds/"
            ) and request.url.path.endswith("/refine"):
                limit = 256 * 1024
            if request.url.path == "/api/admin/soul-question-bank/import":
                limit = 8 * 1024 * 1024
            if request.url.path == "/api/me/knowledge/assets":
                limit = MAX_ASSET + 65536
            if request.url.path.startswith(
                "/api/conversations/"
            ) and request.url.path.endswith("/diagrams"):
                limit = 10 * 1024 * 1024
            elif request.url.path.startswith(
                "/api/conversations/"
            ) and request.url.path.endswith("/diagrams/lookup"):
                limit = 400000
            if request.url.path.startswith("/api/conversations/") and request.url.path.endswith(
                ("/animations/lookup", "/animations/promote")
            ):
                # The JSON envelope may escape bytes in the animation source.
                limit = MAX_ENVELOPE_BYTES
            if re.fullmatch(r"/api/(?:chat(?:/stream|/generations)?|agent-dialogues(?:/[^/]+/(?:resume|confirm|accept))?|"
                            r"agent-sessions/[^/]+/(?:messages|accept|task-draft)|agent-intents/[^/]+/confirm|"
                            r"town/avatars/[^/]+/tasks)", request.url.path):
                # A JSON escape can take six wire bytes for one byte of decoded text.
                limit = 7 * TEXT_BYTES
                if request.url.path.startswith("/api/agent-intents/"):
                    limit += 12 * 60000  # Optional legacy shared_content, up to 60k Unicode characters.
            if request.method == "POST" and re.fullmatch(r"/api/conversations/[^/]+/files", request.url.path):
                # The multipart endpoint spools originals and validates each file type.
                limit = None
            if limit is not None and size > limit:
                return JSONResponse({"detail": "请求内容过大"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path == "/api/auth/oidc/callback":
            response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if request.url.path in (
            "/",
            "/admin",
            "/platform-entry",
        ) or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        if request.url.path in ("/", "/admin", "/platform-entry"):
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self'; font-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            )
        if request.url.path in {"/static/preview/mermaid.html", "/static/preview/animation.html"}:
            # 仅受信静态渲染页允许同源嵌入；iframe 自身不带 allow-same-origin，拥有不透明 origin。
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = (
                "sandbox allow-scripts; default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; connect-src 'none'; object-src 'none'; frame-src 'none'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'"
            )
        if request.url.path == "/static/preview/html.html":
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = (
                "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline' data:; worker-src 'none'; "
                "style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data: blob:; connect-src 'none'; "
                "frame-ancestors 'self'; base-uri 'none'; form-action 'none'"
            )
        return response

    app.include_router(migration_router)
    from avatar.api.town_routes import router as town_router

    app.include_router(town_router)
    app.include_router(oidc_router)
    app.include_router(router)
    from avatar.api.knowledge_routes import router as knowledge_router

    app.include_router(knowledge_router)
    from avatar.api.style_learning_routes import router as learning_router
    from avatar.api.embedding_routes import router as embedding_router

    app.include_router(learning_router)
    app.include_router(embedding_router)
    from avatar.api.rag_routes import router as rag_router

    app.include_router(rag_router)
    from avatar.api.resource_routes import router as resource_router
    app.include_router(resource_router)
    from avatar.api.question_bank_routes import router as question_bank_router

    app.include_router(question_bank_router)
    app.include_router(review_router)
    app.include_router(diagram_router)
    app.include_router(system_router)
    app.include_router(entity_router)
    app.include_router(collaboration_router)
    from avatar.api.chat_file_routes import router as chat_files_router

    app.include_router(chat_files_router)
    from avatar.api.frontend import mount_frontend
    mount_frontend(app, ROOT / 'frontend' / 'dist')

    from avatar.core.performance import RouteTimings
    app.add_middleware(RouteTimings)
    return app
