"""Application factory.

Cross-cutting concerns live here so that routers stay declarative: settings, database lifecycle,
engine/node/action registry warm-up, CORS, request correlation, the canonical error envelope, and
the WebSocket event stream wiring.

Nothing in this module knows about wells or drilling; it is the transport shell.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.gzip import GZipMiddleware

from drillai.api.routers import (
    assets,
    context,
    documents,
    drilling,
    evidence,
    health,
    platform,
    registry,
    runs,
    twin,
    workflows,
)
from drillai.api.version import API_VERSION
from drillai.core.config import REPO_ROOT, Settings, get_settings
from drillai.core.errors import AuthenticationRequired, ConfigurationError, DrillAIError
from drillai.core.ids import new_id
from drillai.core.logging import clear_log_context, configure_logging, get_logger, set_log_context
from drillai.db.session import Database
from drillai.ingestion.storage import blob_store_from_settings

logger = get_logger(__name__)

def _warm_registries() -> dict[str, int]:
    """Import the registries that every request may need, and report what got loaded.

    Importing them is not decoration: engines, node types, actions, section providers and tools
    register themselves on import, and a missing import would surface as an empty catalogue to a
    user rather than as a startup failure.
    """
    from drillai.ai.agents import agent_catalogue, install_default_agents
    from drillai.ai.tools import registered_tools
    from drillai.context.builder import registered_section_keys
    from drillai.engines.registry import load_default_engines
    from drillai.ingestion.extractors import extractor_catalogue
    from drillai.security import catalog as _catalog  # noqa: F401  (registers platform actions)
    from drillai.security.actions import registered_actions
    from drillai.workflow.nodes import registered_node_types

    install_default_agents()
    engines = load_default_engines()
    catalogue = agent_catalogue()
    return {
        "engines": len(engines),
        "agents": len(catalogue["agents"]),
        "skills": len(catalogue["skills"]),
        "node_types": len(registered_node_types()),
        "actions": len(registered_actions()),
        "tools": len(registered_tools()),
        "context_sections": len(registered_section_keys()),
        "extractors": len(extractor_catalogue()),
    }


def _jsonable_issues(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pydantic's validation issues, reduced to the plain shapes a client can act on.

    The raw list is not always serialisable: a custom validator that raises puts the exception object
    itself in ``ctx``, and ``input`` carries whatever the caller sent (a bytes field, a datetime). An
    error response that cannot be encoded is a validation failure reported as a server error, which is
    the one thing the error contract exists to prevent. ``type``, ``loc`` and ``msg`` are kept exactly
    as Pydantic reported them; everything else is stringified.
    """
    issues: list[dict[str, Any]] = []
    for error in errors:
        issue: dict[str, Any] = {
            "type": error.get("type"),
            "loc": list(error.get("loc", ())),
            "msg": error.get("msg"),
        }
        context = error.get("ctx")
        if context:
            issue["ctx"] = {key: str(value) for key, value in context.items()}
        raw = error.get("input")
        if raw is None or isinstance(raw, (str, int, float, bool)):
            issue["input"] = raw
        else:
            issue["input"] = repr(raw)[:200]
        issues.append(issue)
    return issues


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    if settings.is_production and not settings.auth_enabled:
        raise ConfigurationError("DRILLAI_AUTH_ENABLED must be true in production")
    if settings.is_production and settings.cors_origins.strip() == "*":
        raise ConfigurationError("wildcard CORS origins are not allowed in production")
    if settings.is_production and settings.e2e_faults:
        # The fault routes are unauthenticated by construction (they exist to make the client fail,
        # not to be authorised), so a deployment that enabled them by accident must not start at all.
        raise ConfigurationError("DRILLAI_E2E_FAULTS must not be enabled in production")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        catalogues = app.state.catalogues
        logger.info("catalogues loaded", extra={"extra_fields": catalogues})
        if settings.run_migrations_on_start:
            await _run_migrations(settings)
        app.state.catalogues = catalogues
        app.state.started_at = time.time()
        yield
        await app.state.database.dispose()

    # Registries are warmed in the factory, not only in the lifespan: an ASGI app mounted without
    # a lifespan (test clients, worker entry points) must not present an empty catalogue, which
    # would make every action resolve to the fail-closed L4 default.
    catalogues = _warm_registries()

    app = FastAPI(
        title=settings.app_name,
        version=API_VERSION,
        description=(
            "Well Engineering Intelligence Platform — engineering context, digital well twin, "
            "data fabric, engineering engines and workflow runtime behind one governed contract. "
            "The LLM is one replaceable component; engineering numbers come from engines."
        ),
        lifespan=lifespan,
        docs_url=f"{settings.api_prefix}/docs",
        openapi_url=f"{settings.api_prefix}/openapi.json",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.catalogues = catalogues
    app.state.database = Database(settings=settings)
    # The blob store is process-scoped: a per-request instance would be correct for the
    # filesystem backend and would silently lose every blob for the in-memory one.
    app.state.blob_store = blob_store_from_settings(settings)

    if settings.e2e_faults:
        # Registered *first*, which makes it the innermost layer: a faulted response still travels
        # out through compression, CORS and the correlation middleware. That ordering is the whole
        # point — a response injected past the CORS layer would be blocked by the browser and read
        # as a network failure, and one injected past the correlation middleware would carry no
        # request id, which is exactly the detail the error contract promises.
        from drillai.api.routers import faults

        app.add_middleware(faults.FaultInjectorMiddleware)

    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials="*" not in settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Trace-ID", "Content-Language"],
    )

    _install_error_handlers(app)
    _install_request_middleware(app)

    prefix = settings.api_prefix
    app.include_router(health.router, prefix=prefix)
    app.include_router(platform.router, prefix=prefix)
    app.include_router(assets.router, prefix=prefix)
    app.include_router(context.router, prefix=prefix)
    app.include_router(documents.router, prefix=prefix)
    app.include_router(evidence.router, prefix=prefix)
    app.include_router(registry.router, prefix=prefix)
    app.include_router(workflows.router, prefix=prefix)
    app.include_router(runs.router, prefix=prefix)
    app.include_router(twin.router, prefix=prefix)
    app.include_router(drilling.router, prefix=prefix)

    if settings.e2e_faults:
        from drillai.api.routers import faults

        logger.warning("e2e fault injection is enabled: /faults endpoints are mounted")
        app.include_router(faults.router, prefix=prefix)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, object]:
        return {
            "service": settings.app_name,
            "api_version": API_VERSION,
            "api_prefix": settings.api_prefix,
            "environment": settings.environment,
            "auth": "bearer" if settings.auth_enabled else "development-roles",
            "docs": f"{settings.api_prefix}/docs",
        }

    return app


def _install_request_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def correlation(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("X-Request-ID") or new_id("req")
        # Published on the request so the error handlers report the same id this middleware puts in
        # the response header. Reading the *incoming* header alone (as the handlers used to) meant a
        # client that sent no id got an envelope with no trace id at all, and — for an unhandled
        # error — no id header either, because that response is produced outside this middleware.
        request.state.request_id = request_id
        set_log_context(request_id=request_id, http_path=request.url.path, http_method=request.method)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            clear_log_context()
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
        return response


def _request_id(request: Request) -> str:
    """The correlation id for this request, however far into the stack the handler is running."""
    published = getattr(request.state, "request_id", None)
    if isinstance(published, str) and published:
        return published
    return request.headers.get("X-Request-ID") or new_id("req")


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DrillAIError)
    async def drillai_error(request: Request, exc: DrillAIError) -> JSONResponse:
        trace_id = _request_id(request)
        if exc.http_status >= 500:
            logger.exception("request failed", extra={"extra_fields": {"code": exc.code, "path": request.url.path}})
        else:
            logger.info(
                "request rejected",
                extra={"extra_fields": {"code": exc.code, "path": request.url.path, "message": exc.message}},
            )
        headers = {"Content-Language": _language(request), "X-Request-ID": trace_id}
        if isinstance(exc, AuthenticationRequired):
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse(exc.to_payload(trace_id=trace_id), status_code=exc.http_status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "platform.validation_failed",
                    "message": "request payload failed validation",
                    "retryable": False,
                    "details": {"issues": _jsonable_issues(exc.errors())},
                    "trace_id": _request_id(request),
                }
            },
            status_code=422,
            headers={"Content-Language": _language(request), "X-Request-ID": _request_id(request)},
        )

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error", extra={"extra_fields": {"path": request.url.path}})
        trace_id = _request_id(request)
        return JSONResponse(
            {
                "error": {
                    "code": "platform.internal_error",
                    "message": "internal error",  # never leak internals to a client
                    "retryable": False,
                    "details": {},
                    "trace_id": trace_id,
                }
            },
            status_code=500,
            # This response is produced outside the correlation middleware (the server-error handler
            # is the outermost layer), so the id has to be set here or a 500 would be the one response
            # a client could not trace.
            headers={"X-Request-ID": trace_id},
        )


def _language(request: Request) -> str:
    header = request.headers.get("Accept-Language") or "en"
    return header.split(",")[0].split("-")[0].strip() or "en"


async def _run_migrations(settings: Settings) -> None:
    """Apply Alembic migrations at startup (opt-in; the deployment pipeline normally runs them)."""
    import asyncio

    from alembic import command
    from alembic.config import Config

    def _upgrade() -> None:
        config = Config(str(REPO_ROOT / "backend" / "alembic.ini"))
        config.set_main_option("script_location", str(REPO_ROOT / "backend" / "alembic"))
        config.set_main_option("sqlalchemy.url", settings.database_url)
        command.upgrade(config, "head")

    await asyncio.to_thread(_upgrade)
    logger.info("database migrations applied")
