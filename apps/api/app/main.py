"""FastAPI application factory and lifespan wiring."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from core.config import get_settings
from core.logging import configure_logging, new_request_id, reset_request_id, set_request_id
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as system_router
from app.auth.adapters.redis_store import close_redis
from app.auth.errors import register_auth_exception_handlers
from app.auth.keys_router import router as keys_router
from app.auth.router import router as auth_router
from app.auth.workspaces.invitations import router as invitations_router
from app.auth.workspaces.router import router as workspaces_router
from app.db import dispose_engine, run_migrations
from app.documents.delete import router as documents_delete_router
from app.documents.upload import router as documents_router

logger = logging.getLogger("knowledge_os.api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    logger.info(
        "api_starting",
        extra={"event": "api_starting", "env": settings.app_env, "version": settings.version},
    )

    if settings.migrations_on_boot:
        try:
            # Alembic is sync; run it off the event loop.
            await asyncio.to_thread(run_migrations)
            logger.info("migrations_applied", extra={"event": "migrations_applied"})
        except Exception as exc:
            logger.warning(
                "migrations_failed",
                extra={"event": "migrations_failed", "error": f"{type(exc).__name__}: {exc}"},
            )

    try:
        yield
    finally:
        await close_redis()
        await dispose_engine()
        logger.info("api_stopped", extra={"event": "api_stopped"})


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Knowledge OS API",
        version=settings.version,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_id_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get("x-request-id") or new_request_id()
        token = set_request_id(request_id)
        try:
            response = await call_next(request)
        finally:
            reset_request_id(token)
        response.headers["x-request-id"] = request_id
        return response

    app.include_router(system_router)
    app.include_router(auth_router)
    app.include_router(workspaces_router)
    app.include_router(invitations_router)
    app.include_router(keys_router)
    app.include_router(documents_router)
    app.include_router(documents_delete_router)
    register_auth_exception_handlers(app)
    return app


app = create_app()
