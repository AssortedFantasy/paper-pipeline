from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from tests.fakes import FakeLLMProvider

from paper_pipeline.cli import main
from paper_pipeline.config import AppConfig
from paper_pipeline.convert.runner import ConverterSpec
from paper_pipeline.jobs.model import Job, JobKind, JobState
from paper_pipeline.jobs.queue import CancellationToken
from paper_pipeline.services.runtime import PaperSession, RuntimeRegistry
from paper_pipeline.web.api import WebContext, event_stream
from paper_pipeline.web.app import create_app

FIXTURES = Path(__file__).parents[1] / "fixtures" / "zotero"


def _config(tmp_path: Path) -> AppConfig:
    values: dict[str, object] = {
        "config_dir": tmp_path / "config",
        "llm_provider": "fake",
        "llm_model": "fake-model",
        "llm_concurrency": 2,
        "converter_timeout_seconds": 5,
        "_env_file": None,
    }
    return AppConfig(**cast(Any, values))


@asynccontextmanager
async def _client(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncClient, WebContext, FakeLLMProvider]]:
    provider = FakeLLMProvider()
    registry = RuntimeRegistry(provider_factories={"fake": lambda: provider})
    app = create_app(
        registry=registry,
        config=_config(tmp_path),
        converter_spec=ConverterSpec("tests.fakes:FakeConverter"),
    )
    context = cast(WebContext, app.state.web_context)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, context, provider
    await registry.queue.shutdown()


async def _create_library(client: AsyncClient, path: Path) -> None:
    response = await client.post(
        "/api/library/create",
        json={"path": str(path), "name": "Test Library"},
    )
    assert response.status_code == 200, response.text


async def _import_fixture(client: AsyncClient, fixture: str = "clean") -> dict[str, object]:
    preview = await client.post(
        "/api/import/preview",
        json={"export_path": str(FIXTURES / fixture)},
    )
    assert preview.status_code == 200, preview.text
    response = await client.post("/api/import/apply", json={"plan": preview.json()})
    assert response.status_code == 200, response.text
    return cast(dict[str, object], response.json())


@pytest.mark.asyncio
async def test_library_lifecycle_and_import_contract(tmp_path: Path) -> None:
    library = tmp_path / "library"
    async with _client(tmp_path) as (client, _, _):
        assert (await client.get("/api/library")).status_code == 409

        await _create_library(client, library)
        current = await client.get("/api/library")
        assert current.status_code == 200
        assert current.json() == {"root": str(library.resolve())}

        report = await client.post("/api/library/validate")
        assert report.status_code == 200
        assert report.json()["problems"] == []

        reindex = await client.post("/api/library/reindex")
        assert reindex.status_code == 200
        assert reindex.json()["state"] == "succeeded"
        assert (library / "indexes" / "titles.md").is_file()

        imported = await _import_fixture(client)
        assert len(cast(list[str], imported["added"])) == 5
        assert imported["refreshed"] == []
        assert imported["replaced"] == []

        reopened = await client.post("/api/library/open", json={"path": str(library)})
        assert reopened.status_code == 200
        assert reopened.json()["root"] == str(library.resolve())


@pytest.mark.asyncio
async def test_paper_list_filters_and_detail_contract(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, _, _):
        await _create_library(client, tmp_path / "library")
        await _import_fixture(client)

        all_papers = await client.get("/api/papers")
        assert all_papers.status_code == 200
        assert all_papers.json()["total"] == 5

        filtered = await client.get(
            "/api/papers",
            params={"query": "journal", "author": "Ada", "year": 2024},
        )
        assert filtered.status_code == 200
        assert filtered.json()["total"] == 1
        paper = filtered.json()["papers"][0]

        paged = await client.get("/api/papers", params={"limit": 2, "offset": 1})
        assert paged.status_code == 200
        assert len(paged.json()["papers"]) == 2
        assert paged.json()["total"] == 5

        detail = await client.get(f"/api/papers/{paper['metadata']['citekey']}")
        assert detail.status_code == 200
        assert detail.json()["metadata"]["title"] == paper["metadata"]["title"]
        assert (await client.get("/api/papers/not-a-paper")).status_code == 404


@pytest.mark.asyncio
async def test_conversion_recipe_and_job_list_contract(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, context, provider):
        await _create_library(client, tmp_path / "library")
        await _import_fixture(client)
        papers = (await client.get("/api/papers")).json()["papers"]
        citekey = papers[0]["metadata"]["citekey"]

        conversion = await client.post("/api/jobs/conversion", json={"citekeys": [citekey]})
        assert conversion.status_code == 202, conversion.text
        conversion_id = conversion.json()["jobs"][0]["id"]
        assert context.runtime is not None
        converted = await context.runtime.queue.wait(conversion_id)
        assert converted.state is JobState.SUCCEEDED

        recipes = await client.post(
            "/api/jobs/recipes",
            json={
                "citekeys": [citekey],
                "recipe_names": ["summary"],
                "provider": "fake",
                "model": "fake-model",
            },
        )
        assert recipes.status_code == 202, recipes.text
        recipe_id = recipes.json()["jobs"][0]["id"]
        completed = await context.runtime.queue.wait(recipe_id)
        assert completed.state is JobState.SUCCEEDED
        assert provider.calls

        succeeded = await client.get("/api/jobs", params={"state": "succeeded"})
        assert succeeded.status_code == 200
        ids = {job["id"] for job in succeeded.json()["jobs"]}
        assert {conversion_id, recipe_id} <= ids

        conversion_jobs = await client.get("/api/jobs", params={"kind": "conversion"})
        assert conversion_jobs.status_code == 200
        assert {job["kind"] for job in conversion_jobs.json()["jobs"]} == {"conversion"}


@pytest.mark.asyncio
async def test_cancel_and_retry_routes(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, context, _):
        await _create_library(client, tmp_path / "library")
        assert context.runtime is not None
        runtime = context.runtime

        started = asyncio.Event()

        async def waits_for_cancel(
            _session: PaperSession, _job: Job, token: CancellationToken
        ) -> None:
            started.set()
            await token.wait()

        cancellable = await runtime.enqueue_paper(
            "cancel-me",
            JobKind.CONVERSION,
            "cancel-test",
            waits_for_cancel,
        )
        await started.wait()
        cancelled_response = await client.post(f"/api/jobs/{cancellable.id}/cancel")
        assert cancelled_response.status_code == 200
        cancelled = await runtime.queue.wait(cancellable.id)
        assert cancelled.state is JobState.CANCELLED

        attempts = 0

        async def fails_once(_session: PaperSession, _job: Job, _token: CancellationToken) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("first attempt fails")

        failed = await runtime.enqueue_paper(
            "retry-me",
            JobKind.RECIPE,
            "retry-test",
            fails_once,
        )
        assert (await runtime.queue.wait(failed.id)).state is JobState.FAILED
        retried_response = await client.post(f"/api/jobs/{failed.id}/retry")
        assert retried_response.status_code == 202, retried_response.text
        retried_id = retried_response.json()["id"]
        assert retried_id != failed.id
        assert (await runtime.queue.wait(retried_id)).state is JobState.SUCCEEDED


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_sse_events_and_disconnect_does_not_cancel_job(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, context, _):
        await _create_library(client, tmp_path / "library")
        assert context.runtime is not None
        runtime = context.runtime
        stream = event_stream(cast(Request, _ConnectedRequest()), runtime)
        connected = await anext(stream)
        assert connected == ": connected\n\n"

        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked(_session: PaperSession, _job: Job, _token: CancellationToken) -> None:
            started.set()
            await release.wait()

        job = await runtime.enqueue_paper(
            "sse-paper",
            JobKind.RECIPE,
            "sse-test",
            blocked,
        )
        event = await asyncio.wait_for(anext(stream), timeout=1)
        assert "event: state" in event
        assert job.id in event
        await started.wait()

        await stream.aclose()
        current = runtime.queue.get(job.id)
        assert current is not None
        assert current.state is JobState.RUNNING
        release.set()
        assert (await runtime.queue.wait(job.id)).state is JobState.SUCCEEDED


def test_serve_uses_localhost_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, object] = {}

    def fake_run(app: str, **kwargs: object) -> None:
        called["app"] = app
        called.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)
    assert main(["serve"]) == 0
    assert called == {
        "app": "paper_pipeline.web.app:create_app",
        "factory": True,
        "host": "127.0.0.1",
        "port": 8000,
    }
