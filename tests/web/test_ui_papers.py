from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from urllib.error import URLError
from urllib.request import urlopen

import pytest
import uvicorn
from playwright.sync_api import Page, expect
from tests.fakes import FakeLLMProvider

from paper_pipeline.config import AppConfig
from paper_pipeline.convert.runner import ConverterSpec
from paper_pipeline.services.runtime import RuntimeRegistry
from paper_pipeline.web.app import create_app

pytestmark = pytest.mark.browser
FIXTURE_EXPORT = Path(__file__).parents[1] / "fixtures" / "zotero" / "clean"


def _config(tmp_path: Path) -> AppConfig:
    values: dict[str, object] = {
        "config_dir": tmp_path / "config",
        "llm_model": "fake-model",
        "converter_timeout_seconds": 5,
        "_env_file": None,
    }
    return AppConfig(**cast(Any, values))


@pytest.fixture
def ui_server(tmp_path: Path, page: Page) -> Iterator[str]:
    provider = FakeLLMProvider(response="A concise generated result.")
    registry = RuntimeRegistry(provider_factories={"fake": lambda: provider})
    app = create_app(
        registry=registry,
        config=_config(tmp_path),
        converter_spec=ConverterSpec("tests.fakes:FakeConverter"),
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
        server.should_exit = True
        thread.join(timeout=5)
        pytest.fail("UI test server did not start")
    yield url
    page.close()
    server.should_exit = True
    server.force_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive()


def _create_library(page: Page, url: str, root: Path) -> None:
    response = page.request.post(
        f"{url}/api/library/create",
        data={"path": str(root), "name": "Browser Test Library"},
    )
    assert response.ok, response.text()


def _import_papers(page: Page, url: str) -> None:
    preview = page.request.post(
        f"{url}/api/import/preview",
        data={"export_path": str(FIXTURE_EXPORT)},
    )
    assert preview.ok, preview.text()
    applied = page.request.post(
        f"{url}/api/import/apply",
        data={"plan": preview.json()},
    )
    assert applied.ok, applied.text()


def _wait_for_jobs(page: Page, url: str, *, count: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = page.request.get(f"{url}/api/jobs")
        assert response.ok, response.text()
        jobs = response.json()["jobs"]
        if len(jobs) >= count and all(
            job["state"] in {"succeeded", "failed", "cancelled"} for job in jobs
        ):
            assert all(job["state"] == "succeeded" for job in jobs), jobs
            return
        time.sleep(0.05)
    pytest.fail("jobs did not finish")


def test_papers_load_filter_select_and_launch(page: Page, ui_server: str, tmp_path: Path) -> None:
    _create_library(page, ui_server, tmp_path / "library")
    _import_papers(page, ui_server)

    page.goto(f"{ui_server}/papers")
    expect(page).to_have_title("Papers · Paper Pipeline")
    expect(page.get_by_role("heading", name="Papers", exact=True)).to_be_visible()
    expect(page.locator("tbody tr")).to_have_count(5)
    expect(page.locator("script[src*='htmx.min.js']")).to_have_count(1)
    expect(page.get_by_label("Recipe").locator("option")).to_have_count(4)
    assert page.get_by_label("Recipe").locator("option").evaluate_all(
        "options => options.map(option => option.value)"
    ) == ["summary", "contributions", "intro", "method"]

    page.get_by_placeholder("Title, author, or citekey").fill("journal")
    expect(page.locator("tbody tr")).to_have_count(1)
    page.get_by_placeholder("Title, author, or citekey").fill("")
    expect(page.locator("tbody tr")).to_have_count(5)

    page.get_by_role("button", name="Select all pending").click()
    expect(page.locator("tbody input[type=checkbox]:checked")).to_have_count(5)
    for checkbox in page.locator("tbody input[type=checkbox]").all():
        checkbox.uncheck()

    page.get_by_role("button", name="Convert selected").click()
    expect(page.get_by_role("alert")).to_contain_text("Select at least one paper")

    first_checkbox = page.locator("tbody input[type=checkbox]").first
    first_checkbox.check()
    page.get_by_role("button", name="Convert selected").click()
    expect(page.get_by_role("status")).to_contain_text("Queued 1 conversion job")
    _wait_for_jobs(page, ui_server, count=1)

    page.reload()
    page.get_by_label("Conversion").select_option("ready")
    expect(page.locator("tbody tr")).to_have_count(1)
    page.locator("tbody input[type=checkbox]").check()
    page.get_by_role("button", name="Run recipe").click()
    expect(page.get_by_role("status")).to_contain_text("Queued 1 recipe job")
    _wait_for_jobs(page, ui_server, count=2)

    expect(page.locator(".job-stream")).to_contain_text("running")


def test_empty_library_state(page: Page, ui_server: str, tmp_path: Path) -> None:
    _create_library(page, ui_server, tmp_path / "empty-library")
    page.goto(f"{ui_server}/")

    expect(page).to_have_url(f"{ui_server}/")
    expect(page.get_by_role("heading", name="No papers found")).to_be_visible()
    expect(page.get_by_text("Import papers to start building this library.")).to_be_visible()
    expect(page.locator("tbody")).to_have_count(0)


def test_no_library_error_state(page: Page, ui_server: str) -> None:
    page.goto(f"{ui_server}/papers")
    expect(page.get_by_role("heading", name="No library is open")).to_be_visible()
    expect(page.locator(".job-stream")).to_contain_text("No library open")


def test_no_library_dashboard_can_create_library(
    page: Page, ui_server: str, tmp_path: Path
) -> None:
    library = tmp_path / "created-from-dashboard"
    page.goto(f"{ui_server}/papers")

    page.get_by_label("New library folder").fill(str(library))
    page.get_by_label("Library name").fill("Dashboard Library")
    page.get_by_role("button", name="Create library").click()

    expect(page.locator(".library-operation-result").get_by_role("status")).to_contain_text(
        "Created and opened library"
    )
    expect(page.locator("#library-chip")).to_contain_text("created-from-dashboard")
    assert (library / "library.json").is_file()
    page.get_by_role("link", name="View papers").click()
    expect(page.get_by_role("heading", name="No papers found")).to_be_visible()


def test_active_library_can_validate_and_rebuild(
    page: Page, ui_server: str, tmp_path: Path
) -> None:
    library = tmp_path / "maintained-library"
    _create_library(page, ui_server, library)
    _import_papers(page, ui_server)
    page.goto(f"{ui_server}/papers")

    page.get_by_role("button", name="Validate library").click()
    expect(page.get_by_role("status")).to_contain_text("validation passed")

    page.get_by_role("button", name="Rebuild indexes").click()
    expect(page.get_by_role("status")).to_contain_text("Rebuilt indexes")
    assert "Journal Article" in (library / "indexes" / "titles.md").read_text(encoding="utf-8")

    source = next((library / "papers" / "SmithJournal2024" / "source").glob("*.pdf"))
    source.unlink()
    page.get_by_role("button", name="Validate library").click()
    validation = page.locator(".library-operation-result").get_by_role("alert")
    expect(validation).to_contain_text("Validation found 1 problem")
    expect(validation).to_contain_text("Source PDF is missing")


def test_stale_papers_tab_cannot_launch_against_new_library(
    page: Page, ui_server: str, tmp_path: Path
) -> None:
    first = tmp_path / "first-library"
    second = tmp_path / "second-library"
    _create_library(page, ui_server, first)
    _import_papers(page, ui_server)
    page.goto(f"{ui_server}/papers")
    page.locator("tbody input[type=checkbox]").first.check()

    _create_library(page, ui_server, second)
    page.get_by_role("button", name="Convert selected").click()

    expect(page.get_by_role("alert")).to_contain_text("selected library changed")
    jobs = page.request.get(f"{ui_server}/api/jobs")
    assert jobs.ok, jobs.text()
    assert jobs.json()["jobs"] == []

    page.get_by_role("button", name="Validate library").click()
    expect(page.locator(".library-operation-result").get_by_role("alert")).to_contain_text(
        "selected library changed"
    )
