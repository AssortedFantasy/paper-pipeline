"""FastAPI application factory and shared production wiring."""

import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from paper_pipeline.config import AppConfig, load_config
from paper_pipeline.convert.runner import ConverterSpec
from paper_pipeline.recipes.openai_provider import OpenAIProvider
from paper_pipeline.services.runtime import RuntimeRegistry
from paper_pipeline.web.api import WebContext, create_api_router
from paper_pipeline.web.jobs_ui import create_jobs_router
from paper_pipeline.web.ui import create_ui_router

_DEFAULT_REGISTRY: RuntimeRegistry | None = None
_DEFAULT_LOCK = threading.Lock()


def create_app(
    *,
    registry: RuntimeRegistry | None = None,
    config: AppConfig | None = None,
    converter_spec: ConverterSpec | None = None,
    provider_name: str = "openai",
) -> FastAPI:
    """Create one web app over the process-shared runtime registry."""
    config = config or load_config()
    registry = registry or _default_registry(config)
    app = FastAPI(title="Paper Pipeline")

    @app.middleware("http")
    async def reject_cross_origin_mutations(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin is not None:
                expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
                if origin.rstrip("/").casefold() != expected.rstrip("/").casefold():
                    return JSONResponse(
                        {"detail": "cross-origin mutations are not allowed"},
                        status_code=403,
                    )
        return await call_next(request)

    context = WebContext(
        registry=registry,
        config=config,
        converter_spec=converter_spec or _configured_converter(config),
        provider_name=provider_name,
    )
    app.state.web_context = context
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="static",
    )
    app.include_router(create_ui_router(context))
    app.include_router(create_jobs_router(context))
    app.include_router(create_api_router(context))

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _configured_converter(config: AppConfig) -> ConverterSpec:
    if config.remote_converter_host:
        return ConverterSpec(
            "paper_pipeline.convert.remote:RemoteConverter",
            {
                "host": config.remote_converter_host,
                "remote_root": config.remote_converter_root,
                "remote_python": config.remote_converter_python,
            },
        )
    return ConverterSpec("paper_pipeline.convert.marker:MarkerConverter")


def _default_registry(config: AppConfig) -> RuntimeRegistry:
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        if _DEFAULT_REGISTRY is None:
            _DEFAULT_REGISTRY = RuntimeRegistry(
                llm_concurrency=config.llm_concurrency,
                provider_factories={"openai": lambda: OpenAIProvider(config)},
            )
        return _DEFAULT_REGISTRY
