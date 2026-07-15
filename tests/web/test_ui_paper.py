from __future__ import annotations

import base64
import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
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
from paper_pipeline.library.model import ConversionRecord, RecipeRecord
from paper_pipeline.library.storage import open_library
from paper_pipeline.services.runtime import RuntimeRegistry
from paper_pipeline.web.app import create_app

pytestmark = pytest.mark.browser
FIXTURE_EXPORT = Path(__file__).parents[1] / "fixtures" / "zotero" / "clean"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _config(tmp_path: Path) -> AppConfig:
    values: dict[str, object] = {
        "config_dir": tmp_path / "config",
        "llm_model": "fake-model",
        "converter_timeout_seconds": 5,
        "_env_file": None,
    }
    return AppConfig(**cast(Any, values))


@pytest.fixture
def paper_server(tmp_path: Path, page: Page) -> Iterator[str]:
    provider = FakeLLMProvider(response="Fake response that is never called.")
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


def _create_and_import(page: Page, url: str, root: Path) -> None:
    created = page.request.post(
        f"{url}/api/library/create",
        data={"path": str(root), "name": "Paper Detail Test"},
    )
    assert created.ok, created.text()
    preview = page.request.post(
        f"{url}/api/import/preview",
        data={"export_path": str(FIXTURE_EXPORT)},
    )
    assert preview.ok, preview.text()
    applied = page.request.post(f"{url}/api/import/apply", data={"plan": preview.json()})
    assert applied.ok, applied.text()


def _seed_full_paper(root: Path) -> None:
    library = open_library(root)
    citekey = "SmithJournal2024"
    record = library.read_paper(citekey)

    conversion_stage = library.stage_dir()
    (conversion_stage / "transcription.md").write_text(
        "# Findings\n\nConverted body from the source.\n\n- First result\n- Second result\n",
        encoding="utf-8",
    )
    figures = conversion_stage / "figures"
    figures.mkdir()
    (figures / "figure-1.png").write_bytes(PNG)
    hashes = library.install_conversion_bundle(citekey, conversion_stage)
    transcription_path = f"papers/{citekey}/transcription.md"
    record.conversion = ConversionRecord(
        source_sha256=record.source_sha256,
        transcription_sha256=hashes[transcription_path],
        backend="fake",
        backend_version="test",
        completed_at=datetime.now(UTC),
    )

    generated_stage = library.stage_dir() / "summary.md"
    generated_stage.write_text(
        "---\n"
        "recipe: summary\n"
        "recipe_version: 2\n"
        "provider: fake\n"
        "model: fake-model\n"
        f"input: {transcription_path}\n"
        f"input_sha256: {hashes[transcription_path]}\n"
        "created: 2026-07-15T12:00:00+00:00\n"
        "---\n"
        "## Summary\n\nA safely rendered generated analysis.\n",
        encoding="utf-8",
    )
    output_path = f"papers/{citekey}/summary.md"
    output_hash = library.install_artifact(generated_stage, output_path)
    record.recipes["summary"] = RecipeRecord(
        recipe_version=2,
        provider="fake",
        model="fake-model",
        input_artifact=transcription_path,
        input_sha256=hashes[transcription_path],
        output_artifact=output_path,
        output_sha256=output_hash,
        completed_at=datetime.now(UTC),
    )
    library.write_paper(record)


def test_full_paper_detail_renders_artifacts_and_provenance(
    page: Page, paper_server: str, tmp_path: Path
) -> None:
    root = tmp_path / "full-library"
    _create_and_import(page, paper_server, root)
    _seed_full_paper(root)

    page.goto(f"{paper_server}/papers/SmithJournal2024")

    expect(page).to_have_title("Journal Article · Paper Pipeline")
    expect(page.get_by_role("heading", name="Journal Article", exact=True)).to_be_visible()
    expect(page.get_by_text("Ada Smith, Ben Jones", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="Findings", exact=True)).to_be_visible()
    expect(page.get_by_text("Converted body from the source.")).to_be_visible()
    expect(page.get_by_text("LLM-generated")).to_have_count(2)
    expect(page.locator(".generated-output h2", has_text="Summary")).to_be_visible()
    page.get_by_text("Provenance", exact=True).click()
    expect(page.get_by_text("fake-model", exact=True)).to_be_visible()
    expect(page.locator(".figure-gallery img")).to_have_count(1)
    expect(page.locator(".figure-gallery img")).to_have_js_property("naturalWidth", 1)

    source_link = page.get_by_role("link", name="Open source PDF")
    source_href = source_link.get_attribute("href")
    assert source_href is not None
    source_response = page.request.get(source_href)
    assert source_response.ok
    assert source_response.headers["content-type"].startswith("application/pdf")


def test_unconverted_paper_has_deliberate_empty_states(
    page: Page, paper_server: str, tmp_path: Path
) -> None:
    root = tmp_path / "unconverted-library"
    _create_and_import(page, paper_server, root)

    page.goto(f"{paper_server}/papers/SmithJournal2024")

    expect(page.get_by_text("Not converted", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="Not converted yet")).to_be_visible()
    expect(page.get_by_role("heading", name="No generated analysis")).to_be_visible()
    expect(page.get_by_text("No extracted figures are available.")).to_be_visible()
    expect(page.get_by_role("link", name="Open source PDF")).to_be_visible()


def test_missing_source_is_reported_without_a_broken_link(
    page: Page, paper_server: str, tmp_path: Path
) -> None:
    root = tmp_path / "missing-source-library"
    _create_and_import(page, paper_server, root)
    library = open_library(root)
    record = library.read_paper("SmithJournal2024")
    assert record.source_pdf is not None
    root.joinpath(*record.source_pdf.split("/")).unlink()

    page.goto(f"{paper_server}/papers/SmithJournal2024")

    expect(page.get_by_role("heading", name="Journal Article", exact=True)).to_be_visible()
    expect(page.get_by_text("Source PDF unavailable in this clone", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Open source PDF")).to_have_count(0)
    response = page.request.get(f"{paper_server}/papers/SmithJournal2024/source")
    assert response.status == 404
