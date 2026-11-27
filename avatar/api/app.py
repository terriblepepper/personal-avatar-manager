"""Create the web application for the platform CLI and WIT host."""
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from avatar.api.frontend import mount_frontend, mount_ops
from avatar.api.middleware import SecurityMiddleware, invalid_request
from avatar.api.router import router
from avatar.bootstrap.container import configure
from avatar.bootstrap.lifespan import lifespan
from avatar.core.config import ROOT, Settings, application_version
from avatar.core.http_metrics import ObserveHTTP
from avatar.core.performance import RouteTimings


# 职责：组装 FastAPI Web 边界和 wit 3.0 智能体运行时。
# 输入：settings 可覆盖环境配置，provider 和 model_client 可注入测试实现，run_worker 控制是否启动任务轮询。
# 结果与约束：Web、系统 mTLS 和可选系统 ATR 分别装配；系统自动注册须显式启用，个人分身正式派生由配置选择的队列注册器处理；小镇仍禁止分身代答。
def create_app(settings=None, provider=None, run_worker=True, model_client=None):
    settings = settings or Settings.from_env()
    app = FastAPI(
        title="个人分身智能体管理系统",
        version=application_version(),
        lifespan=lifespan,
        docs_url="/api/docs" if settings.enable_api_docs else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.enable_api_docs else None,
    )
    configure(app.state, settings, provider, run_worker, model_client)
    app.add_exception_handler(RequestValidationError, invalid_request)
    app.add_middleware(SecurityMiddleware, public_url=settings.oidc_public_url)
    app.include_router(router)
    mount_ops(app, ROOT / 'admin-frontend' / 'dist')
    mount_frontend(app, ROOT / 'frontend' / 'dist')
    app.add_middleware(RouteTimings)
    app.add_middleware(ObserveHTTP, metrics=app.state.monitor.http)
    return app
