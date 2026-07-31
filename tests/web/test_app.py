from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient

from paper_pipeline.cli import main
from paper_pipeline.config import AppConfig
from paper_pipeline.services.runtime import RuntimeRegistry
from paper_pipeline.web.app import create_app


def _config(tmp_path: Path) -> AppConfig:
    values: dict[str, object] = {
        "config_dir": tmp_path / "config",
        "llm_provider": "fake",
        "llm_model": "fake-model",
        "converter_timeout_seconds": 5,
        "_env_file": None,
    }
    return AppConfig(**cast(Any, values))


@pytest.mark.asyncio
async def test_cross_origin_mutation_is_rejected(tmp_path: Path) -> None:
    app = create_app(registry=RuntimeRegistry(), config=_config(tmp_path))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://paper-pipeline.test",
    ) as client:
        response = await client.post(
            "/library/create",
            content=f"library_path={tmp_path / 'library'}",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://malicious.example",
            },
        )

    assert response.status_code == 403
    assert not (tmp_path / "library").exists()


@pytest.mark.asyncio
async def test_json_api_is_not_exposed(tmp_path: Path) -> None:
    app = create_app(registry=RuntimeRegistry(), config=_config(tmp_path))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://paper-pipeline.test",
    ) as client:
        response = await client.get("/api/library")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_exit_stops_jobs_then_requests_server_shutdown(tmp_path: Path) -> None:
    shutdown_requested: list[bool] = []
    registry = RuntimeRegistry()
    app = create_app(
        registry=registry,
        config=_config(tmp_path),
        request_shutdown=lambda: shutdown_requested.append(True),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://paper-pipeline.test",
    ) as client:
        response = await client.post("/exit")

    assert response.status_code == 200
    assert "Paper Pipeline has stopped" in response.text
    assert "window.close()" in response.text
    assert shutdown_requested == [True]


def test_serve_defaults_to_a_loopback_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    invocation: dict[str, object] = {}

    def record_server_start(host: str, port: int) -> int:
        invocation.update({"host": host, "port": port})
        return 0

    monkeypatch.setattr("paper_pipeline.cli._run_serve", record_server_start)

    assert main(["serve"]) == 0
    assert invocation["host"] in {"127.0.0.1", "localhost", "::1"}
