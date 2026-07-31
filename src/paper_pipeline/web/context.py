"""Shared dependencies for the server-rendered web application."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from paper_pipeline.config import AppConfig
from paper_pipeline.convert.runner import ConverterSpec
from paper_pipeline.pages.runner import PageRendererSpec
from paper_pipeline.services.runtime import LibraryRuntime, RuntimeRegistry


@dataclass
class WebContext:
    """Process-owned services and the currently selected library."""

    registry: RuntimeRegistry
    config: AppConfig
    converter_spec: ConverterSpec
    page_renderer_spec: PageRendererSpec
    provider_name: str = "openai"
    runtime: LibraryRuntime | None = None
    request_shutdown: Callable[[], None] | None = None
