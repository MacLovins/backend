from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from leadradar_auth import auth_settings, create_auth_router
from sqlalchemy import text

from leadradar_core.db.session import engine, get_db_session
from leadradar_core.errors import ERROR_RESPONSES, register_exception_handlers
from leadradar_core.logging import setup_logging
from leadradar_core.security import OriginCheckMiddleware, configure_security, is_dev_env
from leadradar_core.settings import settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    logger = structlog.get_logger()

    if settings.ENV != "test":
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as e:
            logger.warning("database_ping_failed_on_startup", error=str(e))

        try:
            app.state.redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        except Exception as e:
            logger.warning("redis_connect_failed_on_startup", error=str(e))

        from leadradar_core.worker.broker import broker

        await broker.startup()  # the API only enqueues tasks; the worker process executes them
        app.state.broker = broker

    yield

    if getattr(app.state, "broker", None):
        await app.state.broker.shutdown()

    if getattr(app.state, "redis", None):
        try:
            await app.state.redis.aclose()
        except Exception:
            pass

    await engine.dispose()


def create_app() -> FastAPI:
    configure_security(settings)  # fails fast outside dev without a real JWT secret
    docs_enabled = is_dev_env(settings.ENV)
    app = FastAPI(
        title="LeadRadar API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
        responses=ERROR_RESPONSES,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.PUBLIC_ORIGIN],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(OriginCheckMiddleware, app_settings=settings, cookie_name=auth_settings.COOKIE_NAME)

    register_exception_handlers(app)

    # Health endpoints
    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    async def health_ready(response: Response) -> dict[str, str]:
        health_status = {"status": "ready", "database": "ok", "redis": "ok"}
        is_ready = True

        # Check DB
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as e:
            health_status["database"] = f"error: {e!s}"
            is_ready = False

        # Check Redis
        try:
            redis_client = getattr(app.state, "redis", None)
            if redis_client:
                await redis_client.ping()
            else:
                r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
                await r.ping()
                await r.aclose()
        except Exception as e:
            health_status["redis"] = f"error: {e!s}"
            is_ready = False

        if not is_ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            health_status["status"] = "unavailable"

        return health_status

    # Include Routers under /api/v1
    from leadradar_core.modules.accounts.router import router as accounts_router
    from leadradar_core.modules.activity.router import router as activity_router
    from leadradar_core.modules.config.router import router as config_router
    from leadradar_core.modules.discovery.router import router as discovery_router
    from leadradar_core.modules.feedback.router import router as feedback_router
    from leadradar_core.modules.leads.router import router as leads_router
    from leadradar_core.modules.meta.router import router as meta_router
    from leadradar_core.modules.runs.router import router as runs_router

    app.include_router(create_auth_router(get_session=get_db_session), prefix="/api/v1")
    app.include_router(meta_router, prefix="/api/v1")
    app.include_router(config_router, prefix="/api/v1")
    app.include_router(accounts_router, prefix="/api/v1")
    app.include_router(discovery_router, prefix="/api/v1")
    app.include_router(runs_router, prefix="/api/v1")
    app.include_router(leads_router, prefix="/api/v1")
    app.include_router(feedback_router, prefix="/api/v1")
    app.include_router(activity_router, prefix="/api/v1")

    return app


app = create_app()
