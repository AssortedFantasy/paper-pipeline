from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from tests.fakes import FakeLLMProvider

from paper_pipeline.cli import main
from paper_pipeline.config import AppConfig
from paper_pipeline.convert.runner import ConverterSpec
from paper_pipeline.jobs.model import Job, JobKind, JobState
from paper_pipeline.jobs.queue import CancellationToken
from paper_pipeline.pages.runner import PageRendererSpec
from paper_pipeline.services.runtime import PaperSession, RuntimeRegistry
from paper_pipeline.web.api import WebContext
from paper_pipeline.web.app import create_app

FIXTURES = Path(__file__).parents[1] / "fixtures" / "zotero"


@pytest.mark.asyncio
async def test_cross_origin_mutation_is_rejected(tmp_path: Path) -> None:
    app = create_app(registry=RuntimeRegistry(), config=_config(tmp_path))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://paper-pipeline.test",
    ) as client:
        response = await client.post(
            "/api/library/create",
            json={"path": str(tmp_path / "library")},
            headers={"Origin": "https://malicious.example"},
        )

    assert response.status_code == 403
    assert not (tmp_path / "library").exists()


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
) -> AsyncIterator[tuple[AsyncClient, WebContext]]:
    provider = FakeLLMProvider()
    registry = RuntimeRegistry(provider_factories={"fake": lambda: provider})
    app = create_app(
        registry=registry,
        config=_config(tmp_path),
        converter_spec=ConverterSpec("tests.fakes:FakeConverter"),
        page_renderer_spec=PageRendererSpec("tests.fakes:FakePageRenderer"),
    )
    context = cast(WebContext, app.state.web_context)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, context
    await registry.queue.shutdown()


async def _create_library(client: AsyncClient, path: Path) -> None:
    response = await client.post(
        "/api/library/create",
        json={"path": str(path), "name": "Test Library"},
    )
    assert response.status_code == 200, response.text


async def _import_fixture(
    client: AsyncClient,
    fixture: str = "clean",
    *,
    addition_limit: int | None = 1,
) -> tuple[dict[str, object], dict[str, object]]:
    preview = await client.post(
        "/api/import/preview",
        json={"export_path": str(FIXTURES / fixture)},
    )
    assert preview.status_code == 200, preview.text
    plan = cast(dict[str, object], preview.json())
    if addition_limit is not None:
        plan["additions"] = cast(list[object], plan["additions"])[:addition_limit]
    response = await client.post("/api/import/apply", json={"plan": plan})
    assert response.status_code == 200, response.text
    return (
        plan,
        cast(dict[str, object], response.json()),
    )


@pytest.mark.asyncio
async def test_job_requests_reject_ambiguous_or_duplicate_work(tmp_path: Path) -> None:
    invalid_requests = [
        ("/api/jobs/conversion", {}),
        ("/api/jobs/conversion", {"citekeys": ["Smith2024"], "pending": True}),
        ("/api/jobs/conversion", {"citekeys": ["Smith2024", "Smith2024"]}),
        ("/api/jobs/pages", {}),
        ("/api/jobs/pages", {"citekeys": ["Smith2024"], "pending": True}),
        (
            "/api/jobs/recipes",
            {"citekeys": ["Smith2024"], "recipe_names": []},
        ),
        (
            "/api/jobs/recipes",
            {
                "citekeys": ["Smith2024"],
                "recipe_names": ["summary", "summary"],
            },
        ),
    ]

    async with _client(tmp_path) as (client, _):
        for endpoint, payload in invalid_requests:
            response = await client.post(endpoint, json=payload)
            assert response.status_code == 422


@pytest.mark.asyncio
async def test_library_lifecycle_and_import_contract(tmp_path: Path) -> None:
    library = tmp_path / "library"
    async with _client(tmp_path) as (client, _):
        assert (await client.get("/api/library")).status_code == 409

        await _create_library(client, library)
        current = await client.get("/api/library")
        assert current.status_code == 200
        assert Path(current.json()["root"]) == library.resolve()

        report = await client.post("/api/library/validate")
        assert report.status_code == 200
        assert report.json()["problems"] == []

        reindex = await client.post("/api/library/reindex")
        assert reindex.status_code == 200
        assert reindex.json()["state"] == "succeeded"

        preview, imported = await _import_fixture(client)
        planned_citekeys = {
            item["metadata"]["citekey"] for item in cast(list[dict[str, Any]], preview["additions"])
        }
        assert planned_citekeys
        assert set(cast(list[str], imported["added"])) == planned_citekeys
        assert not imported["failed"]

        reopened = await client.post("/api/library/open", json={"path": str(library)})
        assert reopened.status_code == 200
        assert Path(reopened.json()["root"]) == library.resolve()


@pytest.mark.asyncio
async def test_paper_list_filters_and_detail_contract(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, _):
        await _create_library(client, tmp_path / "library")
        await _import_fixture(client, addition_limit=None)

        all_papers = await client.get("/api/papers")
        assert all_papers.status_code == 200
        all_payload = all_papers.json()
        assert all_payload["papers"]
        assert all_payload["total"] == len(all_payload["papers"])

        filtered = await client.get(
            "/api/papers",
            params={"query": "journal", "author": "Ada", "year": 2024},
        )
        assert filtered.status_code == 200
        filtered_payload = filtered.json()
        assert filtered_payload["total"] == len(filtered_payload["papers"]) == 1
        paper = filtered_payload["papers"][0]
        assert paper["metadata"]["year"] == 2024
        assert any("Ada" in author for author in paper["metadata"]["authors"])

        paged = await client.get("/api/papers", params={"limit": 2, "offset": 1})
        assert paged.status_code == 200
        assert [item["metadata"]["citekey"] for item in paged.json()["papers"]] == [
            item["metadata"]["citekey"] for item in all_payload["papers"][1:3]
        ]
        assert paged.json()["total"] == all_payload["total"]

        detail = await client.get(f"/api/papers/{paper['metadata']['citekey']}")
        assert detail.status_code == 200
        assert detail.json()["metadata"]["citekey"] == paper["metadata"]["citekey"]
        assert (await client.get("/api/papers/not-a-paper")).status_code == 404


@pytest.mark.asyncio
async def test_conversion_recipe_and_job_list_contract(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, context):
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

        pages = await client.post("/api/jobs/pages", json={"citekeys": [citekey]})
        assert pages.status_code == 202, pages.text
        pages_id = pages.json()["jobs"][0]["id"]
        rendered = await context.runtime.queue.wait(pages_id)
        assert rendered.state is JobState.SUCCEEDED
        assert rendered.kind is JobKind.PAGE_RENDER

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

        conversion_jobs = await client.get(
            "/api/jobs",
            params={"state": "succeeded", "kind": "conversion"},
        )
        assert conversion_jobs.status_code == 200
        ids = {job["id"] for job in conversion_jobs.json()["jobs"]}
        assert conversion_id in ids
        assert recipe_id not in ids


@pytest.mark.asyncio
async def test_cancel_and_retry_routes(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, context):
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


def test_serve_defaults_to_a_loopback_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    invocation: dict[str, object] = {}

    def record_server_start(_app: str, **options: object) -> None:
        invocation.update(options)

    monkeypatch.setattr("uvicorn.run", record_server_start)

    assert main(["serve"]) == 0
    assert invocation["host"] in {"127.0.0.1", "localhost", "::1"}
