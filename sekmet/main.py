"""FastAPI application factory."""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from .config import Settings, get_settings
from .context import AppContext
from .traffic.log import TrafficMiddleware


def create_app(settings: Settings | None = None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = settings or get_settings()
    ctx = AppContext(settings)
    app = FastAPI(title=settings.software_name, version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.ctx = ctx

    def get_ctx() -> AppContext:
        return ctx

    from .auth.server import build_router as auth_router
    from .fhir.router import build_router as fhir_router
    from .subscriptions.engine import build_hook_router
    from .ui.routes import build_router as ui_router, mount_static
    from .bulk.server import build_router as bulk_router

    app.include_router(auth_router(get_ctx))
    app.include_router(build_hook_router(get_ctx))
    app.include_router(bulk_router(get_ctx))  # before the /fhir catch-all
    app.include_router(fhir_router(get_ctx))
    app.include_router(ui_router(get_ctx))
    mount_static(app)

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/ui")

    app.add_middleware(TrafficMiddleware, get_ctx=get_ctx)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
                       expose_headers=["Location", "ETag", "Last-Modified", "Content-Location", "X-Request-Id"])
    return app


def app_factory() -> FastAPI:
    """uvicorn --factory entry point."""
    return create_app()
