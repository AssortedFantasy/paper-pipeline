from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.error import URLError
from urllib.request import urlopen

import pytest
import uvicorn
from playwright.sync_api import Page, Route, expect
from tests.fakes import FakeLLMProvider

from paper_pipeline.config import AppConfig
from paper_pipeline.convert.runner import ConverterSpec
from paper_pipeline.jobs.model import JobKind, JobScope
from paper_pipeline.jobs.recovery import AttemptMarker, AttemptMarkerStore
from paper_pipeline.library.model import PaperMetadata, PaperRecord
from paper_pipeline.library.paths import ATTEMPTS_DIR, FORMAT_VERSION
from paper_pipeline.library.storage import create_library
from paper_pipeline.services.runtime import RuntimeRegistry
from paper_pipeline.web.app import create_app

pytestmark = pytest.mark.browser
FIXTURE_EXPORT = Path(__file__).parents[1] / "fixtures" / "zotero" / "clean"


@dataclass(frozen=True)
class RunningServer:
    url: str


def _config(tmp_path: Path) -> AppConfig:
    values: dict[str, object] = {
        "config_dir": tmp_path / "config",
        "llm_model": "fake-model",
        "converter_timeout_seconds": 10,
        "_env_file": None,
    }
    return AppConfig(**cast(Any, values))


@pytest.fixture
def jobs_server(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    page: Page,
) -> Iterator[RunningServer]:
    converter_kwargs = cast(dict[str, object], getattr(request, "param", {}))
    registry = RuntimeRegistry(
        provider_factories={"fake": lambda: FakeLLMProvider(response="Fake result")}
    )
    app = create_app(
        registry=registry,
        config=_config(tmp_path),
        converter_spec=ConverterSpec("tests.fakes:FakeConverter", kwargs=converter_kwargs),
        provider_name="fake",
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = cast(tuple[str, int], probe.getsockname())[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            with urlopen(f"{url}/healthz", timeout=0.2) as response:
                if response.status == 200:
                    break
        except (OSError, URLError):
            time.sleep(0.02)
    else:
        pytest.fail("jobs UI test server did not start")
    yield RunningServer(url=url)
    page.close()
    server.should_exit = True
    server.force_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive()


def _create_and_import(page: Page, server: RunningServer, root: Path) -> str:
    created = page.request.post(
        f"{server.url}/api/library/create",
        data={"path": str(root), "name": "Jobs Test Library"},
    )
    assert created.ok, created.text()
    preview = page.request.post(
        f"{server.url}/api/import/preview",
        data={"export_path": str(FIXTURE_EXPORT)},
    )
    assert preview.ok, preview.text()
    applied = page.request.post(f"{server.url}/api/import/apply", data={"plan": preview.json()})
    assert applied.ok, applied.text()
    papers = page.request.get(f"{server.url}/api/papers")
    assert papers.ok, papers.text()
    return cast(str, papers.json()["papers"][0]["metadata"]["citekey"])


def _queue_conversion(page: Page, server: RunningServer, citekey: str) -> str:
    response = page.request.post(f"{server.url}/api/jobs/conversion", data={"citekeys": [citekey]})
    assert response.ok, response.text()
    return cast(str, response.json()["jobs"][0]["id"])


def test_jobs_update_live_over_sse(page: Page, jobs_server: RunningServer, tmp_path: Path) -> None:
    citekey = _create_and_import(page, jobs_server, tmp_path / "library")
    page.goto(f"{jobs_server.url}/jobs")
    expect(page.get_by_role("heading", name="Jobs", exact=True)).to_have_count(0)
    expect(page.locator(".state-panel .state-icon")).to_have_count(0)
    expect(page.locator("#connection-status")).to_contain_text("connected")
    expect(page.get_by_text("Queue is clear.")).to_be_visible()

    job_id = _queue_conversion(page, jobs_server, citekey)
    row = page.locator(f"[data-job-id='{job_id}']")
    expect(row).to_be_visible()
    expect(row.locator(".badge-succeeded")).to_have_text("succeeded")


@pytest.mark.parametrize("jobs_server", [{"mode": "hang", "hang_seconds": 30}], indirect=True)
def test_cancel_running_job(page: Page, jobs_server: RunningServer, tmp_path: Path) -> None:
    citekey = _create_and_import(page, jobs_server, tmp_path / "library")
    page.goto(f"{jobs_server.url}/jobs")
    expect(page.locator("#connection-status")).to_contain_text("connected")
    job_id = _queue_conversion(page, jobs_server, citekey)
    row = page.locator(f"[data-job-id='{job_id}']")
    expect(row.locator(".badge-running")).to_be_visible()
    row.get_by_role("button", name="Cancel").click()
    expect(page.locator(f"[data-job-id='{job_id}'] .badge-cancelled")).to_be_visible()


@pytest.mark.parametrize("jobs_server", [{"mode": "failure"}], indirect=True)
def test_failed_job_log_tail_and_retry(
    page: Page, jobs_server: RunningServer, tmp_path: Path
) -> None:
    citekey = _create_and_import(page, jobs_server, tmp_path / "library")
    page.goto(f"{jobs_server.url}/jobs")
    expect(page.locator("#connection-status")).to_contain_text("connected")
    job_id = _queue_conversion(page, jobs_server, citekey)
    row = page.locator(f"[data-job-id='{job_id}']")
    expect(row.locator(".badge-failed")).to_be_visible()

    row.get_by_role("button", name="View log").click()
    expect(row.locator(".log-tail")).to_contain_text("fake converter failure")

    row.get_by_role("button", name="Retry").click()
    retry_row = page.locator(".job-row", has_text=f"Retry of {job_id}")
    expect(retry_row).to_be_visible()
    expect(retry_row.locator(".badge-failed")).to_be_visible()


@pytest.mark.parametrize("jobs_server", [{"mode": "failure"}], indirect=True)
def test_retry_selected_failed_jobs(page: Page, jobs_server: RunningServer, tmp_path: Path) -> None:
    _create_and_import(page, jobs_server, tmp_path / "library")
    papers = page.request.get(f"{jobs_server.url}/api/papers")
    citekeys = [paper["metadata"]["citekey"] for paper in papers.json()["papers"][:2]]
    page.goto(f"{jobs_server.url}/jobs")
    response = page.request.post(
        f"{jobs_server.url}/api/jobs/conversion",
        data={"citekeys": citekeys},
    )
    assert response.ok, response.text()
    original_ids = [job["id"] for job in response.json()["jobs"]]
    for job_id in original_ids:
        expect(page.locator(f"[data-job-id='{job_id}'] .badge-failed")).to_be_visible()

    page.get_by_role("button", name="Retry selected").click()
    expect(page.get_by_role("alert")).to_contain_text("select at least one")
    checkboxes = page.locator("#batch-retry-selection input[name=job_ids]")
    expect(checkboxes).to_have_count(2)
    for checkbox in checkboxes.all():
        checkbox.check()
    page.get_by_role("button", name="Retry selected").click()

    for job_id in original_ids:
        retry_row = page.locator(".job-row", has_text=f"Retry of {job_id}")
        expect(retry_row).to_be_visible()
        expect(retry_row.locator(".badge-failed")).to_be_visible()


def test_interrupted_attempt_is_labeled_and_retryable(
    page: Page, jobs_server: RunningServer, tmp_path: Path
) -> None:
    root = tmp_path / "interrupted-library"
    library = create_library(root, name="Interrupted Test")
    citekey = "Interrupted2026"
    library.write_paper(
        PaperRecord(
            format_version=FORMAT_VERSION,
            metadata=PaperMetadata(citekey=citekey, title="Interrupted paper"),
        )
    )
    marker = AttemptMarker(
        job_id="interrupted-conversion",
        target=f"papers/{citekey}",
        operation="convert",
        kind=JobKind.CONVERSION,
        scope=JobScope.PAPER,
        started_at=datetime.now(UTC),
    )
    AttemptMarkerStore(
        library.operational_dir() / ATTEMPTS_DIR,
        managed_root=root,
    ).create(marker)
    opened = page.request.post(f"{jobs_server.url}/api/library/open", data={"path": str(root)})
    assert opened.ok, opened.text()

    page.goto(f"{jobs_server.url}/jobs")
    interrupted = page.locator("[data-job-id='interrupted-conversion']")
    expect(interrupted.locator(".badge-interrupted")).to_have_text("Interrupted")
    expect(interrupted).to_contain_text(citekey)
    interrupted.get_by_role("button", name="Retry").click()
    expect(page.locator("[data-job-id='interrupted-conversion']")).to_have_count(0)
    expect(page.locator(".job-row", has_text="Retry of interrupted-conversion")).to_be_visible()


def test_disconnected_indicator_recovers(
    page: Page, jobs_server: RunningServer, tmp_path: Path
) -> None:
    created = page.request.post(
        f"{jobs_server.url}/api/library/create",
        data={"path": str(tmp_path / "library"), "name": "Jobs Test Library"},
    )
    assert created.ok, created.text()

    def abort_events(route: Route) -> None:
        route.abort()

    page.route("**/events", abort_events)
    page.goto(f"{jobs_server.url}/jobs")
    expect(page.locator("#connection-status")).to_contain_text("disconnected")
    page.unroute("**/events", abort_events)
    expect(page.locator("#connection-status")).to_contain_text("connected", timeout=15_000)


@pytest.mark.parametrize("jobs_server", [{"mode": "hang", "hang_seconds": 30}], indirect=True)
def test_closing_real_sse_tab_does_not_cancel_running_job(
    page: Page, jobs_server: RunningServer, tmp_path: Path
) -> None:
    citekey = _create_and_import(page, jobs_server, tmp_path / "library")
    dashboard = page.context.new_page()
    try:
        dashboard.goto(f"{jobs_server.url}/jobs")
        expect(dashboard.locator("#connection-status")).to_contain_text("connected")
        job_id = _queue_conversion(page, jobs_server, citekey)
        expect(dashboard.locator(f"[data-job-id='{job_id}'] .badge-running")).to_be_visible()
        dashboard.close()

        jobs = page.request.get(f"{jobs_server.url}/api/jobs")
        assert jobs.ok, jobs.text()
        state = next(job["state"] for job in jobs.json()["jobs"] if job["id"] == job_id)
        assert state == "running"

        cancelled = page.request.post(f"{jobs_server.url}/api/jobs/{job_id}/cancel")
        assert cancelled.ok, cancelled.text()
    finally:
        if not dashboard.is_closed():
            dashboard.close()
