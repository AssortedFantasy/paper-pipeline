from __future__ import annotations

import base64
import socket
import struct
import threading
import time
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.error import URLError
from urllib.request import urlopen

import pytest
import uvicorn
from playwright.sync_api import Page, Route, ViewportSize, expect
from tests.fakes import FakeLLMProvider

from paper_pipeline.config import AppConfig
from paper_pipeline.convert.runner import ConverterSpec
from paper_pipeline.jobs.model import JobKind, JobScope, JobState
from paper_pipeline.jobs.recovery import AttemptMarker, AttemptMarkerStore
from paper_pipeline.library.model import (
    ConversionRecord,
    PaperMetadata,
    PaperRecord,
    RecipeRecord,
)
from paper_pipeline.library.paths import ATTEMPTS_DIR, FORMAT_VERSION
from paper_pipeline.library.storage import create_library, open_library
from paper_pipeline.services.runtime import RuntimeRegistry
from paper_pipeline.web.app import create_app

pytestmark = pytest.mark.browser
FIXTURES = Path(__file__).parents[1] / "fixtures" / "zotero"
VIEWPORT = ViewportSize(width=1440, height=1000)
SNAPSHOTS = Path(__file__).with_name("snapshots")
FIXED_TIME = datetime(2026, 7, 15, 14, 30, tzinfo=UTC)
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@dataclass(frozen=True)
class VisualServer:
    url: str
    registry: RuntimeRegistry


def _config(tmp_path: Path) -> AppConfig:
    values: dict[str, object] = {
        "config_dir": tmp_path / "config",
        "llm_model": "fake-model",
        "converter_timeout_seconds": 5,
        "_env_file": None,
    }
    return AppConfig(**cast(Any, values))


@pytest.fixture
def visual_server(tmp_path: Path, page: Page) -> Iterator[VisualServer]:
    registry = RuntimeRegistry(
        provider_factories={"fake": lambda: FakeLLMProvider(response="Seeded fake output")}
    )
    app = create_app(
        registry=registry,
        config=_config(tmp_path),
        converter_spec=ConverterSpec("tests.fakes:FakeConverter", kwargs={"mode": "failure"}),
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
        pytest.fail("visual-test server did not start")
    page.set_viewport_size(VIEWPORT)
    yield VisualServer(url, registry)
    page.close()
    server.should_exit = True
    server.force_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive()


def _create_library(page: Page, server: VisualServer, root: Path) -> None:
    response = page.request.post(
        f"{server.url}/api/library/create",
        data={"path": str(root), "name": "Visual Fixture Library"},
    )
    assert response.ok, response.text()


def _open_library(page: Page, server: VisualServer, root: Path) -> None:
    response = page.request.post(f"{server.url}/api/library/open", data={"path": str(root)})
    assert response.ok, response.text()


def _import_fixture(page: Page, server: VisualServer, fixture: str = "clean") -> None:
    preview = page.request.post(
        f"{server.url}/api/import/preview",
        data={"export_path": str(FIXTURES / fixture)},
    )
    assert preview.ok, preview.text()
    applied = page.request.post(
        f"{server.url}/api/import/apply",
        data={"plan": preview.json()},
    )
    assert applied.ok, applied.text()


def _seed_full_paper(root: Path) -> None:
    library = open_library(root)
    citekey = "SmithJournal2024"
    record = library.read_paper(citekey)
    stage = library.stage_dir()
    (stage / "transcription.md").write_text(
        "# Findings\n\n"
        "The seeded transcription keeps visual diffs focused on layout.\n\n"
        "## Main result\n\n"
        "- Portable folder-based library\n"
        "- Hash-validated source artifacts\n",
        encoding="utf-8",
    )
    figures = stage / "figures"
    figures.mkdir()
    (figures / "figure-1.png").write_bytes(PNG)
    hashes = library.install_conversion_bundle(citekey, stage)
    transcription_path = f"papers/{citekey}/transcription.md"
    record.conversion = ConversionRecord(
        source_sha256=record.source_sha256,
        transcription_sha256=hashes[transcription_path],
        backend="fake",
        backend_version="visual-fixture",
        completed_at=FIXED_TIME,
    )
    generated = library.stage_dir() / "summary.md"
    generated.write_text(
        "---\n"
        "recipe: summary\n"
        "recipe_version: 1\n"
        "provider: fake\n"
        "model: fake-model\n"
        f"input: {transcription_path}\n"
        f"input_sha256: {hashes[transcription_path]}\n"
        f"created: {FIXED_TIME.isoformat()}\n"
        "---\n"
        "## Summary\n\nA concise, seeded analysis of the paper.\n",
        encoding="utf-8",
    )
    output_path = f"papers/{citekey}/summary.md"
    output_hash = library.install_artifact(generated, output_path)
    record.recipes["summary"] = RecipeRecord(
        recipe_version=1,
        provider="fake",
        model="fake-model",
        input_artifact=transcription_path,
        input_sha256=hashes[transcription_path],
        output_artifact=output_path,
        output_sha256=output_hash,
        completed_at=FIXED_TIME,
    )
    library.write_paper(record)


def _settle(page: Page) -> None:
    page.evaluate("document.fonts.ready")
    page.add_style_tag(
        content=(
            "*, *::before, *::after { animation: none !important; transition: none !important; }"
        )
    )


def _snapshot(page: Page, name: str, *, update: bool) -> None:
    _settle(page)
    actual = page.screenshot(
        full_page=True,
        animations="disabled",
        caret="hide",
        scale="css",
    )
    baseline = SNAPSHOTS / name
    if update:
        SNAPSHOTS.mkdir(parents=True, exist_ok=True)
        baseline.write_bytes(actual)
        return
    if not baseline.is_file():
        pytest.fail(
            f"missing visual baseline {baseline}; run the documented --update-snapshots command"
        )
    changed, total, maximum_delta = _pixel_difference(actual, baseline.read_bytes())
    allowed = max(10, int(total * 0.0005))
    assert changed <= allowed, (
        f"visual snapshot differs: {name}; {changed}/{total} pixels changed "
        f"(allowed {allowed}, max channel delta {maximum_delta}); run the documented "
        "--update-snapshots command after reviewing the UI change"
    )


def _pixel_difference(actual: bytes, expected: bytes) -> tuple[int, int, int]:
    actual_size, actual_pixels = _decode_png(actual)
    expected_size, expected_pixels = _decode_png(expected)
    assert actual_size == expected_size, (
        f"visual dimensions changed from {expected_size} to {actual_size}"
    )
    changed = 0
    maximum_delta = 0
    for offset in range(0, len(actual_pixels), 4):
        delta = max(
            abs(actual_pixels[offset + channel] - expected_pixels[offset + channel])
            for channel in range(4)
        )
        maximum_delta = max(maximum_delta, delta)
        if delta > 2:
            changed += 1
    return changed, actual_size[0] * actual_size[1], maximum_delta


def _decode_png(payload: bytes) -> tuple[tuple[int, int], bytes]:
    """Decode Playwright's 8-bit RGB/RGBA PNGs into RGBA pixels."""
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    offset = 8
    compressed = bytearray()
    width = height = channels = 0
    while offset < len(payload):
        length = int.from_bytes(payload[offset : offset + 4], "big")
        kind = payload[offset + 4 : offset + 8]
        data = payload[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            width, height, depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", data
            )
            assert depth == 8 and color_type in (2, 6)
            assert (compression, filtering, interlace) == (0, 0, 0)
            channels = 3 if color_type == 2 else 4
        elif kind == b"IDAT":
            compressed.extend(data)
        elif kind == b"IEND":
            break
    stride = width * channels
    raw = zlib.decompress(bytes(compressed))
    assert len(raw) == height * (stride + 1)
    pixels = bytearray(height * stride)
    for row in range(height):
        source = row * (stride + 1)
        filter_type = raw[source]
        scanline = raw[source + 1 : source + 1 + stride]
        destination = row * stride
        for column, value in enumerate(scanline):
            left = pixels[destination + column - channels] if column >= channels else 0
            above = pixels[destination - stride + column] if row else 0
            upper_left = (
                pixels[destination - stride + column - channels]
                if row and column >= channels
                else 0
            )
            if filter_type == 0:
                decoded = value
            elif filter_type == 1:
                decoded = value + left
            elif filter_type == 2:
                decoded = value + above
            elif filter_type == 3:
                decoded = value + ((left + above) // 2)
            elif filter_type == 4:
                decoded = value + _paeth(left, above, upper_left)
            else:
                raise AssertionError(f"unsupported PNG filter: {filter_type}")
            pixels[destination + column] = decoded & 0xFF
    if channels == 4:
        rgba = pixels
    else:
        rgba = bytearray(width * height * 4)
        for source in range(0, len(pixels), 3):
            destination = (source // 3) * 4
            rgba[destination : destination + 3] = pixels[source : source + 3]
            rgba[destination + 3] = 255
    return (width, height), bytes(rgba)


def _paeth(left: int, above: int, upper_left: int) -> int:
    prediction = left + above - upper_left
    distances = (
        abs(prediction - left),
        abs(prediction - above),
        abs(prediction - upper_left),
    )
    return (left, above, upper_left)[distances.index(min(distances))]


def test_visual_papers_table_filled(
    page: Page, visual_server: VisualServer, tmp_path: Path, update_snapshots: bool
) -> None:
    root = tmp_path / "visual-library"
    _create_library(page, visual_server, root)
    _import_fixture(page, visual_server)

    page.goto(f"{visual_server.url}/papers")
    expect(page.locator("tbody tr")).to_have_count(5)
    _snapshot(page, "papers-table-filled.png", update=update_snapshots)


def test_visual_papers_table_empty(
    page: Page, visual_server: VisualServer, tmp_path: Path, update_snapshots: bool
) -> None:
    _create_library(page, visual_server, tmp_path / "visual-library")

    page.goto(f"{visual_server.url}/papers")
    expect(page.get_by_role("heading", name="No papers found")).to_be_visible()
    _snapshot(page, "papers-table-empty.png", update=update_snapshots)


def test_visual_paper_detail(
    page: Page, visual_server: VisualServer, tmp_path: Path, update_snapshots: bool
) -> None:
    root = tmp_path / "visual-library"
    _create_library(page, visual_server, root)
    _import_fixture(page, visual_server)
    _seed_full_paper(root)

    page.goto(f"{visual_server.url}/papers/SmithJournal2024")
    expect(page.get_by_role("heading", name="Findings", exact=True)).to_be_visible()
    expect(page.locator(".figure-gallery img")).to_have_js_property("naturalWidth", 1)
    page.get_by_text("Provenance", exact=True).click()
    _snapshot(page, "paper-detail.png", update=update_snapshots)


def test_visual_jobs_running_failed_interrupted(
    page: Page, visual_server: VisualServer, tmp_path: Path, update_snapshots: bool
) -> None:
    root = tmp_path / "visual-library"
    library = create_library(root, name="Visual Fixture Library")
    for citekey, title in (
        ("Running2026", "A running conversion"),
        ("Failed2026", "A failed conversion"),
        ("Interrupted2026", "An interrupted conversion"),
    ):
        library.write_paper(
            PaperRecord(
                format_version=FORMAT_VERSION,
                metadata=PaperMetadata(citekey=citekey, title=title),
            )
        )
    AttemptMarkerStore(
        library.operational_dir() / ATTEMPTS_DIR,
        managed_root=root,
    ).create(
        AttemptMarker(
            job_id="interrupted-visual",
            target="papers/Interrupted2026",
            operation="convert",
            kind=JobKind.CONVERSION,
            scope=JobScope.PAPER,
            started_at=FIXED_TIME,
        )
    )
    _open_library(page, visual_server, root)

    job_ids: list[str] = []
    for citekey in ("Running2026", "Failed2026"):
        response = page.request.post(
            f"{visual_server.url}/api/jobs/conversion",
            data={"citekeys": [citekey]},
        )
        assert response.ok, response.text()
        job_ids.append(cast(str, response.json()["jobs"][0]["id"]))
    runtime = visual_server.registry.open(root)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        jobs = [runtime.queue.get(job_id) for job_id in job_ids]
        if all(job is not None and job.state.is_terminal for job in jobs):
            break
        time.sleep(0.02)
    else:
        pytest.fail("seeded visual jobs did not finish")
    running = runtime.queue.get(job_ids[0])
    failed = runtime.queue.get(job_ids[1])
    assert running is not None and failed is not None
    running.state = JobState.RUNNING
    running.started_at = FIXED_TIME
    running.finished_at = None
    running.error = None
    failed.state = JobState.FAILED
    failed.started_at = FIXED_TIME
    failed.finished_at = FIXED_TIME
    failed.error = "The fake converter could not process this source PDF."

    page.goto(f"{visual_server.url}/jobs")
    expect(page.locator(".badge-running")).to_be_visible()
    expect(page.locator(".badge-failed")).to_be_visible()
    expect(page.locator(".badge-interrupted")).to_be_visible()
    _snapshot(page, "jobs-running-failed-interrupted.png", update=update_snapshots)
    running.state = JobState.FAILED


def test_visual_import_preview(
    page: Page, visual_server: VisualServer, tmp_path: Path, update_snapshots: bool
) -> None:
    _create_library(page, visual_server, tmp_path / "visual-library")
    page.goto(f"{visual_server.url}/import")
    page.get_by_label("Zotero RDF export").fill(str(FIXTURES / "clean"))
    page.get_by_role("button", name="Preview import").click()
    expect(page.get_by_role("heading", name="5 actionable papers")).to_be_visible()
    page.get_by_label("Library folder", exact=True).fill(r"D:\Libraries\Visual Fixture")
    page.get_by_label("Existing library folder").fill(r"D:\Libraries\Visual Fixture")
    page.get_by_label("Zotero RDF export").fill(r"D:\Exports\library.rdf")
    _snapshot(page, "import-preview.png", update=update_snapshots)


def test_visual_error_state(
    page: Page, visual_server: VisualServer, tmp_path: Path, update_snapshots: bool
) -> None:
    _create_library(page, visual_server, tmp_path / "visual-library")
    page.goto(f"{visual_server.url}/import")
    page.get_by_label("Zotero RDF export").fill(str(FIXTURES / "problems"))
    page.get_by_role("button", name="Preview import").click()
    expect(page.get_by_role("heading", name="Problems require attention")).to_be_visible()
    page.get_by_label("Library folder", exact=True).fill(r"D:\Libraries\Visual Fixture")
    page.get_by_label("Existing library folder").fill(r"D:\Libraries\Visual Fixture")
    page.get_by_label("Zotero RDF export").fill(r"D:\Exports\problem-library.rdf")
    _snapshot(page, "import-error-state.png", update=update_snapshots)


def test_visual_disconnected_state(
    page: Page, visual_server: VisualServer, tmp_path: Path, update_snapshots: bool
) -> None:
    _create_library(page, visual_server, tmp_path / "visual-library")

    def abort_events(route: Route) -> None:
        route.abort()

    page.route("**/events", abort_events)
    page.goto(f"{visual_server.url}/jobs")
    expect(page.locator("#connection-status")).to_contain_text("disconnected")
    _snapshot(page, "jobs-disconnected.png", update=update_snapshots)
