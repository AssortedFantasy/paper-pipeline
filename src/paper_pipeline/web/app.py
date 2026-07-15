"""FastAPI application factory and shared production wiring."""

import threading
from pathlib import Path

from fastapi import FastAPI
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
    context = WebContext(
        registry=registry,
        config=config,
        converter_spec=converter_spec
        or ConverterSpec("paper_pipeline.convert.marker:MarkerConverter"),
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


def _default_registry(config: AppConfig) -> RuntimeRegistry:
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        if _DEFAULT_REGISTRY is None:
            _DEFAULT_REGISTRY = RuntimeRegistry(
                llm_concurrency=config.llm_concurrency,
                provider_factories={"openai": lambda: OpenAIProvider(config)},
            )
        return _DEFAULT_REGISTRY
