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
from pypdf import PdfWriter
from tests.fakes import FakeLLMProvider

from paper_pipeline.config import AppConfig
from paper_pipeline.convert.runner import ConverterSpec
from paper_pipeline.pages.runner import PageRendererSpec
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
    provider = FakeLLMProvider(
        response="A concise generated result.",
        delay_seconds=0.1,
        cached_tokens=40,
    )
    registry = RuntimeRegistry(provider_factories={"fake": lambda: provider})
    app = create_app(
        registry=registry,
        config=_config(tmp_path),
        converter_spec=ConverterSpec("tests.fakes:FakeConverter"),
        page_renderer_spec=PageRendererSpec("tests.fakes:FakePageRenderer"),
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


def _replace_with_pdf(path: Path, pages: int) -> None:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    with path.open("wb") as stream:
        writer.write(stream)


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


def _configure_work(page: Page, *options: str) -> None:
    page.get_by_role("button", name="Configure", exact=False).click()
    for option in options:
        page.get_by_role("checkbox", name=option, exact=False).click()
    page.get_by_role("button", name="Done", exact=True).click()


def test_papers_load_filter_select_and_launch(page: Page, ui_server: str, tmp_path: Path) -> None:
    _create_library(page, ui_server, tmp_path / "library")
    _import_papers(page, ui_server)

    page.goto(f"{ui_server}/papers")
    expect(page).to_have_title("Papers · Paper Pipeline")
    expect(page.get_by_role("heading", name="Papers", exact=True)).to_have_count(0)
    expect(page.locator("tbody tr")).to_have_count(5)
    expect(page.locator("script[src*='htmx.min.js']")).to_have_count(1)

    expect(page.get_by_role("button", name="Citekey, sorted ascending", exact=True)).to_be_visible()
    citekeys = page.locator("tbody .citekey")
    ascending = citekeys.all_text_contents()
    assert ascending == sorted(ascending, key=str.casefold)
    page.get_by_role("button", name="Citekey, sorted ascending", exact=True).click()
    expect(
        page.get_by_role("button", name="Citekey, sorted descending", exact=True)
    ).to_be_visible()
    assert citekeys.all_text_contents() == list(reversed(ascending))

    page.get_by_placeholder("Title, author, or citekey").fill("journal")
    expect(page.locator("tbody tr")).to_have_count(1)
    page.get_by_placeholder("Title, author, or citekey").fill("")
    expect(page.locator("tbody tr")).to_have_count(5)

    page.get_by_role("button", name="Select pending conversion", exact=True).click()
    expect(page.locator("tbody input[type=checkbox]:checked")).to_have_count(5)
    page.get_by_role("button", name="Unselect all").click()
    expect(page.locator("tbody input[type=checkbox]:checked")).to_have_count(0)
    page.get_by_role("button", name="Select all", exact=True).click()
    expect(page.locator("tbody input[type=checkbox]:checked")).to_have_count(5)
    page.get_by_role("button", name="Unselect all").click()

    _configure_work(page, "Transcribe PDF")
    expect(page.get_by_role("button", name="Configure (1 selected)")).to_be_visible()
    queue_button = page.get_by_role("button", name="Queue (0 papers)")
    expect(queue_button).to_be_disabled()

    first_checkbox = page.locator("tbody input[type=checkbox]").first
    first_checkbox.check()
    queue_button = page.get_by_role("button", name="Queue (1 papers)")
    expect(queue_button).to_be_enabled()
    queue_button.click()
    expect(page.get_by_role("status")).to_contain_text("Queued 1 job across 1 paper")
    _wait_for_jobs(page, ui_server, count=1)

    page.reload()
    page.get_by_label("Conversion").select_option("ready")
    expect(page.locator("tbody tr")).to_have_count(1)
    page.locator("tbody input[type=checkbox]").check()
    _configure_work(page, "Recipe: Summary", "Recipe: Contributions")
    page.get_by_role("button", name="Queue (1 papers)").click()
    expect(page.get_by_role("status")).to_contain_text("Queued 1 job across 1 paper")
    expect(page.locator("tbody .live-job-status")).to_contain_text("running")
    _wait_for_jobs(page, ui_server, count=2)
    expect(page.locator("tbody .spend-cell")).to_contain_text("$0.0020")
    expect(page.locator("tbody .spend-cell")).to_have_attribute("title", "Cache hit rate: 40.0%")

    citekey = page.locator("tbody .citekey").text_content()
    assert citekey is not None
    assert (tmp_path / "library" / "papers" / citekey / "summary.md").is_file()
    assert (tmp_path / "library" / "papers" / citekey / "contributions.md").is_file()

    expect(page.locator(".job-stream")).to_contain_text("running")


def test_large_document_page_count_and_bulk_selection_guard(
    page: Page, ui_server: str, tmp_path: Path
) -> None:
    library = tmp_path / "library"
    _create_library(page, ui_server, library)
    _import_papers(page, ui_server)
    book_source = next((library / "papers" / "DoeBook2020" / "source").glob("*.pdf"))
    _replace_with_pdf(book_source, 100)

    page.goto(f"{ui_server}/papers")
    page.get_by_role("button", name="Refresh").click()

    book_row = page.locator("tr[data-citekey='DoeBook2020']")
    expect(page.get_by_role("columnheader", name="Pages")).to_be_visible()
    expect(book_row.locator(".page-count-large")).to_have_text("100")
    expect(page.get_by_text("1 large document (100+ pages) excluded")).to_be_visible()

    page.get_by_role("button", name="Select all", exact=True).click()
    expect(page.locator("tbody input[type=checkbox]:checked")).to_have_count(4)
    expect(book_row.locator("input[type=checkbox]")).not_to_be_checked()

    page.get_by_role("button", name="Select pending conversion", exact=True).click()
    expect(page.locator("tbody input[type=checkbox]:checked")).to_have_count(4)
    book_row.locator("input[type=checkbox]").check()
    expect(book_row.locator("input[type=checkbox]")).to_be_checked()


def test_page_rendering_is_a_separate_local_action(
    page: Page, ui_server: str, tmp_path: Path
) -> None:
    _create_library(page, ui_server, tmp_path / "library")
    _import_papers(page, ui_server)
    page.goto(f"{ui_server}/papers")
    first_row = page.locator("tbody tr").first
    first_row.locator("input[type=checkbox]").check()

    _configure_work(page, "Render PDF Pages")
    page.get_by_role("button", name="Queue (1 papers)").click()
    expect(page.get_by_role("status")).to_contain_text("Queued 1 job across 1 paper")
    _wait_for_jobs(page, ui_server, count=1)

    page.reload()
    expect(page.locator("tbody tr").first.locator("td").nth(7)).to_contain_text("ready")
    page.get_by_role("button", name="Select pending pages", exact=True).click()
    expect(page.locator("tbody input[type=checkbox]:checked")).to_have_count(4)


def test_papers_refresh_preserves_filters(page: Page, ui_server: str, tmp_path: Path) -> None:
    _create_library(page, ui_server, tmp_path / "library")
    _import_papers(page, ui_server)
    page.goto(f"{ui_server}/papers")

    page.get_by_placeholder("Title, author, or citekey").fill("journal")
    expect(page.locator("tbody tr")).to_have_count(1)
    page.get_by_role("button", name="Citekey, sorted ascending", exact=True).click()
    expect(
        page.get_by_role("button", name="Citekey, sorted descending", exact=True)
    ).to_be_visible()

    page.get_by_role("button", name="Refresh").click()

    expect(page.locator("tbody tr")).to_have_count(1)
    expect(page.get_by_placeholder("Title, author, or citekey")).to_have_value("journal")
    expect(
        page.get_by_role("button", name="Citekey, sorted descending", exact=True)
    ).to_be_visible()


def test_papers_table_text_display_and_column_resize(
    page: Page, ui_server: str, tmp_path: Path
) -> None:
    _create_library(page, ui_server, tmp_path / "library")
    _import_papers(page, ui_server)
    page.goto(f"{ui_server}/papers")

    title = page.locator(".paper-title").first
    expect(title).to_have_css("white-space", "nowrap")
    expect(page.locator(".table-view-controls")).to_have_count(0)
    expect(page.locator(".column-wrap-toggle")).to_have_count(3)
    expect(page.get_by_role("button", name="Wrap Paper text", exact=True)).to_have_attribute(
        "aria-pressed", "false"
    )

    paper_header = page.locator(".paper-column-header")
    initial_width = paper_header.bounding_box()
    assert initial_width is not None
    page.get_by_role("separator", name="Resize Paper column").press("ArrowRight")
    resized_width = paper_header.bounding_box()
    assert resized_width is not None
    assert resized_width["width"] > initial_width["width"]

    page.get_by_role("button", name="Wrap Paper text", exact=True).click()
    expect(title).to_have_css("white-space", "normal")
    expect(page.locator(".authors-text").first).to_have_css("white-space", "nowrap")
    expect(page.get_by_role("button", name="Wrap Paper text", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )

    page.get_by_role("button", name="Citekey, sorted ascending", exact=True).click()
    expect(page.locator(".paper-title").first).to_have_css("white-space", "normal")
    expect(page.get_by_role("button", name="Wrap Paper text", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    width_after_sort = page.locator(".paper-column-header").bounding_box()
    assert width_after_sort is not None
    assert width_after_sort["width"] == pytest.approx(resized_width["width"], abs=1)


def test_no_library_error_state(page: Page, ui_server: str) -> None:
    page.goto(f"{ui_server}/papers")
    expect(page.get_by_role("heading", name="No library is open")).to_be_visible()
    expect(page.get_by_text("Open a library to view papers.")).to_be_visible()
    expect(page.locator(".no-library-state .state-icon")).to_have_count(0)
    expect(page.get_by_role("button", name="Create library")).to_have_count(0)
    expect(page.get_by_role("button", name="Open library")).to_have_count(0)
    expect(page.locator(".job-stream")).to_contain_text("No library open")


def test_library_page_can_create_library(page: Page, ui_server: str, tmp_path: Path) -> None:
    library = tmp_path / "created-from-dashboard"
    page.goto(f"{ui_server}/library")

    page.get_by_label("New library folder").fill(str(library))
    page.get_by_role("button", name="Create library").click()

    expect(page.locator("#library-setup-result").get_by_role("status")).to_contain_text(
        "Created and opened library"
    )
    expect(page.get_by_role("region", name="Library maintenance")).to_contain_text(
        "created-from-dashboard"
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
    page.goto(f"{ui_server}/library")

    page.get_by_role("button", name="Validate library").click()
    expect(page.get_by_role("status")).to_contain_text("validation passed")

    page.get_by_role("button", name="Rebuild indexes").click()
    expect(page.get_by_role("status")).to_contain_text("Rebuilt indexes")
    assert "Journal Article" in (library / "indexes" / "titles.md").read_text(encoding="utf-8")

    source = next((library / "papers" / "SmithJournal2024" / "source").glob("*.pdf"))
    source.unlink()
    page.get_by_role("button", name="Validate library").click()
    validation = page.locator("#library-maintenance-result").get_by_role("alert")
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
    _configure_work(page, "Transcribe PDF")

    _create_library(page, ui_server, second)
    page.get_by_role("button", name="Queue (1 papers)").click()

    expect(page.get_by_role("alert")).to_contain_text("selected library changed")
    jobs = page.request.get(f"{ui_server}/api/jobs")
    assert jobs.ok, jobs.text()
    assert jobs.json()["jobs"] == []


def test_configured_work_cycles_run_overwrite_and_skips_ready_results(
    page: Page, ui_server: str, tmp_path: Path
) -> None:
    _create_library(page, ui_server, tmp_path / "library")
    _import_papers(page, ui_server)
    page.goto(f"{ui_server}/papers")
    page.locator("tbody input[type=checkbox]").first.check()

    page.get_by_role("button", name="Configure", exact=False).click()
    summary = page.get_by_role("checkbox", name="Recipe: Summary", exact=False)
    summary.click()
    expect(summary).to_have_attribute("aria-checked", "true")
    expect(summary.locator("strong")).to_be_hidden()
    page.get_by_role("button", name="Done", exact=True).click()
    page.get_by_role("button", name="Queue (1 papers)").click()
    _wait_for_jobs(page, ui_server, count=1)

    page.reload()
    page.locator("tbody input[type=checkbox]").first.check()
    _configure_work(page, "Recipe: Summary")
    page.get_by_role("button", name="Queue (1 papers)").click()
    expect(page.get_by_role("status")).to_contain_text("already up to date")
    jobs = page.request.get(f"{ui_server}/api/jobs").json()["jobs"]
    assert len([job for job in jobs if job["kind"] == "recipe"]) == 1

    page.get_by_role("button", name="Configure", exact=False).click()
    summary = page.get_by_role("checkbox", name="Recipe: Summary", exact=False)
    summary.click()
    expect(summary).to_have_attribute("aria-checked", "mixed")
    expect(summary.locator("strong")).to_be_visible()
    expect(summary.locator("strong")).to_have_text("(Overwrite)")
    page.get_by_role("button", name="Done", exact=True).click()
    page.get_by_role("button", name="Queue (1 papers)").click()
    _wait_for_jobs(page, ui_server, count=2)
