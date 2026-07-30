from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from paper_pipeline.ingest.plan import ImportPlan, PlannedImport
from paper_pipeline.jobs.model import Job, JobKind, JobScope, JobState
from paper_pipeline.jobs.queue import CancellationToken
from paper_pipeline.library.model import (
    ConversionRecord,
    PaperMetadata,
    PaperRecord,
    RecipeRecord,
)
from paper_pipeline.library.storage import sha256_file
from paper_pipeline.library.validation import validate_library
from paper_pipeline.services.import_ops import apply_import, preview_import
from paper_pipeline.services.runtime import (
    LibraryRuntime,
    LibrarySession,
    PaperSession,
    RuntimeRegistry,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "zotero"


def planned(
    tmp_path: Path,
    citekey: str,
    *,
    title: str = "Title",
    body: bytes = b"pdf",
    expected_source_sha256: str | None = None,
) -> PlannedImport:
    attachment = tmp_path / f"{citekey}-{hashlib.sha256(body).hexdigest()[:8]}.pdf"
    attachment.write_bytes(body)
    return PlannedImport(
        metadata=PaperMetadata(citekey=citekey, title=title),
        attachment_path=attachment,
        attachment_sha256=hashlib.sha256(body).hexdigest(),
        expected_source_sha256=expected_source_sha256,
    )


async def read_papers(runtime: LibraryRuntime, *citekeys: str) -> dict[str, PaperRecord]:
    records: dict[str, PaperRecord] = {}

    async def worker(session: LibrarySession, job: Job, token: CancellationToken) -> None:
        del job, token
        records.update(
            session.inspect(
                lambda library: {citekey: library.read_paper(citekey) for citekey in citekeys}
            )
        )

    job = await runtime.enqueue_library_read(JobKind.MAINTENANCE, "read", worker)
    assert (await runtime.queue.wait(job.id)).state is JobState.SUCCEEDED
    return records


async def read(runtime: LibraryRuntime, citekey: str) -> PaperRecord:
    return (await read_papers(runtime, citekey))[citekey]


async def seed(runtime: LibraryRuntime, record: PaperRecord) -> None:
    async def worker(session: PaperSession, job: Job, token: CancellationToken) -> None:
        del job, token
        session.write_record(record)

    job = await runtime.enqueue_paper(record.metadata.citekey, JobKind.IMPORT, "seed", worker)
    assert (await runtime.queue.wait(job.id)).state is JobState.SUCCEEDED


async def test_preview_returns_an_actionable_plan_through_a_library_read(
    tmp_path: Path,
) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")

    plan = await preview_import(runtime, FIXTURES / "clean")

    assert any(item.metadata.citekey == "SmithJournal2024" for item in plan.additions)
    preview_job = runtime.queue.list_jobs()[-1]
    assert preview_job.kind is JobKind.IMPORT
    assert preview_job.scope is JobScope.LIBRARY_READ


async def test_import_is_additive_and_reapplying_a_plan_is_idempotent(
    tmp_path: Path,
) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")
    first = planned(tmp_path, "First2024", body=b"first")
    second = planned(tmp_path, "Second2024", body=b"second")

    assert (await apply_import(runtime, ImportPlan(additions=[first]))).ok
    first_before = await read(runtime, "First2024")
    assert (await apply_import(runtime, ImportPlan(additions=[second]))).ok
    before_replay = await read_papers(runtime, "First2024", "Second2024")

    replay = await apply_import(runtime, ImportPlan(additions=[second]))

    after_replay = await read_papers(runtime, "First2024", "Second2024")
    assert replay.ok
    assert before_replay["First2024"] == first_before
    assert after_replay == before_replay
    for citekey, expected_bytes in (
        ("First2024", b"first"),
        ("Second2024", b"second"),
    ):
        record = after_replay[citekey]
        assert record.source_pdf is not None
        assert (runtime.root / record.source_pdf).read_bytes() == expected_bytes
        assert len(list((runtime.root / "papers" / citekey / "source").glob("*.pdf"))) == 1


async def test_refresh_preserves_provenance_then_replacement_stales_outputs(
    tmp_path: Path,
) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")
    original = planned(tmp_path, "Changed2024", title="Old", body=b"old source")
    assert (await apply_import(runtime, ImportPlan(additions=[original]))).ok
    record = await read(runtime, "Changed2024")
    original_source_path = runtime.root / (record.source_pdf or "")

    transcription = runtime.root / "papers" / "Changed2024" / "transcription.md"
    transcription.write_text("existing transcription", encoding="utf-8")
    summary = runtime.root / "papers" / "Changed2024" / "summary.md"
    summary.write_text("existing summary", encoding="utf-8")
    record.conversion = ConversionRecord(
        source_sha256=record.source_sha256,
        transcription_sha256=sha256_file(transcription),
        backend="fake",
    )
    record.recipes["summary"] = RecipeRecord(
        input_artifact="papers/Changed2024/transcription.md",
        input_sha256=record.conversion.transcription_sha256,
        output_artifact="papers/Changed2024/summary.md",
        output_sha256=sha256_file(summary),
    )
    await seed(runtime, record)

    refresh = planned(
        tmp_path,
        "Changed2024",
        title="Corrected",
        body=b"old source",
        expected_source_sha256=original.attachment_sha256,
    )
    assert (await apply_import(runtime, ImportPlan(refreshes=[refresh]))).ok

    refreshed = await read(runtime, "Changed2024")
    assert refreshed.metadata.title == "Corrected"
    assert refreshed.source_pdf == record.source_pdf
    assert refreshed.source_sha256 == record.source_sha256
    assert refreshed.conversion == record.conversion
    assert refreshed.recipes == record.recipes

    replacement = planned(
        tmp_path,
        "Changed2024",
        title="Corrected",
        body=b"new source",
        expected_source_sha256=original.attachment_sha256,
    )
    assert (await apply_import(runtime, ImportPlan(source_replacements=[replacement]))).ok

    replaced = await read(runtime, "Changed2024")
    assert replaced.source_sha256 == replacement.attachment_sha256
    assert replaced.source_pdf is not None
    assert (runtime.root / replaced.source_pdf).read_bytes() == b"new source"
    assert replaced.conversion == record.conversion
    assert replaced.conversion.source_sha256 != replaced.source_sha256
    assert replaced.recipes == record.recipes
    assert transcription.read_text(encoding="utf-8") == "existing transcription"
    assert summary.read_text(encoding="utf-8") == "existing summary"
    assert original_source_path.read_bytes() == b"old source"


async def test_failed_copy_leaves_no_partial_paper(tmp_path: Path) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")
    item = planned(tmp_path, "Broken2024")
    assert item.attachment_path is not None
    item.attachment_path.unlink()

    report = await apply_import(runtime, ImportPlan(additions=[item]))

    assert not report.ok
    assert "Broken2024" in report.failed
    assert not (runtime.root / "papers" / "Broken2024").exists()


async def test_interruption_after_source_install_leaves_valid_metadata_only_paper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")
    item = planned(tmp_path, "Interrupted2024")
    original_install = PaperSession.install_artifact

    def install_then_interrupt(self: PaperSession, staged_path: Path, destination: str) -> str:
        original_install(self, staged_path, destination)
        raise OSError("simulated interruption after source installation")

    monkeypatch.setattr(PaperSession, "install_artifact", install_then_interrupt)

    report = await apply_import(runtime, ImportPlan(additions=[item]))

    assert not report.ok
    assert "Interrupted2024" in report.failed
    assert validate_library(runtime.root).ok
    record = await read(runtime, "Interrupted2024")
    assert record.source_pdf is None
    assert record.source_sha256 is None


async def test_import_waits_for_the_existing_paper_lane_owner(tmp_path: Path) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")
    initial = planned(tmp_path, "Lane2024", title="Old")
    assert (await apply_import(runtime, ImportPlan(additions=[initial]))).ok
    started = asyncio.Event()
    release = asyncio.Event()

    async def conversion(session: PaperSession, job: Job, token: CancellationToken) -> None:
        del session, job, token
        started.set()
        await release.wait()

    await runtime.enqueue_paper("Lane2024", JobKind.CONVERSION, "convert", conversion)
    await started.wait()
    refresh = planned(
        tmp_path,
        "Lane2024",
        title="New",
        expected_source_sha256=initial.attachment_sha256,
    )
    applying = asyncio.create_task(apply_import(runtime, ImportPlan(refreshes=[refresh])))
    try:
        await asyncio.sleep(0)
        assert not applying.done()
        assert (await read(runtime, "Lane2024")).metadata.title == "Old"
    finally:
        release.set()

    report = await applying
    assert report.ok
    assert (await read(runtime, "Lane2024")).metadata.title == "New"


async def test_stale_replacement_cannot_overwrite_an_intervening_source(
    tmp_path: Path,
) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")
    original = planned(tmp_path, "Cas2024", body=b"original")
    assert (await apply_import(runtime, ImportPlan(additions=[original]))).ok
    stale = planned(
        tmp_path,
        "Cas2024",
        body=b"stale proposal",
        expected_source_sha256=original.attachment_sha256,
    )
    intervening = planned(
        tmp_path,
        "Cas2024",
        body=b"intervening",
        expected_source_sha256=original.attachment_sha256,
    )
    assert (await apply_import(runtime, ImportPlan(source_replacements=[intervening]))).ok

    rejected = await apply_import(runtime, ImportPlan(source_replacements=[stale]))

    assert not rejected.ok
    assert "Cas2024" in rejected.failed
    current = await read(runtime, "Cas2024")
    assert current.source_sha256 == intervening.attachment_sha256
    assert current.source_pdf is not None
    assert (runtime.root / current.source_pdf).read_bytes() == b"intervening"
