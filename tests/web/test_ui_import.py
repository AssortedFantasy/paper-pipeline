from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple, cast
from urllib.error import URLError
from urllib.request import urlopen

import pytest
import uvicorn
from playwright.sync_api import Page, expect
from tests.fakes import FakeLLMProvider

from paper_pipeline.config import AppConfig
from paper_pipeline.convert.runner import ConverterSpec
from paper_pipeline.library.storage import create_library as seed_library
from paper_pipeline.services.runtime import RuntimeRegistry
from paper_pipeline.web.app import create_app

pytestmark = pytest.mark.browser
FIXTURES = Path(__file__).parents[1] / "fixtures" / "zotero"


class ImportServer(NamedTuple):
    url: str
    library: Path


def _config(tmp_path: Path) -> AppConfig:
    values: dict[str, object] = {
        "config_dir": tmp_path / "config",
        "llm_model": "fake-model",
        "converter_timeout_seconds": 5,
        "_env_file": None,
    }
    return AppConfig(**cast(Any, values))


@pytest.fixture
def import_server(tmp_path: Path, page: Page) -> Iterator[ImportServer]:
    provider = FakeLLMProvider(response="A concise generated result.")
    app = create_app(
        registry=RuntimeRegistry(provider_factories={"fake": lambda: provider}),
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

    library = tmp_path / "library"
    response = page.request.post(
        f"{url}/api/library/create",
        data={"path": str(library), "name": "Import Browser Test"},
    )
    assert response.ok, response.text()
    yield ImportServer(url, library)
    page.close()
    server.should_exit = True
    server.force_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive()


def _preview(page: Page, server: ImportServer, fixture: str) -> None:
    page.goto(f"{server.url}/import")
    page.get_by_label("Zotero RDF export").fill(str(FIXTURES / fixture))
    page.get_by_role("button", name="Preview import").click()


def test_fixture_preview_and_apply_flow(page: Page, import_server: ImportServer) -> None:
    _preview(page, import_server, "clean")

    expect(page).to_have_title("Library · Paper Pipeline")
    expect(page.get_by_role("heading", name="5 actionable papers")).to_be_visible()
    expect(page.get_by_text("5 additions", exact=True)).to_be_visible()
    expect(page.locator(".import-group tbody tr")).to_have_count(5)

    page.get_by_role("button", name="Apply import").click()
    expect(page.get_by_role("heading", name="5 papers updated")).to_be_visible()
    expect(page.get_by_text("5 added", exact=True)).to_be_visible()
    assert len(list((import_server.library / "papers").glob("*/paper.json"))) == 5


def test_problems_are_prominent(page: Page, import_server: ImportServer) -> None:
    _preview(page, import_server, "problems")

    problems = page.locator(".import-problems[role=alert]")
    expect(problems).to_be_visible()
    expect(problems.get_by_role("heading", name="Problems require attention")).to_be_visible()
    expect(problems).to_contain_text("<no citekey>")
    expect(problems).to_contain_text("unsupported item type")


def test_cancel_before_apply_leaves_library_untouched(
    page: Page, import_server: ImportServer
) -> None:
    _preview(page, import_server, "clean")
    expect(page.get_by_role("heading", name="5 actionable papers")).to_be_visible()

    page.get_by_role("button", name="Cancel").click()

    expect(page.get_by_role("status")).to_contain_text(
        "Import cancelled. The library was not changed."
    )
    assert not list((import_server.library / "papers").glob("*/paper.json"))


def test_previews_are_isolated_across_browser_tabs(page: Page, import_server: ImportServer) -> None:
    second = page.context.new_page()
    try:
        _preview(page, import_server, "clean")
        _preview(second, import_server, "clean")
        page.get_by_role("button", name="Cancel").click()
        expect(page.get_by_role("status")).to_contain_text("Import cancelled")

        second.get_by_role("button", name="Apply import").click()
        expect(second.get_by_role("heading", name="5 papers updated")).to_be_visible()
        assert len(list((import_server.library / "papers").glob("*/paper.json"))) == 5
    finally:
        second.close()


def test_import_page_can_open_an_existing_library(
    page: Page, import_server: ImportServer, tmp_path: Path
) -> None:
    existing = tmp_path / "existing-library"
    seed_library(existing, name="Existing Library")
    page.goto(f"{import_server.url}/import")

    page.get_by_label("Existing library folder").fill(str(existing))
    page.get_by_role("button", name="Open library").click()

    expect(page.get_by_role("status")).to_contain_text("Opened library")
    expect(page.locator("#library-chip")).to_contain_text("existing-library")
    expect(page.locator("#import-library-path")).to_have_value(str(existing))


def test_library_control_error_is_designed(
    page: Page, import_server: ImportServer, tmp_path: Path
) -> None:
    nonempty = tmp_path / "not-empty"
    nonempty.mkdir()
    (nonempty / "keep.txt").write_text("keep", encoding="utf-8")
    page.goto(f"{import_server.url}/import")

    page.get_by_label("New library folder").fill(str(nonempty))
    page.get_by_role("button", name="Create library").click()

    error = page.get_by_role("alert")
    expect(error).to_contain_text("Library operation failed")
    expect(error).to_contain_text("Refusing to create a library in non-empty directory")
    assert (nonempty / "keep.txt").read_text(encoding="utf-8") == "keep"
